"""In-process gRPC harness for the data-plane servicers, behind the REAL interceptor chain.

`serving_dataplane()` serves `ProviderConnectionService` / `DistributionService` /
`MediaService` exactly as `grpc_internal.server.build_internal_grpc_server` registers them,
behind `build_interceptor_chain` (deadline -> auth -> rate limit -> OTel), so a test exercises
signature/scope/tenant verification and the servicer's own checks -- never a servicer reached
around its interceptors. `DataplaneClient` issues a correctly scoped machine JWT per call (or a
caller-supplied token / none) over a real loopback channel.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import grpc
from flask_core.service_jwt import ServiceJwtIssuer

_PB_ROOT = Path(__file__).resolve().parents[1] / "grpc_internal" / "pb"
if str(_PB_ROOT) not in sys.path:  # generated waddles.* stubs, same as grpc_internal/__init__
    sys.path.insert(0, str(_PB_ROOT))

from waddles.hub.internal.v1 import (  # noqa: E402
    distribution_pb2_grpc,
    media_pb2_grpc,
    provider_connection_pb2_grpc,
)

from grpc_internal.dataplane_servicers import (  # noqa: E402
    DistributionServicer,
    MediaServicer,
    ProviderConnectionServicer,
)
from grpc_internal.server import build_interceptor_chain  # noqa: E402

#: Client deadline under the server's 5.0s ceiling (a literal 5 arrives a hair over it).
TEST_DEADLINE_SECONDS = 4.0


@asynccontextmanager
async def serving_dataplane(
    config: Mapping[str, Any], issuer: ServiceJwtIssuer, service_factory: Any = None
) -> AsyncIterator[str]:
    """Serve the three data-plane servicers over an insecure loopback port; yields `host:port`.

    `config` is the Quart-app-config-shaped mapping the servicers read their dependencies from;
    `service_factory` builds the `ProviderConnectionService` from it (omit when not exercised).
    """
    if service_factory is None:

        def service_factory(_config: Mapping[str, Any]) -> Any:
            raise AssertionError("this test did not configure a ProviderConnectionService")

    server = grpc.aio.server(interceptors=build_interceptor_chain(issuer))
    provider_connection_pb2_grpc.add_ProviderConnectionServiceServicer_to_server(
        ProviderConnectionServicer(config, service_factory), server
    )
    distribution_pb2_grpc.add_DistributionServiceServicer_to_server(
        DistributionServicer(config), server
    )
    media_pb2_grpc.add_MediaServiceServicer_to_server(MediaServicer(config), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(grace=None)


class DataplaneClient:
    """Unary caller that attaches `Authorization: Bearer <token>` per RPC.

    `call(method, request, response_cls, token=...)`: `token=None` sends no credential.
    Raises `grpc.aio.AioRpcError` on a non-OK status, exactly as a real client sees it.
    """

    def __init__(self, address: str) -> None:
        """Remember the target; a fresh channel is opened per call (cheap on loopback)."""
        self._address = address

    async def call(
        self,
        method: str,
        request: Any,
        response_cls: Any,
        *,
        token: str | None,
        timeout: float = TEST_DEADLINE_SECONDS,  # noqa: ASYNC109 - gRPC deadline, not asyncio
    ) -> Any:
        """Invoke `method` (full `/pkg.Service/Rpc` name) and decode `response_cls`."""
        metadata = (("authorization", f"Bearer {token}"),) if token is not None else ()
        async with grpc.aio.insecure_channel(self._address) as channel:
            rpc = channel.unary_unary(
                method,
                request_serializer=request.SerializeToString,
                response_deserializer=response_cls.FromString,
            )
            return await rpc(request, metadata=metadata, timeout=timeout)
