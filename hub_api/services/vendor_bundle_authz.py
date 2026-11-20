"""Vendor-vs-admin authorization for `POST /apps/{app_id}/versions` (spec Sec9.2 follow-on).

security.md Authentication & Authorization: scope checks run before any
request-body work, and permission decisions use OIDC scopes only, never
role names. Two independent paths satisfy this endpoint:

  - `platform:admin` scope -- unrestricted `app_id`, exactly this
    endpoint's original (pre-vendor-onboarding) behavior.
  - `vendor:onboard` scope (minted only when `hub_users.is_vendor`,
    see `services/auth_service.py::create_session_token`) -- restricted
    to a well-formed third-party `app_id` the caller owns. Reserved orgs
    (`RESERVED_ORGS`: first-party, legacy `waddles`, platform-hosted `psychoboy`) and
    any app_id first registered by a different publisher are refused with 403 --
    never a silent narrowing or a 404 (no ambiguity about why).

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
from dataclasses import dataclass
from typing import Any

from flask_core.auth import verify_jwt_token
from flask_core.authz import has_required_scopes
from quart import Request

from services.app_id_ownership import owner_of
from services.bundle_app_id import CORE_NAMESPACE_PREFIX as _CORE_NAMESPACE_PREFIX
from services.bundle_app_id import is_valid_app_id
from services.errors import forbidden, unauthorized

logger = logging.getLogger(__name__)

PLATFORM_ADMIN_SCOPE = "platform:admin"
VENDOR_ONBOARD_SCOPE = "vendor:onboard"

#: The reserved first-party namespace -- never onboardable by a vendor,
#: regardless of scope. Re-exported from the one grammar module.
CORE_NAMESPACE_PREFIX = _CORE_NAMESPACE_PREFIX

#: Org segments no vendor-path app_id may use, for any repo: `penguintechinc` is the
#: first-party org, `waddles` blocks the legacy `waddles.<x>.<y>` shape (which still matches
#: the 3-segment pattern), and `psychoboy` is the first-party-hosted SuperPenguinTV port
#: namespace, seeded by us (`seed_core_bundles`, `requested_by` NULL) and platform-admin only.
RESERVED_ORGS: frozenset[str] = frozenset({"penguintechinc", "waddles", "psychoboy"})


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


async def enforce_vendor_namespace(install_dal: Any, *, app_id: str, caller_id: int) -> None:
    """403s unless `app_id` is a well-formed, non-reserved id this vendor may publish to.

    Rejects: anything that is not `<github-org>.<repo>.<bundle-name>`; any id whose org
    segment is in `RESERVED_ORGS`; and (ownership lock) any `app_id` that already has an
    upload row created by a different caller -- including `requested_by IS NULL`, the
    system actor that seeded first-party and platform-hosted bundles. An app_id therefore
    belongs to the vendor who first registered it. Verifying that the vendor really owns
    the GitHub org is a separate follow-up (OAuth / vendor_orgs mapping).
    """
    reason: str | None = None
    if not is_valid_app_id(app_id):
        reason = "app_id must be '<github-org>.<repo>.<bundle-name>' ([a-z0-9-] segments)"
    elif app_id.split(".", 1)[0] in RESERVED_ORGS:
        reason = "this org namespace is reserved (first-party / platform-admin only)"
    else:
        has_owner, owner_id = await owner_of(install_dal, app_id)
        if has_owner:
            if owner_id != caller_id:
                reason = "this app_id is already registered by another publisher"
        else:
            # No claim row yet (legacy rows predate `app_id_owners`): fall back to the upload
            # history so a pre-existing foreign upload still locks the id. The authoritative,
            # race-free claim is `claim_app_id`, run in the upload transaction.
            rows = await install_dal(install_dal.app_version_uploads.app_id == app_id).select(
                install_dal.app_version_uploads.requested_by
            )
            if any(row.requested_by != caller_id for row in rows):
                reason = "this app_id is already registered by another publisher"
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
