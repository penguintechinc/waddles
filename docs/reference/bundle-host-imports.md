# Bundle host imports: `stage-next` and the `reputation` reference capability

Issue #726. `world stage` (`waddle:bundle@1.0.0`) is frozen. Every host import
added after it lives in `world stage-next` (`wit/waddle-bundle/stage.wit`).
`reputation` is the reference capability; economy (#714) and
streaming-lifecycle (#716) repeat its recipe.

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
| Schema | alembic `0043_bundle_reputation_store` (DDL in `scripts/db/bundle_reputation_store.sql`) |
| Role | `waddles_bundle_reputation`: DML on `bundle_reputation_scores`, SELECT/INSERT on `bundle_reputation_adjustments`, column SELECT on `community_members`/`communities`. Created NOLOGIN unless `DB_REPUTATION_PASSWORD` is set when migrating |
| svc_process env | `BUNDLE_REPUTATION_{HOST,PORT,NAME,USER}`, `BUNDLE_REPUTATION_PASSWORD`, `BUNDLE_REPUTATION_MEMBERSHIP_REFRESH_S` |
| Flag | `waddles.bundle-reputation-capability` (default OFF) |
| Caps | catalog: 5 per call, 5 per user per rolling 24h, 50 per app per community per rolling 24h (`reputation.community.write`). Both daily caps are enforced DURABLY in the store txn from the audit ledger (a restart or second replica cannot reset or multiply them); the gate's in-memory copy is only a fast pre-filter. A zero delta is `invalid_args` |
| Identity | targets are `community_members.user_uuid`, NULL until hub-api's IdentityService assigns it (#429). NULL means nobody is addressable: the capability is fail-closed until then |
