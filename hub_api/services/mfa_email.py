"""`send_email(...)` adapter for MFA one-time codes -- a thin seam over hub-api's email transport.

hub-api's general SMTP transport (account email verification and friends) is being built
separately (`feature/email-verification`) as `services/email_service.py`. This adapter is the
only place MFA touches email, so wiring is one import:

* the transport module must expose ``async send_email(*, to: str, subject: str, text: str)``
  and ``is_configured() -> bool``;
* until that module exists, or while it reports unconfigured, :func:`email_available` is False
  (the webui shows email MFA as unavailable and enrollment is refused) and :func:`send_email`
  raises :func:`email_unavailable`'s 503 -- a loud 503, never a pretend success that
  would leave a user waiting for a code that was never sent.

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


def email_available() -> bool:
    """True when a transport is installed AND reports itself configured (SMTP host etc. set)."""
    module = _load_transport()
    return bool(module is not None and module.is_configured())


async def send_email(*, to: str, subject: str, text: str) -> None:
    """Deliver one plain-text message through the transport; raise loudly when there is none.

    Raises:
        ApiError: 503 ``EMAIL_MFA_UNAVAILABLE`` -- no transport installed, or it is unconfigured.
    """
    module = _load_transport()
    if module is None or not module.is_configured():
        logger.warning("mfa_email.unavailable (no configured SMTP transport)")
        raise email_unavailable()
    await module.send_email(to=to, subject=subject, text=text)
