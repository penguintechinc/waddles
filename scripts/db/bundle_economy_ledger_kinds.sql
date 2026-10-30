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
-- `exchange` (external-points claim credited as currency).
--
-- No new grant: the runtime role's table-level INSERT on `economy_ledger` and
-- column-scoped UPDATE on `economy_balances` already cover every new op; the
-- ledger stays append-only.

ALTER TABLE economy_ledger DROP CONSTRAINT IF EXISTS economy_ledger_kind_check;
ALTER TABLE economy_ledger
    ADD CONSTRAINT economy_ledger_kind_check
    CHECK (kind IN ('wager', 'transfer_out', 'transfer_in',
                    'credit', 'debit', 'admin_adjust', 'pot_payout', 'exchange'));
