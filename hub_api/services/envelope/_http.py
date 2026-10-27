"""Shared HTTP plumbing for the REST-based KMS adapters (GCP Cloud KMS, Azure Key Vault).

Three small pieces, each deliberately boring and unit-testable:

- :class:`SharedHttp` -- one pooled :class:`httpx.AsyncClient` per event loop
  (created lazily so importing the module never touches the loop), with
  redirects *off* and ``trust_env`` *off*: a KMS endpoint that answers with a
  redirect, or a proxy variable in the environment, must never be able to
  steer a credential-bearing request somewhere else.
- :class:`TokenCache` -- single-flight OAuth token cache. Concurrent callers
  that find the token missing/expiring share ONE refresh instead of stampeding
  the identity provider; the token lives in memory only and is never logged.
- :func:`observed_call` -- the span + latency histogram + hard timeout + error
  classification wrapper every provider call goes through, so a hung customer
  KMS can neither stall the event loop nor leak through as a bare
  ``httpx``/``asyncio`` exception.
- :func:`error_code` -- extracts the provider's short error *code* from a JSON
  error body. Provider messages are never kept: they can echo key ARNs,
  principals and request ids, and none of that belongs in a log line.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from services.envelope import metrics
from services.envelope.errors import KmsError, KmsUnavailableError

logger = logging.getLogger(__name__)

#: connect / read / write / pool-acquire bounds, seconds. The outer
#: :func:`observed_call` timeout is the binding one; these keep a half-open
#: socket from holding a pooled connection past it.
_TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=3.0)
_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=8)

_CODE_SAFE = re.compile(r"[^A-Za-z0-9_.:-]")
_MAX_CODE = 64


class SharedHttp:
    """Hands out one :class:`httpx.AsyncClient` per running event loop."""

    def __init__(self, client_factory: Callable[[], httpx.AsyncClient] | None = None) -> None:
        """Create the holder; `client_factory` is injectable for tests."""
        self._factory = client_factory or self._default_client
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def _default_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=_TIMEOUT,
            limits=_LIMITS,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "waddles-hub-api-envelope"},
        )

    def client(self) -> httpx.AsyncClient:
        """Return the client for the current loop, creating (or replacing a stale) one."""
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop or self._client.is_closed:
            self._client = self._factory()
            self._loop = loop
        return self._client

    async def aclose(self) -> None:
        """Close the pooled client (idempotent); called from the app's shutdown hook."""
        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            await client.aclose()


@dataclass(slots=True)
class _Token:
    """A bearer token and the monotonic-clock instant it stops being usable."""

    value: str = field(repr=False)
    expires_at: float


class TokenCache:
    """Single-flight, in-memory cache for one identity's bearer token."""

    def __init__(
        self,
        fetch: Callable[[], Awaitable[tuple[str, float]]],
        *,
        clock: Callable[[], float] = time.monotonic,
        refresh_margin_s: float = 120.0,
    ) -> None:
        """`fetch` returns ``(token, lifetime_seconds)``; refreshed `refresh_margin_s` early."""
        self._fetch = fetch
        self._clock = clock
        self._margin = refresh_margin_s
        self._token: _Token | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def get(self) -> str:
        """Return a token valid for at least the refresh margin, fetching one if needed."""
        token = self._token
        if token is not None and token.expires_at - self._margin > self._clock():
            return token.value
        async with self._get_lock():
            token = self._token
            if token is not None and token.expires_at - self._margin > self._clock():
                return token.value
            value, lifetime = await self._fetch()
            self._token = _Token(value, self._clock() + max(0.0, lifetime))
            return value

    def invalidate(self) -> None:
        """Forget the cached token (e.g. after the API rejected it)."""
        self._token = None


async def observed_call[T](
    provider: str,
    operation: str,
    timeout_s: float,
    call: Callable[[], Awaitable[T]],
) -> T:
    """Run one provider request under a span, a latency histogram and a hard timeout.

    Classification: a timeout or any transport-level failure becomes
    :class:`KmsUnavailableError` (transient by definition); an already
    classified :class:`KmsError` passes through unchanged. Anything else is a
    bug and propagates untouched rather than being disguised as a KMS outage.
    """
    started = time.perf_counter()
    outcome = "ok"
    with metrics.tracer.start_as_current_span(f"envelope.kms.{operation}") as span:
        span.set_attribute("kms.provider", provider)
        try:
            return await asyncio.wait_for(call(), timeout=timeout_s)
        except TimeoutError:
            outcome = "timeout"
            raise KmsUnavailableError(f"{provider} request timed out", code="Timeout") from None
        except httpx.HTTPError as exc:
            outcome = "KmsUnavailableError"
            raise KmsUnavailableError(
                f"{provider} unreachable ({type(exc).__name__})", code=type(exc).__name__
            ) from exc
        except KmsError as exc:
            outcome = type(exc).__name__
            raise
        finally:
            metrics.record_kms_call(provider, operation, outcome, time.perf_counter() - started)


def error_code(response: httpx.Response, *keys: str) -> str:
    """Pull a short, log-safe error code out of a JSON error body ("" when absent).

    `keys` is the path of nested keys to the code (e.g. ``("error", "status")``
    for Google, ``("error", "code")`` for Azure Key Vault). The result is
    stripped to a conservative charset and length-capped so a hostile or
    verbose body can never inject log content.
    """
    try:
        node: Any = response.json()
    except ValueError:
        logger.debug("envelope.http.error_body_not_json", extra={"status": response.status_code})
        return ""
    for key in keys:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    if not isinstance(node, str):
        return ""
    return _CODE_SAFE.sub("", node)[:_MAX_CODE]
