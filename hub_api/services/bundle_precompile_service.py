"""AOT precompile queue for approved bundle artifacts (executor-scale plan task 4).

hub-api's side of `bundle-executor --precompile`:

- `enqueue_precompile()` -- called right after a version's signed sidecar is uploaded
  (approval, tenant-wide activation, signing backfill, sidecar reconcile). Idempotent:
  `INSERT ... ON CONFLICT (artifact_digest) DO NOTHING`, so a digest is queued at most once
  while pending no matter how many communities approve it.
- `claim_pending()` -- the `executor-precompile` CronJob's export step: leases up to `limit`
  rows (`claimed_at`, `attempts += 1`) that are unclaimed or whose lease expired and that are
  under the attempt cap, joined to `app_versions` for the bucket keys the precompiler fetches.
- `select_all_active()` -- the backfill Job's export step: every signed, currently-active
  version (run with a NEW executor image before rollout so its engine fingerprint is warm).
- `ack_report()` -- deletes rows the precompiler reports `compiled`/`skipped`; records
  `last_error` on `failed` rows (they retry after the lease, up to the cap).

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
#: Attempts after which a row is left for operators (dead letter, `last_error` kept).
DEFAULT_MAX_ATTEMPTS = 5
_LAST_ERROR_MAX = 500
_DIGEST_RE = re.compile(r"(?:sha256:)?[0-9a-fA-F]{64}")

_ENQUEUE_SQL = """
    INSERT INTO bundle_precompile_requests (artifact_digest, app_id, version)
    VALUES (:digest, :app_id, :version)
    ON CONFLICT (artifact_digest) DO NOTHING
    RETURNING id
"""

_CLAIM_SQL = """
    WITH picked AS (
        SELECT r.id
        FROM bundle_precompile_requests r
        WHERE r.attempts < :max_attempts
          AND (r.claimed_at IS NULL OR r.claimed_at < NOW() - make_interval(secs => :lease))
        ORDER BY r.requested_at ASC
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE bundle_precompile_requests r
    SET claimed_at = NOW(), attempts = r.attempts + 1
    FROM picked
    WHERE r.id = picked.id
    RETURNING r.artifact_digest, r.app_id, r.version
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
    UPDATE bundle_precompile_requests SET last_error = :error WHERE artifact_digest = :digest
"""


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
    return get_meter().create_counter(
        "waddles_hub_bundle_precompile_requests_total",
        description="Bundle precompile queue operations, by operation and outcome",
    )


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
) -> list[PrecompileItem]:
    """Lease up to `limit` pending rows and resolve their bucket keys.

    A claimed row whose `app_versions` row is not (yet) signed is still leased -- its sidecar
    may not be uploaded yet -- and is retried after the lease; it is logged at WARNING.
    """
    if limit < 1 or lease_seconds < 1 or max_attempts < 1:
        raise ValueError("limit, lease_seconds and max_attempts must be >= 1")
    claimed = await raw_sql_write(
        install_dal,
        _CLAIM_SQL,
        {"limit": limit, "lease": lease_seconds, "max_attempts": max_attempts},
    )
    items: list[PrecompileItem] = []
    for row in claimed:
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
    _counter().add(len(claimed), {"op": "claim", "outcome": "claimed"})
    logger.info(
        "bundle precompile: claim",
        extra={"claimed": len(claimed), "exportable": len(items)},
    )
    return items


def _item(row: Any) -> PrecompileItem:
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


async def ack_report(install_dal: Any, report: dict[str, Any]) -> AckSummary:
    """Apply a precompiler report: delete compiled/skipped rows, record errors on failed ones.

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
            await raw_sql_write(install_dal, _FAIL_SQL, {"digest": digest, "error": error})
            failed += 1
        else:
            raise ValueError(f"precompile report has unknown status {status!r}")
    counter = _counter()
    counter.add(acked, {"op": "ack", "outcome": "done"})
    counter.add(failed, {"op": "ack", "outcome": "failed"})
    return AckSummary(examined=len(results), acked=acked, failed=failed)
