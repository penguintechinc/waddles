"""svc-process -- Quart control-plane + background stage-runner loop.

Mirrors `core/svc_ingest/app.py` exactly, one stage over: polls hub-api's
distribution endpoint for the `process` stage's active bundles, RPOPs each
bundle's `:process` Valkey key, runs the bundle's real `transform()`
entrypoint, and LPUSHes the result onto that bundle's `:action` key.

DB: as of the App Bundle bundle-runtime freeze (`docs/
APP_BUNDLE_AUTHORING.md`, 'Accessing the database / shared state'),
svc-process also constructs a `penguin_dal.AsyncDB` here (mirroring
`core/svc_action/app.py`'s DAL construction) and binds it once via
`flask_core.set_bundle_dal()` after calling `await async_dal.reflect()`,
which discovers `live_activity_events` (and every other real table) from
the live schema directly -- so a stateful process bundle can call
`get_bundle_dal()` from inside its own `transform()` body, and `runner.py`'s
best-effort activity emit has a table to write to with no separate
per-table binding step. The frozen `transform(event) -> PlatformEvent | None`
signature itself carries no DAL parameter.
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

from config import Config
from runner import ProcessRunner

app = Quart(__name__)
# security.md A05 hardening -- JSON-only service, default deny-everything CSP.
install_security_headers(app)

health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)


def _jwt_provider() -> str:
    """Mint a fresh 1h service JWT for this runner's tenant scope (see svc-ingest's docstring)."""
    return cast(
        str,
        create_jwt_token(
            user_id="svc-process",
            username="svc-process",
            email="svc-process@internal.waddlebot",
            roles=["service"],
            secret_key=Config.SECRET_KEY,
            tenant=Config.RUNNER_TENANT_SLUG,
            scope=Config.JWT_SCOPE,
            expiration_hours=1,
        ),
    )


@app.before_serving
async def startup() -> None:
    """Wire the httpx client, Valkey client, DAL, poller, and start the background loop."""
    http_client = httpx.AsyncClient()
    redis_client = redis.from_url(Config.VALKEY_URL, encoding="utf-8", decode_responses=True)
    # penguin-dal (D21a): AsyncDB.reflect() discovers the entire live Postgres
    # schema via SQLAlchemy MetaData.reflect() -- this supersedes the old
    # per-table init_live_activity_events_table()/pydal-stub-definition
    # pattern outright; every real table (live_activity_events included) is
    # already present as dal.<table_name> after one reflect() call.
    async_dal = AsyncDB(Config.DATABASE_URL, pool_size=Config.DB_POOL_SIZE)
    await async_dal.reflect()
    set_bundle_dal(async_dal)

    poller = BundlePoller(
        http_client,
        Config.DISTRIBUTION_URL,
        stage=Config.PIPELINE_STAGE,
        jwt_provider=_jwt_provider,
        community_id=Config.RUNNER_COMMUNITY_ID,
        poll_interval_s=Config.POLL_INTERVAL_S,
        base_backoff_s=Config.BASE_BACKOFF_S,
        max_backoff_s=Config.MAX_BACKOFF_S,
    )
    runner = ProcessRunner(
        poller=poller, redis_client=redis_client, tenant_slug=Config.RUNNER_TENANT_SLUG
    )

    app.config["http_client"] = http_client
    app.config["redis_client"] = redis_client
    app.config["async_dal"] = async_dal
    app.config["runner"] = runner
    app.config["runner_task"] = asyncio.ensure_future(runner.run_forever())
    logger.system("svc-process started", action="startup", result="SUCCESS")


@app.after_serving
async def shutdown() -> None:
    """Stop the background loop and close every client -- must never raise."""
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
        # Defensive try/except -- mirrors `core/svc_action/app.py::shutdown`'s
        # own comment: `AsyncDAL.close_async()` runs pydal's `DAL.close()`
        # inside its own ThreadPoolExecutor, on a different thread than the
        # one that created the DAL; a cross-thread close can raise, and
        # failing to release the pool cleanly on shutdown must never crash
        # the ASGI lifespan.
        try:
            await async_dal.close_async()
        except Exception as exc:  # noqa: BLE001 -- shutdown must not raise
            logger.warning(f"Error closing DAL on shutdown: {exc}")
    logger.system("svc-process shutdown complete", action="shutdown", result="SUCCESS")


if __name__ == "__main__":  # pragma: no cover - process entrypoint, not exercised by unit tests
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig

    hyper_config = HyperConfig()
    hyper_config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, hyper_config))
