# Waddles v3 Rust Data Plane — Multi-Tenant Scale Design

Amends `2026-09-14-rust-data-plane-design.md` (§4.2/4.3/4.5, §5, §7, §9). That spec is
correct for one tenant per deployment; this document removes that ceiling. It does not
change the wire protocol, envelope contracts, WIT world, or trust boundaries — only
*how many* of each thing a replica owns and *when* it loads a bundle.

## 0. Sizing targets (authoritative, illustrative math throughout is order-of-magnitude)

| Dimension | Target |
|---|---|
| Tenants | 100s (worked examples use 500) |
| Communities | 10,000s (worked examples use 20,000) |
| Ingest sources (channels), all platforms | ~10,000 |
| Active bundle **installs** ((app_id, tenant/community) rows) | 10,000s (worked examples use 30,000) |
| Distinct **digests** (content-addressed compiled artifacts) | low thousands — most communities run the same first-party catalog; only the long tail of custom bundles adds unique digests |
| Users | 1000s |

The install-count vs. digest-count gap is load-bearing: §3 depends on it.

## 1. Multi-tenant replicas + work partitioning

**Problem.** `BUNDLE_SCOPE_TENANT_ID` (`core/svc_process/src/lib.rs:461`) hardcodes one
tenant per Deployment. At 500 tenants that's 500 Deployments of svc-process and
svc-action each — a Kubernetes object count and control-loop cost nobody should carry,
and it caps replica reuse at exactly the tenants that happen to share a pod.

**Decision: shared replica pool, hash-partitioned by source stream, ownership via
Valkey lease.**

- Partition key space: fixed `N_PARTITIONS = 2048` virtual partitions (headroom for
  hundreds of replicas without repartitioning the hash space itself).
  `partition(source_id) = rendezvous_hash(source_id) % 2048` — HRW (highest random
  weight) hashing, not `hash % replica_count`, because replica count changes constantly
  under HPA and HRW only reassigns the fraction of keys that must move, not everything.
- Ownership: `SET waddles:lease:partition:{n} {replica_id} NX PX {LEASE_TTL_MS}`
  (default TTL `30000`), renewed every `LEASE_RENEW_MS` (default `10000`) with a
  compare-and-renew Lua script (renew only if still the holder). A replica reconciles
  its owned-partition set every tick: claim unowned/expired partitions whose sources
  hash to them, release partitions it holds but whose sources no longer hash to it
  (post-rebalance), and release everything on graceful shutdown (`DEL`, not wait-for-TTL)
  so scale-down is near-instant.
- **Why Valkey leases over alternatives:** (a) a `StatefulSet` ordinal / `hash %
  replica_count` scheme was considered and rejected — it doesn't survive scale up/down
  without a coordinated rehash, and HPA replica churn would thrash it constantly; (b) an
  external coordinator (Raft group, dedicated scheduler) was considered and rejected —
  unjustified new infra when Valkey already provides atomic `SET NX`/Lua and is already a
  hard dependency; (c) client-side ring computed from a heartbeat set (no explicit lease,
  Kafka-consumer-group style) was considered — lower renewal traffic, but needs a
  handoff/fencing protocol to avoid double-ownership during membership-view skew right
  after a scale event. Leases give that mutual exclusion for free via Valkey atomicity;
  revisit (c) only if lease-renewal traffic itself becomes the bottleneck.
- **Rebalance bound:** worst case a moved partition sits unclaimed for `LEASE_TTL_MS`
  after its old owner dies ungracefully; a graceful scale-down releases immediately.
  Reconciliation tick (`10s`) + TTL (`30s`) bounds total reassignment latency to ~40s.

**RO-DB watermark across all tenants.** §Watermark in `bundle_active_set/src/query.rs`
already scopes to one `(tenant_id, community_id)` pair — that part is *not* the alpha
bug. The bug is cardinality: a replica now touches however many `(tenant, community)`
pairs its owned partitions' sources belong to (could be hundreds), and polling each
pair's watermark separately every 5s doesn't scale, while a single hash over the *entire*
global active set (naively "fix" this by going coarser) would be worse — any tenant's
change anywhere would force every replica to re-read its whole scope, and at 20,000
communities that recomputation is neither cheap nor localized to what actually moved.

**Decision: an additive change-log/version counter, not a coarser hash.**

```sql
-- new, additive — no existing table's shape changes
CREATE TABLE bundle_active_set_changes (
  seq          BIGSERIAL PRIMARY KEY,     -- global monotonic order
  tenant_id    INT NOT NULL,
  community_id INT NOT NULL,
  changed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- trigger on app_active_versions and app_source_bindings INSERT/UPDATE/DELETE
-- appends one row per (tenant_id, community_id) touched, not per changed column
```

Each replica keeps `last_seen_seq` (in-memory, resets to `0` on restart — a cold replica
re-reads everything it owns once, which is correct and bounded by its own partition
size, not the fleet's). Every poll tick: one query,
`SELECT DISTINCT tenant_id, community_id FROM bundle_active_set_changes WHERE seq >
$last_seen_seq ORDER BY seq`, cost proportional to **changes since last tick**, not to
total tenants/communities/rows. Intersect that list against the `(tenant, community)`
pairs reachable from the replica's currently-owned partitions, and run the existing
`read_active_set` (unchanged) only for those. A pair outside the changed set is never
re-read — this is what makes per-tenant polling affordable at 500×20,000 scale instead
of the existing per-scope query becoming either O(tenants) round trips or one dangerously
coarse global fingerprint.

## 2. Consumer model: one group per source, in-process fan-out

**Problem, with the math.** Today `source_supervisor.rs` spawns one task — one
`GroupReader`, one dedicated blocking Valkey connection (§5.7 of the base spec forbids
sharing that connection) — per `(app_id, platform, source_id)` binding, group name =
`app_id`. Illustrative math at target scale: 20,000 communities × ~5 installed apps ×
~1.5 sources/community ≈ **150,000 consumer groups and blocking connections**
fleet-wide. That number grows with every install, forever, and is already well past
what any Valkey deployment should be asked to hold in blocking clients.

**Decision: one consumer group per source stream (fixed group name, e.g. `stage`), one
blocking `XREADGROUP` per owned stream, in-process fan-out to every locally-subscribed
app's bounded queue.**

- Groups/connections fleet-wide ≈ **10,000** (one per configured source), independent
  of install count — adding the 10,000th bundle to an existing source costs zero new
  groups or connections, only a new fan-out target in memory.
- Read loop: `XREADGROUP GROUP stage {consumer_id} ... STREAMS {stream} >`, owned
  exclusively by whichever replica's lease covers that source's partition (§1) — this
  preserves the existing per-stream FIFO guarantee unchanged, since exactly one reader
  ever holds the group's PEL for that stream at a time.
- Fan-out: for each entry, evaluate every locally-subscribed app's `consumes` filter
  (unchanged, §5.3 of the base spec), then dispatch concurrently (not serially) to each
  matching app's bounded `mpsc` queue (default depth `256`, `PROCESS_APP_QUEUE_DEPTH`).
  A full queue is a **per-app** backpressure event — `waddles_fanout_queue_full_total
  {app_id}` +1, that app's copy is dropped straight to its own DLQ, other apps on the
  same entry are unaffected.
- **Ack semantics.** Since the PEL is per-*entry*, not per-*(entry, app)*, the shared
  group acks once every locally-subscribed app has reached a terminal state for that
  entry (success, per-app DLQ, or filter-skip) — dispatched **concurrently**, so wait
  time is `max(latencies)`, not `sum(latencies)`, bounded by `EXECUTOR_CALL_TIMEOUT_MS`
  (2000ms default, 10000ms hard ceiling, §7.3 base spec). A permanently slow/broken app
  cannot stall the entry indefinitely: the existing three-strike trip/disable (§7.5 base
  spec) routes its future entries straight to its own DLQ without waiting, removing it
  from the ack-blocking set within its `EXECUTOR_TRIP_WINDOW_S` (300s).
- **Isolation preserved, DLQ per app.** Re-delivery via `XAUTOCLAIM` after a crash
  re-fans-out to every locally-subscribed app again, including ones that already
  succeeded — this is safe *because* the base spec already requires bundle idempotency
  keyed on `message-id` (§5.4); a shared group does not create a new correctness
  requirement, it exercises an existing one harder.
- **Ordering vs. throughput.** Per-source FIFO is unchanged (single owning reader).
  Throughput scales by adding partitions/replicas across *sources*, not within one hot
  source — a single very active source is still bottlenecked by one replica's
  read+fan-out loop for that stream specifically, same ceiling the alpha design had.

## 3. Bundle lifecycle at scale

**Problem.** `bundle_loader.rs::run_tick` loads every row the active-set query returns
for its scope — at 30,000 installs across all tenants (even after §1's partitioning
narrows "its scope" to one replica's touched tenants/communities), a busy partition can
still span hundreds of communities, and "every active bundle for my partition" is not
automatically small. Separately, cold compiles are real: the spec's own measurement is
21.6MB components, ~3.3–4.5s **uncached** vs. ~4.5–5.4ms from a precompiled `.cwasm`
(§7.2 base spec) — a bulk-approve of one bundle version across hundreds of communities at
once is a thundering herd if every replica independently fetches and (worse) recompiles.

**Decision: lazy load + LRU by digest, on top of §1's partition scoping — two
independent levers, not one.**

- Partition scoping (§1) bounds the *candidate* set a replica could ever need.
- LRU-by-digest bounds what's actually *resident*: track last-invoked time per digest;
  evict least-recently-invoked when `EXECUTOR_BUNDLE_CACHE_BUDGET_MB` (per-replica memory
  budget, not entry count — components vary in size) is exceeded. A cache miss triggers
  fetch-on-demand rather than eagerly loading the whole scope up front.
- **Digest dedup makes this affordable.** 30,000 installs, low-thousands of distinct
  digests (§0) — a replica's actually-hot working set at any moment is bounded by how
  many *distinct* digests its owned communities use concurrently, typically far fewer
  than its install count, since most communities run the same first-party catalog
  versions. Size the cache (illustrative: 200–300 resident digests, ~30–50MB each
  including `EXECUTOR_INSTANCES_PER_BUNDLE` warm stores) against that, not against total
  installs.
- **Cold-start latency budget.** Because `bundle_compiler` (§4.6 base spec) precompiles
  the `.cwasm` once at **publish** time and stores it keyed by `{digest}-{wasmtime_abi}-
  {collector}`, a cache miss on any replica is an object-store `GET` (~21MB, target
  <300ms in-cluster) + ~5–10ms deserialize — **not** a 3–4s Cranelift compile. That path
  only triggers on a genuine cache-key mismatch (wasmtime/collector upgrade), which
  should be rare and is exactly the case the base spec already treats as "discard and
  recompile."
- **Thundering herd on bulk-approve.** Precompilation already happening once at publish
  time removes the *compile* herd. What remains is N replicas' first `GET` for the same
  digest landing at once: mitigate with **single-flight dedup per digest** (one in-flight
  fetch per digest per replica-local cache; concurrent waiters join it rather than each
  issuing their own `GET`) — the natural place for this is the cache component in the
  next bullet. Proactive pre-warm (hub-api pushing a load hint ahead of the poll tick on
  bulk activation) is a worthwhile follow-up, not required for correctness.
- **Fetch decoupled from the executor (this is a boundary change, not just a cache).**
  Today `ComponentSource::fetch` (`bundle_executor/src/invoke.rs`) runs inside the
  executor's `on_load`, and the executor's network policy already allows it a bucket
  `GET` (§7.1 base spec). Decision: move fetch, digest verification, single-flight dedup,
  and the LRU/precompiled-artifact cache into a **separate local component** — a sidecar
  in the executor's pod, sharing a read-only-to-the-executor cache volume — so the
  executor process itself never holds bucket credentials or bucket network egress at
  all; its `on_load` becomes "read a local path the sidecar already verified," full stop.
  - *Rejected alternative:* embed component bytes directly in the `Load` wire frame.
    Frame size is capped at `EXECUTOR_MAX_FRAME_BYTES` (1MiB default) versus a ~21MB
    component — would require raising that ceiling fleet-wide for every frame type to
    accommodate a rare cold-load path, not worth the DoS-surface increase.
  - *Rejected alternative:* keep fetch in the executor as-is. Works, but every replica
    independently fetches and independently owns bucket egress, which is both a larger
    attack surface for an escaped guest (§7.1's "executor → bucket ALLOW" stays live even
    though the executor never needs to *initiate* that call itself) and a missed chance
    to dedup fetches across the executor's own instance pool.
  - Net effect: tighter network policy (executor pod: stage mTLS only, zero bucket
    egress; sidecar: bucket GET only, zero stage access) *and* the scale fix, from one
    change.
  - Trust: the sidecar verifies `sha256(bytes) == digest` before ever writing to the
    shared cache path (fail-closed, unchanged verification rule from `invoke.rs::
    verify_digest`); the executor is not required to re-verify since it has no network
    path to have been handed anything else, but re-checks anyway as defense-in-depth at
    negligible cost (one hash over bytes already in memory).

## 4. svc_action under the same model

**Problem.** `svc_action`'s distribution poll (`distribution.rs::run_poll_loop`) is
configured with one `action_app_id` — one Deployment per app bundle. At 10,000s of
active bundles that's 10,000s of Deployments, which is not a Kubernetes topology anyone
should run.

**Decision:** apply §1's partition-lease model to `svc_action`, partitioned by
`app_id` (action streams are already `waddles:t:{tenant}:c:{community}:app:{app_id}:
action` with exactly one group, `{app_id}` — §5.9 base spec) instead of by source. No
fan-out complexity here: action is already 1:1 (stream, group, app), so §2's shared-group
machinery isn't needed — only §1's ownership/rebalancing and §3's lazy-load/LRU bundle
lifecycle carry over unchanged. A shared `svc-action` replica pool claims a shard of
`app_id`s via the same Valkey leases, each owned `app_id`'s action stream read by
whichever replica holds its partition, same rebalance bound (~40s).

## 5. Capacity table, HPA signals, failure modes

| Signal | Metric | Alpha shape | New shape (worked example) |
|---|---|---|---|
| Deployments (process+action) | k8s objects | ~500 tenants × 2 = 1,000+, growing with tenants | fixed replica pool, independent of tenant count |
| Consumer groups / blocking conns | `waddles_group_pending`, conn count | ~150,000 (§2 math), grows with installs | ~10,000 (= source count), flat vs. installs |
| Resident bundle digests/replica | `waddles_bundle_cache_*` | every active bundle in scope (unbounded growth) | ~200–300, budget-capped, LRU-evicted |
| Watermark poll cost/tick | DB rows touched | O(tenant's whole active set) × replicas | O(changes since last tick) (§1 changelog) |
| Partition rebalance latency | `waddles_partition_reassignments_total` | N/A (static scope) | ≤ ~40s (renew 10s + TTL 30s) |

**HPA signals:** scale svc-process/svc-action on `avg(waddles_partition_lag)` per
replica (sum of `XPENDING`+backlog across owned streams, target e.g. 500 sustained 2min)
— not on CPU alone, since a replica can be CPU-idle while its owned partition backs up.
Track `waddles_owned_partitions{replica}` to catch hashing/lease skew, and
`waddles_bundle_cache_hit_ratio` to catch an undersized cache budget before it shows up
as latency.

**Failure modes:**

| Failure | Behavior |
|---|---|
| Valkey lease store unreachable | Replicas keep serving currently-owned partitions (no self-eviction); cannot claim freed ones. Degrades to static ownership until recovery — never drops in-flight work. |
| RO-DB changelog query fails | Fall back to last-known active set, never crash, never speculatively unload (existing flag/license graceful-degradation pattern). |
| Fetch/cache sidecar down | Executor `on_load` fails closed (`LOAD_FAILED`); already-resident bundles keep serving until evicted; new loads retry with backoff. |
| Partition flapping | `waddles_partition_reassignments_total` rate alert; usually undersized lease TTL vs. GC pause/network jitter — runbook: raise TTL. |

## 6. Migration path (each increment independently shippable, flag-gated, opt-out default OFF)

1. **Additive changelog table + trigger** (§1) — schema-only, inert until read; no
   runtime behavior change.
2. **Multi-tenant watermark polling** — replace the `BUNDLE_SCOPE_TENANT_ID` single-scope
   gate with changelog-driven polling across a configured tenant set. Flag:
   `PROCESS_MULTI_TENANT_WATERMARK_ENABLED` (default off; off = today's single-tenant
   env-scoped behavior, unchanged).
3. **Shared source-stream consumer groups + in-process fan-out** (§2), replacing
   per-(app,source) groups. Flag: `PROCESS_SHARED_SOURCE_GROUPS_ENABLED` (default off).
4. **Partition ownership via Valkey leases** (§1) across source streams; wire HPA to
   `waddles_partition_lag`. Requires #3 (nothing to own until groups are shared). Flag:
   `PROCESS_PARTITION_LEASES_ENABLED` (default off).
5. **Bundle fetch/cache sidecar** (§3) — decouple fetch out of `bundle_executor`,
   tighten the executor pod's network policy. Flag: `EXECUTOR_LOCAL_CACHE_ENABLED`
   (default off; off = today's direct in-executor fetch path).
6. **Lazy load + LRU eviction** in the DB bundle loader, built on #5's cache. Flag:
   `PROCESS_LAZY_BUNDLE_LOAD_ENABLED` (default off).
7. **svc_action multi-app partitioning** (§4), same lease model over `app_id`. Flag:
   `ACTION_MULTI_APP_ENABLED` (default off).
8. **Decommission the single-tenant/single-app topology** — remove
   `BUNDLE_SCOPE_TENANT_ID`/`action_app_id` single-scope code paths and hub-api's
   per-(app,source) group provisioning, once 1–7 have soaked green at representative load
   (beta/gamma load test approximating §0's sizing table). This step is a deletion after
   a full soak, not itself a kill-switched feature.
