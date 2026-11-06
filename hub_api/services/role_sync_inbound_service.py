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

- **Entries are verified before anything is applied** (same checks as
  ``svc_process::hop::verify_hop``): ``binding.mac`` is recomputed with the shared spine
  keyring (``ENVELOPE_BINDING_KEYS``; faithful port of ``penguin_spine::verify_binding``'s v3 arm,
  pinned to the Rust test vector; schema v2 is refused), the envelope tenant/community must
  equal the scope parsed
  from the STREAM KEY, and the numeric ids are resolved from the DB by that scope -- never taken
  from the JSON. Any failure acks the entry, increments
  ``waddles_role_sync_inbound_tenant_boundary_violations_total`` and applies nothing.
- **The payload is NOT MAC-covered** (the v3 MAC signs scope/ids only), so it is treated as
  untrusted: ``event_id`` is claimed once (Valkey ``SET NX`` + TTL, replays acked + counted) and,
  before applying, the member's current roles are fetched from Discord for the community's paired
  guild; a payload that disagrees is refused (``payload_mismatch``). Signing a payload digest
  into the v3 MAC (svc-ingest + spine) is tracked in #1059.
- **Bridge:** this consumer is a beta bridge on hub-api; it is to be migrated into the platform
  service bundles (svc-gateway / connector side) -- tracked in a follow-up issue.

The periodic reconcile (``role_sync_service``) remains the correctness backstop
for missed pushes (gateway gap, bot offline); this is the low-latency path.

**Logging hygiene.** Only pairing/community/guild ids, role ids and counts are
logged -- never a username, nickname or the member's platform id.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import struct
import sys
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import redis.exceptions

from services.bundle_telemetry import bundle_span, get_meter
from services.credential_resolver import DefaultCredentialResolver, TransportUnavailable
from services.guild_pairing import list_bindings
from services.role_sync_service import (
    _DISCORD_TO_PLATFORM_DIRECTIONS,
    CredentialResolver,
    DiscordSyncError,
    HttpDiscordRoleTargetClient,
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


_violations_counter = _meter.create_counter(
    "waddles_role_sync_inbound_tenant_boundary_violations_total",
    description="inbound entries refused by MAC / stream-scope / DB-scope verification",
)

_TENANT_WIDE_SEGMENT = "_tenant"
_V3_DOMAIN = b"waddles.binding.v3\0"
_TRACEPARENT = re.compile(r"^00-([0-9a-fA-F]{32})-[0-9a-fA-F]{16}-[0-9a-fA-F]{2}$")


class InboundEventError(ValueError):
    """A stream entry that is not a well-formed inbound role event (poison; acked + counted)."""


class TenantBoundaryError(ValueError):
    """An entry failed MAC / scope verification (forged, replayed cross-tenant, or misrouted)."""

    def __init__(self, reason: str) -> None:
        """Carry the stable metric-label ``reason``."""
        super().__init__(reason)
        self.reason = reason


class KeyRing:
    """``kid -> HMAC key`` ring parsed from ``ENVELOPE_BINDING_KEYS`` (``kid:hex[,kid:hex]``)."""

    def __init__(self, keys: dict[str, bytes]) -> None:
        """Hold the ring; an empty ring is refused (never fail-open)."""
        if not keys:
            raise ValueError("binding keyring is empty -- refusing to verify with no keys")
        self._keys = keys

    @classmethod
    def parse(cls, raw: str) -> KeyRing:
        """Parse the env shape; a malformed/empty ring is a hard error (never fail-open)."""
        keys: dict[str, bytes] = {}
        for entry in (e.strip() for e in raw.split(",")):
            if not entry:
                continue
            kid, sep, hexkey = entry.partition(":")
            if not sep or not kid or not hexkey:
                raise ValueError("ENVELOPE_BINDING_KEYS entry is not of the form kid:hexkey")
            keys[kid] = bytes.fromhex(hexkey)
        return cls(keys)

    def key_for(self, kid: str) -> bytes | None:
        """The key bytes for ``kid`` or ``None``."""
        return self._keys.get(kid)


def _framed(field: str) -> bytes:
    raw = field.encode()
    return struct.pack(">I", len(raw)) + raw


def compute_binding_mac_v3(
    ring: KeyRing,
    kid: str,
    tenant_id: int,
    community_id: int,
    tenant: str,
    community: str | None,
    workstream_id: str,
    event_id: str,
    trace_id: str | None,
) -> str:
    """Schema-v3 ``binding.mac`` (port of ``penguin_spine::compute_binding_mac_v3``)."""
    key = ring.key_for(kid)
    if key is None:
        raise TenantBoundaryError("unknown_kid")
    msg = _V3_DOMAIN + struct.pack(">q", tenant_id) + struct.pack(">i", community_id)
    msg += _framed(tenant)
    msg += b"\x01" + _framed(community) if community is not None else b"\x00"
    msg += _framed(workstream_id) + _framed(event_id) + _framed(trace_id or "")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _is_int(value: Any, lo: int, hi: int) -> bool:
    """``True`` for a real in-range int (bool is NOT an int here)."""
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def verify_binding(ring: KeyRing, env: dict[str, Any]) -> None:
    """Recompute ``binding.mac`` (v3 ONLY) and compare in constant time; raises on ANY failure.

    Port of ``penguin_spine::verify_binding``'s v3 arm: framed fields, signed numeric ids.
    Schema v2 (unframed, ids unsigned) is refused outright by this new consumer. Any unexpected
    exception while verifying a hostile entry becomes ``malformed_binding_fields`` -- a bad
    entry can never abort the batch.
    """
    try:
        _verify_binding_v3(ring, env)
    except TenantBoundaryError:
        raise
    except Exception as exc:  # noqa: BLE001 - hostile input: any failure is a refusal
        raise TenantBoundaryError("malformed_binding_fields") from exc


def _verify_binding_v3(ring: KeyRing, env: dict[str, Any]) -> None:
    binding = env.get("binding")
    if not isinstance(binding, dict):
        raise TenantBoundaryError("no_binding")
    kid, mac = binding.get("kid"), binding.get("mac")
    tenant, community = env.get("tenant"), env.get("community")
    ws, event_id = env.get("workstream_id"), env.get("event_id")
    if not all(isinstance(v, str) for v in (kid, mac, tenant, ws, event_id)) or not (
        community is None or isinstance(community, str)
    ):
        raise TenantBoundaryError("malformed_binding_fields")
    trace = env.get("trace")
    tp = trace.get("traceparent") if isinstance(trace, dict) else None
    match = _TRACEPARENT.match(tp) if isinstance(tp, str) else None
    trace_id = match.group(1) if match else None
    version = env.get("schema_version")
    tid, cid = env.get("tenant_id"), env.get("community_id")
    if version != 3:
        # v2 MACs do not cover the numeric ids and use unframed fields: refused outright.
        raise TenantBoundaryError("unsupported_schema_version")
    if tid is None or cid is None:
        raise TenantBoundaryError("v3_missing_ids")
    if not _is_int(tid, -(2**63), 2**63 - 1) or not _is_int(cid, -(2**31), 2**31 - 1):
        raise TenantBoundaryError("ids_out_of_range")
    expected = compute_binding_mac_v3(
        ring, kid, tid, cid, tenant, community, ws, event_id, trace_id
    )
    if not hmac.compare_digest(expected.encode(), mac.encode()):
        raise TenantBoundaryError("mac_mismatch")


def parse_scope_from_key(key: str) -> tuple[str, str | None] | None:
    """``waddles:t:{tenant}:c:{community}:...`` -> ``(tenant, community|None)`` (spine parser)."""
    rest = key.removeprefix("waddles:t:") if key.startswith("waddles:t:") else None
    if rest is None or ":c:" not in rest:
        return None
    tenant, _, tail = rest.partition(":c:")
    community = tail.split(":", 1)[0]
    if not tenant or not community:
        return None
    return tenant, (None if community == _TENANT_WIDE_SEGMENT else community)


def _resolve_scope_ids(dal: Any, tenant_slug: str, community_name: str) -> tuple[int, int]:
    """Numeric ``(tenant_id, community_id)`` for the stream-key scope, from the DB only."""
    _bind_scope_tables(dal)
    tenant = dal(dal.tenants.slug == tenant_slug).select().first()
    if tenant is None:
        raise TenantBoundaryError("unknown_tenant")
    community = (
        dal((dal.communities.name == community_name) & (dal.communities.tenant_id == tenant.id))
        .select()
        .first()
    )
    if community is None:
        raise TenantBoundaryError("unknown_community")
    return int(tenant.id), int(community.id)


def _bind_scope_tables(dal: Any) -> None:
    from services.role_sync_service import _bind_reference_tenants
    from services.schema import bind_auth_tables

    bind_auth_tables(dal)
    _bind_reference_tenants(dal)


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
    event_id: str = ""


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


def parse_inbound_entry(
    fields: dict[Any, Any], *, stream_key: str, ring: KeyRing, dal: Any
) -> InboundRoleEvent | None:
    """Decode AND VERIFY one ``XREADGROUP`` entry. ``None`` = a type this worker ignores.

    Raises ``InboundEventError`` for a malformed role event and ``TenantBoundaryError`` when
    the MAC, the stream-key scope or the DB scope check fails. The returned event's numeric ids
    come from the DB resolution of the STREAM KEY scope, never from the entry's JSON.
    Non-role events (messages, interactions) are normal traffic and return ``None`` unverified
    (nothing is applied for them).
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
    tenant_id, community_id = _verified_scope(env, stream_key, ring, dal)
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
            event_id=str(env["event_id"]),
        )
    return InboundRoleEvent(
        tenant_id=tenant_id,
        community_id=community_id,
        event_type=event_type,
        guild_id=guild_id,
        user_id=None,
        role_id=_snowflake(payload.get("role_id"), "role_id"),
        role_ids=frozenset(),
        event_id=str(env["event_id"]),
    )


def _verified_scope(
    env: dict[str, Any], stream_key: str, ring: KeyRing, dal: Any
) -> tuple[int, int]:
    """MAC + stream-key scope + DB-resolved numeric ids (``verify_hop`` parity)."""
    verify_binding(ring, env)
    scope = parse_scope_from_key(stream_key)
    if scope is None or scope[1] is None:
        raise TenantBoundaryError("bad_stream_key")
    key_tenant, key_community = scope
    if env.get("tenant") != key_tenant:
        raise TenantBoundaryError("tenant_mismatch")
    if env.get("community") != key_community:
        raise TenantBoundaryError("community_mismatch")
    tenant_id, community_id = _resolve_scope_ids(dal, key_tenant, key_community)
    for claimed, resolved in (
        (env.get("tenant_id"), tenant_id),
        (env.get("community_id"), community_id),
    ):
        if claimed is not None and claimed != resolved:
            raise TenantBoundaryError("json_ids_differ_from_scope")
    return tenant_id, community_id


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


class DiscordConfirm(Protocol):
    """Reads a member's CURRENT roles from Discord so the (unsigned) payload is never trusted."""

    async def member_roles(
        self, dal: Any, *, tenant_id: int, is_global: bool, guild_id: str, user_id: str
    ) -> set[str] | None:
        """The member's role ids now, or ``None`` when they are not in the guild."""
        ...


class ApiDiscordConfirm:
    """``DiscordConfirm`` over the existing credential resolver + Discord REST client."""

    def __init__(
        self,
        http_client: Any,
        credential_resolver: CredentialResolver | None = None,
    ) -> None:
        """Bind the shared HTTP client and (optionally) a custom credential resolver."""
        self._http = http_client
        self._resolver = credential_resolver or DefaultCredentialResolver()

    async def member_roles(
        self, dal: Any, *, tenant_id: int, is_global: bool, guild_id: str, user_id: str
    ) -> set[str] | None:
        """Resolve the tenant's bot token and ``GET /guilds/{guild}/members/{user}``."""
        creds = await self._resolver.resolve(
            dal, tenant_id=tenant_id, is_global_tenant=is_global, platform="discord"
        )
        token = str(creds.payload.get("bot_token", ""))
        if not token:
            raise TransportUnavailable(f"tenant {tenant_id} has no discord bot token")
        client = HttpDiscordRoleTargetClient(self._http, bot_token=token)
        return await client.get_member_role_ids(guild_id=guild_id, user_id=user_id)


async def apply_inbound_event(
    dal: Any, event: InboundRoleEvent, confirm: DiscordConfirm
) -> InboundOutcome:
    """Reconcile ONE inbound event into Waddles under the role-sync direction rules.

    Returns the classified outcome; never raises for an expected refusal. Writes only
    ``community_members.role`` (via the shared idempotent applier) -- never writes Discord.

    The event PAYLOAD is not covered by ``binding.mac``, so it is untrusted: before anything is
    applied the member's current roles are fetched from Discord (the community's paired guild,
    the tenant's own credential) and must equal the payload; only that confirmed state is used.
    """
    tenant = _resolve_tenant_for_community(dal, event.community_id)
    if tenant is None:
        return InboundOutcome("no_tenant")
    tenant_id, tenant_slug, is_global = tenant
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
    try:
        confirmed = await confirm.member_roles(
            dal,
            tenant_id=tenant_id,
            is_global=is_global,
            guild_id=str(pairing.discord_guild_id),
            user_id=event.user_id,
        )
    except (TransportUnavailable, DiscordSyncError) as exc:
        logger.error(
            "role_sync.inbound_confirm_unavailable community_id=%s error_type=%s",
            event.community_id,
            type(exc).__name__,
        )
        return InboundOutcome("confirm_unavailable")
    if confirmed is None or set(confirmed) != set(event.role_ids):
        logger.error(
            "role_sync.inbound_payload_mismatch community_id=%s guild_id=%s -- refused",
            event.community_id,
            event.guild_id,
        )
        return InboundOutcome("payload_mismatch")
    desired = _resolve_desired_community_role(
        _community_role_priorities(dal, event.community_id),
        {inbound_roles[r] for r in confirmed if r in inbound_roles},
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
    violations: int = 0
    replayed: int = 0


async def _ensure_group(client: Any, stream: str) -> None:
    try:
        await client.xgroup_create(stream, INBOUND_CONSUMER_GROUP, id="$", mkstream=False)
    except redis.exceptions.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def run_inbound_batch(
    dal: Any,
    redis_client: Any,
    ring: KeyRing,
    confirm: DiscordConfirm,
    *,
    batch_size: int = 200,
    consumer_name: str = "hub-api-1",
) -> InboundBatchResult:
    """One bounded, non-blocking drain of every Discord source stream.

    Per entry: poison / violation / replay / failure is counted and acked (failures are first
    written to a bounded dead-letter stream), so one bad entry never aborts the batch or strands
    a pending-entries-list slot. The periodic reconcile is the correctness backstop.
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
                    await _handle_entry(
                        dal, redis_client, ring, confirm, stream, entry_id, fields, result
                    )
    return result


_DEDUPE_TTL_SECONDS = 24 * 3600
_DLQ_STREAM = "waddles:dlq:role_sync_inbound"
_DLQ_MAXLEN = 1000


async def _handle_entry(
    dal: Any,
    redis_client: Any,
    ring: KeyRing,
    confirm: DiscordConfirm,
    stream: str,
    entry_id: Any,
    fields: Any,
    result: InboundBatchResult,
) -> None:
    """Never raises: any failure is dead-lettered (id + reason only) and the entry is acked."""
    try:
        await _process_entry(dal, redis_client, ring, confirm, stream, entry_id, fields, result)
    except Exception as exc:  # noqa: BLE001 - one bad entry must never abort the batch
        result.failed += 1
        _events_counter.add(1, {"outcome": "failed"})
        logger.error("role_sync.inbound_entry_failed error_type=%s", type(exc).__name__)
        try:
            await redis_client.xadd(
                _DLQ_STREAM,
                {"stream": stream, "entry_id": _decode(entry_id), "error": type(exc).__name__},
                maxlen=_DLQ_MAXLEN,
                approximate=True,
            )
        except Exception as dlq_exc:  # noqa: BLE001 - DLQ is best effort, the ack still happens
            logger.error("role_sync.inbound_dlq_failed error_type=%s", type(dlq_exc).__name__)
        try:
            await redis_client.xack(stream, INBOUND_CONSUMER_GROUP, entry_id)
        except Exception as ack_exc:  # noqa: BLE001
            logger.error("role_sync.inbound_ack_failed error_type=%s", type(ack_exc).__name__)


async def _process_entry(
    dal: Any,
    redis_client: Any,
    ring: KeyRing,
    confirm: DiscordConfirm,
    stream: str,
    entry_id: Any,
    fields: Any,
    result: InboundBatchResult,
) -> None:
    try:
        event = parse_inbound_entry(fields, stream_key=stream, ring=ring, dal=dal)
    except TenantBoundaryError as exc:
        result.violations += 1
        _violations_counter.add(1, {"reason": exc.reason})
        logger.error("role_sync.inbound_tenant_boundary_violation reason=%s", exc.reason)
        await redis_client.xack(stream, INBOUND_CONSUMER_GROUP, entry_id)
        return
    except InboundEventError:
        result.poison += 1
        _events_counter.add(1, {"outcome": "poison"})
        await redis_client.xack(stream, INBOUND_CONSUMER_GROUP, entry_id)
        return
    if event is None:
        await redis_client.xack(stream, INBOUND_CONSUMER_GROUP, entry_id)
        return
    result.role_events += 1
    # Replay guard: a (validly MAC'd) event_id is honoured once. Claimed BEFORE applying.
    claimed = await redis_client.set(
        f"role_sync:inbound:evt:{event.event_id}", "1", nx=True, ex=_DEDUPE_TTL_SECONDS
    )
    if not claimed:
        result.replayed += 1
        _events_counter.add(1, {"outcome": "replayed"})
        await redis_client.xack(stream, INBOUND_CONSUMER_GROUP, entry_id)
        return
    outcome = await apply_inbound_event(dal, event, confirm)
    _events_counter.add(1, {"outcome": outcome.outcome})
    if outcome.outcome == "applied":
        result.applied += 1
    await redis_client.xack(stream, INBOUND_CONSUMER_GROUP, entry_id)


@contextlib.asynccontextmanager
async def _opened_dal() -> Any:
    """Async-context adapter over the sync ``open_worker_dal`` context manager."""
    with open_worker_dal(os.environ["DATABASE_URL"]) as async_dal:
        yield async_dal


async def main() -> int:
    """CronJob entrypoint: drain up to 20 batches, print denominators."""
    ring = KeyRing.parse(os.environ["ENVELOPE_BINDING_KEYS"])
    redis_client = build_client()
    total = InboundBatchResult()
    async with httpx.AsyncClient() as http_client, _opened_dal() as async_dal:
        confirm = ApiDiscordConfirm(http_client)
        for _ in range(20):
            r = await run_inbound_batch(async_dal.dal, redis_client, ring, confirm)
            total.streams = max(total.streams, r.streams)
            total.examined += r.examined
            total.role_events += r.role_events
            total.applied += r.applied
            total.poison += r.poison
            total.failed += r.failed
            total.violations += r.violations
            total.replayed += r.replayed
            if r.examined == 0:
                break
    print(
        f"role_sync_inbound: streams={total.streams} examined={total.examined} "
        f"role_events={total.role_events} applied={total.applied} "
        f"poison={total.poison} failed={total.failed} violations={total.violations} "
        f"replayed={total.replayed}"
    )
    if total.streams == 0:
        print("role_sync_inbound: zero Discord streams found", file=sys.stderr)
    return 1 if total.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
