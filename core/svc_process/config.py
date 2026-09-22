"""Configuration for svc-process. Mirrors `core/svc_ingest/config.py` -- see its docstring.

DB config (`DATABASE_URL`/`DB_POOL_SIZE` below) is new as of the App Bundle
bundle-runtime freeze (`docs/APP_BUNDLE_AUTHORING.md`, 'Accessing the
database / shared state') -- svc-process previously had no DAL at all (a
pure-transform stage). Mirrors `core/svc_action/config.py`'s DB env
handling exactly (`_DB_URI_SCHEMES`/`_normalize_pydal_scheme`/
`_build_db_url`, same env var names), so both stage runners build their
`DATABASE_URL` identically -- see that module's docstring for the
pydal-scheme-normalization rationale.

Per-service DB account (backend.md Database Tier Architecture): svc-process
gets its own scoped grant, distinct from svc-action's -- `DB_USER` here
defaults to `svc-process-rw`, not `svc-action-rw`. For alpha this reuses
svc-action's established `DATABASE_URL`/`DB_HOST`/`DB_PORT`/`DB_NAME`/
`DB_USER`/`DB_PASS`/`DB_TYPE` env var *pattern* against the same Helm
secret shape; provisioning the actual distinct grant (a real
`svc-process-rw` Postgres role with its own narrower privileges) is
ops/migration work, tracked separately, not part of this change.
"""

from __future__ import annotations

import os
from urllib.parse import quote_plus

from dotenv import load_dotenv
from flask_core.secrets import require_secret_key

load_dotenv()


def _optional_int(value: str | None) -> int | None:
    return int(value) if value not in (None, "") else None


#: DB_TYPE values understood -- mirrors `core/svc_action/config.py`'s own
#: `_DB_URI_SCHEMES` exactly (see that module's docstring for why both
#: "postgresql" and "postgres" are accepted).
_DB_URI_SCHEMES: dict[str, str] = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "mysql": "mysql",
    "sqlite": "sqlite",
}


def _normalize_pydal_scheme(url: str) -> str:
    """Rewrite a ``postgresql://`` (or ``postgresql+driver://``) URI to pydal's ``postgres://``.

    See `core/svc_action/config.py::_normalize_pydal_scheme` -- identical
    chokepoint, applied to both the direct-`DATABASE_URL` and
    component-built paths below.
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


class Config:
    """Runtime configuration for the svc-process stage-runner."""

    MODULE_NAME = os.getenv("MODULE_NAME", "svc-process")
    MODULE_VERSION = "0.1.0"
    MODULE_PORT = int(os.getenv("MODULE_PORT", "8211"))
    PIPELINE_STAGE = "process"
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    HUB_API_URL = os.getenv("HUB_API_URL", "http://hub-api:8204")
    DISTRIBUTION_URL = os.getenv("DISTRIBUTION_URL", f"{HUB_API_URL}/api/v1/distribution/bundles")
    POLL_INTERVAL_S = float(os.getenv("POLL_INTERVAL_S", "5.0"))
    BASE_BACKOFF_S = float(os.getenv("BASE_BACKOFF_S", "1.0"))
    MAX_BACKOFF_S = float(os.getenv("MAX_BACKOFF_S", "60.0"))

    # `services/reputation_gate_client.py` -- the content-moderation gate's
    # real HTTP call to `core/reputation_module`'s internal API (service
    # boundary, never a direct DB write or vendored-code import). Same
    # `X-Service-Key` shared-secret convention `hub_api/services/
    # analytics_proxy.py`/`community_loyalty.py` already use to call an
    # `internal_bp`-gated endpoint on another service; `SERVICE_API_KEY` is
    # the same env var/Helm secret every module's `internal_bp` already
    # reads (`core/reputation_module/config.py`'s own `SERVICE_API_KEY`).
    REPUTATION_API_URL = os.getenv("REPUTATION_API_URL", "http://waddlebot-reputation:8021")
    SERVICE_API_KEY = os.getenv("SERVICE_API_KEY", "")

    RUNNER_TENANT_SLUG = os.getenv("RUNNER_TENANT_SLUG", "global")
    RUNNER_COMMUNITY_ID = _optional_int(os.getenv("RUNNER_COMMUNITY_ID"))

    #: Board-demo live activity feed (`services/activity_feed.py`) fallback
    #: community -- the pipeline runs tenant-wide/`community=None` today, so
    #: this is the community every `live_activity_events` row lands under
    #: when an envelope carries no community. 4 is the demo "waddlebot"
    #: community. Also the `demo_default` passed to `services.
    #: community_resolver.resolve_community` (see `COMMUNITY_RESOLUTION_
    #: ENABLED` below).
    DEMO_ACTIVITY_COMMUNITY_ID = int(os.getenv("DEMO_ACTIVITY_COMMUNITY_ID", "4"))

    #: Community resolution (gh #311, `services/community_resolver.py`) --
    #: default ON. `false` restores the pipeline's prior unconditional
    #: demo-shim mapping for a tenant-wide (`community=None`) envelope, with
    #: no per-user/channel lookup at all -- an operational escape hatch, not
    #: expected to be flipped off in normal operation.
    COMMUNITY_RESOLUTION_ENABLED = os.getenv(
        "COMMUNITY_RESOLUTION_ENABLED", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}

    SECRET_KEY = require_secret_key()
    JWT_SCOPE = "distribution:read"

    VALKEY_URL = os.getenv("VALKEY_URL", "redis://localhost:6379/0")

    # DB account -- bound at startup via `flask_core.set_bundle_dal()` so a
    # stateful process bundle can call `get_bundle_dal()` from inside its
    # own `transform()` body. See module docstring for the per-service
    # account note.
    DB_TYPE = os.getenv("DB_TYPE", "postgresql")
    DATABASE_URL = _normalize_pydal_scheme(
        os.getenv("DATABASE_URL")
        or _build_db_url(
            db_type=DB_TYPE,
            host=os.getenv("DB_HOST", "infra-postgres"),
            port=os.getenv("DB_PORT", "5432"),
            name=os.getenv("DB_NAME", "waddlebot"),
            user=os.getenv("DB_USER", "svc-process-rw"),
            password=os.getenv("DB_PASS", ""),
        )
    )
    DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
