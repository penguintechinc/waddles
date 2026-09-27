# Waddles v3 Rust Data Plane — Multi-Tenant Scale Design

Amends `2026-09-14-rust-data-plane-design.md` (§4.2/4.3/4.5, §5, §7, §9). That spec is
correct for one tenant per deployment; this document removes that ceiling. It does not
change the wire protocol, envelope contracts, WIT world, or trust boundaries — only
*how many* of each thing a replica owns and *when* it loads a bundle.

**Revision note.** This revision replaces rev 1's completion-gated source ack (head-of-
line blocking risk) with a durable per-app hand-off, adds lease fencing and rebalance-
storm controls, adds an explicit multi-tenant fairness layer, adds bundle-cache
hit-rate math/pinning/DoS protection/authz, fixes a change-log sequence-gap bug, and
switches the migration flags to PostHog opt-out kill-switches per house rules.

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
tenant per Deployment — 500 tenants would mean 500 Deployments of svc-process and
svc-action each.

**Decision: shared replica pool, hash-partitioned by source stream, ownership via
Valkey lease.**

- Partition key space: fixed `N_PARTITIONS = 2048` virtual partitions (headroom for
  hundreds of replicas without repartitioning the hash space itself).
  `partition(source_id) = rendezvous_hash(source_id) % 2048` — HRW (highest random
  weight) hashing, not `hash % replica_count`, because replica count changes constantly
  under HPA and HRW only reassigns the fraction of keys that must move.
- Ownership: `SET waddles:lease:partition:{n} {replica_id}:{epoch} NX PX {LEASE_TTL_MS}`
  (default TTL `30000`), renewed every `LEASE_RENEW_MS` (default `10000`) with a
  compare-and-renew Lua script.
- **Why Valkey leases over alternatives:** (a) `hash % replica_count` was rejected — it
  doesn't survive scale up/down without a coordinated rehash, and HPA churn would thrash
  it; (b) an external coordinator (Raft group, dedicated scheduler) was rejected —
  unjustified new infra when Valkey already provides atomic `SET NX`/Lua; (c) a
  client-side ring computed from a heartbeat set (no explicit lease) was considered —
  lower renewal traffic, but needs a handoff/fencing protocol to avoid double-ownership
  during membership-view skew right after a scale event; revisit only if lease-renewal
  traffic itself becomes the bottleneck.

**Fencing (epochs).** Valkey has no native fenced-write primitive for arbitrary keys, so
enforcement is layered:
- **Hard CAS at the lease itself.** Acquire mints an epoch via
  `INCR waddles:lease:epoch:{partition}`; the lease value is `{replica_id}:{epoch}`.
  Renewal extends TTL only if the stored value's `(replica_id, epoch)` still matches the
  caller's — a replica that lost and reacquired (or a duplicate replica_id) fails
  renewal, not merely a different replica_id winning.
- **Soft fencing on the client for everything else.** `XACK`/`XADD` have no
  conditional-on-arbitrary-value form, so the owning replica keeps a local `fenced`
  flag, flipped `false` the instant a renewal fails or a claim is lost; every
  partition-scoped write (source `XACK`, work-stream `XADD`, bundle load/unload from
  that partition's diff) checks the flag immediately before issuing the command and is
  skipped if unset. This **bounds, not eliminates**, the double-ownership window to the
  gap between renewal failure and the next write attempt (one poll tick, ≤ a few hundred
  ms).
- **Idempotency is the backstop** for that residual window, exactly as it already is for
  ordinary at-least-once redelivery (§5.4 base spec, message-id keyed) — a duplicate
  write during the fencing gap is a duplicate delivery, not a correctness violation.

**Rebalance storms.** A replica dying frees all its leases at once; every survivor's
next tick would otherwise race to claim the same partitions simultaneously.
- **Jittered ticks** (±50% random) spread claim attempts instead of lockstep.
- **Rate-limited claims** — `MAX_PARTITION_CLAIMS_PER_TICK` (default `10`) caps how many
  new partitions one replica acquires per tick, so a mass failure doesn't fan a single
  survivor's cold-load burst into one instant.
- **Shuffled candidate order** per tick reduces repeated collisions with the same
  competitors.
- **Rate-limited release on graceful scale-down** — a draining replica releases leases
  in the same batch size over a few ticks, not one `DEL` burst.
- **Why this bounds the herd, not just slows it:** a failed `SET NX` is one cheap
  rejected command — no bundle load, fetch, or compute is triggered by losing a race.
  Real work only happens on a *successful* claim, and successful claims are capped at
  `replicas × MAX_PARTITION_CLAIMS_PER_TICK` per tick fleet-wide.

**RO-DB watermark across all tenants.** A per-`(tenant, community)` scoped watermark
already exists (`bundle_active_set/src/query.rs`); the fix at this cardinality is an
additive change-log/version-counter, not a coarser hash — full design, including the
sequence-gap and retention issues, is §7.

## 2. Consumer model: shared source read, durable per-app hand-off, isolated fan-out

**Rev-1 flaw (head-of-line blocking).** Rev 1 acked a shared source entry only once
every locally-subscribed app reached a terminal state. That couples the shared group's
PEL to the *slowest* app on that source: a poison/slow app inflates PEL for every other
app sharing it, forces `XAUTOCLAIM` to re-deliver the whole entry (re-processing apps
that already succeeded), and — because the executor's concurrency ceiling is
fleet-shared — can degrade throughput for tenants unrelated to the stuck app. **Ack must
not wait on downstream processing.**

**Decision: ack the source stream on durable hand-off, not on completion — insert a
per-app work stream with its own group/PEL, mirroring the process→action pattern the
base spec already uses (§5.9).**

```
source stream (group="stage", shared)
   read → filter → XADD to each matched app's work stream (awaited, fast) → XACK source
                                     │
                                     ▼
waddles:t:{tenant}:c:{community}:app:{app_id}:work   (stream, one group: {app_id})
   read → invoke → XACK work entry (per app, independent PEL)  →  action stream (§5.9, unchanged)
```

- The shared source reader's only job per entry: evaluate filters, `XADD` the matching
  envelope (same `message-id`, unmodified) to every matched app's `:work` stream, then
  `XACK` the source entry. All three sub-steps are fast, bounded-latency Valkey writes —
  no invoke, no wait on any app's outcome. Source PEL depth now reflects only
  write-availability, never app health.
- Each app's `:work` stream has its **own** group (`{app_id}`) and PEL, independent of
  every other app sharing the source. A slow/poison app inflates only its own PEL, trips
  only its own three-strike disable (§7.5 base spec), and DLQs only its own entries.
- **Ordering, stated explicitly.** One source's entries are read by exactly one reader
  (the partition owner) in stream order and `XADD`ed to a given app's work stream in
  that order by that single-threaded reader — per-`(source, app)` FIFO holds.
  Cross-source ordering into one app's work stream is still not guaranteed (unchanged
  from the base spec, inherent to fan-in from independent readers).
- **At-least-once across the new hop.** A crash between the work-stream `XADD`(s)
  succeeding and the source `XACK` re-processes the source entry, re-`XADD`ing to the
  same app(s) — a duplicate work-stream entry, caught by the existing message-id
  idempotency contract (§5.4 base spec), not a new failure mode.

**Capacity math holds at target scale.** A naive "one stream per install" reading would
be ~30,000 streams+groups — worse than groups alone suggests, since Valkey groups are
cheap metadata. The scarce resource was never group count, it was **dedicated blocking
connections** (§5.7 base spec forbids sharing one). `XREADGROUP` accepts multiple stream
keys in one blocking call: a replica's work-stream readers multiplex up to
`WORK_QUEUE_BATCH_SIZE` (default `500`) locally-owned work streams per blocking
connection, each still keeping its own independent group/PEL.

| Layer | Streams/groups | Blocking connections |
|---|---|---|
| Source stage (shared group) | ~10,000 | ~10,000 (one per active source, spread across replicas) |
| Per-app work queue | ~30,000 (= install count) | ≈ `replicas × workers/replica` (e.g. `50 × 4 = 200`) via multiplexed `XREADGROUP`, **not** one per install |

Connections stay flat against install growth on both hops; only work-stream *group*
count grows with installs, and that is metadata, not the scarce resource.

## 3. Bundle lifecycle at scale

**Problem.** Loading every active row for a replica's scope, and cold Cranelift
compiles, don't survive a bulk-approve across hundreds of communities.

**Decision: lazy load + LRU by digest, on top of §1's partition scoping.**

- Partition scoping bounds the *candidate* set; LRU-by-digest bounds what's actually
  *resident*: evict least-recently-invoked when `EXECUTOR_BUNDLE_CACHE_BUDGET_MB` is
  exceeded; a miss fetches on demand.
- **Working-set / hit-rate math.** At illustrative `replicas=50`, one replica owns
  `2048/50 ≈ 40` partitions × ~5 sources/partition ≈ 200 sources, spanning maybe
  100–150 distinct communities. At ~5 apps/community but heavy digest reuse across the
  shared first-party catalog (§0), the *distinct-digest* working set is bounded by
  catalog size long before `communities × apps` — illustrative ~100–150 digests hot at
  once. Size the LRU to ~1.5–2× that (`250` entries) to absorb rotation; target **≥95%
  steady-state hit rate**, alerted via `waddles_bundle_cache_hit_ratio`.
- **Admission/pinning.** A burst of cold long-tail digests must not evict the shared
  first-party catalog every replica needs constantly. Track invocation frequency per
  digest and pin the fleet-wide top-`K` (default `50`) from eviction; pins recompute on
  a slow cadence (`15m`) and never block admission of a new digest, only protect
  already-hot ones from a transient spike.
- **Cold-start latency budget.** `bundle_compiler` (§4.6 base spec) precompiles the
  `.cwasm` once at **publish** time, keyed `{digest}-{wasmtime_abi}-{collector}` — a
  cache miss on any replica is an object-store `GET` (~21MB, target <300ms in-cluster) +
  ~5–10ms deserialize, **not** a 3–4s Cranelift compile (§7.2 base spec measurement).
  That path only triggers on a genuine cache-key mismatch, which should be rare.
- **Cold-start DoS protection.** Single-flight dedup covers concurrent fetches of the
  *same* digest; it does not stop one tenant triggering many *distinct* cold loads
  (rapid activate/deactivate, or a burst of unique custom bundles) and starving the
  fetch path or evicting other tenants' pinned entries. Add a **per-tenant cold-load
  rate limit** (`TENANT_BUNDLE_LOAD_RATE_LIMIT`, default `5/s`, token bucket) in the
  cache component; over-limit loads queue briefly, then fail retryable.
- **Fetch decoupled from the executor.** Move fetch, digest verification, single-flight
  dedup, and the LRU/precompiled-artifact cache into a **sidecar** in the executor's
  pod, sharing a read-only-to-the-executor cache volume — the executor process holds no
  bucket credentials or bucket egress at all; `on_load` becomes "read a local path the
  sidecar already verified."
  - *Rejected:* embedding component bytes in the `Load` wire frame — `EXECUTOR_MAX_
    FRAME_BYTES` (1MiB) versus ~21MB components, not worth raising for a rare cold path.
  - *Rejected:* status quo (fetch inside the executor) — every replica independently
    owns bucket egress (larger attack surface for an escaped guest) and independently
    fetches with no cross-instance dedup.
  - Net effect: tighter network policy (executor: stage mTLS only; sidecar: bucket GET
    only) *and* the scale fix, from one change.
- **Per-tenant authz in the fetch path.** The sidecar fetches only digests present in
  the active set of a `(tenant, community)` the calling replica currently owns
  (cross-checked against the replica's own partition-scoped active-set cache, refreshed
  via §7) — belt-and-suspenders against a crafted or buggy `Load` frame naming a digest
  outside the caller's authority, consistent with "the stage never trusts a
  cross-boundary reference alone" (§5.9 base spec).

## 4. svc_action under the same model

**Problem.** One `action_app_id` per Deployment (`distribution.rs::run_poll_loop`) means
10,000s of Deployments at target bundle-install scale.

**Decision:** apply §1's partition-lease model (including fencing and jittered
rebalance, unchanged) to `svc_action`, partitioned by `app_id` — action streams are
already 1:1 `(stream, group={app_id}, app)` (§5.9 base spec), so §2's durable-hand-off
machinery isn't needed there; only §1's ownership/rebalancing, §3's lazy-load/LRU, and
§5's tenant fairness carry over.

## 5. Multi-tenant fairness & isolation (every pod is multi-tenant)

Partitioning (§1) isolates *ownership*, and the per-app work queue (§2) isolates a
poison app's *blast radius* — neither stops one tenant's aggregate volume from
starving another tenant sharing the same replica's shared resources (executor
concurrency ceiling, work-queue reader threads, fan-out CPU).

| Mechanism | Where | Default |
|---|---|---|
| Per-tenant event-rate limit | Work-queue consumer, before dispatch | `TENANT_RATE_LIMIT_EPS` token bucket, illustrative `200/s` |
| Weighted fair dispatch | Multiplexed `XREADGROUP` result loop (§2) | Round-robin **across tenants first, then apps within a tenant** — one entry per tenant per pass, never drain-one-stream-then-next; equal weight by default, override for contractual SLAs |
| Per-tenant executor concurrency sub-ceiling | Instance-pool checkout (§7.2 base spec) | `EXECUTOR_MAX_CONCURRENT_CALLS_PER_TENANT` ≤ global ceiling (e.g. `8` of `32`) — no tenant claims the whole pool |
| Per-tenant aggregate trip | Extends the per-`(app_id, digest)` trip (§7.5 base spec) | A tenant whose apps collectively trip above `TENANT_TRIP_RATE` gets a lower rate limit, never a full tenant-wide disable — backpressure, not an outage |
| Per-tenant metrics | OTel | `waddles_tenant_events_processed_total{tenant}`, `waddles_tenant_queue_depth{tenant}`, `waddles_tenant_executor_concurrency{tenant}`, `waddles_tenant_rate_limited_total{tenant}` — tenant cardinality (100s) is within budget |

Isolation is layered, cheapest-first: per-app work queue (structural) → per-tenant rate
limit (rejects before any resource is spent) → weighted dispatch (fairness among
admitted work) → per-tenant concurrency sub-ceiling (hard cap in the executor, the last
line before one tenant's compute reaches another's).

## 6. Capacity table, HPA signals, failure modes

| Signal | Metric | Alpha shape | New shape (worked example) |
|---|---|---|---|
| Deployments (process+action) | k8s objects | ~500 tenants × 2 = 1,000+, growing with tenants | fixed replica pool, independent of tenant count |
| Source-stream groups/conns | `waddles_group_pending`, conn count | ~150,000 (per-(app,source) groups) | ~10,000 (= source count), flat vs. installs |
| Work-queue groups/conns | conn count | N/A | ~30,000 groups / ~200 blocking conns (§2) |
| Resident bundle digests/replica | `waddles_bundle_cache_*` | every active bundle in scope (unbounded) | ~100–150 hot, `250`-entry LRU budget |
| Watermark poll cost/tick | DB rows touched | O(tenant's whole active set) × replicas | O(changes since last tick, delayed-cutoff safe, §7) |
| Partition rebalance latency | `waddles_partition_reassignments_total` | N/A (static scope) | ≤ ~40s, jittered + rate-limited (§1) |
| Fencing gap window | — | N/A | ≤ one poll tick after renewal failure |

**HPA signals:** scale on **partition-level** source-stream lag and **tenant-level**
aggregate work-queue lag (excluding tripped/disabled apps) — never on a single app's
queue depth, so one poison app cannot trigger fleet-wide scale-out; that case is handled
by the app's own trip/disable (§7.5 base spec), not by adding replicas. Track
`waddles_owned_partitions{replica}` for hashing/lease skew and
`waddles_bundle_cache_hit_ratio` for an undersized cache budget.

**Failure modes:**

| Failure | Behavior |
|---|---|
| Valkey lease store unreachable | Replicas keep serving currently-owned partitions (no self-eviction); cannot claim freed ones. Degrades to static ownership until recovery. |
| Lease renewal fails (fencing trip) | Local `fenced` flag flips false immediately; in-flight writes for that partition stop; partition becomes claimable within the reconcile+TTL bound (~40s) — the old holder never double-acts past its next attempted write. |
| Mass replica loss (rebalance storm) | Jittered, rate-limited claims (§1) bound cold-load fan-in to `MAX_PARTITION_CLAIMS_PER_TICK × replicas` per tick — no synchronized cache-eviction/compile spike. |
| RO-DB change-log query fails | Fall back to last-known active set, never crash, never speculatively unload. |
| Fetch/cache sidecar down | Executor `on_load` fails closed (`LOAD_FAILED`); resident bundles keep serving until evicted; new loads retry with backoff. |
| One tenant over its rate limit | Excess events rejected with a retryable backpressure signal at the work-queue consumer (§5); other tenants on the same replica are unaffected. |

## 7. Change-log correctness, retention, and full-reconcile safety net

**Sequence-gap / in-flight-transaction visibility.** A `BIGSERIAL seq` can be allocated
by a transaction that commits *after* a later-`seq` transaction commits. A replica that
polls `WHERE seq > last_seen_seq` and advances `last_seen_seq` to the max `seq` observed
would permanently skip the earlier, late-committing row. **Decision: advance the
watermark on a delayed, commit-safe cutoff, not the raw max observed** — poll
`WHERE seq > last_seen_seq AND changed_at <= now() - CHANGELOG_SAFETY_MARGIN`
(default `5s`, comfortably longer than any realistic transaction on this table), and
only advance `last_seen_seq` up to that cutoff. Cost: up to `5s` extra propagation
latency on a poll that was already background/eventual, never on the request path.
*Rejected:* reading Postgres's `xmin`/snapshot horizon directly for an exact safe
sequence — more precise, materially more coupling to Postgres internals; revisit only
if `5s` proves operationally too slow.

**Retention.** `bundle_active_set_changes` grows unboundedly otherwise. A retention job
(hub-api cron) deletes rows older than `CHANGELOG_RETENTION` (default `48h`) — safe
unconditionally past that horizon because any replica down longer than `48h` is already
past the point where full reconcile, not changelog replay, is the correct recovery.

**Periodic full reconcile — the safety net for both of the above.** Independent of
change-log correctness, every replica re-runs `read_active_set` in full for every
`(tenant, community)` its owned partitions touch on a slow cadence
(`FULL_RECONCILE_INTERVAL`, default `15m`), and immediately upon acquiring a new
partition. This bounds the blast radius of any change-log gap, trigger bug, or missed
row to at most one reconcile interval — the same trust-but-verify pattern the rest of
this design relies on, and cheap here because a partition's touched-scope row count is
small by construction (§1).

## 8. Migration path

**Flag mechanism (house rule).** Every increment ships behind a PostHog **opt-out
kill-switch**, `waddles.core.disable-<mechanism>` — not an env-var default-off flag.
Unseen or unreachable resolves to **not disabled** (new mechanism stays ON — fail-safe,
not fail-open); flipping it ON is the operator's rollback to the legacy path it
replaces. This still matches the house default ("never-seen flags default OFF"): these
are *disable* switches, so OFF-by-default is exactly "new path active by default."

1. **Additive change-log table + trigger, delayed-cutoff read** (§7) — schema-only,
   inert until read, no flag.
2. **Multi-tenant watermark polling** (§1, §7). `waddles.core.disable-multi-tenant-watermark`.
3. **Shared source-stream read + durable per-app work-queue hand-off** (§2), replacing
   both the alpha per-(app,source) groups and the completion-gated ack this revision
   removed. `waddles.core.disable-shared-source-fanout`.
4. **Partition ownership via fenced Valkey leases, jittered/rate-limited rebalance**
   (§1). Requires #3. `waddles.core.disable-partition-leases`.
5. **Bundle fetch/cache sidecar, with per-tenant fetch authz** (§3).
   `waddles.core.disable-bundle-cache-sidecar`.
6. **Lazy load + LRU with hot-digest pinning and per-tenant cold-load rate limits**
   (§3), built on #5. `waddles.core.disable-lazy-bundle-load`.
7. **svc_action multi-app partitioning** (§4), same lease/fencing model over `app_id`.
   `waddles.core.disable-action-multi-app`.
8. **Multi-tenant fairness controls** (§5) — per-tenant rate limits, weighted dispatch,
   executor concurrency sub-ceiling, per-tenant metrics; layers onto #3/#4/#7 and can
   ship independently once those land. `waddles.core.disable-tenant-fairness`.
9. **Decommission the legacy topology** — delete `BUNDLE_SCOPE_TENANT_ID`/`action_app_id`
   single-scope paths, hub-api's per-(app,source) group provisioning, and the
   completion-gated ack path, once 1–8 have soaked green at representative load
   (beta/gamma load test approximating §0's sizing table). Deletes the kill switches for
   2–8 rather than leaving them permanently dark.
