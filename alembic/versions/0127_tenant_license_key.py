"""Per-tenant license key store: `tenant_license_keys` (sealed key + non-secret fingerprint).

Issue #901: licensing resolves per tenant, so each tenant can carry its OWN license key (the
credential POSTed to the license server's entitlements endpoint to learn that tenant's pool).

The key is a credential, so it lives in its own table (one row per tenant), NOT as extra columns on
`tenants`: `tenants` rows are selected and serialized all over hub-api, and a sealed secret sitting
there would ride along on any `SELECT *`. Only `tenant_license_service.py` reads this table.

- `license_key_sealed`: the vault's opaque base64 ciphertext (AES-256-GCM, AAD-bound to the tenant
  id so a blob copied onto another tenant's row fails authentication). Never the plaintext.
- `seal_version`: vault seal version stamped at write time (1 = AAD-bound). CHECK `>= 1`: 0 is
  the legacy no-AAD open path and is never valid for this table (the read path rejects it too).
- `license_key_fingerprint`: first 8 hex chars of SHA-256(key); display/audit only, not secret.
- `license_key_set_at` / `license_key_set_by`: when and by whom (hub_users.id, SET NULL on erasure).

Downgrade drops the table but REFUSES while it holds keys unless
`ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE=1` (a silent drop would leave every tenant's pool failing
loud with no explanation). Plain `CREATE TABLE` (no `IF NOT EXISTS`): a pre-existing table of
unknown shape must fail loudly, never skip these constraints. `SET LOCAL lock_timeout` bounds
the FK lock waits.

Revision ID: 0127_tenant_license_key
Revises: 0126_community_tier_allocations
Create Date: 2026-11-17
"""

from __future__ import annotations

import os

import sqlalchemy as sa

from alembic import op

revision = "0127_tenant_license_key"
down_revision = "0126_community_tier_allocations"
branch_labels = None
depends_on = None

_UPGRADE = (
    "SET LOCAL lock_timeout = '10s'",
    """
CREATE TABLE tenant_license_keys (
    id                      BIGSERIAL PRIMARY KEY,
    tenant_id               INTEGER NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
    license_key_sealed      TEXT NOT NULL,
    seal_version            SMALLINT NOT NULL DEFAULT 1,
    license_key_fingerprint VARCHAR(16) NOT NULL,
    license_key_set_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    license_key_set_by      INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
    CONSTRAINT ck_tlk_seal_version CHECK (seal_version >= 1)
)
""",
    (
        "CREATE INDEX IF NOT EXISTS ix_tlk_set_by "
        "ON tenant_license_keys (license_key_set_by) WHERE license_key_set_by IS NOT NULL"
    ),
)

_DOWNGRADE = ("DROP TABLE IF EXISTS tenant_license_keys",)


#: Set to `1` to knowingly discard stored tenant license keys on downgrade.
_ALLOW_ENV = "ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE"
_COUNT = (
    "SELECT CASE WHEN to_regclass('tenant_license_keys') IS NULL THEN 0 "
    "ELSE (SELECT count(*) FROM tenant_license_keys) END"
)


def upgrade() -> None:
    """Create the sealed per-tenant license key table."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Drop the table; REFUSES while it holds keys unless `ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE=1`."""
    held = op.get_bind().execute(sa.text(_COUNT)).scalar() or 0
    if held and os.environ.get(_ALLOW_ENV) != "1":
        raise RuntimeError(
            f"Refusing to drop tenant_license_keys: it holds {held} sealed license key(s). "
            f"Preserve them, or set {_ALLOW_ENV}=1."
        )
    for statement in _DOWNGRADE:
        op.execute(statement)
