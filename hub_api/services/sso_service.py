"""Enterprise SSO orchestration: connection CRUD, login start, login completion.

Ties the protocol layers (`sso_oidc`, `sso_saml`) to hub-api's identity model:

* **Entitlement.** Every protocol maps to a tier-catalog flag (`sso_types.
  flag_for_protocol`): SAML/OIDC -> `waddles.auth.sso_saml` (Enterprise), Google
  -> `waddles.auth.sso_google` (Professional). Evaluated with the real
  two-gate `flask_core.feature_flags.feature_enabled` for the connection's
  tenant at connection create/update/enable, when listing login options, at
  login start AND again at login completion -- a downgrade stops logins at once.
  Disabling and deleting a connection is always allowed.
* **Tenancy.** A connection belongs to exactly one tenant and the session minted
  on success carries that tenant. The public login/callback routes resolve the
  tenant from the connection row; nothing tenant-shaped is ever read from the
  request. Admin routes are tenant-scoped by the verified JWT (IDOR-safe: another
  tenant's connection is a 404, never a 403).
* **Accounts.** JIT provisioning is keyed on `(connection, subject)` in
  `sso_identities`. An existing `hub_users` row is NEVER adopted by email --
  `hub_users` is global across tenants, so an email match from one tenant's IdP
  would otherwise be a cross-tenant account takeover. A collision is a refusal.
  `allowed_domains` is mandatory on every connection and enforced at every login.
* **PII.** Logs, metrics and spans carry the connection's opaque `public_id`, the
  protocol and fixed reason codes; the user is identified by `hub_users.uuid`
  only. Emails, subjects and display names never leave this module except into
  the database.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlparse

import httpx
from flask_core.feature_flags import feature_enabled

from config import HubAPIConfig
from services import sso_crypto, sso_saml, sso_state
from services.auth_service import SessionUser, add_user_to_global_community, create_session_token
from services.errors import ApiError, bad_request, not_found, payment_required, unprocessable
from services.sso_http import SsoHttp, check_outbound_url
from services.sso_oidc import OidcClient, new_nonce, new_pkce_pair, validate_oidc_settings
from services.sso_settings import GOOGLE_DISCOVERY_URL, GOOGLE_ISSUER, SsoSettings
from services.sso_telemetry import (
    connection_change_counter,
    log_sso_error,
    login_counter,
    login_duration,
    sso_span,
    start_counter,
)
from services.sso_types import (
    PROTOCOL_GOOGLE,
    PROTOCOL_OIDC,
    PROTOCOL_SAML,
    REASON_ACCOUNT_CONFLICT,
    REASON_ACCOUNT_INACTIVE,
    REASON_DOMAIN_NOT_ALLOWED,
    REASON_EMAIL_REQUIRED,
    REASON_EMAIL_UNVERIFIED,
    REASON_NOT_ENTITLED,
    REASON_SESSION_MISMATCH,
    VALID_PROTOCOLS,
    ExternalIdentity,
    LoginOutcome,
    OidcSettings,
    SamlSettings,
    SsoConfigError,
    SsoConnection,
    SsoError,
    SsoPolicyError,
    SsoProtocolError,
    flag_for_protocol,
)

logger = logging.getLogger(__name__)

_DOMAIN_RE: Final = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_SCOPE_TOKEN_RE: Final = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]{1,100}$")
_MAX_DOMAINS: Final = 20
_MAX_CERTS: Final = 5
_MAX_NAME_LEN: Final = 100
_MAX_CLIENT_ID_LEN: Final = 255
_MAX_CLIENT_SECRET_LEN: Final = 1024
_MAX_URL_LEN: Final = 2048
_MAX_ATTRIBUTE_NAME_LEN: Final = 255
_DEFAULT_OIDC_SCOPES: Final[tuple[str, ...]] = ("openid", "email", "profile")

#: Free/public mailbox providers: no organisation owns these domains, so a tenant
#: may not claim them as "its" login domain.
PUBLIC_MAILBOX_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
        "yahoo.com", "ymail.com", "aol.com", "icloud.com", "me.com", "mac.com", "proton.me",
        "protonmail.com", "pm.me", "gmx.com", "gmx.net", "mail.com", "zoho.com", "yandex.com",
        "qq.com", "163.com", "126.com", "fastmail.com", "hey.com",
    }
)  # fmt: skip


@dataclass(slots=True, frozen=True)
class SsoContext:
    """Everything the SSO services need from the running app, passed explicitly."""

    install_dal: Any
    async_dal: Any
    dal: Any
    cfg: HubAPIConfig
    settings: SsoSettings
    #: Test seam: swaps the IdP *socket* only; all guard/parse/validate logic still runs.
    transport: httpx.AsyncBaseTransport | None = None
    clock: Callable[[], float] = field(default=time.time, repr=False)


@dataclass(slots=True, frozen=True)
class ConnectionInput:
    """Raw, unvalidated connection fields from an admin request. `None` = not supplied."""

    display_name: str | None = None
    enabled: bool | None = None
    allowed_domains: list[str] | None = None
    # OIDC / Google
    issuer: str | None = None
    discovery_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    clear_client_secret: bool = False
    scopes: list[str] | None = None
    hosted_domain: str | None = None
    use_platform_client: bool | None = None
    # SAML
    idp_metadata_xml: str | None = None
    idp_entity_id: str | None = None
    idp_sso_url: str | None = None
    idp_certificates: list[str] | None = None
    name_id_format: str | None = None
    email_attribute: str | None = None
    name_attribute: str | None = None
    force_authn: bool | None = None


@dataclass(slots=True, frozen=True)
class SpUrls:
    """The externally reachable URLs for one connection."""

    entity_id: str
    acs_url: str
    callback_url: str
    metadata_url: str
    login_url: str


@dataclass(slots=True, frozen=True)
class LoginOption:
    """A login button the tenant's sign-in page can render."""

    id: str
    display_name: str
    protocol: str


@dataclass(slots=True, frozen=True)
class BeginResult:
    """Result of starting a login: where to send the browser and the binder cookie to set."""

    redirect_url: str
    state: str
    binder: str
    protocol: str
    cookie_cross_site: bool


@dataclass(slots=True, frozen=True)
class TenantRef:
    """The three tenant facts the SSO flows need."""

    id: int
    slug: str
    is_active: bool


def sp_urls(cfg: HubAPIConfig, public_id: str) -> SpUrls:
    """Compute the SP-side URLs for `public_id` from the configured external base URL."""
    base = cfg.identity_callback_base_url.rstrip("/")
    root = f"{base}/api/v1/auth/sso/{public_id}"
    return SpUrls(
        entity_id=f"{root}/metadata",
        acs_url=f"{root}/acs",
        callback_url=f"{root}/callback",
        metadata_url=f"{root}/metadata",
        login_url=f"{root}/login",
    )


# --------------------------------------------------------------------------
# Table access + row mapping
# --------------------------------------------------------------------------


def _table(ctx: SsoContext, name: str) -> Any:
    table = getattr(ctx.install_dal, name, None)
    if table is None:
        raise SsoConfigError(
            "sso_schema_missing",
            f"table {name!r} is not present -- run the 0049_sso_connections migration",
        )
    return table


def _oidc_of(conn: SsoConnection) -> OidcSettings:
    """Return the OIDC body of `conn`, or fail loudly if it is not an OIDC/Google connection."""
    if conn.oidc is None:
        raise SsoConfigError("wrong_protocol", "connection is not an OIDC/Google connection")
    return conn.oidc


def _saml_of(conn: SsoConnection) -> SamlSettings:
    """Return the SAML body of `conn`, or fail loudly if it is not a SAML connection."""
    if conn.saml is None:
        raise SsoConfigError("wrong_protocol", "connection is not a SAML connection")
    return conn.saml


def _tuple_of_str(value: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in value) if isinstance(value, list | tuple) else ()


def _row_to_connection(row: Any) -> SsoConnection:
    config = row.config if isinstance(row.config, dict) else {}
    protocol = str(row.protocol)
    oidc: OidcSettings | None = None
    saml: SamlSettings | None = None
    if protocol in (PROTOCOL_OIDC, PROTOCOL_GOOGLE):
        oidc = OidcSettings(
            issuer=str(config.get("issuer", "")),
            client_id=str(config.get("client_id") or ""),
            discovery_url=str(config.get("discovery_url", "")),
            scopes=_tuple_of_str(config.get("scopes")) or _DEFAULT_OIDC_SCOPES,
            allowed_domains=_tuple_of_str(config.get("allowed_domains")),
            hosted_domain=config.get("hosted_domain") or None,
            use_platform_client=bool(config.get("use_platform_client", False)),
        )
    elif protocol == PROTOCOL_SAML:
        saml = SamlSettings(
            idp_entity_id=str(config.get("idp_entity_id", "")),
            idp_sso_url=str(config.get("idp_sso_url", "")),
            idp_certs_pem=_tuple_of_str(config.get("idp_certs_pem")),
            name_id_format=str(config.get("name_id_format") or sso_saml.NAMEID_EMAIL),
            allowed_domains=_tuple_of_str(config.get("allowed_domains")),
            email_attribute=config.get("email_attribute") or None,
            name_attribute=config.get("name_attribute") or None,
            force_authn=bool(config.get("force_authn", False)),
        )
    else:
        raise SsoConfigError("unknown_protocol", "stored connection has an unknown protocol")
    return SsoConnection(
        id=int(row.id),
        public_id=str(row.public_id),
        tenant_id=int(row.tenant_id),
        protocol=protocol,
        display_name=str(row.display_name),
        enabled=bool(row.enabled),
        oidc=oidc,
        saml=saml,
        secret_ciphertext=row.secret_ciphertext,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _settings_to_config(
    conn_protocol: str, oidc: OidcSettings | None, saml: SamlSettings | None
) -> dict[str, Any]:
    """Serialise validated settings to the JSON stored in `sso_connections.config`."""
    if conn_protocol == PROTOCOL_SAML and saml is not None:
        return {
            "idp_entity_id": saml.idp_entity_id,
            "idp_sso_url": saml.idp_sso_url,
            "idp_certs_pem": list(saml.idp_certs_pem),
            "name_id_format": saml.name_id_format,
            "allowed_domains": list(saml.allowed_domains),
            "email_attribute": saml.email_attribute,
            "name_attribute": saml.name_attribute,
            "force_authn": saml.force_authn,
        }
    if oidc is not None:
        return {
            "issuer": oidc.issuer,
            "client_id": oidc.client_id or None,
            "discovery_url": oidc.discovery_url,
            "scopes": list(oidc.scopes),
            "allowed_domains": list(oidc.allowed_domains),
            "hosted_domain": oidc.hosted_domain,
            "use_platform_client": oidc.use_platform_client,
        }
    raise SsoConfigError("unknown_protocol", "cannot serialise settings without a protocol body")


async def _tenant_by_id(ctx: SsoContext, tenant_id: int) -> TenantRef | None:
    rows = await ctx.async_dal.select_async(ctx.dal(ctx.dal.tenants.id == tenant_id))
    if not rows:
        return None
    row = rows.first()
    return TenantRef(id=int(row.id), slug=str(row.slug), is_active=bool(row.is_active))


async def _tenant_by_slug(ctx: SsoContext, slug: str) -> TenantRef | None:
    rows = await ctx.async_dal.select_async(ctx.dal(ctx.dal.tenants.slug == slug))
    if not rows:
        return None
    row = rows.first()
    return TenantRef(id=int(row.id), slug=str(row.slug), is_active=bool(row.is_active))


async def load_connection(ctx: SsoContext, public_id: str) -> SsoConnection | None:
    """Fetch one connection by its public id (no tenant filter -- callers must scope it)."""
    table = _table(ctx, "sso_connections")
    rows = list(await ctx.install_dal(table.public_id == public_id).select())
    return _row_to_connection(rows[0]) if rows else None


async def list_connections(ctx: SsoContext, tenant_id: int) -> list[SsoConnection]:
    """All connections of `tenant_id`, oldest first."""
    table = _table(ctx, "sso_connections")
    rows = list(await ctx.install_dal(table.tenant_id == tenant_id).select(orderby=table.id))
    return [_row_to_connection(r) for r in rows]


async def get_connection(ctx: SsoContext, tenant_id: int, public_id: str) -> SsoConnection:
    """Tenant-scoped fetch; another tenant's connection is indistinguishable from a missing one."""
    conn = await load_connection(ctx, public_id)
    if conn is None or conn.tenant_id != tenant_id:
        raise not_found("SSO connection not found")
    return conn


# --------------------------------------------------------------------------
# Entitlement
# --------------------------------------------------------------------------


async def is_entitled(tenant_slug: str, protocol: str) -> bool:
    """True when the two-gate entitlement check passes for `tenant_slug` and `protocol`."""
    return bool(await feature_enabled(flag_for_protocol(protocol), tenant=tenant_slug))


async def _require_entitled(tenant_slug: str, protocol: str) -> None:
    if not await is_entitled(tenant_slug, protocol):
        tier = "Enterprise" if protocol != PROTOCOL_GOOGLE else "Professional"
        raise payment_required(f"{protocol} SSO requires the {tier} plan or higher")


# --------------------------------------------------------------------------
# Validation of admin input
# --------------------------------------------------------------------------


def _clean_text(
    value: str | None, *, field_name: str, max_len: int, required: bool = False
) -> str | None:
    if value is None:
        if required:
            raise bad_request(f"{field_name} is required")
        return None
    cleaned = value.strip()
    if not cleaned:
        if required:
            raise bad_request(f"{field_name} is required")
        return None
    if len(cleaned) > max_len:
        raise bad_request(f"{field_name} must be at most {max_len} characters")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in cleaned):
        raise bad_request(f"{field_name} contains control characters")
    return cleaned


def _require_text(value: str | None, *, field_name: str, max_len: int) -> str:
    """Like `_clean_text(required=True)` but typed to return `str` (never None)."""
    cleaned = _clean_text(value, field_name=field_name, max_len=max_len, required=True)
    if cleaned is None:
        raise bad_request(f"{field_name} is required")
    return cleaned


def _clean_domains(values: list[str] | None, *, protocol: str) -> tuple[str, ...]:
    if not values:
        raise bad_request("allowedDomains must list at least one email domain")
    if len(values) > _MAX_DOMAINS:
        raise bad_request(f"allowedDomains may list at most {_MAX_DOMAINS} domains")
    cleaned: list[str] = []
    for raw in values:
        domain = raw.strip().lower().lstrip("@")
        if not _DOMAIN_RE.match(domain):
            raise bad_request("allowedDomains contains an invalid domain name")
        if domain in PUBLIC_MAILBOX_DOMAINS:
            raise bad_request(
                "allowedDomains may not include a public mailbox provider; "
                "list only domains your organisation owns"
            )
        if domain not in cleaned:
            cleaned.append(domain)
    return tuple(cleaned)


def _clean_https_url(value: str | None, *, field_name: str, settings: SsoSettings) -> str:
    url = _require_text(value, field_name=field_name, max_len=_MAX_URL_LEN)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    approved = host in settings.allowed_private_hosts
    if parsed.scheme != "https" and not (parsed.scheme == "http" and approved):
        raise bad_request(f"{field_name} must be an https URL")
    if not host or parsed.username or parsed.password or parsed.fragment:
        raise bad_request(f"{field_name} is not a valid URL")
    return url


def _clean_scopes(values: list[str] | None) -> tuple[str, ...]:
    scopes = tuple(dict.fromkeys(values)) if values else _DEFAULT_OIDC_SCOPES
    for scope in scopes:
        if not _SCOPE_TOKEN_RE.match(scope):
            raise bad_request("scopes contains an invalid scope token")
    if "openid" not in scopes:
        raise bad_request("scopes must include 'openid'")
    if len(scopes) > 20:
        raise bad_request("too many scopes")
    return scopes


def _input_from_connection(conn: SsoConnection) -> ConnectionInput:
    """Project an existing connection back to editable input (never including secrets)."""
    if conn.oidc is not None:
        o = conn.oidc
        return ConnectionInput(
            display_name=conn.display_name,
            enabled=conn.enabled,
            allowed_domains=list(o.allowed_domains),
            issuer=o.issuer,
            discovery_url=o.discovery_url,
            client_id=o.client_id or None,
            scopes=list(o.scopes),
            hosted_domain=o.hosted_domain,
            use_platform_client=o.use_platform_client,
        )
    s = _saml_of(conn)
    return ConnectionInput(
        display_name=conn.display_name,
        enabled=conn.enabled,
        allowed_domains=list(s.allowed_domains),
        idp_entity_id=s.idp_entity_id,
        idp_sso_url=s.idp_sso_url,
        idp_certificates=list(s.idp_certs_pem),
        name_id_format=s.name_id_format,
        email_attribute=s.email_attribute,
        name_attribute=s.name_attribute,
        force_authn=s.force_authn,
    )


def _overlay(base: ConnectionInput, patch: ConnectionInput) -> ConnectionInput:
    """Overlay `patch`'s supplied (non-None) fields onto `base`."""
    changes = {
        f.name: getattr(patch, f.name)
        for f in fields(patch)
        if getattr(patch, f.name) is not None and f.name != "clear_client_secret"
    }
    merged = replace(base, **changes)
    return replace(merged, clear_client_secret=patch.clear_client_secret)


@dataclass(slots=True, frozen=True)
class _Validated:
    display_name: str
    enabled: bool
    oidc: OidcSettings | None
    saml: SamlSettings | None
    #: Plaintext secret to (re-)encrypt, or None to keep whatever is stored.
    new_secret: str | None


async def _validate_input(
    ctx: SsoContext, protocol: str, data: ConnectionInput, *, has_stored_secret: bool = False
) -> _Validated:
    """Validate `data` for `protocol`; a connection is a *draft* until it is enabled.

    Drafts exist because both SAML and OIDC need the connection's own callback /
    ACS URL (which contains its `public_id`) to register the app at the IdP --
    so the connection must be creatable before the IdP details exist. Whatever
    IS supplied is always validated; completeness (issuer, client id, signing
    certificates, ...) is enforced only when `enabled` is true.
    """
    if protocol not in VALID_PROTOCOLS:
        raise bad_request(f"protocol must be one of {', '.join(VALID_PROTOCOLS)}")
    display_name = _require_text(data.display_name, field_name="displayName", max_len=_MAX_NAME_LEN)
    domains = _clean_domains(data.allowed_domains, protocol=protocol)
    enabled = bool(data.enabled)

    if protocol == PROTOCOL_SAML:
        saml = _validate_saml(ctx, data, domains, complete=enabled)
        return _Validated(display_name, enabled, None, saml, None)

    secret = _clean_text(
        data.client_secret, field_name="clientSecret", max_len=_MAX_CLIENT_SECRET_LEN
    )
    client_id = _clean_text(data.client_id, field_name="clientId", max_len=_MAX_CLIENT_ID_LEN)
    if protocol == PROTOCOL_GOOGLE:
        oidc = _validate_google(
            ctx,
            data,
            domains,
            client_id=client_id,
            has_secret=secret is not None,
            has_stored_secret=has_stored_secret,
            complete=enabled,
        )
    else:
        oidc = await _validate_generic_oidc(
            ctx, data, domains, client_id=client_id, complete=enabled
        )
    return _Validated(display_name, enabled, oidc, None, secret)


async def _validate_generic_oidc(
    ctx: SsoContext,
    data: ConnectionInput,
    domains: tuple[str, ...],
    *,
    client_id: str | None,
    complete: bool,
) -> OidcSettings:
    issuer = ""
    discovery = ""
    if data.issuer:
        issuer = _clean_https_url(data.issuer, field_name="issuer", settings=ctx.settings)
        discovery = (
            _clean_https_url(data.discovery_url, field_name="discoveryUrl", settings=ctx.settings)
            if data.discovery_url
            else issuer.rstrip("/") + "/.well-known/openid-configuration"
        )
    elif complete:
        raise bad_request("issuer is required to enable an OIDC connection")
    if complete and client_id is None:
        raise bad_request("clientId is required to enable an OIDC connection")
    oidc = OidcSettings(
        issuer=issuer,
        client_id=client_id or "",
        discovery_url=discovery,
        scopes=_clean_scopes(data.scopes),
        allowed_domains=domains,
    )
    if issuer:
        try:
            if complete:
                validate_oidc_settings(oidc)
            await check_outbound_url(discovery, ctx.settings)
        except SsoConfigError as exc:
            raise unprocessable(exc.message) from exc
    return oidc


def _validate_google(
    ctx: SsoContext,
    data: ConnectionInput,
    domains: tuple[str, ...],
    *,
    client_id: str | None,
    has_secret: bool,
    has_stored_secret: bool,
    complete: bool,
) -> OidcSettings:
    hosted = _clean_text(data.hosted_domain, field_name="hostedDomain", max_len=253)
    hosted = hosted.lower() if hosted else (domains[0] if len(domains) == 1 else None)
    if hosted is None:
        raise bad_request("hostedDomain is required when allowedDomains lists several domains")
    if not _DOMAIN_RE.match(hosted) or hosted in PUBLIC_MAILBOX_DOMAINS:
        raise bad_request("hostedDomain must be a Google Workspace domain you own")
    if hosted not in domains:
        raise bad_request("hostedDomain must also appear in allowedDomains")

    own_client = client_id is not None or has_secret or has_stored_secret
    use_platform = bool(data.use_platform_client) or not own_client
    if use_platform and (client_id or has_secret):
        raise bad_request("usePlatformClient cannot be combined with clientId/clientSecret")
    if complete:
        if use_platform and not ctx.settings.platform_google_configured:
            raise unprocessable(
                "this deployment has no shared Google OAuth client; "
                "supply clientId and clientSecret"
            )
        if not use_platform and (client_id is None or not (has_secret or has_stored_secret)):
            raise bad_request("clientId and clientSecret are required for your own Google client")
    return OidcSettings(
        issuer=GOOGLE_ISSUER,
        client_id=client_id or "",
        discovery_url=GOOGLE_DISCOVERY_URL,
        scopes=("openid", "email", "profile"),
        allowed_domains=domains,
        hosted_domain=hosted,
        use_platform_client=use_platform,
    )


def _validate_saml(
    ctx: SsoContext, data: ConnectionInput, domains: tuple[str, ...], *, complete: bool
) -> SamlSettings:
    entity_id = _clean_text(data.idp_entity_id, field_name="idpEntityId", max_len=1024)
    sso_url = _clean_text(data.idp_sso_url, field_name="idpSsoUrl", max_len=_MAX_URL_LEN)
    certs: list[str] = list(data.idp_certificates or [])
    if data.idp_metadata_xml:
        if len(data.idp_metadata_xml) > 512_000:
            raise bad_request("idpMetadataXml is too large")
        try:
            meta = sso_saml.parse_idp_metadata(data.idp_metadata_xml.encode("utf-8"))
        except SsoProtocolError as exc:
            raise unprocessable(f"IdP metadata rejected: {exc.message}") from exc
        except SsoConfigError as exc:
            raise unprocessable(exc.message) from exc
        entity_id, sso_url = meta.entity_id, meta.sso_url
        certs = list(meta.certs_pem)

    if complete:
        if entity_id is None:
            raise bad_request("idpEntityId (or idpMetadataXml) is required to enable SAML")
        if sso_url is None:
            raise bad_request("idpSsoUrl (or idpMetadataXml) is required to enable SAML")
        if not certs:
            raise bad_request("at least one IdP signing certificate is required to enable SAML")
    if sso_url is not None:
        sso_url = _clean_https_url(sso_url, field_name="idpSsoUrl", settings=ctx.settings)
    if len(certs) > _MAX_CERTS:
        raise bad_request(f"at most {_MAX_CERTS} IdP certificates are allowed")
    try:
        normalized = tuple(dict.fromkeys(sso_saml.normalize_certificate(c) for c in certs))
    except SsoConfigError as exc:
        raise unprocessable(exc.message) from exc

    name_id_format = data.name_id_format or sso_saml.NAMEID_EMAIL
    if name_id_format not in sso_saml.ALLOWED_NAMEID_FORMATS:
        raise bad_request("nameIdFormat must be emailAddress, persistent or unspecified")
    return SamlSettings(
        idp_entity_id=entity_id or "",
        idp_sso_url=sso_url or "",
        idp_certs_pem=normalized,
        name_id_format=name_id_format,
        allowed_domains=domains,
        email_attribute=_clean_text(
            data.email_attribute, field_name="emailAttribute", max_len=_MAX_ATTRIBUTE_NAME_LEN
        ),
        name_attribute=_clean_text(
            data.name_attribute, field_name="nameAttribute", max_len=_MAX_ATTRIBUTE_NAME_LEN
        ),
        force_authn=bool(data.force_authn),
    )


# --------------------------------------------------------------------------
# Admin CRUD
# --------------------------------------------------------------------------


async def _audit(
    ctx: SsoContext, *, actor_id: int | None, action: str, public_id: str, details: dict[str, Any]
) -> None:
    """Best-effort `audit_log` row; a failure is logged (never swallowed) but never aborts."""
    try:
        await ctx.install_dal.audit_log.async_insert(
            user_id=actor_id,
            action=action,
            target_type="sso_connection",
            target_id=public_id,
            details=details,
            created_at=datetime.now(UTC),
        )
    except Exception as exc:
        log_sso_error(logger, "sso.audit.write_failed", exc, connection=public_id)


async def create_connection(
    ctx: SsoContext,
    *,
    tenant: TenantRef,
    actor_id: int | None,
    protocol: str,
    data: ConnectionInput,
) -> SsoConnection:
    """Validate and store a new connection (disabled unless `data.enabled`)."""
    if protocol not in VALID_PROTOCOLS:
        raise bad_request(f"protocol must be one of {', '.join(VALID_PROTOCOLS)}")
    await _require_entitled(tenant.slug, protocol)
    validated = await _validate_input(ctx, protocol, data)
    table = _table(ctx, "sso_connections")
    public_id = str(uuid.uuid4())
    secret_ct = (
        sso_crypto.encrypt_secret(validated.new_secret, aad=public_id)
        if validated.new_secret
        else None
    )
    now = datetime.now(UTC)
    try:
        await table.async_insert(
            public_id=public_id,
            tenant_id=tenant.id,
            protocol=protocol,
            display_name=validated.display_name,
            enabled=validated.enabled,
            config=_settings_to_config(protocol, validated.oidc, validated.saml),
            secret_ciphertext=secret_ct,
            created_by_user_id=actor_id,
            created_at=now,
            updated_at=now,
        )
    except Exception as exc:
        if await _display_name_taken(ctx, tenant.id, validated.display_name):
            raise ApiError(
                "A connection with that displayName already exists", 409, "CONFLICT"
            ) from exc
        raise
    connection_change_counter.add(1, {"protocol": protocol, "operation": "create"})
    logger.info(
        "sso.connection.created connection=%s tenant_id=%s protocol=%s enabled=%s",
        public_id,
        tenant.id,
        protocol,
        validated.enabled,
    )
    await _audit(
        ctx,
        actor_id=actor_id,
        action="sso.connection.create",
        public_id=public_id,
        details={"protocol": protocol, "tenant_id": tenant.id, "enabled": validated.enabled},
    )
    return await get_connection(ctx, tenant.id, public_id)


async def _display_name_taken(ctx: SsoContext, tenant_id: int, display_name: str) -> bool:
    table = _table(ctx, "sso_connections")
    rows = list(
        await ctx.install_dal(
            (table.tenant_id == tenant_id) & (table.display_name == display_name)
        ).select()
    )
    return bool(rows)


async def update_connection(
    ctx: SsoContext,
    *,
    tenant: TenantRef,
    actor_id: int | None,
    public_id: str,
    patch: ConnectionInput,
) -> SsoConnection:
    """Apply `patch` to a connection (re-validating the merged result in full)."""
    existing = await get_connection(ctx, tenant.id, public_id)
    wants_enable = patch.enabled is True and not existing.enabled
    touches_config = patch.clear_client_secret or any(
        getattr(patch, f.name) is not None
        for f in fields(patch)
        if f.name not in ("enabled", "clear_client_secret")
    )
    if wants_enable or touches_config:
        await _require_entitled(tenant.slug, existing.protocol)

    if patch.enabled is False and not touches_config:
        # Switching a connection OFF must always work -- including when its stored
        # config would no longer validate (an IdP cert that has since expired), and
        # when the tenant is no longer entitled. No revalidation, just the flag.
        table = _table(ctx, "sso_connections")
        await ctx.install_dal(table.id == existing.id).update(
            enabled=False, updated_at=datetime.now(UTC)
        )
        connection_change_counter.add(1, {"protocol": existing.protocol, "operation": "disable"})
        logger.info(
            "sso.connection.disabled connection=%s tenant_id=%s protocol=%s",
            public_id,
            tenant.id,
            existing.protocol,
        )
        await _audit(
            ctx,
            actor_id=actor_id,
            action="sso.connection.disable",
            public_id=public_id,
            details={"protocol": existing.protocol, "tenant_id": tenant.id},
        )
        return await get_connection(ctx, tenant.id, public_id)

    merged = _overlay(_input_from_connection(existing), patch)
    if existing.protocol == PROTOCOL_GOOGLE:
        if patch.use_platform_client is True:
            merged = replace(merged, client_id=None)
        elif patch.use_platform_client is None and (patch.client_id or patch.client_secret):
            merged = replace(merged, use_platform_client=False)
    validated = await _validate_input(
        ctx,
        existing.protocol,
        merged,
        has_stored_secret=existing.secret_ciphertext is not None and not patch.clear_client_secret,
    )

    secret_ct = existing.secret_ciphertext
    if existing.protocol == PROTOCOL_GOOGLE and validated.oidc is not None:
        if validated.oidc.use_platform_client:
            secret_ct = None
    if patch.clear_client_secret:
        secret_ct = None
    if validated.new_secret:
        secret_ct = sso_crypto.encrypt_secret(validated.new_secret, aad=public_id)

    table = _table(ctx, "sso_connections")
    await ctx.install_dal(table.id == existing.id).update(
        display_name=validated.display_name,
        enabled=validated.enabled,
        config=_settings_to_config(existing.protocol, validated.oidc, validated.saml),
        secret_ciphertext=secret_ct,
        updated_at=datetime.now(UTC),
    )
    connection_change_counter.add(1, {"protocol": existing.protocol, "operation": "update"})
    logger.info(
        "sso.connection.updated connection=%s tenant_id=%s protocol=%s enabled=%s",
        public_id,
        tenant.id,
        existing.protocol,
        validated.enabled,
    )
    await _audit(
        ctx,
        actor_id=actor_id,
        action="sso.connection.update",
        public_id=public_id,
        details={
            "protocol": existing.protocol,
            "tenant_id": tenant.id,
            "enabled": validated.enabled,
            "config_changed": touches_config,
        },
    )
    return await get_connection(ctx, tenant.id, public_id)


async def delete_connection(
    ctx: SsoContext, *, tenant: TenantRef, actor_id: int | None, public_id: str
) -> None:
    """Delete a connection (and, by FK cascade, every identity link it created)."""
    existing = await get_connection(ctx, tenant.id, public_id)
    table = _table(ctx, "sso_connections")
    # The FK is ON DELETE CASCADE in Postgres; deleting the links explicitly keeps
    # that guarantee on every backend (and makes the intent visible here).
    identities = _table(ctx, "sso_identities")
    await ctx.install_dal(identities.connection_id == existing.id).delete()
    await ctx.install_dal(table.id == existing.id).delete()
    connection_change_counter.add(1, {"protocol": existing.protocol, "operation": "delete"})
    logger.info(
        "sso.connection.deleted connection=%s tenant_id=%s protocol=%s",
        public_id,
        tenant.id,
        existing.protocol,
    )
    await _audit(
        ctx,
        actor_id=actor_id,
        action="sso.connection.delete",
        public_id=public_id,
        details={"protocol": existing.protocol, "tenant_id": tenant.id},
    )


# --------------------------------------------------------------------------
# Public login: discovery of options, start
# --------------------------------------------------------------------------


async def list_login_options(ctx: SsoContext, tenant_slug: str) -> list[LoginOption]:
    """Enabled, currently-entitled connections for `tenant_slug` (empty for unknown tenants)."""
    tenant = await _tenant_by_slug(ctx, tenant_slug)
    if tenant is None or not tenant.is_active:
        return []
    options: list[LoginOption] = []
    for conn in await list_connections(ctx, tenant.id):
        if conn.enabled and await is_entitled(tenant.slug, conn.protocol):
            options.append(LoginOption(conn.public_id, conn.display_name, conn.protocol))
    return options


async def _resolve_loginable(ctx: SsoContext, public_id: str) -> tuple[SsoConnection, TenantRef]:
    """Load an enabled connection of an active, entitled tenant -- or raise `SsoPolicyError`."""
    conn = await load_connection(ctx, public_id)
    if conn is None or not conn.enabled:
        raise SsoPolicyError("connection_unavailable", "SSO connection is unavailable")
    tenant = await _tenant_by_id(ctx, conn.tenant_id)
    if tenant is None or not tenant.is_active:
        raise SsoPolicyError("tenant_unavailable", "tenant is unavailable")
    if not await is_entitled(tenant.slug, conn.protocol):
        raise SsoPolicyError(
            "not_entitled",
            "tenant is not entitled to this SSO protocol",
            reason=REASON_NOT_ENTITLED,
        )
    return conn, tenant


def _http(ctx: SsoContext, protocol: str) -> SsoHttp:
    return SsoHttp(ctx.settings, protocol=protocol, transport=ctx.transport)


async def _oidc_credentials(ctx: SsoContext, conn: SsoConnection) -> tuple[str, str | None]:
    """Return `(client_id, client_secret)` for `conn`, decrypting the stored secret."""
    oidc = _oidc_of(conn)
    if oidc.use_platform_client:
        if not ctx.settings.platform_google_configured:
            raise SsoConfigError(
                "platform_google_missing", "shared Google client is not configured"
            )
        return str(ctx.settings.google_client_id), ctx.settings.google_client_secret
    secret = (
        sso_crypto.decrypt_secret(conn.secret_ciphertext, aad=conn.public_id)
        if conn.secret_ciphertext
        else None
    )
    return oidc.client_id, secret


async def begin_login(ctx: SsoContext, public_id: str) -> BeginResult:
    """Start a login for `public_id`: mint state, build the IdP redirect, derive the binder."""
    started = time.monotonic()
    conn, _tenant = await _resolve_loginable(ctx, public_id)
    urls = sp_urls(ctx.cfg, conn.public_id)
    with sso_span(
        "sso.login.start", **{"sso.protocol": conn.protocol, "sso.connection": public_id}
    ):
        if conn.protocol == PROTOCOL_SAML:
            saml = _saml_of(conn)
            # The request id must be stored with the state, but the state token is
            # also the RelayState inside the request URL: mint the id first.
            request_id = sso_saml.new_request_id()
            state = await sso_state.create_state(
                sso_state.SsoStatePayload(
                    connection_public_id=conn.public_id,
                    protocol=conn.protocol,
                    request_id=request_id,
                ),
                ttl_s=ctx.settings.state_ttl_s,
            )
            _, redirect_url = sso_saml.build_authn_request(
                saml,
                sp_entity_id=urls.entity_id,
                acs_url=urls.acs_url,
                relay_state=state,
                request_id=request_id,
            )
            cross_site = True
        else:
            oidc = _oidc_of(conn)
            client_id, _secret = await _oidc_credentials(ctx, conn)
            nonce = new_nonce()
            verifier, challenge = new_pkce_pair()
            state = await sso_state.create_state(
                sso_state.SsoStatePayload(
                    connection_public_id=conn.public_id,
                    protocol=conn.protocol,
                    nonce=nonce,
                    code_verifier=verifier,
                ),
                ttl_s=ctx.settings.state_ttl_s,
            )
            redirect_url = await OidcClient(
                _http(ctx, conn.protocol), ctx.settings, clock=ctx.clock
            ).authorization_url(
                oidc,
                client_id=client_id,
                redirect_uri=urls.callback_url,
                state=state,
                nonce=nonce,
                code_challenge=challenge,
            )
            cross_site = False
    start_counter.add(1, {"protocol": conn.protocol})
    logger.debug(
        "sso.login.started connection=%s protocol=%s elapsed_ms=%d",
        public_id,
        conn.protocol,
        int((time.monotonic() - started) * 1000),
    )
    return BeginResult(
        redirect_url=redirect_url,
        state=state,
        binder=sso_crypto.binder_value(state),
        protocol=conn.protocol,
        cookie_cross_site=cross_site,
    )


# --------------------------------------------------------------------------
# Public login: completion
# --------------------------------------------------------------------------


def _record(protocol: str, started: float, *, outcome: str, reason: str = "none") -> None:
    login_counter.add(1, {"protocol": protocol, "outcome": outcome, "reason": reason})
    login_duration.record(time.monotonic() - started, {"protocol": protocol})


async def _consume_bound_state(
    state: str, binder_cookie: str | None, public_id: str, protocol_expected: str
) -> sso_state.SsoStatePayload:
    payload = await sso_state.consume_state(state)
    if payload is None:
        raise SsoProtocolError(
            "state_unknown",
            "login state is missing, expired or already used",
            reason=REASON_SESSION_MISMATCH,
        )
    if payload.connection_public_id != public_id or payload.protocol != protocol_expected:
        raise SsoProtocolError(
            "state_connection_mismatch",
            "login state belongs to a different connection",
            reason=REASON_SESSION_MISMATCH,
        )
    if not sso_crypto.verify_binder(state, binder_cookie):
        raise SsoProtocolError(
            "binder_mismatch",
            "login was not started in this browser",
            reason=REASON_SESSION_MISMATCH,
        )
    return payload


async def complete_oidc(
    ctx: SsoContext, public_id: str, *, code: str, state: str, binder_cookie: str | None
) -> LoginOutcome:
    """Finish an OIDC/Google login from the IdP's redirect back (`code` + `state`)."""
    started = time.monotonic()
    protocol = PROTOCOL_OIDC
    try:
        conn, tenant = await _resolve_loginable(ctx, public_id)
        protocol = conn.protocol
        if conn.oidc is None:
            raise SsoConfigError("wrong_protocol", "connection is not an OIDC connection")
        payload = await _consume_bound_state(state, binder_cookie, public_id, conn.protocol)
        if payload.code_verifier is None or payload.nonce is None:
            raise SsoProtocolError("state_incomplete", "login state carries no PKCE/nonce")
        client_id, secret = await _oidc_credentials(ctx, conn)
        urls = sp_urls(ctx.cfg, conn.public_id)
        identity = await OidcClient(
            _http(ctx, conn.protocol), ctx.settings, clock=ctx.clock
        ).complete(
            conn.oidc,
            protocol=conn.protocol,
            client_id=client_id,
            client_secret=secret,
            redirect_uri=urls.callback_url,
            code=code,
            code_verifier=payload.code_verifier,
            expected_nonce=payload.nonce,
        )
        if conn.protocol == PROTOCOL_GOOGLE:
            _enforce_google_policy(conn, identity)
        outcome = await _finish_login(ctx, conn, tenant, identity)
    except SsoError as exc:
        _record(protocol, started, outcome="failure", reason=exc.code)
        log_sso_error(logger, "sso.login.failed", exc, connection=public_id)
        raise
    except Exception as exc:
        _record(protocol, started, outcome="failure", reason="unexpected")
        log_sso_error(logger, "sso.login.crashed", exc, connection=public_id)
        raise
    _record(protocol, started, outcome="success")
    return outcome


async def complete_saml(
    ctx: SsoContext,
    public_id: str,
    *,
    saml_response: str,
    relay_state: str,
    binder_cookie: str | None,
) -> LoginOutcome:
    """Finish a SAML login from the IdP's POST to the ACS."""
    started = time.monotonic()
    try:
        conn, tenant = await _resolve_loginable(ctx, public_id)
        if conn.saml is None:
            raise SsoConfigError("wrong_protocol", "connection is not a SAML connection")
        payload = await _consume_bound_state(relay_state, binder_cookie, public_id, PROTOCOL_SAML)
        if payload.request_id is None:
            raise SsoProtocolError("state_incomplete", "login state carries no AuthnRequest id")
        urls = sp_urls(ctx.cfg, conn.public_id)
        validated = await asyncio.to_thread(
            sso_saml.validate_response,
            conn.saml,
            saml_response_b64=saml_response,
            expected_request_id=payload.request_id,
            sp_entity_id=urls.entity_id,
            acs_url=urls.acs_url,
            clock_skew_s=ctx.settings.clock_skew_s,
        )
        remaining = int((validated.not_on_or_after - datetime.now(UTC)).total_seconds())
        fresh = await sso_state.remember_assertion(
            f"{conn.public_id}:{validated.assertion_id}",
            ttl_s=remaining + ctx.settings.clock_skew_s,
        )
        if not fresh:
            raise SsoProtocolError("saml_replay", "assertion has already been used")
        outcome = await _finish_login(ctx, conn, tenant, validated.identity)
    except SsoError as exc:
        _record(PROTOCOL_SAML, started, outcome="failure", reason=exc.code)
        log_sso_error(logger, "sso.login.failed", exc, connection=public_id)
        raise
    except Exception as exc:
        _record(PROTOCOL_SAML, started, outcome="failure", reason="unexpected")
        log_sso_error(logger, "sso.login.crashed", exc, connection=public_id)
        raise
    _record(PROTOCOL_SAML, started, outcome="success")
    return outcome


def _enforce_google_policy(conn: SsoConnection, identity: ExternalIdentity) -> None:
    """Google logins must come from the tenant's Workspace domain with a verified email."""
    oidc = _oidc_of(conn)
    if not identity.email_verified:
        raise SsoPolicyError(
            "email_unverified",
            "Google reports the email as unverified",
            reason=REASON_EMAIL_UNVERIFIED,
        )
    hosted = oidc.hosted_domain
    if hosted and identity.hosted_domain != hosted:
        raise SsoPolicyError(
            "hosted_domain_mismatch",
            "Google account is not in the tenant's Workspace domain",
            reason=REASON_DOMAIN_NOT_ALLOWED,
        )


def _email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


def _enforce_domain_policy(conn: SsoConnection, identity: ExternalIdentity) -> None:
    allowed = conn.allowed_domains
    if not allowed or identity.email is None:
        return
    if _email_domain(identity.email) not in allowed:
        raise SsoPolicyError(
            "domain_not_allowed",
            "email domain is not permitted for this connection",
            reason=REASON_DOMAIN_NOT_ALLOWED,
        )


async def _finish_login(
    ctx: SsoContext, conn: SsoConnection, tenant: TenantRef, identity: ExternalIdentity
) -> LoginOutcome:
    """Resolve or provision the hub user for `identity`, then mint the tenant session."""
    with sso_span("sso.login.provision", **{"sso.protocol": conn.protocol}):
        _enforce_domain_policy(conn, identity)
        user_row, created = await _resolve_user(ctx, conn, identity)

    user = SessionUser(
        id=int(user_row.id),
        email=user_row.email,
        username=user_row.username,
        avatar_url=user_row.avatar_url,
        is_super_admin=bool(user_row.is_super_admin),
        is_vendor=bool(user_row.is_vendor),
        is_analytics_consumer=bool(user_row.is_analytics_consumer),
    )
    await ctx.async_dal.update_async(ctx.dal.hub_users.id == user.id, last_login=datetime.now(UTC))
    token = await create_session_token(
        ctx.async_dal,
        ctx.dal,
        ctx.cfg,
        user=user,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
    )
    user_uuid = str(user_row.uuid)
    logger.info(
        "sso.login.succeeded connection=%s protocol=%s user_uuid=%s created=%s",
        conn.public_id,
        conn.protocol,
        user_uuid,
        created,
    )
    return LoginOutcome(
        token=token,
        user_uuid=user_uuid,
        connection_public_id=conn.public_id,
        protocol=conn.protocol,
        created_user=created,
    )


async def _find_identity_link(ctx: SsoContext, conn_id: int, subject: str) -> Any | None:
    table = _table(ctx, "sso_identities")
    rows = list(
        await ctx.install_dal(
            (table.connection_id == conn_id) & (table.subject == subject)
        ).select()
    )
    return rows[0] if rows else None


async def _user_row(ctx: SsoContext, user_id: int) -> Any | None:
    rows = await ctx.async_dal.select_async(ctx.dal(ctx.dal.hub_users.id == user_id))
    return rows.first() if rows else None


async def _resolve_user(
    ctx: SsoContext, conn: SsoConnection, identity: ExternalIdentity
) -> tuple[Any, bool]:
    """Return `(hub_users row, created)` for `identity` on `conn`; JIT-provision on first login."""
    link = await _find_identity_link(ctx, conn.id, identity.subject)
    if link is not None:
        return await _existing_user(ctx, conn, link), False

    if identity.email is None:
        raise SsoPolicyError(
            "email_required",
            "the IdP did not supply an email address",
            reason=REASON_EMAIL_REQUIRED,
        )
    if not identity.email_verified:
        raise SsoPolicyError(
            "email_unverified",
            "the IdP did not verify the email address",
            reason=REASON_EMAIL_UNVERIFIED,
        )

    existing = await ctx.async_dal.select_async(ctx.dal(ctx.dal.hub_users.email == identity.email))
    if existing:
        # Never adopt by email: hub_users is global across tenants (see module docstring).
        raise SsoPolicyError(
            "account_conflict",
            "an account with this email already exists and is not linked to this connection",
            reason=REASON_ACCOUNT_CONFLICT,
        )

    now = datetime.now(UTC)
    # pydal returns a `Reference` (an int subclass whose lazy attributes confuse
    # SQLAlchemy's column coercion when handed to penguin-dal) -- normalise to a plain int.
    new_id = await ctx.async_dal.insert_async(
        ctx.dal.hub_users,
        email=identity.email,
        display_name=identity.display_name,
        username=None,
        is_active=True,
        email_verified=True,
        created_at=now,
        updated_at=now,
    )
    new_id = int(new_id)
    table = _table(ctx, "sso_identities")
    try:
        await table.async_insert(
            connection_id=conn.id,
            subject=identity.subject,
            hub_user_id=new_id,
            created_at=now,
            last_login_at=now,
        )
    except Exception as exc:
        # Lost a first-login race on (connection, subject): adopt the winner's row and
        # discard the orphan user this attempt just created.
        winner = await _find_identity_link(ctx, conn.id, identity.subject)
        await ctx.async_dal.delete_async(ctx.dal.hub_users.id == new_id)
        if winner is None:
            raise
        log_sso_error(
            logger, "sso.login.identity_race", exc, connection=conn.public_id, level=logging.WARNING
        )
        return await _existing_user(ctx, conn, winner), False
    await add_user_to_global_community(ctx.async_dal, ctx.dal, user_id=new_id)
    row = await _user_row(ctx, int(new_id))
    if row is None:
        raise SsoConfigError("user_vanished", "freshly created user could not be read back")
    return row, True


async def _existing_user(ctx: SsoContext, conn: SsoConnection, link: Any) -> Any:
    row = await _user_row(ctx, int(link.hub_user_id))
    if row is None:
        raise SsoConfigError("identity_orphaned", "identity link points at a missing user")
    if not row.is_active:
        raise SsoPolicyError(
            "account_inactive", "the linked account is inactive", reason=REASON_ACCOUNT_INACTIVE
        )
    table = _table(ctx, "sso_identities")
    await ctx.install_dal(table.id == link.id).update(last_login_at=datetime.now(UTC))
    return row


__all__ = [
    "BeginResult",
    "ConnectionInput",
    "LoginOption",
    "PUBLIC_MAILBOX_DOMAINS",
    "SpUrls",
    "SsoContext",
    "TenantRef",
    "begin_login",
    "complete_oidc",
    "complete_saml",
    "create_connection",
    "delete_connection",
    "get_connection",
    "is_entitled",
    "list_connections",
    "list_login_options",
    "load_connection",
    "sp_urls",
    "update_connection",
]
