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
    USER_PASSWORD_RESET = "user.password_reset"  # noqa: S105 - an action name, not a credential
    PRIVACY_DSAR_EXPORT = "privacy.dsar_export"
    PRIVACY_ERASURE_REQUESTED = "privacy.erasure_requested"
    LICENSE_SUBSCRIPTION_CHANGED = "license.subscription_changed"
    LICENSE_PROVIDER_EVENT = "license.provider_event"
    ADMIN_ACTION = "admin.action"
    ADMIN_PLATFORM_CONFIG_CHANGED = "admin.platform_config_changed"
    AUDIT_EXPORTED = "audit.exported"
    AUDIT_VERIFY_FAILED = "audit.verify_failed"


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
    then the explicit semantic map; then the generic rule that every state-changing
    request which passed a scope check is an admin action. Reads are never audited
    (their denial is), and unauthenticated 401s are not (they are pre-identity noise
    an attacker could use to bloat the chain).
    """
    if facts.rule is None:
        return None
    if facts.rule.startswith(UNAUDITED_RULE_PREFIXES):
        return None
    method = facts.method.upper()
    if facts.denied and facts.authenticated:
        return Classification(AuditCategory.AUTHZ, AuditAction.AUTHZ_DENIED, AuditOutcome.DENIED)
    if facts.status_code == 403 and facts.authenticated and not facts.scope_checked:
        return Classification(AuditCategory.AUTHZ, AuditAction.AUTHZ_DENIED, AuditOutcome.DENIED)
    semantic = SEMANTIC_ROUTES.get((method, facts.rule))
    if semantic is not None:
        outcome = AuditOutcome.SUCCESS if 200 <= facts.status_code < 400 else AuditOutcome.FAILURE
        return Classification(semantic.category, semantic.action, outcome)
    if method in MUTATING_METHODS and facts.scope_checked and not facts.denied:
        outcome = AuditOutcome.SUCCESS if 200 <= facts.status_code < 400 else AuditOutcome.FAILURE
        return Classification(AuditCategory.ADMIN, AuditAction.ADMIN_ACTION, outcome)
    return None
