"""
Tenant Isolation
================

Tenant is a hard boundary (security.md Tenant Isolation): every token
carries a `tenant` claim, tenant resolution runs before any scope check,
and every query touching tenant-scoped data is constrained at the ORM
layer -- never from a request body, query string, or path parameter.

This module provides the three load-bearing pieces:
- `TenantContext`: the validated tenant for a request.
- `tenant_middleware`: decodes the bearer JWT and publishes `TenantContext`
  before the wrapped handler (and any scope check inside it) runs.
- `tenant_scoped`: the single ORM-layer helper every tenant-scoped query
  must go through, so 178+ hand-written filters don't have to each get it
  right independently.

The default tenant (`DEFAULT_TENANT_SLUG`, matching the catch-all tenant row,
slug `default`) is a tenant like any other -- it runs through
this exact same code with N=1, never a `if tenant == "default": skip()`
shortcut. See docs/plans/2026-08-26-v3-scbm-apps-design.md, Identity and
data scoping.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from typing import Any, FrozenSet, Optional

from pydal.objects import Field, Query, Table

from .auth import DEFAULT_TENANT_SLUG, verify_jwt_token
from .database import db_operation

logger = logging.getLogger(__name__)


class TenantIsolationError(Exception):
    """Raised when a request or query cannot be safely bound to a tenant."""


@dataclass(slots=True, frozen=True)
class TenantContext:
    """
    The validated tenant for the current request.

    Built exclusively by `tenant_middleware` from the decoded JWT `tenant`
    claim (or a verified Community Access Token's bound community/tenant) --
    never from client-supplied body/query/path data. Frozen so nothing
    downstream can swap tenants mid-request.
    """

    tenant_id: int
    tenant_slug: str
    is_default: bool = False
    cat_community_id: Optional[int] = None
    cat_created_by_user_id: Optional[int] = None


@dataclass(slots=True, frozen=True)
class CatPrincipal:
    """Verified Community Access Token (`wdl_c_...`) principal."""

    token_id: int
    community_id: int
    tenant_id: int
    tenant_slug: str
    is_default: bool
    scopes: FrozenSet[str]
    created_by_user_id: Optional[int] = None


# ---------------------------------------------------------------------------
# ORM-layer scoping -- the one correct shape, not 178 hand-written filters
# ---------------------------------------------------------------------------


def _resolve_table(query: Query) -> Table:
    """Walk a (possibly AND-chained) pydal Query down to its leftmost Field's Table."""
    node: Any = query
    while isinstance(node, Query) and not isinstance(node.first, Field):
        node = node.first
    if isinstance(node, Query) and isinstance(node.first, Field):
        return node.first.table
    raise TenantIsolationError(
        "tenant_scoped() could not resolve a table from the given query -- "
        "pass a query built directly off a Field comparison, e.g. "
        "db.communities.id == cid"
    )


def tenant_scoped(query: Query, ctx: TenantContext) -> Query:
    """
    AND a tenant filter onto `query` at the ORM layer.

    This is the ONLY correct shape for tenant scoping -- a module does
    `db(tenant_scoped(db.communities.id == cid, ctx))` and cannot forget the
    tenant filter, unlike a hand-written `.tenant_id == ctx.tenant_id`
    repeated at every call site. Resolves the table being queried and
    constrains it either directly (a `tenant_id` column) or one hop through
    `community_id -> communities.tenant_id`. Tables with neither raise
    loudly rather than silently returning an unscoped query.
    """
    table = _resolve_table(query)
    dal = table._db

    if "tenant_id" in table.fields:
        return query & (table.tenant_id == ctx.tenant_id)

    if "community_id" in table.fields:
        tenant_communities = dal(dal.communities.tenant_id == ctx.tenant_id)._select(
            dal.communities.id
        )
        return query & table.community_id.belongs(tenant_communities)

    raise TenantIsolationError(
        f"table '{table._tablename}' has neither tenant_id nor community_id -- "
        "add one before this table can be safely tenant-scoped"
    )


# ---------------------------------------------------------------------------
# Request-layer resolution -- runs before any scope check
# ---------------------------------------------------------------------------


async def resolve_tenant_context(payload: dict[str, Any], dal: Any) -> TenantContext:
    """
    Build a `TenantContext` from a verified JWT payload's `tenant` claim.

    `verify_jwt_token` rejects any token without a non-empty `tenant` claim
    (the legacy default-tenant fallback is gone -- H-2 Phase 0), so
    `payload["tenant"]` is always present for a verified payload; a payload
    that still lacks it (a caller passing an unverified dict) is a hard 403,
    never a silent default.

    This is the near-universal per-request chokepoint (`tenant_middleware`
    and `install_community_scoped_auth` both call it on almost every
    hub-api route), which is exactly why its raw `.select()` is wrapped in
    `db_operation()`: `dal` is the shared, un-pooled raw pydal DAL (see
    `database.py`'s docstring), and any exception here -- including the
    `InFailedSqlTransaction` cascade from some earlier, unrelated failed
    write that never rolled back -- must not leave the connection
    poisoned for every request that follows.
    """
    tenant_slug = payload.get("tenant")
    if not tenant_slug:
        raise TenantIsolationError("token carries no tenant claim")

    with db_operation(dal, "resolve_tenant_context:tenants.select"):
        row = dal(dal.tenants.slug == tenant_slug).select().first()
    if row is None:
        raise TenantIsolationError(f"tenant '{tenant_slug}' does not exist")
    if not row.is_active:
        raise TenantIsolationError(f"tenant '{tenant_slug}' is inactive")

    return TenantContext(
        tenant_id=row.id,
        tenant_slug=row.slug,
        is_default=(row.slug == DEFAULT_TENANT_SLUG),
    )


def _log_auth_rejection(reason: str, path: str) -> None:
    """Log a 401 rejection with a reason code and path only (never the token or any PII)."""
    logger.info(
        "auth rejected reason=%s path=%s",
        reason,
        path,
        extra={"event_type": "AUTH", "action": "tenant_middleware", "result": "UNAUTHORIZED"},
    )


def _parse_cat_scopes(raw_scopes: Any) -> FrozenSet[str]:
    """Parse CAT `scopes` column (JSON list or space-delimited string) into a frozenset."""
    if isinstance(raw_scopes, (list, tuple, set, frozenset)):
        return frozenset(str(s).strip() for s in raw_scopes if str(s).strip())
    if isinstance(raw_scopes, str):
        stripped = raw_scopes.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return frozenset(str(s).strip() for s in parsed if str(s).strip())
            except ValueError:
                pass
        return frozenset(stripped.split())
    return frozenset()


def resolve_cat_token(dal: Any, token: str) -> Optional[CatPrincipal]:
    """Resolve and validate a `wdl_c_` Community Access Token against `dal`.

    Returns `None` for invalid, revoked, or expired tokens (caller responds
    401/403). Raises `TenantIsolationError` if the bound community/tenant is
    missing or inactive (caller responds 403).
    """
    if not token or not token.startswith("wdl_c_") or len(token) <= len("wdl_c_"):
        return None
    if dal is None:
        return None
    tables = getattr(dal, "tables", ())
    if (
        "community_access_tokens" not in tables
        or "communities" not in tables
        or "tenants" not in tables
    ):
        return None

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with db_operation(dal, "resolve_cat_token:cat.select"):
        row = dal(dal.community_access_tokens.token_hash == token_hash).select().first()
    if row is None:
        return None
    if bool(getattr(row, "is_revoked", False)):
        return None

    expires_at = getattr(row, "expires_at", None)
    if expires_at is not None:
        if isinstance(expires_at, str):
            try:
                expires_at = datetime.fromisoformat(expires_at)
            except ValueError:
                return None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            return None

    with db_operation(dal, "resolve_cat_token:community.select"):
        community = dal(dal.communities.id == row.community_id).select().first()
    if community is None:
        return None
    if not getattr(community, "is_active", True):
        raise TenantIsolationError("community is inactive")

    tenant_id = getattr(community, "tenant_id", None)
    if tenant_id is None:
        raise TenantIsolationError("community has no tenant")

    with db_operation(dal, "resolve_cat_token:tenant.select"):
        tenant = dal(dal.tenants.id == tenant_id).select().first()
    if tenant is None:
        raise TenantIsolationError("tenant does not exist")
    if not tenant.is_active:
        raise TenantIsolationError(f"tenant '{tenant.slug}' is inactive")

    return CatPrincipal(
        token_id=int(row.id),
        community_id=int(row.community_id),
        tenant_id=int(tenant.id),
        tenant_slug=str(tenant.slug),
        is_default=(tenant.slug == DEFAULT_TENANT_SLUG),
        scopes=_parse_cat_scopes(getattr(row, "scopes", None)),
        created_by_user_id=getattr(row, "created_by_user_id", None),
    )


def tenant_middleware(f):
    """
    Decorator enforcing security.md's ordering contract: tenant before scope.

    Decodes the bearer JWT (or resolves a `wdl_c_` Community Access Token),
    resolves and publishes `TenantContext` on `request.tenant_context`, and
    only then calls the wrapped handler. Must be the outermost auth decorator
    on every protected route -- any scope check has to run inside it, never
    outside, or it answers the right question against an unverified tenant.
    """

    @wraps(f)
    async def decorated_function(*args, **kwargs):
        from quart import current_app, request

        from .api_utils import error_response

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            _log_auth_rejection("no_bearer", request.path)
            return error_response("Authentication required", status_code=401)

        token = auth_header[7:]
        dal = current_app.config.get("dal")

        if token.startswith("wdl_c_"):
            try:
                cat = resolve_cat_token(dal, token)
            except TenantIsolationError as exc:
                logger.warning(
                    "CAT tenant resolution failed: %s",
                    exc,
                    extra={
                        "event_type": "AUTH",
                        "action": "tenant_middleware",
                        "result": "FORBIDDEN",
                    },
                )
                return error_response(str(exc), status_code=403)

            if cat is None:
                _log_auth_rejection("invalid_or_expired_cat", request.path)
                return error_response("Invalid or expired token", status_code=401)

            view_cid = (request.view_args or {}).get("community_id")
            if view_cid is not None and view_cid != cat.community_id:
                logger.warning(
                    "CAT community mismatch path=%s",
                    request.path,
                    extra={
                        "event_type": "AUTH",
                        "action": "tenant_middleware",
                        "result": "FORBIDDEN",
                    },
                )
                return error_response("Token not authorized for this community", status_code=403)

            request.tenant_context = TenantContext(
                tenant_id=cat.tenant_id,
                tenant_slug=cat.tenant_slug,
                is_default=cat.is_default,
                cat_community_id=cat.community_id,
                cat_created_by_user_id=cat.created_by_user_id,
            )
            return await f(*args, **kwargs)

        payload = verify_jwt_token(token)
        if payload is None:
            _log_auth_rejection("invalid_or_expired_token", request.path)
            return error_response("Invalid or expired token", status_code=401)

        try:
            ctx = await resolve_tenant_context(payload, dal)
        except TenantIsolationError as exc:
            logger.warning(
                "Tenant resolution failed: %s",
                exc,
                extra={
                    "event_type": "AUTH",
                    "action": "tenant_middleware",
                    "result": "FORBIDDEN",
                },
            )
            return error_response(str(exc), status_code=403)

        request.tenant_context = ctx
        return await f(*args, **kwargs)

    return decorated_function


def get_tenant_context(request: Any) -> Optional[TenantContext]:
    """Return the `TenantContext` `tenant_middleware` published on `request`, if any."""
    return getattr(request, "tenant_context", None)
