"""v1 `community.reputation` group -- read-only score + tier visibility (gh-310).

Two member-facing routes, mounted at `/api/v1/community/<communityId>/
reputation/...` (same `url_prefix` `community_activity.py`'s/
`community_interaction.py`'s own member-facing blueprints use): the
caller's own community + tenant reputation snapshot (`GET .../me`) and a
community's top-N reputation leaderboard (`GET .../leaderboard`, display
names only -- no ids/emails). Both `tenant_middleware` -> `require_scope
("community.reputation:read")` -- a plain member-tier read scope, not an
admin one (contrast `blueprints/v1/admin.py`'s `community:admin`-gated
reputation *write* route, which this group never touches).

`waddles.community.reputation` is this group's PostHog feature flag
(general.md: every feature ships flagged), `default=True` on evaluator
outage -- matches `community_loyalty.py`/`community_activity.py`'s
identical alpha-rollout convention (the underlying score itself is
already live via `!rep`/`community_reputation_process.py`; this flag
only gates the NEW read-visibility surface this group adds).

Envelope: `{"status": "success", "data": {...}, "meta": {"version": 1}}`
on success (`_envelope()`), `flask_core.api_utils.error_response`'s
`{success, error: {code, message}}` shape on failure (`_err()`) -- same
split `community_loyalty.py`'s own module docstring documents.

Discovery contract: a module-level `BLUEPRINTS: list[Blueprint]`, found
and mounted by `routers/v1.py`'s auto-discovery (`routers/_discovery.py`)
-- no `routers/v1.py` edit, no separate registration call anywhere.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request

from services import community_reputation_service as reputation_svc
from services.community_common import community_in_tenant
from services.current_user import get_current_user_id
from services.errors import ApiError, not_found
from services.pagination import parse_limit

reputation_bp = Blueprint("v1_community_reputation", __name__, url_prefix="/api/v1/community")

#: Two-gate Feature flag -- see module docstring's rollout rationale.
FEATURE_COMMUNITY_REPUTATION = "waddles.community.reputation"


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config -- tables bound lazily by the service layer."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _tenant_ok(community_id: int) -> bool:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    return community_in_tenant(current_app.config["dal"], community_id, ctx)


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _envelope(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    return {"status": "success", "data": data, "meta": {"version": 1}}, 200


@reputation_bp.route("/<int:community_id>/reputation/me", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("community.reputation:read")  # type: ignore[untyped-decorator]
async def get_my_reputation(community_id: int) -> tuple[dict[str, Any], int]:
    """`GET /api/v1/community/<id>/reputation/me` -- caller's own community + tenant scores."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not await feature_enabled(
        FEATURE_COMMUNITY_REPUTATION, tenant=ctx.tenant_slug, community=community_id, default=True
    ):
        return _err(ApiError("Community reputation is not enabled", 402, "FEATURE_NOT_ENABLED"))
    if not _tenant_ok(community_id):
        return _err(not_found("Community not found"))

    try:
        hub_user_id = get_current_user_id(request)
    except ApiError as exc:
        return _err(exc)

    async_dal, dal = _dal()
    snapshot = await reputation_svc.get_my_reputation(
        async_dal, dal, community_id=community_id, hub_user_id=hub_user_id, tenant_id=ctx.tenant_id
    )
    return _envelope(asdict(snapshot))


@reputation_bp.route("/<int:community_id>/reputation/leaderboard", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("community.reputation:read")  # type: ignore[untyped-decorator]
async def get_reputation_leaderboard(community_id: int) -> tuple[dict[str, Any], int]:
    """`GET /api/v1/community/<id>/reputation/leaderboard?limit=` -- top scorers, names only."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not await feature_enabled(
        FEATURE_COMMUNITY_REPUTATION, tenant=ctx.tenant_slug, community=community_id, default=True
    ):
        return _err(ApiError("Community reputation is not enabled", 402, "FEATURE_NOT_ENABLED"))
    if not _tenant_ok(community_id):
        return _err(not_found("Community not found"))

    limit = parse_limit(request.args.get("limit"), default=10)
    async_dal, dal = _dal()
    entries = await reputation_svc.get_leaderboard(
        async_dal, dal, community_id=community_id, limit=limit
    )
    return _envelope({"entries": [asdict(entry) for entry in entries]})


BLUEPRINTS: list[Blueprint] = [reputation_bp]
