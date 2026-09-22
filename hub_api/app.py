"""hub-api: Quart application factory + hypercorn entry point.

Task 0.5 skeleton (docs/plans/2026-08-31-hubapi-node-to-quart-migration.md
M0 Foundation) -- the real app skeleton the 55 ported Node controllers
land in, phase by phase. Wires: DAL init (pydal, per `backend-database.md`
-- penguin-dal is the runtime-ops mandate everywhere except this
monorepo, which has standardized on `flask_core`'s pydal wrapper instead,
see mem0 "waddlebot uses pydal directly"), `flask_core.tenancy.
tenant_middleware`-compatible DAL binding, AAA logging, the health
blueprint, the MCP blueprint (`flask_core.mcp_routes` -- its own
docstring names hub-api as exactly this integration point), the two
versioned API routers (`blueprints.register_blueprints`), and the
two-document OpenAPI split (`openapi.routes.register_openapi_docs`).

Rootless-friendly: no filesystem writes outside `LOG_DIR`
(`/var/log/waddlebotlog` by default, overridable), no listen on a
privileged port, runs as `USER appuser` in the Dockerfile.
"""

from __future__ import annotations

import asyncio
from typing import Any

from flask_core import (
    create_health_blueprint,
    init_database,
    install_db_resilience,
    install_security_headers,
    setup_aaa_logging,
)
from flask_core.mcp_routes import create_mcp_blueprint
from pydal import Field
from quart import Quart, request
from quart_schema import Info, QuartSchema

from blueprints import register_blueprints
from config import HubAPIConfig
from openapi.routes import register_openapi_docs
from services.bundle_install_dal import build_install_dal
from services.rate_limiting import install_rate_limiting
from services.schema import (
    bind_ai_routing_tables,
    bind_lifecycle_tables,
    bind_music_tables,
    bind_platform_tables,
    bind_token_billing_tables,
)
from services.session_cookie import bearer_token_from_cookie


def _bind_reference_tables(dal: Any) -> None:
    """Bind read tables hub-api's own auth chain needs -- schema owned elsewhere.

    `migrate=False` always: the `tenants` table's schema is owned by
    `config/postgres/migrations/058_tenants_and_claims.sql`, not by this
    process (backend-database.md: "NO automatic Alembic migrations on
    startup -- manual or K8s Job only"). This binding exists only so
    `flask_core.tenancy.resolve_tenant_context`'s `dal.tenants.slug` /
    `dal.tenants.is_active` field access has a table object to resolve
    against; it never creates or alters the real table.

    `logo_url`/`config` were added here by the M1 Core Identity/Auth
    group -- `auth_service.get_tenant_login_info()` needs them and
    `tenants` can only be `define_table()`-d once per DAL instance.
    Every future group that needs more `tenants` columns extends this
    same call rather than re-defining the table; group-owned tables
    (everything else the M1 group needs) live in
    `services/schema.py::bind_auth_tables()` instead, called below, to
    keep this shared function's diff small for parallel port PRs -- see
    `hub_api/PORTING.md`.

    `bind_platform_tables()` (M3 Platform-admin/Public group) supersedes
    the plain `bind_auth_tables()` call here -- it calls `bind_auth_tables()`
    itself first, then binds its own additional tables
    (`platform_admins`/`collector_modules`/`audit_log`/`coordination`/
    `hub_modules`/`hub_module_reviews`/`hub_module_installations`/
    `platform_integrations`). PORTING.md's checklist step 2 calls for
    every group's `bind_<group>_tables()` to be wired in from exactly this
    function; M3 is the first group after M1 with its own new tables, so
    this is a net-new call site, not a redefinition of M1's own tables.
    """
    dal.define_table(
        "tenants",
        Field("slug", "string", length=100),
        Field("display_name", "string", length=255),
        Field("logo_url", "text"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
        migrate=False,
    )
    bind_platform_tables(dal)
    # App Bundle 3-tier lifecycle (marketplace_lifecycle group) -- append-only
    # per hub_api/PORTING.md's per-group isolation note; see
    # services/schema.py::bind_lifecycle_tables's own docstring.
    bind_lifecycle_tables(dal)
    # Premium-AI model-routing (services/ai_routing/, services/token_ledger.py)
    # -- see services/schema.py::bind_ai_routing_tables() for the full rationale.
    bind_ai_routing_tables(dal)
    # Metered token billing (migration 076, marketplace module) -- this
    # group's own PORTING.md instruction: "one call at END of
    # app.py::_bind_reference_tables", appended after every existing
    # group's own bind_*_tables() call rather than interleaved with them.
    bind_token_billing_tables(dal)
    # Music Station queue feature (new schema, not a Node port) --
    # services/schema.py::bind_music_tables()'s own docstring explains why
    # it follows PORTING.md's normal "call from _bind_reference_tables"
    # checklist step instead of bind_streaming_tables()'s per-request
    # lazy-bind workaround.
    bind_music_tables(dal)


async def bridge_session_cookie_to_bearer() -> None:
    """Synthesize `Authorization: Bearer <token>` from the session cookie.

    security.md C4 fix (`services/session_cookie.py`'s own docstring has
    the full rationale): the browser SPA's JWT now lives in an HttpOnly
    cookie, never `localStorage`, so it can no longer be attached
    client-side as an `Authorization` header. hub-api has roughly a dozen
    independent token-extraction call sites (`flask_core.tenancy.
    tenant_middleware`, `flask_core.authz.require_scope`, `services.
    current_user`, `services.community_authz`, a handful of routes that
    forward the caller's own header verbatim to a downstream proxy) --
    rewriting the incoming request ONCE, here, before any of them run,
    means every one of those call sites keeps reading `request.headers.
    get("Authorization")` completely unchanged, including the ones that
    forward it downstream. An `Authorization` header the client already
    set always wins -- service-to-service JWTs, API clients, and every
    existing test are unaffected.

    Module-level (not a nested closure inside `create_app()`) so
    `tests/test_v1_auth_blueprint.py`'s lightweight `auth_bp`-only app
    fixture can register this exact same function and exercise it against
    real (migrated) tables -- `create_app()`'s own DB binding runs
    `migrate=False` (production: schema owned by SQL migrations, see
    `services/schema.py::bind_auth_tables`), so a `sqlite:memory` app
    built via `create_app()` has no tables to query against in a test.
    """
    if request.headers.get("Authorization"):
        return
    token = bearer_token_from_cookie(request)
    if token:
        request.headers["Authorization"] = f"Bearer {token}"


def create_app(config: HubAPIConfig | None = None) -> Quart:
    """Build the hub-api Quart application.

    Accepts an explicit `config` (tests pass a `sqlite:memory` config to
    avoid a real Postgres dependency); production uses `HubAPIConfig.
    from_env()`, computed once at import time below.
    """
    cfg = config or HubAPIConfig.from_env()
    app = Quart(__name__)
    app.config["HUB_API_CONFIG"] = cfg
    app.secret_key = cfg.secret_key

    # Default quart-schema doc routes are unauthenticated and cover every
    # route -- exactly what security.md's Docs/spec endpoints rule forbids.
    # Disabled here; openapi/routes.py mounts the two-document replacement.
    QuartSchema(
        app,
        openapi_path=None,
        swagger_ui_path=None,
        redoc_ui_path=None,
        scalar_ui_path=None,
        info=Info(title=cfg.module_name, version=cfg.module_version),
    )

    logger = setup_aaa_logging(cfg.module_name, cfg.module_version, log_level=cfg.log_level)
    app.config["logger"] = logger

    # security.md A05 hardening -- global after_request hook, same
    # "one call site, not a per-route decorator" shape as the rate limiter
    # below. hub-api is JSON-only (no HTML ever served), so the default
    # deny-everything CSP (`flask_core.security_headers.DEFAULT_CSP`) is
    # correct with no override.
    install_security_headers(app)

    # Service-wide safety net for the shared raw-pydal DAL (`app.config["dal"]`,
    # bound below in startup()) -- rolls back + logs on any request failure so
    # one bad query can't poison the connection for every request after it.
    # See flask_core.database.install_db_resilience's docstring (#306,
    # recurring InFailedSqlTransaction). Registered before the DAL exists is
    # fine -- the teardown hook reads current_app.config lazily.
    install_db_resilience(app)

    # security.md A04 hardening -- global before_request hook covering
    # every route registered below, not a per-blueprint decorator (see
    # services/rate_limiting.py's own docstring for why). Registered
    # before the blueprints so it's unmistakably "the whole app", not
    # scoped to whichever blueprint happens to be registered first.
    rate_limiter = install_rate_limiting(app, cfg)

    app.register_blueprint(create_health_blueprint(cfg.module_name, cfg.module_version))
    app.register_blueprint(create_mcp_blueprint())
    register_blueprints(app)
    register_openapi_docs(app)

    app.before_request(bridge_session_cookie_to_bearer)

    @app.before_serving
    async def startup() -> None:
        """Initialize the DAL, connect the rate limiter, bind reference tables."""
        logger.system("Starting hub-api", action="startup", extra={"port": cfg.module_port})
        # Connects to Redis/Valkey; falls back to an in-memory limiter
        # (per-process, non-distributed) if unreachable -- see
        # flask_core.rate_limiter.RateLimiter.connect()'s own fail-open
        # doc comment. Never blocks startup on Redis being down.
        await rate_limiter.connect()
        async_dal = init_database(
            cfg.database_url,
            pool_size=cfg.db_pool_size,
            read_replica_uri=cfg.database_read_replica_url,
        )
        # tenancy.py / mcp_routes.py call `dal(query)` directly (pydal's
        # callable-DAL query syntax) -- AsyncDAL has no __call__, only
        # __getattr__ passthrough, so the auth chain needs the *raw* pydal
        # DAL, not the AsyncDAL wrapper. Keep both: raw for the auth chain,
        # the wrapper for any future async-offloaded query helpers.
        dal = async_dal.dal
        _bind_reference_tables(dal)
        app.config["async_dal"] = async_dal
        app.config["dal"] = dal
        # penguin-dal (R52): a SEPARATE AsyncDB/pool against the same
        # DATABASE_URL, reflecting the ingest_sources/workstreams/
        # workstream_usage_hourly tables migrations 0020-0021 create
        # (spec Sec5.11/Sec5.12, D30/D31) -- see bundle_install_dal.py's
        # own docstring for why this coexists with the pydal DAL above
        # rather than replacing it.
        install_dal = await build_install_dal(cfg.database_url, pool_size=cfg.db_pool_size)
        app.config["install_dal"] = install_dal
        logger.system("hub-api started", action="startup", result="SUCCESS")

    @app.after_serving
    async def shutdown() -> None:
        """Close the DAL connection pool.

        Defensive try/except around the close, matching
        `services/core-community/app.py`'s established shutdown pattern:
        `AsyncDAL.close_async()` runs pydal's `DAL.close()` inside its
        ThreadPoolExecutor, on a different thread than the one that
        created the DAL -- pydal's `close()` reads `THREAD_LOCAL.
        _pydal_db_instances_`, which is only ever populated on the
        *creating* thread, so a cross-thread close raises `AttributeError`
        (a flask_core bug, not this service's -- out of scope to patch a
        shared lib from this PR). Failing to release the pool cleanly on
        shutdown must never crash the ASGI lifespan.
        """
        async_dal = app.config.get("async_dal")
        if async_dal is not None:
            try:
                await async_dal.close_async()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.warning(f"Error closing DAL on shutdown: {exc}")
        install_dal = app.config.get("install_dal")
        if install_dal is not None:
            try:
                await install_dal.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.warning(f"Error closing install_dal on shutdown: {exc}")
        try:
            await rate_limiter.disconnect()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning(f"Error closing rate limiter on shutdown: {exc}")
        logger.system("hub-api shutdown complete", action="shutdown", result="SUCCESS")

    return app


app = create_app()


if __name__ == "__main__":
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig

    hyper_config = HyperConfig()
    hyper_config.bind = [f"0.0.0.0:{app.config['HUB_API_CONFIG'].module_port}"]
    asyncio.run(hypercorn.asyncio.serve(app, hyper_config))
