"""One-off backfill: signs every already-approved `app_versions` row missing a signature.

Covers a row that predates artifact signing (spec SS5.6, Gemini review
condition 9), or whose signed sidecar upload previously failed after the
DB-side signature already committed.

Run as `python3 -m cli.sign_approved_bundles` from hub-api's own `/app` WORKDIR (same top-level-
module convention as `cli/seed_core_bundles.py` -- see that module's own docstring for why).
In-cluster, one-shot Job -- no HTTP endpoint, no JWT, hub-api's own DB/MinIO env
(`HubAPIConfig.from_env()`), the same `BUNDLE_SIGNING_PRIVATE_KEY`/`BUNDLE_SIGNING_KEY_ID`
`services/bundle_signing_service.py` reads for every live approval.

**Selection.** Every `app_versions` row with a non-NULL `artifact_digest` (published) and a
NULL `artifact_signature` (never signed, or a prior signing attempt never committed), that has
at least one CURRENT (`superseded_by IS NULL`) `app_install_approvals` row for the same
`(app_id, version)` -- an unapproved version is never signed by this CLI (matches
`bundle_approval_service.approve_version()`'s own posture: signing only ever happens alongside
an approval). Idempotent re-run: a row this CLI already signed no longer matches the
`artifact_signature IS NULL` filter, so a second run is a true no-op for it.

**Why the same artifact can be re-signed safely.** `app_versions` is content-addressed --
one row per `(app_id, version)`, one WASM digest, shared across every tenant/community that
installs it. `app_install_approvals` is per-`(tenant, community, app_id, version)` -- several
approval rows can reference the SAME `app_versions` row. This CLI (like
`bundle_approval_service._write_approval_and_activate()` itself) picks ANY one current approval
for the version being signed; the signature's job is "hub-api legitimately approved this exact
digest", not "this exact tenant's approval", so which current approval id gets embedded does
not change what the executor's `core/bundle_executor/src/signing.rs` verification actually
checks.
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
from services import bundle_signing_service
from services.bundle_install_dal import build_install_dal, raw_sql_rows
from services.bundle_telemetry import get_meter

logger = logging.getLogger("waddles.hub_api.sign_approved_bundles")

_SELECT_UNSIGNED_APPROVED_VERSIONS_SQL = """
    SELECT v.id AS version_id, v.app_id AS app_id, v.version AS version, a.id AS approval_id
    FROM app_versions v
    JOIN app_install_approvals a
      ON a.app_id = v.app_id AND a.version = v.version AND a.superseded_by IS NULL
    WHERE v.artifact_digest IS NOT NULL
      AND v.artifact_signature IS NULL
    GROUP BY v.id, v.app_id, v.version, a.id
    ORDER BY v.id ASC
"""


@dataclass(slots=True, frozen=True)
class BackfillResult:
    """The outcome of signing one previously-unsigned `app_versions` row."""

    app_id: str
    version: str
    outcome: str
    detail: str = ""


async def _select_unsigned_approved_versions(install_dal: Any) -> list[Any]:
    """Rows needing a signature -- see module docstring's selection rule.

    One row per DISTINCT `app_versions.id` even if several current
    approvals reference it (the `GROUP BY` collapses duplicates); `MIN`
    would also work but `GROUP BY` alone with a deterministic `ORDER BY`
    is sufficient since this CLI only needs *any one* current approval id,
    not the specific minimum.
    """
    rows = await raw_sql_rows(install_dal, _SELECT_UNSIGNED_APPROVED_VERSIONS_SQL)
    return list(rows)


async def sign_one(install_dal: Any, row: Any) -> BackfillResult:
    """Signs one `app_versions` row and uploads its signed sidecar.

    Mirrors `bundle_approval_service._write_approval_and_activate()`'s own
    ordering: the DB write happens inside its own transaction (rolled back
    whole on any failure), then the bucket sidecar upload happens after
    that transaction commits -- a sidecar-upload failure is reported as
    this row's own outcome rather than raised, so one bad row's bucket
    connectivity issue never aborts the whole backfill batch.
    """
    versions_table = install_dal.metadata.tables["app_versions"]
    app_id = str(row.app_id)
    version = str(row.version)

    async with install_dal.engine.begin() as conn:
        signing_result = await bundle_signing_service.sign_and_record_version(
            conn,
            app_versions_table=versions_table,
            version_id=int(row.version_id),
            app_id=app_id,
            version=version,
            approval_id=int(row.approval_id),
        )

    try:
        await bundle_signing_service.upload_signed_sidecar(**signing_result)
    except Exception as exc:  # noqa: BLE001 -- one row's bucket failure must not abort the batch
        logger.error(
            "sign-approved-bundles: sidecar upload failed after the signature committed",
            extra={"app_id": app_id, "version": version, "error": str(exc)},
        )
        return BackfillResult(
            app_id,
            version,
            "signed_sidecar_upload_failed",
            f"signed but sidecar upload failed: {exc}",
        )

    return BackfillResult(app_id, version, "signed")


async def _run() -> int:
    config = HubAPIConfig.from_env()
    install_dal = await build_install_dal(config.database_url, pool_size=2)
    counter = get_meter().create_counter(
        "waddles_hub_sign_approved_bundles_total",
        description="sign-approved-bundles backfill attempts, by outcome",
    )

    failures = 0
    try:
        rows = await _select_unsigned_approved_versions(install_dal)
        logger.info(
            "sign-approved-bundles: starting",
            extra={"unsigned_approved_versions_examined": len(rows)},
        )

        for row in rows:
            try:
                result = await sign_one(install_dal, row)
            except Exception as exc:  # noqa: BLE001 -- one row's failure must not abort the batch or hide the exit code
                logger.error(
                    "sign-approved-bundles: row failed",
                    extra={"app_id": row.app_id, "version": row.version, "error": str(exc)},
                )
                counter.add(1, {"app_id": str(row.app_id), "outcome": "failed"})
                failures += 1
                continue

            logger.info(
                "sign-approved-bundles: result",
                extra={
                    "app_id": result.app_id,
                    "version": result.version,
                    "outcome": result.outcome,
                    "detail": result.detail,
                },
            )
            counter.add(1, {"app_id": result.app_id, "outcome": result.outcome})
            if result.outcome != "signed":
                failures += 1

        logger.info(
            "sign-approved-bundles: summary",
            extra={"examined": len(rows), "failures": failures},
        )
    finally:
        await install_dal.close()

    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point -- returns a process exit code (0 = every row signed/no-op'd cleanly)."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "").parse_args(argv)
    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover - script-execution guard, never hit under pytest
    sys.exit(main())
