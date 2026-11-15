"""
MCP transport -- Streamable HTTP (JSON response mode)
=========================================================

Per docs/plans/2026-08-26-v3-scbm-apps-design.md ``Interaction surfaces``
Open items: "a hosted server needs HTTP/SSE or streamable HTTP... pick one
and version it alongside REST" and "whether MCP is its own container or
rides in `hub-api`... riding along is the default until it earns a
split."

**Chosen: MCP Streamable HTTP transport (protocol 2025-06-18), JSON
response mode only.** A single ``POST /mcp/v1`` endpoint accepts a
JSON-RPC 2.0 request and returns a JSON-RPC response body directly,
rather than upgrading the response to ``text/event-stream``. This is a
spec-legal subset of Streamable HTTP -- a server MAY answer a POST with
either ``application/json`` or ``text/event-stream``; no server-initiated
push is needed for ``tools/list``/``tools/call``, so only the former is
implemented. It also keeps this Quart-native: the official ``mcp`` SDK's
own Streamable HTTP server is Starlette-based, which would mean either
importing Starlette into a Quart application (never -- see
`penguin-python-dev`'s Stack Decisions) or running a second ASGI app
beside this one. A hand-rolled JSON-RPC endpoint avoids both.

Mounted at ``/mcp/v1`` -- versioned alongside REST's ``/api/v{major}``
convention (backend.md API Versioning), since a breaking MCP tool-shape
change is exactly the kind of change REST versions for.

**Tenant verification is reimplemented here, not delegated to
``tenancy.tenant_middleware``**, for one reason: the ``tools/call`` scope
gate needs the decoded JWT's ``roles`` claim, and the middleware decorator
does not publish it (only the resolved ``TenantContext``). This module
calls the exact same underlying functions the decorator calls --
``auth.verify_jwt_token`` then ``tenancy.resolve_tenant_context``, in the
same order -- so there is still exactly one tenant-verification
implementation, just not the decorator wrapper around it. The scope and
feature/licensing gates live below this transport, in
``mcp_server.authorize_and_resolve_tool_call``, shared by any future
transport (stdio, SSE) the same way.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from quart import Blueprint, current_app, jsonify, request

from .app_binding import AppInstallation, InstallationLookup
from .auth import verify_jwt_token
from .mcp_server import (
    MCP_PROTOCOL_VERSION,
    McpAuthorizationError,
    authorize_and_resolve_tool_call,
    effective_scopes,
    list_tools_for_tenant,
)
from .tenancy import TenantIsolationError, resolve_tenant_context

logger = logging.getLogger(__name__)

mcp_bp = Blueprint("mcp", __name__, url_prefix="/mcp/v1")

# Standard JSON-RPC 2.0 error codes.
_JSONRPC_INVALID_REQUEST = -32600
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602

# MCP gate-failure codes (outside the standard JSON-RPC range) -- one per
# `McpAuthorizationError.reason`, so a client/test can distinguish *why*
# tools/call was denied without string-matching the message.
_MCP_ERROR_CODES: Dict[str, int] = {
    "unknown_tool": -32001,
    "scope_denied": -32002,
    "feature_disabled": -32003,
    "no_app_bound": -32004,
}
_MCP_ERROR_DEFAULT_CODE = -32000


class _EmptyInstallationLookup:
    """
    Default `InstallationLookup` -- no explicit App bindings are recorded
    for MCP calls yet (no MCP-specific rows exist in `app_installations`),
    so `resolve_app` always falls through to each Feature's shipped
    default App. A deployment with real bindings injects its own lookup
    via `current_app.config["mcp_installations"]`.
    """

    async def find(
        self, feature: str, *, tenant: str, community: Optional[int]
    ) -> List[AppInstallation]:
        """No bindings recorded -- resolve_app falls through to the default App."""
        return []


class _AuthError(Exception):
    """Internal signal from `_resolve_identity` -- carries the HTTP status to answer with."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


async def _resolve_identity(dal: Any) -> tuple[Any, Dict[str, Any]]:
    """
    Decode + verify the bearer JWT and resolve tenant -- the same two
    calls, same order, `tenancy.tenant_middleware` makes, but also
    returning the decoded payload (needed for the `roles` claim below).
    Raises `_AuthError` on any failure; never returns a partial identity.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise _AuthError(401, "Authentication required")

    payload = verify_jwt_token(auth_header[7:])
    if payload is None:
        raise _AuthError(401, "Invalid or expired token")

    try:
        tenant_ctx = await resolve_tenant_context(payload, dal)
    except TenantIsolationError as exc:
        raise _AuthError(403, str(exc)) from exc

    return tenant_ctx, payload


def _rpc_result(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(
    request_id: Any, code: int, message: str, data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


@mcp_bp.route("", methods=["POST"])
async def mcp_endpoint() -> Any:
    """
    Single Streamable-HTTP entrypoint: dispatches ``initialize``,
    ``tools/list``, and ``tools/call`` by JSON-RPC ``method``.

    Tenant is verified FIRST, before the request body is even
    interpreted past its JSON-RPC envelope -- design doc ordering,
    "tenant check -> scope check -> feature/licensing check" starts at
    the transport boundary, not inside a specific method handler.
    """
    dal = current_app.config.get("dal")
    try:
        tenant_ctx, payload = await _resolve_identity(dal)
    except _AuthError as exc:
        logger.warning(
            "mcp.identity_denied",
            extra={
                "event_type": "AUTH",
                "action": "mcp_endpoint",
                "result": "FORBIDDEN",
            },
        )
        return jsonify(
            _rpc_error(None, _JSONRPC_INVALID_REQUEST, exc.message)
        ), exc.status_code

    body = await request.get_json(silent=True)
    if not isinstance(body, dict) or "method" not in body:
        return jsonify(
            _rpc_error(
                None, _JSONRPC_INVALID_REQUEST, "request must be a JSON-RPC object"
            )
        ), 400

    request_id = body.get("id")
    method = body["method"]
    params = body.get("params") or {}
    if not isinstance(method, str):
        return jsonify(
            _rpc_error(
                request_id, _JSONRPC_INVALID_REQUEST, "'method' must be a string"
            )
        ), 400

    # A JSON-RPC request with no 'id' is a notification (e.g.
    # 'notifications/initialized') -- acknowledged, not dispatched, no body.
    if "id" not in body:
        return "", 202

    if method == "initialize":
        return jsonify(
            _rpc_result(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "waddlebot-hub-api", "version": "3.0"},
                },
            )
        )

    if method == "tools/list":
        community = params.get("community_id")
        tools = await list_tools_for_tenant(
            tenant=tenant_ctx.tenant_slug, community=community
        )
        return jsonify(
            _rpc_result(request_id, {"tools": [tool.to_dict() for tool in tools]})
        )

    if method == "tools/call":
        return await _handle_tools_call(
            request_id, params, tenant_ctx=tenant_ctx, payload=payload
        )

    return jsonify(
        _rpc_error(request_id, _JSONRPC_METHOD_NOT_FOUND, f"unknown method {method!r}")
    ), 404


async def _handle_tools_call(
    request_id: Any, params: Dict[str, Any], *, tenant_ctx: Any, payload: Dict[str, Any]
) -> Any:
    """``tools/call``: resolve caller scopes, then run the below-the-surface gate chain."""
    tool_name = params.get("name")
    if not isinstance(tool_name, str):
        return jsonify(
            _rpc_error(request_id, _JSONRPC_INVALID_PARAMS, "'name' is required")
        ), 400

    arguments = params.get("arguments") or {}
    community = arguments.get("community_id")
    granted_scopes = effective_scopes(payload.get("roles", []))
    installations: InstallationLookup = (
        current_app.config.get("mcp_installations") or _EmptyInstallationLookup()
    )

    try:
        contract, app = await authorize_and_resolve_tool_call(
            tool_name,
            tenant=tenant_ctx.tenant_slug,
            community=community,
            granted_scopes=granted_scopes,
            installations=installations,
        )
    except McpAuthorizationError as exc:
        code = _MCP_ERROR_CODES.get(exc.reason, _MCP_ERROR_DEFAULT_CODE)
        logger.warning(
            "mcp.tools_call_denied",
            extra={
                "event_type": "AUTH",
                "action": "tools/call",
                "result": exc.reason,
                "tool": tool_name,
                "tenant": tenant_ctx.tenant_slug,
            },
        )
        return jsonify(_rpc_error(request_id, code, str(exc), {"reason": exc.reason}))

    passthrough_args = arguments.get("arguments", {})
    result_text = (
        f"Dispatched {contract.id}@{contract.version} for tenant {tenant_ctx.tenant_slug!r} "
        f"(community={community!r}) to app {app.app_id!r} ({app.name} v{app.version}); "
        f"arguments={passthrough_args!r}"
    )
    return jsonify(
        _rpc_result(
            request_id,
            {"content": [{"type": "text", "text": result_text}], "isError": False},
        )
    )


def create_mcp_blueprint() -> Blueprint:
    """
    Return the MCP Streamable-HTTP blueprint for mounting into any Quart
    app -- ``app.register_blueprint(create_mcp_blueprint())``. hub-api
    (`k8s/helm/waddlebot/templates/hub-api.yaml`) has no app code of its
    own yet (Task 0.5 skeleton); once it does, mounting this blueprint
    is the entire integration: hub-api must set ``app.config["dal"]``
    (already required for any tenant-scoped route) and, optionally,
    ``app.config["mcp_installations"]`` once real App bindings exist.
    """
    return mcp_bp
