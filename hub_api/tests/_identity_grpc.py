"""In-process gRPC harness for identity tests that runs the REAL interceptor chain.

The identity servicers authorize every request against the verified token claims the
interceptors store, so a test server that adds the servicer directly (no interceptors)
exercises nothing real -- the claims are never populated and every call fails closed.
`serving()` builds the server from `grpc_internal.server.build_interceptor_chain`, the
same function production uses, and `AuthedIdentityStub` attaches a freshly issued,
correctly scoped machine JWT per RPC, so tests go through deadline -> auth (signature,
audience, scope, tenant claim) -> rate limit -> OTel -> servicer exactly as in prod.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import grpc
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.service_jwt import SYSTEM_TENANT, ServiceIdentity, ServiceJwtIssuer, SigningKey

_PB_ROOT = Path(__file__).resolve().parents[1] / "grpc_internal" / "pb"
if str(_PB_ROOT) not in sys.path:  # generated waddles.* stubs, same as grpc_internal/__init__
    sys.path.insert(0, str(_PB_ROOT))

from waddles.hub.internal.v1 import identity_pb2, identity_pb2_grpc  # noqa: E402

from grpc_internal.server import build_interceptor_chain  # noqa: E402
from grpc_internal.servicers import REQUIRED_SCOPES, IdentityServicer  # noqa: E402

SPIFFE_ID = "spiffe://penguintech.io/alpha/svc-process"
MAX_TEST_DEADLINE_SECONDS = 4.0
_SERVICE = "/waddles.hub.internal.v1.IdentityService/"
IDENTITY_SCOPES = frozenset(
    {
        "identity:ephemeral:mint",
        "identity:displayname:read",
        "identity:handle:resolve",
    }
)


def make_issuer(
    tenant: str = SYSTEM_TENANT,
    *,
    service_id: str = SPIFFE_ID,
    scopes: frozenset[str] = IDENTITY_SCOPES,
) -> ServiceJwtIssuer:
    """A throwaway single-key issuer with one allow-listed identity bound to `tenant`.

    `tenant=""` models a legacy/misconfigured identity whose tokens carry no tenant claim.
    """
    key = Ed25519PrivateKey.generate()
    identity = ServiceIdentity(
        service_id=service_id,
        k8s_namespace="waddlebot",
        k8s_service_account="svc-process",
        allowed_scopes=scopes,
        tenant=tenant,
    )
    signing = SigningKey(kid="test-kid", private_key=key, public_key=key.public_key())
    return ServiceJwtIssuer(
        keys={"test-kid": signing},
        active_kid="test-kid",
        identities={identity.service_id: identity},
    )


#: Operator-plane (system tenant) issuer used by tests that are not about tenant scoping.
DEFAULT_ISSUER = make_issuer()


@asynccontextmanager
async def serving(async_dal: Any, issuer: ServiceJwtIssuer | None = None) -> AsyncIterator[str]:
    """Serve `IdentityServicer(async_dal)` behind the production interceptor chain.

    Yields the `host:port` address of an insecure loopback listener (TLS credential
    loading is covered by `test_grpc_internal_tls.py`); the verifier trusts `issuer`.
    """
    server = grpc.aio.server(interceptors=build_interceptor_chain(issuer or DEFAULT_ISSUER))
    identity_pb2_grpc.add_IdentityServiceServicer_to_server(IdentityServicer(async_dal), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(grace=None)


def _bounded(timeout: float) -> float:
    """Clamp a test's client deadline under the server's 5.0s ceiling (it rejects >= 5.0)."""
    return min(timeout, MAX_TEST_DEADLINE_SECONDS)


class AuthedIdentityStub:
    """`IdentityServiceStub` that attaches a freshly issued token scoped to each RPC.

    Client deadlines are clamped below `DeadlineInterceptor`'s 5.0s ceiling (a literal
    `timeout=5` arrives a hair over it and is rejected).
    """

    def __init__(
        self,
        channel: grpc.aio.Channel,
        issuer: ServiceJwtIssuer | None = None,
        service_id: str = SPIFFE_ID,
    ) -> None:
        """Wrap `channel`; tokens are issued by `issuer` (default: the system-tenant one)."""
        self._stub = identity_pb2_grpc.IdentityServiceStub(channel)
        self._issuer = issuer or DEFAULT_ISSUER
        self._service_id = service_id

    def _metadata(self, rpc: str) -> tuple[tuple[str, str], ...]:
        token = self._issuer.issue(self._service_id, REQUIRED_SCOPES[_SERVICE + rpc])
        return (("authorization", f"Bearer {token}"),)

    async def MintEphemeralPseudonyms(  # noqa: N802 - mirrors the generated stub
        self,
        request: identity_pb2.MintEphemeralPseudonymsRequest,
        timeout: float = 5,  # noqa: ASYNC109
    ) -> identity_pb2.MintEphemeralPseudonymsResponse:
        """Mint call with a mint-scoped token."""
        return await self._stub.MintEphemeralPseudonyms(
            request,
            metadata=self._metadata("MintEphemeralPseudonyms"),
            timeout=_bounded(timeout),
        )

    async def ResolveDisplayNames(  # noqa: N802 - mirrors the generated stub
        self,
        request: identity_pb2.ResolveDisplayNamesRequest,
        timeout: float = 5,  # noqa: ASYNC109
    ) -> identity_pb2.ResolveDisplayNamesResponse:
        """Display-name call with a displayname-scoped token."""
        return await self._stub.ResolveDisplayNames(
            request,
            metadata=self._metadata("ResolveDisplayNames"),
            timeout=_bounded(timeout),
        )

    async def ResolveHandle(  # noqa: N802 - mirrors the generated stub
        self,
        request: identity_pb2.ResolveHandleRequest,
        timeout: float = 5,  # noqa: ASYNC109
    ) -> identity_pb2.ResolveHandleResponse:
        """Handle call with a handle-scoped token."""
        return await self._stub.ResolveHandle(
            request, metadata=self._metadata("ResolveHandle"), timeout=_bounded(timeout)
        )
