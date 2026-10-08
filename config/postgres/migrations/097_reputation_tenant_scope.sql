-- Migration 097: Re-scope reputation from community+GLOBAL to community+TENANT.
--
-- `reputation_global` (migration 080) was keyed by a bare `hub_user_id`,
-- aggregating a user's reputation across EVERY community on the platform
-- regardless of which tenant owns that community -- a direct violation of
-- security.md's tenant-as-hard-boundary rule (tenant is never allowed to
-- aggregate or leak across tenants). `communities.tenant_id` (migration
-- 058, NOT NULL) is the only mapping needed to re-home this aggregate: a
-- user's "cross-community" score becomes "cross-community-WITHIN-ONE-
-- TENANT", keyed by `(tenant_id, hub_user_id)`, never a bare `hub_user_id`.
--
-- `reputation_tenant` replaces `reputation_global` outright (not an
-- ALTER-in-place `tenant_id` column with the old PK kept) -- the OLD key
-- shape (bare `hub_user_id`) is exactly the bug; keeping it around even as
-- a non-PK column invites a query that forgets the new WHERE tenant_id=...
-- clause and silently falls back to the old cross-tenant behavior.
--
-- Backfill: a pre-existing `reputation_global` row was never computed
-- per-tenant (there was only ever one number per hub_user_id), so there is
-- no historical "what would tenant X's score have been" to reconstruct
-- exactly -- `reputation_events` IS the full audit trail of every event
-- that fed that single number, but a precise replay would have to re-run
-- ReputationService._clamp_score()'s per-event incremental clamp in
-- application code, not SQL (out of scope for a migration file). Instead,
-- this seeds EVERY tenant a user has ANY reputation_events activity in
-- with a COPY of their prior global snapshot (score/total_events/
-- last_event_at unchanged) -- a defensible one-time carry-forward, not a
-- claim that this was always the tenant's "true" historical score. From
-- the moment this migration applies, every new event updates exactly one
-- tenant's row (ReputationService._update_tenant_reputation), so scores
-- correctly diverge per tenant going forward; a user active in multiple
-- tenants starts with duplicate snapshots that drift apart immediately
-- rather than silently staying merged forever.
--
-- See core/reputation_module/services/reputation_service.py
-- (get_tenant_reputation, get_tenant_leaderboard, _update_tenant_reputation)
-- and hub_api/services/community_reputation_service.py /
-- core/svc_process/bundles/community_reputation_process.py for the
-- corresponding read-path changes.

BEGIN;

CREATE TABLE IF NOT EXISTS reputation_tenant (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    hub_user_id INTEGER NOT NULL REFERENCES hub_users(id) ON DELETE CASCADE,
    score INTEGER NOT NULL DEFAULT 600,
    total_events INTEGER NOT NULL DEFAULT 0,
    last_event_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, hub_user_id)
);

-- get_tenant_leaderboard(): RANK() OVER (PARTITION BY tenant ORDER BY score DESC).
CREATE INDEX IF NOT EXISTS idx_reputation_tenant_score
    ON reputation_tenant(tenant_id, score DESC);

-- One-time carry-forward backfill (see header comment). DISTINCT +
-- ON CONFLICT DO NOTHING: a user with events in several communities that
-- all belong to the SAME tenant must only produce one row for that tenant.
INSERT INTO reputation_tenant (tenant_id, hub_user_id, score, total_events, last_event_at, created_at, updated_at)
SELECT DISTINCT
    c.tenant_id,
    rg.hub_user_id,
    rg.score,
    rg.total_events,
    rg.last_event_at,
    rg.created_at,
    rg.updated_at
FROM reputation_global rg
JOIN reputation_events re ON re.hub_user_id = rg.hub_user_id
JOIN communities c ON c.id = re.community_id
ON CONFLICT (tenant_id, hub_user_id) DO NOTHING;

-- A `reputation_global` row with no matching `reputation_events` row
-- shouldn't exist (the table is only ever written by
-- `_update_global_reputation()`, always called from `adjust()` within the
-- same transaction as its own `reputation_events` INSERT) but if one ever
-- did (e.g. a hand-edited row), it is intentionally dropped here rather
-- than guessed into an arbitrary tenant -- there is no community to derive
-- a tenant_id from.

-- `reputation_global` is fully superseded -- see header comment for why
-- this is a hard replacement, not an additive column.
DROP TABLE IF EXISTS reputation_global;

-- Reputation Module (mod_core_reputation, see 031_scoped_database_users.sql)
-- needs the same SELECT/INSERT/UPDATE grants `reputation_global` had.
-- Guarded by a role-existence check for the same pre-existing ordering-gap
-- reason migration 080 documents (031 can fail before creating this role
-- on a fresh DB bootstrapped via alembic's baseline).
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'mod_core_reputation') THEN
        GRANT SELECT, INSERT, UPDATE ON reputation_tenant TO mod_core_reputation;
    END IF;
END
$$;

COMMIT;
