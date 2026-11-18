"""Service-layer error taxonomy -- mirrors Node's `middleware/errorHandler.js` `errors.*` factories.

Node's controllers call `next(errors.badRequest(...))` and a single Express
error-handler middleware converts that into `{success: false, error:
{code, message}}`. Quart has no direct equivalent of `next(err)`, so
service functions raise :class:`ApiError` and every blueprint route
catches it at the top level and converts it via
`flask_core.api_utils.error_response` (same `{success, error: {code,
message}}` shape Node produces -- `AuthContext.jsx` reads
`err.response?.data?.error?.message`, unaffected by the port).

quart-schema's `@validate_response(Model)` only validates a 200 response
(`status_code` defaults to 200) -- an error tuple with a non-200 status
skips DTO validation entirely, so raising here and returning
`error_response(...)` from the route is safe under a `@validate_response`
decorator without needing an error-shaped DTO registered per status code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ApiError(Exception):
    """Raised by the service layer; caught by the blueprint route and converted to JSON."""

    message: str
    status_code: int = 400
    code: str = "BAD_REQUEST"
    details: dict[str, Any] | None = None


def bad_request(message: str = "Bad request") -> ApiError:
    """400 -- malformed/missing input."""
    return ApiError(message, 400, "BAD_REQUEST")


def unauthorized(message: str = "Unauthorized") -> ApiError:
    """401 -- missing/invalid credentials."""
    return ApiError(message, 401, "UNAUTHORIZED")


def forbidden(message: str = "Forbidden") -> ApiError:
    """403 -- authenticated but not permitted."""
    return ApiError(message, 403, "FORBIDDEN")


def not_found(message: str = "Not found") -> ApiError:
    """404 -- resource does not exist."""
    return ApiError(message, 404, "NOT_FOUND")


def conflict(message: str = "Conflict") -> ApiError:
    """409 -- e.g. duplicate email."""
    return ApiError(message, 409, "CONFLICT")


def unprocessable(message: str = "Unprocessable") -> ApiError:
    """422 -- request is well-formed but semantically rejected (policy/provider state)."""
    return ApiError(message, 422, "UNPROCESSABLE")


def payment_required(message: str = "This feature requires a higher plan") -> ApiError:
    """402 -- the two-gate `feature_enabled(...)` check denied the request.

    Flag OFF or below the Feature contract's `min_tier` -- see
    `flask_core.feature_flags`.
    """
    return ApiError(message, 402, "FEATURE_NOT_ENABLED")


#: Wire message for a Free tenant asked to hold Premium communities (402 TENANT_TIER_REQUIRED).
TENANT_TIER_REQUIRED_MESSAGE = "Premium communities require a Professional or Enterprise license"


def tenant_tier_required() -> ApiError:
    """402 -- the tenant is Free (no license / no Premium pool): it cannot have Premium communities.

    Same envelope for the tenant-admin and the operator-override paths; the UI keys on the
    `TENANT_TIER_REQUIRED` code and `details.requiredTenantTier` / `details.upgrade`.
    """
    return ApiError(
        TENANT_TIER_REQUIRED_MESSAGE,
        402,
        "TENANT_TIER_REQUIRED",
        {"requiredTenantTier": "professional", "upgrade": True},
    )
