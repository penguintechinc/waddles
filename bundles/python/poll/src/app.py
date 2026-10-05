"""`!poll` -> chat-command community polls, backed by the shared Postgres `community_polls`/
`poll_options`/`poll_votes` tables (migration 028, same tables `hub_api/blueprints/v1/
community_polls.py`'s REST API reads/writes).

Ported from `core/svc_process/bundles/community_polls_process.py` (the bot_process
monolith's `!poll create/vote/close/list/view`) onto `waddle_sdk.command`'s standard
grammar and `waddle_sdk.db`'s structured facade -- see `sdk/waddle-sdk/AUTHORING.md` SS1
for the grammar and `waddle_sdk/db.py`'s own module docstring for the structured
insert/select/update facade this bundle uses instead of the source file's raw
`raw_sql_rows`/`raw_sql_write` calls.

Grammar mapping -- the fixed verb vocabulary (`waddle_sdk.command.VERBS`) has no
`create`/`vote`/`close`/`view` verb, so the five domain actions map onto it like this:

| Domain action | Grammar                          | Example                                |
|---------------|-----------------------------------|-----------------------------------------|
| create        | `add`                              | `!poll add "title" "opt1" "opt2"`      |
| vote          | `set`                              | `!poll set 5 2`                        |
| close         | `remove` (ends the poll's active   | `!poll remove 5`                       |
|               | life -- never deletes rows)        |                                          |
| list          | bare `list`                        | `!poll list`                           |
| view          | `list` with one argument           | `!poll list 5`                         |

No sub-modules are declared (`CommandSpec.sub_modules` is empty) -- a poll's own identity
is a dynamic numeric id, not a fixed named sub-module, so `waddle_sdk.sub_modules.
SubModuleGate` does not apply here (AUTHORING.md SS1's sub-module section is for a fixed,
bundle-declared name set like `!shoutout`'s `auto`/`ai`).

Business logic split -- mirrors `lurk`'s own convention (see that bundle's module
docstring), NOT `count`'s: `transform` only recognizes the command via `parse_command()`
and forwards the normalized badge signal; ALL `db` work happens in `dispatch`, because
`db`'s row-level-security is scoped to the envelope's `community` (`wit/waddle-bundle/
stage.wit`'s own `db` interface doc: "row-level-security scoped to the envelope's
tenant/community"), and `community` only exists on `stage-envelope` (action-stage), never
on the bare `platform-event` `transform` receives (`wit/waddle-bundle/stage.wit::types`).

PII (2026-10-05, tokenization pipeline #429 not merged -- `event.actor` may still be a
raw username): never stored or logged raw. A poll's creator identity and a vote's voter
identity are both SHA-256 hashed into a non-reversible pseudonym before ever reaching
`db` -- see `_actor_hash()`. `community_polls.created_by`/`poll_votes.user_id` are
`hub_users.id` foreign keys this bundle has no way to resolve (a chat actor's Twitch/
Discord handle has no linked `hub_users` row today -- the exact gap migration 089's own
FLAG comment already documented: "requires actor->user lookup table or UUID-based actor
field", not yet built). Rather than stub a fake FK value (which would either violate the
FK constraint outright or silently misattribute every poll to one placeholder user),
migration 097 makes `community_polls.created_by` nullable and adds `created_by_hash` for
exactly this case; `poll_votes.ip_hash` (already nullable, migration 028, originally for
anonymous form submissions) is reused unchanged for the voter pseudonym -- no vote-side
schema change was needed at all.

Double-vote handling: unlike a typical single-choice poll, `poll_votes` has no UNIQUE
constraint on `(poll_id, user_id)` alone -- its real constraint is `(poll_id, option_id,
user_id)` (migration 028), i.e. approval voting: a caller may vote for more than one
option in the same poll, and each is tracked separately. Re-voting for the SAME option
updates that row's `voted_at` rather than inserting a duplicate (`_record_vote()`'s
select-then-update-or-insert below is the structured-ops equivalent of the source file's
`ON CONFLICT (poll_id, option_id, user_id) DO UPDATE` upsert -- `waddle_sdk.db` has no
upsert primitive, so this fidelity is reproduced at the application layer instead).

Permissions (explicit product decision, not a 1:1 port -- the source file checked none
of this): `create`/`close` require broadcaster/moderator, reusing `count`'s own
`_is_privileged()` pattern (fails CLOSED, never guesses, when the platform's normalizer
emits neither `is_mod` nor `is_broadcaster` -- true for Discord today, see
`core/svc_ingest/src/normalize.rs::normalize_discord`). `vote`/`list`/`view` are open to
anyone.

Fail-loud: any `db` failure (`waddle_sdk.db.DALError`) is logged at ERROR, replied to
chat with a generic retry message, AND re-raised -- mirrors `lurk`'s `_fail_kv`, never
`count`'s older swallow-and-reply-only shape. No stubs: every declared command path
below has a real implementation; nothing here returns a canned/fake result.

Gated behind the PostHog flag ``waddles.command-poll`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate rationale and
ordering. BUILD-ONLY: this bundle is registered in `bundles/core-bundles.yaml` and
`bundles/Dockerfile.core-bundles` but the flag defaults OFF, and the legacy
`core/svc_process` monolith path (`bot_process._FEATURE_MODULES`) is left untouched --
cut-over and activation are a separate, later change.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, NoReturn

from waddle_sdk import clock, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.db import DALError, create_dal
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-poll"

SPEC = CommandSpec(name="poll")

_USAGE = (
    'Usage: !poll add "title" "opt1" "opt2" ... | !poll set <poll_id> <option_number> | '
    "!poll remove <poll_id> | !poll list | !poll list <poll_id> "
    "(add/remove: moderator/broadcaster only)"
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can create or close polls"

_POLL_ID_RE = re.compile(r"^\d+$")

#: `poll_votes.ip_hash`/`community_polls.created_by_hash` are `VARCHAR(64)` -- exactly
#: one SHA-256 hex digest, never truncated or padded.
_ACTOR_HASH_LEN = 64


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


def _actor_hash(actor: str | None) -> str:
    """SHA-256 hex digest of `actor` -- the only form of caller identity this bundle persists.

    Matches `lurk._state_key()`'s own hashing pattern. `actor or "anonymous"` so an
    event with no actor at all still produces a stable, non-empty pseudonym rather than
    `None` reaching a `VARCHAR` column.
    """
    digest = hashlib.sha256((actor or "anonymous").encode()).hexdigest()
    assert len(digest) == _ACTOR_HASH_LEN  # nosec B101 -- sha256 hexdigest is always 64 chars
    return digest


def _is_privileged(payload: dict[str, Any]) -> bool:
    """Broadcaster/moderator check -- fails CLOSED when role info isn't on the event.

    Identical contract to `count._is_privileged()` -- reads the exact `is_mod`/
    `is_broadcaster` boolean fields `core/svc_ingest/src/normalize.rs::
    normalize_twitch_irc` populates (Twitch) or omits (Discord, today).
    """
    is_mod = payload.get("is_mod")
    is_broadcaster = payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("poll.role_info_unavailable")
        return False
    return bool(is_mod) or bool(is_broadcaster)


def _parse_quoted_args(args: str) -> list[str]:
    """Parse quoted arguments from a command line string -- byte-for-byte port of the
    source file's own `_parse_quoted_args` (`core/svc_process/bundles/
    community_polls_process.py`).

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


def _map_action(parsed: ParsedCommand) -> tuple[str, str | None] | None:
    """Map a successfully-parsed `!poll ...` onto one of this bundle's five `db` actions.

    Returns `(action, args)` for a real action, or `None` for a bare `!poll` or any
    grammar-valid-but-poll-unhandled verb (`sub`/`enable`/`disable`/`delete`/`reset`) --
    `parse_command()` only rejects a token that ISN'T a verb at all; a verb this bundle
    doesn't use is still grammatically valid and must get the usage reply, never silence
    (`None` here signals "reply with `_USAGE` directly, no `dispatch`/`db` needed").
    """
    if parsed.option == "add":
        return "create", parsed.args
    if parsed.option == "set":
        return "vote", parsed.args
    if parsed.option == "remove":
        return "close", parsed.args
    if parsed.option == "list":
        return ("view", parsed.args) if parsed.args else ("list", None)
    return None


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!poll ...` and forward the parsed action.

    No `db` access here -- see module docstring for why `community` (required to scope
    every query) is only available in `dispatch`. Returns `None` for any non-matching
    payload or while `waddles.command-poll` is disabled.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    if not text.strip().lower().startswith("!poll"):
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    try:
        parsed = parse_command(text, SPEC)
    except CommandUsageError as exc:
        log.info("poll.usage_error", error=str(exc))
        return _reply(event, str(exc))

    mapped = _map_action(parsed)
    if mapped is None:
        log.info("poll.transform matched", action="usage")
        return _reply(event, _USAGE)
    action, args = mapped

    log.info("poll.transform matched", action=action)
    payload: dict[str, Any] = {"action": action, "channel_id": event.payload.get("channel_id")}
    if args is not None:
        payload["args"] = args
    # Forward the normalized badge signal, if present -- absence (e.g. Discord today)
    # must reach `dispatch` as absence, not as an implicit `False` (see `_is_privileged`).
    if "is_mod" in event.payload:
        payload["is_mod"] = bool(event.payload["is_mod"])
    if "is_broadcaster" in event.payload:
        payload["is_broadcaster"] = bool(event.payload["is_broadcaster"])

    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
        occurred_at=event.occurred_at,
    )


def _reply(event: PlatformEvent, text: str) -> PlatformEvent:
    """Build a same-shape `PlatformEvent` carrying a usage/error reply, bypassing `db` entirely."""
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"action": "usage", "channel_id": event.payload.get("channel_id"), "text": text},
        occurred_at=event.occurred_at,
    )


async def _fail_db(exc: Exception, *, provider: str, channel_id: str, op: str) -> NoReturn:
    """Fail-loud `db` error path: log, reply an error to chat, then re-raise.

    Mirrors `lurk._fail_kv()` exactly -- never silent, the pipeline still sees a real
    failure.
    """
    log.error("poll.db_error", op=op, error=str(exc))
    await relay.push(
        provider,
        {"channel": channel_id, "text": "polls are temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"poll db {op} failed: {exc}") from exc


async def _handle_create(
    args: str | None, *, community_id: int, actor: str | None
) -> str:
    """`!poll add "title" "opt1" "opt2" ...` -- create a new poll. Raises `DALError` fail-loud."""
    if not args:
        return 'Usage: !poll add "title" "option1" "option2" ...'

    parsed_args = _parse_quoted_args(args)
    if len(parsed_args) < 3:
        return "Poll must have a title and at least 2 options."

    title = parsed_args[0]
    options = parsed_args[1:]

    db = create_dal()
    poll_id = await db.community_polls.async_insert(
        community_id=community_id,
        created_by=None,
        created_by_hash=_actor_hash(actor),
        title=title,
        is_active=True,
    )
    if not poll_id:
        return "Failed to create poll."

    for idx, option_text in enumerate(options):
        await db.poll_options.async_insert(poll_id=poll_id, option_text=option_text, sort_order=idx)

    reply_text = f"Poll created! ID: {poll_id}\nTitle: {title}\nOptions:\n"
    for idx, opt in enumerate(options):
        reply_text += f"  {idx + 1}. {opt}\n"
    reply_text += f"Vote with: `!poll set {poll_id} <option_number>`"
    return reply_text


async def _fetch_poll(db: Any, poll_id: int, community_id: int) -> dict[str, Any] | None:
    """Fetch one poll scoped to `community_id` (IDOR fix, ported from the source file)."""
    query = (db.community_polls.id == poll_id) & (db.community_polls.community_id == community_id)
    rows = await db(query).select()
    return rows.first().as_dict() if rows else None


async def _fetch_options(db: Any, poll_id: int) -> list[dict[str, Any]]:
    """Fetch every option for `poll_id`, sorted by `sort_order` client-side.

    `AsyncQuerySet.select()` has no `ORDER BY` support (`waddle_sdk/db.py`'s own
    documented gap), so this bundle sorts the (small, bounded) result set in Python
    instead of reaching for raw SQL.
    """
    rows = await db(db.poll_options.poll_id == poll_id).select()
    return sorted((r.as_dict() for r in rows), key=lambda r: r["sort_order"])


async def _fetch_vote_counts(db: Any, poll_id: int) -> dict[int, int]:
    """Return `{option_id: vote_count}` for `poll_id`, computed client-side (no JOIN/GROUP BY
    in the structured facade)."""
    rows = await db(db.poll_votes.poll_id == poll_id).select()
    counts: dict[int, int] = {}
    for row in rows:
        option_id = row["option_id"]
        counts[option_id] = counts.get(option_id, 0) + 1
    return counts


async def _handle_vote(args: str | None, *, community_id: int, actor: str | None) -> str:
    """`!poll set <poll_id> <option_number>`. Raises `DALError` fail-loud."""
    if not args:
        return "Usage: !poll set <poll_id> <option_number>"

    parts = args.split()
    if len(parts) < 2 or not _POLL_ID_RE.match(parts[0]) or not _POLL_ID_RE.match(parts[1]):
        return "Usage: !poll set <poll_id> <option_number> (both must be numeric)"

    poll_id = int(parts[0])
    option_number = int(parts[1])

    db = create_dal()
    poll = await _fetch_poll(db, poll_id, community_id)
    if poll is None or not poll["is_active"]:
        return f"Poll {poll_id} not found or is closed."

    options = await _fetch_options(db, poll_id)
    if not options or option_number < 1 or option_number > len(options):
        return f"Invalid option number. Poll {poll_id} has {len(options)} options."

    option_id = options[option_number - 1]["id"]
    voter_hash = _actor_hash(actor)

    # Select-then-update-or-insert: the structured-ops equivalent of the source file's
    # `ON CONFLICT (poll_id, option_id, user_id) DO UPDATE` -- see module docstring's
    # "Double-vote handling" section.
    existing_query = (
        (db.poll_votes.poll_id == poll_id)
        & (db.poll_votes.option_id == option_id)
        & (db.poll_votes.ip_hash == voter_hash)
    )
    existing = await db(existing_query).select()
    now = clock.now_rfc3339()
    if existing:
        await db(existing_query).update(voted_at=now)
    else:
        await db.poll_votes.async_insert(
            poll_id=poll_id, option_id=option_id, user_id=None, ip_hash=voter_hash, voted_at=now
        )

    return f"Vote recorded for option {option_number} on poll {poll_id}!"


async def _handle_close(args: str | None, *, community_id: int) -> str:
    """`!poll remove <poll_id>` -- ends the poll (never deletes rows). Raises `DALError`
    fail-loud."""
    if not args or not _POLL_ID_RE.match(args.strip()):
        return "Usage: !poll remove <poll_id>"

    poll_id = int(args.strip())

    db = create_dal()
    poll = await _fetch_poll(db, poll_id, community_id)
    if poll is None:
        return f"Poll {poll_id} not found."

    query = (db.community_polls.id == poll_id) & (db.community_polls.community_id == community_id)
    await db(query).update(is_active=False)

    options = await _fetch_options(db, poll_id)
    counts = await _fetch_vote_counts(db, poll_id)

    reply_text = f"Poll {poll_id} closed: {poll['title']}\n\nResults:\n"
    for opt in options:
        count = counts.get(opt["id"], 0)
        reply_text += f"  - {opt['option_text']}: {count} vote{'s' if count != 1 else ''}\n"
    return reply_text


async def _handle_list(*, community_id: int) -> str:
    """`!poll list` -- active polls for this community, newest first, capped at 10.

    `AsyncQuerySet.select()` has no `ORDER BY`/`LIMIT` support, so this bundle sorts and
    slices the (per-community, bounded) result set in Python.
    """
    db = create_dal()
    query = (db.community_polls.community_id == community_id) & (
        db.community_polls.is_active == True  # noqa: E712 -- structured query, not a bool check
    )
    rows = await db(query).select()
    polls = sorted((r.as_dict() for r in rows), key=lambda r: r["created_at"], reverse=True)[:10]

    if not polls:
        return "No active polls in this community."

    reply_text = "Active polls:\n"
    for poll in polls:
        reply_text += f"  - Poll {poll['id']}: {poll['title']}\n"
    reply_text += "\nView with: `!poll list <id>` | Vote with: `!poll set <id> <option>`"
    return reply_text


async def _handle_view(args: str | None, *, community_id: int) -> str:
    """`!poll list <poll_id>` -- a specific poll's options and current vote counts."""
    if not args or not _POLL_ID_RE.match(args.strip()):
        return "Usage: !poll list <poll_id>"

    poll_id = int(args.strip())

    db = create_dal()
    poll = await _fetch_poll(db, poll_id, community_id)
    if poll is None:
        return f"Poll {poll_id} not found."

    status = "Active" if poll["is_active"] else "Closed"
    options = await _fetch_options(db, poll_id)
    counts = await _fetch_vote_counts(db, poll_id)

    reply_text = f"Poll {poll_id}: {poll['title']} [{status}]\n\n"
    for idx, opt in enumerate(options):
        count = counts.get(opt["id"], 0)
        vote_word = "votes" if count != 1 else "vote"
        reply_text += f"  {idx + 1}. {opt['option_text']} ({count} {vote_word})\n"

    if poll["is_active"]:
        reply_text += f"\nVote with: `!poll set {poll_id} <option_number>`"
    return reply_text


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: all `db` reads/writes, permission gate, then relay.

    Raises:
        ValueError: The envelope's payload has no `channel_id`; the envelope has no
            `community` (no tenant-wide fallback, same as `lurk`); or an unrecognized
            `action` (defensive -- `transform` only ever emits a member of the five
            actions + `"usage"`).
        RuntimeError: A `db` call failed (see `_fail_db` -- a chat error reply and an
            ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("poll reply requires a channel_id from the inbound chat.message")

    # A `transform`-built usage/error reply carries its own `text` already -- relay it
    # unchanged, no `db` or community needed at all.
    if "text" in payload:
        text = payload["text"]
        provider = envelope.event.platform
        await relay.push(provider, {"channel": channel_id, "text": text})
        return DispatchResult(transport=provider, detail="usage")

    action = payload.get("action")
    if action not in {"create", "vote", "close", "list", "view"}:
        raise ValueError(f"unrecognized poll action: {action!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("poll.missing_community", action=action)
        raise ValueError("poll requires a community context and cannot operate tenant-wide")
    community_id = int(community)

    args = payload.get("args") if isinstance(payload.get("args"), str) else None
    actor = envelope.event.actor

    if action in ("create", "close") and not _is_privileged(payload):
        log.info("poll.permission_denied", action=action)
        await relay.push(provider, {"channel": channel_id, "text": _PERMISSION_DENIED_MSG})
        return DispatchResult(transport=provider, detail=f"{action}:denied")

    try:
        if action == "create":
            reply_text = await _handle_create(args, community_id=community_id, actor=actor)
        elif action == "vote":
            reply_text = await _handle_vote(args, community_id=community_id, actor=actor)
        elif action == "close":
            reply_text = await _handle_close(args, community_id=community_id)
        elif action == "list":
            reply_text = await _handle_list(community_id=community_id)
        else:  # view
            reply_text = await _handle_view(args, community_id=community_id)
    except DALError as exc:
        await _fail_db(exc, provider=provider, channel_id=channel_id, op=action)

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("poll.dispatch relayed", platform=provider, action=action)
    return DispatchResult(transport=provider, detail=action)
