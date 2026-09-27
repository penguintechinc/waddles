"""Vendor-vs-admin authorization for `POST /apps/{app_id}/versions` (spec Sec9.2 follow-on).

security.md Authentication & Authorization: scope checks run before any
request-body work, and permission decisions use OIDC scopes only, never
role names. Two independent paths satisfy this endpoint:

  - `platform:admin` scope -- unrestricted `app_id`, exactly this
    endpoint's original (pre-vendor-onboarding) behavior.
  - `vendor:onboard` scope (minted only when `hub_users.is_vendor`,
    see `services/auth_service.py::create_session_token`) -- restricted
    to the caller's own `waddles.integrations.vendor-{caller_id}.*`
    namespace. `waddles.core.*` (the reserved first-party namespace) and
    every other vendor's `waddles.integrations.vendor-{other_id}.*` are
    refused with 403, never a silent narrowing or a 404 (no ambiguity
    about why).

**Namespace shape, and why it is NOT the bare `vendor.{id}.*` a literal
reading of "vendor namespacing" might suggest.** Every `app_id` is
hard-validated by `services/bundle_manifest_v2.py`'s `_APP_ID_RE` against
`waddles.<module>.<feature-name>.<app>`, where `<module>` MUST be one of
`flask_core.app_manifest.KNOWN_MODULES` -- a small, deliberately closed
taxonomy with its own dedicated exact-set regression test
(`libs/core_platform_module/tests/test_known_modules_expansion.py`)
guarding against silent widening. Rather than adding a new `"vendor"`
module value to that shared, platform-wide taxonomy (used far beyond
bundle onboarding -- Helm deployment grouping, every product's
`features.py`), this module reuses the EXISTING `"integrations"` module
(semantically apt: a vendor bundle is, by definition, a third-party
integration -- see the manifest's own `provider: thirdparty` field) and
places the vendor id in the FEATURE-NAME segment instead:
`waddles.integrations.vendor-{vendor_id}.{app-name}`. This needs zero
changes to the manifest grammar or its guarded taxonomy, at the cost of
diverging from the task's shorthand `vendor.{id}.*` phrasing -- flagged
here explicitly since it is a security-relevant naming decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from flask_core.auth import verify_jwt_token
from flask_core.authz import has_required_scopes
from flask_core.secrets import require_secret_key
from quart import Request

from services.errors import forbidden, unauthorized

logger = logging.getLogger(__name__)

PLATFORM_ADMIN_SCOPE = "platform:admin"
VENDOR_ONBOARD_SCOPE = "vendor:onboard"

#: The reserved first-party namespace -- never onboardable by a vendor,
#: regardless of scope.
CORE_NAMESPACE_PREFIX = "waddles.core."


def vendor_namespace_prefix(vendor_id: int) -> str:
    """The one `app_id` namespace `vendor_id` may onboard into."""
    return f"waddles.integrations.vendor-{vendor_id}."


@dataclass(slots=True, frozen=True)
class OnboardingAuthorization:
    """Which path admitted the caller -- `is_admin=False` means the vendor path.

    `authorize_onboarding_scope()` returns this WITHOUT yet checking the
    vendor namespace (that needs `caller_id`, resolved later -- see
    `enforce_vendor_namespace()`); `is_admin=False` alone only means "the
    caller has `vendor:onboard`", not "the app_id has been cleared".
    """

    is_admin: bool


def _granted_scopes(request: Request) -> frozenset[str]:
    """Independently re-decode the bearer JWT's `scope` claim.

    Matches `flask_core.authz.require_scope`'s own precedent (see its
    docstring): a self-contained re-decode rather than reaching into
    `tenant_middleware`'s request-local state, so this check is testable
    in isolation and does not depend on decorator ordering elsewhere.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise unauthorized("Authentication required")
    payload = verify_jwt_token(auth_header[7:], require_secret_key())
    if payload is None:
        raise unauthorized("Invalid or expired token")
    raw_scope = payload.get("scope")
    if not raw_scope or not isinstance(raw_scope, str):
        return frozenset()
    return frozenset(raw_scope.split())


def authorize_onboarding_scope(request: Request, *, app_id: str) -> OnboardingAuthorization:
    """403s unless the caller carries `platform:admin` or `vendor:onboard`.

    Deliberately does NOT resolve `caller_id`/check the vendor namespace
    yet -- callers must invoke this FIRST, before reading any multipart
    file part (security.md: never buffer an unauthorized caller's
    payload) and before resolving a subject claim that may itself be
    malformed for callers this check would reject anyway. The vendor
    namespace is enforced separately by `enforce_vendor_namespace()`,
    once a `caller_id` is available.
    """
    granted = _granted_scopes(request)
    if has_required_scopes(granted, (PLATFORM_ADMIN_SCOPE,)):
        return OnboardingAuthorization(is_admin=True)

    if not has_required_scopes(granted, (VENDOR_ONBOARD_SCOPE,)):
        logger.warning(
            "bundle onboarding: insufficient scope",
            extra={
                "event_type": "AUTHZ",
                "action": "authorize_onboarding_scope",
                "result": "FORBIDDEN",
                "app_id": app_id,
            },
        )
        raise forbidden(f"{PLATFORM_ADMIN_SCOPE!r} or {VENDOR_ONBOARD_SCOPE!r} scope required")

    return OnboardingAuthorization(is_admin=False)


def enforce_vendor_namespace(*, app_id: str, caller_id: int) -> None:
    """403s unless `app_id` falls under the calling vendor's own namespace prefix.

    Covers both required rejections in one check: `waddles.core.*` and
    every other vendor's `waddles.integrations.vendor-{other_id}.*` both
    simply fail to match this caller's own required prefix.
    """
    required_prefix = vendor_namespace_prefix(caller_id)
    if not app_id.startswith(required_prefix):
        logger.warning(
            "bundle onboarding: app_id outside caller's vendor namespace",
            extra={
                "event_type": "AUTHZ",
                "action": "enforce_vendor_namespace",
                "result": "FORBIDDEN",
                "app_id": app_id,
                "required_prefix": required_prefix,
            },
        )
        raise forbidden(
            f"vendors may only onboard app_id under {required_prefix!r} "
            "(waddles.core.* and other vendors' namespaces are reserved)"
        )
