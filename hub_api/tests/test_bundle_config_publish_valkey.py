"""`publish_version` against a REAL Valkey 8 (own container, unique name/port, removed after).

Proves the wire the Rust stage probe reads: `SET waddles:bundle_config:v:T:C:APP "<n>" EX ttl`.
Docker absent is a FAILURE (no skip), matching the other real-infra gates.
"""

from __future__ import annotations

import socket
import subprocess
import time
import uuid
from collections.abc import Iterator

import pytest
import redis.asyncio as aioredis

from services import bundle_community_config_runtime as rt


def _free_port() -> int:
    """An unused localhost TCP port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def valkey_url() -> Iterator[str]:
    """Own `valkey/valkey:8` container; waits up to 180s for PING, force-removed afterwards."""
    port = _free_port()
    name = f"waddles-hubapi-valkey-{uuid.uuid4().hex[:8]}"
    subprocess.run(  # noqa: S603, S607
        ["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:6379", "valkey/valkey:8"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    try:
        deadline = time.monotonic() + 180
        while True:
            probe = subprocess.run(  # noqa: S603, S607
                ["docker", "exec", name, "valkey-cli", "ping"],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.stdout.strip() == "PONG":
                break
            assert time.monotonic() < deadline, "valkey never became ready"
            time.sleep(0.5)
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)  # noqa: S603, S607


async def test_publish_version_sets_key_with_ttl_on_real_valkey(valkey_url: str) -> None:
    """The announced version is the decimal string at the shared key, with a bounded TTL."""
    client = aioredis.from_url(valkey_url, decode_responses=True)
    try:
        ok = await rt.publish_version(
            client, tenant_id=7, community_id=42, app_id="waddles.socials.demo.default", version=3
        )
        assert ok is True
        key = "waddles:bundle_config:v:7:42:waddles.socials.demo.default"
        assert rt.version_key(7, 42, "waddles.socials.demo.default") == key
        assert await client.get(key) == "3"
        assert 0 < await client.ttl(key) <= rt.VERSION_KEY_TTL_S
        await rt.publish_version(
            client, tenant_id=7, community_id=42, app_id="waddles.socials.demo.default", version=4
        )
        assert await client.get(key) == "4"
    finally:
        await client.aclose()


async def test_publish_version_reports_false_not_raise_when_valkey_down() -> None:
    """A dead Valkey only delays propagation (TTL backstop): returns False, never raises."""
    dead = aioredis.from_url(f"redis://127.0.0.1:{_free_port()}/0", socket_connect_timeout=1)
    try:
        ok = await rt.publish_version(dead, tenant_id=1, community_id=1, app_id="a", version=1)
        assert ok is False
    finally:
        await dead.aclose()
