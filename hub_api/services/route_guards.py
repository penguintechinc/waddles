"""Route decorators that put a community admin/member/owner guard on `*:read` routes.

Every authenticated session carries the viewer bundle's universal `*:read`, so a route whose only
scope is `X:read` is reachable by any signed-in user in the tenant. A tenant check
(`community_in_tenant`) proves the community belongs to the caller's tenant, not that the caller
belongs to the community. These decorators add the missing check and stamp the wrapped view with
`__authz_guard__` so the CI walker in `tests/test_read_route_guards.py` can prove no
community-scoped read route ships without one.

Order on a route: `@tenant_middleware`, `@require_scope(...)`, then one guard below.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any, Final

from flask_core.tenancy import get_tenant_context
from quart import current_app, request

from services.community_authz import resolve_caller_standing_sync
from services.community_common import api_error, community_in_tenant
from services.current_user import get_current_user_id
from services.errors import ApiError
from services.global_identity import is_global_admin_sync

logger = logging.getLogger(__name__)

#: Attribute stamped on a guarded view (propagates through `functools.wraps`).
GUARD_ATTR: Final = "__authz_guard__"
GUARD_ADMIN: Final = "admin"
GUARD_MEMBER: Final = "member"
GUARD_OWNER: Final = "owner"


def _guard(kind: str) -> Callable[[Any], Any]:
    """Build a decorator enforcing `kind` ("admin" | "member") for the route's `community_id`."""

    def decorate(fn: Any) -> Any:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            community_id = kwargs.get("community_id")
            if not isinstance(community_id, int):
                return api_error("Community not found", status_code=404)
            ctx = get_tenant_context(request)
            if ctx is None:
                return api_error("Authentication required", status_code=401)
            dal = current_app.config["dal"]
            if not community_in_tenant(dal, community_id, ctx):
                return api_error("Community not found", status_code=404)
            try:
                standing = resolve_caller_standing_sync(request, dal, community_id=community_id)
            except ApiError as exc:
                logger.debug("handled %s in route guard: %s", type(exc).__name__, exc)
                return api_error(exc.message, exc.status_code, exc.code)
            allowed = standing.is_admin if kind == GUARD_ADMIN else standing.is_member
            if not allowed:
                label = "Community admin" if kind == GUARD_ADMIN else "Community membership"
                return api_error(f"{label} access required", status_code=403)
            return await fn(*args, **kwargs)

        setattr(wrapper, GUARD_ATTR, kind)
        return wrapper

    return decorate


#: 403 unless the caller administers the community (404 for a foreign-tenant community).
community_admin_guard = _guard(GUARD_ADMIN)
#: 403 unless the caller is an active, non-banned member (admins qualify; 404 foreign tenant).
community_member_guard = _guard(GUARD_MEMBER)


def mark_guarded(kind: str) -> Callable[[Any], Any]:
    """Stamp a view whose handler already enforces `kind` inline (owner/own-data checks)."""

    def decorate(fn: Any) -> Any:
        setattr(fn, GUARD_ATTR, kind)
        return fn

    return decorate


GUARD_PLATFORM: Final = "platform"
GUARD_TENANT_ADMIN: Final = "tenant-admin"


def tenant_admin_guard(fn: Any) -> Any:
    """403 unless the caller is an admin of the request's tenant (or a global admin)."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        ctx = get_tenant_context(request)
        if ctx is None:
            return api_error("Authentication required", status_code=401)
        try:
            user_id = get_current_user_id(request)
        except ApiError as exc:
            return api_error(exc.message, exc.status_code, exc.code)
        dal = current_app.config["dal"]
        row = (
            dal(
                (dal.tenant_admins.user_id == user_id)
                & (dal.tenant_admins.tenant_id == ctx.tenant_id)
            )
            .select()
            .first()
        )
        if row is None and not is_global_admin_sync(dal, user_id):
            return api_error("Tenant admin access required", status_code=403)
        return await fn(*args, **kwargs)

    setattr(wrapper, GUARD_ATTR, GUARD_TENANT_ADMIN)
    return wrapper


def _caller_is_platform_analyst(dal: Any, user_id: int) -> bool:
    """True for a global admin (system-tenant admin) or a `hub_users.is_analytics_consumer` user.

    DB-authoritative: a token's `analytics:read` is NOT proof -- every tenant maintainer bundle
    carries it literally and the viewer's `*:read` covers it by wildcard.
    """
    if is_global_admin_sync(dal, user_id):
        return True
    if "hub_users" not in dal.tables or "is_analytics_consumer" not in dal.hub_users.fields:
        return False
    row = dal(dal.hub_users.id == user_id).select(dal.hub_users.is_analytics_consumer).first()
    return bool(row is not None and row.is_analytics_consumer)


def platform_analytics_guard(fn: Any) -> Any:
    """403 unless the caller is a platform/system admin or an analytics consumer.

    Platform-wide analytics span every tenant, so a tenant-scoped session (even one holding
    `analytics:read` through `*:read` or a maintainer bundle) must never reach them.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            user_id = get_current_user_id(request)
        except ApiError as exc:
            return api_error(exc.message, exc.status_code, exc.code)
        if not _caller_is_platform_analyst(current_app.config["dal"], user_id):
            return api_error("Platform analytics access required", status_code=403)
        return await fn(*args, **kwargs)

    setattr(wrapper, GUARD_ATTR, GUARD_PLATFORM)
    return wrapper
