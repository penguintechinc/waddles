# Waddles v3 Rust Data Plane — Multi-Tenant Scale Design

Amends `2026-09-14-rust-data-plane-design.md` (§4.1/4.2/4.3/4.5, §5, §7, §9). That spec
is correct for one tenant per deployment; this document removes that ceiling. It does
not change the envelope contracts, WIT world, or trust boundaries, except where noted
in §5 (sidecar) below.

**Revision note (rev 3, addresses rejected review).** Replaces soft client-side
fencing with hard self-eviction + server-side Lua-enforced epoch fencing (§1); replaces
the change-log time-margin with exact xid-visibility (§7); restructures ingress from
~10,000 per-source streams to `N=2048` fixed partition streams, re-deriving all
connection/memory/CPU math (§2, §6); adds load-aware rebalancing and hot-source
isolation (§1); makes the bundle-cache sidecar an independent trust boundary with its
own stage-fed channel, never trusting the executor's claims (§3); adds age-based unpin,
survivor-set tracking, and a hard byte cap to pinning (§3).

## 0. Sizing targets (unchanged)

| Dimension | Target |
|---|---|
| Tenants | 100s (worked examples use 500) |
| Communities | 10,000s (worked examples use 20,000) |
| Ingest sources (channels), all platforms | ~10,000 |
| Active bundle installs | 10,000s (worked examples use 30,000) |
| Distinct digests | low thousands |
| Users | 1000s |

## 1. Multi-tenant replicas + partitioning

**Decision (unchanged from rev 2): `N_PARTITIONS = 2048` fixed virtual partitions,
`partition(source_id) = rendezvous_hash(source_id) % 2048`, ownership via Valkey lease.**
Rev 3 makes this partition space the *literal transport unit* too — see §2. Ownership:
`SET waddles:lease:partition:{n} {replica_id}:{epoch} NX PX {LEASE_TTL_MS}` (default
`30000`), renewed every `LEASE_RENEW_MS` (default `10000`).

**Fencing — replaced (rev 2's soft client flag was insufficient).** A GC pause or
network partition can leave a replica's local "am I fenced" state stale for longer than
the lease TTL: the renewal loop simply never runs during the pause, so a boolean that
only flips *on renewal failure* never flips at all, and the replica can resume issuing
writes as if nothing happened while another replica has already taken the lease. Two
independent, both-required fixes:

1. **Hard self-eviction, monotonic-clock deadline.** Track
   `deadline = last_successful_renewal_instant + LEASE_TTL_MS - SAFETY_MARGIN_MS`
   (`SAFETY_MARGIN_MS` default `5000`) using `Instant` (monotonic), never wall-clock.
   Before *any* partition-scoped operation — not just on a renewal failure callback —
   check `Instant::now() < deadline`; if not, treat the partition as lost immediately:
   stop consuming and stop attempting writes, without waiting for an explicit renewal
   response. This closes the GC-pause hole because the deadline is computed from elapsed
   time, not from whether a renewal attempt happened to run.
2. **Server-side fencing, atomic Lua script.** Every write for a partition — the
   fenced hand-off-and-ack described in §2 — goes through one Lua script that reads
   `waddles:lease:epoch:{partition}`, compares it to the caller's `expected_epoch`
   atomically, and performs the writes only on a match:
   ```
   EVAL fenced_commit 0 epoch_key expected_epoch source_stream group entry_id
        work_stream_1 fields_1 [work_stream_2 fields_2 ...]
     if redis.call('GET', epoch_key) ~= expected_epoch then return {err='STALE_EPOCH'} end
     for each (work_stream, fields): redis.call('XADD', work_stream, '*', fields)
     redis.call('XACK', source_stream, group, entry_id)
     return 'OK'
   ```
   A new claimant bumps the epoch (`INCR waddles:lease:epoch:{partition}`) on acquire;
   the instant it does, every subsequent call from the old holder is rejected
   server-side, regardless of the old holder's local clock/flag state.

**Residual window: ~0, and why.** With (1) the old holder stops *attempting* writes
within `SAFETY_MARGIN_MS` of its last successful renewal on its own; with (2) any write
it does attempt after epoch bump is rejected atomically by Valkey — there is no ordering
in which a stale-epoch write partially succeeds. The only remaining ambiguity is which
of two commands Valkey happens to sequence first at the exact instant of handoff, which
is not a hazard (either ordering is a valid linearization, since the old holder was
still the legitimate owner up to that instant). **Idempotency remains the backstop**
(§5.4 base spec, message-id keyed) for the case where a `STALE_EPOCH` rejection causes a
source entry to be retried by the new owner — a duplicate delivery, not a correctness
violation, exactly as at-least-once redelivery already requires bundles to tolerate.

**Rebalance storms (unchanged from rev 2).** Jittered ticks (±50%), rate-limited claims
(`MAX_PARTITION_CLAIMS_PER_TICK`, default `10`), shuffled candidate order, rate-limited
release on graceful drain. A failed `SET NX` is one cheap rejected command; real work
(cold load, cache admission) only follows a *successful* claim, capped at
`replicas × MAX_PARTITION_CLAIMS_PER_TICK` per tick fleet-wide.

**Partition skew — load-aware rebalancing (new).** Fixed-count partitioning bounds
*cardinality*, not *load*: several above-average sources can collide into one
partition by chance. Each owning replica publishes measured throughput per owned
partition to `waddles:partition:stats:{n}` (a small hash, updated on its own metrics
tick). Two mechanisms on top of §1's reactive (failure-driven) rebalancing:
- **Voluntary shed.** On a slow cadence (`LOAD_REBALANCE_INTERVAL`, default `5m`), a
  replica compares its total owned load to the fleet average (visible in the shared
  stats hash) and releases its single highest-load partition if it exceeds
  `150%` of average — rate-limited to one release per cycle, same philosophy as §1's
  claim cap, so shedding never itself causes a storm.
- **Hot-source isolation (optional, M-later).** A source whose own throughput
  (`waddles_source_events_total{source_id}`, unchanged cardinality-bounded metric)
  exceeds `HOT_SOURCE_SPLIT_THRESHOLD` can be pulled out of the normal hash space into
  one of `M=64` reserved overflow partitions, recorded in a small
  `hot_source_overrides(source_id, partition_id)` table both ingest and readers
  consult before falling back to the hash — so one very hot source stops sharing
  transport capacity with unrelated co-located sources. Not required for the base
  migration; add when `waddles_partition_lag` shows a partition consistently >5× the
  fleet median despite voluntary shedding.

**RO-DB watermark across all tenants.** Unchanged framing — the fix is an additive
change-log, now with exact xid-based visibility instead of a time margin; full design
in §7.

## 2. Consumer model: partition streams, atomic fenced hand-off, isolated fan-out

**Rev-1/2 flaw carried forward into this revision's fix.** Per-source streams (rev 1/2)
meant ~10,000 streams and — even after rev 2's multiplexed-work-queue trick — ~10,000
dedicated blocking reads at the source layer, unbounded against source-count growth.
Separately, rev 2's two-round-trip hand-off (await `XADD`, then `XACK`) left a small gap
a crash could exploit (closed by idempotency, but avoidable).

**Decision: svc_ingest writes into `N_PARTITIONS = 2048` fixed streams, not one per
source — the partition space §1 already defined for *ownership* becomes the literal
*transport* unit.**

```
svc_ingest:  partition = rendezvous_hash(source_id) % 2048   (same pure fn as §1, shared crate)
             XADD waddles:partition:{n}:events MAXLEN ~ {SPINE_PARTITION_STREAM_MAXLEN} * env {envelope_json}

owning replica (leases partition n, §1):
   XREADGROUP GROUP stage {consumer_id} ... STREAMS waddles:partition:{n}:events >
     → evaluate each locally-subscribed app's `consumes` filter against the entry's own
       source_id/platform fields (carried in the envelope regardless of which physical
       stream it arrived on — filtering was never keyed off which stream was read;
       §5.2 base spec already states Valkey structure is not the enforcement boundary)
     → ONE atomic Lua call (§1 fencing): XADD to every matched app's
       waddles:t:{tenant}:c:{community}:app:{app_id}:work stream, then XACK the
       partition-stream entry — hand-off and ack are now a single atomic op, not two
       round trips
```

- **Ordering.** Per-source FIFO holds: all of one source's events hash to the same
  partition and are appended by the same ingest write path in occurrence order; the
  single leaseholder reads that partition in stream order, so one source's subsequence
  is FIFO both on the wire and into any one app's work stream. Cross-source ordering
  within a shared partition, and cross-source ordering into one app's work stream, are
  both *not* guaranteed — unchanged from the base spec's existing "no ordering across
  sources" statement, now made explicit at the transport layer too.
- **No new head-of-line risk from consolidation.** Valkey's PEL/`XACK` operate per
  *entry*, not per co-located source — a slow entry from source A does not block
  acking a different, already-ready entry from source B in the same partition stream;
  the single reader loop processing entries strictly in order is the only place
  cross-source contention could appear, which is exactly what §1's load-aware
  rebalancing and hot-source isolation exist to catch.
- **Grant enforcement unaffected.** Consolidating sources into shared streams removes
  no real security boundary: §5.2 base spec already states per-source Valkey structure
  was never authoritative, only the stage's in-process grant check was. That check is
  unchanged, now evaluated against the envelope's own source fields post-read rather
  than inferred from which per-source stream was read. `app_stream_grants` (§6.8 base
  spec) remains the source of truth for the grant *list*; hub-api stops driving
  per-grant `XGROUP CREATE`/`DESTROY` (moot once the group is per-partition, created
  once at partition bring-up, not per grant).
- **PEL bound / redelivery backoff.** `XAUTOCLAIM` unchanged in mechanism
  (`SPINE_CLAIM_INTERVAL_MS`/`SPINE_CLAIM_IDLE_MS`, `SPINE_MAX_DELIVERIES=5` → DLQ,
  §5.4/§5.5 base spec), with one addition: reclaim wait scales with observed delivery
  count (`idle_threshold = SPINE_CLAIM_IDLE_MS × delivery_count`, linear) so a poison
  entry isn't hot-looped every claim interval on its way to the 5-delivery DLQ cutoff.

**Capacity math, re-derived.**

| Layer | Streams/groups | Blocking connections (illustrative, `replicas=50`) |
|---|---|---|
| Partition stream (ingress) | **2048** (fixed, independent of source count) | multiplexed like the work-queue layer: `PARTITION_STREAM_BATCH_SIZE` (default `64`) partitions per blocking `XREADGROUP` call ⇒ ≈ `replicas × readers/replica` (e.g. `50 × 4 = 200`), not up to 2048 |
| Per-app work queue | ~30,000 (= install count) | ≈ `replicas × workers/replica` (e.g. `50 × 4 = 200`), via `WORK_QUEUE_BATCH_SIZE=500` multiplexing (unchanged from rev 2) |
| **Total, fleet-wide** | ~32,000 groups (metadata, cheap) | **≈ 400 blocking connections** |

Headline: **~180,000 (rev 1) → ~10,200 (rev 2) → ~400 (rev 3)** blocking connections at
target scale, and connection count is now flat against *both* source-count and
install-count growth.

**Memory/CPU consequences of fewer, larger streams.** Consolidating ~5 sources/partition
(10,000/2048) into one stream means the old per-source `SPINE_STREAM_MAXLEN` (`100000`)
no longer gives each source the same retention window — scale it up:
`SPINE_PARTITION_STREAM_MAXLEN` default `500000` (~5×), monitored per-partition trim
rate to catch an imbalanced hot partition early (feeds §1's skew detection). Per-stream
metadata overhead (radix-tree nodes, group/consumer bookkeeping) drops ~5× from fewer,
larger streams. `SPINE_READ_COUNT` (per-call batch) bumped `64 → 256` for partition
streams to drain a busier multiplexed stream efficiently per wake.

## 3. Bundle lifecycle at scale

Lazy load + LRU by digest on top of partitioning (unchanged core decision from rev 2):
working-set math (~100–150 hot digests at `replicas=50`, `250`-entry LRU budget,
≥95% target hit rate), publish-time precompilation (sub-second cold start), and
single-flight dedup are unchanged from rev 2.

**Pinning — hardened (age-based unpin, survivor tracking, byte cap).**
- **Decayed score, not raw count.** Pin score is an exponentially decayed invocation
  frequency (half-life `30m`), so a burst that has since gone quiet naturally falls out
  of ranking rather than staying pinned on stale history.
- **Hard age-based unpin.** A pinned digest with zero invocations for `PIN_MAX_IDLE`
  (default `1h`) is unpinned immediately regardless of its decayed score — a floor under
  the decay curve, not just a slope.
- **Survivor-set tracking, dampens churn.** A digest must appear in the top-`K`
  ranking for `PIN_CONFIRM_CYCLES` (default `2`, at the `15m` recompute cadence) before
  it is actually pinned, avoiding pin-thrash from one noisy cycle; tenure (consecutive
  cycles pinned) is tracked so that if pinned entries must ever be shed under memory
  pressure, newest/non-survivor pins are released before long-tenured ones.
- **Hard byte cap, not just entry count.** `K=50` bounds entry count but not memory —
  component sizes vary (21MB Python vs. much smaller Rust). Add
  `PINNED_CACHE_BUDGET_MB` (default `50%` of `EXECUTOR_BUNDLE_CACHE_BUDGET_MB`), so
  pinning can never crowd out all headroom for ordinary LRU rotation of the long tail.
  Admitting a new pin that would exceed the byte cap evicts the lowest-scoring existing
  pin first; a single digest too large to fit within the remaining pin budget is simply
  not pinned (falls back to ordinary LRU), never exceeds the cap.

**Sidecar authz — independent trust boundary (hardened).** Rev 2 left it ambiguous
whether the sidecar's authorization view could be influenced by the executor it shares a
pod with. Fixed:
- **Executor → sidecar (UDS, minimal, no identity claims).** Same pod, Unix domain
  socket on the shared volume (file-mode restricted), not TCP — the executor still
  opens *zero* network sockets. Frame set: `RequestBundle{digest, component_key,
  sidecar_key}` → `Ready{local_path}` or `Err{UNAUTHORIZED|NOT_FOUND|FETCH_FAILED|
  RATE_LIMITED}`. **The executor supplies no tenant/app identity at all** — there is
  deliberately nothing for it to claim; digest is the only input, and digest alone
  cannot forge authorization.
- **Sidecar's own view, fed directly by the stage — never relayed through the
  executor.** Routing the authorization set *through* the executor (stage → executor →
  sidecar) would let a compromised executor tamper with or replay it, defeating
  independence. Instead the sidecar holds its **own** mTLS client identity and a
  **second** small stage endpoint (`:8303`, read-only "authz feed", distinct from the
  executor's `:8301` invoke/load channel and cert) over which the stage pushes the
  current authorized-digest set. *Rejected alternative:* give the sidecar its own
  direct Postgres RO-DB connection — genuinely independent of the executor, but
  reintroduces DB credentials into the executor Deployment's pod boundary, undoing the
  base spec's core property that pod holds none (§4.5, §7.1 base spec); the second mTLS
  channel gets the same independence without that regression.
- **Sync mechanism — same watermark/change-log, no new poll loop.** The stage already
  derives its active set from §7's change-log poll; it additionally flattens that into
  `authorized_digests: Set<Digest>` and pushes it (or a diff) to its sidecar peer on
  every refresh — reusing the existing cadence rather than adding one. The sidecar
  rejects `RequestBundle` for any digest outside this set *before* touching the bucket,
  surfacing as the existing `LOAD_FAILED` shape (§7.2/§7.3 base spec).
- **Cold-start DoS protection and single-flight dedup are unchanged from rev 2**
  (per-tenant `TENANT_BUNDLE_LOAD_RATE_LIMIT`, default `5/s`), now enforced in the
  sidecar's independently-authorized request path.

## 4. svc_action under the same model

Unchanged from rev 2: same partition-lease model (now including hard self-eviction,
server-side fencing, and load-aware rebalancing from §1) applied to `svc_action`,
partitioned by `app_id`. Action streams stay 1:1 `(stream, group={app_id}, app)`
(§5.9 base spec) — no partition-stream consolidation needed there, since it was never
fanned across multiple apps per stream the way ingest sources were.

## 5. Multi-tenant fairness & isolation (unchanged from rev 2)

Per-tenant rate limit → weighted fair dispatch → per-tenant executor concurrency
sub-ceiling → per-tenant aggregate trip → per-tenant metrics (`waddles_tenant_*`).
Layered cheapest-first, as before; unaffected by the ingress restructuring in §2.

## 6. Capacity table, HPA signals, failure modes

| Signal | Alpha shape | Rev 2 | Rev 3 (this revision) |
|---|---|---|---|
| Ingress streams/groups | ~150,000 (per-(app,source)) | ~10,000 (per-source) | **2048** (fixed partition streams) |
| Ingress blocking connections | ~150,000 | ~10,000 | **~200** (multiplexed) |
| Work-queue groups/conns | N/A | ~30,000 groups / ~200 conns | unchanged |
| **Total blocking connections** | ~150,000+ | ~10,200 | **~400** |
| Resident digests/replica | unbounded | ~100–150 hot, `250`-entry LRU | unchanged, now with byte-capped pinning |
| Watermark poll | O(tenant's active set) × replicas | O(changes/tick), `5s` time-margin | O(changes/tick), **exact xid-safe cutoff, no margin** (§7) |
| Fencing gap window | N/A | soft, bounded by poll tick | **~0**, atomic server-side reject (§1) |
| Partition rebalance | N/A | reactive only | reactive + voluntary load-based shed (§1) |

**HPA** unchanged from rev 2: scale on partition-level source lag and tenant-level
aggregate work-queue lag, never on one app's queue depth.

**Failure modes, additions:**

| Failure | Behavior |
|---|---|
| Stale-epoch write attempted | Rejected atomically by the Lua script; old holder's self-eviction deadline (§1) independently stops further attempts within `SAFETY_MARGIN_MS` even if it never sees the rejection. |
| Sidecar's authz-feed connection to stage drops | Sidecar keeps serving its last-known digest set (fail-static, never fail-open to "allow all"); new digests activated during the outage are rejected `UNAUTHORIZED` until the feed reconnects — a correctness-safe false negative, alerted on. |
| Single partition consistently hot | `waddles_partition_lag{partition}` >5× median triggers voluntary shed (§1); persistent skew after shedding escalates to hot-source isolation. |

## 7. Change-log correctness, retention, and full-reconcile safety net

**Sequence-gap fix — replaced (rev 2's `5s` time margin dropped).** Decision: **exact
transaction-visibility (xid-safe watermark)**, not an arbitrary delay.

```sql
-- one RO-connection read per tick:
SELECT seq, tenant_id, community_id, xmin::text::bigint AS xid
FROM bundle_active_set_changes WHERE seq > $last_seen_seq ORDER BY seq;
SELECT pg_snapshot_xmin(pg_current_snapshot()) AS horizon;   -- PG13+, no elevated privilege
```

Process every returned row immediately (safe — a row seen early is just seen early);
advance `last_seen_seq` only to `MAX(seq WHERE xid < horizon)`, **never past a row
whose inserting transaction could still be concurrently in flight**. A row above that
line is simply re-read next tick — a harmless duplicate wake-up (`read_active_set` is
idempotent), never a skip. This removes rev 2's `5s` added latency entirely; the
watermark is exact, not delayed.

**Feasibility on the RO reader.** `pg_current_snapshot()`/`pg_snapshot_xmin()` require
no special privilege and are correct on an ordinary read-only session (session-level
`default_transaction_read_only`) exactly as on a writer session, and are *also* correct
on a genuine hot-standby physical replica — hot standby's `KnownAssignedXids` mechanism
exists precisely to give a consistent MVCC visibility horizon over replayed WAL.
Confirm at implementation time which shape `DB_READER_*` actually targets; if some
managed-Postgres tier restricts these functions on its specific replica product (rare),
fall back to hub-api — the sole writer (§6.10 base spec) — computing the same safe
sequence once in its own session and publishing it into a one-row
`bundle_active_set_watermark` table for replicas to read instead of computing their own
snapshot.

**Retention (unchanged from rev 2).** `CHANGELOG_RETENTION` default `48h`; safe past
that horizon because a replica down longer is already in full-reconcile territory.

**Periodic full reconcile (unchanged from rev 2, kept as the ask required).** Every
replica re-runs `read_active_set` in full for its owned scope every
`FULL_RECONCILE_INTERVAL` (default `15m`) and immediately on acquiring a new partition —
bounds the blast radius of any change-log defect to one interval, independent of §7's
own correctness.

## 8. Migration path

**Flag mechanism (unchanged house rule).** PostHog opt-out kill-switches,
`waddles.core.disable-<mechanism>` — unseen/unreachable = new path ON (fail-safe);
ON = legacy fallback.

1. **Change-log table + trigger, xid-safe polling** (§7) — schema-only + read-logic,
   no flag.
2. **Multi-tenant watermark polling** (§1, §7). `waddles.core.disable-multi-tenant-watermark`.
3. **Partition-stream ingress + shared per-partition group + atomic fenced hand-off to
   per-app work streams** (§1, §2) — replaces per-(app,source) groups (alpha) and the
   two-round-trip hand-off (rev 2). New sources cut over immediately behind the flag;
   existing per-source streams migrate one at a time via a hub-api-driven
   drain-then-cutover job (flip a source only once its old stream's backlog and PEL are
   empty), not a big-bang rewrite. `waddles.core.disable-partition-stream-fanout`
   (renamed from rev 2's `disable-shared-source-fanout` — scope grew to include ingest).
4. **Partition ownership: fenced leases (hard self-eviction + server-side epoch
   check), jittered/rate-limited rebalance, load-aware voluntary shedding** (§1).
   Requires #3. `waddles.core.disable-partition-leases`.
5. **Bundle fetch/cache sidecar, independent stage-fed authz channel** (§3).
   `waddles.core.disable-bundle-cache-sidecar`.
6. **Lazy load + LRU with decayed-score/age-unpin/survivor-tracked/byte-capped pinning
   and per-tenant cold-load rate limits** (§3), built on #5.
   `waddles.core.disable-lazy-bundle-load`.
7. **svc_action multi-app partitioning** (§4), same fenced-lease model over `app_id`.
   `waddles.core.disable-action-multi-app`.
8. **Multi-tenant fairness controls** (§5). `waddles.core.disable-tenant-fairness`.
9. **Decommission the legacy topology** — delete `BUNDLE_SCOPE_TENANT_ID`/`action_app_id`
   single-scope paths, hub-api's per-(app,source) group provisioning, the old
   per-source stream write path in svc_ingest, and rev 1/2's completion-gated and
   two-round-trip ack paths, once 1–8 have soaked green at representative load.
