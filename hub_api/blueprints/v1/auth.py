"""v1 `auth` group -- ported from `authController.js` (migration plan phase M1).

Full port of `routes/auth.js`'s contract: local email/password auth,
legacy admin login, temp-password login, OAuth login/link/unlink
(Discord/Twitch/Slack), session refresh/logout, `/me`, and password
changes. Copy-me pattern (route -> `tenant_middleware` where the
endpoint is tenant-scoped -> `require_scope` -> `@validate_request`/
`@validate_response` DTOs) matches `blueprints/v2/platform.py`; every
path below is IDENTICAL to `admin/hub_module/backend/src/routes/auth.js`
mounted at `/api/v1/auth` (verified against `frontend/src/services/
api.js` and `contexts/AuthContext.jsx` -- see `hub_api/PORTING.md`).

Pre-auth endpoints (login, register, oauth start/callback, refresh,
tenant lookup) carry NO `tenant_middleware`/`require_scope` -- there is
no JWT yet to resolve a tenant from, matching Node's `requireAuth`-free
routes in `routes/auth.js`. Endpoints requiring an existing session
(set-password, oauth link/unlink) resolve the caller via
`services.current_user.get_current_user_id` -- see that module's
docstring for why `tenant_middleware`/`require_scope` alone don't expose
`sub`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from flask_core.api_utils import error_response
from quart import Blueprint, current_app, redirect, request
from quart_schema import validate_request, validate_response

from config import HubAPIConfig
from services import auth_service, branding_service, oauth_service, passkey_service
from services.current_user import get_current_user_id, get_optional_current_user_id
from services.dto_response import jsonify_dto
from services.errors import ApiError
from services.session_cookie import (
    clear_session_cookie_after_request,
    issue_session_cookie,
    set_session_cookie,
)

auth_bp = Blueprint("v1_auth", __name__, url_prefix="/api/v1/auth")


def _cfg() -> HubAPIConfig:
    """Return the app's `HubAPIConfig` (cast -- Quart's config storage is Any-typed)."""
    return cast(HubAPIConfig, current_app.config["HUB_API_CONFIG"])


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _callback_base_url() -> str:
    """Return hub-api's own externally-reachable base URL for OAuth redirects."""
    return _cfg().identity_callback_base_url


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    # flask_core is `follow_imports = "skip"` (pyproject.toml) -- error_response()
    # is typed Any from mypy's perspective, cast to the real, stable shape.
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


# ---------------------------------------------------------------------------
# DTOs -- camelCase field names deliberately break PEP8 snake_case
# convention: these are wire contracts pinned to `frontend/src/services/
# api.js` (migration plan D2, security.md Output Validation), and quart-
# schema serializes a plain dataclass field name as the JSON key verbatim
# with no alias mechanism in this codebase's established pattern
# (`blueprints/v2/platform.py` has no camelCase fields to demonstrate
# this, but the rule is the same either way).
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class LoginUserDTO:
    """Login user DTO."""

    id: int
    email: str | None
    username: str | None
    avatarUrl: str | None
    isSuperAdmin: bool
    isVendor: bool
    isAnalyticsConsumer: bool
    linkedPlatforms: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class LoginResponse:
    """Response DTO for login endpoints."""

    success: bool
    token: str
    user: LoginUserDTO


@dataclass(slots=True, frozen=True)
class VerificationRequiredResponse:
    """Response DTO for verification required endpoints."""

    success: bool
    requiresVerification: bool
    message: str


@dataclass(slots=True, frozen=True)
class RegisterSuccessResponse:
    """Response DTO for `POST /register` and `GET /verify-email`'s full-success branch."""

    success: bool
    token: str
    user: LoginUserDTO
    message: str | None = None


@dataclass(slots=True, frozen=True)
class VerificationPendingResponse:
    """Response DTO for `POST /register`'s email-verification-required branch.

    Deliberately its OWN status code (202, not 200) rather than a union
    DTO with `Optional[LoginUserDTO]` sharing `RegisterSuccessResponse`'s
    200 -- an `Optional[<nested dataclass>]` field on a response model
    reliably crashed quart-schema's app-wide response-conversion hook
    (`pydantic.TypeAdapter(...).dump_python()` raising `TypeError: 'None'
    is not an instance of 'SchemaSerializer'`) once this blueprint's full
    set of DTOs was registered together -- reproduced, isolated, and
    confirmed NOT to reproduce with only 1-2 DTOs registered (a pydantic-
    core / quart-schema interaction under many co-registered dataclasses,
    not a bug in this DTO's own shape). Splitting into two required-only-
    fields DTOs at two distinct 2xx status codes (both still `response.
    data.success`-readable by `AuthContext.jsx`, which never branches on
    the exact status code) sidesteps it entirely. See `hub_api/PORTING.md`.
    """

    success: bool
    requiresVerification: bool
    message: str


@dataclass(slots=True, frozen=True)
class RegisterRequest:
    """Request DTO for register endpoints."""

    email: str
    password: str
    username: str | None = None
    captcha_token: str | None = None


@dataclass(slots=True, frozen=True)
class LoginRequest:
    """Request DTO for login endpoints."""

    email: str
    password: str
    tenantSlug: str | None = None


@dataclass(slots=True, frozen=True)
class AdminLoginRequest:
    """Request DTO for admin login endpoints."""

    username: str
    password: str


@dataclass(slots=True, frozen=True)
class AdminLoginUserDTO:
    """Admin login user DTO."""

    id: int
    email: str | None
    username: str | None
    avatarUrl: str | None
    isAdmin: bool
    isSuperAdmin: bool


@dataclass(slots=True, frozen=True)
class AdminLoginResponse:
    """Response DTO for admin login endpoints."""

    success: bool
    token: str
    user: AdminLoginUserDTO


@dataclass(slots=True, frozen=True)
class TempPasswordLoginRequest:
    """Request DTO for temp password login endpoints."""

    identifier: str
    password: str


@dataclass(slots=True, frozen=True)
class TempPasswordLoginResponse:
    """Response DTO for temp password login endpoints."""

    success: bool
    token: str
    requiresOAuthLink: bool


@dataclass(slots=True, frozen=True)
class RefreshResponse:
    """Response DTO for refresh endpoints."""

    success: bool
    token: str


@dataclass(slots=True, frozen=True)
class SimpleSuccessResponse:
    """Response DTO for simple success endpoints."""

    success: bool


@dataclass(slots=True, frozen=True)
class MessageResponse:
    """Response DTO for `{success, message}` endpoints (resend-verification)."""

    success: bool
    message: str


@dataclass(slots=True, frozen=True)
class LinkedPlatformDTO:
    """Linked platform DTO."""

    platform: str
    username: str | None
    avatar: str | None


@dataclass(slots=True, frozen=True)
class CommunityMembershipDTO:
    """Community membership DTO."""

    id: int
    name: str
    displayName: str
    role: str


@dataclass(slots=True, frozen=True)
class MeUserDTO:
    """Me user DTO."""

    id: int
    email: str | None
    username: str | None
    avatarUrl: str | None
    isSuperAdmin: bool
    isVendor: bool
    isAnalyticsConsumer: bool
    hasPassword: bool
    linkedPlatforms: list[LinkedPlatformDTO]
    roles: list[str]
    communities: list[CommunityMembershipDTO]


@dataclass(slots=True, frozen=True)
class MeResponse:
    """Response DTO for me endpoints."""

    success: bool
    user: MeUserDTO | None


@dataclass(slots=True, frozen=True)
class SetPasswordRequest:
    """Request DTO for set password endpoints."""

    newPassword: str
    currentPassword: str | None = None


@dataclass(slots=True, frozen=True)
class OAuthStartResponse:
    """Response DTO for o auth start endpoints."""

    success: bool
    authorizeUrl: str
    state: str


@dataclass(slots=True, frozen=True)
class ExchangeCodeRequest:
    """Request DTO for the OAuth exchange-code handoff endpoint."""

    code: str


@dataclass(slots=True, frozen=True)
class ExchangeCodeResponse:
    """Response DTO for the OAuth exchange-code handoff endpoint."""

    success: bool
    token: str


@dataclass(slots=True, frozen=True)
class TenantConfigDTO:
    """Tenant config DTO."""

    theme: str | None
    welcomeMessage: str | None


@dataclass(slots=True, frozen=True)
class TenantLoginInfoDTO:
    """Tenant login info DTO."""

    slug: str
    displayName: str
    logoUrl: str | None
    isGlobal: bool
    enabledPlatforms: list[str]
    config: TenantConfigDTO
    #: True only when CUSTOM branding (logo/theme/welcome message) is being
    #: served -- i.e. the tenant is entitled to `tenancy.whitelabel`
    #: (Professional+) AND has configured some. See `services/branding_service.py`.
    whitelabeled: bool = False


@dataclass(slots=True, frozen=True)
class TenantLoginInfoResponse:
    """Response DTO for tenant login info endpoints."""

    success: bool
    tenant: TenantLoginInfoDTO


@dataclass(slots=True, frozen=True)
class PasskeyOptionsResponse:
    """Response DTO for passkey options endpoints."""

    success: bool
    options: dict[str, Any]


@dataclass(slots=True, frozen=True)
class PasskeyLoginFinishRequest:
    """Request DTO for passkey login finish endpoints."""

    credential: dict[str, Any]


@dataclass(slots=True, frozen=True)
class PasskeyLoginFinishResponse:
    """Response DTO for passkey login finish endpoints."""

    success: bool
    token: str
    user: LoginUserDTO


def _login_user_dto(result: auth_service.LoginResult) -> LoginUserDTO:
    return LoginUserDTO(
        id=result.user_id,
        email=result.email,
        username=result.username,
        avatarUrl=result.avatar_url,
        isSuperAdmin=result.is_super_admin,
        isVendor=result.is_vendor,
        isAnalyticsConsumer=result.is_analytics_consumer,
        linkedPlatforms=result.linked_platforms,
    )


# ---------------------------------------------------------------------------
# Local auth (email/password)
# ---------------------------------------------------------------------------


@auth_bp.route("/register", methods=["POST"])
@validate_request(RegisterRequest)
# NOT @validate_response -- register() inserts into hub_users, which hits
# the crash documented in services/dto_response.py's module docstring.
# jsonify_dto() below is the equivalent-safety workaround.
async def register(data: RegisterRequest) -> tuple[Any, int]:
    """Register."""
    async_dal, dal = _dal()
    try:
        requires_verification, result = await auth_service.register(
            async_dal,
            dal,
            _cfg(),
            email=data.email,
            password=data.password,
            username=data.username,
            captcha_token=data.captcha_token,
            remote_ip=request.remote_addr,
        )
    except ApiError as exc:
        return _err(exc)

    if requires_verification:
        return jsonify_dto(
            VerificationPendingResponse(
                success=True,
                requiresVerification=True,
                message="Please check your email to verify your account",
            ),
            202,
        )
    assert result is not None  # nosec B101 - register() always returns a result when not requiring verification
    response, status = jsonify_dto(
        RegisterSuccessResponse(success=True, token=result.token, user=_login_user_dto(result))
    )
    return set_session_cookie(response, result.token), status


@auth_bp.route("/verify-email", methods=["GET"])
# NOT @validate_response -- same crash class as register() above (this
# route also inserts nothing itself, but auth_service.verify_email()'s own
# update_async + hub_sessions insert_async chain hits the same pattern).
async def verify_email() -> tuple[Any, int]:
    """Verify email."""
    async_dal, dal = _dal()
    token = request.args.get("token", "")
    try:
        result = await auth_service.verify_email(async_dal, dal, _cfg(), token=token)
    except ApiError as exc:
        return _err(exc)
    response, status = jsonify_dto(
        RegisterSuccessResponse(
            success=True,
            message="Email verified successfully",
            token=result.token,
            user=_login_user_dto(result),
        )
    )
    return set_session_cookie(response, result.token), status


@dataclass(slots=True, frozen=True)
class ResendVerificationRequest:
    """Request DTO for resend verification endpoints."""

    email: str


@auth_bp.route("/resend-verification", methods=["POST"])
@validate_request(ResendVerificationRequest)
@validate_response(MessageResponse)
async def resend_verification(
    data: ResendVerificationRequest,
) -> MessageResponse | tuple[dict[str, object], int]:
    """Resend verification."""
    async_dal, dal = _dal()
    try:
        await auth_service.resend_verification(async_dal, dal, email=data.email)
    except ApiError as exc:
        return _err(exc)
    return MessageResponse(success=True, message="Verification email sent")


@auth_bp.route("/login", methods=["POST"])
@validate_request(LoginRequest)
@validate_response(LoginResponse, 200)
@validate_response(VerificationRequiredResponse, 403)
async def login(
    data: LoginRequest,
) -> LoginResponse | tuple[VerificationRequiredResponse, int] | tuple[dict[str, object], int]:
    """Login."""
    async_dal, dal = _dal()
    try:
        result = await auth_service.login(
            async_dal,
            dal,
            _cfg(),
            email=data.email,
            password=data.password,
            tenant_slug=data.tenantSlug or "global",
        )
    except auth_service.RequiresVerificationError:
        return (
            VerificationRequiredResponse(
                success=False,
                requiresVerification=True,
                message="Please verify your email address before logging in",
            ),
            403,
        )
    except ApiError as exc:
        return _err(exc)
    issue_session_cookie(result.token)
    return LoginResponse(success=True, token=result.token, user=_login_user_dto(result))


@auth_bp.route("/admin", methods=["POST"])
@validate_request(AdminLoginRequest)
@validate_response(AdminLoginResponse)
async def admin_login(
    data: AdminLoginRequest,
) -> AdminLoginResponse | tuple[dict[str, object], int]:
    """Admin login."""
    async_dal, dal = _dal()
    try:
        result = await auth_service.admin_login(
            async_dal, dal, _cfg(), username=data.username, password=data.password
        )
    except ApiError as exc:
        return _err(exc)
    issue_session_cookie(result.token)
    return AdminLoginResponse(
        success=True,
        token=result.token,
        user=AdminLoginUserDTO(
            id=result.user_id,
            email=result.email,
            username=result.username,
            avatarUrl=result.avatar_url,
            isAdmin=result.is_super_admin,  # legacy compatibility, matches Node
            isSuperAdmin=result.is_super_admin,
        ),
    )


@auth_bp.route("/temp-password", methods=["POST"])
@validate_request(TempPasswordLoginRequest)
@validate_response(TempPasswordLoginResponse)
async def temp_password_login(
    data: TempPasswordLoginRequest,
) -> TempPasswordLoginResponse | tuple[dict[str, object], int]:
    """Temp password login."""
    async_dal, dal = _dal()
    try:
        result = await auth_service.temp_password_login(
            async_dal, dal, _cfg(), identifier=data.identifier, password=data.password
        )
    except ApiError as exc:
        return _err(exc)
    issue_session_cookie(result.token)
    return TempPasswordLoginResponse(
        success=True, token=result.token, requiresOAuthLink=result.requires_oauth_link
    )


@auth_bp.route("/link-oauth", methods=["POST"])
async def link_oauth_legacy() -> tuple[dict[str, object], int]:
    """Legacy temp-password OAuth-linking flow -- dead in the current session model.

    Node's `linkOAuth()` writes `req.user.platformUserId` into
    `hub_temp_passwords`, a field the unified `createSession()` JWT
    payload (`authController.js` itself, since the unified-auth
    refactor) never sets -- this route has been non-functional in Node
    for as long as the unified session model has existed. Faithfully
    porting "always writes NULL/undefined into a WHERE clause" would
    just hide the same dead code behind a Python traceback instead of a
    JS one; returning a clear 501 is more honest than either. Flagged in
    `hub_api/PORTING.md` -- confirm with product whether this route can
    be retired from the v1 contract entirely.
    """
    return cast(
        tuple[dict[str, object], int],
        error_response(
            "Legacy temp-password OAuth linking is not supported under the unified session model",
            501,
            "NOT_IMPLEMENTED",
        ),
    )


@auth_bp.route("/refresh", methods=["POST"])
@validate_response(RefreshResponse)
async def refresh() -> RefreshResponse | tuple[dict[str, object], int]:
    """Refresh."""
    async_dal, dal = _dal()
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else None
    if not token:
        body = await request.get_json(silent=True) or {}
        token = body.get("token")
    try:
        new_token = await auth_service.refresh_token(async_dal, dal, _cfg(), token=token or "")
    except ApiError as exc:
        return _err(exc)
    issue_session_cookie(new_token)
    return RefreshResponse(success=True, token=new_token)


@auth_bp.route("/logout", methods=["POST"])
@validate_response(SimpleSuccessResponse)
async def logout() -> SimpleSuccessResponse:
    """Logout."""
    async_dal, dal = _dal()
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else None
    await auth_service.logout(async_dal, dal, token=token)
    clear_session_cookie_after_request()
    return SimpleSuccessResponse(success=True)


@auth_bp.route("/me", methods=["GET"])
@validate_response(MeResponse)
async def me() -> MeResponse:
    """Me."""
    async_dal, dal = _dal()
    user_id = get_optional_current_user_id(request)
    result = await auth_service.get_current_user(async_dal, dal, user_id=user_id)
    if result is None:
        return MeResponse(success=True, user=None)
    return MeResponse(
        success=True,
        user=MeUserDTO(
            id=result.id,
            email=result.email,
            username=result.username,
            avatarUrl=result.avatar_url,
            isSuperAdmin=result.is_super_admin,
            isVendor=result.is_vendor,
            isAnalyticsConsumer=result.is_analytics_consumer,
            hasPassword=result.has_password,
            linkedPlatforms=[
                LinkedPlatformDTO(platform=p.platform, username=p.username, avatar=p.avatar)
                for p in result.linked_platforms
            ],
            roles=(["admin", "super_admin"] if result.is_super_admin else [])
            + (["vendor"] if result.is_vendor else []),
            communities=[
                CommunityMembershipDTO(
                    id=c.id, name=c.name, displayName=c.display_name, role=c.role
                )
                for c in result.communities
            ],
        ),
    )


@auth_bp.route("/password", methods=["POST"])
@validate_request(SetPasswordRequest)
@validate_response(SimpleSuccessResponse)
async def set_password(
    data: SetPasswordRequest,
) -> SimpleSuccessResponse | tuple[dict[str, object], int]:
    """Set password."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        await auth_service.set_password(
            async_dal,
            dal,
            user_id=user_id,
            current_password=data.currentPassword,
            new_password=data.newPassword,
        )
    except ApiError as exc:
        return _err(exc)
    return SimpleSuccessResponse(success=True)


@auth_bp.route("/tenant/<slug>", methods=["GET"])
@validate_response(TenantLoginInfoResponse)
async def tenant_login_info(slug: str) -> TenantLoginInfoResponse | tuple[dict[str, object], int]:
    """Tenant login info."""
    async_dal, dal = _dal()
    try:
        info = await auth_service.get_tenant_login_info(async_dal, dal, slug=slug)
    except ApiError as exc:
        return _err(exc)
    # Whitelabel gate (Professional, `tenancy.whitelabel`): a tenant that is
    # not entitled is served DEFAULT branding, never its stored custom
    # branding. Fail-closed -- see `services/branding_service.py`.
    branding, whitelabeled = await branding_service.resolve_login_branding(
        info.slug,
        branding_service.LoginBranding(
            logo_url=info.logo_url, theme=info.theme, welcome_message=info.welcome_message
        ),
    )
    return TenantLoginInfoResponse(
        success=True,
        tenant=TenantLoginInfoDTO(
            slug=info.slug,
            displayName=info.display_name,
            logoUrl=branding.logo_url,
            isGlobal=info.is_global,
            enabledPlatforms=info.enabled_platforms,
            config=TenantConfigDTO(theme=branding.theme, welcomeMessage=branding.welcome_message),
            whitelabeled=whitelabeled,
        ),
    )


# ---------------------------------------------------------------------------
# OAuth login flow (Discord/Twitch/Slack -- see oauth_service.py's docstring
# for the youtube/kick scope reduction)
# ---------------------------------------------------------------------------


@auth_bp.route("/oauth/<platform>", methods=["GET"])
@validate_response(OAuthStartResponse)
async def oauth_start(platform: str) -> OAuthStartResponse | tuple[dict[str, object], int]:
    """Oauth start."""
    async_dal, dal = _dal()
    mode = request.args.get("mode", "login")
    try:
        authorize_url, state = await oauth_service.start_oauth(
            async_dal, dal, platform=platform, mode=mode, callback_base_url=_callback_base_url()
        )
    except ApiError as exc:
        return _err(exc)
    return OAuthStartResponse(success=True, authorizeUrl=authorize_url, state=state)


@auth_bp.route("/oauth/<platform>/callback", methods=["GET"])
async def oauth_callback(platform: str):  # type: ignore[no-untyped-def]
    """Oauth callback.

    SECURITY: does NOT put the session JWT in the redirect URL/query string
    -- query strings leak into proxy/access logs, browser history, and the
    `Referer` header of any outbound request the callback page happens to
    make. Instead mints a short-lived (60s), single-use opaque exchange code
    (`oauth_service.create_oauth_exchange_code`) and redirects with THAT in
    the URL; the frontend immediately redeems it for the real JWT via
    `POST /exchange` (`exchange_oauth_code` below), delivered over the
    response BODY, never the URL. See `hub_api/PORTING.md` Gotcha #8.
    """
    async_dal, dal = _dal()
    code = request.args.get("code")
    state = request.args.get("state")
    oauth_error = request.args.get("error")
    frontend_origin = _cfg().frontend_origin

    if oauth_error:
        return redirect(f"{frontend_origin}/login?error=oauth_denied")
    if not code or not state:
        return redirect(f"{frontend_origin}/login?error=oauth_failed")

    try:
        token = await oauth_service.oauth_callback(
            async_dal,
            dal,
            _cfg(),
            platform=platform,
            code=code,
            state=state,
            callback_base_url=_callback_base_url(),
        )
    except ApiError:
        return redirect(f"{frontend_origin}/login?error=oauth_failed")

    exchange_code = await oauth_service.create_oauth_exchange_code(
        async_dal, dal, token=token, platform=platform
    )
    return redirect(f"{frontend_origin}/auth/callback?code={exchange_code}")


@auth_bp.route("/exchange", methods=["POST"])
@validate_request(ExchangeCodeRequest)
@validate_response(ExchangeCodeResponse)
async def exchange_oauth_code(
    data: ExchangeCodeRequest,
) -> ExchangeCodeResponse | tuple[dict[str, object], int]:
    """Exchange a short-lived, single-use OAuth callback code for the session JWT.

    Completes the exchange-code handoff `oauth_callback` starts -- see that
    route's docstring. No `tenant_middleware`/`require_scope`: like `login`/
    `refresh`, there is no session yet at this point -- only the opaque code
    the callback redirect just handed the frontend via the URL.
    """
    async_dal, dal = _dal()
    try:
        token = await oauth_service.redeem_oauth_exchange_code(async_dal, dal, code=data.code)
    except ApiError as exc:
        return _err(exc)
    issue_session_cookie(token)
    return ExchangeCodeResponse(success=True, token=token)


@auth_bp.route("/oauth/<platform>/link", methods=["GET"])
@validate_response(OAuthStartResponse)
async def oauth_link_start(platform: str) -> OAuthStartResponse | tuple[dict[str, object], int]:
    """Oauth link start."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        authorize_url, state = await oauth_service.start_link(
            async_dal,
            dal,
            platform=platform,
            user_id=user_id,
            callback_base_url=_callback_base_url(),
        )
    except ApiError as exc:
        return _err(exc)
    return OAuthStartResponse(success=True, authorizeUrl=authorize_url, state=state)


@auth_bp.route("/oauth/<platform>/link-callback", methods=["GET"])
async def oauth_link_callback(platform: str):  # type: ignore[no-untyped-def]
    """Oauth link callback."""
    async_dal, dal = _dal()
    code = request.args.get("code")
    state = request.args.get("state")
    oauth_error = request.args.get("error")
    frontend_origin = _cfg().frontend_origin

    if oauth_error:
        return redirect(f"{frontend_origin}/dashboard/settings?error=link_denied")
    try:
        await oauth_service.link_callback(
            async_dal,
            dal,
            platform=platform,
            code=code or "",
            state=state or "",
            callback_base_url=_callback_base_url(),
        )
    except ApiError:
        return redirect(f"{frontend_origin}/dashboard/settings?error=link_failed")
    return redirect(f"{frontend_origin}/dashboard/settings?linked={platform}")


@auth_bp.route("/oauth/<platform>", methods=["DELETE"])
@validate_response(SimpleSuccessResponse)
async def oauth_unlink(platform: str) -> SimpleSuccessResponse | tuple[dict[str, object], int]:
    """Oauth unlink."""
    async_dal, dal = _dal()
    try:
        user_id = get_current_user_id(request)
        await oauth_service.unlink_account(async_dal, dal, user_id=user_id, platform=platform)
    except ApiError as exc:
        return _err(exc)
    return SimpleSuccessResponse(success=True)


# ---------------------------------------------------------------------------
# Passkey login (owned by passkeyController.js, mounted under /auth/passkey/*
# in Node's routes/passkeys.js -- ported here, not blueprints/v1/passkey.py,
# to keep the exact Node route->file mapping the recipe every other group
# copies)
# ---------------------------------------------------------------------------


@auth_bp.route("/passkey/login/start", methods=["POST"])
@validate_response(PasskeyOptionsResponse)
async def passkey_login_start() -> PasskeyOptionsResponse:
    """Passkey login start."""
    options = await passkey_service.start_login()
    return PasskeyOptionsResponse(success=True, options=options)


@auth_bp.route("/passkey/login/finish", methods=["POST"])
@validate_request(PasskeyLoginFinishRequest)
@validate_response(PasskeyLoginFinishResponse)
async def passkey_login_finish(
    data: PasskeyLoginFinishRequest,
) -> PasskeyLoginFinishResponse | tuple[dict[str, object], int]:
    """Passkey login finish."""
    async_dal, dal = _dal()
    try:
        token, user = await passkey_service.finish_login(
            async_dal, dal, _cfg(), credential=data.credential
        )
    except ApiError as exc:
        return _err(exc)
    issue_session_cookie(token)
    return PasskeyLoginFinishResponse(
        success=True,
        token=token,
        user=LoginUserDTO(
            id=user.id,
            email=user.email,
            username=user.username,
            avatarUrl=user.avatar_url,
            isSuperAdmin=user.is_super_admin,
            isVendor=user.is_vendor,
            isAnalyticsConsumer=user.is_analytics_consumer,
        ),
    )


BLUEPRINTS: list[Blueprint] = [auth_bp]
