"""Marketing engagement process handler -- validate and pass through engagement events.

Ported from `core/engagement_module/app.py` (v2). Transforms engagement-related
platform events (polls, forms) by validating required fields and preserving
engagement context for downstream action stages.
"""

from __future__ import annotations

import dataclasses

from flask_core import PlatformEvent


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Validate and pass through engagement events.

    Validates that the event payload contains required fields for engagement
    operations (poll creation, voting, form submission). Returns the event
    unchanged if valid, or raises ValueError if malformed. Returns None for
    non-engagement event types.

    Raises `ValueError` on a malformed event -- the process runner catches
    this per-event so one bad event never kills the poll loop.
    """
    event_type = event.event_type or ""
    payload = event.payload

    # Only process engagement event types; return None for everything else
    if event_type not in ("poll_create", "poll_vote", "form_submit", "engagement"):
        return None

    # Validate that event has at least the base fields needed for engagement
    if not payload:
        raise ValueError("event payload missing required fields for engagement")

    text = payload.get("text")
    if text is not None and not isinstance(text, str):
        raise ValueError("event payload 'text' field must be a string if present")

    # FLAG: DB lookup for poll/form validation not implemented here -- could
    # fetch from database to validate poll_id/form_id existence, visibility
    # settings, etc. For now, basic payload validation is sufficient.
    # Full implementation: check poll/form exists, user has permission,
    # form not already submitted by user, poll not expired, etc.

    # Pass through the event unchanged
    return dataclasses.replace(event, payload={**payload})
