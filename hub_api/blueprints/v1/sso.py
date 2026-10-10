"""v1 enterprise SSO -- SAML 2.0 / OIDC (Enterprise) and Google OAuth2 (Professional).

Two blueprints, split the same way `tenant_twitch_install.py` and the OAuth login
flow split theirs:

**Public (pre-auth) -- `/api/v1/auth/sso/...`.** No bearer token exists yet, so
none of these routes trusts anything tenant-shaped from the request: the tenant
comes from the connection row looked up by its opaque `public_id`.

* `GET  /options?tenant=<slug>`      -- login buttons the tenant's sign-in page shows
* `GET  /<id>/start`                 -- JSON `{redirectUrl}` (SPA-driven navigation)
* `GET  /<id>/login`                 -- same, as a direct 302 (IdP-portal bookmarks)
* `GET  /<id>/callback`              -- OIDC / Google redirect URI
* `POST /<id>/acs`                   -- SAML Assertion Consumer Service
* `GET  /<id>/metadata`              -- SAML SP metadata (public by necessity; no secrets)

A successful login hands off exactly like the existing OAuth login: a single-use
60-second exchange code in the redirect URL, redeemed for the session JWT by
`POST /api/v1/auth/exchange` -- the JWT itself never appears in a URL.

**Tenant admin -- `/api/v1/tenant/sso/connections`.** `tenant_middleware` ->
`require_scope("auth.sso:admin")` (tenant before scope, security.md), CRUD for the
tenant's own connections. Responses are explicit DTOs: the encrypted client secret
never leaves the database, only `hasClientSecret`.

Failure handling is deliberately uniform towards browsers: any login failure is a
redirect to `<frontend>/login?error=<fixed reason code>`; the detail (type, safe
message, frame-only traceback, `connection=<public_id>`) goes to the log. No
attacker-controlled text is ever reflected.

Everything here is behind the entitlement gate in `services.sso_service` and the
feature flags default OFF -- an unentitled tenant gets 402 on admin routes and an
empty options list / generic failure on the public ones.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from pydantic import ConfigDict
from quart import Blueprint, Response, after_this_request, current_app, redirect, request
from quart_schema import validate_request, validate_response
from werkzeug.wrappers import Response as WerkzeugResponse

from config import HubAPIConfig
from services import oauth_service
from services.current_user import get_current_user_id
from services.errors import ApiError, not_found
from services.sso_saml import build_sp_metadata
from services.sso_service import (
    ConnectionInput,
    SsoContext,
    TenantRef,
    begin_login,
    complete_oidc,
    complete_saml,
    create_connection,
    delete_connection,
    get_connection,
    is_entitled,
    list_connections,
    list_login_options,
    load_connection,
    sp_urls,
    update_connection,
)
from services.sso_settings import SsoSettings, load_settings
from services.sso_telemetry import log_sso_error
from services.sso_types import (
    PROTOCOL_SAML,
    REASON_DENIED,
    REASON_NOT_ENTITLED,
    REASON_UNAVAILABLE,
    SCOPE_SSO_ADMIN,
    LoginOutcome,
    SsoConfigError,
    SsoConnection,
    SsoError,
    SsoIdpUnavailableError,
)

logger = logging.getLogger(__name__)

#: `after_this_request` is typed against Quart's `Response | werkzeug Response` union
#: (same alias `services/session_cookie.py` uses).
_AnyResponse = Response | WerkzeugResponse

sso_public_bp = Blueprint("v1_sso_public", __name__, url_prefix="/api/v1/auth/sso")
sso_admin_bp = Blueprint("v1_sso_admin", __name__, url_prefix="/api/v1/tenant/sso")

#: Browser-binding cookie guarding each login flow against login CSRF.
BINDER_COOKIE = "wb_sso_bind"

#: `app.config` keys tests (and only tests) use to inject a socket-level IdP
#: transport and pre-built settings; production never sets either.
TRANSPORT_CONFIG_KEY = "sso_http_transport"
SETTINGS_CONFIG_KEY = "sso_settings"


# --------------------------------------------------------------------------
# Wiring helpers
# --------------------------------------------------------------------------


def _cfg() -> HubAPIConfig:
    return cast(HubAPIConfig, current_app.config["HUB_API_CONFIG"])


def _settings() -> SsoSettings:
    cached = current_app.config.get(SETTINGS_CONFIG_KEY)
    if cached is None:
        cached = load_settings()
        current_app.config[SETTINGS_CONFIG_KEY] = cached
    return cast(SsoSettings, cached)


def _ctx() -> SsoContext:
    return SsoContext(
        install_dal=current_app.config["install_dal"],
        async_dal=current_app.config["async_dal"],
        dal=current_app.config["dal"],
        cfg=_cfg(),
        settings=_settings(),
        transport=current_app.config.get(TRANSPORT_CONFIG_KEY),
    )


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _sso_err(exc: SsoError) -> tuple[dict[str, object], int]:
    """Map an `SsoError` to the admin/JSON error envelope (fixed, application-authored text)."""
    if exc.reason == REASON_NOT_ENTITLED:
        return _err(ApiError("This feature requires a higher plan", 402, "FEATURE_NOT_ENABLED"))
    if isinstance(exc, SsoConfigError):
        return _err(ApiError(exc.message, 503, "SSO_UNAVAILABLE"))
    if isinstance(exc, SsoIdpUnavailableError):
        return _err(ApiError("The identity provider is unreachable", 502, "IDP_UNAVAILABLE"))
    return _err(ApiError("SSO is unavailable for this connection", 404, "NOT_FOUND"))


def _tenant_from_token() -> TenantRef:
    tenant_ctx = get_tenant_context(request)
    if tenant_ctx is None:
        # tenant_middleware always publishes this before the handler runs.
        raise ApiError("Tenant context missing", 403, "FORBIDDEN")
    return TenantRef(id=tenant_ctx.tenant_id, slug=tenant_ctx.tenant_slug, is_active=True)


def _login_error_redirect(reason: str) -> Response:
    """302 to the SPA's sign-in page with a fixed, browser-safe reason code."""
    response = redirect(f"{_cfg().frontend_origin}/login?error={reason}", code=302)
    response.headers["Cache-Control"] = "no-store"
    return cast(Response, response)


def _set_binder_cookie(response: Response, public_id: str, value: str, *, cross_site: bool) -> None:
    response.set_cookie(
        BINDER_COOKIE,
        value,
        max_age=_settings().state_ttl_s,
        path=f"/api/v1/auth/sso/{public_id}",
        secure=True,
        httponly=True,
        # SAML's ACS receives a cross-site POST, which a Lax cookie would not
        # accompany. The binder is a login-CSRF token, not a session credential.
        samesite="None" if cross_site else "Lax",
    )


def _clear_binder_cookie(response: Response, public_id: str) -> None:
    response.delete_cookie(BINDER_COOKIE, path=f"/api/v1/auth/sso/{public_id}")


# --------------------------------------------------------------------------
# DTOs (security.md Output Validation: explicit shapes, never raw rows)
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SsoOptionDTO:
    """One login button."""

    id: str
    displayName: str
    protocol: str


@dataclass(slots=True, frozen=True)
class SsoOptionsResponse:
    """Response for `GET /options`."""

    success: bool
    options: list[SsoOptionDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class SsoStartResponse:
    """Response for `GET /<id>/start`: where the SPA navigates the browser."""

    success: bool
    redirectUrl: str
    protocol: str


@dataclass(slots=True, frozen=True)
class SsoConnectionDTO:
    """Admin view of one connection. Carries no secret -- only `hasClientSecret`."""

    id: str
    protocol: str
    displayName: str
    enabled: bool
    entitled: bool
    allowedDomains: list[str]
    issuer: str | None
    discoveryUrl: str | None
    clientId: str | None
    hasClientSecret: bool
    usePlatformClient: bool
    hostedDomain: str | None
    scopes: list[str]
    idpEntityId: str | None
    idpSsoUrl: str | None
    idpCertificateCount: int
    nameIdFormat: str | None
    emailAttribute: str | None
    nameAttribute: str | None
    forceAuthn: bool
    spEntityId: str
    acsUrl: str | None
    callbackUrl: str | None
    metadataUrl: str | None
    loginUrl: str
    createdAt: str | None
    updatedAt: str | None


@dataclass(slots=True, frozen=True)
class SsoConnectionResponse:
    """Response wrapping one connection."""

    success: bool
    connection: SsoConnectionDTO


@dataclass(slots=True, frozen=True)
class SsoConnectionListResponse:
    """Response wrapping the tenant's connections."""

    success: bool
    connections: list[SsoConnectionDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class SsoDeleteResponse:
    """Response for DELETE."""

    success: bool


@dataclass(slots=True, frozen=True)
class CreateConnectionRequest:
    """Body of `POST /connections`. Unknown fields are a 400."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    protocol: str
    displayName: str
    allowedDomains: list[str]
    enabled: bool = False
    issuer: str | None = None
    discoveryUrl: str | None = None
    clientId: str | None = None
    clientSecret: str | None = field(default=None, repr=False)
    scopes: list[str] | None = None
    hostedDomain: str | None = None
    usePlatformClient: bool | None = None
    idpMetadataXml: str | None = None
    idpEntityId: str | None = None
    idpSsoUrl: str | None = None
    idpCertificates: list[str] | None = None
    nameIdFormat: str | None = None
    emailAttribute: str | None = None
    nameAttribute: str | None = None
    forceAuthn: bool | None = None


@dataclass(slots=True, frozen=True)
class UpdateConnectionRequest:
    """Body of `PATCH /connections/<id>`. Omitted fields are unchanged; protocol is immutable."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    displayName: str | None = None
    allowedDomains: list[str] | None = None
    enabled: bool | None = None
    issuer: str | None = None
    discoveryUrl: str | None = None
    clientId: str | None = None
    clientSecret: str | None = field(default=None, repr=False)
    clearClientSecret: bool = False
    scopes: list[str] | None = None
    hostedDomain: str | None = None
    usePlatformClient: bool | None = None
    idpMetadataXml: str | None = None
    idpEntityId: str | None = None
    idpSsoUrl: str | None = None
    idpCertificates: list[str] | None = None
    nameIdFormat: str | None = None
    emailAttribute: str | None = None
    nameAttribute: str | None = None
    forceAuthn: bool | None = None


def _input_from_create(body: CreateConnectionRequest) -> ConnectionInput:
    return ConnectionInput(
        display_name=body.displayName,
        enabled=body.enabled,
        allowed_domains=body.allowedDomains,
        issuer=body.issuer,
        discovery_url=body.discoveryUrl,
        client_id=body.clientId,
        client_secret=body.clientSecret,
        scopes=body.scopes,
        hosted_domain=body.hostedDomain,
        use_platform_client=body.usePlatformClient,
        idp_metadata_xml=body.idpMetadataXml,
        idp_entity_id=body.idpEntityId,
        idp_sso_url=body.idpSsoUrl,
        idp_certificates=body.idpCertificates,
        name_id_format=body.nameIdFormat,
        email_attribute=body.emailAttribute,
        name_attribute=body.nameAttribute,
        force_authn=body.forceAuthn,
    )


def _input_from_update(body: UpdateConnectionRequest) -> ConnectionInput:
    return ConnectionInput(
        display_name=body.displayName,
        enabled=body.enabled,
        allowed_domains=body.allowedDomains,
        issuer=body.issuer,
        discovery_url=body.discoveryUrl,
        client_id=body.clientId,
        client_secret=body.clientSecret,
        clear_client_secret=body.clearClientSecret,
        scopes=body.scopes,
        hosted_domain=body.hostedDomain,
        use_platform_client=body.usePlatformClient,
        idp_metadata_xml=body.idpMetadataXml,
        idp_entity_id=body.idpEntityId,
        idp_sso_url=body.idpSsoUrl,
        idp_certificates=body.idpCertificates,
        name_id_format=body.nameIdFormat,
        email_attribute=body.emailAttribute,
        name_attribute=body.nameAttribute,
        force_authn=body.forceAuthn,
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def _connection_dto(
    ctx: SsoContext, tenant: TenantRef, conn: SsoConnection
) -> SsoConnectionDTO:
    urls = sp_urls(ctx.cfg, conn.public_id)
    is_saml = conn.protocol == PROTOCOL_SAML
    oidc, saml = conn.oidc, conn.saml
    return SsoConnectionDTO(
        id=conn.public_id,
        protocol=conn.protocol,
        displayName=conn.display_name,
        enabled=conn.enabled,
        entitled=await is_entitled(tenant.slug, conn.protocol),
        allowedDomains=list(conn.allowed_domains),
        issuer=(oidc.issuer or None) if oidc else None,
        discoveryUrl=(oidc.discovery_url or None) if oidc else None,
        clientId=(oidc.client_id or None) if oidc else None,
        hasClientSecret=conn.secret_ciphertext is not None,
        usePlatformClient=bool(oidc and oidc.use_platform_client),
        hostedDomain=oidc.hosted_domain if oidc else None,
        scopes=list(oidc.scopes) if oidc else [],
        idpEntityId=(saml.idp_entity_id or None) if saml else None,
        idpSsoUrl=(saml.idp_sso_url or None) if saml else None,
        idpCertificateCount=len(saml.idp_certs_pem) if saml else 0,
        nameIdFormat=saml.name_id_format if saml else None,
        emailAttribute=saml.email_attribute if saml else None,
        nameAttribute=saml.name_attribute if saml else None,
        forceAuthn=bool(saml and saml.force_authn),
        spEntityId=urls.entity_id,
        acsUrl=urls.acs_url if is_saml else None,
        callbackUrl=None if is_saml else urls.callback_url,
        metadataUrl=urls.metadata_url if is_saml else None,
        loginUrl=urls.login_url,
        createdAt=_iso(conn.created_at),
        updatedAt=_iso(conn.updated_at),
    )


# --------------------------------------------------------------------------
# Public routes
# --------------------------------------------------------------------------


@sso_public_bp.route("/options", methods=["GET"])
@validate_response(SsoOptionsResponse)
async def sso_options() -> SsoOptionsResponse | tuple[dict[str, object], int]:
    """`GET /options?tenant=<slug>` -- enabled, currently-entitled SSO buttons for a tenant."""
    slug = (request.args.get("tenant") or "global").strip()[:100]
    try:
        options = await list_login_options(_ctx(), slug)
    except SsoError as exc:
        log_sso_error(logger, "sso.options.failed", exc)
        return _sso_err(exc)
    return SsoOptionsResponse(
        success=True,
        options=[
            SsoOptionDTO(id=o.id, displayName=o.display_name, protocol=o.protocol) for o in options
        ],
    )


@sso_public_bp.route("/<public_id>/start", methods=["GET"])
@validate_response(SsoStartResponse)
async def sso_start(public_id: str) -> SsoStartResponse | tuple[dict[str, object], int]:
    """`GET /<id>/start` -- begin a login; sets the binder cookie, returns the IdP URL as JSON."""
    try:
        begun = await begin_login(_ctx(), public_id)
    except SsoError as exc:
        log_sso_error(logger, "sso.start.failed", exc, connection=public_id)
        return _sso_err(exc)

    @after_this_request
    def _attach(response: _AnyResponse) -> _AnyResponse:
        _set_binder_cookie(
            cast(Response, response), public_id, begun.binder, cross_site=begun.cookie_cross_site
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    return SsoStartResponse(success=True, redirectUrl=begun.redirect_url, protocol=begun.protocol)


@sso_public_bp.route("/<public_id>/login", methods=["GET"])
async def sso_login(public_id: str) -> Response:
    """`GET /<id>/login` -- begin a login and 302 straight to the IdP."""
    try:
        begun = await begin_login(_ctx(), public_id)
    except SsoError as exc:
        log_sso_error(logger, "sso.login_redirect.failed", exc, connection=public_id)
        return _login_error_redirect(exc.reason)
    response = cast(Response, redirect(begun.redirect_url, code=302))
    _set_binder_cookie(response, public_id, begun.binder, cross_site=begun.cookie_cross_site)
    response.headers["Cache-Control"] = "no-store"
    return response


async def _finish_redirect(public_id: str, outcome_token: str, protocol: str) -> Response:
    """Mint the single-use exchange code and redirect the browser into the SPA."""
    ctx = _ctx()
    code = await oauth_service.create_oauth_exchange_code(
        ctx.async_dal, ctx.dal, token=outcome_token, platform="sso"
    )
    response = cast(
        Response, redirect(f"{_cfg().frontend_origin}/auth/callback?code={code}", code=303)
    )
    _clear_binder_cookie(response, public_id)
    response.headers["Cache-Control"] = "no-store"
    logger.debug("sso.login.handoff connection=%s protocol=%s", public_id, protocol)
    return response


async def _complete_and_redirect(
    public_id: str, start_completion: Callable[[], Awaitable[LoginOutcome]]
) -> Response:
    """Run a login completion and turn ANY outcome into a browser redirect.

    A browser mid-login cannot do anything useful with a JSON error or a bare 500, so
    every failure becomes `<frontend>/login?error=<fixed reason>`. Nothing is hidden by
    this: `SsoError`s were already logged (type, code, safe message, frame-only traceback)
    and counted by `services.sso_service.complete_*`; anything else is logged HERE with
    its type and frames before the generic `sso_unavailable` redirect, never swallowed.
    """
    try:
        outcome = await start_completion()
        return await _finish_redirect(public_id, outcome.token, outcome.protocol)
    except SsoError as exc:
        # Already logged (type, code, frames) and counted by services.sso_service.complete_*.
        reason = exc.reason  # a fixed REASON_* constant, never attacker-controlled text
        logger.debug(
            "sso.callback.rejected connection=%s reason=%s code=%s", public_id, reason, exc.code
        )
        return _login_error_redirect(reason)
    except Exception as exc:
        log_sso_error(logger, "sso.callback.unexpected_failure", exc, connection=public_id)
        return _login_error_redirect(REASON_UNAVAILABLE)


@sso_public_bp.route("/<public_id>/callback", methods=["GET"])
async def sso_oidc_callback(public_id: str) -> Response:
    """`GET /<id>/callback` -- OIDC / Google redirect URI."""
    if request.args.get("error"):
        logger.info("sso.callback.idp_denied connection=%s", public_id)
        return _login_error_redirect(REASON_DENIED)
    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        return _login_error_redirect(REASON_DENIED)
    binder_cookie = request.cookies.get(BINDER_COOKIE)
    return await _complete_and_redirect(
        public_id,
        lambda: complete_oidc(
            _ctx(), public_id, code=code, state=state, binder_cookie=binder_cookie
        ),
    )


@sso_public_bp.route("/<public_id>/acs", methods=["POST"])
async def sso_saml_acs(public_id: str) -> Response:
    """`POST /<id>/acs` -- SAML Assertion Consumer Service (HTTP-POST binding)."""
    form = await request.form
    saml_response = form.get("SAMLResponse", "")
    relay_state = form.get("RelayState", "")
    if not saml_response or not relay_state:
        return _login_error_redirect(REASON_DENIED)
    binder_cookie = request.cookies.get(BINDER_COOKIE)
    return await _complete_and_redirect(
        public_id,
        lambda: complete_saml(
            _ctx(),
            public_id,
            saml_response=saml_response,
            relay_state=relay_state,
            binder_cookie=binder_cookie,
        ),
    )


@sso_public_bp.route("/<public_id>/metadata", methods=["GET"])
async def sso_saml_metadata(public_id: str) -> tuple[Response, int] | tuple[dict[str, object], int]:
    """`GET /<id>/metadata` -- SAML SP metadata for the IdP admin. Public; contains no secret."""
    try:
        conn = await load_connection(_ctx(), public_id)
    except SsoError as exc:
        log_sso_error(logger, "sso.metadata.failed", exc, connection=public_id)
        return _sso_err(exc)
    if conn is None or conn.saml is None:
        return _err(not_found("SAML connection not found"))
    urls = sp_urls(_cfg(), conn.public_id)
    xml = build_sp_metadata(
        entity_id=urls.entity_id, acs_url=urls.acs_url, name_id_format=conn.saml.name_id_format
    )
    response = Response(xml, mimetype="application/samlmetadata+xml")
    response.headers["Cache-Control"] = "public, max-age=300"
    return response, 200


# --------------------------------------------------------------------------
# Tenant-admin routes
# --------------------------------------------------------------------------


@sso_admin_bp.route("/connections", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(SCOPE_SSO_ADMIN)  # type: ignore[untyped-decorator]
@validate_response(SsoConnectionListResponse)
async def list_sso_connections() -> SsoConnectionListResponse | tuple[dict[str, object], int]:
    """`GET /connections` -- the caller's tenant's connections (no secrets)."""
    try:
        tenant = _tenant_from_token()
        ctx = _ctx()
        conns = await list_connections(ctx, tenant.id)
        dtos = [await _connection_dto(ctx, tenant, c) for c in conns]
    except ApiError as exc:
        logger.debug(
            "sso.admin.request_rejected path=%s status=%s code=%s",
            request.path,
            exc.status_code,
            exc.code,
        )
        return _err(exc)
    except SsoError as exc:
        log_sso_error(logger, "sso.admin.list_failed", exc)
        return _sso_err(exc)
    return SsoConnectionListResponse(success=True, connections=dtos)


@sso_admin_bp.route("/connections", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(SCOPE_SSO_ADMIN)  # type: ignore[untyped-decorator]
@validate_request(CreateConnectionRequest)
@validate_response(SsoConnectionResponse, 201)
async def create_sso_connection(
    data: CreateConnectionRequest,
) -> tuple[SsoConnectionResponse, int] | tuple[dict[str, object], int]:
    """`POST /connections` -- create a connection (a disabled draft unless `enabled: true`)."""
    try:
        tenant = _tenant_from_token()
        ctx = _ctx()
        conn = await create_connection(
            ctx,
            tenant=tenant,
            actor_id=get_current_user_id(request),
            protocol=data.protocol,
            data=_input_from_create(data),
        )
        dto = await _connection_dto(ctx, tenant, conn)
    except ApiError as exc:
        logger.debug(
            "sso.admin.request_rejected path=%s status=%s code=%s",
            request.path,
            exc.status_code,
            exc.code,
        )
        return _err(exc)
    except SsoError as exc:
        log_sso_error(logger, "sso.admin.create_failed", exc)
        return _sso_err(exc)
    return SsoConnectionResponse(success=True, connection=dto), 201


@sso_admin_bp.route("/connections/<public_id>", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(SCOPE_SSO_ADMIN)  # type: ignore[untyped-decorator]
@validate_response(SsoConnectionResponse)
async def get_sso_connection(
    public_id: str,
) -> SsoConnectionResponse | tuple[dict[str, object], int]:
    """`GET /connections/<id>` -- one of the caller's tenant's connections."""
    try:
        tenant = _tenant_from_token()
        ctx = _ctx()
        conn = await get_connection(ctx, tenant.id, public_id)
        dto = await _connection_dto(ctx, tenant, conn)
    except ApiError as exc:
        logger.debug(
            "sso.admin.request_rejected path=%s status=%s code=%s",
            request.path,
            exc.status_code,
            exc.code,
        )
        return _err(exc)
    except SsoError as exc:
        log_sso_error(logger, "sso.admin.get_failed", exc, connection=public_id)
        return _sso_err(exc)
    return SsoConnectionResponse(success=True, connection=dto)


@sso_admin_bp.route("/connections/<public_id>", methods=["PATCH"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(SCOPE_SSO_ADMIN)  # type: ignore[untyped-decorator]
@validate_request(UpdateConnectionRequest)
@validate_response(SsoConnectionResponse)
async def update_sso_connection(
    public_id: str, data: UpdateConnectionRequest
) -> SsoConnectionResponse | tuple[dict[str, object], int]:
    """`PATCH /connections/<id>` -- partial update; enabling re-validates the full config."""
    try:
        tenant = _tenant_from_token()
        ctx = _ctx()
        conn = await update_connection(
            ctx,
            tenant=tenant,
            actor_id=get_current_user_id(request),
            public_id=public_id,
            patch=_input_from_update(data),
        )
        dto = await _connection_dto(ctx, tenant, conn)
    except ApiError as exc:
        logger.debug(
            "sso.admin.request_rejected path=%s status=%s code=%s",
            request.path,
            exc.status_code,
            exc.code,
        )
        return _err(exc)
    except SsoError as exc:
        log_sso_error(logger, "sso.admin.update_failed", exc, connection=public_id)
        return _sso_err(exc)
    return SsoConnectionResponse(success=True, connection=dto)


@sso_admin_bp.route("/connections/<public_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(SCOPE_SSO_ADMIN)  # type: ignore[untyped-decorator]
@validate_response(SsoDeleteResponse)
async def delete_sso_connection(
    public_id: str,
) -> SsoDeleteResponse | tuple[dict[str, object], int]:
    """`DELETE /connections/<id>` -- always allowed, regardless of entitlement."""
    try:
        tenant = _tenant_from_token()
        await delete_connection(
            _ctx(), tenant=tenant, actor_id=get_current_user_id(request), public_id=public_id
        )
    except ApiError as exc:
        logger.debug(
            "sso.admin.request_rejected path=%s status=%s code=%s",
            request.path,
            exc.status_code,
            exc.code,
        )
        return _err(exc)
    except SsoError as exc:
        log_sso_error(logger, "sso.admin.delete_failed", exc, connection=public_id)
        return _sso_err(exc)
    return SsoDeleteResponse(success=True)


BLUEPRINTS = [sso_public_bp, sso_admin_bp]
