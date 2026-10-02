"""Shared fixtures for svc-ingest's own tests.

`redis_client` is a real `fakeredis.FakeAsyncRedis` -- genuine LPUSH/RPOP/
LIST semantics executed in-memory (not a mock of the call), the "fakeredis
... real key round-trip" option this PR's own task spec calls out.
`redis_server` is the `FakeServer` backing it, exposed separately so a
test can build a SECOND, independent client pointed at the same logical
server -- e.g. `outbound_drain.TwitchOutboundDrain`'s own deliberately
split BRPOP/lease connections (see that module's own "Two separate Valkey
clients" docstring section for why one shared client is unsafe there).
"""

from __future__ import annotations

from typing import Any

import fakeredis
import httpx
import pytest

from identity_crypto import DekProvider


@pytest.fixture
def redis_server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
async def redis_client(redis_server: fakeredis.FakeServer) -> Any:
    client = fakeredis.FakeAsyncRedis(decode_responses=True, server=redis_server)
    yield client
    await client.aclose()


class _FakeDekProvider:
    """Deterministic in-memory `DekProvider` for tests -- one fixed 32-byte DEK per tenant.

    Real AES-256-GCM keys (not a no-op/mock of the crypto itself), just a
    trivial resolution rule (`tenant_id` bytes, zero-padded) standing in
    for the hub-api broker call so runner tests exercise real encryption.
    """

    def __init__(self, *, version: int = 1) -> None:
        self.version = version

    async def get_dek(self, tenant_id: str, *, dek_version: int | None = None) -> tuple[bytes, int]:
        key = tenant_id.encode("utf-8").ljust(32, b"\0")[:32]
        return key, dek_version if dek_version is not None else self.version


@pytest.fixture
def dek_provider() -> DekProvider:
    return _FakeDekProvider()


@pytest.fixture
def http_client_factory():
    """Factory: build an `httpx.AsyncClient` wired to a `httpx.MockTransport` handler."""

    def _make(handler: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return _make
