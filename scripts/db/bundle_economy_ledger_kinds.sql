-- Bundle economy ledger kinds for the compound host-cap ops (P4 Task 1) -- the
-- single source of truth read by BOTH
-- `alembic/versions/0069_economy_ledger_kinds.py` (production schema) and the
-- Rust integration tests (`include_str!`, applied after
-- `bundle_economy_idempotency.sql`), so the store's SQL is always tested
-- against the exact DDL that ships. Idempotent: the CHECK is dropped and
-- re-added by name, so it reaches existing DBs and re-applies cleanly.
--
-- Prerequisite: `bundle_economy_store.sql` (the `economy_ledger` table; its
-- inline `kind` CHECK is auto-named `economy_ledger_kind_check`).
--
-- New kinds (all <= 16 chars, the column width): `credit` (mint, one user),
-- `debit` (spend/burn), `admin_adjust` (privileged absolute-set or delta),
-- `pot_payout` (a pot account's debit row and each winner's credit row),
-- `exchange` (external-points claim credited as currency), `cond_debit` (a
-- conditional debit; its own kind so it never shares an idempotency identity
-- with a plain `debit`).
--
-- No new grant: the runtime role's table-level INSERT on `economy_ledger` and
-- column-scoped UPDATE on `economy_balances` already cover every new op; the
-- ledger stays append-only.

ALTER TABLE economy_ledger DROP CONSTRAINT IF EXISTS economy_ledger_kind_check;
ALTER TABLE economy_ledger
    ADD CONSTRAINT economy_ledger_kind_check
    CHECK (kind IN ('wager', 'transfer_out', 'transfer_in',
                    'credit', 'debit', 'cond_debit', 'admin_adjust', 'pot_payout',
                    'exchange'));

-- Replay-binding columns (nullable; only the ops below write them): an
-- `exchange` records the rate it was claimed at, a `cond_debit` the floor it
-- was conditioned on, so a replay under the same key with a different rate or
-- floor is an idempotency conflict, not a silent replay.
ALTER TABLE economy_ledger ADD COLUMN IF NOT EXISTS rate_num BIGINT;
ALTER TABLE economy_ledger ADD COLUMN IF NOT EXISTS rate_den BIGINT;
ALTER TABLE economy_ledger ADD COLUMN IF NOT EXISTS min_balance BIGINT;

-- Windows (host-side, documented here for operators): the daily caps are per
-- COMMUNITY across all apps; `credit`, `batch_credit` and `exchange` share ONE
-- mint window; `debit` and `cond_debit` share one spend window; `admin_adjust`
-- has NO daily window by design (privileged, audited, supply-capped).
