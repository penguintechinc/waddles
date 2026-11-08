"""Async proxy client to the RTC control plane (port of `callsController.js`'s `axios` calls).

Every `callsController.js` handler is a pure reverse-proxy: forward one
request to `MODULE_RTC_URL` (Node's Go `module_rtc`, LiveKit control
plane), relay its JSON body back reshaped into `{success: true, ...}`.
`docs/plans/2026-08-31-svc-streaming-design.md` §6/§8.1 folds `module_rtc`
into `svc-streaming` (Rust) -- that service's own RTC control-plane
contract (REST vs gRPC, exact port) is still an open decision in that
doc, so `STREAMING_RTC_URL` is a forward-looking env var name (falling
back to `MODULE_RTC_URL` for the cutover window where both may be
deployed) rather than a rename of Node's variable in place. Reshaping
each response into hub-api's own `{success, ...}` envelope stays in
`blueprints/v1/calls.py` (mirrors Node's controller functions
themselves doing the reshaping, not `calendarProxy.js`'s pure passthrough
-- see that blueprint's module docstring), this module only forwards the
request and relays the raw JSON body via `ProxyResult`.

Reuses `services.event_calendar_proxy.ProxyResult` rather than
redefining an identical `(ok, status_code, body)` shape -- same
opaque-relay rationale (`event_calendar_proxy.py`'s own docstring: not a
hub-api-owned row, security.md's Output Validation over-serialization
concern doesn't apply to a body that was never hub-api's row to begin
with).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx
from flask_core.internal_tls import InternalTlsPolicy, PooledClient

from services.errors import ApiError
from services.event_calendar_proxy import ProxyResult

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 10.0


class StreamingUrlNotConfiguredError(RuntimeError):
    """Neither `STREAMING_RTC_URL` nor `MODULE_RTC_URL` is set.

    The svc-streaming address is deployment-specific (the Helm Service name carries
    the release prefix; the port is `pipeline.svcStreaming.port`), so there is no
    in-code default -- a guessed host/port (this module once defaulted to
    `svc-streaming:8093` while the chart serves 8208) silently 502s every call.
    """


@dataclass(slots=True, frozen=True)
class CallsProxyConfig:
    """Connection settings for the downstream RTC control plane."""

    base_url: str
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> CallsProxyConfig:
        """Build from `STREAMING_RTC_URL` (falls back to `MODULE_RTC_URL`) / `_TIMEOUT_SECONDS`.

        Raises `StreamingUrlNotConfiguredError` when neither URL variable is set --
        Helm renders `STREAMING_RTC_URL` into the hub-api Deployment.
        """
        base_url = (
            os.getenv("STREAMING_RTC_URL", "").strip() or os.getenv("MODULE_RTC_URL", "").strip()
        )
        if not base_url:
            raise StreamingUrlNotConfiguredError(
                "STREAMING_RTC_URL is not set (svc-streaming base URL, rendered by the "
                "Helm chart into the hub-api Deployment); refusing to guess a host/port"
            )
        return cls(
            base_url=base_url.rstrip("/"),
            timeout_seconds=float(
                os.getenv("STREAMING_RTC_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS))
            ),
        )


class CallsProxyClient:
    """Forwards one request to the RTC control plane, relaying its JSON body.

    Forwards the CALLER's own bearer token unchanged (`Authorization`
    header) -- matches Node's `headers: {Authorization: req.headers.
    authorization}` exactly. This is unrelated to (and does not replace)
    the community-membership/admin authz `blueprints/v1/calls.py` already
    enforces before ever calling this client (`services.community_access`)
    -- the downstream service still needs to know WHO is calling to mint
    a correctly-scoped LiveKit identity, same as Node's design.
    """

    def __init__(self, config: CallsProxyConfig | None = None) -> None:
        """Build the client; `config` defaults to `CallsProxyConfig.from_env()` on first use.

        Resolution is lazy so importing the blueprint never needs the env; a missing
        URL fails loudly (`StreamingUrlNotConfiguredError`) on the first forwarded call.
        """
        self._explicit_config = config
        # One pooled HTTP/2 client per loop (TLS+CA per INTERNAL_* policy); the timeout is
        # applied per request below so a config change never needs a new pool.
        self._pool = PooledClient()

    @property
    def _config(self) -> CallsProxyConfig:
        """The explicit config, else one freshly read from the environment."""
        return self._explicit_config or CallsProxyConfig.from_env()

    async def request(
        self,
        method: str,
        path: str,
        *,
        authorization: str | None,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> ProxyResult:
        """Forward `method path` to the RTC control plane, query/body passed through as-is."""
        config = self._config
        url = f"{config.base_url}{path}"
        headers = {"Content-Type": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        try:
            InternalTlsPolicy.from_env().check_url(url)
            response = await self._pool.get().request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=config.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.error(
                "svc-streaming call failed: %s %s -> %s: %s",
                method,
                path,
                type(exc).__name__,
                exc,
            )
            raise ApiError(
                message="Streaming service unavailable",
                status_code=502,
                code="STREAMING_UNAVAILABLE",
            ) from exc

        data: Any = None
        if response.content:
            try:
                data = response.json()
            except ValueError as exc:
                logger.error(
                    "svc-streaming returned a non-JSON body: %s %s -> HTTP %s (%s)",
                    method,
                    path,
                    response.status_code,
                    type(exc).__name__,
                )
                raise ApiError(
                    message="Streaming service returned an invalid response",
                    status_code=502,
                    code="STREAMING_BAD_RESPONSE",
                ) from exc

        if response.is_success:
            return ProxyResult(ok=True, status_code=response.status_code, body=data)
        return ProxyResult(ok=False, status_code=response.status_code, body=data)
