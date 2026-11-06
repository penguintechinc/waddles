"""Discord -> platform INBOUND role sync: the event-driven fast path (gateway push).

The Discord service bundle (1.0.3) forwards ``GUILD_MEMBER_UPDATE`` as
``guild.member.update`` and ``GUILD_ROLE_CREATE|UPDATE|DELETE`` as
``guild.role.*`` onto the per-source ingest stream. This worker consumes them
and applies the SAME direction rules ``role_sync_service`` documents -- it adds
no new authority:

- **Discord is the source of truth only for ``community_role`` bindings.** A
  member update is mapped through ``community_role_sync_bindings`` rows with
  ``sync_scope = 'community_role'`` and written to ``community_members.role``
  via the shared, idempotent ``_apply_community_role()`` (grant-only, never
  touches ``community-owner``, never creates membership).
- **Echo / loop prevention (origin marker).** Roles bound ``subscriber_tier`` /
  ``moderator`` are OUTBOUND-OWNED: Waddles writes them to Discord
  (``reconcile_pairing``). A member update that Discord emits because of that
  outbound ``role.add`` carries those role ids; they are classified
  ``echo_ignored`` and never read as an inbound change. ``sync_scope`` is the
  durable origin marker (a role id is bound to exactly ONE write direction for
  life, enforced by ``guild_pairing.create_binding``), so an outbound write can
  never be re-applied inbound. Re-delivery of the same event is additionally
  harmless: the apply is idempotent (``unchanged``) and stream entries are
  acked per consumer group.
- **Cross-community refused.** The event's ``(tenant_id, community_id)`` come
  from the ingest envelope (host-resolved, never the guild's own claim); a
  guild id not paired to that exact community, or a community whose tenant does
  not match the envelope, is refused and counted -- never applied.
- **Role events are informational.** ``guild.role.create|update`` are noted;
  ``guild.role.delete`` of a BOUND role is logged loudly (the binding is now
  dangling for an operator to repair) but never auto-demotes anyone -- demotion
  stays an explicit admin action / the periodic reconcile's guarded pass.

The periodic reconcile (``role_sync_service``) remains the correctness backstop
for missed pushes (gateway gap, bot offline); this is the low-latency path.

**Logging hygiene.** Only pairing/community/guild ids, role ids and counts are
logged -- never a username, nickname or the member's platform id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

import redis.exceptions

from services.bundle_telemetry import bundle_span, get_meter
from services.guild_pairing import list_bindings
from services.role_sync_service import (
    _DISCORD_TO_PLATFORM_DIRECTIONS,
    _apply_community_role,
    _community_role_priorities,
    _find_linked_hub_user_by_discord,
    _flag_enabled,
    _resolve_desired_community_role,
    _resolve_tenant_for_community,
)
from services.schema import bind_bar_citizen_tables
from services.sync_worker_dal import open_worker_dal
from services.valkey_admin_client import build_client

logger = logging.getLogger(__name__)

#: Consumer group this worker owns on each Discord source stream.
INBOUND_CONSUMER_GROUP = "hub_api_role_sync_inbound"
#: ``SCAN MATCH`` for every Discord source stream (``...:src:discord:*``).
DISCORD_STREAM_PATTERN = "waddles:t:*:c:*:src:discord:*"
_MEMBER_EVENT = "guild.member.update"
_ROLE_EVENTS = frozenset({"guild.role.create", "guild.role.update", "guild.role.delete"})
#: Sync scopes whose Discord role ids Waddles WRITES (outbound-owned -> echo on inbound).
_OUTBOUND_SCOPES = frozenset({"subscriber_tier", "moderator"})

_meter = get_meter()
_events_counter = _meter.create_counter(
    "waddles_role_sync_inbound_events_total",
    description="inbound Discord role events processed, by outcome",
)
_applied_counter = _meter.create_counter(
    "waddles_role_sync_inbound_applied_total",
    description="community_members.role changes applied from an inbound Discord push",
)


class InboundEventError(ValueError):
    """A stream entry that is not a well-formed inbound role event (poison; acked + counted)."""


@dataclass(slots=True, frozen=True)
class InboundRoleEvent:
    """One decoded ``guild.member.update`` / ``guild.role.*`` stream entry."""

    tenant_id: int
    community_id: int
    event_type: str
    guild_id: str
    user_id: str | None
    role_id: str | None
    role_ids: frozenset[str]


@dataclass(slots=True, frozen=True)
class InboundOutcome:
    """The classified result of applying one event (the metric label + test oracle)."""

    outcome: str
    applied_role: str | None = None


def _decode(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _snowflake(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.isdigit():
        raise InboundEventError(f"`{field}` missing or not a snowflake")
    return value


def parse_inbound_entry(fields: dict[Any, Any]) -> InboundRoleEvent | None:
    """Decode one ``XREADGROUP`` entry. ``None`` = a valid event of a type this worker ignores.

    Raises ``InboundEventError`` for a malformed Discord role event. Non-role events (messages,
    interactions) are the normal traffic on the same stream and return ``None``.
    """
    decoded = {_decode(k): v for k, v in fields.items()}
    raw = decoded.get("env")
    if raw is None:
        raise InboundEventError("entry has no `env` field")
    try:
        env = json.loads(_decode(raw))
    except json.JSONDecodeError as exc:
        raise InboundEventError("`env` is not JSON") from exc
    event = env.get("event") if isinstance(env, dict) else None
    if not isinstance(event, dict):
        raise InboundEventError("`env.event` missing")
    event_type = event.get("event_type")
    if event_type != _MEMBER_EVENT and event_type not in _ROLE_EVENTS:
        return None
    if event.get("platform") != "discord":
        raise InboundEventError("role event on a non-discord platform")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise InboundEventError("`payload` missing")
    tenant_id, community_id = env.get("tenant_id"), env.get("community_id")
    if not isinstance(tenant_id, int) or not isinstance(community_id, int):
        raise InboundEventError("envelope carries no resolved tenant/community ids")
    guild_id = _snowflake(payload.get("guild_id"), "guild_id")
    if event_type == _MEMBER_EVENT:
        roles = payload.get("role_ids")
        if not isinstance(roles, list):
            raise InboundEventError("`role_ids` missing")
        return InboundRoleEvent(
            tenant_id=tenant_id,
            community_id=community_id,
            event_type=event_type,
            guild_id=guild_id,
            user_id=_snowflake(payload.get("user_id"), "user_id"),
            role_id=None,
            role_ids=frozenset(_snowflake(r, "role_ids[]") for r in roles),
        )
    return InboundRoleEvent(
        tenant_id=tenant_id,
        community_id=community_id,
        event_type=event_type,
        guild_id=guild_id,
        user_id=None,
        role_id=_snowflake(payload.get("role_id"), "role_id"),
        role_ids=frozenset(),
    )


def _find_pairing(dal: Any, community_id: int, guild_id: str) -> Any | None:
    """The enabled pairing for EXACTLY this ``(community, guild)``; ``None`` refuses the event."""
    bind_bar_citizen_tables(dal)
    t = dal.guild_tenant_pairings
    return (
        dal(
            (t.community_id == community_id)
            & (t.discord_guild_id == guild_id)
            & (t.sync_enabled == True)  # noqa: E712 - pydal idiom
        )
        .select()
        .first()
    )


async def apply_inbound_event(dal: Any, event: InboundRoleEvent) -> InboundOutcome:
    """Reconcile ONE inbound event into Waddles under the role-sync direction rules.

    Returns the classified outcome; never raises for an expected refusal. Writes only
    ``community_members.role`` (via the shared idempotent applier) -- never Discord.
    """
    tenant = _resolve_tenant_for_community(dal, event.community_id)
    if tenant is None:
        return InboundOutcome("no_tenant")
    tenant_id, tenant_slug, _is_global = tenant
    if tenant_id != event.tenant_id:
        logger.warning("role_sync.inbound_tenant_mismatch community_id=%s", event.community_id)
        return InboundOutcome("cross_tenant_refused")
    pairing = _find_pairing(dal, event.community_id, event.guild_id)
    if pairing is None:
        logger.warning(
            "role_sync.inbound_no_pairing community_id=%s guild_id=%s",
            event.community_id,
            event.guild_id,
        )
        return InboundOutcome("cross_community_refused")
    if pairing.direction not in _DISCORD_TO_PLATFORM_DIRECTIONS:
        return InboundOutcome("direction_not_inbound")
    if not await _flag_enabled(tenant_slug):
        return InboundOutcome("flag_off")

    bindings = list_bindings(dal, event.community_id, int(pairing.id))
    if event.event_type != _MEMBER_EVENT:
        return _note_role_event(event, bindings)

    inbound_roles = {
        b.discord_role_id: b.community_role
        for b in bindings
        if b.sync_scope == "community_role" and b.community_role is not None
    }
    outbound_ids = {b.discord_role_id for b in bindings if b.sync_scope in _OUTBOUND_SCOPES}
    mapped = {inbound_roles[r] for r in event.role_ids if r in inbound_roles}
    if not mapped:
        if event.role_ids & outbound_ids:
            # Discord echoing a role Waddles itself wrote (subscriber tier / moderator).
            return InboundOutcome("echo_ignored")
        return InboundOutcome("no_mapped_roles")
    if event.user_id is None:
        return InboundOutcome("malformed")
    hub_user_id = _find_linked_hub_user_by_discord(dal, event.user_id)
    if hub_user_id is None:
        return InboundOutcome("unlinked")
    desired = _resolve_desired_community_role(
        _community_role_priorities(dal, event.community_id), mapped
    )
    if desired is None:  # defensive: `mapped` is non-empty, so never expected
        return InboundOutcome("no_mapped_roles")
    result = _apply_community_role(
        dal, community_id=event.community_id, hub_user_id=hub_user_id, desired_role=desired
    )
    if result == "applied":
        _applied_counter.add(1)
        return InboundOutcome("applied", desired)
    return InboundOutcome(result)


def _note_role_event(event: InboundRoleEvent, bindings: list[Any]) -> InboundOutcome:
    """``guild.role.*``: informational. A deleted BOUND role is surfaced for operator repair."""
    if event.event_type == "guild.role.delete" and any(
        b.discord_role_id == event.role_id for b in bindings
    ):
        logger.error(
            "role_sync.inbound_bound_role_deleted community_id=%s guild_id=%s role_id=%s "
            "-- binding is dangling; no member is demoted",
            event.community_id,
            event.guild_id,
            event.role_id,
        )
        return InboundOutcome("bound_role_deleted")
    return InboundOutcome("role_event_noted")


@dataclass(slots=True)
class InboundBatchResult:
    """Denominators for one drain pass (zero examined is reported, never silent)."""

    streams: int = 0
    examined: int = 0
    role_events: int = 0
    applied: int = 0
    poison: int = 0
    failed: int = 0


async def _ensure_group(client: Any, stream: str) -> None:
    try:
        await client.xgroup_create(stream, INBOUND_CONSUMER_GROUP, id="$", mkstream=False)
    except redis.exceptions.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def run_inbound_batch(
    dal: Any, redis_client: Any, *, batch_size: int = 200, consumer_name: str = "hub-api-1"
) -> InboundBatchResult:
    """One bounded, non-blocking drain of every Discord source stream.

    Per entry: poison (unparseable role event) is acked + counted; an apply that raises is
    NOT acked (stays pending for retry) and is counted ``failed`` so it is never silent.
    """
    result = InboundBatchResult()
    async with bundle_span("hub.role_sync.inbound_batch"):
        keys = [k async for k in redis_client.scan_iter(match=DISCORD_STREAM_PATTERN)]
        for raw_key in keys:
            stream = _decode(raw_key)
            result.streams += 1
            await _ensure_group(redis_client, stream)
            response = await redis_client.xreadgroup(
                INBOUND_CONSUMER_GROUP, consumer_name, {stream: ">"}, count=batch_size
            )
            for _key, entries in response or []:
                for entry_id, fields in entries:
                    result.examined += 1
                    await _handle_entry(dal, redis_client, stream, entry_id, fields, result)
    return result


async def _handle_entry(
    dal: Any, redis_client: Any, stream: str, entry_id: Any, fields: Any, result: InboundBatchResult
) -> None:
    try:
        event = parse_inbound_entry(fields)
    except InboundEventError:
        result.poison += 1
        _events_counter.add(1, {"outcome": "poison"})
        await redis_client.xack(stream, INBOUND_CONSUMER_GROUP, entry_id)
        return
    if event is None:
        await redis_client.xack(stream, INBOUND_CONSUMER_GROUP, entry_id)
        return
    result.role_events += 1
    try:
        outcome = await apply_inbound_event(dal, event)
    except Exception as exc:  # noqa: BLE001 - one bad event must not stop the drain; left pending
        result.failed += 1
        _events_counter.add(1, {"outcome": "failed"})
        logger.error(
            "role_sync.inbound_apply_failed community_id=%s error_type=%s",
            event.community_id,
            type(exc).__name__,
        )
        return
    _events_counter.add(1, {"outcome": outcome.outcome})
    if outcome.outcome == "applied":
        result.applied += 1
    await redis_client.xack(stream, INBOUND_CONSUMER_GROUP, entry_id)


async def main() -> int:
    """CronJob entrypoint: drain up to 20 batches, print denominators."""
    redis_client = build_client()
    total = InboundBatchResult()
    with open_worker_dal(os.environ["DATABASE_URL"]) as async_dal:
        for _ in range(20):
            r = await run_inbound_batch(async_dal.dal, redis_client)
            total.streams = max(total.streams, r.streams)
            total.examined += r.examined
            total.role_events += r.role_events
            total.applied += r.applied
            total.poison += r.poison
            total.failed += r.failed
            if r.examined == 0:
                break
    print(
        f"role_sync_inbound: streams={total.streams} examined={total.examined} "
        f"role_events={total.role_events} applied={total.applied} "
        f"poison={total.poison} failed={total.failed}"
    )
    if total.streams == 0:
        print("role_sync_inbound: zero Discord streams found", file=sys.stderr)
    return 1 if total.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
