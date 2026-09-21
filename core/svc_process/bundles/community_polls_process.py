"""Community polls process bundle -- parses poll commands and executes state changes.

Ported from `hub_api/blueprints/v1/community_polls.py`'s REST API endpoints
into a chat-command-driven App Bundle. Responds to `!poll create`, `!poll vote`,
`!poll close`, and `!poll list/view` commands; returns `None` for ordinary chatter.

Process stage handles both read operations (list, view) and write coordination
(create, vote, close) -- fetching poll state, executing state changes, building
reply text. Action stage sends the formatted reply to the platform.
"""

from __future__ import annotations

import dataclasses
import re

from flask_core import PlatformEvent, get_bundle_context, get_bundle_dal
from flask_core.bundle_runtime import raw_sql_rows, raw_sql_write

_COMMAND_PREFIX = "!poll"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Parse poll commands and execute state changes or return formatted reply.

    Supported commands:
    - `!poll create "title" "option1" "option2" ...` -- create a new poll
    - `!poll vote <poll_id> <option_index>` -- vote on a poll
    - `!poll close <poll_id>` -- close a poll
    - `!poll list` -- list active polls
    - `!poll view <poll_id>` -- view a specific poll

    Raises `ValueError` on a malformed event -- the process runner catches
    this per-event so one bad event never kills the poll loop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        raise ValueError("event payload missing required 'text' string field")

    text = text.strip()
    if not text or not text.startswith(_COMMAND_PREFIX):
        return None  # not a poll command

    # Parse command: !poll [subcommand] [args...]
    parts = text[len(_COMMAND_PREFIX) :].strip().split(maxsplit=1)
    if not parts:
        # Just "!poll" with no subcommand
        reply_text = (
            'Poll commands: `!poll create "title" "opt1" "opt2" ...` | '
            "`!poll list` | `!poll view <id>` | `!poll vote <id> <option>` | "
            "`!poll close <id>`"
        )
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    subcommand = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if subcommand == "create":
        return await _handle_poll_create(event, rest)
    elif subcommand == "vote":
        return await _handle_poll_vote(event, rest)
    elif subcommand == "close":
        return await _handle_poll_close(event, rest)
    elif subcommand == "list":
        return await _handle_poll_list(event)
    elif subcommand == "view":
        return await _handle_poll_view(event, rest)
    else:
        reply_text = f"Unknown poll command: `{subcommand}`. Try `!poll help`."
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})


async def _handle_poll_create(event: PlatformEvent, args: str) -> PlatformEvent | None:
    """Parse and execute poll creation.

    Format: "title" "option1" "option2" ...
    """
    if not args:
        reply_text = 'Usage: `!poll create "title" "option1" "option2" ...`'
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    # Parse quoted arguments
    parsed_args = _parse_quoted_args(args)
    if len(parsed_args) < 3:
        reply_text = "Poll must have a title and at least 2 options."
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    title = parsed_args[0]
    options = parsed_args[1:]

    try:
        ctx = get_bundle_context()
        dal = get_bundle_dal()
        if not ctx.community:
            reply_text = "Poll creation requires a community context."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        community_id = int(ctx.community)

        # Create poll in database, scoped to community
        creator_id = event.actor or "unknown"
        result = await raw_sql_write(
            dal,
            "INSERT INTO community_polls "
            "(community_id, created_by, title, is_active, created_at, updated_at) "
            "VALUES (:community_id, :created_by, :title, TRUE, NOW(), NOW()) RETURNING id",
            {"community_id": community_id, "created_by": creator_id, "title": title},
        )
        poll_id = result[0]["id"] if result else None

        if not poll_id:
            reply_text = "Failed to create poll."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        # Create poll options
        for idx, option_text in enumerate(options):
            await raw_sql_write(
                dal,
                "INSERT INTO poll_options (poll_id, option_text, sort_order) "
                "VALUES (:poll_id, :option_text, :sort_order)",
                {"poll_id": poll_id, "option_text": option_text, "sort_order": idx},
            )

        reply_text = f"Poll created! ID: {poll_id}\nTitle: {title}\nOptions:\n"
        for idx, opt in enumerate(options):
            reply_text += f"  {idx + 1}. {opt}\n"
        reply_text += f"Vote with: `!poll vote {poll_id} <option_number>`"

        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    except Exception as e:
        reply_text = f"Error creating poll: {str(e)}"
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})


async def _handle_poll_vote(event: PlatformEvent, args: str) -> PlatformEvent | None:
    """Parse and execute poll vote.

    Format: <poll_id> <option_number>
    """
    if not args:
        reply_text = "Usage: `!poll vote <poll_id> <option_number>`"
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    parts = args.split()
    if len(parts) < 2:
        reply_text = "Usage: `!poll vote <poll_id> <option_number>`"
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    if not re.match(r"^\d+$", parts[0]) or not re.match(r"^\d+$", parts[1]):
        reply_text = "Poll ID and option number must be numeric."
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    poll_id = int(parts[0])
    option_number = int(parts[1])

    try:
        ctx = get_bundle_context()
        dal = get_bundle_dal()
        if not ctx.community:
            reply_text = "Poll voting requires a community context."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        community_id = int(ctx.community)

        # Fetch poll to verify it exists and belongs to this community (IDOR fix)
        poll_result = await raw_sql_rows(
            dal,
            "SELECT id, title FROM community_polls "
            "WHERE id = :poll_id AND community_id = :community_id AND is_active = TRUE",
            {"poll_id": poll_id, "community_id": community_id},
        )
        if not poll_result:
            reply_text = f"Poll {poll_id} not found or is closed."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        # Fetch poll options (safe because we already scoped to community via poll)
        opts_result = await raw_sql_rows(
            dal,
            "SELECT id FROM poll_options WHERE poll_id = :poll_id ORDER BY sort_order",
            {"poll_id": poll_id},
        )
        num_opts = len(opts_result) if opts_result else 0
        if not opts_result or option_number < 1 or option_number > len(opts_result):
            reply_text = f"Invalid option number. Poll {poll_id} has {num_opts} options."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        option_id = opts_result[option_number - 1]["id"]

        # Record vote using actor (username) as voter identifier
        voter_id = event.actor or "unknown"
        await raw_sql_write(
            dal,
            "INSERT INTO poll_votes (poll_id, option_id, user_id, voted_at) "
            "VALUES (:poll_id, :option_id, :user_id, NOW()) "
            "ON CONFLICT (poll_id, option_id, user_id) DO UPDATE SET voted_at = NOW()",
            {"poll_id": poll_id, "option_id": option_id, "user_id": voter_id},
        )

        reply_text = f"Vote recorded for option {option_number} on poll {poll_id}!"
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    except Exception as e:
        reply_text = f"Error recording vote: {str(e)}"
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})


async def _handle_poll_close(event: PlatformEvent, args: str) -> PlatformEvent | None:
    """Parse and execute poll close.

    Format: <poll_id>
    """
    if not args or not re.match(r"^\d+$", args.strip()):
        reply_text = "Usage: `!poll close <poll_id>`"
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    poll_id = int(args.strip())

    try:
        ctx = get_bundle_context()
        dal = get_bundle_dal()
        if not ctx.community:
            reply_text = "Poll closing requires a community context."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        community_id = int(ctx.community)

        # Fetch poll to verify it exists and belongs to this community (IDOR fix)
        result = await raw_sql_rows(
            dal,
            "SELECT id, title FROM community_polls "
            "WHERE id = :poll_id AND community_id = :community_id",
            {"poll_id": poll_id, "community_id": community_id},
        )
        if not result:
            reply_text = f"Poll {poll_id} not found."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        poll = result[0]

        # Close poll, scoped to community
        await raw_sql_write(
            dal,
            "UPDATE community_polls SET is_active = FALSE "
            "WHERE id = :poll_id AND community_id = :community_id",
            {"poll_id": poll_id, "community_id": community_id},
        )

        # Fetch results
        results = await raw_sql_rows(
            dal,
            "SELECT po.option_text, COUNT(pv.id) as vote_count "
            "FROM poll_options po LEFT JOIN poll_votes pv ON po.id = pv.option_id "
            "WHERE po.poll_id = :poll_id GROUP BY po.id, po.option_text ORDER BY po.sort_order",
            {"poll_id": poll_id},
        )

        reply_text = f"Poll {poll_id} closed: {poll['title']}\n\nResults:\n"
        for row in results or []:
            count = row.get("vote_count", 0)
            reply_text += f"  • {row['option_text']}: {count} vote{'s' if count != 1 else ''}\n"

        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    except Exception as e:
        reply_text = f"Error closing poll: {str(e)}"
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})


async def _handle_poll_list(event: PlatformEvent) -> PlatformEvent | None:
    """Fetch and list active polls."""
    try:
        ctx = get_bundle_context()
        dal = get_bundle_dal()
        community_id = int(ctx.community) if ctx.community else 0

        results = await raw_sql_rows(
            dal,
            "SELECT id, title FROM community_polls WHERE community_id = :community_id "
            "AND is_active = TRUE ORDER BY created_at DESC LIMIT 10",
            {"community_id": community_id},
        )

        if not results:
            reply_text = "No active polls in this community."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        reply_text = "Active polls:\n"
        for row in results:
            reply_text += f"  • Poll {row['id']}: {row['title']}\n"
        reply_text += "\nView with: `!poll view <id>` | Vote with: `!poll vote <id> <option>`"

        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    except Exception as e:
        reply_text = f"Error listing polls: {str(e)}"
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})


async def _handle_poll_view(event: PlatformEvent, args: str) -> PlatformEvent | None:
    """Fetch and display a specific poll."""
    if not args or not re.match(r"^\d+$", args.strip()):
        reply_text = "Usage: `!poll view <poll_id>`"
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    poll_id = int(args.strip())

    try:
        ctx = get_bundle_context()
        dal = get_bundle_dal()
        if not ctx.community:
            reply_text = "Poll viewing requires a community context."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        community_id = int(ctx.community)

        # Fetch poll, scoped to community (IDOR fix)
        result = await raw_sql_rows(
            dal,
            "SELECT id, title, is_active FROM community_polls "
            "WHERE id = :poll_id AND community_id = :community_id",
            {"poll_id": poll_id, "community_id": community_id},
        )
        if not result:
            reply_text = f"Poll {poll_id} not found."
            return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

        poll = result[0]
        status = "Active" if poll["is_active"] else "Closed"

        # Fetch options and vote counts
        options = await raw_sql_rows(
            dal,
            "SELECT po.id, po.option_text, COUNT(pv.id) as vote_count "
            "FROM poll_options po LEFT JOIN poll_votes pv ON po.id = pv.option_id "
            "WHERE po.poll_id = :poll_id GROUP BY po.id, po.option_text ORDER BY po.sort_order",
            {"poll_id": poll_id},
        )

        reply_text = f"Poll {poll_id}: {poll['title']} [{status}]\n\n"
        for idx, opt in enumerate(options or []):
            count = opt.get("vote_count", 0)
            vote_word = "votes" if count != 1 else "vote"
            reply_text += f"  {idx + 1}. {opt['option_text']} ({count} {vote_word})\n"

        if poll["is_active"]:
            reply_text += f"\nVote with: `!poll vote {poll_id} <option_number>`"

        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})

    except Exception as e:
        reply_text = f"Error viewing poll: {str(e)}"
        return dataclasses.replace(event, payload={**event.payload, "text": reply_text})


def _parse_quoted_args(args: str) -> list[str]:
    """Parse quoted arguments from a command line string.

    Example: '"title" "opt1" "opt2"' -> ['title', 'opt1', 'opt2']
    """
    result = []
    current = ""
    in_quotes = False
    escaped = False

    for char in args:
        if escaped:
            current += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
        elif char in (" ", "\t") and not in_quotes:
            if current:
                result.append(current)
                current = ""
        else:
            current += char

    if current:
        result.append(current)

    return result
