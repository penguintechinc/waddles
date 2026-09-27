# Waddles v3 Rust Data Plane — Multi-Tenant Scale Design

Amends `2026-09-14-rust-data-plane-design.md` (§4.1/4.2/4.3/4.5, §5, §7, §9). That spec
is correct for one tenant per deployment; this document removes that ceiling. It does
not change the envelope contracts, WIT world, or trust boundaries, except where noted
in §5 (sidecar) below.

**Revision note (rev 4, addresses a second rejected review).** Keeps rev 3's
server-side Lua epoch fencing as the real guarantee and states explicitly what it
cannot cover — side effects outside Valkey (executor invocation, outbound relay) remain
at-least-once and rely on message-id idempotency (§1). Moves the change-log safe-horizon
computation from the RO replica (unsound — a replica can lag the primary's own
in-flight-transaction knowledge) to the primary itself, publishing one `safe_seq` row
for replicas to consume read-only, plus writer transaction-timeout bounds and a
stall-detected reconcile trigger (§7). Corrects the Lua hand-off to reflect that Valkey
scripts do not roll back — explicit XADD-then-XACK ordering with a mandatory dedup key,
documented (§1, §2). Drops per-source overflow-partition migration (a FIFO hazard) in
favor of fixed hashing plus partition→replica load rebalancing only (§1). Adds an
explicit trust-boundary statement for shared partition streams (§2). Specifies
`SO_PEERCRED` (not a local TLS handshake) for the executor↔sidecar UDS channel plus an
explicit compromised-executor threat model (§3). Adds explicit pin eviction ordering and
promotion-stampede prevention (§3). Folds in a data-model correction: a physical source
is linked to many communities (often across tenants); ingest holds one connection per
physical source, and fan-out resolves source→links→apps after reading from the
(platform-level, not tenant-scoped) partition stream, preserving per-linked-community
FIFO (§2).

## 0. Sizing targets (unchanged)

| Dimension | Target |
|---|---|
| Tenants | 100s (worked examples use 500) |
| Communities | 10,000s (worked examples use 20,000) |
| Ingest sources (physical platform connections), all platforms | ~10,000 |
| Source→community links (a source may be linked to several communities, avg `k`) | ~20,000 (illustrative `k≈2`) |
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
        work_stream_1 fields_1(incl. dedup_key=message-id) [work_stream_2 fields_2 ...]
     if redis.call('GET', epoch_key) ~= expected_epoch then return {err='STALE_EPOCH'} end
     for each (work_stream, fields): redis.call('XADD', work_stream, '*', fields)
     redis.call('XACK', source_stream, group, entry_id)   -- deliberately last
     return 'OK'
   ```
   A new claimant bumps the epoch (`INCR waddles:lease:epoch:{partition}`) on acquire;
   the instant it does, every subsequent call from the old holder is rejected at the
   first line, server-side, regardless of the old holder's local clock/flag state.
   **Valkey/Redis scripts do not roll back a partial execution** — this is exactly why
   `XACK` is the last statement: if the script errors after some `XADD`s but before the
   `XACK` (a work-stream write failure, a script timeout), the source entry is simply
   left pending and is redelivered later (safe — never silent loss), and a redelivery's
   re-`XADD`s are recognized as duplicates by the `dedup_key` at the work-stream
   consumer (§2), not by attempting to make the script itself transactional.

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

**What fencing does not, and cannot, cover.** The Lua epoch check makes *who is allowed
to write/ack in Valkey* exact — but it says nothing about side effects a stale-epoch
attempt may have already produced **outside** Valkey before the fenced write was even
attempted: an executor invocation (wasmtime call) or an outbound relay send (Discord,
Twitch, §7.4 base spec `relay` capability) that already fired cannot be un-fenced after
the fact, because those are not Valkey operations the Lua script touches at all. Fencing
bounds ownership of the *transport*; it does not make invocation or outbound delivery
exactly-once. Those remain at-least-once end-to-end, same as the rest of this design,
and rely on the same idempotency-by-message-id contract (§5.4 base spec) — extended in
§2 to a stage-enforced dedup key on the work-stream hand-off itself, and applying
equally to `svc_action`'s outbound senders, which must dedupe an outbound send by
message-id rather than assume invocation happens exactly once.

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
- **Hot-source overflow migration — considered and dropped.** Moving a single hot
  *source* out of its assigned partition into a dedicated one (as proposed in rev 3)
  requires a barrier/drain protocol to preserve that source's FIFO guarantee — stop
  routing its new entries, wait until every entry already in the old partition for that
  source has been handed off, only then switch — real correctness machinery for a
  narrow benefit. **Decision: keep source→partition assignment fixed (rendezvous hash,
  never reassigned), and rebalance load only at the *partition→replica* granularity**
  (the voluntary shed above). This has no FIFO hazard at all: a partition's ownership
  moves as one atomic lease handoff, the whole stream moves as a unit, no per-source
  barrier is ever needed. The tradeoff, stated plainly: co-located sources sharing a
  hot partition stay co-located — isolation happens by giving that *partition* a
  replica with shed-away capacity (voluntary shed above naturally causes a hot
  partition's holder to shed everything else it owns), not by separating the sources
  themselves. If chronic skew persists after shedding, raise `N_PARTITIONS` to lower
  average co-location multiplicity rather than reintroducing per-source migration.

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
*transport* unit. Partition streams are platform-level, not tenant-scoped.**

**Source ≠ community: a physical source is linked to many communities, resolved at
fan-out, not baked into the stream.** A physical platform connection (one Twitch
channel, one Discord guild — one credential/identity, `source_id`) is commonly linked
into *several* communities at once — a streamer's own community, a team community, a
sponsor community — including across different tenants. Ingest holds and polls
**exactly one connection per physical `source_id`**, regardless of how many communities
link it; a stream keyed by `source_id` therefore cannot also be keyed by tenant/community
the way the base spec's original per-source stream name
(`waddles:t:{tenant}:c:{community}:src:...`) assumed. **This amends §5.10 base spec**:
tenant/community are no longer sourced from the stream key — they are resolved by the
trusted stage from a source-link table (`source_id → [(tenant_id, community_id,
connection_id), ...]`, one row per link; a schema split of the base spec's
`intake_sources` into a physical-source table and a link table, a hub-api-side change
this data-plane doc notes but does not itself design). The security invariant is
unchanged: tenant/community still never come from event *payload*, only now from a
trusted DB-backed lookup keyed off ingest's own resolved `source_id`, not from which
stream happened to carry the entry.

```
svc_ingest:  partition = rendezvous_hash(source_id) % 2048   (same pure fn as §1, shared crate)
             XADD waddles:partition:{n}:events MAXLEN ~ {SPINE_PARTITION_STREAM_MAXLEN} * env {envelope_json}
             -- envelope carries source_id/platform identity only; no tenant/community

owning replica (leases partition n, §1), per entry:
   XREADGROUP GROUP stage {consumer_id} ... STREAMS waddles:partition:{n}:events >
     → resolve links(source_id) = every (tenant, community, connection) that has linked
       this physical source (trusted DB-cached lookup, refreshed via §7's change-log —
       same mechanism, one more consumer of it)
     → for each link, evaluate each of that (tenant, community)'s locally-subscribed
       apps' `consumes` filter against the entry (§5.2 base spec grant resolution,
       now scoped per link rather than per raw source — Valkey structure was never the
       enforcement boundary, the in-process check always was)
     → ONE Lua call (§1 fencing), epoch-gated: XADD to every matched (link, app) pair's
       own waddles:t:{tenant}:c:{community}:app:{app_id}:work stream (each entry carries
       dedup_key = source message-id), THEN XACK the partition-stream entry last —
       ordered deliberately since Valkey scripts do not roll back a partial failure
       (§1); a script that fails between XADDs and XACK leaves the entry pending for
       safe redelivery, and the redelivered XADDs are recognized as duplicates by
       dedup_key at the work-stream consumer (below), not prevented by the script
```

Downstream of fan-out, nothing changes: a work stream is already keyed by
`(tenant, community, app_id)`, which is exactly right for "tenant/community attach
after fan-out" — a community's app can receive fan-out from several different physical
sources it has linked (its Twitch channel and its Discord guild both feed the same
moderation app's one work stream), and per-tenant fairness/quotas/encryption (§5) apply
at this layer, downstream of link resolution, unaffected by how many links a source has.

- **Ordering, including per linked community.** Per-source FIFO holds: all of one
  source's events hash to the same partition and are appended by the same ingest write
  path in occurrence order; the single leaseholder reads that partition in stream order
  and fans out to every matched link synchronously, within the same entry's processing
  step, before advancing to the next entry. A source linked to communities C1 and C2
  therefore has its **own FIFO subsequence preserved independently into C1's app work
  streams and into C2's app work streams** — the single-threaded, strictly-sequential
  reader is what guarantees this, with no extra mechanism needed per link. Cross-source
  ordering within a shared partition, and cross-source ordering into one app's work
  stream (e.g. a community's moderation app fed by both its Twitch and Discord links),
  are both *not* guaranteed — unchanged from the base spec's existing "no ordering
  across sources" statement.
- **No new head-of-line risk from consolidation.** Valkey's PEL/`XACK` operate per
  *entry*, not per co-located source — a slow entry from source A does not block
  acking a different, already-ready entry from source B in the same partition stream;
  the single reader loop processing entries strictly in order is the only place
  cross-source contention could appear, which is exactly what §1's load-aware
  rebalancing and hot-source isolation exist to catch.
- **Work-stream dedup, concrete mechanism.** Every work-stream entry's `dedup_key`
  field is the source event's `message-id` (mandatory, not optional). Before invoking,
  the work-stream consumer attempts
  `SET waddles:dedup:{app_id}:{dedup_key} 1 NX PX {DEDUP_WINDOW_MS}` (default `60000`,
  comfortably longer than a realistic partial-failure retry per §1); on a failed `SET`
  (already seen) it `XACK`s the work-stream entry immediately without invoking — an
  active drop, not a hope that every bundle author implemented idempotency correctly,
  layered **in addition to**, not instead of, the existing bundle-level idempotency
  contract (§5.4 base spec) as defense in depth.
- **Trust boundary, stated explicitly.** A partition stream carries many tenants'
  events interleaved, but its only reader is trusted platform code — the owning
  replica's stage process — never a bundle or guest; bundles hold no Valkey connection
  at all (§5.2 base spec, unchanged). Co-locating many tenants in one physical stream
  crosses no tenant boundary *because* the sole reader is the trusted stage enforcing
  per-app grant filtering in-process before anything reaches less-trusted bundle code —
  consistent with the base spec's existing statement that Valkey structure was never
  the enforcement boundary, the in-process check always was.
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

Headline: **~180,000 (rev 1) → ~10,200 (rev 2) → ~400 (rev 3/4)** blocking connections at
target scale, and connection count is now flat against *both* source-count and
install-count growth.

**Links add fan-out CPU, not streams/groups/connections.** Sources ≠ links: ~10,000
physical sources at avg `k≈2` links/source (§0) is ~20,000 source-community links, and
each link's own set of subscribed apps (~5 avg) puts the *per-entry fan-out work* at
roughly `links × apps/link` in the worst case — but this cost is a trusted in-process
DB-cached lookup plus concurrent `XADD`s, not a new stream, group, or connection.
Table above is unaffected by `k`: partition-stream count stays `2048` (fixed), and
work-stream count stays ~30,000 (= install count, which already counts an app once per
`(tenant, community)` it's installed in, however many physical sources feed that
community). Only the CPU cost of resolving links and evaluating filters per entry scales
with `k` — bounded, cacheable, and orthogonal to the connection math above.

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
- **Eviction order, explicit.** (1) Non-pinned entries evicted by plain LRU recency —
  exhausted first, before pinned space is ever touched. (2) Only if the pinned set
  itself must shrink (byte cap exceeded by pinned entries alone, or genuine memory
  pressure) evict lowest decayed-score pins first, longest-tenured survivors last.
- **Stampede prevention on pin promotion.** A newly-promoted pin can trigger a proactive
  fetch if the digest isn't already resident; since replicas observe similar traffic,
  many could promote the same digest at the same `15m` recompute boundary. Mitigate
  with jittered recompute ticks per replica (same philosophy as §1's rebalance jitter,
  so promotions don't land in lockstep), the existing single-flight dedup (per digest,
  per replica — a plain object-store `GET` is cheap and safely concurrent across
  replicas regardless, so this bounds request *rate*, not correctness), and the
  existing per-tenant `TENANT_BUNDLE_LOAD_RATE_LIMIT` (§3 above), which applies to
  pin-triggered loads exactly as it does to ordinary cold loads.

**Sidecar authz — independent trust boundary (hardened).** Rev 2 left it ambiguous
whether the sidecar's authorization view could be influenced by the executor it shares a
pod with. Fixed:
- **Executor → sidecar: UDS + `SO_PEERCRED`, no local handshake.** Same pod, Unix
  domain socket on the shared volume — no TCP, no TLS handshake on this hop, since the
  channel is already OS-isolated within one pod's network/mount namespace. The sidecar
  authenticates the connecting peer via `SO_PEERCRED` (kernel-verified UID/GID/PID of
  the connecting process, not a self-reported credential) at accept time, rejecting any
  peer whose UID doesn't match the known executor container's — cheaper and simpler
  than a redundant local mTLS handshake for same-pod IPC that Linux already isolates by
  process/user. Frame set: `RequestBundle{digest, component_key, sidecar_key}` →
  `Ready{local_path}` or `Err{UNAUTHORIZED|NOT_FOUND|FETCH_FAILED|RATE_LIMITED}`. **The
  executor supplies no tenant/app identity at all** — there is deliberately nothing for
  it to claim; digest is the only input, and digest alone cannot forge authorization.
- **Threat model, stated explicitly.** A *fully compromised* executor still holds no
  Postgres/Valkey credentials, opens no network sockets, and can send exactly one kind
  of request (`RequestBundle{digest}`) with no identity parameter to forge. Its blast
  radius is therefore bounded to: triggering a fetch/load of a digest that is *already*
  legitimately active somewhere within this replica's own owned partitions — it cannot
  reach a digest belonging to a tenant/community this replica doesn't serve, because
  the sidecar checks against its own stage-fed authorized set (below), never against
  anything the executor asserts.
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
| Watermark poll | O(tenant's active set) × replicas | O(changes/tick), `5s` time-margin | O(changes/tick), **exact `safe_seq` computed on the primary** (§7) |
| Fencing gap window | N/A | soft, bounded by poll tick | **~0**, atomic server-side reject (§1) |
| Partition rebalance | N/A | reactive only | reactive + voluntary load-based shed (§1) |

**HPA** unchanged from rev 2: scale on partition-level source lag and tenant-level
aggregate work-queue lag, never on one app's queue depth.

**Failure modes, additions:**

| Failure | Behavior |
|---|---|
| Stale-epoch write attempted | Rejected atomically by the Lua script; old holder's self-eviction deadline (§1) independently stops further attempts within `SAFETY_MARGIN_MS` even if it never sees the rejection. |
| Sidecar's authz-feed connection to stage drops | Sidecar keeps serving its last-known digest set (fail-static, never fail-open to "allow all"); new digests activated during the outage are rejected `UNAUTHORIZED` until the feed reconnects — a correctness-safe false negative, alerted on. |
| Single partition consistently hot | `waddles_partition_lag{partition}` >5× median triggers voluntary shed (§1); persistent skew after shedding means raising `N_PARTITIONS`, not per-source migration (§1). |
| `safe_seq` stalls on the primary | Alert at `30s` stale; past `5m` stale, affected replicas proactively full-reconcile their owned scope instead of trusting the incremental watermark (§7). |

## 7. Change-log correctness, retention, and full-reconcile safety net

**Sequence-gap fix — replaced again (rev 3's replica-side `pg_snapshot_xmin` read was
itself unsound).** Computing the xid horizon *on the RO replica* depends on that
replica's own knowledge of which primary transactions are still in flight — a hot
standby learns about a just-started primary transaction only via periodically-emitted
`xl_running_xacts` WAL records, not instantly, so a replica's snapshot can *underestimate*
what's still running on the primary and produce a horizon that is too aggressive
(unsafe), reintroducing exactly the gap this mechanism exists to close. The primary
itself has no such lag — it has direct, real-time knowledge of its own live
transactions. **Decision: compute the safe horizon on the primary, publish it, consume
it read-only everywhere else.**

```sql
-- runs periodically (e.g. every 2s) on the PRIMARY, in hub-api or a small dedicated job:
SELECT pg_snapshot_xmin(pg_current_snapshot()) AS horizon;
SELECT COALESCE(MAX(seq), 0) AS safe_seq
  FROM bundle_active_set_changes WHERE xmin::text::bigint < horizon;
UPDATE bundle_active_set_watermark SET safe_seq = $safe_seq, computed_at = now()
  WHERE id = 1;   -- one row, ordinary write, replicates to standbys the normal way
```

Every replica then does a trivial, cheap read —
`SELECT safe_seq FROM bundle_active_set_watermark WHERE id = 1` — and polls
`WHERE seq > last_seen_seq AND seq <= safe_seq ORDER BY seq`, advancing `last_seen_seq`
only up to `safe_seq`. No replica ever computes its own horizon; correctness no longer
depends on replication lag at all, only on the primary's own live view, which is exact
by construction.

**Bounding staleness at the source.** A long-running or idle-in-transaction writer on
`app_active_versions`/`app_source_bindings`/`bundle_active_set_changes` would hold
`safe_seq` back indefinitely. Writer roles touching these tables get
`statement_timeout` (default `30s`) and `idle_in_transaction_session_timeout` (default
`10s`), bounding the worst case a stuck transaction can stall the horizon.

**Stall detection, two tiers.** (1) Alert (`SAFE_SEQ_STALL_ALERT`, default `30s`) when
`now() - computed_at` on the watermark row exceeds threshold — the primary-side job
itself is stuck or blocked. (2) If the stall persists past
`FULL_RECONCILE_ON_STALL_MINUTES` (default `5m`), affected replicas stop trusting the
incremental watermark and proactively trigger their own full reconcile (below) for
their owned scope rather than waiting on the next scheduled one — a faster reaction
specifically to a *detected* stall, layered on top of the always-on periodic reconcile.

**Fallback.** If a specific managed-Postgres product restricts `pg_snapshot_xmin` even
on the primary (not expected, but worth stating), the same computation can be done via
`txid_current_snapshot()`/`txid_snapshot_xmin()` (the pre-PG13 equivalents) with no
change to the published-watermark contract above.

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
3. **Partition-stream ingress + source-link resolution at fan-out + shared
   per-partition group + atomic fenced hand-off to per-app work streams** (§1, §2) —
   replaces per-(app,source) groups (alpha) and the two-round-trip hand-off (rev 2).
   Requires the hub-api-side schema split noted in §2 (physical-source table +
   source-link table) so ingest holds one connection per physical source while
   tenant/community attach at fan-out. New sources cut over immediately behind the
   flag; existing per-source streams migrate one at a time via a hub-api-driven
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
