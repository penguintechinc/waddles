"""AUTO-BIND: record + provision which `ingest_sources` an approved app actually reads.

Root problem this fixes: svc-process consumes events from
`waddles:t:{tenant}:c:{community|_tenant}:src:{platform}:{source_id}:events`
(penguin-spine `Scope::source_stream`) with consumer group = `app_id`, but
until this module existed hub-api had no record of which sources a given
installed app should be granted a consumer group on, and
`bundle_version_service.process_prebuilt_component()` provisioned a
`process`-stage consumer group on a key
(`waddles:t:{tenant}:c:_tenant:app:{app_id}:process`) nobody ever reads.

This module is split into two steps, run from two different places in
`bundle_approval_service.py`, deliberately never merged into one:

  1. `sync_bindings()` -- a pure DB write, run INSIDE
     `_write_approval_and_activate()`'s single `engine.begin()` transaction
     (same atomicity guarantee as the approval + activation writes: a
     failure anywhere in that transaction rolls back the bindings too).
     Replaces (delete-then-insert) `app_id`'s bindings for
     `(tenant_id, community_id)` on every approval, including
     re-approval -- a narrowed manifest or a since-removed
     `ingest_sources` row never leaves a stale binding behind. An app
     consuming a platform with zero configured `ingest_sources` rows
     binds nothing for that platform and logs a WARN, not an error --
     that is a legitimate (if incomplete) install, not a failure.

  2. `provision_source_stream_groups()` -- a Valkey side effect
     (`ensure_group` per bound source stream), run AFTER the transaction
     in step 1 commits. `ensure_group` has no rollback, so it must never
     run inside a DB transaction that might still abort -- provisioning a
     consumer group for a binding whose DB row then gets rolled back
     would leave a dangling, ungoverned consumer group.

Matching contract (Rust side, `core/svc_process`): the stream key this
module provisions groups on MUST be byte-identical to `Scope::
source_stream()` (`penguin-libs` `packages/rust-spine/src/scope.rs`) --
built here via `flask_core.stream_pipeline.source_stream_key()`, the
Python mirror of that exact format.
"""

from __future__ import annotations

import logging
from typing import Any

from flask_core.stream_pipeline import source_stream_key
from sqlalchemy import delete, select

from services import valkey_admin_client
from services.bundle_manifest_v2 import BundleManifestV2

logger = logging.getLogger(__name__)

#: Mirrors `bundle_approval_service.TENANT_WIDE_COMMUNITY_SENTINEL` --
#: `app_source_bindings.community_id` (migration 0025) uses the identical
#: sentinel convention as `app_active_versions` (migration 0022):
#: `communities.id` is a real SERIAL starting at 1, so 0 never collides
#: with a real row. Kept as this module's own constant (not imported from
#: `bundle_approval_service`) to avoid a circular import -- that module
#: imports this one, not the reverse.
TENANT_WIDE_COMMUNITY_SENTINEL = 0


async def sync_bindings(
    conn: Any,
    *,
    tenant_id: int,
    community_id: int | None,
    app_id: str,
    manifest: BundleManifestV2,
    bindings_table: Any,
    ingest_sources_table: Any,
) -> dict[str, list[str]]:
    """Replace `app_id`'s source bindings for `(tenant_id, community_id)`, inside the caller's tx.

    For every distinct platform in `manifest.consumes`, binds every
    matching, enabled `ingest_sources` row for this tenant/community and
    that platform (a `ConsumeRule.source_id` filter, when present, is
    NOT applied here -- the manifest's declared platform is the binding
    grain; per-source filtering is the runtime's own concern, spec
    Sec6.4.3). `community_id=None` (tenant-wide) matches only tenant-wide
    `ingest_sources` rows (`community_id IS NULL`) -- a tenant-wide
    install is not scoped to any one community's sources.

    Args:
        conn: The SQLAlchemy `AsyncConnection` already inside
            `install_dal.engine.begin()` -- every statement here commits
            or rolls back together with the caller's approval/activation
            writes.
        tenant_id: The approving call's tenant (never client-supplied
            beyond what `approve_version()` already validated).
        community_id: `None` for a tenant-wide approval, else a
            tenant-owned `communities.id` (already validated by
            `bundle_approval_service._validate_community_tenant`).
        app_id: The app being approved.
        manifest: The approved version's parsed manifest.
        bindings_table: `install_dal.metadata.tables["app_source_bindings"]`.
        ingest_sources_table: `install_dal.metadata.tables["ingest_sources"]`.

    Returns:
        `{platform: [source_id, ...]}` for every binding just written --
        the caller passes this to `provision_source_stream_groups()`
        AFTER the transaction commits.
    """
    platforms = sorted({rule.platform for rule in manifest.consumes})
    active_community_id = TENANT_WIDE_COMMUNITY_SENTINEL if community_id is None else community_id

    await conn.execute(
        delete(bindings_table).where(
            (bindings_table.c.tenant_id == tenant_id)
            & (bindings_table.c.community_id == active_community_id)
            & (bindings_table.c.app_id == app_id)
        )
    )

    bound: dict[str, list[str]] = {}
    if not platforms:
        return bound

    community_filter = (
        ingest_sources_table.c.community_id.is_(None)
        if community_id is None
        else ingest_sources_table.c.community_id == community_id
    )
    rows = (
        await conn.execute(
            select(ingest_sources_table.c.platform, ingest_sources_table.c.source_id).where(
                (ingest_sources_table.c.tenant_id == tenant_id)
                & community_filter
                & (ingest_sources_table.c.platform.in_(platforms))
                & (ingest_sources_table.c.enabled.is_(True))
            )
        )
    ).all()

    by_platform: dict[str, list[str]] = {}
    for row in rows:
        by_platform.setdefault(row.platform, []).append(row.source_id)

    for platform in platforms:
        source_ids = by_platform.get(platform, [])
        if not source_ids:
            logger.warning(
                "app source binding: no configured ingest sources for consumed platform",
                extra={
                    "app_id": app_id,
                    "tenant_id": tenant_id,
                    "community_id": community_id,
                    "platform": platform,
                },
            )
            continue
        bound[platform] = source_ids
        await conn.execute(
            bindings_table.insert(),
            [
                {
                    "tenant_id": tenant_id,
                    "community_id": active_community_id,
                    "app_id": app_id,
                    "platform": platform,
                    "source_id": source_id,
                }
                for source_id in source_ids
            ],
        )

    logger.info(
        "app source binding: synced",
        extra={
            "app_id": app_id,
            "tenant_id": tenant_id,
            "community_id": community_id,
            "platforms_bound": len(bound),
            "sources_bound": sum(len(v) for v in bound.values()),
        },
    )
    return bound


async def provision_source_stream_groups(
    client: Any,
    *,
    tenant_slug: str,
    community_segment: str | None,
    app_id: str,
    bound: dict[str, list[str]],
) -> None:
    """`ensure_group(group=app_id)` on every bound source's stream key.

    Call ONLY after the transaction `sync_bindings()` ran inside has
    committed -- `ensure_group` is a Valkey side effect with no rollback.

    Args:
        client: A `redis.asyncio.Redis` (real or test double).
        tenant_slug: The tenant's slug segment (never a client-supplied
            value -- resolved from `tenants.slug` by the caller).
        community_segment: The community's `name` segment, or `None` for
            a tenant-wide activation (renders as `_tenant`, matching
            `source_stream_key`'s own convention).
        app_id: The approved app -- also the consumer group name.
        bound: `sync_bindings()`'s own return value.
    """
    for platform, source_ids in bound.items():
        for source_id in source_ids:
            stream_key = source_stream_key(tenant_slug, community_segment, platform, source_id)
            await valkey_admin_client.ensure_group(client, stream=stream_key, group=app_id)
            logger.info(
                "app source binding: consumer group provisioned",
                extra={
                    "app_id": app_id,
                    "tenant": tenant_slug,
                    "community": community_segment,
                    "platform": platform,
                    "source_id": source_id,
                    "stream_key": stream_key,
                },
            )


async def clear_bindings(
    conn: Any,
    *,
    tenant_id: int,
    community_id: int,
    app_id: str,
    bindings_table: Any,
) -> None:
    """Delete every `app_source_bindings` row for `(tenant_id, community_id, app_id)`.

    The teardown half of `sync_bindings()` -- called from
    `bundle_approval_service.deactivate_for_community()` inside the SAME
    transaction as the `app_active_versions` row removal, so a
    rolled-back deactivation never leaves a dangling unbind (or vice
    versa). Unlike `sync_bindings()`, `community_id` here is always a
    real community id (never the tenant-wide sentinel) -- COMMUNITY-tier
    activation no longer writes that sentinel for new rows (see
    `bundle_approval_service.py`'s own module docstring).
    """
    await conn.execute(
        delete(bindings_table).where(
            (bindings_table.c.tenant_id == tenant_id)
            & (bindings_table.c.community_id == community_id)
            & (bindings_table.c.app_id == app_id)
        )
    )
    logger.info(
        "app source binding: cleared",
        extra={"app_id": app_id, "tenant_id": tenant_id, "community_id": community_id},
    )
