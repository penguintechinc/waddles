"""v1 internal scheduled-bundle listing -- `GET /api/v1/internal/bundle-schedules`.

svc-ingest's `HubScheduleSource` polls this to learn which `(tenant, community, app)` scopes it must
tick with a `hub`/`bundle.reconcile` event (`docs/BUNDLE_HOST_SCHEDULING_ADMIN_METRICS.md`).

Machine identity only: JWT scope `bundle-schedules:read` from the operator-plane `system` tenant
(`services.machine_auth`; granted in the chart to svc-ingest alone), plus a caller-name allow-list
as defence in depth. The route deliberately spans tenants (the host schedules for all) but only
ever returns ids, display names and the cadence -- no manifest content, no secrets, no PII. Optional
`tenant_id` / `community_id` / `app_id` filters narrow the listing. The response is an explicit DTO
(`@validate_response`), `no-store`, and every refusal is generic.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Final

from flask_core.api_utils import error_response
from quart import Blueprint, Response, current_app, g
from quart_schema import validate_querystring, validate_response

from services import bundle_schedule_service as schedules
from services.machine_auth import machine_authenticated

logger = logging.getLogger(__name__)

bundle_schedules_internal_bp = Blueprint(
    "v1_bundle_schedules_internal", __name__, url_prefix="/api/v1/internal"
)

#: Machine-JWT scope the data plane's svc-ingest identity must hold (and no other service).
SCOPE_BUNDLE_SCHEDULES_READ: Final = "bundle-schedules:read"
#: Service names (last SPIFFE path segment) allowed to call; `BUNDLE_SCHEDULES_ALLOWED_SERVICES`
#: (comma list) overrides.
DEFAULT_ALLOWED_SERVICES: Final = ("svc-ingest",)
_APP_ID_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MAX_ID: Final = 2**31 - 1


@dataclass(slots=True, frozen=True)
class ScheduleQuery:
    """Optional listing filters; numeric ids only (the data plane never keys on slugs)."""

    tenant_id: int | None = None
    community_id: int | None = None
    app_id: str | None = None


@dataclass(slots=True, frozen=True)
class ScheduleTargetDTO:
    """One scheduled scope. `community_id` 0 = tenant-wide (then `community_name` is null)."""

    tenant_id: int
    community_id: int
    tenant_slug: str
    community_name: str | None
    app_id: str
    interval_s: int
    jitter: float


@dataclass(slots=True, frozen=True)
class ScheduleListResponse:
    """`{"targets": [...]}` -- the exact shape svc-ingest's `TargetsDto` deserialises."""

    targets: list[ScheduleTargetDTO] = field(default_factory=list)


_auth = machine_authenticated(
    SCOPE_BUNDLE_SCHEDULES_READ,
    unconfigured_event="bundle_schedules.service_auth_unconfigured",
    rejected_event="bundle_schedules.service_auth_rejected",
    wrong_tenant_event="bundle_schedules.service_auth_wrong_tenant",
)


@bundle_schedules_internal_bp.after_request
async def _no_store(response: Response) -> Response:
    """The listing is operational data -- never cached by an intermediary."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _allowed_services() -> frozenset[str]:
    raw = os.getenv("BUNDLE_SCHEDULES_ALLOWED_SERVICES")
    if raw is None:
        return frozenset(DEFAULT_ALLOWED_SERVICES)
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def _caller_allowed() -> bool:
    """True when the verified caller's SPIFFE service segment is allow-listed."""
    subject = str(getattr(g, "machine_subject", "") or "")
    name = subject.rsplit("/", 1)[-1] if subject.startswith("spiffe://") else ""
    return name in _allowed_services()


def _bad_filters(q: ScheduleQuery) -> bool:
    if q.tenant_id is not None and not 1 <= q.tenant_id <= _MAX_ID:
        return True
    if q.community_id is not None and not 0 <= q.community_id <= _MAX_ID:
        return True
    return q.app_id is not None and _APP_ID_RE.fullmatch(q.app_id) is None


@bundle_schedules_internal_bp.route("/bundle-schedules", methods=["GET"])
@_auth
@validate_querystring(ScheduleQuery)
@validate_response(ScheduleListResponse)
async def list_bundle_schedules(query_args: ScheduleQuery) -> Any:
    """`GET /api/v1/internal/bundle-schedules` -> `{targets:[...]}` of active scheduled bundles.

    Ladder after machine auth: caller allow-list (403) -> filter shape (400) -> listing. A
    candidate that fails the manifest/binding checks is excluded and logged/metered by the service,
    never returned.
    """
    if not _caller_allowed():
        logger.warning("bundle_schedules.caller_not_allowlisted")
        return {"error": "forbidden"}, 403
    if _bad_filters(query_args):
        return error_response("invalid request", 400, "BAD_REQUEST")
    listing = await schedules.list_schedule_targets(
        current_app.config["install_dal"],
        tenant_id=query_args.tenant_id,
        community_id=query_args.community_id,
        app_id=query_args.app_id,
    )
    return ScheduleListResponse(
        targets=[
            ScheduleTargetDTO(
                tenant_id=t.tenant_id,
                community_id=t.community_id,
                tenant_slug=t.tenant_slug,
                community_name=t.community_name,
                app_id=t.app_id,
                interval_s=t.interval_s,
                jitter=t.jitter,
            )
            for t in listing.targets
        ]
    )


BLUEPRINTS = [bundle_schedules_internal_bp]
