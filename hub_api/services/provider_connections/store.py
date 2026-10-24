"""Persistence for the connection framework: penguin-dal's reflected tables, one transaction each.

Storage is the three-layer model from migration 0035, extended by 0058 (see
``docs/CONNECTION_MODEL.local.md``):

* ``platform_connections`` (layer 2) -- the link itself: provider, resource, status, expiry,
  adapter metadata, and the **only** place a token lives -- as AES-GCM ciphertext
  (``SealedSecrets``).
* ``community_connection_access`` (layer 3) -- which community may use a connection, whether it is
  approved, and the per-community ``enabled`` switch. No credentials.

Isolation is structural, not a convention: every admin-facing query joins through the *grant* for
``community_id`` **and** filters ``platform_connections.tenant_id`` to the caller's tenant, so a row
is unreachable unless both match. Tokens are never selected by any admin-facing query -- the
read models carry ``has_refresh_token`` only -- and ciphertext is loaded solely by the two
data-plane resolve functions at the bottom.

Multi-statement writes run in a single ``engine.begin()`` transaction (penguin-dal auto-commits per
statement, which cannot express "insert the connection and its grant atomically" or the
refresh-under-lock the data-plane resolve needs). Only reflected ``Table`` objects and bound
parameters are used -- no string-built SQL.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Table, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from services.provider_connections.errors import (
    ConnectionConflict,
    ConnectionNotFound,
    SchemaMissing,
)

GRANT_APPROVED: Final = "approved"
GRANT_REVOKED: Final = "revoked"
STATUS_ACTIVE: Final = "active"

_REQUIRED_CONNECTION_COLUMNS: Final = (
    "resource_metadata",
    "expires_at",
    "status_reason",
    "public_id",
    "seal_version",
)


@dataclass(slots=True, frozen=True)
class SealedSecrets:
    """Ciphertext of a connection's tokens, exactly as stored. Excluded from ``repr``."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    #: 0 = legacy no-AAD seal, 1 = AAD-bound (see ``vault``). Required: a caller must say which,
    #: so forgetting it can never silently mark a ciphertext AAD-bound.
    seal_version: int


@dataclass(slots=True, frozen=True)
class ConnectionRow:
    """Layer 2, secrets stripped: ``has_refresh_token`` replaces the token columns."""

    id: int
    public_id: UUID
    tenant_id: int
    platform: str
    resource_type: str
    resource_id: str
    status: str
    status_reason: str | None
    resource_metadata: dict[str, Any]
    expires_at: datetime | None
    has_refresh_token: bool
    installed_by_user_id: int | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(slots=True, frozen=True)
class GrantRow:
    """Layer 3: one community's access to a connection."""

    id: int
    community_id: int
    connection_id: int
    status: str
    enabled: bool
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(slots=True, frozen=True)
class Binding:
    """A connection as one community sees it. ``shared_with`` counts *other* live grants."""

    connection: ConnectionRow
    grant: GrantRow
    shared_with: int


@dataclass(slots=True, frozen=True)
class ResolveState:
    """Everything the data-plane resolve needs, including ciphertext (never leaves the service)."""

    connection: ConnectionRow
    grant: GrantRow
    secrets: SealedSecrets


@dataclass(slots=True, frozen=True)
class DeleteOutcome:
    """Result of removing a community's connection: whether the shared link itself was purged."""

    purged_connection: bool


@dataclass(slots=True, frozen=True)
class _Tables:
    connections: Table
    grants: Table
    communities: Table


def _tables(db: Any) -> _Tables:
    """Resolve the reflected tables, failing loudly if a migration has not been applied."""
    tables = db.metadata.tables
    missing = [
        name
        for name in ("platform_connections", "community_connection_access", "communities")
        if name not in tables
    ]
    if missing:
        raise SchemaMissing(f"table(s) not present: {', '.join(missing)} -- run migrations")
    connections = tables["platform_connections"]
    grants = tables["community_connection_access"]
    absent = [name for name in _REQUIRED_CONNECTION_COLUMNS if name not in connections.c]
    if "enabled" not in grants.c:
        absent.append("enabled")
    if absent:
        raise SchemaMissing(
            f"column(s) not present: {', '.join(absent)} -- run migrations "
            "0058_provider_connections and 0063_seal_aad_version"
        )
    return _Tables(connections=connections, grants=grants, communities=tables["communities"])


def _aware(value: datetime | None) -> datetime | None:
    """Normalise a driver datetime to timezone-aware UTC (sqlite returns naive values)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _as_dict(value: Any) -> dict[str, Any]:
    """Decode a JSON column that some drivers hand back as text."""
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value) if isinstance(value, Mapping) else {}


def _connection_columns(t: _Tables) -> list[Any]:
    """Explicit non-secret ``platform_connections`` column list (never a bare table select)."""
    c = t.connections.c
    return [
        c.id,
        c.public_id,
        c.tenant_id,
        c.platform,
        c.resource_type,
        c.resource_id,
        c.status,
        c.status_reason,
        c.resource_metadata,
        c.expires_at,
        c.refresh_token.is_not(None).label("has_refresh_token"),
        c.installed_by_user_id,
        c.created_at,
        c.updated_at,
    ]


def _grant_columns(t: _Tables) -> list[Any]:
    """Explicit ``community_connection_access`` column list, labelled to avoid name clashes."""
    g = t.grants.c
    return [
        g.id.label("grant_id"),
        g.community_id,
        g.connection_id,
        g.status.label("grant_status"),
        g.enabled,
        g.created_at.label("grant_created_at"),
        g.updated_at.label("grant_updated_at"),
    ]


def _connection_from(row: Any) -> ConnectionRow:
    return ConnectionRow(
        id=int(row.id),
        public_id=UUID(str(row.public_id)),
        tenant_id=int(row.tenant_id),
        platform=row.platform,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        status=row.status,
        status_reason=row.status_reason,
        resource_metadata=_as_dict(row.resource_metadata),
        expires_at=_aware(row.expires_at),
        has_refresh_token=bool(row.has_refresh_token),
        installed_by_user_id=(
            int(row.installed_by_user_id) if row.installed_by_user_id is not None else None
        ),
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def _grant_from(row: Any) -> GrantRow:
    return GrantRow(
        id=int(row.grant_id),
        community_id=int(row.community_id),
        connection_id=int(row.connection_id),
        status=row.grant_status,
        enabled=bool(row.enabled),
        created_at=_aware(row.grant_created_at),
        updated_at=_aware(row.grant_updated_at),
    )


def _joined(t: _Tables, columns: list[Any]) -> Any:
    """``SELECT columns FROM grants JOIN connections`` -- the only shape admin reads use."""
    return select(*columns).select_from(
        t.grants.join(t.connections, t.connections.c.id == t.grants.c.connection_id)
    )


async def _other_grant_counts(
    conn: AsyncConnection, t: _Tables, connection_ids: list[int], community_id: int
) -> dict[int, int]:
    """Per connection id, how many *other* communities hold a non-revoked grant."""
    if not connection_ids:
        return {}
    g = t.grants.c
    result = await conn.execute(
        select(g.connection_id, func.count().label("n"))
        .where(
            g.connection_id.in_(connection_ids),
            g.community_id != community_id,
            g.status != GRANT_REVOKED,
        )
        .group_by(g.connection_id)
    )
    return {int(row.connection_id): int(row.n) for row in result}


async def _binding(
    conn: AsyncConnection,
    t: _Tables,
    *,
    tenant_id: int,
    community_id: int,
    connection_id: int,
    for_update: bool = False,
) -> Binding | None:
    """One binding, or ``None`` if this community has no live grant on this tenant's connection."""
    stmt = _joined(t, [*_connection_columns(t), *_grant_columns(t)]).where(
        t.grants.c.community_id == community_id,
        t.grants.c.connection_id == connection_id,
        t.grants.c.status != GRANT_REVOKED,
        t.connections.c.tenant_id == tenant_id,
    )
    if for_update:
        stmt = stmt.with_for_update(of=t.connections)
    row = (await conn.execute(stmt)).first()
    if row is None:
        return None
    others = await _other_grant_counts(conn, t, [connection_id], community_id)
    return Binding(_connection_from(row), _grant_from(row), others.get(connection_id, 0))


async def community_tenant_id(db: Any, community_id: int) -> int | None:
    """The tenant a community belongs to, or ``None`` if it does not exist."""
    t = _tables(db)
    async with db.engine.connect() as conn:
        value = (
            await conn.execute(
                select(t.communities.c.tenant_id).where(t.communities.c.id == community_id)
            )
        ).scalar_one_or_none()
    return int(value) if value is not None else None


async def insert_connection(
    db: Any,
    *,
    tenant_id: int,
    community_id: int,
    platform: str,
    resource_type: str,
    resource_id: str,
    public_id: UUID,
    secrets: SealedSecrets,
    expires_at: datetime | None,
    resource_metadata: Mapping[str, Any],
    installed_by_user_id: int | None,
    enabled: bool,
    now: datetime,
) -> Binding:
    """Atomically create the link (layer 2) and this community's approved grant (layer 3).

    Re-verifies in the same transaction that ``community_id`` belongs to ``tenant_id``, and refuses
    (:class:`ConnectionConflict`) to overwrite an existing link for the same resource: the unique
    key ``(tenant, platform, resource)`` means someone already installed it, and silently replacing
    their tokens would be a takeover, not a create.

    ``public_id`` is minted by the caller *before* sealing because it is part of the tokens' AAD
    (finding M4); the row is stored with it and with the secrets' ``seal_version``.
    """
    t = _tables(db)
    c = t.connections.c
    async with db.engine.begin() as conn:
        owner = (
            await conn.execute(
                select(t.communities.c.tenant_id).where(t.communities.c.id == community_id)
            )
        ).scalar_one_or_none()
        if owner is None or int(owner) != tenant_id:
            raise ConnectionNotFound("community not found")
        existing = (
            await conn.execute(
                select(c.id).where(
                    c.tenant_id == tenant_id, c.platform == platform, c.resource_id == resource_id
                )
            )
        ).first()
        if existing is not None:
            raise ConnectionConflict("that resource is already connected for this tenant")
        try:
            result = await conn.execute(
                insert(t.connections).values(
                    tenant_id=tenant_id,
                    platform=platform,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    public_id=str(public_id),
                    access_token=secrets.access_token,
                    refresh_token=secrets.refresh_token,
                    seal_version=secrets.seal_version,
                    status=STATUS_ACTIVE,
                    status_reason=None,
                    resource_metadata=dict(resource_metadata),
                    expires_at=expires_at,
                    installed_by_user_id=installed_by_user_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            connection_id = int(result.inserted_primary_key[0])
            await conn.execute(
                insert(t.grants).values(
                    community_id=community_id,
                    connection_id=connection_id,
                    status=GRANT_APPROVED,
                    enabled=enabled,
                    requested_by_user_id=installed_by_user_id,
                    approved_by_user_id=installed_by_user_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        except IntegrityError as exc:
            raise ConnectionConflict("the connection could not be stored (conflict)") from exc
        binding = await _binding(
            conn, t, tenant_id=tenant_id, community_id=community_id, connection_id=connection_id
        )
    if binding is None:  # pragma: no cover - the rows were inserted one statement ago
        raise ConnectionNotFound("connection not found")
    return binding


async def list_bindings(db: Any, *, tenant_id: int, community_id: int) -> list[Binding]:
    """Every live binding for ``community_id`` on connections of ``tenant_id``, oldest first."""
    t = _tables(db)
    stmt = (
        _joined(t, [*_connection_columns(t), *_grant_columns(t)])
        .where(
            t.grants.c.community_id == community_id,
            t.grants.c.status != GRANT_REVOKED,
            t.connections.c.tenant_id == tenant_id,
        )
        .order_by(t.connections.c.id)
    )
    async with db.engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
        counts = await _other_grant_counts(conn, t, [int(r.id) for r in rows], community_id)
    return [Binding(_connection_from(r), _grant_from(r), counts.get(int(r.id), 0)) for r in rows]


async def get_binding(
    db: Any, *, tenant_id: int, community_id: int, connection_id: int
) -> Binding | None:
    """One binding, tenant- and community-scoped; ``None`` when it is not visible to the caller."""
    t = _tables(db)
    async with db.engine.connect() as conn:
        return await _binding(
            conn, t, tenant_id=tenant_id, community_id=community_id, connection_id=connection_id
        )


async def set_enabled(
    db: Any,
    *,
    tenant_id: int,
    community_id: int,
    connection_id: int,
    enabled: bool,
    now: datetime,
) -> Binding | None:
    """Flip this community's switch only; the link and every other community are untouched."""
    t = _tables(db)
    g = t.grants.c
    visible = select(t.connections.c.id).where(t.connections.c.tenant_id == tenant_id)
    async with db.engine.begin() as conn:
        result = await conn.execute(
            update(t.grants)
            .where(
                g.community_id == community_id,
                g.connection_id == connection_id,
                g.status != GRANT_REVOKED,
                g.connection_id.in_(visible),
            )
            .values(enabled=enabled, updated_at=now)
        )
        if result.rowcount == 0:
            return None
        return await _binding(
            conn, t, tenant_id=tenant_id, community_id=community_id, connection_id=connection_id
        )


async def update_link(
    db: Any,
    *,
    tenant_id: int,
    community_id: int,
    connection_id: int,
    values: Mapping[str, Any],
    now: datetime,
) -> Binding:
    """Change the shared link (layer 2) -- allowed only while this community is its sole user.

    Rotating tokens or repointing the resource would silently change what *other* communities are
    connected to, so a shared link is refused (:class:`ConnectionConflict`) rather than mutated.
    """
    t = _tables(db)
    async with db.engine.begin() as conn:
        binding = await _binding(
            conn,
            t,
            tenant_id=tenant_id,
            community_id=community_id,
            connection_id=connection_id,
            for_update=True,
        )
        if binding is None:
            raise ConnectionNotFound("connection not found")
        if binding.shared_with > 0:
            raise ConnectionConflict(
                "this connection is shared with other communities and cannot be changed here"
            )
        try:
            await conn.execute(
                update(t.connections)
                .where(
                    t.connections.c.id == connection_id,
                    t.connections.c.tenant_id == tenant_id,
                )
                .values(**dict(values), updated_at=now)
            )
        except IntegrityError as exc:
            raise ConnectionConflict("that resource is already connected for this tenant") from exc
        refreshed = await _binding(
            conn, t, tenant_id=tenant_id, community_id=community_id, connection_id=connection_id
        )
    if refreshed is None:  # pragma: no cover - same transaction, row just updated
        raise ConnectionNotFound("connection not found")
    return refreshed


async def delete_for_community(
    db: Any, *, tenant_id: int, community_id: int, connection_id: int
) -> DeleteOutcome | None:
    """Remove this community's grant; purge the link (and its tokens) when nobody else holds one.

    Returns ``None`` when the caller has no such connection. Deleting never touches another
    community's grant, and the ciphertext is gone from the database the moment the last grant is.
    """
    t = _tables(db)
    async with db.engine.begin() as conn:
        binding = await _binding(
            conn,
            t,
            tenant_id=tenant_id,
            community_id=community_id,
            connection_id=connection_id,
            for_update=True,
        )
        if binding is None:
            return None
        await conn.execute(delete(t.grants).where(t.grants.c.id == binding.grant.id))
        remaining = (
            await conn.execute(
                select(func.count()).where(t.grants.c.connection_id == connection_id)
            )
        ).scalar_one()
        purged = int(remaining) == 0
        if purged:
            await conn.execute(
                delete(t.connections).where(
                    t.connections.c.id == connection_id, t.connections.c.tenant_id == tenant_id
                )
            )
    return DeleteOutcome(purged_connection=purged)


async def list_dataplane(
    db: Any, *, platform: str, after_grant_id: int, limit: int
) -> list[Binding]:
    """Usable bindings for ``platform`` across all tenants (operator plane), keyed by grant id.

    "Usable" = the grant is approved **and** enabled and the link is active. No tokens are loaded.
    """
    t = _tables(db)
    stmt = (
        _joined(t, [*_connection_columns(t), *_grant_columns(t)])
        .where(
            t.connections.c.platform == platform,
            t.connections.c.status == STATUS_ACTIVE,
            t.grants.c.status == GRANT_APPROVED,
            t.grants.c.enabled.is_(True),
            t.grants.c.id > after_grant_id,
        )
        .order_by(t.grants.c.id)
        .limit(limit)
    )
    async with db.engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return [Binding(_connection_from(r), _grant_from(r), 0) for r in rows]


def _resolve_columns(t: _Tables) -> list[Any]:
    c = t.connections.c
    return [
        *_connection_columns(t),
        *_grant_columns(t),
        c.access_token,
        c.refresh_token,
        c.seal_version,
    ]


def _resolve_state_from(row: Any) -> ResolveState:
    return ResolveState(
        connection=_connection_from(row),
        grant=_grant_from(row),
        secrets=SealedSecrets(
            access_token=row.access_token,
            refresh_token=row.refresh_token,
            seal_version=int(row.seal_version),
        ),
    )


async def read_resolve_state(
    db: Any, *, community_id: int, connection_id: int
) -> ResolveState | None:
    """Lock-free read of one (community, connection) pair including ciphertext, any status.

    The caller decides usability from the returned statuses so it can report a precise reason.
    Tenant comes from the connection row itself: the caller is the operator-plane data plane, which
    is not bound to a tenant.
    """
    t = _tables(db)
    stmt = _joined(t, _resolve_columns(t)).where(
        t.grants.c.community_id == community_id, t.grants.c.connection_id == connection_id
    )
    async with db.engine.connect() as conn:
        row = (await conn.execute(stmt)).first()
    return _resolve_state_from(row) if row is not None else None


class LockedResolve:
    """A connection row held ``FOR UPDATE`` for the duration of a token refresh."""

    def __init__(self, conn: AsyncConnection, t: _Tables, state: ResolveState) -> None:
        """Bind the open transaction and the row state read under the lock."""
        self._conn = conn
        self._t = t
        self.state = state

    async def save_tokens(
        self, *, secrets: SealedSecrets, expires_at: datetime | None, now: datetime
    ) -> None:
        """Persist a rotated token pair and clear any failure state, in the same transaction."""
        c = self._t.connections.c
        await self._conn.execute(
            update(self._t.connections)
            .where(c.id == self.state.connection.id)
            .values(
                access_token=secrets.access_token,
                refresh_token=secrets.refresh_token,
                seal_version=secrets.seal_version,
                expires_at=expires_at,
                status=STATUS_ACTIVE,
                status_reason=None,
                updated_at=now,
            )
        )

    async def mark_unusable(self, *, status: str, reason: str, now: datetime) -> None:
        """Flag the link ``expired``/``revoked`` with a fixed reason code (tokens are kept)."""
        c = self._t.connections.c
        await self._conn.execute(
            update(self._t.connections)
            .where(c.id == self.state.connection.id)
            .values(status=status, status_reason=reason, updated_at=now)
        )


@asynccontextmanager
async def lock_for_refresh(
    db: Any, *, community_id: int, connection_id: int
) -> AsyncIterator[LockedResolve | None]:
    """Open a transaction holding the connection row ``FOR UPDATE``; yield its current state.

    Concurrent resolvers of the same connection queue here, so the first refreshes and the rest
    re-read the already-rotated token instead of spending a (single-use) refresh token twice --
    which Twitch treats as compromise. Yields ``None`` if the pair no longer exists. The
    transaction commits on a clean exit and rolls back if the body raises.
    """
    t = _tables(db)
    stmt = (
        _joined(t, _resolve_columns(t))
        .where(t.grants.c.community_id == community_id, t.grants.c.connection_id == connection_id)
        .with_for_update(of=t.connections)
    )
    async with db.engine.begin() as conn:
        row = (await conn.execute(stmt)).first()
        yield LockedResolve(conn, t, _resolve_state_from(row)) if row is not None else None


async def reseal_legacy(
    db: Any,
    *,
    connection_id: int,
    rewrap: Callable[[UUID, str, SealedSecrets], SealedSecrets | None],
) -> bool:
    """Upgrade one legacy (``seal_version`` 0) row to AAD-bound under its own row lock.

    ``rewrap(public_id, platform, current)`` returns the re-sealed pair, or ``None`` to leave the
    row untouched (a suspected downgrade -- the caller logs it). The row is locked
    ``FOR UPDATE`` and re-checked, so concurrent callers upgrade it exactly once; returns ``False``
    when the row is gone or already upgraded. ``updated_at`` is left alone: nothing about the
    connection changed, only how its secrets are sealed.
    """
    t = _tables(db)
    c = t.connections.c
    stmt = (
        select(c.id, c.public_id, c.platform, c.access_token, c.refresh_token, c.seal_version)
        .where(c.id == connection_id)
        .with_for_update()
    )
    async with db.engine.begin() as conn:
        row = (await conn.execute(stmt)).first()
        if row is None or int(row.seal_version) != 0:
            return False
        current = SealedSecrets(row.access_token, row.refresh_token, seal_version=0)
        upgraded = rewrap(UUID(str(row.public_id)), row.platform, current)
        if upgraded is None:
            return False
        await conn.execute(
            update(t.connections)
            .where(c.id == connection_id)
            .values(
                access_token=upgraded.access_token,
                refresh_token=upgraded.refresh_token,
                seal_version=upgraded.seal_version,
            )
        )
    return True
