"""gRPC servicer for `DataplaneScopeService` -- source -> numeric tenant/community scope.

Replaces the former REST hop `GET /api/v1/internal/dataplane/resolve-scope` (+ `/batch`) that
svc-ingest's scope resolver called per cold source. The resolution rules live unchanged in
`services.dataplane_scope` (approved links beat legacy claims, cross-tenant claims refuse, inactive
rows never resolve); this adapter adds only the transport contract: the interceptor chain has
already verified the machine JWT and its `dataplane:resolve-scope` scope, the servicer requires the
operator-plane `system` tenant, and every refusal is a loud gRPC status with a constant message
(NOT_FOUND = "not linked", never a guessed scope). Ids and display names only; no value from the
request is logged.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import grpc
from flask_core.service_jwt import SYSTEM_TENANT
from waddles.hub.internal.v1 import dataplane_scope_pb2, dataplane_scope_pb2_grpc

from grpc_internal.interceptors import service_claims
from services import dataplane_scope as scopes

logger = logging.getLogger(__name__)

#: Machine-JWT scope the data plane's svc-ingest identity must hold (and no other service).
SCOPE_DATAPLANE_RESOLVE_SCOPE: Final = "dataplane:resolve-scope"

#: Method -> required scope, merged into the interceptor chain's table by `server.py`.
DATAPLANE_SCOPE_SCOPES: Final[dict[str, str]] = {
    "/waddles.hub.internal.v1.DataplaneScopeService/ResolveScope": SCOPE_DATAPLANE_RESOLVE_SCOPE,
    "/waddles.hub.internal.v1.DataplaneScopeService/ResolveScopeBatch": (
        SCOPE_DATAPLANE_RESOLVE_SCOPE
    ),
}

_STATUS = dataplane_scope_pb2.ScopeStatus


def _scope_message(found: scopes.ResolvedScope) -> dataplane_scope_pb2.ResolveScopeResponse:
    """The wire form of one resolved scope."""
    return dataplane_scope_pb2.ResolveScopeResponse(
        tenant_id=found.tenant_id,
        community_id=found.community_id,
        tenant_slug=found.tenant_slug,
        community_name=found.community_name,
    )


class DataplaneScopeServicer(dataplane_scope_pb2_grpc.DataplaneScopeServiceServicer):  # type: ignore[misc]
    """Thin adapter over `services.dataplane_scope.resolve_scope`."""

    def __init__(self, async_dal: Any = None) -> None:
        """Bind the AsyncDAL; without one every RPC fails UNAVAILABLE (never a default scope)."""
        self._async_dal = async_dal

    async def _require(self, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        """Check caller identity + store; return the AsyncDAL or abort with the refusal status."""
        claims = service_claims.get()
        if not claims:
            # Reached around the interceptor chain -- fail closed, never trust the request.
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "unauthorized")
            raise AssertionError("unreachable")
        if str(claims.get("tenant", "")).strip() != SYSTEM_TENANT:
            logger.warning("dataplane_scope.wrong_tenant")
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "forbidden")
            raise AssertionError("unreachable")
        if self._async_dal is None:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "scope store not configured")
            raise AssertionError("unreachable")
        return self._async_dal

    async def ResolveScope(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: dataplane_scope_pb2.ResolveScopeRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> dataplane_scope_pb2.ResolveScopeResponse:
        """Resolve one source; INVALID_ARGUMENT / NOT_FOUND / FAILED_PRECONDITION on refusal."""
        dal = await self._require(context)
        try:
            found = await scopes.resolve_scope(dal, dal.dal, request.platform, request.source_id)
        except scopes.InvalidSourceError:
            code, detail = grpc.StatusCode.INVALID_ARGUMENT, "platform or source_id is not valid"
        except scopes.AmbiguousScopeError:
            code = grpc.StatusCode.FAILED_PRECONDITION
            detail = "source maps to more than one community"
        except scopes.ScopeNotFoundError:
            code, detail = grpc.StatusCode.NOT_FOUND, "no community is linked to this source"
        else:
            return _scope_message(found)
        logger.debug("dataplane_scope.refused code=%s", code.name)
        await context.abort(code, detail)
        raise AssertionError("unreachable")

    async def ResolveScopeBatch(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: dataplane_scope_pb2.ResolveScopeBatchRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> dataplane_scope_pb2.ResolveScopeBatchResponse:
        """Resolve 1..MAX_BATCH sources; each result carries its own status, order preserved."""
        dal = await self._require(context)
        if not 1 <= len(request.items) <= scopes.MAX_BATCH:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"items must contain between 1 and {scopes.MAX_BATCH} entries",
            )
            raise AssertionError("unreachable")
        results: list[dataplane_scope_pb2.ScopeResult] = []
        for item in request.items:
            result = dataplane_scope_pb2.ScopeResult(
                platform=item.platform, source_id=item.source_id
            )
            try:
                found = await scopes.resolve_scope(dal, dal.dal, item.platform, item.source_id)
            except scopes.InvalidSourceError:
                result.status = _STATUS.SCOPE_STATUS_INVALID
            except scopes.AmbiguousScopeError:
                result.status = _STATUS.SCOPE_STATUS_AMBIGUOUS
            except scopes.ScopeNotFoundError:
                result.status = _STATUS.SCOPE_STATUS_NOT_FOUND
            else:
                result.status = _STATUS.SCOPE_STATUS_RESOLVED
                result.scope.CopyFrom(_scope_message(found))
            results.append(result)
        return dataplane_scope_pb2.ResolveScopeBatchResponse(results=results)
