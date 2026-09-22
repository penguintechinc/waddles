"""ACTION stage-runner dispatch audit log -- write helper.

Schema owned by `config/postgres/migrations/074_action_dispatch_log.sql`
(the source of truth) -- this module's `define_table` passes
`migrate=False` throughout, matching `flask_core.app_bundle_tables`'
established "pydal maps onto the already-migrated table, it never owns
this DDL" convention.

`detail` is always a short, human-readable status string -- request/
response bodies and resolved secrets are never persisted here (security.md
"log masked, never raw PII"; this task's "never log secrets/tokens/full
bodies").
"""

from __future__ import annotations

from datetime import datetime

from penguin_dal import AsyncDB


async def record_dispatch(
    dal: AsyncDB,
    *,
    tenant_id: int,
    community_id: int | None,
    app_id: str,
    target_type: str,
    status: str,
    attempt: int,
    http_status: int | None,
    detail: str,
    envelope_ts: datetime | None,
) -> None:
    """Insert one audit row.

    Raises on a DB write failure -- callers (`runner.py::_record`) catch
    and log rather than let an audit-log write failure mask or retry-loop
    the dispatch outcome it's trying to record.
    """
    await dal.action_dispatch_log.async_insert(
        tenant_id=tenant_id,
        community_id=community_id,
        app_id=app_id,
        target_type=target_type,
        status=status,
        attempt=attempt,
        http_status=http_status,
        detail=detail[:500],  # bounded -- this is a status string, not a body dump
        envelope_ts=envelope_ts,
    )
