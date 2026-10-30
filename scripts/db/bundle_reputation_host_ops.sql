-- Bundle reputation host-cap ops (bundle-MCP-tools P5 Task 1): admin override,
-- batch adjust, bounded batch reads, leaderboard. Read by BOTH
-- `alembic/versions/0069_reputation_host_ops.py` (production) and
-- `core/bundle_host_reputation/tests/postgres_integration.rs` (`include_str!`),
-- so the tested schema is the shipped schema. Idempotent; runs AFTER
-- `bundle_reputation_store.sql` (0050).

-- 1. Score clamp. The store clamps every write into this range; the CHECK is
-- the backstop so no code path (or future writer) can persist an absurd score.
-- NOT VALID: enforced for every new/updated row without scanning legacy rows
-- (the per-user daily cap makes an out-of-range legacy row implausible, and
-- NOT VALID means a surprise one can never fail the migration).
-- Keep -1000000 / 1000000 in lockstep with MIN_SCORE / MAX_SCORE in
-- `core/bundle_host_reputation/src/lib.rs`.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_bundle_reputation_scores_balance_range'
    ) THEN
        ALTER TABLE bundle_reputation_scores
            ADD CONSTRAINT ck_bundle_reputation_scores_balance_range
            CHECK (balance BETWEEN -1000000 AND 1000000) NOT VALID;
    END IF;
END
$$;

-- 2. Ledger extensions (the 0041 audit table). `kind` separates a capped
-- bundle `adjust` from an uncapped admin `override` (overrides are excluded
-- from the rolling-24h caps); `requested_value` is what the caller asked for
-- (delta for an adjust, absolute score for an override) while `delta` stays
-- the APPLIED change after clamping; `balance_after` lets an idempotent
-- replay answer with the original result; `idempotency_key` is the
-- host-derived (event, ordinal) key, NULL for the legacy un-keyed adjust.
ALTER TABLE bundle_reputation_adjustments
    ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'adjust';
ALTER TABLE bundle_reputation_adjustments
    ADD COLUMN IF NOT EXISTS requested_value BIGINT;
ALTER TABLE bundle_reputation_adjustments
    ADD COLUMN IF NOT EXISTS balance_after BIGINT;
ALTER TABLE bundle_reputation_adjustments
    ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_bundle_reputation_adjustments_kind'
    ) THEN
        ALTER TABLE bundle_reputation_adjustments
            ADD CONSTRAINT ck_bundle_reputation_adjustments_kind
            CHECK (kind IN ('adjust', 'admin_override'));
    END IF;
END
$$;

-- One keyed row per (scope, app, key, target): a replay can never double-apply
-- even if the store's own replay check were bypassed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_bundle_reputation_adjustments_idem
    ON bundle_reputation_adjustments
        (tenant_id, community_id, app_id, idempotency_key, target_user_uuid)
    WHERE idempotency_key IS NOT NULL;
