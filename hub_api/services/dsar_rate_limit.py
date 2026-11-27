"""Rate limiting + single-use (replay) markers for DSAR intake. Fails CLOSED.

Backends, in order:

1. The app's shared Valkey client (the same one the SSO state store and the global limiter use:
   `app.config["sso_state_redis"]` or the installed `RateLimiter`'s connection), so limits and
   replay markers hold across replicas. Rate limit = atomic fixed window (`INCR` + `EXPIRE NX` in
   one `MULTI`); replay marker = `SET NX EX`.
2. A bounded in-process LRU, used ONLY when the app has no shared limiter configured at all
   (tests / single-process dev).

If a shared store IS configured but is unreachable, errors or is not connected, every call raises
`ApiError(503, "DSAR_UNAVAILABLE")`: the public path never degrades to per-process state, never
fails open, and the uniform 503 reveals nothing about the caller. The local LRU never `clear()`s;
when full it evicts expired entries, then the least-recently-used rate bucket; a full table of live
replay markers fails closed rather than forgetting one. Keys are opaque (callers pass HMAC
fingerprints, never raw emails).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from quart import current_app

from services.errors import ApiError
from services.rate_limiting import RATE_LIMITER_CONFIG_KEY

logger = logging.getLogger(__name__)

#: Same config key `services.sso_state` caches/injects its raw Valkey client under.
_REDIS_CONFIG_KEY = "sso_state_redis"
_PREFIX = "dsar:"
_BACKEND_TIMEOUT_SECONDS = 2.0
_MAX_LOCAL_KEYS = 20_000

#: key -> (hit timestamps | [expiry]); OrderedDict order == recency (oldest first).
_LOCAL: OrderedDict[str, list[float]] = OrderedDict()
_LOCAL_ONCE: OrderedDict[str, float] = OrderedDict()


def unavailable() -> ApiError:
    """The uniform fail-closed error (same code/shape the form already uses for outages)."""
    return ApiError("The request form is temporarily unavailable", 503, "DSAR_UNAVAILABLE")


def reset_local() -> None:
    """Clear the in-process state (tests only; never called in production paths)."""
    _LOCAL.clear()
    _LOCAL_ONCE.clear()


def _shared_client(on_unavailable: Callable[[], ApiError] = unavailable) -> Any | None:
    """The shared Valkey client, or `None` when no shared store is configured.

    Raises the fail-closed error (`on_unavailable()`) when a limiter IS configured but its
    connection is not usable.
    """
    injected = current_app.config.get(_REDIS_CONFIG_KEY)
    if injected is not None:
        return injected
    limiter: Any = current_app.config.get(RATE_LIMITER_CONFIG_KEY)
    if limiter is None:
        return None
    client = getattr(limiter, "_redis", None)
    if client is None or not getattr(limiter, "_connected", False):
        logger.error("dsar.limiter_backend_not_connected")
        raise on_unavailable()
    return client


def _evict_for_room(table: OrderedDict[str, Any], now: float, live: Any) -> bool:
    """Make room in a full local table: drop expired entries, then (if allowed) the LRU entry."""
    if len(table) < _MAX_LOCAL_KEYS:
        return True
    for stale in [k for k, v in table.items() if not live(v, now)]:
        table.pop(stale, None)
    return len(table) < _MAX_LOCAL_KEYS


def _local_allow(key: str, limit: int, window: int, now: float) -> bool:
    """Bounded in-process sliding window with LRU eviction (never clears the table)."""
    hits = [t for t in _LOCAL.get(key, []) if t > now - window]
    if key not in _LOCAL and not _evict_for_room(
        _LOCAL, now, lambda v, n: bool(v) and v[-1] > n - 3600
    ):
        _LOCAL.popitem(last=False)  # least recently used rate bucket
    if len(hits) >= limit:
        _LOCAL[key] = hits
        _LOCAL.move_to_end(key)
        return False
    hits.append(now)
    _LOCAL[key] = hits
    _LOCAL.move_to_end(key)
    return True


def _local_once(key: str, ttl: int, now: float) -> bool:
    """Bounded in-process single-use marker; a table full of LIVE markers fails closed."""
    expiry = _LOCAL_ONCE.get(key)
    if expiry is not None and expiry > now:
        return False
    if key not in _LOCAL_ONCE and not _evict_for_room(_LOCAL_ONCE, now, lambda v, n: v > n):
        logger.error("dsar.replay_store_full")
        raise unavailable()
    _LOCAL_ONCE[key] = now + ttl
    _LOCAL_ONCE.move_to_end(key)
    return True


def _local_hit(key: str, window: int, now: float) -> int:
    """Record one hit in a bounded in-process sliding window; return the hits now in it."""
    hits = [t for t in _LOCAL.get(key, []) if t > now - window]
    if key not in _LOCAL and not _evict_for_room(
        _LOCAL, now, lambda v, n: bool(v) and v[-1] > n - 3600
    ):
        _LOCAL.popitem(last=False)  # least recently used rate bucket
    hits.append(now)
    _LOCAL[key] = hits
    _LOCAL.move_to_end(key)
    return len(hits)


def _local_key(key: str, prefix: str) -> str:
    """Local-table key: DSAR keys stay bare (unchanged layout), other limiters are namespaced."""
    return key if prefix == _PREFIX else f"{prefix}{key}"


async def allow(
    key: str,
    limit: int,
    window_seconds: int,
    *,
    prefix: str = _PREFIX,
    on_unavailable: Callable[[], ApiError] = unavailable,
) -> bool:
    """True when `key` is within `limit` hits per `window_seconds` (and records the hit).

    Shared by other fail-closed limiters (account MFA): `prefix` namespaces the Valkey key and
    the local bucket, `on_unavailable` builds the caller's own 503 when the configured shared
    store cannot answer (fail closed).
    """
    client = _shared_client(on_unavailable)
    if client is None:
        return _local_allow(_local_key(key, prefix), limit, window_seconds, time.time())
    return await _redis_hit(client, f"{prefix}rl:{key}", window_seconds, on_unavailable) <= limit


async def _redis_hit(
    client: Any, redis_key: str, window_seconds: int, on_unavailable: Callable[[], ApiError]
) -> int:
    """Atomic fixed-window `INCR` + `EXPIRE NX`; the count after this hit (fails closed)."""
    try:
        async with asyncio.timeout(_BACKEND_TIMEOUT_SECONDS):
            pipe = client.pipeline(transaction=True)
            pipe.incr(redis_key)
            pipe.expire(redis_key, window_seconds, nx=True)
            return int((await pipe.execute())[0])
    except Exception as exc:
        logger.error("dsar.limiter_backend_error", extra={"err": type(exc).__name__})
        raise on_unavailable() from exc


async def hit(
    key: str,
    window_seconds: int,
    *,
    prefix: str = _PREFIX,
    on_unavailable: Callable[[], ApiError] = unavailable,
) -> int:
    """Record one hit on `key` and return how many hits the current window holds.

    Same backends and fail-closed policy as :func:`allow`; callers that need the count itself
    (exponential backoff after N failures) use this instead of a yes/no budget.
    """
    client = _shared_client(on_unavailable)
    if client is None:
        return _local_hit(_local_key(key, prefix), window_seconds, time.time())
    return await _redis_hit(client, f"{prefix}rl:{key}", window_seconds, on_unavailable)


async def once(
    key: str,
    ttl_seconds: int,
    *,
    prefix: str = _PREFIX,
    on_unavailable: Callable[[], ApiError] = unavailable,
) -> bool:
    """Atomically claim `key` for `ttl_seconds`; False when it was already claimed (a replay).

    Raises the caller's 503 (`on_unavailable`, default `DSAR_UNAVAILABLE`) when the configured
    shared store cannot answer (fail closed).
    """
    ttl = max(1, int(ttl_seconds))
    client = _shared_client(on_unavailable)
    if client is None:
        return _local_once(_local_key(key, prefix), ttl, time.time())
    try:
        async with asyncio.timeout(_BACKEND_TIMEOUT_SECONDS):
            stored = await client.set(f"{prefix}once:{key}", "1", nx=True, ex=ttl)
    except Exception as exc:
        logger.error("dsar.replay_backend_error", extra={"err": type(exc).__name__})
        raise on_unavailable() from exc
    return bool(stored)


async def is_claimed(
    key: str,
    *,
    prefix: str = _PREFIX,
    on_unavailable: Callable[[], ApiError] = unavailable,
) -> bool:
    """True while a :func:`once` marker for `key` is live (read only; never claims it)."""
    client = _shared_client(on_unavailable)
    if client is None:
        expiry = _LOCAL_ONCE.get(_local_key(key, prefix))
        return expiry is not None and expiry > time.time()
    try:
        async with asyncio.timeout(_BACKEND_TIMEOUT_SECONDS):
            present = await client.exists(f"{prefix}once:{key}")
    except Exception as exc:
        logger.error("dsar.replay_backend_error", extra={"err": type(exc).__name__})
        raise on_unavailable() from exc
    return bool(present)
