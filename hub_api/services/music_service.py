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
`controlPlayback` calls are therefore contract entries with NO real
backing implementation anywhere in the Node app today -- out of scope for
a byte-faithful controller port (`hub_api/PORTING.md`'s "port the
EXISTING controller endpoints, don't build new features"); a follow-up
ticket should implement these five for real against a chosen schema (see
`services/schema.py::bind_streaming_tables()`'s docstring for the
`community_music_settings`-family-vs-`music_provider_config`-family
schema-gap discussion) rather than have this port invent throwaway logic.

Schema gap: every table this module queries
(`community_music_settings`, `community_music_providers`,
`community_radio_stations`, `oauth_state_tokens`, `oauth_tokens`) is
bound by `services.schema.bind_streaming_tables()` but does not exist in
any real migration -- see that function's docstring for the full
gap analysis (`hub_api/PORTING.md` Gotcha #4's pattern). Every function
below is byte-faithful to Node's SQL regardless.

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

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from services.community_public_id import lookup_public_id
from services.errors import bad_request, not_found
from services.url_guard import validate_outbound_url

_VALID_PROVIDERS: frozenset[str] = frozenset({"spotify", "soundcloud", "youtube"})
_STATE_TOKEN_TTL_MINUTES = 15


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
    """Wire DTO for one connected music provider."""

    id: int
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
    """Wire DTO for one community radio station."""

    id: int
    communityId: str
    name: str
    url: str
    description: str | None
    genre: str | None
    isActive: bool
    createdBy: int | None
    createdAt: str | None
    updatedAt: str | None


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
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _provider_dto(row: Any, public_community_id: str) -> MusicProviderDTO:
    """Wire DTO; `communityId` is the community's PUBLIC UUID (never the numeric id)."""
    return MusicProviderDTO(
        id=row.id,
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
        id=row.id,
        communityId=public_community_id,
        name=row.name,
        url=row.url,
        description=row.description,
        genre=row.genre,
        isActive=bool(row.is_active),
        createdBy=row.created_by,
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
