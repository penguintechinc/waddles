"""Service layer for the App Bundle 3-tier lifecycle -- install/available/activate.

Wires the C3 tables (``app_catalog`` / ``app_tenant_availability`` /
``app_activations``, ``config/postgres/migrations/069_app_bundle_tiers.sql``,
see ``docs/plans/2026-08-31-app-bundle-sdk-design.md`` Sec5.1) into real
DB-backed reads/writes for ``blueprints/v1/marketplace_lifecycle.py``. The
subset invariant ``activated <= available <= installed`` is enforced here at
write time via ``flask_core.app_installations_db``'s
``check_availability_insert_allowed``/``check_activation_insert_allowed`` --
raised :class:`~flask_core.app_installations_db.AppTierError` is converted to
a 409 :class:`ApiError` below (see ``_from_tier_error``).

Manifest/registry note: ``app_catalog`` persists the columns a bundle
listing needs, but the process-wide :class:`~flask_core.app_registry.AppRegistry`
singleton (what ``flask_core.app_binding.resolve_apps`` actually reads) is
in-memory only -- it resets on every hub-api restart. ``ensure_registered()``
below is the self-healing bridge: any code path that needs a manifest out of
the registry (activate, resolve) calls it first, and it lazily
re-``parse_manifest``s + registers straight from the ``app_catalog`` row if
the registry doesn't already have it. ``install_bundle()`` also registers
eagerly at install time so the common case (no restart in between) never
takes the lazy path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any

from flask_core.app_binding import detect_conflict, resolve_apps
from flask_core.app_installations_db import (
    AppTierError,
    DBInstallationLookup,
    check_activation_insert_allowed,
    check_availability_insert_allowed,
)
from flask_core.app_manifest import AppManifest, ManifestError, parse_manifest
from flask_core.app_registry import AppRegistry, RegistryError, get_registry

from services.errors import ApiError, conflict, not_found

#: Statuses that still count as "installed" for downstream tier writes --
#: a `yanked`/`deprecated` app_id remains a valid app_catalog FK target (its
#: history must not vanish), but should not be newly opted INTO by a tenant
#: that hasn't already done so. `active` is the only status a fresh
#: `make_available()` call accepts; see `make_available()`'s own docstring.
_ACTIVE_STATUS = "active"


def _from_tier_error(exc: AppTierError) -> ApiError:
    """Convert a `flask_core` `AppTierError` (subset-invariant violation) into a 409 `ApiError`."""
    return conflict(exc.detail)


def _manifest_dict_from_row(row: Any) -> dict[str, Any]:
    """Rebuild a `parse_manifest()`-shaped dict from a persisted `app_catalog` row.

    Inverse of `install_bundle()`'s own write below. `stages`/`surfaces`
    are not persisted by migration 069/070 (no column for them) -- omitted
    here, which `parse_manifest` accepts (only the six `_REQUIRED_STR_FIELDS`
    are mandatory); a manifest reconstructed this way has empty
    `stage_specs`/`surfaces`, which `resolve_apps`/`detect_conflict` never
    read (they only need `app_id`/`feature`/`compatible_with`/
    `incompatible_with`/`is_default`).
    """
    platform_compat = row.platform_compatibility or {}
    return {
        "app_id": row.app_id,
        "name": row.name or row.app_id,
        "version": row.manifest_version,
        "feature": row.feature,
        "module": row.module,
        "provider": row.provider,
        "execution_model": row.execution_model,
        "is_default": bool(row.is_default),
        "compatible_with": list(row.compatible_with or []),
        "incompatible_with": list(row.incompatible_with or []),
        "platform_compatibility": {
            "tested_with": platform_compat.get("tested_with", ""),
            "min_version": platform_compat.get("min_version"),
            "max_version": platform_compat.get("max_version"),
        },
        "author": row.author,
        "license": row.license,
        "source_url": row.source_url,
        "alternative_to": list(row.alternative_to or []),
        "homepage_url": row.homepage_url,
        "notice": row.notice,
        "category": row.category,
    }


async def ensure_registered(
    dal: Any, app_id: str, *, registry: AppRegistry | None = None
) -> AppManifest:
    """Return the registered `AppManifest` for `app_id`, lazily registering it from the DB.

    See this module's own docstring -- self-healing bridge between the
    persisted `app_catalog` row and the in-memory `AppRegistry` singleton.
    Raises `ApiError` 404 if `app_id` has no `app_catalog` row at all.
    """
    reg = registry if registry is not None else get_registry()
    try:
        return reg.get(app_id)
    except RegistryError:
        pass

    row = dal(dal.app_catalog.app_id == app_id).select().first()
    if row is None:
        raise not_found(f"Bundle {app_id!r} is not installed")

    manifest = parse_manifest(_manifest_dict_from_row(row))
    try:
        reg.register(manifest)
    except RegistryError:
        # Raced with a concurrent lazy-registration of the same app_id --
        # both callers derive the identical manifest from the same DB row,
        # so the winner is equally correct; just return it.
        return reg.get(app_id)
    return manifest


# ---------------------------------------------------------------------------
# GLOBAL tier: app_catalog (install / uninstall / list)
# ---------------------------------------------------------------------------


async def install_bundle(
    dal: Any, *, manifest_data: dict[str, Any], registry: AppRegistry | None = None
) -> Any:
    """Validate `manifest_data`, register it, and insert its `app_catalog` row.

    Raises `ApiError` 400 (bad manifest shape, `ManifestError`) or 409
    (already installed -- both a DB-level duplicate `app_id` check AND the
    in-memory registry's own `RegistryError` duplicate check are honored,
    since the registry resets across restarts but the DB row does not).
    """
    try:
        manifest = parse_manifest(manifest_data)
    except ManifestError as exc:
        raise ApiError(f"{exc.reason}: {exc}", 400, "BAD_REQUEST") from exc

    existing = dal(dal.app_catalog.app_id == manifest.app_id).count()
    if existing > 0:
        raise conflict(f"Bundle {manifest.app_id!r} is already installed")

    reg = registry if registry is not None else get_registry()
    try:
        reg.register(manifest)
    except RegistryError as exc:
        raise conflict(str(exc)) from exc

    dal.app_catalog.insert(
        app_id=manifest.app_id,
        name=manifest.name,
        manifest_version=manifest.version,
        module=manifest.module,
        feature=manifest.feature,
        provider=manifest.provider,
        execution_model=manifest.execution_model,
        is_default=manifest.is_default,
        compatible_with=list(manifest.compatible_with),
        incompatible_with=list(manifest.incompatible_with),
        platform_compatibility={
            "tested_with": manifest.platform_compatibility.tested_with,
            "min_version": manifest.platform_compatibility.min_version,
            "max_version": manifest.platform_compatibility.max_version,
        },
        status=_ACTIVE_STATUS,
        installed_at=datetime.now(UTC),
        author=manifest.author,
        license=manifest.license,
        license_review_required=manifest.license_requires_review,
        source_url=manifest.source_url,
        alternative_to=list(manifest.alternative_to),
        homepage_url=manifest.homepage_url,
        notice=manifest.notice,
        category=manifest.category,
    )
    dal.commit()
    row = dal(dal.app_catalog.app_id == manifest.app_id).select().first()
    if row is None:  # pragma: no cover - insert+commit guarantees this
        raise ApiError("Bundle insert failed", 500, "INTERNAL_ERROR")
    return row


async def uninstall_bundle(dal: Any, *, app_id: str) -> None:
    """Soft-delete: set `app_catalog.status = 'yanked'`. Raises 404 if unknown."""
    row = dal(dal.app_catalog.app_id == app_id).select().first()
    if row is None:
        raise not_found(f"Bundle {app_id!r} is not installed")
    dal(dal.app_catalog.app_id == app_id).update(status="yanked")
    dal.commit()


async def list_installed(
    dal: Any,
    *,
    module: str | None,
    feature: str | None,
    provider: str | None,
    status: str | None,
    category: str | None = None,
    page: int,
    limit: int,
) -> tuple[list[Any], int, int]:
    """List `app_catalog` rows with optional filters + pagination.

    `category` (migration 0026, e.g. `alternatives`) is the marketplace
    listing category -- lets a browsing tenant filter down to bundles
    positioned as an alternative to a feature/product they don't want to
    self-host, paired with each row's own `alternative_to` list.
    """
    # `app_catalog` has no surrogate `id` column (`primarykey=["app_id"]`,
    # see `services/schema.py::bind_lifecycle_tables`) -- the usual
    # `dal.<table>.id > 0` always-true base query (see `platform_service.py`)
    # isn't available; `app_id` is `notnull=True`, so `!= None` is
    # equivalently always-true here.
    query = dal.app_catalog.app_id != None  # noqa: E711
    if module:
        query &= dal.app_catalog.module == module
    if feature:
        query &= dal.app_catalog.feature == feature
    if provider:
        query &= dal.app_catalog.provider == provider
    if status:
        query &= dal.app_catalog.status == status
    if category:
        query &= dal.app_catalog.category == category

    total = dal(query).count()
    page = max(1, page)
    limit = min(100, max(1, limit))
    offset = (page - 1) * limit
    rows = dal(query).select(orderby=dal.app_catalog.app_id, limitby=(offset, offset + limit))
    total_pages = (total + limit - 1) // limit if total else 0
    return list(rows), total, total_pages


# ---------------------------------------------------------------------------
# TENANT tier: app_tenant_availability (make-available / make-unavailable / list)
# ---------------------------------------------------------------------------


def _guarded_upsert_availability_sync(
    dal: Any, *, tenant_id: int, app_id: str, config_defaults: dict[str, Any]
) -> Any:
    """The atomic make-available unit -- see `make_available()`'s own docstring for why this shape.

    UPDATE-first, INSERT-if-missing, then a re-read, all inside ONE
    executor job (no `await` anywhere in this function) submitted as a
    single `loop.run_in_executor()` call -- the fix for the same class of
    bug `services/token_billing_service.py`'s module docstring documents
    in detail: the old `async with async_dal.transaction_async(): await
    async_dal.update_async(...)` / `await async_dal.insert_async(...)`
    shape used SEPARATE executor submissions with `await` points between
    them, so `transaction_async()`'s commit/rollback (acting on `dal`'s
    single ambient, connection-scoped transaction, not one scoped to this
    call's own writes) could be interleaved with -- and have its write
    wiped by -- a concurrently in-flight sibling call's rollback. Bundling
    the whole read-modify-write + commit into one non-preemptible executor
    job removes that window entirely for this process's own concurrent
    callers.
    """
    updated = dal(
        (dal.app_tenant_availability.tenant_id == tenant_id)
        & (dal.app_tenant_availability.app_id == app_id)
    ).update(available=True, config_defaults=config_defaults)
    if not updated:
        dal.app_tenant_availability.insert(
            tenant_id=tenant_id,
            app_id=app_id,
            available=True,
            config_defaults=config_defaults,
        )
    dal.commit()
    return (
        dal(
            (dal.app_tenant_availability.tenant_id == tenant_id)
            & (dal.app_tenant_availability.app_id == app_id)
        )
        .select()
        .first()
    )


async def make_available(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    app_id: str,
    config_defaults: dict[str, Any] | None,
) -> Any:
    """Make an installed bundle available to `tenant_id`. Upserts on `(tenant_id, app_id)`.

    Enforces `available <= installed` via `check_availability_insert_allowed`
    (409 if `app_id` has no `app_catalog` row) PLUS an additional
    `status == 'active'` check not covered by that shared invariant helper
    (a `yanked`/`deprecated` app_id still satisfies "exists in app_catalog"
    -- see this module's own docstring on `_ACTIVE_STATUS`) -- 409 if the
    bundle was uninstalled.
    """
    try:
        await check_availability_insert_allowed(dal, app_id)
    except AppTierError as exc:
        raise _from_tier_error(exc) from exc

    catalog_row = dal(dal.app_catalog.app_id == app_id).select().first()
    if catalog_row is None or catalog_row.status != _ACTIVE_STATUS:
        raise conflict(f"Bundle {app_id!r} is not currently installed/active")

    defaults = config_defaults if config_defaults is not None else {}
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        async_dal.executor,
        partial(
            _guarded_upsert_availability_sync,
            dal,
            tenant_id=tenant_id,
            app_id=app_id,
            config_defaults=defaults,
        ),
    )


def _guarded_set_unavailable_sync(dal: Any, *, tenant_id: int, app_id: str) -> int:
    """The atomic make-unavailable unit -- write + commit in ONE executor job.

    Same rationale as `_guarded_upsert_availability_sync` above: even a
    single `update_async()` call wrapped in `async with
    async_dal.transaction_async(): ...` was still 3 SEPARATE executor
    submissions (the context manager's own begin no-op, the update, and
    its commit) with `await` points between them -- interleaving-vulnerable
    the same way, just with a smaller window. Bundling the update and its
    commit into one non-preemptible callable closes it.
    """
    updated = dal(
        (dal.app_tenant_availability.tenant_id == tenant_id)
        & (dal.app_tenant_availability.app_id == app_id)
    ).update(available=False)
    dal.commit()
    return int(updated)


async def make_unavailable(async_dal: Any, dal: Any, *, tenant_id: int, app_id: str) -> None:
    """Soft-disable: set `app_tenant_availability.available = False`. Raises 404 if no such row.

    Existence check goes through `async_dal.count_async` (not a sync
    `dal(...).count()`) -- see `make_available()`'s own comment on why: a
    prior `make_available()` write on a different pool worker thread may
    still only be visible on `async_dal`'s own connection.
    """
    existing_query = (dal.app_tenant_availability.tenant_id == tenant_id) & (
        dal.app_tenant_availability.app_id == app_id
    )
    existing = await async_dal.count_async(existing_query)
    if existing == 0:
        raise not_found(f"Bundle {app_id!r} is not available for this tenant")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        async_dal.executor,
        partial(_guarded_set_unavailable_sync, dal, tenant_id=tenant_id, app_id=app_id),
    )


async def list_available(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    module: str | None,
    feature: str | None,
    available: bool | None,
    page: int,
    limit: int,
) -> tuple[list[Any], int, int]:
    """List `app_tenant_availability` rows (joined to `app_catalog`) for `tenant_id`."""
    query = dal.app_tenant_availability.tenant_id == tenant_id
    if available is not None:
        query &= dal.app_tenant_availability.available == available
    if module:
        query &= dal.app_catalog.module == module
    if feature:
        query &= dal.app_catalog.feature == feature
    query &= dal.app_tenant_availability.app_id == dal.app_catalog.app_id

    # async_dal.count_async, not sync dal(query).count() -- see
    # make_unavailable()'s own comment: a just-committed write from a
    # different pool worker thread needs the same async connection to be
    # reliably visible immediately.
    total = await async_dal.count_async(query)
    page = max(1, page)
    limit = min(100, max(1, limit))
    offset = (page - 1) * limit
    rows = await async_dal.select_async(
        dal(query),
        dal.app_tenant_availability.ALL,
        dal.app_catalog.ALL,
        left=dal.app_catalog.on(dal.app_tenant_availability.app_id == dal.app_catalog.app_id),
        orderby=dal.app_tenant_availability.id,
        limitby=(offset, offset + limit),
    )
    total_pages = (total + limit - 1) // limit if total else 0
    return list(rows), total, total_pages


# ---------------------------------------------------------------------------
# COMMUNITY tier: app_activations (activate / deactivate / list)
# ---------------------------------------------------------------------------


def _guarded_upsert_activation_sync(
    dal: Any,
    *,
    community_id: int,
    tenant_id: int,
    app_id: str,
    config: dict[str, Any],
    activated_by: int,
) -> Any:
    """The atomic activate unit -- see `_guarded_upsert_availability_sync`'s docstring for why.

    Same UPDATE-first/INSERT-if-missing + commit, all inside ONE executor
    job -- `activated_by` is intentionally only set on the INSERT branch,
    matching the pre-fix code's own behavior (a re-activation of an
    already-known `(community_id, app_id)` pair does not change who
    originally activated it).
    """
    updated = dal(
        (dal.app_activations.community_id == community_id) & (dal.app_activations.app_id == app_id)
    ).update(enabled=True, config=config, updated_at=datetime.now(UTC))
    if not updated:
        dal.app_activations.insert(
            community_id=community_id,
            tenant_id=tenant_id,
            app_id=app_id,
            enabled=True,
            config=config,
            activated_by=activated_by,
        )
    dal.commit()
    return (
        dal(
            (dal.app_activations.community_id == community_id)
            & (dal.app_activations.app_id == app_id)
        )
        .select()
        .first()
    )


async def activate_bundle(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    tenant_id: int,
    app_id: str,
    config: dict[str, Any] | None,
    activated_by: int,
    registry: AppRegistry | None = None,
) -> Any:
    """Activate `app_id` for `community_id`. Upserts on `(community_id, app_id)`.

    Enforces `activated <= available` via `check_activation_insert_allowed`
    (409 if not available to `tenant_id`), then a coexistence check
    (`flask_core.app_binding.detect_conflict`, design doc Sec7.3) against
    every OTHER currently-enabled activation for this community -- 409
    naming the conflicting `app_id` if `candidate` cannot coexist with an
    already-active App.
    """
    try:
        await check_activation_insert_allowed(dal, tenant_id, app_id)
    except AppTierError as exc:
        raise _from_tier_error(exc) from exc

    candidate = await ensure_registered(dal, app_id, registry=registry)

    active_rows = await async_dal.select_async(
        dal(
            (dal.app_activations.community_id == community_id)
            & (dal.app_activations.enabled == True)  # noqa: E712
            & (dal.app_activations.app_id != app_id)
        ),
        dal.app_activations.app_id,
    )
    active_manifests = [
        await ensure_registered(dal, r.app_id, registry=registry) for r in active_rows
    ]
    conflicting = detect_conflict(candidate, active_manifests)
    if conflicting is not None:
        raise conflict(f"Bundle {app_id!r} conflicts with already-active bundle {conflicting!r}")

    payload = config if config is not None else {}
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        async_dal.executor,
        partial(
            _guarded_upsert_activation_sync,
            dal,
            community_id=community_id,
            tenant_id=tenant_id,
            app_id=app_id,
            config=payload,
            activated_by=activated_by,
        ),
    )


def _guarded_set_deactivated_sync(dal: Any, *, community_id: int, app_id: str) -> int:
    """The atomic deactivate unit -- write + commit in ONE executor job.

    Same rationale as `_guarded_set_unavailable_sync` above.
    """
    updated = dal(
        (dal.app_activations.community_id == community_id) & (dal.app_activations.app_id == app_id)
    ).update(enabled=False, updated_at=datetime.now(UTC))
    dal.commit()
    return int(updated)


async def deactivate_bundle(async_dal: Any, dal: Any, *, community_id: int, app_id: str) -> None:
    """Soft-disable: set `app_activations.enabled = False`. Raises 404 if no such row.

    Same `async_dal.count_async` rationale as `make_unavailable()`'s own
    comment.
    """
    existing_query = (dal.app_activations.community_id == community_id) & (
        dal.app_activations.app_id == app_id
    )
    existing = await async_dal.count_async(existing_query)
    if existing == 0:
        raise not_found(f"Bundle {app_id!r} is not activated for this community")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        async_dal.executor,
        partial(_guarded_set_deactivated_sync, dal, community_id=community_id, app_id=app_id),
    )


async def list_activated(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    module: str | None,
    feature: str | None,
    enabled: bool | None,
    page: int,
    limit: int,
) -> tuple[list[Any], int, int]:
    """List `app_activations` rows (joined to `app_catalog`) for `community_id`."""
    query = dal.app_activations.community_id == community_id
    if enabled is not None:
        query &= dal.app_activations.enabled == enabled
    if module:
        query &= dal.app_catalog.module == module
    if feature:
        query &= dal.app_catalog.feature == feature
    query &= dal.app_activations.app_id == dal.app_catalog.app_id

    total = await async_dal.count_async(query)
    page = max(1, page)
    limit = min(100, max(1, limit))
    offset = (page - 1) * limit
    rows = await async_dal.select_async(
        dal(query),
        dal.app_activations.ALL,
        dal.app_catalog.ALL,
        left=dal.app_catalog.on(dal.app_activations.app_id == dal.app_catalog.app_id),
        orderby=dal.app_activations.id,
        limitby=(offset, offset + limit),
    )
    total_pages = (total + limit - 1) // limit if total else 0
    return list(rows), total, total_pages


# ---------------------------------------------------------------------------
# Resolved active set (community -> tenant -> default ladder, coexistence)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ResolvedConflict:
    """One detected coexistence conflict in a community's resolved app set."""

    app_id: str
    conflicts_with_app_id: str


async def resolve_community_bundles(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    community_id: int,
    registry: AppRegistry | None = None,
) -> tuple[list[AppManifest], list[ResolvedConflict]]:
    """Resolve every enabled/available App across every Feature visible to `community_id`.

    Iterates the distinct `feature`s present in `app_catalog` rows this
    tenant/community can see (joined through `app_tenant_availability`),
    calling `flask_core.app_binding.resolve_apps` once per Feature (its own
    contract is single-Feature) and flattening the union. Conflicts are then
    detected pairwise across the FULL flattened set (not just within one
    Feature) via `detect_conflict`, since `incompatible_with` may name an
    app_id implementing a different Feature entirely (design doc Sec7.3
    places no same-Feature restriction on it).
    """
    reg = registry if registry is not None else get_registry()
    lookup = DBInstallationLookup(dal)

    feature_rows = await async_dal.select_async(
        dal(
            (dal.app_tenant_availability.tenant_id == tenant_id)
            & (dal.app_tenant_availability.app_id == dal.app_catalog.app_id)
        ),
        dal.app_catalog.feature,
        distinct=True,
    )
    features = sorted({r.feature for r in feature_rows})

    resolved: list[AppManifest] = []
    seen: set[str] = set()
    for feature in features:
        try:
            apps = await resolve_apps(
                feature,
                tenant=str(tenant_id),
                community=community_id,
                installations=lookup,
                registry=reg,
            )
        except Exception:  # noqa: BLE001, S112 - no binding/default for this feature; skip
            continue
        for app in apps:
            if app.app_id in seen:
                continue
            seen.add(app.app_id)
            resolved.append(app)

    conflicts: list[ResolvedConflict] = []
    for candidate in resolved:
        others = [a for a in resolved if a.app_id != candidate.app_id]
        conflicting = detect_conflict(candidate, others)
        if conflicting is not None:
            conflicts.append(
                ResolvedConflict(app_id=candidate.app_id, conflicts_with_app_id=conflicting)
            )

    return resolved, conflicts
