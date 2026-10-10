"""Bundle economy idempotency keys (#751 money-safety review): no double-credit.

A retried or replayed economy call (a spine event redelivered after a crash, a
host-call retried over a flaky wire) must credit ONCE. Every money-moving call
now carries a host-derived idempotency key, stored on the ledger row that
originates the movement (`wager`, or a transfer's `transfer_out`) under a
partial UNIQUE index scoped to `(tenant, community, app)`; the store answers a
repeat of the same call from that row and refuses the same key with different
parameters (`core/bundle_host_economy`).

Purely additive and idempotent: one nullable column (existing rows keep NULL),
a shape CHECK, and the partial unique index. No new role or privilege -- the
runtime role's table-level `INSERT` on `economy_ledger` already covers the new
column, and the ledger stays append-only (no UPDATE/DELETE).

The DDL lives in `scripts/db/bundle_economy_idempotency.sql` (copied into the
migrations image, and `include_str!`'d by the Rust integration tests) so the
shipped schema and the tested schema cannot drift.

Revision ID: 0052_economy_idempotency
Revises: 0051_bundle_identity_resolve
Create Date: 2026-10-10
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0052_economy_idempotency"
down_revision = "0051_bundle_identity_resolve"
branch_labels = None
depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "db"
    / "bundle_economy_idempotency.sql"
)


def upgrade() -> None:
    """Add the idempotency column, its shape CHECK and the partial unique index."""
    # exec_driver_sql: the file is a multi-statement script with a `$$` block
    # and quotes; bypass SQLAlchemy `text()` bind-parameter parsing entirely.
    op.get_bind().exec_driver_sql(_SQL_PATH.read_text(encoding="utf-8"))


def downgrade() -> None:
    """Drop the index, CHECK and column (keys are a replay aid, not audit data)."""
    op.execute("DROP INDEX IF EXISTS uq_economy_ledger_idempotency")
    op.execute(
        "ALTER TABLE economy_ledger "
        "DROP CONSTRAINT IF EXISTS economy_ledger_idempotency_key_shape"
    )
    op.execute("ALTER TABLE economy_ledger DROP COLUMN IF EXISTS idempotency_key")
