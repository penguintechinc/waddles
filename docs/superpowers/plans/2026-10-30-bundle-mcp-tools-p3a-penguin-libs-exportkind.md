# Bundle MCP-Tools P3a — penguin-libs `ExportKind::ToolCall` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (the standing default for this project) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `ToolCall` variant to the `ExportKind` wire enum in penguin-libs `penguin-bundle-host` so the bundle-to-bundle tool-invocation framework (P3b, in waddles) has a wire kind to route on; release it as `0.4.0`.

**Architecture:** `penguin-bundle-host` is a **wire-codec + manifest-validator crate only** — it holds no wasmtime host engine (that lives in waddles `core/bundle_executor`). So this plan adds exactly one enum variant plus the exhaustive-match/test/version updates it forces. The payload is already opaque bytes-in/bytes-out (`InvokeBody.payload` / `ResultBody.payload`, canonical JSON), so NO new host primitive, trait, or runtime is added here. All invoke machinery is deferred to P3b.

**Tech Stack:** Rust, serde, the crate's existing wire module.

**Spec:** `docs/superpowers/specs/2026-10-29-bundle-mcp-tools-framework.md` (§10 migration program, P3a).

## Global Constraints

- **Repo & crate:** penguin-libs, `packages/rust-bundle-host`, package name `penguin-bundle-host`. The work is in penguin-libs, NOT waddles — this plan file lives in the waddles docs tree only for program continuity.
- **Base branch must contain the crate.** The default penguin-libs checkout (`feature/rust-shared-libs`) does NOT contain `packages/rust-bundle-host`. Branch the worktree off a base that does — the waddles-pinned rev `0b97183f` (on `origin/fix/tokio-1533-bundle-host`) or the latest `origin/release/rust-bundle-host/v0.2.x`. Confirm `packages/rust-bundle-host/Cargo.toml` exists on the base before Task 1.
- **`ExportKind` is deliberately NOT `#[non_exhaustive]`** (doc comment ~line 190, `CHANGELOG.md:19`). Adding a variant is intentionally source-breaking — every exhaustive match is a compile error until updated. That is the mechanism, not a bug.
- **Wire label is `tool_call`** — the enum carries `#[serde(rename_all = "snake_case")]`, so the variant `ToolCall` serializes as `"tool_call"`. The `as_str()` output is BOTH the wire label and a metric label — keep it `tool_call`.
- **Version bump 0.3.0 → 0.4.0** (breaking change, following the 0.2.0→0.3.0 precedent for the prior enum change). Bump only `packages/rust-bundle-host/Cargo.toml`.
- **Keep the crate codec-only.** Do NOT add any wasmtime, execution, slot, or host-invoke code here. If a task seems to need it, it belongs in P3b — stop and note it.
- **Release rule (company-wide):** merging into penguin-libs `release/*` is `--admin`-authorized; `main` is the hard gate (explicit user approval). The publish/release step is execution handoff, not a code task (see Execution Handoff).

## Review Focus

- **`ALL` array length is hard-coded `[ExportKind; 6]`** (`message.rs:219`) — must become `7` and include `ToolCall`, or it won't compile / will omit the variant from any `ALL`-driven iteration. Task 1 test asserts `ALL` contains `ToolCall`.
- **`wire_codec_tests.rs:412-444` asserts `expected.len() == ALL.len()`** against a label table — this test breaks the instant `ALL` grows; the label table must gain the `tool_call` row in the same task. Task 1 updates it.
- **`is_connector()` must return `false` for `ToolCall`** (it is not a connector export) — an accidental `true` would mis-route it down the connector path. Task 1 test pins `is_connector(ToolCall) == false`.
- **Round-trip stability** — `ToolCall` must serialize to exactly `"tool_call"` and deserialize back; a wrong rename silently breaks P3b routing. Task 1 test pins the exact JSON string.
- **No semantic drift in `as_str()`** — it feeds metrics cardinality; the label must be the stable `tool_call`, not a display variant.

---

### Task 1: Add the `ToolCall` wire variant + fix all exhaustive matches

**Files:**
- Modify: `packages/rust-bundle-host/src/wire/message.rs` (enum ~199-214, `ALL` ~219, `as_str` ~232, `is_connector` ~247)
- Test: `packages/rust-bundle-host/tests/wire_codec_tests.rs` (label table ~412-444)

**Interfaces:**
- Consumes: the existing `ExportKind` enum, `#[serde(rename_all = "snake_case")]`, `ExportKind::ALL`, `as_str(&self) -> &'static str`, `is_connector(&self) -> bool`.
- Produces: `ExportKind::ToolCall` (wire label `"tool_call"`), `ALL` length 7 containing `ToolCall`, `as_str(ToolCall) == "tool_call"`, `is_connector(ToolCall) == false`. P3b (waddles) consumes `ExportKind::ToolCall` by name.

- [ ] **Step 1: Write the failing tests** in `wire_codec_tests.rs`:

```rust
#[test]
fn tool_call_serializes_to_tool_call() {
    assert_eq!(serde_json::to_string(&ExportKind::ToolCall).unwrap(), "\"tool_call\"");
    assert_eq!(serde_json::from_str::<ExportKind>("\"tool_call\"").unwrap(), ExportKind::ToolCall);
}

#[test]
fn tool_call_in_all_and_not_connector() {
    assert!(ExportKind::ALL.contains(&ExportKind::ToolCall));
    assert_eq!(ExportKind::ALL.len(), 7);
    assert_eq!(ExportKind::ToolCall.as_str(), "tool_call");
    assert!(!ExportKind::ToolCall.is_connector());
}
```

Also add a `tool_call` row to the existing label table (~412-444) so its `expected.len() == ALL.len()` assertion still holds.

- [ ] **Step 2: Run the tests, verify they fail** (missing variant → compile error, then assertion).

Run: `cargo test -p penguin-bundle-host --test wire_codec_tests`
Expected: FAIL (does not compile: `ToolCall` not found; then length mismatch).

- [ ] **Step 3: Add the variant and update the three exhaustive sites** in `message.rs`:
  - Add `ToolCall` to the `ExportKind` enum.
  - `ALL`: change `[ExportKind; 6]` → `[ExportKind; 7]` and append `ExportKind::ToolCall`.
  - `as_str`: add arm `ExportKind::ToolCall => "tool_call"`.
  - `is_connector`: add arm `ExportKind::ToolCall => false` (or ensure the catch-all/default returns false for it).

- [ ] **Step 4: Run the full crate test suite, verify green.**

Run: `cargo test -p penguin-bundle-host`
Expected: PASS (all, including `wire_codec_tests`).

- [ ] **Step 5: Lint.**

Run: `cargo clippy -p penguin-bundle-host --all-targets -- -D warnings && cargo fmt -p penguin-bundle-host -- --check`
Expected: clean.

- [ ] **Step 6: Commit.**

```bash
git add packages/rust-bundle-host/src/wire/message.rs packages/rust-bundle-host/tests/wire_codec_tests.rs
git commit -m "feat(bundle-host): add ExportKind::ToolCall wire variant"
```

---

### Task 2: Version bump to 0.4.0 + CHANGELOG

**Files:**
- Modify: `packages/rust-bundle-host/Cargo.toml` (version, line 3)
- Modify: `packages/rust-bundle-host/CHANGELOG.md`

**Interfaces:**
- Consumes: the Task 1 variant.
- Produces: `penguin-bundle-host` version `0.4.0` + a CHANGELOG entry. P3b pins this exact version (`=0.4.0`) + the released rev.

- [ ] **Step 1: Bump the version** in `packages/rust-bundle-host/Cargo.toml` from `0.3.0` to `0.4.0`.

- [ ] **Step 2: Add a CHANGELOG entry** under a new `## 0.4.0` heading: "Added `ExportKind::ToolCall` wire variant for bundle-to-bundle tool invocation (source-breaking: `ExportKind` is not `#[non_exhaustive]`; downstream exhaustive matches must add a `ToolCall` arm)."

- [ ] **Step 3: Verify the crate still builds + packages cleanly.**

Run: `cargo build -p penguin-bundle-host && cargo package -p penguin-bundle-host --allow-dirty --no-verify 2>&1 | tail -5`
Expected: builds; package lists `0.4.0`.

- [ ] **Step 4: Commit.**

```bash
git add packages/rust-bundle-host/Cargo.toml packages/rust-bundle-host/CHANGELOG.md
git commit -m "chore(bundle-host): release 0.4.0 (ExportKind::ToolCall)"
```

---

## Execution Handoff (not code tasks — release + downstream pin)

These are git/release operations gated by the company-wide release rule and the waddles-side P3b plan; they are NOT part of the subagent code loop:

1. **Publish the rev on origin.** Open a PR into penguin-libs `release/rust-bundle-host/v0.4.x` (create the release line if absent), verify green, merge `--admin` (release-branch merge is pre-authorized). Record the merged commit SHA — P3b pins it.
2. **Downstream rev-bump is a P3b (waddles) task**, listed here so it isn't lost: update the `penguin-bundle-host` pin to `version = "=0.4.0"` + the new `rev` in **all six** waddles Cargo.tomls — `core/bundle_executor/Cargo.toml:80`, `core/bundle_host_http/Cargo.toml:28`, `core/svc_action/Cargo.toml:91`, `core/svc_ingest/Cargo.toml:112`, `core/svc_process/Cargo.toml:110`, and `core/bundle_compiler/Cargo.toml:59` (currently lagging at `=0.2.1` — decide in P3b whether to bring it to 0.4.0 or leave it, since the compiler may not need the new variant) — plus the matching `Cargo.lock` files (minimal resolution: restore base lock, add only the new rev; never `cargo generate-lockfile`). The waddles tree will not compile against 0.4.0 until P3b adds the `ToolCall` arms at `invoke.rs:1104/1296/1355` and `connector_wire.rs:337` — so the rev-bump and the P3b code land together.

---

*Plan written 2026-10-30 against penguin-libs rev `0b97183f` recon. P3a is the cross-repo dependency root; P3b (waddles tool.invoke framework) consumes the released 0.4.0 crate.*
