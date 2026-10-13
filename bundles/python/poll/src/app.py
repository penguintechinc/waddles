"""`!poll` -> chat-command community polls on the structured `db` + `community_kv` APIs (v2).

Ported from `core/svc_process/builtin_handlers/community_polls_process.py` (the bot_process
monolith's `!poll create/vote/close/list/view`) onto `waddle_sdk.command`'s standard grammar.

**Ported off the retired DAL-style facade (2026-10-10).** The v1 build imported
`waddle_sdk.db.create_dal()` and drove three shared Postgres tables
(`community_polls`/`poll_options`/`poll_votes`) through a `penguin_dal` query builder. That
facade was removed when the structured `db` capability landed (design rule: "no
bundle-supplied SQL, ever" -- see `sdk/waddle-sdk/src/waddle_sdk/db.py`), so `import app`
raised `ImportError: cannot import name 'create_dal'` and the bundle could neither load nor
build. The replacement `db` interface exposes five structured ops (`insert`/`get`/`query`/
`update`/`delete`), **one app-owned table per bundle, no `table` parameter and no
column-equality filter**, with tenant/community scoping applied host-side. A faithful port is
therefore a data-model decision, not a mechanical swap -- this is the same shape `quote`/`rank`
use (db row + `community_kv` index), extended with `kv` counters for the vote tally:

- **Poll definition** (title, options, `is_active`, creator pseudonym, final `results`): one
  `db` row in `poll_records` -- durable, listable via `db.query(order_by="poll_id")`.
- **Chat-visible numeric poll id**: `poll.seq.counter` (`community_kv.increment`) -- atomic, short
  and typeable (`!poll set 5 2`), never a raw row UUID.
- **id -> `db` `row_id`**: `poll.rowid.<id>` (`community_kv`) -- `db.get` takes only a `row_id`.
- **Per-option live tally**: `poll.tally.<id>.<n>` (atomic `increment`) -- concurrent votes never
  lose an update (no read-modify-write).
- **One vote per option per voter**: `poll.voted.<id>.<n>.<voter>` marker claimed with an atomic
  `increment` (the first caller sees `1`) -- race-free double-vote protection; claiming first
  means a crash can only under-count, never over-count.

All keys use `.` never `:` (gh-631: `:` is the host's reserved kv separator). Markers and
tallies carry a 30-day TTL (the host's `KV_MAX_TTL_S` cap) so they do not accumulate against the
per-community 10,000-key quota; **closing a poll snapshots the final tallies into the `results`
column**, so a closed poll's results are permanent and independent of kv expiry.

Grammar mapping -- the fixed verb vocabulary (`waddle_sdk.command.VERBS`) has no
`create`/`vote`/`close`/`view` verb, so the five domain actions map onto it:

| Domain action | Grammar                                  | Example                           |
|---------------|------------------------------------------|-----------------------------------|
| create        | `add`                                    | `!poll add "title" "opt1" "opt2"` |
| vote          | `set`                                    | `!poll set 5 2`                   |
| close         | `remove` (ends the poll, never deletes)  | `!poll remove 5`                  |
| list          | bare `list`                              | `!poll list`                      |
| view          | `list` with one argument                 | `!poll list 5`                    |

Voting is **approval voting** (unchanged from v1): a caller may vote for more than one option of
the same poll; re-voting for an option already voted for is a no-op (never double-counted).

Bounds (host limits are 64 `kv` ops and 64 `db` ops per invocation, 8 KiB per text column):
title <= 200 chars, 2-10 options of <= 100 chars each, unique case-insensitively; poll ids and
option numbers are ASCII-digit-only (Python's regex digit class also accepts non-ASCII digits).

Business-logic split -- mirrors `lurk`/`quote`: `transform` only recognizes the command via
`parse_command()` and forwards the normalized badge signal; ALL `kv`/`db` work happens in
`dispatch`, because `community` only exists on `stage-envelope` (action-stage), never on the
bare `platform-event` `transform` receives.

PII: `event.actor` may still be a raw username (tokenization pipeline #429 not merged). It is
never stored or logged raw -- the creator is stored as a SHA-256 hex digest (`created_by_hash`)
and a voter is a truncated SHA-256 inside the marker key. Log lines carry only `op`/`action`/
counts/exception type, never a title, option, argument or the actor (the hygiene gate
`scripts/ci/check-bundle-source-hygiene.py --check log-pii` enforces this).

Permissions: `add`/`remove` require a real boolean `True` `is_mod`/`is_broadcaster` (a string
badge such as `"false"` is NOT truthy here -- see `_is_privileged`); fails CLOSED when the
normalizer emits neither (true for Discord today). `set`/`list` are open to anyone.

Tenant-wide scope: `envelope.community is None` is the host's explicit tenant-wide sentinel
(alpha's only activation shape) and is scoped under `community_kv.TENANT_WIDE_SENTINEL`, the
same way `lurk` and `community_kv` itself do; an **empty string** community is a caller bug and
raises.

Fail-loud: any `kv`/`db` failure is logged at ERROR (op + error class name only), replied to
chat with a generic retry message, AND re-raised. A failed second step of a two-step write
(index write after row insert; tally after vote-marker claim) is compensated before raising so
the failure never strands an unreachable poll or a vote that was claimed but never counted. No
stubs: every declared command path has a real implementation.

Gated behind the PostHog flag ``waddles.command-poll`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate rationale and ordering.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, NoReturn, TypeVar

from waddle_sdk import community_kv, db, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.community_kv import TENANT_WIDE_SENTINEL
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-poll"

SPEC = CommandSpec(name="poll")

MAX_TITLE_LEN = 200
MAX_OPTION_LEN = 100
MIN_OPTIONS = 2
MAX_OPTIONS = 10

#: How many of the newest `poll_records` rows `!poll list` inspects (host `query` clamps at 200).
_LIST_FETCH_LIMIT = 50
#: How many active polls `!poll list` shows.
_LIST_SHOW = 10
#: Bounded optimistic-concurrency retry budget for closing a poll.
_MAX_CONFLICT_RETRIES = 5
#: 30 days -- the host's `KV_MAX_TTL_S` cap (`core/bundle_host_kv/src/limits.rs`).
_KV_TTL_SECONDS = 30 * 24 * 60 * 60
#: Truncated SHA-256 hex length used for the voter component of a marker key (64 bits).
_VOTER_TOKEN_LEN = 16

_SEQ_KEY = "poll.seq.counter"

_USAGE = (
    'Usage: !poll add "title" "opt1" "opt2" ... | !poll set <poll_id> <option_number> | '
    "!poll remove <poll_id> | !poll list | !poll list <poll_id> "
    "(add/remove: moderator/broadcaster only)"
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can create or close polls"
_UNAVAILABLE_MSG = "polls are temporarily unavailable, try again shortly."

#: ASCII digits only -- `\d` would also match non-ASCII Unicode digits.
_POLL_ID_RE = re.compile(r"^[0-9]{1,9}$")
_OPTION_NO_RE = re.compile(r"^[0-9]{1,3}$")

_KNOWN_ACTIONS = frozenset({"create", "vote", "close", "list", "view"})

_T = TypeVar("_T")


@dataclass(slots=True, frozen=True)
class _Ctx:
    """Per-dispatch routing context: where to reply and which community scopes `kv`."""

    provider: str
    channel_id: str
    community: str


@dataclass(slots=True, frozen=True)
class _Poll:
    """A decoded `poll_records` row plus the identifiers needed to update it."""

    row_id: str
    version: int
    poll_id: int
    title: str
    options: tuple[str, ...]
    is_active: bool
    results: tuple[int, ...] | None


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


def _normalize_identity(actor: str | None) -> str:
    """Canonical form of a caller identity before hashing (strip + lower-case)."""
    return (actor or "anonymous").strip().lower()


def _actor_hash(actor: str | None) -> str:
    """Full SHA-256 hex digest of the caller -- the only form of creator identity persisted."""
    return hashlib.sha256(_normalize_identity(actor).encode()).hexdigest()


def _voter_token(actor: str | None) -> str:
    """Truncated SHA-256 hex of the caller, used inside a vote-marker kv key."""
    return _actor_hash(actor)[:_VOTER_TOKEN_LEN]


def _rowid_key(poll_id: int) -> str:
    """Key (in `kv`) mapping a chat-visible poll id to its `db` `row_id`."""
    return f"poll.rowid.{poll_id}"


def _tally_key(poll_id: int, option_no: int) -> str:
    """Key (in `kv`) holding the live vote count for one option (1-based `option_no`)."""
    return f"poll.tally.{poll_id}.{option_no}"


def _voted_key(poll_id: int, option_no: int, voter: str) -> str:
    """Key (in `kv`) claiming one voter's vote for one option of one poll."""
    return f"poll.voted.{poll_id}.{option_no}.{voter}"


def _is_privileged(payload: dict[str, Any]) -> bool:
    """Broadcaster/moderator check -- fails CLOSED, and only a real boolean `True` counts.

    Reads the exact `is_mod`/`is_broadcaster` booleans `core/svc_ingest/src/normalize.rs`
    populates (Twitch/Discord). A string badge (`"false"`, `"0"`, `"true"`) is never trusted:
    `bool("false")` is `True`, which is how a non-moderator used to pass the mod gate. Anything
    that is not literally `True` is a denial.
    """
    is_mod = payload.get("is_mod")
    is_broadcaster = payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("poll.role_info_unavailable")
        return False
    return is_mod is True or is_broadcaster is True


def _parse_quoted_args(args: str) -> list[str]:
    """Split `args` on unquoted whitespace; quotes group words and a backslash escapes one char.

    Example: '"title" "opt1" "opt2"' -> ['title', 'opt1', 'opt2'].
    """
    result: list[str] = []
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
    """Map a parsed `!poll ...` onto one of the five actions, or `None` for the usage reply.

    `parse_command()` only rejects a token that ISN'T a verb at all; a verb this bundle does
    not use (`sub`/`enable`/`disable`/`delete`/`reset`) and a bare `!poll` are still valid
    grammar and must get the usage reply, never silence.
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


def _reply(event: PlatformEvent, text: str) -> PlatformEvent:
    """Build a same-shape `PlatformEvent` carrying a usage/error reply, bypassing `db` entirely."""
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"action": "usage", "channel_id": event.payload.get("channel_id"), "text": text},
        occurred_at=event.occurred_at,
    )


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!poll ...` and forward the parsed action.

    No `kv`/`db` access here -- `community` is only available in `dispatch`. Returns `None`
    for any non-matching payload or while `waddles.command-poll` is disabled.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head = text.strip().partition(" ")[0]
    if head.lower() != "!poll":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    try:
        parsed = parse_command(text, SPEC)
    except CommandUsageError as exc:
        log.info("poll.usage_error", error=type(exc).__name__)
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
    # Forward the normalized badge signal, if present -- absence (e.g. a normalizer that emits
    # no badges) must reach `dispatch` as absence, not as an implicit `False`. Identity check:
    # a string badge ("false") must never be laundered into a real `True` here.
    if "is_mod" in event.payload:
        payload["is_mod"] = event.payload["is_mod"] is True
    if "is_broadcaster" in event.payload:
        payload["is_broadcaster"] = event.payload["is_broadcaster"] is True

    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
        occurred_at=event.occurred_at,
    )


async def _fail_backend(exc: Exception, ctx: _Ctx, op: str) -> NoReturn:
    """Fail-loud backend error path: log (op + error class only), reply to chat, then re-raise.

    The class name is read structurally (`getattr(exc, "value", exc)`) so a generated WIT
    `Err` wrapper reports its real `db.error`/`kv.error` case. Never logs the exception text,
    which could carry a key or a stored value.
    """
    case_name = type(getattr(exc, "value", exc)).__name__
    log.error("poll.backend_error", op=op, error=case_name)
    await relay.push(ctx.provider, {"channel": ctx.channel_id, "text": _UNAVAILABLE_MSG})
    raise RuntimeError(f"poll {op} failed: {case_name}") from exc


async def _guarded(  # noqa: UP047 -- TypeVar: the embedded componentize-py CPython is not pinned
    ctx: _Ctx,
    op: str,
    call: Awaitable[_T],
    *,
    passthrough: tuple[type[Exception], ...] = (),
) -> _T:
    """Await one `kv`/`db` call; any failure goes through `_fail_backend` (log, reply, raise).

    `passthrough` names exception types the caller handles itself (e.g. `db.ConflictError`
    in the optimistic-concurrency close loop) -- re-raised untouched.
    """
    try:
        return await call
    except passthrough:
        raise
    except Exception as exc:  # noqa: BLE001 -- classified structurally, see `_fail_backend`
        await _fail_backend(exc, ctx, op)


def _decode_poll(row_id: str, row: dict[str, Any]) -> _Poll:
    """Decode and validate one `poll_records` row; raises `ValueError` on any corruption."""
    options = json.loads(str(row["options"]))
    if (
        not isinstance(options, list)
        or not options
        or not all(isinstance(o, str) for o in options)
    ):
        raise ValueError("options column is not a non-empty JSON array of strings")
    is_active = row["is_active"]
    if not isinstance(is_active, bool):
        raise ValueError("is_active column is not a boolean")
    raw_results = row.get("results")
    results: tuple[int, ...] | None = None
    if raw_results is not None:
        decoded = json.loads(str(raw_results))
        if (
            not isinstance(decoded, list)
            or len(decoded) != len(options)
            or not all(isinstance(c, int) and not isinstance(c, bool) for c in decoded)
        ):
            raise ValueError("results column does not match the options")
        results = tuple(decoded)
    if not is_active and results is None:
        raise ValueError("closed poll has no results snapshot")
    return _Poll(
        row_id=row_id,
        version=int(row["version"]),
        poll_id=int(row["poll_id"]),
        title=str(row["title"]),
        options=tuple(options),
        is_active=is_active,
        results=results,
    )


async def _load_poll(ctx: _Ctx, poll_id: int) -> _Poll | None:
    """Resolve a chat-visible poll id to its decoded row, or `None` if no such poll exists.

    An id the kv index knows but `db.get` cannot find, or a row that fails validation, is
    index/row corruption and fails loud -- never silently treated as "not found".
    """
    raw = await _guarded(ctx, "kv_get", community_kv.get(ctx.community, _rowid_key(poll_id)))
    if raw is None:
        return None
    try:
        row_id = raw.decode()
    except UnicodeDecodeError as exc:
        await _fail_backend(exc, ctx, "index_decode")
    row = await _guarded(ctx, "db_get", db.get(row_id))
    if row is None:
        await _fail_backend(RuntimeError("kv index points at missing db row"), ctx, "index_stale")
    try:
        return _decode_poll(row_id, row)
    except (ValueError, KeyError, TypeError) as exc:
        await _fail_backend(exc, ctx, "row_decode")


async def _read_tallies(ctx: _Ctx, poll: _Poll) -> list[int]:
    """Live per-option vote counts for `poll`, in option order (`0` for an unvoted option)."""
    counts: list[int] = []
    for option_no in range(1, len(poll.options) + 1):
        raw = await _guarded(
            ctx, "kv_get", community_kv.get(ctx.community, _tally_key(poll.poll_id, option_no))
        )
        if raw is None:
            counts.append(0)
            continue
        try:
            counts.append(int(raw.decode()))
        except (UnicodeDecodeError, ValueError) as exc:
            await _fail_backend(exc, ctx, "tally_decode")
    return counts


def _format_counts(poll: _Poll, counts: list[int]) -> str:
    """Render one `  N. option (K votes)` line per option."""
    lines = []
    for idx, (option, count) in enumerate(zip(poll.options, counts, strict=True), start=1):
        lines.append(f"  {idx}. {option} ({count} {'vote' if count == 1 else 'votes'})")
    return "\n".join(lines) + "\n"


async def _handle_create(ctx: _Ctx, args: str | None, actor: str | None) -> str:
    """`!poll add "title" "opt1" "opt2" ...` -- validate, allocate an id, insert, index."""
    if not args:
        return 'Usage: !poll add "title" "option1" "option2" ...'

    parts = [p.strip() for p in _parse_quoted_args(args)]
    if len(parts) < 1 + MIN_OPTIONS:
        return f"Poll must have a title and at least {MIN_OPTIONS} options."
    title, options = parts[0], parts[1:]
    if not title or not all(options):
        return "Poll title and options cannot be empty."
    if len(title) > MAX_TITLE_LEN:
        return f"Poll title must be {MAX_TITLE_LEN} characters or fewer."
    if len(options) > MAX_OPTIONS:
        return f"Polls support at most {MAX_OPTIONS} options."
    if any(len(o) > MAX_OPTION_LEN for o in options):
        return f"Poll options must be {MAX_OPTION_LEN} characters or fewer."
    if len({o.casefold() for o in options}) != len(options):
        return "Poll options must be unique."

    poll_id: int = await _guarded(
        ctx, "kv_increment", community_kv.increment(ctx.community, _SEQ_KEY, 1)
    )
    inserted = await _guarded(
        ctx,
        "db_insert",
        db.insert(
            {
                "poll_id": poll_id,
                "title": title,
                "options": json.dumps(options),
                "is_active": True,
                "created_by_hash": _actor_hash(actor),
                "results": None,
            }
        ),
    )
    row_id = str(inserted["row_id"])
    try:
        await community_kv.set(ctx.community, _rowid_key(poll_id), row_id.encode(), ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001 -- compensated, then classified by `_fail_backend`
        await _discard_orphan_row(row_id, int(inserted["version"]))
        await _fail_backend(exc, ctx, "kv_set")

    log.info("poll.created", community=ctx.community, option_count=len(options))
    reply = f"Poll created! ID: {poll_id}\nTitle: {title}\nOptions:\n"
    for idx, option in enumerate(options, start=1):
        reply += f"  {idx}. {option}\n"
    reply += f"Vote with: `!poll set {poll_id} <option_number>`"
    return reply


async def _discard_orphan_row(row_id: str, version: int) -> None:
    """Best-effort delete of a just-inserted row whose kv index write failed.

    Without the index the poll can never be reached by id, so leaving the row would strand an
    unreachable poll. A failed cleanup is logged loudly (the caller then raises the primary
    failure anyway) -- never swallowed silently.
    """
    try:
        await db.delete(row_id, version)
    except Exception as exc:  # noqa: BLE001 -- logged; the primary failure is raised next
        log.error("poll.orphan_cleanup_failed", error=type(getattr(exc, "value", exc)).__name__)


async def _handle_vote(ctx: _Ctx, args: str | None, actor: str | None) -> str:
    """`!poll set <poll_id> <option_number>` -- claim a one-vote marker, then count it."""
    if not args:
        return "Usage: !poll set <poll_id> <option_number>"
    parts = args.split()
    if len(parts) != 2 or not _POLL_ID_RE.match(parts[0]) or not _OPTION_NO_RE.match(parts[1]):
        return "Usage: !poll set <poll_id> <option_number> (both must be numeric)"
    poll_id, option_no = int(parts[0]), int(parts[1])

    poll = await _load_poll(ctx, poll_id)
    if poll is None or not poll.is_active:
        return f"Poll {poll_id} not found or is closed."
    if option_no < 1 or option_no > len(poll.options):
        return f"Invalid option number. Poll {poll_id} has {len(poll.options)} options."

    marker = _voted_key(poll_id, option_no, _voter_token(actor))
    claimed: int = await _guarded(
        ctx, "kv_claim", community_kv.increment(ctx.community, marker, 1, _KV_TTL_SECONDS)
    )
    if claimed != 1:
        log.debug("poll.vote_duplicate", community=ctx.community)
        return f"You already voted for option {option_no} on poll {poll_id}."
    try:
        await community_kv.increment(
            ctx.community, _tally_key(poll_id, option_no), 1, _KV_TTL_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 -- compensated, then classified by `_fail_backend`
        await _release_marker(ctx, marker)
        await _fail_backend(exc, ctx, "kv_tally")

    log.info("poll.vote_recorded", community=ctx.community)
    return f"Vote recorded for option {option_no} on poll {poll_id}!"


async def _release_marker(ctx: _Ctx, marker: str) -> None:
    """Best-effort release of a claimed vote marker whose tally increment failed.

    Releasing lets the voter retry; if the release itself fails it is logged loudly (the
    caller raises the primary failure next) so the stuck marker is visible, not silent.
    """
    try:
        await community_kv.delete(ctx.community, marker)
    except Exception as exc:  # noqa: BLE001 -- logged; the primary failure is raised next
        log.error("poll.marker_release_failed", error=type(getattr(exc, "value", exc)).__name__)


async def _handle_close(ctx: _Ctx, args: str | None) -> str:
    """`!poll remove <poll_id>` -- snapshot the tallies into `results` and end the poll.

    Version-gated: a concurrent close surfaces as `ConflictError`, re-reads the row, and
    (now inactive) replies with the already-snapshotted results instead of double-closing.
    """
    if not args or not _POLL_ID_RE.match(args.strip()):
        return "Usage: !poll remove <poll_id>"
    poll_id = int(args.strip())

    for _attempt in range(_MAX_CONFLICT_RETRIES):
        poll = await _load_poll(ctx, poll_id)
        if poll is None:
            return f"Poll {poll_id} not found."
        if not poll.is_active:
            return (
                f"Poll {poll_id} is already closed: {poll.title}\n\nResults:\n"
                + _format_counts(poll, list(poll.results or ()))
            )
        counts = await _read_tallies(ctx, poll)
        try:
            await _guarded(
                ctx,
                "db_update",
                db.update(
                    poll.row_id,
                    poll.version,
                    {"is_active": False, "results": json.dumps(counts)},
                ),
                passthrough=(db.ConflictError,),
            )
        except db.ConflictError:
            continue
        log.info("poll.closed", community=ctx.community, option_count=len(counts))
        return f"Poll {poll_id} closed: {poll.title}\n\nResults:\n" + _format_counts(poll, counts)
    await _fail_backend(RuntimeError("conflict retries exhausted"), ctx, "db_update_retry")


async def _handle_list(ctx: _Ctx) -> str:
    """`!poll list` -- the newest active polls (of the `_LIST_FETCH_LIMIT` newest rows)."""
    rows = await _guarded(
        ctx,
        "db_query",
        db.query(limit=_LIST_FETCH_LIMIT, order_by="poll_id", descending=True),
    )
    active = [r for r in rows if r.get("is_active") is True][:_LIST_SHOW]
    if not active:
        return "No active polls in this community."
    reply = "Active polls:\n"
    for row in active:
        reply += f"  - Poll {int(row['poll_id'])}: {row['title']}\n"
    reply += "\nView with: `!poll list <id>` | Vote with: `!poll set <id> <option>`"
    return reply


async def _handle_view(ctx: _Ctx, args: str | None) -> str:
    """`!poll list <poll_id>` -- one poll's options with live (open) or final (closed) counts."""
    if not args or not _POLL_ID_RE.match(args.strip()):
        return "Usage: !poll list <poll_id>"
    poll_id = int(args.strip())

    poll = await _load_poll(ctx, poll_id)
    if poll is None:
        return f"Poll {poll_id} not found."
    counts = await _read_tallies(ctx, poll) if poll.is_active else list(poll.results or ())
    status = "Active" if poll.is_active else "Closed"
    reply = f"Poll {poll_id}: {poll.title} [{status}]\n\n" + _format_counts(poll, counts)
    if poll.is_active:
        reply += f"\nVote with: `!poll set {poll_id} <option_number>`"
    return reply


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: permission gate, all `kv`/`db` work, then relay.

    Raises:
        ValueError: The payload has no `channel_id`; the envelope's community is the empty
            string (a caller bug -- `None` is the legitimate tenant-wide sentinel); or an
            unrecognized `action` (defensive -- `transform` only emits the five actions and
            `"usage"`).
        RuntimeError: A `kv`/`db` call failed (see `_fail_backend` -- a chat error reply and
            an ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("poll reply requires a channel_id from the inbound chat.message")
    provider = envelope.event.platform

    # A `transform`-built usage/error reply carries its own `text` already -- relay it
    # unchanged, no `kv`/`db` or community needed at all.
    if "text" in payload:
        await relay.push(provider, {"channel": channel_id, "text": payload["text"]})
        return DispatchResult(transport=provider, detail="usage")

    action = payload.get("action")
    if action not in _KNOWN_ACTIONS:
        raise ValueError(f"unrecognized poll action: {action!r}")

    community = envelope.community
    if community == "":
        log.error("poll.empty_community", action=action)
        raise ValueError("poll received an empty-string community (a caller-side bug)")
    ctx = _Ctx(
        provider=provider,
        channel_id=channel_id,
        community=community if community is not None else TENANT_WIDE_SENTINEL,
    )

    args = payload.get("args") if isinstance(payload.get("args"), str) else None
    actor = envelope.event.actor

    if action in ("create", "close") and not _is_privileged(payload):
        log.info("poll.permission_denied", action=action)
        await relay.push(provider, {"channel": channel_id, "text": _PERMISSION_DENIED_MSG})
        return DispatchResult(transport=provider, detail=f"{action}:denied")

    if action == "create":
        reply_text = await _handle_create(ctx, args, actor)
    elif action == "vote":
        reply_text = await _handle_vote(ctx, args, actor)
    elif action == "close":
        reply_text = await _handle_close(ctx, args)
    elif action == "list":
        reply_text = await _handle_list(ctx)
    else:  # view
        reply_text = await _handle_view(ctx, args)

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("poll.dispatch relayed", platform=provider, action=action)
    return DispatchResult(transport=provider, detail=action)
