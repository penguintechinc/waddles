"""Global sliding-window HTTP rate limiting for any flask_core Quart service -- security.md A04.

Security review found 97-102 of 164 core-service HTTP endpoints with zero
rate limiting -- only `ai_researcher_module` (its own feature-level
`services/rate_limiter.py`, per-command not per-HTTP-request) and
`hub_api`'s `services/webhook_api.py` had any enforcement. `hub_api` itself
fixed this globally in gh security PR #255
(`hub_api/services/rate_limiting.py`) with a single Quart `before_request`
hook rather than 449 individual per-route decorators, which would silently
miss the 450th route added later.

This module extracts that same approach into `flask_core` so every
`core/*` service shares one implementation instead of re-copying it --
built on the same `flask_core.rate_limiter.RateLimiter` (Redis sliding
window, in-memory fail-open fallback) hub_api's own module already uses.
`hub_api/services/rate_limiting.py` keeps its own copy (its config shape --
a frozen `HubAPIConfig` dataclass with its own auth-tier path list -- and
its 2100+ already-green tests are out of scope for this change); every
`core/*` service wires this one directly.

Usage (one line, at module scope in a service's `app.py`, alongside its
other blueprint registrations)::

    from flask_core import install_rate_limiting

    install_rate_limiting(app, namespace=Config.MODULE_NAME)

Two tiers, same as hub_api:

- **auth**: paths matching `auth_path_prefixes` (a service passes its own
  login/credential/token-sensitive routes, if any) -- the brute-force-
  sensitive surface, tighter defaults.
- **standard**: everything else.

Identifier is per-authenticated-user (JWT `sub` claim) when a valid bearer
token is present, else per-client-IP -- crucially IP-based for unauthenticated
requests (including a failed/absent token on a login-style endpoint itself),
which is exactly the brute-force surface the auth tier exists to bound.
Health probes are exempt so K8s liveness/readiness never 429s.

`_client_ip()` never trusts `X-Forwarded-For`'s left-most (client-suppliable)
hop by default -- see its own docstring and `trusted_proxy_hops` for why an
unconditional left-most-hop read is a rate-limit-bucket-selection bypass,
not just an inaccurate IP (mirrors hub_api's own fix for the identical gap).
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Iterable

from quart import Quart, request

from .api_utils import error_response
from .auth import verify_jwt_token
from .rate_limiter import RateLimiter

#: K8s liveness/readiness/metrics probes -- never rate limited, or a busy
#: cluster starts killing healthy pods for the wrong reason.
DEFAULT_EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/healthz", "/metrics"})

#: `app.config` key the installed `RateLimiter` instance is stored under.
RATE_LIMITER_CONFIG_KEY = "rate_limiter"

#: Hardcoded sensible defaults (env-overridable) -- deliberately generous
#: for the standard tier (this hook covers every route in a service, unlike
#: hub_api's per-blueprint tuning) and tight for the auth tier.
_DEFAULT_MAX_REQUESTS = 120
_DEFAULT_WINDOW_SECONDS = 60
_DEFAULT_AUTH_MAX_REQUESTS = 20
_DEFAULT_AUTH_WINDOW_SECONDS = 60


def _resolve_redis_url(redis_url: str | None) -> str:
    """`redis_url` if given, else `VALKEY_URL`/`REDIS_URL` env vars, else the in-cluster default.

    `REDIS_URL` is the canonical shared secret every core service already
    receives (`k8s/helm/waddlebot/templates/secrets.yaml`) -- `VALKEY_URL`
    is checked first only so a service migrated to a Valkey-labeled secret
    doesn't need a code change to pick it up.
    """
    return redis_url or os.getenv("VALKEY_URL") or os.getenv("REDIS_URL") or "redis://redis:6379/0"


def _client_ip(trusted_proxy_hops: int) -> str:
    """Client IP for rate-limit bucketing -- never trusts a client-controlled header alone.

    Identical logic to `hub_api/services/rate_limiting.py::_client_ip` --
    see that module's docstring for the full spoofing rationale. Summary:
    `X-Forwarded-For` is entirely client-suppliable unless a known number of
    trusted proxies (`trusted_proxy_hops`) are guaranteed to append their own
    observed peer address; with N trusted hops, `hops[-N]` is the first
    trusted proxy's direct observation of the real client, unaffected by
    anything the client itself prepended further left. Falls back to
    `request.remote_addr` whenever `trusted_proxy_hops` is 0, the header is
    absent, there aren't enough hops to index, or the chosen value doesn't
    parse as an IP.
    """
    if trusted_proxy_hops <= 0:
        return request.remote_addr or "unknown"

    forwarded = request.headers.get("X-Forwarded-For")
    if not forwarded:
        return request.remote_addr or "unknown"

    hops = [hop.strip() for hop in forwarded.split(",")]
    if trusted_proxy_hops > len(hops):
        return request.remote_addr or "unknown"

    candidate = hops[-trusted_proxy_hops]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return request.remote_addr or "unknown"
    return candidate


def _current_user_id() -> str | None:
    """The bearer JWT's `sub` claim, or `None` if absent/invalid -- never raises."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    payload = verify_jwt_token(auth_header[7:])
    if payload is None:
        return None
    sub = payload.get("sub")
    return str(sub) if sub is not None else None


def _rate_limit_identifier(trusted_proxy_hops: int) -> str:
    """Per-user identifier when authenticated, else per-IP (the brute-force surface)."""
    user_id = _current_user_id()
    if user_id is not None:
        return f"user:{user_id}"
    return f"ip:{_client_ip(trusted_proxy_hops)}"


def install_rate_limiting(
    app: Quart,
    *,
    namespace: str,
    redis_url: str | None = None,
    max_requests: int | None = None,
    window_seconds: int | None = None,
    auth_max_requests: int | None = None,
    auth_window_seconds: int | None = None,
    auth_path_prefixes: Iterable[str] = (),
    exempt_paths: frozenset[str] = DEFAULT_EXEMPT_PATHS,
    trusted_proxy_hops: int = 0,
) -> RateLimiter:
    """Wire a global rate-limit `before_request` hook onto `app` -- one call site covers every route.

    Owns its own Redis connect/disconnect lifecycle via `before_serving`/
    `after_serving` (registered alongside, never replacing, a service's own
    hooks -- Quart runs every registered hook) so a service only needs the
    single call shown in this module's docstring.

    Args:
        app: The Quart application.
        namespace: Rate-limit key namespace -- pass `Config.MODULE_NAME` so
            two services never collide on the same Redis keyspace.
        redis_url: Explicit Redis/Valkey URL; falls back to `VALKEY_URL`/
            `REDIS_URL` env vars, then an in-cluster default. `RateLimiter`
            itself fails open to an in-memory fallback if unreachable.
        max_requests: Standard-tier request budget per `window_seconds`.
            Defaults to `RATE_LIMIT_MAX_REQUESTS` env var, else 120.
        window_seconds: Standard-tier sliding window, in seconds. Defaults
            to `RATE_LIMIT_WINDOW_SECONDS` env var, else 60.
        auth_max_requests: Auth-tier request budget. Defaults to
            `RATE_LIMIT_AUTH_MAX_REQUESTS` env var, else 20.
        auth_window_seconds: Auth-tier sliding window, in seconds. Defaults
            to `RATE_LIMIT_AUTH_WINDOW_SECONDS` env var, else 60.
        auth_path_prefixes: Path prefixes routed to the stricter auth tier
            (prefix match on `request.path`) -- e.g. login/credential/token
            routes. Empty by default: a service with no such routes gets
            the standard tier everywhere, which is still the fix for "zero
            rate limiting" -- the primary gap this closes.
        exempt_paths: Paths never rate limited (health/metrics probes).
        trusted_proxy_hops: Number of trusted reverse proxies in front of
            this service that append their own observed peer address to
            `X-Forwarded-For` -- see `_client_ip`. Defaults to 0 (header
            not trusted at all), matching hub_api's own default.

    Returns:
        The installed `RateLimiter` (also stashed on
        `app.config[RATE_LIMITER_CONFIG_KEY]`).
    """
    max_requests = max_requests or int(os.getenv("RATE_LIMIT_MAX_REQUESTS", str(_DEFAULT_MAX_REQUESTS)))
    window_seconds = window_seconds or int(
        os.getenv("RATE_LIMIT_WINDOW_SECONDS", str(_DEFAULT_WINDOW_SECONDS))
    )
    auth_max_requests = auth_max_requests or int(
        os.getenv("RATE_LIMIT_AUTH_MAX_REQUESTS", str(_DEFAULT_AUTH_MAX_REQUESTS))
    )
    auth_window_seconds = auth_window_seconds or int(
        os.getenv("RATE_LIMIT_AUTH_WINDOW_SECONDS", str(_DEFAULT_AUTH_WINDOW_SECONDS))
    )
    auth_prefixes = tuple(auth_path_prefixes)

    limiter = RateLimiter(redis_url=_resolve_redis_url(redis_url), namespace=f"{namespace}_rate_limit")
    app.config[RATE_LIMITER_CONFIG_KEY] = limiter

    @app.before_serving
    async def _connect_rate_limiter() -> None:
        await limiter.connect()

    @app.after_serving
    async def _disconnect_rate_limiter() -> None:
        await limiter.disconnect()

    @app.before_request
    async def _enforce_rate_limit() -> tuple[dict[str, object], int] | None:
        """Reject with 429 once the caller's bucket for this tier is exhausted."""
        path = request.path
        if path in exempt_paths:
            return None

        is_auth_tier = any(path.startswith(prefix) for prefix in auth_prefixes)
        if is_auth_tier:
            limit, window, tier_label = auth_max_requests, auth_window_seconds, "auth"
        else:
            limit, window, tier_label = max_requests, window_seconds, "standard"

        # Tier-namespaced so an auth-tier bucket and a standard-tier bucket
        # for the same identifier never collide state.
        key = f"{tier_label}:{_rate_limit_identifier(trusted_proxy_hops)}"

        allowed = await limiter.check_rate_limit(key, limit, window)
        if allowed:
            return None

        response, status_code = error_response(
            "Rate limit exceeded. Please try again later.",
            429,
            "RATE_LIMIT_EXCEEDED",
        )
        response.headers["Retry-After"] = str(window)
        return response, status_code

    return limiter
