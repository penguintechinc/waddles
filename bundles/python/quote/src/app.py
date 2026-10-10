"""`!quote` -> a per-community quote book, DB-backed (structured `db` facade, v2).

Strangler extraction of `core/svc_process/builtin_handlers/social_quote_process.py`'s
`!quote` command (`feature/bundle-quote`), modeled on `bundles/python/rank`'s
structured-`db` + `community_kv`-index template -- see that bundle's own
module docstring for the full rationale this one reuses byte-for-byte.
Commands: `!quote add <text>` (mod/broadcaster only), `!quote <id>`,
`!quote random`, `!quote list`, `!quote remove <id>` (mod/broadcaster only).
`add`/`list`/`remove` route through `waddle_sdk.command.parse_command`;
`random` and a bare numeric `<id>` are two deliberate pre-checks *outside*
`parse_command`'s fixed `VERBS` vocabulary (neither is a declared verb there
-- see `sdk/waddle-sdk/src/waddle_sdk/command.py`), the same layering
`rank`'s own `!rank <user>` extension uses.

**Ported off the retired DAL-style facade.** The original build of this
bundle used `waddle_sdk.db.create_dal()`'s `penguin_dal`-compatible
`TableProxy`/`AsyncQuerySet` query builder plus its raw-SQL `db.execute()`
escape hatch for the two `ORDER BY ... LIMIT` reads (`random`/`list`). Both
are retired (design doc SS1 round-1 CRITICAL finding: "no bundle-supplied
SQL, ever" -- see `sdk/waddle-sdk/src/waddle_sdk/db.py`'s own module
docstring). The replacement WIT `db` interface exposes exactly five
structured ops -- `insert`/`get`/`query`/`update`/`delete` -- with **no
column-equality filter and no `table` parameter** (a bundle owns exactly
one app-provisioned table, scoped per-community automatically by the host).
`db.query(random=True, limit=1)` and `db.query(order_by=..., limit=...)`
cover `random`/`list` directly, with no raw-SQL fallback needed. `db.get`
only takes a `row_id` (a host-assigned UUID, never a small sequential
int), so this bundle keeps a `community_kv` index exactly like `rank`'s own
`actor_hash -> row_id` index, except keyed by a short, chat-typeable
sequence number instead of a user pseudonym: `quote.rowid.<seq> -> row_id`.
`quote.seq.counter` (`community_kv.increment`, atomic) hands out that
sequence number on every `add`, so `!quote 1`/`!quote remove 1` keep working
as a short typed number, never a raw UUID in chat.

**Data model.** One app-owned `db` table (declared in `bundle.yaml`'s
`data.tables`), two columns: `seq` (the chat-visible sequence number, also
the `list`/`random` sort/display key) and `quote_text`. No `community_id`
column -- scoping is automatic server-side (`waddle_sdk.db`'s own
docstring), unlike the retired facade which had to filter every statement
on `community_id` by hand.

**No author/username column**, matching the original build's own PII
rationale: `event.actor` (and any chat-typed target) is a raw
platform-specific string, not a `hub_users` PK -- writing it into a table
the API server doesn't exclusively own would violate PII tokenization
(`critical-rules.md`). Every rendered quote shows `unknown` for the author,
same as before.

**Deletion is a real delete, not a soft-delete.** The retired facade's
`quotes.deleted_at` soft-delete column has no equivalent in the new
single-table-per-bundle schema (no extra platform-reserved columns beyond
`row_id`/`version`/`created_at`/`updated_at`); `db.delete(row_id,
expected_version)` removes the row outright, gated on the version read
moments earlier by `db.get` (optimistic concurrency, same `version` field
`rank`'s own update loop uses) -- a stale kv index after a concurrent
double-removal surfaces as a loud `index_stale`/`NotFoundError`, never a
silent no-op.

Permission model -- mirrors `count`/`rank`'s `_caller_role_signal()`
byte-for-byte: broadcaster/moderator only for `add`/`remove`; reading
(bare `<id>`, `random`, `list`) is open to anyone. Fails CLOSED, not open,
when `is_mod`/`is_broadcaster` aren't present as actual `bool`s on the
event (the same Discord-normalizer gap `count`'s own module docstring
describes).

Gated behind the PostHog flag ``waddles.command-quote`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (cheap command-match first, flag check second, real
grammar parse last).
"""

from __future__ import annotations

from typing import Any, NoReturn

from waddle_sdk import community_kv, db, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-quote"

_COMMAND_PREFIX = "!quote"
_SPEC = CommandSpec(name="quote", sub_modules=frozenset())

#: App-level bound on a saved quote's length -- the legacy transform enforced
#: none at all; the new per-bundle `quote_text` column is unbounded `TEXT`,
#: so this stays a deliberate guard against a pathological single message.
MAX_QUOTE_LEN = 500

#: `!quote list`'s result size -- a community-wide chat reply, not a paginated
#: admin view, so this stays small on purpose.
_LIST_LIMIT = 10

#: `.`-separated, never `:` -- see `rank`'s own module docstring for why a colon here would be
#: rejected by the real `kv` host capability (gh-631).
_INDEX_KEY_PREFIX = "quote.rowid."
_SEQ_COUNTER_KEY = "quote.seq.counter"

_USAGE = "Usage: !quote add <text> | !quote <id> | !quote random | !quote list | !quote remove <id>"
_PERMISSION_DENIED_ADD = "Only the broadcaster or a moderator can add quotes."
_PERMISSION_DENIED_REMOVE = "Only the broadcaster or a moderator can remove quotes."
_UNAVAILABLE_MSG = "Something went wrong accessing quote storage - please try again."

_KNOWN_COMMANDS = frozenset({"add", "list", "remove", "random", "get", "usage", "unknown"})


def _index_key(seq: int) -> str:
    """Per-(community, seq) `kv` key holding that quote's `db` `row_id` -- see module docstring."""
    return f"{_INDEX_KEY_PREFIX}{seq}"


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `count`/`rank`'s own identical helper -- `None` (neither `is_mod`/
    `is_broadcaster` present, e.g. Discord's normalizer today) must be
    treated as denied, never as an implicit allow.
    """
    is_mod = payload.get("is_mod")
    is_broadcaster = payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        return None
    return bool(is_mod) or bool(is_broadcaster)


def _resolve_command(parsed: ParsedCommand) -> tuple[str, str | None, str | None]:
    """Map a successful `parse_command()` result onto this bundle's commands.

    Returns `(command, arg, raw_option)` -- `arg` holds `add`'s raw text or
    `remove`'s raw target string, `None` otherwise; `raw_option` is only
    set for `command == "unknown"`, naming the unrecognized verb in the
    chat reply.
    """
    if parsed.option is None:
        return "usage", None, None
    if parsed.option == "add":
        return "add", parsed.args, None
    if parsed.option == "list":
        return "list", None, None
    if parsed.option == "remove":
        return "remove", (parsed.args or "").strip(), None
    return "unknown", None, parsed.option


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!quote` and its grammar.

    Cheap-skip first (exact `!quote` first token -- `None`, zero cost; a
    `startswith` check alone would wrongly match `!quotex`/`!quoter`, the
    same first-token-equality guard `count`/`rank`'s own `transform()`
    use), flag check second, real grammar parse last. Builds a normalized
    command payload for `dispatch` to act on; all `kv`/`db` I/O and
    permission checks happen there, mirroring `rank`'s own transform/
    dispatch split.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head = stripped.partition(" ")[0]
    if head.lower() != _COMMAND_PREFIX:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    after_prefix = stripped[len(_COMMAND_PREFIX) :].strip()
    command: str
    arg: str | None = None
    raw_option: str | None = None

    if not after_prefix:
        command = "usage"
    else:
        first_token = after_prefix.split(maxsplit=1)[0].lower()
        if first_token == "random":  # noqa: S105 - the `random` sub-command, not a secret
            command = "random"
        elif first_token.isdigit():
            command, arg = "get", first_token
        else:
            try:
                parsed: ParsedCommand = parse_command(stripped, _SPEC)
            except CommandUsageError as exc:
                command, arg = "usage", str(exc)
            else:
                command, arg, raw_option = _resolve_command(parsed)

    log.info("quote.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if arg is not None:
        payload["arg"] = arg
    if raw_option is not None:
        payload["raw_option"] = raw_option
    # Forward the normalized badge signal, if present -- see `count`/`rank`'s own identical
    # forwarding comment for why absence must reach `dispatch` as absence, not `False`.
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


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def _fail_backend(exc: Exception, *, provider: str, channel_id: str, op: str) -> NoReturn:
    """Fail-loud backend error path: log (PII-free: op + exception type only), reply, re-raise.

    Shared by every `kv`/`db` call site -- see `rank`'s own `_fail_backend()`
    for the identical structural-classification pattern this mirrors. Never
    logs quote text, a target id, or any chat-typed value.
    """
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("quote.backend_error", op=op, error=case_name)
    await relay.push(provider, {"channel": channel_id, "text": _UNAVAILABLE_MSG})
    raise RuntimeError(f"quote {op} failed: {case_name}") from exc


async def _kv_get_rowid(community: str, seq: int, *, provider: str, channel_id: str) -> str | None:
    """Look up the `db` `row_id` for sequence number `seq`, or `None` if it has none."""
    try:
        raw = await community_kv.get(community, _index_key(seq))
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_backend`
        await _fail_backend(exc, provider=provider, channel_id=channel_id, op="kv_get")
    return raw.decode() if raw is not None else None


async def _kv_set_rowid(
    community: str, seq: int, row_id: str, *, provider: str, channel_id: str
) -> None:
    """Persist `seq`'s `db` `row_id` into the lookup index."""
    try:
        await community_kv.set(community, _index_key(seq), row_id.encode(), ttl_seconds=0)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_backend`
        await _fail_backend(exc, provider=provider, channel_id=channel_id, op="kv_set")


async def _kv_delete_rowid(community: str, seq: int, *, provider: str, channel_id: str) -> None:
    """Remove `seq`'s lookup-index entry after its row is deleted."""
    try:
        await community_kv.delete(community, _index_key(seq))
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_backend`
        await _fail_backend(exc, provider=provider, channel_id=channel_id, op="kv_delete")


async def _next_seq(community: str, *, provider: str, channel_id: str) -> int:
    """Atomically hand out the next chat-visible sequence number for `community`."""
    try:
        seq: int = await community_kv.increment(community, _SEQ_COUNTER_KEY, 1)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_backend`
        await _fail_backend(exc, provider=provider, channel_id=channel_id, op="kv_increment")
    return seq


async def _db_get(row_id: str, *, provider: str, channel_id: str) -> dict[str, Any] | None:
    """`waddle_sdk.db.get`, fail-loud on a backend error (see `_fail_backend`)."""
    try:
        result: dict[str, Any] | None = await db.get(row_id)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_backend`
        await _fail_backend(exc, provider=provider, channel_id=channel_id, op="db_get")
    return result


async def _db_insert(row: dict[str, Any], *, provider: str, channel_id: str) -> dict[str, Any]:
    """`waddle_sdk.db.insert`, fail-loud on a backend error (see `_fail_backend`/`_db_get`)."""
    try:
        inserted: dict[str, Any] = await db.insert(row)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_backend`
        await _fail_backend(exc, provider=provider, channel_id=channel_id, op="db_insert")
    return inserted


async def _db_query(
    *,
    random: bool,
    order_by: str | None,
    descending: bool,
    limit: int,
    provider: str,
    channel_id: str,
) -> list[dict[str, Any]]:
    """`waddle_sdk.db.query`, fail-loud on a backend error (see `_fail_backend`/`_db_get`)."""
    try:
        rows: list[dict[str, Any]] = await db.query(
            limit=limit, order_by=order_by, descending=descending, random=random
        )
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_backend`
        await _fail_backend(exc, provider=provider, channel_id=channel_id, op="db_query")
    return rows


async def _db_delete(row_id: str, expected_version: int, *, provider: str, channel_id: str) -> None:
    """`waddle_sdk.db.delete`, fail-loud on a backend error (see `_fail_backend`/`_db_get`)."""
    try:
        await db.delete(row_id, expected_version)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_backend`
        await _fail_backend(exc, provider=provider, channel_id=channel_id, op="db_delete")


def _format_quote(seq: int, quote_text: str) -> str:
    """Render one quote row. No author shown -- see module docstring's PII rationale."""
    return f'#{seq}: "{quote_text}" — unknown'


async def _handle_add(
    community: str, text_raw: str | None, *, provider: str, channel_id: str
) -> str:
    """Validate + insert one new quote, assigning it the next chat-visible sequence number."""
    quote_text = (text_raw or "").strip()
    if not quote_text:
        return "Usage: !quote add <text>"
    if len(quote_text) > MAX_QUOTE_LEN:
        return f"Quotes must be {MAX_QUOTE_LEN} characters or fewer."
    seq = await _next_seq(community, provider=provider, channel_id=channel_id)
    inserted = await _db_insert(
        {"seq": seq, "quote_text": quote_text}, provider=provider, channel_id=channel_id
    )
    await _kv_set_rowid(
        community, seq, str(inserted["row_id"]), provider=provider, channel_id=channel_id
    )
    log.info("quote.created", command="add")
    return f"Saved as quote #{seq}."


async def _handle_get(community: str, target: str | None, *, provider: str, channel_id: str) -> str:
    """Render the quote at the typed sequence number, or a not-found reply.

    `target` is always a digit string here -- `transform` only ever emits
    `command="get"` after its own `first_token.isdigit()` pre-check.
    """
    if target is None:
        raise ValueError("quote get dispatched with no target sequence number")
    seq = int(target)
    row_id = await _kv_get_rowid(community, seq, provider=provider, channel_id=channel_id)
    if row_id is None:
        return f"Quote #{seq} not found."
    row = await _db_get(row_id, provider=provider, channel_id=channel_id)
    if row is None:
        await _fail_backend(
            RuntimeError(f"kv index points at missing db row for seq {seq}"),
            provider=provider,
            channel_id=channel_id,
            op="index_stale",
        )
    return _format_quote(int(row["seq"]), str(row["quote_text"]))


async def _handle_random(community: str, *, provider: str, channel_id: str) -> str:
    """Render one random quote, or a not-found reply if none exist yet."""
    rows = await _db_query(
        random=True,
        order_by=None,
        descending=False,
        limit=1,
        provider=provider,
        channel_id=channel_id,
    )
    if not rows:
        return "No quotes found."
    row = rows[0]
    return _format_quote(int(row["seq"]), str(row["quote_text"]))


async def _handle_list(community: str, *, provider: str, channel_id: str) -> str:
    """Render up to `_LIST_LIMIT` quote numbers, most-recently-added first."""
    rows = await _db_query(
        random=False,
        order_by="seq",
        descending=True,
        limit=_LIST_LIMIT,
        provider=provider,
        channel_id=channel_id,
    )
    if not rows:
        return "No quotes have been saved yet."
    return "Recent quotes: " + ", ".join(f"#{int(r['seq'])}" for r in rows)


async def _handle_remove(
    community: str, target: str | None, *, provider: str, channel_id: str
) -> str:
    """Delete the quote at the typed sequence number -- real delete, not soft-delete."""
    if target is None or not target.isdigit():
        return "Usage: !quote remove <id>"
    seq = int(target)
    row_id = await _kv_get_rowid(community, seq, provider=provider, channel_id=channel_id)
    if row_id is None:
        return f"Quote #{seq} not found."
    row = await _db_get(row_id, provider=provider, channel_id=channel_id)
    if row is None:
        await _fail_backend(
            RuntimeError(f"kv index points at missing db row for seq {seq}"),
            provider=provider,
            channel_id=channel_id,
            op="index_stale",
        )
    await _db_delete(row_id, int(row["version"]), provider=provider, channel_id=channel_id)
    await _kv_delete_rowid(community, seq, provider=provider, channel_id=channel_id)
    log.info("quote.removed", command="remove")
    return f"Removed quote #{seq}."


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: permission checks, all `kv`/`db` I/O, then relay.

    Raises:
        ValueError: The envelope's payload has no `channel_id`; the
            envelope has no `community` (no tenant-wide fallback -- `db`
            rows are always community-scoped); or an unrecognized
            `command` (defensive -- `transform` only ever emits a member of
            `_KNOWN_COMMANDS`).
        RuntimeError: A `kv`/`db` backend call failed (see `_fail_backend`
            -- a chat error reply and an ERROR log line are always emitted
            first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("quote reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized quote command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("quote.missing_community", command=command)
        raise ValueError("quote requires a community context and cannot operate tenant-wide")

    if command == "usage":
        arg = payload.get("arg")
        reply_text = f"{arg} | {_USAGE}" if isinstance(arg, str) else _USAGE
    elif command == "unknown":
        raw_option = payload.get("raw_option", "")
        reply_text = f"Unknown quote command '{raw_option}'. {_USAGE}"
    elif command == "add":
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("quote.permission_denied", command="add")
            reply_text = _PERMISSION_DENIED_ADD
        else:
            reply_text = await _handle_add(
                community, payload.get("arg"), provider=provider, channel_id=channel_id
            )
    elif command == "get":
        reply_text = await _handle_get(
            community, payload.get("arg"), provider=provider, channel_id=channel_id
        )
    elif command == "random":
        reply_text = await _handle_random(community, provider=provider, channel_id=channel_id)
    elif command == "list":
        reply_text = await _handle_list(community, provider=provider, channel_id=channel_id)
    else:  # command == "remove"
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("quote.permission_denied", command="remove")
            reply_text = _PERMISSION_DENIED_REMOVE
        else:
            reply_text = await _handle_remove(
                community, payload.get("arg"), provider=provider, channel_id=channel_id
            )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("quote.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
