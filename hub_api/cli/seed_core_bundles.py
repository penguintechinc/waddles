"""Core-bundle seeder -- activates first-party `waddles.core.*` App Bundles at deploy time.

Run as `python3 -m cli.seed_core_bundles` from hub-api's own `/app` WORKDIR (mirrors this
repo's existing top-level-module convention -- `hub_api/conftest.py` inserts `hub_api/` itself
onto `sys.path` so `services.*`/`blueprints.*` import as top-level packages, not
`hub_api.services.*`; this CLI's own `cli/` package sits alongside them the same way, rather
than under a `hub_api.` namespace nothing else in this codebase uses). In-cluster only, via a
Helm post-install/post-upgrade hook Job (`k8s/helm/waddlebot/templates/core-bundle-seeder-
job.yaml`) -- no HTTP endpoint, no JWT, hub-api's own DB/MinIO/Valkey env (`HubAPIConfig.
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
from pathlib import Path
from typing import Any

import yaml
from penguin_dal import AsyncDB

from config import HubAPIConfig
from services import vendor_bundle_authz
from services.bundle_approval_service import activate_for_community, install_version_globally
from services.bundle_install_dal import build_install_dal, raw_sql_write
from services.bundle_manifest_v2 import BundleManifestV2, parse_bundle_manifest_v2
from services.bundle_telemetry import get_meter
from services.bundle_version_service import create_version, process_prebuilt_component
from services.errors import ApiError
from services.ingest_source_service import ensure_ingest_source
from services.tenant_app_availability_service import set_available

logger = logging.getLogger("waddles.hub_api.core_bundle_seeder")

#: The SYSTEM actor string persisted to `app_install_approvals.approval_source`
#: (migration 0026) -- see this module's own docstring.
SYSTEM_ACTOR = "system:core-seeder"

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


@dataclass(slots=True, frozen=True)
class ActivationTarget:
    """One `(tenant, community)` pair to seed.

    `community_id=None` means TENANT-tier availability only (no
    COMMUNITY-tier activation for this target; see the 3-tier split,
    `services/bundle_approval_service.py`'s own module docstring).
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
        extra={"app_id": manifest.app_id, "module": manifest.module},
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
                f"{entry.app_id}@{entry.version} is already published with a different digest "
                f"({existing_row.artifact_digest!r} != {expected_digest!r}) -- bump the "
                "catalog's version alongside the artifact, never republish under the same version",
                409,
                "digest_conflict",
            )
        return int(existing_row.id)

    try:
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
    except ApiError as exc:
        if exc.code != "CONFLICT":
            raise
        # A previous run created the app_version_uploads row but crashed before
        # process_prebuilt_component() published it -- resuming from an arbitrary
        # mid-FSM state is out of scope (see module docstring's idempotency note);
        # fail loudly with a clear, actionable message rather than silently
        # retrying a transition the state machine may now refuse.
        raise ApiError(
            f"{entry.app_id}@{entry.version} already has an app_version_uploads row that never "
            "reached PUBLISHED -- a previous seeder run likely crashed mid-publish; inspect and "
            "clear that row manually before re-running",
            exc.status_code,
            "stalled_core_bundle_upload",
        ) from exc

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
            # TIER-2 only for this target -- catalog config declares no
            # community to activate in (see `ActivationTarget`'s own docstring).
            continue

        if await _already_active(
            install_dal,
            app_id=entry.app_id,
            tenant_id=tenant_id,
            community_id=target.community_id,
            version_id=version_id,
        ):
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
        results.append(
            SeedResult(
                entry.app_id,
                entry.version,
                "activated",
                f"tenant={target.tenant_slug!r} community_id={target.community_id!r}",
            )
        )
    return results


async def _run(bundles_dir: Path, catalog_path: Path) -> int:
    config = HubAPIConfig.from_env()
    install_dal = await build_install_dal(config.database_url, pool_size=2)
    counter = get_meter().create_counter(
        "waddles_hub_core_bundle_seed_total",
        description="core-bundle-seeder bundle-activation attempts, by outcome",
    )

    failures = 0
    seeded = 0
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
                    "core-bundle-seeder: refused",
                    extra={"app_id": entry.app_id, "version": entry.version, "reason": str(exc)},
                )
                counter.add(1, {"app_id": entry.app_id, "outcome": "refused"})
                failures += 1
                continue
            except Exception as exc:  # noqa: BLE001 -- one bundle's failure must not abort the batch or hide the exit code
                logger.error(
                    "core-bundle-seeder: bundle failed",
                    extra={"app_id": entry.app_id, "version": entry.version, "error": str(exc)},
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

        logger.info(
            "core-bundle-seeder: summary",
            extra={
                "connections_examined": len(connections),
                "bundles_examined": len(entries),
                "results": seeded,
                "failures": failures,
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
