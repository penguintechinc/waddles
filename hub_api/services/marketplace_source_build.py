"""Async scan + (sandbox build) + ingest orchestration for external marketplace sources (G1/G2b).

ONE shared entry point, `scan_build_and_ingest`, used by BOTH triggers (add-source initial scan
and rescan / approve-update) via `launch_sandbox_scan`::

    PENDING_SCAN -> scan (clone pinned SHA, archive each bundle tree) -> per candidate:
        build_and_ingest (K8s Job, hub-recomputed sha, create_unapproved_version)
        -> ACTIVE / NEEDS_APPROVAL

Builds take minutes, so callers never await it on a request thread: `launch_sandbox_scan` spawns a
tracked background task. Every failure (builder not configured, gate refusal, scan rejection, build
failure/timeout, unexpected error) lands as source status ERROR with a sanitized `last_scan_error`
plus a log line -- never silent. Nothing here approves, signs or installs.

PREBUILT sources use the same launch/error scaffolding (`launch_prebuilt_scan`): PENDING_SCAN ->
scan -> `ingest_candidates` (no build) -> ACTIVE, so an add never sits in PENDING_SCAN forever.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from penguin_dal import AsyncDB

from services import marketplace_source_telemetry as mkt_telemetry
from services.errors import ApiError
from services.marketplace_sandbox_builder import (
    BuildJobClient,
    KubernetesJobClient,
    SandboxBuilderConfig,
    SandboxBuildError,
    assert_build_allowed,
    build_and_ingest,
    pack_bundle_dir,
)
from services.marketplace_source_ingest import (
    OUTCOME_NEEDS_APPROVAL,
    OUTCOME_REJECTED,
    OUTCOME_SKIPPED_IDENTICAL,
    IngestSummary,
    _existing_digest,
    ingest_candidates,
)
from services.marketplace_source_scanner import (
    BundleCandidate,
    ScanRejectedError,
    bundle_tarball_path,
    scan_source,
)
from services.marketplace_source_service import (
    SourceRecord,
    SourceStatus,
    update_status,
)

logger = logging.getLogger(__name__)

_TASKS: set[asyncio.Task[Any]] = set()


@dataclass(slots=True, frozen=True)
class SandboxRunSummary:
    """Counts for one sandbox scan+build run (`examined` is the scan denominator)."""

    examined: int
    accepted: int
    built: int
    skipped_existing: int
    failed: int


async def _fail(dal: AsyncDB, source: SourceRecord, reason: str) -> None:
    """Record a loud ERROR status + log; a DISABLED source (409) drops the status write."""
    logger.warning(
        "marketplace sandbox run failed",
        extra={"source_public_id": str(source.public_id), "reason": reason},
    )
    try:
        await update_status(
            dal, source.public_id, SourceStatus.ERROR, last_scan_error=reason[:500], scanned=True
        )
    except ApiError as exc:
        if exc.status_code != 409:
            raise


async def scan_build_and_ingest(
    dal: AsyncDB,
    source: SourceRecord,
    *,
    tier: str,
    tenant_id: int,
    tenant_slug: str,
    requested_by: int | None = None,
    valkey_client: Any | None = None,
    config: SandboxBuilderConfig | None = None,
    client_factory: Callable[[], BuildJobClient] | None = None,
) -> SandboxRunSummary:
    """Scan a SANDBOX source and build+ingest each candidate (the single shared call site).

    Gates (Enterprise tier + current ack bound to the pinned commit) are checked BEFORE any clone
    or Job; a refusal marks the source ERROR (existing versions are never touched/uninstalled).
    Raises nothing for build/scan failures -- they become status ERROR and a failed count.
    """
    sid = str(source.public_id)
    try:
        assert_build_allowed(source, tier)
        cfg = config or SandboxBuilderConfig.from_env()
        client = (client_factory or KubernetesJobClient)()
    except SandboxBuildError as exc:
        await _fail(dal, source, f"sandbox_build:{exc.reason}")
        return SandboxRunSummary(0, 0, 0, 0, 1)
    built = skipped = failed = 0
    with tempfile.TemporaryDirectory(prefix="mktbuild-") as workdir:
        pack_dir = Path(workdir) / "tarballs"
        pack_dir.mkdir()
        try:
            result = await scan_source(
                source,
                workdir=Path(workdir),
                dal=dal,
                actor_id=requested_by,
                pack_dir=pack_dir,
                packer=pack_bundle_dir,
            )
        except ScanRejectedError as exc:  # scan_source already set ERROR + audited
            logger.warning(
                "marketplace sandbox scan rejected",
                extra={"source_public_id": sid, "reason": exc.reason},
            )
            return SandboxRunSummary(0, 0, 0, 0, 1)
        except Exception:
            logger.exception("marketplace sandbox scan crashed", extra={"source_public_id": sid})
            await _fail(dal, source, "sandbox_scan:unexpected_error")
            return SandboxRunSummary(0, 0, 0, 0, 1)
        async with mkt_telemetry.source_span("marketplace.sandbox.run", source.public_id):
            for candidate in result.bundles:
                app_id = candidate.assigned_app_id
                try:
                    if await _existing_digest(dal, app_id, candidate.version) is not None:
                        skipped += 1  # immutable (app_id, version): never rebuild over it
                        continue
                    outcome = await build_and_ingest(
                        dal,
                        source,
                        candidate,
                        bundle_tarball_path(pack_dir, candidate),
                        config=cfg,
                        tier=tier,
                        client=client,
                        tenant_id=tenant_id,
                        tenant_slug=tenant_slug,
                        requested_by=requested_by,
                        valkey_client=valkey_client,
                    )
                except Exception as exc:  # noqa: BLE001 - isolate per bundle, fail loud
                    logger.exception(
                        "marketplace sandbox build crashed",
                        extra={"source_public_id": sid, "app_id": app_id},
                    )
                    await _fail(dal, source, f"sandbox_build:{type(exc).__name__}")
                    failed += 1
                    continue
                if outcome.outcome == OUTCOME_NEEDS_APPROVAL:
                    built += 1
                elif outcome.outcome == OUTCOME_SKIPPED_IDENTICAL:
                    skipped += 1
                elif outcome.outcome == OUTCOME_REJECTED:
                    failed += 1
                    if outcome.reason.startswith(("component_", "source_disabled")):
                        await _fail(dal, source, f"sandbox_ingest:{outcome.reason}")
                else:
                    failed += 1
    summary = SandboxRunSummary(result.examined, len(result.bundles), built, skipped, failed)
    logger.info(
        "marketplace sandbox run complete",
        extra={"source_public_id": sid, **{k: getattr(summary, k) for k in summary.__slots__}},
    )
    return summary


def _launch(
    dal: AsyncDB,
    source: SourceRecord,
    run: Callable[[], Awaitable[Any]],
    *,
    label: str,
) -> asyncio.Task[Any]:
    """Spawn `run` as a tracked background task (the shared launch + crash->ERROR scaffolding).

    Any escaped exception is logged and recorded as status ERROR so a crash cannot leave the
    source stuck in PENDING_SCAN.
    """

    async def _guarded() -> Any:
        try:
            return await run()
        except Exception:
            logger.exception(
                "marketplace %s task crashed",
                label,
                extra={"source_public_id": str(source.public_id)},
            )
            await _fail(dal, source, f"{label}_run:unexpected_error")
            return None

    task = asyncio.create_task(_guarded())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


def launch_sandbox_scan(
    dal: AsyncDB, source: SourceRecord, **kwargs: Any
) -> asyncio.Task[SandboxRunSummary | None]:
    """Spawn `scan_build_and_ingest` as a tracked background task (never blocks a request)."""
    return _launch(
        dal, source, lambda: scan_build_and_ingest(dal, source, **kwargs), label="sandbox"
    )


async def fetch_candidate_wasm(candidate: BundleCandidate) -> bytes:
    """Read a candidate's committed prebuilt artifact (scan-time temp tree is already guarded)."""
    if candidate.wasm_path is None:
        raise OSError("no prebuilt artifact")
    return await asyncio.to_thread(Path(candidate.wasm_path).read_bytes)


async def scan_and_ingest_prebuilt(
    dal: AsyncDB,
    source: SourceRecord,
    *,
    tenant_id: int,
    tenant_slug: str,
    requested_by: int | None = None,
    valkey_client: Any | None = None,
) -> IngestSummary | None:
    """Scan a PREBUILT source at its pinned SHA and ingest each bundle (no build, unapproved).

    `scan_source` already marks ERROR + audits on a rejection (returns None here); an ingest
    failure (disabled/unknown source, unexpected error) is recorded as ERROR via `_fail`.
    """
    sid = str(source.public_id)
    with tempfile.TemporaryDirectory(prefix="mktscan-") as workdir:
        try:
            result = await scan_source(
                source, workdir=Path(workdir), dal=dal, actor_id=requested_by
            )
        except ScanRejectedError as exc:
            logger.warning(
                "marketplace prebuilt scan rejected",
                extra={"source_public_id": sid, "reason": exc.reason},
            )
            return None
        try:
            summary = await ingest_candidates(
                dal,
                source,
                result,
                fetch_wasm=fetch_candidate_wasm,
                tenant_id=tenant_id,
                tenant_slug=tenant_slug,
                requested_by=requested_by,
                valkey_client=valkey_client,
            )
        except ApiError as exc:
            await _fail(dal, source, f"prebuilt_ingest:{exc.code}")
            return None
    logger.info(
        "marketplace prebuilt run complete",
        extra={"source_public_id": sid, "examined": result.examined, "created": summary.created},
    )
    return summary


def launch_prebuilt_scan(
    dal: AsyncDB, source: SourceRecord, **kwargs: Any
) -> asyncio.Task[IngestSummary | None]:
    """Spawn `scan_and_ingest_prebuilt` as a tracked background task (never blocks a request)."""
    return _launch(
        dal, source, lambda: scan_and_ingest_prebuilt(dal, source, **kwargs), label="prebuilt"
    )


async def mark_pending_scan(dal: AsyncDB, source: SourceRecord) -> SourceRecord:
    """Move an existing source to PENDING_SCAN before a background run (clears the last error)."""
    return await update_status(dal, source.public_id, SourceStatus.PENDING_SCAN)


__all__ = [
    "SandboxRunSummary",
    "fetch_candidate_wasm",
    "launch_prebuilt_scan",
    "launch_sandbox_scan",
    "mark_pending_scan",
    "scan_and_ingest_prebuilt",
    "scan_build_and_ingest",
]
