"""Resolve the calling user's identity from the bearer JWT.

`flask_core.tenancy.tenant_middleware` publishes `request.tenant_context`
(tenant only); `flask_core.authz.require_scope` re-validates the bearer
token independently rather than reaching into that request-local state
(see its own docstring). Self-service routes in this group (a user's own
profile/identities/passkeys) need the caller's `sub` (user id), which
neither primitive exposes -- this module follows `require_scope`'s own
precedent of an independent, self-contained re-decode rather than adding
a new field to `flask_core.tenancy.TenantContext` (shared infra several
other M-phase groups build on; not touched by this PR).
"""

from __future__ import annotations

from flask_core.auth import verify_jwt_token
from flask_core.authz import parse_scope_claim
from flask_core.secrets import require_secret_key
from quart import Request

from services.errors import unauthorized


def get_current_user_id(request: Request) -> int:
    """Return the authenticated caller's `hub_users.id` as an int, or raise 401.

    Re-validates the bearer token via the same `verify_jwt_token` helper
    `tenant_middleware`/`require_scope` use. Safe to call after
    `tenant_middleware` has already run (token is known-valid at that
    point) -- this is a second, independent decode, not a second
    round-trip to the DB.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise unauthorized("Authentication required")

    token = auth_header[7:]
    secret_key = require_secret_key()
    payload = verify_jwt_token(token, secret_key)
    if payload is None:
        raise unauthorized("Invalid or expired token")

    try:
        return int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise unauthorized("Token missing subject claim") from exc


def get_optional_current_user_id(request: Request) -> int | None:
    """Same as `get_current_user_id`, but returns `None` instead of raising.

    Matches Node's `optionalAuth` middleware (`GET /api/v1/auth/me`) --
    an absent/invalid token is a valid "not logged in" state, not an
    error.
    """
    try:
        return get_current_user_id(request)
    except Exception:  # noqa: BLE001 - any failure means "not logged in"
        return None


def get_current_scopes(request: Request) -> frozenset[str]:
    """Return the caller's verified OIDC scope set from the bearer JWT, or raise 401.

    The `scope` claim is parsed exactly as `flask_core.authz.require_scope` parses it. A token
    without the claim yields the EMPTY set -- "grants nothing", never an error swallowed into a
    default -- so anything authorised against it (e.g. a model-requested tool call) fails closed.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise unauthorized("Authentication required")

    secret_key = require_secret_key()
    payload = verify_jwt_token(auth_header[7:], secret_key)
    if payload is None:
        raise unauthorized("Invalid or expired token")
    scopes: frozenset[str] = parse_scope_claim(payload.get("scope"))
    return scopes
