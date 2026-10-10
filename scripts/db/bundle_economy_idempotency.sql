-- Bundle economy idempotency DDL (issue #714, #751 money-safety review) -- the
-- single source of truth read by BOTH
-- `alembic/versions/0052_economy_idempotency.py` (production schema) and the
-- Rust integration tests (`include_str!`, applied right after
-- `bundle_economy_store.sql`), so the store's SQL is always tested against the
-- exact DDL that ships. Idempotent (IF NOT EXISTS / guarded DO blocks).
--
-- Prerequisite: `bundle_economy_store.sql` (the `economy_ledger` table).
--
-- Every money-moving economy call carries a host-derived idempotency key (see
-- `core/bundle_host_economy/src/lib.rs`). It is stored on the ledger row that
-- ORIGINATES the movement -- the `wager` row, or a transfer's `transfer_out`
-- row; the matching `transfer_in` row carries none -- so a key is claimed by
-- exactly one ledger row per (tenant, community, app). The partial UNIQUE index
-- below is the database's own backstop: even a bug (or a race the store's
-- advisory lock does not serialize, e.g. a wager and a transfer on one key)
-- cannot credit the same key twice. Existing rows keep a NULL key.

ALTER TABLE economy_ledger ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128);

-- Host keys are `<event uuid>:<wager|transfer>:<ordinal>`; the CHECK keeps the
-- column to that opaque, log-safe alphabet (no whitespace/control characters).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'economy_ledger'::regclass
           AND conname = 'economy_ledger_idempotency_key_shape'
    ) THEN
        ALTER TABLE economy_ledger
            ADD CONSTRAINT economy_ledger_idempotency_key_shape
            CHECK (idempotency_key IS NULL OR idempotency_key ~ '^[A-Za-z0-9:_.-]{1,128}$');
    END IF;
END
$$;

-- One keyed ledger row per (tenant, community, app, key). Also serves the
-- store's replay lookup.
CREATE UNIQUE INDEX IF NOT EXISTS uq_economy_ledger_idempotency
    ON economy_ledger (tenant_id, community_id, app_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

COMMENT ON COLUMN economy_ledger.idempotency_key IS
    'Host-derived replay key of the call that originated this movement (wager / transfer_out rows); a repeat of the key returns the original result and moves nothing';
