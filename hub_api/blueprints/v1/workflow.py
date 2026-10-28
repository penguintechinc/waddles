"""v1 `workflow` group -- ported from `workflowController.js`/`routes/workflow.js` (M-automation).

Every route below is IDENTICAL to the path documented in
`workflowController.js`'s own JSDoc comments (e.g. `POST /api/v1/admin/
:communityId/workflows`) and confirmed against `frontend/src/services/
api.js`'s `workflowApi` -- NOT to `routes/admin.js`'s actual mount line
(`router.use('/:communityId/workflows', workflowRoutes)`), which doubles
the `/:communityId/workflows` prefix on top of `workflow.js`'s own
already-prefixed route strings. Traced through Express's own mount
semantics, that double-prefix mount can never actually match any real
request path (the sub-router only ever sees an empty remainder) --
every one of these 15 endpoints is dead/unreachable in Node's current
routing today, a pre-existing bug, not a behavior this port reproduces
(unlike `blueprints/v1/event.py`'s deliberately-preserved
`errorHandler.js` property-mismatch bug, reproducing THIS bug would
mean porting a feature that returns 404 for literally every call, which
defeats "port the workflow controllers" as a goal). The controller's own
JSDoc and `api.js` agree on the single-prefix shape; ported that way.

Two known pre-existing `api.js`/Node-route contract gaps, NOT invented
here (`hub_api/PORTING.md` Gotcha #4's "document a gap, don't silently
invent" precedent):
  - `workflowApi.unpublishWorkflow()` / `.regenerateWebhookSecret()` call
    paths (`.../unpublish`, `.../webhooks/:id/regenerate`) that
    `routes/workflow.js` never registers at all -- no Node handler
    exists to port.
  - `workflowApi.validateWorkflow(communityId, workflowId)` calls
    `.../workflows/:workflowId/validate`, but the real registered route
    is `POST /:communityId/workflows/validate` (literal, no `workflowId`
    segment, `definition` in the body) -- `validateWorkflow()`'s own
    handler body never reads `req.params.workflowId` either. Ported to
    match the real route + controller body, matching `api.js`'s call
    would 404 against this route today too.

Authz: every endpoint requires `tenant_middleware` (security.md: tenant
before scope) + `services.community_authz.require_community_admin()`,
a faithful DB-backed port of Node's `requireCommunityAdmin` (real
`community_members`/`community_roles` join) -- NOT `blueprints/v1/
event.py`'s flattened `require_scope(SCOPE_ADMIN)` shortcut. That
shortcut fit event.py's group (a single proxied service with no owned
per-community DB model); this group needs the real thing because (a)
`github_sync.py` (sibling group, same Node `requireCommunityAdmin`
gate) owns real per-community secrets (GitHub PATs) a static JWT scope
can't express membership changes for without re-issuing tokens, and
(b) `community_members.role`/`community_roles.base_claims.scopes` IS
already an OIDC-scope-shaped value (`community:manage_members` /
`community:manage_channels`), just resolved per-community from the DB
instead of embedded in the static JWT `scope` claim -- still a scope
check, not a role-name branch, per security.md's actual concern.

SECURITY FIX (not a faithful-port item): every route addressing a
specific `workflow_id` (update/delete/publish/execute/test/executions/
webhooks) verifies community ownership via `workflow_service.
get_workflow_or_403()` before proceeding -- Node's own `getWorkflow()`
is the ONLY handler with this check today; see `workflow_service.py`'s
module docstring for the full IDOR rationale.

Response bodies proxied from `workflow-core` are opaque (not a hub-api-
owned row) -- no `@validate_response` DTO, same rationale as `blueprints/
v1/event.py`'s module docstring (security.md's Output Validation concern
is about accidentally over-serializing an OWNED model, not an opaque
reverse-proxy body that was never hub-api's row to begin with).
"""

from __future__ import annotations

from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, request

from services.community_authz import require_community_admin, require_valid_community_id
from services.current_user import get_current_user_id
from services.errors import ApiError, bad_request, forbidden
from services.workflow_service import (
    ProxyResult,
    WorkflowCoreProxyClient,
    error_from_proxy,
    get_workflow_or_403,
    validate_license,
)

workflow_bp = Blueprint("v1_workflow", __name__, url_prefix="/api/v1/admin")

_client = WorkflowCoreProxyClient()


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config -- same accessor shape as `auth.py`."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


def _int_arg(name: str, default: int) -> int:
    """Port of Node's `parseInt(req.query.X || 'default', 10)` -- non-numeric falls back."""
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


async def _require_admin(community_id: int) -> None:
    """Raise `ApiError` unless the caller admins `community_id` -- see module docstring Authz."""
    async_dal, dal = _dal()
    await require_community_admin(async_dal, dal, request, community_id=community_id)


async def _guard(raw_community_id: str) -> int | tuple[dict[str, object], int]:
    """Validate + authorize `community_id` -- returns the int id, or an error tuple."""
    try:
        community_id = require_valid_community_id(raw_community_id)
        await _require_admin(community_id)
    except ApiError as exc:
        return _err(exc)
    return community_id


def _proxy_response(result: ProxyResult, *, not_found_message: str) -> ApiError | None:
    """Return an `ApiError` if `result` failed, else `None` -- caller returns on non-None."""
    if result.ok:
        return None
    return error_from_proxy(result, not_found_message=not_found_message)


def _default_pagination(page: int, limit: int) -> dict[str, int]:
    """Fallback pagination envelope when `workflow-core` doesn't return one."""
    return {"page": page, "limit": limit, "total": 0, "totalPages": 0}


# ---------------------------------------------------------------------------
# Workflow CRUD
# ---------------------------------------------------------------------------


@workflow_bp.route("/<community_id>/workflows", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def list_workflows(community_id: str) -> tuple[dict[str, object], int]:
    """List workflows."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    page, limit = max(1, _int_arg("page", 1)), min(100, max(1, _int_arg("limit", 25)))
    params: dict[str, Any] = {"communityId": guard, "page": page, "limit": limit}
    status = request.args.get("status")
    if status:
        params["status"] = status

    result = await _client.request("GET", "/workflows", params=params)
    err = _proxy_response(result, not_found_message="Workflow not found")
    if err is not None:
        return _err(err)
    body = result.body if isinstance(result.body, dict) else {}
    return {
        "success": True,
        "workflows": body.get("workflows") or [],
        "pagination": body.get("pagination") or _default_pagination(page, limit),
    }, 200


@workflow_bp.route("/<community_id>/workflows", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
async def create_workflow(community_id: str) -> tuple[dict[str, object], int]:
    """Create workflow."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard

    payload = await request.get_json(silent=True) or {}
    name = payload.get("name")
    description = payload.get("description")
    definition = payload.get("definition")
    if not name or not definition:
        return _err(bad_request("Name and definition are required"))

    async_dal, dal = _dal()
    license_check = await validate_license(async_dal, dal, community_id=guard)
    if not license_check.valid:
        return _err(forbidden(f"Workflows not available: {license_check.reason}"))

    user_id = get_current_user_id(request)
    result = await _client.request(
        "POST",
        "/workflows",
        json_body={
            "communityId": guard,
            "name": name,
            "description": description,
            "definition": definition,
            "createdBy": user_id,
        },
    )
    err = _proxy_response(result, not_found_message="Workflow not found")
    if err is not None:
        return _err(err)
    return {"success": True, "workflow": result.body}, 201


@workflow_bp.route("/<community_id>/workflows/<workflow_id>", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def get_workflow(community_id: str, workflow_id: str) -> tuple[dict[str, object], int]:
    """Get workflow."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    try:
        body = await get_workflow_or_403(_client, community_id=guard, workflow_id=workflow_id)
    except ApiError as exc:
        return _err(exc)
    return {"success": True, "workflow": body}, 200


@workflow_bp.route("/<community_id>/workflows/<workflow_id>", methods=["PUT"])
@tenant_middleware  # type: ignore[misc]
async def update_workflow(community_id: str, workflow_id: str) -> tuple[dict[str, object], int]:
    """Update workflow."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    try:
        await get_workflow_or_403(_client, community_id=guard, workflow_id=workflow_id)
    except ApiError as exc:
        return _err(exc)

    payload = await request.get_json(silent=True) or {}
    body: dict[str, Any] = {"communityId": guard, "updatedBy": get_current_user_id(request)}
    if payload.get("name"):
        body["name"] = payload["name"]
    if "description" in payload:
        body["description"] = payload["description"]
    if payload.get("definition"):
        body["definition"] = payload["definition"]

    result = await _client.request("PUT", f"/workflows/{workflow_id}", json_body=body)
    err = _proxy_response(result, not_found_message="Workflow not found")
    if err is not None:
        return _err(err)
    return {"success": True, "workflow": result.body}, 200


@workflow_bp.route("/<community_id>/workflows/<workflow_id>", methods=["DELETE"])
@tenant_middleware  # type: ignore[misc]
async def delete_workflow(community_id: str, workflow_id: str) -> tuple[dict[str, object], int]:
    """Delete workflow."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    try:
        await get_workflow_or_403(_client, community_id=guard, workflow_id=workflow_id)
    except ApiError as exc:
        return _err(exc)

    result = await _client.request(
        "DELETE",
        f"/workflows/{workflow_id}",
        json_body={"communityId": guard, "deletedBy": get_current_user_id(request)},
    )
    err = _proxy_response(result, not_found_message="Workflow not found")
    if err is not None:
        return _err(err)
    return {"success": True, "message": "Workflow deleted successfully"}, 200


# ---------------------------------------------------------------------------
# Workflow operations (publish, validate)
# ---------------------------------------------------------------------------


@workflow_bp.route("/<community_id>/workflows/<workflow_id>/publish", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
async def publish_workflow(community_id: str, workflow_id: str) -> tuple[dict[str, object], int]:
    """Publish workflow."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    try:
        await get_workflow_or_403(_client, community_id=guard, workflow_id=workflow_id)
    except ApiError as exc:
        return _err(exc)

    async_dal, dal = _dal()
    license_check = await validate_license(async_dal, dal, community_id=guard)
    if not license_check.valid:
        return _err(forbidden(f"Workflows not available: {license_check.reason}"))

    result = await _client.request(
        "POST",
        f"/workflows/{workflow_id}/publish",
        json_body={"communityId": guard, "publishedBy": get_current_user_id(request)},
    )
    err = _proxy_response(result, not_found_message="Workflow not found")
    if err is not None:
        return _err(err)
    return {"success": True, "workflow": result.body}, 200


@workflow_bp.route("/<community_id>/workflows/validate", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
async def validate_workflow(community_id: str) -> tuple[dict[str, object], int]:
    """Validate workflow definition."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard

    payload = await request.get_json(silent=True) or {}
    definition = payload.get("definition")
    if not definition:
        return _err(bad_request("Workflow definition is required"))

    result = await _client.request(
        "POST", "/workflows/validate", json_body={"communityId": guard, "definition": definition}
    )
    err = _proxy_response(result, not_found_message="Workflow not found")
    if err is not None:
        return _err(err)
    body = result.body if isinstance(result.body, dict) else {}
    return {
        "success": True,
        "isValid": body.get("isValid"),
        "errors": body.get("errors") or [],
        "warnings": body.get("warnings") or [],
    }, 200


# ---------------------------------------------------------------------------
# Workflow execution
# ---------------------------------------------------------------------------


@workflow_bp.route("/<community_id>/workflows/<workflow_id>/execute", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
async def execute_workflow(community_id: str, workflow_id: str) -> tuple[dict[str, object], int]:
    """Execute workflow."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    try:
        await get_workflow_or_403(_client, community_id=guard, workflow_id=workflow_id)
    except ApiError as exc:
        return _err(exc)

    async_dal, dal = _dal()
    license_check = await validate_license(async_dal, dal, community_id=guard)
    if not license_check.valid:
        return _err(forbidden(f"Workflows not available: {license_check.reason}"))

    payload = await request.get_json(silent=True) or {}
    result = await _client.request(
        "POST",
        f"/workflows/{workflow_id}/execute",
        json_body={
            "communityId": guard,
            "input": payload.get("input") or {},
            "context": payload.get("context") or {},
            "executedBy": get_current_user_id(request),
        },
    )
    err = _proxy_response(result, not_found_message="Workflow not found")
    if err is not None:
        return _err(err)
    return {"success": True, "execution": result.body}, 200


@workflow_bp.route("/<community_id>/workflows/<workflow_id>/test", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
async def test_workflow(community_id: str, workflow_id: str) -> tuple[dict[str, object], int]:
    """Test execute workflow.

    No license gate -- matches Node, which never calls `validateLicense()` here.
    """
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    try:
        await get_workflow_or_403(_client, community_id=guard, workflow_id=workflow_id)
    except ApiError as exc:
        return _err(exc)

    payload = await request.get_json(silent=True) or {}
    result = await _client.request(
        "POST",
        f"/workflows/{workflow_id}/test",
        json_body={
            "communityId": guard,
            "input": payload.get("input") or {},
            "context": payload.get("context") or {},
            "testedBy": get_current_user_id(request),
        },
    )
    err = _proxy_response(result, not_found_message="Workflow not found")
    if err is not None:
        return _err(err)
    return {"success": True, "result": result.body}, 200


@workflow_bp.route("/<community_id>/workflows/<workflow_id>/executions", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def list_executions(community_id: str, workflow_id: str) -> tuple[dict[str, object], int]:
    """List executions."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    try:
        await get_workflow_or_403(_client, community_id=guard, workflow_id=workflow_id)
    except ApiError as exc:
        return _err(exc)

    page, limit = max(1, _int_arg("page", 1)), min(100, max(1, _int_arg("limit", 25)))
    params: dict[str, Any] = {"communityId": guard, "page": page, "limit": limit}
    status = request.args.get("status")
    if status:
        params["status"] = status

    result = await _client.request("GET", f"/workflows/{workflow_id}/executions", params=params)
    err = _proxy_response(result, not_found_message="Workflow not found")
    if err is not None:
        return _err(err)
    body = result.body if isinstance(result.body, dict) else {}
    return {
        "success": True,
        "executions": body.get("executions") or [],
        "pagination": body.get("pagination") or _default_pagination(page, limit),
    }, 200


@workflow_bp.route(
    "/<community_id>/workflows/<workflow_id>/executions/<execution_id>", methods=["GET"]
)
@tenant_middleware  # type: ignore[misc]
async def get_execution(
    community_id: str, workflow_id: str, execution_id: str
) -> tuple[dict[str, object], int]:
    """Get execution."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    try:
        await get_workflow_or_403(_client, community_id=guard, workflow_id=workflow_id)
    except ApiError as exc:
        return _err(exc)

    result = await _client.request(
        "GET", f"/workflows/{workflow_id}/executions/{execution_id}", params={"communityId": guard}
    )
    err = _proxy_response(result, not_found_message="Execution not found")
    if err is not None:
        return _err(err)
    return {"success": True, "execution": result.body}, 200


@workflow_bp.route(
    "/<community_id>/workflows/<workflow_id>/executions/<execution_id>/cancel", methods=["POST"]
)
@tenant_middleware  # type: ignore[misc]
async def cancel_execution(
    community_id: str, workflow_id: str, execution_id: str
) -> tuple[dict[str, object], int]:
    """Cancel execution."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    try:
        await get_workflow_or_403(_client, community_id=guard, workflow_id=workflow_id)
    except ApiError as exc:
        return _err(exc)

    result = await _client.request(
        "POST",
        f"/workflows/{workflow_id}/executions/{execution_id}/cancel",
        json_body={"communityId": guard, "cancelledBy": get_current_user_id(request)},
    )
    err = _proxy_response(result, not_found_message="Execution not found")
    if err is not None:
        return _err(err)
    return {"success": True, "message": "Execution cancelled successfully"}, 200


# ---------------------------------------------------------------------------
# Workflow webhooks
# ---------------------------------------------------------------------------


@workflow_bp.route("/<community_id>/workflows/<workflow_id>/webhooks", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
async def list_webhooks(community_id: str, workflow_id: str) -> tuple[dict[str, object], int]:
    """List webhooks."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    try:
        await get_workflow_or_403(_client, community_id=guard, workflow_id=workflow_id)
    except ApiError as exc:
        return _err(exc)

    result = await _client.request(
        "GET", f"/workflows/{workflow_id}/webhooks", params={"communityId": guard}
    )
    err = _proxy_response(result, not_found_message="Workflow not found")
    if err is not None:
        return _err(err)
    body = result.body if isinstance(result.body, dict) else {}
    return {"success": True, "webhooks": body.get("webhooks") or []}, 200


@workflow_bp.route("/<community_id>/workflows/<workflow_id>/webhooks", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
async def create_webhook(community_id: str, workflow_id: str) -> tuple[dict[str, object], int]:
    """Create webhook."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    try:
        await get_workflow_or_403(_client, community_id=guard, workflow_id=workflow_id)
    except ApiError as exc:
        return _err(exc)

    payload = await request.get_json(silent=True) or {}
    event, url = payload.get("event"), payload.get("url")
    if not event or not url:
        return _err(bad_request("Event and URL are required"))

    result = await _client.request(
        "POST",
        f"/workflows/{workflow_id}/webhooks",
        json_body={
            "communityId": guard,
            "event": event,
            "url": url,
            "retryCount": payload.get("retryCount") or 3,
            "retryDelay": payload.get("retryDelay") or 5000,
            "createdBy": get_current_user_id(request),
        },
    )
    err = _proxy_response(result, not_found_message="Workflow not found")
    if err is not None:
        return _err(err)
    return {"success": True, "webhook": result.body}, 201


@workflow_bp.route(
    "/<community_id>/workflows/<workflow_id>/webhooks/<webhook_id>", methods=["DELETE"]
)
@tenant_middleware  # type: ignore[misc]
async def delete_webhook(
    community_id: str, workflow_id: str, webhook_id: str
) -> tuple[dict[str, object], int]:
    """Delete webhook."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    try:
        await get_workflow_or_403(_client, community_id=guard, workflow_id=workflow_id)
    except ApiError as exc:
        return _err(exc)

    result = await _client.request(
        "DELETE",
        f"/workflows/{workflow_id}/webhooks/{webhook_id}",
        json_body={"communityId": guard, "deletedBy": get_current_user_id(request)},
    )
    err = _proxy_response(result, not_found_message="Webhook not found")
    if err is not None:
        return _err(err)
    return {"success": True, "message": "Webhook deleted successfully"}, 200


BLUEPRINTS: list[Blueprint] = [workflow_bp]
