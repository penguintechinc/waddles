"""svc-action service configuration.

Mirrors `core/svc_process/config.py`/`core/svc_ingest/config.py`'s poller
wiring exactly (`HUB_API_URL`/`DISTRIBUTION_URL`/`POLL_INTERVAL_S`/
`RUNNER_TENANT_SLUG`/`RUNNER_COMMUNITY_ID`/`SECRET_KEY`/`JWT_SCOPE`) -- one
uniform stage-runner config shape across ingest/process/action, per the
App Bundle SDK bundle-runtime proof (docs/plans/2026-08-31-app-bundle-sdk-
design.md). Two genuine differences from ingest/process's config: (1)
svc-action keeps a DB connection (`DATABASE_URL`/`DB_POOL_SIZE`) for
`action_dispatch_log` audit writes -- ingest/process have no audit table,
they either enqueue or drop; (2) svc-action keeps retry-with-backoff
config (`ACTION_MAX_RETRIES`/`ACTION_RETRY_INITIAL_DELAY`/
`ACTION_RETRY_MAX_DELAY`) -- action dispatches to a real external system
(a real HTTP call, IRC connection, etc.) where a transient failure is
worth retrying, unlike ingest/process's pure in-memory transform (nothing
to retry -- a bad event is just dropped and logged, `runner.py`'s own
docstring).

No central `presentation_base_url`/`smtp_*`/transport-specific settings
here -- `services/transports/` primitives take those as direct kwargs
(`services/transports/__init__.py::dispatch_transport`'s `settings`
param), sourced from a bundle's own `stages.action.config`
(`app_catalog`, migration 071/082) rather than a service-wide object
every transport implicitly depends on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote_plus

from flask_core.secrets import require_secret_key

#: DB_TYPE values understood -- backend-database.md Database Support Matrix
#: minus MariaDB Galera (not needed for this service's narrow audit-log use).
#: Both "postgresql" (backend-database.md's documented DB_TYPE convention)
#: and "postgres" (the value this service's own configmap actually ships,
#: k8s/helm/waddlebot/templates/configmap.yaml -- not owned by this
#: service, left as-is) are accepted so the component-built fallback URL
#: never regresses on either spelling; pydal itself only recognizes the
#: "postgres://" URI scheme.
_DB_URI_SCHEMES: dict[str, str] = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "mysql": "mysql",
    "sqlite": "sqlite",
}


def _optional_int(value: str | None) -> int | None:
    return int(value) if value not in (None, "") else None


def _normalize_pydal_scheme(url: str) -> str:
    """Rewrite a ``postgresql://`` (or ``postgresql+driver://``) URI to pydal's ``postgres://``.

    pydal's adapter registry keys off the URI scheme literally and only
    registers ``postgres``, never the also-valid ``postgresql`` -- SQLAlchemy's
    (and this repo's shared `DATABASE_URL` secret's) required scheme. A
    directly-supplied `DATABASE_URL` bypasses `_build_db_url`'s own
    `_DB_URI_SCHEMES` translation entirely (`from_env` only calls
    `_build_db_url` as a fallback when `DATABASE_URL` is unset), so this is
    the single chokepoint applied to *both* paths below -- no future code
    path can construct a `database_url` without going through it. Without
    it, pydal raises `SyntaxError: Adapter not found for postgresql` at
    `DAL()` construction, surfacing as a Quart lifespan startup failure.
    """
    if url.startswith("postgresql://") or url.startswith("postgresql+"):
        return "postgres://" + url.split("://", 1)[1]
    return url


def _build_db_url(
    *, db_type: str, host: str, port: str, name: str, user: str, password: str
) -> str:
    """Build a pydal-compatible DB URI from DB_TYPE + components."""
    scheme = _DB_URI_SCHEMES.get(db_type)
    if scheme is None:
        raise ValueError(
            f"unsupported DB_TYPE {db_type!r} -- expected one of {sorted(_DB_URI_SCHEMES)}"
        )
    if scheme == "sqlite":
        return f"sqlite:{name}:"
    if password:
        return f"{scheme}://{user}:{quote_plus(password)}@{host}:{port}/{name}"
    return f"{scheme}://{user}@{host}:{port}/{name}"


@dataclass(slots=True, frozen=True)
class ActionConfig:
    """Validated, immutable svc-action configuration -- built once via `from_env()`."""

    module_name: str
    module_version: str
    module_port: int
    pipeline_stage: str
    log_level: str

    # Distribution API (hub_api/blueprints/v1/distribution.py) poll wiring
    # -- flask_core.stage_runner.BundlePoller, same shape svc-ingest/
    # svc-process use.
    hub_api_url: str
    distribution_url: str
    poll_interval_s: float
    base_backoff_s: float
    max_backoff_s: float

    # This runner instance's own tenant/community scope -- security.md
    # Tenant Isolation: never widened at request time, fixed at deploy
    # time, matching the JWT `tenant` claim this runner mints for itself.
    runner_tenant_slug: str
    runner_community_id: int | None

    # Shared HS256 secret -- mirrors flask_core.tenancy/authz's own
    # os.getenv("SECRET_KEY", ...) fallback exactly, so a token minted
    # here verifies against hub-api's own decorators.
    secret_key: str
    jwt_scope: str

    # Valkey (process -> action queue; each active bundle's own
    # `:action` key, flask_core.stream_pipeline.bundle_stream_key).
    valkey_url: str

    # DB account -- action_dispatch_log audit writes only (svc-action
    # never reads app_catalog/app_activations/app_tenant_availability
    # directly anymore; the poller resolves bundle config over HTTP).
    database_url: str
    db_pool_size: int

    # Shared httpx client timeout (distribution poll + every transport's
    # outbound call) and per-envelope retry-with-backoff.
    http_timeout_seconds: float
    max_retries: int
    retry_initial_delay: float
    retry_max_delay: float

    @classmethod
    def from_env(cls) -> ActionConfig:
        """Build config from the process environment. Raises on an invalid DB_TYPE."""
        db_type = os.getenv("DB_TYPE", "postgresql")
        database_url = _normalize_pydal_scheme(
            os.getenv("DATABASE_URL")
            or _build_db_url(
                db_type=db_type,
                host=os.getenv("DB_HOST", "infra-postgres"),
                port=os.getenv("DB_PORT", "5432"),
                name=os.getenv("DB_NAME", "waddlebot"),
                user=os.getenv("DB_USER", "svc-action-rw"),
                password=os.getenv("DB_PASS", ""),
            )
        )
        valkey_url = os.getenv("VALKEY_URL") or os.getenv("REDIS_URL") or "redis://localhost:6379/0"
        hub_api_url = os.getenv("HUB_API_URL", "http://hub-api:8204")

        return cls(
            module_name=os.getenv("MODULE_NAME", "svc-action"),
            module_version=os.getenv("MODULE_VERSION", "0.1.0"),
            module_port=int(os.getenv("MODULE_PORT", "8202")),
            pipeline_stage="action",
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            hub_api_url=hub_api_url,
            distribution_url=os.getenv(
                "DISTRIBUTION_URL", f"{hub_api_url}/api/v1/distribution/bundles"
            ),
            poll_interval_s=float(os.getenv("POLL_INTERVAL_S", "5.0")),
            base_backoff_s=float(os.getenv("BASE_BACKOFF_S", "1.0")),
            max_backoff_s=float(os.getenv("MAX_BACKOFF_S", "60.0")),
            runner_tenant_slug=os.getenv("RUNNER_TENANT_SLUG", "global"),
            runner_community_id=_optional_int(os.getenv("RUNNER_COMMUNITY_ID")),
            secret_key=require_secret_key(),
            jwt_scope="distribution:read",
            valkey_url=valkey_url,
            database_url=database_url,
            db_pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
            http_timeout_seconds=float(os.getenv("ACTION_HTTP_TIMEOUT_SECONDS", "10")),
            max_retries=int(os.getenv("ACTION_MAX_RETRIES", "3")),
            retry_initial_delay=float(os.getenv("ACTION_RETRY_INITIAL_DELAY", "1.0")),
            retry_max_delay=float(os.getenv("ACTION_RETRY_MAX_DELAY", "30.0")),
        )
