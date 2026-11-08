"""`waddle_transports.community_credentials` against a LIVE hub-api Quart app over real HTTP.

No httpx mocking: hypercorn serves the real `connections_internal_bp` on a loopback port, the real
gRPC `ServiceAuthService` (production interceptor chain) serves the machine-JWT mint on another, and
the library's own gRPC mint client + `httpx.AsyncClient` round-trip to them. Only the Kubernetes
TokenReview call (the cluster is not available here) and the provider-token DB read are stubbed.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import grpc
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey, spiffe_id
from hypercorn.asyncio import serve
from hypercorn.config import Config
from quart import Quart, request
from waddle_transports import community_credentials, service_jwt_client
from waddles.hub.internal.v1 import service_auth_pb2_grpc

import blueprints.v1.community_connections as connections_module
from grpc_internal.server import build_interceptor_chain
from grpc_internal.service_auth_servicer import ServiceAuthServicer
from services.community_connections import DecryptedTokens

SERVICE_ID = spiffe_id("alpha", "svc-action")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
async def live_hub(
    community_db: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> AsyncIterator[tuple[str, int, list[dict[str, str]]]]:
    """Serve the real issuer + token route on loopback; yield `(base_url, community_id, seen)`."""
    dal, community_id = community_db
    private = Ed25519PrivateKey.generate()
    identity = ServiceIdentity(
        SERVICE_ID, "waddlebot", "svc-action", frozenset({"connections:resolve"}), "system"
    )
    issuer = ServiceJwtIssuer(
        keys={"k1": SigningKey("k1", private, private.public_key())},
        active_kid="k1",
        identities={SERVICE_ID: identity},
    )
    app = Quart(__name__)
    app.register_blueprint(connections_module.connections_internal_bp)
    app.config["dal"] = dal
    app.config["async_dal"] = dal
    app.config["SERVICE_JWT_ISSUER"] = issuer
    app.config["SERVICE_JWT_VERIFIER"] = issuer.as_verifier()
    seen: list[dict[str, str]] = []

    @app.before_request
    async def _record() -> None:
        seen.append({"path": request.path, **{k.lower(): v for k, v in request.headers.items()}})

    grpc_server = grpc.aio.server(interceptors=build_interceptor_chain(issuer))
    service_auth_pb2_grpc.add_ServiceAuthServiceServicer_to_server(
        ServiceAuthServicer(issuer, token_reviewer=lambda **_kw: ("waddlebot", "svc-action")),
        grpc_server,
    )
    grpc_port = grpc_server.add_insecure_port("127.0.0.1:0")
    await grpc_server.start()
    monkeypatch.setattr(
        connections_module.connections_svc,
        "get_decrypted_tokens",
        AsyncMock(
            return_value=DecryptedTokens(
                access_token="AT_LIVE",  # noqa: S106
                refresh_token=None,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scopes=["a"],
            )
        ),
    )
    sa = tmp_path / "sa-token"
    sa.write_text("projected-sa", encoding="utf-8")
    monkeypatch.setenv("SERVICE_JWT_SA_TOKEN_PATH", str(sa))
    monkeypatch.setenv("SERVICE_JWT_TOKEN_ENDPOINT", f"http://127.0.0.1:{grpc_port}")
    # A valid shared key exists in the environment -- the client must never send it.
    monkeypatch.setenv("SERVICE_API_KEY", "test-service-key")

    port = _free_port()
    config = Config()
    config.bind = [f"127.0.0.1:{port}"]
    shutdown = asyncio.Event()
    task = asyncio.create_task(serve(app, config, shutdown_trigger=shutdown.wait))
    for _ in range(100):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            break
        except OSError:
            await asyncio.sleep(0.05)
    else:
        raise AssertionError("live hub-api test server did not start")
    community_credentials.reset_cache_for_tests()
    try:
        yield f"http://127.0.0.1:{port}", community_id, seen
    finally:
        shutdown.set()
        await task
        await grpc_server.stop(grace=None)
        await service_jwt_client.close_channels()
        community_credentials.reset_cache_for_tests()


async def test_waddle_transports_client_sends_bearer_not_service_key(
    live_hub: tuple[str, int, list[dict[str, str]]],
) -> None:
    """Real mint + real fetch: the resource call is Bearer-authenticated, never X-Service-Key."""
    base_url, community_id, seen = live_hub

    tokens = await community_credentials.fetch_community_tokens(
        community_id, "youtube", hub_api_url=base_url
    )

    assert tokens is not None and tokens.access_token == "AT_LIVE"
    paths = [r["path"] for r in seen]
    # The mint is gRPC now: the only HTTP request is the resource call itself.
    assert paths == [f"/api/v1/internal/communities/{community_id}/connections/youtube/token"]
    resource = seen[0]
    assert resource["authorization"].startswith("Bearer ")
    assert "x-service-key" not in resource
    assert all("x-service-key" not in r for r in seen)


async def test_client_token_is_refused_for_another_community(
    live_hub: tuple[str, int, list[dict[str, str]]],
) -> None:
    """The JWT the client minted for community A is useless against community B (403 -> None)."""
    base_url, community_id, seen = live_hub
    assert await community_credentials.fetch_community_tokens(
        community_id, "youtube", hub_api_url=base_url
    )
    import httpx

    minted = community_credentials._jwt_cache[community_id][1]
    async with httpx.AsyncClient() as client:
        other = await client.get(
            f"{base_url}/api/v1/internal/communities/{community_id + 1}/connections/youtube/token",
            headers={"Authorization": f"Bearer {minted}"},
        )
    assert other.status_code == 403
