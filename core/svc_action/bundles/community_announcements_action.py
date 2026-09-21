"""Community announcements action bundle -- fan-out broadcast to platforms.

Ported from `hub_api/services/community_announcements.py`'s
`broadcast_to_all_platforms` and `_post_to_platform` logic into the App
Bundle SDK's action-stage script contract: `async def <name>(envelope,
config, *, http_client) -> TransportResult` (`runner.py`).

Consumes an enriched announcement envelope (from the process stage),
looks up active community_servers matching the target platforms,
POSTs the announcement to each server's platform endpoint via the
announced `*_ACTION_URL` (from env vars or config), records the result
in `announcement_broadcasts`, and returns a single `TransportResult`.

DB access uses `flask_core.get_bundle_dal()` per docs/APP_BUNDLE_AUTHORING.md
Accessing the database / shared state. The runner binds the DAL at startup
via `set_bundle_dal()`; the DAL itself is `penguin-dal`'s public API
(docs/superpowers/specs/2026-09-14-rust-data-plane-design.md D21a / M1.5),
replacing the legacy DAL wrapper this module used before.

Deliberately does **not** implement retry/sleep loops -- `retry_with_
backoff` in `runner.py::_handle_envelope` owns all backoff timing on
`RetryableTransportError`. Similarly, this bundle records DB audit
results independently; the runner's own `action_dispatch_log` provides
a parallel record of transport-level outcomes.

`community_servers`/`announcement_broadcasts` are never bound anywhere
else on this service's `dal` (svc-action's own startup only binds
`tenants`/`communities`/`app_catalog`/`action_dispatch_log` --
`reference_tables.py`, `flask_core.app_bundle_tables`,
`services/dispatch_log.py`), so this bundle binds its own minimal
stubs (`_ensure_announcement_tables`, idempotent, `migrate=False` --
schema owned by `config/postgres/migrations/000_create_base_schema.sql`),
same "bind only the columns this bundle actually touches" convention
`twitch_shoutout_action.py::_ensure_shoutout_tables` already
establishes. `penguin_dal.db.AsyncDB.define_table()` is async, so
`_ensure_announcement_tables` is awaited from its call site. Query
building keeps the same `dal.table.column == value` shape the legacy
wrapper used, but `dal(query).select()` runs directly against the
`Query` `penguin_dal` returns -- no intermediate Set-conversion step
(gh #298, matching `runner.py::_resolve_tenant_id`'s own migrated call
shape).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from flask_core import StageEnvelope, get_bundle_dal
from penguin_dal import Field
from waddle_transports import NonRetryableTransportError, RetryableTransportError, TransportResult
from waddle_transports.url_guard import SSRFError, guarded_request

logger = logging.getLogger(__name__)


async def _ensure_announcement_tables(dal: Any) -> None:
    """Idempotently bind `community_servers`/`announcement_broadcasts` -- only the columns used.

    Mirrors `twitch_shoutout_action.py::_ensure_shoutout_tables`'s own
    "minimal stub, no DDL" convention -- `migrate=False` throughout,
    schema owned by `000_create_base_schema.sql`. Must run on a `dal`
    that already has `communities` defined (svc-action's own `app.py`
    startup binds it via `bind_minimal_reference_tables` before
    `set_bundle_dal()`).
    """
    if "community_servers" not in dal.tables:
        await dal.define_table(
            "community_servers",
            Field("community_id", "reference communities", notnull=True),
            Field("platform", "string", notnull=True),
            migrate=False,
        )
    if "announcement_broadcasts" not in dal.tables:
        await dal.define_table(
            "announcement_broadcasts",
            Field("announcement_id", "integer", notnull=True),
            Field("community_server_id", "integer"),
            Field("platform", "string", notnull=True),
            Field("status", "string", default="pending"),
            Field("error_message", "string"),
            Field("broadcasted_at", "datetime"),
            Field("created_at", "datetime", default=lambda: datetime.now(UTC)),
            migrate=False,
        )


#: Per-platform action service base URLs, fallback defaults from env vars
_PLATFORM_ENDPOINTS = {
    "discord": os.getenv("DISCORD_ACTION_URL", "http://localhost:8070"),
    "slack": os.getenv("SLACK_ACTION_URL", "http://localhost:8071"),
    "twitch": os.getenv("TWITCH_ACTION_URL", "http://localhost:8072"),
    "youtube": os.getenv("YOUTUBE_ACTION_URL", "http://localhost:8073"),
}

_TIMEOUT_SECONDS = 10.0


async def _post_to_platform(
    http_client: httpx.AsyncClient,
    platform: str,
    announcement_data: dict[str, Any],
) -> tuple[bool, str | None]:
    """POST one announcement to a platform's action service endpoint.

    Returns `(success, error_message)`. On success, returns `(True, None)`.
    On error, returns `(False, error_description)` for logging but does not
    raise -- the caller decides whether to retry or record the failure.
    """
    endpoint = _PLATFORM_ENDPOINTS.get(platform)
    if endpoint is None:
        return False, f"No action endpoint configured for platform {platform!r}"

    url = f"{endpoint}/internal/announce"
    headers = {"Content-Type": "application/json"}

    try:
        response = await guarded_request(
            http_client,
            "POST",
            url,
            headers=headers,
            json=announcement_data,
        )
        success = response.status_code < 400
        error = None if success else f"HTTP {response.status_code}"
        return success, error

    except SSRFError as exc:
        return False, f"SSRF guard rejected URL: {exc}"
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        return False, f"Network error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Unexpected error: {exc}"


async def broadcast_announcement(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Fan out an announcement to every matching community_server per platform.

    Expects `envelope.event.payload` to contain:
    - `announcement`: dict with {id, title, content, announcement_type, status, ...}
    - `target_platforms`: list of platform names (discord, twitch, slack, youtube)
    - `announcement_id`: int, for audit trail

    Raises `NonRetryableTransportError` for a config/DB failure (no platforms,
    no servers, missing announcement data) and `RetryableTransportError` for
    transient network errors (5xx from platform endpoint, timeout). Catches
    `ValueError` from malformed envelope data and wraps as non-retryable.
    """
    event_payload = envelope.event.payload

    announcement_data = event_payload.get("announcement")
    if not isinstance(announcement_data, dict):
        raise NonRetryableTransportError("envelope event.payload missing 'announcement' dict")

    target_platforms = event_payload.get("target_platforms", [])
    if not isinstance(target_platforms, list) or not target_platforms:
        raise NonRetryableTransportError(
            "envelope event.payload missing or empty 'target_platforms' list"
        )

    announcement_id = event_payload.get("announcement_id")
    if not isinstance(announcement_id, int):
        raise NonRetryableTransportError("envelope event.payload missing 'announcement_id' int")

    dal = get_bundle_dal()
    await _ensure_announcement_tables(dal)

    # Look up community_servers matching the target platforms
    community_id = envelope.community
    if not community_id:
        raise NonRetryableTransportError("envelope missing required community_id")

    try:
        query = (
            (dal.community_servers.community_id == int(community_id))
            & (dal.community_servers.platform.belongs(target_platforms))
        )
        servers = await dal(query).select()

        if not servers:
            raise NonRetryableTransportError(
                f"no active servers found for community_id={community_id} "
                f"platforms={target_platforms}"
            )

        # Fan out to each server, collecting results
        results = []
        successful_count = 0

        for server in servers:
            platform = server.platform
            success, error = await _post_to_platform(http_client, platform, announcement_data)

            # Record broadcast attempt in database
            try:
                await dal.announcement_broadcasts.async_insert(
                    announcement_id=announcement_id,
                    community_server_id=server.id,
                    platform=platform,
                    status="sent" if success else "failed",
                    error_message=error,
                    broadcasted_at=datetime.now(UTC),
                    created_at=datetime.now(UTC),
                )
            except Exception as exc:  # noqa: BLE001 -- audit write failure must never fail the broadcast
                # The action_dispatch_log (runner's own audit) still records the outcome.
                logger.warning(
                    "community_announcements_action.broadcast_audit_write_failed "
                    "announcement_id=%s platform=%s error=%s",
                    announcement_id,
                    platform,
                    exc,
                )

            results.append(
                {
                    "platform": platform,
                    "server_id": server.id,
                    "success": success,
                    "error": error,
                }
            )

            if success:
                successful_count += 1

        total_servers = len(results)
        failed_count = total_servers - successful_count

        if failed_count > 0:
            error_details = "; ".join(
                f"{r['platform']}:{r['server_id']} {r['error']}"
                for r in results
                if not r["success"]
            )
            if failed_count == total_servers:
                # All failed -> retryable (transient network issues likely)
                raise RetryableTransportError(
                    f"announcement broadcast to all {total_servers} servers failed: {error_details}"
                )
            else:
                # Some failed, some succeeded -> non-retryable (partial success)
                raise NonRetryableTransportError(
                    f"announcement broadcast partial failure: {successful_count}/{total_servers} "
                    f"servers succeeded. Failures: {error_details}"
                )

        return TransportResult(
            transport="bundle",
            detail=f"announcement broadcast to {successful_count}/{total_servers} servers",
            http_status=200,
        )

    except (NonRetryableTransportError, RetryableTransportError):
        raise
    except ValueError as exc:
        raise NonRetryableTransportError(f"malformed announcement envelope: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise NonRetryableTransportError(f"unexpected error in broadcast: {exc}") from exc
