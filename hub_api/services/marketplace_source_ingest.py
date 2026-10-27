"""Prebuilt marketplace-source ingest + approval (Phase 1, Task 5).

Flow for every scanned PREBUILT bundle (fail-closed at each step)::

    scan -> namespace recheck -> publisher attestation (Task 4) -> same-version/digest check
         -> create_version -> process_prebuilt_component (PUBLISHED, source_id stamped)
         -> real DB-backed permission summary -> NEEDS_APPROVAL (a PUBLISHED version with no
            global install; the SYSTEM auto-install path is built-in only)
    human platform:admin -> approve_source_version: install_version_globally(source=...,
         install_source="human") -> platform Ed25519 re-sign (the signature the executor trusts)
         -> signed sidecar upload.

An unattested prebuilt NEVER yields a version row. Logs/audit carry app_id + counts only (no PII).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import yaml
from penguin_dal import AsyncDB

from services import bundle_approval_service, bundle_audit, bundle_signing_service
from services.bundle_telemetry import bundle_span, get_meter
from services.bundle_version_service import (
    STATUS_PUBLISHED,
    create_version,
    process_prebuilt_component,
)
from services.errors import ApiError, bad_request
from services.marketplace_publisher_attestation import (
    AttestationError,
    VerifiedPrebuilt,
    verify_prebuilt_attestation,
)
from services.marketplace_source_scanner import BundleCandidate, ScanResult
from services.marketplace_source_service import (
    BuildMode,
    SourceRecord,
    get_source_pk,
)
from services.vendor_bundle_authz import INSTALL_SOURCE_HUMAN, enforce_source_namespace

logger = logging.getLogger(__name__)

FetchWasm = Callable[[BundleCandidate], Awaitable[bytes]]

OUTCOME_NEEDS_APPROVAL = "needs_approval"
OUTCOME_SKIPPED_IDENTICAL = "skipped_identical"
OUTCOME_REFUSED_CONFLICT = "refused_conflict"
OUTCOME_REJECTED = "rejected"


@dataclass(slots=True, frozen=True)
class IngestedBundle:
    """Per-bundle ingest outcome; summary/hash are set only when a version exists."""

    app_id: str
    version: str
    outcome: str
    reason: str = ""
    permission_summary: dict[str, Any] | None = None
    permission_hash: str | None = None
    key_fingerprint: str | None = None


@dataclass(slots=True, frozen=True)
class IngestSummary:
    """Counts + per-bundle results of one ingest run."""

    created: int = 0
    skipped_identical: int = 0
    refused_conflict: int = 0
    rejected: int = 0
    bundles: tuple[IngestedBundle, ...] = field(default=())


def _reject(c: BundleCandidate, reason: str) -> IngestedBundle:
    """Build a rejected outcome and count it."""
    get_meter().create_counter("marketplace.ingest.total").add(1, {"outcome": reason})
    logger.warning(
        "marketplace ingest rejected bundle", extra={"app_id": c.assigned_app_id, "reason": reason}
    )
    return IngestedBundle(c.assigned_app_id, c.version, OUTCOME_REJECTED, reason)


async def _existing_digest(install_dal: AsyncDB, app_id: str, version: str) -> str | None:
    """Return the digest of an already-ingested (app_id, version), or None."""
    rows = await install_dal(
        (install_dal.app_versions.app_id == app_id) & (install_dal.app_versions.version == version)
    ).select()
    row = rows.first()
    return None if row is None else str(row.artifact_digest)


async def _ingest_one(
    install_dal: AsyncDB,
    source: SourceRecord,
    source_pk: int,
    candidate: BundleCandidate,
    wasm: bytes,
    *,
    tenant_id: int,
    tenant_slug: str,
    requested_by: int | None,
    valkey_client: Any | None,
) -> IngestedBundle:
    """Attest + version one prebuilt candidate (the caller has already fetched `wasm`)."""
    app_id = candidate.assigned_app_id
    try:
        enforce_source_namespace(app_id=app_id, source=source)  # defense in depth
    except ApiError:
        return _reject(candidate, "namespace_violation")
    try:
        verified: VerifiedPrebuilt = verify_prebuilt_attestation(candidate, source, wasm)
    except AttestationError as exc:
        return _reject(candidate, f"attestation_{exc.reason}")

    existing = await _existing_digest(install_dal, app_id, candidate.version)
    if existing is not None:
        if existing.removeprefix("sha256:") == verified.digest:
            return IngestedBundle(
                app_id, candidate.version, OUTCOME_SKIPPED_IDENTICAL, key_fingerprint=None
            )
        get_meter().create_counter("marketplace.ingest.total").add(1, {"outcome": "conflict"})
        logger.warning(
            "marketplace ingest refused same-version different-digest",
            extra={"app_id": app_id, "version": candidate.version},
        )
        return IngestedBundle(
            app_id, candidate.version, OUTCOME_REFUSED_CONFLICT, "digest_differs_for_version"
        )

    await create_version(
        install_dal,
        tenant_id=tenant_id,
        app_id=app_id,
        requested_by=requested_by,
        manifest_bytes=yaml.safe_dump(candidate.raw_manifest).encode(),
        source_bytes=None,
        component_bytes=wasm,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )
    row = await process_prebuilt_component(
        install_dal,
        app_id=app_id,
        version=candidate.version,
        component_bytes=wasm,
        tenant_slug=tenant_slug,
        valkey_client=valkey_client,
        source_id=source_pk,
    )
    if row.status != STATUS_PUBLISHED:
        return _reject(candidate, f"component_{row.reject_reason or 'not_published'}")

    summary, permission_hash = await bundle_approval_service.get_permission_summary(
        install_dal, app_id=app_id, version=candidate.version
    )
    await bundle_audit.record(
        install_dal,
        actor_id=requested_by,
        action="marketplace_prebuilt_attested",
        target_type="app_versions",
        target_id=f"{app_id}@{candidate.version}",
        details={
            "source_public_id": str(source.public_id),
            "publisher_key_fingerprint": verified.key_fingerprint,
            "wasm_sha256": verified.digest,
            "permission_hash": permission_hash,
        },
    )
    get_meter().create_counter("marketplace.ingest.total").add(1, {"outcome": "needs_approval"})
    logger.info(
        "marketplace ingest: version awaiting human approval",
        extra={"app_id": app_id, "version": candidate.version},
    )
    return IngestedBundle(
        app_id,
        candidate.version,
        OUTCOME_NEEDS_APPROVAL,
        permission_summary=summary,
        permission_hash=permission_hash,
        key_fingerprint=verified.key_fingerprint,
    )


async def ingest_candidates(
    install_dal: AsyncDB,
    source: SourceRecord,
    result: ScanResult,
    *,
    fetch_wasm: FetchWasm,
    tenant_id: int,
    tenant_slug: str,
    requested_by: int | None = None,
    valkey_client: Any | None = None,
) -> IngestSummary:
    """Ingest every valid bundle of a PREBUILT-source scan as an unapproved PUBLISHED version.

    Never approves, signs or installs. Refuses (raises 400) a non-PREBUILT or built-in source
    (the SYSTEM seeder path owns built-ins; sandbox builds feed `ingest` via Task 7 later).
    Raises `ApiError` 404 if the source row is unknown. A failing bundle is rejected/counted
    without aborting its siblings.
    """
    if source.is_builtin or source.build_mode != BuildMode.PREBUILT:
        raise bad_request("prebuilt ingest only applies to external PREBUILT sources")
    source_pk = await get_source_pk(install_dal, source.public_id)
    if source_pk is None:
        raise ApiError("marketplace source not found", 404, "source_not_found")
    out: list[IngestedBundle] = []
    async with bundle_span("marketplace.source.ingest", source_public_id=str(source.public_id)):
        for candidate in result.bundles:
            try:
                wasm = await fetch_wasm(candidate)
            except (OSError, ApiError) as exc:
                logger.warning(
                    "marketplace ingest: artifact unavailable",
                    extra={"app_id": candidate.assigned_app_id, "error": type(exc).__name__},
                )
                out.append(_reject(candidate, "artifact_unavailable"))
                continue
            out.append(
                await _ingest_one(
                    install_dal,
                    source,
                    source_pk,
                    candidate,
                    wasm,
                    tenant_id=tenant_id,
                    tenant_slug=tenant_slug,
                    requested_by=requested_by,
                    valkey_client=valkey_client,
                )
            )

    def count(outcome: str) -> int:
        return sum(1 for b in out if b.outcome == outcome)

    return IngestSummary(
        created=count(OUTCOME_NEEDS_APPROVAL),
        skipped_identical=count(OUTCOME_SKIPPED_IDENTICAL),
        refused_conflict=count(OUTCOME_REFUSED_CONFLICT),
        rejected=count(OUTCOME_REJECTED),
        bundles=tuple(out),
    )


async def approve_source_version(
    install_dal: AsyncDB,
    source: SourceRecord,
    *,
    app_id: str,
    version: str,
    approved_by: int,
    expected_permission_hash: str | None,
) -> dict[str, Any]:
    """Human-approve an ingested external version: install, then platform re-sign.

    `approved_by` is the concrete platform:admin (never None/SYSTEM). The platform signing key
    is checked BEFORE installing so a missing key never leaves an installed-but-unsigned
    version; if signing/sidecar upload still fails after install, the install is revoked
    (fail-closed) and the error re-raised. The signature binds the global-install id as
    `approval_id`, exactly the payload the executor verifies (`build_signing_payload`).
    """
    bundle_signing_service.PlatformSigner.from_env()  # fail before any state change
    versions = await install_dal(
        (install_dal.app_versions.app_id == app_id) & (install_dal.app_versions.version == version)
    ).select()
    vrow = versions.first()
    if vrow is None:
        raise ApiError(f"version {version} of {app_id} not found", 404, "version_not_found")
    source_pk = await get_source_pk(install_dal, source.public_id)
    if source_pk is None or vrow.source_id != source_pk:
        raise ApiError("version does not belong to this source", 403, "FORBIDDEN")
    install = await bundle_approval_service.install_version_globally(
        install_dal,
        app_id=app_id,
        version=version,
        installed_by=approved_by,
        expected_permission_hash=expected_permission_hash,
        install_source=INSTALL_SOURCE_HUMAN,
        source=source,
    )
    versions_table = install_dal.metadata.tables["app_versions"]
    try:
        async with install_dal.engine.begin() as conn:
            signing = await bundle_signing_service.sign_and_record_version(
                conn,
                app_versions_table=versions_table,
                version_id=int(vrow.id),
                app_id=app_id,
                version=version,
                approval_id=int(install.id),
            )
        await bundle_signing_service.upload_signed_sidecar(**signing)
    except Exception:
        logger.error(
            "marketplace approve: re-sign failed; revoking install",
            extra={"app_id": app_id, "version": version},
        )
        await bundle_approval_service.uninstall_globally(
            install_dal, app_id=app_id, revoked_by=approved_by
        )
        raise
    get_meter().create_counter("marketplace.approve.total").add(1, {"outcome": "signed"})
    return signing


__all__ = [
    "IngestSummary",
    "IngestedBundle",
    "approve_source_version",
    "ingest_candidates",
]
