# Bundle `db` Host Capability & Bundle-Owned Schema/Migrations — Design

**Status:** Proposed design (no code in this doc)
**Date:** 2026-09-28
**Scope:** `core/svc_process/src/capabilities.rs`, `core/svc_action/src/capabilities.rs`, `hub_api/services/bundle_manifest_v2.py`, `hub_api/services/bundle_approval_service.py`, new hub-api schema-compiler service, new `bundle-data` Postgres database, `config/postgres/rbac-matrix.yaml`.
**Drivers:** `db`/`kv` are hardcoded `denied` for every call today (`core/svc_process/src/capabilities.rs:221-228`, `core/svc_action/src/capabilities.rs:582-589`) — no bundle can persist state regardless of manifest `data.tables`. `data.tables` (`hub_api/services/bundle_manifest_v2.py:266-274`) validates table-name shape only; nothing provisions a table. Both gaps are called out as the real go/no-go blocker in `docs/superpowers/specs/2026-09-28-superpenguin-fish-game-port.md` §3 (PR #414, `docs/superpenguin-fish-game-port` branch), whose ~7-table schema is this doc's worked example (§9).
**Builds on (approved, not re-litigated here):** `docs/superpowers/specs/2026-09-28-wit-stage-v1-1-design.md` §3/§7 (sqlparser-rs AST validation, `SET LOCAL` statement/lock timeouts, RLS leak-proofing, per-component `Linker` isolation, `db.execute-batch`) and `docs/superpowers/specs/2026-09-28-tenant-envelope-encryption-design.md` (key hierarchy, AAD-with-`row_uuid`, tombstone/restore-runbook pattern) — this design reuses both verbatim rather than inventing parallel mechanisms.

---

## 1. Problem statement

| Gap | Where | Consequence |
|---|---|---|
| `db`/`kv` unconditionally denied | `svc_process/capabilities.rs:221-228`, `svc_action/capabilities.rs:582-589` | Zero bundles can read/write state today, independent of manifest content |
| No schema/migration mechanism | `bundle_manifest_v2.py:266-274`, confirmed absent across `bundle_executor`/`svc_process`/`svc_action`/hub-api | Every bundle table needs a hand-written core-repo Alembic PR — doesn't scale to third-party vendor bundles |
| `data.tables` is name-only | `bundle_manifest_v2.py` `_TABLE_RE`/`_RESERVED_TABLES` | An allowlist with nothing behind it to allow access *to* |

This doc designs both fixes together because they're coupled: wiring `db.execute` with no provisioning mechanism gives bundles a capability with nothing to point it at, and a provisioning mechanism with `db` still denied ships schemas nothing can use.

---

## 2. Where bundle data lives (architecture decision)

**Constraint:** hub-api is the only read-write path to the *control-plane* Postgres database; `svc_ingest`/`svc_process`/`svc_action` hold `waddles_bundle_reader`, a read-only role against a read replica of that same database.

**Decision: a second, physically separate `bundle-data` Postgres database — not a schema inside the control-plane database, and not the control-plane database's primary at all.** The data plane (svc_process/svc_action) gets a direct, restricted-role connection to `bundle-data`, and writes there at runtime. This does not violate "hub-api is the only RW path to control-plane" — that invariant is scoped to the control-plane database specifically, and `bundle-data` is deliberately outside it.

| Reason | Detail |
|---|---|
| Blast radius | A bundle bug or malicious vendor DDL/DML can never reach `app_versions`, `communities`, `connection_credentials`, billing, or auth tables — those live in a database the bundle-data role has no network route to, not merely a schema it lacks grants on |
| Backup/restore decoupling | `bundle-data` gets its own backup policy (§8) independent of the control-plane DB's compliance-driven retention — a bundle-data restore can never resurrect control-plane rows and vice versa |
| Noisy-neighbor isolation | Thousands of bundles across 10k channels doing `db.execute` traffic never contends for the control-plane primary's connection pool, WAL, or lock table — a runaway bundle query degrades only `bundle-data` |
| Quota enforcement locality | Row/byte quota scans (§6) run against `bundle-data` only; they never need cross-database joins against control-plane tables |
| Matches the existing RO-replica pattern | The data plane already holds no write credential to the control-plane DB (encryption doc §5). Extending that role with write access to control-plane, even scoped, would be a bigger invariant break than standing up a second database the data plane *does* write to directly |

**hub-api's role in `bundle-data`: DDL only, never runtime DML.** hub-api (via its own dedicated `bundle_schema_owner` role in `bundle-data`) creates/alters/drops per-app schemas and roles at bundle approval/uninstall time — the same "hub-api owns provisioning" pattern it already has for the control-plane DB's `migration_runner` role. The data plane never gets DDL rights in `bundle-data`, only `SELECT`/`INSERT`/`UPDATE`/`DELETE` scoped to one app's own schema (§5).

```
hub-api (bundle_schema_owner: DDL only)          svc_process / svc_action
        │                                        (per-app role: DML only, RLS-scoped)
        ▼                                                 ▼
   ┌─────────────────────────  bundle-data (new DB)  ─────────────────────────┐
   │  schema app_<h(fishing-core)>   schema app_<h(fishing-shop)>   ...       │
   │   fish_catches, fishing_gold     fish_types, fishing_shop_items          │
   └────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Capability A: wiring `db.execute` / `db.execute-batch`

### 3.1 Required pieces (all already approved conditions, none new)

| Piece | Source of the requirement | This design's application |
|---|---|---|
| sqlparser-rs AST validation | wit-v1.1 §3, §7.4 | Every statement parsed (Postgres dialect) before reaching `bundle-data`; rejects any statement whose referenced relations aren't fully enumerable from the AST, checked against the manifest's `data.tables` **and** resolved to the caller's own app schema only |
| `SET LOCAL statement_timeout` / `lock_timeout` | wit-v1.1 §3, §7.2 | `EXECUTOR_DB_STATEMENT_TIMEOUT_MS` (default 2000) / `EXECUTOR_DB_LOCK_TIMEOUT_MS` (default 1000), set before the first statement in every `execute`/`execute-batch` call |
| `SET LOCAL` for RLS, never session `SET` | wit-v1.1 §3, §7.3 | `waddles.tenant`/`waddles.community`/`waddles.app_id` set per transaction from `InvokeScope` (never bundle args); pooled-connection checkin runs `DISCARD ALL` behind a panic-safe guard |
| Rate limit before call | wit-v1.1 moderation ordering (§2, §7.1), reused here | The existing per-tenant `UsageBatcher` token bucket is checked **before** connection acquisition — a throttled bundle makes zero `bundle-data` round trips, not a partially-executed one |
| Per-component `Linker` registers only declared functions | wit-v1.1 §5 | `db.execute` is registered in a component's `Linker` only if the manifest's `data.tables` is non-empty; `db.execute-batch` additionally requires `capabilities: [db-batch]` and `wit-world: stage-v1_1` — identical gating already specified for `moderation`/`scheduled-stage` |

### 3.2 Call path (per invoke)

1. `InvokeScope` (tenant, community, app_id) resolved from the delivered envelope — identical to today's `context`/`log`/`relay` handling, never from `args`.
2. Rate-limit/quota gate (§6) checked. Fail closed (`access-denied`) before any connection is touched.
3. A pooled connection to `bundle-data` is checked out, scoped to the per-app role `bundle_<app_hash>` (§5).
4. `SET LOCAL waddles.tenant/community/app_id` + `SET LOCAL statement_timeout/lock_timeout`.
5. sqlparser-rs AST validation of the statement(s) against the app's `data.tables` allowlist; `execute-batch` validates every statement before executing any of them and opens one transaction (all-or-nothing, per wit-v1.1 §3).
6. Execute; record usage (`UsageBatcher`, same "host calls by kind" metering as `relay`/`http`).
7. Connection checkin runs `DISCARD ALL` under a `Drop`-guard, regardless of success/panic/early-return.

This replaces the two `Err(denied("not_implemented", ...))` arms in both `capabilities.rs` files with a real implementation; `CapabilityKind::Kv` remains a separate, smaller follow-up (bundle-scoped Valkey hash, no schema dependency) and is out of scope here except to note it shares the same rate-limit gate.

---

## 4. Capability B: bundle-owned schema/migration mechanism — options

| Option | How it works | DDL/SQL source | Isolation substrate |
|---|---|---|---|
| **1. Manifest DSL → compiled DDL** | Manifest declares tables/columns/indexes/constraints in a structured (JSON/YAML) schema, never raw SQL; hub-api's compiler deterministically emits `CREATE TABLE`/`CREATE INDEX` DDL | 100% hub-api-generated; bundle never supplies executable text | Per-app Postgres schema + role + RLS |
| **2. Generic document table (JSONB per app)** | One shared table (`bundle_documents(tenant_id, community_id, app_id, collection, doc_id, data jsonb, row_uuid, ...)`) with a GIN index; bundles write arbitrary JSON, no DDL ever | None — no DDL at all, ever | RLS row-scoping only; no per-app schema |
| **3. Per-app schema + RLS via reviewed raw SQL/migration file** | Manifest points at a `data/schema.sql` (or Alembic-style migration file) in the bundle package; a human reviewer approves it, hub-api runs it in a sandboxed transaction restricted to `CREATE TABLE`/`CREATE INDEX`/`ALTER TABLE ADD COLUMN` | Bundle-vendor-authored raw SQL, sqlparser-rs-gated to a DDL allowlist before execution | Per-app Postgres schema + role + RLS (same substrate as Option 1) |

### 4.1 Comparison

| Criterion | Option 1 (DSL→DDL) | Option 2 (JSONB doc) | Option 3 (reviewed raw SQL) |
|---|---|---|---|
| DDL-injection surface | **Zero** — no bundle-supplied text ever becomes DDL | Zero — no DDL at all | Non-zero — mitigated by review + sqlparser-rs DDL-statement allowlist, never eliminated |
| Relational expressiveness (FKs, CHECK, composite indexes) | Full, within an allowlisted grammar (§4.3) | None — app-layer joins over JSONB paths only | Full — genuine SQL |
| Onboarding friction for a vendor | Structured form, no SQL knowledge required, fully machine-validatable pre-review | Zero — nothing to submit | Vendor must write correct, reviewable SQL; human reviewer is the only gate |
| Fits the fish-game 7-table schema (§9) with FKs/indexes | Yes | Poorly — leaderboard/tournament sort-and-filter queries degrade to JSONB path scans | Yes |
| Consistency with platform posture | Matches `db.execute`'s own "no raw bundle text reaches Postgres unvalidated" philosophy (wit-v1.1 §3's AST gate) exactly, one level up (DDL instead of DML) | Consistent but sacrifices the relational model bundles like fishing need | Inconsistent — the one place the platform would trust bundle-authored executable text outright |
| Migration diffing (§7) | Mechanical — diff two structured documents | Trivial — schema never changes | Manual — vendor writes a new migration file each version, same as today's core-repo Alembic burden, just moved to the vendor |

### 4.2 Recommendation: **Option 1 (manifest DSL compiled to DDL by hub-api)**

Rationale: it is the only option that (a) supports fishing's real relational needs (FK from `fish_catches.fish_type_id` to `fish_types.id` within the same app schema, composite indexes for leaderboard queries, `CHECK` on `stars BETWEEN 1 AND 3`) and (b) keeps the zero-raw-SQL-from-a-bundle invariant the rest of the platform already holds for `db.execute`. Option 3 is kept as a **time-boxed escape hatch, not a parallel default path**: if the DSL genuinely can't express a needed construct, a bundle may fall back to a reviewed raw migration file under the same per-app-schema substrate, but this requires an explicit manifest flag (`data.schema.raw_sql: true`) that hub-api's approval UI surfaces as a higher-severity review item — never a silent alternative a vendor reaches for by default. Option 2 is rejected as the default model (kills relational queries fishing/tournaments/leaderboards need) but remains available as a documented pattern for bundles that genuinely only need a key-value-shaped document store and don't want schema overhead at all.

### 4.3 DSL shape (structural sketch, not an implementation)

| Field | Type | Notes |
|---|---|---|
| `table.name` | string | Must appear in manifest `data.tables` (existing `_TABLE_RE`/`_RESERVED_TABLES` checks reused) |
| `table.columns[].name` / `.type` | string / enum | `{uuid, text, varchar(n), integer, bigint, boolean, timestamptz, numeric(p,s), jsonb}` — no `bytea`/superuser-only types |
| `table.columns[].nullable` / `.default` | bool / restricted literal | `default` accepts literals or a tiny allowlisted function set (`now()`, `gen_random_uuid()`), sqlparser-rs-validated before splicing into DDL |
| `table.columns[].primary_key` | bool | Exactly one PK column (or declared composite) per table |
| `table.columns[].references` | `{table, column}` | FK target must be another table in the **same app's own schema** — cross-app/cross-schema FKs are rejected outright (isolation, §5) |
| `table.indexes[].columns` / `.unique` / `.where` | list / bool / restricted expr | Partial-index predicates sqlparser-rs-validated the same way `db.execute` statements are |
| `table.checks[].expr` | restricted expr | Comparison/range checks only, against an allowlisted operator set |

### 4.4 Provisioning flow (bundle approval, existing human-review gate)

1. Manifest passes existing pure-YAML checks (`bundle_manifest_v2.py`) plus new DSL structural validation.
2. hub-api's schema compiler deterministically renders the DSL to DDL (Option 1) — same output for the same input, so a reviewer can diff DDL across versions instead of trusting the DSL blindly.
3. `bundle_approval_service` runs the DDL inside the same transaction as today's approval + activation, via `bundle_schema_owner`, targeting a freshly-created `app_<sha256(app_id)[:16]>` schema in `bundle-data`.
4. A per-app runtime role `bundle_<same hash>` is created/granted `SELECT/INSERT/UPDATE/DELETE` on that schema only (RBAC-matrix-style explicit grant row, `config/postgres/rbac-matrix.yaml` pattern extended into a second matrix scoped to `bundle-data`).
5. RLS policies are attached to every table, keyed on `current_setting('waddles.tenant')`/`community` — generated by the compiler, not hand-written per bundle.

---

## 5. Isolation model against a malicious vendor bundle

| Threat | Defense | Layer |
|---|---|---|
| Bundle reads/writes another app's table | Postgres schema-level `GRANT` — the per-app role has no `USAGE` on any other app's schema at all | Outer wall, independent of RLS |
| Bundle reads/writes another tenant's rows in its own schema | RLS policy keyed on `SET LOCAL waddles.tenant`/`community`, values taken from server-resolved `InvokeScope`, never bundle `args` | Row-level |
| SQL injection / DDL smuggling via a crafted statement | sqlparser-rs AST validation (DML, §3) and DSL-only DDL generation with no bundle-supplied raw SQL (DML+DDL, §4) — zero paths where bundle text becomes executable structure | Statement-level |
| Resource exhaustion (giant scan, huge batch) | `statement_timeout`/`lock_timeout`, `EXECUTOR_DB_BATCH_MAX_STATEMENTS` cap (wit-v1.1 §3), row/byte quota gate (§6) | Query-cost |
| App id / tenant / community spoofing | Schema name and role name are computed server-side from `app_id`, never accepted as bundle input — matches the existing "no bundle host call accepts a tenant/community argument" rule (wit spec §5.11) extended to schema resolution | Scope-derivation |
| Bundle persists PII it was only handed transiently | Out of this design's enforcement — the platform-wide "UUIDs only" contract (no PII columns, `critical-rules.md` PII Tokenization) is the mitigation, same as everywhere else; the DSL has no `email`/`username`-shaped type to make this easy, but cannot stop a `text` column from holding anything | Not newly solved here, called out explicitly |

---

## 6. Quotas

| Quota | Enforcement point | Mechanism |
|---|---|---|
| Rows per app (per tenant) | `db.execute`/`execute-batch` pre-call gate | Periodic (5 min) hub-api background scan of `bundle-data` (`pg_class.reltuples` per app schema, cheap estimate) writes a per-`(tenant, app_id)` usage row hub-api's cache serves to the gate check — last-known value on scan failure, never crash (mirrors flag/license graceful degradation) |
| Bytes per app (per tenant) | Same gate | `pg_total_relation_size()` summed per app schema, same scan job |
| Query cost | Same gate, pre-execution | No EXPLAIN-based cost model (too slow for the hot path) — `statement_timeout`/`lock_timeout` + `EXECUTOR_DB_BATCH_MAX_STATEMENTS` are the entire query-cost control, deliberately simple and already-approved (§3.1) |
| Exceeding any quota | Fail closed | `access-denied("quota_exceeded")` — never a partial write, never silently allowed "just this once" |

At 10k channels / thousands of bundles, the scan is O(apps × tenants-that-installed-that-app), not O(rows) — bounded by activation count, not data volume.

---

## 7. Migrations on bundle version upgrade and rollback

- Every approved version's compiled DSL is stored (`bundle_schema_versions(app_id, version, schema_json, ddl_hash)`, hub-api-owned, control-plane DB — it's metadata about bundles, not bundle data itself).
- **Upgrade:** hub-api diffs the new version's DSL against the currently-active version's stored DSL.
  - **Additive-only** changes (new table, new nullable/defaulted column, new index) auto-generate `CREATE`/`ALTER ADD` DDL and apply inside the same approval transaction — no extra human step beyond today's approval gate.
  - **Destructive** changes (drop column/table, narrow a type, add `NOT NULL` with no default, remove an FK) require an explicit manifest flag (`data.migration.allow_destructive: true`) **and** a reviewer acknowledgment in the approval UI; absent either, approval fails closed with reason code `destructive_schema_change_requires_ack` — same posture as the encryption doc's "no disable-once-enforced kill-switch."
- **Rollback:** additive changes need no reverse migration — old bundle code simply never references the new column/table, so reactivating a prior version is safe by construction. Destructive changes are **never auto-reverted** — data removed by a destructive migration is gone; rollback restores the old schema shape but not deleted data. Bundles are steered toward a soft-delete/deprecate-over-two-versions pattern (mirrors the encryption doc's phased *(a)→(e)* discipline) instead of same-release destructive drops.

---

## 8. Deletion on uninstall & backups

**Not crypto-shred by default — bundle-data is plaintext, RLS/schema-isolated, not PII (per §5's "UUIDs only" boundary).** Real row/schema deletion is the primary mechanism; crypto-shred (via the tenant DEK already established in the encryption doc) is an **opt-in defense-in-depth** for any DSL column a vendor explicitly marks `encrypted: true`, reusing that doc's key hierarchy — no second KMS integration, no second per-tenant key table.

| Event | Action |
|---|---|
| One tenant uninstalls an app | `bundle_schema_owner` runs `DELETE FROM <table> WHERE tenant_id = X` for every table in that app's schema (bypasses RLS as the owning role; the explicit `WHERE` is the safety boundary), inside one transaction, plus an append-only `bundle_data_deletions(tenant_id, app_id, table, deleted_at)` row |
| Last tenant anywhere uninstalls that app_id | Schema is **retained** for a 30-day grace window (matches the Nodes & Seats staleness convention) in case of accidental uninstall/reinstall, then a background job runs `DROP SCHEMA app_<hash> CASCADE` + a `schema_dropped` tombstone entry |
| Backup restore of `bundle-data` | **Mandatory reconciliation step, reusing the encryption doc's §4 restore runbook verbatim**: any restored row/schema matching a tombstoned `(tenant_id, app_id)` or `app_id` is re-deleted immediately post-restore, before the restored system serves traffic |

**Backups:** `bundle-data` gets its own PITR policy, shorter retention than the control-plane DB's compliance-driven window (proposed **14 days** vs. 30-90) — bundle rows aren't the platform's compliance record of truth, and a shorter window narrows the tombstone-reconciliation exposure. At-rest encryption is still the unconditional baseline (`security.md` Storage) regardless of retention length.

---

## 9. Observability

| Signal | Name / shape |
|---|---|
| Metric (histogram) | `waddles_bundle_db_call_duration_seconds{app_id,capability,op,result}` |
| Metric (counter) | `waddles_bundle_db_calls_total{app_id,result}`, `waddles_bundle_db_ast_rejections_total{app_id,reason}` (security signal for obfuscation attempts), `waddles_bundle_quota_denied_total{app_id,quota_kind}` |
| Metric (gauge) | `waddles_bundle_schema_rows{app_id}`, `waddles_bundle_schema_bytes{app_id}` |
| Trace | Span per `db.execute`/`execute-batch` call (nested under the existing invoke span); span for DDL application at approval time; span for the periodic quota-scan job |
| Log | `tracing`/penguin-logging (sanitized, per existing `capabilities.rs` pattern) — WARN at 80% of a quota, ERROR on denied/AST-rejected, INFO on schema provisioned/migrated/dropped |

All OTLP-exported per `critical-rules.md` Observability — no new backend-specific code.

---

## 10. Worked example: the fishing bundles' 7 tables

From `docs/superpowers/specs/2026-09-28-superpenguin-fish-game-port.md` §3.3 (PR #414, `docs/superpenguin-fish-game-port` branch):

| Bundle | Table | Notable DSL features exercised |
|---|---|---|
| fishing-core | `fishing_gold` | PK on `user_uuid`, no FK |
| fishing-core | `fish_catches` | FK → `fish_types.id` (cross-*bundle*-schema — see note below), `CHECK (stars BETWEEN 1 AND 3)`, index on `(user_uuid, caught_at)` |
| fishing-core | `user_fishing_boosts` | FK → `fishing_shop_items.id` (cross-bundle), partial index `WHERE is_equipped` |
| fishing-shop | `fish_types` | Unique index on `name` |
| fishing-shop | `fishing_shop_items` | Multiple nullable boost columns, defaulted `max_uses` |
| fishing-tournaments | `fishing_tournaments` | `CHECK (ends_at > starts_at)`, index on `status` for the scheduler poll |
| fishing-tournaments | `fishing_tournament_catches` | FK → `fishing_tournaments.id`, composite index for leaderboard queries (`tournament_id, gold_earned DESC`) |

**Cross-bundle FK note:** §4.3's DSL restricts FKs to "the same app's own schema." Fishing's own split (§2 of the fish-game spec) has `fish_catches` (fishing-core) referencing `fish_types` (fishing-shop) — a genuine cross-*bundle* reference. This design does **not** extend the DSL to allow cross-app-schema FKs (that would reopen the isolation boundary §5 depends on). Resolution for fishing specifically: either (a) co-locate `fish_types`/`fishing_shop_items` and `fish_catches`/`user_fishing_boosts` in one shared schema owned by a single `feature`-level app grouping (fishing-core absorbs the reference tables), or (b) drop the FK and validate `fish_type_id`/`shop_item_id` at the application layer via the existing `process-stage`/`action-stage` event contract between bundles. Either is a fishing-spec-level decision, not a blocker to this design — flagged here so the fishing implementation doesn't silently assume a cross-schema FK the DSL will reject.

---

## 11. Phased implementation plan (agent-sized, ≤30 min each)

| Phase | Task | Component | Depends on |
|---|---|---|---|
| 0 | Stand up `bundle-data` Postgres database (new instance/logical DB), `bundle_schema_owner` role, empty `rbac-matrix-bundle-data.yaml` | infra/hub-api | none |
| 0 | Add DSL structural fields (`data.schema.tables[]` etc., §4.3) to `BundleManifestV2` dataclass, parse-only, no validation logic yet | hub-api | Phase 0 |
| 0 | Add DSL field-level validators (column type enum, name regex reuse of `_TABLE_RE`, FK same-schema check) | hub-api | previous task |
| 1 | Write the DSL→DDL compiler for `CREATE TABLE` (columns, PK, defaults) — unit tests against fixed input/output pairs | hub-api | Phase 0 |
| 1 | Extend compiler for `CREATE INDEX` (btree, unique, partial `WHERE`) | hub-api | previous task |
| 1 | Extend compiler for `CHECK` constraints with the allowlisted-operator grammar | hub-api | previous task |
| 1 | Wire sqlparser-rs validation of compiler-emitted `default`/`CHECK`/index-`WHERE` expressions before DDL is finalized | hub-api | previous task |
| 2 | Add per-app schema/role provisioning step to `bundle_approval_service` (schema create, role create, grants) inside the existing approval transaction | hub-api | Phase 1 |
| 2 | Generate + attach RLS policies (tenant/community) per compiled table | hub-api | previous task |
| 2 | Extend rbac-matrix tooling (`scripts/db/rbac_matrix.py`) to also render `bundle-data` grants from the per-app role table | hub-api | previous task |
| 3 | Replace `CapabilityKind::Db` `denied` arm in `svc_process/capabilities.rs` with real `execute`: per-invoke pooled connection to `bundle-data`, `SET LOCAL` timeouts | Rust (svc_process) | Phase 2 (a schema must exist to point at) |
| 3 | Same for `svc_action/capabilities.rs` | Rust (svc_action) | Phase 2 |
| 3 | Add `SET LOCAL waddles.tenant/community/app_id` from `InvokeScope`, plus the `DISCARD ALL` panic-safe checkin guard | Rust (both stages) | previous two tasks |
| 3 | Wire sqlparser-rs AST validation of the *statement* (not DDL — DML from `db.execute`) against `data.tables`, with the negative-test suite from wit-v1.1 §7.4 | Rust (both stages) | previous task |
| 3 | Regression test: RLS context never leaks across two sequential checkouts of the same pooled connection under different tenants (wit-v1.1 §7.3) | Rust (both stages) | previous task |
| 4 | Wire `db.execute-batch` atop the same connection/timeout/AST machinery, single transaction, `EXECUTOR_DB_BATCH_MAX_STATEMENTS` cap | Rust (both stages) | Phase 3, `stage-v1_1` linker gating already specified in wit-v1.1 §5 |
| 5 | Add the per-app row/byte usage scan background job in hub-api, cached last-known-value read path | hub-api | Phase 2 |
| 5 | Wire the quota gate into the `db.execute`/`execute-batch` pre-call path (fail closed, `quota_exceeded`) | Rust (both stages) | previous task, Phase 3/4 |
| 6 | DSL-diff engine: classify a new version's schema change as additive vs. destructive per column/table | hub-api | Phase 1 |
| 6 | Additive-change auto-apply path inside version-approval transaction | hub-api | previous task |
| 6 | Destructive-change ack flag + approval-UI gate + `destructive_schema_change_requires_ack` failure path | hub-api | previous task |
| 7 | Single-tenant uninstall: transactional per-table `DELETE ... WHERE tenant_id` + `bundle_data_deletions` audit row | hub-api | Phase 2 |
| 7 | Last-tenant-uninstalled grace-window tracker + scheduled `DROP SCHEMA ... CASCADE` job + tombstone entry | hub-api | previous task |
| 7 | Extend the encryption doc's restore runbook to also reconcile `bundle-data` tombstones (schema/tenant drops), not just keystore | hub-api (runbook doc, not code) | previous task |
| 8 | `bundle-data` backup policy: separate PITR schedule, 14-day retention, at-rest encryption confirmed | infra | Phase 0 |
| 9 | OTel metrics (§9's histogram/counter/gauge set) on the new `db.execute`/`execute-batch` path | Rust (both stages) | Phase 3/4 |
| 9 | OTel spans for DDL application (hub-api) and the quota-scan job | hub-api | Phase 2, Phase 5 |
| 10 | Fishing worked example: author `fishing-core`'s DSL (§10) end-to-end through the compiler, provision the schema, run one `db.execute` insert from a test harness | hub-api + Rust | Phases 1-4 |
| 10 | Resolve the cross-bundle FK question (§10) for fishing-core/fishing-shop before their schemas are finalized | fishing spec owner | Phase 1 (DSL FK rule exists to check against) |

Phases 0-1 (DSL + compiler) have no Rust dependency and can proceed in parallel with nothing else in this plan; Phase 3 (capability wiring) has no dependency on the DSL compiler's *output format* beyond "a schema exists" and could equally be validated against a hand-provisioned test schema while Phase 1-2 lands.

---

## 12. Open questions (not blockers, flagged for follow-up)

- **Per-column opt-in encryption** (§8's `encrypted: true` DSL flag) is sketched but not designed in DSL-grammar or key-derivation detail here — worth its own short addendum once a bundle actually needs it (fishing does not).
- **`kv` capability** shares this design's rate-limit gate but has no schema dependency; wiring it is a smaller, separate PR not sequenced into the phased plan above.
- **Cross-bundle data access** (fishing-shop's tables read by fishing-core) has no capability today beyond raw event contracts between `process-stage`/`action-stage` exports — out of scope here, noted in §10.
