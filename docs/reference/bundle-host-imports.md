# Bundle host imports: `stage-next` and the `reputation` reference capability

Issue #726. `world stage` (`waddle:bundle@1.0.0`) is frozen. Every host import
added after it lives in `world stage-next` (`wit/waddle-bundle/stage.wit`).
`reputation` is the reference capability; `economy` (#714, below) is its
first clone, `identity` (below) is the actor/mention -> `user_uuid` resolver
the points-game bundles need before they can call either, and
streaming-lifecycle (#716) repeats the recipe.

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
     1. args (UUID user, non-zero i32 delta, [a-z0-9._:-] reason)
     2. CapabilityGate.authorize(ReputationScoped{target, delta})   grant, bounds, quotas, membership snapshot
     3. flag waddles.bundle-reputation-capability / wiring present   fail-loud not_implemented | feature_disabled
     4. ReputationStore.adjust                                       one txn: live membership re-check (NULL is_active = non-member),
                                                                     per-scope advisory lock, row lock, rolling-24h per-user AND
                                                                     per-scope caps (both durable), balance, ledger row
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
| Schema | alembic `0049_bundle_reputation_store` (DDL in `scripts/db/bundle_reputation_store.sql`) |
| Role | `waddles_bundle_reputation`: DML on `bundle_reputation_scores`, SELECT/INSERT on `bundle_reputation_adjustments`, column SELECT on `community_members`/`communities`. Created NOLOGIN unless `DB_REPUTATION_PASSWORD` is set when migrating |
| svc_process env | `BUNDLE_REPUTATION_{HOST,PORT,NAME,USER}`, `BUNDLE_REPUTATION_PASSWORD`, `BUNDLE_REPUTATION_MEMBERSHIP_REFRESH_S` |
| Flag | `waddles.bundle-reputation-capability` (default OFF) |
| Caps | catalog: 5 per call, 5 per user per rolling 24h, 50 per app per community per rolling 24h (`reputation.community.write`). Both daily caps are enforced DURABLY in the store txn from the audit ledger (a restart or second replica cannot reset or multiply them); the gate's in-memory copy is only a fast pre-filter. A zero delta is `invalid_args` |
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
used by both the gate and the stage's durable store call. The gate's per-user
and per-community daily aggregates are an in-memory fast pre-filter (reset on
restart, not shared across replicas); the SAME two aggregates are enforced
DURABLY in the store transaction from the append-only ledger (stakes for a
wager, amounts sent for a transfer, per app and community), under a
transaction-scoped advisory lock per `(tenant, community, app, wager|transfer)`,
so a restart or a second replica can neither reset nor multiply them. Both
paths answer the same `quota_exceeded` code. Only APPLIED movements count.

### Atomicity and server-side caps

| Rule | Mechanism |
|---|---|
| No overdraw, ever | `wager` is ONE statement: `UPDATE economy_balances SET balance = balance - stake + payout WHERE ... AND balance >= stake` (+ membership predicate + ledger insert as data-modifying CTEs). No read-modify-write; `CHECK (balance >= 0)` is the DB backstop |
| Can't stake what you don't hold | the guard is `balance >= stake`, not `balance + payout >= stake` |
| Max bet | stage computes `min(declared max_bet, ceiling)`; the store refuses `stake > max_bet` with `over-cap(max_bet)` before touching the DB |
| Daily aggregates | durable, ledger-derived, advisory-locked (above); the lock is the first and only advisory lock a transaction takes, so lock order is uniform |
| Membership | `community_members.is_active IS TRUE` -- a NULL `is_active` is NOT a member (fail-closed), in the gate snapshot loader, the live re-check, balance reads and the leaderboard |
| Payout bound | `payout <= stake * 100` (`over-cap(stake*100)`); the bundle decides the outcome, the host bounds the mint |
| Transfers | one txn: both members verified live, both rows locked in uuid order (no opposite-direction deadlock), then one statement debits, credits and writes both ledger rows |
| Ledger | `economy_ledger` is append-only for the runtime role; one row per movement, written in the same statement |

### Operating the economy store

| Item | Value |
|---|---|
| Schema | alembic `0050_bundle_economy_store` (DDL in `scripts/db/bundle_economy_store.sql`) |
| Role | `waddles_economy_runtime`: SELECT on `economy_balances` with COLUMN-scoped INSERT (`tenant_id, community_id, user_uuid` only: a new row can only start at 0, INSERT cannot mint) and COLUMN-scoped UPDATE (`balance, updated_at` only: no re-keying), no DELETE; SELECT/INSERT on `economy_ledger` (append-only); column SELECT on `community_members`/`communities`. Created NOLOGIN unless `DB_ECONOMY_PASSWORD` is set when migrating |
| svc_process env | `BUNDLE_ECONOMY_{HOST,PORT,NAME,USER}`, `BUNDLE_ECONOMY_PASSWORD`, `BUNDLE_ECONOMY_MEMBERSHIP_REFRESH_S`. Unset password or failed connect: every call is `not_implemented`, membership snapshot stays empty |
| Flag | `waddles.bundle-economy-capability` (default OFF; OFF is `feature_disabled` after the gate) |
| Identity | targets are `community_members.user_uuid`, minted inside hub-api's PII boundary (#429, alembic `0045_identity_resolution` once on the branch; NULL until then and for unresolvable rows). NULL matches nobody: the capability is fail-closed for those rows |
| Funding | this capability only MOVES balance. Initial funding / earn flows are a separate privileged hub-side writer; an unfunded member reads 0 |
| Wire | like reputation: `capability = db`, ops `economy.balance|wager|transfer|max_bet|leaderboard`; numeric refusals (`insufficient_funds`, `over_cap`) carry the bare decimal as the wire message |

## `identity`: the triggering actor / a mention target -> community `user_uuid`

The prerequisite of every points-game bundle. `reputation`/`economy` name their
targets by `community_members.user_uuid`; a bundle only holds the tokenized
`{user:<token>}` placeholder (`core/svc_process/src/pii_tokenize.rs`), which is
**not** that UUID. `stage-next` only.

| Surface | Shape |
|---|---|
| WIT | `interface identity`: `resolve-actor() -> string`, `resolve-mention(token: string) -> string`; error `variant { denied(string), not-linked, not-a-member, not-found, ambiguous, invalid(string), unavailable(string), backend(string) }` |
| Python SDK | `waddle_sdk.identity.resolve_actor()` / `resolve_mention(token)` (async), `mention_tokens(text)` (the `<token>` of each `{user:<token>}` in the delivered text); raises `DeniedError(code)`, `NotLinkedError`, `NotAMemberError`, `NotFoundError`, `AmbiguousError`, `InvalidIdentityArgError`, `UnavailableError`, `BackendError`. Test double: `waddle_sdk.testing.install_fake_identity_host` |
| Permission | `identity.resolve` (`normal`, no params, 20 calls/s rate limit). A bundle that never declared it gets `denied("not_granted")` -- the gate runs before wiring/flag state |
| Flag | `waddles.bundle-identity-capability` (default OFF; OFF is `feature_disabled` after the gate) |

```
!steal @bob 25              text the bundle receives: "!steal {user:<tok>} 25"
  actor  = await identity.resolve_actor()                    # -> community user_uuid
  target = await identity.resolve_mention(tokens[0])         # tokens = identity.mention_tokens(text)
  await economy.transfer(actor, target, 25)                  # the uuids economy accepts
```

### Why it is PII-safe

| Property | Mechanism |
|---|---|
| UUID-only | the success value is `Uuid::to_string()`; the executor additionally refuses any non-canonical-UUID `user` as a loud `backend` error (never forwarded, never echoed). The SDK re-checks |
| The bundle selects no one | `resolve-actor` takes no argument: the stage derives the actor from the RAW event's platform account id (`user_id`, then `author_id`) under the invocation's host-derived `(tenant, community)`. `resolve-mention` takes only the opaque token the bundle was already shown |
| Not a directory lookup | the stage keeps a per-invocation table of the references THIS message carried (tokenization on: keyed by the token inside each `{user:<token>}`; off: keyed by the raw `<@id>`/`@handle` the bundle saw anyway). An unknown token, or a raw handle never shown, is `not-found` -- no probing whether a handle exists, and the same answer for "not in this message" and "no such identity" |
| Raw references stay host-side | `MentionRef`/`MentionBinding`/`InvocationIdentity` have redacted `Debug`; no log line, error message or metric label carries a handle, platform id or token (labels are the fixed `op`/`via`/`outcome` vocabularies) |
| Backend detail never reaches a guest | a store failure is logged and answered as a generic `backend` |

### Resolution and refusals

| Reference | Path | Refusals |
|---|---|---|
| actor | `community_member_identities` (read-only `waddles_bundle_reader`, tenant + community scoped, active members only) by the event's platform account id | no account id on the event or `user_uuid IS NULL` -> `not-linked`; no active row -> `not-a-member` |
| `<@id>` mention | the same exact lookup by the mentioned account id (read-only: no hub-api round trip, no pseudonym minted) | as above |
| free-text `@handle` | hub-api `IdentityService.ResolveHandle` (#748: tenant-scoped, matched inside the PII boundary, never guessed), then CONFIRMED an active member of THIS community by `user_uuid` | NOT_FOUND -> `not-found`; ambiguous -> `ambiguous`; tenant identity outside this community -> `not-a-member`; resolver not configured/reachable -> `unavailable` |
| any | -- | wrong tenant for the community -> `not-a-member` (the tenant predicate is in the query) |

`not-linked` is the "your account is not linked" case: `community_members.user_uuid`
is NULL (an identity the hub has not resolved). The bundle must surface it and
stop; there is no code path that substitutes the tokenized pseudonym for a UUID.

### Operating it

| Item | Value |
|---|---|
| Schema | alembic `0051_bundle_identity_resolve` (DDL in `scripts/db/bundle_identity_resolve.sql`): appends `tenant_id` and `is_active_member` to the existing PII-free `community_member_identities` view. No new table, role, password or privilege |
| DB account | the stage's existing read-only `waddles_bundle_reader` connection (the grant loader's `DB_READER_PASSWORD` account); no `BUNDLE_IDENTITY_*` secret exists. Not configured: every call is `not_implemented` |
| hub-api | free-text handles use a second, single-scope `hub_client` (`identity:handle:resolve`) built from the existing `HUB_API_GRPC_ENDPOINT`/`SERVICE_JWT_*` env. Unset/unreachable at startup: handle mentions are `unavailable` until restart (actor and `<@id>` mentions are unaffected) |
| Scope | multi-tenant (changelog-consumer) path only; the legacy env-only single-bundle path runs at the `(0, 0)` fail-closed scope where every community-scoped capability refuses `invalid_args` |
| Wire | like reputation/economy: `capability = db`, ops `identity.resolve_actor` / `identity.resolve_mention`; refusal codes `not_linked`, `not_a_member`, `not_found`, `ambiguous`, `invalid_args`, `unavailable`, `backend` |
| Placeholder pseudonyms | `core/svc_process/src/pii_tokenize.rs` mints a `handle:<name>` placeholder pseudonym for every free-text `@handle` (it needs an opaque token for the text). That row is a token, not an identity: hub-api's handle matcher (`identity_resolution_service._HANDLE_CANDIDATES_SQL`) excludes `platform_user_id LIKE 'handle:%'`, otherwise every mentioned Twitch `@bob` would turn into an `ambiguous` pair next to the real chatter's row. The placeholder still detokenizes (`ResolveDisplayNames`). The tokenizer's scanner also no longer reads the digits inside a Discord `<@123>` as a second `@123` handle mention |
