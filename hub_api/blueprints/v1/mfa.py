"""v1 `mfa` group -- account multi-factor authentication (TOTP preferred, email code, recovery).

Mount: `/api/v1/auth/mfa`. Logic lives in `services/mfa_service.py` (see its docstring for the
security properties); these handlers only translate HTTP <-> service calls.

Signed-in (tenant_middleware, the caller's own account only):

  GET  /status                     methods + states, email availability, recovery codes left
  POST /totp/enroll                new PENDING secret -> otpauth URI (QR rendered client-side)
  POST /totp/confirm               first valid code activates TOTP -> 10 recovery codes (once)
  POST /email/enroll               verified address required -> emails a code
  POST /email/confirm              code activates the email method (-> recovery codes if none)
  POST /email/send-code            email a code for disable / regenerate
  POST /disable                    re-auth (password or fresh sign-in) + valid code
  POST /recovery-codes/regenerate  re-auth + valid code -> 10 new codes (once)

Pre-auth (the opaque MFA-pending token from a 202 `/auth/login` answer; no JWT exists yet):

  POST /login/email                email a sign-in code for the pending login
  POST /login/verify               TOTP / email / recovery code -> the real session (cookies)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.auth import DEFAULT_TENANT_SLUG
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from blueprints.v1.auth import LoginResponse, _login_user_dto
from config import HubAPIConfig
from services import auth_service, mfa_service, mfa_stepup
from services.current_user import get_current_user_id
from services.errors import ApiError
from services.session_cookie import issue_session_cookie

logger = logging.getLogger(__name__)

mfa_bp = Blueprint("v1_mfa", __name__, url_prefix="/api/v1/auth/mfa")


@dataclass(slots=True, frozen=True)
class MfaMethodState:
    """One method's state: `status` 0 none / 1 pending / 2 active; `available` = usable here."""

    status: int
    available: bool


@dataclass(slots=True, frozen=True)
class MfaStatusResponse:
    """Response DTO for `GET /status` (no secret material)."""

    success: bool
    enabled: bool
    totp: MfaMethodState
    email: MfaMethodState
    emailVerified: bool
    hasPassword: bool
    recoveryCodesRemaining: int
    preferredMethod: str | None


@dataclass(slots=True, frozen=True)
class TotpEnrollResponse:
    """Response DTO for `POST /totp/enroll` -- returned only to the enrolling user."""

    success: bool
    otpauthUri: str
    secret: str


@dataclass(slots=True, frozen=True)
class CodeRequest:
    """Request DTO carrying one verification code."""

    code: str


@dataclass(slots=True, frozen=True)
class RecoveryCodesResponse:
    """Response DTO carrying freshly issued recovery codes (shown once; may be empty)."""

    success: bool
    recoveryCodes: list[str]


@dataclass(slots=True, frozen=True)
class SimpleResponse:
    """Response DTO for actions with no payload."""

    success: bool


@dataclass(slots=True, frozen=True)
class EmailSentResponse:
    """Response DTO after an email code was handed to the transport."""

    success: bool
    expiresIn: int


@dataclass(slots=True, frozen=True)
class DisableRequest:
    """Request DTO for `POST /disable`: which method, re-auth password, and a valid code."""

    method: str
    code: str
    password: str | None = None


@dataclass(slots=True, frozen=True)
class ReauthCodeRequest:
    """Request DTO for `POST /recovery-codes/regenerate`."""

    code: str
    password: str | None = None


@dataclass(slots=True, frozen=True)
class LoginEmailRequest:
    """Request DTO for `POST /login/email`."""

    mfaToken: str


@dataclass(slots=True, frozen=True)
class LoginVerifyRequest:
    """Request DTO for `POST /login/verify`."""

    mfaToken: str
    method: str
    code: str


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Typed error response for an :class:`ApiError`."""
    return cast(
        tuple[dict[str, object], int],
        error_response(exc.message, exc.status_code, exc.code, exc.details),
    )


def _dal() -> tuple[Any, Any]:
    """`(async_dal, dal)` from app config."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _envelope() -> Any | None:
    """The envelope runtime (TOTP secrets are sealed with it); None in a harness without one."""
    return current_app.config.get("envelope_runtime")


def _tenant_slug() -> str:
    """The caller's tenant (set by `tenant_middleware`)."""
    ctx = get_tenant_context(request)
    return str(ctx.tenant_slug) if ctx is not None else DEFAULT_TENANT_SLUG


@mfa_bp.route("/status", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(MfaStatusResponse)
async def mfa_status() -> MfaStatusResponse | tuple[dict[str, object], int]:
    """The caller's MFA methods and their states."""
    async_dal, dal = _dal()
    try:
        state = await mfa_service.get_status(
            async_dal, dal, user_id=get_current_user_id(request), tenant_slug=_tenant_slug()
        )
    except ApiError as exc:
        return _err(exc)
    return MfaStatusResponse(
        success=True,
        enabled=state.mechanism_enabled,
        totp=MfaMethodState(status=state.totp_status, available=state.mechanism_enabled),
        email=MfaMethodState(status=state.email_status, available=state.email_available),
        emailVerified=state.email_verified,
        hasPassword=state.has_password,
        recoveryCodesRemaining=state.recovery_codes_remaining,
        preferredMethod=state.preferred_method,
    )


@mfa_bp.route("/totp/enroll", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_response(TotpEnrollResponse)
async def totp_enroll() -> TotpEnrollResponse | tuple[dict[str, object], int]:
    """Start (or restart) TOTP enrollment; the secret stays PENDING until confirmed."""
    async_dal, dal = _dal()
    try:
        enrollment = await mfa_service.start_totp_enrollment(
            async_dal,
            dal,
            _envelope(),
            user_id=get_current_user_id(request),
            tenant_slug=_tenant_slug(),
        )
    except ApiError as exc:
        logger.info("mfa totp enroll refused (%s)", exc.code)
        return _err(exc)
    return TotpEnrollResponse(
        success=True, otpauthUri=enrollment.otpauth_uri, secret=enrollment.secret
    )


@mfa_bp.route("/totp/confirm", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_request(CodeRequest)
@validate_response(RecoveryCodesResponse)
async def totp_confirm(data: CodeRequest) -> RecoveryCodesResponse | tuple[dict[str, object], int]:
    """Activate TOTP with a first valid code; answers the recovery codes (shown once)."""
    async_dal, dal = _dal()
    try:
        codes = await mfa_service.confirm_totp_enrollment(
            async_dal,
            dal,
            _envelope(),
            user_id=get_current_user_id(request),
            code=data.code,
            tenant_slug=_tenant_slug(),
        )
    except ApiError as exc:
        logger.info("mfa totp confirm refused (%s)", exc.code)
        return _err(exc)
    return RecoveryCodesResponse(success=True, recoveryCodes=codes)


@mfa_bp.route("/email/enroll", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_response(EmailSentResponse)
async def email_enroll() -> EmailSentResponse | tuple[dict[str, object], int]:
    """Start email-code enrollment (verified address required); emails a confirmation code."""
    async_dal, dal = _dal()
    try:
        await mfa_service.start_email_enrollment(
            async_dal, dal, user_id=get_current_user_id(request), tenant_slug=_tenant_slug()
        )
    except ApiError as exc:
        logger.info("mfa email enroll refused (%s)", exc.code)
        return _err(exc)
    return EmailSentResponse(success=True, expiresIn=mfa_service.EMAIL_CODE_TTL_SECONDS)


@mfa_bp.route("/email/confirm", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_request(CodeRequest)
@validate_response(RecoveryCodesResponse)
async def email_confirm(data: CodeRequest) -> RecoveryCodesResponse | tuple[dict[str, object], int]:
    """Activate the email method with the emailed code."""
    async_dal, dal = _dal()
    try:
        codes = await mfa_service.confirm_email_enrollment(
            async_dal,
            dal,
            user_id=get_current_user_id(request),
            code=data.code,
            tenant_slug=_tenant_slug(),
        )
    except ApiError as exc:
        logger.info("mfa email confirm refused (%s)", exc.code)
        return _err(exc)
    return RecoveryCodesResponse(success=True, recoveryCodes=codes)


@mfa_bp.route("/email/send-code", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_response(EmailSentResponse)
async def email_send_code() -> EmailSentResponse | tuple[dict[str, object], int]:
    """Email a code the signed-in user can use to disable a method or regenerate codes."""
    async_dal, dal = _dal()
    try:
        await mfa_service.send_account_code(
            async_dal, dal, user_id=get_current_user_id(request), tenant_slug=_tenant_slug()
        )
    except ApiError as exc:
        logger.info("mfa email send-code refused (%s)", exc.code)
        return _err(exc)
    return EmailSentResponse(success=True, expiresIn=mfa_service.EMAIL_CODE_TTL_SECONDS)


@mfa_bp.route("/disable", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_request(DisableRequest)
@validate_response(SimpleResponse)
async def mfa_disable(data: DisableRequest) -> SimpleResponse | tuple[dict[str, object], int]:
    """Disable one method: re-authentication plus a valid code from an active factor."""
    async_dal, dal = _dal()
    try:
        await mfa_service.disable_method(
            async_dal,
            dal,
            _envelope(),
            user_id=get_current_user_id(request),
            method=data.method,
            password=data.password,
            code=data.code,
            claims=mfa_stepup.request_claims(request),
            tenant_slug=_tenant_slug(),
        )
    except ApiError as exc:
        logger.info("mfa disable refused (%s)", exc.code)
        return _err(exc)
    return SimpleResponse(success=True)


@mfa_bp.route("/recovery-codes/regenerate", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_request(ReauthCodeRequest)
@validate_response(RecoveryCodesResponse)
async def regenerate_recovery_codes(
    data: ReauthCodeRequest,
) -> RecoveryCodesResponse | tuple[dict[str, object], int]:
    """Replace all recovery codes (re-auth + a valid code); answers the new ones once."""
    async_dal, dal = _dal()
    try:
        codes = await mfa_service.regenerate_recovery_codes(
            async_dal,
            dal,
            _envelope(),
            user_id=get_current_user_id(request),
            password=data.password,
            code=data.code,
            claims=mfa_stepup.request_claims(request),
            tenant_slug=_tenant_slug(),
        )
    except ApiError as exc:
        logger.info("mfa recovery regenerate refused (%s)", exc.code)
        return _err(exc)
    return RecoveryCodesResponse(success=True, recoveryCodes=codes)


@mfa_bp.route("/login/email", methods=["POST"])
@validate_request(LoginEmailRequest)
@validate_response(EmailSentResponse)
async def login_email(data: LoginEmailRequest) -> EmailSentResponse | tuple[dict[str, object], int]:
    """Email a sign-in code for a pending (password-verified) login."""
    async_dal, dal = _dal()
    try:
        await mfa_service.send_login_email_code(async_dal, dal, token=data.mfaToken)
    except ApiError as exc:
        logger.info("mfa login email refused (%s)", exc.code)
        return _err(exc)
    return EmailSentResponse(success=True, expiresIn=mfa_service.EMAIL_CODE_TTL_SECONDS)


@mfa_bp.route("/login/verify", methods=["POST"])
@validate_request(LoginVerifyRequest)
@validate_response(LoginResponse)
async def login_verify(data: LoginVerifyRequest) -> LoginResponse | tuple[dict[str, object], int]:
    """Complete a pending login with a second factor; sets the session + refresh cookies."""
    async_dal, dal = _dal()
    cfg = cast(HubAPIConfig, current_app.config["HUB_API_CONFIG"])
    try:
        result = await auth_service.complete_mfa_login(
            async_dal,
            dal,
            cfg,
            _envelope(),
            token=data.mfaToken,
            method=data.method,
            code=data.code,
        )
    except ApiError as exc:
        logger.info("mfa login verify refused (%s)", exc.code)
        return _err(exc)
    issue_session_cookie(result.token)
    return LoginResponse(success=True, token=result.token, user=_login_user_dto(result))


BLUEPRINTS: list[Blueprint] = [mfa_bp]
