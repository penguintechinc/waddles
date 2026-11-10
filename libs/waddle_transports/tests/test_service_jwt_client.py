"""service_jwt_client.py -- config/failure paths that need no server.

The real client <-> server round trip (mint over gRPC, verify via gRPC-served JWKS, TLS with a
pinned ECDSA P-256 CA) lives in `hub_api/tests/test_service_auth_grpc.py`, next to the servicer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from waddle_transports import service_jwt_client
from waddle_transports.service_jwt_client import ServiceTokenError, mint_service_token


@pytest.fixture
def sa_token(tmp_path: Path) -> str:
    path = tmp_path / "sa"
    path.write_text("projected-sa-token\n", encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def _no_ca_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUB_API_GRPC_CA_FILE", raising=False)


@pytest.mark.parametrize(
    ("endpoint", "match"),
    [
        ("http://hub-api.internal:50204", "non-loopback"),
        ("hub-api:50204", "invalid hub-api gRPC endpoint"),
        ("ftp://127.0.0.1:1", "invalid hub-api gRPC endpoint"),
        ("http://127.0.0.1", "invalid hub-api gRPC endpoint"),
        ("https://hub-api.internal:50204", "HUB_API_GRPC_CA_FILE is required"),
    ],
)
async def test_refuses_insecure_or_malformed_endpoints(
    endpoint: str, match: str, sa_token: str
) -> None:
    with pytest.raises(ServiceTokenError, match=match):
        await mint_service_token(endpoint=endpoint, scope="s", sa_token_path=sa_token)
    assert service_jwt_client._channels == {}


async def test_unreadable_ca_file_fails_loud(sa_token: str, tmp_path: Path) -> None:
    with pytest.raises(ServiceTokenError, match="cannot read gRPC CA file"):
        await mint_service_token(
            endpoint="https://hub-api.internal:50204",
            scope="s",
            sa_token_path=sa_token,
            ca_file=str(tmp_path / "absent.pem"),
        )


@pytest.mark.parametrize(("content", "match"), [(None, "unreadable"), ("  \n", "empty")])
async def test_bad_sa_token_fails_loud(tmp_path: Path, content: str | None, match: str) -> None:
    path = tmp_path / "sa"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(ServiceTokenError, match=match):
        await mint_service_token(endpoint="http://127.0.0.1:1", scope="s", sa_token_path=str(path))


async def test_sa_token_path_defaults_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SERVICE_JWT_SA_TOKEN_PATH", str(tmp_path / "absent"))
    with pytest.raises(ServiceTokenError, match="unreadable"):
        await mint_service_token(endpoint="http://127.0.0.1:1", scope="s")


async def test_unreachable_hub_is_a_loud_error(sa_token: str) -> None:
    try:
        with pytest.raises(ServiceTokenError, match="UNAVAILABLE"):
            await mint_service_token(
                endpoint="http://127.0.0.1:1", scope="s", sa_token_path=sa_token, timeout_s=1.0
            )
    finally:
        await service_jwt_client.close_channels()


@pytest.mark.parametrize(
    ("host", "loopback"),
    [("localhost", True), ("127.0.0.1", True), ("::1", True), ("10.0.0.5", False), ("hub", False)],
)
def test_loopback_detection(host: str, loopback: bool) -> None:
    assert service_jwt_client._is_loopback(host) is loopback
