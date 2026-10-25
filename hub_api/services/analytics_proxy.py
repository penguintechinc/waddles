"""Async proxy client to the `analytics-core` module (port of `analyticsService.js`).

`analyticsController.js` holds no business logic of its own -- its own
module docstring says so plainly ("Proxies to analytics-core module. Hub
is the auth boundary. Analytics-core returns aggregate data only."). Every
handler forwards a GET request to the standalone `analytics-core` service
(`core/analytics_core_module`, port 8040) via `analyticsService.js`'s thin
axios wrapper, then relays (or lightly reshapes -- see
`services/analytics_service.py::platform_overview`) its JSON response
back. This module is the 1:1 async port of `admin/hub_module/backend/src/
services/analyticsService.js`, following the same shape as
`services/event_calendar_proxy.py`'s `EventCalendarProxyClient` (the
Event module's own proxy-to-a-standalone-service port).

Unlike `EventCalendarProxyClient` (one opaque `X-User-Context` JSON blob),
`analytics-core` expects two flat headers per its own `blueprints/
user_bp.py` module docstring ("Expects X-Caller-User-ID, X-Caller-Role,
X-Service-Key headers from hub"), matching `analyticsService.js::
userHeaders`. Only user-scoped calls carry them -- platform-wide calls
(`getPlatformSummary()` etc.) never do, per `platform_bp.py`'s own module
docstring ("Requires X-Service-Key header ... No PII in responses").

Because there is no owned data model here (the response shape is
whatever `analytics-core` returns, not a hub-api ORM row),
`ProxyResult.body` is intentionally `Any` -- same rationale as
`event_calendar_proxy.py`'s own module docstring: security.md's Output
Validation rule guards against over-serializing an OWNED row, not an
opaque reverse-proxy body that was never hub-api's row to begin with.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Mirrors `config/index.js`'s `analyticsCore` -> `ANALYTICS_CORE_API_URL` env var + default.
_DEFAULT_BASE_URL = "http://analytics-core:8040"
#: Mirrors `analyticsService.js`'s axios client `timeout: 10000` (milliseconds).
_DEFAULT_TIMEOUT_MS = 10000
#: Mirrors `config/index.js`'s `serviceApiKey` -> `SERVICE_API_KEY` env var.
_DEV_ONLY_API_KEY = "dev-service-key-ONLY-FOR-DEVELOPMENT"


@dataclass(slots=True, frozen=True)
class AnalyticsProxyConfig:
    """Connection settings for the downstream `analytics-core` service."""

    base_url: str
    api_key: str
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> AnalyticsProxyConfig:
        """Build from `ANALYTICS_CORE_API_URL` / `SERVICE_API_KEY` / timeout env vars."""
        timeout_ms = int(os.getenv("ANALYTICS_PROXY_TIMEOUT_MS", str(_DEFAULT_TIMEOUT_MS)))
        return cls(
            base_url=os.getenv("ANALYTICS_CORE_API_URL", _DEFAULT_BASE_URL),
            api_key=os.getenv("SERVICE_API_KEY", _DEV_ONLY_API_KEY),
            timeout_seconds=timeout_ms / 1000,
        )


@dataclass(slots=True, frozen=True)
class ProxyResult:
    """Outcome of a proxied call -- either a relayed success or a masked failure.

    `ok=False` covers every downstream non-2xx response AND transport
    failure (timeout/connection error). `transport_error` distinguishes the
    latter (no HTTP response) so `analytics_service._upstream_error` can map
    transport failures, upstream 5xx, and 401/403/other 4xx -> 502 while
    re-wrapping client-meaningful 4xx {400,404,409,422,429} (gh-894).
    """

    ok: bool
    status_code: int
    body: Any
    #: True when no HTTP response was received (connect error / timeout).
    transport_error: bool = False


class AnalyticsCoreProxyClient:
    """Forwards one GET request to `analytics-core`, relaying its JSON body.

    `analyticsService.js` only ever issues GETs -- no write surface to
    port. `X-Caller-User-ID`/`X-Caller-Role` are attached only when the
    caller supplies them (user-scoped endpoints); platform-wide calls omit
    both, matching `analyticsService.js`'s own platform functions (no
    `userHeaders()` call).
    """

    def __init__(self, config: AnalyticsProxyConfig | None = None) -> None:
        """Build the client; `config` defaults to `AnalyticsProxyConfig.from_env()`."""
        self._config = config or AnalyticsProxyConfig.from_env()

    async def get(
        self,
        path: str,
        *,
        query: dict[str, str] | None = None,
        caller_user_id: int | None = None,
        caller_role: str | None = None,
    ) -> ProxyResult:
        """Forward `GET path` to `analytics-core`, relaying its JSON response."""
        url = f"{self._config.base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "X-Service-Key": self._config.api_key,
        }
        if caller_user_id is not None:
            headers["X-Caller-User-ID"] = str(caller_user_id)
            headers["X-Caller-Role"] = caller_role or "user"
        try:
            async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                response = await client.get(url, params=query, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "analytics_proxy_transport_error path=%s error_type=%s",
                path,
                type(exc).__name__,
            )
            # Network failure / timeout -- axios's rejected-promise branch,
            # collapsed the same way errorHandler.js does (see module
            # docstring): no downstream status code to relay.
            return ProxyResult(ok=False, status_code=502, body=None, transport_error=True)

        try:
            data: Any = response.json()
        except ValueError:
            data = None

        if response.is_success:
            return ProxyResult(ok=True, status_code=response.status_code, body=data)
        return ProxyResult(ok=False, status_code=response.status_code, body=data)
