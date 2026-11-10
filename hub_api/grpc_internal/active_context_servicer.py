"""gRPC servicer for `ActiveContextService` -- the data plane's per-user community context.

Replaces the former REST hop `GET|PUT /api/v1/internal/dataplane/active-context` that svc-ingest's
slash router called. The choice itself (table `user_active_context`, default = first-registered
community, stale choices yielding to the default) is owned by `services.app_command_registry`;
this adapter adds only what the data plane needs on top of it: the interceptor chain has already
verified the machine JWT and its `dataplane:active-context` scope, the servicer requires the
operator-plane `system` tenant, and the answer carries the NUMERIC community id (resolved from the
same paired set -- the registry deliberately never exposes numeric PKs) beside the `public_id`.

Every refusal is a loud gRPC status with a constant message; no id, name or user value is logged.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Final

import grpc
from flask_core.service_jwt import SYSTEM_TENANT
from waddles.hub.internal.v1 import active_context_pb2, active_context_pb2_grpc

from grpc_internal.interceptors import service_claims
from services import app_command_registry as registry
from services.dataplane_scope import AmbiguousScopeError, InvalidSourceError, ScopeNotFoundError

logger = logging.getLogger(__name__)

#: Machine-JWT scope the data plane's svc-ingest identity must hold (and no other service).
SCOPE_ACTIVE_CONTEXT: Final = "dataplane:active-context"

#: Method -> required scope, merged into the interceptor chain's table by `server.py`.
ACTIVE_CONTEXT_SCOPES: Final[dict[str, str]] = {
    "/waddles.hub.internal.v1.ActiveContextService/GetActiveContext": SCOPE_ACTIVE_CONTEXT,
    "/waddles.hub.internal.v1.ActiveContextService/SetActiveContext": SCOPE_ACTIVE_CONTEXT,
}


class ActiveContextServicer(active_context_pb2_grpc.ActiveContextServiceServicer):  # type: ignore[misc]
    """Thin adapter over `app_command_registry.{get,set}_active_context`."""

    def __init__(self, install_dal: Any = None) -> None:
        """Bind the install DAL; without one every RPC fails UNAVAILABLE (never a default)."""
        self._install_dal = install_dal

    async def _require(self, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        """Check caller identity + store; return the DAL or abort with the refusal status."""
        claims = service_claims.get()
        if not claims:
            # Reached around the interceptor chain -- fail closed, never trust the request.
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "unauthorized")
            raise AssertionError("unreachable")
        if str(claims.get("tenant", "")).strip() != SYSTEM_TENANT:
            logger.warning("active_context.wrong_tenant")
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "forbidden")
            raise AssertionError("unreachable")
        if self._install_dal is None:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "install store not configured")
            raise AssertionError("unreachable")
        return self._install_dal

    async def _require_user(self, user: str, context: grpc.aio.ServicerContext[Any, Any]) -> None:
        """Refuse a non-canonical tokenized user UUID before any lookup (INVALID_ARGUMENT)."""
        try:
            canonical = str(uuid.UUID(user)) == user.lower()
        except (ValueError, AttributeError, TypeError):
            canonical = False
        if not canonical:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid request")
            raise AssertionError("unreachable")

    async def _refuse(self, exc: Exception, context: grpc.aio.ServicerContext[Any, Any]) -> None:
        """Map a registry refusal to its gRPC status (fixed, application-authored text)."""
        if isinstance(exc, InvalidSourceError):
            code, detail = grpc.StatusCode.INVALID_ARGUMENT, "invalid request"
        elif isinstance(exc, AmbiguousScopeError):
            code, detail = grpc.StatusCode.FAILED_PRECONDITION, "source is paired across tenants"
        elif isinstance(exc, registry.ContextNotFoundError):
            code, detail = grpc.StatusCode.NOT_FOUND, "community is not paired to this source"
        else:
            code, detail = grpc.StatusCode.NOT_FOUND, "no community is linked to this source"
        logger.warning("active_context.refused type=%s", type(exc).__name__)
        await context.abort(code, detail)

    async def _answer(
        self,
        dal: Any,
        found: registry.ActiveContext,
        platform: str,
        source_id: str,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> active_context_pb2.ActiveContext:
        """Build the wire answer, adding the numeric id from the source's paired set.

        A community the registry just returned but the paired set no longer holds (the pairing
        changed between the two reads) is a loud ABORTED -- the data plane never routes on a guess.
        """
        pair = await registry.paired_source(dal, platform, source_id)
        numeric = {c.public_id: c.community_id for c in pair.communities}
        if found.community_public_id not in numeric:
            logger.error("active_context.pairing_changed_mid_request")
            await context.abort(grpc.StatusCode.ABORTED, "source pairing changed; retry")
            raise AssertionError("unreachable")
        return active_context_pb2.ActiveContext(
            community_public_id=found.community_public_id,
            community_id=numeric[found.community_public_id],
            community_name=found.community_name,
            explicit=found.explicit,
            communities=[
                active_context_pb2.SwitchableCommunity(
                    public_id=c.public_id, display_name=c.name, position=c.position
                )
                for c in found.communities
            ],
        )

    async def GetActiveContext(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: active_context_pb2.GetActiveContextRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> active_context_pb2.GetActiveContextResponse:
        """The user's active community for the source (default = first-registered)."""
        dal = await self._require(context)
        await self._require_user(request.user, context)
        try:
            found = await registry.get_active_context(
                dal, request.platform, request.source_id, request.user
            )
        except (InvalidSourceError, AmbiguousScopeError, ScopeNotFoundError) as exc:
            await self._refuse(exc, context)
            raise AssertionError("unreachable") from exc
        answer = await self._answer(dal, found, request.platform, request.source_id, context)
        return active_context_pb2.GetActiveContextResponse(context=answer)

    async def SetActiveContext(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: active_context_pb2.SetActiveContextRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> active_context_pb2.SetActiveContextResponse:
        """Switch the user's active community (a `public_id` among the source's pairings)."""
        dal = await self._require(context)
        await self._require_user(request.user, context)
        try:
            found = await registry.set_active_context(
                dal, request.platform, request.source_id, request.user, request.community
            )
        except (
            InvalidSourceError,
            AmbiguousScopeError,
            ScopeNotFoundError,
            registry.ContextNotFoundError,
        ) as exc:
            await self._refuse(exc, context)
            raise AssertionError("unreachable") from exc
        answer = await self._answer(dal, found, request.platform, request.source_id, context)
        return active_context_pb2.SetActiveContextResponse(context=answer)
