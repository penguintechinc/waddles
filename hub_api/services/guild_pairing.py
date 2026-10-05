"""Service layer for Bar Citizen's guild<->tenant pairing + role-sync bindings (migration 0034).

**Foundation module** -- the pairing/binding CRUD other Bar Citizen units
build on. Tables are bound lazily via `services.schema.bind_bar_citizen_tables()`,
the same idempotent guarded-membership-check pattern every other hub_api
service module in this port follows (see `community_connections.py`'s own
module docstring).

Query style matches `services/community_common.py`/`services/
community_activity.py`'s established pattern for this port (not
`backend-python.md`'s general `asyncio.to_thread` guidance): the raw
`pydal` `dal` is called synchronously from inside async blueprint
handlers, every write is followed by an explicit `dal.commit()` (no
autocommit), and uniqueness is enforced by a select-then-insert check in
this layer (mirroring `community_connections.py::_sync_upsert`) rather
than catching a DB-level `IntegrityError` -- the partial unique indexes
`0034_bar_citizen_guild_pairing.py` creates are production's second,
belt-and-suspenders guard, not the only one.

Every function is tenant-scoped by `community_id` alone -- the caller
(blueprint layer, `blueprints/v1/guild_pairing.py`) has already resolved
and authorized `community_id` against the token's tenant via
`services.community_common.community_in_tenant()`, same trust boundary
this port's other service modules assume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.admin_service import VALID_MEMBER_ROLES
from services.errors import bad_request, conflict, not_found
from services.schema import bind_bar_citizen_tables

#: `guild_tenant_pairings.direction` -- migration 0034's own CHECK constraint values.
#: `discord_to_twitch` is the Discord -> platform direction's historical name (kept
#: verbatim rather than renamed, see `role_sync_service.py`'s module docstring) --
#: Discord guild role changes drive the linked user's `community_role` binding, never
#: an actual Twitch-side write (Twitch grants no API to assign subscriber tiers).
VALID_DIRECTIONS: tuple[str, ...] = ("discord_to_twitch", "twitch_to_discord", "bidirectional")

#: `community_role_sync_bindings.sync_scope` -- migration 0034/0036's own CHECK
#: constraint values. `community_role` (0036) is the Discord -> platform direction's
#: own binding type, mutually exclusive with `subscriber_tier`/`moderator` (Twitch ->
#: Discord) -- see migration 0036's own docstring for the structural loop-prevention
#: argument (each binding's scope fixes its one authoritative write direction).
VALID_SYNC_SCOPES: tuple[str, ...] = ("subscriber_tier", "moderator", "community_role")

#: `community_role_sync_bindings.subscriber_tier` -- migration 0034's own CHECK constraint values.
VALID_SUBSCRIBER_TIERS: tuple[int, ...] = (1, 2, 3)

#: `community_role_sync_bindings.community_role` -- migration 0036's own CHECK constraint
#: values, the exact same set `services/admin_service.py::update_member_role()` accepts
#: for a human-driven role change -- imported, not duplicated, so the two never drift.
#: `community-owner` is deliberately excluded (never assignable by sync, see
#: `role_sync_service.py`'s owner-protection invariant).
VALID_COMMUNITY_ROLES: tuple[str, ...] = VALID_MEMBER_ROLES

_MAX_ROLE_NAME_PREFIX_LEN = 50
_MAX_EXTERNAL_ID_LEN = 255


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class GuildPairing:
    """One `guild_tenant_pairings` row -- the wire-shape DTO for blueprint/API responses."""

    id: int
    community_id: int
    discord_guild_id: str
    direction: str
    sync_enabled: bool
    role_name_prefix: str
    created_by_user_id: int | None
    created_at: str | None
    updated_at: str | None


@dataclass(slots=True, frozen=True)
class RoleSyncBinding:
    """One `community_role_sync_bindings` row -- the wire-shape DTO for blueprint/API responses."""

    id: int
    pairing_id: int
    sync_scope: str
    subscriber_tier: int | None
    community_role: str | None
    discord_role_id: str
    created_at: str | None
    updated_at: str | None


# ---------------------------------------------------------------------------
# Validation / table binding
# ---------------------------------------------------------------------------


def _ensure_tables(dal: Any) -> None:
    bind_bar_citizen_tables(dal)


def _validate_discord_guild_id(value: str) -> str:
    value = (value or "").strip()
    if not value or not value.isdigit() or len(value) > _MAX_EXTERNAL_ID_LEN:
        raise bad_request("discord_guild_id must be a non-empty numeric Discord snowflake")
    return value


def _validate_discord_role_id(value: str) -> str:
    value = (value or "").strip()
    if not value or not value.isdigit() or len(value) > _MAX_EXTERNAL_ID_LEN:
        raise bad_request("discord_role_id must be a non-empty numeric Discord snowflake")
    return value


def _validate_direction(value: str) -> str:
    if value not in VALID_DIRECTIONS:
        raise bad_request(f"direction must be one of {VALID_DIRECTIONS}")
    return value


def _validate_role_name_prefix(value: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > _MAX_ROLE_NAME_PREFIX_LEN:
        raise bad_request(f"role_name_prefix must be 1-{_MAX_ROLE_NAME_PREFIX_LEN} characters")
    return value


def _validate_sync_scope_and_tier(
    sync_scope: str, subscriber_tier: int | None, community_role: str | None
) -> None:
    if sync_scope not in VALID_SYNC_SCOPES:
        raise bad_request(f"sync_scope must be one of {VALID_SYNC_SCOPES}")
    if sync_scope == "subscriber_tier":
        if subscriber_tier not in VALID_SUBSCRIBER_TIERS:
            raise bad_request(f"subscriber_tier must be one of {VALID_SUBSCRIBER_TIERS}")
        if community_role is not None:
            raise bad_request("community_role must be omitted when sync_scope is 'subscriber_tier'")
    elif sync_scope == "moderator":
        if subscriber_tier is not None:
            raise bad_request("subscriber_tier must be omitted when sync_scope is 'moderator'")
        if community_role is not None:
            raise bad_request("community_role must be omitted when sync_scope is 'moderator'")
    else:  # "community_role"
        if subscriber_tier is not None:
            raise bad_request("subscriber_tier must be omitted when sync_scope is 'community_role'")
        if community_role not in VALID_COMMUNITY_ROLES:
            raise bad_request(f"community_role must be one of {VALID_COMMUNITY_ROLES}")


# ---------------------------------------------------------------------------
# DTO conversion
# ---------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _pairing_from_row(row: Any) -> GuildPairing:
    return GuildPairing(
        id=int(row.id),
        community_id=int(row.community_id),
        discord_guild_id=row.discord_guild_id,
        direction=row.direction,
        sync_enabled=bool(row.sync_enabled),
        role_name_prefix=row.role_name_prefix,
        created_by_user_id=row.created_by_user_id,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _binding_from_row(row: Any) -> RoleSyncBinding:
    return RoleSyncBinding(
        id=int(row.id),
        pairing_id=int(row.pairing_id),
        sync_scope=row.sync_scope,
        subscriber_tier=row.subscriber_tier,
        community_role=row.community_role,
        discord_role_id=row.discord_role_id,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


# ---------------------------------------------------------------------------
# Pairings
# ---------------------------------------------------------------------------


def list_pairings(dal: Any, community_id: int) -> list[GuildPairing]:
    """Return every `guild_tenant_pairings` row for `community_id`, newest first."""
    _ensure_tables(dal)
    t = dal.guild_tenant_pairings
    rows = dal(t.community_id == community_id).select(orderby=~t.created_at)
    return [_pairing_from_row(row) for row in rows]


def get_pairing(dal: Any, community_id: int, pairing_id: int) -> GuildPairing | None:
    """Return one pairing scoped to `community_id`, or `None` if absent/not owned by it."""
    _ensure_tables(dal)
    t = dal.guild_tenant_pairings
    row = dal((t.id == pairing_id) & (t.community_id == community_id)).select().first()
    return _pairing_from_row(row) if row is not None else None


def create_pairing(
    dal: Any,
    community_id: int,
    *,
    discord_guild_id: str,
    direction: str,
    role_name_prefix: str,
    sync_enabled: bool = False,
    actor_user_id: int | None,
) -> GuildPairing:
    """Create a new N:M guild<->community pairing; opt-in by default (`sync_enabled=False`).

    Raises `conflict()` if this `(community_id, discord_guild_id)` pair
    already has a pairing -- `UNIQUE (community_id, discord_guild_id)` in
    migration 0034 is the production backstop, this check is the clear,
    typed error path.
    """
    discord_guild_id = _validate_discord_guild_id(discord_guild_id)
    direction = _validate_direction(direction)
    role_name_prefix = _validate_role_name_prefix(role_name_prefix)

    _ensure_tables(dal)
    t = dal.guild_tenant_pairings
    try:
        existing = (
            dal((t.community_id == community_id) & (t.discord_guild_id == discord_guild_id))
            .select()
            .first()
        )
        if existing is not None:
            raise conflict(
                f"community {community_id} is already paired with guild {discord_guild_id}"
            )

        now = datetime.now(UTC)
        row_id = t.insert(
            community_id=community_id,
            discord_guild_id=discord_guild_id,
            direction=direction,
            sync_enabled=bool(sync_enabled),
            role_name_prefix=role_name_prefix,
            created_by_user_id=actor_user_id,
            created_at=now,
            updated_at=now,
        )
        row = dal(t.id == row_id).select().first()
        dal.commit()
    except Exception:
        dal.rollback()
        raise
    return _pairing_from_row(row)


def update_pairing(
    dal: Any,
    community_id: int,
    pairing_id: int,
    *,
    sync_enabled: bool | None = None,
    direction: str | None = None,
    role_name_prefix: str | None = None,
) -> GuildPairing:
    """Partially update a pairing's opt-in toggle / sync direction / role-name prefix.

    Raises `not_found()` if `pairing_id` doesn't exist under `community_id`.
    """
    if direction is not None:
        direction = _validate_direction(direction)
    if role_name_prefix is not None:
        role_name_prefix = _validate_role_name_prefix(role_name_prefix)

    _ensure_tables(dal)
    t = dal.guild_tenant_pairings
    try:
        existing = dal((t.id == pairing_id) & (t.community_id == community_id)).select().first()
        if existing is None:
            raise not_found(f"no pairing {pairing_id} for community {community_id}")

        updates: dict[str, Any] = {"updated_at": datetime.now(UTC)}
        if sync_enabled is not None:
            updates["sync_enabled"] = bool(sync_enabled)
        if direction is not None:
            updates["direction"] = direction
        if role_name_prefix is not None:
            updates["role_name_prefix"] = role_name_prefix

        dal(t.id == pairing_id).update(**updates)
        row = dal(t.id == pairing_id).select().first()
        dal.commit()
    except Exception:
        dal.rollback()
        raise
    return _pairing_from_row(row)


def delete_pairing(dal: Any, community_id: int, pairing_id: int) -> bool:
    """Delete a pairing (and, via `ON DELETE CASCADE`, its role-sync bindings).

    Returns `False` (no-op) if there was nothing to delete.
    """
    _ensure_tables(dal)
    t = dal.guild_tenant_pairings
    try:
        existing = dal((t.id == pairing_id) & (t.community_id == community_id)).select().first()
        if existing is None:
            dal.commit()
            return False

        # pydal (unlike real Postgres) doesn't execute the migration's own
        # `ON DELETE CASCADE` against a sqlite:memory test table defined
        # without an explicit reference -- delete bindings explicitly so
        # behavior is identical in both the test fixture and production.
        if "community_role_sync_bindings" in dal.tables:
            dal(dal.community_role_sync_bindings.pairing_id == pairing_id).delete()
        dal(t.id == pairing_id).delete()
        dal.commit()
    except Exception:
        dal.rollback()
        raise
    return True


# ---------------------------------------------------------------------------
# Role-sync bindings
# ---------------------------------------------------------------------------


def list_bindings(dal: Any, community_id: int, pairing_id: int) -> list[RoleSyncBinding]:
    """Return every role-sync binding for `pairing_id`, which must belong to `community_id`.

    Raises `not_found()` if the pairing doesn't exist under `community_id`.
    """
    _ensure_tables(dal)
    if get_pairing(dal, community_id, pairing_id) is None:
        raise not_found(f"no pairing {pairing_id} for community {community_id}")
    b = dal.community_role_sync_bindings
    rows = dal(b.pairing_id == pairing_id).select(orderby=~b.created_at)
    return [_binding_from_row(row) for row in rows]


def create_binding(
    dal: Any,
    community_id: int,
    pairing_id: int,
    *,
    sync_scope: str,
    discord_role_id: str,
    subscriber_tier: int | None = None,
    community_role: str | None = None,
) -> RoleSyncBinding:
    """Create a role-sync binding under `pairing_id` (which must belong to `community_id`).

    At most one binding per subscriber tier, at most one `moderator`
    binding, and at most one `community_role` binding per `discord_role_id`,
    per pairing -- migration 0034/0036's own partial unique indexes are the
    production backstop; this is the clear, typed error path. A
    `discord_role_id` already bound as `subscriber_tier`/`moderator` under
    this pairing may never also be bound as `community_role` (and vice
    versa) -- the DB schema alone cannot express a cross-partial-index
    UNIQUE, so this is the one place that invariant is actually enforced
    (see migration 0036's own docstring on structural loop-prevention).
    """
    _validate_sync_scope_and_tier(sync_scope, subscriber_tier, community_role)
    discord_role_id = _validate_discord_role_id(discord_role_id)

    _ensure_tables(dal)
    if get_pairing(dal, community_id, pairing_id) is None:
        raise not_found(f"no pairing {pairing_id} for community {community_id}")

    b = dal.community_role_sync_bindings
    try:
        cross_direction = dal(
            (b.pairing_id == pairing_id) & (b.discord_role_id == discord_role_id)
        ).select()
        for row in cross_direction:
            is_new_community_role = sync_scope == "community_role"
            is_existing_community_role = row.sync_scope == "community_role"
            if is_new_community_role != is_existing_community_role:
                raise conflict(
                    f"discord role {discord_role_id} is already bound as "
                    f"'{row.sync_scope}' under pairing {pairing_id} -- a role may never "
                    "be bound for both directions under the same pairing"
                )

        query = (b.pairing_id == pairing_id) & (b.sync_scope == sync_scope)
        if sync_scope == "subscriber_tier":
            query &= b.subscriber_tier == subscriber_tier
        elif sync_scope == "community_role":
            query &= b.discord_role_id == discord_role_id
        existing = dal(query).select().first()
        if existing is not None:
            raise conflict(
                f"pairing {pairing_id} already has a '{sync_scope}' "
                f"{'tier ' + str(subscriber_tier) if subscriber_tier else ''}binding".strip()
            )

        now = datetime.now(UTC)
        row_id = b.insert(
            pairing_id=pairing_id,
            sync_scope=sync_scope,
            subscriber_tier=subscriber_tier,
            community_role=community_role,
            discord_role_id=discord_role_id,
            created_at=now,
            updated_at=now,
        )
        row = dal(b.id == row_id).select().first()
        dal.commit()
    except Exception:
        dal.rollback()
        raise
    return _binding_from_row(row)


def delete_binding(dal: Any, community_id: int, pairing_id: int, binding_id: int) -> bool:
    """Delete a role-sync binding scoped to `pairing_id`/`community_id`.

    Returns `False` (no-op) if there was nothing matching to delete.
    """
    _ensure_tables(dal)
    if get_pairing(dal, community_id, pairing_id) is None:
        raise not_found(f"no pairing {pairing_id} for community {community_id}")

    b = dal.community_role_sync_bindings
    try:
        existing = dal((b.id == binding_id) & (b.pairing_id == pairing_id)).select().first()
        if existing is None:
            dal.commit()
            return False
        dal(b.id == binding_id).delete()
        dal.commit()
    except Exception:
        dal.rollback()
        raise
    return True
