"""Keystore schema: `tenant_encryption_keys` + `key_tombstones` (tenant-DEK broker).

Per `docs/superpowers/specs/2026-09-28-tenant-envelope-encryption-design.md`
Sec4/Sec7 step 1: key material lives in a **separate `keystore` schema**,
own role, own (short-retention) backup policy -- never the same
schema/backup set as the tenant-data tables the DEKs protect. This is
what makes crypto-shredding final instead of "final until the next data-DB
restore".

`wrapped_dek` is the tenant DEK (256-bit, CSPRNG) wrapped under the root
KEK (`kek_ref`/`kek_kind` -- platform k8s-Secret KEK today, pluggable
customer-KMS `kek_ref` for Enterprise BYOK later, no schema change
needed). `key_tombstones` is the durable, append-only crypto-shred record
(Sec4) -- a restore of this schema MUST reconcile against it before
serving traffic; that reconciliation is an operational runbook step, not
something this migration can enforce.

NOTE: this repo's `alembic/versions/` head at branch-cut time was
`0026_app_install_approval_source` -- the `0034`/next-number instruction
in this feature's task brief assumes other in-flight numbered migrations
(e.g. the sibling `connections-credentials-design` work) that hadn't
landed on this branch yet. Numbered `0027` here to stay contiguous with
what actually exists on `release/v3.0.X`; **renumber this file if it
collides with another migration merged first.**

RBAC (rbac-matrix.yaml, out of scope for this migration file itself):
hub-api's dedicated key-store role gets SELECT/INSERT/UPDATE on both
tables in this schema; every other role (`waddles_bundle_reader`,
svc_ingest/svc_action/svc_process, webui, migration_runner) gets
`privileges: []` -- identical posture to `connection_credentials`,
extended to the schema boundary.

Revision ID: 0027_keystore_tenant_encryption_keys
Revises: 0026_app_install_approval_source
Create Date: 2026-09-28
"""

from __future__ import annotations

from alembic import op

revision = "0027_keystore_tenant_encryption_keys"
down_revision = "0026_app_install_approval_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the `keystore` schema, `tenant_encryption_keys`, and `key_tombstones`."""
    op.execute("CREATE SCHEMA IF NOT EXISTS keystore")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS keystore.tenant_encryption_keys (
            id            BIGSERIAL PRIMARY KEY,
            tenant_id     INTEGER NOT NULL,
            dek_version   INTEGER NOT NULL,
            wrapped_dek   BYTEA,
            kek_ref       VARCHAR(255) NOT NULL,
            kek_kind      VARCHAR(20) NOT NULL DEFAULT 'platform'
                CHECK (kek_kind IN ('platform', 'customer_kms')),
            status        VARCHAR(20) NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'retired', 'destroyed')),
            usage_count   BIGINT NOT NULL DEFAULT 0,
            activated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            retired_at    TIMESTAMPTZ,
            destroyed_at  TIMESTAMPTZ,
            UNIQUE (tenant_id, dek_version)
        )
        """
    )
    # wrapped_dek is nullable at the column level only so a `destroyed` row
    # can null it out on shred (Sec4: "delete wrapped_dek from the key
    # store") without a DDL step in the hot shred path -- enforced NOT NULL
    # for any non-destroyed row via CHECK below.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_tenant_encryption_keys_wrapped_dek_present'
                  AND connamespace = 'keystore'::regnamespace
            ) THEN
                ALTER TABLE keystore.tenant_encryption_keys
                  ADD CONSTRAINT ck_tenant_encryption_keys_wrapped_dek_present
                  CHECK (status = 'destroyed' OR wrapped_dek IS NOT NULL);
            END IF;
        END $$
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_tenant_encryption_keys_tenant_active
          ON keystore.tenant_encryption_keys (tenant_id)
          WHERE status = 'active'
        """
    )

    # Append-only crypto-shred tombstone (Sec4) -- source of truth across a
    # key-store restore, independent of the `destroyed` status flag above.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS keystore.key_tombstones (
            id           BIGSERIAL PRIMARY KEY,
            tenant_id    INTEGER NOT NULL,
            dek_version  INTEGER NOT NULL,
            shredded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reason       VARCHAR(255)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_key_tombstones_tenant
          ON keystore.key_tombstones (tenant_id)
        """
    )


def downgrade() -> None:
    """Drop `key_tombstones`, `tenant_encryption_keys`, and the `keystore` schema."""
    op.execute("DROP TABLE IF EXISTS keystore.key_tombstones")
    op.execute("DROP TABLE IF EXISTS keystore.tenant_encryption_keys")
    op.execute("DROP SCHEMA IF EXISTS keystore")
