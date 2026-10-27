"""Thin gRPC servicer adapters for `waddles.hub.internal.v1`.

`MintEphemeralPseudonyms` (#429), `ResolveDisplayNames` (#427) and `ResolveHandle`
are real and delegate to `services.identity_resolution_service`; `GetStreamDek`
(#442) remains an UNIMPLEMENTED stub. Keep these methods thin adapters over plain
service functions -- no business logic here. Every failure aborts with a gRPC
status; none returns a default UUID or name.
"""

from __future__ import annotations

from typing import Any, NoReturn

import grpc
from waddles.hub.internal.v1 import identity_pb2, identity_pb2_grpc, key_pb2, key_pb2_grpc

from grpc_internal.interceptors import service_claims
from services.identity_resolution_service import (
    AmbiguousHandleError,
    HandleNotFoundError,
    IdentityRequest,
    IdentityResolutionError,
    IdentityValidationError,
    TenantAccessDeniedError,
    TenantNotFoundError,
    authorize_tenant,
    resolve_display_names,
    resolve_identities,
    resolve_target,
)

#: Method -> required scope, consumed by `interceptors.AuthInterceptor`.
REQUIRED_SCOPES: dict[str, str] = {
    "/waddles.hub.internal.v1.IdentityService/MintEphemeralPseudonyms": "identity:ephemeral:mint",
    "/waddles.hub.internal.v1.IdentityService/ResolveDisplayNames": "identity:displayname:read",
    "/waddles.hub.internal.v1.IdentityService/ResolveHandle": "identity:handle:resolve",
    "/waddles.hub.internal.v1.KeyService/GetStreamDek": "key:stream-dek:read",
}

_MATCH_KINDS: dict[str, int] = {
    "mention": identity_pb2.MATCH_KIND_MENTION,
    "handle": identity_pb2.MATCH_KIND_HANDLE,
}


async def _abort_for(
    exc: IdentityResolutionError, context: grpc.aio.ServicerContext[Any, Any]
) -> NoReturn:
    """Map a resolution failure to its gRPC status (messages are PII-free constants)."""
    if isinstance(exc, IdentityValidationError):
        code, detail = grpc.StatusCode.INVALID_ARGUMENT, str(exc)
    elif isinstance(exc, TenantNotFoundError | HandleNotFoundError):
        code, detail = grpc.StatusCode.NOT_FOUND, str(exc)
    elif isinstance(exc, AmbiguousHandleError):
        code, detail = grpc.StatusCode.FAILED_PRECONDITION, str(exc)
    elif isinstance(exc, TenantAccessDeniedError):
        code, detail = grpc.StatusCode.PERMISSION_DENIED, str(exc)
    else:
        code, detail = grpc.StatusCode.INTERNAL, "identity resolution failed"
    await context.abort(code, detail)
    raise AssertionError("unreachable") from exc


class IdentityServicer(identity_pb2_grpc.IdentityServiceServicer):  # type: ignore[misc]
    """Adapter for `IdentityService`: pseudonym mint (#429), display names (#427), handles."""

    def __init__(self, async_dal: Any = None) -> None:
        """Bind the AsyncDAL; without one every RPC fails UNAVAILABLE (never a default)."""
        self._async_dal = async_dal

    async def _require_dal(self, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        """Return the bound AsyncDAL or abort UNAVAILABLE."""
        if self._async_dal is None:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "identity store not configured")
            raise AssertionError("unreachable")
        return self._async_dal

    async def _caller(self, context: grpc.aio.ServicerContext[Any, Any]) -> tuple[str, str]:
        """Return `(tenant claim, sub)` of the verified caller, or abort UNAUTHENTICATED.

        Reads the claims `AuthInterceptor` stored after verifying the token. Absent
        claims mean this servicer was reached around the interceptor chain -- fail
        closed rather than fall back to trusting the request body's tenant.
        """
        claims = service_claims.get()
        tenant = claims.get("tenant") if claims else None
        if not claims or not isinstance(tenant, str) or not tenant.strip():
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "unauthorized")
            raise AssertionError("unreachable")
        return tenant.strip(), str(claims.get("sub", ""))

    async def MintEphemeralPseudonyms(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: identity_pb2.MintEphemeralPseudonymsRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> identity_pb2.MintEphemeralPseudonymsResponse:
        """Mint-or-return the stable UUID for each platform identity (batch <=100).

        Every distinct tenant in the batch is authorized against the caller's token
        before anything is minted -- one denied tenant rejects the whole batch.
        """
        claim_tenant, caller = await self._caller(context)
        dal = await self._require_dal(context)
        try:
            effective: dict[str, str] = {}
            for ref in dict.fromkeys(i.tenant_id for i in request.items):
                effective[ref] = await authorize_tenant(
                    dal, claim_tenant, ref, op="mint", caller=caller
                )
            items = [
                IdentityRequest(effective[i.tenant_id], i.platform, i.platform_user_id, i.handle)
                for i in request.items
            ]
            resolved = await resolve_identities(dal, items)
        except IdentityResolutionError as exc:
            await _abort_for(exc, context)
        return identity_pb2.MintEphemeralPseudonymsResponse(
            pseudonyms=[
                identity_pb2.EphemeralPseudonym(
                    platform_user_id=r.platform_user_id, pseudonym=str(r.uuid)
                )
                for r in resolved
            ]
        )

    async def ResolveDisplayNames(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: identity_pb2.ResolveDisplayNamesRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> identity_pb2.ResolveDisplayNamesResponse:
        """Resolve a tenant-scoped batch of UUIDs to display names (egress detokenizer)."""
        claim_tenant, caller = await self._caller(context)
        dal = await self._require_dal(context)
        try:
            tenant = await authorize_tenant(
                dal, claim_tenant, request.tenant_id, op="display_names", caller=caller
            )
            result = await resolve_display_names(dal, tenant, list(request.uuids))
        except IdentityResolutionError as exc:
            await _abort_for(exc, context)
        return identity_pb2.ResolveDisplayNamesResponse(
            names=[
                identity_pb2.ResolvedDisplayName(
                    uuid=n.uuid, display_name=n.display_name, is_hub_user=n.is_hub_user
                )
                for n in result.names
            ],
            unresolved_uuids=list(result.unresolved),
        )

    async def ResolveHandle(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: identity_pb2.ResolveHandleRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> identity_pb2.ResolveHandleResponse:
        """Resolve one raw handle/mention to a UUID; the handle never leaves hub-api.

        Only a verified login (never a display name / nickname) of a CURRENT tenant member
        resolves; a mention never mints for a non-member. Every refusal -- unknown, not a
        member, display-name-only, ambiguous -- is a loud NOT_FOUND / FAILED_PRECONDITION,
        and `HandleUnverifiedError` / `TargetNotMemberError` map to NOT_FOUND like any miss.
        """
        claim_tenant, caller = await self._caller(context)
        dal = await self._require_dal(context)
        try:
            tenant = await authorize_tenant(
                dal, claim_tenant, request.tenant_id, op="resolve_handle", caller=caller
            )
            resolved = await resolve_target(dal, tenant, request.platform, request.target)
        except IdentityResolutionError as exc:
            await _abort_for(exc, context)
        return identity_pb2.ResolveHandleResponse(
            uuid=str(resolved.uuid), match_kind=_MATCH_KINDS[resolved.kind]
        )


class KeyServicer(key_pb2_grpc.KeyServiceServicer):  # type: ignore[misc]
    """Adapter for `KeyService` -- ported logic lands in PR #442."""

    async def GetStreamDek(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: key_pb2.GetStreamDekRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> key_pb2.GetStreamDekResponse:
        """Stub -- real DEK sealing ports from PR #442 onto this method."""
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "GetStreamDek: pending PR #442")
        raise AssertionError("unreachable")
