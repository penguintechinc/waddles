"""
HTTP-Layer Scope Enforcement
=============================

The missing middle of the tenant -> scope -> feature chain
(security.md Authentication & Authorization). Feature contracts declare
``requires_scopes`` (see ``feature_contract.py``) and ``tenant_middleware``
(``tenancy.py``) resolves the request's ``TenantContext``, but until now
nothing at the HTTP layer checked the caller's JWT ``scope`` claim against
a handler's declared requirement -- scopes were declared and unenforced.

Ordering contract (security.md: tenant before scope, never role names):
``tenant_middleware`` (outermost) -> ``require_scope`` -> ``feature_enabled``
check / handler body (innermost). ``require_scope`` independently
re-validates the bearer token via the same :func:`verify_jwt_token` helper
``tenant_middleware`` uses, rather than reaching into ``tenancy.py``'s
request-local state -- this keeps the decorator self-contained, testable in
isolation, and leaves the heavily-regression-tested tenant-isolation code
path untouched. In the real request path ``tenant_middleware`` has already
proven the token is valid (its own 401 fires first); this second decode
only extracts the ``scope`` claim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, FrozenSet, Optional

from .auth import verify_jwt_token
from .secrets import require_secret_key

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class AuthzDecision:
    """The verdict of one :func:`require_scope` check, published on ``request.authz_decision``.

    Read after the handler by audit middleware (hub-api's ``audit_http``) so authorization
    decisions -- denials above all -- reach the tamper-evident audit log without every route
    having to remember to log them. ``subject`` is the verified token's ``sub`` (``None`` when
    there was no valid token); ``granted`` is kept for in-process checks only and excluded from
    ``repr`` so it can never be logged by accident.
    """

    required_scopes: tuple[str, ...]
    allowed: bool
    reason: str
    subject: Optional[str] = None
    granted: FrozenSet[str] = field(default=frozenset(), repr=False)


def get_authz_decision(request: Any) -> Optional[AuthzDecision]:
    """Return the :class:`AuthzDecision` ``require_scope`` published on ``request``, if any."""
    decision = getattr(request, "authz_decision", None)
    return decision if isinstance(decision, AuthzDecision) else None


def _parse_scope_claim(raw_scope: Any) -> FrozenSet[str]:
    """Space-delimited OIDC ``scope`` claim -> a set of scope strings.

    Missing, empty, or non-string claims all resolve to the empty set --
    "no scopes granted", never an error swallowed silently.
    """
    if not raw_scope or not isinstance(raw_scope, str):
        return frozenset()
    return frozenset(raw_scope.split())


def _scope_covers(granted: str, required: str) -> bool:
    """
    True if a single granted scope string satisfies ``required``.

    Exact match always satisfies. Wildcard match follows the existing
    ``SCOPE_BUNDLES`` convention (``auth.py``, security.md's
    admin/maintainer/viewer table): only the *resource* half may be ``*``
    (e.g. ``*:write`` covers ``customer.account:write``) and the *action*
    half must match exactly. No bundle grants a bare unbounded ``*``
    (``test_no_bundle_grants_unbounded_wildcard`` in test_tenancy.py) and
    this check mirrors that -- the action half is never wildcarded, and a
    granted scope missing the ``resource:action`` shape never matches.
    """
    if granted == required:
        return True
    if ":" not in granted or ":" not in required:
        return False
    granted_resource, _, granted_action = granted.partition(":")
    required_resource, _, required_action = required.partition(":")
    return granted_resource == "*" and granted_action == required_action


def has_required_scopes(
    granted_scopes: FrozenSet[str], required_scopes: tuple[str, ...]
) -> bool:
    """True iff every scope in ``required_scopes`` is covered by some granted scope."""
    if not granted_scopes:
        return False
    return all(
        any(_scope_covers(granted, required) for granted in granted_scopes)
        for required in required_scopes
    )


def require_scope(*required_scopes: str) -> Callable:
    """
    Decorator enforcing that the caller's JWT carries every scope required.

    Usage (order matters -- ``tenant_middleware`` must be outermost)::

        @api_bp.route("/accounts", methods=["POST"])
        @tenant_middleware
        @require_scope("customer.account:write")
        @async_endpoint
        async def create_account(): ...

    Re-validates the bearer token (:func:`verify_jwt_token`, same secret
    lookup as ``tenant_middleware``) and checks its ``scope`` claim
    (space-delimited, OIDC convention) against every scope passed here,
    honoring the ``SCOPE_BUNDLES`` ``*:action`` wildcard convention (see
    :func:`_scope_covers`). Fails closed: a missing/invalid token, or a
    missing/empty ``scope`` claim that doesn't cover every requirement, is
    a 403 -- never a silent pass. Checks scopes only, never role names, per
    security.md.

    Raises:
        ValueError: if called with no required scopes -- a bare
            ``@require_scope()`` would silently authorize everything,
            which is refused at decoration time rather than at runtime.
    """
    if not required_scopes:
        raise ValueError("require_scope() needs at least one required scope")

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        async def decorated_function(*args: Any, **kwargs: Any) -> Any:
            from quart import request

            from .api_utils import error_response

            auth_header = request.headers.get("Authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                request.authz_decision = AuthzDecision(required_scopes, False, "no_bearer")
                logger.warning(
                    "require_scope: no bearer token",
                    extra={
                        "event_type": "AUTHZ",
                        "action": "require_scope",
                        "result": "FORBIDDEN",
                    },
                )
                return error_response("Authentication required", status_code=403)

            token = auth_header[7:]
            secret_key = require_secret_key()
            payload = verify_jwt_token(token, secret_key)
            if payload is None:
                request.authz_decision = AuthzDecision(required_scopes, False, "invalid_token")
                logger.warning(
                    "require_scope: invalid or expired token",
                    extra={
                        "event_type": "AUTHZ",
                        "action": "require_scope",
                        "result": "FORBIDDEN",
                    },
                )
                return error_response("Invalid or expired token", status_code=403)

            granted = _parse_scope_claim(payload.get("scope"))
            subject = payload.get("sub")
            subject = None if subject is None else str(subject)
            if not has_required_scopes(granted, required_scopes):
                request.authz_decision = AuthzDecision(
                    required_scopes, False, "insufficient_scope", subject, granted
                )
                logger.warning(
                    "require_scope: insufficient scope required=%s granted=%s",
                    required_scopes,
                    sorted(granted),
                    extra={
                        "event_type": "AUTHZ",
                        "action": "require_scope",
                        "result": "FORBIDDEN",
                    },
                )
                return error_response("Insufficient scope", status_code=403)

            request.authz_decision = AuthzDecision(required_scopes, True, "ok", subject, granted)
            return await f(*args, **kwargs)

        return decorated_function

    return decorator
