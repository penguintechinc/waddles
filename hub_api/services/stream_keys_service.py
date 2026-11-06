"""Stream-key and restream-destination management for a community (svc-streaming's tables).

Security model (see alembic 0076):

- The community's inbound RTMP/SRT/WHIP stream key is generated here with a CSPRNG, only its
  SHA-256 is stored (`streaming_configs.stream_key_hash`; svc-streaming's ingest-auth hashes the
  presented key and matches it), and the plaintext is returned exactly once by `create_config` /
  `rotate_key` -- never by a read path, never logged. Rotation overwrites the hash, so the old key
  stops authorizing the instant the UPDATE commits.
- Third-party destination credentials (the RTMP URL and the platform stream key) are sealed with
  AES-256-GCM, AAD-bound to `(config_id, public_id)`, and never decrypted by any API read; the
  list/get DTOs expose only a redacted `scheme://host[:port]`.
- Every lookup is scoped `community_id -> config -> destination`; a destination id belonging to
  another community's config is indistinguishable from a missing one (404). The caller has
  already been tenant-checked against `communities.tenant_id` by `authorize_community`.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from services.errors import bad_request, conflict, not_found
from services.platform_integrations_crypto import encrypt_token, stream_destination_aad

log = logging.getLogger(__name__)

_KEY_PREFIX = "wbsk_"
_KEY_BYTES = 32
VALID_PLATFORMS: frozenset[str] = frozenset({"twitch", "youtube", "facebook", "kick", "custom"})
VALID_RESOLUTIONS: frozenset[str] = frozenset({"720p", "1080p", "1440p", "2160p"})
MAX_DESTINATIONS_PER_CONFIG = 10
_MAX_URL_LEN = 1024
_MAX_KEY_LEN = 500
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: Codecs svc-streaming's FLV/RTMP egress cannot carry (`spec_builder.rs` rule) -- rejected loudly.
_FLV_UNSUPPORTED_CODECS: frozenset[str] = frozenset({"h265", "hevc", "av1"})
FLV_CODEC_MESSAGE = (
    "FLV/RTMP cannot carry HEVC/AV1 (needs enhanced-RTMP); use H.264 for RTMP destinations"
)


@dataclass(slots=True, frozen=True)
class StreamConfigDTO:
    """Wire DTO for a community's stream config -- never includes the key or its hash."""

    enabled: bool
    ingest_url: str | None
    stream_key_set: bool
    stream_key_fingerprint: str | None
    stream_key_rotated_at: str | None
    video_codec: str
    audio_codec: str
    created_at: str | None
    updated_at: str | None


@dataclass(slots=True, frozen=True)
class StreamDestinationDTO:
    """Wire DTO for one restream destination -- URL redacted to scheme://host, key never sent."""

    id: str
    platform: str
    rtmp_url: str
    max_resolution: str
    video_codec: str
    enabled: bool
    force_cut: bool
    created_at: str | None


@dataclass(slots=True, frozen=True)
class StreamStatusDTO:
    """Wire DTO for live status derived from svc-streaming's `streaming_sessions`."""

    is_streaming: bool
    started_at: str | None
    transcode_applied: bool
    destinations_enabled: int


@dataclass(slots=True, frozen=True)
class DestinationInput:
    """Validated input for `add_destination` (plaintext only until sealed)."""

    platform: str
    rtmp_url: str
    stream_key: str
    max_resolution: str
    enabled: bool
    force_cut: bool


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def generate_stream_key() -> str:
    """Return a fresh 256-bit CSPRNG stream key (`wbsk_` + 43 URL-safe chars)."""
    return _KEY_PREFIX + secrets.token_urlsafe(_KEY_BYTES)


def hash_stream_key(key: str) -> str:
    """Hex SHA-256 of `key` -- the only form persisted; identical to svc-streaming's lookup."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def redact_rtmp_url(url: str) -> str:
    """Reduce an RTMP URL to `scheme://host[:port]` -- path/query/userinfo can carry the key."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme.lower()}://{host}{port}"[:300]


def _config_dto(row: Any, *, ingest_url: str | None) -> StreamConfigDTO:
    return StreamConfigDTO(
        enabled=bool(row.enabled),
        ingest_url=ingest_url,
        stream_key_set=bool(row.stream_key_hash),
        stream_key_fingerprint=row.stream_key_fingerprint,
        stream_key_rotated_at=_iso(row.stream_key_rotated_at),
        video_codec=row.video_codec,
        audio_codec=row.audio_codec,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _destination_dto(row: Any) -> StreamDestinationDTO:
    return StreamDestinationDTO(
        id=row.public_id,
        platform=row.platform,
        rtmp_url=row.display_url,
        max_resolution=row.max_resolution,
        video_codec=row.video_codec,
        enabled=bool(row.enabled),
        force_cut=bool(row.force_cut),
        created_at=_iso(row.created_at),
    )


async def _config_row(async_dal: Any, dal: Any, community_id: int) -> Any | None:
    rows = await async_dal.select_async(
        dal(dal.streaming_configs.community_id == community_id),
        dal.streaming_configs.ALL,
    )
    return rows.first() if rows else None


async def _require_config(async_dal: Any, dal: Any, community_id: int) -> Any:
    row = await _config_row(async_dal, dal, community_id)
    if row is None:
        raise not_found("Stream configuration not found")
    return row


async def get_config(
    async_dal: Any, dal: Any, *, community_id: int, ingest_url: str | None
) -> StreamConfigDTO | None:
    """Return the community's config DTO, or `None` when none exists (webui shows setup CTA)."""
    row = await _config_row(async_dal, dal, community_id)
    return _config_dto(row, ingest_url=ingest_url) if row else None


async def create_config(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    enabled: bool,
    ingest_url: str | None,
) -> tuple[StreamConfigDTO, str]:
    """Create the community's config and mint its first key; returns `(dto, plaintext_key)`.

    The plaintext is the caller's one chance to show it. 409 if a config already exists.
    """
    if await _config_row(async_dal, dal, community_id) is not None:
        raise conflict("Stream configuration already exists")
    key = generate_stream_key()
    digest = hash_stream_key(key)
    now = _now()
    try:
        await async_dal.insert_async(
            dal.streaming_configs,
            community_id=community_id,
            source_url="",
            source_type="rtmp",
            enabled=enabled,
            stream_key_hash=digest,
            stream_key_fingerprint=digest[:8],
            stream_key_rotated_at=now,
            created_at=now,
            updated_at=now,
        )
    except Exception:
        # Lost a create race against the UNIQUE(community_id) -- report it as the conflict it is.
        if await _config_row(async_dal, dal, community_id) is not None:
            raise conflict("Stream configuration already exists") from None
        raise
    row = await _require_config(async_dal, dal, community_id)
    log.info("stream_config_created community_id=%s", community_id)
    return _config_dto(row, ingest_url=ingest_url), key


async def rotate_key(
    async_dal: Any, dal: Any, *, community_id: int, ingest_url: str | None
) -> tuple[StreamConfigDTO, str]:
    """Mint a new key and overwrite the stored hash; the previous key stops authorizing at once."""
    row = await _require_config(async_dal, dal, community_id)
    key = generate_stream_key()
    digest = hash_stream_key(key)
    now = _now()
    await async_dal.update_async(
        dal.streaming_configs.id == row.id,
        stream_key_hash=digest,
        stream_key_fingerprint=digest[:8],
        stream_key_rotated_at=now,
        updated_at=now,
    )
    fresh = await _require_config(async_dal, dal, community_id)
    log.warning("stream_key_rotated community_id=%s", community_id)
    return _config_dto(fresh, ingest_url=ingest_url), key


def validate_destination_input(body: dict[str, Any]) -> DestinationInput:
    """Validate + normalize a destination create body (snake_case, camelCase aliases accepted).

    Raises `ApiError` 400 on a missing/oversized field, unknown platform/resolution, non-rtmp(s)
    URL, or an HEVC/AV1 codec request (FLV cannot carry them -- svc-streaming's rule).
    """
    platform = str(body.get("platform") or "").lower()
    rtmp_url = body.get("rtmp_url", body.get("rtmpUrl"))
    stream_key = body.get("stream_key", body.get("streamKey"))
    if not platform or not isinstance(rtmp_url, str) or not isinstance(stream_key, str):
        raise bad_request("Missing required fields: platform, rtmp_url, stream_key")
    if platform not in VALID_PLATFORMS:
        raise bad_request("Invalid platform")
    rtmp_url = rtmp_url.strip()
    if not rtmp_url or len(rtmp_url) > _MAX_URL_LEN:
        raise bad_request("Invalid RTMP URL")
    if not stream_key or len(stream_key) > _MAX_KEY_LEN:
        raise bad_request("Invalid stream key")
    parsed = urlparse(rtmp_url)
    if parsed.scheme.lower() not in ("rtmp", "rtmps") or not parsed.hostname:
        raise bad_request("Invalid RTMP URL format")
    resolution = str(body.get("max_resolution", body.get("maxResolution")) or "1080p")
    if resolution not in VALID_RESOLUTIONS:
        raise bad_request("Invalid max_resolution")
    codec = body.get("video_codec", body.get("videoCodec"))
    if codec is not None:
        codec_key = str(codec).lower()
        if codec_key in _FLV_UNSUPPORTED_CODECS:
            raise bad_request(FLV_CODEC_MESSAGE)
        if codec_key != "h264":
            raise bad_request("Invalid video_codec")
    force_cut = body.get("force_cut", body.get("forceCut"))
    return DestinationInput(
        platform=platform,
        rtmp_url=rtmp_url,
        stream_key=stream_key,
        max_resolution=resolution,
        enabled=body.get("enabled") is not False,
        force_cut=force_cut is True,
    )


def parse_destination_id(raw: str) -> str:
    """Normalize a path `destination_id` to a lowercase UUID string, or raise 404."""
    candidate = raw.lower()
    if not _UUID_RE.match(candidate):
        raise not_found("Destination not found")
    return candidate


async def list_destinations(
    async_dal: Any, dal: Any, *, community_id: int
) -> list[StreamDestinationDTO]:
    """List the community's destinations (empty when it has no config)."""
    config = await _config_row(async_dal, dal, community_id)
    if config is None:
        return []
    rows = await async_dal.select_async(
        dal(dal.streaming_destinations.config_id == config.id),
        dal.streaming_destinations.ALL,
        orderby=dal.streaming_destinations.id,
    )
    return [_destination_dto(r) for r in rows]


async def add_destination(
    async_dal: Any, dal: Any, *, community_id: int, data: DestinationInput
) -> StreamDestinationDTO:
    """Seal and store a destination; 404 without a config, 409 past the per-config cap."""
    config = await _require_config(async_dal, dal, community_id)
    existing = await async_dal.select_async(
        dal(dal.streaming_destinations.config_id == config.id), dal.streaming_destinations.id
    )
    if len(existing) >= MAX_DESTINATIONS_PER_CONFIG:
        raise conflict(f"At most {MAX_DESTINATIONS_PER_CONFIG} destinations per community")
    public_id = str(uuid.uuid4())
    aad = stream_destination_aad(config_id=int(config.id), public_id=public_id)
    now = _now()
    await async_dal.insert_async(
        dal.streaming_destinations,
        public_id=public_id,
        config_id=config.id,
        platform=data.platform,
        display_url=redact_rtmp_url(data.rtmp_url),
        rtmp_url_enc=encrypt_token(data.rtmp_url, aad=aad),
        stream_key_enc=encrypt_token(data.stream_key, aad=aad),
        max_resolution=data.max_resolution,
        video_codec="h264",
        enabled=data.enabled,
        force_cut=data.force_cut,
        created_at=now,
        updated_at=now,
    )
    row = await _destination_row(async_dal, dal, config.id, public_id)
    log.info("stream_destination_added community_id=%s platform=%s", community_id, data.platform)
    return _destination_dto(row)


async def _destination_row(async_dal: Any, dal: Any, config_id: int, public_id: str) -> Any:
    rows = await async_dal.select_async(
        dal(
            (dal.streaming_destinations.config_id == config_id)
            & (dal.streaming_destinations.public_id == public_id)
        ),
        dal.streaming_destinations.ALL,
    )
    if not rows:
        raise not_found("Destination not found")
    return rows.first()


async def remove_destination(
    async_dal: Any, dal: Any, *, community_id: int, destination_id: str
) -> None:
    """Delete a destination; 404 if it is not under this community's config."""
    config = await _require_config(async_dal, dal, community_id)
    row = await _destination_row(async_dal, dal, config.id, parse_destination_id(destination_id))
    await async_dal.delete_async(dal.streaming_destinations.id == row.id)
    log.info("stream_destination_removed community_id=%s", community_id)


async def set_force_cut(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    destination_id: str,
    force_cut: bool | None,
) -> StreamDestinationDTO:
    """Set (or, when `force_cut` is None, toggle) a destination's force-cut flag."""
    config = await _require_config(async_dal, dal, community_id)
    public_id = parse_destination_id(destination_id)
    row = await _destination_row(async_dal, dal, config.id, public_id)
    target = (not bool(row.force_cut)) if force_cut is None else force_cut
    await async_dal.update_async(
        dal.streaming_destinations.id == row.id, force_cut=target, updated_at=_now()
    )
    return _destination_dto(await _destination_row(async_dal, dal, config.id, public_id))


async def get_status(async_dal: Any, dal: Any, *, community_id: int) -> StreamStatusDTO:
    """Live status from the newest `streaming_sessions` row; offline when no config/session."""
    config = await _config_row(async_dal, dal, community_id)
    if config is None:
        return StreamStatusDTO(False, None, False, 0)
    sessions = await async_dal.select_async(
        dal(dal.streaming_sessions.config_id == config.id),
        dal.streaming_sessions.ALL,
        orderby=~dal.streaming_sessions.id,
        limitby=(0, 1),
    )
    session = sessions.first() if sessions else None
    live = session if session is not None and session.status == "running" else None
    dests = await async_dal.select_async(
        dal(
            (dal.streaming_destinations.config_id == config.id)
            & (dal.streaming_destinations.enabled == True)  # noqa: E712 - pydal comparison
        ),
        dal.streaming_destinations.id,
    )
    return StreamStatusDTO(
        is_streaming=live is not None,
        started_at=_iso(live.started_at) if live is not None else None,
        transcode_applied=bool(live.transcode_applied) if live is not None else False,
        destinations_enabled=len(dests),
    )
