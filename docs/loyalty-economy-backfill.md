# Loyalty balance backfill

The loyalty command bundle now delegates `!points` and `!give` to the economy
service during process-stage. The former `loyalty_balances` rows remain intact.
They use SHA-256 pseudonyms, which cannot be reversed into member UUIDs.

The backfill is a separate one-way maintenance action requiring explicit user
approval. It is not part of automatic Alembic upgrades or deployment. No backfill
has been applied to a live database by this work.

Before preparing a live plan, stop writes to the old loyalty storage and export
its exact rows from the scoped bundle-data store. Each JSON row must contain
`tenant_id`, `community_id`, `actor_hash` (64 lowercase hexadecimal characters),
and `balance` (a nonnegative integer). An operator must supply a separate,
verified mapping JSON array with the same scope/hash and a canonical `user_uuid`.
Do not infer mappings from usernames, assume a default tenant, or discard
unmapped balances. Duplicate rows, duplicate targets and incomplete maps abort.

Prepare the report and SQL without any database connection:

```bash
python scripts/db/prepare_loyalty_backfill.py \
  --legacy-export /secure/loyalty-export.json \
  --identity-map /secure/verified-identities.json \
  --report /secure/loyalty-backfill-report.json \
  --sql /secure/loyalty-backfill-review.sql
```

The report includes a deterministic plan ID, scoped totals and all movements.
Its status is `REQUIRES_EXPLICIT_APPROVAL`. The script has no apply mode and
never opens a database connection. Review the exact plan, identity mapping and
supply policy with the owner before applying the generated SQL. Generated
reports and exports can contain member UUIDs; store them securely and do not
commit live exports.

On explicit approval, an authorized operator can run the reviewed SQL with
`psql` and `ON_ERROR_STOP=1` during the maintenance window. It uses one
transaction, serializes balance writes, locks membership against concurrent
changes, verifies exact tenant/community membership, refuses nonzero existing
target funds, and checks each community's live supply ceiling. It records every
movement as an `admin_adjust` ledger entry with a deterministic retry key. A
repeated plan, or a revised plan containing an account an earlier plan already
migrated (even if its balance was since spent), aborts visibly instead of
crediting twice; the supply cap may not exceed the runtime ceiling (1,000,000). Any refusal rolls the
whole transaction back. Existing nonzero target balances require separate
manual reconciliation; the migration never overwrites them.

The legacy source is preserved for audit and recovery. Verify balance/ledger
counts and scoped totals before ending maintenance. Removing legacy tables is
a separate reviewed action after the migration is accepted.

Validation executes generated SQL against disposable PostgreSQL and verifies
ledgered success, replay refusal, cross-tenant refusal, supply limits and
rollback. Synthetic dry-run examples are test fixtures, not a report of live
community balances.
