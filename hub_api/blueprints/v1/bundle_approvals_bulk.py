"""v1 bulk bundle approvals -- `POST /api/v1/apps/approvals/bulk` (GLOBAL tier, `platform:admin`).

One request approves or rejects up to `MAX_BULK_ITEMS` `(appId, version)` pairs. Every item
runs the SAME service call the single `post_approve` / `post_deny` handlers use
(`svc.install_version_globally` / `svc.deny_version`), each in its own transaction, so one
failing item never aborts the others. The HTTP status is 200 for any partial success; the
per-item `status` and the `summary` carry the outcome (207-style semantics in the body).
Scope and tenant handling are identical to the single endpoints: `tenant_middleware` +
`platform:admin`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response
from sqlalchemy.exc import IntegrityError

from services import bundle_approval_service as svc
from services import bundle_audit
from services.bundle_telemetry import bundle_span
from services.current_user import get_current_user_id
from services.errors import ApiError

logger = logging.getLogger(__name__)

MAX_BULK_ITEMS = 200

bundle_approvals_bulk_bp = Blueprint(
    "v1_bundle_approvals_bulk", __name__, url_prefix="/api/v1/apps"
)


@dataclass(slots=True, frozen=True)
class BulkItem:
    """One `(appId, version)` pair in a bulk request."""

    appId: str  # noqa: N815 - camelCase is the wire contract
    version: str


@dataclass(slots=True, frozen=True)
class BulkApprovalRequest:
    """Request DTO for `POST /api/v1/apps/approvals/bulk`."""

    action: Literal["approve", "reject"]
    items: list[BulkItem] = field(default_factory=list)
    reason: str | None = None


@dataclass(slots=True, frozen=True)
class BulkItemError:
    """Per-item failure detail."""

    code: str
    message: str


@dataclass(slots=True, frozen=True)
class BulkItemResult:
    """Per-item outcome: approved / rejected / already_installed / error."""

    appId: str  # noqa: N815 - camelCase is the wire contract
    version: str
    status: Literal["approved", "rejected", "already_installed", "error"]
    error: BulkItemError | None = None


@dataclass(slots=True, frozen=True)
class BulkSummary:
    """Batch totals; `succeeded` counts approved + rejected + already_installed."""

    total: int
    succeeded: int
    failed: int


@dataclass(slots=True, frozen=True)
class BulkApprovalResponse:
    """Response DTO for the bulk endpoint (security.md Output Validation)."""

    success: bool
    results: list[BulkItemResult]
    summary: BulkSummary


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


async def _is_current_install(install_dal: AsyncDB, app_id: str, version: str) -> bool:
    """True when `(app_id, version)` already has a current (non-superseded) global install row."""
    rows = await install_dal(
        (install_dal.app_global_installs.app_id == app_id)
        & (install_dal.app_global_installs.version == version)
        & (install_dal.app_global_installs.superseded_by == None)  # noqa: E711
    ).select()
    return rows.first() is not None


async def _audit(
    install_dal: AsyncDB, *, actor_id: int | None, item: BulkItem, outcome: str
) -> None:
    """Write one bulk-outcome audit row; audit failure is logged and never aborts the batch."""
    try:
        await bundle_audit.record(
            install_dal,
            actor_id=actor_id,
            action=f"bulk_approval_{outcome}",
            target_type="app_versions",
            target_id=f"{item.appId}@{item.version}",
            details={"bulk": True},
        )
    except Exception:  # pragma: no cover - AuditWriteError already logged + counted
        logger.error("bulk approval audit write failed", extra={"app_id": item.appId})


async def _process_item(
    install_dal: AsyncDB,
    *,
    action: str,
    item: BulkItem,
    actor_id: int | None,
    reason: str | None,
) -> BulkItemResult:
    """Run one item through the same service call as the single endpoint; never raises."""
    try:
        if action == "approve":
            try:
                await svc.install_version_globally(
                    install_dal,
                    app_id=item.appId,
                    version=item.version,
                    installed_by=actor_id,
                )
            except IntegrityError:
                # uq_app_global_installs_current: this version is already the current install.
                if not await _is_current_install(install_dal, item.appId, item.version):
                    raise
                await _audit(install_dal, actor_id=actor_id, item=item, outcome="already_installed")
                return BulkItemResult(item.appId, item.version, "already_installed")
            # install_version_globally audits the install itself.
            return BulkItemResult(item.appId, item.version, "approved")
        await svc.deny_version(
            install_dal,
            app_id=item.appId,
            version=item.version,
            reason=reason or "rejected in bulk",
        )
        await _audit(install_dal, actor_id=actor_id, item=item, outcome="rejected")
        return BulkItemResult(item.appId, item.version, "rejected")
    except ApiError as exc:
        await _audit(install_dal, actor_id=actor_id, item=item, outcome="failed")
        return BulkItemResult(
            item.appId, item.version, "error", BulkItemError(exc.code, exc.message)
        )
    except Exception:
        logger.exception(
            "bulk approval item failed", extra={"app_id": item.appId, "version": item.version}
        )
        await _audit(install_dal, actor_id=actor_id, item=item, outcome="failed")
        return BulkItemResult(
            item.appId,
            item.version,
            "error",
            BulkItemError("internal_error", "unexpected error processing this item"),
        )


@bundle_approvals_bulk_bp.route("/approvals/bulk", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@require_scope("platform:admin")  # type: ignore[misc]
@validate_request(BulkApprovalRequest)
@validate_response(BulkApprovalResponse)
async def post_bulk_approvals(
    data: BulkApprovalRequest,
) -> BulkApprovalResponse | tuple[dict[str, Any], int]:
    """Approve or reject many bundle versions; per-item results, partial success is HTTP 200."""
    if not data.items:
        return cast(
            tuple[dict[str, Any], int],
            error_response("items must not be empty", 400, "invalid_request"),
        )
    if len(data.items) > MAX_BULK_ITEMS:
        return cast(
            tuple[dict[str, Any], int],
            error_response(f"at most {MAX_BULK_ITEMS} items per request", 400, "too_many_items"),
        )
    install_dal = _install_dal()
    actor_id = get_current_user_id(request)
    results: list[BulkItemResult] = []
    async with bundle_span(
        "bundle_approvals.bulk", action=data.action, items_total=len(data.items)
    ) as span:
        for item in data.items:
            results.append(
                await _process_item(
                    install_dal,
                    action=data.action,
                    item=item,
                    actor_id=actor_id,
                    reason=data.reason,
                )
            )
        failed = sum(1 for r in results if r.status == "error")
        span.set_attribute("items_succeeded", len(results) - failed)
        span.set_attribute("items_failed", failed)
    logger.info(
        "bulk bundle approvals processed",
        extra={"action": data.action, "total": len(results), "failed": failed},
    )
    return BulkApprovalResponse(
        success=failed == 0,
        results=results,
        summary=BulkSummary(total=len(results), succeeded=len(results) - failed, failed=failed),
    )


BLUEPRINTS: list[Blueprint] = [bundle_approvals_bulk_bp]
