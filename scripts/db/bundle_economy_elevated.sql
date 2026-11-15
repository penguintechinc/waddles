-- Bundle economy ELEVATED app-caller primitives (`transfer_from`, app mint) --
-- the single source of truth read by BOTH
-- `alembic/versions/0088_economy_elevated.py` (production schema) and the Rust
-- integration tests (`include_str!`, applied after
-- `bundle_economy_ledger_kinds.sql`), so the store's SQL is always tested
-- against the exact DDL that ships. Idempotent.
--
-- Prerequisites: `bundle_economy_store.sql` (`economy_ledger`), the ledger
-- kinds / idempotency scripts, and (production + tests) the
-- `community_permission_grants` table (alembic 0041).
--
-- 1. `community_permission_grants.grant_id`: a stable numeric id for one
--    (community, app, permission) approval so an audit row can name "the
--    approving grant" without a composite key. Guarded: skipped if the grants
--    table does not exist (the store then fails closed: no grant, no call).
-- 2. Ledger kinds `transfer_from` (victim debit row, carries the idempotency
--    key) and `transfer_from_in` (recipient credit row), both <= 16 chars.
-- 3. `economy_elevated_audit`: one row per elevated call -- caller app, the
--    approving grant (+ the admin who approved it), from/to, amount, reason
--    code, status and resulting balances. UUIDs / ids only, no PII. The
--    runtime role may INSERT and settle the status columns; never DELETE.

DO $grants$
BEGIN
    IF to_regclass('community_permission_grants') IS NOT NULL THEN
        ALTER TABLE community_permission_grants
            ADD COLUMN IF NOT EXISTS grant_id BIGSERIAL;
        IF NOT EXISTS (
            SELECT 1 FROM pg_indexes
             WHERE tablename = 'community_permission_grants'
               AND indexname = 'uq_community_permission_grants_grant_id'
        ) THEN
            CREATE UNIQUE INDEX uq_community_permission_grants_grant_id
                ON community_permission_grants (grant_id);
        END IF;
    END IF;
END
$grants$;

DO $kinds$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'economy_ledger'::regclass
           AND conname = 'economy_ledger_kind_check'
           AND position('transfer_from_in' IN pg_get_constraintdef(oid)) > 0
    ) THEN
        ALTER TABLE economy_ledger DROP CONSTRAINT IF EXISTS economy_ledger_kind_check;
        ALTER TABLE economy_ledger
            ADD CONSTRAINT economy_ledger_kind_check
            CHECK (kind IN ('wager', 'transfer_out', 'transfer_in',
                            'credit', 'debit', 'cond_debit', 'admin_adjust', 'pot_payout',
                            'exchange', 'transfer_from', 'transfer_from_in')) NOT VALID;
    END IF;
END
$kinds$;

CREATE TABLE IF NOT EXISTS economy_elevated_audit (
    id                 BIGSERIAL    PRIMARY KEY,
    tenant_id          INTEGER      NOT NULL REFERENCES tenants(id),
    community_id       INTEGER      NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    caller_app_id      VARCHAR(255) NOT NULL,
    service_app_id     VARCHAR(255) NOT NULL,
    permission_id      VARCHAR(64)  NOT NULL
                       CHECK (permission_id IN ('economy.transfer_from', 'economy.app_mint')),
    grant_id           BIGINT       NOT NULL,
    approved_by        INTEGER,
    op                 VARCHAR(32)  NOT NULL,
    target_user        UUID         NOT NULL,
    counterparty_user  UUID,
    amount             BIGINT       NOT NULL CHECK (amount >= 0),
    reason             VARCHAR(64)  NOT NULL CHECK (reason ~ '^[a-z0-9_.-]{1,64}$'),
    idempotency_key    VARCHAR(128) NOT NULL
                       CHECK (idempotency_key ~ '^[A-Za-z0-9:_.-]{1,128}$'),
    status             SMALLINT     NOT NULL DEFAULT 1 CHECK (status IN (1, 2, 3)),
    from_balance_after BIGINT,
    to_balance_after   BIGINT,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    settled_at         TIMESTAMPTZ
);

COMMENT ON TABLE economy_elevated_audit IS
    'Audit + cap ledger of ELEVATED economy calls made by an app bundle via the economy service (transfer_from, app mint). status: 1 reserved, 2 applied, 3 failed (not counted against caps). No PII: UUIDs and ids only.';

-- One audit row per (community, calling app, call key): the replay lookup and
-- the database backstop against a double-apply of the same call.
CREATE UNIQUE INDEX IF NOT EXISTS uq_economy_elevated_audit_key
    ON economy_elevated_audit (tenant_id, community_id, caller_app_id, idempotency_key);

-- Serves the rolling-24h per-app / per-target cap SUMs.
CREATE INDEX IF NOT EXISTS idx_economy_elevated_audit_window
    ON economy_elevated_audit (tenant_id, community_id, caller_app_id, permission_id, created_at);
CREATE INDEX IF NOT EXISTS idx_economy_elevated_audit_target
    ON economy_elevated_audit (tenant_id, community_id, caller_app_id, target_user, created_at);

REVOKE ALL ON economy_elevated_audit FROM PUBLIC;

DO $role$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_economy_runtime') THEN
        GRANT SELECT, INSERT ON economy_elevated_audit TO waddles_economy_runtime;
        GRANT UPDATE (status, from_balance_after, to_balance_after, settled_at)
            ON economy_elevated_audit TO waddles_economy_runtime;
        GRANT USAGE ON SEQUENCE economy_elevated_audit_id_seq TO waddles_economy_runtime;
        IF to_regclass('community_permission_grants') IS NOT NULL THEN
            GRANT SELECT (tenant_id, community_id, app_id, permission_id, params_json,
                          granted_by, revoked_at, grant_id)
                ON community_permission_grants TO waddles_economy_runtime;
        END IF;
    END IF;
END
$role$;

-- @@PHASE: VALIDATE@@
ALTER TABLE economy_ledger VALIDATE CONSTRAINT economy_ledger_kind_check;
