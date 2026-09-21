"""svc-action -- Quart control-plane + background stage-runner loop.

Mirrors `core/svc_process/app.py`/`core/svc_ingest/app.py` exactly: polls
hub-api's distribution endpoint for the `action` stage's active bundles
(`flask_core.stage_runner.BundlePoller`), RPOPs each bundle's own
`:action` Valkey key, loads its real script entrypoint (`flask_core.
stage_runner.load_entrypoint`), and dispatches -- see `runner.py`'s own
docstring for the one real difference from ingest/process (retry-with-
backoff + an audit-log DB connection, since action dispatches to a real
external system). `/health`/`/healthz`/`/metrics` come from `flask_core`'s
standard health blueprint, same as every other pipeline-stage container.
"""

from __future__ import annotations

import asyncio
from typing import cast

import httpx
import redis.asyncio as redis
from flask_core import (
    create_health_blueprint,
    install_security_headers,
    set_bundle_dal,
    setup_aaa_logging,
)
from flask_core.auth import create_jwt_token
from flask_core.stage_runner import BundlePoller
from penguin_dal import AsyncDB
from quart import Quart

from config import ActionConfig
from runner import ActionRunner

app = Quart(__name__)
# security.md A05 hardening -- JSON-only service, default deny-everything CSP.
install_security_headers(app)

_config = ActionConfig.from_env()

health_bp = create_health_blueprint(_config.module_name, _config.module_version)
app.register_blueprint(health_bp)

logger = setup_aaa_logging(_config.module_name, _config.module_version)


def _jwt_provider() -> str:
    """Mint a fresh 1h service JWT for this runner's own tenant scope (see svc-ingest's own)."""
    return cast(
        str,
        create_jwt_token(
            user_id="svc-action",
            username="svc-action",
            email="svc-action@internal.waddlebot",
            roles=["service"],
            secret_key=_config.secret_key,
            tenant=_config.runner_tenant_slug,
            scope=_config.jwt_scope,
            expiration_hours=1,
        ),
    )


@app.before_serving
async def startup() -> None:
    """Wire the httpx/Valkey/DAL clients, poller, and start the background loop.

    `http2=True` on the shared httpx client -- the `http` transport's
    `grpc` sub-type (`services/transports/http.py`) needs real HTTP/2
    negotiation; every other caller of this client (the distribution poll,
    every other transport, `bundles/discord_send_action.py`) works
    identically over HTTP/1.1 or /2, so sharing one client is safe.
    """
    http_client = httpx.AsyncClient(
        follow_redirects=False, http2=True, timeout=_config.http_timeout_seconds
    )
    redis_client = redis.from_url(_config.valkey_url, encoding="utf-8", decode_responses=True)
    # penguin-dal (D21a): reflect() discovers the whole live schema, so the
    # old bind_minimal_reference_tables() pydal-stub pattern (tenants/
    # communities/app_catalog/action_dispatch_log) is redundant -- delete it.
    async_dal = AsyncDB(_config.database_url, pool_size=_config.db_pool_size)
    await async_dal.reflect()
    # Bind for `get_bundle_dal()` -- an action bundle needing DB access
    # beyond the audit log (e.g. fetching a stored quote) reaches this same
    # DAL from inside its own entrypoint body. See docs/
    # APP_BUNDLE_AUTHORING.md, 'Accessing the database / shared state'.
    set_bundle_dal(async_dal)

    poller = BundlePoller(
        http_client,
        _config.distribution_url,
        stage=_config.pipeline_stage,
        jwt_provider=_jwt_provider,
        community_id=_config.runner_community_id,
        poll_interval_s=_config.poll_interval_s,
        base_backoff_s=_config.base_backoff_s,
        max_backoff_s=_config.max_backoff_s,
    )
    runner = ActionRunner(
        poller=poller,
        redis_client=redis_client,
        dal=async_dal,
        http_client=http_client,
        tenant_slug=_config.runner_tenant_slug,
        max_retries=_config.max_retries,
        retry_initial_delay=_config.retry_initial_delay,
        retry_max_delay=_config.retry_max_delay,
    )

    app.config["http_client"] = http_client
    app.config["redis_client"] = redis_client
    app.config["async_dal"] = async_dal
    app.config["runner"] = runner
    app.config["runner_task"] = asyncio.ensure_future(runner.run_forever())
    logger.system("svc-action started", action="startup", result="SUCCESS")


@app.after_serving
async def shutdown() -> None:
    """Stop the background loop and close all clients -- must never raise."""
    runner = app.config.get("runner")
    if runner is not None:
        runner.stop()
    task = app.config.get("runner_task")
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # expected -- this is our own cancel() above, not a failure
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning(f"Error stopping runner task: {exc}")

    http_client = app.config.get("http_client")
    if http_client is not None:
        await http_client.aclose()
    redis_client = app.config.get("redis_client")
    if redis_client is not None:
        await redis_client.aclose()
    async_dal = app.config.get("async_dal")
    if async_dal is not None:
        # Defensive try/except -- flask_core's AsyncDAL.close_async() runs
        # pydal's DAL.close() inside its own ThreadPoolExecutor, on a
        # different thread than the one that created the DAL; pydal's
        # close() reads THREAD_LOCAL state only ever populated on the
        # *creating* thread, so a cross-thread close can raise. Failing to
        # release the pool cleanly on shutdown must never crash the ASGI
        # lifespan (same pattern the pre-refactor runner used).
        try:
            await async_dal.close_async()
        except Exception as exc:  # noqa: BLE001 -- shutdown must not raise
            logger.warning(f"Error closing DAL on shutdown: {exc}")
    logger.system("svc-action shutdown complete", action="shutdown", result="SUCCESS")


if __name__ == "__main__":  # pragma: no cover - process entrypoint, not exercised by unit tests
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig

    hyper_config = HyperConfig()
    hyper_config.bind = [f"0.0.0.0:{_config.module_port}"]
    asyncio.run(hypercorn.asyncio.serve(app, hyper_config))
