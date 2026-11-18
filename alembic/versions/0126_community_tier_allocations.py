"""Community tier allocations + per-tenant license pool cache (Pro/Ent team entitlements).

A community's tier is Free (no row, consumes no pool slot, never blocked), Professional (2) or
Enterprise (3). Pro/Ent are *allocated* from the tenant's license pool (team axis):

- `community_tier_allocations`: append-style ledger. At most ONE active (`released_at IS NULL`)
  row per community (partial unique). A release stamps `released_at`; rows are never deleted by the
  service, so the history of who allocated what survives. `last_active_at` drives the 30-day
  no-contact staleness rule (a stale allocation stops counting against the pool; a community that
  becomes active again simply re-counts).
- `tenant_license_pools`: the last pool limit observed per (tenant, axis, tier): from the license
  server (online), an offline entitlement file, or set by an operator. `limit_count` NULL = not
  licensed, -1 = unlimited. `axis_code` 3 = team.
- `communities.license_tier` / `communities.is_premium` become DENORMALIZED copies of the active
  allocation, written in the same transaction as the ledger row. They are not dropped here.

Enum codes (SMALLINT, never strings/booleans): source_code 1 tenant_admin / 2 operator_override /
3 backfill; release_reason_code 1 dealloc / 2 tier_change / 3 community_deleted; pool source_code
1 license_server / 2 offline file / 3 operator_set.

Backfill: every community that is premium (`is_premium IS TRUE`, or `license_tier` one of
enterprise / professional / pro / premium) and has no active allocation gets a row (source 3,
last_active_at = now): tier 3 when `license_tier` is `enterprise`, else tier 2 (Professional is only
the fallback for `is_premium` with no/unknown tier string, so Enterprise communities are never
downgraded). The backfill is idempotent (`NOT EXISTS` guard). The tables use plain `CREATE TABLE`
so a pre-existing table of unknown shape fails loudly instead of silently skipping its
constraints. `SET LOCAL lock_timeout` bounds every lock wait (FK / ALTER on the hot tables).
`communities.public_id` already exists (0084) and is not touched.

Downgrade drops both new tables but REFUSES while either holds rows (the ledger is the only
history of who allocated what, and the pools are paid entitlements the downgraded code would
neither read nor enforce) unless `ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE=1` is set. The denormalized
`communities.license_tier`/`is_premium` columns are left exactly as they are.

Revision ID: 0126_community_tier_allocations
Revises: 0125_tenant_do_not_sell
Create Date: 2026-11-17
"""

from __future__ import annotations

import os

import sqlalchemy as sa

from alembic import op

revision = "0126_community_tier_allocations"
down_revision = "0125_tenant_do_not_sell"
branch_labels = None
depends_on = None

_UPGRADE = (
    "SET LOCAL lock_timeout = '10s'",
    """
CREATE TABLE community_tier_allocations (
    id                   BIGSERIAL PRIMARY KEY,
    public_id            UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    tenant_id            INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    community_id         INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    tier_code            SMALLINT NOT NULL,
    source_code          SMALLINT NOT NULL,
    allocated_by         INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
    allocated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at          TIMESTAMPTZ,
    released_by          INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
    release_reason_code  SMALLINT,
    last_active_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_cta_tier CHECK (tier_code IN (2, 3)),
    CONSTRAINT ck_cta_source CHECK (source_code BETWEEN 1 AND 3),
    CONSTRAINT ck_cta_release_reason
        CHECK (release_reason_code IS NULL OR release_reason_code BETWEEN 1 AND 3)
)
""",
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_cta_active_community "
        "ON community_tier_allocations (community_id) WHERE released_at IS NULL"
    ),
    (
        "CREATE INDEX IF NOT EXISTS ix_cta_tenant_tier_active "
        "ON community_tier_allocations (tenant_id, tier_code) WHERE released_at IS NULL"
    ),
    # Full (non-partial) indexes on the FK columns: cascade/SET NULL deletes from tenants,
    # communities and hub_users would otherwise seq-scan the ledger.
    "CREATE INDEX IF NOT EXISTS ix_cta_tenant ON community_tier_allocations (tenant_id)",
    "CREATE INDEX IF NOT EXISTS ix_cta_community ON community_tier_allocations (community_id)",
    (
        "CREATE INDEX IF NOT EXISTS ix_cta_allocated_by "
        "ON community_tier_allocations (allocated_by) WHERE allocated_by IS NOT NULL"
    ),
    (
        "CREATE INDEX IF NOT EXISTS ix_cta_released_by "
        "ON community_tier_allocations (released_by) WHERE released_by IS NOT NULL"
    ),
    """
CREATE TABLE tenant_license_pools (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    axis_code    SMALLINT NOT NULL,
    tier_code    SMALLINT NOT NULL,
    limit_count  INTEGER,
    observed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_code  SMALLINT NOT NULL,
    set_by       INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
    CONSTRAINT ck_tlp_tier CHECK (tier_code IN (2, 3)),
    CONSTRAINT ck_tlp_axis CHECK (axis_code = 3),
    CONSTRAINT ck_tlp_source CHECK (source_code BETWEEN 1 AND 3),
    CONSTRAINT ck_tlp_limit CHECK (limit_count IS NULL OR limit_count >= -1),
    CONSTRAINT ux_tlp_tenant_axis_tier UNIQUE (tenant_id, axis_code, tier_code)
)
""",
    (
        "CREATE INDEX IF NOT EXISTS ix_tlp_set_by "
        "ON tenant_license_pools (set_by) WHERE set_by IS NOT NULL"
    ),
    # No-ops on real DBs (0011/0012 added them); keeps the minimal CI bootstrap schema replayable.
    "ALTER TABLE communities ADD COLUMN IF NOT EXISTS license_tier VARCHAR(50)",
    "ALTER TABLE communities ADD COLUMN IF NOT EXISTS is_premium BOOLEAN",
    """
INSERT INTO community_tier_allocations
    (tenant_id, community_id, tier_code, source_code, allocated_at, last_active_at)
SELECT c.tenant_id, c.id,
       CASE WHEN lower(btrim(c.license_tier)) = 'enterprise' THEN 3 ELSE 2 END,
       3, now(), now()
  FROM communities c
 WHERE (c.is_premium IS TRUE
        OR lower(btrim(c.license_tier)) IN ('enterprise', 'professional', 'pro', 'premium'))
   AND c.tenant_id IS NOT NULL
   AND EXISTS (SELECT 1 FROM tenants t WHERE t.id = c.tenant_id)
   AND NOT EXISTS (
        SELECT 1 FROM community_tier_allocations a
         WHERE a.community_id = c.id AND a.released_at IS NULL)
""",
)

_DOWNGRADE = (
    "DROP TABLE IF EXISTS tenant_license_pools",
    "DROP TABLE IF EXISTS community_tier_allocations",
)


#: Set to `1` to knowingly discard the ledger and pools on downgrade.
_ALLOW_ENV = "ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE"
_COUNT = (
    "SELECT (CASE WHEN to_regclass('community_tier_allocations') IS NULL THEN 0 "
    "ELSE (SELECT count(*) FROM community_tier_allocations) END) "
    "+ (CASE WHEN to_regclass('tenant_license_pools') IS NULL THEN 0 "
    "ELSE (SELECT count(*) FROM tenant_license_pools) END)"
)


def upgrade() -> None:
    """Create the ledger and pool tables, then backfill allocations for premium communities."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Drop both tables; REFUSES while they hold rows unless `ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE=1`.

    The ledger is the only record of who allocated what and the pools are paid entitlements;
    dropping them silently would make a rollback fail open. Export first, or set the env var.
    """
    held = op.get_bind().execute(sa.text(_COUNT)).scalar() or 0
    if held and os.environ.get(_ALLOW_ENV) != "1":
        raise RuntimeError(
            f"Refusing to drop community_tier_allocations/tenant_license_pools: they hold "
            f"{held} row(s) the downgraded code would not enforce. Preserve them, or set "
            f"{_ALLOW_ENV}=1."
        )
    for statement in _DOWNGRADE:
        op.execute(statement)
