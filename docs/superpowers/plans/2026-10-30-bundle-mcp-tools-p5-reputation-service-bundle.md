# Bundle MCP-Tools P5 — Reputation Service Bundle (migrate logic → WASM) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (standing default). Steps use checkbox (`- [ ]`) syntax.
> **DEPENDS ON P3b merged** (`tool-provider` export + `tool.invoke` + SDK). Finalize Tasks 3-4 against the merged P3b API.

**Goal:** Move reputation LOGIC/policy (the `!rep` UX, delta policy for win/loss/loyalty, admin score-override, tiers) into a Rust WASM **service bundle** that calls the existing `reputation` host capability for atomic score work, and add the admin-override + batch/leaderboard ops the economy/giveaway features need.

**Architecture:** Identical shape to P4: the atomic score core stays in `bundle_host_reputation` (advisory lock → `FOR UPDATE` → per-user/per-scope 24h caps → ledgered adjustment); the bundle holds policy + verbs + exported tools. Imports `reputation` (host cap) + `db`/`kv`; exports `tool-provider` on the P3b `stage-tools` world. The giveaway/raffle rep-score **filter at draw** (the fraud/abuse gate) reads scores through this bundle's `get_score`/`get_many` tools; admins drop a user below the line via the `override` tool.

**Tech Stack:** Rust WASM bundle, `bundle_host_reputation` (ReputationStore trait + postgres impl), WIT `interface reputation`, permission catalog, `waddle-sdk-rs` (P3b).

**Spec:** `docs/superpowers/specs/2026-10-29-bundle-mcp-tools-framework.md` §10 (P5). Consumers: the giveaway/raffle/dungeon economy (`docs/superpowers/specs/2026-10-29-waddlebucks-economy.md`) uses the rep filter.

## Global Constraints

- **Atomic score ops stay in the host cap** — the bundle calls `reputation` ops; one atomic host-cap call per mutation. Advisory lock + `FOR UPDATE` + 24h |delta| cap + per-scope cap preserved (`postgres.rs:112-258`).
- **Community-scoped only** — reputation scores are keyed `(community_id, user)`. The cross-community/tenant FICO score is a SEPARATE legacy system (`core/reputation_module`, Python gRPC `reputation_tenant.score`) — a tenant-wide reputation bundle is OUT OF SCOPE for P5 (flag as a follow-up; it needs new tenant-scope host ops that don't exist).
- **WIT additive-only** — add funcs to `interface reputation` (`stage.wit:355`); don't change existing signatures.
- **Role `waddles_bundle_reputation`** (alembic 0050); tables `bundle_reputation_scores`/`bundle_reputation_adjustments`.
- **kind: service, always-on, trusted_source** — `seed_core_bundles.py`, `bundles/core-bundles.yaml` `wit_world: stage-tools`.
- **Zero-trust + admin gating** — `override` is admin-scope only; caps are catalog-driven (`reputation_caps()` `capabilities.rs:88`), never bundle-asserted.
- Docs + tests + OTel every task.

## Review Focus

- **Admin override is absolute + authorized** — `override(user, score, reason)` sets an absolute score (not a capped delta), admin-scope only, ledgered; a non-admin caller is denied. This is the documented way an admin drops a user below the giveaway rep line. Test: non-admin denied; admin override lands + is audit-ledgered.
- **Score floor/clamp** — recon found NO `CHECK` floor on scores; verify and add a clamp (min/max) so an adjust/override can't drive a score to an absurd value that breaks the draw filter. Test: adjust past the floor clamps, not wraps.
- **`get_many`/leaderboard bounded** — the draw-time filter fetches many users' scores in one call; bound the batch size (≤ some cap) and keep it a single host-cap round-trip. Test: oversized batch rejected; result ordering stable.
- **24h cap still enforced through the bundle** — moving the delta policy into the bundle must NOT let a caller bypass the per-user 24h |delta| cap (host-enforced). Test: rapid adjusts beyond the cap are refused by the host cap regardless of bundle logic.
- **Community isolation** — scores for community A never visible/mutable via a community-B-scoped call (host-asserted scope). Test: cross-community read/write denied.

---

### Task 1: Add host-cap ops to `bundle_host_reputation`

**Files:** `core/bundle_host_reputation/src/lib.rs` (ReputationStore ~:165), `core/bundle_host_reputation/src/postgres.rs` (~:112-258), `wit/waddle-bundle/stage.wit` (`interface reputation` :355, additive), `core/svc_process/src/capabilities.rs` (`handle_reputation` ~:614; `reputation_caps()` ~:88), permission catalog (+ Python mirror), alembic if a score-floor `CHECK` or `kind` value is added.

**Ops to add:**
| op | signature | notes |
|---|---|---|
| `override` | `(scope,user,absolute_score,reason,key)->new` | admin-scope only; ledger `admin_override`; clamped |
| `adjust_many` | `(scope,[(user,delta)],reason,key)->()` | batch, one txn, per-user caps still apply |
| `get_many` | `(scope,[user])->[(user,score)]` | bounded batch for the draw filter |
| `leaderboard` | `(scope,limit<=100)->[(user,score)]` | ordered |
| (verify) score floor/clamp | — | add `CHECK`/clamp if absent |

- [ ] Steps per op (TDD): failing host-cap test (atomicity, cap, admin-gate, clamp) → trait method → postgres impl reusing the advisory-lock/cap pattern → WIT func → `handle_reputation` arm → catalog + caps → green. Commit per op-group.
- [ ] `cargo test -p bundle-host-reputation` + catalog-mirror test green; clippy/fmt clean.

---

### Task 2: Reputation service bundle skeleton + registration

**Files:** new `bundles/rust/reputation/` (copy `bundles/rust/ping`), `bundles/core-bundles.yaml` (entry: `language: rust`, `wit_world: stage-tools`, `kind: service`), `hub_api/cli/seed_core_bundles.py`, Rust core-bundle build path (reuse the stanza P4 Task 2 adds).

- [ ] Scaffold; register as `kind: service` always-on; grant the `reputation` cap via `hub-manifest.yaml`; own `db`/`kv` for delta-policy/tier config. Build+seed smoke. Commit.

---

### Task 3: Reputation tools + verbs *(finalize against merged P3b SDK)*

**Files:** `bundles/rust/reputation/src/` — `tool-provider.call` dispatch + handlers; delta-policy/tier config in bundle `db`/`kv`.

**Tools exported:** `get_score`, `get_many`, `adjust` (apply the bundle's win/loss/loyalty delta policy → host-cap `adjust`), `override` (admin), `leaderboard`, `rep` (`!rep` read UX). Each: validate → resolve community policy → host-cap call with idempotency key → typed result.

- [ ] TDD per tool (SDK fake); then implement. Commit.

---

### Task 4: Real-path e2e + telemetry + docs

- [ ] e2e: a caller (e.g. a giveaway app bundle) fetches scores via `get_many` and an admin `override` through `tool.invoke`; assert cap/admin/clamp/community-isolation + telemetry (`record_tool_call` + reputation metrics).
- [ ] README + host-cap-ops docs; cross-ref the economy/giveaway draw-filter usage.
- [ ] Full suite + clippy/fmt/security green. Commit.

---

## Execution Handoff
`feature/reputation-service-bundle` off `release/v3.0.X` AFTER P3b (can run parallel to P4 — disjoint crates/bundles). One PR into `release/v3.0.X`, auto-merge when green (capability change → adversarial review). Tenant-wide reputation bundle = flagged follow-up (separate from this community-scoped bundle).

*Plan drafted 2026-10-30 from P4/P5 recon. Sibling of P4.*
