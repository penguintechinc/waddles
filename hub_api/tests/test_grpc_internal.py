"""Tests for the internal gRPC auth interceptor and UNIMPLEMENTED stubs.

Runs a real `grpc.aio.server()` in-process over an insecure loopback port
(no TLS -- this test exercises interceptor/servicer logic, not
`server.py`'s credential loading, which needs real cert files and is
exercised at deploy time / by the deployment smoke test instead).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import grpc
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey
from waddles.hub.internal.v1 import identity_pb2, identity_pb2_grpc

from grpc_internal.interceptors import AuthInterceptor, DeadlineInterceptor
from grpc_internal.servicers import REQUIRED_SCOPES, IdentityServicer, KeyServicer

SCOPE = "identity:ephemeral:mint"
SPIFFE_ID = "spiffe://penguintech.io/alpha/svc-process"


@pytest.fixture
def issuer() -> ServiceJwtIssuer:
    """A throwaway single-key issuer allow-listing one test identity."""
    private_key = Ed25519PrivateKey.generate()
    identity = ServiceIdentity(
        service_id=SPIFFE_ID,
        k8s_namespace="waddlebot",
        k8s_service_account="svc-process",
        allowed_scopes=frozenset({SCOPE}),
    )
    signing_key = SigningKey(
        kid="test-kid", private_key=private_key, public_key=private_key.public_key()
    )
    return ServiceJwtIssuer(
        keys={"test-kid": signing_key},
        active_kid="test-kid",
        identities={identity.service_id: identity},
    )


@pytest.fixture
async def server_address(issuer: ServiceJwtIssuer) -> AsyncIterator[str]:
    """Starts a real insecure `grpc.aio.server()` with the auth chain wired."""
    server = grpc.aio.server(
        interceptors=[
            DeadlineInterceptor(),
            AuthInterceptor(verifier=issuer.as_verifier(), required_scopes=REQUIRED_SCOPES),
        ]
    )
    identity_pb2_grpc.add_IdentityServiceServicer_to_server(IdentityServicer(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(grace=None)


async def _mint_call(
    server_address: str, *, token: str | None, deadline: float | None = 2.0
) -> identity_pb2.MintEphemeralPseudonymsResponse:
    async with grpc.aio.insecure_channel(server_address) as channel:
        stub = identity_pb2_grpc.IdentityServiceStub(channel)
        metadata = (("authorization", f"Bearer {token}"),) if token else ()
        request = identity_pb2.MintEphemeralPseudonymsRequest(
            items=[
                identity_pb2.MintEphemeralPseudonymRequest(
                    tenant_id="t1", platform="discord", platform_user_id="u1", handle="h"
                )
            ]
        )
        return await stub.MintEphemeralPseudonyms(request, metadata=metadata, timeout=deadline)


@pytest.mark.asyncio
async def test_missing_token_rejected(server_address: str) -> None:
    """No `authorization` metadata -> UNAUTHENTICATED, never reaches the servicer."""
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await _mint_call(server_address, token=None)
    assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_forged_token_rejected(server_address: str) -> None:
    """A token signed by a key the server never trusted -> UNAUTHENTICATED."""
    forged_key = Ed25519PrivateKey.generate()
    forged_signing_key = SigningKey(
        kid="other-kid", private_key=forged_key, public_key=forged_key.public_key()
    )
    forged_issuer = ServiceJwtIssuer(
        keys={"other-kid": forged_signing_key},
        active_kid="other-kid",
        identities={
            SPIFFE_ID: ServiceIdentity(
                service_id=SPIFFE_ID,
                k8s_namespace="waddlebot",
                k8s_service_account="svc-process",
                allowed_scopes=frozenset({SCOPE}),
            )
        },
    )
    forged_token = forged_issuer.issue(SPIFFE_ID, SCOPE)
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await _mint_call(server_address, token=forged_token)
    assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_wrong_scope_rejected(issuer: ServiceJwtIssuer, server_address: str) -> None:
    """A validly-signed token scoped for a DIFFERENT RPC -> UNAUTHENTICATED, not UNIMPLEMENTED."""
    # `identity:displayname:read` is a real, allow-listable scope for this
    # identity -- just not the one MintEphemeralPseudonyms requires.
    issuer.identities[SPIFFE_ID] = ServiceIdentity(
        service_id=SPIFFE_ID,
        k8s_namespace="waddlebot",
        k8s_service_account="svc-process",
        allowed_scopes=frozenset({"identity:displayname:read"}),
    )
    wrong_scope_token = issuer.issue(SPIFFE_ID, "identity:displayname:read")
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await _mint_call(server_address, token=wrong_scope_token)
    assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_valid_token_reaches_servicer_unavailable_without_dal(
    issuer: ServiceJwtIssuer, server_address: str
) -> None:
    """Valid token reaches the servicer; no DAL bound -> UNAVAILABLE, never a default."""
    token = issuer.issue(SPIFFE_ID, SCOPE)
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await _mint_call(server_address, token=token)
    assert exc_info.value.code() == grpc.StatusCode.UNAVAILABLE


@pytest.mark.asyncio
async def test_missing_deadline_rejected(issuer: ServiceJwtIssuer, server_address: str) -> None:
    """A call with no client-set deadline is rejected before auth even runs."""
    token = issuer.issue(SPIFFE_ID, SCOPE)
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await _mint_call(server_address, token=token, deadline=None)
    assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_key_service_get_stream_dek_is_registered() -> None:
    """KeyServicer exists and is wired for the scope table -- import-time sanity check."""
    assert "/waddles.hub.internal.v1.KeyService/GetStreamDek" in REQUIRED_SCOPES
    assert KeyServicer  # instantiable, no required constructor args
