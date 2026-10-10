"""Core-bundle seeder -- activates first-party `waddles.core.*` App Bundles at deploy time.

Run as `python3 -m cli.seed_core_bundles` from hub-api's own `/app` WORKDIR (mirrors this
repo's existing top-level-module convention -- `hub_api/conftest.py` inserts `hub_api/` itself
onto `sys.path` so `services.*`/`blueprints.*` import as top-level packages, not
`hub_api.services.*`; this CLI's own `cli/` package sits alongside them the same way, rather
than under a `hub_api.` namespace nothing else in this codebase uses). In-cluster only, via a
Helm post-install/post-upgrade hook Job (`k8s/helm/waddlebot/templates/core-bundle-seeder-
job.yaml`) -- no HTTP endpoint, no JWT, hub-api's own DB/S3/Valkey env (`HubAPIConfig.
from_env()`).

**Why this exists.** `services/bundle_approval_service.py::approve_version()` is otherwise
reachable only through `blueprints/v1/bundle_approvals.py::post_approve`, gated on
`@require_scope("platform:admin")` (Justin's 2026-09-27 vendor-separation ruling: a vendor
SUBMITS, only a GLOBAL ADMIN APPROVES). First-party `waddles.core.*` bundles need a way to
activate with zero human involvement at deploy time -- this module is that path, reusing
`approve_version()`/`create_version()`/`process_prebuilt_component()` unchanged (their own new
`approval_source`/nullable `approved_by`/`requested_by` parameters exist for exactly this
caller) rather than duplicating the transaction/state-machine/validation logic those functions
already own.

**HARD GUARD** (`_guard_core_namespace`, called first thing in `seed_one()`, before any DB
write): every `app_id` this module ever touches MUST fall under
`services.vendor_bundle_authz.CORE_NAMESPACE_PREFIX` ("waddles.core."). A catalog entry outside
that namespace is refused with a non-zero exit for that bundle -- vendor bundles must never be
seedable, no matter what a (compromised or mistaken) catalog file says.

**System-actor representation.** `approved_by=None` (a real SQL NULL -- the column is
nullable, migration 0023) + `approval_source="system:core-seeder"` (migration 0026) -- never a
fake `hub_users` row. Same for `create_version(requested_by=None)`.

**Idempotency.** Keyed on `(app_id, version, artifact_digest)`: if `app_versions` already has
this exact `(app_id, version)` published with the matching digest, publishing is skipped and
the existing version_id is reused; per activation target, if `app_active_versions` already
points at that same `version_id`, activation is skipped too (a true no-op, logged, no new
`app_install_approvals` row). A different digest under the SAME version string is a data
integrity conflict (app_versions has its own `UNIQUE(app_id, version)`) -- refused, not
overwritten; bump the catalog's `version` field alongside the artifact.

**Platform connections** (`bundles/core-bundles.yaml`'s own `platform_connections:` section,
extended by the `CORE_BUNDLES_PLATFORM_CONNECTIONS` env var -- a JSON array, populated by
`k8s/helm/waddlebot/templates/core-bundle-seeder-job.yaml` from the SAME Helm values already
wired to svc-ingest, e.g. `pipeline.rustDataPlane.svcProcess.processIngestPlatform`/
`processIngestSourceId` for its Discord guild) are registered into `ingest_sources`
(`services/ingest_source_service.py::ensure_ingest_source()`, SYSTEM actor, idempotent, NO
credentials/tokens stored -- the platform's own client authenticates independently) BEFORE any
bundle is activated, so `app_source_binding_service.sync_bindings()`'s auto-bind has a matching
row to grant a bundle's `consumes` rule against on its very first activation.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from penguin_dal import AsyncDB

from config import HubAPIConfig
from services import vendor_bundle_authz
from services.app_source_binding_service import TENANT_WIDE_COMMUNITY_SENTINEL
from services.bundle_approval_service import (
    activate_for_community,
    activate_tenant_wide,
    deactivate_for_community,
    deactivate_tenant_wide,
    install_version_globally,
)
from services.bundle_install_dal import build_install_dal, raw_sql_write
from services.bundle_manifest_v2 import BundleManifestV2, parse_bundle_manifest_v2
from services.bundle_permission_service import (
    grant_community_permissions,
    seed_core_permission_requests,
)
from services.bundle_telemetry import get_meter
from services.bundle_version_service import (
    STATUS_ADDRESSING,
    STATUS_INSPECTING,
    STATUS_REJECTED,
    STATUS_UPLOADED,
    STATUS_VALIDATING,
    create_version,
    process_prebuilt_component,
)
from services.errors import ApiError
from services.ingest_source_service import ensure_ingest_source
from services.tenant_app_availability_service import set_available

# Guarded the same way `services/role_sync_service.py`/`services/event_discord_sync_service.py`
# already do -- this module's own tests monkeypatch the module-level `feature_enabled` name
# directly (no live PostHog/license server needed); `None` only outside a real `flask_core`
# install (never production).
try:
    from flask_core.feature_flags import feature_enabled
except ImportError:  # pragma: no cover -- exercised only outside the real flask_core install
    feature_enabled = None

logger = logging.getLogger("waddles.hub_api.core_bundle_seeder")

#: The SYSTEM actor string persisted to `app_install_approvals.approval_source`
#: (migration 0026) -- see this module's own docstring.
SYSTEM_ACTOR = "system:core-seeder"

#: Opt-out kill-switch (critical-rules.md "Core platform mechanisms... opt-out kill-switch"):
#: unseen/OFF performs the reconcile-uninstall sweep below (`reconcile_removed_core_bundles()`,
#: the default -- requirement: "uninstall should delete them just like install adds them,
#: otherwise our scale will get out of sync"); ON reverts to the legacy activate-only seeder
#: (no deletes) -- a same-day mitigation if the sweep ever misbehaves, without a redeploy.
FLAG_DISABLE_SEEDER_UNINSTALL_RECONCILE = "waddles.disable-seeder-uninstall-reconcile"

#: Where the built catalog + manifests + `.wasm` artifacts live inside the seeder image
#: (`bundles/Dockerfile.core-bundles`'s final stage) -- overridable for local/dev runs
#: against a plain checkout of `bundles/` (see `bundles/core-bundles.yaml`'s own header).
DEFAULT_BUNDLES_DIR = "/core-bundles"
DEFAULT_CATALOG_FILENAME = "core-bundles.yaml"

#: Env var: comma-separated tenant slugs overriding EVERY catalog entry's own
#: `activation_targets` with a tenant-wide activation per listed slug.
_TENANT_SLUGS_OVERRIDE_ENV = "CORE_BUNDLES_TENANT_SLUGS"

#: Env var: a JSON array of platform-connection objects (same shape as one
#: `platform_connections:` catalog entry) EXTENDING (never replacing) the catalog's own
#: static list -- how a deploy-time-only value (e.g. an alpha cluster's real Discord guild
#: id) reaches the seeder without baking it into the image. See module docstring.
_PLATFORM_CONNECTIONS_ENV = "CORE_BUNDLES_PLATFORM_CONNECTIONS"

#: Strict lowercase-alnum-dot-hyphen charset for a well-formed dotted app_id -- the segment
#: shape every real `waddles.core.*` app_id already uses (`bundle_manifest_v2._APP_ID_RE`'s
#: own `[a-z0-9][a-z0-9_-]*` segments, minus underscores: no legitimate core app_id has ever
#: used one). Deliberately ASCII-only -- see `_guard_core_namespace()`'s own docstring.
_APP_ID_CHARSET_RE = re.compile(r"^[a-z0-9]+(\.[a-z0-9-]+)+$")

#: `ApiError.code` values that must NOT abort the whole Helm hook (gh-576: "seeder fails
#: whole hook on one conflict"). Today this is exactly `digest_conflict` --
#: `_resolve_or_publish_version()`'s own immutable-version guard -- which recurs
#: indefinitely for `language: python` bundles specifically: `componentize-py` is a KNOWN,
#: pre-approved non-reproducible build (see this repo's own mem0 note
#: "python-bundle-snapshot-fallback" / `scripts/verify-core-bundles-reproducible.sh`'s
#: Rust-only repro gate) -- an unrelated image rebuild (e.g. a different bundle's Dockerfile
#: stage changing) can legitimately produce new `pyping.wasm` bytes under an UNCHANGED
#: `core-bundles.yaml` version string, 409-ing forever against the one row that already
#: published successfully. The already-published row keeps serving correctly either way --
#: there is nothing an operator can act on except "bump the version", which does not even
#: fix the underlying non-reproducibility for the NEXT rebuild. Treating this one code as a
#: WARNING + skip (never a hard failure) stops a cosmetic artifact-byte drift from blocking
#: every subsequent `helm upgrade` indefinitely; every OTHER `ApiError` code (tenant_not_
#: found, stalled_core_bundle_upload, ...) still fails the run -- those genuinely need a
#: human to intervene before the row they describe can ever resolve itself.
RECOVERABLE_API_ERROR_CODES = frozenset({"digest_conflict"})

#: Env var: how old (seconds) an orphaned `waddles.core.*` `app_version_uploads` row's own
#: `status_changed_at` must be before `_recover_stalled_core_upload()` resets it, rather than
#: deferring to the generic `stalled_core_bundle_upload` fail-loud path below.
#:
#: Deliberately far shorter than `bundle_version_service.BUNDLE_UPLOAD_STALL_TIMEOUT_SECONDS`
#: (15m default, sized for vendor uploads where multiple human/API clients can genuinely be
#: mid-upload at once): this seeder is a single-shot, non-concurrent CLI process -- `seed_one()`
#: is called exactly once per catalog entry per run, and `_resolve_or_publish_version()` is the
#: FIRST write this process ever attempts against a given `(app_id, version)`'s own upload row.
#: Any pre-existing non-terminal row found here was therefore left behind by a DIFFERENT process
#: invocation -- the real alpha incident this fixes: a prior seeder pod crashed mid-INSPECTING
#: (e.g. after a SeaweedFS blip), Kubernetes restarted the Job per its own `backoffLimit` within
#: minutes, well inside the 15m general-purpose window above, which never got a chance to fire
#: and self-heal it first. The short grace window below is pure defense-in-depth against a
#: genuinely-overlapping SECOND seeder Job (e.g. a Helm hook firing twice) -- it is not there to
#: wait out this run's own in-flight work, which never reaches this branch to begin with.
CORE_SEEDER_STALL_RECOVERY_SECONDS_ENV = "CORE_BUNDLE_SEEDER_STALL_RECOVERY_SECONDS"
_DEFAULT_CORE_SEEDER_STALL_RECOVERY_SECONDS = 30

#: Non-terminal `app_version_uploads.status` values the core-bundle seeder's own pre-built-
#: component path (`bundle_version_service.process_prebuilt_component()`: UPLOADED ->
#: VALIDATING -> INSPECTING -> ADDRESSING -> PUBLISHED) can ever leave a row at mid-pipeline.
#: Listed explicitly, never "anything not PUBLISHED/REJECTED" -- a state-machine change
#: elsewhere must not silently widen what this seeder is willing to auto-recover.
_RECOVERABLE_UPLOAD_STATUSES = frozenset(
    {STATUS_UPLOADED, STATUS_VALIDATING, STATUS_INSPECTING, STATUS_ADDRESSING}
)


def _core_seeder_stall_recovery_seconds() -> int:
    """The configurable grace window (default 30s), re-read per call so tests can monkeypatch."""
    raw = os.environ.get(CORE_SEEDER_STALL_RECOVERY_SECONDS_ENV, "")
    try:
        return int(raw) if raw else _DEFAULT_CORE_SEEDER_STALL_RECOVERY_SECONDS
    except ValueError:
        # A malformed env var is an operator misconfiguration, not an expected empty/unset
        # value (that path never reaches `int(raw)` -- see the `if raw` guard above) -- logged
        # so a bad deploy-time override is visible instead of silently reverting to the
        # default with no trace (critical-rules.md Fail-Loud Code Paths).
        logger.warning(
            "core-bundle-seeder: invalid %s=%r, falling back to default %ds",
            CORE_SEEDER_STALL_RECOVERY_SECONDS_ENV,
            raw,
            _DEFAULT_CORE_SEEDER_STALL_RECOVERY_SECONDS,
            extra={"env_var": CORE_SEEDER_STALL_RECOVERY_SECONDS_ENV, "raw_value": raw},
        )
        return _DEFAULT_CORE_SEEDER_STALL_RECOVERY_SECONDS


async def _recover_stalled_core_upload(install_dal: AsyncDB, *, app_id: str, version: str) -> bool:
    """Reset an orphaned, genuinely-stalled `app_version_uploads` row so re-seeding can proceed.

    Called only after `create_version()` has already refused `(app_id, version)` with a 409
    `CONFLICT` -- i.e. a row for this core bundle exists and is not already `REJECTED`. Returns
    `True` once the row has been reset to `REJECTED` (safe to retry `create_version()`, which
    then takes the already-tested REJECTED-row-reuse path -- see that function's own docstring),
    `False` if this row is NOT a safe self-heal candidate, in which case the caller must fail
    loudly rather than ever guessing.

    Refuses to touch anything except exactly the shape this seeder itself could have left
    behind:

      - Exactly one matching row -- `app_version_uploads` has `UNIQUE(app_id, version)`, so more
        than one is a schema-level impossibility, treated as ambiguous rather than picking one.
      - `status` is one of `_RECOVERABLE_UPLOAD_STATUSES`. A `PUBLISHED` upload row is the one
        way this function is ever reached with a terminal status -- `_resolve_or_publish_version`
        already checked `app_versions` directly first and only reaches `create_version()` when
        no published `app_versions` row exists -- so a `PUBLISHED` `app_version_uploads` row
        here means the two tables disagree: a genuine data-integrity gap, never auto-resolved.
      - `status_changed_at` (falling back to `updated_at`/`created_at`, same convention
        `create_version()`'s own general stall check uses) is older than
        `CORE_BUNDLE_SEEDER_STALL_RECOVERY_SECONDS` (default 30s) -- see that env var's own
        docstring for why this window is so much shorter than the general-purpose one.

    Never raises -- a `False` return always leaves the row completely untouched; the caller
    decides what to do about it (fail loudly, in every current call site).
    """
    rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    if len(rows) != 1:
        return False
    row = rows.first()
    if row is None or row.status not in _RECOVERABLE_UPLOAD_STATUSES:
        return False

    status_ts = row.status_changed_at or row.updated_at or row.created_at
    if status_ts.tzinfo is None:
        status_ts = status_ts.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    age_seconds = (now - status_ts).total_seconds()
    if age_seconds < _core_seeder_stall_recovery_seconds():
        return False

    await install_dal(install_dal.app_version_uploads.id == row.id).update(
        status=STATUS_REJECTED,
        reject_reason=(
            f"stalled: core-bundle-seeder self-heal, orphaned {row.status} row "
            f"(age={int(age_seconds)}s) left by an earlier crashed/killed seeder run"
        ),
        updated_at=now,
        status_changed_at=now,
    )
    # PII-free by construction -- app_id/version/upload_id/status/age only, no user data,
    # see this repo's own "bundle logs must be PII-free" convention.
    logger.warning(
        "core-bundle-seeder: recovered an orphaned stalled upload row",
        extra={
            "app_id": app_id,
            "version": version,
            "upload_id": row.id,
            "prior_status": row.status,
            "age_seconds": int(age_seconds),
        },
    )
    return True


@dataclass(slots=True, frozen=True)
class ActivationTarget:
    """One `(tenant, community)` pair to seed.

    `community_id=None` means TENANT-WIDE activation (`bundle_approval_
    service.activate_tenant_wide()`, the schema's own sentinel convention
    -- `app_active_versions.community_id=0`/`app_install_approvals.
    community_id=NULL`) -- NOT a skip. Regression: seeder skipped
    activation for community_id null (alpha 2026-10-02) -- every core
    bundle's catalog entry declares `community_id: null` and the seeder
    used to `continue` past COMMUNITY-tier activation entirely for it,
    leaving `app_active_versions`/`app_source_bindings` empty forever. See
    `services/bundle_approval_service.py`'s own module docstring for the
    3-tier split and why this sentinel path is SYSTEM-actor-only.
    """

    tenant_slug: str
    community_id: int | None = None


@dataclass(slots=True, frozen=True)
class CatalogEntry:
    """One `bundles/core-bundles.yaml` row -- what to seed, and for whom."""

    app_id: str
    version: str
    language: str
    manifest_path: str
    artifact_path: str
    activation_targets: tuple[ActivationTarget, ...] = field(default_factory=tuple)


@dataclass(slots=True, frozen=True)
class SeedResult:
    """The outcome of seeding one `(app_id, version)` for one activation target."""

    app_id: str
    version: str
    outcome: str
    detail: str = ""


@dataclass(slots=True, frozen=True)
class PlatformConnection:
    """One `ingest_sources` row to register before bundle activation -- see module docstring.

    Never carries a secret/token -- the platform's own client (e.g. svc-ingest's Discord bot)
    authenticates independently of this table; see `ensure_ingest_source()`'s own docstring.
    """

    tenant_slug: str
    platform: str
    source_id: str
    label: str
    community_id: int | None = None


@dataclass(slots=True, frozen=True)
class ConnectionResult:
    """The outcome of registering one `PlatformConnection`."""

    platform: str
    source_id: str
    outcome: str


class CoreBundleSeederError(RuntimeError):
    """Raised for a non-retriable seeder-level failure (bad catalog, non-core app_id, ...)."""


def _parse_connection(item: dict[str, Any]) -> PlatformConnection:
    """One `platform_connections:` catalog/env entry -> `PlatformConnection`.

    `label` defaults to `source_id` when omitted -- every real entry so far
    (`processIngestSourceId`-shaped) has no separate human label of its own.
    """
    return PlatformConnection(
        tenant_slug=item["tenant_slug"],
        platform=item["platform"],
        source_id=item["source_id"],
        label=item.get("label") or item["source_id"],
        community_id=item.get("community_id"),
    )


def load_catalog(catalog_path: Path) -> tuple[CatalogEntry, ...]:
    """Parse `catalog_path` (`bundles/core-bundles.yaml`'s own shape) into `CatalogEntry` rows."""
    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    entries: list[CatalogEntry] = []
    for item in raw.get("bundles") or []:
        targets = tuple(
            ActivationTarget(
                tenant_slug=target["tenant_slug"],
                community_id=target.get("community_id"),
            )
            for target in item.get("activation_targets") or []
        )
        entries.append(
            CatalogEntry(
                app_id=item["app_id"],
                version=str(item["version"]),
                language=item["language"],
                manifest_path=item["manifest_path"],
                artifact_path=item["artifact_path"],
                activation_targets=targets or (ActivationTarget(tenant_slug="global"),),
            )
        )
    return tuple(entries)


def load_platform_connections(catalog_path: Path) -> tuple[PlatformConnection, ...]:
    """`catalog_path`'s own `platform_connections:` list, EXTENDED by the env override.

    The catalog file's list is static (baked into the seeder image) -- for a value that must
    vary per deployment (an alpha cluster's real Discord guild id, never invented/hardcoded),
    `CORE_BUNDLES_PLATFORM_CONNECTIONS` (a JSON array of the same object shape) adds MORE
    connections rather than replacing the catalog's own list, matching this module's
    tenant-slug override's own additive-vs-replacing note (see `_resolve_activation_targets`,
    which instead replaces -- deliberately different: a tenant-slug override changes WHERE
    every bundle activates, a connection is registered independently per entry).
    """
    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
    connections = [_parse_connection(item) for item in raw.get("platform_connections") or []]

    env_raw = os.getenv(_PLATFORM_CONNECTIONS_ENV)
    if env_raw:
        connections.extend(_parse_connection(item) for item in json.loads(env_raw))
    return tuple(connections)


def _resolve_activation_targets(entry: CatalogEntry) -> tuple[ActivationTarget, ...]:
    """`CORE_BUNDLES_TENANT_SLUGS`, if set, overrides `entry`'s own catalog-declared targets."""
    override = os.getenv(_TENANT_SLUGS_OVERRIDE_ENV)
    if not override:
        return entry.activation_targets
    slugs = [slug.strip() for slug in override.split(",") if slug.strip()]
    return tuple(ActivationTarget(tenant_slug=slug) for slug in slugs)


def _guard_core_namespace(app_id: str) -> None:
    """Refuse an `app_id` outside the reserved `waddles.core.*` namespace (see module docstring).

    Hardened against lookalike/homoglyph app_ids (security review, 2026-09-27): two checks run
    BEFORE the prefix comparison itself, so a crafted app_id can never reach that comparison in
    a form that could confuse it.

      1. NFKC-normalizing `app_id` must be a no-op. A confusable character that NFKC folds
         toward a different byte sequence (e.g. a fullwidth or ligature form of an ASCII
         letter) would otherwise let an app_id LOOK different from `waddles.core.*` to this
         raw `.startswith()` check while a downstream consumer using normalized/collated
         comparison (a DB index, a case/width-insensitive lookup) could treat it as the same
         string -- rejecting any app_id normalization would actually change closes that gap
         outright, independent of what any downstream consumer happens to do.
      2. `_APP_ID_CHARSET_RE` requires plain ASCII lowercase/digits/dot/hyphen -- the exact
         segment shape every real `waddles.core.*` app_id already uses. This independently
         rejects every non-ASCII homoglyph (Cyrillic/Greek lookalikes, etc.) that NFKC leaves
         untouched (NFKC does not fold across scripts), plus malformed shapes (empty segments,
         uppercase, leading/trailing/consecutive dots) a bare prefix check would not catch.

    `"waddles.corex.*"` (a near-miss, not a homoglyph) is refused by the prefix check itself,
    same as before -- covered here only for the charset/normalization checks' own scope.
    """
    if unicodedata.normalize("NFKC", app_id) != app_id or not _APP_ID_CHARSET_RE.match(app_id):
        raise CoreBundleSeederError(
            f"refusing to seed {app_id!r}: not a well-formed lowercase dotted app_id "
            "(ASCII a-z0-9.- only, NFKC-normalization must be a no-op)"
        )
    if not app_id.startswith(vendor_bundle_authz.CORE_NAMESPACE_PREFIX):
        raise CoreBundleSeederError(
            f"refusing to seed {app_id!r}: core-bundle-seeder only seeds "
            f"{vendor_bundle_authz.CORE_NAMESPACE_PREFIX}* app_ids -- a vendor bundle must go "
            "through POST /apps/{app_id}/versions/approve (platform:admin, human-gated)"
        )


async def _ensure_app_catalog_row(install_dal: AsyncDB, manifest: BundleManifestV2) -> None:
    """Idempotent `app_catalog` upsert -- the FK target `app_version_uploads`/`app_versions` need.

    Mirrors `config/postgres/migrations/071_app_catalog_stages.sql`'s own seeded
    `waddles.core.demo.echo` row (`INSERT ... ON CONFLICT (app_id) DO NOTHING`) -- done here in
    Python (existence-check then insert) so a new core bundle only needs a
    `bundles/core-bundles.yaml` entry, not a hand-written SQL migration per bundle.

    Goes through `raw_sql_write()` (`bundle_install_dal.py`'s own documented escape hatch),
    not `install_dal.app_catalog.async_insert()` -- `app_catalog` is a pre-existing,
    pydal-defined table (`services/schema.py::bind_app_bundle_tables()`), and `platform_
    compatibility`'s real production type is Postgres `JSONB` (migration 069) but reflects as
    a plain TEXT column against the sqlite fixture pydal's own `migrate=True` builds for tests
    -- `TableProxy.async_insert()` cannot bind a raw Python `dict` there. `json.dumps()`-ing it
    ourselves and binding a plain string sidesteps that split: asyncpg casts a text parameter
    into the target `jsonb` column same as SQLAlchemy's own JSON bind processor would, and
    sqlite stores it as the TEXT it already is.
    """
    existing = await install_dal(install_dal.app_catalog.app_id == manifest.app_id).select()
    if existing:
        return
    await raw_sql_write(
        install_dal,
        """
        INSERT INTO app_catalog (
            app_id, name, manifest_version, module, feature, provider,
            execution_model, is_default, platform_compatibility, status
        ) VALUES (
            :app_id, :name, :manifest_version, :module, :feature, :provider,
            :execution_model, :is_default, :platform_compatibility, :status
        )
        """,
        {
            "app_id": manifest.app_id,
            "name": manifest.name,
            "manifest_version": manifest.version,
            "module": manifest.module,
            "feature": manifest.feature,
            "provider": manifest.provider,
            "execution_model": manifest.execution_model,
            "is_default": manifest.is_default,
            "platform_compatibility": json.dumps(
                {"tested_with": manifest.version, "min_version": None, "max_version": None}
            ),
            "status": "active",
        },
    )
    logger.info(
        "core-bundle-seeder: app_catalog row created",
        # "module" collides with logging.LogRecord's own reserved `module` attribute
        # (the calling module's name, always present on every record) -- passing it
        # via `extra` unconditionally raises `KeyError: "Attempt to overwrite 'module'
        # in LogRecord"` from Logger.makeRecord(), regardless of handler/formatter.
        # This crashed EVERY first-time seed of a catalog entry (the only time this
        # branch's log call fires -- a pre-existing app_catalog row skips it entirely),
        # surfacing as a generic "bundle failed" ApiError-shaped failure on a genuinely
        # fresh install. Renamed to bundle_module -- never reuse a LogRecord reserved
        # name (message/asctime/name/msg/args/levelname/levelno/pathname/filename/
        # module/exc_info/exc_text/stack_info/lineno/funcName/created/msecs/
        # relativeCreated/thread/threadName/processName/process) in any `extra` dict.
        extra={"app_id": manifest.app_id, "bundle_module": manifest.module},
    )


async def _resolve_tenant_id(install_dal: AsyncDB, tenant_slug: str) -> int:
    """`tenants.id` for `tenant_slug`. Raises if the tenant does not exist (never auto-created)."""
    rows = await install_dal(install_dal.tenants.slug == tenant_slug).select()
    row = rows.first()
    if row is None:
        raise ApiError(
            f"tenant slug {tenant_slug!r} does not exist -- cannot activate a core bundle for it",
            500,
            "tenant_not_found",
        )
    return int(row.id)


async def _already_installed_globally(
    install_dal: AsyncDB, *, app_id: str, version_id: int
) -> bool:
    """Whether `app_id`'s CURRENT `app_global_installs` row already points at `version_id`."""
    rows = await install_dal(
        (install_dal.app_global_installs.app_id == app_id)
        & (install_dal.app_global_installs.superseded_by == None)  # noqa: E711
        & (install_dal.app_global_installs.revoked_at == None)  # noqa: E711
    ).select()
    row = rows.first()
    return row is not None and int(row.version_id) == int(version_id)


async def _already_available(install_dal: AsyncDB, *, tenant_id: int, app_id: str) -> bool:
    """Whether `app_id` is already enabled in `tenant_id`'s marketplace."""
    rows = await install_dal(
        (install_dal.bundle_tenant_availability.tenant_id == tenant_id)
        & (install_dal.bundle_tenant_availability.app_id == app_id)
        & (install_dal.bundle_tenant_availability.available == True)  # noqa: E712
    ).select()
    return bool(rows.first())


async def _already_active(
    install_dal: AsyncDB, *, app_id: str, tenant_id: int, community_id: int, version_id: int
) -> bool:
    """Whether `(app_id, tenant_id, community_id)` already points at `version_id`.

    True is the no-op case -- the caller skips re-activating.
    """
    rows = await install_dal(
        (install_dal.app_active_versions.app_id == app_id)
        & (install_dal.app_active_versions.tenant_id == tenant_id)
        & (install_dal.app_active_versions.community_id == community_id)
    ).select()
    row = rows.first()
    return row is not None and int(row.version_id) == int(version_id)


async def _resolve_or_publish_version(
    install_dal: AsyncDB,
    *,
    entry: CatalogEntry,
    manifest_bytes: bytes,
    component_bytes: bytes,
    digest_hex: str,
    publish_tenant_id: int,
    publish_tenant_slug: str,
    valkey_client: Any | None = None,
) -> int:
    """The `app_versions.id` for `(entry.app_id, entry.version)`, publishing it if not yet present.

    Raises `ApiError` 409 `digest_conflict` if this exact `(app_id, version)` is already
    published with a DIFFERENT digest (`app_versions` is immutable per version string, spec
    Sec6.10) -- the catalog's `version` field must be bumped alongside the artifact, never
    silently republished under the same version string.
    """
    existing = await install_dal(
        (install_dal.app_versions.app_id == entry.app_id)
        & (install_dal.app_versions.version == entry.version)
    ).select()
    existing_row = existing.first()
    # bundle_version_service._publish_prebuilt_version() writes the raw hex digest with no
    # "sha256:" prefix for a prebuilt artifact (unlike the "sha256:"-prefixed convention some
    # `source`-artifact test fixtures use elsewhere) -- see its own `hashlib.sha256(...).
    # hexdigest()` call and test_bundle_version_service.py::test_process_prebuilt_component_
    # happy_path's identical `expected_digest`.
    expected_digest = digest_hex
    if existing_row is not None:
        if existing_row.artifact_digest != expected_digest:
            raise ApiError(
                f"core-bundle-seeder: refusing to seed {entry.app_id}@{entry.version} -- "
                f"app_versions already has this exact (app_id, version) published with a "
                f"DIFFERENT digest (existing={existing_row.artifact_digest!r}, "
                f"this_build={expected_digest!r}). app_versions is immutable per version "
                "string, so this is NEVER auto-resolved. ACTION REQUIRED: bump the version "
                "in bundles/core-bundles.yaml AND the bundle's own manifest "
                f"(bundles/{entry.language}/.../hub-manifest.yaml) to a new version string, "
                "then re-run the seeder.",
                409,
                "digest_conflict",
            )
        return int(existing_row.id)

    async def _attempt_create_version() -> None:
        await create_version(
            install_dal,
            tenant_id=publish_tenant_id,
            app_id=entry.app_id,
            requested_by=None,
            manifest_bytes=manifest_bytes,
            source_bytes=None,
            component_bytes=component_bytes,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            # Core bundles are always allowed prebuilt, independent of the
            # tenant-configurable `platform_settings.bundles.allow_prebuilt`
            # toggle (that gate exists for vendor/tenant-uploaded bundles).
            allow_prebuilt=True,
        )

    try:
        await _attempt_create_version()
    except ApiError as exc:
        if exc.code != "CONFLICT":
            raise
        # A previous run created the app_version_uploads row but crashed before
        # process_prebuilt_component() published it. Resuming from an arbitrary mid-FSM
        # state is still out of scope (see module docstring's idempotency note) -- but
        # unlike a vendor upload, THIS namespace is guaranteed single-writer (the HARD
        # GUARD in seed_one() ensures only this seeder ever touches waddles.core.* rows,
        # and this process is single-shot/non-concurrent -- see
        # `_recover_stalled_core_upload()`'s own docstring), so a genuinely orphaned row
        # left by an earlier crashed run is self-healed here instead of requiring a
        # human to clear it manually.
        if not await _recover_stalled_core_upload(
            install_dal, app_id=entry.app_id, version=entry.version
        ):
            raise ApiError(
                f"{entry.app_id}@{entry.version} already has an app_version_uploads row that "
                "never reached PUBLISHED and is not (yet) eligible for seeder self-heal -- "
                "either it is ambiguous (a PUBLISHED upload row with no matching app_versions "
                "row, a genuine data-integrity gap) or it was touched too recently to safely "
                "assume the owning process is dead; inspect and clear that row manually before "
                "re-running",
                exc.status_code,
                "stalled_core_bundle_upload",
            ) from exc
        try:
            await _attempt_create_version()
        except ApiError as retry_exc:
            raise ApiError(
                f"{entry.app_id}@{entry.version}: seeder self-heal reset the orphaned upload "
                f"row but the re-upload still failed ({retry_exc.code}): {retry_exc.message} -- "
                "this needs manual investigation",
                retry_exc.status_code,
                "stalled_core_bundle_upload",
            ) from retry_exc

    published = await process_prebuilt_component(
        install_dal,
        app_id=entry.app_id,
        version=entry.version,
        component_bytes=component_bytes,
        tenant_slug=publish_tenant_slug,
        valkey_client=valkey_client,
    )
    if published.app_version_id is None:  # pragma: no cover - defensive, see bundle_version_service
        raise ApiError(
            f"{entry.app_id}@{entry.version} published with no app_version_id",
            500,
            "publish_failed",
        )
    return int(published.app_version_id)


async def seed_platform_connection(
    install_dal: AsyncDB, connection: PlatformConnection
) -> ConnectionResult:
    """Register one `PlatformConnection` into `ingest_sources` under the SYSTEM actor.

    Idempotent (`ensure_ingest_source()`'s own contract) -- `outcome` is `"created"` on a first
    registration, `"no_op"` on every re-run after that (byte-identical row already enabled).
    """
    tenant_id = await _resolve_tenant_id(install_dal, connection.tenant_slug)
    existing = await install_dal(
        (install_dal.ingest_sources.tenant_id == tenant_id)
        & (install_dal.ingest_sources.platform == connection.platform)
        & (install_dal.ingest_sources.source_id == connection.source_id)
    ).select()
    existing_row = existing.first()
    already_present = existing_row is not None and bool(existing_row.enabled)

    await ensure_ingest_source(
        install_dal,
        tenant_id=tenant_id,
        community_id=connection.community_id,
        platform=connection.platform,
        source_id=connection.source_id,
        label=connection.label,
    )
    outcome = "no_op" if already_present else "created"
    logger.info(
        "core-bundle-seeder: platform connection registered",
        extra={
            "platform": connection.platform,
            "source_id": connection.source_id,
            "tenant": connection.tenant_slug,
            "outcome": outcome,
        },
    )
    return ConnectionResult(
        platform=connection.platform, source_id=connection.source_id, outcome=outcome
    )


async def seed_one(
    install_dal: AsyncDB,
    entry: CatalogEntry,
    bundles_dir: Path,
    *,
    valkey_client: Any | None = None,
) -> list[SeedResult]:
    """Seed one catalog entry: publish (if new) + activate for every resolved target.

    HARD GUARD runs first, before any manifest parse or DB write. `valkey_client`, when passed
    (tests only), is forwarded to `process_prebuilt_component()`/`approve_version()` as-is --
    same convention those functions already use; `None` (the real call site) lets each build
    its own client.
    """
    _guard_core_namespace(entry.app_id)

    manifest_bytes = (bundles_dir / entry.manifest_path).read_bytes()
    component_bytes = (bundles_dir / entry.artifact_path).read_bytes()
    digest_hex = hashlib.sha256(component_bytes).hexdigest()

    manifest = parse_bundle_manifest_v2(
        yaml.safe_load(manifest_bytes),
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )
    if manifest.version != entry.version:
        raise CoreBundleSeederError(
            f"catalog version {entry.version!r} does not match manifest version "
            f"{manifest.version!r} for {entry.app_id}"
        )

    await _ensure_app_catalog_row(install_dal, manifest)

    targets = _resolve_activation_targets(entry)
    publish_target = targets[0]
    publish_tenant_id = await _resolve_tenant_id(install_dal, publish_target.tenant_slug)

    version_id = await _resolve_or_publish_version(
        install_dal,
        entry=entry,
        manifest_bytes=manifest_bytes,
        component_bytes=component_bytes,
        digest_hex=digest_hex,
        publish_tenant_id=publish_tenant_id,
        publish_tenant_slug=publish_target.tenant_slug,
        valkey_client=valkey_client,
    )

    already_installed = await _already_installed_globally(
        install_dal, app_id=entry.app_id, version_id=version_id
    )
    if not already_installed:
        await install_version_globally(
            install_dal,
            app_id=entry.app_id,
            version=entry.version,
            installed_by=None,
            install_source=SYSTEM_ACTOR,
        )

    # spec Sec3.6: pre-grant every permission this core bundle's manifest declares --
    # same SYSTEM-actor convention as the install above. `_guard_core_namespace()` (top
    # of this function) already hard-guards `entry.app_id` to CORE_NAMESPACE_PREFIX
    # before any DB write. Called UNCONDITIONALLY every seeder run, not just inside the
    # `if not already_installed:` branch above (review finding, PR #433 blocker): a core
    # bundle first installed before the permission-catalog system existed (e.g.
    # count/lurk/rps, installed 2026-09-27, one day before the catalog landed
    # 2026-09-28) is `already_installed` forever, so gating this call on that flag left
    # its GLOBAL tier (`app_permission_requests`) permanently empty. With the GLOBAL
    # ceiling never backfilled, `_grant_core_bundle_permissions()`'s own COMMUNITY-tier
    # self-heal below could never succeed either -- `grant_community_permissions()`
    # computes `allowed = approved(ceiling) - restricted`, finds every required
    # permission "not in catalog", and raises `permission_not_in_catalog_grant` (422),
    # caught and logged, never fixed -- so `community_permission_grants` stayed empty
    # for every pre-existing core bundle. `record_permission_requests()` (what this
    # calls) is a DELETE-then-INSERT keyed on `(app_id, version)`, so re-running it for
    # an already-seeded bundle is a safe, idempotent no-op -- it does not touch any
    # human/vendor-submitted `app_permission_requests` row for a different `app_id`.
    await seed_core_permission_requests(
        install_dal, app_id=entry.app_id, version=entry.version, manifest=manifest
    )

    results: list[SeedResult] = []
    for target in targets:
        tenant_id = await _resolve_tenant_id(install_dal, target.tenant_slug)

        if not await _already_available(install_dal, tenant_id=tenant_id, app_id=entry.app_id):
            await set_available(
                install_dal, tenant_id=tenant_id, app_id=entry.app_id, updated_by=None
            )
            results.append(
                SeedResult(
                    entry.app_id, entry.version, "made_available", f"tenant={target.tenant_slug!r}"
                )
            )

        if target.community_id is None:
            # TENANT-WIDE activation -- see `ActivationTarget`'s own docstring
            # (regression: seeder skipped activation for community_id null,
            # alpha 2026-10-02). The DB-side sentinel is 0
            # (`TENANT_WIDE_COMMUNITY_SENTINEL`), so `_already_active` is
            # checked against that, not a literal `None`.
            if await _already_active(
                install_dal,
                app_id=entry.app_id,
                tenant_id=tenant_id,
                community_id=TENANT_WIDE_COMMUNITY_SENTINEL,
                version_id=version_id,
            ):
                await _grant_core_bundle_permissions(
                    install_dal,
                    tenant_id=tenant_id,
                    community_id=TENANT_WIDE_COMMUNITY_SENTINEL,
                    app_id=entry.app_id,
                    version=entry.version,
                    manifest=manifest,
                    valkey_client=valkey_client,
                )
                results.append(
                    SeedResult(
                        entry.app_id,
                        entry.version,
                        "no_op",
                        f"already active tenant-wide for tenant={target.tenant_slug!r}",
                    )
                )
                continue

            await activate_tenant_wide(
                install_dal,
                tenant_id=tenant_id,
                app_id=entry.app_id,
                activated_by=None,
                approval_source=SYSTEM_ACTOR,
                valkey_client=valkey_client,
            )
            await _grant_core_bundle_permissions(
                install_dal,
                tenant_id=tenant_id,
                community_id=TENANT_WIDE_COMMUNITY_SENTINEL,
                app_id=entry.app_id,
                version=entry.version,
                manifest=manifest,
                valkey_client=valkey_client,
            )
            results.append(
                SeedResult(
                    entry.app_id,
                    entry.version,
                    "activated",
                    f"tenant={target.tenant_slug!r} community_id=tenant-wide",
                )
            )
            continue

        if await _already_active(
            install_dal,
            app_id=entry.app_id,
            tenant_id=tenant_id,
            community_id=target.community_id,
            version_id=version_id,
        ):
            await _grant_core_bundle_permissions(
                install_dal,
                tenant_id=tenant_id,
                community_id=target.community_id,
                app_id=entry.app_id,
                version=entry.version,
                manifest=manifest,
                valkey_client=valkey_client,
            )
            results.append(
                SeedResult(
                    entry.app_id,
                    entry.version,
                    "no_op",
                    f"already active for tenant={target.tenant_slug!r} "
                    f"community_id={target.community_id!r}",
                )
            )
            continue

        await activate_for_community(
            install_dal,
            tenant_id=tenant_id,
            community_id=target.community_id,
            app_id=entry.app_id,
            activated_by=None,
            approval_source=SYSTEM_ACTOR,
            valkey_client=valkey_client,
        )
        await _grant_core_bundle_permissions(
            install_dal,
            tenant_id=tenant_id,
            community_id=target.community_id,
            app_id=entry.app_id,
            version=entry.version,
            manifest=manifest,
            valkey_client=valkey_client,
        )
        results.append(
            SeedResult(
                entry.app_id,
                entry.version,
                "activated",
                f"tenant={target.tenant_slug!r} community_id={target.community_id!r}",
            )
        )
    return results


#: `communities.name` of the tenant-wide sentinel row (id `TENANT_WIDE_COMMUNITY_SENTINEL`).
_SENTINEL_COMMUNITY_NAME = "__tenant_wide__"


async def _ensure_sentinel_community(install_dal: AsyncDB, *, tenant_id: int) -> None:
    """Ensure the tenant-wide sentinel `communities` row (id 0) exists -- idempotent.

    `community_permission_grants.community_id` is `REFERENCES communities(id)` (migration
    0041) but tenant-wide grants use the `TENANT_WIDE_COMMUNITY_SENTINEL` (0) scope, and
    `communities.id` is a SERIAL starting at 1 -- so without this row every tenant-wide
    grant insert raised an FK IntegrityError (36/40 core bundles failed to grant on a
    fresh DB, #480 kind-e2e). The row is owned by the `global` tenant when it exists
    (so deleting an ordinary tenant never cascades away every tenant's grants), else by
    the granting tenant; inactive + non-public so it never surfaces in community listings.
    Inserting an explicit id leaves the SERIAL sequence untouched.
    """
    await raw_sql_write(
        install_dal,
        "INSERT INTO communities (id, tenant_id, name, is_active, is_public) "
        "VALUES (:id, COALESCE((SELECT id FROM tenants WHERE slug = 'global'), :tenant_id), "
        ":name, FALSE, FALSE) ON CONFLICT (id) DO NOTHING",
        {
            "id": TENANT_WIDE_COMMUNITY_SENTINEL,
            "tenant_id": tenant_id,
            "name": _SENTINEL_COMMUNITY_NAME,
        },
    )


async def _grant_core_bundle_permissions(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int,
    app_id: str,
    version: str,
    manifest: BundleManifestV2,
    valkey_client: Any | None,
) -> None:
    """COMMUNITY-tier auto-grant for a SYSTEM-seeded core bundle (spec Sec3.3/Sec3.6).

    `seed_core_permission_requests()` above only writes the GLOBAL catalog-approval tier
    (`app_permission_requests`) -- it never writes `community_permission_grants` (the
    COMMUNITY tier `bundle_capability_gate::authorize()`'s `PgGrantLoader` actually reads
    at data-plane enforcement time, `core/svc_action::grant_gate`/`core/svc_process::
    grant_gate`'s own doc). Before the capability gate was wired in (PR #433), every host
    call fell back to the interim `AlwaysGrantedLoader`-only seam, so this gap was latent;
    once `authorize()` is live, EVERY already-seeded core bundle (count/lurk/rps/etc., spec
    Sec3.6) would silently start denying every `storage.kv`/etc. host call with zero grant
    row ever having been written for it. Called unconditionally on every seeder run
    (including for an already-active bundle) so this is self-healing for bundles installed
    before this fix shipped, not just newly-seeded ones -- `grant_community_permissions`'s
    own DELETE-then-INSERT is already idempotent (safe to call every run).

    Grants exactly what the manifest declares (no more, no less) -- mirrors
    `grant_community_permissions`'s own `required` vs `allowed` check, which this call
    must satisfy identically to a human community-admin's explicit consent, just performed
    by the SYSTEM actor instead. Never raises on a transient failure: a grant gap for one
    core bundle must not abort the whole seeder run (every other install/activation this
    run already committed) -- logged loud instead, so an operator notices and reruns.
    """
    required = frozenset(d.id for d in manifest.permission_declarations)
    if not required:
        return
    try:
        if community_id == TENANT_WIDE_COMMUNITY_SENTINEL:
            await _ensure_sentinel_community(install_dal, tenant_id=tenant_id)
        await grant_community_permissions(
            install_dal,
            tenant_id=tenant_id,
            community_id=community_id,
            app_id=app_id,
            version=version,
            manifest=manifest,
            granted_permission_ids=required,
            params_by_id=None,
            granted_by=None,
            valkey_client=valkey_client,
        )
    except ApiError:
        logger.exception(
            "core bundle permission auto-grant failed -- every non-platform host call for "
            "this (tenant, community, app) scope will deny until this is resolved",
            extra={
                "app_id": app_id,
                "tenant_id": tenant_id,
                "community_id": community_id,
                "required_permission_ids": sorted(required),
            },
        )
    except Exception as exc:  # noqa: BLE001 -- one bundle's grant failure must not abort the run
        logger.exception(
            "core bundle permission auto-grant hit an unexpected error (%s: %s) -- continuing "
            "with the remaining bundles; grants for this scope will deny until resolved",
            type(exc).__name__,
            exc,
            extra={"app_id": app_id, "tenant_id": tenant_id, "community_id": community_id},
        )


async def _tenant_slug_for_id(install_dal: AsyncDB, tenant_id: int) -> str:
    """`tenants.slug` for `tenant_id` -- local to this module, only needed for the flag check."""
    rows = await install_dal(install_dal.tenants.id == tenant_id).select()
    row = rows.first()
    if row is None or not row.slug:
        raise ApiError(f"tenant {tenant_id} has no slug configured", 500, "tenant_slug_missing")
    return str(row.slug)


async def _reconcile_uninstall_disabled(tenant_slug: str) -> bool:
    """Whether `FLAG_DISABLE_SEEDER_UNINSTALL_RECONCILE` is ON for `tenant_slug`.

    Unseen/OFF (the overwhelming common case, including every env with no PostHog reachable --
    `feature_enabled()`'s own outage degradation returns `default`) means the kill-switch is OFF
    and the reconcile sweep stays ON. `feature_enabled is None` only outside a real `flask_core`
    install (never production).
    """
    if feature_enabled is None:  # pragma: no cover -- only in a flask_core-less environment
        return False
    return bool(await feature_enabled(FLAG_DISABLE_SEEDER_UNINSTALL_RECONCILE, tenant=tenant_slug))


async def reconcile_removed_core_bundles(
    install_dal: AsyncDB, *, catalog_app_ids: frozenset[str]
) -> tuple[list[SeedResult], int]:
    """Hard-delete every SYSTEM-seeded `app_active_versions` row dropped from the catalog.

    Requirement (Justin): "uninstall should delete them just like install adds them, otherwise
    our scale will get out of sync" -- the Rust data plane (`core/bundle_active_set/src/
    query.rs::read_active_set()`) reads `app_active_versions` directly on every hub-api replica,
    so a `waddles.core.*` bundle removed from `bundles/core-bundles.yaml` must have its row
    actually DELETED here, the same way `seed_one()` above INSERTs it on the way in -- a row
    left behind drifts horizontally-scaled replicas (and the data plane) out of sync with each
    other.

    **Scope -- the safety boundary is the query predicate itself, not a post-hoc filter.** Only
    `app_install_approvals` rows with `approval_source == SYSTEM_ACTOR` (migration 0026) AND
    `superseded_by IS NULL` (the CURRENT row) are ever candidates. A user who later re-activates
    a dropped core bundle through the human COMMUNITY-admin path supersedes that row with their
    own `approval_source="human"` one (`_write_approval_and_activate()`'s own `previous_id`
    supersession is unconditional on actor) -- this function then no longer sees it, by
    construction of the join predicate, never by filtering a human-owned row out after matching
    it. A vendor-approved or any other human-installed activation is NEVER touched here for the
    exact same reason: it simply never satisfies the `approval_source` predicate in the first
    place.

    Idempotent across repeated runs: once `deactivate_for_community()`/`deactivate_tenant_wide()`
    removes the `app_active_versions` row, a `NOT_FOUND` from either call (already gone, e.g. a
    previous run already swept it) is treated as a no-op here, not a failure.

    Transactional per-row (each `deactivate_*` call is already its own `engine.begin()`
    transaction) -- one row's failure is logged and counted, never aborts the rest of the sweep
    (same best-effort-cascade convention `bundle_approval_service.uninstall_globally()`/
    `tenant_app_availability_service.unset_available()` already use for their own cascades).
    """
    rows = await install_dal(
        (install_dal.app_install_approvals.approval_source == SYSTEM_ACTOR)
        & (install_dal.app_install_approvals.superseded_by == None)  # noqa: E711
    ).select()

    results: list[SeedResult] = []
    failures = 0
    seen: set[tuple[int, int | None, str]] = set()
    flag_cache: dict[str, bool] = {}

    for row in rows:
        app_id = str(row.app_id)
        if app_id in catalog_app_ids:
            continue
        tenant_id = int(row.tenant_id)
        community_id = int(row.community_id) if row.community_id is not None else None
        key = (tenant_id, community_id, app_id)
        if key in seen:
            continue
        seen.add(key)

        try:
            tenant_slug = await _tenant_slug_for_id(install_dal, tenant_id)
            if tenant_slug not in flag_cache:
                flag_cache[tenant_slug] = await _reconcile_uninstall_disabled(tenant_slug)
            if flag_cache[tenant_slug]:
                logger.info(
                    "core-bundle-seeder: reconcile-uninstall skipped (kill-switch ON)",
                    extra={
                        "app_id": app_id,
                        "tenant_id": tenant_id,
                        "community_id": community_id,
                    },
                )
                continue

            if community_id is None:
                await deactivate_tenant_wide(
                    install_dal, tenant_id=tenant_id, app_id=app_id, deactivated_by=None
                )
            else:
                await deactivate_for_community(
                    install_dal,
                    tenant_id=tenant_id,
                    community_id=community_id,
                    app_id=app_id,
                    deactivated_by=None,
                )
        except ApiError as exc:
            if exc.code == "NOT_FOUND":
                continue  # already gone -- idempotent no-op, not a failure
            logger.error(
                f"core-bundle-seeder: reconcile-uninstall failed "
                f"({app_id}, tenant_id={tenant_id}, community_id={community_id}, "
                f"{exc.code}): {exc.message}",
                extra={
                    "app_id": app_id,
                    "tenant_id": tenant_id,
                    "community_id": community_id,
                    "error_code": exc.code,
                    "error": exc.message,
                    "reason": "seeder-reconcile",
                },
                exc_info=True,
            )
            failures += 1
            continue
        except Exception as exc:  # noqa: BLE001 -- one row's failure must not abort the sweep
            logger.error(
                f"core-bundle-seeder: reconcile-uninstall failed "
                f"({type(exc).__name__}: {exc}, app_id={app_id})",
                extra={
                    "app_id": app_id,
                    "tenant_id": tenant_id,
                    "community_id": community_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "reason": "seeder-reconcile",
                },
                exc_info=True,
            )
            failures += 1
            continue

        logger.info(
            "core-bundle-seeder: reconcile-uninstall deleted stale activation",
            extra={
                "app_id": app_id,
                "tenant_id": tenant_id,
                "community_id": community_id,
                "reason": "seeder-reconcile",
            },
        )
        results.append(
            SeedResult(
                app_id,
                str(row.version),
                "uninstalled_reconcile",
                f"tenant_id={tenant_id} community_id={community_id!r} (dropped from catalog)",
            )
        )
    return results, failures


async def _run(bundles_dir: Path, catalog_path: Path) -> int:
    config = HubAPIConfig.from_env()
    install_dal = await build_install_dal(config.database_url, pool_size=2)
    counter = get_meter().create_counter(
        "waddles_hub_core_bundle_seed_total",
        description="core-bundle-seeder bundle-activation attempts, by outcome",
    )

    failures = 0
    seeded = 0
    #: `(app_id, version, code)` strings for every RECOVERABLE_API_ERROR_CODES hit this run --
    #: never counted in `failures` (see that constant's own docstring), but always surfaced in
    #: the final summary log so an operator/dashboard sees it without the Job failing.
    skipped_conflicts: list[str] = []
    try:
        entries = load_catalog(catalog_path)
        connections = load_platform_connections(catalog_path)
        logger.info(
            "core-bundle-seeder: starting",
            extra={
                "catalog": str(catalog_path),
                "bundle_count": len(entries),
                "connection_count": len(connections),
            },
        )

        # Platform connections register BEFORE any bundle activates -- auto-bind
        # (app_source_binding_service.sync_bindings()) only grants a bundle's `consumes`
        # rule against an `ingest_sources` row that already exists at approval time.
        for connection in connections:
            try:
                connection_result = await seed_platform_connection(install_dal, connection)
            except Exception as exc:  # noqa: BLE001 -- one connection's failure must not abort the batch
                logger.error(
                    "core-bundle-seeder: platform connection failed",
                    extra={
                        "platform": connection.platform,
                        "source_id": connection.source_id,
                        "error": str(exc),
                    },
                )
                counter.add(1, {"platform": connection.platform, "outcome": "failed"})
                failures += 1
                continue
            counter.add(
                1, {"platform": connection_result.platform, "outcome": connection_result.outcome}
            )
            seeded += 1

        for entry in entries:
            try:
                results = await seed_one(install_dal, entry, bundles_dir)
            except CoreBundleSeederError as exc:
                logger.error(
                    f"core-bundle-seeder: refused ({entry.app_id}@{entry.version}): {exc}",
                    extra={"app_id": entry.app_id, "version": entry.version, "reason": str(exc)},
                    exc_info=True,
                )
                counter.add(1, {"app_id": entry.app_id, "outcome": "refused"})
                failures += 1
                continue
            except ApiError as exc:
                # ApiError is a bare @dataclass(Exception) with no Exception.__init__() call
                # (see services/errors.py) -- str(exc) renders the raw
                # (message, status_code, code) args tuple, not the message text, which is
                # exactly the operator-facing clarity gap this branch exists to close.
                # `exc.code` (e.g. "digest_conflict") is a stable, actionable outcome label --
                # keep it in both the log and the metric so an operator/dashboard sees the
                # SAME word, not a generic "failed" that hides why.
                #
                # Alpha incident (2026-10-03): this branch logged `extra={"error_code": ...,
                # "error": exc.message, ...}` ONLY -- never embedded in the message string
                # itself. `main()` wires nothing but `logging.basicConfig()` (default format
                # "%(levelname)s:%(name)s:%(message)s"), which silently drops every `extra`
                # key from the rendered line: `caplog` (this module's own test suite) sees
                # `extra` regardless of formatter, masking the gap in CI, but a plain
                # `kubectl logs` tail (this Job's only real-world observability) rendered a
                # bare "core-bundle-seeder: bundle failed" with NO app_id, no code, no
                # message -- see test_run_reports_a_clear_digest_conflict_and_nonzero_exit's
                # updated docstring. The app_id/version/code/message are therefore put
                # directly in the message string itself, same fix already applied to the
                # generic Exception branch below. `exc_info=True` attaches the real
                # traceback to the rendered line too (Python's logging renders a record's
                # traceback unconditionally when `exc_info` is set, independent of the
                # format string).
                recoverable = exc.code in RECOVERABLE_API_ERROR_CODES
                log_fn = logger.warning if recoverable else logger.error
                verb = "skipped (recoverable conflict, gh-576)" if recoverable else "failed"
                log_fn(
                    f"core-bundle-seeder: bundle {verb} "
                    f"({entry.app_id}@{entry.version}, {exc.code}): {exc.message}",
                    extra={
                        "app_id": entry.app_id,
                        "version": entry.version,
                        "error_code": exc.code,
                        "status_code": exc.status_code,
                        "error": exc.message,
                    },
                    exc_info=True,
                )
                if recoverable:
                    # Never counted in `failures` -- see RECOVERABLE_API_ERROR_CODES's own
                    # docstring. Still surfaced in the final summary (never silent).
                    skipped_conflicts.append(f"{entry.app_id}@{entry.version} ({exc.code})")
                    counter.add(1, {"app_id": entry.app_id, "outcome": "skipped_" + exc.code})
                else:
                    # This is fail-closed by construction: the exception already means no DB
                    # write happened for this entry, and `failures += 1` guarantees a
                    # non-zero process exit code (see `_run()`'s own `return 1 if failures
                    # else 0`) -- a seeding run that hits this branch must never be reported
                    # as a clean success.
                    counter.add(1, {"app_id": entry.app_id, "outcome": exc.code.lower()})
                    failures += 1
                continue
            except Exception as exc:  # noqa: BLE001 -- one bundle's failure must not abort the batch or hide the exit code
                # regression: this branch was the actual alpha !ping blocker -- a boto3
                # ClientError (bad S3 credentials / bucket-grant mismatch) landed here and
                # rendered as a bare "core-bundle-seeder: bundle failed" with NO detail: this
                # module's own `main()` wires only `logging.basicConfig()` (default format
                # "%(levelname)s:%(name)s:%(message)s"), which silently drops every `extra`
                # key from the rendered line -- `extra=` still reaches a structured consumer
                # (e.g. pytest's `caplog`) via the LogRecord's attributes, but a plain
                # `kubectl logs` tail (this Job's only real-world observability today) never
                # sees them. The exception's type AND message are therefore put directly in
                # the message string itself -- sanitized by construction: `str(exc)` on a
                # botocore ClientError/any stdlib exception never embeds the S3 secret key
                # (boto3 never puts credentials in an exception message), only the operation,
                # bucket/key, and the server's error code. `exc_info=True` attaches the full
                # traceback to the rendered line too, not only to `extra`.
                logger.error(
                    f"core-bundle-seeder: bundle failed ({type(exc).__name__}: {exc})",
                    extra={
                        "app_id": entry.app_id,
                        "version": entry.version,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                    exc_info=True,
                )
                counter.add(1, {"app_id": entry.app_id, "outcome": "failed"})
                failures += 1
                continue

            for result in results:
                logger.info(
                    "core-bundle-seeder: result",
                    extra={
                        "app_id": result.app_id,
                        "version": result.version,
                        "outcome": result.outcome,
                        "detail": result.detail,
                    },
                )
                counter.add(1, {"app_id": result.app_id, "outcome": result.outcome})
                seeded += 1

        # Reconcile-uninstall sweep (see `reconcile_removed_core_bundles()`'s own docstring) --
        # runs AFTER every catalog entry above has been seeded/no-op'd, so `catalog_app_ids`
        # reflects this run's full, current catalog. A failure in the sweep itself (not a
        # single row) must still surface in the final summary and exit code, never silently
        # swallowed -- it is reported exactly like any other bundle failure above.
        catalog_app_ids = frozenset(entry.app_id for entry in entries)
        reconcile_results: list[SeedResult] = []
        try:
            reconcile_results, reconcile_failures = await reconcile_removed_core_bundles(
                install_dal, catalog_app_ids=catalog_app_ids
            )
        except Exception as exc:  # noqa: BLE001 -- the sweep itself must not abort the summary/exit code
            logger.error(
                "core-bundle-seeder: reconcile-uninstall sweep failed "
                f"({type(exc).__name__}: {exc})",
                extra={"error_type": type(exc).__name__, "error": str(exc)},
                exc_info=True,
            )
            counter.add(1, {"app_id": "reconcile", "outcome": "failed"})
            failures += 1
        else:
            failures += reconcile_failures
            for result in reconcile_results:
                counter.add(1, {"app_id": result.app_id, "outcome": result.outcome})
                seeded += 1

        logger.info(
            "core-bundle-seeder: summary "
            f"(examined={len(connections) + len(entries)}, results={seeded}, "
            f"failures={failures}, skipped_conflicts={skipped_conflicts}, "
            f"reconcile_uninstalled={len(reconcile_results)})",
            extra={
                "connections_examined": len(connections),
                "bundles_examined": len(entries),
                "results": seeded,
                "failures": failures,
                "skipped_conflicts": skipped_conflicts,
                "reconcile_uninstalled": len(reconcile_results),
            },
        )
    finally:
        await install_dal.close()

    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point -- returns a process exit code (0 = every bundle seeded/no-op'd cleanly)."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument(
        "--bundles-dir",
        type=Path,
        default=Path(os.getenv("CORE_BUNDLES_DIR", DEFAULT_BUNDLES_DIR)),
        help="Directory containing the catalog + manifests + .wasm artifacts.",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help=f"Catalog file path (default: <bundles-dir>/{DEFAULT_CATALOG_FILENAME}).",
    )
    args = parser.parse_args(argv)

    catalog_path: Path = args.catalog or (args.bundles_dir / DEFAULT_CATALOG_FILENAME)
    return asyncio.run(_run(args.bundles_dir, catalog_path))


if __name__ == "__main__":  # pragma: no cover - script-execution guard, never hit under pytest
    sys.exit(main())
