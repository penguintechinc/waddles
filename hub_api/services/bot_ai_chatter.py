"""AIChatter config proxy -- ports `controllers/aiChatterController.js`.

Pure proxy to `ai-interaction`'s `/api/v1/ai/config/chatter`; hub-api
holds no AIChatter state of its own, matching the Node controller's own
docstring ("Proxies AIChatter configuration to ai_interaction_module").
`httpx.AsyncClient` (already a `flask_core` transitive dep, see
`hub_api/requirements.in`'s comment on outbound HTTP) replaces axios;
validation ranges are copied verbatim from the Node `next(errors.
badRequest(...))` checks.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

#: Defaults returned when ai-interaction has no config yet for a community
#: (Node: `err.response?.status === 404` -> hardcoded defaults).
_DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "max_responses_per_window": 10,
    "window_seconds": 600,
    "max_per_user_per_window": 2,
    "response_probability": 0.30,
    "min_message_length": 10,
}


logger = logging.getLogger(__name__)
SERVICE_NAME = "ai-interaction"


class AIChatterServiceUnavailableError(RuntimeError):
    """`AI_INTERACTION_API_URL` is unset: the legacy ai-interaction service no longer exists."""


class AIChatterValidationError(ValueError):
    """A field failed the same range checks `aiChatterController.js` enforces."""


@dataclass(slots=True, frozen=True)
class AIChatterConfigUpdate:
    """Optional-field update -- `None` means "leave unchanged" (Node's `undefined` check)."""

    enabled: bool | None = None
    max_responses_per_window: int | None = None
    window_seconds: int | None = None
    max_per_user_per_window: int | None = None
    response_probability: float | None = None
    min_message_length: int | None = None


def is_configured() -> bool:
    """True when `AI_INTERACTION_API_URL` is set; else log an ERROR (routes return 503)."""
    if os.environ.get("AI_INTERACTION_API_URL", ""):
        return True
    logger.error("ai_chatter_service_unavailable service=%s", SERVICE_NAME)
    return False


def _base_url() -> str:
    """Return the configured upstream URL, or raise loudly (typed 503 at the route) when unset."""
    url = os.environ.get("AI_INTERACTION_API_URL", "")
    if not url:
        logger.error("ai_chatter_service_unavailable service=%s", SERVICE_NAME)
        raise AIChatterServiceUnavailableError(SERVICE_NAME)
    return url


def _service_key_headers() -> dict[str, str]:
    key = os.environ.get("SERVICE_API_KEY", "")
    return {"X-Service-Key": key} if key else {}


def _unwrap_data(body: Any) -> dict[str, Any]:
    """Node: `response.data?.data || response.data` -- ai-interaction wraps in `{data: {...}}`."""
    if isinstance(body, dict):
        nested = body.get("data")
        if isinstance(nested, dict):
            return nested
        return body
    return {}


def _validate(update: AIChatterConfigUpdate) -> None:
    if update.max_responses_per_window is not None and not (
        1 <= update.max_responses_per_window <= 100
    ):
        raise AIChatterValidationError("max_responses_per_window must be 1-100")
    if update.window_seconds is not None and not (60 <= update.window_seconds <= 3600):
        raise AIChatterValidationError("window_seconds must be 60-3600")
    if update.max_per_user_per_window is not None and not (
        1 <= update.max_per_user_per_window <= 20
    ):
        raise AIChatterValidationError("max_per_user_per_window must be 1-20")
    if update.response_probability is not None and not (0.05 <= update.response_probability <= 1.0):
        raise AIChatterValidationError("response_probability must be 0.05-1.0")


async def get_chatter_config(community_id: int) -> dict[str, Any]:
    """`GET .../ai-chatter/config` -- 404 upstream falls back to `_DEFAULT_CONFIG`."""
    async with httpx.AsyncClient(base_url=_base_url(), timeout=8.0) as client:
        response = await client.get(
            "/api/v1/ai/config/chatter",
            params={"community_id": community_id},
            headers=_service_key_headers(),
        )
    if response.status_code == 404:
        return dict(_DEFAULT_CONFIG)
    response.raise_for_status()
    return _unwrap_data(response.json())


async def update_chatter_config(community_id: int, update: AIChatterConfigUpdate) -> dict[str, Any]:
    """`PUT .../ai-chatter/config` -- raises `AIChatterValidationError` on an out-of-range field."""
    _validate(update)
    payload: dict[str, Any] = {"community_id": community_id}
    for field, value in (
        ("enabled", update.enabled),
        ("max_responses_per_window", update.max_responses_per_window),
        ("window_seconds", update.window_seconds),
        ("max_per_user_per_window", update.max_per_user_per_window),
        ("response_probability", update.response_probability),
        ("min_message_length", update.min_message_length),
    ):
        if value is not None:
            payload[field] = value

    async with httpx.AsyncClient(base_url=_base_url(), timeout=8.0) as client:
        response = await client.post(
            "/api/v1/ai/config/chatter", json=payload, headers=_service_key_headers()
        )
    response.raise_for_status()
    return _unwrap_data(response.json())
