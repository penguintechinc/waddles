"""v1 `community.loyalty` group -- MVP core-currency loyalty (gh-317).

Admin blueprint (`loyalty_bp`) keeps this group's pre-existing URL prefix
(`/api/v1/admin/<community_id>/loyalty/...`) and scope chain
(`tenant_middleware` -> `require_scope`), now re-pointed at
`services.community_loyalty`'s local implementation instead of a
reverse-proxy to the separate `loyalty-interaction` deployment. Only the
routes with a direct MVP-schema equivalent survive the swap (config,
leaderboard, balance adjustment, wipe, stats) -- the old giveaways/games/
gear-shop routes proxied a feature set that deployment owned outright and
this MVP's schema (`services.schema.bind_loyalty_tables()`) has no tables
for; see `services/community_loyalty.py`'s own module docstring.

`loyalty_internal_bp` is new: a chat command (`!points`/`!redeem`/mod
commands, no user JWT) needs a service-to-service path, same
`X-Service-Key` pattern as `community_music_queue.py`'s `music_internal_bp`
(see that module's own docstring for the rationale) -- registered here,
in this same file's `BLUEPRINTS` list, rather than a second edit site.

Both blueprints return the `{status, data, meta}` envelope on success
(`_envelope()`) and `flask_core.api_utils.error_response`'s `{success,
error: {code, message}}` shape on failure (`_err()`) -- the latter matches
every other ApiError-raising blueprint in this port (see
`services/errors.py`'s own docstring), so callers reading
`err.response?.data?.error?.message` are unaffected by this rewrite.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request

from services import community_loyalty as loyalty_svc
from services.community_common import community_in_tenant, is_valid_service_key
from services.current_user import get_current_user_id
from services.errors import ApiError, bad_request, not_found
from services.pagination import parse_limit

loyalty_bp = Blueprint("v1_community_loyalty", __name__, url_prefix="/api/v1/admin")

#: Service-to-service only -- see module docstring.
loyalty_internal_bp = Blueprint(
    "v1_community_loyalty_internal", __name__, url_prefix="/api/v1/internal"
)

#: Two-gate Feature flag -- gates the internal blueprint's WRITE routes
#: (earn/adjust/redeem) only; the admin blueprint's own `get_config` route
#: enforces the same flag for the dashboard surface. `default=True` in
#: alpha -- see gh-317's rollout plan; not yet validated in beta/prod.
FEATURE_COMMUNITY_LOYALTY = "waddles.community.loyalty"


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config -- tables bound lazily by the service layer."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _tenant_ok(community_id: int) -> bool:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    return community_in_tenant(current_app.config["dal"], community_id, ctx)


def _community_exists(dal: Any, community_id: int) -> bool:
    return dal(dal.communities.id == community_id).select(dal.communities.id).first() is not None


def _tenant_slug_for_community(dal: Any, community_id: int) -> str:
    """Best-effort tenant slug lookup for a service-key route's `feature_enabled()` check.

    No JWT/`TenantContext` on the internal blueprint (see module
    docstring) -- looked up straight from the community's own
    `tenant_id`, "global" fallback only for the practically-unreachable
    dangling-FK case. Mirrors `community_music_queue.py`'s
    `internal_enqueue_song_request()` exactly.
    """
    community_row = (
        dal(dal.communities.id == community_id)
        .select(dal.communities.id, dal.communities.tenant_id)
        .first()
    )
    if community_row is None:
        return "global"
    tenant_row = (
        dal(dal.tenants.id == int(community_row.tenant_id)).select(dal.tenants.slug).first()
    )
    return tenant_row.slug if tenant_row is not None else "global"


def _parse_int(raw: str | None) -> int | None:
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _envelope(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    return {"status": "success", "data": data, "meta": {"version": 1}}, 200


# ===== Admin: config =====


@loyalty_bp.route("/<int:community_id>/loyalty/config", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.loyalty:read")  # type: ignore[misc]
async def get_config(community_id: int) -> tuple[dict[str, Any], int]:
    """`GET /api/v1/admin/<id>/loyalty/config`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not await feature_enabled(FEATURE_COMMUNITY_LOYALTY, tenant=ctx.tenant_slug):
        return _err(
            ApiError(
                "Community loyalty requires a Professional plan or higher",
                402,
                "FEATURE_NOT_ENABLED",
            )
        )
    if not _tenant_ok(community_id):
        return _err(not_found("Community not found"))

    async_dal, dal = _dal()
    try:
        config = await loyalty_svc.get_config(async_dal, dal, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    return _envelope(asdict(config))


@loyalty_bp.route("/<int:community_id>/loyalty/config", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.loyalty:write")  # type: ignore[misc]
async def update_config(community_id: int) -> tuple[dict[str, Any], int]:
    """`PUT /api/v1/admin/<id>/loyalty/config` -- partial update, unset fields left unchanged."""
    if not _tenant_ok(community_id):
        return _err(not_found("Community not found"))

    body = await request.get_json(force=True, silent=True) or {}
    async_dal, dal = _dal()
    try:
        config = await loyalty_svc.set_config(
            async_dal,
            dal,
            community_id=community_id,
            currency_name=body.get("currency_name"),
            currency_symbol=body.get("currency_symbol"),
            earn_chat_points=body.get("earn_chat_points"),
            earn_chat_cooldown_s=body.get("earn_chat_cooldown_s"),
            earn_watch_points_per_min=body.get("earn_watch_points_per_min"),
            earn_watch_enabled=body.get("earn_watch_enabled"),
            max_balance=body.get("max_balance"),
            enabled=body.get("enabled"),
        )
    except ApiError as exc:
        return _err(exc)
    return _envelope(asdict(config))


# ===== Admin: leaderboard =====


@loyalty_bp.route("/<int:community_id>/loyalty/leaderboard", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.loyalty:read")  # type: ignore[misc]
async def get_leaderboard(community_id: int) -> tuple[dict[str, Any], int]:
    """`GET /api/v1/admin/<id>/loyalty/leaderboard?limit=`."""
    if not _tenant_ok(community_id):
        return _err(not_found("Community not found"))

    limit = parse_limit(request.args.get("limit"), default=10)
    async_dal, dal = _dal()
    config = await loyalty_svc.get_config(async_dal, dal, community_id=community_id)
    entries = await loyalty_svc.leaderboard(async_dal, dal, community_id=community_id, limit=limit)
    return _envelope(
        {
            "entries": [asdict(entry) for entry in entries],
            "currency_name": config.currency_name,
            "currency_symbol": config.currency_symbol,
        }
    )


# ===== Admin: balance adjustment =====


@loyalty_bp.route("/<int:community_id>/loyalty/user/<platform_user_id>/balance", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.loyalty:admin")  # type: ignore[misc]
async def adjust_balance(community_id: int, platform_user_id: str) -> tuple[dict[str, Any], int]:
    """`PUT /api/v1/admin/<id>/loyalty/user/<platformUserId>/balance` -- admin add/remove points.

    `platform_user_id` replaces the pre-MVP `<int:user_id>` (hub identity)
    path segment: `loyalty_balances` is keyed by `(community_id, platform,
    platform_user_id)`, not a `hub_users.id` -- see
    `services/schema.py::bind_loyalty_tables()`. `platform` is required in
    the body since one path segment can't disambiguate it; `delta` may be
    negative (removal).
    """
    if not _tenant_ok(community_id):
        return _err(not_found("Community not found"))

    body = await request.get_json(force=True, silent=True) or {}
    platform = body.get("platform")
    delta = body.get("delta")
    note = body.get("note")
    allow_negative = body.get("allow_negative", False)
    if not isinstance(platform, str) or not platform or not isinstance(delta, int):
        return _err(bad_request("platform (string) and delta (int) are required"))

    async_dal, dal = _dal()
    try:
        actor_id = get_current_user_id(request)
        balance = await loyalty_svc.adjust(
            async_dal,
            dal,
            community_id=community_id,
            platform=platform,
            platform_user_id=platform_user_id,
            delta=delta,
            actor=str(actor_id),
            note=note if isinstance(note, str) else None,
            allow_negative=bool(allow_negative),
        )
    except ApiError as exc:
        return _err(exc)
    return _envelope(asdict(balance))


# ===== Admin: wipe / stats =====


@loyalty_bp.route("/<int:community_id>/loyalty/wipe", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.loyalty:admin")  # type: ignore[misc]
async def wipe_currency(community_id: int) -> tuple[dict[str, Any], int]:
    """`POST /api/v1/admin/<id>/loyalty/wipe` -- zero every balance in the community."""
    if not _tenant_ok(community_id):
        return _err(not_found("Community not found"))

    async_dal, dal = _dal()
    affected = await loyalty_svc.wipe(async_dal, dal, community_id=community_id)
    return _envelope({"affected": affected})


@loyalty_bp.route("/<int:community_id>/loyalty/stats", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("community.loyalty:read")  # type: ignore[misc]
async def get_stats(community_id: int) -> tuple[dict[str, Any], int]:
    """`GET /api/v1/admin/<id>/loyalty/stats`."""
    if not _tenant_ok(community_id):
        return _err(not_found("Community not found"))

    async_dal, dal = _dal()
    stats = await loyalty_svc.get_stats(async_dal, dal, community_id=community_id)
    return _envelope(asdict(stats))


# ===== Internal (service-to-service, X-Service-Key) =====


@loyalty_internal_bp.route("/loyalty/balance", methods=["GET"])
async def internal_get_balance() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/internal/loyalty/balance?community_id=&platform=&platform_user_id=`."""
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    community_id = _parse_int(request.args.get("community_id"))
    platform = request.args.get("platform")
    platform_user_id = request.args.get("platform_user_id")
    if community_id is None or not platform or not platform_user_id:
        return _err(bad_request("community_id, platform, and platform_user_id are required"))

    async_dal, dal = _dal()
    if not _community_exists(dal, community_id):
        return _err(not_found("Community not found"))

    balance = await loyalty_svc.get_balance(
        async_dal,
        dal,
        community_id=community_id,
        platform=platform,
        platform_user_id=platform_user_id,
    )
    return _envelope(asdict(balance))


@loyalty_internal_bp.route("/loyalty/leaderboard", methods=["GET"])
async def internal_leaderboard() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/internal/loyalty/leaderboard?community_id=&limit=`."""
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    community_id = _parse_int(request.args.get("community_id"))
    if community_id is None:
        return _err(bad_request("community_id is required"))

    async_dal, dal = _dal()
    if not _community_exists(dal, community_id):
        return _err(not_found("Community not found"))

    limit = parse_limit(request.args.get("limit"), default=10)
    config = await loyalty_svc.get_config(async_dal, dal, community_id=community_id)
    entries = await loyalty_svc.leaderboard(async_dal, dal, community_id=community_id, limit=limit)
    return _envelope(
        {
            "entries": [asdict(entry) for entry in entries],
            "currency_name": config.currency_name,
            "currency_symbol": config.currency_symbol,
        }
    )


@loyalty_internal_bp.route("/loyalty/items", methods=["GET"])
async def internal_list_items() -> tuple[dict[str, Any], int]:
    """`GET /api/v1/internal/loyalty/items?community_id=` -- enabled items only (shop display)."""
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    community_id = _parse_int(request.args.get("community_id"))
    if community_id is None:
        return _err(bad_request("community_id is required"))

    async_dal, dal = _dal()
    if not _community_exists(dal, community_id):
        return _err(not_found("Community not found"))

    items = await loyalty_svc.list_items(
        async_dal, dal, community_id=community_id, enabled_only=True
    )
    return _envelope({"items": [asdict(item) for item in items]})


async def _require_internal_write_enabled(dal: Any, community_id: int) -> ApiError | None:
    """Shared feature-flag gate for the internal blueprint's earn/adjust/redeem routes."""
    tenant_slug = _tenant_slug_for_community(dal, community_id)
    enabled = await feature_enabled(
        FEATURE_COMMUNITY_LOYALTY, tenant=tenant_slug, community=community_id, default=True
    )
    if not enabled:
        return ApiError("Community loyalty is not enabled", 402, "FEATURE_NOT_ENABLED")
    return None


@loyalty_internal_bp.route("/loyalty/earn", methods=["POST"])
async def internal_earn() -> tuple[dict[str, Any], int]:
    """`POST /api/v1/internal/loyalty/earn` -- credit chat/watch points on a viewer's behalf."""
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    body = await request.get_json(force=True, silent=True) or {}
    community_id = body.get("community_id")
    platform = body.get("platform")
    platform_user_id = body.get("platform_user_id")
    kind = body.get("kind")
    points = body.get("points")
    ref = body.get("ref")
    if (
        not isinstance(community_id, int)
        or not isinstance(platform, str)
        or not isinstance(platform_user_id, str)
        or not isinstance(kind, str)
        or not isinstance(points, int)
    ):
        return _err(
            bad_request("community_id, platform, platform_user_id, kind, and points are required")
        )

    async_dal, dal = _dal()
    if not _community_exists(dal, community_id):
        return _err(not_found("Community not found"))
    gate_error = await _require_internal_write_enabled(dal, community_id)
    if gate_error is not None:
        return _err(gate_error)

    try:
        result = await loyalty_svc.earn(
            async_dal,
            dal,
            community_id=community_id,
            platform=platform,
            platform_user_id=platform_user_id,
            kind=kind,
            points=points,
            ref=ref if isinstance(ref, str) else None,
        )
    except ApiError as exc:
        return _err(exc)
    return _envelope({"balance": asdict(result.balance), "applied_delta": result.applied_delta})


@loyalty_internal_bp.route("/loyalty/adjust", methods=["POST"])
async def internal_adjust() -> tuple[dict[str, Any], int]:
    """`POST /api/v1/internal/loyalty/adjust` -- mod command add/remove points."""
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    body = await request.get_json(force=True, silent=True) or {}
    community_id = body.get("community_id")
    platform = body.get("platform")
    platform_user_id = body.get("platform_user_id")
    delta = body.get("delta")
    actor_platform_user_id = body.get("actor_platform_user_id")
    note = body.get("note")
    if (
        not isinstance(community_id, int)
        or not isinstance(platform, str)
        or not isinstance(platform_user_id, str)
        or not isinstance(delta, int)
    ):
        return _err(bad_request("community_id, platform, platform_user_id, and delta are required"))

    async_dal, dal = _dal()
    if not _community_exists(dal, community_id):
        return _err(not_found("Community not found"))
    gate_error = await _require_internal_write_enabled(dal, community_id)
    if gate_error is not None:
        return _err(gate_error)

    try:
        balance = await loyalty_svc.adjust(
            async_dal,
            dal,
            community_id=community_id,
            platform=platform,
            platform_user_id=platform_user_id,
            delta=delta,
            actor=actor_platform_user_id if isinstance(actor_platform_user_id, str) else None,
            note=note if isinstance(note, str) else None,
        )
    except ApiError as exc:
        return _err(exc)
    return _envelope(asdict(balance))


@loyalty_internal_bp.route("/loyalty/redeem", methods=["POST"])
async def internal_redeem() -> tuple[dict[str, Any], int]:
    """`POST /api/v1/internal/loyalty/redeem` -- chat-command shop redemption.

    409 `error.message` on rejection is one of: `not enough points (have X,
    need Y)`, `item out of stock`, `unknown item 'sku'`, `loyalty is
    disabled here` -- these strings reach chat verbatim (see
    `services.community_loyalty._sync_redeem()`'s own docstring).
    """
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    body = await request.get_json(force=True, silent=True) or {}
    community_id = body.get("community_id")
    platform = body.get("platform")
    platform_user_id = body.get("platform_user_id")
    sku = body.get("sku")
    if (
        not isinstance(community_id, int)
        or not isinstance(platform, str)
        or not isinstance(platform_user_id, str)
        or not isinstance(sku, str)
    ):
        return _err(bad_request("community_id, platform, platform_user_id, and sku are required"))

    async_dal, dal = _dal()
    if not _community_exists(dal, community_id):
        return _err(not_found("Community not found"))
    gate_error = await _require_internal_write_enabled(dal, community_id)
    if gate_error is not None:
        return _err(gate_error)

    try:
        redemption = await loyalty_svc.redeem(
            async_dal,
            dal,
            community_id=community_id,
            platform=platform,
            platform_user_id=platform_user_id,
            sku=sku,
        )
    except ApiError as exc:
        return _err(exc)
    return _envelope(asdict(redemption))


BLUEPRINTS: list[Blueprint] = [loyalty_bp, loyalty_internal_bp]
