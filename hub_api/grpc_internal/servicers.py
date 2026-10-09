"""Thin gRPC servicer adapters for `waddles.hub.internal.v1`.

`MintEphemeralPseudonyms` is real (#429, delegates to
`services.identity_resolution_service`). `ResolveDisplayNames` (#427) and
`GetStreamDek` (#442) remain UNIMPLEMENTED stubs. Keep these methods thin
adapters over plain service functions -- no business logic here.
"""

from __future__ import annotations

from typing import Any

import grpc
from waddles.hub.internal.v1 import identity_pb2, identity_pb2_grpc, key_pb2, key_pb2_grpc

from services.identity_resolution_service import (
    IdentityRequest,
    IdentityResolutionError,
    IdentityValidationError,
    TenantNotFoundError,
    resolve_identities,
)

#: Method -> required scope, consumed by `interceptors.AuthInterceptor`.
REQUIRED_SCOPES: dict[str, str] = {
    "/waddles.hub.internal.v1.IdentityService/MintEphemeralPseudonyms": "identity:ephemeral:mint",
    "/waddles.hub.internal.v1.IdentityService/ResolveDisplayNames": "identity:displayname:read",
    "/waddles.hub.internal.v1.KeyService/GetStreamDek": "key:stream-dek:read",
}


class IdentityServicer(identity_pb2_grpc.IdentityServiceServicer):  # type: ignore[misc]
    """Adapter for `IdentityService` -- mint is real (#429); display names land in #427."""

    def __init__(self, async_dal: Any = None) -> None:
        """Bind the AsyncDAL; without one, mint fails UNAVAILABLE (never a default)."""
        self._async_dal = async_dal

    async def MintEphemeralPseudonyms(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: identity_pb2.MintEphemeralPseudonymsRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> identity_pb2.MintEphemeralPseudonymsResponse:
        """Mint-or-return the stable UUID for each platform identity (batch <=100)."""
        if self._async_dal is None:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "identity store not configured")
            raise AssertionError("unreachable")
        items = [
            IdentityRequest(i.tenant_id, i.platform, i.platform_user_id, i.handle)
            for i in request.items
        ]
        try:
            resolved = await resolve_identities(self._async_dal, items)
        except IdentityValidationError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise AssertionError("unreachable") from exc
        except TenantNotFoundError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            raise AssertionError("unreachable") from exc
        except IdentityResolutionError as exc:
            await context.abort(grpc.StatusCode.INTERNAL, "identity resolution failed")
            raise AssertionError("unreachable") from exc
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
        """Stub -- real display-name resolution ports from PR #427 onto this method."""
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "ResolveDisplayNames: pending PR #427")
        raise AssertionError("unreachable")


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
