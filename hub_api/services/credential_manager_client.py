"""Client for the credential manager's operator-secret operations (gh-969).

hub-api's DB role is denied `platform_integrations` (H-3) and no SQL function hands it an
operator BOT token, so anything that needs the decrypted bot credential -- today only the
superadmin "test connection" check -- is delegated to the credential manager
(`core/credential_manager_module`), the sanctioned operator-secret holder. hub-api sends the
platform name only and receives a verdict only; it never sees the token.

The credential manager authorizes the call itself (`credentials:admin`): hub-api forwards the
superadmin caller's own bearer token. Failure is LOUD: an unreachable/erroring/rejecting
credential manager (it is not deployed on every environment, e.g. alpha) is a 503 with an
explicit code -- never a silent "valid".
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import httpx

from services.errors import ApiError, bad_request, not_found, unauthorized

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://credential-manager:8095"
_DEFAULT_TIMEOUT_S = 15.0
_PLATFORM = re.compile(r"[a-z0-9_-]{1,50}")
UNAVAILABLE_CODE = "CREDENTIAL_MANAGER_UNAVAILABLE"


@dataclass(slots=True, frozen=True)
class CredentialManagerConfig:
    """Where the credential manager lives; env-configured, never hardcoded in callers."""

    base_url: str
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> CredentialManagerConfig:
        """Build from `CREDENTIAL_MANAGER_URL` / `CREDENTIAL_MANAGER_TIMEOUT_S`."""
        return cls(
            base_url=os.getenv("CREDENTIAL_MANAGER_URL", _DEFAULT_BASE_URL).rstrip("/"),
            timeout_seconds=float(
                os.getenv("CREDENTIAL_MANAGER_TIMEOUT_S", str(_DEFAULT_TIMEOUT_S))
            ),
        )


def _unavailable(reason: str) -> ApiError:
    return ApiError(f"credential manager unavailable: {reason}", 503, UNAVAILABLE_CODE)


async def test_bot_connection(
    platform: str, *, bearer_token: str | None, config: CredentialManagerConfig | None = None
) -> tuple[bool, str | None]:
    """Ask the credential manager to validate `platform`'s bot credential; `(valid, error)`.

    Raises `not_found` (no credential), `bad_request`, `unauthorized`, or a 503
    `CREDENTIAL_MANAGER_UNAVAILABLE` `ApiError` -- never returns a guess.
    """
    if not _PLATFORM.fullmatch(platform or ""):
        raise bad_request("invalid platform")
    if not bearer_token:
        raise unauthorized("bearer token required")
    cfg = config or CredentialManagerConfig.from_env()
    url = f"{cfg.base_url}/api/v1/credentials/{platform}/test"
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout_seconds) as client:
            resp = await client.post(url, headers={"Authorization": f"Bearer {bearer_token}"})
    except httpx.HTTPError as exc:
        logger.error(
            "credential_manager.unreachable platform=%s type=%s", platform, type(exc).__name__
        )
        raise _unavailable(type(exc).__name__) from exc
    if resp.status_code == 404:
        raise not_found(f"No credentials found for platform: {platform}")
    if resp.status_code == 400:
        raise bad_request("credential manager rejected the platform")
    if resp.status_code != 200:
        logger.error(
            "credential_manager.bad_status platform=%s status=%s", platform, resp.status_code
        )
        raise _unavailable(f"HTTP {resp.status_code}")
    try:
        data = resp.json()["data"]
        return bool(data["valid"]), (str(data["error"]) if data.get("error") else None)
    except (ValueError, KeyError, TypeError) as exc:
        logger.error("credential_manager.bad_body platform=%s", platform)
        raise _unavailable("malformed response") from exc
