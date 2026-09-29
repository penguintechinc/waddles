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

NOTE: this repo's `alembic/versions/` head as seen on this worktree's
checkout of `release/v3.0.X` was `0026_app_install_approval_source`, but
the chain is actually queued through `0034_upload_abandoned_status`
(other in-flight numbered migrations, e.g. the sibling
`connections-credentials-design` work, not yet visible on this branch).
Numbered `0035` and chained off `0034_upload_abandoned_status` per that
queue; **renumber and re-chain this file if the merged order at PR-merge
time differs from what's assumed here** (i.e. if `0034_upload_abandoned_
status` lands under a different number, or something else lands after it
first).

The id is abbreviated to `0036_keystore_tenant_dek` (not the more
descriptive `..._tenant_encryption_keys`) to stay under
`alembic_version.version_num`'s VARCHAR(32) column -- same constraint
that already shortened 0011's `communities_license_cols` and 0020's
`ingest_sources_rbac` in this same `versions/` directory.

RBAC (rendered from config/postgres/rbac-matrix.yaml at migration-run
time via scripts/db/rbac_matrix.py, per spec D28 -- this file contains
no hand-written GRANT statement, same discipline as 0020): a dedicated
`hub_api_keystore` role gets SELECT/INSERT/UPDATE on both tables in this
schema. Every other role -- including plain `hub_api` itself, not just
the data-plane roles -- gets `privileges: []`; the point of the
dedicated role (spec Sec4 intro) is that even a compromised `hub_api`
application-data credential does not also unlock the key store.

**Amendment 2026-09-28 (post security-review, PR #442):** added
`purpose` column (`tenant_encryption_keys.dek_version` uniqueness is now
scoped `(tenant_id, purpose, dek_version)`, not just `(tenant_id,
dek_version)`) to support the purpose-limited `ingest-stream` DEK
alongside the original `at-rest` DEK -- see
`docs/superpowers/specs/2026-09-28-tenant-envelope-encryption-design.md`
Sec5a. Existing rows (none in practice, since this table and this
migration ship together) default to `at-rest`.

Revision ID: 0036_keystore_tenant_dek
Revises: 0035_upload_abandoned_status
Create Date: 2026-09-28
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0036_keystore_tenant_dek"
down_revision = "0035_upload_abandoned_status"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset({"keystore.tenant_encryption_keys", "keystore.key_tombstones"})


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0035", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    # Register in sys.modules BEFORE exec_module() -- see 0020's identical
    # comment for why (rbac_matrix.py's dataclasses resolve their own
    # module via sys.modules during class creation).
    sys.modules["waddles_rbac_matrix_0035"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    """Create the `keystore` schema, `tenant_encryption_keys`, `key_tombstones`, and grant RBAC."""
    op.execute("CREATE SCHEMA IF NOT EXISTS keystore")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS keystore.tenant_encryption_keys (
            id            BIGSERIAL PRIMARY KEY,
            tenant_id     INTEGER NOT NULL,
            purpose       VARCHAR(30) NOT NULL DEFAULT 'at-rest'
                CHECK (purpose IN ('at-rest', 'ingest-stream')),
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
            UNIQUE (tenant_id, purpose, dek_version)
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

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    roles = sorted(matrix_module.matrix_roles(matrix_path))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_create_roles_sql(roles):
        op.execute(statement)
    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute("GRANT USAGE ON SCHEMA keystore TO hub_api_keystore;")
    op.execute(
        "GRANT USAGE ON SEQUENCE keystore.tenant_encryption_keys_id_seq TO hub_api_keystore;"
    )
    op.execute("GRANT USAGE ON SEQUENCE keystore.key_tombstones_id_seq TO hub_api_keystore;")


def downgrade() -> None:
    """Drop `key_tombstones`, `tenant_encryption_keys`, the `keystore` schema, and its role."""
    op.execute("DROP TABLE IF EXISTS keystore.key_tombstones")
    op.execute("DROP TABLE IF EXISTS keystore.tenant_encryption_keys")
    op.execute("DROP SCHEMA IF EXISTS keystore")
    op.execute(
        "DO $$ BEGIN\n"
        "  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hub_api_keystore') THEN\n"
        "    DROP ROLE hub_api_keystore;\n"
        "  END IF;\n"
        "EXCEPTION WHEN dependent_objects_still_exist THEN\n"
        "  NULL; -- role still owns objects from a later migration; leave it\n"
        "END $$;"
    )
