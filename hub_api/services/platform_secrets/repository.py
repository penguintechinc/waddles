"""Access to `platform_vault` (migration 0109): marker, secrets, events -- plain SQL, no crypto.

`PlatformVaultRepository` is the narrow interface the bootstrap and the cache use; `SqlRepository`
implements it over one sync SQLAlchemy connection (the same access pattern as hub-api's bootstrap,
which already runs raw SQL on a sync engine inside its advisory lock). Writes are insert-only:

  * secrets are INSERTed with `ON CONFLICT DO NOTHING` (no conflict target, so BOTH the
    (type, scope, version) key and the one-active-per-(type, scope) partial index are covered) and
    the caller re-SELECTs the winner when nothing was returned;
  * there is NO UPDATE of a secret row anywhere in this module -- an active row is never touched at
    bootstrap (the table's triggers would also refuse most updates);
  * events are only ever appended (the table refuses UPDATE/DELETE).

The grants give `waddles_hub_api` no DELETE/TRUNCATE on any of these tables, so none is attempted.
Bound parameters carry the ciphertext; the bootstrap engine is created with `hide_parameters=True`
so a driver error can never echo it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol

from sqlalchemy import text

SCHEMA: Final = "platform_vault"
#: NOTIFY channel hub-api replicas LISTEN on; the payload is always empty (never carries a value).
NOTIFY_CHANNEL: Final = "platform_secrets_changed"

#: `secrets.state` codes.
STATE_PENDING, STATE_ACTIVE, STATE_VERIFY_ONLY, STATE_RETIRED, STATE_DESTROYED = 1, 2, 3, 4, 5
#: `secrets.reason` codes used here.
REASON_GENERATED, REASON_IMPORTED, REASON_RESTORED, REASON_ROTATED, REASON_RETIRED = 1, 2, 3, 4, 5
#: `secret_events.event_type` codes used here.
EVENT_GENERATED, EVENT_IMPORTED, EVENT_RESTORED = 1, 2, 3
EVENT_ACTIVATED, EVENT_VERIFY_ONLY, EVENT_RETIRED, EVENT_DESTROYED = 4, 5, 6, 7
EVENT_ROTATION_REQUESTED = 8
EVENT_DRIFT, EVENT_ESCROWED = 10, 11
#: Operator reason codes (migration 0115): why a rotation / retirement was requested.
REASON_OP_SCHEDULED, REASON_OP_COMPROMISE, REASON_OP_MAINTENANCE = 8, 9, 10
#: `rotation_requests.action` codes.
ACTION_ROTATE, ACTION_RETIRE = 1, 2
#: `secret_events.outcome` codes.
OUTCOME_SUCCESS, OUTCOME_FAILURE = 1, 2


@dataclass(slots=True, frozen=True)
class MarkerRow:
    """The one `root_marker` row: the KEK id and its keyed MAC (never the KEK)."""

    kek_id: str
    check_mac: bytes = field(repr=False)


@dataclass(slots=True, frozen=True)
class AnchorRow:
    """The escrow freshness anchor kept in `root_marker` (outside the bucket, KEK-MAC'd).

    Highest manifest generation ever written + the digest of that manifest. A restore refuses any
    manifest older than this, or the same generation with another digest (replay / tamper).
    """

    generation: int
    manifest_digest: bytes = field(repr=False)
    kek_id: str
    mac: bytes = field(repr=False)


@dataclass(slots=True, frozen=True)
class SecretRow:
    """One `secrets` row. `sealed_value` / `fingerprint` are hidden from repr (never logged)."""

    id: int
    public_id: uuid.UUID
    secret_type: int
    scope_id: int | None
    version: int
    state: int
    algorithm: int
    sealed_value: bytes | None = field(repr=False)
    kek_id: str
    seal_version: int
    kid: str | None
    public_material: bytes | None
    fingerprint: bytes = field(repr=False)
    activated_at: datetime | None
    rotated_at: datetime | None
    retired_at: datetime | None


@dataclass(slots=True, frozen=True)
class NewSecret:
    """Everything an INSERT of a brand-new ACTIVE secret needs."""

    public_id: uuid.UUID
    secret_type: int
    scope_id: int | None
    version: int
    algorithm: int
    sealed_value: bytes = field(repr=False)
    kek_id: str
    seal_version: int
    kid: str | None
    public_material: bytes | None
    fingerprint: bytes = field(repr=False)
    reason: int
    activated_at: datetime
    actor_uuid: uuid.UUID | None = None


@dataclass(slots=True, frozen=True)
class EventRow:
    """One `secret_events` row (all columns the chain hash covers, plus the chain links)."""

    public_id: uuid.UUID
    secret_id: int | None
    secret_type: int
    scope_id: int | None
    version: int | None
    event_type: int
    outcome: int
    reason: int | None
    actor_uuid: uuid.UUID | None
    occurred_at: datetime
    prev_chain_hash: bytes | None
    chain_hash: bytes


class PlatformVaultRepository(Protocol):
    """What bootstrap / cache need from storage (the SQL class and a test fake implement it)."""

    def read_marker(self) -> MarkerRow | None:
        """The root marker, or None before the first run."""

    def insert_marker(self, kek_id: str, check_mac: bytes) -> bool:
        """Insert the marker if absent; True when this call wrote it, False when it existed."""

    def count_secrets(self) -> int:
        """Number of rows in `secrets` (any state)."""

    def rows_for(self, secret_type: int, scope_id: int | None) -> list[SecretRow]:
        """Every row of one (type, scope), ascending version."""

    def openable_rows(self) -> list[SecretRow]:
        """Every active and verify-only row (what the cache serves), ascending id."""

    def insert_secret(self, new: NewSecret) -> SecretRow | None:
        """INSERT ... ON CONFLICT DO NOTHING; the new row, or None when something conflicted."""

    def update_state(
        self, secret_id: int, state: int, *, at: datetime, reason: int, actor_uuid: uuid.UUID | None
    ) -> bool:
        """Move a row FORWARD to `state` (3 verify-only / 4 retired); True when a row changed.

        Stamps `rotated_at` (and `retired_at` for state 4) with `at`. The table's trigger refuses a
        backward move, so a False return means the row was already at or past `state`.
        """

    def read_anchor(self) -> AnchorRow | None:
        """The escrow freshness anchor, or None while no manifest has ever been written."""

    def advance_anchor(
        self, generation: int, manifest_digest: bytes, kek_id: str, mac: bytes
    ) -> bool:
        """Raise the anchor to `generation` (strictly higher only); True when it moved."""

    def claim_request(
        self, idempotency_key: uuid.UUID, *, secret_type: int, action: int, actor_uuid: uuid.UUID
    ) -> bool:
        """Claim an `Idempotency-Key`; True when new, False when it was already used (a replay)."""

    def chain_tip(self) -> bytes | None:
        """`chain_hash` of the newest event, or None for an empty chain."""

    def insert_event(self, event: EventRow) -> None:
        """Append one event (the chain fields are already computed)."""

    def events(self) -> list[EventRow]:
        """Every event in chain (id) order."""

    def notify_changed(self) -> None:
        """Send an empty NOTIFY on :data:`NOTIFY_CHANNEL`."""


_VAULT_COLUMNS: Final = (
    "id, public_id, secret_type, scope_id, version, state, algorithm, sealed_value, kek_id, "
    "seal_version, kid, public_material, fingerprint, activated_at, rotated_at, retired_at"  # noqa: S105
)
_EVENT_COLUMNS: Final = (
    "public_id, secret_id, secret_type, scope_id, version, event_type, outcome, reason, "
    "actor_uuid, occurred_at, prev_chain_hash, chain_hash"
)


def _bytes(value: Any) -> bytes | None:
    """Normalise a driver BYTEA (memoryview / bytes / None) to bytes."""
    return None if value is None else bytes(value)


def _uuid(value: Any) -> uuid.UUID | None:
    """Normalise a driver UUID (UUID or str) to :class:`uuid.UUID`."""
    return None if value is None else uuid.UUID(str(value))


def _secret_row(row: Any) -> SecretRow:
    """Build a :class:`SecretRow` from a result row in `_VAULT_COLUMNS` order."""
    return SecretRow(
        id=row[0],
        public_id=uuid.UUID(str(row[1])),
        secret_type=row[2],
        scope_id=row[3],
        version=row[4],
        state=row[5],
        algorithm=row[6],
        sealed_value=_bytes(row[7]),
        kek_id=row[8],
        seal_version=row[9],
        kid=row[10],
        public_material=_bytes(row[11]),
        fingerprint=bytes(row[12]),
        activated_at=row[13],
        rotated_at=row[14],
        retired_at=row[15],
    )


def _event_row(row: Any) -> EventRow:
    """Build an :class:`EventRow` from a result row in `_EVENT_COLUMNS` order."""
    return EventRow(
        public_id=uuid.UUID(str(row[0])),
        secret_id=row[1],
        secret_type=row[2],
        scope_id=row[3],
        version=row[4],
        event_type=row[5],
        outcome=row[6],
        reason=row[7],
        actor_uuid=_uuid(row[8]),
        occurred_at=row[9],
        prev_chain_hash=_bytes(row[10]),
        chain_hash=bytes(row[11]),
    )


class SqlRepository:
    """:class:`PlatformVaultRepository` over one sync SQLAlchemy connection.

    The caller owns the transaction (commit / rollback) and the advisory lock; this class issues
    statements only. Every statement is a fixed literal with bound parameters.
    """

    def __init__(self, conn: Any) -> None:
        """Hold the connection (not owned: the caller commits and closes it)."""
        self._conn = conn

    def read_marker(self) -> MarkerRow | None:
        """The singleton root marker row, or None."""
        row = self._conn.execute(
            text(f"SELECT kek_id, check_mac FROM {SCHEMA}.root_marker WHERE id = 1")  # nosec B608  # noqa: S608
        ).first()
        return None if row is None else MarkerRow(row[0], bytes(row[1]))

    def insert_marker(self, kek_id: str, check_mac: bytes) -> bool:
        """`INSERT ... ON CONFLICT DO NOTHING`; rowcount 1 means this call wrote the marker."""
        result = self._conn.execute(
            text(
                f"INSERT INTO {SCHEMA}.root_marker (id, kek_id, check_mac) "  # nosec B608  # noqa: S608
                "VALUES (1, :kek_id, :mac) ON CONFLICT DO NOTHING"
            ),
            {"kek_id": kek_id, "mac": check_mac},
        )
        return bool(result.rowcount == 1)

    def count_secrets(self) -> int:
        """Row count of `secrets`."""
        return int(
            self._conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.secrets")).scalar() or 0  # nosec B608  # noqa: S608
        )

    def rows_for(self, secret_type: int, scope_id: int | None) -> list[SecretRow]:
        """All versions of one (type, scope); NULL scope matches NULL (global)."""
        rows = self._conn.execute(
            text(
                f"SELECT {_VAULT_COLUMNS} FROM {SCHEMA}.secrets "  # nosec B608  # noqa: S608
                "WHERE secret_type = :t AND scope_id IS NOT DISTINCT FROM :s ORDER BY version"
            ),
            {"t": secret_type, "s": scope_id},
        ).all()
        return [_secret_row(r) for r in rows]

    def openable_rows(self) -> list[SecretRow]:
        """Active and verify-only rows, the set the cache serves."""
        rows = self._conn.execute(
            text(
                f"SELECT {_VAULT_COLUMNS} FROM {SCHEMA}.secrets "  # nosec B608  # noqa: S608
                "WHERE state IN (2, 3) ORDER BY id"
            )
        ).all()
        return [_secret_row(r) for r in rows]

    def insert_secret(self, new: NewSecret) -> SecretRow | None:
        """Insert an ACTIVE row; `ON CONFLICT DO NOTHING` -> None on any unique conflict."""
        row = self._conn.execute(
            text(
                f"INSERT INTO {SCHEMA}.secrets (public_id, secret_type, scope_id, version, state, "  # nosec B608  # noqa: S608
                "algorithm, sealed_value, kek_id, seal_version, kid, public_material, "
                "fingerprint, reason, activated_at, actor_uuid) "
                "VALUES (CAST(:public_id AS uuid), :t, :s, :v, 2, :alg, :sealed, :kek, :sv, "
                ":kid, :pub, :fp, :reason, :at, CAST(:actor AS uuid)) "
                f"ON CONFLICT DO NOTHING RETURNING {_VAULT_COLUMNS}"
            ),
            {
                "public_id": str(new.public_id),
                "t": new.secret_type,
                "s": new.scope_id,
                "v": new.version,
                "alg": new.algorithm,
                "sealed": new.sealed_value,
                "kek": new.kek_id,
                "sv": new.seal_version,
                "kid": new.kid,
                "pub": new.public_material,
                "fp": new.fingerprint,
                "reason": new.reason,
                "at": new.activated_at,
                "actor": None if new.actor_uuid is None else str(new.actor_uuid),
            },
        ).first()
        return None if row is None else _secret_row(row)

    def update_state(
        self, secret_id: int, state: int, *, at: datetime, reason: int, actor_uuid: uuid.UUID | None
    ) -> bool:
        """Forward-only state move; `WHERE state < :state` keeps it idempotent and race-safe."""
        result = self._conn.execute(
            text(
                f"UPDATE {SCHEMA}.secrets SET state = :state, "  # nosec B608  # noqa: S608
                "rotated_at = COALESCE(rotated_at, :at), "
                "retired_at = CASE WHEN :state = 4 THEN COALESCE(retired_at, :at) "
                "ELSE retired_at END, "
                "reason = :reason, actor_uuid = CAST(:actor AS uuid) "
                "WHERE id = :id AND state < :state AND state IN (2, 3)"
            ),
            {
                "state": state,
                "at": at,
                "reason": reason,
                "actor": None if actor_uuid is None else str(actor_uuid),
                "id": secret_id,
            },
        )
        return bool(result.rowcount == 1)

    def read_anchor(self) -> AnchorRow | None:
        """The anchor columns of the singleton marker row; None while generation is 0."""
        row = self._conn.execute(
            text(
                "SELECT escrow_generation, escrow_manifest_hash, escrow_anchor_kek_id, "  # nosec B608  # noqa: S608
                f"escrow_anchor_mac FROM {SCHEMA}.root_marker WHERE id = 1"
            )
        ).first()
        if row is None or not row[0]:
            return None
        return AnchorRow(int(row[0]), bytes(row[1]), str(row[2]), bytes(row[3]))

    def advance_anchor(
        self, generation: int, manifest_digest: bytes, kek_id: str, mac: bytes
    ) -> bool:
        """Forward-only: `WHERE escrow_generation < :g` (the trigger also forbids going back)."""
        result = self._conn.execute(
            text(
                f"UPDATE {SCHEMA}.root_marker SET escrow_generation = :g, "  # nosec B608  # noqa: S608
                "escrow_manifest_hash = :h, escrow_anchor_kek_id = :k, escrow_anchor_mac = :m, "
                "updated_at = now() WHERE id = 1 AND escrow_generation < :g"
            ),
            {"g": generation, "h": manifest_digest, "k": kek_id, "m": mac},
        )
        return bool(result.rowcount == 1)

    def claim_request(
        self, idempotency_key: uuid.UUID, *, secret_type: int, action: int, actor_uuid: uuid.UUID
    ) -> bool:
        """`INSERT ... ON CONFLICT DO NOTHING` on the ledger's unique key; 1 row = first use."""
        result = self._conn.execute(
            text(
                f"INSERT INTO {SCHEMA}.rotation_requests "  # nosec B608  # noqa: S608
                "(idempotency_key, secret_type, action, actor_uuid) "
                "VALUES (CAST(:k AS uuid), :t, :a, CAST(:actor AS uuid)) ON CONFLICT DO NOTHING"
            ),
            {"k": str(idempotency_key), "t": secret_type, "a": action, "actor": str(actor_uuid)},
        )
        return bool(result.rowcount == 1)

    def chain_tip(self) -> bytes | None:
        """Newest event's chain hash (events are linear, so highest id)."""
        value = self._conn.execute(
            text(f"SELECT chain_hash FROM {SCHEMA}.secret_events ORDER BY id DESC LIMIT 1")  # nosec B608  # noqa: S608
        ).scalar()
        return _bytes(value)

    def insert_event(self, event: EventRow) -> None:
        """Append one pre-hashed event."""
        self._conn.execute(
            text(
                f"INSERT INTO {SCHEMA}.secret_events ({_EVENT_COLUMNS}) "  # nosec B608  # noqa: S608
                "VALUES (CAST(:pid AS uuid), :sid, :t, :s, :v, :et, :oc, :rs, "
                "CAST(:actor AS uuid), :at, :prev, :hash)"
            ),
            {
                "pid": str(event.public_id),
                "sid": event.secret_id,
                "t": event.secret_type,
                "s": event.scope_id,
                "v": event.version,
                "et": event.event_type,
                "oc": event.outcome,
                "rs": event.reason,
                "actor": None if event.actor_uuid is None else str(event.actor_uuid),
                "at": event.occurred_at,
                "prev": event.prev_chain_hash,
                "hash": event.chain_hash,
            },
        )

    def events(self) -> list[EventRow]:
        """The whole chain in order."""
        rows = self._conn.execute(
            text(f"SELECT {_EVENT_COLUMNS} FROM {SCHEMA}.secret_events ORDER BY id")  # nosec B608  # noqa: S608
        ).all()
        return [_event_row(r) for r in rows]

    def notify_changed(self) -> None:
        """`pg_notify` with an empty payload (delivered on commit)."""
        self._conn.execute(text("SELECT pg_notify(:ch, '')"), {"ch": NOTIFY_CHANNEL})
