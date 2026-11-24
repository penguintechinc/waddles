"""Per-tenant, per-tier community-entitlement POOL limits (Team/OU axis).

A tenant admin allocates Premium entitlements to communities out of ONE pool
whose size is licensed per tenant. This module answers "how big is tenant T's
Premium pool" and fails LOUD when it cannot know:

* License-server deployments: the tenant's own license key is POSTed to
  ``/api/v2/entitlements`` and ``team.premium.limit`` is read (ASSUMED future
  contract, see ``TEAM_AXIS_KEY`` / ``TIER_RESPONSE_KEYS`` and
  ``docs/license-pool.md``; the live server has no team axis yet).
* Bypass domains (hardcoded, never env/flag driven): NO license-server call;
  only the global admin's ``operator_set`` row in the store is read.

A tenant with NO license key is a Free tenant: ``PoolInfo(limit=None)`` ("not licensed"), not an
error. Real failures (outage, bad response, no team axis, unreadable key) raise PoolUnavailable.
Errors are failures: no silent demotion, no default number. ``penguin_licensing``
is deliberately NOT used because it returns the free tier on any error, which
would mask an outage as "pool = 0".
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from opentelemetry import metrics

logger = logging.getLogger(__name__)

# --- ASSUMED license-server contract (verify against alpha; see docs) -------
ENTITLEMENTS_PATH = "/api/v2/entitlements"
TEAM_AXIS_KEY = "team"
TIER_RESPONSE_KEYS: dict[int, str] = {2: "premium"}
UNLIMITED = -1

SOURCE_LICENSE_SERVER = 1
SOURCE_OFFLINE = 2
SOURCE_OPERATOR_SET = 3
_SOURCE_NAMES: dict[int, str] = {
    SOURCE_LICENSE_SERVER: "license_server",
    SOURCE_OFFLINE: "offline",
    SOURCE_OPERATOR_SET: "operator_set",
}

DEFAULT_TTL_SECONDS = 300.0
DEFAULT_GRACE_SECONDS = 72 * 3600.0

_meter = metrics.get_meter("waddles.flask_core.license_pool")
_refresh_counter = _meter.create_counter(
    "waddles_license_pool_refresh_total",
    description="License-server pool refresh attempts by outcome (no tenant labels).",
)
_refresh_histogram = _meter.create_histogram(
    "waddles_license_pool_refresh_duration_seconds",
    unit="s",
    description="Wall time of a license-server pool refresh by outcome.",
)


class PoolUnavailable(Exception):
    """The pool size cannot be determined (never defaulted to 0/free/unlimited)."""


@dataclass(frozen=True, slots=True)
class PoolInfo:
    """A tenant's pool for one tier. limit None = not licensed, -1 = unlimited."""

    limit: int | None
    observed_at: datetime
    stale: bool
    source: Literal["license_server", "offline", "operator_set"]


@dataclass(frozen=True, slots=True)
class PoolRow:
    """Persisted pool row; source_code 1 license_server, 2 offline, 3 operator_set."""

    limit_count: int | None
    observed_at: datetime
    source_code: int


class PoolStore(Protocol):
    """Persistence for pool rows (DB-backed in hub_api; in-memory in tests)."""

    async def get(self, tenant_id: int, tier_code: int) -> PoolRow | None:
        """Return the stored row or None."""
        ...

    async def put(
        self,
        tenant_id: int,
        tier_code: int,
        limit_count: int | None,
        source_code: int,
        observed_at: datetime,
        set_by: int | None = None,
    ) -> None:
        """Upsert the row for (tenant_id, tier_code)."""
        ...

    async def delete_license_server_rows(self, tenant_id: int) -> int:
        """Evict the tenant's license_server-sourced rows (never operator_set); return the count."""
        ...


class PoolProvider(Protocol):
    """Contract consumed by the tier-allocation service (tier_code 2 = premium, the only pool)."""

    async def get_pool(self, tenant_id: int, tier_code: int) -> PoolInfo:
        """Return the pool or raise PoolUnavailable."""
        ...


def _utcnow() -> datetime:
    """Timezone-aware UTC now (default clock)."""
    return datetime.now(timezone.utc)


def _default_http_client() -> httpx.AsyncClient:
    """Strict-TLS client with bounded timeouts and pool (never verify=False)."""
    return httpx.AsyncClient(
        verify=True,
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=False,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
    )


def _aware_utc(value: datetime) -> datetime:
    """Normalize a naive datetime (sqlite/offline rows) to aware UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def validate_base_url(url: str, *, allow_loopback_http: bool = False) -> str:
    """Return the normalized license-server base URL or raise ValueError.

    https only; no userinfo, query or fragment. Plain http is accepted solely for a loopback
    host AND only when the caller opts in (`allow_loopback_http`), so it can never point a
    production/bypass deployment at an unencrypted remote server.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("license server URL must be an absolute https URL")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("license server URL must not contain userinfo")
    if parts.query or parts.fragment:
        raise ValueError("license server URL must not contain a query or fragment")
    if parts.scheme == "http":
        loopback = parts.hostname == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(parts.hostname).is_loopback
            except ValueError:
                loopback = False
        if not (allow_loopback_http and loopback):
            raise ValueError("license server URL must use https")
    return url.rstrip("/")


def _info(row: PoolRow, *, stale: bool) -> PoolInfo:
    """Convert a stored row to a PoolInfo."""
    return PoolInfo(
        limit=row.limit_count,
        observed_at=_aware_utc(row.observed_at),
        stale=stale,
        source=_SOURCE_NAMES[row.source_code],  # type: ignore[arg-type]
    )


class _Refused(Exception):
    """Internal: refresh failed; carries a bounded outcome + reason (no secrets)."""

    def __init__(self, outcome: str, reason: str, *, transient: bool = False) -> None:
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason
        #: Only transient failures (network/timeout/429/5xx) may fall back to a cached pool.
        self.transient = transient


def _parse_team_axis(payload: Any) -> dict[int, int | None]:
    """Extract {tier_code: limit} from the response or raise _Refused(team_axis_absent)."""
    team = payload.get(TEAM_AXIS_KEY) if isinstance(payload, dict) else None
    if not isinstance(team, dict):
        raise _Refused("team_axis_absent", "response has no team axis")
    out: dict[int, int | None] = {}
    for code, key in TIER_RESPONSE_KEYS.items():
        entry = team.get(key)
        if entry is None:
            out[code] = None  # tier not licensed
            continue
        limit = entry.get("limit") if isinstance(entry, dict) else None
        if limit is None:
            out[code] = None
        elif isinstance(limit, int) and not isinstance(limit, bool) and limit >= -1:
            out[code] = limit
        else:
            raise _Refused("error", "invalid team limit value")
    return out


class LicenseServerPoolProvider:
    """PoolProvider backed by the license server, a PoolStore cache and bypass operator rows."""

    def __init__(
        self,
        store: PoolStore,
        license_key_resolver: Callable[[int], Awaitable[str | None] | str | None],
        is_bypass_domain: Callable[[], bool],
        *,
        base_url: str = "",
        product: str = "waddles",
        http_client_factory: Callable[[], httpx.AsyncClient] = _default_http_client,
        ttl: float = DEFAULT_TTL_SECONDS,
        grace: float = DEFAULT_GRACE_SECONDS,
        clock: Callable[[], datetime] = _utcnow,
        max_attempts: int = 3,
        backoff_base: float = 0.25,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        total_timeout: float = 20.0,
        allow_loopback_http: bool = False,
    ) -> None:
        """Wire the provider. `is_bypass_domain()` is evaluated per call (hardcoded domain
        check only); `license_key_resolver(tenant_id)` may be sync or async."""
        self._store = store
        self._resolve_key = license_key_resolver
        self._is_bypass = is_bypass_domain
        self._base_url = (
            validate_base_url(base_url, allow_loopback_http=allow_loopback_http)
            if base_url
            else ""
        )
        self._client: httpx.AsyncClient | None = None
        self._total_timeout = total_timeout
        self._product = product
        self._client_factory = http_client_factory
        self._ttl = ttl
        self._grace = grace
        self._clock = clock
        self._max_attempts = max(1, max_attempts)
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._inflight: dict[int, asyncio.Task[dict[int, int | None]]] = {}

    async def get_pool(self, tenant_id: int, tier_code: int) -> PoolInfo:
        """Return the tenant's pool for `tier_code` or raise PoolUnavailable.

        A tenant with no license key resolves to ``limit=None`` (not licensed), not an error;
        PoolUnavailable is reserved for real failures (outage, bad response, no team axis).
        """
        if tier_code not in TIER_RESPONSE_KEYS:
            raise ValueError(f"tier_code must be one of {sorted(TIER_RESPONSE_KEYS)}")
        now = self._clock()
        if self._is_bypass():
            row = await self._store.get(tenant_id, tier_code)
            if row is not None and row.source_code == SOURCE_OPERATOR_SET:
                return _info(row, stale=False)
            # No operator row: the operator has not licensed this tier -> limit None
            # ("not licensed", surfaced as 402), deliberately not an outage (503).
            return PoolInfo(
                limit=None, observed_at=now, stale=False, source="operator_set"
            )

        row = await self._store.get(tenant_id, tier_code)
        if row is not None and row.source_code == SOURCE_OPERATOR_SET:
            row = None  # operator rows only count on bypass domains
        if (
            row is not None
            and (now - _aware_utc(row.observed_at)).total_seconds() < self._ttl
        ):
            return _info(row, stale=False)

        try:
            limits = await self._refresh_single_flight(tenant_id)
        except _Refused as exc:
            if exc.outcome == "no_tenant_license":
                # A tenant with no license key is a Free tenant: a definitive "not licensed"
                # answer (limit None), deliberately NOT an outage. Any cached pool must not
                # outlive the key's removal.
                logger.info("license_pool.not_licensed tenant_id=%s", tenant_id)
                if row is not None:
                    await self._evict(tenant_id)
                return PoolInfo(
                    limit=None, observed_at=now, stale=False, source="license_server"
                )
            logger.error(
                "license_pool.%s type=%s reason=%s",
                "team_axis_absent"
                if exc.outcome == "team_axis_absent"
                else "refresh_failed",
                exc.outcome,
                exc.reason,
            )
            if exc.transient:
                fallback = self._last_known(row, now)
                if fallback is not None:
                    return fallback
            elif row is not None:
                # Definitive refusal (no key / 401 / 403 / no team axis): the cached
                # pool must not outlive it.
                await self._evict(tenant_id)
            raise PoolUnavailable(exc.reason) from None
        observed = self._clock()
        return PoolInfo(
            limit=limits[tier_code],
            observed_at=observed,
            stale=False,
            source="license_server",
        )

    def _last_known(self, row: PoolRow | None, now: datetime) -> PoolInfo | None:
        """Last-known row within the grace window, marked stale; else None."""
        if row is None or row.source_code == SOURCE_OPERATOR_SET:
            return None
        if (now - _aware_utc(row.observed_at)).total_seconds() > self._grace:
            return None
        return _info(row, stale=True)

    async def _evict(self, tenant_id: int) -> None:
        """Best-effort eviction of cached license_server rows after a definitive refusal."""
        try:
            await self._store.delete_license_server_rows(tenant_id)
        except Exception as exc:  # noqa: BLE001 - eviction must not mask the refusal
            logger.error("license_pool.evict_failed type=%s", type(exc).__name__)

    async def aclose(self) -> None:
        """Close the shared HTTP client (call on app shutdown)."""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _refresh_single_flight(self, tenant_id: int) -> dict[int, int | None]:
        """Coalesce concurrent refreshes for one tenant into a single HTTP round trip."""
        task = self._inflight.get(tenant_id)
        if task is None:
            task = asyncio.ensure_future(self._refresh(tenant_id))
            self._inflight[tenant_id] = task
            task.add_done_callback(lambda _t: self._inflight.pop(tenant_id, None))
        return await asyncio.shield(task)

    async def _refresh(self, tenant_id: int) -> dict[int, int | None]:
        """Fetch, parse and persist the premium pool; record telemetry; raise _Refused on failure."""
        started = time.monotonic()
        outcome = "success"
        try:
            key = self._resolve_key(tenant_id)
            if asyncio.iscoroutine(key) or isinstance(key, asyncio.Future):
                key = await key
            if not key:
                outcome = "no_tenant_license"
                raise _Refused(outcome, "no_tenant_license")
            payload = await self._fetch(str(key))
            limits = _parse_team_axis(payload)
            observed = self._clock()
            for code, limit in limits.items():
                await self._store.put(
                    tenant_id, code, limit, SOURCE_LICENSE_SERVER, observed
                )
            return limits
        except _Refused as exc:
            outcome = exc.outcome
            raise
        except Exception as exc:  # noqa: BLE001 - any failure is a loud refresh failure
            outcome = "error"
            raise _Refused("error", type(exc).__name__, transient=True) from None
        finally:
            attrs = {"outcome": outcome}
            _refresh_counter.add(1, attrs)
            _refresh_histogram.record(time.monotonic() - started, attrs)

    async def _fetch(self, license_key: str) -> Any:
        """POST the entitlements lookup (key in body only); retry transient errors with jitter.

        One shared client (bounded pool) and a hard overall deadline so a slow server cannot
        stack attempts into an unbounded wait.
        """
        try:
            async with asyncio.timeout(self._total_timeout):
                return await self._fetch_attempts(license_key)
        except TimeoutError:
            raise _Refused("error", "timeout", transient=True) from None

    async def _fetch_attempts(self, license_key: str) -> Any:
        """The bounded retry loop behind :meth:`_fetch`."""
        url = f"{self._base_url}{ENTITLEMENTS_PATH}"
        body = {"license_key": license_key, "product": self._product}
        if self._client is None:
            self._client = self._client_factory()
        client = self._client
        last = "unavailable"
        for attempt in range(self._max_attempts):
            if attempt:
                delay = self._backoff_base * (2 ** (attempt - 1))
                await self._sleep(delay + random.uniform(0, delay / 2))
            try:
                resp = await client.post(url, json=body)
            except httpx.HTTPError as exc:
                last = f"transport:{type(exc).__name__}"
                continue
            status = resp.status_code
            if status == 429 or status >= 500:
                last = f"http_{status}"
                continue
            if status != 200:
                # 401/403/404/...: an explicit refusal, never grace-eligible.
                raise _Refused("error", f"http_{status}")
            try:
                return resp.json()
            except ValueError:
                raise _Refused("error", "invalid_json") from None
        raise _Refused("error", last, transient=True)
