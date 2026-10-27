"""v1 `marketplace.sources` group -- global-admin CRUD for external marketplace sources.

A source is a pinned git repo that supplies bundles (design 4.1/4.2). Every route is
`platform:admin` (global admin) and gated server-side, regardless of any UI:

- PostHog flag `waddles.marketplace-sources` (default OFF): off -> 404 (the capability does not
  exist for that deployment).
- License tier via `get_tier()`: PREBUILT source = Professional, SANDBOX source = Enterprise;
  below tier -> 402 with the required tier + upgrade path. The built-in first-party source is
  never gated and is NOT creatable/deletable/approvable through this API (`is_builtin` is
  seeder/migration-only; a request carrying it is refused).
- Sandbox add requires `sandbox_trust_ack` == hash of the current versioned warning text
  (`services.marketplace_sandbox_warning`); missing/stale -> 422. Ack (user, time, text hash,
  commit SHA) is stored on the row and audit-logged.

Rescan is a bounded SYNCHRONOUS scan+ingest for Phase 1 (the scanner has hard clone/size/time
limits; the CronJob in Task 8 does background scans). Rate limiting is the app-wide
`services.rate_limiting` limiter. Responses are always DTOs -- never the row, never numeric ids.
Found and mounted by `routers/v1.py`'s auto-discovery via `BLUEPRINTS`.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlparse

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled, get_tier
from flask_core.tenancy import get_tenant_context, tenant_middleware
from flask_core.tier_catalog import TIER_ENTERPRISE, TIER_LEVELS, TIER_PROFESSIONAL
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import marketplace_source_service as svc
from services.bundle_telemetry import get_meter
from services.current_user import get_current_user_id
from services.errors import ApiError, bad_request, unprocessable
from services.marketplace_publisher_attestation import fingerprint_publisher_key
from services.marketplace_sandbox_warning import WARNING_HASH
from services.marketplace_source_ingest import ingest_candidates
from services.marketplace_source_scanner import BundleCandidate, ScanRejectedError, scan_source
from services.marketplace_source_service import BuildMode, SandboxAck, SourceRecord, SourceStatus
from services.url_guard import validate_outbound_url

logger = logging.getLogger(__name__)

FEATURE_MARKETPLACE_SOURCES = "waddles.marketplace-sources"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_CREATE_FIELDS = frozenset(
    {
        "name",
        "git_url",
        "ref",
        "build_mode",
        "commit_sha",
        "publisher_public_key",
        "sandbox_trust_ack",
    }  # fmt: skip
)

sources_bp = Blueprint("v1_marketplace_sources", __name__, url_prefix="/api/v1/marketplace/sources")


@dataclass(slots=True, frozen=True)
class SourceCreateRequest:
    """Request DTO for `POST /sources` -- `is_builtin` is deliberately not a field."""

    name: str
    git_url: str
    ref: str
    build_mode: Literal["prebuilt", "sandbox"]
    commit_sha: str
    publisher_public_key: str | None = None
    sandbox_trust_ack: str | None = None


@dataclass(slots=True, frozen=True)
class SourceDTO:
    """Response DTO: one marketplace source (public_id only; no numeric ids, no user ids)."""

    publicId: str
    name: str
    isBuiltin: bool
    gitUrl: str | None
    ref: str | None
    pinnedCommitSha: str | None
    pendingCommitSha: str | None
    buildMode: str
    publisherKeyFingerprint: str | None
    sandboxAcknowledged: bool
    sandboxAckAt: str | None
    status: str
    lastScannedAt: str | None
    lastScanError: str | None
    createdAt: str
    updatedAt: str


@dataclass(slots=True, frozen=True)
class SourceResponse:
    """Response envelope for a single source."""

    success: bool
    source: SourceDTO


@dataclass(slots=True, frozen=True)
class SourceListResponse:
    """Response envelope for the source list."""

    success: bool
    sources: list[SourceDTO]


@dataclass(slots=True, frozen=True)
class SourceRemoveResponse:
    """Outcome of `DELETE`: hard-deleted, or disabled when bundle versions still reference it."""

    success: bool
    disposition: Literal["deleted", "disabled"]


@dataclass(slots=True, frozen=True)
class ScanSummaryDTO:
    """Counts + status of one rescan+ingest run (no manifests, no per-bundle detail)."""

    success: bool
    status: str
    examined: int
    accepted: int
    rejectedAtScan: int
    created: int
    skippedIdentical: int
    refusedConflict: int
    rejectedAtIngest: int


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dto(r: SourceRecord) -> SourceDTO:
    """Map a `SourceRecord` to the response DTO (explicit field list -- never `__dict__`)."""
    return SourceDTO(
        publicId=str(r.public_id),
        name=r.name,
        isBuiltin=r.is_builtin,
        gitUrl=r.git_url,
        ref=r.ref,
        pinnedCommitSha=r.pinned_commit_sha,
        pendingCommitSha=r.pending_commit_sha,
        buildMode=r.build_mode.name.lower(),
        publisherKeyFingerprint=r.publisher_key_fingerprint,
        sandboxAcknowledged=r.sandbox_ack_at is not None,
        sandboxAckAt=_iso(r.sandbox_ack_at),
        status=r.status.name.lower(),
        lastScannedAt=_iso(r.last_scanned_at),
        lastScanError=r.last_scan_error,
        createdAt=r.created_at.isoformat(),
        updatedAt=r.updated_at.isoformat(),
    )


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _count(op: str, outcome: str) -> None:
    get_meter().create_counter("marketplace.source.api.total").add(
        1, {"op": op, "outcome": outcome}
    )


def _flag_gate[**P, R](
    fn: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R | tuple[dict[str, object], int]]]:
    """Return 404 unless the PostHog flag is on (runs before body validation)."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | tuple[dict[str, object], int]:
        ctx = get_tenant_context(request)
        if ctx is None or not await feature_enabled(
            FEATURE_MARKETPLACE_SOURCES, tenant=ctx.tenant_slug
        ):
            _count("gate", "flag_off")
            return _err(ApiError("Not found", 404, "NOT_FOUND"))
        return await fn(*args, **kwargs)

    return wrapper


async def _require_tier(mode: BuildMode) -> tuple[dict[str, object], int] | None:
    """Hard-block below the tier for `mode` (prebuilt=Pro, sandbox=Ent); None when allowed."""
    required = TIER_ENTERPRISE if mode == BuildMode.SANDBOX else TIER_PROFESSIONAL
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware ran first
    current = await get_tier(tenant=ctx.tenant_slug)
    if TIER_LEVELS.get(current, 0) >= TIER_LEVELS[required]:
        return None
    _count("gate", "tier_denied")
    logger.info(
        "marketplace source: tier denied",
        extra={"required_tier": required, "current_tier": current, "build_mode": int(mode)},
    )
    return cast(
        tuple[dict[str, object], int],
        error_response(
            f"{mode.name.lower()} marketplace sources require the {required.title()} plan; "
            "upgrade to add this source",
            402,
            "UPGRADE_REQUIRED",
            {"requiredTier": required, "currentTier": current},
        ),
    )


def _parse_id(public_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(public_id)
    except ValueError as exc:
        raise ApiError("marketplace source not found", 404, "NOT_FOUND") from exc


async def _load(public_id: str) -> SourceRecord:
    record = await svc.get_source(_install_dal(), _parse_id(public_id))
    if record is None:
        raise ApiError("marketplace source not found", 404, "NOT_FOUND")
    return record


@sources_bp.route("", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@_flag_gate
@validate_request(SourceCreateRequest)
@validate_response(SourceResponse, 201)
async def add_source(
    data: SourceCreateRequest,
) -> tuple[SourceResponse, int] | tuple[dict[str, object], int]:
    """Add an external source (tier- and ack-gated); built-in rows are never creatable here."""
    raw = await request.get_json(force=True, silent=True) or {}
    extra = sorted(set(raw) - _CREATE_FIELDS) if isinstance(raw, dict) else []
    if extra:
        _count("add", "rejected_fields")
        return _err(bad_request(f"unsupported fields: {', '.join(extra)}"))
    try:
        mode = BuildMode.PREBUILT if data.build_mode == "prebuilt" else BuildMode.SANDBOX
        if not _SHA_RE.fullmatch(data.commit_sha):
            raise bad_request("commit_sha must be a 40-hex SHA")
        parsed = urlparse(data.git_url)
        if parsed.username is not None or parsed.password is not None:
            raise bad_request("git_url must not embed credentials")
        await validate_outbound_url(data.git_url, allowed_schemes=("https",))  # https-only + SSRF
        if mode == BuildMode.PREBUILT and not data.publisher_public_key:
            raise bad_request("prebuilt sources require a publisher_public_key")
        denied = await _require_tier(mode)
        if denied is not None:
            return denied
        caller = get_current_user_id(request)
        ack: SandboxAck | None = None
        fingerprint: str | None = None
        if mode == BuildMode.SANDBOX:
            if data.sandbox_trust_ack != WARNING_HASH:
                raise unprocessable(
                    "sandbox sources require acknowledging the current trust warning"
                )
            ack = SandboxAck(
                by=caller, at=datetime.now(UTC), text_hash=WARNING_HASH, commit_sha=data.commit_sha
            )
        elif data.publisher_public_key:
            fingerprint = fingerprint_publisher_key(data.publisher_public_key)
        record = await svc.create_source(
            _install_dal(),
            name=data.name,
            git_url=data.git_url,
            ref=data.ref,
            build_mode=mode,
            created_by=caller,
            publisher_key_fingerprint=fingerprint,
            publisher_public_key=data.publisher_public_key if mode == BuildMode.PREBUILT else None,
            sandbox_ack=ack,
            pinned_commit_sha=data.commit_sha,
        )
    except ApiError as exc:
        _count("add", "error")
        return _err(exc)
    _count("add", "ok")
    logger.info(
        "marketplace source: added",
        extra={
            "source_public_id": str(record.public_id),
            "build_mode": int(mode),
            "sandbox_ack_text_hash": ack.text_hash if ack else None,
            "actor_id": caller,
        },
    )
    return SourceResponse(success=True, source=_dto(record)), 201


@sources_bp.route("", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@_flag_gate
@validate_response(SourceListResponse)
async def list_sources() -> SourceListResponse:
    """List all sources (built-in first)."""
    rows = await svc.list_sources(_install_dal())
    return SourceListResponse(success=True, sources=[_dto(r) for r in rows])


@sources_bp.route("/<public_id>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@_flag_gate
@validate_response(SourceResponse)
async def get_source(public_id: str) -> SourceResponse | tuple[dict[str, object], int]:
    """Get one source by public id."""
    try:
        return SourceResponse(success=True, source=_dto(await _load(public_id)))
    except ApiError as exc:
        return _err(exc)


@sources_bp.route("/<public_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@_flag_gate
@validate_response(SourceRemoveResponse)
async def remove_source(public_id: str) -> SourceRemoveResponse | tuple[dict[str, object], int]:
    """Remove a source: hard-delete, or disable when versions reference it. Built-in refused."""
    dal = _install_dal()
    try:
        record = await _load(public_id)
        if record.is_builtin:
            _count("remove", "builtin_refused")
            raise ApiError("the built-in marketplace source cannot be removed", 409, "CONFLICT")
        try:
            await svc.delete_source(dal, record.public_id, actor_id=get_current_user_id(request))
            disposition: Literal["deleted", "disabled"] = "deleted"
        except ApiError as exc:
            if exc.status_code != 409:
                raise
            # Disable FIRST (blocks rescan/approve), then stop its installed bundles running.
            await svc.disable_source(dal, record.public_id, actor_id=get_current_user_id(request))
            await svc.deactivate_source_bundles(
                dal, record.public_id, revoked_by=get_current_user_id(request)
            )
            disposition = "disabled"
    except ApiError as exc:
        return _err(exc)
    _count("remove", disposition)
    logger.info(
        "marketplace source: removed",
        extra={"source_public_id": public_id, "disposition": disposition},
    )
    return SourceRemoveResponse(success=True, disposition=disposition)


async def _fetch_wasm(candidate: BundleCandidate) -> bytes:
    """Read a candidate's committed prebuilt artifact (scan-time temp tree is already guarded)."""
    if candidate.wasm_path is None:
        raise OSError("no prebuilt artifact")
    return await asyncio.to_thread(Path(candidate.wasm_path).read_bytes)


@sources_bp.route("/<public_id>/rescan", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@_flag_gate
@validate_response(ScanSummaryDTO)
async def rescan_source(public_id: str) -> ScanSummaryDTO | tuple[dict[str, object], int]:
    """Bounded synchronous scan + prebuilt ingest at the pinned SHA (versions land unapproved)."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware ran first
    dal = _install_dal()
    try:
        record = await _load(public_id)
        if record.is_builtin or record.build_mode != BuildMode.PREBUILT:
            raise ApiError("only external prebuilt sources can be rescanned here", 409, "CONFLICT")
        if record.status == SourceStatus.DISABLED:
            raise ApiError("source is disabled", 409, "CONFLICT")
        denied = await _require_tier(record.build_mode)
        if denied is not None:
            return denied
        with tempfile.TemporaryDirectory(prefix="mktscan-") as workdir:
            try:
                result = await scan_source(
                    record,
                    workdir=Path(workdir),
                    dal=dal,
                    actor_id=get_current_user_id(request),
                )
            except ScanRejectedError as exc:
                _count("rescan", "scan_rejected")
                raise unprocessable(f"scan rejected: {exc.reason}") from exc
            summary = await ingest_candidates(
                dal,
                record,
                result,
                fetch_wasm=_fetch_wasm,
                tenant_id=ctx.tenant_id,
                tenant_slug=ctx.tenant_slug,
                requested_by=get_current_user_id(request),
                valkey_client=current_app.config.get("valkey_client"),
            )
        refreshed = await svc.get_source(dal, record.public_id)
    except ApiError as exc:
        return _err(exc)
    _count("rescan", "ok")
    return ScanSummaryDTO(
        success=True,
        status=(refreshed.status if refreshed else record.status).name.lower(),
        examined=result.examined,
        accepted=len(result.bundles),
        rejectedAtScan=len(result.rejected),
        created=summary.created,
        skippedIdentical=summary.skipped_identical,
        refusedConflict=summary.refused_conflict,
        rejectedAtIngest=summary.rejected,
    )


@dataclass(slots=True, frozen=True)
class ApproveUpdateRequest:
    """Body for `approve-update`: echo the reviewed pending SHA; sandbox adds a fresh trust ack."""

    pending_commit_sha: str
    sandbox_trust_ack: str | None = None


@sources_bp.route("/<public_id>/approve-update", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@_flag_gate
@validate_request(ApproveUpdateRequest)
@validate_response(SourceResponse)
async def approve_update(
    public_id: str, data: ApproveUpdateRequest
) -> SourceResponse | tuple[dict[str, object], int]:
    """Human-approve a pending update: pinned_commit_sha <- pending_commit_sha.

    Advancing the pin is privileged, so it re-applies `add`'s gates: not DISABLED (409), license
    tier (402), SANDBOX needs a fresh trust ack for the new commit (422), and the client echoes
    the pending SHA it reviewed (mismatch -> 409, enforced atomically in the UPDATE).
    """
    try:
        if not _SHA_RE.fullmatch(data.pending_commit_sha):
            raise bad_request("pending_commit_sha must be a 40-hex SHA")
        record = await _load(public_id)
        if record.status == SourceStatus.DISABLED:
            raise ApiError("source is disabled", 409, "CONFLICT")
        denied = await _require_tier(record.build_mode)
        if denied is not None:
            return denied
        caller = get_current_user_id(request)
        ack: SandboxAck | None = None
        if record.build_mode == BuildMode.SANDBOX:
            if data.sandbox_trust_ack != WARNING_HASH:
                raise unprocessable(
                    "sandbox sources require acknowledging the current trust warning"
                )
            ack = SandboxAck(
                by=caller,
                at=datetime.now(UTC),
                text_hash=WARNING_HASH,
                commit_sha=data.pending_commit_sha,
            )
        updated = await svc.approve_update(
            _install_dal(),
            record.public_id,
            expected_pending_sha=data.pending_commit_sha,
            sandbox_ack=ack,
            actor_id=caller,
        )
    except ApiError as exc:
        return _err(exc)
    _count("approve_update", "ok")
    logger.info(
        "marketplace source: update approved by admin",
        extra={
            "source_public_id": public_id,
            "actor_id": caller,
            "sandbox_ack_text_hash": ack.text_hash if ack else None,
        },
    )
    return SourceResponse(success=True, source=_dto(updated))


BLUEPRINTS: list[Blueprint] = [sources_bp]
