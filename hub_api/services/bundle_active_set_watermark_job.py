"""Primary-side `bundle_active_set_watermark.safe_seq` publisher (data-plane scale design Sec7).

Increment 1, control-plane side, of
`docs/superpowers/specs/2026-09-28-dataplane-scale-design.md`. The
change-log table (`bundle_active_set_changes`) and its per-table
triggers are migration `0028_bundle_active_set_changelog`; this module
is the periodic job that turns that append-only log into the single
exact-visibility watermark every replica polls.

**Why a hub-api background task, not a K8s CronJob.** This repo's other
periodic control-plane job (`services/usage_aggregator_service.py`) is a
standalone CronJob because a several-minute cadence suits cron. Sec7's
own worked cadence is `e2s` (every 2 seconds) -- below Kubernetes
CronJob's practical minimum granularity -- so this runs as a tight
in-process loop inside the already-long-running hub-api Quart process
instead, sharing its existing `install_dal` `AsyncDB` pool
(`app.py::startup()`) rather than opening a new one.

**Why the primary, and why this can safely run in every hub-api
replica.** Sec7 rev3's replica-side `pg_snapshot_xmin` read was itself
unsound (a hot standby only learns about a just-started primary
transaction via periodic `xl_running_xacts` WAL records, so it can
under-count in-flight transactions and publish an unsafe, too-aggressive
horizon). `DATABASE_URL` here must point at the PRIMARY, not a read
replica (same connection hub-api already uses for every other write --
no new routing). Running this loop in *every* hub-api replica
concurrently is safe, not just tolerated: `compute_once()` is a pure
function of the primary's current state (no replica-local memory feeds
its result), so concurrent runs converge on the same answer and the
final `UPDATE ... WHERE id = 1` is a plain idempotent overwrite -- no
leader election needed for this increment. The only cost is redundant
read/write load, bounded by `tick_seconds` and the number of replicas.
`compute_once()` also verifies `pg_is_in_recovery() = false` at runtime,
every tick, before doing anything else -- a config assumption
("`DATABASE_URL` points at the primary") is not the same as a runtime
guarantee, and a failover that repoints the connection string at a
standby before this process restarts would otherwise silently
reintroduce rev3's exact unsound computation.

**Retention** (Sec7 "unchanged from rev 2", default 48h) is pruned by
this same job on a slower cadence (`retention_tick_every` ticks) so a
short-staffed deployment isn't required to run a second scheduled job
just to keep `bundle_active_set_changes` bounded. Each prune also
republishes `min_retained_seq` (the lowest surviving `seq`) so a
consumer whose `last_seen_seq` has fallen below it can tell it's past
retention and must full-reconcile instead of trusting a range poll that
silently skips pruned rows.

**`writer_xid xid8`, not the 32-bit `xmin` system column (post-review
fix).** `xmin` wraps at 2^32 and reads back as `FrozenTransactionId` (2)
once a tuple is frozen by `VACUUM FREEZE` -- comparing it against
`pg_snapshot_xmin()`'s 64-bit, epoch-extended `xid8` via a `bigint` cast
is unsound at any table age beyond a wraparound/freeze boundary. Every
comparison here is native `xid8 < xid8` against migration 0028's
`bundle_active_set_changes.writer_xid` column instead.

**One replica computes per tick; the rest skip (post-review fix).**
`compute_once()` being a pure function of primary state means *concurrent*
computation across replicas converges to the same answer, but it is still
wasted, redundant read/write load that scales with replica count for no
benefit. `pg_try_advisory_xact_lock()` (auto-released on transaction
end, never needs an explicit unlock) makes exactly one replica's
transaction do the real work per tick; every other replica's
`pg_try_advisory_xact_lock()` call returns `false` immediately and that
replica returns the watermark's current published value unchanged. This
is a cheap load-shedding optimization, not a correctness requirement --
see the `UPDATE ... WHERE safe_seq < :safe_seq` monotonic guard below,
which independently makes even a hypothetical concurrent write safe.

**Bounded, contiguous-prefix scan; monotonic publish (post-review
correctness/perf fix).** An unconditional `MAX(seq) WHERE writer_xid <
horizon` re-scans the whole table every tick *and* can publish a `seq`
that shadows a still-in-flight, lower-`seq` row from a transaction whose
xid was assigned at an earlier statement than its watched-table write --
see migration `0028`'s own docstring for the exact scenario and why
`seq` order and xid order aren't guaranteed to coincide. Fixed by
scanning only `seq > current safe_seq` (`LIMIT`-capped, a plain PK
-range index scan) and using a window function to find the longest
*contiguous* prefix of that batch where every row's `writer_xid` is
already below the horizon -- the moment a row fails that test, nothing
past it is considered "safe" this tick, even if a later row in the same
batch would individually qualify. The final publish additionally guards
`WHERE safe_seq < :safe_seq` so the watermark can never regress, even
if ticks somehow interleave unexpectedly.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Final

import structlog
from penguin_dal import AsyncDB
from sqlalchemy import text

from services.bundle_telemetry import get_meter

logger = structlog.get_logger()

#: Sec7's own worked cadence.
DEFAULT_TICK_SECONDS: Final[float] = 2.0
#: Sec7 "Retention (unchanged from rev 2)".
DEFAULT_RETENTION_HOURS: Final[float] = 48.0
#: Prune roughly every 5 minutes at the default 2s tick -- far slower than
#: the safe_seq computation itself; retention doesn't need second-granularity.
DEFAULT_RETENTION_TICK_EVERY: Final[int] = 150
#: Rows scanned per compute_once() tick -- bounds the query to a PK range
#: scan capped at this size, never a full-table scan.
DEFAULT_BATCH_SIZE: Final[int] = 1000

#: Cross-replica coordination key for pg_try_advisory_xact_lock() -- must
#: stay fixed forever (it's how independent hub-api replicas agree on
#: "who computes this tick"), never derived from anything that could
#: change between processes/deploys. Value: ASCII "wadb" (0x77616462) in
#: the high 32 bits, a fixed 0x00000007 tag in the low 32 bits.
_ADVISORY_LOCK_KEY: Final[int] = 0x7761646200000007

#: asyncpg decodes `xid8` natively as a Python `int` (its own codec
#: rejects a `str` on the way back in, so no `::text` round-trip here) --
#: `_SAFE_SEQ_SQL` re-casts it explicitly via `CAST(:horizon AS xid8)`.
#: Never downcast to `bigint` (that's the exact 32-bit-vs-64-bit
#: unsoundness migration 0028 fixed).
_HORIZON_SQL = "SELECT pg_snapshot_xmin(pg_current_snapshot()) AS horizon"
#: Runtime primary-only guard (post-review fix). `DATABASE_URL` pointing
#: at the primary is a deployment-config assumption, not something this
#: job can verify at import time -- a config drift (e.g. a failover that
#: repoints DNS/the connection string at the new standby before this
#: process restarts) would otherwise let compute_once() run rev3's exact
#: unsound replica-side computation. Checked first, every tick, inside
#: the same transaction as everything else.
_IS_IN_RECOVERY_SQL = "SELECT pg_is_in_recovery()"
_TRY_LOCK_SQL = "SELECT pg_try_advisory_xact_lock(:key) AS acquired"
_CURRENT_SAFE_SEQ_SQL = "SELECT safe_seq FROM bundle_active_set_watermark WHERE id = 1"
#: Bounded PK-range scan (never a full-table scan): only rows past the
#: currently-published safe_seq, capped at :batch_size. The window
#: function computes, per row in seq order, whether every row from the
#: start of this batch through this one is already xid-safe
#: (`prefix_safe`) -- the contiguous-prefix fix migration 0028's
#: docstring and this module's own docstring describe. MAX(seq) over only
#: `prefix_safe` rows can never jump past a not-yet-safe row, unlike an
#: unconditional MAX() over the whole qualifying set.
_SAFE_SEQ_SQL = """
WITH candidates AS (
    SELECT seq, (writer_xid < CAST(:horizon AS xid8)) AS is_safe
    FROM bundle_active_set_changes
    WHERE seq > :current_safe_seq
    ORDER BY seq
    LIMIT :batch_size
),
prefixed AS (
    SELECT seq, is_safe,
           bool_and(is_safe) OVER (
               ORDER BY seq ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
           ) AS prefix_safe
    FROM candidates
)
SELECT COALESCE(MAX(seq), :current_safe_seq) AS new_safe_seq
FROM prefixed
WHERE prefix_safe
"""
#: Monotonic guard -- `safe_seq` can never regress even under an
#: unexpected interleaving; a no-op UPDATE (0 rows) when new <= current.
_UPDATE_WATERMARK_SQL = (
    "UPDATE bundle_active_set_watermark SET safe_seq = :safe_seq, computed_at = now() "
    "WHERE id = 1 AND safe_seq < :safe_seq"
)
_PRUNE_SQL = (
    "DELETE FROM bundle_active_set_changes "
    "WHERE changed_at < now() - (:retention_hours * interval '1 hour') "
    "RETURNING seq"
)
_MIN_RETAINED_SEQ_SQL = "SELECT MIN(seq) FROM bundle_active_set_changes"
_UPDATE_MIN_RETAINED_SEQ_SQL = (
    "UPDATE bundle_active_set_watermark SET min_retained_seq = :min_retained_seq WHERE id = 1"
)

_meter = get_meter()
_safe_seq_gauge = _meter.create_gauge(
    "waddles.bundle_active_set.safe_seq",
    description="Latest safe_seq published to bundle_active_set_watermark",
)
_computed_at_gauge = _meter.create_gauge(
    "waddles.bundle_active_set.watermark_computed_at_epoch_seconds",
    description=(
        "Unix epoch seconds of the last successful safe_seq computation -- "
        "alert when time() - this value exceeds SAFE_SEQ_STALL_ALERT (default 30s, Sec7)"
    ),
)
_tick_failures_counter = _meter.create_counter(
    "waddles.bundle_active_set.watermark_tick_failures_total",
    description="Failed safe_seq computation ticks (DB error, etc.) -- feeds the same stall alert",
)
_replica_skipped_counter = _meter.create_counter(
    "waddles.bundle_active_set.watermark_replica_skipped_total",
    description=(
        "Ticks skipped because DATABASE_URL resolved to a standby "
        "(pg_is_in_recovery() = true) -- config drift, not a normal replica-count cost"
    ),
)
_pruned_rows_histogram = _meter.create_histogram(
    "waddles.bundle_active_set.changes_pruned",
    description="bundle_active_set_changes rows deleted per retention pass",
)


@dataclass(slots=True, frozen=True)
class WatermarkJobConfig:
    """Tunables for `BundleActiveSetWatermarkJob` -- all have Sec7's own stated defaults."""

    tick_seconds: float = DEFAULT_TICK_SECONDS
    retention_hours: float = DEFAULT_RETENTION_HOURS
    retention_tick_every: int = DEFAULT_RETENTION_TICK_EVERY
    batch_size: int = DEFAULT_BATCH_SIZE


class BundleActiveSetWatermarkJob:
    """Owns the periodic safe_seq computation + retention prune loop.

    One instance per hub-api process, started in `app.py::startup()`
    after `install_dal` exists and stopped in `shutdown()` -- see this
    module's own docstring for why running it in every replica
    concurrently is safe by construction, not just tolerated.
    """

    def __init__(self, dal: AsyncDB, config: WatermarkJobConfig | None = None) -> None:
        """Bind the shared `install_dal` pool and this job's tunables (defaults per Sec7)."""
        self._dal = dal
        self._config = config or WatermarkJobConfig()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        """Start the background loop. Idempotent -- a second call while running is a no-op."""
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="bundle-active-set-watermark")

    async def stop(self) -> None:
        """Cancel the background loop and wait for it to unwind. Safe to call when not running."""
        if self._task is None:
            return
        self._stopping.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        tick = 0
        while not self._stopping.is_set():
            try:
                await self.compute_once()
                tick += 1
                if tick % self._config.retention_tick_every == 0:
                    await self.prune_once()
            except Exception as exc:  # noqa: BLE001 -- one bad tick must never kill the loop
                _tick_failures_counter.add(1)
                logger.warning(
                    "bundle_active_set_watermark_tick_failed",
                    error=str(exc),
                )
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._config.tick_seconds)
            except TimeoutError:
                pass

    async def compute_once(self) -> int:
        """One safe_seq computation + publish, xid8-exact and load-shed across replicas.

        Runs entirely inside one transaction (required for
        `pg_try_advisory_xact_lock()`, which auto-releases on transaction
        end): acquire the cross-replica lock; if another replica already
        holds it this tick, return the currently-published value
        unchanged. Otherwise compute the horizon, scan the bounded
        contiguous-safe-prefix batch, and publish via the monotonic
        `WHERE safe_seq < :safe_seq` guard -- see this module's own
        docstring for why each piece is necessary.

        Returns the published `safe_seq` (unchanged from the previous
        tick when nothing new is safe yet, when another replica holds
        the lock this tick, or when `DATABASE_URL` has drifted onto a
        standby -- see the primary-only guard below).
        """
        async with self._dal.engine.begin() as conn:
            # Primary-only guard, checked before the advisory lock (post
            # -review fix). `DATABASE_URL` pointing at the primary is a
            # deployment-config assumption this process cannot verify at
            # startup -- a failover that repoints the connection string at
            # the new standby before this process restarts would otherwise
            # silently reintroduce rev3's exact unsound replica-side
            # computation. Never crash on this: skip the tick, warn, count.
            in_recovery = (await conn.execute(text(_IS_IN_RECOVERY_SQL))).scalar_one()
            if in_recovery:
                _replica_skipped_counter.add(1)
                logger.warning(
                    "bundle_active_set_watermark_skipped_not_primary",
                    reason="pg_is_in_recovery",
                )
                return int((await conn.execute(text(_CURRENT_SAFE_SEQ_SQL))).scalar_one())

            acquired = (
                await conn.execute(text(_TRY_LOCK_SQL), {"key": _ADVISORY_LOCK_KEY})
            ).scalar_one()
            current_safe_seq = int((await conn.execute(text(_CURRENT_SAFE_SEQ_SQL))).scalar_one())
            if not acquired:
                # Another replica is computing this tick -- skip entirely,
                # never contend for the same work (see module docstring).
                return current_safe_seq

            horizon = (await conn.execute(text(_HORIZON_SQL))).scalar_one()
            if horizon is None:
                # No live snapshot (e.g. a brand-new, connectionless
                # primary) -- nothing is unsafe to skip; try again next tick.
                return current_safe_seq

            new_safe_seq = int(
                (
                    await conn.execute(
                        text(_SAFE_SEQ_SQL),
                        {
                            "horizon": horizon,
                            "current_safe_seq": current_safe_seq,
                            "batch_size": self._config.batch_size,
                        },
                    )
                ).scalar_one()
            )
            if new_safe_seq > current_safe_seq:
                await conn.execute(text(_UPDATE_WATERMARK_SQL), {"safe_seq": new_safe_seq})

        published = max(new_safe_seq, current_safe_seq)
        _safe_seq_gauge.set(published)
        _computed_at_gauge.set(time.time())
        return published

    async def prune_once(self) -> int:
        """Delete `bundle_active_set_changes` rows older than `retention_hours`.

        Age-based only (Sec7 "safe past that horizon because a replica
        down longer is already in full-reconcile territory") -- never
        gated on `safe_seq`, since the retention window is far longer
        than any realistic consumer lag. Republishes `min_retained_seq`
        in the same transaction so consumers can detect falling behind
        retention.
        """
        async with self._dal.engine.begin() as conn:
            pruned_rows = (
                await conn.execute(
                    text(_PRUNE_SQL), {"retention_hours": self._config.retention_hours}
                )
            ).fetchall()
            min_retained = (await conn.execute(text(_MIN_RETAINED_SEQ_SQL))).scalar_one()
            if min_retained is None:
                # Nothing left in the log -- nothing is "behind" any point,
                # so fall back to the currently published safe_seq.
                min_retained = (await conn.execute(text(_CURRENT_SAFE_SEQ_SQL))).scalar_one()
            await conn.execute(
                text(_UPDATE_MIN_RETAINED_SEQ_SQL), {"min_retained_seq": min_retained}
            )

        pruned = len(pruned_rows)
        _pruned_rows_histogram.record(pruned)
        return pruned
