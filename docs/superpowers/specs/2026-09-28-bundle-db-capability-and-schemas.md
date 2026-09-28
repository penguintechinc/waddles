# Bundle `db` Host Capability & Bundle-Owned Data Tables — Design (Rev 2)

**Status:** Proposed design (no code in this doc) — **Rev 2, supersedes Rev 1 after Gemini review round 1 (FAILED) + a mid-review steer on table cardinality/naming**
**Date:** 2026-09-28
**Scope:** `core/svc_process/src/capabilities.rs`, `core/svc_action/src/capabilities.rs`, `hub_api/services/bundle_manifest_v2.py`, `hub_api/services/bundle_approval_service.py`, new `bundle-data` Postgres database, new fixed-template table provisioner in hub-api.
**Drivers:** `db`/`kv` are hardcoded `denied` for every call today (`core/svc_process/src/capabilities.rs:221-228`, `core/svc_action/src/capabilities.rs:582-589`). Rev 1 designed a manifest-DSL-to-DDL compiler with a raw-SQL escape hatch and per-app Postgres schemas; Gemini found three CRITICAL defects in that approach (below). Rev 2 replaces the whole data-access model.
**Builds on (approved, not re-litigated here):** `docs/superpowers/specs/2026-09-28-wit-stage-v1-1-design.md` §7.2/§7.3 (`SET LOCAL` statement/lock timeouts, RLS leak-proofing, per-component `Linker` isolation) — reused for mechanism, **not** its §3/§7.4 sqlparser-rs AST-validation path, which this design retires for the `db` capability entirely (§4). `docs/superpowers/specs/2026-09-28-tenant-envelope-encryption-design.md` (key hierarchy, restore runbook) — reused verbatim for crypto-shred (§8).
**Umbrella alignment:** `docs/bundle-permissions-capability-gate` (in progress, not yet merged) defines permission ids `storage.tables` / `storage.objects`, the `authorize(scope, permission, resource)` gate, and `AppScoped` resources. This design implements `storage.tables` only; object storage is out of scope, referenced by name only (§6).

---

## 1. Gemini review round 1 → resolution

| # | Sev | Finding (Rev 1) | Resolution (Rev 2) | Where |
|---|---|---|---|---|
| 1 | CRITICAL | Parser differential: bundle-supplied SQL (DML via `db.execute`, plus a raw-SQL DDL escape hatch) validated with sqlparser-rs — a non-Postgres parser — created a gap between what's validated and what Postgres executes | **No bundle-supplied SQL at all, anywhere, including the escape hatch.** Bundles call a structured `tables.*` API; the host builds 100% parameterized SQL from typed, host-authored templates. Any future SQL-text parsing (tooling only, never the hot path) must use `pg_query`, the real Postgres parser | §4 |
| 2 | CRITICAL | Per-app Postgres role + implied per-app/per-tenant pool multiplication | **One role (`waddles_bundle_runtime`), one pool per service** (svc_process, svc_action — two pools total, same role). Isolation is RLS on session GUCs set via `SET LOCAL` from server-resolved scope, `RESET ALL`/`DISCARD ALL` at checkin, pinned `search_path` | §5 |
| 3 | CRITICAL | Manifest-DSL→DDL compiler emitting bundle-influenced columns/types/FKs/CHECKs per app — unbounded DDL grammar, catalog multiplication (apps × N tables) | **No bundle-influenced DDL ever.** One fixed, platform-authored table template (identical columns for every app); hub-api provisions it by name only at approval. Re-scoped further mid-review to exactly one physical table per app (§3) | §3 |
| 4 | HIGH | Needed: `SET LOCAL`-only enforcement, negative tests for CTE/comment/SET-RESET smuggling | Enforced structurally — there's no SQL text path for a bundle to smuggle through. Negative tests retarget the query **builder**: reject any code path that emits session-level `SET`, and fuzz filter/order-by values for injection-as-parameter (never as text) | §4.2, §9 |
| 5 | HIGH | Quotas via periodic scan (`pg_class.reltuples`) — stale, scan-based | **Transactional counters** — a fixed trigger on the one template maintains a per-`(app_id, tenant_id)` row/byte counter in the same transaction as every write; the pre-write check reads that counter, not a scan. Small per-call row/byte limits, fail closed | §7 |
| 6 | HIGH/MED | 30-day schema-retention grace window before physical delete; no emergency override; no migration semantics for per-app schema versions | Crypto-shred (DEK destroy) is immediate on uninstall; physical rows chunk-delete promptly after a short human-confirmable hold window (no 30-day retention); optional encrypted archive is opt-in only; migration semantics defined for the fixed-shape model (kinds/slot-mappings, not DDL) | §8, §10 |
| 7 | — (new, umbrella alignment) | Rev 1 predated the permission-gate umbrella spec | Every `tables.*` call gated by `authorize(scope, "storage.tables", AppScoped(app_id))` before connection acquisition; object storage (`storage.objects`) referenced only, not designed here | §6 |

---

## 2. Where bundle data lives (unchanged from Rev 1, re-affirmed)

A second, physically separate `bundle-data` Postgres database — not a schema inside the control-plane database. svc_process/svc_action get a direct, restricted-role connection to `bundle-data`; hub-api is DDL-only there via a distinct `waddles_bundle_ddl` role, used only at approval/migration/uninstall time, never at request time.

| Reason | Detail |
|---|---|
| Blast radius | A bundle bug can never reach `app_versions`, `communities`, `connection_credentials`, billing, or auth tables — no network route, not merely missing grants |
| Backup/quota/noisy-neighbor decoupling | Own PITR policy, own quota-scan-free counters, own connection pool — never contends with or restores over the control-plane DB |

```
hub-api (waddles_bundle_ddl: provision/migrate/drop, request-time never)      svc_process / svc_action
                                                                          (waddles_bundle_runtime: DML only, RLS-scoped)
        │                                                                              │
        ▼                                                                              ▼
   ┌──────────────────────────────────  bundle-data (new DB), one schema  ─────────────────────────────────┐
   │  table core_fishingcore   table community_a1b2c3d4   table core_fishingshop   ...  (one per app)       │
   └──────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Data model: exactly one physical table per app, one fixed template

**Rule: every app bundle gets exactly one table.** Multi-entity bundles use a `kind` discriminator column plus four generic sort slots — never multiple tables, never bundle-declared columns.

### 3.1 Fixed template (identical for every app, platform-authored, versioned as a migration — never compiled from a manifest)

| Column | Type | Purpose |
|---|---|---|
| `row_id` | `uuid` PK, `default gen_random_uuid()` | Row identity |
| `tenant_id` | `uuid not null` | RLS key |
| `community_id` | `uuid not null` | RLS key |
| `kind` | `text not null` | Discriminator — the "logical entity" a generic-JSONB app would otherwise need a separate table for |
| `version` | `bigint not null default 1` | Optimistic concurrency |
| `data` | `jsonb not null` | Bundle payload — schemaless, UUID-only per PII rules |
| `sort_num_1`, `sort_num_2` | `numeric`, nullable | Declared-mapping numeric order/filter slots (e.g. leaderboard score) |
| `sort_text_1`, `sort_text_2` | `text`, nullable | Declared-mapping text order/filter slots |
| `created_at`, `updated_at` | `timestamptz not null default now()` | Audit |

Indexes (3 relations per table, all part of the same fixed template): PK on `row_id`; `(tenant_id, community_id)` for RLS-scoped lookups; `(tenant_id, kind, sort_num_1, sort_num_2, sort_text_1, sort_text_2)` for leaderboard/order-by queries — `tables.query`'s `order_by` is restricted to a prefix of this tuple (standard btree prefix rule), which the host enforces, not the bundle.

A `BEFORE INSERT/UPDATE/DELETE` trigger (also part of the fixed template, `bundle_data.fn_update_counters()`) maintains `_bundle_counters(app_id, tenant_id, row_count, byte_estimate)` transactionally (§7) — no bundle input reaches the trigger definition.

The manifest declares only **metadata**, stored in hub-api's control-plane DB (never in `bundle-data`, never as DDL input): the set of `kind` values the app uses, and which logical field of each `kind` maps to which of the four sort slots. This is validation/translation metadata for the host's query builder (§4), not schema.

### 3.2 Table naming and identifier safety

Name = `{prefix}_{safe_app_id}`, where `prefix` is `core` for first-party `waddles.core.*` bundles and `community` for third-party/vendor bundles (e.g. the superpenguin bundles) — derived server-side at catalog approval from the bundle's provider/namespace, **never from the manifest's own claim** — and `safe_app_id` is the app id lower-cased with any character outside `[a-z0-9_]` rejected at manifest-approval time (the platform's existing app-id slug rule). If `prefix + "_" + safe_app_id` exceeds 63 bytes (Postgres `NAMEDATALEN` limit), `safe_app_id` is truncated and an 8-hex-char suffix of `sha256(app_id)` is appended to guarantee uniqueness. The computed name is derived **once**, server-side, at approval time, stored as `bundle_data_table_name` in hub-api's app registry (authoritative source — the data plane looks it up, never re-derives it), and every reference to it in generated SQL goes through the database client's identifier-quoting function (`quote_ident`), never `format!` string interpolation. Hyphens (`core-<app-id>` as originally proposed) are avoided in the physical identifier to sidestep needing quoted-identifier discipline everywhere; the hyphenated form remains the *display* convention in hub-api's UI/API responses.

### 3.3 Physical-per-app-table vs. shared-partitioned-table — numbers, and the recommendation

With the cardinality now fixed at **one table per app** (not per-app-times-N as in Rev 1), the catalog-bloat math changes substantially from what Gemini flagged.

| | (a) Physical per-app table, fixed template | (b) One shared table, hash-partitioned |
|---|---|---|
| Catalog footprint at 5k apps | 5,000 tables × 4 relations (heap + 3 indexes) = 20,000 relations | ~128-256 partitions, flat regardless of app count |
| Catalog footprint at 10k apps | 40,000 relations | Same ~128-256 partitions |
| Per-query planning cost | Plans against one app's own rows only — no need to prune a shared multi-tenant table down from a much larger combined row count | Partition pruning narrows to one partition, but that partition still holds many apps' rows |
| Noisy-neighbor | None — a runaway app's writes/vacuum load only touches its own table | Real — apps sharing a hash partition contend for the same heap/indexes/vacuum cycles |
| DDL frequency | Once per app at approval (rare, not hot-path) — no ongoing catalog churn | Fixed at deploy time, never grows |
| Operational ceiling | Tens of thousands of tables is well within Postgres's established operating envelope — `autovacuum_max_workers`'s per-relation stats check is a cheap catalog scan, and actual VACUUM work only runs on tables crossing the dead-tuple threshold, so thousands of mostly-quiet per-app tables aren't themselves a vacuum bottleneck. The commonly-cited concern threshold for catalog-wide tooling (`pg_dump`, some monitoring queries) latency is in the hundreds-of-thousands-of-relations range | No ceiling from table count; ceiling instead comes from single-partition row/write volume as apps × rows grows |

**Recommendation: (a), physical per-app table from the fixed template**, at the stated 5-10k app scale — it matches Justin's naming convention (real tables, not a logical partition key), avoids noisy-neighbor entirely, and the catalog-size numbers hold comfortably at this scale. **Revisit at ~50k-100k apps**: fold into (b) (either by moving to shared hash-partitioned storage, or by sharding across multiple `bundle-data` databases) before catalog-wide tooling latency becomes a real operational cost. This is a documented, honest scaling limit, not a hidden one.

### 3.4 Limits of the fixed-slot model (stated honestly)

- No DB-enforced foreign keys between kinds or between apps — referential integrity is application-layer only (via the existing `process-stage`/`action-stage` event contract), same conclusion Rev 1 reached for cross-bundle references, now the default for *all* references since the generic table has no FK mechanism at all.
- Exactly four sort slots (2 numeric, 2 text) per app, shared across every `kind` that app declares. A bundle needing a fifth independent order/filter dimension must either reuse a slot by convention (e.g., encode a composite sort key) or filter in-memory after a bounded `tables.query` (capped by `limit`) — acceptable for the fishing/leaderboard use case, a real constraint for anything needing more than two independent numeric rankings per entity kind.
- `order_by` must be a prefix of the fixed index tuple (§3.1) — arbitrary JSONB-path ordering is not indexed and not offered in v1.

---

## 4. Capability: structured `tables.*` API (no SQL, ever)

### 4.1 WIT surface

| Function | Signature (informal) | Notes |
|---|---|---|
| `tables.insert` | `(kind: string, data: jsonb, sort-keys: {num1?, num2?, text1?, text2?}) -> {row-id, version} \| error` | `error` ∈ `{access-denied, quota-exceeded, invalid-kind}` |
| `tables.get` | `(row-id: uuid) -> row \| not-found \| access-denied` | RLS + `tenant/community` scope applied server-side |
| `tables.query` | `(kind?: string, filters: list<{slot: sort-slot, op: eq\|ne\|lt\|lte\|gt\|gte\|in, value}>, order-by?: {slot, dir}, limit: u32) -> list<row> \| access-denied` | `filters`/`order-by` reference only `kind` (eq-only) and the four declared sort slots — never a free column name; `limit` capped at `TABLES_QUERY_MAX_LIMIT` (default 200) |
| `tables.update` | `(row-id, expected-version, data, sort-keys) -> {version} \| conflict \| not-found \| access-denied` | Optimistic concurrency — mismatch never applies a partial write |
| `tables.delete` | `(row-id, expected-version) -> ok \| conflict \| not-found \| access-denied` | Same concurrency contract |

No `execute`/`execute-batch`, no raw-SQL manifest field, no DDL field. This retires wit-v1.1 §3's `db.execute` design and its DSL-escape-hatch (Rev 1 §4.2) entirely for the `db` capability.

### 4.2 Host implementation (query builder, not a parser)

The host translates each typed call into one of a small, fixed set of **host-authored, parameterized SQL statement templates** (values always bound as `$1, $2, ...`, never interpolated into the statement text). Because the statement text is fixed and only the bound values vary, there is no bundle-controlled SQL text to parse or validate — the parser-differential risk (finding 1) doesn't arise structurally, rather than being patched after the fact.

- **If any SQL text is ever parsed** by future tooling (e.g., an offline query-log auditor) — never on this hot path — it must use `pg_query` (the real Postgres parser, via its Rust bindings), never sqlparser-rs.
- Negative tests target the builder itself, not a parser: attempting to pass a filter `value` containing SQL syntax (`'; DROP TABLE--`) must round-trip as an inert bound parameter; attempting to request `order_by` on a non-declared slot must be rejected with `invalid-kind`/a builder-level error before any SQL is constructed; no code path in the builder may emit session-level `SET` (CI grep-gate, §9) — only `SET LOCAL`.

---

## 5. Pooling and isolation

| Element | Design |
|---|---|
| Role | Exactly one, `waddles_bundle_runtime` — `GRANT SELECT, INSERT, UPDATE, DELETE` on every per-app table (Postgres `GRANT ... ON ALL TABLES IN SCHEMA bundle_data`, re-applied at provisioning time so new app tables inherit it automatically) |
| Pools | Exactly one per service — svc_process holds one pool, svc_action holds one pool, both authenticate as `waddles_bundle_runtime`. No per-app, per-tenant, or per-bundle pool |
| `search_path` | Pinned to `bundle_data, pg_catalog` for both pools — never bundle-influenced |
| Scope injection | `SET LOCAL waddles.tenant_id`, `waddles.community_id`, `waddles.app_id` at the start of every transaction, values taken **only** from the server-resolved `InvokeScope`, never from bundle `args` |
| RLS | Policy on every per-app table: `USING (tenant_id = current_setting('waddles.tenant_id')::uuid AND community_id = current_setting('waddles.community_id')::uuid)` — generated once as part of the fixed template, identical for every app |
| Checkin | `RESET ALL` then `DISCARD ALL` under a `Drop`-guard, regardless of success/panic/early-return — guarantees no GUC or prepared-statement state survives into the next transaction on a reused connection |
| DDL role | `waddles_bundle_ddl` — used only by hub-api at approval/migration/uninstall time, never in the request-time pools above |

**Negative tests (moot for SQL injection since there is no bundle SQL, still load-bearing for the pooling/RLS contract):** two sequential tenants checking out the same pooled connection never see each other's RLS scope; a transaction that panics mid-call still runs the checkin guard; an attempt anywhere in the builder to issue a session-level `SET` (vs. `SET LOCAL`) fails a lint/CI gate.

---

## 6. Authorization (umbrella alignment)

Every `tables.*` call runs `authorize(scope, "storage.tables", AppScoped(app_id))` **before** connection acquisition — same fail-closed-before-any-connection-touched posture as the rate-limit gate (§7). `scope` is the server-resolved `InvokeScope` (tenant, community, app_id), never bundle input. This is the single source of truth for whether an app may call `tables.*` at all; the per-app physical table's existence (§3) is what it acts on once authorized — the two are independent gates (permission, then object).

Object storage (`storage.objects`) is a sibling permission under the same umbrella spec (`docs/bundle-permissions-capability-gate`) and is out of scope here — mentioned only so the two permission ids aren't designed in isolation from each other.

---

## 7. Quotas — fail closed, transactional, no scans

| Quota | Mechanism |
|---|---|
| Rows per `(app_id, tenant_id)` | `_bundle_counters` row, maintained by the fixed-template trigger (§3.1) in the *same transaction* as every insert/delete — no periodic scan job exists in this design |
| Bytes per `(app_id, tenant_id)` | Same trigger, `byte_estimate` incremented/decremented by `pg_column_size(NEW)`/`(OLD)` |
| Per-call size | `tables.insert`/`update` payload capped (default 64 KiB `data`); `tables.query` `limit` capped at `TABLES_QUERY_MAX_LIMIT` (default 200) — checked host-side before any SQL is issued |
| Enforcement | The trigger raises before the write commits if the counter would exceed the app's cap — the same transaction that would have exceeded the quota is the one that fails, so there is never a partial write and never a stale-scan race window |
| Exceeding any quota | `quota-exceeded` error, fail closed — never a partial write, never a silent one-time allowance |

---

## 8. Deletion on uninstall & backups

| Event | Action |
|---|---|
| Uninstall initiated | Short (default 5 min), human-confirmable hold window in the approval/uninstall UI — the **emergency override**: canceling within the window aborts the uninstall with zero data effect. This is the only recovery point; once the hold expires, step 2 is irreversible |
| Hold expires | The tenant's per-`(tenant, app)` data-encryption key is **crypto-shredded immediately**, reusing the tenant-envelope-encryption design's key hierarchy verbatim. Any `data` subfield a vendor marked `encrypted: true` becomes permanently unrecoverable at that instant — this is the actual deletion guarantee, not a promise contingent on the physical-delete job finishing |
| Physical delete | Chunked, `LIMIT`-batched `DELETE FROM <app table> WHERE tenant_id = $1 LIMIT 5000` in a loop, each batch its own transaction, looping to zero rows affected; a checkpoint row in hub-api's control-plane DB tracks progress so a restarted job resumes rather than rescanning. **No 30-day retention window** — this replaces Rev 1's grace-window-before-drop, since crypto-shred already makes encrypted data unrecoverable and plaintext fields carry no PII (§3.4's UUID-only boundary) worth retaining |
| Optional archive | Only if a compliance/legal-hold flag is set **at uninstall time** (default off): an encrypted export of the tenant+app rows, encrypted under a separate, retained archive key — never the just-shredded per-`(tenant, app)` DEK, so shredding remains a genuine one-way door regardless of the archive flag |
| Backup restore of `bundle-data` | Mandatory reconciliation step, reusing the encryption doc's restore runbook verbatim: any restored row matching a tombstoned `(tenant_id, app_id)` is re-deleted immediately post-restore, before the restored system serves traffic |

**Backups:** `bundle-data` gets its own PITR policy (proposed 14-day retention, shorter than the control-plane DB's compliance window), at-rest encryption as the unconditional baseline regardless of retention length (`security.md` Storage).

---

## 9. Migration semantics (fixed-shape model — no DDL diffing)

Because every app's table has the *same* physical shape forever, there is no per-app DDL to diff. What "migrates" between bundle versions is the manifest's declared metadata: the set of `kind` values and the sort-slot mapping (§3.1).

| Change | Classification | Handling |
|---|---|---|
| New `kind` value | Additive | Free — auto-applied at approval, no reviewer step |
| New field inside `data` for an existing `kind` | Additive | Free — JSONB is schemaless; old rows simply lack the field, bundle read code must tolerate absence |
| Removing/renaming a `kind` still present in existing rows | Breaking | Requires `data.migration.allow_destructive: true` **and** reviewer acknowledgment; hub-api runs a declared, chunked (`LIMIT`-batched, same mechanism as §8) transform job that rewrites affected rows before the new version activates |
| Reassigning which logical field maps to a sort slot | Breaking | Same as above — old rows' `sort_num_*`/`sort_text_*` values were written under the old mapping and must be rewritten by the declared transform, not silently reinterpreted |
| Any shape change too large for a live transform | — | Recommended default: ship as a new `app_id` (e.g. `fishing-core-v2`), coexisting with the old app, data carried across via the normal `process-stage`/`action-stage` event contract rather than a live in-place rewrite |

**Rollback:** additive changes roll back for free — old bundle code simply never references the new kind/field/slot mapping. A destructive-change transform is **not automatically reversible**; recovery is either a documented reverse-transform authored and reviewed the same way as the forward one, or a PITR restore + tombstone-reconciliation (§8) for genuine emergencies. Never automatic.

---

## 10. Observability

| Signal | Name / shape |
|---|---|
| Metric (histogram) | `waddles_bundle_tables_call_duration_seconds{app_id,op,result}` |
| Metric (counter) | `waddles_bundle_tables_calls_total{app_id,result}`, `waddles_bundle_tables_builder_rejections_total{app_id,reason}` (invalid slot/kind/order-by — replaces Rev 1's AST-rejection metric since there's no AST), `waddles_bundle_quota_denied_total{app_id,quota_kind}` |
| Metric (gauge) | `waddles_bundle_table_rows{app_id}`, `waddles_bundle_table_bytes{app_id}` (sourced from `_bundle_counters`, not a scan) |
| Trace | Span per `tables.*` call (nested under the invoke span); span for table provisioning/migration/drop at hub-api |
| Log | `tracing`/penguin-logging, sanitized — WARN at 80% of a quota, ERROR on denied/rejected, INFO on table provisioned/migrated/dropped |

All OTLP-exported per `critical-rules.md` Observability — no new backend-specific code.

---

## 11. Worked example: the fish game, collapsed to one table per app

Rev 1's 7 tables across `fishing-core` (3), `fishing-shop` (2), `fishing-tournaments` (2) collapse to **one table per app**, discriminated by `kind`:

| App | Table | `kind` values (former tables) | Sort-slot mapping |
|---|---|---|---|
| fishing-core | `core_fishingcore` | `gold_balance`, `fish_catch`, `fishing_boost` | `fish_catch`: `sort_num_1=stars`, `sort_text_1=caught_at` (ISO8601, string-sortable) |
| fishing-shop | `core_fishingshop` | `fish_type`, `shop_item` | `shop_item`: `sort_text_1=name` (uniqueness enforced app-side, not DB-side) |
| fishing-tournaments | `core_fishingtournaments` | `tournament`, `tournament_catch` | `tournament_catch`: `sort_num_1=gold_earned` (leaderboard `ORDER BY sort_num_1 DESC LIMIT n`), `sort_text_1=tournament_id` |

The former `CHECK (stars BETWEEN 1 AND 3)` and FK constraints (`fish_catches.fish_type_id → fish_types.id`, cross-*bundle* in Rev 1) no longer exist at the DB layer — every reference is application-layer, validated by the bundle at write time and re-validated via the existing `process-stage`/`action-stage` event contract between fishing-core and fishing-shop. This is a strict generalization of Rev 1 §10's "resolution (b)" (drop the FK, validate at the application layer) — now the *only* option, since the fixed-shape model has no FK mechanism for any app, not just cross-bundle ones. **This removes Rev 1's open "cross-bundle FK" question entirely** — there's nothing left to resolve.

---

## 12. Phased implementation plan (agent-sized, ≤30 min each)

| Phase | Task | Component | Depends on |
|---|---|---|---|
| 0 | Author the fixed table template (columns, indexes, RLS policy, counter trigger) as a versioned platform migration — not compiled from any bundle input; unit test asserts exact DDL text for a given table name | hub-api / infra | none |
| 0 | Table-name derivation function (charset validation, server-side prefix selection from bundle provider/namespace — `core_`/`community_`, never manifest-claimed — 63-byte truncation + hash suffix) + collision unit tests | hub-api | none |
| 0 | Stand up `bundle-data` DB, `waddles_bundle_ddl` role (DDL-only), `waddles_bundle_runtime` role (DML-only, `GRANT ... ON ALL TABLES` re-applied per provision) | infra | none |
| 1 | Manifest fields: `data.table.kinds[]`, `data.table.sort_slots` mapping — structural validation only, no DDL derived from them | hub-api | Phase 0 |
| 1 | Approval-flow hook: derive table name, run the fixed template via `waddles_bundle_ddl` inside the approval transaction, persist name + kind/slot metadata | hub-api | Phase 0 |
| 2 | WIT interface: `tables.insert/get/query/update/delete`, error variants | Rust (wit) | Phase 1 |
| 2 | Query builder: typed filter/order-by/limit → fixed parameterized statement templates; injection-as-value negative tests | Rust (shared) | previous task |
| 3 | Replace `CapabilityKind::Db` `denied` arms in `svc_process`/`svc_action` `capabilities.rs` with real dispatch; one pool per service, `waddles_bundle_runtime` | Rust (both stages) | Phase 2 |
| 3 | `SET LOCAL` scope injection from `InvokeScope` + statement/lock timeouts + `RESET ALL`/`DISCARD ALL` panic-safe checkin guard | Rust (both stages) | previous task |
| 3 | `authorize(scope, "storage.tables", AppScoped(app_id))` gate wired before connection acquisition | Rust (both stages) | umbrella permission-gate spec merged |
| 3 | RLS-leak regression test: two sequential tenants on one pooled connection | Rust (both stages) | previous three tasks |
| 4 | Quota trigger wiring (`_bundle_counters`) + per-call size/limit caps in the builder; fail-closed tests | hub-api (trigger) + Rust (caps) | Phase 3 |
| 5 | Uninstall flow: hold-and-confirm UI/API step, DEK-shred call (reuse encryption doc), chunked `LIMIT`-batched delete with resumable checkpoint | hub-api | Phase 3 |
| 5 | Optional encrypted-archive path (feature-flagged, default off) | hub-api | previous task |
| 6 | Kind/slot-mapping diff: classify additive vs. destructive metadata changes on version upgrade | hub-api | Phase 1 |
| 6 | Destructive-change ack flag + reviewer gate + chunked transform job (reuse §8's batching) | hub-api | previous task |
| 7 | OTel metrics/traces/logs (§10) | Rust (both stages) + hub-api | Phase 3, 4 |
| 8 | Fishing worked example: provision `core_fishingcore`/`core_fishingshop`/`core_fishingtournaments`, port the 7 former tables' data shapes into kind/slot mappings, one end-to-end `tables.insert`+`tables.query` through a test harness | hub-api + Rust | Phases 1-4 |

Phase 0-1 (template + naming + manifest metadata) has no Rust dependency and can proceed in parallel with nothing else in this plan.

---

## 13. Open questions

- **`core_`/`community_` prefix — confirmed.** `core_` = first-party `waddles.core.*` bundles, `community_` = third-party/vendor bundles (e.g. superpenguin). Derived server-side at catalog approval from the bundle's provider/namespace, never from the manifest's own claim (§3.2). Purely a naming/provenance signal — every table, regardless of prefix, still carries `tenant_id` + `community_id` and the same RLS policy (§5); there is no structural fork between the two prefixes.
- **Per-column opt-in encryption** (`encrypted: true` on a `data` subfield, §8): sketched, not designed in derivation detail — worth its own short addendum once a bundle needs it.
- **`kv` capability**: shares this design's `authorize`/quota posture but has no table dependency; wiring it is a smaller, separate follow-up not sequenced into the plan above.
