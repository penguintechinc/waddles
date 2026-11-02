# Bundle MCP-Tools P3b — waddles `tool.invoke` Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (standing default) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a running bundle call `tool.invoke(target, tool, args) -> result` and have the host (stage) authorize it (P2 gate + P1 app→app matrix), route it to the target service bundle's exported tool, and return a typed result — the bundle-to-bundle MCP-like invocation layer.

**Architecture:** `tool.invoke` is a **stage-owned host call**, not executor logic. The guest calls a host import that rides the closed `CapabilityKind::Db` with op-prefix `tool.` (the established `economy.`/`reputation.` pattern). The stage's `dispatch()` routes `tool.*` to a new `handle_tool` that gates, enforces the invocation matrix, guards depth/cycle/self-call, resolves the target's platform-installed version, and sends a nested `InvokeBody{export: ExportKind::ToolCall}` frame; the executor runs the target's exported tool via a new `stage-tools` WIT world. Same-stage targets use a synchronous nested `conn.invoke`; cross-stage uses the Discord-ack queue precedent (deferred to a later task / only if needed).

**Tech Stack:** Rust (wasmtime, wit-bindgen, Tokio, Axum stages), the `core/bundle_capability_gate` crate (P2), the `waddle-sdk` (Python + Rust).

**Spec:** `docs/superpowers/specs/2026-10-29-bundle-mcp-tools-framework.md` (§5.4 invocation, §6 hard problems, §10 P3b). **Depends on:** P3a (penguin-libs `penguin-bundle-host` 0.4.0 @ rev `76741cb520b6e06be1a2792ff4a1af5e55920811`).

## Global Constraints

- **Pin penguin-bundle-host to `version = "=0.4.0"`, `rev = "76741cb520b6e06be1a2792ff4a1af5e55920811"`** in the six Cargo.tomls (`core/bundle_executor:80`, `core/bundle_host_http:28`, `core/svc_action:91`, `core/svc_ingest:112`, `core/svc_process:110`; `core/bundle_compiler:59` lags at `=0.2.1` — **bring it to 0.4.0 too** so the workspace is single-version, unless it fails to build, then leave it and note why). Update `Cargo.lock` by minimal resolution (restore base lock, add only the new rev — never `cargo generate-lockfile`).
- **`CapabilityKind` is closed in penguin-libs** → the new host import rides `Db` with op-prefix `tool.` (mirror `capabilities.rs:2054`). Do NOT add a `CapabilityKind` variant.
- **WIT worlds are frozen** — `stage`/`stage-next`/`stage-v2` records are baked into already-compiled components; do NOT edit them. Add a NEW `stage-tools` world (follow the `stage-v2` precedent: `bindgen!` in `engine.rs`, `WitWorld` variant `manifest.rs:23`, `GuestPre` variant `invoke.rs:123`, `build_linker_for` arm `engine.rs:304`, fixture wit copies).
- **Exactly one `Store::new`** — a pre-commit grep asserts a single hit at `new_bounded_store` (`invoke.rs:622`). The nested invoke reuses `run_guest`; do NOT add a second Store constructor.
- **No stubs / fail-loud** — `handle_tool` and `call_tool_call` must be fully wired real paths; an unimplemented branch fails loudly (the no-stubs gate is fatal on release merges).
- **Telemetry mandatory** — add `record_tool_call` to `metrics.rs` (count + latency histogram + outcome); a tool call that emits nothing fails the telemetry gate.
- **Zero-trust caller** — the stage validates every caller-supplied value (target id, tool name, args size, idempotency key); the app can NEVER name the community/tenant/actor (host-asserted `InvokeScope` passes them unchanged); the target runs under its OWN grants.
- **Depth ≤ 2, cycle detection, self-call rejected** before acquiring slots (`acquire_call_slots` deadlocks on a self/cycle bundle-semaphore). Host-computed remaining-budget becomes the nested call's `deadline_ms` (8s host ceiling; each nested call gets its own epoch deadline).
- **Tool writes require a caller-supplied idempotency key, retry-safe** (reuse the economy ledger idempotency mechanism).
- **ARGS/RESULT WIRE CONTRACT (pinned, Task 2 ruling 2026-10-30):** tool args and results are **JSON** (spec §8: a small JSON-Schema subset validated host-side), carried as **canonical JSON bytes inside the WIT `list<u8>`** at the guest boundary and as a `serde_json::Value` in the stage↔executor `InvokeBody.payload`. The executor result envelope is **`{"ok": <json>}`** on success and **`{"tool_error": <info>}`** on a tool error (non-JSON guest output → `MalformedFrame`). Tasks 3/4/5/6 MUST all use this exact encoding end-to-end — the Task 3 host import encodes the guest's JSON-bytes args into `payload`, Task 4 `handle_tool` validates the JSON, Task 5's nested invoke carries it, and the Task 6 SDK serializes/deserializes JSON ↔ native types + unwraps the `{ok}/{tool_error}` envelope. Do NOT base64 or treat args as opaque bytes. (Task 2 already implements this executor-side; Task 5 may extract the inline branch into `call_tool_call` and reconcile its test.)
- **Logging:** `tracing` (Rust), PII-free (log app_id/target/tool/community_id/outcome/counts, never raw args).

## Review Focus

- **`Connection::invoke` re-entrancy — RESOLVED by Task 0: SYNC NESTED INVOKE IS SAFE.** `host_api.rs:200-307` shows `Connection::invoke` is multiplexed (allocates an id, registers a oneshot in the `CorrelationTable`, awaits it — no lock across the await); host calls are `tokio::spawn`'d (`host_api.rs:657`) behind a permit cap (`:633`), so the handler is NOT on the task awaiting the outer invoke. Residual must-dos (now folded into Tasks 4/5): (1) `CapabilityHandler::handle` has no `Connection` → **inject `Arc<ConnectionRegistry>` and call `.active()` at call time**; (2) depth cap + cycle reject + nested deadline shorter than outer (outer holds executor slots at `invoke.rs:470`, so a cycle fails with `pool_exhausted` after `pool_wait`, not a hang); (3) build a fresh `InvokeScope` for the target app+version. Ruling notes: `.../notes/p3b-task0-connection-reentrancy.md`.
- **app→app bypass.** A gate grant for `A:t` must NOT alone authorize an app→app call — `handle_tool` must independently check caller `kind` + `requested_tools` vs the target's `exported_tools` (P1 manifest matrix): app→app forbidden, app→service / service→service allowed. Test: a malicious app bundle with a (somehow) present grant still gets `app→app forbidden`.
- **Self-call / cycle deadlock** on the per-bundle slot semaphore — reject self (`caller==target`) and cycles (target already in the call-chain) BEFORE `acquire_call_slots`. Test: `A→A` and `A→B→A` both fail fast with a typed error, no hang.
- **Budget exhaustion across the chain** — depth cap 2 enforced; the nested `deadline_ms` = `min(remaining_outer_deadline, tool_cap)`; over-depth → `ToolDepthExceeded`, over-budget → `ToolTimeout`. Test: depth-3 rejected; a slow target trips the host timeout, not the WASM trap.
- **Version/digest pin mismatch** — the nested invoke resolves the target to its platform-installed `app_version` + `verify_digest`; a stale/tampered/unknown target is rejected (`ToolNotGranted`/`ToolTargetUnavailable`), reusing the live `app_version` pinning (`scope.rs:144`, `gate.rs:1202`). Test: a target digest that doesn't match the approved grant is refused.

---

### Task 0: Verify `Connection::invoke` nested re-entrancy (spike → ruling)

**Files:**
- Read: `core/bundle_executor/src/` connection/transport module (find `Connection::invoke`), `invoke.rs:978 on_invoke`, `spine.rs:448`, `dispatch.rs:477`.
- Write: `docs/superpowers/plans/notes/p3b-task0-connection-reentrancy.md` (findings + ruling).

**Interfaces:**
- Produces: a documented ruling — "same-stage sync nested `conn.invoke` is safe" OR "must use a dedicated connection / ack-queue". Tasks 4-5 consume this ruling.

- [ ] **Step 1:** Read `Connection::invoke`'s signature and body: is it multiplexed by `invoke_id` (concurrent in-flight calls on one connection OK) or serialized (one outstanding call per connection)? Check whether a host-call handler runs on the same task that holds the outer `conn.invoke` future.
- [ ] **Step 2:** Write the findings + a go/no-go ruling to the notes file. If sync is unsafe, specify the alternative (dedicated per-call connection vs. the `await_discord_ack` queue pattern at `svc_action/capabilities.rs:1340`).
- [ ] **Step 3:** Commit the notes file. (No code; this gates Task 4/5 design.)

---

### Task 1: New `stage-tools` WIT world (import `tool.invoke` + export `tool-provider.call`)

**Files:**
- Create: `wit/waddle-bundle/tool.wit` (or add interfaces to a new world in `stage.wit` WITHOUT touching existing worlds) — `interface tool { invoke(target: string, tool: string, args: list<u8>, idempotency-key: option<string>) -> result<list<u8>, tool-error> }` (host import) and `interface tool-provider { call(name: string, args: list<u8>) -> result<list<u8>, tool-error> }` (guest export); `world stage-tools` importing the stage-next shared interfaces + `tool`, exporting `tool-provider`.
- Modify: `core/bundle_executor/src/engine.rs` (add `bindgen!` for `stage_tools_world` ~after :81, with `imports/exports: { default: async }` + `with:` remaps to the shared interface types), `build_linker_for` arm (:304), a `link_stage_tools_imports` (mirror `:132 link_stage_next_imports`).
- Modify: `core/bundle_executor/src/manifest.rs:23` (`WitWorld::StageTools` variant + its string mapping).
- Modify: `core/bundle_executor/src/invoke.rs:123` (`GuestPre::Tools(StageToolsPre<ExecState>)` variant + `GuestPre::new` arm :129).
- Create: fixture wit copies under the new fixtures' `wit/` (Task 7 builds the fixtures; this task just adds the world + a round-trip bindgen build).
- Test: `core/bundle_executor/tests/stage_tools_world.rs` (world compiles + a trivial fixture instantiates).

**Interfaces:**
- Consumes: the `stage-v2` world pattern (engine.rs:81), `ExecState` (host/mod.rs:32).
- Produces: `WitWorld::StageTools`, `GuestPre::Tools`, `tool-error` type (variant: `not-granted`, `forbidden`, `timeout`, `depth-exceeded`, `pool-exhausted`, `target-unavailable`, `invalid-args`), the `tool` host-import interface + `tool-provider` guest-export interface.

- [ ] **Step 1:** Write the `tool.wit` interfaces + `world stage-tools` (import `tool`, export `tool-provider`, import the shared log/clock/flags/db interfaces via `with:` remap).
- [ ] **Step 2:** Add `bindgen!` for the new world in `engine.rs`; add `WitWorld::StageTools` (`manifest.rs:23`) + `GuestPre::Tools` (`invoke.rs:123`/:129). It must compile with the existing worlds untouched.
- [ ] **Step 3:** Write `tests/stage_tools_world.rs` asserting the world type-checks and a minimal component (empty `tool-provider.call`) pre-links via `build_linker_for`. Run it to fail first (no linker arm), then add `link_stage_tools_imports` + the `build_linker_for` arm.
- [ ] **Step 4:** `cargo build -p bundle_executor && cargo test -p bundle_executor stage_tools_world` → green. clippy/fmt clean.
- [ ] **Step 5:** Commit.

---

### Task 2: Rev-bump to 0.4.0 + `ExportKind::ToolCall` executor arms (run the guest's exported tool)

**Files:**
- Modify: the six Cargo.tomls (Global Constraints) + `Cargo.lock` (minimal).
- Modify: `core/bundle_executor/src/invoke.rs` — fuel-pick match `:1104` (add `ToolCall` arm → new `fuel_limit_tool` config or reuse the dispatch limit), run_guest dispatch match `:1297/1323/1355` (add `ExportKind::ToolCall => self.call_tool_call(...)`), and the connector routing check that must now also reject `ToolCall` where connectors are rejected.
- Modify: `core/bundle_executor/src/connector_wire.rs:337` (add `ToolCall` to the reject arm).
- Create: `call_tool_call(&self, store, instance, args) -> Result<ResultBody, ErrorBody>` in `invoke.rs` — instantiates via `GuestPre::Tools` and calls the guest's `tool-provider.call(name, args)` export, reusing `new_bounded_store`/fuel/epoch like `call_dispatch`.
- Test: inline `#[cfg(test)]` in `invoke.rs` + `tests/stage_tools_world.rs` extension (a provider fixture's `call` returns bytes).

**Interfaces:**
- Consumes: `ExportKind::ToolCall` (P3a 0.4.0), `GuestPre::Tools` (Task 1), `new_bounded_store`, `acquire_call_slots`.
- Produces: `call_tool_call` — the executor-side "run a tool export" path; `ResultBody{payload, duration_ms, fuel_used}`.

- [ ] **Step 1:** Bump the six pins + `Cargo.lock`. `cargo build` will now FAIL at the three exhaustive `ExportKind` matches — expected (P3a is source-breaking).
- [ ] **Step 2:** Write a failing test: a `tool-provider` fixture whose `call("echo", b"hi")` returns `b"hi"`, invoked via an `InvokeBody{export: ExportKind::ToolCall, payload: {tool, args}}`.
- [ ] **Step 3:** Add the `ToolCall` arms (fuel `:1104`, dispatch `:1297`→`call_tool_call`, connector reject `:1355` + `connector_wire.rs:337`); implement `call_tool_call`.
- [ ] **Step 4:** `cargo build` green across the workspace; the fixture test passes. clippy/fmt clean.
- [ ] **Step 5:** Commit.

---

### Task 3: Executor host import — guest calls `tool.invoke` → `Db:"tool.invoke"`

**Files:**
- Create: `core/bundle_executor/src/host/stage_tools_tool.rs` — `impl tool::Host for ExecState` whose `invoke(target, tool, args, idempotency_key)` calls `self.call(CapabilityKind::Db, "tool.invoke", <encoded args>)` (mirror `host/stage_next_economy.rs`).
- Modify: `engine.rs` `link_stage_tools_imports` to `add_to_linker` the `tool` interface (mirror `:135`).
- Test: `tests/host_tool_import.rs` (the host import encodes the call and routes to a fake `HostBridge` that records `(Db, "tool.invoke", args)`).

**Interfaces:**
- Consumes: `ExecState` (`host/mod.rs:32`), `HostBridge::call` (`host/bridge.rs:22`), `CapabilityKind::Db`.
- Produces: the `tool.invoke` guest→host import, encoded as a `Db`/`tool.invoke` host call for the stage to route.

- [ ] **Step 1:** Write a failing test asserting a guest `tool.invoke(...)` surfaces at `HostBridge::call(Db, "tool.invoke", args)` with the args (target/tool/idempotency) encoded (canonical JSON payload, matching `InvokeBody.payload`).
- [ ] **Step 2:** Implement `impl tool::Host for ExecState` + wire `add_to_linker` in `link_stage_tools_imports`.
- [ ] **Step 3:** Test green; clippy/fmt clean. Commit.

---

### Task 4: Stage-side `handle_tool` — gate + matrix + guards + resolve (svc_process, then svc_action twin)

**Files:**
- Modify: `core/svc_process/src/capabilities.rs` — add `op.starts_with("tool.")` arm at the prefix dispatch (`:2054`) routing to a new `handle_tool(&self, call: HostCallBody) -> ...` (near `handle_economy :976`).
- Modify: `core/svc_action/src/capabilities.rs` — the twin arm/handler (gate region `:1064+`).
- Use: `core/bundle_capability_gate` — `gate.authorize(&scope, PermissionId::ToolCall{target,tool}, ResourceRef::ToolScoped(ToolCallTarget{..}))` (`gate.rs:80`), P1 manifest `kind`/`requested_tools`/`exported_tools` lookups.
- Test: `core/svc_process/tests/handle_tool_gate.rs` (+ svc_action twin).

**Interfaces:**
- Consumes: `HostCallBody`, `InvokeScope` (host-asserted tenant/community/actor, `scope.rs:144`), the gate (`authorize`), P1 manifest records, `ensure_grants_loaded` (`pc.rs:2013`).
- Produces: a validated, authorized, target-resolved tool call ready for the nested-invoke helper (Task 5) — returns a typed `tool-error` on any failure. The call-chain (list of `(app_id, tool)`) is threaded for cycle/depth.

- [ ] **Step 1:** Write failing tests (table-driven): (a) app→service with a valid grant + matching `requested_tools`/`exported_tools` → authorized; (b) **app→app → `forbidden` even with a grant present**; (c) grant for `A:t` used to call `B:u` → `ResourceScopeMismatch`; (d) self-call `A→A` → `forbidden`/`cycle`, no slot acquire; (e) cycle `A→B→A` → `cycle`; (f) depth 3 → `depth-exceeded`; (g) unknown/stale target digest → `target-unavailable`; (h) oversized args / missing idempotency-key on a write tool → `invalid-args`.
- [ ] **Step 2:** Implement `handle_tool`: parse + validate caller inputs (zero-trust); load grants (`ensure_grants_loaded`); check the P1 matrix (caller.kind vs target.kind: app→app forbidden); `gate.authorize(ToolCall/ToolScoped)`; self/cycle/depth guard on the carried chain BEFORE any slot work; resolve target → platform-installed `app_version` + `verify_digest`; compute remaining budget. Hand off to Task 5's helper for the actual invoke.
- [ ] **Step 3:** Mirror into `svc_action/capabilities.rs`. (If the two handlers share >~30 lines, extract a shared fn into a common crate/module rather than duplicating — DRY.)
- [ ] **Step 4:** Tests green both stages; clippy/fmt clean. Commit.

---

### Task 5: Nested-invoke helper `invoke_tool_call` (same-stage sync per Task 0 ruling)

**Files:**
- Create: `invoke_tool_call(conn, target_app_id, target_digest, target_app_version, tool, args, deadline_ms, call_chain, trace, capabilities)` beside `spine.rs:448 invoke_transform` (svc_process) / `dispatch.rs:477 invoke_dispatch` (svc_action).
- Modify: `handle_tool` (Task 4) to call it and map the `ResultBody`/`ErrorBody` to the `tool-error` surface.
- Test: `tests/tool_nested_invoke.rs` (same-stage round-trip caller→provider through the stage).

**Interfaces:**
- Consumes: the Task 0 ruling (sync vs. ack-queue), `conn.invoke(InvokeBody, Arc<dyn CapabilityHandler>)`, `ExportKind::ToolCall`, `call_tool_call` (Task 2).
- Produces: `Result<Vec<u8>, ToolError>` — the typed tool result back to the caller guest.

- [ ] **Step 1:** Write a failing e2e-ish test: a caller fixture invokes a provider fixture's tool through the real stage path; assert the returned bytes + that depth/budget are carried in the `InvokeBody`.
- [ ] **Step 2:** Implement `invoke_tool_call` per the Task 0 ruling — build `InvokeBody{app_id: target, digest: target_digest, export: ExportKind::ToolCall, payload: {tool, args, call_chain, idempotency_key}, deadline_ms: remaining, trace}` and `conn.invoke`; enforce the host `min(remaining, 8s)` timeout with a typed `ToolTimeout` on elapse (do NOT rely on the WASM trap). If Task 0 ruled sync unsafe, implement the dedicated-connection or ack-queue path instead.
- [ ] **Step 3:** Tests green; the timeout path returns `ToolTimeout`; clippy/fmt clean. Commit.

---

### Task 6: SDK — tool client + service tool-export registry (Python + Rust)

**Files:**
- Create: `sdk/waddle-sdk/src/waddle_sdk/tool.py` — `async def invoke(target, tool, args, *, idempotency_key=None) -> bytes|obj` (client) + a `@tool("name")` decorator registry and a `dispatch(name, args)` the service bundle's `tool-provider.call` delegates to. Mirror `economy.py`.
- Modify: `sdk/waddle-sdk/src/waddle_sdk/testing.py` — a fake tool host (record calls, scripted responses) for unit tests.
- Create: `sdk/waddle-sdk-rs/src/tool.rs` — the Rust mirror (client + `#[tool]`-style registry or a `register_tool` fn); export from `lib.rs`.
- Test: `sdk/waddle-sdk/tests/test_tool.py` + Rust `#[cfg(test)]` in `tool.rs`.

**Interfaces:**
- Consumes: the `tool` import + `tool-provider` export bindings (Task 1).
- Produces: the ergonomic surface bundles use — `await economy_is_a_bundle.invoke("waddles.core.economy", "adjust_balance", ...)` client-side; `@tool("adjust_balance")` server-side.

- [ ] **Step 0 (framework fix carried from Task 4+5):** `tool-provider.call(name, args)` has no idempotency param, and the stage currently does NOT forward `tool.invoke`'s `idempotency_key` into the args the target guest receives — so a target bundle can't honor it. Fix: the stage (`core/bundle_tool_call` / the nested-invoke payload build) merges `idempotency_key` into the forwarded args JSON under a reserved field (e.g. `"__idempotency_key"`); the service-side SDK helper reads it from there and passes it to the host cap (economy/reputation). Add a test that the key round-trips to the target handler. Without this, P4 write-idempotency is broken.
- [ ] **Step 1:** Write failing SDK tests (client encodes/decodes; registry routes `call("echo", ...)` to the decorated fn; unknown tool → typed error; idempotency-key round-trips).
- [ ] **Step 2:** Implement `tool.py` + `testing.py` fake; implement `tool.rs`.
- [ ] **Step 3:** `make test` for the SDK (Python + Rust) green; lint clean. Commit.

---

### Task 7: Real-path e2e + reference bundles + telemetry

**Files:**
- Create: `core/bundle_executor/tests/fixtures/tool-provider-fixture-src/` (a service bundle exporting `echo` + a guarded `privileged_op`) and `tool-caller-fixture-src/` (an app bundle invoking them), each with its own `wit/` copy — built like `economy-fixture-src`.
- Create: `core/bundle_executor/tests/tool_invoke_e2e.rs` (+ a Python `python_tool_e2e.rs` if the fixture is Python) — the flag-on real-path e2e.
- Modify: `core/bundle_executor/src/metrics.rs` — `record_tool_call(target, tool, outcome, duration)` (counter + latency histogram); call it in the invoke path.
- Test: the e2e asserts the full matrix + telemetry emission.

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: the merge-gating real-path proof + the telemetry signal.

- [ ] **Step 1:** Build the two fixtures (caller app + provider service) with real tool export/invoke.
- [ ] **Step 2:** Write `tool_invoke_e2e.rs`: (a) happy path app→service returns the result; (b) app→app denied; (c) missing grant denied; (d) timeout path; (e) depth-limit; (f) digest/version pin mismatch denied; (g) self-call guarded. Assert `record_tool_call` emits ≥1 count + ≥1 histogram point (telemetry gate).
- [ ] **Step 3:** Add `record_tool_call` to `metrics.rs` + call sites; wire it so the e2e's telemetry assertions pass.
- [ ] **Step 4:** Full suite `cargo test -p bundle_executor -p svc_process -p svc_action` + SDK tests green; clippy/fmt/security clean. Commit.

---

## Execution Handoff

- All tasks land on a single `feature/bundle-tool-invoke` worktree off `origin/release/v3.0.X`; open ONE PR into `release/v3.0.X` when the whole-branch review is green; auto-merge is pre-authorized when green (capability/error-path change → adversarial review applies, per `merging-to-release`).
- **Carries a pre-existing dependency on P3a's rev being reachable on origin** (it is: `76741cb5`). The formal penguin-libs 0.4.0 release-line merge remains a user-flagged item; the rev-pin is valid meanwhile (immutable SHA).
- After merge, P4 (economy → WASM bundle) and P5 (reputation) can start — they consume this framework's `tool-provider` export + SDK.

*Plan written 2026-10-30 against `origin/release/v3.0.X` recon. Task 0 (connection re-entrancy) gates the Task 4/5 sync design — do it first.*
