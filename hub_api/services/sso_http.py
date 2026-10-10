"""SSRF-guarded outbound HTTP for SSO (OIDC discovery, JWKS, token endpoint).

Every URL fetched here is ultimately tenant-admin-supplied (the OIDC issuer /
discovery URL), so an unguarded client would let any tenant admin point hub-api
at `http://169.254.169.254/` or the cluster API. Defences, all fail-closed:

* `https` only. `http` is accepted solely for hosts the *operator* put in
  `SSO_ALLOWED_PRIVATE_HOSTS` (an on-prem Keycloak on a private address) --
  the same allowlist is the only way a private/loopback/link-local address is
  ever reachable. A tenant admin cannot widen it.
* Resolution-aware address check via `services.url_guard.is_private_host`
  (`getaddrinfo`, not a regex), run in a worker thread so DNS never blocks the
  event loop, and run before EVERY request.
* No redirects are followed (a 3xx is a failure): an IdP JSON endpoint that
  redirects is misconfigured, and following it would re-open the SSRF hole.
* Bounded response size and a hard timeout; no credentials in URLs.

`transport` exists for the test suite: it swaps the *socket* (httpx
`MockTransport`) while the real guard, request construction, size limits,
parsing and error handling all still execute.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Final
from urllib.parse import urlparse

import httpx

from services import url_guard
from services.sso_settings import SsoSettings
from services.sso_telemetry import idp_request_duration, sso_span
from services.sso_types import SsoConfigError, SsoIdpUnavailableError

#: Hard ceiling on any IdP response body we are willing to buffer.
MAX_RESPONSE_BYTES: Final = 1_048_576

_OAUTH_ERROR_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


async def check_outbound_url(url: str, settings: SsoSettings) -> None:
    """Raise `SsoConfigError` unless `url` is an acceptable outbound IdP URL.

    Used both at connection-save time (so an admin gets immediate feedback) and
    before every outbound request (so DNS rebinding after save is still caught).
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise SsoConfigError("idp_url_invalid", "IdP URL has no host")
    if parsed.username or parsed.password:
        raise SsoConfigError("idp_url_invalid", "IdP URL must not embed credentials")
    operator_approved = host in settings.allowed_private_hosts
    if parsed.scheme != "https" and not (parsed.scheme == "http" and operator_approved):
        raise SsoConfigError("idp_url_insecure", "IdP URLs must use https")
    if operator_approved:
        return
    blocked = await asyncio.to_thread(url_guard.is_private_host, host)
    if blocked:
        raise SsoConfigError(
            "idp_url_blocked",
            "IdP host resolves to a private, loopback, link-local or unresolvable address",
        )


class SsoHttp:
    """Thin guarded JSON-over-HTTP client for the OIDC flows."""

    def __init__(
        self,
        settings: SsoSettings,
        *,
        protocol: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Bind operator settings, the metric label `protocol`, and an optional test transport."""
        self._settings = settings
        self._protocol = protocol
        self._transport = transport

    async def get_json(self, url: str, *, operation: str) -> Any:
        """GET `url` and return the decoded JSON body."""
        return await self._request("GET", url, operation=operation)

    async def post_form(
        self,
        url: str,
        data: dict[str, str],
        *,
        operation: str,
        basic_auth: tuple[str, str] | None = None,
    ) -> Any:
        """POST `data` form-encoded to `url` (optionally HTTP Basic); return the JSON body."""
        return await self._request("POST", url, operation=operation, data=data, auth=basic_auth)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        data: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> Any:
        await check_outbound_url(url, self._settings)
        started = time.monotonic()
        try:
            with sso_span(
                f"sso.idp.{operation}", **{"sso.protocol": self._protocol, "http.method": method}
            ):
                body = await self._send(method, url, data=data, auth=auth)
        finally:
            idp_request_duration.record(
                time.monotonic() - started,
                {"protocol": self._protocol, "operation": operation},
            )
        try:
            return json.loads(body)
        except ValueError as exc:
            raise SsoIdpUnavailableError(
                "idp_bad_json", f"IdP {operation} response was not valid JSON"
            ) from exc

    async def _send(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None,
        auth: tuple[str, str] | None,
    ) -> bytes:
        try:
            async with (
                httpx.AsyncClient(
                    transport=self._transport,
                    timeout=self._settings.http_timeout_s,
                    follow_redirects=False,
                    headers={"User-Agent": "waddles-hub-api-sso/1", "Accept": "application/json"},
                ) as client,
                client.stream(
                    method, url, data=data, auth=httpx.BasicAuth(*auth) if auth else None
                ) as response,
            ):
                if response.status_code != 200:
                    raise SsoIdpUnavailableError("idp_http_status", await _status_message(response))
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise SsoIdpUnavailableError(
                            "idp_response_too_large", "IdP response exceeded the size limit"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as exc:
            # httpx messages can include the full request URL; surface only the type.
            raise SsoIdpUnavailableError(
                "idp_unreachable", f"IdP request failed ({type(exc).__name__})"
            ) from exc


async def _status_message(response: httpx.Response) -> str:
    """Build the failure message for a non-200: status plus a whitelisted OAuth `error` code."""
    message = f"IdP returned HTTP {response.status_code}"
    try:
        body = await response.aread()
        parsed = json.loads(body[:4096])
    except (ValueError, httpx.HTTPError):
        return message
    if isinstance(parsed, dict):
        code = parsed.get("error")
        if isinstance(code, str) and _OAUTH_ERROR_RE.match(code):
            return f"{message} (oauth error={code})"
    return message
