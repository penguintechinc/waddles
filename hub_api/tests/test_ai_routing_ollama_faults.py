"""Timeout / connection / HTTP-fault handling of `OllamaClient` over REAL sockets.

No live endpoint is needed.

A tiny in-process loopback HTTP server misbehaves on purpose (never answers, resets, 500s, sends
garbage). Real `httpx` + real TCP, so timeouts and connection errors are the genuine exceptions the
production code sees -- and none of it touches the lab GPU, so it runs in CI. The live-endpoint
twin is `test_ai_routing_ollama_realpath.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import pytest

from services.ai_routing.clients import OllamaClient, OllamaConfig
from services.ai_routing.errors import ApiError
from services.ai_routing.models import AIRequest


@dataclass(slots=True)
class FaultServer:
    """Loopback server counters; `url` is its base URL."""

    url: str
    host: str
    connections: int = 0
    concurrent: int = 0
    max_concurrent: int = 0
    requests: int = 0


Behavior = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def _read_request(reader: asyncio.StreamReader) -> bytes:
    """Consume one HTTP request (headers + Content-Length body); return the body."""
    head = await reader.readuntil(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    return await reader.readexactly(length)


def _http(status: str, body: bytes, content_type: str = "application/json") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body


async def _never_answers(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await _read_request(reader)
    await reader.read()  # parked until the client gives up and disconnects


async def _http_500(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await _read_request(reader)
    writer.write(_http("500 Internal Server Error", b'{"error": "boom"}'))


async def _garbage_body(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await _read_request(reader)
    writer.write(_http("200 OK", b"<html>not json</html>", "text/html"))


async def _truncated(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await _read_request(reader)
    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{"respon')


async def _ok(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await _read_request(reader)
    await asyncio.sleep(0.05)  # long enough that an overlapping caller would be visible
    body = {"response": "pong", "done": True, "done_reason": "stop", "eval_count": 2}
    writer.write(_http("200 OK", json.dumps(body).encode()))


@contextlib.asynccontextmanager
async def serve(behavior: Behavior) -> AsyncIterator[FaultServer]:
    """Run `behavior` for every connection on an ephemeral loopback port."""
    state = FaultServer(url="", host="127.0.0.1")

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        state.connections += 1
        state.concurrent += 1
        state.max_concurrent = max(state.max_concurrent, state.concurrent)
        try:
            state.requests += 1
            await behavior(reader, writer)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            state.concurrent -= 1
            writer.close()

    server = await asyncio.start_server(handle, state.host, 0)
    port = server.sockets[0].getsockname()[1]
    state.url = f"http://{state.host}:{port}"
    try:
        yield state
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()


def _client(url: str, timeout: float = 5.0) -> OllamaClient:
    return OllamaClient(OllamaConfig(base_url=url, model="m", timeout_seconds=timeout))


async def test_unresponsive_endpoint_times_out_with_a_typed_error() -> None:
    async with serve(_never_answers) as server:
        started = time.monotonic()
        with pytest.raises(ApiError) as exc_info:
            await _client(server.url, timeout=0.3).generate(AIRequest(prompt="hi"), tier="free")
        elapsed = time.monotonic() - started

    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "AI_PROVIDER_ERROR"
    assert "ReadTimeout" in exc_info.value.message
    assert server.host not in exc_info.value.message
    assert elapsed < 5.0  # the configured timeout bounds the call; no unbounded hang
    assert server.requests == 1  # exactly one attempt, no retry storm


async def test_refused_connection_is_a_typed_error() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing is listening on this port now

    with pytest.raises(ApiError) as exc_info:
        await _client(f"http://127.0.0.1:{port}").generate(AIRequest(prompt="hi"), tier="premium")

    assert exc_info.value.code == "AI_PROVIDER_ERROR"
    assert "ConnectError" in exc_info.value.message
    assert "127.0.0.1" not in exc_info.value.message


async def test_http_500_is_a_typed_error_with_status_only() -> None:
    async with serve(_http_500) as server:
        with pytest.raises(ApiError) as exc_info:
            await _client(server.url).generate(AIRequest(prompt="hi"), tier="free")

    assert exc_info.value.message == "Ollama (free) request failed: HTTP 500"


async def test_garbage_200_body_is_a_typed_error_not_a_crash() -> None:
    async with serve(_garbage_body) as server:
        with pytest.raises(ApiError, match="non-JSON response body"):
            await _client(server.url).generate(AIRequest(prompt="hi"), tier="free")


async def test_connection_dropped_mid_body_is_a_typed_error() -> None:
    async with serve(_truncated) as server:
        with pytest.raises(ApiError) as exc_info:
            await _client(server.url).generate(AIRequest(prompt="hi"), tier="free")

    assert exc_info.value.code == "AI_PROVIDER_ERROR"
    assert "127.0.0.1" not in exc_info.value.message


async def test_sequential_calls_never_hold_more_than_one_connection() -> None:
    async with serve(_ok) as server:
        client = _client(server.url)
        for _ in range(3):
            response = await client.generate(AIRequest(prompt="hi"), tier="free")
            assert response.text == "pong"

    assert server.requests == 3
    assert server.max_concurrent == 1
