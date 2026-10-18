"""v1 `github_sync` group -- ported from `githubSyncController.js`/`routes/githubSync.js`.

M-automation port group.

Two blueprints, matching Node's own two auth models on this one
controller group (`routes/githubSync.js`'s own module docstring:
"Connection management routes require JWT auth ... Webhook receiver is
public"):

- `github_sync_webhook_bp` (`/api/v1/github-sync/webhook`) -- NO
  `tenant_middleware`/`require_community_admin`, matching the "Pre-auth"
  row of `hub_api/PORTING.md`'s Auth pattern table (there is no JWT on
  an inbound GitHub webhook call). Authenticated instead via HMAC-SHA256
  payload signature (`X-Hub-Signature-256`) against the connection's
  own `webhook_secret` -- `services.github_sync_service.
  verify_webhook_signature()`, timing-safe (`hmac.compare_digest`).
- `github_sync_admin_bp` (`/api/v1/<community_id>/github-sync/...`) --
  `tenant_middleware` + `services.community_authz.
  require_community_admin()`, same faithful DB-backed port of Node's
  `requireCommunityAdmin` that `blueprints/v1/workflow.py` uses (see
  that module's docstring for why this group doesn't use `blueprints/
  v1/event.py`'s flattened-scope shortcut).

Response envelope is `{"status": "success"/"error", "data"/"message":
...}` -- `githubSyncController.js`'s OWN convention (`res.json({status:
'success', data: ...})` / `res.status(...).json({status: 'error',
message: ...})`), DIFFERENT from `workflowController.js`'s `{success:
true/false, ...}` shape ported in `blueprints/v1/workflow.py`. Each
Node controller group defined its own ad-hoc envelope; this port
preserves each group's own shape rather than unifying them (unifying
would be an actual behavior/contract change, out of scope for a
faithful port -- see `backend.md`'s `{status, data, meta}` mandate,
which is the FUTURE direction, not the current Node contract this PR
must match byte-for-byte).

Security fixes ported from `services/github_sync_service.py` (repo
coordinate validation, `vendor_id` IDOR) are documented in that module's
own docstring, not repeated here.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, cast

from flask_core.tenancy import tenant_middleware
from quart import Blueprint, Response, current_app, jsonify, request

from services import github_sync_service as svc
from services.community_authz import require_community_admin, require_valid_community_id
from services.current_user import get_current_user_id
from services.errors import ApiError, bad_request
from services.schema import bind_github_sync_tables

github_sync_webhook_bp = Blueprint(
    "v1_github_sync_webhook", __name__, url_prefix="/api/v1/github-sync"
)
github_sync_admin_bp = Blueprint("v1_github_sync_admin", __name__, url_prefix="/api/v1")


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config -- same accessor shape as `auth.py`."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """`{status: 'error', message}` envelope -- `githubSyncController.js`'s own shape.

    NOT `error_response()`'s `{success, error: {code, message}}` shape.
    """
    return {"status": "error", "message": exc.message}, exc.status_code


async def _guard(raw_community_id: str) -> int | tuple[dict[str, object], int]:
    """Validate + authorize `community_id` -- returns the int id, or an error tuple."""
    try:
        community_id = require_valid_community_id(raw_community_id)
        async_dal, dal = _dal()
        await require_community_admin(async_dal, dal, request, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    return community_id


def _ok(payload: dict[str, object], status: int = 200) -> tuple[Response, int]:
    """Build the response via `jsonify()` directly rather than returning a bare dict tuple.

    Every caller of this helper follows an `insert_async`/`update_async`
    write -- `hub_api/PORTING.md` Gotcha #3: quart-schema's app-wide
    `make_response` hook runs `TypeAdapter(dict).dump_python()` on EVERY
    dict/list response regardless of `@validate_response`, and that
    crashes (`TypeError: 'None' is not an instance of 'SchemaSerializer'`)
    for a plain dict too, not just a nested-dataclass DTO -- reproduced
    directly by `test_v1_github_sync_blueprint.py::TestTriggerSync::
    test_syncs_ticket_to_github` during this port. A real `quart.Response`
    (built via `jsonify()`) falls through `model_dump`'s `else: value =
    raw` branch untouched, same workaround shape as `services/
    dto_response.py::jsonify_dto()`.
    """
    return jsonify(payload), status


# ---------------------------------------------------------------------------
# Public webhook receiver -- HMAC-authenticated, no tenant_middleware
# ---------------------------------------------------------------------------


@github_sync_webhook_bp.route("/webhook", methods=["POST"])
async def receive_webhook() -> tuple[dict[str, object], int] | tuple[Response, int]:
    """Receive webhook."""
    event = request.headers.get("X-GitHub-Event")
    signature = request.headers.get("X-Hub-Signature-256")
    # `get_data()`'s own return type is `str | bytes` regardless of `as_text`
    # (no overloads in Quart's stub) -- `as_text=False` guarantees bytes at
    # runtime; the cast documents that rather than working around it.
    raw_body = cast(bytes, await request.get_data(as_text=False))

    if not event:
        return _err(bad_request("Missing X-GitHub-Event header"))
    if not signature:
        return _err(bad_request("Missing X-Hub-Signature-256 header"))

    try:
        payload = json.loads(raw_body)
    except ValueError:
        return _err(bad_request("Invalid JSON payload"))

    repo_owner = ((payload.get("repository") or {}).get("owner") or {}).get("login")
    repo_name = (payload.get("repository") or {}).get("name")
    if not repo_owner or not repo_name:
        return _err(bad_request("Missing repository info in payload"))

    async_dal, dal = _dal()
    try:
        await svc.handle_github_webhook(
            async_dal,
            dal,
            repo_owner=repo_owner,
            repo_name=repo_name,
            event=event,
            payload=payload,
            raw_body=raw_body,
            signature=signature,
        )
    except ApiError as exc:
        return _err(exc)
    return _ok({"status": "success"})


# ---------------------------------------------------------------------------
# Authenticated connection management
# ---------------------------------------------------------------------------


@github_sync_admin_bp.route("/<community_id>/github-sync/connections", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
async def list_connections(community_id: str) -> tuple[dict[str, object], int]:
    """List connections."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    async_dal, dal = _dal()
    connections = await svc.get_repo_connections(async_dal, dal, community_id=guard)
    return {"status": "success", "data": [_connection_dict(c) for c in connections]}, 200


@github_sync_admin_bp.route("/<community_id>/github-sync/connections", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
async def create_connection(
    community_id: str,
) -> tuple[dict[str, object], int] | tuple[Response, int]:
    """Create connection."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard

    payload = await request.get_json(silent=True) or {}
    async_dal, dal = _dal()
    try:
        connection = await svc.create_repo_connection(
            async_dal,
            dal,
            community_id=guard,
            repo_owner=payload.get("repo_owner", ""),
            repo_name=payload.get("repo_name", ""),
            auth_type=payload.get("auth_type", ""),
            token=payload.get("token", ""),
            sync_mode=payload.get("sync_mode") or "tickets_only",
            default_labels=payload.get("default_labels"),
            auto_close_on_github_close=payload.get("auto_close_on_github_close", True),
            installation_id=payload.get("installation_id"),
        )
    except ApiError as exc:
        return _err(exc)
    return _ok({"status": "success", "data": _connection_created_dict(connection)}, 201)


@github_sync_admin_bp.route(
    "/<community_id>/github-sync/connections/<connection_id>", methods=["DELETE"]
)
@tenant_middleware  # type: ignore[untyped-decorator]
async def delete_connection(
    community_id: str, connection_id: str
) -> tuple[dict[str, object], int] | tuple[Response, int]:
    """Delete connection."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    try:
        cid = int(connection_id)
    except ValueError:
        return _err(bad_request("Invalid connection ID"))

    async_dal, dal = _dal()
    user_id = get_current_user_id(request)
    try:
        await svc.delete_repo_connection(
            async_dal, dal, user_id=user_id, community_id=guard, connection_id=cid
        )
    except ApiError as exc:
        return _err(exc)
    return _ok({"status": "success", "message": "Connection deleted"})


# ---------------------------------------------------------------------------
# Ticket sync status + manual trigger
# ---------------------------------------------------------------------------


@github_sync_admin_bp.route(
    "/<community_id>/github-sync/ticket/<ticket_id>/sync-status", methods=["GET"]
)
@tenant_middleware  # type: ignore[untyped-decorator]
async def get_sync_status(community_id: str, ticket_id: str) -> tuple[dict[str, object], int]:
    """Get sync status."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    try:
        tid = int(ticket_id)
    except ValueError:
        return _err(bad_request("Invalid ticket ID"))

    async_dal, dal = _dal()
    bind_github_sync_tables(dal)
    rows = await async_dal.select_async(
        dal(dal.ticket_github_sync.ticket_id == tid),
        dal.ticket_github_sync.ALL,
        dal.github_repo_connections.ALL,
        left=dal.github_repo_connections.on(
            dal.ticket_github_sync.github_repo_connection_id == dal.github_repo_connections.id
        ),
        orderby=~dal.ticket_github_sync.created_at,
    )
    data = [
        {
            "id": r.ticket_github_sync.id,
            "ticket_id": r.ticket_github_sync.ticket_id,
            "github_issue_number": r.ticket_github_sync.github_issue_number,
            "github_issue_node_id": r.ticket_github_sync.github_issue_node_id,
            "sync_status": r.ticket_github_sync.sync_status,
            "last_synced_at": r.ticket_github_sync.last_synced_at,
            "last_error": r.ticket_github_sync.last_error,
            "retry_count": r.ticket_github_sync.retry_count,
            "created_at": r.ticket_github_sync.created_at,
            "repo_owner": r.github_repo_connections.repo_owner,
            "repo_name": r.github_repo_connections.repo_name,
            "sync_mode": r.github_repo_connections.sync_mode,
        }
        for r in rows
    ]
    return {"status": "success", "data": data}, 200


@github_sync_admin_bp.route("/<community_id>/github-sync/ticket/<ticket_id>/sync", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
async def trigger_sync(
    community_id: str, ticket_id: str
) -> tuple[dict[str, object], int] | tuple[Response, int]:
    """Trigger sync."""
    guard = await _guard(community_id)
    if isinstance(guard, tuple):
        return guard
    try:
        tid = int(ticket_id)
    except ValueError:
        return _err(bad_request("Invalid ticket ID"))

    payload = await request.get_json(silent=True) or {}
    raw_repo_connection_id = payload.get("repo_connection_id")
    if raw_repo_connection_id is None:
        return _err(bad_request("repo_connection_id is required"))
    try:
        repo_connection_id = int(raw_repo_connection_id)
    except (TypeError, ValueError):
        return _err(bad_request("repo_connection_id is required"))

    async_dal, dal = _dal()
    try:
        record = await svc.sync_ticket_to_github(
            async_dal, dal, ticket_id=tid, repo_connection_id=repo_connection_id
        )
    except ApiError as exc:
        return _err(exc)
    return _ok(
        {
            "status": "success",
            "data": {
                "id": record.id,
                "ticket_id": record.ticket_id,
                "github_repo_connection_id": record.github_repo_connection_id,
                "github_issue_number": record.github_issue_number,
                "github_issue_node_id": record.github_issue_node_id,
                "sync_status": record.sync_status,
            },
        }
    )


@dataclass(slots=True, frozen=True)
class RepoConnectionDTO:
    """Wire shape of a GitHub repo connection (encrypted token never a field).

    `webhook_secret` is NEVER a field here (write-only): list/get return only the
    `webhook_secret_set` boolean. The server-generated secret is revealed exactly
    once, in the create response, via `_connection_created_dict`.
    """

    id: int
    community_id: int | None
    vendor_id: int | None
    module_id: int | None
    repo_owner: str
    repo_name: str
    sync_mode: str
    default_labels: list[str]
    auto_close_on_github_close: bool
    auth_type: str
    webhook_secret_set: bool
    installation_id: str | None
    is_active: bool
    token_masked: str


def _connection_created_dict(c: svc.RepoConnection) -> dict[str, object]:
    """Shape a freshly created connection, revealing `webhook_secret` once (never re-served)."""
    body = _connection_dict(c)
    body["webhook_secret"] = c.webhook_secret
    return body


def _connection_dict(c: svc.RepoConnection) -> dict[str, object]:
    """Shape a `RepoConnection` via `RepoConnectionDTO` (no raw token)."""
    return asdict(
        RepoConnectionDTO(
            id=c.id,
            community_id=c.community_id,
            vendor_id=c.vendor_id,
            module_id=c.module_id,
            repo_owner=c.repo_owner,
            repo_name=c.repo_name,
            sync_mode=c.sync_mode,
            default_labels=c.default_labels,
            auto_close_on_github_close=c.auto_close_on_github_close,
            auth_type=c.auth_type,
            webhook_secret_set=bool(c.webhook_secret),
            installation_id=c.installation_id,
            is_active=c.is_active,
            token_masked=c.token_masked,
        )
    )


BLUEPRINTS: list[Blueprint] = [github_sync_webhook_bp, github_sync_admin_bp]
