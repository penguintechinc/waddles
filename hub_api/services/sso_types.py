"""Shared types for the enterprise SSO subsystem (SAML 2.0, OIDC, Google OAuth2).

Everything the SSO modules pass between each other is a slotted dataclass --
never a bare dict -- and every failure is an `SsoError` subclass carrying a
fixed, application-authored `code`/`message` pair. That pair is the ONLY
exception text that ever reaches a log line (see `sso_telemetry.log_sso_error`):
parser/crypto/HTTP library exceptions routinely embed the offending token,
XML fragment or URL, any of which can carry PII, so they are logged by type
and frame-only traceback, never by message.

Tier mapping (critical-rules.md Feature Flags & License Tiers; the catalog
lives in `flask_core.tier_catalog.FEATURE_MIN_TIERS`):

* `saml` and `oidc` connections -> `waddles.auth.sso_saml` (Enterprise)
* `google` connections          -> `waddles.auth.sso_google` (Professional)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

PROTOCOL_SAML: Final = "saml"
PROTOCOL_OIDC: Final = "oidc"
PROTOCOL_GOOGLE: Final = "google"
VALID_PROTOCOLS: Final[tuple[str, ...]] = (PROTOCOL_SAML, PROTOCOL_OIDC, PROTOCOL_GOOGLE)

#: Feature flags (also the tier-catalog keys) gating each protocol family.
FLAG_SSO_ENTERPRISE: Final = "waddles.auth.sso_saml"
FLAG_SSO_GOOGLE: Final = "waddles.auth.sso_google"

#: OIDC scope every admin-managed tenant SSO admin route requires
#: (`libs/core_platform_module/features.py` -> `requires_scopes`).
SCOPE_SSO_ADMIN: Final = "auth.sso:admin"

#: Fixed reason codes the browser is allowed to see on `/login?error=...`.
#: Anything finer-grained than this stays in server logs.
REASON_IDP_UNAVAILABLE: Final = "sso_idp_unavailable"
REASON_INVALID_RESPONSE: Final = "sso_invalid_response"
REASON_DENIED: Final = "sso_denied"
REASON_NOT_ENTITLED: Final = "sso_not_entitled"
REASON_ACCOUNT_CONFLICT: Final = "sso_account_conflict"
REASON_DOMAIN_NOT_ALLOWED: Final = "sso_domain_not_allowed"
REASON_EMAIL_REQUIRED: Final = "sso_email_required"
REASON_EMAIL_UNVERIFIED: Final = "sso_email_unverified"
REASON_SESSION_MISMATCH: Final = "sso_session_mismatch"
REASON_UNAVAILABLE: Final = "sso_unavailable"
REASON_ACCOUNT_INACTIVE: Final = "sso_account_inactive"


def flag_for_protocol(protocol: str) -> str:
    """Return the entitlement flag gating `protocol`; raises on an unknown protocol."""
    if protocol in (PROTOCOL_SAML, PROTOCOL_OIDC):
        return FLAG_SSO_ENTERPRISE
    if protocol == PROTOCOL_GOOGLE:
        return FLAG_SSO_GOOGLE
    raise SsoConfigError("unknown_protocol", f"Unknown SSO protocol {protocol!r}")


class SsoError(Exception):
    """Base SSO failure: a fixed `code` (log/metric label) and application-authored `message`.

    `reason` is the coarse, browser-safe code used in login-page redirects;
    it never varies with attacker-controlled input.
    """

    def __init__(self, code: str, message: str, *, reason: str = REASON_DENIED) -> None:
        """Store the fixed code/message/reason triple."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason


class SsoConfigError(SsoError):
    """The connection or platform configuration is unusable (admin-facing, 4xx on CRUD)."""

    def __init__(self, code: str, message: str, *, reason: str = REASON_UNAVAILABLE) -> None:
        """Config failures surface to browsers only as the generic `unavailable` reason."""
        super().__init__(code, message, reason=reason)


class SsoProtocolError(SsoError):
    """The IdP's response failed validation (signature, audience, nonce, expiry, ...)."""

    def __init__(self, code: str, message: str, *, reason: str = REASON_INVALID_RESPONSE) -> None:
        """Protocol failures surface as `sso_invalid_response`."""
        super().__init__(code, message, reason=reason)


class SsoIdpUnavailableError(SsoError):
    """The IdP could not be reached or returned an unusable HTTP response."""

    def __init__(self, code: str, message: str) -> None:
        """Network-level failures surface as `sso_idp_unavailable`."""
        super().__init__(code, message, reason=REASON_IDP_UNAVAILABLE)


class SsoPolicyError(SsoError):
    """The IdP vouched for the user but tenant policy refuses the login."""


@dataclass(slots=True, frozen=True)
class OidcSettings:
    """Non-secret OIDC/Google settings parsed out of `sso_connections.config`."""

    issuer: str
    client_id: str
    discovery_url: str
    scopes: tuple[str, ...] = ("openid", "email", "profile")
    allowed_domains: tuple[str, ...] = ()
    #: Google Workspace hosted-domain (`hd`) the login must come from.
    hosted_domain: str | None = None
    #: Google only: use the operator-provisioned platform client instead of a
    #: tenant-supplied `client_id`/`client_secret` pair.
    use_platform_client: bool = False


@dataclass(slots=True, frozen=True)
class SamlSettings:
    """Non-secret SAML SP-side settings for one IdP, parsed out of `config`."""

    idp_entity_id: str
    idp_sso_url: str
    idp_certs_pem: tuple[str, ...]
    name_id_format: str = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
    allowed_domains: tuple[str, ...] = ()
    email_attribute: str | None = None
    name_attribute: str | None = None
    force_authn: bool = False


@dataclass(slots=True, frozen=True)
class SsoConnection:
    """One configured IdP. Exactly one of `oidc` / `saml` is populated, per `protocol`."""

    id: int
    public_id: str
    tenant_id: int
    protocol: str
    display_name: str
    enabled: bool
    oidc: OidcSettings | None
    saml: SamlSettings | None
    secret_ciphertext: str | None = field(default=None, repr=False)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def allowed_domains(self) -> tuple[str, ...]:
        """Email domains this connection may log in (empty tuple = unrestricted)."""
        if self.oidc is not None:
            return self.oidc.allowed_domains
        if self.saml is not None:
            return self.saml.allowed_domains
        return ()


@dataclass(slots=True, frozen=True)
class ExternalIdentity:
    """The IdP-vouched identity a validated login assertion carries.

    Every field here is PII (or PII-adjacent): `repr=False` throughout so an
    accidental `%r`/f-string of the dataclass can never write it to a log.
    """

    subject: str = field(repr=False)
    email: str | None = field(default=None, repr=False)
    email_verified: bool = False
    display_name: str | None = field(default=None, repr=False)
    hosted_domain: str | None = field(default=None, repr=False)


@dataclass(slots=True, frozen=True)
class LoginOutcome:
    """Result of a completed SSO login: the session JWT plus non-PII bookkeeping."""

    token: str = field(repr=False)
    user_uuid: str
    connection_public_id: str
    protocol: str
    created_user: bool
