"""Inventory (Quartermaster) process bundle -- `!inventory` chat commands.

Board-demo command reading/writing the REAL, already-migrated
`inventory_items` table (`config/postgres/migrations/014_add_quartermaster_
tables.sql`) that `hub_api/services/community_inventory.py` also serves via
its own REST API -- this bundle is a second, chat-driven surface onto the
same table, not a new schema, and no DDL runs from here. Community-scoped
via `get_bundle_context()` (never from `event.payload`, which is untrusted
platform-supplied data).

Implemented subcommands (`add`/`remove`/`list`) use only `inventory_items`
columns that exist for real: `community_id`, `name`, `item_type`, `quantity`,
`available_quantity`, `metadata` (JSONB -- `tags`/`owner` land here since
neither is a real column), `created_at`/`updated_at`, `deleted_at`
(soft-delete, matching `community_inventory.delete_item`'s own convention --
no hard DELETE). No `UNIQUE(community_id, name)` constraint exists at the DB
layer (verified against the migration -- only plain indexes), so `add`'s
"already exists" check is an application-level SELECT-then-INSERT, not an
`ON CONFLICT`; a benign duplicate-name race under concurrent adds is
possible and acceptable for a demo command.

`checkout`/`checkin`/`return` are intentionally DEFERRED, not implemented:
the real `inventory_checkouts` table requires `user_id INTEGER NOT NULL
REFERENCES hub_users(id) ON DELETE RESTRICT` -- a platform chat event
(`event.actor` / `event.payload["author_id"]`) has no verified, safe way to
resolve to a real `hub_users.id` from this bundle, and `community_inventory.
checkout_item`/`checkin_item` additionally call Postgres helper functions
(`add_inventory_stock`/`remove_inventory_stock`) to keep stock counts and
the `inventory_log` audit trail consistent. Reimplementing that
transactional logic ad hoc here, against a real shared production table,
without a real user identity, risks bad FK writes or stock-count drift --
worse than a command that honestly says "not yet". They return a graceful
stub reply instead of touching the DB.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

from flask_core import PlatformEvent, get_bundle_context, get_bundle_dal
from flask_core.bundle_runtime import raw_sql_rows, raw_sql_write

logger = logging.getLogger(__name__)

_COMMAND_PREFIX = "!inventory"

#: Simple nix-style single-value flags (`-t <value>`). `-T` (checkout target)
#: is reserved for when checkout is implemented -- the shared flag parser is
#: future work (#301); this is intentionally minimal for the demo.
_VALUE_FLAGS = ("-t", "-o", "-T")

_CHECKOUT_STUB_REPLY = (
    "checkout/checkin isn't wired up yet -- needs linking your chat account to a "
    "hub user first. Coming soon! \U0001f427"
)


def _parse_flags(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split whitespace-tokenized args into positional args and single-value flags."""
    positional: list[str] = []
    values: dict[str, str] = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in _VALUE_FLAGS and i + 1 < len(argv):
            values[token] = argv[i + 1]
            i += 2
        else:
            positional.append(token)
            i += 1
    return positional, values


def _require_community_id() -> int | None:
    """Return the current community id from `get_bundle_context()`, or `None`."""
    ctx = get_bundle_context()
    return int(ctx.community) if ctx.community else None


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Route `!inventory <subcommand> ...` to its handler; `None` for anything else.

    Raises `ValueError` on a malformed event -- `bot_process._dispatch_feature`
    catches this per-event so one bad event never kills the bot.
    """
    raw_text = event.payload.get("text")
    if not isinstance(raw_text, str):
        raise ValueError("event payload missing required 'text' string field")
    text = raw_text.strip()
    if not text.lower().startswith(_COMMAND_PREFIX):
        return None

    rest = text[len(_COMMAND_PREFIX) :].strip()
    parts = rest.split(maxsplit=1)
    if not parts:
        reply_text = (
            "Inventory commands: `!inventory add <name> [-t tags] [-o owner]` | "
            "`!inventory remove <name>` | `!inventory list`"
        )
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    subcommand = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if subcommand == "add":
        return await _handle_add(event, args)
    if subcommand == "remove":
        return await _handle_remove(event, args)
    if subcommand == "list":
        return await _handle_list(event)
    if subcommand in ("checkout", "checkin", "return"):
        return dataclasses.replace(event, payload={**event.payload, "text": _CHECKOUT_STUB_REPLY})

    reply_text = f"Unknown inventory command: `{subcommand}`. Try `!inventory`."
    return dataclasses.replace(event, payload={**event.payload, "text": reply_text})


async def _handle_add(event: PlatformEvent, args: str) -> PlatformEvent | None:
    """`!inventory add <name> [-t tags] [-o owner]` -- insert if not already present."""
    positional, flags = _parse_flags(args.split())
    name = positional[0] if positional else ""
    if not name:
        reply_text = "Usage: `!inventory add <name> [-t tags] [-o owner]`"
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    try:
        community_id = _require_community_id()
        if community_id is None:
            reply_text = "Inventory commands require a community context."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        dal = get_bundle_dal()
        existing = await raw_sql_rows(
            dal,
            "SELECT id FROM inventory_items "
            "WHERE community_id = :community_id AND name = :name AND deleted_at IS NULL LIMIT 1",
            {"community_id": community_id, "name": name},
        )
        if existing:
            reply_text = f"'{name}' already exists in inventory."
        else:
            metadata = {"tags": flags.get("-t"), "owner": flags.get("-o")}
            await raw_sql_write(
                dal,
                "INSERT INTO inventory_items "
                "(community_id, name, item_type, quantity, available_quantity, "
                "metadata, created_at, updated_at) "
                "VALUES (:community_id, :name, 'general', 1, 1, "
                "CAST(:metadata AS jsonb), NOW(), NOW())",
                {
                    "community_id": community_id,
                    "name": name,
                    "metadata": json.dumps(metadata),
                },
            )
            reply_text = f"\U0001f4e6 added '{name}' to inventory."
    except Exception as exc:  # noqa: BLE001 -- a bad insert must never crash the bot
        logger.error("inventory_process.add_failed name=%s error=%s", name, exc)
        reply_text = f"Error adding '{name}' to inventory."

    return dataclasses.replace(event, payload={**event.payload, "text": reply_text})


async def _handle_remove(event: PlatformEvent, args: str) -> PlatformEvent | None:
    """`!inventory remove <name>` -- soft-delete (inverse of `add`)."""
    stripped = args.strip()
    name = stripped.split(maxsplit=1)[0] if stripped else ""
    if not name:
        reply_text = "Usage: `!inventory remove <name>`"
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    try:
        community_id = _require_community_id()
        if community_id is None:
            reply_text = "Inventory commands require a community context."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        dal = get_bundle_dal()
        result = await raw_sql_write(
            dal,
            "UPDATE inventory_items SET deleted_at = NOW(), updated_at = NOW() "
            "WHERE community_id = :community_id AND name = :name AND deleted_at IS NULL "
            "RETURNING id",
            {"community_id": community_id, "name": name},
        )
        reply_text = (
            f"\U0001f5d1 removed '{name}' from inventory."
            if result
            else f"'{name}' not found in inventory."
        )
    except Exception as exc:  # noqa: BLE001 -- a bad update must never crash the bot
        logger.error("inventory_process.remove_failed name=%s error=%s", name, exc)
        reply_text = f"Error removing '{name}' from inventory."

    return dataclasses.replace(event, payload={**event.payload, "text": reply_text})


async def _handle_list(event: PlatformEvent) -> PlatformEvent | None:
    """`!inventory list` -- active items for this community, with availability."""
    try:
        community_id = _require_community_id()
        if community_id is None:
            reply_text = "Inventory commands require a community context."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        dal = get_bundle_dal()
        rows = await raw_sql_rows(
            dal,
            "SELECT name, quantity, available_quantity, metadata FROM inventory_items "
            "WHERE community_id = :community_id AND deleted_at IS NULL ORDER BY name LIMIT 20",
            {"community_id": community_id},
        )
        reply_text = _format_list(rows)
    except Exception as exc:  # noqa: BLE001 -- a bad read must never crash the bot
        logger.error("inventory_process.list_failed error=%s", exc)
        reply_text = "Error listing inventory."

    return dataclasses.replace(event, payload={**event.payload, "text": reply_text})


def _format_list(rows: list[dict[str, Any]]) -> str:
    """Format inventory rows into a readable reply -- a friendly hint if empty."""
    if not rows:
        return "(no items yet -- add one with `!inventory add <name>`)"

    lines = ["**Inventory:**"]
    for row in rows:
        metadata = _coerce_metadata(row.get("metadata"))
        owner = metadata.get("owner")
        tags = metadata.get("tags")
        status = f"{row.get('available_quantity')}/{row.get('quantity')} available"
        suffix = ""
        if owner:
            suffix += f" (owner: {owner})"
        if tags:
            suffix += f" [tags: {tags}]"
        lines.append(f"- {row.get('name')}: {status}{suffix}")
    return "\n".join(lines)


def _coerce_metadata(raw: object) -> dict[str, Any]:
    """Normalize a `metadata` JSONB value -- already a dict, a JSON string, or garbage."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            loaded = json.loads(raw)
        except ValueError:
            return {}
        if isinstance(loaded, dict):
            return loaded
    return {}
