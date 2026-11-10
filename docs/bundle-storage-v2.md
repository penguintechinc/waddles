# Bundle storage v2 (`kv-v2` + `db-v2`)

Additive, backward-compatible successor to the bundle `kv` and `db` host
capabilities. Existing bundles keep working untouched; a bundle opts in by
importing the new interfaces (worlds `stage-next`, `stage-v2`, `stage-tools`).

| Problem (v1) | v2 |
|---|---|
| ONE table per bundle | up to **8** manifest-declared tables per bundle |
| `query(limit<=200, offset, order)`, no WHERE | typed **filtered** query: `= <> < <= > >= IN prefix IS NULL`, `AND`, `ORDER BY` an indexed column, **cursor** pagination |
| no transactions | bounded atomic **`transact`** (1..=16 ops) with `guard-version` / `guard-count` |
| kv has no CAS, no listing, flat 64 ops/invocation | `cas` with versions, `delete-if`, `list-keys`; 256 ops **and** a 1 MiB byte budget |
| row cap only | per-bundle **per-community** row + byte quotas, explicit `quota-exceeded` |

Source of truth: `wit/waddle-bundle/storage-v2.wit`. Host: `core/bundle_host_db/src/v2`,
`core/bundle_host_kv`. Executor: `core/bundle_executor/src/host/stage_next_storage_v2.rs`.
Guest helpers: `sdk/waddle-sdk-rs/src/storage_v2.rs`. DDL/provisioning:
`hub_api/services/bundle_data_v2.py`, alembic `0087_bundle_data_v2`.

## Security model (unchanged principles, new surface)

- **Scope is host-injected, never guest-supplied.** Every statement is bound to the
  invocation's authenticated `(tenant, community, bundle)`: RLS via `SET LOCAL` GUCs
  **and** an explicit `tenant_id = $1 AND community_id = $2` predicate, independently.
  A guest cannot name another community, tenant, bundle, or a physical table; it names
  only *logical* tables its approved manifest declared (resolved from
  `bundle_data.table_registry`, never from the call).
- **No SQL from guests.** A query is a typed AST (`condition`, `order`, `tx-op`). Identifiers
  come only from the declared schema after matching by name; values are bound
  parameters with an explicit cast chosen by the *declared* column type; `prefix` is
  `LIKE ... ESCAPE` with `% _ \` escaped; cursors are decoded to typed values and re-validated.
  Unit tests prove injection-shaped values never reach SQL text and injection-shaped
  identifiers are refused.
- **Query-complexity limits** (host-enforced): <= 8 conditions, <= 64 values per `IN`,
  prefix <= 256 bytes, `limit` 1..=200, cursor <= 1 KiB, 16 ops per transaction, 2 s
  statement timeout, 5 s whole-call deadline. A read must be **index-supported**: some
  condition on the leading column of a declared index (`<>`/`IS NULL` do not count; `prefix`
  needs a `prefix: true` index) or the order column is an index-leading column / `row_id`.
  Otherwise `invalid-query` names what to index. `ORDER BY` columns must be NOT NULL.
- **kv list cost is bounded by the matching range**, not the keyspace: a lexicographic index
  (`ZRANGEBYLEX [prefix (prefix\xff`), <= 1000 members examined per call, 16 `list` calls
  and a fixed 4 KiB byte cost per list per invocation.
- **PII rules unchanged.** Bundle data stays community-scoped; the v1 column rules apply
  (PII-name denylist, `user_ref` is a platform UUID, no raw PII).

## db-v2

### Manifest

```yaml
data:
  tables: [events, subs]            # names: drives the `db` capability + approval UI (unchanged)
  schema:                           # typed detail validated by bundle_data_v2.validate_schema_v2
    tables:
      - name: events                # [a-z][a-z0-9_]{0,31}, no "__"
        columns:                    # same typed allowlist + PII denylist as v1
          - {name: title,    type: "text(200)"}
          - {name: start_at, type: timestamptz}
          - {name: kind,     type: int4, nullable: true}
        indexes:
          - {columns: [start_at]}
          - {columns: [{column: title, prefix: true}]}   # text_pattern_ops: prefix queries
      - name: subs
        columns: [{name: slug, type: "text(64)"}]
        indexes: [{columns: [slug], unique: true}]       # unique per (tenant, community)
```

`data.tables` must equal the set of `data.schema.tables` names. Physical table =
`<app16>_<sha256(app_id)[:12]>__<logical>` (<= 63 bytes): **injective** -- the hash covers the exact
`app_id`, so `a.b` / `a_b` / over-long ids cannot alias, and logical names cannot contain `__`.
Every v2 name contains `__` while v1 names never do (v1 now rejects it), so the families are disjoint.
Provisioning refuses (`TableOwnershipError`) to create over a name that is registered to anyone or
already exists; `migrate_schema` only alters tables the registry says belong to this `(app_id, logical)`;
`teardown_schema` verifies every registry row against the derived name BEFORE the first `DROP`
(all-or-nothing) and never drops a table it cannot prove it owns. `bundle_data.table_registry` has
`UNIQUE (schema_kind, physical_table)`. Indexes are always created over
`(tenant_id, community_id, <declared columns>)`. Migrations are additive-only
(new tables, new nullable/defaulted columns, new indexes); anything destructive is refused.

### Semantics

- `insert/get/update/delete/count/query/transact` — rows carry `row-id`, `version`
  (optimistic concurrency), `created-at`, `updated-at`.
- **CAS:** `update`/`delete` take `expected-version`; mismatch -> `conflict`, missing -> `not-found`.
- **Pagination:** keyset on `(order column, row_id)`; the cursor is opaque, bound to the order
  column + direction it was issued for, and only repositions within the caller's own rows.
- **Transactions.** `transact` is one Postgres transaction at READ COMMITTED. Every write path
  first takes a per-`(tenant, community, bundle)` advisory transaction lock, so writers in a
  scope are **serialized** (a `guard-count` sees every earlier committed write and nothing can
  slip in before commit -- predicate-safe like SERIALIZABLE without retry storms); touched rows
  are also `FOR UPDATE`-locked. Lock order is always scope-lock then rows, so batches cannot
  deadlock. Readers never block. Any failing op rolls the whole batch back; the error names the
  op (`op 2: ...`). An interactive begin/commit across guest code is intentionally **not**
  offered: it would pin a connection and hold the scope lock while the guest runs.
  Failure modes: `conflict` (version/guard/unique -- re-read and retry), `invalid-*` (nothing
  written), `quota-exceeded`, `timeout`.
- **Quotas:** `bundle_data.usage` keeps live row + byte counters per `(bundle, tenant,
  community)` across all the bundle's tables, updated by a conditional upsert in the same
  transaction as the write (bytes = Postgres `pg_column_size(row)`, heap only). Defaults:
  **100 000 rows / 64 MiB** per bundle per community. A write that would exceed either rolls back
  with `quota-exceeded`; deletes and shrinking updates free quota. Other communities are unaffected.

## kv-v2

- `get-versioned` / `cas(key, expected-version, value, ttl)` / `delete-if(key, version)`.
  A key's **version is a content digest** (`1 + first 52 bits of sha1(value)`, `0` = absent)
  computed inside Valkey, so *any* writer -- including v1 `set`/`increment` -- changes it;
  v1 and v2 can safely share keys. Equal content means equal version, which is correct for
  read-modify-write loops. Treat it as an opaque token. `cas` creation counts against the
  10 000-key quota. `increment` is unchanged and remains the right tool for counters.
- `list-keys(prefix, cursor, limit<=200)`: lexicographic, prefix-scoped, cursor = last key.
- **Limits per invocation:** 256 ops, **1 MiB** of key+value bytes moved, 16 `list` calls
  (each also costs 4 KiB of the byte budget). Value <= 64 KiB, key <= 256 bytes (unchanged).
- Rolling upgrade: pods running the previous kv code do not maintain the lex index; a key they
  create is invisible to `list-keys` until it is next written by a new pod. An app whose lex index
  is missing is rebuilt once from the live index on the next `list-keys`.

## Wire (executor <-> stage)

`capability=db`, ops `db2.<insert|get|update|delete|query|count|transact>`;
`capability=kv`, ops `get_versioned|cas|delete_if|list`. Shapes are pinned by contract fixtures
in `bundle_host_db::v2::wire` tests and `stage_next_storage_v2` tests. Both stages call one
function: `bundle_host_db::v2::wire::handle(...)` (db) and `KvHost::{get_versioned,cas,delete_if,list}` (kv).

## Provisioning and stage wiring

Both stages serve v2: `svc_process` and `svc_action` route `db2.*` through
`bundle_host_db::v2::wire::handle` and the four kv v2 ops through `KvHost`. Each loads
`bundle_data.table_registry` at startup and every 30 s. The action stage builds its `db` wiring
exactly like the process stage: `BUNDLE_DB_HOST|PORT|NAME|USER` + `BUNDLE_DB_PASSWORD`
(role `waddles_bundle_runtime`), gated by the `waddles.bundle-db-capability` flag. Password unset =>
INFO and every `db`/`db2.*` call denies `not_implemented`; password set but connect fails => ERROR,
same denial (a misconfiguration, not an opt-out).

Tables follow the **global install lifecycle** in hub-api (`bundle_approval_service`), via
`services/bundle_data_provisioner.py`, **fail closed**:

| Event | Call | Failure |
|---|---|---|
| `install_version_globally`, no live install | `provision_schema` | install not recorded, error propagates |
| `install_version_globally`, live install exists (upgrade) | `migrate_schema(old, new)` (additive only) | destructive change => 409 `bundle_data_migration_refused`, install not recorded |
| `uninstall_globally` | `teardown_schema` (only registry-proven owned tables) | uninstall not recorded, nothing dropped |

Only manifests declaring `data.schema` are touched; a manifest without it provisions nothing and
never opens a migrator connection. An invalid `data.schema` is a 422 `invalid_data_schema`. The DDL
runs as `waddles_bundle_migrator` on a dedicated psycopg2 connection (host/port/db/options from
`DATABASE_URL`, role from `BUNDLE_MIGRATOR_USER`, password from `BUNDLE_MIGRATOR_PASSWORD`; unset
password => 503 `bundle_data_provisioning_unavailable`, approval refused). The table provider
(`builtin` -> `app_core`, else `thirdparty` -> `app_community`) is derived from the app-id namespace
(`waddles.core.` prefix), never from the manifest's own claim.

Atomicity: the migrator transaction commits only after the install-state write committed, and rolls
back if either step fails. One residual window is reported loudly rather than hidden: the migrator
commit failing after the state write committed raises 500 `bundle_data_commit_failed`. For an
install, re-approving recovers (the tables did not commit); for an uninstall, drop the app's tables
by hand with `teardown_schema`.

Real-path tests: `hub_api/tests/test_bundle_data_lifecycle.py` (approval provisions, upgrade
migrates, destructive upgrade refused, uninstall drops only owned tables, provisioning failure aborts
approval; real Postgres) and `core/svc_action/tests/storage_v2_real_path.rs` (action-stage
`db2.*` + kv v2 round trip against real Postgres and Valkey containers).

## Migration guides

### calendar (#1114): kv-only -> db-v2 + kv-v2

The README's kv layout capped ~40 events/community and had a read-modify-write race.

1. Declare `events`, `rsvps`, `sync_map` tables in `data.schema`; index `events.start_at`,
   `events.status` (approval queue), `rsvps.event_ref` (+ `unique` on `(event_ref, user_ref)` via a
   composite index), `sync_map.event_ref`. (`description` trips the PII-name denylist -- use `details`.)
2. "Upcoming events": `query(table=events, filter=[ge(start_at, now)], order=start_at asc, limit=25)` and
   follow `next-cursor` -- no scan, no cap.
3. "Approval queue": `filter=[eq(status, 0)]` ordered by `start_at` (int enum codes, not strings).
4. RSVP upsert: `transact([guard-count(rsvps where event_ref=E and user_ref=U, 0..1), insert])`
   or rely on the unique index and map `conflict` to "already RSVPed"; switching an RSVP is
   `update` with `expected-version`.
5. Approve/reject/update: `update` with the row `version` (CAS); on `conflict` re-read and retry
   (`storage_v2::update_with_retry`).
6. Capacity-limited events: `transact([guard-count(rsvps, 0..cap-1), insert rsvp])` -- atomic, no overbook.
7. Sync mapping (remote id, attempts, payload hash): `update` the `sync_map` row with
   `expected-version` instead of last-write-wins kv.
8. Keep kv for tiny per-event idempotency markers using `cas(key, 0, ...)` (create-if-absent) so two
   workers never apply the same key twice.
9. Data migration: one-time `list-keys("ev.")` + `get` per key -> `transact` batches of <= 16 inserts.

### dj (#1122): kv last-write-wins -> kv-v2 CAS (+ db-v2 for the queue)

1. Per-community settings / policy blobs: replace `get`+`set` with `read_modify_write` (CAS loop);
   no more lost updates when two mods edit concurrently.
2. Request queue + playlists: move to `db-v2` tables `requests` (index `(status, created_at)` via
   `created_at` order) and `playlist_items` (index `playlist_ref`, order by `position`), so
   "next up", "my requests", and per-user caps are indexed queries; `move` = `transact` of two `update`s guarded by
   `guard-version`. Per-user cooldown/max = `guard-count(requests where user_ref=U and status=0, 0..max-1)`.
3. Hourly AI-fill cap and per-user cooldown counters: keep `kv.increment` (atomic, unchanged).
4. The overlay snapshot `dj.public_state` stays a single kv value written with `cas`.
5. Replay/duplicate-song guards: a `unique` index on `(song_ref, status)` or a `guard-count` in the same
   `transact` as the insert.

## Limits at a glance

| | value |
|---|---|
| tables per bundle | 8 |
| columns / indexes per table | 32 / 8 (<= 4 columns per index) |
| filter conditions / `IN` values / prefix | 8 / 64 / 256 B |
| query `limit` | 1..=200 |
| transaction ops | 16 |
| row quota / byte quota per bundle per community | 100 000 / 64 MiB |
| db statement timeout / call deadline | 2 s / 5 s |
| kv ops / bytes / lists per invocation | 256 / 1 MiB / 16 |
| kv live keys per bundle per community | 10 000 |
