"""Business logic ported from `workflowController.js` -- proxy client + owned license check.

`workflowController.js` is almost entirely a reverse-proxy to the
standalone `workflow-core` service (`proxyRequest()`, port 8070) --
mirrors `services/event_calendar_proxy.py`'s `EventCalendarProxyClient`
(same `async with httpx.AsyncClient(...)` per-call pattern, same
`ProxyResult(ok, status_code, body)` shape). Two pieces of REAL,
hub-api-owned logic ride along with the proxying, both ported here:

1. `validate_license()` -- `workflowController.js::validateLicense()`,
   gating `create_workflow`/`publish_workflow`/`execute_workflow` as in Node,
   and (hardening beyond Node) also `update`, `test` and webhook creation --
   every path that mutates or runs a workflow (`require_workflow_tier`). The gate is the
   community's EFFECTIVE tier
   (`community_tier_service.resolve_effective_tier`: Premium iff the
   community has an active allocation -- the tenant tier never uplifts it --
   keyed by numeric ids, fail-closed to Free); the per-community `license_key`/`license_expires_at`
   requirement was dropped (an allocated Premium tier
   unlocks workflows like every other feature).

2. `get_workflow_or_403()` -- SECURITY FIX, not a faithful-port item.
   `getWorkflow()` is the ONLY Node handler that checks
   `result.communityId !== communityId -> 403` before returning a
   workflow proxied from `workflow-core`; `updateWorkflow`/
   `deleteWorkflow`/`publishWorkflow`/`executeWorkflow`/`testWorkflow`/
   every execution and webhook endpoint addressing a specific
   `workflowId` proxy straight through with NO equivalent check --
   an authenticated admin of community A, given a `workflowId`
   belonging to community B (workflow ids are unguessable but not
   secret -- visible in any URL/log/webhook payload), could mutate/
   execute/webhook a workflow they were never granted access to,
   trusting `workflow-core` alone to enforce scoping it has no
   token-derived signal to enforce (hub-api forwards `communityId` as
   a plain request field, same trust level as any other body param).
   `blueprints/v1/workflow.py` routes every `<workflow_id>`-addressing
   mutation through this same ownership check `getWorkflow` already
   proved out, closing the gap uniformly instead of leaving it fixed
   in exactly one of fifteen endpoints.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from services.community_tier_service import TIER_NAME_PREMIUM, resolve_effective_tier
from services.errors import ApiError, forbidden, not_found

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://workflow-core:8070"
_DEFAULT_TIMEOUT_SECONDS = 10.0
#: Workflows need a Premium community (old Node `workflowTiers`: pro/enterprise/premium).
_WORKFLOW_REQUIRED_TIER = TIER_NAME_PREMIUM


@dataclass(slots=True, frozen=True)
class ProxyResult:
    """Outcome of one proxied call to `workflow-core`.

    Mirrors `services.event_calendar_proxy.ProxyResult`.
    """

    ok: bool
    status_code: int
    body: Any


@dataclass(slots=True, frozen=True)
class LicenseCheckResult:
    """Result of `validate_license()` -- `{valid, reason}`, matches Node's return shape."""

    valid: bool
    reason: str | None = None


class WorkflowCoreProxyClient:
    """Forwards one request to `workflow-core`, relaying its JSON body.

    Port of Node's `proxyRequest()`.
    """

    def __init__(
        self, *, base_url: str | None = None, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        """Build the client; `base_url` defaults to `WORKFLOW_CORE_URL` env var, same as Node."""
        self._base_url = base_url or os.getenv("WORKFLOW_CORE_URL", _DEFAULT_BASE_URL)
        self._timeout_seconds = timeout_seconds

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ProxyResult:
        """Forward `method /api/v1{path}` to `workflow-core`."""
        url = f"{self._base_url}/api/v1{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.request(method, url, json=json_body, params=params)
        except httpx.HTTPError as exc:
            # Node's proxyRequest() catch-all: no `err.response` -> statusCode 500,
            # code SERVICE_UNAVAILABLE.
            raise ApiError(
                f"Failed to connect to workflow service: {exc}", 500, "SERVICE_UNAVAILABLE"
            ) from exc

        try:
            data: Any = response.json()
        except ValueError:
            data = None
        return ProxyResult(ok=response.is_success, status_code=response.status_code, body=data)


def error_from_proxy(result: ProxyResult, *, not_found_message: str) -> ApiError:
    """Map a failed `ProxyResult` to an `ApiError`.

    Port of each controller's `err.statusCode` switch. Node:
    `err.statusCode === 404 -> errors.notFound(...)`, else
    `errors.internal(err.message)` (any other status -- Node's own
    controllers collapse every non-404 workflow-core failure to a
    generic 500, faithfully preserved here rather than relaying
    workflow-core's real status code, which Node never does either).
    """
    if result.status_code == 404:
        return not_found(not_found_message)
    message = None
    if isinstance(result.body, dict):
        message = result.body.get("message") or result.body.get("error")
    return ApiError(str(message) if message else "Workflow service error", 500, "INTERNAL_ERROR")


_LICENSE_DENIED_REASON = "Workflows not included in current license tier"


async def validate_license(
    async_dal: Any, dal: Any, *, community_id: int, tenant: str | None = None
) -> LicenseCheckResult:
    """Port of `workflowController.js::validateLicense()`, now an effective-tier gate.

    Valid when the community's effective tier (Premium via allocation only, numeric-id keyed) is
    Premium. No per-community license key is required. An
    unknown, INACTIVE or other-tenant community (when the caller's validated `tenant` slug is
    given) and any lookup failure all produce the SAME denial as Free -- no existence or tier
    oracle -- logged at ERROR (type only) for failures; never a 500.
    """
    denied = LicenseCheckResult(False, _LICENSE_DENIED_REASON)
    try:
        rows = await async_dal.select_async(
            dal((dal.communities.id == community_id) & (dal.communities.is_active == True))  # noqa: E712
        )
        row = rows.first() if rows else None
        if row is None or row.tenant_id is None:
            return denied
        if tenant is not None:
            owner = await async_dal.select_async(dal(dal.tenants.id == int(row.tenant_id)))
            owner_row = owner.first() if owner else None
            if owner_row is None or str(owner_row.slug or "") != tenant:
                return denied
        tier = await resolve_effective_tier(async_dal, dal, int(row.tenant_id), community_id)
    except Exception as exc:  # noqa: BLE001 - a gate fails closed, never 500s on a tier lookup
        logger.error("workflow_license_check_failed error_type=%s", type(exc).__name__)
        return denied

    if tier != _WORKFLOW_REQUIRED_TIER:
        return denied
    return LicenseCheckResult(True)


async def require_workflow_tier(
    async_dal: Any, dal: Any, *, community_id: int, tenant: str | None = None
) -> None:
    """Raise a 403 `ApiError` unless `validate_license` passes.

    The one gate every workflow execution/mutation entry point (create, update, publish,
    execute, test, webhook create) calls, so a downgraded community stops at once.
    """
    check = await validate_license(async_dal, dal, community_id=community_id, tenant=tenant)
    if not check.valid:
        raise forbidden(f"Workflows not available: {check.reason}")


async def get_workflow_or_403(
    client: WorkflowCoreProxyClient, *, community_id: int, workflow_id: str
) -> dict[str, Any]:
    """Fetch a workflow and enforce community ownership -- see module docstring item (2).

    Every route addressing a specific `workflow_id` (not just `getWorkflow`)
    calls this before proceeding, closing the IDOR Node's own controllers
    left open on every mutating/execution/webhook endpoint.
    """
    result = await client.request(
        "GET", f"/workflows/{workflow_id}", params={"communityId": community_id}
    )
    if not result.ok:
        raise error_from_proxy(result, not_found_message="Workflow not found")
    body = result.body if isinstance(result.body, dict) else {}
    if body.get("communityId") != community_id:
        raise forbidden("Access denied")
    return body
