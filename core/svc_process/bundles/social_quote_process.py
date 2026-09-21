"""Social quote process bundle -- parses !quote commands and fetches quote data.

Command-style bundle that responds to `!quote add <text>`, `!quote <id>`,
and `!quote random`; returns `None` (no reply) for ordinary chatter.
Performs read operations (quote lookup, random selection) and builds the
reply text, storing write intentions in the payload for the action stage
to execute.
"""

from __future__ import annotations

import dataclasses
import re

from flask_core import PlatformEvent, get_bundle_dal

from bundles._dal_sql import raw_sql_rows

_COMMAND_PREFIX = "!quote"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Reply to quote commands; return None for everything else.

    Supported commands:
    - `!quote add <text>` -- store quote addition for action stage
    - `!quote <id>` -- fetch and display quote by ID
    - `!quote random` -- fetch and display random approved quote
    - `!quote` -- no args, show help

    Raises `ValueError` on a malformed event -- the process runner
    catches this per-event so one bad event never kills the poll loop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        raise ValueError("event payload missing required 'text' string field")

    text = text.strip()
    text_lower = text.lower()
    if not text or not text_lower.startswith(_COMMAND_PREFIX):
        return None  # not a quote command

    # Parse command: !quote [subcommand] [args...]
    after_prefix = text[len(_COMMAND_PREFIX) :].strip()
    parts = after_prefix.split(maxsplit=1)

    if not parts:
        # Just "!quote" with no subcommand
        reply_text = "Quote commands: `!quote add <text>` | `!quote <id>` | `!quote random`"
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    subcommand = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if subcommand == "add":
        # Add quote command
        if not rest:
            reply_text = "Usage: `!quote add <text>`"
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})
        # Store the add action for the action stage
        return dataclasses.replace(
            event,
            payload={
                **event.payload,
                "text": "",  # Will be populated by action stage
                "_quote_action": "add",
                "_quote_text": rest,
                "_actor": event.actor,
            },
        )

    if subcommand == "random":
        # Fetch random quote
        fetched = await _fetch_random_quote()
        reply_text = fetched or "No quotes found."
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    # Try to parse as quote ID
    if re.match(r"^\d+$", subcommand):
        quote_id = int(subcommand)
        fetched = await _fetch_quote(quote_id)
        reply_text = fetched or f"Quote #{quote_id} not found."
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    # Unknown subcommand
    reply_text = f"Unknown quote command: `{subcommand}`. Try `!quote help`."
    return dataclasses.replace(event, payload={**event.payload, "text": reply_text})


async def _fetch_quote(quote_id: int) -> str | None:
    """Fetch a quote by ID and return formatted text, or None if not found."""
    try:
        dal = get_bundle_dal()
        result = await raw_sql_rows(
            dal,
            "SELECT id, quote_text, quoted_username, created_at FROM quotes "
            "WHERE id = :quote_id AND deleted_at IS NULL",
            {"quote_id": quote_id},
        )
        if not result:
            return None

        row = result[0]
        author = row.get("quoted_username") or "unknown"
        return f'#{row["id"]}: "{row["quote_text"]}" — {author}'
    except Exception:
        return None


async def _fetch_random_quote() -> str | None:
    """Fetch a random approved quote and return formatted text, or None if none found."""
    try:
        dal = get_bundle_dal()
        result = await raw_sql_rows(
            dal,
            "SELECT id, quote_text, quoted_username FROM quotes "
            "WHERE deleted_at IS NULL AND is_approved = TRUE ORDER BY RANDOM() LIMIT 1",
        )
        if not result:
            return None

        row = result[0]
        author = row.get("quoted_username") or "unknown"
        return f'#{row["id"]}: "{row["quote_text"]}" — {author}'
    except Exception:
        return None
