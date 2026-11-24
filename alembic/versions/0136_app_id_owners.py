"""`app_id_owners`: atomic first-claim ownership + unique storage key per bundle app_id.

Closes two security-review findings on the app_id cutover:

* **Ownership-lock race.** The vendor ownership lock was check-then-insert against
  `app_version_uploads`, so two publishers uploading the same fresh app_id concurrently could
  both pass the check. `app_id` is now the PRIMARY KEY here and a claim is a single
  `INSERT ... ON CONFLICT DO NOTHING RETURNING`, so exactly one caller wins.
* **Storage-key collision.** `storage_key` (`bundle_storage_key(app_id)`: readable prefix +
  `sha256(app_id)[:16]`) is the table/kv/object namespace for the bundle; the UNIQUE constraint
  makes the database refuse a second app_id that would land on an existing key.

`owner_id` is NULL for the system actor (seeded first-party / platform-hosted bundles). No
backfill: the app_id cutover is pre-first-release and alpha is reseeded.

Revision ID: 0136_app_id_owners
Revises: 0135_token_debit_idempotency
Create Date: 2026-11-20
"""

from __future__ import annotations

from alembic import op

revision = "0136_app_id_owners"
down_revision = "0135_token_debit_idempotency"
branch_labels = None
depends_on = None

UPGRADE_SQL = (
    """
CREATE TABLE IF NOT EXISTS app_id_owners (
    app_id      VARCHAR(127) PRIMARY KEY,
    owner_id    INTEGER,
    storage_key VARCHAR(63)  NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_app_id_owners_storage_key UNIQUE (storage_key)
)
""",
)


def upgrade() -> None:
    """Create `app_id_owners` (idempotent)."""
    for stmt in UPGRADE_SQL:
        op.execute(stmt)


def downgrade() -> None:
    """Drop `app_id_owners`."""
    op.execute("DROP TABLE IF EXISTS app_id_owners")
