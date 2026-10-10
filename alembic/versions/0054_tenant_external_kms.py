"""Per-tenant envelope encryption key store + Enterprise external-KMS (BYOK) config.

Implements the schema half of `docs/superpowers/specs/2026-09-28-tenant-
envelope-encryption-design.md` Sec4 (key store) and Sec6/Sec7 step 9
(Enterprise BYOK). Two tables:

- `keystore.tenant_encryption_keys` -- one row per tenant per DEK version.
  Holds only the **wrapped** DEK (`wrapped_dek`, sealed by a KEK that lives in
  the platform secret or in the customer's KMS) plus which KEK sealed it
  (`kek_kind` = `platform` | `customer_kms`, `kek_ref` = canonical key
  reference). Each row records its own KEK, so a re-wrap can be partial and
  resumable and old versions stay readable after a KEK change. Retired
  versions are kept (old ciphertext must stay decryptable); rows are never
  hard-deleted by application code (hub-api has no DELETE grant).
  It lives in its own `keystore` schema, apart from the application tables
  whose data the DEKs protect (design Sec4: key material in a separate store).
  The dedicated Postgres *role* and short-retention *backup policy* for that
  schema are infrastructure follow-ups (see PR description); the repository
  layer accepts a connection on that role without code change.

  **Compatibility with the DEK-broker PR (#442).** The table definition here is
  a superset-compatible `IF NOT EXISTS` of the one #442 creates (same columns,
  `purpose` reserved for its `ingest-stream` lineage, same unique key), plus
  the nullable `rewrapped_at` and a one-active-per-(tenant, purpose) partial
  unique index added with `ADD COLUMN IF NOT EXISTS` / `CREATE ... IF NOT
  EXISTS`. Whichever migration lands second is a no-op on the shared parts, so
  the two are order-independent; only `rbac-matrix.yaml` needs reconciling
  (#442 grants a dedicated `hub_api_keystore` role instead of `hub_api`).

- `tenant_kms_configs` -- one row per tenant that opted into BYOK. **No secret
  is stored**: `provider` is `aws_kms` | `gcp_kms` | `azure_key_vault`; `key_ref`
  is the customer's key identifier (AWS key ARN / GCP CryptoKey resource name /
  Azure Key Vault key URL); `principal` is the customer-side identity Waddles
  acts as (AWS: IAM role ARN assumed with the ExternalId; Azure: the customer's
  Entra directory id; GCP: unused); and `external_id` is the server-generated
  proof-of-control token the customer pins in the role trust policy (AWS) or
  writes as a label/tag on the key (GCP/Azure) -- a confused-deputy guard, not
  a credential. `status` is `pending` -> `active` once every DEK has been
  re-wrapped under the customer key; `revoked` is set when the KMS denies
  access.

RBAC (rendered from `config/postgres/rbac-matrix.yaml` via
`scripts/db/rbac_matrix.py`, spec D28 -- no hand-written GRANT): `hub_api` is
the sole reader/writer. Every other role -- svc_ingest/svc_process/svc_action/
svc_streaming/webui/waddles_publisher -- gets `privileges: []`, so a
compromised data-plane credential cannot read even a wrapped DEK. On the key
table hub_api gets `SELECT, INSERT, UPDATE` only (keys are retired, not deleted).

Revision ID: 0054_tenant_external_kms
Revises: 0049_sso_connections
Create Date: 2026-10-09
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0054_tenant_external_kms"
down_revision = "0049_sso_connections"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset({"keystore.tenant_encryption_keys", "tenant_kms_configs"})

#: The schema DDL, exposed so real-Postgres tests can apply exactly what
#: production applies, without re-implementing it (single source of truth).
DDL_STATEMENTS: tuple[str, ...] = (
    "CREATE SCHEMA IF NOT EXISTS keystore",
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
    """,
    "ALTER TABLE keystore.tenant_encryption_keys ADD COLUMN IF NOT EXISTS rewrapped_at TIMESTAMPTZ",
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
    """,
    # At most one active DEK per tenant and lineage: makes concurrent
    # first-key creation / rotation race-safe (the loser gets a unique
    # violation, never two active keys).
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_encryption_keys_one_active
      ON keystore.tenant_encryption_keys (tenant_id, purpose)
      WHERE status = 'active'
    """,
    """
    CREATE TABLE IF NOT EXISTS tenant_kms_configs (
        tenant_id         INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
        provider          VARCHAR(32) NOT NULL,
        key_ref           VARCHAR(2048) NOT NULL,
        region            VARCHAR(64),
        principal         VARCHAR(2048),
        external_id       VARCHAR(128) NOT NULL,
        status            VARCHAR(20) NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'active', 'revoked')),
        last_verified_at  TIMESTAMPTZ,
        last_error_code   VARCHAR(128),
        created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "COMMENT ON TABLE tenant_kms_configs IS "
    "'Per-tenant Enterprise BYOK config: where the customer-managed KEK lives. Holds no "
    "secret -- key_ref/principal are identifiers; external_id is the proof-of-control token "
    "(AWS STS ExternalId, or a label/tag on the GCP/Azure key) the customer pins.'",
    "COMMENT ON TABLE keystore.tenant_encryption_keys IS "
    "'Wrapped per-tenant DEKs. Never holds an unwrapped key; kek_kind/kek_ref record which "
    "KEK (platform secret or customer KMS) sealed each version.'",
)

_DOWNGRADE_STATEMENTS: tuple[str, ...] = (
    "DROP TABLE IF EXISTS tenant_kms_configs",
    "DROP INDEX IF EXISTS keystore.uq_tenant_encryption_keys_one_active",
)


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0054", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    # Registered before exec_module() -- rbac_matrix.py's dataclasses resolve their own
    # module via sys.modules during class creation (same note as 0020/0034).
    sys.modules["waddles_rbac_matrix_0054"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    """Create the key store + BYOK config tables and render their grants from the RBAC matrix."""
    for statement in DDL_STATEMENTS:
        op.execute(statement)

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    # Default-deny on the schema itself; only hub_api may resolve names inside it.
    op.execute("REVOKE ALL ON SCHEMA keystore FROM PUBLIC;")
    op.execute("GRANT USAGE ON SCHEMA keystore TO hub_api, migration_runner;")
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)
    op.execute("GRANT USAGE ON SEQUENCE keystore.tenant_encryption_keys_id_seq TO hub_api;")


def downgrade() -> None:
    """Drop the BYOK config; keep the key store (it may hold the only copy of wrapped DEKs).

    `keystore.tenant_encryption_keys` is deliberately *not* dropped: it may
    already hold wrapped DEKs for live ciphertext (or be shared with the
    DEK-broker migration), and destroying it would be an irreversible
    crypto-shred. Only the one-active index and the BYOK config are removed.
    """
    for statement in _DOWNGRADE_STATEMENTS:
        op.execute(statement)
