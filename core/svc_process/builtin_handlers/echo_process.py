"""Demo process handler -- applies a real transform to a normalized platform event.

Referenced by `app_catalog.stages.process.entrypoint` (migration 071) as
`"builtin_handlers.echo_process:transform"` for the `waddles.core.demo.echo` built-in app
that migration seeds.
"""

from __future__ import annotations

import dataclasses

from flask_core import PlatformEvent


async def transform(event: PlatformEvent) -> PlatformEvent:
    """Apply the demo handler's process transform to one normalized `PlatformEvent`.

    Real, working transform (not a stub): uppercases `event.payload["text"]`
    and tags it with a computed `word_count`, returning a NEW `PlatformEvent`
    (`dataclasses.replace` -- the frozen contract's own instances are never
    mutated in place). Every other payload field is preserved as-is
    (crucially `channel_id`/`guild_id` survive), and every top-level
    `PlatformEvent` field (`platform`, `event_type`, `actor`, `occurred_at`)
    passes through untouched. Raises `ValueError` on a malformed event --
    the process runner catches this per-event so one bad event never kills
    the poll loop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        raise ValueError("event payload missing required 'text' string field")

    return dataclasses.replace(
        event,
        payload={
            **event.payload,
            "text": text.upper(),
            "word_count": len(text.split()),
            "processed": True,
        },
    )
