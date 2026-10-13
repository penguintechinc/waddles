"""v1 `music-station` group -- per-community queue, policy, and moderation.

New feature, not a Node port -- the advertised Music Station surface:
song requests + whole-playlist submissions resolved via `services.
music_providers.resolve()` into a normalized `Track`, an intermingled
per-community queue (mixed providers, now-playing + upcoming), a
per-community policy (`songRequestsAllowed`/`requestsCategoryRestricted`),
and community-admin moderation (kick a song, kick a whole playlist,
override the category restriction for one request) -- every moderation
action audited to `music_moderation_log`.

Mounted at `/api/v1/admin/<community_id>/music-station/*`, the same URL
namespace `blueprints/v1/music.py`/`community_raffle.py` already use for
community-management surfaces (not literal superadmin-only -- see those
modules' own docstrings). Auth follows the M7 Streaming group's `_scoped`
pattern (`services.community_authz.authorize_community()`): tenant
ownership of `community_id` is re-validated on every call (IDOR
hardening beyond a bare membership check), then either member (`admin=
False`, self-service song requests/listing) or admin/moderator (`admin=
True`, policy + all moderation actions) scope is required.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, jsonify, request
from quart_schema import validate_request, validate_response

from config import HubAPIConfig
from services import community_music_queue_service as svc
from services.community_authz import authorize_community
from services.community_common import is_valid_service_key
from services.current_user import get_current_user_id, get_optional_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError, bad_request, not_found
from services.music_status_service import check_spotify_health, check_youtube_health
from services.rate_limiting import RATE_LIMITER_CONFIG_KEY

logger = logging.getLogger(__name__)

#: gh-313 -- gates the `youtube_allowed_labels` community allowlist check in
#: `enqueue_request()`; defaults ON (`default=True` at every call site
#: below) so the label gate is the normal behavior and this flag is purely
#: a kill switch, not an opt-in.
FEATURE_MUSIC_YOUTUBE_LABELS = "waddles.social.music.youtube_labels"

music_queue_bp = Blueprint("v1_community_music_queue", __name__, url_prefix="/api/v1/admin")

#: Service-to-service only -- a chat command (`!sr`/`!songrequest`,
#: `core/svc_action/builtin_handlers/social_music_action.py`) has no user JWT to
#: present, so it can't call `music_queue_bp`'s own admin-scoped enqueue
#: route above. Mirrors `core/reputation_module`'s `POST /api/v1/internal/
#: events` pattern (`X-Service-Key`, no tenant/JWT) -- see
#: `services.community_common.is_valid_service_key`'s own docstring.
music_internal_bp = Blueprint(
    "v1_community_music_queue_internal", __name__, url_prefix="/api/v1/internal"
)


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config -- tables already bound at startup."""
    return current_app.config["async_dal"], current_app.config["dal"]


#: `app.config` key a lazily-opened raw Valkey client is cached under, for
#: apps that never called `services.rate_limiting.install_rate_limiting()`
#: -- see `_redis_client()`'s own docstring for when this path is used.
MUSIC_PLAYBACK_REDIS_CONFIG_KEY = "music_playback_redis"


def _redis_client() -> Any:
    """Resolve the async Valkey/Redis client used for `music:playback:*` state (gh-315).

    `app.py::create_app()` (outside this task's edit scope) always wires
    ONE Redis/Valkey connection via `services.rate_limiting.
    install_rate_limiting()` -> `flask_core.rate_limiter.RateLimiter` --
    reused directly here (`RateLimiter._redis`, the raw `redis.asyncio`
    client that class opens against `HubAPIConfig.valkey_url`) whenever it
    exists, so this feature never opens a second real connection in
    production. `RateLimiter` exposes no public accessor for its raw
    client (it's a rate-limiting abstraction, not a generic cache), so
    this reaches its private attribute directly rather than duplicating
    connection logic against a private, third-party (`libs/flask_core`,
    outside this task's edit scope) implementation detail.

    Test apps (and any hypothetical deployment that never calls
    `install_rate_limiting`) have no `RateLimiter` instance at all -- for
    those, a client is opened lazily against the same `HubAPIConfig.
    valkey_url` (`VALKEY_URL`/`REDIS_URL`, `config.py`'s own fallback
    chain) and cached on `current_app.config[MUSIC_PLAYBACK_REDIS_CONFIG_
    KEY]` so at most one is ever created per app process, never per
    request.
    """
    limiter = current_app.config.get(RATE_LIMITER_CONFIG_KEY)
    existing = getattr(limiter, "_redis", None) if limiter is not None else None
    if existing is not None:
        return existing

    cached = current_app.config.get(MUSIC_PLAYBACK_REDIS_CONFIG_KEY)
    if cached is not None:
        return cached

    import redis.asyncio as redis_asyncio

    cfg = cast(HubAPIConfig, current_app.config["HUB_API_CONFIG"])
    client = redis_asyncio.from_url(
        cfg.valkey_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    current_app.config[MUSIC_PLAYBACK_REDIS_CONFIG_KEY] = client
    return client


def _tenant_id() -> int:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware always runs first
    return cast(int, ctx.tenant_id)


def _tenant_slug() -> str:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware always runs first
    return cast(str, ctx.tenant_slug)


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


# ---------------------------------------------------------------------------
# Request/response DTOs -- camelCase pinned to this group's own new JSON
# contract (see services/community_music_queue_service.py's own "DTO
# casing" note).
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SetPolicyRequest:
    """Request DTO for `PUT .../music-station/policy` -- all fields optional (partial update).

    `youtubeAllowedLabels`: `None` leaves the allowlist unchanged; `[]`
    explicitly clears it back to unrestricted (gh-313).
    """

    songRequestsAllowed: bool | None = None
    requestsCategoryRestricted: bool | None = None
    youtubeAllowedLabels: list[str] | None = None


@dataclass(slots=True, frozen=True)
class PolicyResponse:
    """Response DTO wrapping `PolicyDTO`."""

    success: bool
    policy: svc.PolicyDTO


@dataclass(slots=True, frozen=True)
class EnqueueRequestRequest:
    """Request DTO for `POST .../music-station/queue/requests`."""

    urlOrQuery: str
    provider: str | None = None
    overrideCategoryRestriction: bool = False


@dataclass(slots=True, frozen=True)
class EnqueuePlaylistRequest:
    """Request DTO for `POST .../music-station/queue/playlists`."""

    items: list[str]
    provider: str | None = None
    overrideCategoryRestriction: bool = False


@dataclass(slots=True, frozen=True)
class QueueItemResponse:
    """Response DTO wrapping one `QueueItemDTO`."""

    success: bool
    item: svc.QueueItemDTO


@dataclass(slots=True, frozen=True)
class PlaylistEnqueueResponse:
    """Response DTO for a playlist enqueue -- created items plus the shared playlist id."""

    success: bool
    playlistId: str
    items: list[svc.QueueItemDTO]


@dataclass(slots=True, frozen=True)
class QueueListResponse:
    """Response DTO for `GET .../music-station/queue`."""

    success: bool
    nowPlaying: svc.QueueItemDTO | None
    upcoming: list[svc.QueueItemDTO]


@dataclass(slots=True, frozen=True)
class ReorderQueueRequest:
    """Request DTO for `PUT .../music-station/queue/reorder`."""

    orderedQueueIds: list[int]


@dataclass(slots=True, frozen=True)
class AdvanceResponse:
    """Response DTO for `POST .../music-station/queue/advance`."""

    success: bool
    previous: svc.QueueItemDTO | None
    next: svc.QueueItemDTO | None


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Generic `{success, message}` response DTO."""

    success: bool
    message: str


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@music_queue_bp.route("/<int:community_id>/music-station/policy", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(PolicyResponse)
async def get_policy(community_id: int) -> PolicyResponse | tuple[dict[str, object], int]:
    """Get the community's Music Station policy (community admin only)."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        tenant_id = _tenant_id()
        policy = await svc.get_policy(
            async_dal, dal, tenant_id=tenant_id, community_id=community_id
        )
    except ApiError as exc:
        return _err(exc)
    return PolicyResponse(success=True, policy=policy)


@music_queue_bp.route("/<int:community_id>/music-station/policy", methods=["PUT"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_request(SetPolicyRequest)
# NOT @validate_response -- writes via insert_async/update_async then
# returns a nested-dataclass response (services/dto_response.py's
# documented crash class). jsonify_dto() is the workaround.
async def set_policy(data: SetPolicyRequest, community_id: int) -> Any:
    """Set the community's Music Station policy (community admin only)."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        actor_id = get_optional_current_user_id(request)
        policy = await svc.set_policy(
            async_dal,
            dal,
            tenant_id=_tenant_id(),
            community_id=community_id,
            song_requests_allowed=data.songRequestsAllowed,
            requests_category_restricted=data.requestsCategoryRestricted,
            youtube_allowed_labels=data.youtubeAllowedLabels,
            updated_by=actor_id,
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(PolicyResponse(success=True, policy=policy))


# ---------------------------------------------------------------------------
# Queue: enqueue
# ---------------------------------------------------------------------------


@music_queue_bp.route("/<int:community_id>/music-station/queue/requests", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_request(EnqueueRequestRequest)
async def enqueue_song_request(data: EnqueueRequestRequest, community_id: int) -> Any:
    """Submit a single song request -- self-service, gated by community policy."""
    async_dal, dal = _dal()
    try:
        await authorize_community(
            request,
            async_dal,
            dal,
            community_id=community_id,
            admin=data.overrideCategoryRestriction,
        )
        requester_id = get_current_user_id(request)
        enforce_youtube_labels = await feature_enabled(
            FEATURE_MUSIC_YOUTUBE_LABELS,
            tenant=_tenant_slug(),
            community=community_id,
            default=True,
        )
        item = await svc.enqueue_request(
            async_dal,
            dal,
            _redis_client(),
            tenant_id=_tenant_id(),
            community_id=community_id,
            url_or_query=data.urlOrQuery,
            provider=data.provider,
            requested_by=requester_id,
            is_admin_override=data.overrideCategoryRestriction,
            enforce_youtube_labels=enforce_youtube_labels,
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(QueueItemResponse(success=True, item=item), 201)


@music_queue_bp.route("/<int:community_id>/music-station/queue/playlists", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_request(EnqueuePlaylistRequest)
async def enqueue_playlist_request(data: EnqueuePlaylistRequest, community_id: int) -> Any:
    """Submit a whole playlist (list of URLs/queries) -- self-service, gated by community policy."""
    async_dal, dal = _dal()
    try:
        await authorize_community(
            request,
            async_dal,
            dal,
            community_id=community_id,
            admin=data.overrideCategoryRestriction,
        )
        requester_id = get_current_user_id(request)
        playlist_id, items = await svc.enqueue_playlist(
            async_dal,
            dal,
            tenant_id=_tenant_id(),
            community_id=community_id,
            items=data.items,
            provider=data.provider,
            requested_by=requester_id,
            is_admin_override=data.overrideCategoryRestriction,
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(
        PlaylistEnqueueResponse(success=True, playlistId=playlist_id, items=items), 201
    )


# ---------------------------------------------------------------------------
# Queue: list
# ---------------------------------------------------------------------------


@music_queue_bp.route("/<int:community_id>/music-station/queue", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_response(QueueListResponse)
async def list_queue(community_id: int) -> QueueListResponse | tuple[dict[str, object], int]:
    """List the community's queue -- now-playing (if any) plus upcoming, in order."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=False)
        now_playing, upcoming = await svc.list_queue(async_dal, dal, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    return QueueListResponse(success=True, nowPlaying=now_playing, upcoming=upcoming)


# ---------------------------------------------------------------------------
# Moderation: kick song / kick playlist
# ---------------------------------------------------------------------------


@music_queue_bp.route("/<int:community_id>/music-station/queue/<int:queue_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
async def kick_song(community_id: int, queue_id: int) -> Any:
    """Kick a single song off the queue (community admin only) -- audited."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        actor_id = get_current_user_id(request)
        reason = request.args.get("reason")
        await svc.kick_song(
            async_dal,
            dal,
            _redis_client(),
            tenant_id=_tenant_id(),
            community_id=community_id,
            queue_id=queue_id,
            actor_user_id=actor_id,
            reason=reason,
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(MessageResponse(success=True, message=f"Queue item {queue_id} removed"))


@music_queue_bp.route(
    "/<int:community_id>/music-station/queue/playlists/<playlist_id>", methods=["DELETE"]
)
@tenant_middleware  # type: ignore[untyped-decorator]
async def kick_playlist(community_id: int, playlist_id: str) -> Any:
    """Kick an entire playlist off the queue (community admin only) -- audited once."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        actor_id = get_current_user_id(request)
        reason = request.args.get("reason")
        removed = await svc.kick_playlist(
            async_dal,
            dal,
            tenant_id=_tenant_id(),
            community_id=community_id,
            playlist_id=playlist_id,
            actor_user_id=actor_id,
            reason=reason,
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(
        MessageResponse(success=True, message=f"Removed {removed} item(s) from playlist")
    )


# ---------------------------------------------------------------------------
# Queue: reorder / advance
# ---------------------------------------------------------------------------


@music_queue_bp.route("/<int:community_id>/music-station/queue/reorder", methods=["PUT"])
@tenant_middleware  # type: ignore[untyped-decorator]
@validate_request(ReorderQueueRequest)
async def reorder_queue(data: ReorderQueueRequest, community_id: int) -> Any:
    """Reorder the community's upcoming queue (community admin only)."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        if not data.orderedQueueIds:
            raise bad_request("orderedQueueIds must not be empty")
        upcoming = await svc.reorder_queue(
            async_dal, dal, community_id=community_id, ordered_queue_ids=data.orderedQueueIds
        )
    except ApiError as exc:
        return _err(exc)
    now_playing, _ = await svc.list_queue(async_dal, dal, community_id=community_id)
    return jsonify_dto(QueueListResponse(success=True, nowPlaying=now_playing, upcoming=upcoming))


@music_queue_bp.route("/<int:community_id>/music-station/queue/advance", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
async def advance_queue(community_id: int) -> Any:
    """Advance the queue: mark now-playing played, promote the next queued track (admin only)."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        previous, next_item = await svc.advance_queue(
            async_dal, dal, _redis_client(), community_id=community_id
        )
    except ApiError as exc:
        return _err(exc)
    return jsonify_dto(AdvanceResponse(success=True, previous=previous, next=next_item))


# ---------------------------------------------------------------------------
# Internal: service-to-service enqueue (chat commands)
# ---------------------------------------------------------------------------


async def _resolve_requester(
    dal: Any, *, community_id: int, platform: str | None, platform_user_id: str | None
) -> int | None:
    """Best-effort `hub_users.id` lookup from a chat requester's platform identity.

    `community_members.user_id` is a stringified `hub_users.id` (legacy
    platform-identity membership model, see `hub_api/services/schema.py::
    bind_auth_tables`'s own comment) -- returns `None` on no match/no
    platform info/a non-integer `user_id`, never raises: an unlinked
    chatter can still queue a song, just with no hub user attribution.
    """
    if not platform or not platform_user_id:
        return None
    row = (
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.platform == platform)
            & (dal.community_members.platform_user_id == platform_user_id)
        )
        .select()
        .first()
    )
    if row is None or not row.user_id:
        return None
    try:
        return int(row.user_id)
    except (TypeError, ValueError):
        return None


@music_internal_bp.route("/music/queue/requests", methods=["POST"])
# NOT typed `tuple[dict[str, object], int]` -- the success path returns
# `jsonify_dto(...)`'s `tuple[Response, int]` (see that helper's own
# docstring: bypasses quart-schema's `TypeAdapter` crash on a nested-
# dataclass response), matching `set_policy`/the admin `enqueue_song_
# request` route's own `-> Any` above.
async def internal_enqueue_song_request() -> Any:
    """`POST /api/v1/internal/music/queue/requests` -- service-to-service only.

    Lets a pipeline action bundle (no user JWT -- a chat command, not an
    admin API call) enqueue a Music Station song request on a viewer's
    behalf. `communityId`/`urlOrQuery` are required; `tenantId` is
    deliberately NOT accepted from the caller -- it's derived from the
    community row itself (security.md: never trust a tenant claim from
    the caller when it can instead come from an already-tenant-scoped
    row), same trust boundary reasoning as `communityId` being trusted at
    all here: this route is service-key gated, not open to the internet.
    """
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    body = await request.get_json(force=True, silent=True) or {}
    community_id = body.get("communityId")
    url_or_query = body.get("urlOrQuery")
    if (
        not isinstance(community_id, int)
        or not isinstance(url_or_query, str)
        or not url_or_query.strip()
    ):
        return {"success": False, "error": "communityId and urlOrQuery are required"}, 400

    async_dal, dal = _dal()

    try:
        # `select(dal.communities.id, dal.communities.tenant_id)` -- deliberately
        # NOT a bare `.select()`. `communities` also binds `about_extended`/
        # `social_links`/`website_url`/`discord_invite_url`/`visibility`, a
        # pre-existing pydal-vs-Postgres schema gap documented in
        # `services/schema.py`'s module docstring (gap 4): those columns are
        # bound for pydal query-building but were never added by any numbered
        # migration. A bare `.select()` pulls every bound field and 500s with
        # `psycopg2.errors.UndefinedColumn` the moment it runs -- this route
        # is the first `communities` caller to do a full-row select, so it's
        # the first to trip the gap. Restricting to the two columns this
        # handler actually needs avoids the gap entirely without requiring a
        # schema migration here.
        community_row = (
            dal(dal.communities.id == community_id)
            .select(dal.communities.id, dal.communities.tenant_id)
            .first()
        )
        if community_row is None:
            return _err(not_found("Community not found"))
        tenant_id = int(community_row.tenant_id)

        # No JWT/`TenantContext` on this service-key route (see module
        # docstring) -- the slug `feature_enabled()` needs is looked up
        # straight from the tenant row it was just derived from, "global"
        # fallback only for the practically-unreachable dangling-FK case.
        tenant_row = dal(dal.tenants.id == tenant_id).select(dal.tenants.slug).first()
        tenant_slug = tenant_row.slug if tenant_row is not None else "global"
        enforce_youtube_labels = await feature_enabled(
            FEATURE_MUSIC_YOUTUBE_LABELS, tenant=tenant_slug, community=community_id, default=True
        )

        platform = body.get("platform")
        platform_user_id = body.get("platformUserId")
        requested_by = await _resolve_requester(
            dal,
            community_id=community_id,
            platform=platform if isinstance(platform, str) else None,
            platform_user_id=platform_user_id if isinstance(platform_user_id, str) else None,
        )

        item = await svc.enqueue_request(
            async_dal,
            dal,
            _redis_client(),
            tenant_id=tenant_id,
            community_id=community_id,
            url_or_query=url_or_query,
            provider=body.get("provider"),
            requested_by=requested_by,
            is_admin_override=False,
            enforce_youtube_labels=enforce_youtube_labels,
        )
    except ApiError as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001 - last-resort 500 must still be JSON, never an empty body
        logger.exception(
            "internal_enqueue_song_request.unhandled_error",
            extra={"community_id": community_id},
        )
        return error_response(f"Internal error: {exc}", 500, "INTERNAL_ERROR")
    return jsonify_dto(QueueItemResponse(success=True, item=item), 201)


@music_internal_bp.route("/music/policy", methods=["PUT"])
async def internal_set_music_policy() -> Any:
    """`PUT /api/v1/internal/music/policy` -- service-to-service only (gh-313).

    Lets a chat-side moderation command (no user JWT) update a
    community's `youtube_allowed_labels` allowlist without going through
    the admin-JWT-scoped `music_queue_bp.set_policy` route above. Body:
    `{"community_id": int, "youtube_allowed_labels": [str, ...]}` --
    `tenant_id` is deliberately NOT accepted from the caller, same trust
    boundary reasoning as `internal_enqueue_song_request()`'s own
    docstring: derived from the community row itself, never the caller.
    Validation (bounds/shape) is `services.community_music_queue_service.
    set_policy()`'s own `_normalize_youtube_allowed_labels()` -- one
    validator for both the admin and internal write paths.
    """
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    body = await request.get_json(force=True, silent=True) or {}
    community_id = body.get("community_id")
    youtube_allowed_labels = body.get("youtube_allowed_labels")
    if not isinstance(community_id, int) or not isinstance(youtube_allowed_labels, list):
        return {
            "success": False,
            "error": "community_id and youtube_allowed_labels are required",
        }, 400

    async_dal, dal = _dal()
    try:
        community_row = (
            dal(dal.communities.id == community_id)
            .select(dal.communities.id, dal.communities.tenant_id)
            .first()
        )
        if community_row is None:
            return _err(not_found("Community not found"))
        tenant_id = int(community_row.tenant_id)

        policy = await svc.set_policy(
            async_dal,
            dal,
            tenant_id=tenant_id,
            community_id=community_id,
            song_requests_allowed=None,
            requests_category_restricted=None,
            youtube_allowed_labels=youtube_allowed_labels,
            updated_by=None,
        )
    except ApiError as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001 - last-resort 500 must still be JSON, never an empty body
        logger.exception(
            "internal_set_music_policy.unhandled_error", extra={"community_id": community_id}
        )
        return error_response(f"Internal error: {exc}", 500, "INTERNAL_ERROR")

    return (
        jsonify(
            {
                "status": "success",
                "data": {
                    "community_id": community_id,
                    "youtube_allowed_labels": policy.youtubeAllowedLabels,
                },
                "meta": {"version": 1},
            }
        ),
        200,
    )


@music_internal_bp.route("/music/status", methods=["GET"])
async def internal_music_status() -> Any:
    """`GET /api/v1/internal/music/status?community_id=<id>` -- service-to-service only.

    Backs `!sr status` (`core/svc_action/builtin_handlers/social_music_action.py`).
    Deliberately does NOT check `music_policy.song_requests_allowed` --
    that's a separate per-community admin toggle from the PostHog
    `waddles.social.music` feature flag this endpoint's caller already
    resolved itself before ever reaching hub-api (`social_music_process.
    py`'s `disabled` short-circuit). This endpoint answers one question
    only: is the Spotify provider usable right now, and how big is the
    queue -- both real signals `music_policy` can't answer.

    `state` in the response body is always `"enabled"` or `"error"` (with
    a specific, secret-free `cause`) -- never `"offline"`. `"offline"` is
    a caller-side interpretation of an unreachable hub-api or a non-2xx
    response, not a state this handler can observe about itself; see
    `social_music_action._check_status()`'s own docstring.
    """
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    raw_community_id = request.args.get("community_id")
    try:
        community_id = int(raw_community_id) if raw_community_id is not None else None
    except (TypeError, ValueError):
        community_id = None
    if community_id is None:
        return {"success": False, "error": "community_id query param is required"}, 400

    async_dal, dal = _dal()
    try:
        community_row = dal(dal.communities.id == community_id).select(dal.communities.id).first()
        if community_row is None:
            return _err(not_found("Community not found"))

        length = await svc.queue_length(async_dal, dal, community_id=community_id)
        health = await check_spotify_health()
        youtube_health = await check_youtube_health()
    except ApiError as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001 - last-resort 500 must still be JSON, never an empty body
        logger.exception(
            "internal_music_status.unhandled_error", extra={"community_id": community_id}
        )
        return error_response(f"Internal error: {exc}", 500, "INTERNAL_ERROR")

    # Top-level `state`/`cause`/`provider` keep their pre-YouTube meaning
    # (Spotify's own health) byte-compatible for existing callers, except
    # `state` is now an OR across both providers -- see `providers` below
    # for each provider's own state/cause, and `playback_provider` for
    # which one actually serves full-length playback.
    spotify_state = "enabled" if health.healthy else "error"
    state = "enabled" if (health.healthy or youtube_health.state == "enabled") else "error"
    playback_provider = "youtube" if youtube_health.state == "enabled" else "spotify"
    return (
        jsonify(
            {
                "status": "success",
                "data": {
                    "state": state,
                    "cause": health.cause,
                    "provider": "spotify",
                    "queue_length": length,
                    "providers": {
                        "spotify": {"state": spotify_state, "cause": health.cause},
                        "youtube": {"state": youtube_health.state, "cause": youtube_health.cause},
                    },
                    "playback_provider": playback_provider,
                },
                "meta": {"version": 1},
            }
        ),
        200,
    )


# ---------------------------------------------------------------------------
# Internal: live queue read/advance (OBS overlay + public queue page)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PlaybackSnapshotDTO:
    """`data.playback` wire shape (gh-315) -- shared by both live-queue-read endpoints.

    `paused`/`paused_since` reflect the community's raw `music:playback:*`
    Valkey state; `position_ms` is `None` whenever nothing's playing.
    """

    paused: bool
    paused_since: str | None
    position_ms: int | None


def _playback_snapshot(snapshot: svc.LiveQueueSnapshot) -> PlaybackSnapshotDTO:
    return PlaybackSnapshotDTO(
        paused=snapshot.paused, paused_since=snapshot.paused_since, position_ms=snapshot.position_ms
    )


@dataclass(slots=True, frozen=True)
class LiveQueueStateResponse:
    """`{status, data, meta}`-enveloped data payload for the internal live-queue GET."""

    community_id: int
    now_playing: svc.LiveQueueItemDTO | None
    queue: list[svc.LiveQueueItemDTO]
    updated_at: str
    playback: PlaybackSnapshotDTO


@dataclass(slots=True, frozen=True)
class LiveQueueAdvanceResponse:
    """`{status, data, meta}`-enveloped data payload for the internal guarded-advance POST."""

    community_id: int
    now_playing: svc.LiveQueueItemDTO | None
    queue: list[svc.LiveQueueItemDTO]
    updated_at: str
    advanced: bool
    playback: PlaybackSnapshotDTO


def _live_envelope(data: Any) -> tuple[Any, int]:
    """`{status, data, meta}` envelope -- same shape `internal_music_status()` already returns.

    Manual `jsonify()` (not `@validate_response`) deliberately, matching
    `services/dto_response.py::jsonify_dto`'s own workaround -- both new
    response DTOs above nest `LiveQueueItemDTO`/`RequestedByDTO`, the
    exact shape that module's docstring documents as crashing quart-
    schema's `TypeAdapter` response path.
    """
    return jsonify({"status": "success", "data": asdict(data), "meta": {"version": 1}}), 200


@music_internal_bp.route("/music/queue", methods=["GET"])
async def internal_get_live_queue() -> Any:
    """`GET /api/v1/internal/music/queue?community_id=<id>` -- service-to-service only.

    Backs the OBS overlay player (`core/svc_presentation`) and the public
    (unauthenticated) queue page (`blueprints/v1/public_music_queue.py`) --
    neither calls hub-api's admin-scoped `music_queue_bp` routes above (no
    user JWT to present, same rationale as `internal_enqueue_song_request`).

    Lazily auto-advances on read: an expired `playing` item (its track's
    `duration_ms` plus a grace period elapsed) is marked `played` and the
    oldest `queued` item promoted; an idle queue with nothing playing
    auto-starts its head item. Both inside one locked transaction (see
    `services.community_music_queue_service.get_live_queue_state`'s own
    docstring) so two overlay instances polling concurrently can never
    double-advance.
    """
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    raw_community_id = request.args.get("community_id")
    try:
        community_id = int(raw_community_id) if raw_community_id is not None else None
    except (TypeError, ValueError):
        community_id = None
    if community_id is None:
        return {"success": False, "error": "community_id query param is required"}, 400

    async_dal, dal = _dal()
    try:
        community_row = dal(dal.communities.id == community_id).select(dal.communities.id).first()
        if community_row is None:
            return _err(not_found("Community not found"))

        snapshot = await svc.get_live_queue_state(
            async_dal, dal, _redis_client(), community_id=community_id, auto_advance=True
        )
    except ApiError as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001 - last-resort 500 must still be JSON, never an empty body
        logger.exception(
            "internal_get_live_queue.unhandled_error", extra={"community_id": community_id}
        )
        return error_response(f"Internal error: {exc}", 500, "INTERNAL_ERROR")

    return _live_envelope(
        LiveQueueStateResponse(
            community_id=community_id,
            now_playing=snapshot.now_playing,
            queue=snapshot.queue,
            updated_at=datetime.now(UTC).isoformat(),
            playback=_playback_snapshot(snapshot),
        )
    )


@music_internal_bp.route("/music/queue/advance", methods=["POST"])
async def internal_advance_live_queue() -> Any:
    """`POST /api/v1/internal/music/queue/advance` -- service-to-service only.

    Body: `{"community_id": int, "item_id": int}`. Advances ONLY if
    `item_id` is the community's CURRENT `playing` item -- a guard against
    two overlay instances (or a race against the GET route's own lazy
    auto-advance) both trying to advance the same already-advanced state.
    `advanced: false` (never an error) means another caller already won
    the race; the response still carries the current, authoritative state
    either way.
    """
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    body = await request.get_json(force=True, silent=True) or {}
    community_id = body.get("community_id")
    item_id = body.get("item_id")
    if not isinstance(community_id, int) or not isinstance(item_id, int):
        return {"success": False, "error": "community_id and item_id are required"}, 400

    async_dal, dal = _dal()
    try:
        community_row = dal(dal.communities.id == community_id).select(dal.communities.id).first()
        if community_row is None:
            return _err(not_found("Community not found"))

        snapshot = await svc.advance_live_queue(
            async_dal, dal, _redis_client(), community_id=community_id, item_id=item_id
        )
    except ApiError as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001 - last-resort 500 must still be JSON, never an empty body
        logger.exception(
            "internal_advance_live_queue.unhandled_error", extra={"community_id": community_id}
        )
        return error_response(f"Internal error: {exc}", 500, "INTERNAL_ERROR")

    return _live_envelope(
        LiveQueueAdvanceResponse(
            community_id=community_id,
            now_playing=snapshot.now_playing,
            queue=snapshot.queue,
            updated_at=datetime.now(UTC).isoformat(),
            advanced=snapshot.advanced,
            playback=_playback_snapshot(snapshot),
        )
    )


# ---------------------------------------------------------------------------
# Internal: pause/resume the currently-playing track (gh-315)
# ---------------------------------------------------------------------------


@music_internal_bp.route("/music/playback", methods=["POST"])
async def internal_set_music_playback() -> Any:
    """`POST /api/v1/internal/music/playback` -- service-to-service only (gh-315).

    Body: `{"community_id": int, "action": "pause"|"resume"}`. Pauses or
    resumes the community's currently-`playing` track by writing/clearing
    `music:playback:{community_id}` in Valkey -- see `services.
    community_music_queue_service.set_playback()`'s own docstring for the
    full state machine (`reason` in `{"paused", "resumed",
    "already_paused", "already_playing", "nothing_playing"}`, `changed`
    `False` for every no-op reason).
    """
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    body = await request.get_json(force=True, silent=True) or {}
    community_id = body.get("community_id")
    action = body.get("action")
    if not isinstance(community_id, int) or not isinstance(action, str):
        return {"success": False, "error": "community_id and action are required"}, 400

    async_dal, dal = _dal()
    try:
        community_row = dal(dal.communities.id == community_id).select(dal.communities.id).first()
        if community_row is None:
            return _err(not_found("Community not found"))

        result = await svc.set_playback(
            async_dal, dal, _redis_client(), community_id=community_id, action=action
        )
    except ApiError as exc:
        return _err(exc)
    except Exception as exc:  # noqa: BLE001 - last-resort 500 must still be JSON, never an empty body
        logger.exception(
            "internal_set_music_playback.unhandled_error", extra={"community_id": community_id}
        )
        return error_response(f"Internal error: {exc}", 500, "INTERNAL_ERROR")

    return _live_envelope(result)


BLUEPRINTS: list[Blueprint] = [music_queue_bp, music_internal_bp]
