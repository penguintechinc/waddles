"""HTTP client for hub-api's Discord event-sync internal endpoints (#643's contract).

`calendar_service.py`'s lifecycle hooks (`create_event`/`approve_event`/
`update_event`/`delete_event`) and `app.py`'s `/sync/enable` + manual-resync
routes are the two callers. Both internal endpoints live in hub_api's
`blueprints/v1/event_discord_sync.py`:

- `POST /api/v1/internal/calendar/events/sync-discord` -- `{"event_id": int,
  "action": "create"|"update"|"cancel"}`. Fail-closed on hub-api's side --
  every Discord-side failure comes back as HTTP 200 with
  `sync_status="sync_error"`, never an HTTP error. Only 404 (unknown
  `event_id`) or 400 (bad body) are this endpoint's OWN errors.
- `POST /api/v1/internal/calendar/guild-pairings/event-sync` -- `{"community_id":
  int, "enabled": bool}`. Toggles `event_sync_enabled` on every guild
  pairing under a community (this PR's own addition, see that blueprint's
  module docstring for why it's community-wide, not per-pairing).

Auth: `X-Service-Key` shared-secret header (`Config.SERVICE_API_KEY`),
matching `core/svc_process/services/reputation_gate_client.py`'s
established calling convention for this exact call shape -- a fresh
`httpx.AsyncClient` per call (infrequent, call-scoped, no long-lived
client to manage through this process's own lifecycle).

Fail-closed, same as `ReputationGateClient`: every method NEVER raises --
connection errors, timeouts, non-2xx responses, and malformed JSON all
degrade to a result with `ok=False` and a logged warning. Callers never
need special-case error handling for "hub-api unreachable" vs "hub-api
rejected the call" vs "Discord rejected the push" (the last one is itself
`ok=True, sync_status="sync_error"` -- a successful round trip that
recorded a Discord-side failure for the reconcile CronJob to retry).
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0
_SYNC_PATH = "/api/v1/internal/calendar/events/sync-discord"
_ENABLE_PATH = "/api/v1/internal/calendar/guild-pairings/event-sync"

#: Mirrors `event_discord_sync.py::_VALID_ACTIONS` on the hub-api side.
VALID_SYNC_ACTIONS: tuple[str, ...] = ("create", "update", "cancel")


@dataclass(slots=True, frozen=True)
class DiscordSyncResult:
    """Outcome of one `POST .../calendar/events/sync-discord` call.

    `ok=False` means the HTTP round trip itself failed (unreachable,
    non-2xx, malformed body) -- `sync_status`/`sync_error` are only
    meaningful when `ok=True`, mirroring hub-api's own `SyncResult`
    (`"pending"` | `"synced"` | `"sync_error"`).
    """

    ok: bool
    event_id: int
    discord_event_id: str | None
    sync_status: str | None
    sync_error: str | None


@dataclass(slots=True, frozen=True)
class SyncEnableResult:
    """Outcome of one `POST .../calendar/guild-pairings/event-sync` call."""

    ok: bool
    community_id: int
    event_sync_enabled: bool | None
    pairings_updated: int
    error: str | None


class EventDiscordSyncClient:
    """Thin HTTP client for hub-api's `/api/v1/internal/calendar/...` Discord sync endpoints."""

    def __init__(self, *, base_url: str | None = None, service_api_key: str | None = None) -> None:
        """Build a client for `base_url`/`service_api_key`, defaulting from env vars."""
        self._base_url = (base_url or os.getenv("HUB_API_URL", "http://hub-api:8204")).rstrip("/")
        self._service_api_key = (
            service_api_key if service_api_key is not None else os.getenv("SERVICE_API_KEY", "")
        )

    async def sync_event(self, event_id: int, action: str) -> DiscordSyncResult:
        """POST `{"event_id", "action"}` to the sync-discord endpoint; never raises."""
        if action not in VALID_SYNC_ACTIONS:
            logger.error(
                "event_discord_sync_client.invalid_action event_id=%s action=%s",
                event_id,
                action,
            )
            return DiscordSyncResult(
                ok=False,
                event_id=event_id,
                discord_event_id=None,
                sync_status=None,
                sync_error=f"invalid action {action!r}",
            )

        body, error = await self._post(
            _SYNC_PATH, {"event_id": event_id, "action": action}, label=f"event_id={event_id}"
        )
        if error is not None:
            return DiscordSyncResult(
                ok=False, event_id=event_id, discord_event_id=None, sync_status=None, sync_error=error
            )

        return DiscordSyncResult(
            ok=True,
            event_id=event_id,
            discord_event_id=body.get("discord_event_id"),
            sync_status=body.get("sync_status"),
            sync_error=body.get("sync_error"),
        )

    async def set_event_sync_enabled(self, community_id: int, enabled: bool) -> SyncEnableResult:
        """POST `{"community_id", "enabled"}` to the guild-pairings toggle endpoint; never raises."""
        body, error = await self._post(
            _ENABLE_PATH,
            {"community_id": community_id, "enabled": enabled},
            label=f"community_id={community_id}",
        )
        if error is not None:
            return SyncEnableResult(
                ok=False,
                community_id=community_id,
                event_sync_enabled=None,
                pairings_updated=0,
                error=error,
            )

        return SyncEnableResult(
            ok=True,
            community_id=community_id,
            event_sync_enabled=body.get("event_sync_enabled"),
            pairings_updated=int(body.get("pairings_updated", 0)),
            error=None,
        )

    async def _post(
        self, path: str, payload: dict[str, Any], *, label: str
    ) -> tuple[dict[str, Any], None] | tuple[dict[str, Any], str]:
        """Shared POST + envelope-unwrap; returns `(data, None)` on success, `({}, error)` on failure."""
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{self._base_url}{path}",
                    json=payload,
                    headers={"X-Service-Key": self._service_api_key},
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "event_discord_sync_client.unreachable path=%s %s error=%s", path, label, exc
            )
            return {}, str(exc)

        if response.status_code >= 400:
            logger.warning(
                "event_discord_sync_client.rejected path=%s %s status=%s",
                path,
                label,
                response.status_code,
            )
            return {}, f"HTTP {response.status_code}"

        try:
            envelope = response.json()
        except ValueError as exc:
            logger.warning(
                "event_discord_sync_client.invalid_response path=%s %s error=%s", path, label, exc
            )
            return {}, str(exc)

        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict):
            logger.warning(
                "event_discord_sync_client.malformed_body path=%s %s body=%s", path, label, envelope
            )
            return {}, "malformed response body"

        return data, None


_lock = threading.Lock()
_client: EventDiscordSyncClient | None = None


def get_event_discord_sync_client() -> EventDiscordSyncClient:
    """Lazily construct (once, process-wide) and return the real HTTP-backed sync client."""
    global _client
    with _lock:
        if _client is None:
            _client = EventDiscordSyncClient()
        return _client


def reset_for_tests() -> None:
    """Clear the cached singleton -- test isolation only, never called by production code."""
    global _client
    _client = None
