"""Backfill the `app_commands` registry for bundles activated before the registry existed.

Activation registers commands via `app_command_service.sync_commands`, but every
`app_active_versions` row written before the registry shipped has no `app_commands` rows.
The core-bundle seeder calls `backfill_active_commands` once per run to close that gap.

Ordering contract: each row is synced with `now = row.activated_at`, so a freshly created
registry reproduces the real first-registered-wins order (earliest activation owns a
contested command). `sync_commands` preserves `registered_at` on rows that already exist,
which makes a re-run idempotent and non-reordering.

Lives in its own module (not in `app_command_service`) because it needs
`bundle_approval_service._reparse_trusted`, and that module already imports
`app_command_service` -- importing it from here avoids a circular import.

Logging is PII-free: app_id / community_id / counts only, never command text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from penguin_dal import AsyncDB
from sqlalchemy import select

from services import app_command_service
from services.bundle_approval_service import _reparse_trusted

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class BackfillResult:
    """Denominators of one backfill sweep: rows examined, synced, and per-app failures."""

    examined: int
    synced: int
    failed: tuple[str, ...]


async def backfill_active_commands(
    install_dal: AsyncDB, *, now: datetime | None = None
) -> BackfillResult:
    """Sync `app_commands` for every `app_active_versions` row, isolating per-app failures.

    Each app syncs in its own `engine.begin()` so one corrupt manifest cannot roll back
    the others. `now` is only the fallback stamp for a row with no `activated_at`; normally
    each row's own `activated_at` is used so owner order reflects real activation time.
    """
    active = install_dal.metadata.tables["app_active_versions"]
    versions = install_dal.metadata.tables["app_versions"]
    uploads = install_dal.metadata.tables["app_version_uploads"]
    commands = install_dal.metadata.tables["app_commands"]

    async with install_dal.engine.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    active.c.app_id,
                    active.c.tenant_id,
                    active.c.community_id,
                    active.c.activated_at,
                    versions.c.version,
                )
                .select_from(active.outerjoin(versions, versions.c.id == active.c.version_id))
                .order_by(active.c.activated_at, active.c.tenant_id, active.c.app_id)
            )
        ).all()

    synced = 0
    failed: list[str] = []
    for row in rows:
        label = f"{row.app_id}@{row.community_id}"
        try:
            async with install_dal.engine.begin() as conn:
                raw = (
                    await conn.execute(
                        select(uploads.c.manifest_json)
                        .where(
                            (uploads.c.app_id == row.app_id) & (uploads.c.version == row.version)
                        )
                        .order_by(uploads.c.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if raw is None:
                    raise LookupError("no stored manifest for active version")
                await app_command_service.sync_commands(
                    conn,
                    tenant_id=row.tenant_id,
                    community_id=row.community_id,
                    app_id=row.app_id,
                    manifest=_reparse_trusted(raw),
                    commands_table=commands,
                    now=row.activated_at or now,
                )
            synced += 1
        except Exception as exc:  # noqa: BLE001 -- one app's failure must not abort the sweep
            failed.append(f"{label}: {type(exc).__name__}")
            logger.warning(
                "app commands: backfill failed",
                extra={
                    "app_id": row.app_id,
                    "community_id": row.community_id,
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )

    return BackfillResult(examined=len(rows), synced=synced, failed=tuple(failed))
