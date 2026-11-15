"""Async proxy client to the `calendar-interaction` service (port of `calendarProxy.js`).

Both Event-group Node controllers (`calendarController.js`, `ticketController.js`
-- migration plan §2, Event module = `waddles.event.calendar`) hold no
business logic of their own: every handler forwards the request to the
standalone `calendar-interaction` module (`action/interactive/
calendar_interaction_module`) over HTTP and relays its JSON response back
unchanged. This module is the 1:1 async port of `admin/hub_module/backend/
src/utils/calendarProxy.js` (`proxyToCalendar`/`buildUserContext`) plus
`ticketController.js`'s locally-duplicated copy of the same two helpers --
one client, matching the single downstream contract both Node files share.

Because there is no owned data model here (the response shape is whatever
`calendar-interaction` returns, not a hub-api ORM row), `ProxyResult.body`
is intentionally `Any` rather than a typed DTO -- security.md's Output
Validation rule ("never a raw model or `**dict`") guards against
accidentally over-serializing an owned row; it does not apply to an opaque
reverse-proxy body that was never hub-api's row to begin with. See
`blueprints/v1/event.py`'s module docstring for the full rationale.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

#: `CALENDAR_API_URL` env var. No default: the legacy calendar-interaction service was deleted, so
#: an unset URL fails LOUD with a typed 503 (`service_unavailable`) instead of dialing a dead host.
_DEFAULT_BASE_URL = ""
SERVICE_NAME = "calendar-interaction"
logger = logging.getLogger(__name__)
#: Mirrors `calendarProxy.js`'s `CALENDAR_PROXY_TIMEOUT_MS` (milliseconds).
_DEFAULT_TIMEOUT_MS = 5000
#: Mirrors `config/index.js`'s `serviceApiKey` -> `SERVICE_API_KEY` env var.
_DEV_ONLY_API_KEY = "dev-service-key-ONLY-FOR-DEVELOPMENT"


@dataclass(slots=True, frozen=True)
class EventCalendarProxyConfig:
    """Connection settings for the downstream `calendar-interaction` service."""

    base_url: str
    api_key: str
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> EventCalendarProxyConfig:
        """Build from `CALENDAR_API_URL` / `SERVICE_API_KEY` / `CALENDAR_PROXY_TIMEOUT_MS`."""
        timeout_ms = int(os.getenv("CALENDAR_PROXY_TIMEOUT_MS", str(_DEFAULT_TIMEOUT_MS)))
        return cls(
            base_url=os.getenv("CALENDAR_API_URL", _DEFAULT_BASE_URL),
            api_key=os.getenv("SERVICE_API_KEY", _DEV_ONLY_API_KEY),
            timeout_seconds=timeout_ms / 1000,
        )


@dataclass(slots=True, frozen=True)
class ProxyResult:
    """Outcome of a proxied call -- either a relayed success or a masked failure.

    `ok=False` covers every downstream non-2xx response AND transport
    failure (timeout/connection error) identically -- matching
    `calendarProxy.js::proxyToCalendar`, which `throw`s in both cases and
    lets `errorHandler.js` collapse them to the same envelope.
    """

    ok: bool
    status_code: int
    body: Any


@dataclass(slots=True, frozen=True)
class UserContext:
    """Caller identity forwarded downstream as `X-User-Context` (port of `buildUserContext`)."""

    user_id: str | None
    username: str | None
    role: str

    def to_header_json(self) -> str:
        """Serialize as `buildUserContext` does -- `platform`/`platform_user_id` always set."""
        return json.dumps(
            {
                "user_id": self.user_id,
                "username": self.username,
                "platform": "hub",
                "platform_user_id": str(self.user_id) if self.user_id else "anonymous",
                "role": self.role,
            }
        )


class EventCalendarProxyClient:
    """Forwards one request to `calendar-interaction`, relaying its JSON body.

    Reimplements `proxyToCalendar`'s abort-after-timeout behavior with
    `httpx.AsyncClient`'s own timeout (no separate `AbortController`
    plumbing needed in Python) and its `X-API-Key` + `X-User-Context`
    header pair.
    """

    def __init__(self, config: EventCalendarProxyConfig | None = None) -> None:
        """Build the client; `config` defaults to `EventCalendarProxyConfig.from_env()`."""
        self._config = config or EventCalendarProxyConfig.from_env()

    async def request(
        self,
        method: str,
        path: str,
        *,
        user_context: UserContext,
        query: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> ProxyResult:
        """Forward `method path` to `calendar-interaction`, query/body passed through as-is."""
        if not self._config.base_url:
            logger.error(
                "event_calendar_proxy_service_unavailable service=%s path=%s", SERVICE_NAME, path
            )
            return ProxyResult(
                ok=False,
                status_code=503,
                body={"error": "service_unavailable", "service": SERVICE_NAME},
            )
        url = f"{self._config.base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self._config.api_key,
            "X-User-Context": user_context.to_header_json(),
        }
        try:
            async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                response = await client.request(
                    method,
                    url,
                    params=query,
                    json=json_body,
                    headers=headers,
                )
        except httpx.HTTPError:
            # Network failure / timeout -- calendarProxy.js's AbortController
            # branch, collapsed the same way errorHandler.js does (see
            # module docstring): no downstream status code to relay.
            return ProxyResult(ok=False, status_code=502, body=None)

        try:
            data: Any = response.json()
        except ValueError:
            data = None

        if response.is_success:
            return ProxyResult(ok=True, status_code=response.status_code, body=data)
        return ProxyResult(ok=False, status_code=response.status_code, body=data)
