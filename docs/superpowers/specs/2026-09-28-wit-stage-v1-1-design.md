# Waddles Bundle WIT Contract v1.1 — Design

**Status:** APPROVED-WITH-CONDITIONS (Gemini review, 2026-09-28) — merge gated on §7.
**Date:** 2026-09-28
**Scope:** `wit/waddle-bundle/stage.wit`, `core/bundle_executor`, `core/svc_process`, `core/svc_action`, `core/bundle_compiler`, `hub_api/services/bundle_manifest_v2.py`, `sdk/waddle-sdk-rs`, `sdk/waddle-sdk`, future C# SDK (`feature/csharp-bundle-csping`).
**Drivers:** PenguinTwitchBot porting inventory Gap #1 (scheduled jobs), Gap #2 (moderation/Helix write actions), and the `db.execute` single-statement limitation (§3 of the inventory); relay-authz (`2026-09-28-connections-credentials-design.md` §6) as the enforcement point for any new outbound-write capability; the partition-lease + server-side Lua fencing model (`2026-09-28-dataplane-scale-design.md` §1–2) as the reusable primitive for a scheduler.

## 1. Scheduled triggers

**Who schedules:** manifest-declared only (`stages.scheduled.cron` *or* `interval-seconds`, mutually exclusive) — no runtime API. Matches the existing manifest-driven model for `consumes`/`egress`/`data.tables`: a schedule is reviewable at onboarding, not creatable by a running bundle.

**Where stored:** new hub-api table `bundle_schedules(tenant_id, community_id, app_id, cron, interval_seconds, PRIMARY KEY(tenant_id, community_id, app_id))`, materialized **per activation** (row exists only once a tenant/community activates that app version), same lifecycle as `app_source_bindings`.

**Who fires them:** a scheduler role added to `core/svc_ingest`, reusing dataplane-scale's fixed-partition + Valkey-lease + server-side-Lua-epoch-fencing model verbatim: `N_SCHED_PARTITIONS` fixed partitions, `partition(schedule_id) = rendezvous_hash(schedule_id) % N`, watermark-poll supervisor for hot add/remove. A fire XADDs a `scheduled-tick` envelope onto the normal partition-stream ingress path — same transport, same at-least-once contract, same idempotency-by-key discipline as every other event (spec §5.4); **no exactly-once claim**. Dedup key = `schedule_id:scheduled_for` (the schedule boundary, not wall-clock fire time), so a redelivered tick collapses correctly.

- **Jitter:** ±10% of interval (cron: ≤30s), spread per-replica — avoids thundering-herd on shared minute boundaries (many bundles on `* * * * *`).
- **Per-tenant quotas:** 60s floor on `interval_seconds` enforced at manifest-validation time; concurrent-scheduled-bundle cap per tenant via the same `UsageBatcher` token-bucket already metering outbound (connections-credentials §5).
- **Missed-fire policy: skip-and-log, never backfill.** If the owning partition was unavailable across one or more `scheduled_for` boundaries (lease lost, replica down), those firings are dropped with WARN + a counter; the next fire proceeds from "now" on the schedule's own cadence. Consistent with the rest of the design's "bounded, no catch-up storm" posture.
- **Herd control: registration-time offset, not just fire-time jitter.** Each schedule's phase offset within its interval is `hash(tenant_id, schedule_id) % interval`, fixed at registration — spreads load deterministically instead of relying on jitter alone to desync every `* * * * *` bundle. The firing path itself is a **bounded queue with an SLA**: a tick that cannot be dispatched within its SLA window is dropped, never queued unboundedly: drops are counted and alerted, never silent.
- **Idempotency: a unique constraint, not just a dedup key.** The firing transaction inserts into `bundle_schedule_fires(schedule_id, scheduled_for, fire_id)` under `UNIQUE(schedule_id, scheduled_for)` before XADD — a duplicate fire attempt (e.g. two replicas racing a lease handoff) fails the insert and is dropped, not delivered twice. The generated `fire_id` is passed to the bundle (see `tick` below) as its own idempotency handle, distinct from the host's dedup key.

```wit
interface scheduled-stage {
  use types.{platform-event, unsupported-stage};
  /// `scheduled-for` is the schedule boundary (dedup key material);
  /// `fired-at` is wall-clock actual fire time; `fire-id` is the unique
  /// row id from `bundle_schedule_fires`, the bundle's own idempotency
  /// handle distinct from the host's dedup key. Returning `some` emits a
  /// platform-event back onto the normal downstream path (e.g. an
  /// announcement); `none` means "side effects only" (e.g. a `db` cleanup).
  tick: func(schedule-id: string, scheduled-for: string, fired-at: string, fire-id: string)
    -> result<option<platform-event>, unsupported-stage>;
}
```

`bundle-context.message-id` is populated with the tick's dedup key, so a scheduled tick is idempotency-anchored exactly like a platform event — no parallel idempotency mechanism.

## 2. Moderation / platform write actions

Typed, action-stage-only capability — same "granted only to action-stage bundles" rule as `relay` — additionally gated by **relay-authz** (connections-credentials §6): the call must resolve `(tenant, community, app_id, provider, channel)` to an `app_outbound_bindings` row via `community_connections`, exactly the same O(1) hash-map check svc-action already performs for `relay.push`. No new authz primitive — moderation reuses relay-authz's binding table and its shadow-then-enforce rollout (§6.2), never shipping with a bypass flag.

```wit
interface moderation {
  variant error { denied(string), unsupported(string), backend(string) }
  record redemption-status { fulfilled, canceled }

  timeout: func(user: string, seconds: u32, reason: option<string>) -> result<_, error>;
  ban: func(user: string, reason: option<string>) -> result<_, error>;
  unban: func(user: string) -> result<_, error>;
  delete-message: func(message-id: string) -> result<_, error>;
  update-redemption-status: func(redemption-id: string, status: redemption-status) -> result<_, error>;
  send-announcement: func(text: string, color: option<string>) -> result<_, error>;
}
```

Args are platform-agnostic (login/id, duration, reason) — never a raw Helix/Discord payload. Per-platform mapping happens host-side in a new `core/svc_action::moderation` module (sibling to `capabilities.rs`), same abstraction level as `relay.push(provider, ...)`. Per call, in this order: (1) relay-authz check — **destructive ops (`ban`/`unban`/`delete-message`/`timeout`) re-verify synchronously against the primary, bypassing the relay-authz cache entirely**; non-destructive ops (`update-redemption-status`, `send-announcement`) may use the cached verdict (§6.1 connections-credentials) — deny+audit on miss either way; (2) **per-tenant rate-limit/quota check via the existing `UsageBatcher` token bucket — before any external platform call is made**, so a throttled bundle never partially executes a moderation action; (3) credential broker resolve for the bound `community_connections`/`sources` row; (4) platform call (Twitch Helix Ban Users / Delete Chat Messages / Update Redemption Status; Discord REST guild ban/timeout/message delete); (5) unconditional audit row — new `bundle_moderation_actions(tenant_id, community_id, app_id, platform, op, target, actor, result, occurred_at)`. An unsupported platform/op pair (e.g. `update-redemption-status` on Discord) returns `unsupported`, not a WIT-level failure — new platforms add rows, not WIT changes.

## 3. DB batch

```wit
/// Same statement text with $1..$n placeholders as `execute`. Atomic:
/// one Postgres transaction, all-or-nothing.
execute-batch: func(statements: list<tuple<string, list<value>>>) -> result<list<rows>, error>;
```

Added to the existing `db` interface. Runs inside a single transaction opened stage-side, same `data.tables` allowlist checked per statement — one out-of-scope table fails the whole batch, nothing partially commits.

- **Timeouts, independent of the WASM invoke deadline.** The invoke's `timeout_ms` bounds the whole host-call round trip, but Postgres gets its own, smaller, configurable budget so a slow query can't ride the invoke timeout to its limit: `SET LOCAL statement_timeout = '<EXECUTOR_DB_STATEMENT_TIMEOUT_MS, default 2000>'` per statement and `SET LOCAL lock_timeout = '<EXECUTOR_DB_LOCK_TIMEOUT_MS, default 1000>'` for the transaction, both set before the first statement executes. Statement count capped (`EXECUTOR_DB_BATCH_MAX_STATEMENTS`, default 20).
- **RLS context is transaction-scoped and connection-return-safe.** `SET LOCAL waddles.tenant`/`waddles.community` is used specifically because it auto-resets at `COMMIT`/`ROLLBACK` — never `SET` (session-scoped, would leak). On top of that, the connection pool's checkin path runs `DISCARD ALL` under a panic-safe guard (`Drop` impl / `finally`-equivalent) so a panicking or early-returning handler can never return a connection to the pool with residual session state. A dedicated test asserts RLS context never leaks across two sequential checkouts of the same pooled connection under different tenants.
- **Defense-in-depth ahead of Postgres: `sqlparser-rs` AST validation.** Every statement is parsed (Postgres dialect) before it reaches the database; the validator walks the AST against the manifest's `data.tables` allowlist and rejects any statement whose referenced relations aren't fully enumerable from the AST alone — CTEs, subqueries, and comment-obfuscation tricks that could otherwise reach a disallowed object are rejected outright rather than pattern-matched. This sits *in addition to* RLS, not instead of it — RLS remains the authoritative boundary if the AST check has a gap.

Isolation is identical to single `execute` (per-bundle Postgres role + RLS) — atomicity is the only new property, not a new isolation primitive.

## 4. Capability declaration

New manifest v2 top-level `capabilities: [scheduled, moderation, db-batch]`, required alongside (not instead of) the existing implicit signals — `data.tables`/`egress`/`consumes` remain the source of truth for those, but scheduled/moderation/db-batch have no other self-describing field. `hub_api/services/bundle_manifest_v2.py`: reject `stages.scheduled` without `scheduled` in `capabilities`; reject `moderation`/`db-batch` against `wit-world: stage` (1.0.0, §5). Runtime: `StageCapabilities::handle` in `svc_process`/`svc_action` denies any host-call for these three ops unless the bundle's loaded manifest (cached with the compiled component) lists the capability — defense in depth independent of the compiler's own import allowlist.

## 5. Versioning & compat

Bump the package to `waddle:bundle@1.1.0` in the same file, add a **second world** `stage-v1_1` (extended `db`, new `moderation`, new `scheduled-stage` export) alongside the frozen, untouched `world stage` (1.0.0) — `wasm-tools component wit` supports multiple worlds per package; existing 1.0.0 components keep resolving against the unmodified world. New required manifest field `wit-world: stage | stage-v1_1`, defaulting to `stage` for every existing `bundle.yaml`.

| Consumer | Change |
|---|---|
| `core/bundle_executor` | Second `bindgen!` output for `stage-v1_1`; loads whichever world the compiled component actually exports, instantiates against the matching `Linker`. **Linker isolation is per-component, not global**: a `Linker` is built fresh per instantiation and registers only the host functions belonging to that component's declared `wit-world` *and* its manifest's declared `capabilities` — a `stage` (1.0.0) component's linker never has `moderation`/`scheduled-tick`/`db.execute-batch` registered at all, so it cannot link those imports even if a malformed/tampered component tried to declare them; any import the component requests that isn't in that reduced linker fails at instantiation, before any guest code runs, not at first call. |
| Rust SDK (`waddle-sdk-rs`) | Regenerate via `wit-bindgen` against `stage-v1_1`; new Cargo feature `stage-v1_1` (default off) so 1.0.0 bundles' `Cargo.lock` is untouched. |
| Python SDK (`waddle-sdk`) | `componentize-py -w stage-v1_1`; new `waddle_sdk.moderation`/`waddle_sdk.scheduled` modules, existing imports unchanged. |
| C# SDK (`componentize-dotnet`, `feature/csharp-bundle-csping`) | Targets `stage-v1_1` from first landing — no 1.0.0 C# bundle exists yet, so no compat burden. |
| `bundle_compiler::validate` | Per-world-version import allowlist (`ALLOWED_IMPORTS_V1_0`/`_V1_1`), keyed off `wit-world`. |

## 6. Increment plan (security-first, each independently shippable)

1. `stage.wit` package bump: add `stage-v1_1` world (extended `db`, `moderation`, `scheduled-stage`) beside frozen `stage`; `wasm-tools component wit` parses clean; no consumer wired.
2. Manifest v2 `capabilities:` + `wit-world` fields; hub-api validator cross-checks (§4).
3. Executor dual-world support (`core/bundle_executor` loads either world) — 1.0.0 bundles regression-tested unaffected.
4. `db.execute-batch` wired end-to-end, including `statement_timeout`/`lock_timeout`, the `sqlparser-rs` AST validator, and the pooled-connection RLS-reset guard (lowest risk: no new outbound-write surface, no relay-authz dependency, but not shippable without §7.2/§7.3/§7.4).
5. `moderation` wired through svc-action, **shadow/log-only first** (mirrors relay-authz §6.2), rate-limit-before-call and destructive-ops-bypass-cache ordering built in from the first landing (not a follow-up), ships only after connections-credentials increment 7 (relay-authz enforcement) is live — never before, never with a disable flag once enforced.
6. Scheduled triggers: `bundle_schedules`/`bundle_schedule_fires` tables + hub-api activation-driven CRUD → svc-ingest partition-lease scheduler with registration-time offset spread and a bounded, SLA'd firing queue → `scheduled-stage` firing on the normal ingress path. Ships behind `waddles.core.disable-scheduled-triggers` (opt-out, not security-critical).
7. Rust + Python SDK `stage-v1_1` bindings, once 3–6 are stable.
8. C# SDK on `stage-v1_1` (first landing).
9. Reference bundles: scheduled cleanup, moderation-on-keyword, raffle-draw via `db.execute-batch` — closes the porting inventory's "no example yet" gaps.

## 7. Implementation conditions (merge gates)

Each condition below gates the increment it names — a PR landing that increment without satisfying its condition does not merge, regardless of test pass/fail status elsewhere.

1. **Moderation ordering (gates #5).** The rate-limit/quota check runs strictly before the external platform call, proven by a test that a throttled tenant's moderation call makes zero outbound HTTP requests.
2. **DB batch timeouts (gates #4).** `statement_timeout`/`lock_timeout` are set per batch, independent of and smaller than the WASM invoke deadline; a test asserts a deliberately slow statement is killed by `statement_timeout` well before the invoke's own timeout would fire.
3. **RLS leak-proofing (gates #4).** `SET LOCAL` only, never session-scoped `SET`; connection checkin runs `DISCARD ALL` behind a panic-safe guard; a regression test checks out the same pooled connection twice under two different tenants and asserts zero cross-tenant RLS state survives between them.
4. **SQL AST validation (gates #4).** `sqlparser-rs` parses every batch statement against the Postgres dialect and rejects any statement whose referenced relations cannot be fully enumerated from the AST (CTEs, subqueries, comment tricks) before the statement reaches Postgres; a negative-test suite of known SQL-obfuscation patterns is part of this gate, not a follow-up.
5. **Linker isolation (gates #3).** A per-instantiation `Linker` is built from the component's `wit-world` + manifest `capabilities` only; a test instantiates a `stage` (1.0.0) component and asserts instantiation fails (not "the call fails") if that component's imports include anything from `moderation`/`scheduled-tick`/`db.execute-batch`.
6. **Scheduler herd control (gates #6).** Registration-time phase offset (`hash(tenant_id, schedule_id) % interval`) is applied at schedule creation, not left to fire-time jitter alone; the firing queue is bounded with an SLA, and drops past that SLA are counted and alerted — never silently discarded.
7. **Scheduler idempotency (gates #6).** `bundle_schedule_fires` carries `UNIQUE(schedule_id, scheduled_for)`, enforced inside the same transaction that produces the `fire-id` passed to `scheduled-stage.tick`; a test drives two concurrent fire attempts for the same boundary and asserts exactly one succeeds.
8. **Destructive-action authz freshness (gates #5).** `ban`/`unban`/`delete-message`/`timeout` re-check relay-authz synchronously against the primary (cache bypassed); `update-redemption-status`/`send-announcement` may use the cached verdict (§6.1 connections-credentials); a test forces a stale-cache ALLOW and asserts a destructive op still denies once the primary reflects revocation, while a non-destructive op is unaffected by the same staleness.
