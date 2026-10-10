"""v1 `compliance.audit` group -- read, verify and export the tamper-evident audit log.

Mounted at ``/api/v1/compliance/audit`` (GRC audit finding #3). Every route:

``tenant_middleware`` (tenant from the caller's OWN JWT) -> ``require_scope(
"compliance.audit:admin")`` -> Enterprise entitlement (``feature_enabled``) -> handler.

* The scope is ``:admin``, deliberately not ``:read``: every session carries the ``*:read``
  wildcard, so a ``compliance.audit:read`` requirement would be satisfied by any logged-in user.
* A caller only ever sees the chain of the tenant in their own token. The platform chain
  (``?chain=platform``) additionally requires the platform-only ``users:admin`` scope.
* ``GET /events`` and ``GET /head`` and ``GET /verify`` need ``waddles.compliance.audit_logs``;
  ``GET /export`` additionally needs ``waddles.compliance.audit_export`` -- both Enterprise.
  An un-entitled tenant gets 402, never a partial answer.
* Exporting is itself audited, and **before** any data leaves: if the export event cannot be
  recorded the request fails (500) and nothing is disclosed.
* Statutory per-user rights (DSAR/erasure) are NOT here and are never tier-gated; this group is
  the *admin* compliance tooling.

Responses are explicit DTOs (``@validate_response``), never raw rows. Tamper detection is a 409:
``/verify`` answers ``intact``/``empty`` with 200 and ``broken`` with 409, so a ``curl -f`` cron
cannot mistake a broken chain for success.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from flask_core.authz import get_authz_decision, has_required_scopes, require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services.audit_chain import ChainRecord, ChainStatus, canonical_timestamp
from services.audit_events import (
    PLATFORM_CHAIN_ID,
    ActorKind,
    AuditAction,
    AuditCategory,
    AuditEvent,
    AuditOutcome,
)
from services.audit_service import (
    DEFAULT_VERIFY_MAX_RECORDS,
    FEATURE_AUDIT_EXPORT,
    FEATURE_AUDIT_LOGS,
    MAX_EXPORT_LIMIT,
    MAX_LIST_LIMIT,
    AuditError,
    AuditService,
    AuditWriteError,
    ListFilters,
)
from services.community_common import api_error
from services.current_user import get_current_user_id
from services.errors import ApiError

compliance_audit_bp = Blueprint(
    "v1_compliance_audit", __name__, url_prefix="/api/v1/compliance/audit"
)

logger = logging.getLogger(__name__)

_SCOPE = "compliance.audit:admin"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ACTION_FILTER = re.compile(r"^[a-z][a-z0-9_.]{1,99}$")


@dataclass(slots=True, frozen=True)
class AuditEventDTO:
    """One audit record, including the hashes an auditor needs to re-verify it offline."""

    seq: int
    event_id: str
    occurred_at: str
    actor_uuid: str | None
    actor_kind: str
    category: str
    action: str
    outcome: str
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    prev_hash: str
    record_hash: str
    hash_version: str


@dataclass(slots=True, frozen=True)
class PaginationDTO:
    """Page metadata for list responses."""

    page: int
    limit: int
    total: int
    total_pages: int


@dataclass(slots=True, frozen=True)
class AuditEventsResponse:
    """`GET /events` -- newest-first page of the caller's chain."""

    success: bool
    chain_id: str
    events: list[AuditEventDTO]
    pagination: PaginationDTO


@dataclass(slots=True, frozen=True)
class AuditHeadDTO:
    """The newest record of a chain -- pin this off-box to make tail truncation detectable."""

    seq: int
    record_hash: str
    occurred_at: str


@dataclass(slots=True, frozen=True)
class AuditHeadResponse:
    """`GET /head`; ``head`` is null for a chain with no records yet."""

    success: bool
    chain_id: str
    hash_version: str
    head: AuditHeadDTO | None


@dataclass(slots=True, frozen=True)
class ChainBreakDTO:
    """The first inconsistency found by a verification."""

    seq: int | None
    reason: str
    detail: str


@dataclass(slots=True, frozen=True)
class AuditVerifyResponse:
    """`GET /verify`: ``status`` is intact | empty | broken; only ``intact`` is ``ok``."""

    success: bool
    chain_id: str
    status: str
    ok: bool
    examined: int
    complete: bool
    head_seq: int | None
    head_hash: str | None
    next_seq: int | None
    anchor_hash: str | None
    chain_break: ChainBreakDTO | None
    verified_at: str


@dataclass(slots=True, frozen=True)
class ExportManifestDTO:
    """What an offline verifier needs to check an export slice (``make verify-audit-export``)."""

    count: int
    first_seq: int | None
    last_seq: int | None
    first_prev_hash: str | None
    last_hash: str | None
    has_more: bool
    next_after_seq: int | None
    exported_at: str


@dataclass(slots=True, frozen=True)
class AuditExportResponse:
    """`GET /export` -- an ascending slice of the chain plus its verification manifest."""

    success: bool
    chain_id: str
    hash_version: str
    records: list[AuditEventDTO]
    manifest: ExportManifestDTO


def _dto(record: ChainRecord) -> AuditEventDTO:
    """Project a chain record onto its wire DTO (explicit field list, no ``**row``)."""
    return AuditEventDTO(
        seq=record.seq,
        event_id=record.event_id,
        occurred_at=record.occurred_at,
        actor_uuid=record.actor_uuid,
        actor_kind=record.actor_kind,
        category=record.category,
        action=record.action,
        outcome=record.outcome,
        target_type=record.target_type,
        target_id=record.target_id,
        details=dict(record.details),
        prev_hash=record.prev_hash,
        record_hash=record.record_hash,
        hash_version=record.hash_version,
    )


def _service() -> AuditService:
    """The app's audit service (published by ``app.py::startup``)."""
    service = current_app.config.get("audit_service")
    if not isinstance(service, AuditService):
        raise AuditError("audit service is not initialised")
    return service


def _parse_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Parse a bounded integer query param; garbage is a 400 (via ``ValueError``)."""
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _parse_when(name: str) -> datetime | None:
    """Parse an optional ISO-8601 timestamp query param (naive values are taken as UTC)."""
    raw = request.args.get(name)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _chain_for_request() -> tuple[str | None, tuple[dict[str, object], int] | None]:
    """The chain the caller may read: their tenant's, or the platform chain for super-admins."""
    ctx = get_tenant_context(request)
    if ctx is None:  # pragma: no cover - tenant_middleware guarantees a context
        return None, api_error("Tenant context unavailable", 403)
    if request.args.get("chain") == "platform":
        decision = get_authz_decision(request)
        if decision is None or not has_required_scopes(decision.granted, ("users:admin",)):
            return None, api_error("The platform audit chain requires platform administration", 403)
        return PLATFORM_CHAIN_ID, None
    return f"tenant:{ctx.tenant_id}", None


async def _entitled(*flags: str) -> bool:
    """True only if every listed Enterprise flag is on for the caller's tenant."""
    ctx = get_tenant_context(request)
    if ctx is None:  # pragma: no cover - tenant_middleware guarantees a context
        return False
    for flag in flags:
        if not await feature_enabled(flag, tenant=ctx.tenant_slug):
            return False
    return True


_NOT_ENTITLED = "Tamper-evident audit logging is an Enterprise feature"


@compliance_audit_bp.route("/events", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(_SCOPE)  # type: ignore[untyped-decorator]
@validate_response(AuditEventsResponse)
async def list_events() -> AuditEventsResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/compliance/audit/events` -- filter by category/action/outcome/actor/time."""
    if not await _entitled(FEATURE_AUDIT_LOGS):
        return api_error(_NOT_ENTITLED, 402)
    chain_id, error = _chain_for_request()
    if error is not None or chain_id is None:
        return error or api_error("Forbidden", 403)
    try:
        page = _parse_int("page", 1, minimum=1, maximum=1_000_000)
        limit = _parse_int("limit", 50, minimum=1, maximum=MAX_LIST_LIMIT)
        category = request.args.get("category")
        if category is not None and category not in {c.value for c in AuditCategory}:
            raise ValueError("category is not a known audit category")
        outcome = request.args.get("outcome")
        if outcome is not None and outcome not in {o.value for o in AuditOutcome}:
            raise ValueError("outcome must be success, denied or failure")
        action = request.args.get("action")
        if action is not None and not _ACTION_FILTER.match(action):
            raise ValueError("action must be a snake_case/dotted identifier")
        actor_raw = request.args.get("actor")
        actor = uuid.UUID(actor_raw) if actor_raw else None
        filters = ListFilters(
            category=category,
            action=action,
            outcome=outcome,
            actor_uuid=actor,
            since=_parse_when("since"),
            until=_parse_when("until"),
        )
    except ValueError as exc:
        # The message is one of this module's static validation strings (never the raw value).
        logger.debug("compliance.audit list_events: rejected query parameters: %s", exc)
        return api_error(f"Invalid query: {exc}", 400)
    records, total = await _service().list_events(chain_id, filters=filters, page=page, limit=limit)
    return AuditEventsResponse(
        success=True,
        chain_id=chain_id,
        events=[_dto(r) for r in records],
        pagination=PaginationDTO(
            page=page, limit=limit, total=total, total_pages=-(-total // limit) if total else 0
        ),
    )


@compliance_audit_bp.route("/head", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(_SCOPE)  # type: ignore[untyped-decorator]
@validate_response(AuditHeadResponse)
async def get_head() -> AuditHeadResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/compliance/audit/head` -- the anchor to pin outside the database."""
    if not await _entitled(FEATURE_AUDIT_LOGS):
        return api_error(_NOT_ENTITLED, 402)
    chain_id, error = _chain_for_request()
    if error is not None or chain_id is None:
        return error or api_error("Forbidden", 403)
    head = await _service().head(chain_id)
    return AuditHeadResponse(
        success=True,
        chain_id=chain_id,
        hash_version="sha256-v1",
        head=None
        if head is None
        else AuditHeadDTO(seq=head.seq, record_hash=head.record_hash, occurred_at=head.occurred_at),
    )


@compliance_audit_bp.route("/verify", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(_SCOPE)  # type: ignore[untyped-decorator]
@validate_response(AuditVerifyResponse, 200)
@validate_response(AuditVerifyResponse, 409)
async def verify_chain() -> tuple[AuditVerifyResponse, int] | tuple[dict[str, object], int]:
    """`GET /api/v1/compliance/audit/verify` -- recompute the whole chain (409 if tampered).

    Optional pins: ``expected_head_seq`` / ``expected_head_hash`` (from an earlier ``/head``)
    make tail truncation detectable; ``from_seq`` + ``anchor_hash`` resume a long verification.
    """
    if not await _entitled(FEATURE_AUDIT_LOGS):
        return api_error(_NOT_ENTITLED, 402)
    chain_id, error = _chain_for_request()
    if error is not None or chain_id is None:
        return error or api_error("Forbidden", 403)
    try:
        from_seq = _parse_int("from_seq", 1, minimum=1, maximum=2**62)
        pinned_seq = request.args.get("expected_head_seq")
        expected_seq = (
            _parse_int("expected_head_seq", 0, minimum=1, maximum=2**62) if pinned_seq else None
        )
        max_records = _parse_int(
            "max_records",
            DEFAULT_VERIFY_MAX_RECORDS,
            minimum=1,
            maximum=DEFAULT_VERIFY_MAX_RECORDS,
        )
        anchor = request.args.get("anchor_hash")
        expected_hash = request.args.get("expected_head_hash")
        for label, value in (("anchor_hash", anchor), ("expected_head_hash", expected_hash)):
            if value is not None and not _HEX64.match(value):
                raise ValueError(f"{label} must be a 64-char lowercase hex SHA-256")
        if from_seq > 1 and not anchor:
            raise ValueError("anchor_hash is required when from_seq > 1")
    except ValueError as exc:
        # The message is one of this module's static validation strings (never the raw value).
        logger.debug("compliance.audit verify_chain: rejected query parameters: %s", exc)
        return api_error(f"Invalid query: {exc}", 400)
    report = await _service().verify(
        chain_id,
        from_seq=from_seq,
        anchor_hash=anchor,
        expected_head_seq=expected_seq,
        expected_head_hash=expected_hash,
        max_records=max_records,
    )
    result = report.verification
    broken = result.status is ChainStatus.BROKEN
    body = AuditVerifyResponse(
        success=not broken,
        chain_id=chain_id,
        status=result.status.value,
        ok=result.ok,
        examined=result.examined,
        complete=report.complete,
        head_seq=result.head_seq,
        head_hash=result.head_hash,
        next_seq=report.next_seq,
        anchor_hash=report.anchor_hash,
        chain_break=None
        if result.break_ is None
        else ChainBreakDTO(
            seq=result.break_.seq, reason=result.break_.reason.value, detail=result.break_.detail
        ),
        verified_at=report.verified_at,
    )
    return body, 409 if broken else 200


@compliance_audit_bp.route("/export", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(_SCOPE)  # type: ignore[untyped-decorator]
@validate_response(AuditExportResponse)
async def export_chain() -> (
    tuple[AuditExportResponse, int, dict[str, str]] | tuple[dict[str, object], int]
):
    """`GET /api/v1/compliance/audit/export` -- ascending slice + manifest (Enterprise export).

    Page with ``after_seq`` (the previous manifest's ``next_after_seq``). The export event is
    recorded first; if that fails nothing is returned.
    """
    if not await _entitled(FEATURE_AUDIT_LOGS, FEATURE_AUDIT_EXPORT):
        return api_error("Audit log export is an Enterprise feature", 402)
    chain_id, error = _chain_for_request()
    if error is not None or chain_id is None:
        return error or api_error("Forbidden", 403)
    try:
        after_seq = _parse_int("after_seq", 0, minimum=0, maximum=2**62)
        limit = _parse_int("limit", 500, minimum=1, maximum=MAX_EXPORT_LIMIT)
        user_id = get_current_user_id(request)
    except ValueError as exc:
        # The message is one of this module's static validation strings (never the raw value).
        logger.debug("compliance.audit export_chain: rejected query parameters: %s", exc)
        return api_error(f"Invalid query: {exc}", 400)
    except ApiError as exc:
        logger.debug(
            "compliance.audit export_chain: caller identity refused: status=%s", exc.status_code
        )
        return api_error(exc.message, exc.status_code)

    ctx = get_tenant_context(request)
    if ctx is None:  # pragma: no cover - tenant_middleware guarantees a context
        return api_error("Tenant context unavailable", 403)
    service = _service()
    try:
        recorded = await service.record(
            AuditEvent(
                category=AuditCategory.AUDIT,
                action=AuditAction.AUDIT_EXPORTED,
                outcome=AuditOutcome.SUCCESS,
                actor_kind=ActorKind.USER,
                actor_user_id=user_id,
                tenant_id=ctx.tenant_id,
                tenant_slug=ctx.tenant_slug,
                target_type="audit_chain",
                target_id=chain_id,
                details={"after_seq": after_seq, "limit": limit},
            )
        )
    except AuditWriteError:
        # The write failure itself is already logged at ERROR and counted by the service; this
        # records the consequence for the operator: the export was refused, nothing was disclosed.
        logger.warning(
            "compliance.audit export_chain: export refused, its audit event was not recorded "
            "(chain=%s)",
            chain_id,
        )
        return api_error(
            "The export could not be recorded in the audit log; nothing was exported", 500
        )
    if recorded is None:
        return api_error(_NOT_ENTITLED, 402)

    # One extra row tells us whether another page exists without a second query.
    window = await service.read_range(chain_id, after_seq=after_seq, limit=limit + 1)
    has_more = len(window) > limit
    records = window[:limit]
    manifest = ExportManifestDTO(
        count=len(records),
        first_seq=records[0].seq if records else None,
        last_seq=records[-1].seq if records else None,
        first_prev_hash=records[0].prev_hash if records else None,
        last_hash=records[-1].record_hash if records else None,
        has_more=has_more,
        next_after_seq=records[-1].seq if records and has_more else None,
        exported_at=canonical_timestamp(datetime.now(UTC)),
    )
    body = AuditExportResponse(
        success=True,
        chain_id=chain_id,
        hash_version="sha256-v1",
        records=[_dto(r) for r in records],
        manifest=manifest,
    )
    filename = f"waddles-audit-{chain_id.replace(':', '-')}-{after_seq}.json"
    return body, 200, {"Content-Disposition": f'attachment; filename="{filename}"'}


BLUEPRINTS: list[Blueprint] = [compliance_audit_bp]
