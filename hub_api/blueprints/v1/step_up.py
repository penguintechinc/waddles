"""v1 `step_up` group -- passkey re-authentication right before a sensitive admin action.

Mount: `/api/v1/auth/step-up`.

  POST /passkey/start    WebAuthn assertion options for the caller's OWN credentials, user
                         verification required
  POST /passkey/finish   verify the assertion; answer with the caller's access token re-issued
                         with `amr = [hwk|swk, user, mfa]` and `auth_time = now`

The operator signs in normally, calls these two just before a platform-secret rotate / retire, and
uses the returned token (the session cookie is refreshed too). The token is valid for the MFA gate
(`services/mfa_stepup.py`) for five minutes; nothing else about the session changes and no scope is
added. A forged cross-site request cannot produce the authenticator's signature, so no CSRF token
is needed here. See `services/step_up_service.py` for the verification and its replica caveat.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import mfa_stepup, step_up_service
from services.current_user import get_current_user_id
from services.errors import ApiError
from services.session_cookie import issue_session_cookie

logger = logging.getLogger(__name__)

step_up_bp = Blueprint("v1_step_up", __name__, url_prefix="/api/v1/auth/step-up")


@dataclass(slots=True, frozen=True)
class StepUpOptionsResponse:
    """Response DTO: the WebAuthn `PublicKeyCredentialRequestOptions` for the browser."""

    success: bool
    options: dict[str, Any]


@dataclass(slots=True, frozen=True)
class StepUpFinishRequest:
    """Request DTO: the authenticator's assertion (`navigator.credentials.get` result)."""

    credential: dict[str, Any]


@dataclass(slots=True, frozen=True)
class StepUpFinishResponse:
    """Response DTO: the re-issued access token and the window it is good for."""

    success: bool
    token: str
    mfaValidForSeconds: int


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Typed error response for an :class:`ApiError`."""
    return cast(
        tuple[dict[str, object], int],
        error_response(exc.message, exc.status_code, exc.code, exc.details),
    )


@step_up_bp.route("/passkey/start", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_response(StepUpOptionsResponse)
async def passkey_start() -> StepUpOptionsResponse | tuple[dict[str, object], int]:
    """Options for a user-verified assertion over the caller's own passkeys."""
    try:
        user_id = get_current_user_id(request)
        options = await step_up_service.start(
            current_app.config["async_dal"], current_app.config["dal"], user_id=user_id
        )
    except ApiError as exc:
        return _err(exc)
    return StepUpOptionsResponse(success=True, options=options)


@step_up_bp.route("/passkey/finish", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_request(StepUpFinishRequest)
@validate_response(StepUpFinishResponse)
async def passkey_finish(
    data: StepUpFinishRequest,
) -> StepUpFinishResponse | tuple[dict[str, object], int]:
    """Verify the assertion and re-issue the caller's token with fresh step-up claims."""
    claims = mfa_stepup.request_claims(request)
    try:
        user_id = get_current_user_id(request)
        if claims is None:
            return cast(tuple[dict[str, object], int], error_response("Invalid token", 401))
        token = await step_up_service.finish(
            current_app.config["async_dal"],
            current_app.config["dal"],
            current_app.config["HUB_API_CONFIG"],
            user_id=user_id,
            credential=data.credential,
            claims=claims,
        )
    except ApiError as exc:
        return _err(exc)
    issue_session_cookie(token)
    return StepUpFinishResponse(
        success=True, token=token, mfaValidForSeconds=mfa_stepup.MFA_MAX_AGE_SECONDS
    )


BLUEPRINTS: list[Blueprint] = [step_up_bp]
