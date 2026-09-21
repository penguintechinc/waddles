"""Community-membership authorization -- the shared IDOR/BOLA fix for `community_id`.

A caller holding a valid tenant-scoped bearer JWT still names *which*
community they act on via a URL path parameter (`community_id`) that the
client fully controls. `tenant_middleware` + `require_scope` alone do not
close this: a caller with a global `community:write` scope grant passes
`require_scope` for ANY `community_id`, including one belonging to a
different tenant, or one they hold no membership in at all -- the classic
Broken Object Level Authorization (OWASP A01) class. This module is the
generic (any core service) version of the pattern first ported into this
repo at `hub_api/services/community_access.py` (Node's
`requireCommunityAdmin`) and then re-ported per-service at
`core/svc_streaming/services/community_access.py` -- centralized here so
future services import it rather than re-copying the query logic a third
time.

Usage -- either wire it at the route level (mirrors svc_streaming)::

    @api_bp.route("/<int:community_id>/config", methods=["PUT"])
    @tenant_middleware
    async def set_config(community_id: int):
        ctx = get_tenant_context(request)
        user_id = decode_caller_user_id(request)
        await require_admin(async_dal, dal, request, ctx, community_id=community_id, user_id=user_id)
        ...

or install it once for an entire blueprint via `install_community_scoped_auth`,
which resolves the tenant AND (when the route has a `community_id` path
parameter) the community membership/admin check together, so a route added
later can't accidentally ship without either check.

Every table this module reads (`tenants`, `communities`, `community_members`)
is owned by hub-api's own migrations (000/058) -- never created here
(`migrate=False` unconditionally in production); `bind_shared_read_tables`
defines only the minimal read-only field subset each check needs against
the SAME shared Postgres instance every service's own `DATABASE_URL`
already points at (backend-database.md Per-Service Database Accounts: same
DB, per-service scoped read-only grant).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydal import Field
from quart import Blueprint, Quart, Request

from .auth import verify_jwt_token
from .tenancy import TenantContext, TenantIsolationError, resolve_tenant_context

logger = logging.getLogger(__name__)

#: `community_members.role` values treated as admin-tier -- matches
#: `hub_api/services/community_access.py`'s `_ADMIN_ROLES` (the two
#: system-seeded owner/admin role names).
_ADMIN_ROLES = ("community-owner", "community-admin")

#: HTTP methods that mutate community-scoped state -- these require
#: `require_admin`; everything else (GET, HEAD, OPTIONS) only requires
#: `require_member`. Used by `install_community_scoped_auth`'s default.
DEFAULT_ADMIN_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(slots=True)
class CommunityAccessError(Exception):
    """Raised when the caller isn't authorized for the given community. Maps to 403."""

    message: str = "Community access required"


@dataclass(slots=True)
class CallerIdentityError(Exception):
    """Raised when the bearer token doesn't carry a usable subject. Maps to 401."""

    message: str = "Authentication required"


def bind_shared_read_tables(dal: Any, *, migrate: bool = False) -> None:
    """Define the read-only field subset of `tenants`/`communities`/`community_members`.

    Idempotent (guarded on `community_members`). `migrate=False` in
    production always -- these tables are never this service's to create.
    Tests pass `migrate=True` against a throwaway sqlite DB.
    """
    if "community_members" in dal.tables:
        return

    if "tenants" not in dal.tables:
        dal.define_table(
            "tenants",
            Field("slug", "string", length=100),
            Field("is_active", "boolean", default=True),
            migrate=migrate,
        )

    if "communities" not in dal.tables:
        dal.define_table(
            "communities",
            Field("tenant_id", "integer", notnull=True),
            migrate=migrate,
        )

    dal.define_table(
        "community_members",
        Field("community_id", "integer", notnull=True),
        # VARCHAR in Postgres (legacy platform-identity membership model),
        # not a FK -- matches `hub_api/services/community_authz.py`'s own
        # note; compared as `str(user_id)` at every call site.
        Field("user_id", "string", length=255),
        Field("role", "string", length=50, default="member"),
        Field("is_active", "boolean", default=True),
        migrate=migrate,
    )


def _is_super_admin(request: Request) -> bool:
    """True if the caller's JWT `roles` claim names `super_admin` -- bypasses both checks below."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    secret_key = os.getenv("SECRET_KEY", "change-me-in-production")
    payload = verify_jwt_token(auth_header[7:], secret_key)
    if payload is None:
        return False
    return "super_admin" in (payload.get("roles") or [])


async def _require_community_in_tenant(
    async_dal: Any, dal: Any, ctx: TenantContext, *, community_id: int
) -> None:
    """Raise `CommunityAccessError` unless `community_id` resolves to a row inside `ctx`'s tenant.

    403, not 404: never confirms to an unauthorized caller that a given
    `community_id` exists at all in another tenant.
    """
    bind_shared_read_tables(dal)
    query = dal.communities.id == community_id
    query = query & (dal.communities.tenant_id == ctx.tenant_id)
    rows = await async_dal.select_async(dal(query), dal.communities.id)
    if not rows:
        raise CommunityAccessError("Community access required")


async def require_admin(
    async_dal: Any,
    dal: Any,
    request: Request,
    ctx: TenantContext,
    *,
    community_id: int,
    user_id: int,
) -> None:
    """Raise `CommunityAccessError` unless `user_id` is an active owner/admin of `community_id`.

    Gates every write path in the community's resource space.
    """
    if _is_super_admin(request):
        return

    await _require_community_in_tenant(async_dal, dal, ctx, community_id=community_id)

    membership_query = dal(
        (dal.community_members.community_id == community_id)
        & (dal.community_members.user_id == str(user_id))
        & (dal.community_members.is_active == True)  # noqa: E712 - pydal Field comparison
        & (dal.community_members.role.belongs(_ADMIN_ROLES))
    )
    rows = await async_dal.select_async(membership_query, dal.community_members.id)
    if not rows:
        raise CommunityAccessError("Community admin access required")


async def require_member(
    async_dal: Any,
    dal: Any,
    request: Request,
    ctx: TenantContext,
    *,
    community_id: int,
    user_id: int,
) -> None:
    """Raise `CommunityAccessError` unless `user_id` is an ACTIVE member (any role) of `community_id`.

    Gates read-only paths.
    """
    if _is_super_admin(request):
        return

    await _require_community_in_tenant(async_dal, dal, ctx, community_id=community_id)

    membership_query = dal(
        (dal.community_members.community_id == community_id)
        & (dal.community_members.user_id == str(user_id))
        & (dal.community_members.is_active == True)  # noqa: E712 - pydal Field comparison
    )
    rows = await async_dal.select_async(membership_query, dal.community_members.id)
    if not rows:
        raise CommunityAccessError("Community membership required")


def decode_caller_user_id(request: Request) -> int:
    """Re-decode the bearer JWT for the caller's `sub` (user id). Raises `CallerIdentityError` if missing/invalid."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise CallerIdentityError("Authentication required")
    secret_key = os.getenv("SECRET_KEY", "change-me-in-production")
    payload = verify_jwt_token(auth_header[7:], secret_key)
    if payload is None:
        raise CallerIdentityError("Invalid or expired token")
    try:
        return int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CallerIdentityError("Token missing subject claim") from exc


def install_community_scoped_auth(
    blueprint: Blueprint | Quart,
    *,
    community_param: str = "community_id",
    admin_methods: frozenset[str] = DEFAULT_ADMIN_METHODS,
    async_dal_key: str = "async_dal",
    dal_key: str = "dal",
    exempt_paths: frozenset[str] = frozenset(),
) -> None:
    """Register a `before_request` hook enforcing tenant + community-membership auth.

    `blueprint` accepts a `Quart` app too (both expose the identical
    `before_request` decorator) -- for services that register routes
    directly on `app` rather than a `Blueprint`, pass `app` and use
    `exempt_paths` to carve out `/health`/`/healthz`/`/metrics` (which
    must stay reachable for K8s liveness/readiness probes without a JWT).

    Applies to every route present or future -- unlike a per-route
    decorator, a new route can't ship without this check by omission.
    Ordering matches security.md (tenant before scope/resource):

    1. If `request.path` is in `exempt_paths`, skip entirely (no auth
       required) -- only meaningful for the whole-`app` case above; a
       `Blueprint`'s own routes are never health/metrics endpoints.
    2. Decode + verify the bearer JWT, resolve `TenantContext` (same logic
       `tenant_middleware` uses) -- 401 on missing/invalid token, 403 on
       tenant resolution failure.
    3. If the matched route has a `community_param` path parameter (e.g.
       `community_id`), additionally require community admin (for
       `admin_methods`, default the mutating HTTP verbs) or member
       (everything else) -- 403 on failure.

    Routes with no `community_param` in their path only get step 2 (tenant
    + valid token) -- callers needing scope-level enforcement on those
    still stack `require_scope` themselves.
    """

    @blueprint.before_request
    async def _enforce_tenant_and_community_auth() -> tuple[dict[str, object], int] | None:
        from quart import current_app, request

        from .api_utils import error_response

        if exempt_paths and request.path in exempt_paths:
            return None

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            logger.warning(
                "install_community_scoped_auth: no bearer token",
                extra={"event_type": "AUTH", "action": "community_scoped_auth", "result": "UNAUTHORIZED"},
            )
            return error_response("Authentication required", status_code=401)

        token = auth_header[7:]
        secret_key = os.getenv("SECRET_KEY", "change-me-in-production")
        payload = verify_jwt_token(token, secret_key)
        if payload is None:
            return error_response("Invalid or expired token", status_code=401)

        dal = current_app.config.get(dal_key)
        try:
            ctx = await resolve_tenant_context(payload, dal)
        except TenantIsolationError as exc:
            logger.warning(
                "install_community_scoped_auth: tenant resolution failed: %s",
                exc,
                extra={"event_type": "AUTH", "action": "community_scoped_auth", "result": "FORBIDDEN"},
            )
            return error_response(str(exc), status_code=403)

        request.tenant_context = ctx

        raw_community_id = (request.view_args or {}).get(community_param)
        if raw_community_id is not None:
            async_dal = current_app.config.get(async_dal_key)
            try:
                user_id = decode_caller_user_id(request)
                check: Callable[..., Any] = (
                    require_admin if request.method in admin_methods else require_member
                )
                await check(
                    async_dal,
                    dal,
                    request,
                    ctx,
                    community_id=int(raw_community_id),
                    user_id=user_id,
                )
            except CallerIdentityError as exc:
                return error_response(exc.message, status_code=401)
            except CommunityAccessError as exc:
                return error_response(exc.message, status_code=403)

        return None
