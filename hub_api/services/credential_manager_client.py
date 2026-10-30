"""Client for the credential manager's operator-secret operations (gh-969).

hub-api's DB role is denied `platform_integrations` (H-3) and no SQL function hands it an
operator BOT token, so anything that needs the decrypted bot credential -- today only the
superadmin "test connection" check -- is delegated to the credential manager
(`core/credential_manager_module`), the sanctioned operator-secret holder. hub-api sends the
platform name only and receives a verdict only; it never sees the token.

The credential manager authorizes the call itself (`credentials:admin`): hub-api forwards the
superadmin caller's own bearer token. TRUST BOUNDARY: that bearer is only ever sent to an
APPROVED origin -- `CREDENTIAL_MANAGER_URL` must be an `https://` URL, or an `http://` URL whose
host is an in-cluster service name (single label, `*.svc`, `*.svc.cluster.local`); no userinfo,
query, fragment, or IP/loopback literal, and `CREDENTIAL_MANAGER_ALLOWED_HOSTS` (comma list)
narrows it further when set. Validation runs on every call, the client ignores proxy env
(`trust_env=False`) and never follows redirects, so a mis-set URL cannot exfiltrate the token.
(A service-to-service machine token would be preferable to forwarding the end-user bearer, but
hub-api has no machine-token minting yet -- follow-up.) Failure is LOUD: an
unreachable/erroring/rejecting credential manager (it is not deployed on every environment,
e.g. alpha) is a 503 with an explicit code -- never a silent "valid".
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from services.errors import ApiError, bad_request, not_found, unauthorized

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://credential-manager:8095"
_DEFAULT_TIMEOUT_S = 15.0
_PLATFORM = re.compile(r"[a-z0-9_-]{1,50}")
UNAVAILABLE_CODE = "CREDENTIAL_MANAGER_UNAVAILABLE"


class UntrustedOriginError(ValueError):
    """`CREDENTIAL_MANAGER_URL` is not an approved origin; the bearer is never sent to it."""


_IN_CLUSTER_SUFFIXES = (".svc", ".svc.cluster.local")


def validate_origin(url: str, *, allowed_hosts: frozenset[str] | None = None) -> str:
    """Return `url` (no trailing slash) if it is an approved origin, else raise.

    https is always acceptable; plain http only for in-cluster service names. IP/loopback
    literals, userinfo, query and fragment are rejected outright.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise UntrustedOriginError("credential manager URL must be http(s) with a host")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise UntrustedOriginError("credential manager URL must not carry userinfo/query/fragment")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UntrustedOriginError("credential manager URL must be a DNS name, not an IP literal")
    if host == "localhost" or host.endswith(".localhost"):
        raise UntrustedOriginError("credential manager URL must not be loopback")
    if parts.path not in ("", "/"):
        raise UntrustedOriginError("credential manager URL must be an origin (no path)")
    in_cluster = "." not in host or host.endswith(_IN_CLUSTER_SUFFIXES)
    if parts.scheme == "http" and not in_cluster:
        raise UntrustedOriginError("plain http is only allowed for in-cluster service names")
    if allowed_hosts and host not in allowed_hosts:
        raise UntrustedOriginError("credential manager host is not in the allowed-hosts list")
    return f"{parts.scheme}://{parts.netloc}"


@dataclass(slots=True, frozen=True)
class CredentialManagerConfig:
    """Where the credential manager lives; env-configured, never hardcoded in callers."""

    base_url: str
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> CredentialManagerConfig:
        """Build from `CREDENTIAL_MANAGER_URL` / `CREDENTIAL_MANAGER_TIMEOUT_S`."""
        raw_hosts = os.getenv("CREDENTIAL_MANAGER_ALLOWED_HOSTS", "")
        allowed = frozenset(h.strip().lower() for h in raw_hosts.split(",") if h.strip())
        return cls(
            base_url=validate_origin(
                os.getenv("CREDENTIAL_MANAGER_URL", _DEFAULT_BASE_URL), allowed_hosts=allowed
            ),
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
    try:
        cfg = config or CredentialManagerConfig.from_env()
        validate_origin(cfg.base_url)
    except UntrustedOriginError as exc:
        # The bearer is NOT sent anywhere: a mis-set URL is a loud 503, never a leak.
        logger.error("credential_manager.untrusted_origin reason=%s", exc)
        raise _unavailable("untrusted credential manager origin") from exc
    url = f"{cfg.base_url}/api/v1/credentials/{platform}/test"
    try:
        async with httpx.AsyncClient(
            timeout=cfg.timeout_seconds, trust_env=False, follow_redirects=False
        ) as client:
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
