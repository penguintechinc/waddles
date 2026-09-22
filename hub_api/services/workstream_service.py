"""hub-api-owned workstream lifecycle -- 1:1 with ingest_sources (spec Sec5.11, Sec6.11, D30).

A workstream is created the moment its ingest source is registered
(`ingest_source_service.create_source()` does this atomically itself,
Decision #18(a) -- see that module, not this one, for the create-time
path) and disabled (never deleted) the moment that source is removed --
`workstream_usage_hourly` rows keep their FK target for the life of the
tenant's usage history even after the source itself is gone (migration
0021's docstring explains the nullable `ingest_source_id` FK).
`create_workstream_for_source` here is the standalone, idempotent
helper for any caller other than `create_source()` itself;
`disable_workstream_for_source`/`get_workstream_for_source` are used by
`delete_source()`.

R52: `ingest_sources`/`workstreams` are this slice's own new tables,
queried through the penguin-dal `install_dal: AsyncDB`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from penguin_dal import AsyncDB


async def create_workstream_for_source(
    install_dal: AsyncDB,
    *,
    ingest_source_id: int,
    tenant_id: int,
    community_id: int | None,
    platform: str,
    source_id: str,
) -> Any:
    """Create the 1:1 workstream for an ingest source, or return the existing one. Idempotent."""
    existing = await install_dal(
        install_dal.workstreams.ingest_source_id == ingest_source_id
    ).select()
    first = existing.first()
    if first is not None:
        return first

    new_id = await install_dal.workstreams.async_insert(
        tenant_id=tenant_id,
        community_id=community_id,
        ingest_source_id=ingest_source_id,
        platform=platform,
        source_id=source_id,
        created_at=datetime.now(UTC),
    )
    return (await install_dal(install_dal.workstreams.id == new_id).select()).first()


async def disable_workstream_for_source(install_dal: AsyncDB, *, ingest_source_id: int) -> None:
    """Set `disabled_at` on the source's workstream. No-op if none exists or it is already disabled.

    Called before the owning `ingest_sources` row is deleted -- never
    after, since the FK is `ON DELETE SET NULL` and this function needs
    `ingest_source_id` to still resolve to the right row.
    """
    rows = await install_dal(
        (install_dal.workstreams.ingest_source_id == ingest_source_id)
        & (install_dal.workstreams.disabled_at == None)  # noqa: E711 - penguin-dal IS NULL operator
    ).select()
    target = rows.first()
    if target is None:
        return
    await install_dal(install_dal.workstreams.id == target.id).update(disabled_at=datetime.now(UTC))


async def get_workstream_for_source(install_dal: AsyncDB, *, ingest_source_id: int) -> Any | None:
    """The workstream row for a given ingest source, or `None` if it has none."""
    rows = await install_dal(install_dal.workstreams.ingest_source_id == ingest_source_id).select()
    return rows.first()
