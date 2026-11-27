"""Community music-station settings/providers/radio-stations -- ported from `musicController.js`.

Only the 8 handlers `musicController.js` actually exports are ported here
(`getMusicSettings`, `updateMusicSettings`, `getProviders`, `startOAuth`,
`disconnectProvider`, `getRadioStations`, `addRadioStation`,
`removeRadioStation`) -- `routes/music.js` additionally wires
`updateProviderConfig`/`testRadioStation`/`setDefaultRadioStation`/
`getMusicDashboard`/`controlPlayback` to this same controller module, but
none of those five functions exist as exports in `musicController.js`
(confirmed via `grep -n "^export .*function"` against the file); Node's
`routes/music.js` would throw `TypeError: ... is not a function` at
import time if that route file were ever mounted -- and it never is
(`routes/index.js` has no `import musicRoutes from './music.js'` /
`router.use(..., musicRoutes)` anywhere; confirmed via repo-wide grep).
`frontend/src/services/api.js`'s `updateMusicProviderConfig`/
`testRadioStreamUrl`/`setDefaultRadioStation`/`getMusicDashboard`/
`controlPlayback` calls had NO real backing implementation in the Node
app. Three of them are now implemented natively here against the same
`community_music_providers`/`community_radio_stations` tables:
`update_provider_config` (enable/disable a connected provider),
`probe_radio_station` (header-only SSRF-guarded reachability probe) and
`set_default_radio_station` (one default per community, enforced by
0145's partial unique index). Dashboard/playback remain unserved.

Schema: the tables this module queries are created by alembic
`0012_schema_drift_columns` (plus `0138` public ids and `0145` default /
last-tested columns) and bound for pydal by
`services.schema.bind_streaming_tables()`.

Authorization: every function here is called only after
`services.community_authz.authorize_community(..., admin=True)` --
Node's `requireCommunityAdmin` (`routes/music.js`: `router.use(requireAuth)`
+ per-route `requireCommunityAdmin`). No route in this group is
self-service; `community:manage_channels` (which the seeded `moderator`
role bundle carries -- `058_tenants_and_claims.sql`) is sufficient, not
just full `community-admin`, matching the M7 port instruction's
"moderator scope" requirement for music-station admin actions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from opentelemetry import metrics, trace
from sqlalchemy.exc import IntegrityError

from services.community_public_id import lookup_public_id
from services.errors import ApiError, bad_request, conflict, not_found
from services.url_guard import guarded_probe, validate_outbound_url

logger = logging.getLogger(__name__)

_VALID_PROVIDERS: frozenset[str] = frozenset({"spotify", "soundcloud", "youtube"})
_STATE_TOKEN_TTL_MINUTES = 15

#: Reachability probe budget: connect + first-byte, never the (endless) stream body.
_PROBE_TIMEOUT_SECONDS = 5.0

#: Provider `config` keys never echoed to a client -- credentials are write-only.
_SECRET_CONFIG_MARKERS: tuple[str, ...] = ("token", "secret", "password", "credential", "key")

_tracer = trace.get_tracer("waddles.hub_api.music")
_meter = metrics.get_meter("waddles.hub_api.music")
_ADMIN_ACTIONS = _meter.create_counter(
    "waddles.music.admin_actions",
    description="Music admin actions by action and outcome",
)
_PROBE_MS = _meter.create_histogram(
    "waddles.music.radio_probe_duration_ms",
    unit="ms",
    description="Radio stream reachability probe latency",
)


@dataclass(slots=True, frozen=True)
class MusicSettingsDTO:
    """Wire DTO for one community's music settings -- camelCase pinned to `api.js`."""

    id: int
    communityId: str
    defaultProvider: str | None
    autoplayEnabled: bool
    volumeLimit: int
    allowedGenres: list[str]
    blockedArtists: list[str]
    requireDjApproval: bool
    isActive: bool
    createdAt: str | None
    updatedAt: str | None


@dataclass(slots=True, frozen=True)
class MusicProviderDTO:
    """Wire DTO for one music provider -- keyed by `providerName`; no numeric id, no credentials."""

    communityId: str
    providerName: str
    isConnected: bool
    isActive: bool
    oauthExpiresAt: str | None
    lastSync: str | None
    config: dict[str, Any]
    createdAt: str | None
    updatedAt: str | None


@dataclass(slots=True, frozen=True)
class RadioStationDTO:
    """Wire DTO for one community radio station; `id` is its public UUID."""

    id: str
    communityId: str
    name: str
    url: str
    description: str | None
    genre: str | None
    isActive: bool
    isDefault: bool
    lastTestedAt: str | None
    createdAt: str | None
    updatedAt: str | None


@dataclass(slots=True, frozen=True)
class RadioProbeResult:
    """Outcome of one radio stream reachability probe."""

    reachable: bool
    httpStatus: int | None
    contentType: str | None
    testedAt: str


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _settings_dto(row: Any, public_community_id: str) -> MusicSettingsDTO:
    """Wire DTO; `communityId` is the community's PUBLIC UUID (never the numeric id)."""
    return MusicSettingsDTO(
        id=row.id,
        communityId=public_community_id,
        defaultProvider=row.default_provider,
        autoplayEnabled=bool(row.autoplay_enabled),
        volumeLimit=row.volume_limit,
        allowedGenres=list(row.allowed_genres or []),
        blockedArtists=list(row.blocked_artists or []),
        requireDjApproval=bool(row.require_dj_approval),
        isActive=bool(row.is_active),
        createdAt=_iso(row.created_at),
        updatedAt=_iso(row.updated_at),
    )


def _parse_provider_config(raw: Any) -> dict[str, Any]:
    """Port of Node's `row.config ? JSON.parse(row.config) : {}` -- `config` is a TEXT column."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("music provider config is not valid JSON; returning empty config")
        return {}
    if not isinstance(parsed, dict):
        logger.warning("music provider config is not a JSON object; returning empty config")
        return {}
    # Credentials are write-only: never echo a secret-looking key back out.
    return {
        k: v
        for k, v in parsed.items()
        if not any(marker in str(k).lower() for marker in _SECRET_CONFIG_MARKERS)
    }


def _provider_dto(row: Any, public_community_id: str) -> MusicProviderDTO:
    """Wire DTO; `communityId` is the community's PUBLIC UUID (never the numeric id)."""
    return MusicProviderDTO(
        communityId=public_community_id,
        providerName=row.provider_name,
        isConnected=bool(row.is_connected),
        isActive=bool(row.is_active),
        oauthExpiresAt=_iso(row.oauth_expires_at),
        lastSync=_iso(row.last_sync),
        config=_parse_provider_config(row.config),
        createdAt=_iso(row.created_at),
        updatedAt=_iso(row.updated_at),
    )


def _station_dto(row: Any, public_community_id: str) -> RadioStationDTO:
    """Wire DTO; `communityId` is the community's PUBLIC UUID (never the numeric id)."""
    return RadioStationDTO(
        id=str(row.public_id),
        communityId=public_community_id,
        name=row.name,
        url=row.url,
        description=row.description,
        genre=row.genre,
        isActive=bool(row.is_active),
        isDefault=bool(row.is_default),
        lastTestedAt=_iso(row.last_tested_at),
        createdAt=_iso(row.created_at),
        updatedAt=_iso(row.updated_at),
    )


async def get_music_settings(async_dal: Any, dal: Any, *, community_id: int) -> MusicSettingsDTO:
    """Port of `getMusicSettings` -- raises 404 if the community has no settings row."""
    rows = await async_dal.select_async(
        dal(dal.community_music_settings.community_id == community_id)
    )
    if not rows:
        raise not_found("Music settings not found")
    return _settings_dto(rows.first(), await lookup_public_id(community_id))


async def update_music_settings(
    async_dal: Any, dal: Any, *, community_id: int, fields: dict[str, Any]
) -> MusicSettingsDTO:
    """Port of `updateMusicSettings` -- only the fields Node accepts, byte-faithful clamping."""
    update_fields: dict[str, Any] = {}

    if "defaultProvider" in fields and fields["defaultProvider"] is not None:
        update_fields["default_provider"] = fields["defaultProvider"]
    if isinstance(fields.get("autoplayEnabled"), bool):
        update_fields["autoplay_enabled"] = fields["autoplayEnabled"]
    if fields.get("volumeLimit") is not None:
        update_fields["volume_limit"] = min(100, max(0, int(fields["volumeLimit"])))
    if fields.get("allowedGenres") is not None:
        value = fields["allowedGenres"]
        update_fields["allowed_genres"] = list(value) if isinstance(value, list) else []
    if fields.get("blockedArtists") is not None:
        value = fields["blockedArtists"]
        update_fields["blocked_artists"] = list(value) if isinstance(value, list) else []
    if isinstance(fields.get("requireDjApproval"), bool):
        update_fields["require_dj_approval"] = fields["requireDjApproval"]
    if isinstance(fields.get("isActive"), bool):
        update_fields["is_active"] = fields["isActive"]

    if not update_fields:
        raise bad_request("No fields to update")
    update_fields["updated_at"] = datetime.now(UTC)

    query = dal.community_music_settings.community_id == community_id
    # `update_async` self-wraps its `query` arg (`self.dal(query).update(...)`
    # internally) -- passing an already-`dal(query)`-wrapped Set here is
    # the exact bug hub_api/PORTING.md Gotcha #1 warns against; only
    # `select_async` wants the pre-wrapped form.
    updated = await async_dal.update_async(query, **update_fields)
    if not updated:
        raise not_found("Music settings not found")

    rows = await async_dal.select_async(dal(query))
    return _settings_dto(rows.first(), await lookup_public_id(community_id))


async def get_providers(async_dal: Any, dal: Any, *, community_id: int) -> list[MusicProviderDTO]:
    """Port of `getProviders` -- ordered by `provider_name ASC`, matching Node's `ORDER BY`."""
    rows = await async_dal.select_async(
        dal(dal.community_music_providers.community_id == community_id),
        orderby=dal.community_music_providers.provider_name,
    )
    public_id = await lookup_public_id(community_id)
    return [_provider_dto(row, public_id) for row in rows]


def _build_auth_url(provider: str, state_token: str, community_id: int) -> str:
    """Port of `buildSpotifyAuthUrl`/`buildSoundCloudAuthUrl`/`buildYouTubeAuthUrl`.

    Client IDs/redirect URIs are read at call time (not module import) so
    tests can set env vars per-case without reload games; `os.getenv`
    matches Node's `process.env.*` lookup exactly, including the `None` ->
    empty-string-in-URL behavior Node's own template literals produce for
    an unset `*_CLIENT_ID`.
    """
    import os

    state_q = quote(state_token)
    community_q = quote(str(community_id))

    if provider == "spotify":
        client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
        redirect_uri = quote(
            os.getenv("SPOTIFY_REDIRECT_URI", "https://api.waddlebot.io/oauth/spotify/callback")
        )
        scope = quote("playlist-read-private playlist-read-collaborative")
        return (
            f"https://accounts.spotify.com/authorize?client_id={client_id}"
            f"&response_type=code&redirect_uri={redirect_uri}&scope={scope}"
            f"&state={state_q}&community_id={community_q}"
        )
    if provider == "soundcloud":
        client_id = os.getenv("SOUNDCLOUD_CLIENT_ID", "")
        redirect_uri = quote(
            os.getenv(
                "SOUNDCLOUD_REDIRECT_URI", "https://api.waddlebot.io/oauth/soundcloud/callback"
            )
        )
        return (
            f"https://soundcloud.com/oauth/authorize?client_id={client_id}"
            f"&response_type=code&redirect_uri={redirect_uri}&scope=non-expiring"
            f"&state={state_q}&community_id={community_q}"
        )
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "")
    redirect_uri = quote(
        os.getenv("YOUTUBE_REDIRECT_URI", "https://api.waddlebot.io/oauth/youtube/callback")
    )
    scope = quote("https://www.googleapis.com/auth/youtube.readonly")
    return (
        f"https://accounts.google.com/o/oauth2/v2/auth?client_id={client_id}"
        f"&response_type=code&redirect_uri={redirect_uri}&scope={scope}"
        f"&state={state_q}&community_id={community_q}"
    )


async def start_oauth(
    async_dal: Any, dal: Any, *, community_id: int, provider: str, redirect_uri: str
) -> tuple[str, str]:
    """Port of `startOAuth` -- returns `(auth_url, state_token)`.

    `redirect_uri` is validated as a safe outbound URL (SSRF hardening --
    Node only checked truthiness) since it is round-tripped back to this
    service's own OAuth callback and echoed into the provider redirect;
    an attacker-controlled scheme/host here is exactly the "forward-target
    URL" class the M7 port instruction calls out.
    """
    if not redirect_uri:
        raise bad_request("redirectUri is required")
    await validate_outbound_url(redirect_uri, allowed_schemes=("http", "https"))

    provider_key = provider.lower()
    if provider_key not in _VALID_PROVIDERS:
        raise bad_request(f"Unsupported provider: {provider}")

    state_token = secrets.token_hex(32)
    expires_at = datetime.now(UTC) + timedelta(minutes=_STATE_TOKEN_TTL_MINUTES)
    await async_dal.insert_async(
        dal.oauth_state_tokens,
        community_id=community_id,
        provider=provider_key,
        state_token=state_token,
        redirect_uri=redirect_uri,
        expires_at=expires_at,
        created_at=datetime.now(UTC),
    )

    auth_url = _build_auth_url(provider_key, state_token, community_id)
    return auth_url, state_token


async def disconnect_provider(
    async_dal: Any, dal: Any, *, community_id: int, provider: str
) -> None:
    """Port of `disconnectProvider` -- flips connected/active off, clears stored tokens."""
    query = (dal.community_music_providers.community_id == community_id) & (
        dal.community_music_providers.provider_name == provider
    )
    updated = await async_dal.update_async(
        query,
        is_connected=False,
        is_active=False,
        updated_at=datetime.now(UTC),
    )
    if not updated:
        raise not_found("Provider not found")

    # Also clear stored OAuth tokens (Node: `DELETE FROM oauth_tokens
    # WHERE community_id = $1 AND provider = $2`) -- pydal query builder,
    # not raw SQL (hub_api/PORTING.md Gotcha #1).
    token_query = (dal.oauth_tokens.community_id == community_id) & (
        dal.oauth_tokens.provider == provider
    )
    await async_dal.delete_async(token_query)


async def get_radio_stations(
    async_dal: Any, dal: Any, *, community_id: int, page: int, limit: int
) -> tuple[list[RadioStationDTO], int, int, int]:
    """Port of `getRadioStations` -- returns `(stations, page, limit, total)`."""
    page = max(1, page)
    limit = min(100, max(1, limit))
    offset = (page - 1) * limit

    query = dal.community_radio_stations.community_id == community_id
    total = await async_dal.count_async(query)
    rows = await async_dal.select_async(
        dal(query),
        orderby=~dal.community_radio_stations.created_at,
        limitby=(offset, offset + limit),
    )
    public_id = await lookup_public_id(community_id)
    return [_station_dto(row, public_id) for row in rows], page, limit, total


async def add_radio_station(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    name: str,
    url: str,
    description: str | None,
    genre: str | None,
    is_active: bool,
    created_by: int | None,
) -> RadioStationDTO:
    """Port of `addRadioStation` -- SSRF-hardened URL check replaces Node's format-only check."""
    if not name or not url:
        raise bad_request("name and url are required")
    await validate_outbound_url(url, allowed_schemes=("http", "https"))

    now = datetime.now(UTC)
    new_id = await async_dal.insert_async(
        dal.community_radio_stations,
        community_id=community_id,
        name=name[:255],
        url=url,
        description=description[:1000] if description else None,
        genre=genre[:100] if genre else None,
        is_active=is_active,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    rows = await async_dal.select_async(dal(dal.community_radio_stations.id == new_id))
    return _station_dto(rows.first(), await lookup_public_id(community_id))


async def remove_radio_station(
    async_dal: Any, dal: Any, *, community_id: int, station_id: int
) -> None:
    """Port of `removeRadioStation`."""
    query = (dal.community_radio_stations.id == station_id) & (
        dal.community_radio_stations.community_id == community_id
    )
    deleted = await async_dal.delete_async(query)
    if not deleted:
        raise not_found("Radio station not found")


def _record(action: str, outcome: str) -> None:
    """Count one music admin action (bounded labels: fixed action names, fixed outcomes)."""
    _ADMIN_ACTIONS.add(1, {"action": action, "outcome": outcome})


async def update_provider_config(
    async_dal: Any, dal: Any, *, community_id: int, provider: str, enabled: bool
) -> MusicProviderDTO:
    """Enable or disable one provider for a community; only a CONNECTED provider can be enabled.

    Disabling keeps the OAuth connection (unlike `disconnect_provider`) so music requests
    stop routing to it without forcing a reconnect later.
    """
    provider_key = provider.lower()
    if provider_key not in _VALID_PROVIDERS:
        _record("provider_config", "invalid")
        raise bad_request(f"Unsupported provider: {provider}")

    with _tracer.start_as_current_span("music.provider_config") as span:
        span.set_attribute("music.provider", provider_key)
        span.set_attribute("music.enabled", enabled)
        query = (dal.community_music_providers.community_id == community_id) & (
            dal.community_music_providers.provider_name == provider_key
        )
        rows = await async_dal.select_async(dal(query))
        row = rows.first() if rows else None
        if row is None:
            _record("provider_config", "not_found")
            raise not_found("Provider not found")
        if enabled and not row.is_connected:
            _record("provider_config", "not_connected")
            raise conflict("Connect the provider before enabling it")

        await async_dal.update_async(query, is_active=enabled, updated_at=datetime.now(UTC))
        refreshed = (await async_dal.select_async(dal(query))).first()
        _record("provider_config", "ok")
        logger.info("music provider %s set enabled=%s", provider_key, enabled)
        return _provider_dto(refreshed, await lookup_public_id(community_id))


async def _station_row(async_dal: Any, dal: Any, *, community_id: int, station_id: int) -> Any:
    """Load one station inside `community_id` or raise 404 (never another community's row)."""
    query = (dal.community_radio_stations.id == station_id) & (
        dal.community_radio_stations.community_id == community_id
    )
    rows = await async_dal.select_async(dal(query))
    row = rows.first() if rows else None
    if row is None:
        raise not_found("Radio station not found")
    return row


async def probe_radio_station(
    async_dal: Any, dal: Any, *, community_id: int, station_id: int
) -> RadioProbeResult:
    """Probe a station's stream URL (headers only) and stamp `last_tested_at`.

    A transport failure or non-2xx status is a normal `reachable=False` result, not an
    error. A URL that now resolves into a blocked range raises 400 (SSRF guard). The
    stream URL itself is never logged -- it may carry a listener key in its query.
    """
    row = await _station_row(async_dal, dal, community_id=community_id, station_id=station_id)
    status: int | None = None
    content_type: str | None = None
    with _tracer.start_as_current_span("music.radio_probe") as span:
        span.set_attribute("music.station_id", str(row.public_id))
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=_PROBE_TIMEOUT_SECONDS
            ) as client:
                response = await guarded_probe(client, row.url)
            status = response.status_code
            content_type = response.headers.get("content-type")
        except httpx.HTTPError as exc:
            logger.info(
                "radio probe transport failure station=%s error=%s",
                row.public_id,
                type(exc).__name__,
            )
        except ApiError:
            _record("radio_test", "blocked")
            raise
        finally:
            _PROBE_MS.record((time.perf_counter() - started) * 1000.0)
        reachable = status is not None and 200 <= status < 300
        span.set_attribute("music.reachable", reachable)
        if status is not None:
            span.set_attribute("http.response.status_code", status)

    now = datetime.now(UTC)
    await async_dal.update_async(dal.community_radio_stations.id == row.id, last_tested_at=now)
    _record("radio_test", "reachable" if reachable else "unreachable")
    logger.info("radio probe station=%s reachable=%s status=%s", row.public_id, reachable, status)
    return RadioProbeResult(
        reachable=reachable,
        httpStatus=status,
        contentType=content_type[:255] if content_type else None,
        testedAt=now.isoformat(),
    )


def _sync_set_default(dal: Any, *, community_id: int, station_id: int, now: datetime) -> None:
    """Single executor job: clear the community's current default, set the new one, commit."""
    table = dal.community_radio_stations
    try:
        others = (
            (table.community_id == community_id)
            & (table.is_default == True)  # noqa: E712 -- pydal query, not a Python bool test
            & (table.id != station_id)
        )
        dal(others).update(is_default=False, updated_at=now)
        dal(table.id == station_id).update(is_default=True, updated_at=now)
        dal.commit()
    except Exception:
        dal.rollback()
        raise


async def set_default_radio_station(
    async_dal: Any, dal: Any, *, community_id: int, station_id: int
) -> RadioStationDTO:
    """Make one ACTIVE station the community's default; the old default is cleared atomically."""
    row = await _station_row(async_dal, dal, community_id=community_id, station_id=station_id)
    if not row.is_active:
        _record("radio_default", "inactive")
        raise conflict("Only an active station can be the default")

    with _tracer.start_as_current_span("music.radio_set_default") as span:
        span.set_attribute("music.station_id", str(row.public_id))
        now = datetime.now(UTC)
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                async_dal.executor,
                lambda: _sync_set_default(
                    dal, community_id=community_id, station_id=row.id, now=now
                ),
            )
        except IntegrityError as exc:
            # 0145's one-default-per-community index lost a concurrent race.
            _record("radio_default", "conflict")
            logger.warning("radio set-default race station=%s: %s", row.public_id, exc)
            raise conflict("Another default station was set concurrently; retry") from exc

    _record("radio_default", "ok")
    logger.info("radio default set station=%s", row.public_id)
    refreshed = await _station_row(async_dal, dal, community_id=community_id, station_id=row.id)
    return _station_dto(refreshed, await lookup_public_id(community_id))
