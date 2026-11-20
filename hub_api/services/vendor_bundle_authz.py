"""Vendor-vs-admin authorization for `POST /apps/{app_id}/versions` (spec Sec9.2 follow-on).

security.md Authentication & Authorization: scope checks run before any
request-body work, and permission decisions use OIDC scopes only, never
role names. Two independent paths satisfy this endpoint:

  - `platform:admin` scope -- unrestricted `app_id`, exactly this
    endpoint's original (pre-vendor-onboarding) behavior.
  - `vendor:onboard` scope (minted only when `hub_users.is_vendor`,
    see `services/auth_service.py::create_session_token`) -- restricted
    to a well-formed third-party `app_id`. `penguintechinc.waddles.*`
    (the reserved first-party namespace) and every other `penguintechinc`
    org id are refused with 403, as are the admin-only third-party ports
    (`psychoboy.penguintwitchbot.*`) -- never a silent narrowing or a 404 (no
    ambiguity about why).

**Namespace shape.** Every `app_id` is `<github-org>.<repo>.<bundle-name>`
(lowercase, three segments; `services/bundle_manifest_v2.py`'s
`_APP_ID_RE`). First-party bundles are `penguintechinc.waddles.<name>`
and are the ONLY ids that get the core trust treatment (`app_core`
schema, no human approval). Because that trust now rests on an org
string, the `penguintechinc` org segment is reserved: a vendor-path
publish may never use it, whatever the repo or bundle name.
`psychoboy.penguintwitchbot.*` (the SuperPenguinTV ports) is third-party
(`app_community`, human approval) and may only be onboarded by a
platform admin, exactly as the old `superpenguin` ids were.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from flask_core.auth import verify_jwt_token
from flask_core.authz import has_required_scopes
from quart import Request

from services.errors import forbidden, unauthorized

logger = logging.getLogger(__name__)

PLATFORM_ADMIN_SCOPE = "platform:admin"
VENDOR_ONBOARD_SCOPE = "vendor:onboard"

#: The reserved first-party namespace -- never onboardable by a vendor,
#: regardless of scope.
CORE_NAMESPACE_PREFIX = "penguintechinc.waddles."

#: The reserved org segment: no vendor-path app_id may use it for any repo.
RESERVED_ORG = "penguintechinc"

#: Third-party namespaces that only a platform admin may onboard into.
ADMIN_ONLY_PREFIXES: tuple[str, ...] = ("psychoboy.penguintwitchbot.",)

_SEGMENT = r"[a-z0-9][a-z0-9_-]*"
#: `<github-org>.<repo>.<bundle-name>` -- mirrors `bundle_manifest_v2._APP_ID_RE`.
_VENDOR_APP_ID_RE = re.compile(rf"^{_SEGMENT}\.{_SEGMENT}\.{_SEGMENT}$")
_APP_ID_MAX_LEN = 127


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
    payload = verify_jwt_token(auth_header[7:])
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


def enforce_vendor_namespace(*, app_id: str) -> None:
    """403s unless `app_id` is a well-formed, non-reserved third-party id.

    Rejects: anything that is not `<github-org>.<repo>.<bundle-name>`; any id whose
    org segment is the reserved `penguintechinc` (this covers
    `penguintechinc.waddles.*` and `penguintechinc.<other-repo>.*` alike); and
    the admin-only third-party ports (`psychoboy.penguintwitchbot.*`).
    """
    reason: str | None = None
    if len(app_id) > _APP_ID_MAX_LEN or not _VENDOR_APP_ID_RE.fullmatch(app_id):
        reason = "app_id must be '<github-org>.<repo>.<bundle-name>' (lowercase)"
    elif app_id.split(".", 1)[0] == RESERVED_ORG:
        reason = f"the {RESERVED_ORG!r} org namespace is reserved for first-party bundles"
    elif app_id.startswith(ADMIN_ONLY_PREFIXES):
        reason = "this namespace may only be onboarded by a platform admin"
    if reason is not None:
        logger.warning(
            "bundle onboarding: app_id refused on the vendor path",
            extra={
                "event_type": "AUTHZ",
                "action": "enforce_vendor_namespace",
                "result": "FORBIDDEN",
                "app_id": app_id,
            },
        )
        raise forbidden(f"vendors may not onboard this app_id: {reason}")
