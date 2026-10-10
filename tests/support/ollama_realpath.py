"""Shared support for the env-gated, real-endpoint Ollama integration tests.

Used by `hub_api/tests`, `core/ai_researcher_module/tests` and
`action/interactive/ai_interaction_module/tests` (each module's `conftest.py`
puts this directory on `sys.path` and re-exports the fixtures below; see
`docs/testing/ollama-realpath.md`).

Three jobs:

1. **Gate** -- the real-endpoint tests only run when `WADDLE_TEST_OLLAMA_URL`
   is set (LAN-only lab Ollama). Unset -> the tests SKIP (CI). Set but
   unreachable -> the session FAILS, never skips: a test that silently skips
   after the operator asked for it to run is exactly the always-green gate
   this repo forbids.
2. **Single-flight** -- the lab Ollama shares one GPU with the live WaddleAI,
   so *at most one request may be on the wire at a time*, ever.
   `SingleFlightGuard` wraps `httpx.AsyncHTTPTransport.handle_async_request`
   (the one choke point every `httpx.AsyncClient` in the code under test goes
   through) with a cross-process `flock`, held from request-send until the
   response body is fully consumed/closed. Concurrent callers queue; they never
   overlap -- even across `pytest-xdist` workers or two pytest runs.
3. **Safety rails** -- the guard refuses any request to a host other than the
   configured Ollama (an accidental call to a real OpenAI/Anthropic endpoint
   with a synthetic key must blow up, not leave the machine), enforces a
   per-guard request budget, and records every request so tests can assert on
   what actually went over the wire.

Tests built on this assert the INTEGRATION (well-formed request, parsed
response, verdict consumed, errors/timeouts handled) -- never model output
quality; the lab models are the smallest available and will be weak.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

OLLAMA_URL_ENV = "WADDLE_TEST_OLLAMA_URL"
TEXT_MODEL_ENV = "WADDLE_TEST_OLLAMA_TEXT_MODEL"
JSON_MODEL_ENV = "WADDLE_TEST_OLLAMA_JSON_MODEL"
SAFETY_MODEL_ENV = "WADDLE_TEST_OLLAMA_SAFETY_MODEL"

#: Lab defaults (test config only -- NOT a product tier->model map; the
#: product maps tiers to models via deployment env, see docs/testing/ollama-realpath.md).
DEFAULT_TEXT_MODEL = "gemma4:e2b"  # text-only (no JSON/structured output)
DEFAULT_JSON_MODEL = "gemma4:e4b"  # first model that handles JSON
DEFAULT_SAFETY_MODEL = "shieldgemma"  # safety / content classification

#: Hard ceiling on requests one guard may send -- a runaway loop must not hammer
#: the shared GPU. Real tests need 1-3 each.
DEFAULT_MAX_REQUESTS = 12
LOCK_ACQUIRE_TIMEOUT_SECONDS = 900.0
_LOCK_POLL_SECONDS = 0.05


class SingleFlightViolation(RuntimeError):
    """A request broke a real-path safety rail (foreign host / over budget / lock timeout)."""


@dataclass(slots=True, frozen=True)
class RecordedRequest:
    """One request that actually reached the transport, captured for wire-level assertions."""

    method: str
    path: str
    host: str
    headers: httpx.Headers
    body: bytes

    def json(self) -> Any:
        """Decode the request body as JSON (fails loudly if the body is empty/invalid)."""
        return json.loads(self.body)


@dataclass(slots=True)
class SingleFlightGuard:
    """Serialize every real-transport request through a cross-process lock; record them all.

    `install()` monkeypatches `httpx.AsyncHTTPTransport.handle_async_request`; MockTransport
    (used by the offline unit tests) is a different class and is never touched.
    """

    base_url: str
    max_requests: int = DEFAULT_MAX_REQUESTS
    lock_path: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "waddle-test-ollama-singleflight.lock"
    )
    lock_timeout: float = LOCK_ACQUIRE_TIMEOUT_SECONDS
    requests: list[RecordedRequest] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0
    _open_fds: set[int] = field(default_factory=set)

    @property
    def allowed_netloc(self) -> str:
        """`host:port` of the one endpoint this guard permits."""
        return urlsplit(self.base_url).netloc

    def requests_to(self, path: str) -> list[RecordedRequest]:
        """Recorded requests whose URL path equals `path`."""
        return [r for r in self.requests if r.path == path]

    async def _acquire(self) -> int:
        """Take the exclusive cross-process lock, polling (non-blocking) until `lock_timeout`."""
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise SingleFlightViolation(
                        f"timed out after {self.lock_timeout}s waiting for the single-flight "
                        f"lock {self.lock_path} (another run holds the lab Ollama?)"
                    ) from None
                await asyncio.sleep(_LOCK_POLL_SECONDS)
                continue
            self._open_fds.add(fd)
            return fd

    def _release(self, fd: int) -> None:
        """Drop the lock for `fd` (idempotent -- closing the fd releases the flock)."""
        if fd in self._open_fds:
            self._open_fds.discard(fd)
            self.in_flight -= 1
            with contextlib.suppress(OSError):
                os.close(fd)

    def _check(self, request: httpx.Request) -> None:
        if request.url.netloc.decode("ascii") != self.allowed_netloc:
            raise SingleFlightViolation(
                f"real-path tests may only talk to {self.allowed_netloc}; refusing "
                f"{request.method} {request.url.host}{request.url.path}"
            )
        if len(self.requests) >= self.max_requests:
            raise SingleFlightViolation(
                f"request budget of {self.max_requests} exhausted -- refusing to send more "
                "load to the shared GPU"
            )

    async def serialize(
        self,
        send: Callable[[httpx.Request], Awaitable[httpx.Response]],
        request: httpx.Request,
    ) -> httpx.Response:
        """Run `send(request)` under the lock; the lock is released when the response closes."""
        self._check(request)
        fd = await self._acquire()
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.requests.append(
            RecordedRequest(
                method=request.method,
                path=request.url.path,
                host=request.url.host,
                headers=request.headers,
                body=request.content
                if request.stream is not None and _is_buffered(request)
                else b"",
            )
        )
        try:
            response = await send(request)
        except BaseException:
            self._release(fd)
            raise
        response.stream = _ReleasingStream(response.stream, lambda: self._release(fd))  # type: ignore[assignment]
        return response

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Route every real `httpx.AsyncHTTPTransport` request through `serialize`."""
        original = httpx.AsyncHTTPTransport.handle_async_request
        guard = self

        async def guarded(
            transport: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> httpx.Response:
            return await guard.serialize(lambda r: original(transport, r), request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guarded)

    def release_all(self) -> None:
        """Teardown safety net: free any lock a test leaked by not closing a response."""
        for fd in list(self._open_fds):
            self._release(fd)


def _is_buffered(request: httpx.Request) -> bool:
    """True when the request body is already in memory (always the case for `json=`/`content=`)."""
    try:
        request.content  # noqa: B018 - raises RequestNotRead for un-read streams
    except httpx.RequestNotRead:
        return False
    return True


class _ReleasingStream(httpx.AsyncByteStream):
    """Wrap a response stream so the single-flight lock is held until the body is closed."""

    def __init__(self, inner: Any, on_close: Callable[[], None]) -> None:
        self._inner = inner
        self._on_close = on_close

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            self._on_close()


def ollama_url_or_none() -> str | None:
    """The configured lab endpoint (trailing slash stripped), or None when the gate is closed."""
    raw = os.environ.get(OLLAMA_URL_ENV, "").strip()
    return raw.rstrip("/") or None


def split_endpoint(url: str) -> tuple[str, str, bool]:
    """Return `(host, port, use_tls)` for modules configured by OLLAMA_HOST/PORT/USE_TLS."""
    parts = urlsplit(url)
    use_tls = parts.scheme == "https"
    port = str(parts.port or (443 if use_tls else 80))
    return parts.hostname or "", port, use_tls


@pytest.fixture(scope="session")
def ollama_url() -> str:
    """The lab Ollama base URL. Unset -> SKIP; set-but-unreachable -> FAIL (never skip)."""
    url = ollama_url_or_none()
    if url is None:
        pytest.skip(f"{OLLAMA_URL_ENV} not set -- real-Ollama integration tests skipped")
    try:
        httpx.get(f"{url}/api/tags", timeout=10.0).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(
            f"{OLLAMA_URL_ENV}={url} is set but the endpoint is unreachable/unhealthy: "
            f"{type(exc).__name__}: {exc}",
            pytrace=False,
        )
    return url


def _pulled_models(url: str) -> set[str]:
    data = httpx.get(f"{url}/api/tags", timeout=10.0).json()
    names: set[str] = set()
    for model in data.get("models", []):
        names.add(model["name"])
        names.add(model["name"].removesuffix(":latest"))
    return names


@pytest.fixture(scope="session")
def text_model(ollama_url: str) -> str:
    """The free-tier text-only model. Not pulled -> FAIL (it is the baseline under test)."""
    name = os.environ.get(TEXT_MODEL_ENV, DEFAULT_TEXT_MODEL)
    if name not in _pulled_models(ollama_url):
        pytest.fail(f"text model {name!r} is not pulled on {ollama_url}", pytrace=False)
    return name


@pytest.fixture(scope="session")
def json_model(ollama_url: str) -> str:
    """The first JSON-capable model; skip (with reason) if the lab hasn't pulled it."""
    name = os.environ.get(JSON_MODEL_ENV, DEFAULT_JSON_MODEL)
    if name not in _pulled_models(ollama_url):
        pytest.skip(f"JSON-capable model {name!r} is not pulled on {ollama_url}")
    return name


@pytest.fixture(scope="session")
def safety_model(ollama_url: str) -> str:
    """The safety/content-classification model; skip (with reason) if not pulled."""
    name = os.environ.get(SAFETY_MODEL_ENV, DEFAULT_SAFETY_MODEL)
    if name not in _pulled_models(ollama_url):
        pytest.skip(f"safety model {name!r} is not pulled on {ollama_url}")
    return name


@pytest.fixture
def single_flight(ollama_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[SingleFlightGuard]:
    """Install the single-flight guard for one test; assert it never saw overlap on teardown."""
    guard = SingleFlightGuard(base_url=ollama_url)
    guard.install(monkeypatch)
    yield guard
    leaked = len(guard._open_fds)
    guard.release_all()
    if guard.max_in_flight > 1:
        pytest.fail(f"single-flight violated: {guard.max_in_flight} requests in flight at once")
    if leaked:
        pytest.fail(f"{leaked} response(s) were never closed (lock held past the test)")


__all__ = [
    "DEFAULT_JSON_MODEL",
    "DEFAULT_SAFETY_MODEL",
    "DEFAULT_TEXT_MODEL",
    "OLLAMA_URL_ENV",
    "RecordedRequest",
    "SingleFlightGuard",
    "SingleFlightViolation",
    "json_model",
    "ollama_url",
    "ollama_url_or_none",
    "safety_model",
    "single_flight",
    "split_endpoint",
    "text_model",
]
