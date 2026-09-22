"""Live activity feed -- best-effort telemetry writes to `live_activity_events`.

Board-demo crunch feature: the live WebUI feed shows each inbound message
plus the bot's reply by reading this table. Schema is frozen and already
applied out-of-band (the task's own contract) -- this module's
`define_table` passes `migrate=False`, matching `flask_core.
app_bundle_tables`'s established "pydal maps onto the already-migrated
table, it never owns this DDL" convention (see also `core/svc_action/
services/dispatch_log.py`).

`community_id` is bound as a plain `integer` field here, not `reference
communities` -- pydal requires a reference field's target table to already
be `define_table()`-d on the *same* `dal` instance (see `flask_core.
app_bundle_tables`'s module docstring), and svc-process's own DAL never
binds `communities` (unlike svc-action's `services/reference_tables.py`).
The real FK constraint already lives at the DB layer via the owning
migration; the Python-side field type doesn't need to declare it for an
INSERT to succeed, and skipping it keeps this binding self-contained to
`core/svc_process/`, per this task's explicit scope.

This is best-effort telemetry, never the pipeline's source of truth --
every caller (`runner.py::_transform_and_enqueue`) wraps the write in a
broad `except Exception` so a feed outage never breaks the live bot.
"""

from __future__ import annotations

from penguin_dal import AsyncDB


async def record_activity(
    dal: AsyncDB,
    *,
    community_id: int,
    platform: str,
    actor: str | None,
    message_in: str | None,
    reply_out: str | None,
    channel_id: str | None,
) -> None:
    """Insert one `live_activity_events` row. Raises on a DB write failure.

    Stays a thin, raising wrapper -- the fail-safe boundary lives in exactly
    one place (`runner.py::_emit_activity`), matching `services.
    dispatch_log.record_dispatch`'s own division of responsibility.
    """
    await dal.live_activity_events.async_insert(
        community_id=community_id,
        platform=platform,
        actor=actor,
        message_in=message_in,
        reply_out=reply_out,
        channel_id=channel_id,
    )
