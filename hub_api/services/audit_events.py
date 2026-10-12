"""Audit event vocabulary, input validation, and HTTP-request classification.

Everything here is pure (no I/O) so the PII guarantees are provable in unit tests:
an :class:`AuditEvent` *cannot be constructed* with free-text, e-mail-shaped, or
name-shaped content, which is what keeps the tamper-evident store PII-free by
construction rather than by reviewer vigilance (critical-rules.md PII Tokenization:
the audit actor is a UUID, never a username/e-mail, and no raw user input is logged).

Three things live here:

* the closed vocabularies (:class:`AuditCategory`, :class:`AuditOutcome`,
  :class:`ActorKind`) and the first-class :class:`AuditAction` catalog;
* :class:`AuditEvent` -- the validated input to ``AuditService.record``;
* :func:`classify_request` + :data:`SEMANTIC_ROUTES` -- the declarative map from a
  mutating/denied HTTP request to the security event it represents, so coverage is
  a table that a drift test checks against the live URL map rather than a hook that
  has to be remembered on every new route.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

#: Chain id of the platform-level chain (events with no tenant: webhooks, platform admin).
PLATFORM_CHAIN_ID: Final[str] = "platform"

_ACTION_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_.]{1,99}$")
_DETAIL_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
#: Identifier-shaped strings only: no spaces, no ``@`` (so no e-mail address), no quotes.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.:/<>=\-]{1,128}$")
_TARGET_TYPE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_.]{0,49}$")

MAX_DETAIL_KEYS: Final[int] = 16

#: Detail keys that name PII or credential material. Refused even if the value looks
#: like an identifier -- a ``username=bob123`` value passes the token regex, the key
#: is what gives it away.
FORBIDDEN_DETAIL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "email",
        "e_mail",
        "username",
        "user_name",
        "display_name",
        "name",
        "first_name",
        "last_name",
        "full_name",
        "handle",
        "phone",
        "address",
        "ip",
        "ip_address",
        "user_agent",
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "message",
        "body",
        "content",
        "text",
        "rest",
        "args",
        "query",
    }
)


class AuditCategory(StrEnum):
    """Coarse security domain of an event (the filter auditors reach for first)."""

    AUTHN = "authn"
    AUTHZ = "authz"
    TENANT = "tenant"
    ROLE = "role"
    USER = "user"
    PRIVACY = "privacy"
    LICENSE = "license"
    ADMIN = "admin"
    BUNDLE = "bundle"
    AUDIT = "audit"
    CREDENTIAL = "credential"


class AuditOutcome(StrEnum):
    """What happened to the audited operation."""

    SUCCESS = "success"
    DENIED = "denied"
    FAILURE = "failure"


class ActorKind(StrEnum):
    """Who acted. ``USER`` rows carry the ``hub_users.uuid``; the others carry no UUID."""

    USER = "user"
    SERVICE = "service"
    SYSTEM = "system"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"


class AuditAction(StrEnum):
    """First-class security actions (``<domain>.<verb>``); free-form snake_case also accepted.

    The bundle-lifecycle layer predates this catalog and emits its own snake_case
    action strings (``app_installed_globally`` ...); those stay valid -- see
    :data:`_ACTION_RE` -- so no existing caller had to change.
    """

    AUTHZ_DENIED = "authz.denied"
    SESSION_ISSUED = "authn.session_issued"
    SESSION_REVOKED = "authn.session_revoked"
    TENANT_CREATED = "tenant.created"
    TENANT_UPDATED = "tenant.updated"
    TENANT_DELETED = "tenant.deleted"
    TENANT_SETTINGS_CHANGED = "tenant.settings_changed"
    TENANT_MODULES_CHANGED = "tenant.modules_changed"
    TENANT_ADMIN_ADDED = "tenant.admin_added"
    TENANT_ADMIN_REMOVED = "tenant.admin_removed"
    ROLE_SUPER_ADMIN_CHANGED = "role.super_admin_changed"
    ROLE_VENDOR_CHANGED = "role.vendor_changed"
    ROLE_ANALYTICS_CONSUMER_CHANGED = "role.analytics_consumer_changed"
    ROLE_MEMBER_CHANGED = "role.member_changed"
    ROLE_PLATFORM_USER_CHANGED = "role.platform_user_changed"
    ROLE_DEFINITION_CHANGED = "role.definition_changed"
    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_DELETED = "user.deleted"
    USER_PASSWORD_RESET = "user.password_reset"  # noqa: S105 - an action name, not a credential  # nosec B105
    PRIVACY_DSAR_EXPORT = "privacy.dsar_export"
    PRIVACY_ERASURE_REQUESTED = "privacy.erasure_requested"
    LICENSE_SUBSCRIPTION_CHANGED = "license.subscription_changed"
    LICENSE_PROVIDER_EVENT = "license.provider_event"
    ADMIN_ACTION = "admin.action"
    ADMIN_PLATFORM_CONFIG_CHANGED = "admin.platform_config_changed"
    AUDIT_EXPORTED = "audit.exported"
    AUDIT_VERIFY_FAILED = "audit.verify_failed"
    # --- credential / secret lifecycle (inline-authz routes; the secret value is never logged) ---
    CREDENTIAL_BYOK_KEY_SET = "credential.byok_key_set"
    CREDENTIAL_BYOK_KEY_DELETED = "credential.byok_key_deleted"
    CREDENTIAL_STREAM_KEY_REGENERATED = "credential.stream_key_regenerated"
    CREDENTIAL_PAT_CREATED = "credential.pat_created"
    CREDENTIAL_PAT_REVOKED = "credential.pat_revoked"
    CREDENTIAL_PASSWORD_CHANGED = "credential.password_changed"  # noqa: S105 - action name  # nosec B105
    CREDENTIAL_PASSKEY_REGISTERED = "credential.passkey_registered"
    CREDENTIAL_PASSKEY_REMOVED = "credential.passkey_removed"
    CREDENTIAL_CONNECTION_CHANGED = "credential.connection_changed"
    CREDENTIAL_WEBHOOK_CHANGED = "credential.webhook_changed"
    # --- identity linking ---
    USER_IDENTITY_LINKED = "user.identity_linked"
    USER_IDENTITY_UNLINKED = "user.identity_unlinked"
    USER_IDENTITY_PRIMARY_CHANGED = "user.identity_primary_changed"
    # --- membership / delegated administration ---
    ROLE_EVENT_ADMIN_CHANGED = "role.event_admin_changed"
    ROLE_JOIN_REQUEST_DECIDED = "role.join_request_decided"
    # --- bundles ---
    BUNDLE_PERMISSION_CHANGED = "bundle.permission_changed"
    BUNDLE_ACTIVATION_CHANGED = "bundle.activation_changed"
    BUNDLE_VERSION_PUBLISHED = "bundle.version_published"
    # --- money / vendor ---
    LICENSE_TOKEN_BALANCE_CHANGED = "license.token_balance_changed"  # noqa: S105  # nosec B105
    LICENSE_PAYOUT_PROFILE_CHANGED = "license.payout_profile_changed"
    LICENSE_VENDOR_MODULE_CHANGED = "license.vendor_module_changed"
    # --- community administration ---
    ADMIN_COMMUNITY_PROFILE_CHANGED = "admin.community_profile_changed"
    ADMIN_AI_CONFIG_CHANGED = "admin.ai_config_changed"
    ADMIN_MUSIC_CONFIG_CHANGED = "admin.music_config_changed"
    ADMIN_STREAM_CONFIG_CHANGED = "admin.stream_config_changed"
    ADMIN_WORKFLOW_CHANGED = "admin.workflow_changed"
    ADMIN_CALENDAR_EVENT_CHANGED = "admin.calendar_event_changed"
    ADMIN_TICKETING_CHANGED = "admin.ticketing_changed"
    PRIVACY_CONSENT_CHANGED = "privacy.consent_changed"


class AuditValidationError(ValueError):
    """An event violated the PII-free / shape contract (a programming error, raised loudly)."""


#: ``<identifier>@<semver>`` -- how bundle lifecycle events name a versioned artefact
#: (``waddles.core.ping@1.0.0``). The part after ``@`` must look like a version (digits first,
#: no letters-only TLD), so an e-mail address still cannot pass as an identifier.
_VERSIONED_REF_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9_.\-]{1,100}@\d+(\.\d+){0,3}([\-+][A-Za-z0-9.\-+]{0,30})?$"
)


def _check_token(label: str, value: str) -> str:
    """Accept only identifier-shaped text (no spaces; ``@`` only in ``name@version``)."""
    if _TOKEN_RE.match(value) or _VERSIONED_REF_RE.match(value):
        return value
    raise AuditValidationError(
        f"{label} must be identifier-shaped ([A-Za-z0-9_.:/<>=-], 1-128 chars, or "
        "name@version); free text and e-mail-shaped values are not auditable"
    )


def is_auditable_token(value: str) -> bool:
    """True if ``value`` is identifier-shaped (the test :class:`AuditEvent` applies to ids)."""
    return bool(_TOKEN_RE.match(value) or _VERSIONED_REF_RE.match(value))


def is_valid_target_type(value: str) -> bool:
    """True if ``value`` is a snake_case target-type identifier."""
    return bool(_TARGET_TYPE_RE.match(value))


#: Value types a detail may take: identifier-shaped strings, ints, bools, None.
DetailScalar = str | int | bool | None
DetailValue = DetailScalar | list[DetailScalar]

MAX_DETAIL_LIST_ITEMS: Final[int] = 64


def _check_scalar(label: str, value: Any) -> DetailScalar:
    """Validate one detail scalar (``bool``/``int``/``None`` or an identifier-shaped ``str``)."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        return _check_token(label, value)
    raise AuditValidationError(
        f"{label} must be a bool, int, str or None, not {type(value).__name__}"
    )


def validate_details(details: Mapping[str, Any] | None) -> dict[str, DetailValue]:
    """Return a defensive, validated copy of ``details`` (flat, scalar-or-list, PII-free).

    Keys are snake_case and may not name PII/credential material; values are
    ``bool``/``int``/``None``, identifier-shaped strings, or short lists of those.
    Floats, nested mappings and free text are refused so the canonical JSON the hash
    covers has exactly one spelling on every platform and no raw user input can ride in.
    """
    if not details:
        return {}
    if len(details) > MAX_DETAIL_KEYS:
        raise AuditValidationError(f"details has more than {MAX_DETAIL_KEYS} keys")
    clean: dict[str, DetailValue] = {}
    for key, value in details.items():
        if not isinstance(key, str) or not _DETAIL_KEY_RE.match(key):
            raise AuditValidationError("details keys must be snake_case identifiers")
        if key in FORBIDDEN_DETAIL_KEYS:
            raise AuditValidationError(f"details key {key!r} is reserved for PII/credentials")
        label = f"details[{key!r}]"
        if isinstance(value, list | tuple | set | frozenset):
            items = sorted(value, key=str) if isinstance(value, set | frozenset) else list(value)
            if len(items) > MAX_DETAIL_LIST_ITEMS:
                raise AuditValidationError(f"{label} has more than {MAX_DETAIL_LIST_ITEMS} items")
            clean[key] = [_check_scalar(f"{label} item", item) for item in items]
        else:
            clean[key] = _check_scalar(label, value)
    return clean


def lenient_details(details: Mapping[str, Any] | None) -> dict[str, DetailValue]:
    """Best-effort view of legacy ``details`` for the chain: keep what is auditable, drop the rest.

    Used only by the legacy bundle-lifecycle bridge, whose existing callers predate the
    PII-free contract and pass free-text ``reason`` strings. Entries that fail
    :func:`validate_details` are *dropped from the tamper-evident record* (the legacy
    ``audit_log`` row still holds them) and counted in ``dropped_detail_keys`` so the
    omission is visible, never silent.
    """
    if not details:
        return {}
    kept: dict[str, DetailValue] = {}
    dropped = 0
    for key, value in list(details.items())[:MAX_DETAIL_KEYS]:
        try:
            kept.update(validate_details({key: value}))
        except AuditValidationError:
            dropped += 1
    dropped += max(0, len(details) - MAX_DETAIL_KEYS)
    if dropped:
        kept.pop("dropped_detail_keys", None)
        if len(kept) >= MAX_DETAIL_KEYS:
            kept.pop(next(reversed(kept)))
            dropped += 1
        kept["dropped_detail_keys"] = dropped
    return kept


@dataclass(slots=True, frozen=True)
class AuditEvent:
    """A validated security event, ready for ``AuditService.record``.

    ``actor_uuid`` is the ``hub_users.uuid`` of a human actor; ``actor_user_id`` is the
    legacy integer surrogate a caller already holds, resolved to the UUID by the service
    (so call sites never need a second lookup). Supply at most one of them.
    ``tenant_id``/``tenant_slug`` choose the chain; neither means the platform chain.
    """

    category: AuditCategory
    action: str
    outcome: AuditOutcome = AuditOutcome.SUCCESS
    actor_kind: ActorKind = ActorKind.USER
    actor_uuid: uuid.UUID | None = None
    actor_user_id: int | None = None
    tenant_id: int | None = None
    tenant_slug: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate every field so an invalid event never reaches the store."""
        if not _ACTION_RE.match(str(self.action)):
            raise AuditValidationError("action must be snake_case/dotted, 2-100 chars")
        if self.actor_uuid is not None and self.actor_user_id is not None:
            raise AuditValidationError("supply actor_uuid or actor_user_id, not both")
        if self.actor_uuid is not None and not isinstance(self.actor_uuid, uuid.UUID):
            raise AuditValidationError("actor_uuid must be a uuid.UUID")
        if self.actor_kind is ActorKind.USER:
            if self.actor_uuid is None and self.actor_user_id is None:
                raise AuditValidationError("a USER actor needs actor_uuid or actor_user_id")
        elif self.actor_uuid is not None or self.actor_user_id is not None:
            raise AuditValidationError("only USER actors carry a UUID / user id")
        if self.target_type is not None and not _TARGET_TYPE_RE.match(self.target_type):
            raise AuditValidationError("target_type must be a snake_case identifier")
        if self.target_id is not None:
            _check_token("target_id", self.target_id)
        if self.tenant_slug is not None:
            _check_token("tenant_slug", self.tenant_slug)
        object.__setattr__(self, "details", validate_details(self.details))


@dataclass(slots=True, frozen=True)
class RouteAudit:
    """The security meaning of one (method, URL rule): which action, in which category."""

    category: AuditCategory
    action: AuditAction


#: ``(METHOD, url_rule) -> meaning`` for the routes whose semantics matter beyond "an
#: admin mutated something". Every key is checked against the live URL map by
#: ``tests/test_audit_http.py::TestSemanticRoutesMatchUrlMap`` so a renamed route fails
#: a test instead of silently dropping out of the audit trail.
SEMANTIC_ROUTES: Final[Mapping[tuple[str, str], RouteAudit]] = {
    # --- tenant lifecycle and tenant-admin membership ---
    ("PUT", "/api/v1/tenant/<tenant_slug>"): RouteAudit(
        AuditCategory.TENANT, AuditAction.TENANT_UPDATED
    ),
    ("PUT", "/api/v1/tenant/<tenant_slug>/settings"): RouteAudit(
        AuditCategory.TENANT, AuditAction.TENANT_SETTINGS_CHANGED
    ),
    ("PUT", "/api/v1/tenant/<tenant_slug>/modules"): RouteAudit(
        AuditCategory.TENANT, AuditAction.TENANT_MODULES_CHANGED
    ),
    ("POST", "/api/v1/tenant/<tenant_slug>/admins"): RouteAudit(
        AuditCategory.ROLE, AuditAction.TENANT_ADMIN_ADDED
    ),
    ("DELETE", "/api/v1/tenant/<tenant_slug>/admins/<int:user_id>"): RouteAudit(
        AuditCategory.ROLE, AuditAction.TENANT_ADMIN_REMOVED
    ),
    ("POST", "/api/v1/superadmin/tenants"): RouteAudit(
        AuditCategory.TENANT, AuditAction.TENANT_CREATED
    ),
    ("PUT", "/api/v1/superadmin/tenants/<int:tenant_id>"): RouteAudit(
        AuditCategory.TENANT, AuditAction.TENANT_UPDATED
    ),
    ("DELETE", "/api/v1/superadmin/tenants/<int:tenant_id>"): RouteAudit(
        AuditCategory.TENANT, AuditAction.TENANT_DELETED
    ),
    # --- session revocation (admin) ---
    ("POST", "/api/v1/superadmin/users/<int:user_id>/revoke-sessions"): RouteAudit(
        AuditCategory.AUTHN, AuditAction.SESSION_REVOKED
    ),
    ("POST", "/api/v1/superadmin/users/<int:user_id>/sessions/<int:session_id>/revoke"): RouteAudit(
        AuditCategory.AUTHN, AuditAction.SESSION_REVOKED
    ),
    # --- role / privilege changes ---
    ("POST", "/api/v1/superadmin/users/<int:user_id>/super-admin-role"): RouteAudit(
        AuditCategory.ROLE, AuditAction.ROLE_SUPER_ADMIN_CHANGED
    ),
    ("POST", "/api/v1/superadmin/users/<int:user_id>/vendor-role"): RouteAudit(
        AuditCategory.ROLE, AuditAction.ROLE_VENDOR_CHANGED
    ),
    ("POST", "/api/v1/superadmin/users/<int:user_id>/analytics-consumer-role"): RouteAudit(
        AuditCategory.ROLE, AuditAction.ROLE_ANALYTICS_CONSUMER_CHANGED
    ),
    ("PUT", "/api/v1/platform/users/<int:user_id>/role"): RouteAudit(
        AuditCategory.ROLE, AuditAction.ROLE_PLATFORM_USER_CHANGED
    ),
    ("PUT", "/api/v1/admin/<int:community_id>/members/<int:user_id>/role"): RouteAudit(
        AuditCategory.ROLE, AuditAction.ROLE_MEMBER_CHANGED
    ),
    ("POST", "/api/v1/admin/<int:community_id>/interaction/roles"): RouteAudit(
        AuditCategory.ROLE, AuditAction.ROLE_DEFINITION_CHANGED
    ),
    ("PUT", "/api/v1/admin/<int:community_id>/interaction/roles/<int:role_id>"): RouteAudit(
        AuditCategory.ROLE, AuditAction.ROLE_DEFINITION_CHANGED
    ),
    ("DELETE", "/api/v1/admin/<int:community_id>/interaction/roles/<int:role_id>"): RouteAudit(
        AuditCategory.ROLE, AuditAction.ROLE_DEFINITION_CHANGED
    ),
    # --- user administration ---
    ("POST", "/api/v1/superadmin/users"): RouteAudit(AuditCategory.USER, AuditAction.USER_CREATED),
    ("PUT", "/api/v1/superadmin/users/<int:user_id>"): RouteAudit(
        AuditCategory.USER, AuditAction.USER_UPDATED
    ),
    ("DELETE", "/api/v1/superadmin/users/<int:user_id>"): RouteAudit(
        AuditCategory.USER, AuditAction.USER_DELETED
    ),
    ("DELETE", "/api/v1/platform/users/<int:user_id>"): RouteAudit(
        AuditCategory.USER, AuditAction.USER_DELETED
    ),
    ("POST", "/api/v1/superadmin/users/<int:user_id>/password-reset"): RouteAudit(
        AuditCategory.USER, AuditAction.USER_PASSWORD_RESET
    ),
    # --- statutory privacy rights (audited in every entitled tenant; never gated themselves) ---
    ("GET", "/api/v1/user/me/data"): RouteAudit(
        AuditCategory.PRIVACY, AuditAction.PRIVACY_DSAR_EXPORT
    ),
    ("DELETE", "/api/v1/user/me/data"): RouteAudit(
        AuditCategory.PRIVACY, AuditAction.PRIVACY_ERASURE_REQUESTED
    ),
    # --- licence / entitlement / subscription state ---
    ("POST", "/api/v1/marketplace/premium/subscribe"): RouteAudit(
        AuditCategory.LICENSE, AuditAction.LICENSE_SUBSCRIPTION_CHANGED
    ),
    ("POST", "/api/v1/marketplace/premium/cancel"): RouteAudit(
        AuditCategory.LICENSE, AuditAction.LICENSE_SUBSCRIPTION_CHANGED
    ),
    ("POST", "/api/v1/marketplace/payments/checkout"): RouteAudit(
        AuditCategory.LICENSE, AuditAction.LICENSE_SUBSCRIPTION_CHANGED
    ),
    ("POST", "/api/v1/marketplace/payments/refunds"): RouteAudit(
        AuditCategory.LICENSE, AuditAction.LICENSE_SUBSCRIPTION_CHANGED
    ),
    (
        "POST",
        "/api/v1/marketplace/payments/subscriptions/<provider>/<sub_id>/cancel",
    ): RouteAudit(AuditCategory.LICENSE, AuditAction.LICENSE_SUBSCRIPTION_CHANGED),
    ("POST", "/api/v1/marketplace/webhooks/stripe"): RouteAudit(
        AuditCategory.LICENSE, AuditAction.LICENSE_PROVIDER_EVENT
    ),
    ("POST", "/api/v1/marketplace/webhooks/paypal"): RouteAudit(
        AuditCategory.LICENSE, AuditAction.LICENSE_PROVIDER_EVENT
    ),
    # --- platform configuration ---
    ("PUT", "/api/v1/superadmin/platform-config/<platform>"): RouteAudit(
        AuditCategory.ADMIN, AuditAction.ADMIN_PLATFORM_CONFIG_CHANGED
    ),
    ("PUT", "/api/v1/superadmin/settings"): RouteAudit(
        AuditCategory.ADMIN, AuditAction.ADMIN_PLATFORM_CONFIG_CHANGED
    ),
}


def _ra(category: AuditCategory, action: AuditAction) -> RouteAudit:
    """Shorthand for a :class:`RouteAudit` (keeps the route table below readable)."""
    return RouteAudit(category, action)


_P = "/api/v1"
_ADM = "/api/v1/admin/<int:community_id>"
_ADMS = "/api/v1/admin/<community_id>"
_CAL = "/api/v1/admin/<community_id>/calendar/events/<event_id>"
_WF = "/api/v1/admin/<community_id>/workflows"
_APPS = "/api/v1/apps/community/<int:community_id>"

#: Routes that authorize *inline* (a ``tenant_middleware`` + in-handler community/role check, or
#: self-service ownership) instead of ``@require_scope``. ``require_scope`` publishes the
#: :class:`~flask_core.authz.AuthzDecision` the generic admin-action rule keys on, so these
#: mutations of sensitive state (credentials, keys, money, delegated authority, bundle grants)
#: were invisible to the audit trail (SOC2 CC7.2). Listing them here makes each one a first-class
#: event. As with :data:`SEMANTIC_ROUTES`, the event is derived from the URL *rule*, the method
#: and the status only -- never the path, query or body -- so a key/token/password in the request
#: or response can never reach the store. Unlike the semantic map an unauthenticated request is
#: not recorded (pre-identity noise), and an authenticated refusal is already an authz event.
#: ``tests/test_audit_http.py::TestInlineAuthzCoverage`` pins that every mutating route lacking
#: ``require_scope`` is either here or in :data:`INLINE_AUTHZ_UNAUDITED`, and that every key
#: names a live route.
_INLINE_AUTHZ_GROUPS: Final[tuple[tuple[RouteAudit, tuple[tuple[str, str], ...]], ...]] = (
    (
        _ra(AuditCategory.CREDENTIAL, AuditAction.CREDENTIAL_BYOK_KEY_SET),
        (("PUT", f"{_ADM}/ai/byok-keys"),),
    ),
    (
        _ra(AuditCategory.CREDENTIAL, AuditAction.CREDENTIAL_BYOK_KEY_DELETED),
        (("DELETE", f"{_ADM}/ai/byok-keys/<provider>"),),
    ),
    (
        _ra(AuditCategory.CREDENTIAL, AuditAction.CREDENTIAL_STREAM_KEY_REGENERATED),
        (("POST", f"{_ADM}/streams/key/regenerate"),),
    ),
    (
        _ra(AuditCategory.CREDENTIAL, AuditAction.CREDENTIAL_PAT_CREATED),
        (("POST", f"{_P}/user/tokens/pat"),),
    ),
    (
        _ra(AuditCategory.CREDENTIAL, AuditAction.CREDENTIAL_PAT_REVOKED),
        (("DELETE", f"{_P}/user/tokens/pat"),),
    ),
    (
        _ra(AuditCategory.CREDENTIAL, AuditAction.CREDENTIAL_PASSWORD_CHANGED),
        (("POST", f"{_P}/auth/password"),),
    ),
    (
        _ra(AuditCategory.CREDENTIAL, AuditAction.CREDENTIAL_PASSKEY_REGISTERED),
        (("POST", f"{_P}/user/passkey/register/finish"),),
    ),
    (
        _ra(AuditCategory.CREDENTIAL, AuditAction.CREDENTIAL_PASSKEY_REMOVED),
        (("DELETE", f"{_P}/user/passkey/credentials/<int:credential_id>"),),
    ),
    (
        _ra(AuditCategory.CREDENTIAL, AuditAction.CREDENTIAL_CONNECTION_CHANGED),
        (
            ("POST", f"{_P}/<community_id>/github-sync/connections"),
            ("DELETE", f"{_P}/<community_id>/github-sync/connections/<connection_id>"),
            ("POST", f"{_ADM}/music/providers/<provider>/oauth"),
            ("DELETE", f"{_ADM}/music/providers/<provider>"),
            ("POST", f"{_ADM}/streams/destinations"),
            ("DELETE", f"{_ADM}/streams/destinations/<int:destination_id>"),
        ),
    ),
    (
        _ra(AuditCategory.CREDENTIAL, AuditAction.CREDENTIAL_WEBHOOK_CHANGED),
        (
            ("POST", f"{_WF}/<workflow_id>/webhooks"),
            ("DELETE", f"{_WF}/<workflow_id>/webhooks/<webhook_id>"),
        ),
    ),
    (
        _ra(AuditCategory.USER, AuditAction.USER_IDENTITY_LINKED),
        (
            ("POST", f"{_P}/auth/link-oauth"),
            ("POST", f"{_P}/user/identities/link/<platform>"),
        ),
    ),
    (
        _ra(AuditCategory.USER, AuditAction.USER_IDENTITY_UNLINKED),
        (
            ("DELETE", f"{_P}/auth/oauth/<platform>"),
            ("DELETE", f"{_P}/user/identities/<platform>"),
        ),
    ),
    (
        _ra(AuditCategory.USER, AuditAction.USER_IDENTITY_PRIMARY_CHANGED),
        (("PUT", f"{_P}/user/identities/primary"),),
    ),
    (
        _ra(AuditCategory.ROLE, AuditAction.ROLE_EVENT_ADMIN_CHANGED),
        (
            ("POST", f"{_CAL}/admins"),
            ("PUT", f"{_CAL}/admins/<admin_id>"),
            ("DELETE", f"{_CAL}/admins/<admin_id>"),
        ),
    ),
    (
        _ra(AuditCategory.ROLE, AuditAction.ROLE_JOIN_REQUEST_DECIDED),
        (
            ("PUT", f"{_ADM}/join-requests/<int:request_id>/approve"),
            ("PUT", f"{_ADM}/join-requests/<int:request_id>/reject"),
        ),
    ),
    (
        _ra(AuditCategory.BUNDLE, AuditAction.BUNDLE_PERMISSION_CHANGED),
        (
            ("POST", f"{_APPS}/<app_id>/permissions/grant"),
            ("DELETE", f"{_APPS}/<app_id>/permissions/<permission_id>"),
        ),
    ),
    (
        _ra(AuditCategory.BUNDLE, AuditAction.BUNDLE_ACTIVATION_CHANGED),
        (
            ("POST", f"{_APPS}/activation"),
            ("DELETE", f"{_APPS}/activation/<app_id>"),
            ("POST", f"{_P}/marketplace/community/<int:community_id>/bundles"),
            ("DELETE", f"{_P}/marketplace/community/<int:community_id>/bundles/<app_id>"),
        ),
    ),
    (
        _ra(AuditCategory.BUNDLE, AuditAction.BUNDLE_VERSION_PUBLISHED),
        (("POST", f"{_P}/apps/<app_id>/versions"),),
    ),
    (
        _ra(AuditCategory.LICENSE, AuditAction.LICENSE_TOKEN_BALANCE_CHANGED),
        (
            ("POST", f"{_P}/marketplace/communities/<int:community_id>/tokens/credit"),
            ("POST", f"{_P}/marketplace/communities/<int:community_id>/tokens/debit"),
        ),
    ),
    (
        _ra(AuditCategory.LICENSE, AuditAction.LICENSE_PAYOUT_PROFILE_CHANGED),
        (
            ("POST", f"{_P}/marketplace/vendor/profile"),
            ("PUT", f"{_P}/marketplace/vendor/profile"),
        ),
    ),
    (
        _ra(AuditCategory.LICENSE, AuditAction.LICENSE_VENDOR_MODULE_CHANGED),
        (
            ("POST", f"{_P}/marketplace/vendor/request"),
            ("POST", f"{_P}/marketplace/vendor/modules"),
            ("PUT", f"{_P}/marketplace/vendor/modules/<int:module_id>"),
            ("POST", f"{_P}/marketplace/vendor/modules/<int:module_id>/submit"),
        ),
    ),
    (
        _ra(AuditCategory.ADMIN, AuditAction.ADMIN_COMMUNITY_PROFILE_CHANGED),
        (
            ("PUT", f"{_ADM}/profile"),
            ("POST", f"{_ADM}/logo"),
            ("DELETE", f"{_ADM}/logo"),
            ("POST", f"{_ADM}/banner"),
            ("DELETE", f"{_ADM}/banner"),
        ),
    ),
    (
        _ra(AuditCategory.ADMIN, AuditAction.ADMIN_AI_CONFIG_CHANGED),
        (("PUT", f"{_ADM}/ai/config"),),
    ),
    (
        _ra(AuditCategory.ADMIN, AuditAction.ADMIN_MUSIC_CONFIG_CHANGED),
        (
            ("PUT", f"{_ADM}/music/settings"),
            ("POST", f"{_ADM}/music/radio-stations"),
            ("DELETE", f"{_ADM}/music/radio-stations/<int:station_id>"),
            ("PUT", f"{_ADM}/music-station/policy"),
        ),
    ),
    (
        _ra(AuditCategory.ADMIN, AuditAction.ADMIN_STREAM_CONFIG_CHANGED),
        (
            ("POST", f"{_ADM}/streams"),
            ("PUT", f"{_ADM}/streams/destinations/<int:destination_id>/force-cut"),
        ),
    ),
    (
        _ra(AuditCategory.ADMIN, AuditAction.ADMIN_WORKFLOW_CHANGED),
        (
            ("POST", _WF),
            ("PUT", f"{_WF}/<workflow_id>"),
            ("DELETE", f"{_WF}/<workflow_id>"),
            ("POST", f"{_WF}/<workflow_id>/publish"),
            ("POST", f"{_WF}/<workflow_id>/execute"),
        ),
    ),
    (
        _ra(AuditCategory.ADMIN, AuditAction.ADMIN_CALENDAR_EVENT_CHANGED),
        (
            ("POST", f"{_ADMS}/calendar/events"),
            ("PUT", _CAL),
            ("DELETE", _CAL),
            ("POST", f"{_CAL}/approve"),
            ("POST", f"{_CAL}/reject"),
        ),
    ),
    (
        _ra(AuditCategory.ADMIN, AuditAction.ADMIN_TICKETING_CHANGED),
        (
            ("POST", f"{_CAL}/ticketing/enable"),
            ("POST", f"{_CAL}/ticketing/disable"),
            ("POST", f"{_CAL}/ticket-types"),
            ("PUT", f"{_CAL}/ticket-types/<type_id>"),
            ("DELETE", f"{_CAL}/ticket-types/<type_id>"),
            ("POST", f"{_CAL}/tickets"),
            ("POST", f"{_CAL}/tickets/<ticket_id>/cancel"),
            ("POST", f"{_CAL}/tickets/<ticket_id>/transfer"),
        ),
    ),
    (
        _ra(AuditCategory.PRIVACY, AuditAction.PRIVACY_CONSENT_CHANGED),
        (
            ("DELETE", f"{_P}/cookie"),
            ("PATCH", f"{_P}/cookie/preferences"),
        ),
    ),
)

INLINE_AUTHZ_ROUTES: Final[Mapping[tuple[str, str], RouteAudit]] = {
    key: meaning for meaning, keys in _INLINE_AUTHZ_GROUPS for key in keys
}

_R_SESSION: Final[str] = (
    "login/session/registration handshake: session issue/revoke is audited at the mint/revoke "
    "chokepoint (audit_http.record_session_*)"
)
_R_PUBLIC: Final[str] = (
    "unauthenticated public/inbound endpoint: no actor, pre-identity noise an attacker could use "
    "to bloat the chain"
)
_R_SELF: Final[str] = (
    "low-sensitivity self-service data (own calendar/availability/profile/RSVP); no credential, "
    "money or delegated authority changes"
)
_R_OPS: Final[str] = (
    "high-volume operational action (queue/playlist/check-in/dry-run); not a security-relevant "
    "state change"
)
_R_USAGE: Final[str] = (
    "usage metering/ticketing/support traffic; no credential, money or authority change"
)

#: Mutating routes that authorize inline but are *deliberately* not audited, each with the reason.
#: Anything not here and not in :data:`INLINE_AUTHZ_ROUTES` fails
#: ``tests/test_audit_http.py::TestInlineAuthzCoverage`` -- a new sensitive route cannot ship
#: silently unaudited; its author must classify it.
INLINE_AUTHZ_UNAUDITED: Final[Mapping[tuple[str, str], str]] = {
    ("POST", "/api/v1/auth/admin"): _R_SESSION,
    ("POST", "/api/v1/auth/login"): _R_SESSION,
    ("POST", "/api/v1/auth/logout"): _R_SESSION,
    ("POST", "/api/v1/auth/logout-all"): _R_SESSION,
    ("POST", "/api/v1/auth/passkey/login/finish"): _R_SESSION,
    ("POST", "/api/v1/auth/passkey/login/start"): _R_SESSION,
    ("POST", "/api/v1/auth/refresh"): _R_SESSION,
    ("POST", "/api/v1/auth/register"): _R_SESSION,
    ("POST", "/api/v1/auth/resend-verification"): _R_SESSION,
    ("POST", "/api/v1/auth/sso/<public_id>/acs"): _R_SESSION,
    ("POST", "/api/v1/auth/temp-password"): _R_SESSION,
    ("POST", "/api/v1/user/passkey/register/start"): _R_SESSION,
    ("POST", "/api/v1/cookie"): _R_PUBLIC,
    ("POST", "/api/v1/github-sync/webhook"): _R_PUBLIC,
    ("POST", "/api/v1/marketplace/public/vendor/submit"): _R_PUBLIC,
    ("POST", "/api/v1/calendar/book/<slug>"): _R_PUBLIC,
    ("DELETE", "/api/v1/admin/<community_id>/calendar/events/<event_id>/rsvp"): _R_SELF,
    ("POST", "/api/v1/admin/<community_id>/calendar/events/<event_id>/rsvp"): _R_SELF,
    ("DELETE", "/api/v1/calendar/booking-pages/<id>"): _R_SELF,
    ("DELETE", "/api/v1/calendar/booking-pages/<page_id>/members/<user_id>"): _R_SELF,
    ("DELETE", "/api/v1/calendar/bookings/<uuid>"): _R_SELF,
    ("DELETE", "/api/v1/calendar/oauth/calendars/<id>"): _R_SELF,
    ("DELETE", "/api/v1/user/profile/avatar"): _R_SELF,
    ("POST", "/api/v1/calendar/booking-pages"): _R_SELF,
    ("POST", "/api/v1/calendar/booking-pages/<page_id>/members"): _R_SELF,
    ("POST", "/api/v1/calendar/oauth/calendars/<id>/sync"): _R_SELF,
    ("POST", "/api/v1/user/profile/avatar"): _R_SELF,
    ("PUT", "/api/v1/calendar/availability/settings"): _R_SELF,
    ("PUT", "/api/v1/calendar/availability/weekly"): _R_SELF,
    ("PUT", "/api/v1/calendar/booking-pages/<id>"): _R_SELF,
    ("PUT", "/api/v1/user/profile"): _R_SELF,
    ("POST", "/api/v1/<int:community_id>/join-requests"): _R_SELF,
    ("DELETE", "/api/v1/admin/<int:community_id>/music-station/queue/<int:queue_id>"): _R_OPS,
    (
        "DELETE",
        "/api/v1/admin/<int:community_id>/music-station/queue/playlists/<playlist_id>",
    ): _R_OPS,
    ("POST", "/api/v1/<community_id>/github-sync/ticket/<ticket_id>/sync"): _R_OPS,
    ("POST", "/api/v1/admin/<community_id>/calendar/events/<event_id>/check-in"): _R_OPS,
    (
        "POST",
        "/api/v1/admin/<community_id>/calendar/events/<event_id>/tickets/<ticket_id>/undo-check-in",
    ): _R_OPS,
    (
        "POST",
        "/api/v1/admin/<community_id>/workflows/<workflow_id>/executions/<execution_id>/cancel",
    ): _R_OPS,
    ("POST", "/api/v1/admin/<community_id>/workflows/<workflow_id>/test"): _R_OPS,
    ("POST", "/api/v1/admin/<community_id>/workflows/validate"): _R_OPS,
    ("POST", "/api/v1/admin/<int:community_id>/music-station/queue/advance"): _R_OPS,
    ("POST", "/api/v1/admin/<int:community_id>/music-station/queue/playlists"): _R_OPS,
    ("POST", "/api/v1/admin/<int:community_id>/music-station/queue/requests"): _R_OPS,
    ("PUT", "/api/v1/admin/<int:community_id>/music-station/queue/reorder"): _R_OPS,
    ("POST", "/api/v1/admin/calendar/verify-ticket"): _R_OPS,
    (
        "POST",
        "/api/v1/admin/<int:community_id>/support/my-tickets/<int:ticket_id>/comments",
    ): _R_USAGE,
    ("POST", "/api/v1/admin/<int:community_id>/support/submit"): _R_USAGE,
    ("POST", "/api/v1/community/<int:community_id>/ai/completions"): _R_USAGE,
}

#: Methods that change state; a scope-protected request using one is an auditable admin action.
MUTATING_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Rule prefixes for routes that are *never* audited by the generic classifier because they
#: are credential-less service-to-service plumbing or pre-session auth handshakes (their
#: security events are emitted at the token-mint chokepoint instead).
UNAUDITED_RULE_PREFIXES: Final[tuple[str, ...]] = (
    "/api/v1/internal/",
    "/internal/",
    "/mcp/",
)


@dataclass(slots=True, frozen=True)
class RequestFacts:
    """The request/response facts the classifier needs -- nothing user-supplied.

    ``rule`` is the URL *rule* (``/api/v1/tenant/<tenant_slug>/admins``), never the
    concrete path, so a username or id embedded in the URL cannot leak into the log.
    ``scope_checked`` is True when a ``require_scope`` decision was published for the
    request; ``denied`` is that decision's verdict.
    """

    method: str
    rule: str | None
    status_code: int
    scope_checked: bool
    denied: bool
    authenticated: bool


@dataclass(slots=True, frozen=True)
class Classification:
    """The audit event a request maps to, before actor/tenant are attached."""

    category: AuditCategory
    action: str
    outcome: AuditOutcome


def classify_request(facts: RequestFacts) -> Classification | None:
    """Decide whether (and as what) a finished request is auditable; ``None`` = not audited.

    Order matters: an authenticated principal being refused is always an authz event;
    then the explicit semantic map and the inline-authz map (authenticated callers only); then the
    generic rule that every state-changing
    request which passed a scope check is an admin action. Reads are never audited
    (their denial is), and unauthenticated 401s are not (they are pre-identity noise
    an attacker could use to bloat the chain).
    """
    if facts.rule is None:
        return None
    if facts.rule.startswith(UNAUDITED_RULE_PREFIXES):
        return None
    method = facts.method.upper()
    if facts.authenticated and (facts.denied or facts.status_code == 403):
        # A scope-check refusal, or any other authenticated 403 (tenant mismatch, community
        # membership, inline authz after the scope check passed) -- all are authz decisions.
        return Classification(AuditCategory.AUTHZ, AuditAction.AUTHZ_DENIED, AuditOutcome.DENIED)
    semantic = SEMANTIC_ROUTES.get((method, facts.rule))
    if semantic is not None:
        outcome = AuditOutcome.SUCCESS if 200 <= facts.status_code < 400 else AuditOutcome.FAILURE
        return Classification(semantic.category, semantic.action, outcome)
    inline = INLINE_AUTHZ_ROUTES.get((method, facts.rule))
    if inline is not None:
        if not facts.authenticated:
            # No identity to attribute: an unauthenticated 401 is pre-identity noise.
            return None
        outcome = AuditOutcome.SUCCESS if 200 <= facts.status_code < 400 else AuditOutcome.FAILURE
        return Classification(inline.category, inline.action, outcome)
    if method in MUTATING_METHODS and facts.scope_checked and not facts.denied:
        outcome = AuditOutcome.SUCCESS if 200 <= facts.status_code < 400 else AuditOutcome.FAILURE
        return Classification(AuditCategory.ADMIN, AuditAction.ADMIN_ACTION, outcome)
    return None
