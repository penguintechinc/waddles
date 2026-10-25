"""v1 `analytics` group (M9) -- port of Node's `analyticsController.js`.

Every handler here holds no business logic of its own: `analyticsController.
js`'s own module docstring says so plainly ("Proxies to analytics-core
module. Hub is the auth boundary. Analytics-core returns aggregate data
only.") -- every route forwards to the standalone `analytics-core` service
(`core/analytics_core_module`, port 8040) via `services/analytics_proxy.
py`'s `AnalyticsCoreProxyClient`, ported from `analyticsService.js`. Same
pattern deviation from `blueprints/v2/platform.py`'s per-function DTO
exemplar as `blueprints/v1/event.py` (see that module's own docstring):
no owned data model, `@validate_request`/`@validate_response` intentionally
not used here -- the response body is whatever `analytics-core` returns,
not a hub-api-owned row.

Auth (`routes/analytics.js`'s own comment enumerates 4 scenarios --
Node middleware -> this port):

1. Self stats/reputation (`/me/*`): `requireAuth` -> `tenant_middleware`
   only (self-service, `PORTING.md` pattern -- own resource, no scope).
2. Community admin views member (`/community/*`):
   `requireAuth` + `requireCommunityAdmin` -> `tenant_middleware` +
   `require_scope(SCOPE_COMMUNITY_ADMIN)` + a tenant/membership check
   (documented below).
3. Superadmin views any user (`/admin/users/*`):
   `requireAuth` + `requireSuperAdmin` -> `tenant_middleware` +
   `require_scope("users:admin")` (same scope `user_management.py`'s
   own superadmin routes use).
4. Platform overview (`/platform/*`):
   `requireAuth` + `requireAnalyticsConsumer` -> `tenant_middleware` +
   `require_scope("analytics:read")`.

`analytics:read` is the SAME scope name already in security.md's own
SCOPE_BUNDLES table (`maintainer -> ... analytics:read`) -- granted via
the `global`/`tenant` maintainer bundles, and covered by the `global`
admin bundle's `*:read` wildcard (superadmins always pass, matching
Node's `isSuperAdmin` bypass). What was MISSING before this port: Node's
`isAnalyticsConsumer` DB flag (`hub_users.is_analytics_consumer`,
M1-ported) had no corresponding scope GRANT at JWT-mint time --
`services/auth_service.py::create_session_token()` extended (this PR) to
add `analytics:read` when `user.is_analytics_consumer`, the same place
`is_super_admin`/tenant-admin roles already expand scopes. Without this,
no analytics-consumer user could ever satisfy Scenario 4's gate --
inventing this scope GRANT (not the scope name, which pre-exists) mirrors
the Event module's own precedent of wiring a missing auth signal at port
time.

SECURITY -- cross-tenant/cross-membership leak (found + fixed during this
port; NOT a faithful reproduction of a Node bug -- Node's own
`requireCommunityAdmin` dynamically resolves the CALLER's role on the
SPECIFIC `communityId` in the URL via a `community_members` +
`community_roles` join). This port's `require_scope(SCOPE_COMMUNITY_ADMIN)`
is, like every other Community-module blueprint in this repo
(`community_activity.py` et al.), a STATIC JWT scope: it proves the
caller holds a community-admin-shaped grant somewhere, not that they
administer THIS community, and not that the target user even belongs to
it. Two checks close that gap before any `(community_id, user_id)` pair
is forwarded to `analytics-core` (which trusts hub-api completely and
performs no tenant/membership check of its own -- "Hub is the auth
boundary"):
  1. `community_in_tenant(dal, community_id, ctx)` -- the community must
     belong to the CALLER's OWN validated tenant (security.md Tenant
     Isolation). Without this, any caller with the static admin scope
     could request analytics for a community in a DIFFERENT tenant.
  2. `community_member_exists(dal, community_id, user_id)`
     (`analytics_service.py`) -- the target `user_id` must actually be a
     member of `community_id`. Without this, a caller could pair an
     arbitrary `user_id` with a community they legitimately administer
     and still get back whatever `analytics-core` computes for that pair.
Both return 404 (not 403) on failure -- matches this repo's established
`community_in_tenant` failure convention (`community_activity.py` et al.):
"not found" for a resource the caller has no legitimate reason to know
exists, rather than confirming its existence via a 403.

No new owned schema: unlike most M-phase groups, this one binds no new
tables of its own (`services/schema.py`'s edit in this PR only ADDS a
missing `tenant_id` column to the EXISTING `communities` binding --
`community_in_tenant()` needs it and it was a pre-existing cross-group
gap, not a new table). `communities`/`community_members` are the same M1/
M6-owned tables every other Community-module blueprint already reads.

Upstream failures (gh-894): any `analytics-core` failure (4xx/5xx, timeout,
connection refused) is relayed as a 502 JSON envelope naming the upstream
dependency (`services/analytics_service.py::_relay`), never a masked 500.
"""

from __future__ import annotations

from typing import Any

from flask_core.auth import verify_jwt_token
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.secrets import require_secret_key
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request

from services import analytics_service as svc
from services.analytics_proxy import AnalyticsCoreProxyClient
from services.community_common import api_error, community_in_tenant
from services.current_user import get_current_user_id

analytics_bp = Blueprint("v1_analytics", __name__, url_prefix="/api/v1/analytics")

#: Scenario 4 -- pre-existing scope name (security.md SCOPE_BUNDLES table's
#: own `maintainer -> analytics:read` entry); see module docstring.
SCOPE_PLATFORM_READ = "analytics:read"
#: Scenario 2 -- follows this repo's `community.<group>:<action>` convention
#: (`community_activity.py`, `community_loyalty.py`, ...).
SCOPE_COMMUNITY_ADMIN = "community.analytics:admin"
#: Scenario 3 -- same scope `user_management.py`'s superadmin routes use.
SCOPE_USERS_ADMIN = "users:admin"

#: License-catalog Professional-tier flag (`libs/core_platform_module/
#: features.py`, `analytics.community_health`). Only this endpoint of the
#: 6 catalog `analytics.*` features maps onto a route THIS controller
#: owns -- `bad_actor_detection`/`user_journey`/`retention_cohorts`/
#: `engagement_funnels`/`advanced` gate a DIFFERENT, not-yet-ported admin
#: controller (`api.js`'s `adminApi.getAnalyticsBadActors`/`getAnalyticsRetention`/
#: etc, mounted at `/api/v1/admin/<communityId>/analytics/*`), out of
#: scope for this PR.
FEATURE_COMMUNITY_HEALTH = "waddles.analytics.community_health"

_proxy_client = AnalyticsCoreProxyClient()


def _is_super_admin(req: Any) -> bool:
    """Port of `req.user.isSuperAdmin` -- re-decode the bearer token's `roles` claim.

    Same self-contained re-decode `blueprints/v1/event.py::
    _build_user_context` and `flask_core.authz.require_scope` itself use
    (independent of `tenant_middleware`'s request-local state). `roles` is
    audit/display-only per security.md -- this never feeds an authz
    decision, only the `X-Caller-Role` metadata forwarded to
    `analytics-core` on an already-authorized request.
    """
    auth_header = req.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    secret_key = require_secret_key()
    payload = verify_jwt_token(auth_header[7:], secret_key)
    if payload is None:
        return False
    roles = payload.get("roles") or []
    return "super_admin" in roles


def _caller_role(req: Any) -> str:
    """Port of `req.user.isSuperAdmin ? 'superadmin' : 'user'`."""
    return "superadmin" if _is_super_admin(req) else "user"


def _clamp_limit(
    raw: str | None, *, default: int = 50, minimum: int = 1, maximum: int = 200
) -> int:
    """Port of `Math.min(200, Math.max(1, parseInt(req.query.limit || '50', 10)))`."""
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


# ---------------------------------------------------------------------------
# Scenario 1 -- self stats (any authenticated user, own resource)
# ---------------------------------------------------------------------------


@analytics_bp.route("/me/stats", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_my_stats() -> Any:
    """`GET /api/v1/analytics/me/stats`."""
    user_id = get_current_user_id(request)
    return await svc.user_self_stats(_proxy_client, user_id, user_id, _caller_role(request))


@analytics_bp.route("/me/reputation", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_my_reputation() -> Any:
    """`GET /api/v1/analytics/me/reputation`."""
    user_id = get_current_user_id(request)
    return await svc.user_reputation(_proxy_client, user_id, user_id, _caller_role(request))


# ---------------------------------------------------------------------------
# Scenario 2 -- community admin views member (tenant + membership scoped)
# ---------------------------------------------------------------------------


@analytics_bp.route("/community/<int:community_id>/members/<int:user_id>/stats", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_COMMUNITY_ADMIN)  # type: ignore[misc]
async def get_member_stats(community_id: int, user_id: int) -> Any:
    """`GET /api/v1/analytics/community/<communityId>/members/<userId>/stats`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    dal = current_app.config["dal"]
    if not community_in_tenant(dal, community_id, ctx):
        return api_error("Community not found", 404)
    if not svc.community_member_exists(dal, community_id, user_id):
        return api_error("Member not found", 404)
    caller_id = get_current_user_id(request)
    return await svc.user_community_stats(_proxy_client, user_id, community_id, caller_id)


@analytics_bp.route(
    "/community/<int:community_id>/members/<int:user_id>/reputation", methods=["GET"]
)
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_COMMUNITY_ADMIN)  # type: ignore[misc]
async def get_member_reputation(community_id: int, user_id: int) -> Any:
    """`GET /api/v1/analytics/community/<communityId>/members/<userId>/reputation`."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    dal = current_app.config["dal"]
    if not community_in_tenant(dal, community_id, ctx):
        return api_error("Community not found", 404)
    if not svc.community_member_exists(dal, community_id, user_id):
        return api_error("Member not found", 404)
    caller_id = get_current_user_id(request)
    return await svc.user_reputation(_proxy_client, user_id, caller_id, _caller_role(request))


# ---------------------------------------------------------------------------
# Scenario 4 -- platform overview (analytics consumer OR superadmin)
# ---------------------------------------------------------------------------


@analytics_bp.route("/platform/overview", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_PLATFORM_READ)  # type: ignore[misc]
async def get_platform_overview() -> Any:
    """`GET /api/v1/analytics/platform/overview`."""
    return await svc.platform_overview(_proxy_client)


@analytics_bp.route("/platform/reputation", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_PLATFORM_READ)  # type: ignore[misc]
async def get_platform_reputation() -> Any:
    """`GET /api/v1/analytics/platform/reputation`."""
    return await svc.reputation_distribution(_proxy_client)


@analytics_bp.route("/platform/growth", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_PLATFORM_READ)  # type: ignore[misc]
async def get_platform_growth() -> Any:
    """`GET /api/v1/analytics/platform/growth`."""
    period = request.args.get("period", "90d")
    return await svc.growth_trends(_proxy_client, period)


@analytics_bp.route("/platform/activity", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_PLATFORM_READ)  # type: ignore[misc]
async def get_platform_activity() -> Any:
    """`GET /api/v1/analytics/platform/activity`."""
    return await svc.activity_breakdown(_proxy_client)


@analytics_bp.route("/platform/community-health", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_PLATFORM_READ)  # type: ignore[misc]
async def get_platform_community_health() -> Any:
    """`GET /api/v1/analytics/platform/community-health`.

    Professional+ (license catalog `analytics.community_health`, PostHog
    flag `waddles.analytics.community_health`) -- gated here, not in
    `analytics-core`'s own `health_service.py` (that gate covers a
    different call path, per-community `calculate_health_score()`, which
    this platform-wide summaries endpoint never touches).
    """
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    if not await feature_enabled(FEATURE_COMMUNITY_HEALTH, tenant=ctx.tenant_slug):
        return api_error("Community health analytics requires a Professional plan or higher", 402)
    limit = _clamp_limit(request.args.get("limit"))
    return await svc.community_health_summaries(_proxy_client, limit)


# ---------------------------------------------------------------------------
# Scenario 3 -- superadmin views any user
# ---------------------------------------------------------------------------


@analytics_bp.route("/admin/users/<int:user_id>/stats", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_USERS_ADMIN)  # type: ignore[misc]
async def get_admin_user_stats(user_id: int) -> Any:
    """`GET /api/v1/analytics/admin/users/<userId>/stats`."""
    caller_id = get_current_user_id(request)
    return await svc.user_self_stats(_proxy_client, user_id, caller_id, _caller_role(request))


@analytics_bp.route("/admin/users/<int:user_id>/reputation", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope(SCOPE_USERS_ADMIN)  # type: ignore[misc]
async def get_admin_user_reputation(user_id: int) -> Any:
    """`GET /api/v1/analytics/admin/users/<userId>/reputation`."""
    caller_id = get_current_user_id(request)
    return await svc.user_reputation(_proxy_client, user_id, caller_id, _caller_role(request))


BLUEPRINTS: list[Blueprint] = [analytics_bp]
