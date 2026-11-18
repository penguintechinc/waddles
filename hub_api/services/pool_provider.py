"""App-wide team-pool provider: the license server (with a DB cache) behind one cached instance.

`get_pool_provider(app)` builds a :class:`flask_core.license_pool.LicenseServerPoolProvider`
over the DB-backed :class:`services.community_tier_service.PoolStore` and caches it on the app.

* **License key**: resolved per tenant by ``tenant_license_service.get_tenant_license_key``
  (imported lazily; a missing module or a ``None`` key means "no key", which makes the provider
  fail loud with ``PoolUnavailable`` -- never a silent zero).
* **Bypass domains**: :func:`flask_core.entitlement.is_bypass_domain` evaluated against the
  request host on every call. There is no env/config bypass (``critical-rules.md``). The host is
  the trusted-proxy-aware ``current_request_host()`` (``resolve_trusted_host``: the raw ``Host``
  unless ``TRUSTED_FORWARDED_HOST`` is set AND the peer is in ``TRUSTED_PROXY_CIDRS``), then
  normalized by :func:`normalized_request_host` (lowercase, port stripped, userinfo/odd forms
  rejected -> no bypass).
* **Default-tenant bypass pool**: on a bypass-domain host the DEFAULT tenant (the numeric id of the
  configured ``default_tenant_slug`` row, resolved once and compared by id) gets an UNLIMITED
  Premium pool computed in code (``limit=-1``, ``stale=False``, ``source='bypass_default'``): no
  ``tenant_license_pools`` row and no license-server call. Every other tenant on a bypass host
  keeps the operator-set pool (or "not licensed"); non-bypass hosts are unchanged.
* **Base URL**: ``LICENSE_SERVER_URL`` (the same env ``flask_core.entitlement`` and
  ``config.py`` use), defaulting to the public license server; validated by
  ``flask_core.license_pool.validate_base_url`` (https only, no userinfo/query/fragment; plain
  http only for a loopback host with ``LICENSE_SERVER_ALLOW_LOOPBACK_HTTP=1``, never a remote one).
* **Test seam**: ``app.config["POOL_PROVIDER"]`` overrides the provider entirely.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Callable
from typing import Any, Literal

from flask_core.auth import DEFAULT_TENANT_SLUG
from flask_core.entitlement import is_bypass_domain
from flask_core.feature_flags import current_request_host
from flask_core.license_pool import LicenseServerPoolProvider, validate_base_url
from flask_core.license_pool import PoolUnavailable as LibPoolUnavailable

from services.community_tier_service import (
    POOLED_TIER_CODES,
    UNLIMITED,
    PoolInfo,
    PoolProvider,
    PoolStore,
    PoolUnavailable,
    _end_read,
    now_utc,
)

logger = logging.getLogger(__name__)

#: Config key a test (or an operator-supplied provider) sets to bypass construction.
POOL_PROVIDER_CONFIG_KEY = "POOL_PROVIDER"
_CACHE_KEY = "_pool_provider_cache"
_DEFAULT_LICENSE_SERVER_URL = "https://license.penguintech.io"


#: `PoolInfo.source` label of the in-code bypass-domain default-tenant pool (never stored).
_BYPASS_DEFAULT_SOURCE: Literal["bypass_default"] = "bypass_default"

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")


def normalized_request_host() -> str | None:
    """The current request host, normalized for the bypass check, or None when it is odd.

    Lowercased, a single trailing dot and a single ``:port`` stripped. Userinfo (``@``), paths,
    whitespace, commas, multiple colons (IPv6 / ``a:b:c``) and non-hostname characters yield None
    (no bypass): a malformed Host can only ever fail closed.
    """
    raw = current_request_host()
    if not raw or len(raw) > 300:
        return None
    host = raw.strip().lower()
    if any(ch in host for ch in "@/\\ \t\r\n?#,[]"):
        return None
    if host.count(":") > 1:
        return None
    host = host.split(":", 1)[0].rstrip(".")
    return host if _HOST_RE.match(host) else None


def request_is_bypass_domain() -> bool:
    """Hardcoded-domain bypass for the current request host (never env/config driven)."""
    return bool(is_bypass_domain(normalized_request_host()))


_request_is_bypass_domain = request_is_bypass_domain


def license_server_configured() -> bool:
    """True when this deployment has an explicit license server (``LICENSE_SERVER_URL`` set)."""
    return bool((os.getenv("LICENSE_SERVER_URL") or "").strip())


def _allow_loopback_http() -> bool:
    """Dev-only escape: plain http to a LOOPBACK license server (never a remote host)."""
    return os.getenv("LICENSE_SERVER_ALLOW_LOOPBACK_HTTP", "").strip().lower() in ("1", "true")


def _license_server_url() -> str:
    """Validated base URL of the license server (`LICENSE_SERVER_URL`, else the public default).

    Raises ``ValueError`` for a non-https / userinfo / query / fragment URL (fail loud at
    provider construction rather than POSTing a license key somewhere unsafe).
    """
    return validate_base_url(
        os.getenv("LICENSE_SERVER_URL") or _DEFAULT_LICENSE_SERVER_URL,
        allow_loopback_http=_allow_loopback_http(),
    )


def _make_key_resolver(async_dal: Any, dal: Any) -> Any:
    """Async resolver `tenant_id -> license key | None` backed by the tenant-license service."""

    async def _resolve(tenant_id: int) -> str | None:
        try:
            from services.tenant_license_service import (  # type: ignore[import-not-found,unused-ignore]
                get_tenant_license_key,
            )
        except ImportError:
            logger.warning("pool_provider: tenant_license_service unavailable; no license key")
            return None
        key: str | None = await get_tenant_license_key(async_dal, dal, tenant_id)
        return key

    return _resolve


class DefaultTenantIdResolver:
    """Resolves (and caches) the NUMERIC id of the default tenant from its configured slug.

    The slug is only used for this one lookup; every comparison afterwards is by numeric id. A
    missing row is not cached (a late seed is picked up) and yields ``None`` (no bypass pool).
    """

    def __init__(self, async_dal: Any, dal: Any, slug: str) -> None:
        """Bind the shared pydal handle and the configured default-tenant slug."""
        self._async_dal = async_dal
        self._dal = dal
        self._slug = slug
        self._id: int | None = None

    async def get(self) -> int | None:
        """Return the default tenant's numeric id, or ``None`` when no such tenant row exists."""
        if self._id is not None:
            return self._id
        dal, slug = self._dal, self._slug

        def _job() -> int | None:
            try:
                row = dal(dal.tenants.slug == slug).select(dal.tenants.id).first()
                return None if row is None else int(row.id)
            finally:
                _end_read(dal)

        found = await asyncio.get_running_loop().run_in_executor(self._async_dal.executor, _job)
        self._id = found
        return found


class ServicePoolProvider:
    """Adapts the license lib's provider to the service's own types.

    The lib and `community_tier_service` each define `PoolUnavailable` / `PoolInfo`; the service
    only catches its own, so without this a license-server outage would surface as a 500 instead
    of the fail-loud 503. Also serves the in-code unlimited Premium pool of the default tenant on
    a bypass-domain host (see the module docstring).
    """

    def __init__(
        self,
        inner: LicenseServerPoolProvider,
        *,
        default_tenant_id: Callable[[], Any] | None = None,
        is_bypass: Callable[[], bool] = request_is_bypass_domain,
    ) -> None:
        """Wrap the lib provider; `default_tenant_id` is an async `() -> int | None` resolver."""
        self._inner = inner
        self._default_tenant_id = default_tenant_id
        self._is_bypass = is_bypass

    async def get_pool(self, tenant_id: int, tier_code: int) -> PoolInfo:
        """Return the pool as a service `PoolInfo`; lib `PoolUnavailable` becomes the service's."""
        if tier_code in POOLED_TIER_CODES and self._default_tenant_id is not None:
            if self._is_bypass() and tenant_id == await self._default_tenant_id():
                return PoolInfo(
                    limit=UNLIMITED,
                    observed_at=now_utc(),
                    stale=False,
                    source=_BYPASS_DEFAULT_SOURCE,
                )
        try:
            info = await self._inner.get_pool(tenant_id, tier_code)
        except LibPoolUnavailable as exc:
            raise PoolUnavailable(str(exc)) from None
        return PoolInfo(
            limit=info.limit, observed_at=info.observed_at, stale=info.stale, source=info.source
        )


def get_pool_provider(app: Any) -> PoolProvider:
    """Return the app-wide pool provider (override -> cached -> built once and cached)."""
    override = app.config.get(POOL_PROVIDER_CONFIG_KEY)
    if override is not None:
        return override  # type: ignore[no-any-return]
    cached = app.config.get(_CACHE_KEY)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    async_dal, dal = app.config["async_dal"], app.config["dal"]
    cfg = app.config.get("HUB_API_CONFIG")
    default_slug = str(getattr(cfg, "default_tenant_slug", None) or DEFAULT_TENANT_SLUG)
    provider = ServicePoolProvider(
        LicenseServerPoolProvider(
            PoolStore(async_dal, dal),  # type: ignore[arg-type]
            _make_key_resolver(async_dal, dal),
            request_is_bypass_domain,
            base_url=_license_server_url(),
            allow_loopback_http=_allow_loopback_http(),
        ),
        default_tenant_id=DefaultTenantIdResolver(async_dal, dal, default_slug).get,
    )
    app.config[_CACHE_KEY] = provider
    return provider
