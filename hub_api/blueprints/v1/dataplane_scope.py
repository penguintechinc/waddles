"""v1 internal data-plane scope resolver -- ``(platform, source_id)`` -> numeric tenant/community.

Slice A of the numeric-id migration (``docs/superpowers/specs/2026-10-14-data-plane-numeric-ids-
migration.md``). svc-ingest has no database; it asks hub-api which community a platform source (a
Discord guild id, a Twitch channel login) belongs to so the data plane keys on numeric ids.

* ``GET  /api/v1/internal/dataplane/resolve-scope?platform=&source_id=``
* ``POST /api/v1/internal/dataplane/resolve-scope/batch`` -- up to 100 ``{platform, sourceId}``

Machine JWT only: scope ``dataplane:resolve-scope`` (deliberately NOT ``connections:resolve``,
least privilege) from the operator-plane ``system`` tenant. The scope is granted in the chart to
svc-ingest's SPIFFE identity alone and only while ``dynamicConnections.enabled`` is on. The response
carries ids and display names only -- no secret exists to leak. A 200 is cacheable for a short TTL;
every refusal is ``no-store``. 404 means "not linked": the caller fails closed to the tenant-wide
``community_id 0`` sentinel, it never guesses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from flask_core.api_utils import error_response
from quart import Blueprint, Response, current_app, request
from quart_schema import validate_request, validate_response

from services import dataplane_scope as scopes
from services.machine_auth import machine_authenticated

logger = logging.getLogger(__name__)

dataplane_scope_internal_bp = Blueprint(
    "v1_dataplane_scope_internal", __name__, url_prefix="/api/v1/internal/dataplane"
)

#: Machine-JWT scope the data plane's service identity must hold (and no other service should).
SCOPE_DATAPLANE_RESOLVE_SCOPE = "dataplane:resolve-scope"
#: Seconds a successful resolution may be cached by the caller / an intermediary.
CACHE_TTL_SECONDS = 30


@dataclass(slots=True, frozen=True)
class ScopeDTO:
    """Resolved scope. ``tenantId``/``communityId`` are the keys; the names are display-only."""

    success: bool
    tenantId: int
    communityId: int
    tenantSlug: str
    communityName: str


@dataclass(slots=True, frozen=True)
class BatchItem:
    """One lookup of a batch request."""

    platform: str
    sourceId: str


@dataclass(slots=True, frozen=True)
class BatchRequest:
    """Body of the batch form."""

    items: list[BatchItem] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class BatchResult:
    """Outcome of one batch item: ``resolved`` carries ids, anything else carries none."""

    platform: str
    sourceId: str
    status: str  # resolved | not_found | ambiguous | invalid
    tenantId: int | None = None
    communityId: int | None = None
    tenantSlug: str | None = None
    communityName: str | None = None


@dataclass(slots=True, frozen=True)
class BatchResponse:
    """Batch response, in request order."""

    success: bool
    results: list[BatchResult] = field(default_factory=list)


_machine_authenticated = machine_authenticated(
    SCOPE_DATAPLANE_RESOLVE_SCOPE,
    unconfigured_event="dataplane_scope.service_auth_unconfigured",
    rejected_event="dataplane_scope.service_auth_rejected",
    wrong_tenant_event="dataplane_scope.service_auth_wrong_tenant",
)


@dataplane_scope_internal_bp.after_request
async def _cache_policy(response: Response) -> Response:
    """Short private TTL on a successful lookup; ``no-store`` on every refusal."""
    if response.status_code == 200:
        response.headers["Cache-Control"] = f"private, max-age={CACHE_TTL_SECONDS}"
    else:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


async def _resolve(platform: str, source_id: str) -> scopes.ResolvedScope:
    return await scopes.resolve_scope(
        current_app.config["async_dal"], current_app.config["dal"], platform, source_id
    )


def _refusal(exc: scopes.ScopeError) -> tuple[dict[str, object], int]:
    """Map a resolver refusal to the JSON error envelope (fixed, application-authored text)."""
    if isinstance(exc, scopes.InvalidSourceError):
        status, code, message = 400, "BAD_REQUEST", "platform or source_id is not valid"
    elif isinstance(exc, scopes.AmbiguousScopeError):
        status, code, message = 409, "SCOPE_AMBIGUOUS", "source maps to more than one community"
    else:
        status, code, message = 404, "SCOPE_NOT_FOUND", "no community is linked to this source"
    return error_response(message, status, code)  # type: ignore[no-any-return]


@dataplane_scope_internal_bp.route("/resolve-scope", methods=["GET"])
@_machine_authenticated
@validate_response(ScopeDTO)
async def resolve_scope_route() -> Any:
    """``GET`` -- one source to its numeric scope; 404 when unlinked, 409 when ambiguous."""
    try:
        found = await _resolve(request.args.get("platform", ""), request.args.get("source_id", ""))
    except scopes.ScopeError as exc:
        logger.debug("dataplane_scope.refused reason=%s", type(exc).__name__)
        return _refusal(exc)
    return ScopeDTO(
        success=True,
        tenantId=found.tenant_id,
        communityId=found.community_id,
        tenantSlug=found.tenant_slug,
        communityName=found.community_name,
    )


@dataplane_scope_internal_bp.route("/resolve-scope/batch", methods=["POST"])
@_machine_authenticated
@validate_request(BatchRequest)
@validate_response(BatchResponse)
async def resolve_scope_batch(data: BatchRequest) -> Any:
    """``POST`` -- up to 100 lookups; each result carries its own status, order preserved."""
    if not 1 <= len(data.items) <= scopes.MAX_BATCH:
        return error_response(
            f"items must contain between 1 and {scopes.MAX_BATCH} entries", 400, "BAD_REQUEST"
        )
    results: list[BatchResult] = []
    for item in data.items:
        try:
            found = await _resolve(item.platform, item.sourceId)
        except scopes.InvalidSourceError:
            results.append(BatchResult(item.platform, item.sourceId, "invalid"))
        except scopes.AmbiguousScopeError:
            results.append(BatchResult(item.platform, item.sourceId, "ambiguous"))
        except scopes.ScopeNotFoundError:
            results.append(BatchResult(item.platform, item.sourceId, "not_found"))
        else:
            results.append(
                BatchResult(
                    item.platform,
                    item.sourceId,
                    "resolved",
                    found.tenant_id,
                    found.community_id,
                    found.tenant_slug,
                    found.community_name,
                )
            )
    return BatchResponse(success=True, results=results)


BLUEPRINTS = [dataplane_scope_internal_bp]
