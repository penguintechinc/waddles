"""Scheduled reconciliation: re-uploads any missing/stale signed bucket sidecar.

MEDIUM security-review finding on `feature/bundle-artifact-signing`: the DB
side of signing (`app_versions.artifact_signature`) and the bucket side
(the `.json` sidecar `core/bundle_executor/src/signing.rs` actually reads)
are two independent writes -- `bundle_signing_service.upload_signed_sidecar()`
runs AFTER the DB transaction that recorded the signature commits, so a
bucket-write failure there (MinIO/S3 blip, network partition) leaves a row
that is signed in Postgres but never got its sidecar overwritten (still the
pre-signing `{}` stub from `storage_service.upload_bundle_component()`, or
missing entirely if it was never staged that way).

`hub_api/cli/sign_approved_bundles.py` only backfills rows with
`artifact_signature IS NULL` -- it never revisits a row that IS signed in
the DB but whose sidecar upload failed (that row is intentionally excluded
from its selection filter, see that module's docstring). This CLI is the
counterpart: it re-reads every currently-signed row's sidecar and re-runs
`upload_signed_sidecar()` for any that are missing the `signature` field
outright, comparing nothing else (the DB row is always the source of truth
-- this never trusts a partially-written sidecar's own contents).

Run on a schedule (`templates/bundle-signing-cronjob.yaml`, default every
30 minutes) rather than as a one-off, since a bucket-write failure can
recur at any time as new versions are approved and signed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

from config import HubAPIConfig
from services import bundle_signing_service, storage_service
from services.bundle_install_dal import build_install_dal, raw_sql_rows
from services.bundle_telemetry import get_meter

logger = logging.getLogger("waddles.hub_api.reconcile_signed_sidecars")

_SELECT_SIGNED_VERSIONS_SQL = """
    SELECT id AS version_id, app_id, version, artifact_digest, artifact_signature,
           artifact_signature_key_id, artifact_signed_approval_id
    FROM app_versions
    WHERE artifact_signature IS NOT NULL
      AND artifact_digest IS NOT NULL
    ORDER BY id ASC
"""

_MAX_ATTEMPTS = 4
_BASE_BACKOFF_S = 1.0


@dataclass(slots=True, frozen=True)
class ReconcileResult:
    """The outcome of reconciling one signed `app_versions` row's bucket sidecar."""

    app_id: str
    version: str
    outcome: str
    detail: str = ""


async def _select_signed_versions(install_dal: Any) -> list[Any]:
    """Every row already signed in Postgres -- the set this CLI checks the sidecar for."""
    rows = await raw_sql_rows(install_dal, _SELECT_SIGNED_VERSIONS_SQL)
    return list(rows)


async def _sidecar_needs_reupload(row: Any) -> bool:
    """True if the bucket sidecar is missing, unreadable, or lacks a `signature` field.

    Never compares the sidecar's own `signature`/`key_id` bytes against the
    DB row -- the DB is the sole source of truth for what SHOULD be there
    (matching `upload_signed_sidecar()`'s own "overwrite unconditionally"
    posture); this only asks "is a real signed document present at all".
    """
    sha256_hex = str(row.artifact_digest).removeprefix("sha256:")
    document = await storage_service.read_bundle_sidecar(
        str(row.app_id), str(row.version), sha256_hex
    )
    return document is None or not document.get("signature")


async def _reupload_with_backoff(row: Any) -> None:
    """Retries `upload_signed_sidecar()` with capped exponential backoff.

    `_MAX_ATTEMPTS` attempts, `_BASE_BACKOFF_S * 2**attempt` between each --
    a transient bucket blip should not fail this row's whole reconcile pass
    on the very first retry-able error the way `sign_approved_bundles.py`'s
    single-attempt-then-report posture does (that CLI is one-off and
    re-runnable by an operator; this one runs unattended on a schedule).
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            await bundle_signing_service.upload_signed_sidecar(
                app_id=str(row.app_id),
                version=str(row.version),
                digest=str(row.artifact_digest),
                approval_id=int(row.artifact_signed_approval_id),
                key_id=str(row.artifact_signature_key_id),
                signature=str(row.artifact_signature),
            )
            return
        except Exception as exc:  # noqa: BLE001 -- retried below; re-raised after the last attempt
            last_exc = exc
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BASE_BACKOFF_S * (2**attempt))
    if last_exc is None:  # pragma: no cover - defensive: _MAX_ATTEMPTS >= 1 always sets this
        raise RuntimeError("_reupload_with_backoff: no attempt ran")
    raise last_exc


async def reconcile_one(row: Any) -> ReconcileResult:
    """Checks one signed row's sidecar and re-uploads it if missing/stale."""
    app_id = str(row.app_id)
    version = str(row.version)

    try:
        needs_reupload = await _sidecar_needs_reupload(row)
    except Exception as exc:  # noqa: BLE001 -- one row's read failure must not abort the batch
        logger.error(
            "reconcile-signed-sidecars: sidecar read failed",
            extra={"app_id": app_id, "version": version, "error": str(exc)},
        )
        return ReconcileResult(app_id, version, "read_failed", str(exc))

    if not needs_reupload:
        return ReconcileResult(app_id, version, "already_signed")

    try:
        await _reupload_with_backoff(row)
    except Exception as exc:  # noqa: BLE001 -- one row's failure must not abort the batch
        logger.error(
            "reconcile-signed-sidecars: sidecar re-upload failed after retries",
            extra={"app_id": app_id, "version": version, "error": str(exc)},
        )
        return ReconcileResult(app_id, version, "reupload_failed", str(exc))

    logger.info(
        "reconcile-signed-sidecars: sidecar re-uploaded",
        extra={"app_id": app_id, "version": version},
    )
    return ReconcileResult(app_id, version, "reuploaded")


async def _run() -> int:
    config = HubAPIConfig.from_env()
    install_dal = await build_install_dal(config.database_url, pool_size=2)
    counter = get_meter().create_counter(
        "waddles_hub_reconcile_signed_sidecars_total",
        description="reconcile-signed-sidecars runs, by outcome",
    )

    failures = 0
    try:
        rows = await _select_signed_versions(install_dal)
        logger.info(
            "reconcile-signed-sidecars: starting",
            extra={"signed_versions_examined": len(rows)},
        )

        for row in rows:
            result = await reconcile_one(row)
            counter.add(1, {"app_id": result.app_id, "outcome": result.outcome})
            if result.outcome not in ("already_signed", "reuploaded"):
                failures += 1

        logger.info(
            "reconcile-signed-sidecars: summary",
            extra={"examined": len(rows), "failures": failures},
        )
    finally:
        await install_dal.close()

    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point -- returns a process exit code (0 = every signed row's sidecar OK)."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "").parse_args(argv)
    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover - script-execution guard, never hit under pytest
    sys.exit(main())
