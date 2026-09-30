"""hub-api service configuration.

Env-driven configuration for the hub-api control-plane service (Task 0.5
skeleton -- docs/plans/2026-08-31-hubapi-node-to-quart-migration.md M0).
Every value is read from the environment (or an env var backed by a
secrets manager in real deployments); nothing here is a hardcoded
credential (security.md Secrets & Credentials). A frozen, slotted
dataclass rather than a mutable class-with-class-attrs (the pattern
several sibling `services/*/config.py` still use) -- see
`penguin-python-dev`'s Stack Decisions: `@dataclass(slots=True)` for
every data structure, `frozen=True` for a value object built once at
startup and never mutated.

Two field names deliberately mirror `flask_core` verbatim rather than
inventing a hub-api-local name: `secret_key` reads `SECRET_KEY` (not
`JWT_SECRET_KEY`) because `flask_core.tenancy.tenant_middleware` and
`flask_core.authz.require_scope` both call `flask_core.secrets.
require_secret_key()` directly at request time -- a differently-named env
var here would leave those decorators reading a different (or unset)
value than `HubAPIConfig.secret_key`. `require_secret_key()` fails closed
(raises `InsecureSecretError`) rather than silently falling back to the
`"change-me-in-production"` placeholder when running in a production-like
environment (C1, security audit) -- see `flask_core.secrets` module docs.
Likewise `posthog_api_key`/`posthog_host` mirror `flask_core.entitlement`'s
own `POSTHOG_API_KEY`/`POSTHOG_HOST` lookups. Config.from_env() exists so
hub-api's own code (logging, health blueprint naming, OpenAPI info) has
one place to read these from, while flask_core's decorators keep reading
the environment directly -- documented here rather than re-plumbed
through app.config to avoid a second, driftable source of truth.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote_plus

from flask_core.secrets import require_secret_key

#: DB_TYPE values this config understands (backend-database.md Database
#: Support Matrix minus MariaDB Galera, which is not yet in hub-api's plan).
_DB_URI_SCHEMES: dict[str, str] = {
    "postgresql": "postgres",  # pydal wants postgres://, not postgresql://
    "mysql": "mysql",
    "sqlite": "sqlite",
}


def _bool_env(name: str, default: bool) -> bool:
    """Parse a boolean env var; anything not in the truthy set is False."""
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _build_db_url(
    *,
    db_type: str,
    host: str,
    port: str,
    name: str,
    user: str,
    password: str,
) -> str:
    """Build a pydal-compatible DB URI from DB_TYPE + components.

    backend-database.md's canonical env vars (DB_TYPE, DB_HOST, DB_PORT,
    DB_NAME, DB_USER, DB_PASS) in, one connection string out. sqlite
    ignores host/port/user/password -- `name` becomes the file path
    (or `:memory:`/`memory` for ephemeral/test use, matching
    libs/flask_core's own test fixtures, e.g. tests/test_tenancy.py).
    """
    scheme = _DB_URI_SCHEMES.get(db_type)
    if scheme is None:
        raise ValueError(
            f"unsupported DB_TYPE {db_type!r} -- expected one of {sorted(_DB_URI_SCHEMES)}"
        )
    if scheme == "sqlite":
        return f"sqlite:{name}"
    if password:
        return f"{scheme}://{user}:{quote_plus(password)}@{host}:{port}/{name}"
    return f"{scheme}://{user}@{host}:{port}/{name}"


@dataclass(slots=True, frozen=True)
class HubAPIConfig:
    """Validated, immutable hub-api configuration -- built once via `from_env()`."""

    module_name: str
    module_version: str
    module_port: int
    grpc_port: int

    database_url: str
    database_read_replica_url: str | None
    db_pool_size: int
    db_max_retries: int
    db_retry_delay: int

    # Mirrors flask_core's own SECRET_KEY / HS256 lookup -- see module docstring.
    secret_key: str
    jwt_algorithm: str
    default_tenant_slug: str

    # flask_core.entitlement reads POSTHOG_API_KEY/POSTHOG_HOST directly;
    # kept here too so hub-api's own startup logging can report what's wired.
    posthog_api_key: str | None
    posthog_host: str
    license_server_url: str

    # OAuth redirect wiring (authController.js's `config.identity.
    # callbackBaseUrl` / `config.cors.origin`) -- callback_base_url is
    # hub-api's OWN externally-reachable base URL (OAuth providers redirect
    # back to it), frontend_origin is where post-login redirects land.
    identity_callback_base_url: str
    frontend_origin: str

    log_level: str

    # `cookieConsentController.js`/`cookieConsentService.js` fall back to
    # `process.env.COOKIE_CONSENT_VERSION || '1.0.0'` at every call site
    # (Privacy/Compliance group) -- one field here so hub-api reads it
    # once at startup rather than re-reading the env var per request.
    # Defaulted (unlike every field above it) so this group's addition
    # doesn't break the positional/keyword `HubAPIConfig(...)` construction
    # every other group's test file already does -- dataclass field-
    # ordering rules only require defaults on fields declared AFTER this
    # one, and there are none.
    cookie_consent_version: str = "1.0.0"

    # M7 Streaming group -- mirrors `overlayController.js`'s own
    # `OVERLAY_BASE_URL` env var + default; the externally-reachable base
    # the browser-source overlay URL is built against
    # (`{overlay_base_url}/{overlay_key}`), never hub-api's own host.
    # Defaulted (unlike every field above) so the three pre-existing test
    # files that construct `HubAPIConfig(...)` explicitly, field-by-field,
    # don't all need editing for a field their own blueprints never read --
    # dataclass field-ordering requires a defaulted field to trail every
    # non-defaulted one, hence its position here rather than grouped with
    # `identity_callback_base_url`/`frontend_origin` above.
    overlay_base_url: str = "https://overlay.waddlebot.io"

    # M4 Marketplace Billing group -- Stripe/PayPal credentials, mirroring
    # marketplace_module's own `STRIPE_SECRET_KEY`/`STRIPE_WEBHOOK_SECRET`/
    # `PAYPAL_CLIENT_ID`/`PAYPAL_CLIENT_SECRET`/`PAYPAL_WEBHOOK_ID`/
    # `PAYPAL_MODE` env vars byte-for-byte (`admin/marketplace_module/
    # backend/src/config/index.js` had no typed config object for these --
    # `paymentService.js`/`stripeService.js`/`paypalService.js` all read
    # `process.env.*` ad hoc). Defaulted to empty/sandbox rather than
    # raising at import time: `blueprints/v1/marketplace_webhooks.py` fails
    # closed on an empty `stripe_webhook_secret`/`paypal_webhook_id` (every
    # signature check requires a non-empty secret to even attempt a
    # comparison), so an unconfigured deployment safely rejects every
    # webhook rather than crashing hub-api's whole app factory.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_webhook_id: str = ""
    paypal_mode: str = "sandbox"

    # `WADDLES_AI_ENABLED` -- deploy-time kill-switch for the whole
    # `services/ai_routing/` subsystem (`blueprints/v1/ai_routing.py`'s two
    # blueprints, `services/ai_routing/router.py::route_completion()`), so
    # hub-api runs on a non-beefy machine with no Ollama/model backend
    # reachable at all. ONE-WAY: this flag can only ever turn AI OFF --
    # `router.py`'s existing `waddles.ai.routing`/`waddles.ai.premium_models`/
    # `waddles.ai.byok` PostHog-flag + Enterprise-license-tier gates are
    # completely unaffected and still govern who gets access when this is
    # left at its default `True` (current full-feature behavior, unchanged).
    # Defaulted last for the same dataclass field-ordering reason as every
    # other trailing field in this class.
    ai_enabled: bool = True

    # security.md A04 hardening (rate-limit gap fix) -- `services/
    # rate_limiting.py`'s global `before_request` hook reads these, not
    # ad hoc `os.getenv()` calls scattered per-blueprint. `valkey_url`
    # mirrors `core/svc_action/config.py` / `core/svc_presentation/
    # config.py`'s own fallback chain byte-for-byte: `VALKEY_URL` is the
    # primary env var, `REDIS_URL` (the Helm chart's shared secret key --
    # see `k8s/helm/waddlebot/templates/secrets.yaml`, "44 modules read
    # REDIS_URL") is the fallback so this boots against the real cluster
    # secret with zero Helm changes.
    #
    # `RATE_LIMIT_MAX_REQUESTS`/`RATE_LIMIT_WINDOW_MS` are the existing
    # Helm-wired env var names (`values-beta.yaml`, alpha/beta
    # `kustomization.yaml` overlays) -- reused verbatim rather than
    # renamed, since this fix is what makes the app actually read them.
    # Default 100 req/60s matches `values-beta.yaml`'s own comment
    # ("300 = 3x default").
    #
    # `rate_limit_auth_*` is a stricter, separate tier for the
    # brute-force-sensitive auth surface (login/token/passkey --
    # `services/rate_limiting.py::_AUTH_TIER_PREFIXES`), deliberately not
    # sharing the standard tier's env vars: one shared limit would mean
    # either the standard tier is too strict for normal browsing or the
    # auth tier is too loose for brute-force protection.
    valkey_url: str = "redis://localhost:6379/0"
    rate_limit_max_requests: int = 100
    rate_limit_window_seconds: int = 60
    rate_limit_auth_max_requests: int = 10
    rate_limit_auth_window_seconds: int = 60

    # `services/rate_limiting.py::_client_ip()`'s trust boundary for
    # `X-Forwarded-For` -- 0 (default, fail-closed) means don't trust the
    # header at all, bucket on `request.remote_addr` only. `X-Forwarded-
    # For` is client-suppliable in the general case (a raw client hitting
    # hub-api directly, or any hop that doesn't strictly append-only, can
    # put anything in it); trusting an arbitrary hop unconditionally lets
    # a caller pick their own rate-limit bucket, bypassing the auth-tier
    # brute-force limit by rotating the header. >0 means hub-api sits
    # behind exactly that many trusted proxies (each known to append
    # rather than blindly relay) -- operators set this to match their
    # actual ingress chain; it is never inferred automatically.
    trusted_proxy_hops: int = 0

    # security.md Input Validation -- server-side upper bound on every
    # client-suppliable `?limit=` page-size param. Read by
    # `services/pagination.py::parse_limit()`, the shared helper every
    # previously-unbounded list endpoint's `?limit=` parsing now routes
    # through.
    api_max_page_size: int = 100

    # gh-320 Community Connections (per-community OAuth). `public_webui_url`
    # is the browser-facing webui origin used for user-visible links (e.g.
    # chat-embedded URLs) -- often a LAN IP in alpha, never guaranteed to be
    # an OAuth-provider-acceptable redirect host.
    #
    # `connections_callback_base_url` is a SEPARATE, independently
    # overridable base for building the OAuth `redirect_uri` (`blueprints/
    # v1/community_connections.py`'s authorize route) and the `callback_base`
    # value in the connections-list response. Defaults to
    # `public_webui_url` (`from_env()`) since most deployments want the
    # same origin for both, but alpha needs to diverge: Google/Spotify (and
    # most OAuth providers) only accept an `http://` redirect URI on
    # `localhost`/`127.0.0.1`, never an arbitrary LAN IP, so alpha sets
    # `CONNECTIONS_CALLBACK_BASE_URL=http://localhost:<port>` independently
    # of `PUBLIC_WEBUI_URL` (still the LAN IP, for chat links) rather than
    # forcing one shared value to satisfy two incompatible constraints.
    public_webui_url: str = "http://localhost:30879"
    connections_callback_base_url: str = "http://localhost:30879"
    connections_state_ttl_s: int = 600

    # feature/hub-api-internal-grpc: gate for `grpc_internal/server.py`'s
    # `waddles.hub.internal.v1` listener. `grpc_enabled=False` (or a
    # missing TLS cert / empty identity allow-list at startup, handled in
    # `app.py`) means the gRPC surface simply never starts -- fail-closed
    # for the gRPC surface, fail-open for the rest of hub-api's HTTP app.
    # `deployment_env` feeds `flask_core.service_jwt.load_identities_from_
    # env()`'s bare-service-name -> SPIFFE-ID expansion
    # (`spiffe://penguintech.io/<deployment_env>/<service>`, penguintech.md
    # SPIFFE Identity) -- mirrors the `alpha`/`beta`/`gamma`/`v{X}` env
    # naming `penguintech.md` Domains & TLDs already uses.
    grpc_enabled: bool = True
    deployment_env: str = "alpha"

    @classmethod
    def from_env(cls) -> HubAPIConfig:
        """Build config from the process environment. Raises on an invalid DB_TYPE."""
        db_type = os.getenv("DB_TYPE", "postgresql")
        database_url = os.getenv("DATABASE_URL") or _build_db_url(
            db_type=db_type,
            host=os.getenv("DB_HOST", "infra-postgres"),
            port=os.getenv("DB_PORT", "5432"),
            name=os.getenv("DB_NAME", "waddlebot"),
            user=os.getenv("DB_USER", "hub-api-rw"),
            password=os.getenv("DB_PASS", ""),
        )

        read_replica_url = os.getenv("DATABASE_READ_REPLICA_URL")
        if not read_replica_url and os.getenv("DB_READ_REPLICA_HOST"):
            read_replica_url = _build_db_url(
                db_type=db_type,
                host=os.environ["DB_READ_REPLICA_HOST"],
                port=os.getenv("DB_READ_REPLICA_PORT", os.getenv("DB_PORT", "5432")),
                name=os.getenv("DB_NAME", "waddlebot"),
                user=os.getenv("DB_READ_REPLICA_USER", os.getenv("DB_USER", "hub-api-ro")),
                password=os.getenv("DB_READ_REPLICA_PASS", os.getenv("DB_PASS", "")),
            )

        return cls(
            module_name=os.getenv("MODULE_NAME", "hub-api"),
            module_version=os.getenv("MODULE_VERSION", "0.1.0"),
            module_port=int(os.getenv("MODULE_PORT", "8204")),
            grpc_port=int(os.getenv("GRPC_PORT", "50204")),
            database_url=database_url,
            database_read_replica_url=read_replica_url,
            db_pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
            db_max_retries=int(os.getenv("DB_MAX_RETRIES", "5")),
            db_retry_delay=int(os.getenv("DB_RETRY_DELAY", "5")),
            secret_key=require_secret_key(),
            jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
            default_tenant_slug=os.getenv("DEFAULT_TENANT_SLUG", "global"),
            posthog_api_key=os.getenv("POSTHOG_API_KEY") or os.getenv("POSTHOG_KEY"),
            posthog_host=os.getenv("POSTHOG_HOST", "https://license.penguintech.io"),
            license_server_url=os.getenv("LICENSE_SERVER_URL", "https://license.penguintech.io"),
            identity_callback_base_url=os.getenv(
                "IDENTITY_CALLBACK_BASE_URL", "http://localhost:8204"
            ),
            frontend_origin=os.getenv("FRONTEND_ORIGIN", "http://localhost:5173"),
            cookie_consent_version=os.getenv("COOKIE_CONSENT_VERSION", "1.0.0"),
            overlay_base_url=os.getenv("OVERLAY_BASE_URL", "https://overlay.waddlebot.io"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            stripe_secret_key=os.getenv("STRIPE_SECRET_KEY", ""),
            stripe_webhook_secret=os.getenv("STRIPE_WEBHOOK_SECRET", ""),
            paypal_client_id=os.getenv("PAYPAL_CLIENT_ID", ""),
            paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET", ""),
            paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", ""),
            paypal_mode=os.getenv("PAYPAL_MODE", "sandbox"),
            ai_enabled=_bool_env("WADDLES_AI_ENABLED", True),
            valkey_url=os.getenv("VALKEY_URL")
            or os.getenv("REDIS_URL")
            or "redis://localhost:6379/0",
            rate_limit_max_requests=int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "100")),
            rate_limit_window_seconds=max(
                1, int(os.getenv("RATE_LIMIT_WINDOW_MS", "60000")) // 1000
            ),
            rate_limit_auth_max_requests=int(os.getenv("RATE_LIMIT_AUTH_MAX_REQUESTS", "10")),
            rate_limit_auth_window_seconds=max(
                1, int(os.getenv("RATE_LIMIT_AUTH_WINDOW_MS", "60000")) // 1000
            ),
            trusted_proxy_hops=max(0, int(os.getenv("TRUSTED_PROXY_HOPS", "0"))),
            api_max_page_size=int(os.getenv("API_MAX_PAGE_SIZE", "100")),
            public_webui_url=os.getenv("PUBLIC_WEBUI_URL", "http://localhost:30879").rstrip("/"),
            connections_callback_base_url=(
                os.getenv("CONNECTIONS_CALLBACK_BASE_URL", "")
                or os.getenv("PUBLIC_WEBUI_URL", "http://localhost:30879")
            ).rstrip("/"),
            connections_state_ttl_s=int(os.getenv("CONNECTIONS_STATE_TTL_S", "600")),
            grpc_enabled=_bool_env("GRPC_INTERNAL_ENABLED", True),
            deployment_env=os.getenv("DEPLOYMENT_ENV", "alpha"),
        )
