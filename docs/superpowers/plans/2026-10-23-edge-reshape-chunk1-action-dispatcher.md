# Edge-Reshape Chunk 1a: Action Dispatcher + Egress Consolidation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce an action-dispatcher abstraction in `svc-action` that routes a bundle's output op to a destination **by state ownership**, and move the one remaining inline platform egress (Discord `chat.send`) onto the existing stateful outbound-drain route behind a default-OFF flag — the seam slash-command Phases 3–5 build on.

**Architecture:** `svc-action` today performs Discord `chat.send` inline (a pinned bot-REST POST in `capabilities.rs::handle_discord_relay`), while `chat.delete`/`dm.send` already queue to the `svc-ingest` outbound drain with an `op_id` ack handshake. This plan adds a small `Destination` router (route-by-state-ownership) whose only Chunk-1a route is **StatefulPlatformReply → the existing drain queue**, then routes `chat.send` through it — reusing the existing `handle_discord_queued_op` machinery (`build_outbound_envelope`/`stamp_handshake`/ack-wait). Flag OFF = today's exact inline behavior; flag ON = the dispatcher path, shadow-parity-validated. Fail-loud on an op that resolves to no destination.

**Tech Stack:** Rust (Tokio), `svc-action` crate (`core/svc_action`), `svc-ingest` outbound drain (`core/svc_ingest/src/outbound*.rs`), penguin-spine (pinned, **not modified**), Valkey.

**Spec:** `docs/superpowers/specs/2026-10-23-edge-reshape-gateway-dispatcher-design.md` (Chunk 1; §6.2 dispatcher, §6.4 PII scope, §8 Chunk-1 items 1–5).

## Global Constraints

- **penguin-spine is pinned external (rev `ce638ec`, `=0.2.0`) — DO NOT bump, DO NOT add a stage.** `StageEnvelope.stage` is `Process|Action` only; `Stage::parse` rejects `"ingest"`. No `:gateway` stage. This plan touches **no** envelope/spine code.
- **Do NOT rename the `waddles.ingest.*` app_id data namespace** (baked into the app_id regex, grants, source namespace — a data migration). The `svc-ingest`→`svc-gateway` *service* rename is a **separate follow-on plan** (`edge-reshape-chunk1b`), out of scope here.
- **Reuse, do not rebuild, the outbound drain + ack handshake.** `OutboundAction::ChatSend{channel,text}` already exists (`core/svc_ingest/src/outbound_ops.rs`); the producer template is `capabilities.rs::handle_discord_queued_op` (:1409) with `build_outbound_envelope`/`stamp_handshake` (:412) and `await_discord_ack` (:1285). Queue `waddles:transport:irc:discord:outbound`; ack `waddles:transport:discord:ack:<op_id>`.
- **Detokenization stays in `svc-action` for Chunk 1a** (via `detokenize_text`, :965, gated by `DISABLE_PII_DETOKENIZATION_FLAG`), exactly as the existing `dm.send` queued path already does. Moving detokenization into the gateway/drain (spec Q-C, "gateway sole detokenizer") is a tracked **follow-on** — it requires `svc-ingest` to gain a hub detok client and must not regress this parity. Deferring it does not worsen today's PII-on-queue posture (`dm.send` already queues detokenized text).
- **Channel is never bundle-supplied.** The Discord target stays `scope.origin_channel_id` (SSRF pinning preserved on the new route).
- **New flag is default-OFF via the `svc-action` `flags.rs` env-baseline pattern** (`resolve_flag`, `FLAG_`+KEY uppercased with `.`/`-`→`_`) plus the opt-out kill-switch convention; it is NOT a license flag.
- **Rootless; OTel logs+metrics+traces on the new path; no-stubs / fail-loud; 90%+ coverage; real-path (flag-on) tests.** No `.unwrap()`/`.expect()` outside tests; `Result` propagation; `cargo fmt`/`clippy`/`deny` clean.

## Review Focus

- **No-destination op must fail loud, not silently drop** — an op whose provider/op has no route returns a `DispatchError`, emits an error metric, and NACKs/dead-letters; it never disappears. (Task 1 + Task 4 tests.)
- **Flag OFF is byte-for-byte today's inline behavior** — same REST call, same channel pinning, same usage metering, same ack/no-ack semantics; a regression here breaks live alpha. (Task 3 parity test, OFF arm.)
- **Flag ON preserves the SSRF channel pinning** (`scope.origin_channel_id`, never bundle text) and the grant-gate ordering (authorize before route). (Task 3 test.)
- **Ack-handshake failure still surfaces as `relay_unconfirmed`** on the new route (a missing ack is not treated as success). (Task 3 test.)
- **Usage metering records exactly once** (`record_relay_call`) on the new route — not zero, not doubled. (Task 3 test.)

---

### Task 1: `Destination` model + state-ownership router

**Files:**
- Create: `core/svc_action/src/dispatch_destination.rs`
- Modify: `core/svc_action/src/lib.rs` (add `mod dispatch_destination;`)
- Test: inline `#[cfg(test)]` module in `dispatch_destination.rs`

**Interfaces:**
- Consumes: `RelayOp` (`capabilities.rs:~305`, variants `ChatSend`/`ChatDelete`/`DmSend`), the provider string set `RELAY_PROVIDERS` (`capabilities.rs:~245`).
- Produces:
  - `pub enum Destination { StatefulPlatformReply { provider: String, op: RelayOp } }` (Chunk-1a has exactly this one variant; Chunk 2/3 add `ExternalHttp`/`Lambda`/`Gcp`/`Overlay`/`Streaming`).
  - `pub enum DispatchError { NoDestination { provider: String, op: RelayOp } }` (impl `std::error::Error` + `Display`, message names provider + op wire string, **no bundle text**).
  - `pub fn route_destination(provider: &str, op: RelayOp) -> Result<Destination, DispatchError>` — returns `StatefulPlatformReply` for every `(provider, op)` the existing `relay_op_supported` allows (`("twitch",ChatSend)`, `("discord",ChatSend|ChatDelete|DmSend)`); every other pair → `Err(NoDestination{..})`. This is the single state-ownership decision point; it does NOT send anything.

- [ ] **Step 1: Write the failing tests** (table-driven, rstest or a match over cases)

```rust
// supported pairs route to StatefulPlatformReply with provider+op preserved
#[test] fn routes_supported_relay_ops_to_stateful_reply() { /* ("discord",ChatSend)->StatefulPlatformReply{provider:"discord",op:ChatSend}; all 4 supported pairs */ }
// an unsupported pair is a loud NoDestination, never a default/drop
#[test] fn unsupported_pair_is_no_destination_error() { /* ("twitch",DmSend) and ("slack",ChatSend) -> Err(NoDestination) */ }
// route_destination agrees with relay_op_supported for every (provider, op) in RELAY_PROVIDERS x RelayOp
#[test] fn route_matches_relay_op_supported_allowlist() { /* parity guard: iterate the cross product, assert Ok iff relay_op_supported(provider,op) */ }
// Display carries provider + op wire-string, never arbitrary text
#[test] fn no_destination_display_has_no_payload() { /* assert message contains "twitch"/"dm.send", and is stable */ }
```

- [ ] **Step 2: Run to verify it fails** — `cargo test -p svc-action dispatch_destination` → FAIL (module/symbols missing).
- [ ] **Step 3: Implement** `Destination`, `DispatchError`, `route_destination` in `dispatch_destination.rs`. `route_destination` delegates the allow decision to the same predicate `relay_op_supported` uses (call it, or share a const table) so the two can never drift — the `route_matches_relay_op_supported_allowlist` test pins this.
- [ ] **Step 4: Run to verify it passes** — `cargo test -p svc-action dispatch_destination` → PASS.
- [ ] **Step 5: Commit** — `git add core/svc_action/src/dispatch_destination.rs core/svc_action/src/lib.rs && git commit -m "feat(svc-action): destination model + state-ownership router skeleton"`

---

### Task 2: Default-OFF dispatcher flag

**Files:**
- Modify: `core/svc_action/src/flags.rs` (add the flag key constant + a resolver helper following the existing pattern)
- Modify: `core/svc_action/src/capabilities.rs` (hold the flag on `StageCapabilities` via a builder, like `with_discord_ack_timeout`)
- Test: `#[cfg(test)]` in `flags.rs` (env-baseline resolution) + a builder test in `capabilities.rs`

**Interfaces:**
- Consumes: the `flags.rs` env-baseline mechanism (`resolve_flag(key, default)` at :555; `FLAG_`+key uppercased, `.`/`-`→`_`), the `FeatureFlag` trait.
- Produces:
  - `pub const EGRESS_VIA_DISPATCHER_FLAG: &str = "waddles.core.egress-via-dispatcher";` (unseen/absent ⇒ **OFF** ⇒ inline path).
  - A `StageCapabilities` builder `with_egress_dispatcher_flag(flag: Arc<dyn FeatureFlag>)` and an internal `egress_via_dispatcher(&self) -> bool` helper (defaults OFF when no flag wired).

- [ ] **Step 1: Write the failing tests**

```rust
// FLAG_WADDLES_CORE_EGRESS_VIA_DISPATCHER=true -> on; unset -> off; the env var name is exactly the baseline transform
#[test] fn egress_dispatcher_flag_env_baseline() { /* set/unset env, assert resolve_flag(EGRESS_VIA_DISPATCHER_FLAG, false) */ }
// StageCapabilities with no flag wired reports egress_via_dispatcher()==false (fail-safe to inline)
#[tokio::test] async fn caps_without_flag_default_off() { /* build caps, assert false */ }
// with a StaticFlag(true) wired, egress_via_dispatcher()==true
#[tokio::test] async fn caps_with_static_on_flag() { /* with_egress_dispatcher_flag(StaticFlag(true)) -> true */ }
```

- [ ] **Step 2: Run to verify it fails** — `cargo test -p svc-action flags egress` → FAIL.
- [ ] **Step 3: Implement** the constant + `egress_via_dispatcher` helper + the builder. Default OFF; license-flag path untouched (this is an operational toggle, env-baseline only).
- [ ] **Step 4: Run to verify it passes.**
- [ ] **Step 5: Commit** — `git commit -m "feat(svc-action): default-off egress-via-dispatcher flag"`

---

### Task 3: Route Discord `chat.send` through the drain when the flag is ON

**Files:**
- Modify: `core/svc_action/src/capabilities.rs` (`handle_discord_relay` :1610 — branch on `egress_via_dispatcher()`)
- Test: `#[cfg(test)]` in `capabilities.rs` (reuse the existing relay-test harness/mocks for `RelayQueue`, the ack store, and the detokenizer/gate fakes)

**Interfaces:**
- Consumes: `route_destination` (Task 1), `egress_via_dispatcher` + flag (Task 2); existing `handle_discord_queued_op` (:1409), `build_outbound_envelope`/`stamp_handshake` (:412), `await_discord_ack` (:1285), `detokenize_text` (:965), `UsageBatcher::record_relay_call` (`usage.rs:225`), `OutboundAction::ChatSend` (svc-ingest `outbound_ops.rs`, serialized form the drain parses).
- Produces: no new public surface — `handle_discord_relay`, when the flag is ON, routes `RelayOp::ChatSend` via `route_destination` → for `StatefulPlatformReply` builds the outbound `ChatSend{channel: scope.origin_channel_id, text}` entry and queues it through the same path `handle_discord_queued_op` uses (LPUSH + ack-wait), returning the same success/`relay_unconfirmed` result shape the inline path returns. Flag OFF → the current inline REST POST, unchanged.

- [ ] **Step 1: Write the failing tests** (both flag states, real queue/ack fakes — no network)

```rust
// FLAG ON: chat.send is LPUSHed to waddles:transport:irc:discord:outbound (not sent inline), channel == scope.origin_channel_id, text is detokenized
#[tokio::test] async fn flag_on_queues_chat_send_to_drain() {}
// FLAG ON: a posted ack {ok:true} => success; absence of ack => relay_unconfirmed (NOT success)
#[tokio::test] async fn flag_on_missing_ack_is_unconfirmed() {}
// FLAG OFF: unchanged inline path — the queue receives nothing, the inline transport is invoked once
#[tokio::test] async fn flag_off_keeps_inline_path() {}
// grant-gate runs BEFORE routing in both states — a denied grant never enqueues and never sends
#[tokio::test] async fn denied_grant_short_circuits_before_dispatch() {}
// usage is recorded exactly once on the ON route (record_relay_call called once)
#[tokio::test] async fn flag_on_meters_once() {}
// channel pinning preserved: bundle-supplied channel text is ignored; origin_channel_id is used
#[tokio::test] async fn flag_on_pins_channel_to_origin() {}
```

- [ ] **Step 2: Run to verify it fails** — `cargo test -p svc-action capabilities::...relay` → FAIL.
- [ ] **Step 3: Implement** the ON branch in `handle_discord_relay`: detokenize (as today) → `route_destination("discord", ChatSend)` → on `StatefulPlatformReply`, enqueue+await-ack via the extracted-or-shared queued-op helper; meter once; on `DispatchError::NoDestination` return a loud error (Task 4 handles the generic path, but keep this branch fail-loud). Keep the OFF branch exactly as-is. Preserve grant-gate ordering (authorize first).
- [ ] **Step 4: Run to verify it passes** — run the relay tests + the existing capabilities suite (no regression).
- [ ] **Step 5: Commit** — `git commit -m "feat(svc-action): route discord chat.send through outbound drain under flag"`

---

### Task 4: Fail-loud no-destination + shadow-parity harness

**Files:**
- Modify: `core/svc_action/src/capabilities.rs` (the relay entry `handle_relay` :982 — surface `DispatchError` as a NACK/dead-letter with a metric, never a silent success)
- Create: `core/svc_action/tests/egress_parity.rs` (integration test: dispatcher-ON vs inline-OFF produce the same observable effect for the same inputs)
- Test: as above + a metric assertion

**Interfaces:**
- Consumes: `DispatchError` (Task 1), the dead-letter/NACK path already used for unsupported ops, the metrics facade (`metrics_facade`).
- Produces: a `svc_action_dispatch_no_destination_total` counter (or reuse the existing unsupported-op metric) incremented on `NoDestination`; the parity harness comparing the two code paths.

- [ ] **Step 1: Write the failing tests**

```rust
// an op with no route is NACKed/dead-lettered and increments the no-destination metric; it is never acked as success
#[tokio::test] async fn no_destination_is_loud_not_dropped() {}
// PARITY: for a fixed (scope, text), the ON path's enqueued ChatSend{channel,text} equals the OFF path's inline POST target channel + body (shadow comparison), proving behavioral equivalence
#[tokio::test] async fn dispatcher_matches_inline_for_chat_send() {}
```

- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** the loud no-destination handling + metric, and the parity harness (capture both paths' outputs via the test fakes and assert equality; the ON path never performs a real send during the shadow comparison — it enqueues to a fake queue).
- [ ] **Step 4: Run to verify it passes.**
- [ ] **Step 5: Commit** — `git commit -m "feat(svc-action): fail-loud no-destination + egress parity harness"`

---

### Task 5: Telemetry, docs, and the slash extension seam

**Files:**
- Modify: `core/svc_action/src/dispatch_destination.rs` + `capabilities.rs` (OTel span/attributes on the route decision + enqueue)
- Modify: `core/svc_action/README.md` (or the crate module docs) — document the dispatcher, the flag, the one route, and the **extension seam** for slash (a future `Destination` variant / `OutboundAction` for interaction followups) and for Chunk 2/3 adapters.
- Test: an OTel emission assertion in the existing telemetry test harness (span emitted on dispatch; metric data point on route).

**Interfaces:**
- Consumes: `telemetry` module, `metrics_facade`.
- Produces: a `dispatch.route` span (attributes: `provider`, `op`, `destination`, `flag_on`) and the route/no-destination metrics; docs.

- [ ] **Step 1: Write the failing test** — assert a `dispatch.route` span + ≥1 metric data point are emitted when a chat.send is routed (ON path), via the test exporter.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** the span/metrics + write the docs (including: "slash Phases 3–5 add an `InteractionFollowup` `OutboundAction` + a `Destination` route here; the token/defer/followup host store is built in the slash plan, not this one").
- [ ] **Step 4: Run to verify it passes** — telemetry test + `cargo fmt --check` + `cargo clippy -- -D warnings` clean.
- [ ] **Step 5: Commit** — `git commit -m "feat(svc-action): dispatcher telemetry + docs + slash extension seam"`

---

## Notes for the executor

- This plan does **not** touch `svc-ingest` code, the spine envelope, hub-api, or Helm — it is contained to `svc-action` reusing the drain contract that already exists. (The `svc-ingest`→`svc-gateway` rename and the gateway-side detokenization move are separate follow-on plans.)
- Flag stays **OFF** at merge; turning it ON in alpha + the flag-on E2E is the validation step (no-stubs: the ON path is the real path, exercised by the real-queue tests here and an alpha flag-on check at rollout).
- Build/test in the containerized Rust toolchain; cargo artifacts on the external SSD / shared `CARGO_TARGET_DIR` per repo convention.
