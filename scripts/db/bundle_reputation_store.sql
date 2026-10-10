-- Bundle reputation store DDL (issue #726) -- the single source of truth read
-- by BOTH `alembic/versions/0049_bundle_reputation_store.py` (production
-- schema) and `core/bundle_host_reputation/tests/postgres_integration.rs`
-- (`include_str!`), so the Rust store's SQL is always tested against the
-- exact DDL that ships. Idempotent (IF NOT EXISTS / guarded DO blocks); lives
-- under `scripts/db/` because that directory is already copied into the
-- migrations image (`migrations/Dockerfile`).
--
-- Prerequisites (created by earlier migrations / the test harness BEFORE this
-- file runs): `tenants`, `communities(id, tenant_id)`, `community_members`
-- (incl. `is_active`, `left_at`, `removed_at`), `bundle_reputation_adjustments`
-- (0041), and -- for the grants block -- the `waddles_bundle_reputation` role.

-- 1. Stable user identity on community membership. `user_uuid` is the
-- PII-free token a bundle names as the target of a reputation call; it is
-- minted/assigned INSIDE the hub-api PII boundary (IdentityService, #429) and
-- is NULL until then. NULL rows can never match a UUID lookup, so the
-- reputation capability is fail-closed (every target reads as a non-member)
-- until the identity layer populates this column.
ALTER TABLE community_members ADD COLUMN IF NOT EXISTS user_uuid UUID;

CREATE UNIQUE INDEX IF NOT EXISTS uq_community_members_community_user_uuid
    ON community_members (community_id, user_uuid)
    WHERE user_uuid IS NOT NULL;

-- 2. Hub-owned, community-scoped score store. One row per (community, user);
-- `balance` is mutated ONLY by the atomic adjust transaction in
-- `core/bundle_host_reputation` (row lock + ledger insert in one txn).
CREATE TABLE IF NOT EXISTS bundle_reputation_scores (
    tenant_id        INTEGER     NOT NULL REFERENCES tenants(id),
    community_id     INTEGER     NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    user_uuid        UUID        NOT NULL,
    balance          BIGINT      NOT NULL DEFAULT 0,
    adjustment_count BIGINT      NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (community_id, user_uuid)
);

CREATE INDEX IF NOT EXISTS idx_bundle_reputation_scores_leaderboard
    ON bundle_reputation_scores (tenant_id, community_id, balance DESC);

REVOKE ALL ON bundle_reputation_scores FROM PUBLIC;

COMMENT ON TABLE bundle_reputation_scores IS
    'Hub-owned community-scoped bundle reputation balances (issue #726); written only by the bundle reputation host capability''s atomic adjust transaction';

-- 3. Rolling-24h per-(community,user) cap lookup on the existing audit
-- ledger (0041): the cap is the SUM of |delta| of APPLIED community-scope
-- adjustments in the window, read under the score row's lock.
CREATE INDEX IF NOT EXISTS idx_bundle_reputation_adjustments_user_window
    ON bundle_reputation_adjustments (tenant_id, community_id, target_user_uuid, occurred_at)
    WHERE scope = 'community';

-- 4. Least-privilege role grants (role itself is provisioned by the
-- migration / test harness; skipped if absent so this file never depends on
-- ordering). DML only on the two reputation tables, column-scoped SELECT on
-- the membership tables -- no DDL, no other table.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_bundle_reputation') THEN
        GRANT USAGE ON SCHEMA public TO waddles_bundle_reputation;
        GRANT SELECT, INSERT, UPDATE ON bundle_reputation_scores TO waddles_bundle_reputation;
        GRANT SELECT, INSERT ON bundle_reputation_adjustments TO waddles_bundle_reputation;
        GRANT USAGE ON SEQUENCE bundle_reputation_adjustments_id_seq TO waddles_bundle_reputation;
        GRANT SELECT (community_id, user_uuid, is_active, left_at, removed_at)
            ON community_members TO waddles_bundle_reputation;
        GRANT SELECT (id, tenant_id) ON communities TO waddles_bundle_reputation;
    END IF;
END
$$;
