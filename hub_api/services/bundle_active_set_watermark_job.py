"""Primary-side `bundle_active_set_watermark.safe_seq` publisher (data-plane scale design Sec7).

Increment 1, control-plane side, of
`docs/superpowers/specs/2026-09-28-dataplane-scale-design.md`. The
change-log table (`bundle_active_set_changes`) and its per-table
triggers are migration `0026_bundle_active_set_changelog`; this module
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

**Retention** (Sec7 "unchanged from rev 2", default 48h) is pruned by
this same job on a slower cadence (`retention_tick_every` ticks) so a
short-staffed deployment isn't required to run a second scheduled job
just to keep `bundle_active_set_changes` bounded.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Final

import structlog
from penguin_dal import AsyncDB

from services.bundle_install_dal import raw_sql_rows, raw_sql_write
from services.bundle_telemetry import get_meter

logger = structlog.get_logger()

#: Sec7's own worked cadence.
DEFAULT_TICK_SECONDS: Final[float] = 2.0
#: Sec7 "Retention (unchanged from rev 2)".
DEFAULT_RETENTION_HOURS: Final[float] = 48.0
#: Prune roughly every 5 minutes at the default 2s tick -- far slower than
#: the safe_seq computation itself; retention doesn't need second-granularity.
DEFAULT_RETENTION_TICK_EVERY: Final[int] = 150

_HORIZON_SQL = "SELECT pg_snapshot_xmin(pg_current_snapshot())::text::bigint AS horizon"
_SAFE_SEQ_SQL = (
    "SELECT COALESCE(MAX(seq), 0) AS safe_seq FROM bundle_active_set_changes "
    "WHERE xmin::text::bigint < :horizon"
)
_UPDATE_WATERMARK_SQL = (
    "UPDATE bundle_active_set_watermark SET safe_seq = :safe_seq, computed_at = now() WHERE id = 1"
)
_PRUNE_SQL = (
    "DELETE FROM bundle_active_set_changes "
    "WHERE changed_at < now() - (:retention_hours * interval '1 hour') "
    "RETURNING seq"
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
        """One safe_seq computation + publish, per Sec7's own SQL exactly.

        Returns the newly published `safe_seq` (also true when unchanged
        from the previous tick -- the `UPDATE` is idempotent either way).
        """
        horizon_rows = await raw_sql_rows(self._dal, _HORIZON_SQL)
        horizon = horizon_rows[0]["horizon"] if len(horizon_rows) else None
        if horizon is None:
            # No live snapshot (e.g. a brand-new, connectionless primary) --
            # nothing is unsafe to skip; try again next tick.
            return 0
        seq_rows = await raw_sql_rows(self._dal, _SAFE_SEQ_SQL, {"horizon": horizon})
        safe_seq = int(seq_rows[0]["safe_seq"]) if len(seq_rows) else 0
        await raw_sql_write(self._dal, _UPDATE_WATERMARK_SQL, {"safe_seq": safe_seq})
        _safe_seq_gauge.set(safe_seq)
        _computed_at_gauge.set(time.time())
        return safe_seq

    async def prune_once(self) -> int:
        """Delete `bundle_active_set_changes` rows older than `retention_hours`.

        Age-based only (Sec7 "safe past that horizon because a replica
        down longer is already in full-reconcile territory") -- never
        gated on `safe_seq`, since the retention window is far longer
        than any realistic consumer lag.
        """
        result = await raw_sql_write(
            self._dal, _PRUNE_SQL, {"retention_hours": self._config.retention_hours}
        )
        pruned = len(result)
        _pruned_rows_histogram.record(pruned)
        return pruned
