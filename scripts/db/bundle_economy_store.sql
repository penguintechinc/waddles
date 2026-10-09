-- Bundle economy store DDL (issue #714) -- the single source of truth read by
-- BOTH `alembic/versions/0044_bundle_economy_store.py` (production schema) and
-- `core/bundle_host_economy/tests/postgres_integration.rs` (`include_str!`), so
-- the Rust store's SQL is always tested against the exact DDL that ships.
-- Idempotent (IF NOT EXISTS / guarded DO blocks); lives under `scripts/db/`
-- because that directory is already copied into the migrations image.
--
-- Prerequisites (earlier migrations / the test harness): `tenants`,
-- `communities(id, tenant_id)`, `community_members` (incl. `is_active`,
-- `left_at`, `removed_at`) and -- for the grants block -- the
-- `waddles_economy_runtime` role.

-- 1. Stable user identity on community membership. Idempotent re-statement of
-- alembic 0043's column so this file stands alone: `user_uuid` is the PII-free
-- token a bundle names as the target of an economy call; it is minted INSIDE
-- the hub-api PII boundary (IdentityService, #429) and is NULL until then.
-- NULL rows can never match a UUID lookup, so the economy capability is
-- fail-closed (every target reads as a non-member) until the identity layer
-- populates it.
ALTER TABLE community_members ADD COLUMN IF NOT EXISTS user_uuid UUID;

CREATE UNIQUE INDEX IF NOT EXISTS uq_community_members_community_user_uuid
    ON community_members (community_id, user_uuid)
    WHERE user_uuid IS NOT NULL;

-- 2. Hub-owned, community-scoped currency balances. One row per
-- (tenant, community, user). `balance` is mutated ONLY by the single-statement
-- atomic UPDATEs in `core/bundle_host_economy`; the CHECK is the database's
-- own backstop so no code path -- including a future bug -- can overdraw.
-- UUID-keyed, no PII.
CREATE TABLE IF NOT EXISTS economy_balances (
    tenant_id    INTEGER     NOT NULL REFERENCES tenants(id),
    community_id INTEGER     NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    user_uuid    UUID        NOT NULL,
    balance      BIGINT      NOT NULL DEFAULT 0
                 CONSTRAINT economy_balances_non_negative CHECK (balance >= 0),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, community_id, user_uuid)
);

CREATE INDEX IF NOT EXISTS idx_economy_balances_leaderboard
    ON economy_balances (tenant_id, community_id, balance DESC);

REVOKE ALL ON economy_balances FROM PUBLIC;

COMMENT ON TABLE economy_balances IS
    'Hub-owned community-scoped bundle currency balances (issue #714); written only by the bundle economy host capability''s atomic single-statement UPDATEs';

-- 3. Append-only audit ledger: one row per balance movement, inserted in the
-- SAME statement (data-modifying CTE) as the balance UPDATE it records, so a
-- balance can never move without a ledger row nor a ledger row exist without
-- the move. `kind`: `wager` (delta = payout - stake), `transfer_out` /
-- `transfer_in` (counterparty = the other side). No free text, no PII.
CREATE TABLE IF NOT EXISTS economy_ledger (
    id                BIGSERIAL   PRIMARY KEY,
    tenant_id         INTEGER     NOT NULL REFERENCES tenants(id),
    community_id      INTEGER     NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    app_id            VARCHAR(255) NOT NULL,
    user_uuid         UUID        NOT NULL,
    counterparty_uuid UUID,
    kind              VARCHAR(16) NOT NULL CHECK (kind IN ('wager', 'transfer_out', 'transfer_in')),
    delta             BIGINT      NOT NULL,
    stake             BIGINT,
    payout            BIGINT,
    balance_after     BIGINT      NOT NULL CHECK (balance_after >= 0),
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_economy_ledger_user_time
    ON economy_ledger (tenant_id, community_id, user_uuid, occurred_at);

REVOKE ALL ON economy_ledger FROM PUBLIC;

-- 4. Least-privilege role grants (role itself is provisioned by the
-- migration / test harness; skipped if absent so this file never depends on
-- ordering). DML on the two economy tables (balances: no DELETE; ledger:
-- append-only -- no UPDATE/DELETE), column-scoped SELECT on the membership
-- tables -- no DDL, no other table.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_economy_runtime') THEN
        GRANT USAGE ON SCHEMA public TO waddles_economy_runtime;
        GRANT SELECT, INSERT, UPDATE ON economy_balances TO waddles_economy_runtime;
        GRANT SELECT, INSERT ON economy_ledger TO waddles_economy_runtime;
        GRANT USAGE ON SEQUENCE economy_ledger_id_seq TO waddles_economy_runtime;
        GRANT SELECT (community_id, user_uuid, is_active, left_at, removed_at)
            ON community_members TO waddles_economy_runtime;
        GRANT SELECT (id, tenant_id) ON communities TO waddles_economy_runtime;
    END IF;
END
$$;
