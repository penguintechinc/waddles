"""The single-flight guard is a safety rail for a shared GPU -- so the rail itself is tested.

Offline: the guard wraps a FAKE `AsyncHTTPTransport.handle_async_request`, so nothing here touches
a network or the lab Ollama.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path

import httpx
import pytest

from tests.ollama_support import (
    OLLAMA_URL_ENV,
    SingleFlightGuard,
    SingleFlightViolation,
    ollama_url_or_none,
    split_endpoint,
)

BASE = "http://ollama.test:11434"


class _FakeWire:
    """Stands in for the real network: records overlap, answers after a short delay."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def __call__(self, transport: object, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.03)
        finally:
            self.active -= 1
        # An un-read streamed body, exactly what the real transport returns (a pre-read
        # `json=` Response would never be closed by the client, masking the lock release).
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(b'{"ok": true}'),
            request=request,
        )


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> _FakeWire:
    fake = _FakeWire()
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake)
    return fake


@pytest.fixture
def guard(tmp_path: Path, wire: _FakeWire, monkeypatch: pytest.MonkeyPatch) -> SingleFlightGuard:
    g = SingleFlightGuard(base_url=BASE, lock_path=tmp_path / "lock", lock_timeout=2.0)
    g.install(monkeypatch)
    return g


async def test_concurrent_callers_are_serialized_never_overlapped(
    guard: SingleFlightGuard, wire: _FakeWire
) -> None:
    async def call(i: int) -> int:
        async with httpx.AsyncClient() as client:
            return (await client.post(f"{BASE}/api/generate", json={"i": i})).status_code

    results = await asyncio.gather(*(call(i) for i in range(5)))

    assert results == [200] * 5
    assert wire.calls == 5
    assert wire.max_active == 1  # the wire never saw two requests at once
    assert guard.max_in_flight == 1
    assert guard.in_flight == 0
    assert [r.json()["i"] for r in guard.requests] == sorted(r.json()["i"] for r in guard.requests)


async def test_records_what_went_over_the_wire(guard: SingleFlightGuard) -> None:
    async with httpx.AsyncClient() as client:
        await client.post(f"{BASE}/api/generate", json={"model": "m"}, headers={"x-k": "v"})
        await client.get(f"{BASE}/api/tags")

    assert [(r.method, r.path) for r in guard.requests] == [
        ("POST", "/api/generate"),
        ("GET", "/api/tags"),
    ]
    assert guard.requests[0].json() == {"model": "m"}
    assert guard.requests[0].headers["x-k"] == "v"
    assert guard.requests[0].host == "ollama.test"
    assert guard.requests_to("/api/tags")[0].body == b""


async def test_foreign_host_is_refused_before_any_traffic(
    guard: SingleFlightGuard, wire: _FakeWire
) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(SingleFlightViolation, match="may only talk to ollama.test:11434"):
            await client.post("https://api.openai.com/v1/chat/completions", json={})

    assert wire.calls == 0
    assert guard.requests == []


async def test_request_budget_caps_load_on_the_shared_gpu(
    tmp_path: Path, wire: _FakeWire, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = SingleFlightGuard(base_url=BASE, max_requests=2, lock_path=tmp_path / "lock")
    guard.install(monkeypatch)

    async with httpx.AsyncClient() as client:
        await client.get(f"{BASE}/a")
        await client.get(f"{BASE}/b")
        with pytest.raises(SingleFlightViolation, match="budget of 2"):
            await client.get(f"{BASE}/c")

    assert wire.calls == 2


async def test_lock_held_by_another_process_blocks_then_times_out(
    tmp_path: Path, wire: _FakeWire, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "lock"
    guard = SingleFlightGuard(base_url=BASE, lock_path=lock, lock_timeout=0.2)
    guard.install(monkeypatch)
    other = os.open(lock, os.O_CREAT | os.O_RDWR)  # a separate open file description == "other run"
    fcntl.flock(other, fcntl.LOCK_EX)
    try:
        async with httpx.AsyncClient() as client:
            with pytest.raises(SingleFlightViolation, match="timed out"):
                await client.get(f"{BASE}/api/tags")
        assert wire.calls == 0  # never sent while someone else held the GPU
    finally:
        os.close(other)

    async with httpx.AsyncClient() as client:  # lock freed -> proceeds
        assert (await client.get(f"{BASE}/api/tags")).status_code == 200


async def test_lock_is_released_when_the_transport_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(transport: object, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", boom)
    guard = SingleFlightGuard(base_url=BASE, lock_path=tmp_path / "lock", lock_timeout=1.0)
    guard.install(monkeypatch)

    async with httpx.AsyncClient() as client:
        for _ in range(2):  # the 2nd call would time out if the 1st leaked the lock
            with pytest.raises(httpx.ConnectError):
                await client.get(f"{BASE}/api/tags")

    assert guard.in_flight == 0


async def test_release_all_frees_a_leaked_lock(
    guard: SingleFlightGuard, wire: _FakeWire, tmp_path: Path
) -> None:
    request = httpx.Request("GET", f"{BASE}/x")
    response = await guard.serialize(lambda r: wire(None, r), request)  # never closed -> leaked
    assert guard.in_flight == 1

    guard.release_all()

    assert guard.in_flight == 0
    probe = os.open(tmp_path / "lock", os.O_RDWR)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)  # would raise if still held
    finally:
        os.close(probe)
    await response.aclose()  # closing after release_all is a harmless no-op
    assert guard.in_flight == 0


def test_endpoint_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OLLAMA_URL_ENV, raising=False)
    assert ollama_url_or_none() is None
    monkeypatch.setenv(OLLAMA_URL_ENV, "   ")
    assert ollama_url_or_none() is None
    monkeypatch.setenv(OLLAMA_URL_ENV, " http://192.168.2.105:11434/ ")
    assert ollama_url_or_none() == "http://192.168.2.105:11434"
    assert split_endpoint("http://192.168.2.105:11434") == ("192.168.2.105", "11434", False)
    assert split_endpoint("https://ollama.lan") == ("ollama.lan", "443", True)
    assert split_endpoint("http://ollama.lan") == ("ollama.lan", "80", False)
