# Bundle host imports: `stage-next` and the `reputation` reference capability

Issue #726. `world stage` (`waddle:bundle@1.0.0`) is frozen. Every host import
added after it lives in `world stage-next` (`wit/waddle-bundle/stage.wit`).
`reputation` is the reference capability; `economy` (#714, below) is its
first clone and streaming-lifecycle (#716) repeats the recipe.

## What a bundle sees

| Surface | Shape |
|---|---|
| WIT | `interface reputation`: `get(user: string) -> result<s64, error>`, `adjust(user: string, delta: s32, reason: string) -> result<s64, error>` |
| Python SDK | `waddle_sdk.reputation.get(user)` / `.adjust(user, delta, reason)` (async); raises `DeniedError(code)`, `NotAMemberError`, `DailyCapExceededError`, `UnavailableError`, `InvalidReputationArgError`, `BackendError` |
| Permissions | `reputation.read` (get), `reputation.community.write` (adjust, with declared `delta_min`/`delta_max`) |
| Scope | community, tenant, app are server-derived; the bundle names only the target user UUID |

A gate denial is never swallowed: it raises (`DeniedError.code` is the gate's
stable code: `not_granted`, `delta_out_of_bounds`, `quota_exceeded`,
`rate_limited`, `instance_denied`, ...).

## Call path

```
bundle --reputation.adjust--> executor (stage-next linker)
   --host-call{capability=db, op="reputation.adjust"}--> svc_process
   handle_reputation:
     1. args (UUID user, i32 delta, [a-z0-9._:-] reason)
     2. CapabilityGate.authorize(ReputationScoped{target, delta})   grant, bounds, quotas, membership snapshot
     3. flag waddles.bundle-reputation-capability / wiring present   fail-loud not_implemented | feature_disabled
     4. ReputationStore.adjust                                       one txn: live membership re-check,
                                                                     row lock, rolling-24h cap, balance, ledger row
```

`penguin-bundle-host`'s `CapabilityKind` is a closed enum with no `reputation`
member, so the wire carries `db` + an `op` prefix and the stage dispatches on
the prefix before the `storage.tables` path. The gate, not the wire kind, is
the authority. Replace this with a real `CapabilityKind::Reputation` when
penguin-libs grows one.

## Recipe: add the next host import

1. **WIT**: add the `interface` + `import` it in `world stage-next` only.
2. **Executor**: `Host` impl in `core/bundle_executor/src/host/stage_next_imports.rs`;
   link it in `engine::link_stage_next_imports`. Shared interfaces are remapped
   to the `stage` bindings (`with:`), only new interfaces need a `Host` impl.
3. **Stage**: handler in `core/svc_process/src/capabilities.rs`, gate FIRST,
   then wiring/flag, then the store. Fail loud, never default.
4. **Store**: hub-owned table(s) via an Alembic migration (the live chain; the
   `config/postgres/migrations/*.sql` files only run on a brand-new database
   through the 0001 baseline), a dedicated least-privilege role, atomic writes.
5. **SDK**: facade module that imports `wit_world.imports.<name>` at MODULE LOAD
   (componentize-py wizens only eagerly imported bindings), and add the name to
   the tuple in `_component_entry.py`. Add its shapes to
   `scripts/verify_wit_bindings.sh`.
6. **Prove it with the real thing**: a fresh componentize-py build of a Python
   bundle through the real executor (`stage_next_python_reputation_e2e.rs`).
   Mocked tests cannot see a missing wizened binding.

## Operating the reputation store

| Item | Value |
|---|---|
| Schema | alembic `0043_bundle_reputation_store` (DDL in `scripts/db/bundle_reputation_store.sql`) |
| Role | `waddles_bundle_reputation`: DML on `bundle_reputation_scores`, SELECT/INSERT on `bundle_reputation_adjustments`, column SELECT on `community_members`/`communities`. Created NOLOGIN unless `DB_REPUTATION_PASSWORD` is set when migrating |
| svc_process env | `BUNDLE_REPUTATION_{HOST,PORT,NAME,USER}`, `BUNDLE_REPUTATION_PASSWORD`, `BUNDLE_REPUTATION_MEMBERSHIP_REFRESH_S` |
| Flag | `waddles.bundle-reputation-capability` (default OFF) |
| Caps | catalog: 5 per call, 5 per user per rolling 24h, 50 per community per day (`reputation.community.write`) |
| Identity | targets are `community_members.user_uuid`, NULL until hub-api's IdentityService assigns it (#429). NULL means nobody is addressable: the capability is fail-closed until then |

## `economy` (#714): the shared community currency

Cloned from the reputation recipe above. `stage-next` only.

| Surface | Shape |
|---|---|
| WIT | `interface economy`: `balance(user) -> s64`, `wager(user, stake: u64, payout: u64) -> s64`, `transfer(from-user, to-user, amount: u64) -> ()`, `max-bet(user) -> s64`, `leaderboard(limit: u32) -> list<entry{user: string, balance: s64}>`; error `variant { denied(string), insufficient-funds(u64), over-cap(u64), not-a-member, invalid(string), unavailable(string), backend(string) }` |
| Python SDK | `waddle_sdk.economy.balance/wager/transfer/max_bet/leaderboard` (async); raises `DeniedError(code)`, `InsufficientFundsError(balance)`, `OverCapError(cap)`, `NotAMemberError`, `InvalidEconomyArgError`, `UnavailableError`, `BackendError`. Test double: `waddle_sdk.testing.install_fake_economy_host` |
| Permissions | `economy.read` (balance, leaderboard), `economy.wager` (wager, max-bet; `params.max_bet` 1..=1000 required), `economy.transfer` (transfer; `params.max_amount` 1..=1000 required). Wager/transfer are `dangerous` |
| Scope | community, tenant, app server-derived; the bundle names only user UUID(s) and amounts. Every named user must be an active member (gate snapshot, then the write's own live check) |

### Quota family (not reputation's)

`economy.wager`/`economy.transfer` carry `Quota::EconomyAmount` (i64 amounts),
independent of reputation's 5-point `ReputationDelta` caps:

| Permission | Per call (ceiling) | Per user / day | Per community / day |
|---|---|---|---|
| `economy.wager` (meters the STAKE) | 1,000 | 10,000 | 250,000 |
| `economy.transfer` (meters the amount, against the SENDER) | 1,000 | 5,000 | 100,000 |
| `economy.read`, and amount-less `max-bet` | 20 calls/s rate limit | | |

The community-declared `max_bet`/`max_amount` is clamped to the per-call
ceiling by one shared function (`PermissionFamily::economy_amount_bound`)
used by both the gate and the stage's durable store call. Per-user and
per-community daily aggregates are in-memory per process (like reputation's
gate quota: reset on restart, not shared across replicas); the durable guards
are the store's max-bet / payout-multiple caps and the non-negative CHECK.

### Atomicity and server-side caps

| Rule | Mechanism |
|---|---|
| No overdraw, ever | `wager` is ONE statement: `UPDATE economy_balances SET balance = balance - stake + payout WHERE ... AND balance >= stake` (+ membership predicate + ledger insert as data-modifying CTEs). No read-modify-write; `CHECK (balance >= 0)` is the DB backstop |
| Can't stake what you don't hold | the guard is `balance >= stake`, not `balance + payout >= stake` |
| Max bet | stage computes `min(declared max_bet, ceiling)`; the store refuses `stake > max_bet` with `over-cap(max_bet)` before touching the DB |
| Payout bound | `payout <= stake * 100` (`over-cap(stake*100)`); the bundle decides the outcome, the host bounds the mint |
| Transfers | one txn: both members verified live, both rows locked in uuid order (no opposite-direction deadlock), then one statement debits, credits and writes both ledger rows |
| Ledger | `economy_ledger` is append-only for the runtime role; one row per movement, written in the same statement |

### Operating the economy store

| Item | Value |
|---|---|
| Schema | alembic `0044_bundle_economy_store` (DDL in `scripts/db/bundle_economy_store.sql`) |
| Role | `waddles_economy_runtime`: SELECT/INSERT/UPDATE on `economy_balances` (no DELETE), SELECT/INSERT on `economy_ledger`, column SELECT on `community_members`/`communities`. Created NOLOGIN unless `DB_ECONOMY_PASSWORD` is set when migrating |
| svc_process env | `BUNDLE_ECONOMY_{HOST,PORT,NAME,USER}`, `BUNDLE_ECONOMY_PASSWORD`, `BUNDLE_ECONOMY_MEMBERSHIP_REFRESH_S`. Unset password or failed connect: every call is `not_implemented`, membership snapshot stays empty |
| Flag | `waddles.bundle-economy-capability` (default OFF; OFF is `feature_disabled` after the gate) |
| Identity | targets are `community_members.user_uuid`, NULL until hub-api's IdentityService assigns it (#429). NULL matches nobody: the capability is fail-closed until then |
| Funding | this capability only MOVES balance. Initial funding / earn flows are a separate privileged hub-side writer; an unfunded member reads 0 |
| Wire | like reputation: `capability = db`, ops `economy.balance|wager|transfer|max_bet|leaderboard`; numeric refusals (`insufficient_funds`, `over_cap`) carry the bare decimal as the wire message |
