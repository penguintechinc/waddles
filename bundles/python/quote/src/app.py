"""`!quote` -> a per-community quote book backed by the shared Postgres `quotes` table.

Strangler extraction of `core/svc_process/bundles/social_quote_process.py`'s
`!quote` command (`feature/bundle-quote`) into its own componentize-py
bundle, modeled on `bundles/python/count`'s hand-built (not
`bundle_compiler`-routed) structure. Promotes the legacy transform's three
commands (`!quote add <text>`, `!quote <id>`, `!quote random`) to the
standard grammar's full verb set -- `add`/`list`/`remove` via
`waddle_sdk.command.parse_command`, plus `random` and a bare numeric `<id>`
handled as two deliberate pre-checks *outside* `parse_command`'s fixed
`VERBS` vocabulary (neither is a declared verb there -- see
`sdk/waddle-sdk/src/waddle_sdk/command.py`): a bundle is free to recognize
additional first-token shapes before falling through to the standard
grammar, same way `!lurk enable ai`'s own sub-module shape layers on top of
it. `!quote` bare (no further tokens) replies with usage, matching the
legacy transform's own help-on-no-args behavior.

**BUILD-ONLY / INERT.** Ships behind `waddles.command-quote`, default OFF,
and `bot_process._FEATURE_MODULES` (the legacy strangler registry) still
owns live `quote` traffic -- this bundle has not yet been connected into
the cut-over. That happens after the activation-gate P4 work lands;
removing `quote` from `_FEATURE_MODULES` before then would double-reply or
disable the command entirely. See the PR description for the explicit
follow-up.

Storage -- deliberately Postgres, NOT `kv`: `quotes`
(`config/postgres/migrations/015_add_quote_tables.sql`) is a table shared
with hub-api's own admin/moderation surface, so this bundle uses
`waddle_sdk.db`'s structured, `penguin_dal`-compatible facade
(`TableProxy.async_insert`/`__getitem__`-style query/`AsyncQuerySet.update`)
over the WIT `db` import, declaring the `storage.tables` capability
(`core/bundle_host_db/src/authorize.rs::DB_PERMISSION_ID`) in `bundle.yaml`
-- never `storage.kv`. Two reads (`random`, `list`) need `ORDER BY`/`LIMIT`,
which the structured query builder does not support --
`AsyncQuerySet.select(orderby=..., limitby=...)` raises loudly rather than
silently truncating or misordering results (see `waddle_sdk/db.py`), so
those two calls use
`AsyncDB.execute()`'s own documented raw-SQL escape hatch instead, fully
parameterized (`$1`/`$2` placeholders, never f-string interpolation of a
caller-supplied value) -- every other operation (insert/get/update) goes
through the structured table/query-set API per the task's db-client
requirement.

Scope -- PER-COMMUNITY: every query filters on `community_id`, read from
`event.payload["community_id"]` (the convention `core/svc_process/tests/
test_runner.py` documents; unlike `kv`, the WIT `db` import has no
host-side auto-scoping, so this bundle must -- and does -- filter
explicitly on every statement). A missing/non-int `community_id` fails
loud (`quote.community_id_missing`) rather than guessing or querying
unscoped.

PII -- `quoted_username`/`quoted_user_id`/`added_by_user_id` are left
`NULL` on every insert. The legacy transform never resolved them either
(deferred to its action stage, not shown in `social_quote_process.py`'s
`transform`); this bundle has no safe way to turn `event.actor` (a raw
platform-specific string, not a `hub_users` PK) into either column without
writing un-tokenized PII into a table the API server doesn't exclusively
own (`critical-rules.md` PII Tokenization) -- a known, documented gap this
extraction does not attempt to close.

Permission model -- mirrors `count`'s `_is_privileged()` byte-for-byte:
broadcaster/moderator only for `add`/`remove`; reading (bare `<id>`,
`random`, `list`) is open to anyone. **Fails closed, not open** when
`is_mod`/`is_broadcaster` aren't present as actual `bool`s on the event --
see `count`'s own module docstring for the Discord-normalizer gap this
inherits unchanged.

Gated behind the PostHog flag ``waddles.command-quote`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering.
"""

from __future__ import annotations

from typing import Any, cast

from waddle_sdk import clock, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.db import create_dal
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-quote"

_COMMAND_PREFIX = "!quote"
_SPEC = CommandSpec(name="quote", sub_modules=frozenset())

#: App-level bound on a saved quote's length -- the legacy transform enforced
#: none at all (free-text `rest` straight into the action-stage payload); the
#: `quotes.quote_text` column is unbounded `TEXT`, so this is a new, deliberate
#: guard against a pathological single message, not a port of existing behavior.
MAX_QUOTE_LEN = 500

#: `!quote list`'s result size -- a community-wide chat reply, not a paginated
#: admin view, so this stays small on purpose.
_LIST_LIMIT = 10

_USAGE = "Usage: !quote add <text> | !quote <id> | !quote random | !quote list | !quote remove <id>"


class _DbFailure(Exception):
    """Internal-only: a `db` host-call failed. Always caught inside `transform`, never leaked.

    Mirrors `count`'s own `_KvFailure` pattern (`waddle_sdk.db` does not
    classify or catch the generated WIT `Err`/`DALError` itself).
    """


def _classify(exc: Exception, what: str) -> _DbFailure:
    """Reclassify any `db` facade failure (`DALError` or a raised WIT `Err`) into `_DbFailure`."""
    detail = getattr(exc, "value", exc)
    return _DbFailure(f"{what} failed: {detail}")


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- fails CLOSED (rejects) when role info isn't on the event.

    Byte-for-byte the same contract as `count._is_privileged()` -- see that
    bundle's module docstring for where `core/svc_ingest/src/normalize.rs`
    populates (Twitch) or omits (Discord, today) `is_mod`/`is_broadcaster`.
    """
    is_mod = event.payload.get("is_mod")
    is_broadcaster = event.payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("quote.role_info_unavailable", platform=event.platform)
        return False
    return bool(is_mod) or bool(is_broadcaster)


def _format_quote(row: Any) -> str:
    """Render one `quotes` row (a `Row` from a structured select, or a plain dict from `execute`)."""
    author = row.get("quoted_username") or "unknown"
    return f'#{row["id"]}: "{row["quote_text"]}" — {author}'


async def _db_insert(db: Any, community_id: int, quote_text: str, platform: str) -> int:
    """Insert one new quote row and return its new id."""
    try:
        quote_id = await db.quotes.async_insert(
            community_id=community_id,
            quote_text=quote_text,
            platform=platform,
            is_approved=True,
        )
    except Exception as exc:  # noqa: BLE001 - see module docstring, mirrors count._kv_*
        raise _classify(exc, "quotes insert") from exc
    return cast(int, quote_id)


async def _db_get(db: Any, quote_id: int, community_id: int) -> Any:
    """Fetch one non-deleted quote by id, scoped to `community_id`. `None` if not found."""
    try:
        rows = await db(
            (db.quotes.id == quote_id)
            & (db.quotes.community_id == community_id)
            & (db.quotes.deleted_at == None)  # noqa: E711 - FieldProxy.__eq__(None) -> IS NULL
        ).select()
    except Exception as exc:  # noqa: BLE001
        raise _classify(exc, "quotes get") from exc
    return rows.first()


async def _db_random(db: Any, community_id: int) -> Any:
    """Fetch one random approved quote for `community_id`. `None` if none exist.

    `ORDER BY RANDOM() LIMIT 1` -- the structured query builder has no
    `orderby`/`limitby` support (see this module's own docstring), so this
    uses `AsyncDB.execute()`'s raw-SQL escape hatch. Still fully
    parameterized -- `community_id` is the only interpolated value, and it
    crosses as a `$1` placeholder, never string-formatted into the SQL text.
    """
    try:
        rows = await db.execute(
            "SELECT id, quote_text, quoted_username FROM quotes "
            "WHERE community_id = $1 AND deleted_at IS NULL AND is_approved = TRUE "
            "ORDER BY RANDOM() LIMIT 1",
            [community_id],
        )
    except Exception as exc:  # noqa: BLE001
        raise _classify(exc, "quotes random") from exc
    return rows[0] if rows else None


async def _db_list(db: Any, community_id: int, limit: int) -> list[Any]:
    """Fetch up to `limit` most-recent non-deleted quotes for `community_id`.

    Same raw-SQL escape hatch as `_db_random`, for the same `ORDER BY ...
    LIMIT` reason -- `community_id`/`limit` cross as `$1`/`$2` placeholders.
    """
    try:
        rows = await db.execute(
            "SELECT id, quote_text, quoted_username FROM quotes "
            "WHERE community_id = $1 AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT $2",
            [community_id, limit],
        )
    except Exception as exc:  # noqa: BLE001
        raise _classify(exc, "quotes list") from exc
    return cast(list[Any], rows)


async def _db_soft_delete(db: Any, quote_id: int, community_id: int) -> int:
    """Soft-delete one quote (`deleted_at = now`), scoped to `community_id`.

    Returns the number of rows affected (0 if no matching, non-deleted row
    existed for this community). Uses `waddle_sdk.clock.now_rfc3339()` --
    the only time source available inside the sandbox (see that module's
    own docstring); never a bare `datetime.now()`.
    """
    try:
        rows_affected = await db(
            (db.quotes.id == quote_id)
            & (db.quotes.community_id == community_id)
            & (db.quotes.deleted_at == None)  # noqa: E711
        ).update(deleted_at=clock.now_rfc3339())
    except Exception as exc:  # noqa: BLE001
        raise _classify(exc, "quotes remove") from exc
    return cast(int, rows_affected)


async def _dispatch_parsed(
    parsed: ParsedCommand, event: PlatformEvent, db: Any, community_id: int
) -> str:
    """Handle a `parse_command()`-recognized `add`/`list`/`remove` (or unsupported verb)."""
    if parsed.option is None:
        return _USAGE

    if parsed.option == "add":
        if not _is_privileged(event):
            log.info("quote.permission_denied", action="add")
            return "Only the broadcaster or a moderator can add quotes."
        quote_text = (parsed.args or "").strip()
        if not quote_text:
            return "Usage: !quote add <text>"
        if len(quote_text) > MAX_QUOTE_LEN:
            return f"Quotes must be {MAX_QUOTE_LEN} characters or fewer."
        quote_id = await _db_insert(db, community_id, quote_text, event.platform)
        log.info("quote.created", quote_id=quote_id, community_id=community_id)
        return f"Saved as quote #{quote_id}."

    if parsed.option == "list":
        rows = await _db_list(db, community_id, _LIST_LIMIT)
        if not rows:
            return "No quotes have been saved yet."
        return "Recent quotes: " + ", ".join(f"#{r['id']}" for r in rows)

    if parsed.option == "remove":
        if not _is_privileged(event):
            log.info("quote.permission_denied", action="remove")
            return "Only the broadcaster or a moderator can remove quotes."
        target = (parsed.args or "").strip()
        if not target.isdigit():
            return "Usage: !quote remove <id>"
        rows_affected = await _db_soft_delete(db, int(target), community_id)
        if rows_affected == 0:
            return f"Quote #{target} not found."
        log.info("quote.removed", quote_id=int(target), community_id=community_id)
        return f"Removed quote #{target}."

    return f"Unknown quote command '{parsed.option}'. {_USAGE}"


async def _route(text: str, event: PlatformEvent, db: Any, community_id: int) -> str:
    """Resolve one `!quote ...` message to a reply string.

    Pre-checks `random` and a bare numeric id (neither is in
    `waddle_sdk.command.VERBS`) before falling through to
    `parse_command()` for the standard `add`/`list`/`remove` grammar -- see
    this module's own docstring for why both pre-checks exist.
    """
    after_prefix = text[len(_COMMAND_PREFIX) :].strip()
    if not after_prefix:
        return _USAGE

    first_token = after_prefix.split(maxsplit=1)[0].lower()

    if first_token == "random":
        row = await _db_random(db, community_id)
        return _format_quote(row) if row is not None else "No quotes found."

    if first_token.isdigit():
        quote_id = int(first_token)
        row = await _db_get(db, quote_id, community_id)
        return _format_quote(row) if row is not None else f"Quote #{quote_id} not found."

    parsed = parse_command(text, _SPEC)
    return await _dispatch_parsed(parsed, event, db, community_id)


def _reply(event: PlatformEvent, text: str) -> PlatformEvent:
    """Build the outbound `PlatformEvent` `transform` returns -- same minimal shape as `count`."""
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"channel_id": event.payload.get("channel_id"), "text": text},
        occurred_at=event.occurred_at,
    )


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!quote ...`, reply, or return `None`.

    Returns `None` while `waddles.command-quote` is disabled, for any
    non-chat payload, and for text with no leading `!quote` (cheap-skip,
    zero feature-flag/db cost). On a missing/non-int `community_id`, or any
    `db` failure, logs loudly and still returns a reply -- never silently
    drops the message, never crashes `transform`.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped.lower().startswith(_COMMAND_PREFIX):
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    community_id = event.payload.get("community_id")
    if not isinstance(community_id, int):
        log.error("quote.community_id_missing", platform=event.platform)
        return _reply(event, "Quote storage is unavailable right now - please try again.")

    db = create_dal()
    try:
        reply = await _route(stripped, event, db, community_id)
    except CommandUsageError as exc:
        reply = f"{exc} | {_USAGE}"
    except _DbFailure as exc:
        log.error("quote.db_failure", error=str(exc))
        reply = "Something went wrong accessing quote storage - please try again."

    log.info("quote.transform matched", community_id=community_id)
    return _reply(event, reply)


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay the reply text `transform` already built.

    All `db` work happens in `transform` (same split as `count`'s own `kv`
    work, and for the same reason: the lookup/mutation decision has to be
    known before `transform` can decide what text to produce) -- this is a
    pure relay.

    Raises:
        ValueError: The envelope's payload is missing `channel_id` or
            `text` (defensive -- `transform` always sets both when it
            returns a non-`None` event).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError(
            "quote reply requires a channel_id from the inbound chat.message"
        )
    if not isinstance(text, str) or not text:
        raise ValueError("quote reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("quote.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
