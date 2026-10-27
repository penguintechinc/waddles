"""Configuration for svc-streaming.

Env-driven, frozen/slotted dataclass -- same shape `core/svc_presentation/
config.py` established (`_build_db_url` mirrors it verbatim: same
`DB_TYPE`/`DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USER`/`DB_PASS` env vars,
backend-database.md Database Support Matrix). svc-streaming gets its own
`DB_USER`/`DB_PASS` pair in deployment (backend-database.md Per-Service
Database Accounts) even though the Helm chart's shared `envFrom` injects
the same configmap/secret every pipeline-stage container reads today --
provisioning a dedicated scoped account is deployment/infra follow-up, not
a code change here.

This is the CONTROL PLANE + real data-plane orchestrator: it owns its own
`streaming_configs`/`streaming_targets`/`streaming_sessions` tables
(migration 079) and shells out to a real `ffmpeg` binary (`FFMPEG_BINARY`)
to do the actual HLS/RTMP ingest + fan-out-to-N-targets forwarding --
see `services/ffmpeg_engine.py`. This is a deliberate, documented scope
choice vs the design spec's Rust-target / external-MarchProxy-fronting
architecture (`docs/plans/2026-08-31-svc-streaming-design.md` §1, §8.1) --
see this PR's description for the full rationale.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote_plus

from flask_core.secrets import require_secret_key

#: Mirrors hub_api/config.py's own scheme map (backend-database.md Database
#: Support Matrix minus MariaDB Galera).
_DB_URI_SCHEMES: dict[str, str] = {
    "postgresql": "postgres",
    "mysql": "mysql",
    "sqlite": "sqlite",
}


def _build_db_url(
    *,
    db_type: str,
    host: str,
    port: str,
    name: str,
    user: str,
    password: str,
) -> str:
    """Build a pydal-compatible DB URI from DB_TYPE + components (hub_api/config.py twin)."""
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
class Config:
    """Validated, immutable svc-streaming configuration -- built once via `from_env()`."""

    module_name: str
    module_version: str
    module_port: int
    pipeline_stage: str
    log_level: str

    database_url: str
    db_pool_size: int
    #: `bind_streaming_tables(dal, migrate=db_migrate)` -- prod NEVER
    #: auto-migrates (backend-database.md rule 9: schema owned by the
    #: numbered migration file, `079_svc_streaming.sql`); tests set this
    #: True to get real DDL against an ephemeral sqlite file.
    db_migrate: bool

    #: Same `VALKEY_URL`/`REDIS_URL` convention `core/svc_presentation/
    #: config.py` and `core/svc_action/config.py` both read -- required by
    #: `install_rate_limiting(app, ..., redis_url=cfg.valkey_url)` in
    #: `app.py`. Was missing from this dataclass (svc-streaming's own
    #: docstring notes it mirrors svc_presentation's config shape), which
    #: made every `Config.from_env()` call raise `AttributeError` at
    #: `app.py:76` before this fix -- the app never actually started.
    valkey_url: str | None

    hub_api_url: str
    #: Shared HMAC secret used to verify inbound bearer JWTs -- same
    #: env var / default every other Quart service in this repo reads
    #: (`flask_core.auth.verify_jwt_token`, `flask_core.tenancy.
    #: tenant_middleware`), so a token minted by hub-api's auth service
    #: validates identically here.
    jwt_secret_key: str

    #: Real ffmpeg binary this process shells out to -- see
    #: `services/ffmpeg_engine.py`. Overridable so a test/CI image can
    #: point at a stub binary without touching code, though unit tests
    #: never invoke it at all (subprocess creation is mocked).
    ffmpeg_binary: str
    #: Where `record_enabled` streams are segmented to (an emptyDir/PVC
    #: mount in the Helm chart, `k8s/helm/waddlebot/templates/
    #: svc-streaming.yaml`) -- object-store upload of finished segments is
    #: follow-up work (design spec §8.5, S3-compatible object store), out of scope tonight.
    recordings_dir: str

    #: Fixed per-job token cost for admitting a TRANSCODE-enabled forward
    #: (design spec §5: "a token cost function of output profile x
    #: duration x codec" is the eventual model; a flat per-start debit is
    #: the real, minimal admission-check this build implements -- see
    #: `services/token_ledger_client.py`).
    transcode_token_cost: int
    #: `token_products.key` seeded by migration 079 -- the catalog entry
    #: svc-streaming debits against via hub-api's real token ledger
    #: (`hub_api/services/token_billing_service.py`, `blueprints/v1/
    #: token_billing.py`'s `POST .../tokens/debit`).
    transcode_product_key: str

    #: Twitch Helix app-only client-credentials pair, used by
    #: `services/live_channels_service.py` to check live status for a
    #: community's connected Twitch channels (`community_servers`, see
    #: that module's docstring). Empty means "not configured" -- the
    #: associated-channels endpoint degrades to reporting the connection
    #: without a live-status check rather than crashing.
    twitch_client_id: str
    twitch_client_secret: str
    #: YouTube Data API v3 key, same degrade-gracefully posture as Twitch.
    youtube_api_key: str

    @classmethod
    def from_env(cls) -> Config:
        """Build config from the process environment. Raises on an invalid DB_TYPE."""
        db_type = os.getenv("DB_TYPE", "postgresql")
        database_url = os.getenv("DATABASE_URL") or _build_db_url(
            db_type=db_type,
            host=os.getenv("DB_HOST", "infra-postgres"),
            port=os.getenv("DB_PORT", "5432"),
            name=os.getenv("DB_NAME", "waddlebot"),
            user=os.getenv("DB_USER", "svc-streaming-rw"),
            password=os.getenv("DB_PASS", ""),
        )

        return cls(
            module_name=os.getenv("MODULE_NAME", "svc-streaming"),
            module_version=os.getenv("MODULE_VERSION", "0.3.0"),
            module_port=int(os.getenv("MODULE_PORT", "8208")),
            pipeline_stage=os.getenv("PIPELINE_STAGE", "streaming"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            database_url=database_url,
            db_pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
            db_migrate=(
                os.getenv("DB_MIGRATE", "false").strip().lower() in {"1", "true", "yes", "on"}
            ),
            valkey_url=os.getenv("VALKEY_URL") or os.getenv("REDIS_URL") or None,
            hub_api_url=os.getenv("HUB_API_URL", "http://hub-api:8204"),
            jwt_secret_key=require_secret_key(),
            ffmpeg_binary=os.getenv("FFMPEG_BINARY", "ffmpeg"),
            recordings_dir=os.getenv("RECORDINGS_DIR", "/var/lib/svc-streaming/recordings"),
            transcode_token_cost=int(os.getenv("TRANSCODE_TOKEN_COST", "5")),
            transcode_product_key=os.getenv("TRANSCODE_PRODUCT_KEY", "transcoding_minutes"),
            twitch_client_id=os.getenv("TWITCH_CLIENT_ID", ""),
            twitch_client_secret=os.getenv("TWITCH_CLIENT_SECRET", ""),
            youtube_api_key=os.getenv("YOUTUBE_API_KEY", ""),
        )
