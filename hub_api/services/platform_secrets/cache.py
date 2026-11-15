"""In-process cache of opened platform secrets: seeded at boot, lazy-loaded on a miss, refreshed.

A decision-gating cache that is only ever invalidated never fills and fails every check closed, so
this one is seeded by the bootstrap, lazy-loads on a miss (rate-limited by a short negative cache so
an unknown id cannot hammer the database), and is refreshed on `NOTIFY platform_secrets_changed`
plus a 60 s poll (see `refresher`). Behaviour on the security-relevant edges:

  * an unknown key id FAILS CLOSED (:class:`UnknownKeyIdError`) -- never fallback to active key;
  * a verify-only version is honoured only until its `verify_until` (demotion time + the type's
    window), checked at lookup time, so a stale cache cannot keep a retired key alive;
  * a refresh that fails keeps the last-known entries (hub-api keeps serving) and raises to the
    caller, which counts it -- except that expiry is still enforced from the clock;
  * values live only here, in memory; `CachedSecret.__repr__` never shows them.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

from services.platform_secrets.registry import REGISTRY, SecretType
from services.platform_secrets.repository import STATE_ACTIVE, STATE_VERIFY_ONLY

logger = logging.getLogger(__name__)

#: How long a failed lookup is remembered, so a stream of unknown key ids costs one DB read.
NEGATIVE_TTL_SECONDS: Final = 30.0

#: Hard cap on remembered unknown ids, so a stream of distinct hostile kids cannot grow memory.
MAX_NEGATIVE_ENTRIES: Final = 1024

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    """Current UTC time (the default cache clock)."""
    return datetime.now(UTC)


class UnknownKeyIdError(LookupError):
    """No servable version has that key id (unknown, retired, or past `verify_until`)."""


class NoActiveSecretError(LookupError):
    """No active version exists for the (type, scope) -- the caller must fail closed."""


@dataclass(slots=True, frozen=True)
class CachedSecret:
    """One opened version with `plaintext` hidden from repr.

    Never log or label with any field here except the ints (`secret_type`, `version`).
    """

    secret_type: int
    scope_id: int | None
    version: int
    state: int
    kid: str | None
    public_id: uuid.UUID
    public_material: bytes | None
    activated_at: datetime | None
    verify_until: datetime | None
    plaintext: bytes = field(repr=False)

    def servable(self, now: datetime) -> bool:
        """Active always; verify-only only before `verify_until` (a missing bound is expired)."""
        if self.state == STATE_ACTIVE:
            return True
        if self.state == STATE_VERIFY_ONLY:
            return self.verify_until is not None and now < self.verify_until
        return False


def verify_until_for(secret_type: int, state: int, rotated_at: datetime | None) -> datetime | None:
    """The instant a verify-only version stops being honoured; None for active; epoch if unknown.

    `rotated_at` (set when the version was demoted) plus the registry window. A verify-only row
    with no `rotated_at` gets the epoch, i.e. it is never served (fail closed).
    """
    if state != STATE_VERIFY_ONLY:
        return None
    if rotated_at is None:
        return datetime.fromtimestamp(0, UTC)
    spec = REGISTRY.get(SecretType(secret_type))
    window = 0 if spec is None else spec.verify_window_s
    return rotated_at + timedelta(seconds=window)


Loader = Callable[[], list[CachedSecret]]


class PlatformSecretCache:
    """Thread-safe cache keyed by (type, scope, version), indexed by active version and kid."""

    def __init__(
        self,
        loader: Loader | None = None,
        *,
        clock: Clock = _utcnow,
        negative_ttl_s: float = NEGATIVE_TTL_SECONDS,
    ) -> None:
        """`loader` re-reads and opens everything servable (None = a seed-only cache)."""
        self._loader = loader
        self._clock = clock
        self._negative_ttl = timedelta(seconds=negative_ttl_s)
        self._lock = threading.RLock()
        self._entries: dict[tuple[int, int | None, int], CachedSecret] = {}
        self._negative: dict[tuple[int, int | None, str], datetime] = {}
        self._last_miss_load: datetime | None = None
        self._last_kid_miss_load: datetime | None = None

    def seed(self, entries: Iterable[CachedSecret]) -> None:
        """Replace the whole content (boot seed or a successful refresh)."""
        fresh = {(e.secret_type, e.scope_id, e.version): e for e in entries}
        with self._lock:
            self._entries = fresh
            # Keep remembered misses (a reload must not hand an attacker a free reset of the
            # negative cache); drop only the kids this snapshot now contains.
            present = {(e.secret_type, e.scope_id, e.kid) for e in fresh.values() if e.kid}
            for key in [k for k in self._negative if k in present]:
                del self._negative[key]

    def refresh(self) -> int:
        """Reload through the loader and swap atomically; returns the entry count.

        A loader failure propagates and leaves the previous (last-known) content in place.
        """
        if self._loader is None:
            return len(self._entries)
        loaded = self._loader()
        self.seed(loaded)
        return len(loaded)

    def _lazy_load(self, key: tuple[int, int | None, str] | None) -> str:
        """One rate-limited reload on a miss; returns what happened.

        `ran` (a reload completed), `negative` (that kid is already remembered as unknown),
        `limited` (the global limiter blocked the reload) or `error` (the loader failed).
        EVERY miss reload is bounded by a global limiter (one per `negative_ttl`, separately for
        active-version misses and kid misses), on top of the per-kid negative cache: a stream of
        distinct unknown kids costs at most one database reload per interval, never one each.
        Only `ran` proves a kid unknown -- the caller must NOT negative-cache a kid after
        `limited` / `error`, or an attacker's flood could starve adoption of a real rotation.
        """
        if self._loader is None:
            return "error"
        now = self._clock()
        with self._lock:
            if key is not None and (until := self._negative.get(key)) is not None and now < until:
                return "negative"
            last = self._last_miss_load if key is None else self._last_kid_miss_load
            if last is not None and now < last + self._negative_ttl:
                return "limited"
            if key is None:
                self._last_miss_load = now
            else:
                self._last_kid_miss_load = now
        try:
            self.refresh()
        except Exception as exc:  # noqa: BLE001 -- lookup must fail closed, not crash the caller
            logger.error("platform secret lazy-load failed error_type=%s", type(exc).__name__)
            return "error"
        return "ran"

    def _remember_miss(
        self, key: tuple[int, int | None, str], until: datetime, now: datetime
    ) -> None:
        """Record an unknown id in the bounded negative cache (expired first, then oldest)."""
        with self._lock:
            if len(self._negative) >= MAX_NEGATIVE_ENTRIES:
                for stale in [k for k, u in self._negative.items() if u <= now]:
                    del self._negative[stale]
            while len(self._negative) >= MAX_NEGATIVE_ENTRIES:
                del self._negative[min(self._negative, key=self._negative.__getitem__)]
            self._negative[key] = until

    def active(self, secret_type: int, scope_id: int | None = None) -> CachedSecret:
        """The ACTIVE version of (type, scope); lazy-loads once on a miss, else fails closed."""
        found = self._find_active(secret_type, scope_id)
        if found is None and self._lazy_load(None) == "ran":
            found = self._find_active(secret_type, scope_id)
        if found is None:
            raise NoActiveSecretError("no active platform secret for that type and scope")
        return found

    def _find_active(self, secret_type: int, scope_id: int | None) -> CachedSecret | None:
        """Highest-version ACTIVE entry of (type, scope), if any."""
        with self._lock:
            candidates = [
                e
                for (t, s, _v), e in self._entries.items()
                if t == secret_type and s == scope_id and e.state == STATE_ACTIVE
            ]
        return max(candidates, key=lambda e: e.version, default=None)

    def by_kid(self, secret_type: int, kid: str, scope_id: int | None = None) -> CachedSecret:
        """The servable version with key id `kid`; an unknown / expired id FAILS CLOSED."""
        now = self._clock()
        found = self._find_kid(secret_type, scope_id, kid, now)
        if found is not None:
            return found
        negative_key = (secret_type, scope_id, kid)
        outcome = self._lazy_load(negative_key)
        if outcome == "ran":
            found = self._find_kid(secret_type, scope_id, kid, now)
            if found is not None:
                return found
            # A completed reload proves the kid unknown: remember it (bounded).
            self._remember_miss(negative_key, now + self._negative_ttl, now)
        # limited / error / already-negative: do NOT remember it -- the rotation NOTIFY refresh
        # (or the next interval's reload) must still be able to adopt a legitimately new kid.
        raise UnknownKeyIdError("no servable platform key has that key id")

    def _find_kid(
        self, secret_type: int, scope_id: int | None, kid: str, now: datetime
    ) -> CachedSecret | None:
        """An entry with that kid that is still servable at `now`, if any."""
        with self._lock:
            for (t, s, _v), entry in self._entries.items():
                if t == secret_type and s == scope_id and entry.kid == kid:
                    return entry if entry.servable(now) else None
        return None

    def servable_versions(
        self, secret_type: int, scope_id: int | None = None
    ) -> list[CachedSecret]:
        """Every servable version of (type, scope), newest first."""
        now = self._clock()
        with self._lock:
            found = [
                e
                for (t, s, _v), e in self._entries.items()
                if t == secret_type and s == scope_id and e.servable(now)
            ]
        return sorted(found, key=lambda e: e.version, reverse=True)

    def gauge_snapshot(self) -> list[tuple[str, int, float]]:
        """`(type name, active versions, age of the newest active in seconds)` per registry type."""
        now = self._clock()
        out: list[tuple[str, int, float]] = []
        for secret_type, spec in REGISTRY.items():
            with self._lock:
                active = [
                    e
                    for (t, _s, _v), e in self._entries.items()
                    if t == int(secret_type) and e.state == STATE_ACTIVE
                ]
            ages = [
                (now - e.activated_at).total_seconds() for e in active if e.activated_at is not None
            ]
            out.append((spec.name, len(active), min(ages, default=0.0)))
        return out
