"""Credential Manager - Main application entry point.

Quart async web application that provides:
- Health check endpoint
- Background token refresh polling
- Credential status monitoring
- Force refresh capability
- Redis pub/sub notification on credential changes
"""

from __future__ import annotations

import asyncio
import logging
import sys

from flask_core import install_rate_limiting, install_security_headers
from flask_core.authz import require_scope
from quart import Quart, jsonify

from .config import Config
from .services.refresh_service import RefreshService

logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Quart(__name__)
# security.md A05 hardening -- JSON-only service, default deny-everything CSP.
install_security_headers(app)
refresh_service: RefreshService | None = None
_shutdown_event = asyncio.Event()

# SECURITY (A04): `/api/v1/credentials/*` had zero rate limiting -- these
# routes trigger OAuth refresh cycles and expose integration status, so
# they get the stricter auth tier (a caller who obtains valid
# `credentials:admin` scope still shouldn't be able to hammer them
# unboundedly). Shared global before_request hook, see
# flask_core.http_rate_limit module docstring.
install_rate_limiting(
    app,
    namespace=Config.MODULE_NAME,
    redis_url=Config.REDIS_URL,
    auth_path_prefixes=("/api/v1/credentials",),
    auth_max_requests=20,
    auth_window_seconds=60,
)


@app.before_serving
async def startup() -> None:
    """Initialize the refresh service and start polling."""
    global refresh_service

    errors = Config.validate()
    if errors:
        logger.error("Configuration errors: %s", errors)
        sys.exit(1)

    refresh_service = RefreshService(
        database_url=Config.DATABASE_URL,
        redis_url=Config.REDIS_URL,
        redis_prefix=Config.REDIS_KEY_PREFIX,
        refresh_buffer=Config.TOKEN_REFRESH_BUFFER,
        poll_interval=Config.POLL_INTERVAL,
        max_retries=Config.MAX_REFRESH_RETRIES,
        retry_backoff_base=Config.RETRY_BACKOFF_BASE,
    )
    await refresh_service.start()
    logger.info(
        "Credential Manager started (poll=%ds, buffer=%ds)",
        Config.POLL_INTERVAL,
        Config.TOKEN_REFRESH_BUFFER,
    )


@app.after_serving
async def shutdown() -> None:
    """Stop the refresh service gracefully."""
    if refresh_service:
        await refresh_service.stop()
    logger.info("Credential Manager stopped")


@app.route("/health")
async def health() -> tuple:
    """Health check endpoint."""
    status = "healthy"
    details = {
        "module": Config.MODULE_NAME,
        "version": Config.MODULE_VERSION,
    }

    if refresh_service:
        svc_status = refresh_service.get_status()
        details.update(svc_status)
        if not svc_status.get("running"):
            status = "degraded"

    code = 200 if status == "healthy" else 503
    return jsonify({"status": status, **details}), code


@app.route("/api/v1/credentials/status")
@require_scope("credentials:admin")  # type: ignore[untyped-decorator]
async def credential_status() -> tuple:
    """Return status of all tracked platform integrations.

    SECURITY (C6, A01): this endpoint had ZERO authentication -- any
    caller reaching the service's network address could enumerate every
    tracked OAuth integration's refresh status/error state. Not
    tenant/community-scoped (this service manages platform-wide OAuth
    credentials via `asyncpg`, no pydal `dal`/tenant table), so
    `require_scope` alone (no `tenant_middleware`) is the right layer --
    same JWT-only verification `core/svc_streaming/openapi/routes.py`
    uses for its own `require_scope`-only doc route. `*:admin`
    (SCOPE_BUNDLES `global.admin`) covers `credentials:admin` via the
    established wildcard-resource convention.
    """
    if not refresh_service:
        return jsonify({"error": "Service not initialized"}), 503

    stats = await refresh_service.get_credential_stats()
    return jsonify({"success": True, "stats": stats}), 200


@app.route("/api/v1/credentials/refresh-now", methods=["POST"])
@require_scope("credentials:admin")  # type: ignore[untyped-decorator]
async def force_refresh() -> tuple:
    """Force an immediate credential refresh cycle.

    SECURITY (C6, A01): this endpoint had ZERO authentication -- any
    caller reaching the service's network address could trigger OAuth
    refresh cycles (and their downstream rate-limit/quota consumption) at
    will. See `credential_status`'s own docstring for the scope-only
    rationale.
    """
    if not refresh_service:
        return jsonify({"error": "Service not initialized"}), 503

    count = await refresh_service.run_refresh_cycle()
    return jsonify({
        "success": True,
        "message": f"Refreshed {count} credentials",
    }), 200


_TESTABLE_PLATFORMS = frozenset({"twitch", "discord", "youtube", "slack", "kick", "spotify"})


@app.route("/api/v1/credentials/<platform>/test", methods=["POST"])
@require_scope("credentials:admin")  # type: ignore[untyped-decorator]
async def credential_connection_check(platform: str) -> tuple:
    """Validate the operator BOT credential for `platform` against its provider (gh-969).

    hub-api's DB role cannot read `platform_integrations` (H-3), so its superadmin
    "test connection" endpoint delegates here; this is the only place the decrypted bot token
    is used for it. Returns the validation outcome only -- never the token.
    """
    if platform not in _TESTABLE_PLATFORMS:
        return jsonify({"success": False, "error": "unsupported platform"}), 400
    if not refresh_service:
        return jsonify({"error": "Service not initialized"}), 503
    result = await refresh_service.test_bot_connection(platform)
    if result is None:
        return jsonify({"success": False, "error": f"No credentials found for platform: {platform}"}), 404
    valid, error = result
    return jsonify({"success": True, "data": {"platform": platform, "valid": valid, "error": error}}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.MODULE_PORT)
