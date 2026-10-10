# bundle-host-economy

The durable store behind the bundle `economy` host capability (issue #714): the
shared, community-scoped currency a bundle can read, wager with and transfer.
A path dependency of `core/svc_process` (not a penguin-libs crate).

Normative reference for bundle authors and operators:
[`docs/reference/bundle-host-imports.md`](../../docs/reference/bundle-host-imports.md)
(`economy` + *Money safety*). WIT: `wit/waddle-bundle/stage.wit` `interface economy`.
Python SDK: `waddle_sdk.economy`.

## Where it sits

```
bundle --economy.wager--> executor --host-call(db, "economy.wager")--> svc_process::capabilities::handle_economy
   1. parse args (UUIDs, in-range integers)
   2. CapabilityGate::authorize(EconomyWager, EconomyScoped{player, stake, payout})
        grant, declared max_bet, EconomyAmount quotas (stake AND payout), membership pre-filter
   3. wiring + flag state (fail-loud not_implemented / feature_disabled)
   4. bind_actor: the payer MUST be the invocation's actor            <-- theft guard
   5. idempotency key = <event_id>:<wager|transfer>:<ordinal>          <-- double-spend guard
   6. EconomyStore::wager / transfer     <-- THIS CRATE: the authoritative, durable part
        advisory lock -> replay check -> daily caps (stake, payout) -> one atomic statement
```

The gate is a fast, in-memory pre-filter (it resets on restart and multiplies
across replicas). Everything that must hold is re-checked here, durably, inside
the write transaction.

## Money-safety guarantees (#751 review)

| Guarantee | How |
|---|---|
| Only the actor's money moves | enforced in `svc_process` before the store is called: a wager's `user` / a transfer's `from` must equal the actor derived host-side from the delivered event. Anything else is `actor_mismatch`. The store itself trusts the payer it is handed, so **never expose it to a path that has not bound the actor** |
| The mint cap is on the payout | a wager's per-user and per-(tenant, community, app) rolling-24h aggregates meter the **payout credited** (the currency it mints) in addition to, and independently of, the **stake** (throughput). `wager` sums `payout` from the ledger for the one and `stake` for the other; both against the same `EconomyCaps` ceilings. A zero payout mints nothing and skips the payout window. Transfers meter the amount sent, against the sender |
| A retried or replayed call applies once | every `wager`/`transfer` takes a host-built `IdempotencyKey`. A key already on a ledger row with the SAME parameters is a replay: the original result comes back, nothing moves, no budget is consumed (the check precedes every cap). The same key with different parameters, or a different kind of call, is `EconomyError::IdempotencyConflict` (`idempotency_conflict`) and is never applied |

### Idempotency, precisely

* The key is stored on the ledger row that originates the movement: the
  `wager` row, or a transfer's `transfer_out` row (its `transfer_in` twin has
  none). `UNIQUE (tenant_id, community_id, app_id, idempotency_key) WHERE
  idempotency_key IS NOT NULL` makes a key claimable once per app and community,
  whatever the code does. A wager and a transfer racing on one key hold
  different advisory locks; the index decides the race and the loser surfaces
  as `IdempotencyConflict` (never a backend fault, never a second movement).
* Keys are opaque and log-safe: `[A-Za-z0-9:_.-]`, 1..=128 bytes
  (`IdempotencyKey::new`); the host builds them with `IdempotencyKey::for_event`.
  A guest never supplies one.
* A refused or failed call writes no ledger row, so it records no key; a later
  retry under the same key applies normally.
* Replays answer with the **original** result (a wager's `balance_after`), not
  the current balance.

## Atomicity

Money never moves read-modify-write. A wager is one guarded statement
(`SET balance = balance - stake + payout WHERE ... AND balance >= stake`) with
the membership predicate and the ledger insert as data-modifying CTEs of the
same statement; a transfer locks both balance rows in uuid order and then moves
the money in one statement. `CHECK (balance >= 0)` is the database's own
backstop. See the crate docs (`src/lib.rs`) for the lock order.

## Schema and role

| Item | Value |
|---|---|
| Tables | `economy_balances`, `economy_ledger` (append-only) |
| DDL | `scripts/db/bundle_economy_store.sql` (alembic `0050`), `scripts/db/bundle_economy_idempotency.sql` (alembic `0052`: `idempotency_key`, shape CHECK, partial UNIQUE index) |
| Role | `waddles_economy_runtime`: column-scoped INSERT/UPDATE on balances, SELECT/INSERT (never UPDATE/DELETE) on the ledger, column SELECT on the membership tables |
| Funding | this capability only MOVES balance; initial funding / earn flows are a separate privileged hub-side writer |

## Telemetry

OpenTelemetry (global meter provider installed by `penguin-logging`); no user
ids, keys or PII in any label or log body.

| Instrument | Labels |
|---|---|
| `waddles_bundle_economy_call_duration_seconds` (histogram), `waddles_bundle_economy_calls_total` | `op`, `outcome` (`ok`, `not_a_member`, `insufficient_funds`, `over_cap`, `quota_exceeded`, `idempotency_conflict`, `invalid_args`, `backend`) |
| `waddles_bundle_economy_amount` (histogram) | `op` = `wager` (stake), `wager_payout` (minted), `transfer` |
| `waddles_bundle_economy_replays_total` | `op` |

Logs (`tracing`): replays at DEBUG; an idempotency conflict at WARN
(`tenant_id`, `community_id`, `app_id`, `idempotency_key` -- an event UUID, no
PII); backend errors are logged by `svc_process` with their detail and handed to
a guest only as a generic `backend`.

## Tests

```bash
cd core/bundle_host_economy
cargo test                      # unit tests + tests/postgres_integration.rs (needs Docker: testcontainers)
cargo fmt --check && cargo clippy --all-targets --locked -- -D warnings
cargo deny check
cargo llvm-cov --locked --fail-under-lines 90
```

`tests/postgres_integration.rs` runs the store against a real Postgres with the
exact shipped DDL. The #751 regressions:

| Finding | Tests |
|---|---|
| Mint cap on payout | `the_per_user_mint_cap_is_enforced_on_the_payout_not_the_stake`, `a_large_losing_stake_spends_the_stake_budget_not_the_mint_budget`, `the_per_scope_mint_cap_binds_across_users_on_the_payout`, `the_mint_cap_is_durable_across_a_fresh_store`, `a_payout_window_rolls_off_after_24_hours` |
| Replay / double-spend | `a_replayed_wager_credits_once_and_returns_the_original_balance`, `a_replayed_transfer_moves_money_once`, `a_key_reused_with_different_parameters_is_a_conflict_and_moves_nothing`, `a_refused_call_records_no_key_so_a_later_retry_applies_once`, `a_replay_is_not_refused_by_the_budget_its_own_first_run_used_up`, `keys_are_scoped_per_app`, `concurrent_retries_of_one_wager_credit_exactly_once`, `a_wager_and_a_transfer_racing_on_one_key_apply_at_most_one`, `the_database_refuses_a_second_ledger_row_for_one_key` |
| Actor binding (stage side) | `core/svc_process/src/capabilities_economy_tests.rs`, `core/svc_process/tests/economy_pg_e2e.rs`, `core/svc_process/tests/identity_pg_e2e.rs` |
