"""Server-side JWT revocation -- reject a token before its ``exp`` (OWASP session management).

A signed JWT is valid until it expires; logging out, an admin revoking a session or a password
change must not wait for that. This module is the revocation store every platform-token
verification consults (``flask_core.auth.verify_jwt_token``), so a compromised / logged-out /
admin-revoked token is refused on its next use.

Three revocation keys, all written with a TTL so the store forgets a record the moment no token
it could match is still alive (the verifier refuses any token lived longer than
``MAX_JWT_LIFETIME_SECONDS``):

====================  =======================================  =================================
kind                  key                                      rejects a token when
====================  =======================================  =================================
``jti`` (one token)   ``waddles:jwt:rev:jti:<jti>``            its ``jti`` claim matches
``sid`` (one login)   ``waddles:jwt:rev:sid:<sid>``            its ``sid`` claim matches
``sub`` (a user)      ``waddles:jwt:rev:sub:<sub>`` = cutoff   ``iat <= cutoff`` (revoke-all)
====================  =======================================  =================================

The key layout and the ``iat <= cutoff`` rule are a cross-language contract -- the Rust verifiers
implement the identical check against the same keys (see ``docs/TOKEN_REVOCATION.md``).

Lookup path (every verification, one round trip at most)::

    verify_jwt_token -> TokenRevocation.check(claims)
        |-- local cache hit (TTL ``cache_ttl_seconds``, default 5 s) ----------> verdict
        `-- miss -> Valkey MGET(jti, sid, sub)  -> cache -> verdict
                 `-- store unreachable -> circuit opens (``circuit_open_seconds``)
                       |-- within ``grace_seconds`` of the last good contact: serve the last-known
                       |   view, flag the outcome ``degraded_allowed`` (serving is not broken)
                       `-- beyond it: FAIL CLOSED (``unavailable_rejected``) unless the operator
                           opted into ``JWT_REVOCATION_FAIL_MODE=open``

Tradeoffs, stated once:

* **Propagation delay.** A revocation reaches other replicas within ``cache_ttl_seconds``; the
  replica that processed it rejects the token immediately (its own cache is written through).
* **Outage window.** For at most ``grace_seconds`` after the store was last reachable a token
  that is not already known to be revoked is accepted; a revocation issued *during* the outage
  cannot propagate (the store cannot be written). Past the grace window the default is to
  refuse every token rather than silently run without revocation.
* **Blocking I/O.** ``check`` is synchronous (``verify_jwt_token`` is called from async handlers
  and sync gRPC threads alike). The cache keeps the store off the hot path; a miss costs one
  sub-millisecond ``MGET`` and a dead store costs at most ``store_timeout_seconds`` once per
  ``circuit_open_seconds`` -- never one timeout per request.
* **Eviction.** Revocation keys are volatile (TTL'd) but an ``allkeys-*`` ``maxmemory-policy`` can
  still evict them under memory pressure; the exposure is bounded by the access-token TTL.

No Valkey configured (neither ``VALKEY_URL`` nor ``REDIS_URL``) falls back to a process-local
store with a loud WARNING -- revocation then only holds inside the revoking process (single
replica / dev), exactly like the rate limiter's in-memory mode. There is no way to switch
revocation off.

Logs and metrics are PII-free by construction: the token, its ``jti``/``sid`` and the store's
exception text are never rendered -- only closed-vocabulary outcomes and exception *type* names.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol, TypeGuard

import redis
import redis.exceptions
from opentelemetry import metrics, trace

from .jwt_hardening import MAX_JWT_LIFETIME_SECONDS, REVOCATION_RECORD_TTL_SECONDS
from .valkey_tls import build_tls_kwargs, with_password

logger = logging.getLogger(__name__)

#: Cross-language key layout (Rust verifiers MUST use the same strings).
KEY_NAMESPACE: Final = "waddles:jwt"
REVOKED_KEY_PREFIX: Final = f"{KEY_NAMESPACE}:rev"
MARKER_KEY_PREFIX: Final = f"{KEY_NAMESPACE}:mark"

#: ``jti``/``sid`` values: URL-safe identifier, <= 128 chars (key material, never hashed).
MAX_IDENTIFIER_LENGTH: Final = 128
_IDENTIFIER_RE: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:\-]{0,127}")

#: Ceiling on the cutoff/revocation record TTL and the largest ``iat`` skew a record must outlive.
_ISSUED_AT_SKEW_SECONDS: Final = 30

#: Verdict outcomes -- the ``outcome`` metric label (closed set).
CHECK_OK: Final = "ok"
CHECK_REVOKED_JTI: Final = "revoked_jti"
CHECK_REVOKED_SID: Final = "revoked_sid"
CHECK_REVOKED_USER: Final = "revoked_user"
CHECK_DEGRADED_ALLOWED: Final = "degraded_allowed"
CHECK_UNAVAILABLE_ALLOWED: Final = "unavailable_allowed"
CHECK_UNAVAILABLE_REJECTED: Final = "unavailable_rejected"

_REJECTED_OUTCOMES: Final[frozenset[str]] = frozenset(
    {CHECK_REVOKED_JTI, CHECK_REVOKED_SID, CHECK_REVOKED_USER, CHECK_UNAVAILABLE_REJECTED}
)

#: Backends selectable through ``JWT_REVOCATION_BACKEND``.
BACKEND_AUTO: Final = "auto"
BACKEND_VALKEY: Final = "valkey"
BACKEND_MEMORY: Final = "memory"

_PUT_MAX_LUA: Final = """
local cur = redis.call('GET', KEYS[1])
if (not cur) or (tonumber(cur) < tonumber(ARGV[1])) then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return 1
end
local ttl = redis.call('TTL', KEYS[1])
if ttl < tonumber(ARGV[2]) then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


class RevocationKind(StrEnum):
    """What a revocation record names."""

    JTI = "jti"
    SID = "sid"
    USER = "sub"


class RevocationStoreError(Exception):
    """The store could not be read or written; the text is the exception *type* only."""


class RevocationBackend(Protocol):
    """The two operations revocation needs from a store; every method is synchronous."""

    def put_max(self, key: str, value: int, ttl_seconds: int) -> None:
        """Set ``key`` to ``value`` unless it already holds a larger one; (re)arm its TTL."""

    def get_many(self, keys: Sequence[str]) -> list[int | None]:
        """One round trip: the integer under each key (``None`` when absent/expired)."""

    def close(self) -> None:
        """Release the store connection."""


@dataclass(slots=True, frozen=True)
class RevocationVerdict:
    """The result of checking one verified token: ``allowed`` plus the closed-set ``outcome``."""

    allowed: bool
    outcome: str


@dataclass(slots=True, frozen=True)
class RevocationPolicy:
    """Tunables of the revocation check (see the module docstring for what each trades off)."""

    cache_ttl_seconds: float = 5.0
    grace_seconds: float = 60.0
    fail_closed: bool = True
    circuit_open_seconds: float = 5.0
    store_timeout_seconds: float = 0.25
    record_ttl_seconds: int = REVOCATION_RECORD_TTL_SECONDS
    cache_max_entries: int = 50_000

    def __post_init__(self) -> None:
        """Refuse nonsensical values at construction -- a mis-set knob must stop the service."""
        if not 0.0 <= self.cache_ttl_seconds <= 30.0:
            raise ValueError("cache_ttl_seconds must be within [0, 30]")
        if not 0.0 <= self.grace_seconds <= 600.0:
            raise ValueError("grace_seconds must be within [0, 600]")
        if not 0.5 <= self.circuit_open_seconds <= 60.0:
            raise ValueError("circuit_open_seconds must be within [0.5, 60]")
        if not 0.05 <= self.store_timeout_seconds <= 2.0:
            raise ValueError("store_timeout_seconds must be within [0.05, 2]")
        if self.record_ttl_seconds < MAX_JWT_LIFETIME_SECONDS:
            raise ValueError("record_ttl_seconds must outlive the longest-lived token")
        if self.cache_max_entries < 16:
            raise ValueError("cache_max_entries must be at least 16")


def policy_from_env(env: Mapping[str, str] | None = None) -> RevocationPolicy:
    """Build a :class:`RevocationPolicy` from ``JWT_REVOCATION_*`` env vars (invalid -> ValueError)."""
    source = os.environ if env is None else env
    defaults = RevocationPolicy()

    def _float(name: str, default: float) -> float:
        raw = source.get(name, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be a number") from exc

    fail_mode = source.get("JWT_REVOCATION_FAIL_MODE", "").strip().lower() or "closed"
    if fail_mode not in {"closed", "open"}:
        raise ValueError("JWT_REVOCATION_FAIL_MODE must be 'closed' or 'open'")
    return RevocationPolicy(
        cache_ttl_seconds=_float("JWT_REVOCATION_CACHE_TTL_SECONDS", defaults.cache_ttl_seconds),
        grace_seconds=_float("JWT_REVOCATION_GRACE_SECONDS", defaults.grace_seconds),
        fail_closed=fail_mode == "closed",
        circuit_open_seconds=_float(
            "JWT_REVOCATION_CIRCUIT_OPEN_SECONDS", defaults.circuit_open_seconds
        ),
        store_timeout_seconds=_float(
            "JWT_REVOCATION_TIMEOUT_SECONDS", defaults.store_timeout_seconds
        ),
    )


def is_valid_identifier(value: object) -> TypeGuard[str]:
    """True for a ``jti``/``sid`` value that is safe to embed in a store key."""
    return isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value) is not None


def revocation_key(kind: RevocationKind, value: str) -> str:
    """The store key holding the revocation record for ``(kind, value)``."""
    return f"{REVOKED_KEY_PREFIX}:{kind.value}:{value}"


class MemoryBackend:
    """Process-local store: correct inside one process, invisible to every other replica."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        """Create an empty store reading time from ``clock`` (injectable for tests)."""
        self._clock = clock
        self._data: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def put_max(self, key: str, value: int, ttl_seconds: int) -> None:
        """Keep the larger of the stored and given value; the record lives ``ttl_seconds``."""
        now = self._clock()
        with self._lock:
            current = self._data.get(key)
            if current is None or current[1] <= now or current[0] < value:
                self._data[key] = (value, now + ttl_seconds)
            else:
                self._data[key] = (current[0], max(current[1], now + ttl_seconds))
            if len(self._data) > 4096:
                self._data = {k: v for k, v in self._data.items() if v[1] > now}

    def get_many(self, keys: Sequence[str]) -> list[int | None]:
        """The live integer under each key, ``None`` when absent or expired."""
        now = self._clock()
        with self._lock:
            out: list[int | None] = []
            for key in keys:
                record = self._data.get(key)
                out.append(record[0] if record is not None and record[1] > now else None)
            return out

    def close(self) -> None:
        """Nothing to release."""


class ValkeyBackend:
    """Valkey/Redis-backed store using a synchronous client with tight socket timeouts."""

    def __init__(self, client: Any) -> None:
        """Wrap a ``redis.Redis``-compatible client (``decode_responses=True``)."""
        self._client = client
        self._put_max = client.register_script(_PUT_MAX_LUA)

    @classmethod
    def from_url(cls, url: str, *, timeout_seconds: float) -> ValkeyBackend:
        """Connect lazily to ``url`` (TLS CA handled by :func:`build_tls_kwargs`)."""
        client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=timeout_seconds,
            socket_timeout=timeout_seconds,
            **build_tls_kwargs(url),
        )
        return cls(client)

    def put_max(self, key: str, value: int, ttl_seconds: int) -> None:
        """Atomic ``SET key value EX ttl`` unless a larger value is already stored."""
        try:
            self._put_max(keys=[key], args=[int(value), int(ttl_seconds)])
        except (redis.exceptions.RedisError, OSError) as exc:
            raise RevocationStoreError(type(exc).__name__) from exc

    def get_many(self, keys: Sequence[str]) -> list[int | None]:
        """``MGET`` every key in one round trip."""
        if not keys:
            return []
        try:
            raw = self._client.mget(list(keys))
        except (redis.exceptions.RedisError, OSError) as exc:
            raise RevocationStoreError(type(exc).__name__) from exc
        out: list[int | None] = []
        for item in raw:
            if item is None:
                out.append(None)
                continue
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                # A non-integer under a revocation key is corruption/tampering; the key's
                # *existence* is the revocation signal, so it counts as revoked.
                out.append(2**62)
        return out

    def close(self) -> None:
        """Close the connection pool."""
        self._client.close()


@dataclass(slots=True)
class _Entry:
    """One cached store value and the monotonic instant until which it may be trusted."""

    value: int | None
    fresh_until: float


@dataclass(slots=True)
class _Instruments:
    """Revocation counters/histogram, bound to one meter provider."""

    checks: Any
    writes: Any
    write_errors: Any
    store_errors: Any
    lookup_seconds: Any


def _build_instruments(meter_provider: Any = None) -> _Instruments:
    meter = metrics.get_meter("waddles.flask_core.jwt", meter_provider=meter_provider)
    return _Instruments(
        checks=meter.create_counter(
            "waddles_jwt_revocation_checks_total",
            unit="{check}",
            description=(
                "Revocation checks by outcome. revoked_* = a revoked token was presented; "
                "degraded_allowed / unavailable_* = the revocation store was unreachable."
            ),
        ),
        writes=meter.create_counter(
            "waddles_jwt_revocations_total",
            unit="{revocation}",
            description="Revocation records written, by kind (jti / sid / sub).",
        ),
        write_errors=meter.create_counter(
            "waddles_jwt_revocation_write_errors_total",
            unit="{error}",
            description="Revocations that could not be recorded in the store, by kind.",
        ),
        store_errors=meter.create_counter(
            "waddles_jwt_revocation_store_errors_total",
            unit="{error}",
            description="Revocation store read failures (each opens the lookup circuit).",
        ),
        lookup_seconds=meter.create_histogram(
            "waddles_jwt_revocation_lookup_seconds",
            unit="s",
            description="Wall time of one revocation store round trip, by backend.",
        ),
    )


class TokenRevocation:
    """Revocation policy + local cache + failure handling over a :class:`RevocationBackend`."""

    def __init__(
        self,
        backend: RevocationBackend,
        *,
        policy: RevocationPolicy | None = None,
        backend_name: str = BACKEND_MEMORY,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        meter_provider: Any = None,
    ) -> None:
        """Bind to ``backend``; ``clock``/``monotonic`` are injectable for deterministic tests."""
        self._backend = backend
        self.policy = policy or RevocationPolicy()
        self.backend_name = backend_name
        self._clock = clock
        self._monotonic = monotonic
        self._cache: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()
        self._instruments = _build_instruments(meter_provider)
        self._tracer = trace.get_tracer("waddles.flask_core.jwt")
        self._last_ok = monotonic()
        self._circuit_open_until = 0.0
        self._last_logged: dict[str, float] = {}

    # ------------------------------------------------------------------ read side

    def check(self, claims: Mapping[str, Any]) -> RevocationVerdict:
        """Decide whether the already-signature-verified ``claims`` may be accepted.

        Looks up the token's ``jti``, ``sid`` and ``sub`` records (cache first, then one
        round trip) and applies the outage policy when the store cannot answer. Never raises
        for a store failure -- the policy decides -- and never logs token data.
        """
        subject = claims.get("sub")
        issued_at = claims.get("iat")
        keys: dict[RevocationKind, str] = {}
        if isinstance(subject, str) and subject:
            keys[RevocationKind.USER] = revocation_key(RevocationKind.USER, subject)
        for kind in (RevocationKind.JTI, RevocationKind.SID):
            value = claims.get(kind.value)
            if is_valid_identifier(value):
                keys[kind] = revocation_key(kind, value)

        now = self._monotonic()
        values: dict[str, int | None] = {}
        missing: list[str] = []
        with self._lock:
            for key in keys.values():
                entry = self._cache.get(key)
                if entry is not None and entry.fresh_until > now:
                    values[key] = entry.value
                    self._cache.move_to_end(key)
                else:
                    missing.append(key)

        # A cached "revoked" answer settles the verdict without asking the store about the rest
        # -- the hot path for a client retrying with a token it was already told is dead.
        known = self._revoked_outcome(keys, values, issued_at)
        if known is not None:
            return self._finish(known)

        stale_used = False
        if missing:
            fetched = self._fetch(missing, now)
            if fetched is None:
                stale_used = True
                with self._lock:
                    for key in missing:
                        entry = self._cache.get(key)
                        values[key] = entry.value if entry is not None else None
            else:
                values.update(fetched)

        revoked = self._revoked_outcome(keys, values, issued_at)
        if revoked is not None:
            return self._finish(revoked)
        if not stale_used:
            return self._finish(CHECK_OK)
        return self._finish(self._outage_outcome(now))

    def issue_floor(self, sub: str) -> int | None:
        """The per-user revocation cutoff for ``sub`` (``None`` if none is known), for minters.

        A token must never be swept by a revoke-all that happened BEFORE it was minted: ``iat``
        has one-second resolution, so a login in the same second as the revocation would carry
        ``iat == cutoff`` and be rejected. A minter that cares (user logins) takes
        ``max(now, floor + 1)`` as the new token's ``iat`` -- at most one second ahead, well inside
        the verifier's 30 s skew. A store outage yields ``None`` (mint with ``now``; the sweep
        ambiguity only exists within the revoking second and a dead store cannot sweep anyway).
        """
        if not isinstance(sub, str) or not sub or len(sub) > MAX_IDENTIFIER_LENGTH:
            return None
        key = revocation_key(RevocationKind.USER, sub)
        now = self._monotonic()
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and entry.fresh_until > now:
                return entry.value
        fetched = self._fetch([key], now)
        return fetched[key] if fetched is not None else None

    async def aissue_floor(self, sub: str) -> int | None:
        """:meth:`issue_floor` off the event loop."""
        return await asyncio.to_thread(self.issue_floor, sub)

    def _revoked_outcome(
        self, keys: Mapping[RevocationKind, str], values: Mapping[str, int | None], issued_at: Any
    ) -> str | None:
        """The revoked outcome the known ``values`` prove, or ``None``."""
        jti_key = keys.get(RevocationKind.JTI)
        if jti_key is not None and values.get(jti_key) is not None:
            return CHECK_REVOKED_JTI
        sid_key = keys.get(RevocationKind.SID)
        if sid_key is not None and values.get(sid_key) is not None:
            return CHECK_REVOKED_SID
        user_key = keys.get(RevocationKind.USER)
        cutoff = values.get(user_key) if user_key is not None else None
        if cutoff is not None:
            try:
                if int(issued_at) <= cutoff:
                    return CHECK_REVOKED_USER
            except (TypeError, ValueError):
                return CHECK_REVOKED_USER  # an unreadable `iat` cannot prove it post-dates the cutoff
        return None

    def _outage_outcome(self, now: float) -> str:
        """What to do with a token the store could not vouch for (grace, then the fail mode)."""
        if now - self._last_ok <= self.policy.grace_seconds:
            self._log_rate_limited(
                "degraded",
                30.0,
                logging.WARNING,
                "JWT revocation store unreachable: serving last-known revocation view "
                "(backend=%s grace_s=%s)",
                self.backend_name,
                self.policy.grace_seconds,
            )
            return CHECK_DEGRADED_ALLOWED
        if self.policy.fail_closed:
            self._log_rate_limited(
                "unavailable_rejected",
                30.0,
                logging.ERROR,
                "JWT revocation store unreachable beyond the grace window: rejecting tokens "
                "(fail closed; backend=%s)",
                self.backend_name,
            )
            return CHECK_UNAVAILABLE_REJECTED
        self._log_rate_limited(
            "unavailable_allowed",
            30.0,
            logging.CRITICAL,
            "JWT revocation store unreachable beyond the grace window and "
            "JWT_REVOCATION_FAIL_MODE=open: accepting tokens WITHOUT a revocation check "
            "(backend=%s)",
            self.backend_name,
        )
        return CHECK_UNAVAILABLE_ALLOWED

    def _fetch(self, keys: list[str], now: float) -> dict[str, int | None] | None:
        """One store round trip for ``keys``; ``None`` when the store cannot answer right now."""
        if now < self._circuit_open_until:
            return None
        started = time.perf_counter()
        try:
            with self._tracer.start_as_current_span("jwt.revocation.lookup") as span:
                span.set_attribute("waddles.revocation.keys", len(keys))
                span.set_attribute("waddles.revocation.backend", self.backend_name)
                found = self._backend.get_many(keys)
        except RevocationStoreError as exc:
            self._circuit_open_until = now + self.policy.circuit_open_seconds
            self._instruments.store_errors.add(1, {"backend": self.backend_name})
            self._log_rate_limited(
                "store_error",
                10.0,
                logging.ERROR,
                "JWT revocation store lookup failed (%s); lookup circuit open for %ss",
                exc,
                self.policy.circuit_open_seconds,
            )
            return None
        finally:
            self._record_lookup(started)
        self._last_ok = now
        out = dict(zip(keys, found, strict=True))
        for key, value in out.items():
            self._remember(key, value, now)
        return out

    def _record_lookup(self, started: float) -> None:
        try:
            self._instruments.lookup_seconds.record(
                max(time.perf_counter() - started, 0.0), {"backend": self.backend_name}
            )
        except Exception as exc:  # noqa: BLE001 - telemetry must never change the verdict
            logger.warning("JWT revocation metric emit failed (%s)", type(exc).__name__)

    def _finish(self, outcome: str) -> RevocationVerdict:
        """Emit the per-outcome counter and wrap the verdict."""
        try:
            self._instruments.checks.add(1, {"outcome": outcome})
        except Exception as exc:  # noqa: BLE001 - telemetry must never change the verdict
            logger.warning("JWT revocation metric emit failed (%s)", type(exc).__name__)
        return RevocationVerdict(allowed=outcome not in _REJECTED_OUTCOMES, outcome=outcome)

    def _remember(self, key: str, value: int | None, now: float) -> None:
        """Cache one store value: revoked ``jti``/``sid`` records for their whole life, rest briefly."""
        permanent = value is not None and (
            key.startswith(f"{REVOKED_KEY_PREFIX}:{RevocationKind.JTI.value}:")
            or key.startswith(f"{REVOKED_KEY_PREFIX}:{RevocationKind.SID.value}:")
        )
        ttl = self.policy.record_ttl_seconds if permanent else self.policy.cache_ttl_seconds
        with self._lock:
            existing = self._cache.get(key)
            if existing is not None and existing.value is not None and value is not None:
                value = max(existing.value, value)
            self._cache[key] = _Entry(value=value, fresh_until=now + ttl)
            self._cache.move_to_end(key)
            while len(self._cache) > self.policy.cache_max_entries:
                self._cache.popitem(last=False)

    def _log_rate_limited(self, tag: str, interval: float, level: int, msg: str, *args: Any) -> None:
        """Log at most once per ``interval`` seconds per ``tag`` (outages must not flood logs)."""
        now = self._monotonic()
        with self._lock:
            last = self._last_logged.get(tag)
            if last is not None and now - last < interval:
                return
            self._last_logged[tag] = now
        logger.log(
            level,
            msg,
            *args,
            extra={"event_type": "AUTH", "action": "jwt_revocation", "result": tag},
        )

    # ----------------------------------------------------------------- write side

    def revoke_token(self, jti: str, *, exp: int | None = None) -> None:
        """Reject every future use of the single token ``jti`` (until it would expire anyway)."""
        ttl = self.policy.record_ttl_seconds
        if exp is not None:
            ttl = min(ttl, max(1, int(exp - self._clock()) + _ISSUED_AT_SKEW_SECONDS + 1))
        self._write(RevocationKind.JTI, jti, int(self._clock()), ttl)

    def revoke_session(self, sid: str) -> None:
        """Reject every access token minted under login session ``sid``."""
        self._write(RevocationKind.SID, sid, int(self._clock()), self.policy.record_ttl_seconds)

    def revoke_user(self, sub: str, *, cutoff: int | None = None) -> int:
        """Reject every token of ``sub`` issued at or before ``cutoff`` (default: now).

        ``iat`` has one-second resolution, so a token minted in the same second as the
        revocation is rejected too -- the safe side of the race. Returns the cutoff written.
        """
        effective = int(self._clock()) if cutoff is None else int(cutoff)
        self._write(RevocationKind.USER, sub, effective, self.policy.record_ttl_seconds)
        return effective

    def _write(self, kind: RevocationKind, value: str, record: int, ttl: int) -> None:
        if kind is RevocationKind.USER:
            if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER_LENGTH:
                raise ValueError("sub must be a non-empty string of at most 128 characters")
        elif not is_valid_identifier(value):
            raise ValueError(f"{kind.value} must match [A-Za-z0-9_][A-Za-z0-9_.:-]{{0,127}}")
        key = revocation_key(kind, value)
        # Write-through first: this replica rejects the token from now on even if the shared
        # store turns out to be unreachable below.
        self._remember_written(key, record, kind)
        try:
            self._backend.put_max(key, record, ttl)
        except RevocationStoreError as exc:
            self._instruments.write_errors.add(1, {"kind": kind.value})
            logger.error(
                "JWT revocation could not be recorded in the shared store (kind=%s error=%s): "
                "only this replica rejects the token until it expires",
                kind.value,
                exc,
                extra={"event_type": "AUTH", "action": "jwt_revoke", "result": "FAILURE"},
            )
            raise
        self._instruments.writes.add(1, {"kind": kind.value})
        logger.info(
            "JWT revocation recorded (kind=%s ttl_s=%s)",
            kind.value,
            ttl,
            extra={"event_type": "AUTH", "action": "jwt_revoke", "result": "SUCCESS"},
        )

    def _remember_written(self, key: str, record: int, kind: RevocationKind) -> None:
        with self._lock:
            existing = self._cache.get(key)
            value = record if existing is None or existing.value is None else max(existing.value, record)
            ttl = (
                self.policy.cache_ttl_seconds
                if kind is RevocationKind.USER
                else self.policy.record_ttl_seconds
            )
            self._cache[key] = _Entry(value=value, fresh_until=self._monotonic() + ttl)
            self._cache.move_to_end(key)

    async def arevoke_token(self, jti: str, *, exp: int | None = None) -> None:
        """:meth:`revoke_token` off the event loop."""
        await asyncio.to_thread(self.revoke_token, jti, exp=exp)

    async def arevoke_session(self, sid: str) -> None:
        """:meth:`revoke_session` off the event loop."""
        await asyncio.to_thread(self.revoke_session, sid)

    async def arevoke_user(self, sub: str, *, cutoff: int | None = None) -> int:
        """:meth:`revoke_user` off the event loop."""
        return await asyncio.to_thread(self.revoke_user, sub, cutoff=cutoff)

    # -------------------------------------------------------------------- markers

    def put_marker(self, name: str, ttl_seconds: int) -> None:
        """Record a short-lived marker (e.g. "this refresh token was just rotated").

        Raises :class:`RevocationStoreError` when the store cannot take it -- callers that rely
        on a marker must decide what its absence means; it is never silently skipped.
        """
        if not is_valid_identifier(name):
            raise ValueError("marker name must be a URL-safe identifier")
        self._backend.put_max(f"{MARKER_KEY_PREFIX}:{name}", int(self._clock()), max(1, ttl_seconds))

    def has_marker(self, name: str) -> bool:
        """True when marker ``name`` is live; raises :class:`RevocationStoreError` on a dead store."""
        if not is_valid_identifier(name):
            raise ValueError("marker name must be a URL-safe identifier")
        return self._backend.get_many([f"{MARKER_KEY_PREFIX}:{name}"])[0] is not None

    async def aput_marker(self, name: str, ttl_seconds: int) -> None:
        """:meth:`put_marker` off the event loop."""
        await asyncio.to_thread(self.put_marker, name, ttl_seconds)

    async def ahas_marker(self, name: str) -> bool:
        """:meth:`has_marker` off the event loop."""
        return await asyncio.to_thread(self.has_marker, name)

    def close(self) -> None:
        """Release the backend connection."""
        self._backend.close()


def build_from_env(env: Mapping[str, str] | None = None) -> TokenRevocation:
    """Construct the process's :class:`TokenRevocation` from the environment.

    ``JWT_REVOCATION_BACKEND`` = ``auto`` (default: Valkey when ``VALKEY_URL``/``REDIS_URL`` is
    set, else process-local), ``valkey`` (URL required) or ``memory``. ``VALKEY_PASSWORD`` is
    injected when the URL carries no credentials, matching the chart's credential-free
    ``VALKEY_URL_TLS``. Policy knobs: see :func:`policy_from_env`.
    """
    source = os.environ if env is None else env
    policy = policy_from_env(source)
    choice = source.get("JWT_REVOCATION_BACKEND", "").strip().lower() or BACKEND_AUTO
    if choice not in {BACKEND_AUTO, BACKEND_VALKEY, BACKEND_MEMORY}:
        raise ValueError("JWT_REVOCATION_BACKEND must be 'auto', 'valkey' or 'memory'")
    url = source.get("VALKEY_URL", "").strip() or source.get("REDIS_URL", "").strip()

    if choice == BACKEND_VALKEY and not url:
        raise ValueError("JWT_REVOCATION_BACKEND=valkey needs VALKEY_URL or REDIS_URL")
    if choice == BACKEND_MEMORY or not url:
        logger.warning(
            "JWT revocation is PROCESS-LOCAL (backend=memory): a revoked token is rejected only "
            "by the replica that processed the revocation. Set VALKEY_URL for shared revocation."
        )
        return TokenRevocation(MemoryBackend(), policy=policy, backend_name=BACKEND_MEMORY)

    backend = ValkeyBackend.from_url(
        with_password(url, source.get("VALKEY_PASSWORD", "")),
        timeout_seconds=policy.store_timeout_seconds,
    )
    logger.info(
        "JWT revocation store: valkey (cache_ttl_s=%s grace_s=%s fail_mode=%s)",
        policy.cache_ttl_seconds,
        policy.grace_seconds,
        "closed" if policy.fail_closed else "open",
    )
    return TokenRevocation(backend, policy=policy, backend_name=BACKEND_VALKEY)


_instance: TokenRevocation | None = None
_instance_lock = threading.Lock()


def get_token_revocation() -> TokenRevocation:
    """The process-wide :class:`TokenRevocation`, built from the environment on first use.

    Misconfiguration (e.g. an invalid ``JWT_REVOCATION_*`` value) raises here -- and therefore
    out of the first token verification -- rather than silently running without revocation.
    """
    global _instance
    instance = _instance
    if instance is not None:
        return instance
    with _instance_lock:
        if _instance is None:
            _instance = build_from_env()
        return _instance


def configure_token_revocation(instance: TokenRevocation | None) -> TokenRevocation | None:
    """Install ``instance`` as the process-wide revocation (``None`` re-arms lazy env build).

    Returns the previous instance so tests and embedding apps can restore it. The previous
    instance is *not* closed here -- the caller that built it owns its lifecycle.
    """
    global _instance
    with _instance_lock:
        previous = _instance
        _instance = instance
    return previous
