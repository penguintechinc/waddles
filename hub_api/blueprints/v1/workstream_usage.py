"""v1 `workstream_usage` group -- per-community admin usage view (spec Sec5.12, Sec6.12, D31).

Read-only, ungated by a PostHog flag on purpose -- a write surface is
gated, a read surface is not, so a flag flip mid-rollout never blinds an
admin already looking at usage data. Gated instead by `metering.enabled`
(a chart value, not a PostHog flag, spec Sec12.3) -- when metering is
off the aggregator simply never runs and this view returns empty pages,
never an error. R52: reads `current_app.config["install_dal"]` --
`workstream_usage_hourly` is this slice's own new table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from penguin_dal import AsyncDB
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services.errors import ApiError
from services.tenant_service import require_matching_tenant
from services.usage_query_service import query_usage

workstream_usage_bp = Blueprint(
    "v1_workstream_usage", __name__, url_prefix="/api/v1/tenant/<tenant_slug>/usage"
)


def _install_dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int],
        error_response(exc.message, exc.status_code, exc.code),
    )


def _tenant_id(tenant_slug: str) -> int:
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101
    require_matching_tenant(tenant_slug, ctx.tenant_slug)
    return cast(int, ctx.tenant_id)


def _parse_int(raw: str | None, *, field_name: str, code: str) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ApiError(f"{field_name} must be an integer", 422, code) from exc


def _parse_datetime(raw: str | None, *, field_name: str, code: str) -> datetime | None:
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(f"{field_name} must be an RFC3339 timestamp", 422, code) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(slots=True, frozen=True)
class UsageRowDTO:
    """One aggregated usage bucket on the wire."""

    communityId: int | None
    workstreamId: str
    stage: str
    appId: str | None
    hour: str
    events: int
    invocations: int
    hostCalls: int
    actionsDelivered: int
    fuelMs: int
    outboundBytes: int
    mediaMinutes: float | None


@dataclass(slots=True, frozen=True)
class UsageMetaDTO:
    """Pagination metadata."""

    total: int
    limit: int
    offset: int


@dataclass(slots=True, frozen=True)
class UsageListResponse:
    """Response DTO for `GET .../usage`."""

    success: bool
    rows: list[UsageRowDTO] = field(default_factory=list)
    meta: UsageMetaDTO = field(default_factory=lambda: UsageMetaDTO(total=0, limit=0, offset=0))


@workstream_usage_bp.route("", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("tenant:admin")  # type: ignore[untyped-decorator]
@validate_response(UsageListResponse)
async def list_usage(tenant_slug: str) -> UsageListResponse | tuple[dict[str, object], int]:
    """Per-community usage, summed by `(community, workstream, stage, app, hour)`, paginated."""
    install_dal = _install_dal()
    try:
        tenant_id = _tenant_id(tenant_slug)
        community_id = _parse_int(
            request.args.get("communityId"), field_name="communityId", code="invalid_community_id"
        )
        limit = (
            _parse_int(request.args.get("limit"), field_name="limit", code="invalid_limit") or 50
        )
        offset = (
            _parse_int(request.args.get("offset"), field_name="offset", code="invalid_offset") or 0
        )
        hour_from = _parse_datetime(
            request.args.get("from"), field_name="from", code="invalid_date_range"
        )
        hour_to = _parse_datetime(
            request.args.get("to"), field_name="to", code="invalid_date_range"
        )
        rows, total = await query_usage(
            install_dal,
            tenant_id=tenant_id,
            community_id=community_id,
            workstream_id=request.args.get("workstreamId"),
            stage=request.args.get("stage"),
            app_id=request.args.get("appId"),
            hour_from=hour_from,
            hour_to=hour_to,
            limit=limit,
            offset=offset,
        )
    except ApiError as exc:
        return _err(exc)

    return UsageListResponse(
        success=True,
        rows=[
            UsageRowDTO(
                communityId=r.community_id,
                workstreamId=r.workstream_id,
                stage=r.stage,
                appId=r.app_id,
                hour=r.hour.isoformat(),
                events=r.events,
                invocations=r.invocations,
                hostCalls=r.host_calls,
                actionsDelivered=r.actions_delivered,
                fuelMs=r.fuel_ms,
                outboundBytes=r.outbound_bytes,
                mediaMinutes=r.media_minutes,
            )
            for r in rows
        ],
        meta=UsageMetaDTO(total=total, limit=limit, offset=offset),
    )


BLUEPRINTS: list[Blueprint] = [workstream_usage_bp]
