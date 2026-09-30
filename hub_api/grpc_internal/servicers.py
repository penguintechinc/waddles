"""Thin gRPC servicer adapters for `waddles.hub.internal.v1`.

Every method is a stub returning UNIMPLEMENTED -- the real logic is
ported from PRs #429 (identity/ephemeral pseudonyms), #427 (display-name
resolution) and #442 (stream DEK sealing) onto these exact method
bodies next; this PR only lands the transport, auth and observability
foundation they'll be ported onto. Keep these methods thin adapters over
plain service functions (once ported) -- no business logic here.
"""

from __future__ import annotations

from typing import Any

import grpc
from waddles.hub.internal.v1 import identity_pb2, identity_pb2_grpc, key_pb2, key_pb2_grpc

#: Method -> required scope, consumed by `interceptors.AuthInterceptor`.
REQUIRED_SCOPES: dict[str, str] = {
    "/waddles.hub.internal.v1.IdentityService/MintEphemeralPseudonyms": "identity:ephemeral:mint",
    "/waddles.hub.internal.v1.IdentityService/ResolveDisplayNames": "identity:displayname:read",
    "/waddles.hub.internal.v1.KeyService/GetStreamDek": "key:stream-dek:read",
}


class IdentityServicer(identity_pb2_grpc.IdentityServiceServicer):  # type: ignore[misc]
    """Adapter for `IdentityService` -- ported logic lands in PR #429/#427."""

    async def MintEphemeralPseudonyms(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: identity_pb2.MintEphemeralPseudonymsRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> identity_pb2.MintEphemeralPseudonymsResponse:
        """Stub -- real pseudonym minting ports from PR #429 onto this method."""
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "pending PR #429 port")
        raise AssertionError("unreachable")  # context.abort always raises

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
