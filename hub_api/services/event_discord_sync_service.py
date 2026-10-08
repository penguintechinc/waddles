"""Discord event-sync push engine -- Waddles calendar (SoT) -> Discord guild scheduled events.

**Direction: Waddles pushes OUT, Discord never pushes back.** `calendar_
events` (owned by `action/interactive/calendar_interaction_module/
services/calendar_service.py`) is the single source of truth; this
engine's job is purely to mirror an `approved` event onto every Discord
guild its community has opted into via `guild_tenant_pairings.
event_sync_enabled` (migration 0039_event_sync_enabled -- a SECOND,
independent opt-in from 0034's `sync_enabled`, which gates the unrelated
Bar Citizen role-sync engine in `services/role_sync_service.py`). This
module mirrors that module's own shape closely (same `CredentialResolver`
+ `guild_pairing` + reconcile-CronJob precedent) -- see its docstring for
the design rationale this one inherits wholesale.

**Trigger model.** `sync_event()` is the synchronous entry point the
`calendar_interaction_module`'s own trigger-wiring (a SEPARATE follow-up
PR, not built here) will call via `POST /api/v1/internal/calendar/events/
sync-discord` (`blueprints/v1/event_discord_sync.py`) whenever an event is
created/approved/updated/cancelled. `run_event_sync_reconcile_batch()` is
the periodic CronJob backstop (same "reconciliation-by-polling never
fully replaced by push" philosophy as role-sync) that sweeps any event
left `pending`/`sync_error` -- including ones `sync_event()` itself reset
to `pending` after detecting Discord-side drift (see below).

**SoT state machine, no diffing.** Every create/update call sends
Waddles' FULL current field set to Discord -- never a partial PATCH
against a locally-computed diff. `sync_status` moves `pending ->
synced -> sync_error`. A 404 from Discord while patching/cancelling
(the event was deleted on Discord's side, out of band) resets that
guild's row to `discord_event_id=NULL, sync_status='pending'` --
Waddles wins, no merge, no attempt to resurrect the old Discord id; the
next reconcile pass (or the next webhook-triggered call) recreates it
fresh. This is logged as DRIFT (`waddles_event_sync_drift_total`), not
a sync error -- the event WILL be correct again, just not yet.

**Multi-guild.** A community may pair with N guilds (migration 0034's
`guild_tenant_pairings` is already N:M); this engine pushes to every
pairing with `event_sync_enabled=True`, independently. Per-guild sync
state lives in `calendar_event_discord_syncs` (migration
0039_event_sync_enabled) -- `calendar_events.discord_event_id`/
`sync_status`/`sync_error` are kept as a single-value AGGREGATE (first
successful guild's id; `sync_error` if ANY guild failed) purely for
`EventInfo`'s existing single-Discord-event shape; the per-guild table
is the actual source of truth for multi-guild state.

**Generalization hook (#6).** `PlatformEventTarget` is the seam a future
Twitch (`twitch_segment_id`) or YouTube (`youtube_broadcast_id`) push
target would implement -- `calendar_events` already carries those
columns per `EventInfo`, unused by this PR. `DiscordScheduledEventTarget`
is the only registered target today; `sync_event()`'s dispatch loop
iterates whatever `targets` it's given, never hardcoding "discord" except
in the one block that maps a target's result back onto `calendar_events`'
own (today Discord-shaped) three columns -- a second platform landing
will need that aggregation block generalized too, deliberately deferred
(mirrors role-sync's own "scope landed this PR" discipline).

**Fail-closed, every layer.** `sync_event()` never raises -- any
unexpected failure, at the target level or above it, is caught, logged,
counted, and returned as `SyncResult(sync_status="sync_error")`. A
failure syncing one guild never blocks another guild for the same event,
and a failure syncing one event never blocks another event in a reconcile
batch.

**No raw PII in logs.** Every log line carries event/community/guild/
pairing ids and counts only -- never a Discord username or event title.

RBAC: `calendar_event_discord_syncs` is hub-api-owned (sole writer);
`calendar_events` itself is the legacy `calendar_interaction_module`
table (not owned by this migration) -- see `services/schema.py::
bind_calendar_sync_tables()`'s own docstring.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import httpx
from pydal import Field

from services.bundle_telemetry import bundle_span, get_meter
from services.credential_resolver import (
    CredentialResolver,
    DefaultCredentialResolver,
    TransportUnavailable,
)
from services.guild_pairing import list_event_sync_enabled_pairings
from services.schema import bind_auth_tables, bind_calendar_sync_tables

try:
    from flask_core.feature_flags import feature_enabled
except ImportError:  # pragma: no cover -- exercised only outside the real flask_core install
    feature_enabled = None

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
_REQUEST_TIMEOUT_SECONDS = 10.0

#: Discord's own field-length limits (Guild Scheduled Event resource).
_MAX_NAME_LEN = 100
_MAX_DESCRIPTION_LEN = 1000
_MAX_LOCATION_LEN = 100

#: `entity_type`/`privacy_level`/`status` enum values -- Discord API v10.
_ENTITY_TYPE_EXTERNAL = 3
_PRIVACY_LEVEL_GUILD_ONLY = 2
_STATUS_CANCELED = 4

#: Reconcile-batch inter-event backoff after a failure (Discord scheduled-event
#: write limits are stricter than regular message rate limits) -- doubles per
#: consecutive failure, capped.
_BACKOFF_BASE_S = 2.0
_BACKOFF_MAX_S = 60.0
_DEFAULT_RECONCILE_LIMIT = 100

#: PostHog flag gating this entire engine -- defaulted OFF until validated (critical-rules.md).
FEATURE_EVENT_DISCORD_SYNC = "waddles.calendar.event_discord_sync"

_meter = get_meter()
_events_synced_counter = _meter.create_counter(
    "waddles_event_sync_synced_total", description="Calendar events successfully pushed to Discord"
)
_events_errors_counter = _meter.create_counter(
    "waddles_event_sync_errors_total",
    description="Calendar event Discord-push failures, fail-closed",
)
_events_drift_counter = _meter.create_counter(
    "waddles_event_sync_drift_total",
    description="Discord-side 404s during patch/cancel -- event reset to pending for recreation",
)


class DiscordEventSyncError(Exception):
    """Raised for any non-404 Discord REST failure while creating/patching/cancelling an event."""


class DiscordEventNotFoundError(Exception):
    """Raised on a 404 from Discord -- the event was deleted on Discord's side, out of band."""


# ---------------------------------------------------------------------------
# Discord REST client
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class DiscordEventPayload:
    """The FULL Discord Guild Scheduled Event field set -- sent on every create/patch, never a diff."""  # noqa: E501

    name: str
    description: str | None
    scheduled_start_time: str
    scheduled_end_time: str
    location: str

    def as_json(self) -> dict[str, Any]:
        """Render the Discord API v10 request body (`entity_type=EXTERNAL` always)."""
        body: dict[str, Any] = {
            "name": self.name,
            "scheduled_start_time": self.scheduled_start_time,
            "scheduled_end_time": self.scheduled_end_time,
            "privacy_level": _PRIVACY_LEVEL_GUILD_ONLY,
            "entity_type": _ENTITY_TYPE_EXTERNAL,
            "entity_metadata": {"location": self.location},
        }
        if self.description:
            body["description"] = self.description
        return body


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def build_discord_payload(event_row: Any) -> DiscordEventPayload:
    """Map one `calendar_events` row to the FULL Discord payload -- never a partial diff.

    Defaults a missing `end_date` to `event_date` + 1h and a missing
    `location` to `"Online"` -- Discord requires both non-empty for an
    `EXTERNAL` entity type, logged at DEBUG since neither is an error,
    just a Waddles event that didn't set an optional field Discord
    happens to require.
    """
    from datetime import timedelta

    name = str(event_row.title or "")[:_MAX_NAME_LEN]
    description = (
        str(event_row.description)[:_MAX_DESCRIPTION_LEN] if event_row.description else None
    )

    start = event_row.event_date
    end = event_row.end_date
    if end is None:
        logger.debug("event_discord_sync.default_end_date event_id=%s", int(event_row.id))
        end = start + timedelta(hours=1) if isinstance(start, datetime) else start

    location = str(event_row.location or "").strip()[:_MAX_LOCATION_LEN]
    if not location:
        logger.debug("event_discord_sync.default_location event_id=%s", int(event_row.id))
        location = "Online"

    return DiscordEventPayload(
        name=name,
        description=description,
        scheduled_start_time=_iso(start),
        scheduled_end_time=_iso(end),
        location=location,
    )


def _classify_discord_event(response: httpx.Response, *, action: str) -> None:
    """Raise a SPECIFIC error for a non-2xx response; `DiscordEventNotFoundError` for 404."""
    if response.status_code == 404:
        raise DiscordEventNotFoundError(f"discord scheduled event not found (404) during {action}")
    if response.status_code == 401:
        raise DiscordEventSyncError(f"discord bot token didn't work (401) during {action}")
    if response.status_code == 403:
        raise DiscordEventSyncError(f"discord bot lacks permission (403) during {action}")
    if response.status_code == 429:
        raise DiscordEventSyncError(f"discord api rate limited (429) during {action}")
    if response.status_code >= 400:
        raise DiscordEventSyncError(
            f"discord api returned HTTP {response.status_code} during {action}"
        )


class DiscordEventTargetClient(Protocol):
    """Discord-side scheduled-event write surface this engine needs. Real impl below."""

    async def create_scheduled_event(self, *, guild_id: str, payload: DiscordEventPayload) -> str:
        """`POST .../scheduled-events` -- returns the new Discord scheduled-event id."""
        ...

    async def patch_scheduled_event(
        self, *, guild_id: str, discord_event_id: str, payload: DiscordEventPayload
    ) -> None:
        """`PATCH .../scheduled-events/{id}`, FULL field set. Raises `DiscordEventNotFoundError` on 404."""  # noqa: E501
        ...

    async def cancel_scheduled_event(self, *, guild_id: str, discord_event_id: str) -> None:
        """`PATCH .../scheduled-events/{id}` with `status=CANCELED` -- NEVER the DELETE verb.

        Discord's own guidance: deleting a scheduled event removes all
        history/interest data; cancelling preserves it while marking the
        event over. Raises `DiscordEventNotFoundError` on 404.
        """
        ...


class HttpDiscordEventTargetClient:
    """Real Discord REST client for event-sync -- same shape as role_sync_service's own client."""

    def __init__(
        self, http_client: httpx.AsyncClient, *, bot_token: str, api_base: str = DISCORD_API_BASE
    ) -> None:
        """Bind to a shared `httpx.AsyncClient` + this community's tenant's resolved bot token."""
        self._http = http_client
        self._bot_token = bot_token
        self._api_base = api_base

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self._bot_token}"}

    async def create_scheduled_event(self, *, guild_id: str, payload: DiscordEventPayload) -> str:
        """`POST /guilds/{guild_id}/scheduled-events`."""
        try:
            response = await self._http.post(
                f"{self._api_base}/guilds/{guild_id}/scheduled-events",
                headers=self._headers(),
                json=payload.as_json(),
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise DiscordEventSyncError(f"discord scheduled-event create failed: {exc}") from exc
        _classify_discord_event(response, action="create")
        return str(response.json()["id"])

    async def patch_scheduled_event(
        self, *, guild_id: str, discord_event_id: str, payload: DiscordEventPayload
    ) -> None:
        """`PATCH /guilds/{guild_id}/scheduled-events/{discord_event_id}` -- FULL field set."""
        try:
            response = await self._http.patch(
                f"{self._api_base}/guilds/{guild_id}/scheduled-events/{discord_event_id}",
                headers=self._headers(),
                json=payload.as_json(),
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise DiscordEventSyncError(f"discord scheduled-event patch failed: {exc}") from exc
        _classify_discord_event(response, action="patch")

    async def cancel_scheduled_event(self, *, guild_id: str, discord_event_id: str) -> None:
        """`PATCH .../scheduled-events/{discord_event_id}` with `status=CANCELED` -- never DELETE."""  # noqa: E501
        try:
            response = await self._http.patch(
                f"{self._api_base}/guilds/{guild_id}/scheduled-events/{discord_event_id}",
                headers=self._headers(),
                json={"status": _STATUS_CANCELED},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise DiscordEventSyncError(f"discord scheduled-event cancel failed: {exc}") from exc
        _classify_discord_event(response, action="cancel")


# ---------------------------------------------------------------------------
# Tenant/community resolution -- same pattern as role_sync_service.py (reused,
# not imported -- see that module's own docstring on why these stay separate).
# ---------------------------------------------------------------------------


def _bind_reference_tenants(dal: Any) -> None:
    """Idempotently ensure `dal.tenants` exists for a standalone-process (CronJob) DAL."""
    if "tenants" in dal.tables:
        return
    dal.define_table(
        "tenants",
        Field("slug", "string", length=100),
        Field("display_name", "string", length=255),
        Field("logo_url", "text"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
        migrate=False,
    )


def _resolve_tenant_for_community(dal: Any, community_id: int) -> tuple[int, str, bool] | None:
    """`(tenant_id, tenant_slug, is_global)` for `community_id`, `None` if either row is missing."""
    bind_auth_tables(dal)
    _bind_reference_tenants(dal)
    community = dal(dal.communities.id == community_id).select().first()
    if community is None:
        return None
    tenant = dal(dal.tenants.id == community.tenant_id).select().first()
    if tenant is None:
        return None
    return int(tenant.id), str(tenant.slug), bool(tenant.is_global)


async def _flag_enabled(tenant_slug: str) -> bool:
    """`feature_enabled(FEATURE_EVENT_DISCORD_SYNC, tenant=...)`, defaulted OFF."""
    if feature_enabled is None:  # pragma: no cover -- only in a flask_core-less environment
        return False
    return bool(await feature_enabled(FEATURE_EVENT_DISCORD_SYNC, tenant=tenant_slug))


# ---------------------------------------------------------------------------
# Per-guild sync-state row (calendar_event_discord_syncs)
# ---------------------------------------------------------------------------


def _get_or_create_sync_row(
    dal: Any, *, event_id: int, pairing_id: int, discord_guild_id: str
) -> Any:
    t = dal.calendar_event_discord_syncs
    existing = dal((t.event_id == event_id) & (t.pairing_id == pairing_id)).select().first()
    if existing is not None:
        return existing
    now = datetime.now(UTC)
    try:
        row_id = t.insert(
            event_id=event_id,
            pairing_id=pairing_id,
            discord_guild_id=discord_guild_id,
            sync_status="pending",
            created_at=now,
            updated_at=now,
        )
        dal.commit()
    except Exception:
        dal.rollback()
        raise
    row = dal(t.id == row_id).select().first()
    return row


def _update_sync_row(
    dal: Any,
    sync_row_id: int,
    *,
    discord_event_id: str | None = None,
    sync_status: str,
    sync_error: str | None,
    clear_discord_event_id: bool = False,
) -> None:
    t = dal.calendar_event_discord_syncs
    updates: dict[str, Any] = {
        "sync_status": sync_status,
        "sync_error": sync_error,
        "last_sync_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    if clear_discord_event_id:
        updates["discord_event_id"] = None
    elif discord_event_id is not None:
        updates["discord_event_id"] = discord_event_id
    try:
        dal(t.id == sync_row_id).update(**updates)
        dal.commit()
    except Exception:
        dal.rollback()
        raise


def _persist_calendar_event_aggregate(
    dal: Any,
    event_id: int,
    *,
    discord_event_id: str | None,
    sync_status: str,
    sync_error: str | None,
) -> None:
    """Write the single-value aggregate back onto `calendar_events` -- see module docstring."""
    t = dal.calendar_events
    try:
        dal(t.id == event_id).update(
            discord_event_id=discord_event_id,
            sync_status=sync_status,
            sync_error=sync_error,
            last_sync_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        dal.commit()
    except Exception:
        dal.rollback()
        raise


# ---------------------------------------------------------------------------
# Generalization hook (#6) -- PlatformEventTarget protocol
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PlatformSyncResult:
    """One platform target's outcome for one event -- the orchestrator's unit of aggregation."""

    platform: str
    sync_status: str  # "pending" | "synced" | "sync_error"
    external_id: str | None
    sync_error: str | None
    targets_synced: int
    targets_failed: int


class PlatformEventTarget(Protocol):
    """One platform's event-push capability -- the seam a Twitch/YouTube target implements later.

    `calendar_events.twitch_segment_id`/`youtube_broadcast_id` already
    exist (`EventInfo`) for exactly this -- unused until a target for
    them is registered. Intentionally NOT built here, see module docstring.
    """

    platform: str

    async def push(
        self, dal: Any, event_row: Any, *, action: Literal["create", "update", "cancel"]
    ) -> PlatformSyncResult:
        """Push `event_row` to this platform; fail-closed (never raises -- caught by `sync_event`)."""  # noqa: E501
        ...


class DiscordScheduledEventTarget:
    """The Discord `PlatformEventTarget` -- fans out to every `event_sync_enabled` guild pairing."""

    platform = "discord"

    def __init__(
        self,
        *,
        credential_resolver: CredentialResolver,
        make_client: Any,
    ) -> None:
        """`make_client(bot_token) -> DiscordEventTargetClient` -- injected for tests."""
        self._credential_resolver = credential_resolver
        self._make_client = make_client

    async def push(
        self, dal: Any, event_row: Any, *, action: Literal["create", "update", "cancel"]
    ) -> PlatformSyncResult:
        """See `PlatformEventTarget.push`. Raises on setup failure (caught by `sync_event`)."""
        event_id = int(event_row.id)
        community_id = int(event_row.community_id)
        bind_calendar_sync_tables(dal)

        pairings = list_event_sync_enabled_pairings(dal, community_id)
        if not pairings:
            return PlatformSyncResult("discord", "pending", None, None, 0, 0)

        tenant = _resolve_tenant_for_community(dal, community_id)
        if tenant is None:
            raise TransportUnavailable(f"community {community_id} has no resolvable tenant")
        tenant_id, _tenant_slug, is_global = tenant

        creds = await self._credential_resolver.resolve(
            dal, tenant_id=tenant_id, is_global_tenant=is_global, platform="discord"
        )
        bot_token = str(creds.payload.get("bot_token", ""))
        if not bot_token:
            raise TransportUnavailable(
                f"tenant {tenant_id} has no discord bot token for event-sync"
            )

        client = self._make_client(bot_token)
        payload = build_discord_payload(event_row)

        synced = 0
        failed = 0
        first_success_id: str | None = None
        first_error: str | None = None

        for pairing in pairings:
            sync_row = _get_or_create_sync_row(
                dal,
                event_id=event_id,
                pairing_id=pairing.id,
                discord_guild_id=pairing.discord_guild_id,
            )
            try:
                discord_event_id: str | None = None
                if action == "cancel":
                    if not sync_row.discord_event_id:
                        continue  # nothing to cancel on this guild -- not a failure
                    try:
                        await client.cancel_scheduled_event(
                            guild_id=pairing.discord_guild_id,
                            discord_event_id=sync_row.discord_event_id,
                        )
                        discord_event_id = sync_row.discord_event_id
                        _update_sync_row(
                            dal,
                            sync_row.id,
                            sync_status="synced",
                            sync_error=None,
                            discord_event_id=discord_event_id,
                        )
                    except DiscordEventNotFoundError:
                        self._handle_drift(dal, sync_row, pairing, event_id)
                        continue
                elif action == "create" or not sync_row.discord_event_id:
                    discord_event_id = await client.create_scheduled_event(
                        guild_id=pairing.discord_guild_id, payload=payload
                    )
                    _update_sync_row(
                        dal,
                        sync_row.id,
                        sync_status="synced",
                        sync_error=None,
                        discord_event_id=discord_event_id,
                    )
                else:
                    try:
                        await client.patch_scheduled_event(
                            guild_id=pairing.discord_guild_id,
                            discord_event_id=sync_row.discord_event_id,
                            payload=payload,
                        )
                        discord_event_id = sync_row.discord_event_id
                        _update_sync_row(
                            dal,
                            sync_row.id,
                            sync_status="synced",
                            sync_error=None,
                            discord_event_id=discord_event_id,
                        )
                    except DiscordEventNotFoundError:
                        self._handle_drift(dal, sync_row, pairing, event_id)
                        continue

                synced += 1
                if first_success_id is None:
                    first_success_id = discord_event_id

            except (TransportUnavailable, DiscordEventSyncError) as exc:
                failed += 1
                if first_error is None:
                    first_error = str(exc)
                _update_sync_row(dal, sync_row.id, sync_status="sync_error", sync_error=str(exc))
                logger.error(
                    "event_discord_sync.pairing_failed event_id=%s pairing_id=%s error_type=%s",
                    event_id,
                    pairing.id,
                    type(exc).__name__,
                )
            except Exception as exc:  # noqa: BLE001 - fail-closed: one guild's bug never blocks another
                failed += 1
                if first_error is None:
                    first_error = "unexpected_error"
                _update_sync_row(
                    dal, sync_row.id, sync_status="sync_error", sync_error="unexpected_error"
                )
                logger.error(
                    "event_discord_sync.pairing_failed_unexpected event_id=%s pairing_id=%s error_type=%s",  # noqa: E501
                    event_id,
                    pairing.id,
                    type(exc).__name__,
                )

        if failed > 0:
            status = "sync_error"
        elif synced > 0:
            status = "synced"
        else:
            status = "pending"  # every pairing was a no-op (cancel-nothing) or hit drift

        return PlatformSyncResult("discord", status, first_success_id, first_error, synced, failed)

    def _handle_drift(self, dal: Any, sync_row: Any, pairing: Any, event_id: int) -> None:
        """404 on patch/cancel -- Discord-side deletion. Waddles wins: reset to pending, no merge."""  # noqa: E501
        _update_sync_row(
            dal, sync_row.id, sync_status="pending", sync_error=None, clear_discord_event_id=True
        )
        _events_drift_counter.add(1)
        logger.warning(
            "event_discord_sync.drift_detected event_id=%s pairing_id=%s guild_id=%s "
            "reason=discord_404_reset_to_pending",
            event_id,
            pairing.id,
            pairing.discord_guild_id,
        )


def _default_targets(http_client: httpx.AsyncClient) -> list[PlatformEventTarget]:
    """Build the default `[DiscordScheduledEventTarget(...)]` bound to a CALLER-OWNED client.

    Takes `http_client` rather than constructing its own -- `sync_event()`
    owns the client's lifecycle (creates + closes it) when no `targets`
    are injected, so a single-shot caller (the internal blueprint) never
    leaks a connection per call.
    """

    def make_client(bot_token: str) -> DiscordEventTargetClient:
        return HttpDiscordEventTargetClient(http_client, bot_token=bot_token)

    return [
        DiscordScheduledEventTarget(
            credential_resolver=DefaultCredentialResolver(), make_client=make_client
        )
    ]


# ---------------------------------------------------------------------------
# Orchestrator entry point
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SyncResult:
    """`sync_event()`'s outcome -- FAIL-CLOSED, never raised to the caller."""

    event_id: int
    discord_event_id: str | None
    sync_status: str
    sync_error: str | None


async def sync_event(
    dal: Any,
    event_row: Any,
    *,
    action: Literal["create", "update", "cancel"],
    targets: list[PlatformEventTarget] | None = None,
) -> SyncResult:
    """Push `event_row` to every registered `PlatformEventTarget` -- the engine's one entry point.

    FAIL-CLOSED: never raises. `targets` defaults to `[DiscordScheduledEventTarget(...)]`
    (injectable for tests / future multi-platform callers). Persists the
    aggregate result onto `calendar_events` before returning.
    """
    event_id = int(event_row.id)
    bind_calendar_sync_tables(dal)

    owns_http_client = targets is None
    owned_http_client: httpx.AsyncClient | None = None

    try:
        tenant = _resolve_tenant_for_community(dal, int(event_row.community_id))
        if tenant is None:
            no_tenant_result = SyncResult(
                event_id, None, "sync_error", "community has no resolvable tenant"
            )
            _persist_calendar_event_aggregate(
                dal,
                event_id,
                discord_event_id=None,
                sync_status="sync_error",
                sync_error=no_tenant_result.sync_error,
            )
            _events_errors_counter.add(1)
            return no_tenant_result
        _tenant_id, tenant_slug, _is_global = tenant

        if not await _flag_enabled(tenant_slug):
            logger.info(
                "event_discord_sync.flag_disabled event_id=%s tenant=%s", event_id, tenant_slug
            )
            return SyncResult(
                event_id,
                getattr(event_row, "discord_event_id", None),
                str(getattr(event_row, "sync_status", "pending")),
                None,
            )

        if targets is not None:
            active_targets = targets
        else:
            owned_http_client = httpx.AsyncClient()
            active_targets = _default_targets(owned_http_client)

        aggregate_discord_id: str | None = None
        aggregate_status = "pending"
        aggregate_error: str | None = None

        async with bundle_span("calendar.event_discord_sync", event_id=event_id, action=action):
            for target in active_targets:
                try:
                    target_result = await target.push(dal, event_row, action=action)
                except Exception as exc:  # noqa: BLE001 - fail-closed: one target never blocks another
                    logger.error(
                        "event_discord_sync.target_failed_unexpected event_id=%s platform=%s "
                        "error_type=%s",
                        event_id,
                        getattr(target, "platform", "unknown"),
                        type(exc).__name__,
                    )
                    target_result = PlatformSyncResult(
                        getattr(target, "platform", "unknown"),
                        "sync_error",
                        None,
                        "unexpected_error",
                        0,
                        1,
                    )

                if target_result.sync_status == "synced":
                    _events_synced_counter.add(1)
                elif target_result.sync_status == "sync_error":
                    _events_errors_counter.add(1)

                # Discord is the only target today -- its result maps directly onto
                # calendar_events' own (currently Discord-shaped) three columns. A
                # second platform landing needs this aggregation generalized -- see
                # module docstring.
                if target.platform == "discord":
                    aggregate_discord_id = target_result.external_id
                    aggregate_status = target_result.sync_status
                    aggregate_error = target_result.sync_error

        _persist_calendar_event_aggregate(
            dal,
            event_id,
            discord_event_id=aggregate_discord_id,
            sync_status=aggregate_status,
            sync_error=aggregate_error,
        )
        logger.info(
            "event_discord_sync.event_synced event_id=%s action=%s sync_status=%s",
            event_id,
            action,
            aggregate_status,
        )
        return SyncResult(event_id, aggregate_discord_id, aggregate_status, aggregate_error)

    except Exception as exc:  # noqa: BLE001 - the ultimate fail-closed backstop
        logger.error(
            "event_discord_sync.sync_event_failed_unexpected event_id=%s error_type=%s",
            event_id,
            type(exc).__name__,
        )
        _events_errors_counter.add(1)
        try:
            _persist_calendar_event_aggregate(
                dal,
                event_id,
                discord_event_id=None,
                sync_status="sync_error",
                sync_error="unexpected_error",
            )
        except Exception:  # noqa: BLE001 - never let the persistence attempt itself escape
            logger.error("event_discord_sync.persist_failed_unexpected event_id=%s", event_id)
        return SyncResult(event_id, None, "sync_error", "unexpected_error")

    finally:
        if owns_http_client and owned_http_client is not None:
            await owned_http_client.aclose()


# ---------------------------------------------------------------------------
# Reconcile CronJob (mirrors role_sync_service.run_role_sync_reconcile_batch)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReconcileSummary:
    """Aggregate counters for one `run_event_sync_reconcile_batch()` pass -- the CLI's print line."""  # noqa: E501

    events_examined: int = 0
    events_synced: int = 0
    events_failed: int = 0


def _list_pending_events(dal: Any, *, limit: int) -> list[Any]:
    bind_calendar_sync_tables(dal)
    t = dal.calendar_events
    return list(
        dal((t.status == "approved") & (t.sync_status.belongs(["pending", "sync_error"]))).select(
            orderby=t.updated_at, limitby=(0, limit)
        )
    )


async def run_event_sync_reconcile_batch(
    dal: Any,
    *,
    credential_resolver: CredentialResolver | None = None,
    make_discord_client: Any = None,
    http_client: httpx.AsyncClient | None = None,
    limit: int = _DEFAULT_RECONCILE_LIMIT,
    sleep_fn: Any = asyncio.sleep,
) -> ReconcileSummary:
    """One full reconcile pass over every `approved`/`pending`-or-`sync_error` event.

    Retries with exponential backoff BETWEEN events in the same batch
    after a failure (Discord scheduled-event write limits are stricter
    than message rate limits) -- `sleep_fn` injectable for test speed.
    """
    owns_http_client = http_client is None
    http_client = http_client or httpx.AsyncClient()
    credential_resolver = credential_resolver or DefaultCredentialResolver()
    bound_http_client = http_client

    if make_discord_client is None:

        def make_discord_client(bot_token: str) -> DiscordEventTargetClient:
            return HttpDiscordEventTargetClient(bound_http_client, bot_token=bot_token)

    targets: list[PlatformEventTarget] = [
        DiscordScheduledEventTarget(
            credential_resolver=credential_resolver, make_client=make_discord_client
        )
    ]

    summary = ReconcileSummary()
    consecutive_errors = 0
    try:
        events = _list_pending_events(dal, limit=limit)
        summary.events_examined = len(events)
        for event_row in events:
            async with bundle_span(
                "calendar.event_discord_sync.reconcile", event_id=int(event_row.id)
            ):
                result = await sync_event(dal, event_row, action="update", targets=targets)
            if result.sync_status == "sync_error":
                summary.events_failed += 1
                consecutive_errors += 1
                await sleep_fn(min(_BACKOFF_BASE_S * (2**consecutive_errors), _BACKOFF_MAX_S))
            else:
                summary.events_synced += 1
                consecutive_errors = 0
        return summary
    finally:
        if owns_http_client:
            await bound_http_client.aclose()


async def _build_install_dal() -> Any:
    """Open this standalone CronJob process's own penguin-dal connection.

    Same DSN/pattern as `role_sync_service.py::_build_install_dal` /
    `usage_aggregator_service.py::_build_install_dal`.
    """
    from services.bundle_install_dal import build_install_dal

    return await build_install_dal(os.environ["DATABASE_URL"], pool_size=1)


async def main() -> int:
    """CronJob entrypoint: one reconcile pass, denominators printed, never a silent zero."""
    install_dal = await _build_install_dal()
    dal = install_dal.dal

    summary = await run_event_sync_reconcile_batch(dal)
    print(
        f"event_discord_sync: events_examined={summary.events_examined} "
        f"events_synced={summary.events_synced} events_failed={summary.events_failed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
