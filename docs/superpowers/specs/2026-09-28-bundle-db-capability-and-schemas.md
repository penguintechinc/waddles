# Bundle `db` Host Capability & Bundle-Owned Data Tables — Design (Rev 5)

**Status:** Proposed design (no code in this doc) — **Rev 5, supersedes Rev 4** — folds in Gemini review round 3's PASS-WITH-CONDITIONS findings on #415 as normative requirements (§1 round 3 rows).
**Date:** 2026-09-28
**Scope:** `core/svc_process/src/capabilities.rs`, `core/svc_action/src/capabilities.rs`, `hub_api/services/bundle_manifest_v2.py`, `hub_api/services/bundle_approval_service.py`, hub-api's DSAR/erasure workflow, `waddles` Postgres: two new schemas (`app_core`, `app_community`), `waddles_bundle_runtime` and `waddles_bundle_migrator` roles.
**Drivers:** Rev 1 (manifest-DSL-to-DDL, raw-SQL escape hatch, per-app schemas) failed Gemini round 1 (parser differential, pooling, catalog bloat). Rev 2 fixed those with a fixed jsonb+slot template in a separate `bundle-data` DB. Rev 3 added declared typed columns + `user_ref`/erasure for a new user-linked-column requirement. Rev 4 responded to Gemini round 2 + Justin's explicit call: no second database, two dedicated schemas in `waddles` instead, stronger defense-in-depth on the query path. Rev 5 folds in Gemini round 3's PASS-WITH-CONDITIONS items (§1): static enum-mapping proof, `FORCE ROW LEVEL SECURITY`, literal-only column defaults, expanded `REVOKE`/default-privilege precision, `user_ref` cache TTL + erasure invalidation + nullability rule, a stronger PII name-heuristic, `jsonb` secondary review + key scanning, and concrete per-bundle ops thresholds.
**Builds on (unchanged):** `docs/superpowers/specs/2026-09-28-wit-stage-v1-1-design.md` §7.2/§7.3 (`SET LOCAL` timeouts, RLS leak-proofing, per-component `Linker` isolation — **not** its §3/§7.4 sqlparser-rs path, retired since Rev 2). `docs/superpowers/specs/2026-09-28-tenant-envelope-encryption-design.md` for crypto-shred (§9).
**Umbrella alignment (unchanged):** `docs/bundle-permissions-capability-gate` — `storage.tables`/`storage.objects` permission ids, `authorize(scope, permission, resource)`, `AppScoped` resources, `users.profile.read`'s non-identifying field set (referenced only).
**PII boundary (unchanged):** hub-api's `users` table is the sole PII boundary. Bundle tables hold UUIDs only, never names/usernames/emails/phones. No join or lookup path from a bundle table to `users` exists at request time (§4, §5).

---

## 1. Gemini review history

| Round | Sev | Finding | Resolution | Where |
|---|---|---|---|---|
| 1 | CRITICAL | Parser differential: bundle-supplied SQL validated with sqlparser-rs, a non-Postgres parser | No bundle-supplied SQL anywhere, including the raw-SQL escape hatch — structured `tables.*` API, host builds parameterized SQL | §6 |
| 1 | CRITICAL | Per-app Postgres role + pool multiplication | One role (`waddles_bundle_runtime`), one pool per service | §7 |
| 1 | CRITICAL | Manifest-DSL→DDL compiler, bundle-influenced FKs/CHECKs, catalog multiplication | Flat typed-column allowlist, no FK/CHECK, one table per app | §3 |
| 1 | HIGH | `SET LOCAL`-only; negative tests for smuggling | Enforced structurally; builder-level negative tests | §6.2 |
| 1 | HIGH | Quotas via periodic scan | Transactional counters, fail closed | §8 |
| 1 | HIGH/MED | 30-day retention, no override, no migration semantics | Immediate crypto-shred, chunked deletes, override, migration semantics | §9, §10 |
| 2 | CRITICAL | A second physical `bundle-data` database (and any implied per-scale sharding) is unwarranted operational surface — one more instance to provision, back up, patch, and monitor, for isolation this design already gets from roles/RLS | **No separate database.** Bundle tables live in the shared `waddles` Postgres, in two dedicated schemas | §2 |
| 2 | HIGH | Table naming needs to make bundle vs. service/internal tables visually and structurally unambiguous at a glance, not just by a string prefix on a flat name | Two real Postgres schemas, `app_core`/`app_community` — schema membership *is* the marker, not a naming convention riding on a shared namespace with service tables | §3.4 |
| 2 | HIGH | The data-plane write role's blast radius against the *rest of the same instance* wasn't spelled out once bundle tables share physical infrastructure with control-plane tables | `waddles_bundle_runtime` explicitly `REVOKE`d from every other schema/database object; `GRANT`ed only `USAGE` + DML on `app_core`/`app_community`; documented as a narrowly-scoped exception to "hub-api is the only RW path," not a repeal of it | §2, §5 |
| 2 | HIGH | `order_by` (and implicitly filter `column`) as a runtime string, even allowlist-checked, is more surface than needed | `order_by`/filter `column` become a closed WIT enum generated per app at provisioning time (`indexed-column`) — not a string compared at runtime at all | §6.1 |
| 2 | HIGH | RLS-only isolation (`SET LOCAL` + `current_setting`) is a single mechanism; a pooled-connection GUC leak would be silently fatal | **Defense in depth:** every query template also carries an explicit `tenant_id = $n AND community_id = $m` predicate, bound from the same `InvokeScope` that feeds `SET LOCAL`, independently of it — plus the existing `RESET ALL`/`DISCARD ALL` at checkin | §6.2, §7 |
| 2 | MED | Catalog-scale numbers were computed for an isolated `bundle-data` instance; sharing `waddles` changes the operational picture (shared autovacuum workers, shared connection/WAL budget with control-plane tables) | Restated numbers for the shared-instance case, with an autovacuum note, a monitoring threshold, and an explicit revisit trigger | §3.6 |
| 3 (PASS-WITH-CONDITIONS) | C1.1 | `indexed-column` enum → column mapping must be provably static, not just "not a string at runtime" | The mapping is a static lookup table generated once at onboarding (provisioning time) — no bundle value is ever interpolated into it, at generation or at lookup | §6.2 |
| 3 | C1.2 | RLS policies alone don't bind the table owner (`waddles_bundle_migrator`) itself | `ALTER TABLE ... FORCE ROW LEVEL SECURITY` on every app table, unconditionally, part of the fixed template | §3.4, §7 |
| 3 | C2.1 | `now()`/`gen_random_uuid()` as bundle-declared column defaults are function calls, not literals — reopens a (small) expression-evaluation surface | Bundle-declared column defaults are scalar literals only (number, bool, quoted string ≤N chars, `NULL`), checked with a strict literal parser — no functions, casts, or expressions, ever. Platform-owned columns keep their function defaults (fixed template, not bundle input) | §3.2 |
| 3 | C3.1–C3.3 | Runtime role's negative-grant surface (`REVOKE` list) and the `public` schema's default-permissive grants weren't fully enumerated | `REVOKE TEMP ON DATABASE waddles FROM waddles_bundle_runtime`; `REVOKE CREATE, USAGE ON SCHEMA public FROM PUBLIC`; `ALTER DEFAULT PRIVILEGES FOR ROLE waddles_bundle_migrator IN SCHEMA app_core, app_community GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO waddles_bundle_runtime` (role-scoped default privileges, not a bare schema-wide default) | §2 |
| 3 | C4.1 | `user_ref` tenant-membership cache had no stated TTL or erasure-invalidation | TTL ≤30s; cache entry invalidated immediately on user erasure (§4's cascade sweep also clears it) | §4 |
| 3 | C4.2 | A `NOT NULL user_ref` column paired with `on_erasure: anonymize` is unsatisfiable (anonymize needs to null the column) | `user_ref` columns must be `nullable`, unless `on_erasure` is `DELETE ROW` (the default) — a `NOT NULL user_ref` + `anonymize` combination is rejected at manifest validation | §3.2, §4 |
| 3 | C5.1 | PII name-heuristic denylist was small and exact-match, missing separator/case variants and common synonyms | Normalized (case- and separator-insensitive) match against an expanded denylist: `email`, `e-mail`, `mail`, `name`, `username`, `handle`, `login`, `phone`, `address`, `ip`, and their compounds | §3.2 |
| 3 | C5.2 | `jsonb` columns' 16 KiB cap addressed size, not content — a jsonb blob is the one place free-form PII could still land | `jsonb` columns are flagged for mandatory secondary human review at vendor approval (in addition to the size cap); write-time key-name scanning against the same PII denylist drops or rejects offending keys before the write reaches Postgres | §3.1, §2.1 |
| 3 | Ops | Monitoring thresholds for the shared-instance model were qualitative, not actionable | Concrete revisit-per-bundle triggers: a single app table exceeding 50 GB, sustained >500 IOPS, or RLS-policy evaluation exceeding 15% of query CPU flags that specific bundle for remediation (index review, quota tightening, or promotion to a dedicated database) | §3.6 |

---

## 2. Where bundle data lives

**No separate database.** Bundle tables live in the same `waddles` Postgres instance as every control-plane/service table, inside two dedicated schemas: `app_core` (first-party `waddles.core.*` bundles) and `app_community` (third-party/vendor bundles, e.g. superpenguin). Schema membership is the isolation and provenance marker — not a naming convention layered onto a shared flat namespace.

```
                         waddles (single Postgres instance)
   ┌───────────────────────────────────────────────────────────────────────┐
   │  control-plane schemas (public, billing, auth, ...)                   │
   │    - RW only via hub-api's normal service roles (unchanged)           │
   │    - waddles_bundle_runtime: NO grants here (explicit REVOKE)         │
   │                                                                       │
   │  app_core                          app_community                     │
   │    table waddles_core_quotes         table superpenguin_fishing_core │
   │    table fishing_core                table ...                       │
   │    table fishing_shop                                                │
   │    - waddles_bundle_migrator: DDL only, hub-api, approval/upgrade time│
   │    - waddles_bundle_runtime: USAGE + SELECT/INSERT/UPDATE/DELETE only │
   └───────────────────────────────────────────────────────────────────────┘
              ▲                                              ▲
        hub-api (waddles_bundle_migrator)          svc_process / svc_action
        provision/migrate/drop/erase                (waddles_bundle_runtime,
        request-time: never                          RLS + explicit predicate)
```

**Role model:**

| Role | Grants | Used by | When |
|---|---|---|---|
| `waddles_bundle_migrator` | `CREATE`/`ALTER`/`DROP` on `app_core`/`app_community` only; no DML grant needed (it never runs application queries) | hub-api only | Bundle approval, version migration, uninstall, DSAR erasure sweep |
| `waddles_bundle_runtime` | `USAGE` on `app_core`/`app_community` only; `SELECT, INSERT, UPDATE, DELETE` on all tables in those two schemas via `ALTER DEFAULT PRIVILEGES FOR ROLE waddles_bundle_migrator IN SCHEMA app_core, app_community GRANT ... TO waddles_bundle_runtime` (role-scoped to the migrator's own future creations, not a bare schema-wide default, so a table created by any other role never silently grants); explicit `REVOKE ALL` on every other schema and on the database's default `PUBLIC` grants; **`REVOKE TEMP ON DATABASE waddles FROM waddles_bundle_runtime`** (no temp-table creation); no `CREATE` anywhere | svc_process, svc_action (one pool per service, §7) | Every `tables.*` call |

**Instance-wide hardening (applies regardless of this role, standard baseline):** `REVOKE CREATE, USAGE ON SCHEMA public FROM PUBLIC` — no role, bundle-related or not, gets default access to `public` anymore.

**This is a narrowly-scoped exception to "hub-api is the only RW path to control-plane data," not a repeal of it.** The invariant now reads: hub-api is the only RW path to every schema *except* `app_core`/`app_community`, where `waddles_bundle_runtime` gets DML-only, schema-scoped write access — a boundary enforced by Postgres `GRANT`/`REVOKE`, not by which database the connection happens to be pointed at.

**Honest tradeoff vs. Rev 1-3's separate `bundle-data` database:** sharing the instance means bundle tables now compete with control-plane tables for the same `autovacuum_max_workers` pool, connection limit, WAL volume, and PITR/backup policy — isolation that used to be physical (no network route) is now role/schema/RLS logical isolation only. That logical isolation is still complete for *data leakage* (§2's `REVOKE` list, plus RLS, plus the new explicit-predicate defense in depth, §6.2) — what's given up is *resource* isolation (noisy-neighbor at the vacuum/WAL level), addressed operationally in §3.6, not architecturally. The escape hatch (split back out to a dedicated database or instance) remains available if the §3.6 revisit trigger fires; it is not required today.

### 2.1 Onboarding pipeline integration

The data-plane role (`waddles_bundle_runtime`) is real-time read/write only — it never runs DDL. **Every** create-table/index/RLS-policy/grant/upgrade/drop is `waddles_bundle_migrator`, run by hub-api as an explicit step of the existing bundle onboarding pipeline (auth → world-conformance validation → stage to MinIO → provision consumer groups → approval → activation), not a bolt-on side effect:

| Pipeline step | Bundle-table action | Actor / role | Core (`app_core`) vs. Community (`app_community`) |
|---|---|---|---|
| auth | none | — | — |
| world-conformance validation | Schema validation: `data.table.columns[]`/`indexes[]` checked against the type allowlist (§3.1), column/index/width caps, PII name-heuristic gate (§3.2) — rejects here, before anything is staged | hub-api (manifest validator) | Identical — validation doesn't distinguish provenance |
| stage to MinIO | none (package artifact staging only) | — | — |
| provision consumer groups | **Table provisioning happens in this same step** — schema+table name derived (§3.4), DDL plan computed | hub-api (`waddles_bundle_migrator`) | **Vendor (`app_community`):** DDL plan computed here, **execution deferred** until the approval step — a rejected upload never leaves a table behind. **Core (`app_core`):** provisioned immediately via the system seeder path (first-party code, no human-approval gate) |
| approval | Global-admin approval decision. **Any `jsonb`-typed column declared by a vendor bundle adds a mandatory secondary human review item** (in addition to write-time key-name PII scanning, §3.1/§3.2) — approval cannot complete on the primary reviewer's sign-off alone | Global admin (human) + secondary reviewer for `jsonb` columns | **Vendor:** approval is what unblocks the deferred DDL apply from the previous step — `CREATE TABLE`/indexes/RLS/grants execute now, inside the approval transaction. **Core:** no separate approval gate here — already provisioned via the seeder path, gated instead by the platform's normal code-review/merge process for `waddles.core.*` (a `jsonb` column in a core bundle still gets the same secondary-review flag in that review process) |
| activation | Table becomes reachable to `tables.*` calls | svc_process/svc_action (`waddles_bundle_runtime` — grants already exist from provisioning, but `authorize()`/app-active checks, §8, gate actual traffic until this step) | Identical |
| (new-version approval) | Upgrade: schema-version diff, additive auto-apply or destructive ack+transform (§11) | hub-api (`waddles_bundle_migrator`) | Same vendor/core split as initial approval |
| (uninstall) | Teardown: hold-and-confirm, DEK crypto-shred, chunked delete, optional archive (§10) | hub-api (`waddles_bundle_migrator`) | Identical — teardown isn't provenance-gated |

**Backups:** bundle tables now inherit the same PITR/backup policy as the rest of `waddles` — no separately-tunable shorter retention window (a capability Rev 2's separate database offered and this revision gives up deliberately, per Justin's call). At-rest encryption is unconditional regardless (`security.md` Storage, applies to the whole instance already).

---

## 3. Data model: one physical table per app, declared typed columns

Unchanged in shape from Rev 3 — one table per app bundle, declared typed columns from a strict allowlist, mandatory platform-owned columns, no bundle-influenced FK/CHECK grammar. What changes in Rev 4 is **where** the table lives (§2) and **how it's named** (§3.4).

### 3.1 Type allowlist (unchanged from Rev 3)

`user_ref` (uuid, platform user reference, §4), `uuid`, `int4`, `int8`, `numeric(p,s)` (`p≤38, s≤12`), `bool`, `text(max_len)` (`max_len≤8192`), `timestamptz`, `jsonb` (≤16 KiB, host-enforced pre-write; **also flagged for mandatory secondary human review at vendor approval** and write-time key-name PII scanning, §2.1/§3.2 — size alone doesn't address content). No other types.

### 3.2 Column declaration & limits (unchanged from Rev 3)

Name `^[a-z][a-z0-9_]{0,62}$`, type from §3.1, `nullable`.

**`default`: scalar literals only** — a number, `bool`, a quoted string (≤ the column's `max_len`), or `NULL`. Checked with a strict literal parser (a grammar that accepts exactly one of those four shapes and nothing else) — **no functions, casts, or expressions**, for any bundle-declared column, full stop. (Platform-owned columns, §3.3, keep their fixed function-based defaults — `gen_random_uuid()`, `now()` — because those are authored in the platform's own template, not bundle input; the literal-only rule applies to what a *bundle* can declare.)

**PII name-heuristic gate:** column names are normalized (lower-cased, separators `-`/`_`/` ` collapsed) and matched against an expanded denylist — `email`, `e-mail`, `mail`, `name`, `username`, `handle`, `login`, `phone`, `address`, `ip`, `ssn`, `dob`, `birth` — plus their compounds (`first-name`, `display_name`, etc., all normalize to a `name` match). A match **rejects** the manifest at onboarding (`column_name_suggests_pii`), not a silent warning — a reviewer may only proceed by renaming and explicitly confirming no PII.

**`user_ref` nullability:** a `user_ref` column must be declared `nullable`, unless the table's `on_erasure` action for that column is `DELETE ROW` (the default, §4). Declaring `NOT NULL` together with `on_erasure: anonymize` is unsatisfiable (anonymize nulls the column) and is **rejected at manifest validation**.

Caps: ≤32 declared columns, ≤8 indexes, ≤4 columns per composite index.

### 3.3 Platform-owned (mandatory) columns (unchanged from Rev 3)

`row_id` (PK), `tenant_id`, `community_id`, `version`, `created_at`, `updated_at` — host-managed, never bundle-writable. Every table also gets `ALTER TABLE ... FORCE ROW LEVEL SECURITY` (§7) as part of the same fixed template step that attaches the RLS policy — without `FORCE`, the table's owning role (`waddles_bundle_migrator`) would bypass RLS by default; `FORCE` closes that even though the migrator never issues DML in normal operation.

### 3.4 Table naming and identifier safety

**Schema** is chosen server-side at catalog approval from the bundle's provider/namespace — `app_core` for first-party `waddles.core.*` bundles, `app_community` for everything else — **never from the manifest's own claim**. **Table name** is the sanitized app id: lower-cased, any character outside `[a-z0-9_]` rejected at manifest-approval time, truncated with an 8-hex-char `sha256(app_id)` suffix if it would exceed Postgres's 63-byte `NAMEDATALEN`. Every reference is schema-qualified and goes through `quote_ident` for both the schema and table name — never string interpolation. Examples: `app_core.waddles_core_quotes`, `app_community.superpenguin_fishing_core`. The fully-qualified name is computed once at approval, stored as `bundle_data_table_name` in hub-api's app registry (authoritative — the data plane looks it up, never re-derives it).

### 3.5 Indexes (unchanged from Rev 3)

Bundle-declared, drawn from its own columns, compiler auto-prefixes every index with `tenant_id`. No expression or partial indexes.

### 3.6 Catalog impact in the shared instance — numbers and ops notes

| | 10k apps, typical (avg. 2 indexes) | 10k apps, worst case (8 indexes) |
|---|---|---|
| Relations added to `waddles` across both schemas | ~40,000 (10,000 tables × 4 relations) | ~100,000 (10,000 × 10) |

This is *in addition to* `waddles`'s existing control-plane relation count (a few hundred, typically) — negligible relative contribution, but the **shared autovacuum worker pool** (default `autovacuum_max_workers = 3`, cluster-wide, not per-schema) now cycles through control-plane tables and up to 10k+ bundle tables together, whereas Rev 2's separate database had its own independent worker pool.

**Ops notes (new for the shared-instance case):**
- **Autovacuum tuning for the app schemas:** set a lower `autovacuum_vacuum_cost_delay`/higher `autovacuum_vacuum_cost_limit` on `app_core`/`app_community` tables specifically (via `ALTER TABLE ... SET (...)`, applied by the same fixed template so it's uniform, not bundle-influenced) so bundle-table vacuum work doesn't starve control-plane tables of worker time during contention.
- **Monitoring:** table bloat (dead-tuple ratio per relation), RLS policy evaluation cost as a share of query CPU, and lock-wait time on `app_core`/`app_community` tables, alongside the existing combined-relation-count and `last_autovacuum`-lag signals.
- **Revisit trigger — per-bundle, not just instance-wide:** any single app table crossing **50 GB**, sustained **>500 IOPS**, or **RLS evaluation exceeding 15% of that table's query CPU** flags *that specific bundle* for remediation — index review, quota tightening, or promotion to its own dedicated database — independent of the instance-wide 50,000-relation/50-100k-app trigger, which still applies for the aggregate case.

### 3.7 Limits of the model (unchanged from Rev 3)

No DB-enforced FK/CHECK — application-layer only via the event contract. Multi-entity bundles use a `kind` discriminator + bounded `jsonb` extras column (§13). The PII name-heuristic (§3.2) catches name-shaped risk only, not value-shaped risk — stated honestly, not solved.

---

## 4. User reference columns & DSAR cascade erasure

`user_ref` carries the platform user's UUID only, never PII. Write-time validation confirms `(tenant_id[, community_id])` membership against `waddles_bundle_reader`'s RO-replica connection before a `bundle-data` write proceeds. **Cache: TTL ≤30s, and the cached entry is invalidated immediately on user erasure** — the erasure workflow (below) clears it as its first step, so a write can't succeed against a stale "still a member" cache entry for a user mid-erasure. `bundle_user_ref_columns` (hub-api control-plane table) registers every declared `user_ref` column at provisioning time; a DSAR/erasure request extends hub-api's existing erasure workflow with a chunked (`LIMIT`-batched) sweep — default `DELETE ROW`, opt-in `on_erasure: anonymize` (nulls the column + any `pii_adjacent: true` column, only valid when the column is declared `nullable`, §3.2) — via `waddles_bundle_migrator`, fail closed on incompleteness.

---

## 5. Linking to external identity data without crossing the PII boundary (unchanged from Rev 3)

(a) event-delivered actor/target UUIDs; (b) optional `users.profile.read` — non-identifying fields only, defined by the umbrella spec; (c) `{user:<uuid>}` placeholders, detokenized by svc-action at send time — the bundle process never holds a display name. `waddles_bundle_runtime`'s schema-scoped grants (§2) mean there is no `GRANT`-level path from a bundle table to `users` even if a bundle somehow constructed a cross-schema reference — a second, independent enforcement of the same "no PII" guarantee.

---

## 6. Capability: structured `tables.*` API (no SQL, ever)

### 6.1 WIT surface

| Function | Signature (informal) | Notes |
|---|---|---|
| `tables.insert` | `(column-values: map<string, value>) -> {row-id, version} \| error` | Keys ⊆ declared columns; platform columns rejected if present |
| `tables.get` | `(row-id: uuid) -> row \| not-found \| access-denied` | — |
| `tables.query` | `(filters: list<{column: indexed-column, op: filter-op, value}>, order-by?: {column: indexed-column, dir}, limit: u32) -> list<row> \| access-denied` | **`indexed-column` is a WIT enum generated per app at provisioning time** (variants = that app's declared indexed columns, embedded into the app's compiled component bindings) — not a string, not runtime-checked against an allowlist, a compile-time-closed set from the bundle's own perspective. `filter-op` is a fixed WIT enum (`eq\|ne\|lt\|lte\|gt\|gte\|in`) — hardcoded, never extended per app. `limit` capped (default 200) |
| `tables.update` | `(row-id, expected-version, column-values) -> {version} \| conflict \| not-found \| access-denied` | Optimistic concurrency |
| `tables.delete` | `(row-id, expected-version) -> ok \| conflict \| not-found \| access-denied` | Same |

No `execute`/`execute-batch`, no raw-SQL field, no DDL field.

### 6.2 Host implementation — query builder, not a parser, with explicit-predicate defense in depth

The enum-variant-to-column mapping is a **static lookup table generated once, at onboarding** (provisioning time, §2.1) — a fixed array/match indexed by the enum's own discriminant, built entirely from the app's already-validated, already-quoted column identifiers. **No bundle value is ever interpolated into this mapping, at generation time or at lookup time** — generation reads only hub-api's own provisioning-time metadata, and lookup is an enum match, not a string comparison against anything request-supplied. The host then builds one of a fixed set of parameterized SQL templates from the resolved identifier. **Two independent, non-substitutable enforcement layers on every statement, not one:**

1. **RLS via `SET LOCAL`** — `waddles.tenant_id`/`community_id`/`app_id` set per transaction from `InvokeScope`, policy `USING (tenant_id = current_setting(...) AND community_id = current_setting(...))`.
2. **Explicit `WHERE tenant_id = $n AND community_id = $m` predicate, in every query template, in addition to RLS** — bound from the *same* `InvokeScope` independently of the `SET LOCAL` call, so a bug or leak in one mechanism (e.g. a stale GUC surviving a pooled-connection checkin) does not silently fall through to the other. App scope is enforced by table/schema selection itself (each app has exactly one table — there is no column-level "app" predicate to add).

`RESET ALL` then `DISCARD ALL` under a `Drop`-guard still runs at every checkin (§7), regardless of success/panic/early-return — the explicit predicate is additive to this, not a replacement for it.

- Any future SQL-text parsing (tooling only) must use `pg_query`, never sqlparser-rs.
- Negative tests: a filter/order-by `value` containing SQL syntax round-trips as an inert bound parameter; the builder has no code path that can construct a query referencing a column outside the requested `indexed-column` enum's mapping (it's a closed match, not a lookup that can miss); no code path emits session-level `SET`, only `SET LOCAL` (CI grep-gate); a regression test asserts the explicit `tenant_id`/`community_id` predicate is present in every generated template, independent of the RLS test.

---

## 7. Pooling and isolation

| Element | Design |
|---|---|
| Role | `waddles_bundle_runtime` only — schema-scoped grants per §2, explicit `REVOKE` everywhere else |
| Pools | One per service (svc_process, svc_action) — two pools total |
| `search_path` | Pinned to `app_core, app_community, pg_catalog` — never bundle-influenced, never includes any control-plane schema |
| Scope injection | `SET LOCAL waddles.tenant_id/community_id/app_id` per transaction from `InvokeScope` |
| RLS + explicit predicate | Both, always, per §6.2 — plus `FORCE ROW LEVEL SECURITY` on every table (§3.4) so the policy binds even the owning role |
| Checkin | `RESET ALL` then `DISCARD ALL` under a `Drop`-guard |
| DDL role | `waddles_bundle_migrator` — hub-api only, never request-time (§2) |

---

## 8. Authorization (umbrella alignment, unchanged)

Every `tables.*` call runs `authorize(scope, "storage.tables", AppScoped(app_id))` before connection acquisition. `storage.objects`/`users.profile.read` referenced only.

---

## 9. Quotas (unchanged mechanism)

Transactional per-`(app_id, tenant_id)` row/byte counters via a platform-owned trigger, no scans. Per-column size caps (§3.1) and per-call `limit` cap enforced host-side pre-write. Fail closed, never a partial write.

---

## 10. Deletion on uninstall & backups (updated for the shared instance)

5-minute hold-and-confirm → immediate per-`(tenant, app)` DEK crypto-shred (tenant-envelope-encryption design, reused verbatim) → chunked `LIMIT`-batched physical delete with resumable checkpoint, no 30-day retention window → optional encrypted archive only if flagged at uninstall. Restore reconciliation reuses the encryption doc's runbook verbatim. **Backups now follow `waddles`'s single PITR/retention policy** (§2 — the separately-tunable shorter retention Rev 2's dedicated database offered is given up). Uninstall deletion (`tenant_id`-scoped) and DSAR erasure (`user_ref`-scoped, §4) remain independent mechanisms.

---

## 11. Migrations (unchanged from Rev 3)

`bundle_table_schema_versions` (hub-api control-plane) stores each version's declared columns/indexes. Additive changes (new nullable/defaulted column, new index) auto-apply. Destructive changes (type change, drop, `NOT NULL` with no default) require `data.migration.allow_destructive: true` + reviewer ack + a declared chunked transform, or a new `app_id`. Rollback: additive free, destructive not auto-reversible.

---

## 12. Observability (unchanged metric set from Rev 3)

`waddles_bundle_tables_call_duration_seconds{app_id,op,result}`, `waddles_bundle_tables_calls_total{app_id,result}`, `waddles_bundle_tables_builder_rejections_total{app_id,reason}`, `waddles_bundle_quota_denied_total{app_id,quota_kind}`, `waddles_bundle_invalid_user_ref_total{app_id}`, `waddles_bundle_erasure_sweep_rows_total{app_id,action}`, gauges `waddles_bundle_table_rows{app_id}`/`waddles_bundle_table_bytes{app_id}`. Spans per call and per DDL/migration/erasure-sweep. Sanitized `tracing`/penguin-logging throughout.

---

## 13. Worked example: fish game bundles

| App | Table | Bundle-declared columns | Indexes (tenant_id auto-prefixed) |
|---|---|---|---|
| fishing-core (first-party) | `app_core.fishing_core` | `user_ref`; `kind` text(32) (`gold_balance`\|`fish_catch`\|`fishing_boost`); `fish_caught` int4, nullable; `score` int8, nullable; `caught_at` timestamptz, nullable; `fish_type_ref` uuid, nullable (app-layer link, no DB FK); `payload` jsonb ≤16KiB, nullable | `(kind, score desc)`, `(user_ref)` |
| fishing-shop (first-party) | `app_core.fishing_shop` | `kind` text(32); `name` text(128); `price` int4, nullable; `metadata` jsonb ≤4KiB, nullable | `(kind, name)` |
| fishing-tournaments (first-party) | `app_core.fishing_tournaments` | `kind` text(32); `user_ref` uuid, nullable; `tournament_ref` uuid, nullable; `gold_earned` int8, nullable; `starts_at`/`ends_at` timestamptz, nullable | `(kind, tournament_ref, gold_earned desc)` |
| superpenguin-fishing-core (third-party, illustrative) | `app_community.superpenguin_fishing_core` | Same shape as `fishing_core`, vendor-declared | Same pattern |

`tables.query`'s `order_by` for the leaderboard case is the `indexed-column` enum variant corresponding to `(kind, score desc)` — generated into fishing-core's bindings at approval, not a string. Leaderboard output is `{user:<uuid>} — 42 fish, 1,204 pts`; svc-action detokenizes at send time (§5c). No DB-enforced FK exists between `fish_type_ref`/`tournament_ref` and their targets — application-layer only, which is what retired the original "cross-bundle FK" question back in Rev 2.

---

## 14. Phased implementation plan (agent-sized, ≤30 min each)

| Phase | Task | Component | Depends on |
|---|---|---|---|
| 0 | Create `app_core`/`app_community` schemas; `waddles_bundle_migrator` (DDL-only, those 2 schemas) and `waddles_bundle_runtime` (USAGE+DML, those 2 schemas, `ALTER DEFAULT PRIVILEGES`, explicit `REVOKE ALL` elsewhere) roles | infra/hub-api | none |
| 0 | Typed-column DDL compiler (mandatory + declared columns/indexes, `quote_ident`, schema-qualified) + fixed input/output unit tests | hub-api | Phase 0 (schemas) |
| 0 | Column/index/composite-width validators + PII name-heuristic gate | hub-api | previous task |
| 0 | Table-name derivation: server-side schema selection (provider/namespace), charset validation, 63-byte truncation + hash suffix | hub-api | none |
| 0 | `bundle_user_ref_columns`, `bundle_table_schema_versions` control-plane tables | hub-api | none |
| 1 | Manifest fields `data.table.columns[]`/`indexes[]`; approval-flow hook derives schema+table name, runs DDL via `waddles_bundle_migrator` in the approval transaction, persists schema-version snapshot + `user_ref` registry rows | hub-api | Phase 0 |
| 2 | WIT interface + per-app `indexed-column` enum generation at provisioning time; query builder maps enum variants to pre-quoted identifiers (no runtime string lookup); `user_ref` write-time RO-replica validation | Rust (wit + shared) | Phase 1 |
| 3 | Replace `CapabilityKind::Db` `denied` arms; one pool per service; `SET LOCAL` scope + explicit `tenant_id`/`community_id` predicate in every template (§6.2); panic-safe `RESET ALL`/`DISCARD ALL` checkin; `authorize(...)` gate; RLS-leak **and** explicit-predicate-presence regression tests | Rust (both stages) | Phase 2, umbrella permission-gate spec merged |
| 4 | Quota trigger wiring + per-column size caps + fail-closed tests | hub-api + Rust | Phase 3 |
| 5 | Uninstall (hold-and-confirm, DEK-shred, chunked delete) + DSAR cascade-erasure sweep | hub-api | Phase 1, 3 |
| 6 | Schema-version diff engine — additive auto-apply, destructive ack + chunked transform | hub-api | Phase 1 |
| 7 | OTel metrics/traces/logs; autovacuum tuning + monitoring threshold on `app_core`/`app_community` (§3.6) | Rust + hub-api + infra | Phase 3, 4, 5 |
| 8 | Fishing worked example end-to-end (insert/query/leaderboard order-by/`user_ref` write) | hub-api + Rust | Phases 1-4 |

Phase 0-1 has no Rust dependency and can proceed in parallel with nothing else in this plan.

---

## 16. Implementation status (updated 2026-09-29)

| Phase | Status | Notes |
|---|---|---|
| 0 (schemas + roles) | **Done** | `alembic/versions/0030_bundle_app_schemas.py`, merged |
| 0 (DDL compiler) | **Done** (PR #430, open) | `hub_api/services/bundle_data_ddl.py`/`bundle_data_schema.py` |
| 3 (host wiring, `svc_process` only) | **Partial (this PR)** | `core/bundle_host_db` implements `insert`/`get`/`update`/`delete` (RLS + explicit tenant/community predicate, `user_ref` UUID enforcement, size/row quotas, statement timeout, per-call deadline), wired into `core/svc_process/src/capabilities.rs` behind `waddles.bundle-db-capability` (default OFF) and the interim manifest-declared-capability gate (`storage.tables`, mirrors `bundle_host_kv::authorize`) |
| 3 (host wiring, `svc_action`) | **Not started** | Same crate, needs the same `DbWiring` plumbing as `svc_process` |
| 2 (WIT interface) | **Proposed, not landed** | `stage.wit`'s `db` interface still exposes the retired `execute(statement, params)` shape (SS1 round-1 CRITICAL: no bundle-supplied SQL). This PR's host-side wiring dispatches on op strings (`insert`/`get`/`update`/`delete`) at the existing untyped `{capability, op, args}` host-API layer instead of changing the WIT file, because `stage.wit` is also consumed by `core/bundle_executor`'s `bindgen!`-generated `db::Host` trait (`core/bundle_executor/src/host/imports.rs`) and all three Tier-1 SDKs -- changing its shape requires updating those in the same change, out of this slice's scope. See the PR description for the proposed replacement interface (structured `insert`/`get`/`query`/`update`/`delete`, `query`'s `column` as a plain validated `string` rather than the per-app generated `indexed-column` enum this section originally specified, until that codegen step exists) |
| 3 (`query`/list) | **Not started** | Host-side op only; also blocked on the `indexed-column` codegen note above |
| 4 (quota trigger) | **Partial** | Pre-write `COUNT(*)` under the same transaction, not yet the trigger-based counter this section specifies |
| 5 (uninstall/DSAR erasure) | Not started | |
| 7 (OTel) | **Done for the ops landed** | `waddles_bundle_tables_*` metrics + spans, `core/bundle_host_db::metrics` |

## 15. Open questions

- **Autovacuum tuning specifics** (§3.6): exact `autovacuum_vacuum_cost_delay`/`cost_limit` values for `app_core`/`app_community` need a load test against realistic bundle-table write rates before Phase 0 locks the template defaults.
- **PII name-heuristic false negatives** (§3.7): a value-level scanner over `text`/`jsonb` contents remains a sketched follow-up, not designed.
- **Per-column opt-in encryption**, **`kv` capability**: unchanged from Rev 3, still follow-ups.
