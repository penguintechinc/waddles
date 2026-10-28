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

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import yaml
from penguin_dal import AsyncDB
from sqlalchemy import select
from sqlalchemy import update as sa_update

from services import bundle_approval_service, bundle_audit, bundle_signing_service
from services import marketplace_source_telemetry as mkt_telemetry
from services.bundle_version_service import (
    STATUS_PUBLISHED,
    create_version,
    process_prebuilt_component,
)
from services.errors import ApiError, bad_request, conflict
from services.marketplace_publisher_attestation import (
    AttestationError,
    VerifiedPrebuilt,
    verify_prebuilt_attestation,
)
from services.marketplace_source_scanner import BundleCandidate, ScanResult
from services.marketplace_source_service import (
    ACTION_SIGNATURE_ISSUED,
    ACTION_VERSION_APPROVED,
    BuildMode,
    SourceRecord,
    SourceStatus,
    audit_source_event,
    get_source,
    get_source_by_pk,
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
    mkt_telemetry.record_ingest(reason)
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
    except ApiError as exc:
        logger.warning(
            "marketplace ingest: bundle rejected",
            extra={
                "source_public_id": str(source.public_id),
                "reason": "namespace_violation",
                "status": exc.status_code,
            },
        )
        return _reject(candidate, "namespace_violation")
    try:
        verified: VerifiedPrebuilt = verify_prebuilt_attestation(candidate, source, wasm)
    except AttestationError as exc:
        logger.warning(
            "marketplace ingest: bundle rejected",
            extra={
                "source_public_id": str(source.public_id),
                "reason": f"attestation_{exc.reason}",
            },
        )
        return _reject(candidate, f"attestation_{exc.reason}")

    existing = await _existing_digest(install_dal, app_id, candidate.version)
    if existing is not None:
        if existing.removeprefix("sha256:") == verified.digest:
            return IngestedBundle(
                app_id, candidate.version, OUTCOME_SKIPPED_IDENTICAL, key_fingerprint=None
            )
        mkt_telemetry.record_ingest("conflict")
        logger.warning(
            "marketplace ingest refused same-version different-digest",
            extra={"app_id": app_id, "version": candidate.version},
        )
        return IngestedBundle(
            app_id, candidate.version, OUTCOME_REFUSED_CONFLICT, "digest_differs_for_version"
        )

    return await create_unapproved_version(
        install_dal,
        source,
        source_pk,
        candidate,
        wasm,
        audit_action="marketplace_prebuilt_attested",
        audit_details={"publisher_key_fingerprint": verified.key_fingerprint},
        key_fingerprint=verified.key_fingerprint,
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        requested_by=requested_by,
        valkey_client=valkey_client,
    )


async def create_unapproved_version(
    install_dal: AsyncDB,
    source: SourceRecord,
    source_pk: int,
    candidate: BundleCandidate,
    wasm: bytes,
    *,
    audit_action: str,
    audit_details: dict[str, Any],
    key_fingerprint: str | None,
    tenant_id: int,
    tenant_slug: str,
    requested_by: int | None,
    valkey_client: Any | None,
) -> IngestedBundle:
    """Create the PUBLISHED-but-NOT-installed version for already-trusted `wasm` bytes.

    Shared tail of the prebuilt path (post-attestation) and the sandbox-build path (the platform
    built the bytes, so there is no publisher attestation). Never approves, signs or installs.
    """
    app_id = candidate.assigned_app_id
    current = await get_source(install_dal, source.public_id)
    if current is None or current.status == SourceStatus.DISABLED:
        return _reject(candidate, "source_disabled")  # re-check: disabled mid-ingest

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
        action=audit_action,
        target_type="app_versions",
        target_id=f"{app_id}@{candidate.version}",
        details={
            "source_public_id": str(source.public_id),
            "wasm_sha256": hashlib.sha256(wasm).hexdigest(),
            "permission_hash": permission_hash,
            **audit_details,
        },
    )
    mkt_telemetry.record_ingest("needs_approval")
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
        key_fingerprint=key_fingerprint,
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
    fresh = await get_source(install_dal, source.public_id)
    if fresh is None or fresh.status == SourceStatus.DISABLED:
        raise conflict("marketplace source is disabled")
    out: list[IngestedBundle] = []
    async with mkt_telemetry.source_span("marketplace.source.ingest", source.public_id):
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


async def resolve_approval_source(
    install_dal: AsyncDB, *, app_id: str, version: str
) -> SourceRecord | None:
    """Return the EXTERNAL source a version belongs to, or `None` for the legacy path.

    The single decision point that makes the generic approve route source-aware: a version with
    a non-null, non-built-in `source_id` MUST be approved through `approve_source_version` (all
    marketplace gates). `None` (no version row, `source_id` NULL, or the built-in source) keeps
    the legacy `install_version_globally` path. A `source_id` whose source row is missing is an
    integrity error and fails closed rather than falling through to the ungated legacy path.
    """
    rows = await install_dal(
        (install_dal.app_versions.app_id == app_id) & (install_dal.app_versions.version == version)
    ).select()
    vrow = rows.first()
    if vrow is None or vrow.source_id is None:
        return None
    source = await get_source_by_pk(install_dal, int(vrow.source_id))
    if source is None:
        raise ApiError(
            f"version {version} of {app_id} references an unknown marketplace source",
            409,
            "unknown_source",
        )
    return None if source.is_builtin else source


async def approve_source_version(
    install_dal: AsyncDB,
    source: SourceRecord,
    *,
    app_id: str,
    version: str,
    approved_by: int,
    expected_permission_hash: str | None,
) -> dict[str, Any]:
    """Human-approve one ingested version under an approve span (see `_approve_inner`)."""
    async with mkt_telemetry.source_span("marketplace.source.approve", source.public_id):
        return await _approve_inner(
            install_dal,
            source,
            app_id=app_id,
            version=version,
            approved_by=approved_by,
            expected_permission_hash=expected_permission_hash,
        )


async def _rollback_install(
    install_dal: AsyncDB, *, new_install_id: int, version_id: int, revoked_by: int
) -> None:
    """Undo ONE failed approval atomically, keeping <=1 current install per app.

    Single txn. The app's install rows are locked (`FOR UPDATE`) so a concurrent approval of
    the same app cannot interleave. The new install is revoked; the install it superseded is
    restored as current ONLY if the new row was still current (nothing superseded it since) and
    that prior row is not itself revoked -- otherwise a newer approval (B) already owns "current"
    and clearing `superseded_by` would leave two current rows or resurrect a revoked one.
    The version row is made consistent: `approval_id` returns to the restored prior install only
    when the stored signature still binds that prior approval; signature fields that bind the
    revoked approval are cleared. Tenants/communities are untouched (unlike `uninstall_globally`).
    """
    installs = install_dal.metadata.tables["app_global_installs"]
    versions_t = install_dal.metadata.tables["app_versions"]
    async with install_dal.engine.begin() as conn:
        new_row = (
            await conn.execute(select(installs).where(installs.c.id == new_install_id))
        ).first()
        if new_row is None:
            raise RuntimeError("rollback target install row is missing")
        # Lock every row of this app (deterministic id order) before deciding anything.
        await conn.execute(
            select(installs.c.id)
            .where(installs.c.app_id == new_row.app_id)
            .order_by(installs.c.id)
            .with_for_update()
        )
        await conn.execute(
            sa_update(installs)
            .where(installs.c.id == new_install_id)
            .values(revoked_at=datetime.now(UTC), revoked_by=revoked_by)
        )
        restored_id: int | None = None
        still_current = new_row.superseded_by is None
        if still_current:
            prior = (
                await conn.execute(
                    select(installs.c.id)
                    .where(
                        (installs.c.superseded_by == new_install_id)
                        & installs.c.revoked_at.is_(None)
                    )
                    .order_by(installs.c.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if prior is not None:
                await conn.execute(
                    sa_update(installs).where(installs.c.id == prior).values(superseded_by=None)
                )
                restored_id = int(prior)
        else:
            logger.warning(
                "marketplace rollback: install already superseded; not restoring prior",
                extra={"install_id": new_install_id},
            )
        vrow = (await conn.execute(select(versions_t).where(versions_t.c.id == version_id))).first()
        if vrow is None or vrow.approval_id != new_install_id:
            return
        sig_binds_prior = (
            restored_id is not None and vrow.artifact_signed_approval_id == restored_id
        )
        values: dict[str, Any] = {"approval_id": restored_id if sig_binds_prior else None}
        if not sig_binds_prior:
            values.update(
                artifact_signature=None,
                artifact_signature_key_id=None,
                artifact_signed_approval_id=None,
                artifact_signed_at=None,
            )
        await conn.execute(
            sa_update(versions_t)
            .where((versions_t.c.id == version_id) & (versions_t.c.approval_id == new_install_id))
            .values(**values)
        )


async def _approve_inner(
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
    version; if signing/sidecar upload still fails after install, the NEW install is revoked and
    the previously-good install is restored as current (`_rollback_install`) -- never a global
    uninstall of the app -- and the error re-raised. The signature binds the global-install id as
    `approval_id`, exactly the payload the executor verifies (`build_signing_payload`).
    """
    if not expected_permission_hash:
        # An external version is never approved blind: the admin must be bound to the exact
        # permission set they reviewed (review/approve TOCTOU), same as the consent hash check.
        raise ApiError(
            "an external marketplace version requires the reviewed permissionHash",
            422,
            "permission_hash_required",
        )
    bundle_signing_service.PlatformSigner.from_env()  # fail before any state change
    versions = await install_dal(
        (install_dal.app_versions.app_id == app_id) & (install_dal.app_versions.version == version)
    ).select()
    vrow = versions.first()
    if vrow is None:
        raise ApiError(f"version {version} of {app_id} not found", 404, "version_not_found")
    fresh = await get_source(install_dal, source.public_id)
    if fresh is None or fresh.status == SourceStatus.DISABLED:
        raise conflict("marketplace source is disabled")
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
        await _rollback_install(
            install_dal,
            new_install_id=int(install.id),
            version_id=int(vrow.id),
            revoked_by=approved_by,
        )
        raise
    mkt_telemetry.record_approval(outcome="signed")
    await audit_source_event(
        install_dal,
        ACTION_VERSION_APPROVED,
        source.public_id,
        actor_id=approved_by,
        details={"app_id": app_id, "version": version, "permission_hash": expected_permission_hash},
    )
    await audit_source_event(
        install_dal,
        ACTION_SIGNATURE_ISSUED,
        source.public_id,
        actor_id=approved_by,
        details={"app_id": app_id, "version": version, "approval_id": int(install.id)},
    )
    return signing


__all__ = [
    "IngestSummary",
    "IngestedBundle",
    "approve_source_version",
    "create_unapproved_version",
    "ingest_candidates",
    "resolve_approval_source",
]
