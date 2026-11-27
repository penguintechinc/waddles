"""`send_email(...)` adapter for MFA mail -- a thin seam over hub-api's email transport.

hub-api's general SMTP transport (account email verification and friends) is
`services/email_service.py` (`feature/email-verification`, PR #1541). This adapter is the only
place MFA touches email, so wiring is one import. The transport contract it relies on:

* ``async send_email(*, to_email: str, subject: str, text_body: str, kind: str,
  html_body: str | None = None) -> None`` -- raises ``EmailUnavailableError`` (and its
  subclasses ``EmailNotConfiguredError`` / ``SmtpConfigError`` / ``SmtpDeliveryError``) when the
  message cannot be sent;
* ``is_configured() -> bool``.

Until that module is importable, or while it reports unconfigured, :func:`email_available` is
False (the webui shows email MFA as unavailable and enrollment is refused) and
:func:`send_email` raises :func:`email_unavailable`'s 503 -- a loud 503, never a pretend success
that would leave a user waiting for a code that was never sent. A transport-side
``EmailUnavailableError`` maps to the same 503.

Recipient addresses and code values are never logged here.
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Final

from services.errors import ApiError

logger = logging.getLogger(__name__)

#: Module the email-verification work provides (see module docstring for its contract).
TRANSPORT_MODULE: Final = "services.email_service"

EMAIL_UNAVAILABLE_CODE: Final = "EMAIL_MFA_UNAVAILABLE"

#: `kind` labels (the transport's bounded `^[a-z][a-z0-9_]{0,31}$` metric/span label).
KIND_CODE: Final = "mfa_code"
KIND_FACTOR_CHANGE: Final = "mfa_factor_change"
KIND_FAILED_ATTEMPTS: Final = "mfa_failed_attempts"


def email_unavailable() -> ApiError:
    """Uniform 503 (stable code the webui keys on): the email method cannot deliver a code."""
    return ApiError(
        "Email delivery is not configured on this server; use your authenticator app",
        503,
        EMAIL_UNAVAILABLE_CODE,
    )


def _load_transport() -> ModuleType | None:
    """Import the transport module, or None when it is not installed (yet)."""
    try:
        module = importlib.import_module(TRANSPORT_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != TRANSPORT_MODULE:
            raise  # the module exists but one of ITS imports is broken: surface that loudly
        return None
    if not callable(getattr(module, "send_email", None)) or not callable(
        getattr(module, "is_configured", None)
    ):
        logger.error("mfa_email.transport_contract_mismatch module=%s", TRANSPORT_MODULE)
        return None
    return module


def _unavailable_error_type(module: ModuleType) -> type[BaseException] | None:
    """The transport's `EmailUnavailableError` class (None when the module does not export it)."""
    candidate = getattr(module, "EmailUnavailableError", None)
    if isinstance(candidate, type) and issubclass(candidate, BaseException):
        return candidate
    return None


def email_available() -> bool:
    """True when a transport is installed AND reports itself configured (SMTP host etc. set)."""
    module = _load_transport()
    return bool(module is not None and module.is_configured())


async def send_email(*, to: str, subject: str, text: str, kind: str = KIND_CODE) -> None:
    """Deliver one plain-text message through the transport; raise loudly when it cannot.

    Raises:
        ApiError: 503 ``EMAIL_MFA_UNAVAILABLE`` -- no transport installed, it is unconfigured,
            or it raised ``EmailUnavailableError`` (misconfigured / delivery failed).
    """
    module = _load_transport()
    if module is None or not module.is_configured():
        logger.warning("mfa_email.unavailable kind=%s (no configured SMTP transport)", kind)
        raise email_unavailable()
    unavailable_type = _unavailable_error_type(module)
    try:
        await module.send_email(to_email=to, subject=subject, text_body=text, kind=kind)
    except Exception as exc:
        if unavailable_type is not None and isinstance(exc, unavailable_type):
            logger.error(
                "mfa_email.send_failed kind=%s reason=%s",
                kind,
                getattr(exc, "reason", type(exc).__name__),
            )
            raise email_unavailable() from exc
        raise
