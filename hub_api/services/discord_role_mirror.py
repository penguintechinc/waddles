"""Cross-guild Discord role mirror: mapping CRUD + the reconcile pass (migration 0099).

One community can be paired with TWO (or more) Discord servers. A mirror mapping says "members of
guild A holding role X should hold role Y in guild B". Mappings are ONE-WAY; a reverse mapping is a
separate row. People are matched across guilds by the SAME Discord user id.

**Safety model**
- Both guilds must be paired with the mapping's community (so same tenant by construction) and the
  roles must exist (checked against the Discord API at create time).
- Cycles are rejected at create time (a mapping whose target can already reach its source through
  existing mappings): mirrored roles would otherwise chase each other forever.
- The reconcile pass only ever REMOVES a target role it granted itself (`discord_role_mirror_grants`
  ledger); a target role an admin assigned by hand is never taken away. Removal also requires a
  provably complete read of both guild member lists, so a pagination/permission hiccup demotes
  nobody.
- Members who are in the source guild but not the target guild are skipped and counted.
- Idempotent: a second pass over unchanged state makes zero Discord writes.

No raw user input or names are logged: community/mapping ids, guild/role snowflakes and counts only.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services import guild_pairing as pairing_svc
from services.bundle_telemetry import bundle_span, get_meter
from services.credential_resolver import CredentialResolver, TransportUnavailable
from services.discord_guild_roles import DiscordRole
from services.errors import ApiError, bad_request, conflict, not_found
from services.role_sync_service import (
    DiscordRoleTargetClient,
    DiscordSyncError,
    GuildMemberSnapshot,
    _flag_enabled,
    _resolve_tenant_for_community,
)
from services.schema import bind_guild_pairing_tables

logger = logging.getLogger(__name__)

_MAX_SNOWFLAKE_LEN = 255

_meter = get_meter()
_mirror_added_counter = _meter.create_counter(
    "waddles_role_mirror_roles_added_total", description="Target roles granted by the role mirror"
)
_mirror_removed_counter = _meter.create_counter(
    "waddles_role_mirror_roles_removed_total",
    description="Mirror-granted target roles removed by the role mirror",
)
_mirror_skipped_counter = _meter.create_counter(
    "waddles_role_mirror_skipped_not_member_total",
    description="Source role holders skipped because they are not in the target guild",
)
_mirror_errors_counter = _meter.create_counter(
    "waddles_role_mirror_errors_total", description="Role-mirror mapping failures, fail-closed"
)


@dataclass(slots=True, frozen=True)
class RoleMirrorMapping:
    """One `discord_role_mirror_mappings` row -- the wire-shape DTO for API responses."""

    id: int
    community_id: int
    source_guild_id: str
    source_role_id: str
    target_guild_id: str
    target_role_id: str
    last_synced_at: str | None
    last_roles_added: int
    last_roles_removed: int
    last_skipped_not_member: int
    last_error: str | None
    created_at: str | None


@dataclass(slots=True)
class MirrorSummary:
    """Aggregate counters for one `reconcile_role_mirrors()` pass."""

    mappings_examined: int = 0
    mappings_synced: int = 0
    mappings_failed: int = 0
    roles_added: int = 0
    roles_removed: int = 0
    skipped_not_member: int = 0


#: Lists a guild's roles for existence checks (real impl: `list_guild_roles`).
RoleLister = Callable[[Any, int, str], Awaitable[list[DiscordRole]]]


def _ensure_tables(dal: Any) -> None:
    bind_guild_pairing_tables(dal)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _from_row(row: Any) -> RoleMirrorMapping:
    return RoleMirrorMapping(
        id=int(row.id),
        community_id=int(row.community_id),
        source_guild_id=str(row.source_guild_id),
        source_role_id=str(row.source_role_id),
        target_guild_id=str(row.target_guild_id),
        target_role_id=str(row.target_role_id),
        last_synced_at=_iso(row.last_synced_at),
        last_roles_added=int(row.last_roles_added or 0),
        last_roles_removed=int(row.last_roles_removed or 0),
        last_skipped_not_member=int(row.last_skipped_not_member or 0),
        last_error=row.last_error,
        created_at=_iso(row.created_at),
    )


def _snowflake(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text.isdigit() or len(text) > _MAX_SNOWFLAKE_LEN:
        raise bad_request(f"{field} must be a non-empty numeric Discord snowflake")
    return text


def would_create_cycle(
    edges: list[tuple[tuple[str, str], tuple[str, str]]],
    new_edge: tuple[tuple[str, str], tuple[str, str]],
) -> bool:
    """True if adding `new_edge` (source -> target, nodes are `(guild, role)`) closes a cycle.

    A cycle exists iff the new edge's target can already reach its source through `edges`
    (covers the direct A->B + B->A reversal as well as longer chains).
    """
    source, target = new_edge
    adjacency: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for src, dst in edges:
        adjacency.setdefault(src, []).append(dst)
    seen: set[tuple[str, str]] = set()
    stack = [target]
    while stack:
        node = stack.pop()
        if node == source:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adjacency.get(node, []))
    return False


def list_mappings(dal: Any, community_id: int) -> list[RoleMirrorMapping]:
    """Every mirror mapping for `community_id`, oldest first."""
    _ensure_tables(dal)
    t = dal.discord_role_mirror_mappings
    return [_from_row(r) for r in dal(t.community_id == community_id).select(orderby=t.id)]


async def create_mapping(
    dal: Any,
    community_id: int,
    *,
    source_guild_id: str,
    source_role_id: str,
    target_guild_id: str,
    target_role_id: str,
    list_roles: RoleLister,
) -> RoleMirrorMapping:
    """Validate and create a one-way mirror mapping.

    Rejects: non-snowflake ids, identical guilds, a guild not paired with this community
    (hence not in the same tenant), a role absent from its guild (live Discord check), a
    duplicate, and any mapping that would form a cycle.
    """
    src_guild = _snowflake(source_guild_id, "source_guild_id")
    src_role = _snowflake(source_role_id, "source_role_id")
    dst_guild = _snowflake(target_guild_id, "target_guild_id")
    dst_role = _snowflake(target_role_id, "target_role_id")
    if src_guild == dst_guild:
        raise bad_request("source and target must be different Discord servers")

    _ensure_tables(dal)
    paired = {p.discord_guild_id for p in pairing_svc.list_pairings(dal, community_id)}
    for guild in (src_guild, dst_guild):
        if guild not in paired:
            raise ApiError(
                "Both Discord servers must be paired with this community",
                404,
                "DISCORD_PAIRING_NOT_FOUND",
            )

    t = dal.discord_role_mirror_mappings
    existing = list_mappings(dal, community_id)
    if any(
        (m.source_guild_id, m.source_role_id, m.target_guild_id, m.target_role_id)
        == (src_guild, src_role, dst_guild, dst_role)
        for m in existing
    ):
        raise conflict("That mirror mapping already exists")
    edges = [
        ((m.source_guild_id, m.source_role_id), (m.target_guild_id, m.target_role_id))
        for m in existing
    ]
    if would_create_cycle(edges, ((src_guild, src_role), (dst_guild, dst_role))):
        raise conflict("That mapping would create a mirror cycle")

    for guild, role in ((src_guild, src_role), (dst_guild, dst_role)):
        roles = await list_roles(dal, community_id, guild)
        if role not in {r.id for r in roles}:
            raise not_found("That role does not exist in the selected Discord server")

    try:
        row_id = t.insert(
            community_id=community_id,
            source_guild_id=src_guild,
            source_role_id=src_role,
            target_guild_id=dst_guild,
            target_role_id=dst_role,
            created_at=datetime.now(UTC),
        )
        row = dal(t.id == row_id).select().first()
        dal.commit()
    except Exception:
        dal.rollback()
        raise
    logger.info(
        "role_mirror.mapping_created community_id=%s mapping_id=%s src=%s/%s dst=%s/%s",
        community_id,
        row_id,
        src_guild,
        src_role,
        dst_guild,
        dst_role,
    )
    return _from_row(row)


def delete_mapping(dal: Any, community_id: int, mapping_id: int) -> bool:
    """Delete a mapping (and its grant ledger); roles already granted are left on members."""
    _ensure_tables(dal)
    t = dal.discord_role_mirror_mappings
    row = dal((t.id == mapping_id) & (t.community_id == community_id)).select().first()
    if row is None:
        return False
    try:
        dal(dal.discord_role_mirror_grants.mapping_id == mapping_id).delete()
        dal(t.id == mapping_id).delete()
        dal.commit()
    except Exception:
        dal.rollback()
        raise
    logger.info(
        "role_mirror.mapping_deleted community_id=%s mapping_id=%s", community_id, mapping_id
    )
    return True


@dataclass(slots=True)
class _GuildState:
    """One guild's member -> roles map for this pass, kept current as the pass writes."""

    roles: dict[str, set[str]]
    complete: bool


async def _snapshot(
    client: DiscordRoleTargetClient, cache: dict[str, _GuildState], guild_id: str
) -> _GuildState:
    """Member list for `guild_id`, fetched once per pass; `complete=False` if unprovable."""
    if guild_id not in cache:
        checked = getattr(client, "list_guild_members_checked", None)
        if checked is not None:
            snap: GuildMemberSnapshot = await checked(guild_id=guild_id)
        else:
            snap = GuildMemberSnapshot(
                members=await client.list_guild_members(guild_id=guild_id), complete=False
            )
        cache[guild_id] = _GuildState(
            roles={m.user_id: set(m.role_ids) for m in snap.members}, complete=snap.complete
        )
    return cache[guild_id]


def _record_result(
    dal: Any,
    mapping_id: int,
    *,
    added: int,
    removed: int,
    skipped: int,
    error: str | None,
) -> None:
    t = dal.discord_role_mirror_mappings
    dal(t.id == mapping_id).update(
        last_synced_at=datetime.now(UTC),
        last_roles_added=added,
        last_roles_removed=removed,
        last_skipped_not_member=skipped,
        last_error=error,
    )
    dal.commit()


async def _reconcile_mapping(
    dal: Any,
    mapping: Any,
    client: DiscordRoleTargetClient,
    cache: dict[str, _GuildState],
    summary: MirrorSummary,
) -> None:
    """Reconcile ONE mapping; counters are credited to `summary` as work happens."""
    mapping_id = int(mapping.id)
    src_guild, src_role = str(mapping.source_guild_id), str(mapping.source_role_id)
    dst_guild, dst_role = str(mapping.target_guild_id), str(mapping.target_role_id)
    grants_t = dal.discord_role_mirror_grants
    added = removed = skipped = 0
    error: str | None = None
    try:
        source = await _snapshot(client, cache, src_guild)
        target = await _snapshot(client, cache, dst_guild)
        holders = {u for u, roles in source.roles.items() if src_role in roles}
        target_roles = target.roles
        granted = {
            str(r.platform_user_id)
            for r in dal(
                (grants_t.mapping_id == mapping_id) & (grants_t.platform == "discord")
            ).select()
        }

        for uid in sorted(holders):
            roles = target_roles.get(uid)
            if roles is None:
                skipped += 1
                continue
            if dst_role in roles:
                continue  # already present -- either ours (ledger) or manual; never touch
            if await client.add_role(guild_id=dst_guild, user_id=uid, role_id=dst_role):
                added += 1
                roles.add(dst_role)
                if uid not in granted:
                    grants_t.insert(
                        mapping_id=mapping_id,
                        platform="discord",
                        platform_user_id=uid,
                        granted_at=datetime.now(UTC),
                    )
                    dal.commit()
                    granted.add(uid)

        if source.complete and target.complete:
            for uid in sorted(granted - holders):
                roles = target_roles.get(uid)
                if roles is not None and dst_role in roles:
                    shared = await _still_wanted_by_other_mapping(dal, mapping, uid, client, cache)
                    if shared:
                        continue  # another mapping still wants it; keep the ledger row
                    if not await client.remove_role(
                        guild_id=dst_guild, user_id=uid, role_id=dst_role
                    ):
                        continue  # keep the ledger row; retry next pass
                    removed += 1
                    roles.discard(dst_role)
                dal(
                    (grants_t.mapping_id == mapping_id) & (grants_t.platform_user_id == uid)
                ).delete()
                dal.commit()
    except (DiscordSyncError, TransportUnavailable) as exc:
        error = type(exc).__name__
    except Exception as exc:  # noqa: BLE001 - fail-closed: one mapping's bug never crashes the worker
        error = "unexpected_error"
        logger.error(
            "role_mirror.mapping_failed_unexpected mapping_id=%s error_type=%s",
            mapping_id,
            type(exc).__name__,
        )

    _record_result(dal, mapping_id, added=added, removed=removed, skipped=skipped, error=error)
    summary.roles_added += added
    summary.roles_removed += removed
    summary.skipped_not_member += skipped
    _mirror_added_counter.add(added)
    _mirror_removed_counter.add(removed)
    _mirror_skipped_counter.add(skipped)
    if error is None:
        summary.mappings_synced += 1
    else:
        summary.mappings_failed += 1
        _mirror_errors_counter.add(1)
    # Audit trail: structured, PII-free (ids + counts only).
    logger.info(
        "role_mirror.synced community_id=%s mapping_id=%s roles_added=%d roles_removed=%d "
        "skipped_not_member=%d error=%s",
        mapping.community_id,
        mapping_id,
        added,
        removed,
        skipped,
        error,
    )


async def _still_wanted_by_other_mapping(
    dal: Any,
    mapping: Any,
    uid: str,
    client: DiscordRoleTargetClient,
    cache: dict[str, _GuildState],
) -> bool:
    """True if another mapping with the same target role still wants `uid` to hold it.

    Conservative: an incomplete read of that other mapping's source guild counts as "wanted".
    """
    t = dal.discord_role_mirror_mappings
    others = dal(
        (t.community_id == mapping.community_id)
        & (t.id != mapping.id)
        & (t.target_guild_id == mapping.target_guild_id)
        & (t.target_role_id == mapping.target_role_id)
    ).select()
    for other in others:
        state = await _snapshot(client, cache, str(other.source_guild_id))
        if not state.complete or str(other.source_role_id) in state.roles.get(uid, set()):
            return True
    return False


async def reconcile_role_mirrors(
    dal: Any,
    *,
    credential_resolver: CredentialResolver,
    make_discord_client: Callable[[str], DiscordRoleTargetClient],
) -> MirrorSummary:
    """One pass over every mirror mapping, grouped per community (the CronJob's mirror step).

    Per community: flag gate (`waddles.role_sync`), resolve the tenant's bot token, then reconcile
    each mapping. A credential failure marks that community's mappings failed and moves on.
    """
    _ensure_tables(dal)
    summary = MirrorSummary()
    t = dal.discord_role_mirror_mappings
    by_community: dict[int, list[Any]] = {}
    for row in dal(t.id > 0).select(orderby=t.id):
        by_community.setdefault(int(row.community_id), []).append(row)

    for community_id, rows in by_community.items():
        summary.mappings_examined += len(rows)
        tenant = _resolve_tenant_for_community(dal, community_id)
        if tenant is None:
            summary.mappings_failed += len(rows)
            _mirror_errors_counter.add(len(rows))
            continue
        tenant_id, tenant_slug, is_global = tenant
        if not await _flag_enabled(tenant_slug):
            logger.info("role_mirror.flag_disabled community_id=%s", community_id)
            continue
        try:
            creds = await credential_resolver.resolve(
                dal, tenant_id=tenant_id, is_global_tenant=is_global, platform="discord"
            )
            bot_token = str(creds.payload.get("bot_token", ""))
            if not bot_token:
                raise TransportUnavailable("incomplete discord credentials")
        except TransportUnavailable as exc:
            for row in rows:
                _record_result(
                    dal, int(row.id), added=0, removed=0, skipped=0, error=type(exc).__name__
                )
            summary.mappings_failed += len(rows)
            _mirror_errors_counter.add(len(rows))
            logger.error("role_mirror.credentials_unavailable community_id=%s", community_id)
            continue

        client = make_discord_client(bot_token)
        cache: dict[str, _GuildState] = {}
        for row in rows:
            async with bundle_span("role_sync.mirror", mapping_id=int(row.id)):
                await _reconcile_mapping(dal, row, client, cache, summary)
    return summary
