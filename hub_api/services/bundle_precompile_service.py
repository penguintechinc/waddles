"""AOT precompile queue for approved bundle artifacts (executor-scale plan task 4).

hub-api's side of `bundle-executor --precompile`:

- `enqueue_precompile()` -- called right after a version's signed sidecar is uploaded
  (approval, tenant-wide activation, signing backfill, sidecar reconcile). Idempotent:
  `INSERT ... ON CONFLICT (artifact_digest) DO NOTHING`, so a digest is queued at most once
  while pending no matter how many communities approve it.
- `claim_pending()` -- the `executor-precompile` CronJob's export step: leases up to `limit`
  rows (sets `claimed_at`) that are unclaimed or whose lease expired and that are under the
  attempt cap, joined to `app_versions` for the bucket keys the precompiler fetches. A claim does
  NOT consume an attempt (see "Attempt accounting" below).
- `select_all_active()` / `enqueue_all_active()` -- the per-release `executor-precompile-enqueue`
  Job: queue every signed, currently-active version (a new executor image = new engine
  fingerprint), optionally reviving dead-lettered rows. It never compiles, so it never gates
  `helm upgrade`; the CronJob drains it.
- `ack_report()` -- deletes rows the precompiler reports `compiled`/`skipped`; on `failed` rows
  records `last_error` and consumes one attempt (they retry after the lease, up to the cap).
- `requeue_failed()` -- operator tool: reset dead-lettered rows (attempts at the cap) so they are
  claimable again, with `app_id` / error filters, a dry-run default and a loud safety cap.

Attempt accounting (incident 2026-11-16..19, alpha): `attempts` counts only GENUINE failures,
i.e. a `failed` result acked by the precompiler. Claiming a row never increments it. Previously
every claim incremented `attempts`, so a precompile Job that died before acking (OOM, eviction,
node drain) burned an attempt per 1800s lease re-offer until the row dead-lettered with an empty
`last_error` (50 such rows on alpha). A lease that expires with no ack is re-offered
(counted and logged as a `stale_reclaim`) and increments the SEPARATE `stale_reclaims` column, never
`attempts`. Poison-pill guard: a row whose lease expires more than `MAX_STALE_RECLAIMS` times (a
runner that deterministically crashes, is OOM-killed or hangs) is dead-lettered by `claim_pending()`
(`attempts` set to the cap, `last_error` = "lease expired N times without ack (runner
crash/timeout)") so it cannot be re-offered forever. A genuine failed ack resets `stale_reclaims`.
`last_error` is only ever written by a genuine failure and is cleared whenever a row is revived
(`requeue_failed`, or the reconcile Job's `revive_dead_letters`), which also reset `stale_reclaims`;
a successful ack deletes the row.

Per-engine-fingerprint idempotency lives in object storage (`precompiled/<engine_fp>/...`), which
the precompiler checks before compiling -- this table only says "this digest needs a look".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from services.bundle_install_dal import raw_sql_rows, raw_sql_write
from services.bundle_telemetry import get_meter

logger = logging.getLogger("waddles.hub_api.bundle_precompile")

#: A claimed row is re-offered after this many seconds without an ack (precompile Job died).
DEFAULT_LEASE_SECONDS = 1800
#: Genuine-failure attempts after which a row is left for operators (dead letter, error kept).
DEFAULT_MAX_ATTEMPTS = 5
#: Expired-lease reclaims (no ack) tolerated per row; the next one dead-letters it (poison pill).
DEFAULT_MAX_STALE_RECLAIMS = 3
#: Default `requeue_failed` safety cap: more matches than this aborts instead of requeueing.
DEFAULT_REQUEUE_CAP = 500
_LAST_ERROR_MAX = 500
_DIGEST_RE = re.compile(r"(?:sha256:)?[0-9a-fA-F]{64}")

_ENQUEUE_SQL = """
    INSERT INTO bundle_precompile_requests (artifact_digest, app_id, version)
    VALUES (:digest, :app_id, :version)
    ON CONFLICT (artifact_digest) DO NOTHING
    RETURNING id
"""

_STALE_DEAD_LETTER_SQL = """
    UPDATE bundle_precompile_requests
    SET attempts = GREATEST(attempts, :max_attempts),
        stale_reclaims = stale_reclaims + 1,
        last_error = 'lease expired ' || (stale_reclaims + 1)
                     || ' times without ack (runner crash/timeout)'
    WHERE id IN (
        SELECT id FROM bundle_precompile_requests
        WHERE attempts < :max_attempts
          AND claimed_at IS NOT NULL
          AND claimed_at < NOW() - make_interval(secs => :lease)
          AND stale_reclaims + 1 > :max_stale_reclaims
        FOR UPDATE SKIP LOCKED
    )
    RETURNING app_id, version, attempts, stale_reclaims
"""

_CLAIM_SQL = """
    WITH picked AS (
        SELECT r.id, r.claimed_at AS prev_claimed_at
        FROM bundle_precompile_requests r
        WHERE r.attempts < :max_attempts
          AND (r.claimed_at IS NULL OR r.claimed_at < NOW() - make_interval(secs => :lease))
        ORDER BY r.requested_at ASC
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE bundle_precompile_requests r
    SET claimed_at = NOW(),
        stale_reclaims = r.stale_reclaims + CASE WHEN r.claimed_at IS NULL THEN 0 ELSE 1 END
    FROM picked
    WHERE r.id = picked.id
    RETURNING r.artifact_digest, r.app_id, r.version, picked.prev_claimed_at
"""

_KEYS_SQL = """
    SELECT app_id, version, artifact_digest, component_key, sidecar_key
    FROM app_versions
    WHERE app_id = :app_id AND version = :version
      AND artifact_digest = :digest
      AND artifact_signature IS NOT NULL
      AND component_key IS NOT NULL AND sidecar_key IS NOT NULL
"""

_ALL_ACTIVE_SQL = """
    SELECT DISTINCT v.app_id, v.version, v.artifact_digest, v.component_key, v.sidecar_key
    FROM app_active_versions a
    JOIN app_versions v ON v.id = a.version_id
    WHERE v.artifact_signature IS NOT NULL
      AND v.component_key IS NOT NULL AND v.sidecar_key IS NOT NULL
    ORDER BY v.app_id, v.version
"""

_DELETE_SQL = "DELETE FROM bundle_precompile_requests WHERE artifact_digest = :digest"
_FAIL_SQL = """
    UPDATE bundle_precompile_requests
    SET last_error = :error, attempts = attempts + 1, stale_reclaims = 0
    WHERE artifact_digest = :digest
    RETURNING app_id, version, attempts
"""

_REVIVE_SQL = """
    UPDATE bundle_precompile_requests
    SET attempts = 0, stale_reclaims = 0, claimed_at = NULL, last_error = NULL
    WHERE artifact_digest = :digest AND attempts >= :max_attempts
    RETURNING id
"""

_DEAD_COUNT_SQL = """
    SELECT COUNT(*) AS n FROM bundle_precompile_requests WHERE attempts >= :max_attempts
"""

_DEAD_MATCH_SQL = """
    SELECT id, app_id, version, attempts
    FROM bundle_precompile_requests
    WHERE attempts >= :max_attempts
      AND (CAST(:app_id AS TEXT) IS NULL OR app_id = CAST(:app_id AS TEXT))
      AND (CAST(:error_like AS TEXT) IS NULL
           OR last_error ILIKE CAST(:error_like AS TEXT) ESCAPE '\\')
    ORDER BY requested_at ASC
    LIMIT :fetch_limit
"""

_REQUEUE_SQL = """
    UPDATE bundle_precompile_requests
    SET attempts = 0, stale_reclaims = 0, claimed_at = NULL, last_error = NULL
    WHERE id = :id AND attempts >= :max_attempts
    RETURNING id
"""

#: Last dead-letter count observed by this process (read by the observable gauge callback).
_DEAD_LETTERS_SEEN = 0
_GAUGE_REGISTERED = False


@dataclass(slots=True, frozen=True)
class PrecompileItem:
    """One manifest entry -- the exact JSON shape `bundle-executor --precompile` reads."""

    app_id: str
    version: str
    digest: str
    component_key: str
    sidecar_key: str

    def as_json(self) -> dict[str, str]:
        """Serializable dict for the manifest file."""
        return {
            "app_id": self.app_id,
            "version": self.version,
            "digest": self.digest,
            "component_key": self.component_key,
            "sidecar_key": self.sidecar_key,
        }


@dataclass(slots=True, frozen=True)
class AckSummary:
    """Counts from one `ack_report()` call; `examined` is the denominator."""

    examined: int
    acked: int
    failed: int


def _normalize_digest(digest: str) -> str:
    """Bare lowercase hex (the `app_versions.artifact_digest` convention); rejects anything else."""
    if not _DIGEST_RE.fullmatch(digest):
        raise ValueError("artifact digest must be 64 hex chars, optionally sha256:-prefixed")
    return digest.removeprefix("sha256:").lower()


def _counter() -> Any:
    """The queue-operations counter (no-op without an OTel provider)."""
    return get_meter().create_counter(
        "waddles_hub_bundle_precompile_requests_total",
        description="Bundle precompile queue operations, by operation and outcome",
    )


def _observe_dead_letters(_options: Any) -> list[Any]:
    """Observable-gauge callback: report the last dead-letter count this process saw."""
    from opentelemetry.metrics import Observation

    return [Observation(_DEAD_LETTERS_SEEN)]


def _ensure_gauge() -> None:
    """Register the dead-letter gauge once; a no-op meter makes this free without an exporter."""
    global _GAUGE_REGISTERED
    if _GAUGE_REGISTERED:
        return
    get_meter().create_observable_gauge(
        "waddles_hub_bundle_precompile_dead_letters",
        callbacks=[_observe_dead_letters],
        description="Precompile queue rows at the attempt cap (need operator attention)",
    )
    _GAUGE_REGISTERED = True


async def record_dead_letters(install_dal: Any, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> int:
    """Count dead-lettered rows, publish the gauge and log it (WARNING when non-zero).

    Called on every claim and ack cycle so the number is visible in metrics and logs without
    anyone querying Postgres. Returns the count, or -1 if the count query failed -- telemetry
    never fails the queue step, but the failure is logged loudly with a traceback.
    """
    global _DEAD_LETTERS_SEEN
    try:
        rows = await raw_sql_rows(install_dal, _DEAD_COUNT_SQL, {"max_attempts": max_attempts})
        first = rows.first()
        count = int(first.n) if first is not None else 0
    except Exception:  # noqa: BLE001 - telemetry must not fail the queue step
        logger.exception("bundle precompile: dead-letter count query failed")
        return -1
    _DEAD_LETTERS_SEEN = count
    _ensure_gauge()
    logger.log(
        logging.WARNING if count else logging.INFO,
        "bundle precompile: dead letters",
        extra={"dead_letters": count, "max_attempts": max_attempts},
    )
    return count


async def enqueue_precompile(install_dal: Any, *, app_id: str, version: str, digest: str) -> bool:
    """Queue `digest` for AOT precompile; True if newly queued, False if already pending.

    Raises on a DB failure (never swallowed): callers run this post-commit right after the
    signed-sidecar upload, the same raise-to-caller convention that upload uses, and every
    caller is idempotent to re-run.
    """
    normalized = _normalize_digest(digest)
    rows = await raw_sql_write(
        install_dal,
        _ENQUEUE_SQL,
        {"digest": normalized, "app_id": app_id, "version": version},
    )
    created = len(rows) > 0
    _counter().add(1, {"op": "enqueue", "outcome": "queued" if created else "already_pending"})
    logger.info(
        "bundle precompile: enqueue",
        extra={"app_id": app_id, "version": version, "queued": created},
    )
    return created


async def claim_pending(
    install_dal: Any,
    *,
    limit: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_stale_reclaims: int = DEFAULT_MAX_STALE_RECLAIMS,
) -> list[PrecompileItem]:
    """Lease up to `limit` pending rows and resolve their bucket keys.

    A claim does not consume an attempt; rows whose previous lease expired without an ack are
    re-offered, counted as `stale_reclaim` and tracked in `stale_reclaims`. A row whose lease has
    expired more than `max_stale_reclaims` times is dead-lettered first (poison pill: it
    crashes/hangs its runner every time) instead of being offered again. A claimed row whose
    `app_versions` row is not (yet) signed is still leased -- its sidecar may not be uploaded
    yet -- and is retried after the lease; it is logged at WARNING.
    """
    if limit < 1 or lease_seconds < 1 or max_attempts < 1 or max_stale_reclaims < 0:
        raise ValueError(
            "limit, lease_seconds and max_attempts must be >= 1; max_stale_reclaims >= 0"
        )
    poisoned = await raw_sql_write(
        install_dal,
        _STALE_DEAD_LETTER_SQL,
        {
            "lease": lease_seconds,
            "max_attempts": max_attempts,
            "max_stale_reclaims": max_stale_reclaims,
        },
    )
    for dead in poisoned:
        _counter().add(1, {"op": "claim", "outcome": "stale_dead_lettered"})
        logger.warning(
            "bundle precompile: lease expired too many times (dead letter)",
            extra={
                "app_id": str(dead.app_id),
                "version": str(dead.version),
                "attempts": int(dead.attempts),
                "stale_reclaims": int(dead.stale_reclaims),
                "max_stale_reclaims": max_stale_reclaims,
            },
        )
    claimed = await raw_sql_write(
        install_dal,
        _CLAIM_SQL,
        {"limit": limit, "lease": lease_seconds, "max_attempts": max_attempts},
    )
    items: list[PrecompileItem] = []
    stale = 0
    for row in claimed:
        if getattr(row, "prev_claimed_at", None) is not None:
            stale += 1
        keys = await raw_sql_rows(
            install_dal,
            _KEYS_SQL,
            {"app_id": row.app_id, "version": row.version, "digest": row.artifact_digest},
        )
        key_row = keys.first()
        if key_row is None:
            logger.warning(
                "bundle precompile: claimed digest has no signed app_versions row yet",
                extra={"app_id": row.app_id, "version": row.version},
            )
            continue
        items.append(_item(key_row))
    counter = _counter()
    counter.add(len(claimed), {"op": "claim", "outcome": "claimed"})
    if stale:
        counter.add(stale, {"op": "claim", "outcome": "stale_reclaim"})
    logger.info(
        "bundle precompile: claim",
        extra={"claimed": len(claimed), "exportable": len(items), "stale_reclaims": stale},
    )
    await record_dead_letters(install_dal, max_attempts=max_attempts)
    return items


def _item(row: Any) -> PrecompileItem:
    """Build a manifest item from an `app_versions` row."""
    return PrecompileItem(
        app_id=str(row.app_id),
        version=str(row.version),
        digest=str(row.artifact_digest),
        component_key=str(row.component_key),
        sidecar_key=str(row.sidecar_key),
    )


async def select_all_active(install_dal: Any) -> list[PrecompileItem]:
    """Every signed, currently-active version (deduplicated) -- the backfill work list."""
    rows = await raw_sql_rows(install_dal, _ALL_ACTIVE_SQL)
    items = [_item(r) for r in rows]
    logger.info("bundle precompile: backfill export", extra={"examined": len(items)})
    return items


@dataclass(slots=True, frozen=True)
class EnqueueSummary:
    """Counts from one `enqueue_all_active()` call; `examined` is the denominator."""

    examined: int
    queued: int
    already_pending: int
    revived: int = 0


async def enqueue_all_active(
    install_dal: Any,
    *,
    revive_dead_letters: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> EnqueueSummary:
    """Queue every signed, currently-active version for the precompile CronJob to drain.

    Run once per Helm release by the (non-hook) `executor-precompile-enqueue` Job: a new
    executor image has a new engine fingerprint, so every active digest needs a look. It only
    INSERTs queue rows (ON CONFLICT DO NOTHING) -- seconds, never hours -- and the CronJob
    compiles them in bounded batches, skipping digests that already have a valid signed artifact
    for the current fingerprint. Raises on any DB failure (fail loud, never a partial "ok").

    With `revive_dead_letters=True` (the Helm Job passes `--revive-dead-letters`), an active
    digest whose row already sits at the attempt cap is reset (attempts 0, lease and
    `last_error` cleared) instead of being skipped by ON CONFLICT. Design: a deploy is the
    moment a deterministic failure (bad grant, bad image) is most likely fixed, so every release
    gets one fresh round of attempts for ACTIVE versions only -- never an infinite loop (the
    reset happens once per release and the attempt cap still applies) and never touching rows
    for inactive versions. Off by default so ad-hoc runs stay insert-only.
    """
    items = await select_all_active(install_dal)
    queued = revived = 0
    for item in items:
        if await enqueue_precompile(
            install_dal, app_id=item.app_id, version=item.version, digest=item.digest
        ):
            queued += 1
        elif revive_dead_letters:
            hit = await raw_sql_write(
                install_dal,
                _REVIVE_SQL,
                {"digest": _normalize_digest(item.digest), "max_attempts": max_attempts},
            )
            if len(hit) > 0:
                revived += 1
                _counter().add(1, {"op": "enqueue", "outcome": "revived"})
                logger.warning(
                    "bundle precompile: revived dead-lettered row",
                    extra={"app_id": item.app_id, "version": item.version},
                )
    summary = EnqueueSummary(
        examined=len(items),
        queued=queued,
        already_pending=len(items) - queued - revived,
        revived=revived,
    )
    logger.info(
        "bundle precompile: enqueue all active",
        extra={
            "examined": summary.examined,
            "queued": summary.queued,
            "already_pending": summary.already_pending,
            "revived": summary.revived,
        },
    )
    return summary


async def ack_report(
    install_dal: Any,
    report: dict[str, Any],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> AckSummary:
    """Apply a precompiler report: delete compiled/skipped rows, record errors on failed ones.

    A `failed` result is the only thing that consumes an attempt; the row that reaches
    `max_attempts` is logged at WARNING (app_id/version/attempts only, no error body) and counted
    as `dead_lettered`. The dead-letter gauge is refreshed at the end of every ack.

    Validates the report shape strictly -- a malformed report is an error, never "nothing to do".
    """
    results = report.get("results")
    if not isinstance(results, list):
        raise ValueError("precompile report has no 'results' list")
    acked = failed = 0
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("precompile report result is not an object")
        digest = _normalize_digest(str(result.get("digest", "")))
        status = result.get("status")
        if status in ("compiled", "skipped"):
            await raw_sql_write(install_dal, _DELETE_SQL, {"digest": digest})
            acked += 1
        elif status == "failed":
            error = str(result.get("error") or "unknown error")[:_LAST_ERROR_MAX]
            hit = await raw_sql_write(install_dal, _FAIL_SQL, {"digest": digest, "error": error})
            failed += 1
            row = hit.first() if len(hit) else None
            if row is not None and int(row.attempts) >= max_attempts:
                _counter().add(1, {"op": "ack", "outcome": "dead_lettered"})
                logger.warning(
                    "bundle precompile: row reached max attempts (dead letter)",
                    extra={
                        "app_id": str(row.app_id),
                        "version": str(row.version),
                        "attempts": int(row.attempts),
                        "max_attempts": max_attempts,
                    },
                )
        else:
            raise ValueError(f"precompile report has unknown status {status!r}")
    counter = _counter()
    counter.add(acked, {"op": "ack", "outcome": "done"})
    counter.add(failed, {"op": "ack", "outcome": "failed"})
    await record_dead_letters(install_dal, max_attempts=max_attempts)
    return AckSummary(examined=len(results), acked=acked, failed=failed)


class RequeueCapExceededError(RuntimeError):
    """Raised when `requeue_failed` matches more rows than the operator's safety cap."""


@dataclass(slots=True, frozen=True)
class RequeueSummary:
    """Result of one `requeue_failed()` call; `matched` is the denominator."""

    matched: int
    requeued: int
    dry_run: bool


def _like_pattern(fragment: str | None) -> str | None:
    """Turn an operator substring into an escaped ILIKE `%fragment%` pattern (or None)."""
    if fragment is None:
        return None
    escaped = fragment.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def requeue_failed(
    install_dal: Any,
    *,
    app_id: str | None = None,
    error_like: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    cap: int = DEFAULT_REQUEUE_CAP,
    dry_run: bool = True,
) -> RequeueSummary:
    """Reset dead-lettered rows (attempts at the cap) so the CronJob claims them again.

    Replaces the hand-written `UPDATE ... SET attempts=0, claimed_at=NULL` used during the
    2026-11-19 alpha incident. Filters: `app_id` (exact) and `error_like` (case-insensitive
    substring of `last_error`). More than `cap` matches raises `RequeueCapExceededError` before any
    write (fail loud, never a partial requeue). `dry_run` (the default) only counts. Each
    requeued row has attempts reset and `claimed_at` / `last_error` cleared.
    """
    if cap < 1 or max_attempts < 1:
        raise ValueError("cap and max_attempts must be >= 1")
    rows = await raw_sql_rows(
        install_dal,
        _DEAD_MATCH_SQL,
        {
            "max_attempts": max_attempts,
            "app_id": app_id,
            "error_like": _like_pattern(error_like),
            "fetch_limit": cap + 1,
        },
    )
    matched = list(rows)
    if len(matched) > cap:
        raise RequeueCapExceededError(
            f"more than {cap} dead-lettered rows match; narrow the filters or raise --max"
        )
    requeued = 0
    if not dry_run:
        for row in matched:
            hit = await raw_sql_write(
                install_dal, _REQUEUE_SQL, {"id": row.id, "max_attempts": max_attempts}
            )
            requeued += len(hit)
        _counter().add(requeued, {"op": "requeue", "outcome": "requeued"})
    logger.info(
        "bundle precompile: requeue-failed",
        extra={
            "matched": len(matched),
            "requeued": requeued,
            "dry_run": dry_run,
            "app_id": app_id,
        },
    )
    return RequeueSummary(matched=len(matched), requeued=requeued, dry_run=dry_run)
