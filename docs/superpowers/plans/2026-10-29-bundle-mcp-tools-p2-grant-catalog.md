# Bundle MCP-Tools — Phase 2 (Grant Catalog) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make `tool.call:<target>:<tool>` a first-class, authorizable permission — a parameterized gate `PermissionId` + `ResourceRef`, mirrored in the Python permission catalog, wired through the approval→grant path, and version-pinned — so a bundle's `requested_tools` (P1) become real grants the gate can authorize (P3 will invoke through them).

**Architecture:** Add `tool.call` to the CLOSED dual-mirror catalog: a parameterized Rust `PermissionId::ToolCall{target,tool}` (parsed via `strip_prefix("tool.call:")`, like `ChatSend`/`Moderation`) + a `ResourceRef::ToolScoped` variant, and the matching `resolve_risk`/`permission_family` branches in `bundle_permission_catalog.py`. The P1 `requested_tool_permission_id` string is the canonical id both sides must round-trip identically. Grants flow through the existing `community_permission_grants`/`app_permission_requests` tables + `PgGrantLoader`; fix the `app_version=0` placeholder so tool grants pin to the real `app_versions.id`.

**Tech Stack:** Rust (`bundle_capability_gate`, `svc_process`), Python 3.13 (`hub_api`), pytest + `cargo test` (mypy --strict, clippy/deny, ≥90% cov).

**Spec:** `docs/superpowers/specs/2026-10-29-bundle-mcp-tools-framework.md` (§5.3, §6.1). Builds on merged P1 (`requested_tools` + `requested_tool_permission_id` in `bundle_manifest_v2.py:534`).

## Global Constraints

- **Closed dual-mirror catalog — every new id lands in BOTH:** Rust `core/bundle_capability_gate/src/permission.rs` (`PermissionFamily` enum + `ALL` :306 + `id_prefix` :340 + `catalog_entry` :469 + `PermissionId` enum + `family()` + `canonical_id` + `parse` :941 — ~8 sites, enforced by the `every_family_has_a_catalog_entry` + `parse_round_trips` unit tests) AND Python `hub_api/services/bundle_permission_catalog.py` (`resolve_risk` :167 + `permission_family` :215 + `STATIC_PERMISSIONS`). Drift = unparseable manifest OR unauthorizable grant.
- **Identical encoding both sides:** the id encodes a percent-encoded target (`quote(target, safe='')`, `bundle_manifest_v2.py:540`). Rust `parse`/`canonical_id` MUST use the identical percent encoding or the round-trip breaks.
- **`tool.invoke` rides a `tool.` op-prefix on the existing `Db` WIRE `CapabilityKind`** (§6.1) — do NOT add a new wire `CapabilityKind` (that's the closed external penguin-libs enum). Only the in-repo gate `PermissionFamily`/`CapabilityKind` is extensible.
- **`ResourceRef` is matched exhaustively at 3 `gate.rs` sites** (:117, :187, :217) — a new variant forces edits at all three (compile-enforced).
- Caller tenant/community/app come from `InvokeScope` (host-asserted, never bundle-named, §6.3); only the target+tool+access are bundle-supplied (and re-validated).
- The app→app / app→service / service→service matrix stays enforced at activation (P1 Task 6); P2 adds a grant-time belt where cheap but does not move it.
- No new external deps; `cargo clippy -D warnings` + `cargo deny` + `cargo fmt` clean; mypy --strict clean.

## Review Focus

- **Encoding round-trip:** a target app_id containing `.` and a (rejected) `:` must `parse`→`canonical_id` back to the exact P1 string — a mismatch makes a granted permission unauthorizable. — Task 1.
- **Closed-catalog drift:** a `tool.call` id that parses in Rust but is unknown to Python `resolve_risk` (or vice-versa) → either an unparseable manifest or an ungated grant. — Tasks 1 & 3, with a cross-mirror test.
- **Unknown/garbage tool.call id** (missing tool, empty target, bad access) → `UnknownPermission`/`unknown_permission`, not a panic or a silent allow. — Tasks 1 & 3.
- **`app_version=0` placeholder** left in svc_process → a tool grant keyed on version 0 fails closed (no grant) OR matches the wrong version. — Task 5.
- **Grant lookup miss** (a `requested_tool` with no approved grant row) → `NotGranted` fail-closed, never a default-allow. — Task 4.

---

### Task 1: Rust `PermissionId::ToolCall{target,tool}` + catalog entry

**Files:**
- Modify: `core/bundle_capability_gate/src/permission.rs` (the ~8 sites named in Global Constraints)
- Test: the existing `permission.rs` `#[cfg(test)]` module (mirror `parse_round_trips` / the ChatSend/Moderation cases)

**Interfaces:**
- Produces: `PermissionFamily::ToolCall`; `PermissionId::ToolCall { target: String, tool: String }`; `parse("tool.call:<enc-target>:<tool>")`; `canonical_id()` reproducing the exact input string.

- [ ] **Step 1: Write failing tests** — round-trip `parse("tool.call:waddles.core.economy.default:get_balance").canonical_id() == "tool.call:waddles.core.economy.default:get_balance"` (note: the real target is percent-encoded — use the P1 encoding exactly); a dotted/encoded target round-trips; `parse("tool.call:x")` (no tool) and `parse("tool.call::get")` (empty target) → `UnknownPermission`/parse error; `catalog_entry(ToolCall)` returns the expected Risk/CapabilityKind/Quota.
- [ ] **Step 2: Run** `cargo test -p bundle_capability_gate tool_call` → FAIL.
- [ ] **Step 3: Implement** the variant across all ~8 sites: `PermissionFamily::ToolCall` (+ `ALL`, `id_prefix()="tool.call:"`, mark parameterized), `catalog_entry` (Risk per access — Dangerous for write, Normal/Dangerous policy per spec; `CapabilityKind::Db` with the `tool.` op semantics; a sensible default `Quota`), `PermissionId::ToolCall{target,tool}`, `family()`, `canonical_id()` (re-emit `tool.call:` + the SAME percent-encoding P1 uses), and the `parse` arm (`strip_prefix("tool.call:")`, split the last `:` to get tool, percent-decode+validate the target against the app-id rule, validate tool against the tool-name rule). Keep the catalog CLOSED (unknown → `UnknownPermission`).
- [ ] **Step 4: Run** `cargo test -p bundle_capability_gate` (incl. `every_family_has_a_catalog_entry` + `parse_round_trips`) → PASS.
- [ ] **Step 5: Commit** `feat(gate): tool.call parameterized PermissionId + catalog entry`.

### Task 2: `ResourceRef::ToolScoped` + gate authorize arms

**Files:**
- Modify: `core/bundle_capability_gate/src/resource.rs` (`ResourceRef` :93, `ResolvedResource` :104, the `is_*_scoped` predicates :394-420)
- Modify: `core/bundle_capability_gate/src/gate.rs` (the 3 exhaustive `ResourceRef` match sites :117, :187, :217)
- Test: gate authorize tests (mirror the Economy/Reputation scoped cases)

**Interfaces:**
- Consumes: Task 1's `PermissionId::ToolCall`.
- Produces: `ResourceRef::ToolScoped(ToolCallTarget { target_app_id: String, tool: String, access: ToolAccess })`; `ResolvedResource::ToolTarget`; `is_tool_scoped()`.

- [ ] **Step 1: Write failing test** — `authorize` with `PermissionId::ToolCall` + a matching `ResourceRef::ToolScoped` and a granted permission → `Allowed`; a scope-shape mismatch (ToolCall id with an EconomyScoped ref) → `ResourceScopeMismatch`; no grant → `NotGranted`.
- [ ] **Step 2: Run** `cargo test -p bundle_capability_gate authorize_tool` → FAIL.
- [ ] **Step 3: Implement** `ToolCallTarget` + `ToolAccess{Read,Write}`, the `ResourceRef::ToolScoped` + `ResolvedResource::ToolTarget` variants, `is_tool_scoped()`, and the 3 `gate.rs` match arms (resolve → scope-shape check against the ToolCall family → grant lookup by `canonical_id`). Caller identity (tenant/community/app) comes from the `InvokeScope` passed to `authorize`, not the target struct.
- [ ] **Step 4: Run** `cargo test -p bundle_capability_gate` + `cargo clippy -D warnings` → PASS/clean.
- [ ] **Step 5: Commit** `feat(gate): ResourceRef::ToolScoped + authorize arms`.

### Task 3: Python catalog mirror (`resolve_risk` / `permission_family`)

**Files:**
- Modify: `hub_api/services/bundle_permission_catalog.py` (`resolve_risk` :167, `permission_family` :215)
- Test: `hub_api/tests/` catalog test + a manifest-declaration test via `parse_permission_declarations` (`bundle_manifest_v2.py:599`)

**Interfaces:** Consumes P1's `requested_tool_permission_id` string shape. Mirrors Task 1's Rust risk classification exactly.

- [ ] **Step 1: Write failing tests** — `resolve_risk("tool.call:<enc-target>:get_balance")` returns the SAME risk tier Task 1 assigned (per access); `permission_family(...)` → `"tool.call"`; a declared `permissions: [{id: "tool.call:...", justification: ...}]` passes `parse_permission_declarations`; a malformed `tool.call:` id → `resolve_risk is None` → `unknown_permission`.
- [ ] **Step 2: Run** `pytest hub_api/tests -k tool_call` → FAIL.
- [ ] **Step 3: Implement** a `_TOOL_CALL_RE = r"^tool\.call:(?P<target>[^:]+):(?P<tool>[a-z][a-z0-9_.]{0,63})$"` branch in `resolve_risk` (returning the risk mirroring Rust — write=dangerous policy), and a `permission_family` → `"tool.call"` branch. Use `re.compile` at module level + `.fullmatch` (ReDoS/`$`-safety per house rule).
- [ ] **Step 4: Run** `pytest hub_api/tests -k "tool_call or permission_decl"` → PASS.
- [ ] **Step 5: Commit** `feat(hub): tool.call in the python permission catalog mirror`.

### Task 4: requested_tools → approved grant rows

**Files:**
- Modify: `hub_api/services/bundle_approval_service.py` / `bundle_permission_service.py` (wherever `app_permission_requests` + `community_permission_grants` rows are written at `platform:admin` approval)
- Test: approval-path test

**Interfaces:** Consumes P1 `requested_tools` + `requested_tool_permission_id`; produces grant rows keyed by that canonical id that `PgGrantLoader::load` (`svc_process/grant_gate.rs:137`) surfaces.

- [ ] **Step 1: Write failing test** — approving a bundle version whose manifest has a `requested_tool` writes an `app_permission_requests` row with `permission_id == requested_tool_permission_id(rt)` (version-pinned), and once an approved `community_permission_grants` row exists the loader's join yields it. (Mirror how an ordinary `permissions:` id becomes a request/grant row.)
- [ ] **Step 2: Run** the approval test → FAIL.
- [ ] **Step 3: Implement** the wiring so each `requested_tool` is represented as a declared permission id flowing into the existing request/grant write path (it already flows for `permissions:` ids; ensure `requested_tools` are folded into that set — or written alongside — with the derived `tool.call` id + its version). Do not bypass the `permission_hash` (P1 covers exported tools; confirm requested-tool ids are in the summary too so a change re-approves).
- [ ] **Step 4: Run** the approval + permission-summary tests → PASS.
- [ ] **Step 5: Commit** `feat(hub): derive tool.call grant requests from requested_tools`.

### Task 5: Fix the `app_version` grant pin in svc_process

**Files:**
- Modify: `core/svc_process/src/capabilities.rs` (the `app_version=0` placeholder ~:396)
- Test: svc_process grant-pin test (mirror svc_action's `dispatch_invocation_with_the_resolved_app_version` regression at `svc_action/.../capabilities.rs:4923`)

**Interfaces:** Consumes the real `app_versions.id`; feeds `GrantScopeKey.app_version` (`scope.rs:187`) so tool grants pin to the version.

- [ ] **Step 1: Write failing test** — a tool.call authorize in svc_process resolves the REAL `app_versions.id` into the `InvokeScope`/`GrantScopeKey` (not 0), so a grant issued for version N authorizes under N and a stale/0 version fails closed.
- [ ] **Step 2: Run** → FAIL (placeholder returns 0).
- [ ] **Step 3: Implement** the real version resolution (mirror svc_action's threading), replacing the `0` placeholder. Fail closed if the version can't be resolved.
- [ ] **Step 4: Run** `cargo test -p svc_process` → PASS.
- [ ] **Step 5: Commit** `fix(svc_process): pin grants to the real app_version (drop the 0 placeholder)`.

---

## Self-Review

- **Spec coverage:** §5.3 tool.call permission (T1,T3) · gate authorization (T2) · approval→grant (T4) · version pinning (T5). Digest-pinning is deliberately load-time-only (§5.4) — no task, by design. The host `tool.invoke` op + nested-invoke is P3 — out of P2 scope.
- **Review Focus:** encoding round-trip (T1), dual-mirror drift (T1+T3 cross test), garbage id (T1,T3), app_version=0 (T5), grant miss fail-closed (T2,T4) — each pinned to a test above.
- **Type consistency:** `PermissionId::ToolCall{target,tool}` (T1) ↔ `ResourceRef::ToolScoped(ToolCallTarget{target_app_id,tool,access})` (T2) ↔ the `tool.call:<enc-target>:<tool>` string (P1) — names/fields align; the percent-encoding is identical across Rust `canonical_id` and Python `requested_tool_permission_id`.
- **Deferred to P3:** the `tool.invoke` wire op, host-mediated nested invoke, 8s timeout, cross-stage routing.
