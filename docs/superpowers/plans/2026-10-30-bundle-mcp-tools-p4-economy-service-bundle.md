# Bundle MCP-Tools P4 — Economy Service Bundle (migrate logic → WASM) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (standing default). Steps use checkbox (`- [ ]`) syntax.
> **DEPENDS ON P3b merged** (the `tool-provider` export + `tool.invoke` + SDK). Finalize the tool/SDK step detail against the merged P3b API before executing Tasks 3-5.

**Goal:** Move economy LOGIC/policy (verbs `!give`/`!points[ set|view|leaders]`, the twitch-points→waddlebucks exchange, per-community alias) into a Rust WASM **service bundle** that calls the existing `economy` host capability for all atomic DB work, and add the compound host-cap ops the full economy needs.

**Architecture:** The atomic money core STAYS in the Rust host cap (`bundle_host_economy`, next to the DB connection); the bundle holds policy + verbs + the exported tools. The bundle imports `economy` (host cap) + `db`/`kv` (its own alias/rate config) and exports `tool-provider` (so app bundles — games, giveaway, lurker — invoke `adjust_balance`/`transfer`/`pay_pot`/`exchange` via `tool.invoke`). Uses the P3b `stage-tools` WIT world. Every must-be-atomic operation is a SINGLE host-cap call.

**Tech Stack:** Rust WASM bundle (copy `bundles/rust/ping`), `bundle_host_economy` (EconomyStore trait + postgres impl), WIT `interface economy`, `core/bundle_capability_gate` + permission catalog, `waddle-sdk-rs` tool helpers (P3b).

**Spec:** `docs/superpowers/specs/2026-10-29-bundle-mcp-tools-framework.md` §10 (P4); the economy feature is detailed in `docs/superpowers/specs/2026-10-29-waddlebucks-economy.md` (this plan builds the service-bundle substrate it needs).

## Global Constraints

- **Atomic ops stay in the host cap.** The bundle NEVER holds a DB connection or raw SQL; it calls `economy` host-cap ops. Each atomic unit = one host-cap call (no multi-call read-modify-write on a balance — TOCTOU).
- **Host-cap mechanics preserved** — advisory lock, 24h caps from the append-only ledger, idempotency keys, `CHECK(balance>=0)`, mint-cap on payout (`MAX_PAYOUT_MULTIPLE`). New ops reuse these (`postgres.rs` CTE pattern); caps come from the permission catalog (`economy_caps()` `svc_process/capabilities.rs:130`), never bundle-asserted.
- **WIT interface evolution is additive-only** — add new functions to `interface economy` (`stage.wit:~384`); do NOT change existing function signatures (old compiled bundles import the old subset). The host provides all.
- **Role `waddles_economy_runtime`** — new ops must fit its column-scoped grants (alembic 0051: INS/UPD `economy_balances`, SEL/INS `economy_ledger`); an admin-adjust op writes ledger kind `admin_adjust`, mint-capped.
- **kind: service, always-on, trusted_source** — registered via `seed_core_bundles.py` (sets `trusted_source=True`); catalog `bundles/core-bundles.yaml` with `wit_world: stage-tools`.
- **Zero-trust caller** — tool handlers validate every app-supplied value; the host economy cap re-enforces caps/actor-binding regardless of bundle assertions.
- Docs + tests + OTel on every task (project rule).

## Review Focus

- **Exchange atomicity** — the twitch-points→waddlebucks `exchange` (debit external points + credit waddlebucks) MUST be one host-cap call, not two; a crash between halves must not mint free currency. Test: a forced failure mid-exchange leaves both ledgers consistent (idempotency-key replay).
- **`pay_pot` multi-winner atomicity** — splitting a pot across N winners is one atomic op; partial payout on failure is forbidden. Test: N-winner payout either fully commits or fully rolls back; replay with the same key is idempotent.
- **conditional_debit under-balance** — `conditional_debit(user, amt, min_balance)` must reject (not partially apply) when `balance - amt < min`; `CHECK(balance>=0)` is the backstop. Test: debit exceeding balance → typed error, no row change.
- **Admin adjust cannot bypass the mint cap maliciously** — `set_balance`/`adjust_by` are admin-only (scope-gated), ledgered as `admin_adjust`, and still bounded by the supply cap. Test: a non-admin caller is denied; an admin over-mint is capped.
- **Alias/rate is per-community policy, not global** — the per-community alias + exchange rate live in the bundle's own `db`/`kv` keyed by community; a missing config falls back to defaults (alias "waddlebucks", rate 1000:500). Test: two communities with different aliases/rates don't bleed.

---

### Task 1: Add compound host-cap ops to `bundle_host_economy`

**Files:** `core/bundle_host_economy/src/lib.rs` (EconomyStore trait ~:270), `core/bundle_host_economy/src/postgres.rs` (CTE impls ~:157-390), `wit/waddle-bundle/stage.wit` (`interface economy` ~:384, additive), `core/svc_process/src/capabilities.rs` (`handle_economy` ~:976 new arms; `economy_caps()` ~:130), the permission catalog (new `PermissionFamily`/`PermissionId` + Python mirror), alembic if a new ledger `kind` enum value is needed.

**Ops to add** (each: trait method + atomic postgres impl + WIT func + handle arm + catalog entry + cap):
| op | signature | notes |
|---|---|---|
| `credit` | `(scope,user,amount,reason,key)->new_bal` | mint path (no stake); mint-capped; ledger kind `credit` |
| `debit` | `(scope,user,amount,reason,key)->new_bal` | guarded `balance>=amount` |
| `conditional_debit` | `(scope,user,amount,min_balance,key)->new_bal` | reject if would drop below min |
| `admin_adjust` | `(scope,user,delta|absolute,reason,key)->new_bal` | admin-scope only; ledger `admin_adjust`; supply-capped |
| `pay_pot` | `(scope,winners:[(user,amount)],pot_source,key)->()` | atomic multi-winner split |
| `exchange` | `(scope,user,from_amount,rate_num,rate_den,key)->new_bal` | 1-call debit-external + credit-economy |
| `batch_credit` | `(scope,[(user,amount)],reason,key)->()` | bulk, one txn |

- [ ] Steps per op (TDD): failing host-cap unit test (atomicity + cap + idempotency replay) → trait method → postgres CTE impl reusing `lock_scope`/`enforce_daily_caps`/`IdempotencyKey` → WIT func (additive) → `handle_economy` arm → catalog entry + `economy_caps()` → test green. Commit per op-group.
- [ ] Full `cargo test -p bundle-host-economy` + the Python catalog-mirror test green; clippy/fmt clean.

---

### Task 2: Economy service bundle skeleton + registration

**Files:** new `bundles/rust/economy/` (copy `bundles/rust/ping`: `bundle.yaml`, `hub-manifest.yaml`, `Cargo.toml`, `rust-toolchain.toml`, `deny.toml`, `src/`), `bundles/core-bundles.yaml` (new `economy` entry: `source_dir`, `language: rust`, `wit_world: stage-tools`, `kind: service`, `activation_targets`), `bundles/Dockerfile.core-bundles` / `bundles/build_python_bundles.py` (VERIFY + add a Rust build stanza — the loop is Python-only today), `hub_api/cli/seed_core_bundles.py` (ensure the Rust service entry seeds with `trusted_source=True`).

**Interfaces:** Produces an installed, always-on `kind: service` bundle using the `stage-tools` world (imports `economy`+`db`+`kv`, exports `tool-provider`), granted the `economy` cap via `hub-manifest.yaml` `permissions:` + its own `db` table for alias/rate config.

- [ ] Verify the Rust core-bundle build path exists (Dockerfile stanza); if missing, add it — fail-loud if Rust bundles can't build.
- [ ] Scaffold the bundle; register in catalog + seeder; a build+seed smoke proves it installs as `kind: service`.
- [ ] Commit.

---

### Task 3: Economy tools + verbs in the bundle *(finalize against merged P3b SDK)*

**Files:** `bundles/rust/economy/src/` — `tool-provider.call` dispatch + the tool handlers; per-community alias/rate in the bundle's `db`/`kv`.

**Tools exported:** `get_balance`, `adjust_balance` (credit/debit via host cap), `transfer`, `pay_pot`, `exchange`, `give` (`!give` UX → `transfer`), `points_set`/`points_view`/`points_leaders` (`!points` family). Each handler: validate inputs → resolve community alias/rate → call the host-cap op with an idempotency key → return typed result.

- [ ] TDD per tool (SDK testing fake): handler maps verb → host-cap op; alias/rate resolution; bad input rejected. Then implement.
- [ ] Commit.

---

### Task 4: Migrate `!points` off the standalone loyalty bundle

**Files:** `bundles/python/loyalty/`, `bundles/python/commands/src/app.py:75` (alias listing).

- [ ] Point `!points` at the economy service bundle's tools (via `tool.invoke`) instead of loyalty's own `loyalty_balances` table; deprecate the loyalty table path.
- [ ] **FLAG FOR USER (data-sensitive, do NOT auto-run overnight):** migrating existing `loyalty_balances` → `economy_balances` is a one-way data migration affecting real user balances — write the migration + a dry-run report, but leave the actual backfill for explicit user approval. Note it in the report + a GitHub issue.
- [ ] Tests: `!points` now reads/writes via economy; regression that old balances are addressed by the (flagged) migration.

---

### Task 5: Real-path e2e + telemetry + docs

- [ ] e2e: a caller app bundle invokes the economy service bundle's `adjust_balance`/`transfer`/`pay_pot`/`exchange` via `tool.invoke`; assert atomicity/idempotency/cap enforcement + telemetry emission (`record_tool_call` + economy metrics).
- [ ] README for the economy bundle + the new host-cap ops; update the economy feature doc cross-ref.
- [ ] Full suite + clippy/fmt/security green. Commit.

---

## Execution Handoff
Single `feature/economy-service-bundle` worktree off `release/v3.0.X` AFTER P3b merges; one PR into `release/v3.0.X`, auto-merge when green (capability/money change → adversarial review applies). The loyalty balance backfill stays user-gated.

*Plan drafted 2026-10-30 from P4/P5 recon. Tasks 3+ finalize against the merged P3b `tool-provider`/SDK API. P5 (reputation) is the sibling plan.*
