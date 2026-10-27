"""Demo ingest handler -- normalizes a raw inbound event to a `PlatformEvent`.

Referenced by `app_catalog.stages.ingest.entrypoint` (migration 071) as
`"builtin_handlers.echo_ingest:normalize"` for the `waddles.core.demo.echo` built-in app
that migration seeds. Returns `flask_core.PlatformEvent` -- the frozen
stage-to-stage contract (`libs/flask_core/flask_core/stream_pipeline.py`) --
rather than a bare dict; `runner.py` wraps the result in a `StageEnvelope`
and enqueues it typed, not as ad-hoc JSON.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from flask_core import PlatformEvent


async def normalize(raw: dict[str, Any]) -> PlatformEvent:
    """Normalize one raw inbound event dict to a `PlatformEvent`.

    Real, working transform (not a stub): requires `source`/`text` on the
    raw event, trims `text`, and stamps a UTC `occurred_at` when the raw
    event didn't carry its own timestamp. Raises `ValueError` on a
    malformed raw event -- the ingest runner catches this per-event so one
    bad event never kills the poll loop.
    """
    source = raw.get("source")
    text = raw.get("text")
    if not source or not isinstance(source, str):
        raise ValueError("raw event missing required 'source' string field")
    if not text or not isinstance(text, str):
        raise ValueError("raw event missing required 'text' string field")

    return PlatformEvent(
        platform=source,
        event_type=raw.get("event_type", "message"),
        actor=raw.get("actor", "unknown"),
        payload={"text": text.strip()},
        occurred_at=raw.get("occurred_at") or datetime.now(UTC).isoformat(),
    )
