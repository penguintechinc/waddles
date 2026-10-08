"""`!inventory`/`!inv` -> a community-scoped, per-user item collection, DB-backed (v1).

Inspiration credit (not a literal port -- see `bundle.yaml`'s `author`/
`notice`): the general shape of "every viewer can accrue a personal
collection of named items, mods can grant/revoke them, viewers can trade
items with each other" is common across Twitch economy bots, including
superpenguintv (Psychoboy)'s `PenguinTwitchBot`
(https://github.com/Psychoboy/PenguinTwitchBot). No original source code or
text is reused here -- the command grammar, data model, optimistic-
concurrency transfer logic, and listing rendering below are written fresh
for Waddles, so no MIT notice reproduction is required (same convention
`bundles/python/loyalty/src/app.py`'s own docstring documents, contrast
`bundles/csharp/superpenguin-roll`, an actual line-for-line port which does
carry the full verbatim notice).

Uses `waddle_sdk.command.parse_command`/`CommandSpec` for the `add`/
`remove` verb shapes. **Three documented, deliberate extensions beyond the
shared grammar**, all handled in `_resolve_command()`:

1. Two command aliases, `!inventory` and `!inv`, both trigger this bundle
   (`parse_command()`/`CommandSpec` know only a single `name`) --
   `transform()` normalizes either leading token onto the canonical
   `!inventory` text before parsing, so the rest of the grammar is a single
   code path.
2. A bare `!inv <user>` (a chat-typed name/handle with no verb) is not
   expressible through `parse_command()` alone -- an arbitrary single token
   is neither a declared verb nor a declared sub-module, so it correctly
   raises `CommandUsageError`. `_resolve_command()` catches exactly that
   shape (a single whitespace-free token) and treats it as a target-user
   inventory listing; same convention as `loyalty`'s own `!points <user>`
   extension.
3. `!inv give <item> <user>` -- `give` is not a member of
   `waddle_sdk.command.VERBS` (only `set`/`add`/`sub`/`enable`/`disable`/
   `remove`/`delete`/`list`/`reset` are), so `parse_command()` also rejects
   this shape. `_resolve_command()` recognizes the literal `give <item>
   <user>` free-text tail the same way it recognizes the bare-token case
   above.

Anything else that fails to parse is a real usage error.

## Data model

One app-owned `db` table (`inventory_items`, declared in `bundle.yaml`'s
`data.tables` -- the signal that grants this bundle the `db` capability,
`hub_api/services/bundle_approval_service.py::_derive_capabilities`), rows
scoped per-community automatically by the host. Three columns: `actor_hash`
(SHA-256 hex pseudonym of the item-holder -- never a raw username, same
`_pseudonym()` convention as `fish`/`loyalty`/`count`), `item` (normalized
item name), and `quantity` (integer count of that item the holder owns).
One row per `(actor_hash, item)` pair -- a user can own many distinct
items, unlike `loyalty`'s single balance-per-user row.

**Why `kv` holds a per-user *directory*, not a single index value.** The
committed `wit/waddle-bundle/stage.wit` `db` interface exposes exactly
`insert`/`get`/`query`/`update`/`delete` -- `get` takes only a `row-id`,
and `query` has no column-equality filter at all (only `limit`/`offset`/
`order-by`). There is therefore no way to ask "give me every row this user
owns" directly from `db`, and no way to ask "give me the row for this
user's `fish` item" either, other than by its platform-assigned `row_id`.
`loyalty` solved the single-row-per-user case with one `kv` key per user
pointing at one `row_id`; this bundle owns *multiple* rows per user, so the
`kv` value itself is a small JSON object, `{item: row_id, ...}` -- one key
per user (`inventory.dir.<pseudonym>`), not one key per `(user, item)`
pair. This directory is the only way this bundle can answer "list every
item this user owns" (`!inventory`/`!inv <user>`) without an unbounded
`db.query()` scan across the whole community's rows (also wrong at scale:
`query()`'s `limit` is host-clamped to 200 rows,
`core/bundle_host_db/src/limits.rs::MAX_QUERY_LIMIT`), and it also gives
O(1) by-item lookup for `give`/`add`/`remove` (a dict-key lookup inside the
decoded JSON, no extra round trip).

**Known, honestly-documented concurrency limitation.** `kv.set` has no
compare-and-swap -- two concurrent *first-time* grants of two *different*
items to the same user can race on writing this directory value, and
whichever `kv.set` wins last can silently drop the other's new directory
entry (the underlying `db` row for the dropped item still exists --
nothing is lost from the ledger, only the user's own at-a-glance listing
of it, until the next successful directory write for that user touches the
same item again). This is the exact same class of gap `loyalty`'s own
module docstring documents for its single-value index (two concurrent
first-time balance grants to the same user can race the same way) -- not a
new weakness introduced here, just the multi-item shape of the identical
limitation. Every directory write here re-fetches the directory
immediately beforehand to keep the race window as small as practical (see
`_increment_item`/`_maybe_cleanup_zero_row`), the same mitigation
`loyalty` does not need (it has only one directory entry per user) but
this bundle's richer shape does.

**Listing cannot show real display names for items the caller doesn't
already know the owner of.** Like `loyalty`'s leaderboard, there is no
reverse lookup from a stored `actor_hash` back to a chat-visible name
(`critical-rules.md` PII Tokenization). `!inventory`/`!inv <user>` always
have a live, chat-typed name available (the caller's own actor, or the
target text the caller just typed) and echo that straight back into the
reply -- never persisted, identical to `fish`/`loyalty`'s own documented
convention.

**Known, pre-existing platform gap this bundle does not fix.** The richer
declarative `data.table.columns[]` schema (`hub_api/services/
bundle_data_schema.py`) that would let hub-api provision this bundle's
table columns automatically has, by that module's own docstring, no
onboarding-time wiring yet -- identical note to `loyalty`'s own docstring.

Gated behind the PostHog flag ``waddles.command-inventory`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (cheap command-match first, flag check second, real
grammar parse last).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, NoReturn

from waddle_sdk import community_kv, db, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-inventory"

#: Canonical command name `parse_command()` is invoked with -- both `!inventory` and `!inv`
#: (this bundle's two chat aliases) are normalized onto this before parsing, see `transform()`.
_COMMAND_NAME = "inventory"
#: This bundle declares no sub-modules of its own -- `give` is handled as a documented grammar
#: extension (see module docstring), never a declared sub-module.
SPEC = CommandSpec(name=_COMMAND_NAME, sub_modules=frozenset())

_ALIASES = ("!inventory", "!inv")
_DIR_KEY_PREFIX = "inventory.dir."
_MAX_LIST_ITEMS = 25
#: Bounded optimistic-concurrency retry budget for a single row mutation -- see
#: `_db_update_with_retry()`. Five attempts absorbs ordinary concurrent-writer contention without
#: looping forever on a genuinely stuck row.
_MAX_CONFLICT_RETRIES = 5

_USAGE = (
    "Usage: !inventory | !inv <user> | !inv give <item> <user> | "
    "!inv add <item> <user> | !inv remove <item> <user> "
    "(add/remove are broadcaster/mod only)"
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can adjust inventories"
_UNAVAILABLE_MSG = "inventory is temporarily unavailable, try again shortly."

_KNOWN_COMMANDS = frozenset(
    {"list_self", "list_other", "give", "add", "remove", "usage"}
)


class _InsufficientQuantityError(Exception):
    """Raised by a decrement's mutate callback when the row has nothing left to take.

    Caught by the caller as a benign "nothing to give/remove" user-facing
    reply -- never a backend failure. See `_decrement_item()`.
    """


def _pseudonym(identity: str | None) -> str:
    """Non-reversible per-identity key component -- see `loyalty`/`fish`'s own `_pseudonym()`.

    `event.actor` (and any chat-typed target name) may currently be a raw
    username (tokenization pipeline #429 not yet merged); hashing it before
    it ever reaches `community_kv`/`db` keeps this bundle PII-safe today and
    after #429 lands unchanged.
    """
    return hashlib.sha256((identity or "anonymous").encode()).hexdigest()


def _normalize_target(raw: str) -> str:
    """Normalize a chat-typed target user -- see `loyalty`'s own identical helper.

    Strips one leading `@` (common mention syntax) and lower-cases, so
    `!inv @Alice` and `!inv alice` resolve to the same pseudonym. Without a
    tokenization/identity-resolution service (#429 not yet merged) a typed
    display name is the only identity signal this bundle receives for
    someone other than the current caller.
    """
    cleaned = raw.strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    return cleaned.lower()


def _normalize_item(raw: str) -> str:
    """Normalize a chat-typed item name -- strip + lower-case, same rule as `_normalize_target`."""
    return raw.strip().lower()


def _dir_key(pseudonym: str) -> str:
    """Per-(community, user) `kv` key holding that user's `item -> db row_id` directory JSON."""
    return f"{_DIR_KEY_PREFIX}{pseudonym}"


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `fish`/`loyalty`/`count`/`lurk`'s own identical helper -- `None`
    (neither `is_mod`/`is_broadcaster` present, e.g. Discord's normalizer
    today) must be treated as denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!inventory`/`!inv` and its grammar.

    Cheap-skip first (no leading `!inventory`/`!inv` token -- `None`, zero
    cost), flag check second, real grammar parse last -- same ordering as
    `eightball`/`fish`/`loyalty`'s own documented rationale. A recognized-
    but-malformed input still produces a reply (`"usage"`) since the
    caller did invoke this command -- never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    if head.lower() not in _ALIASES:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    rest = rest.strip()
    # Normalize either alias onto the canonical `!inventory` head so the rest of the grammar
    # (and `parse_command()`) is a single code path -- see module docstring, extension 1.
    normalized = f"!{_COMMAND_NAME} {rest}".strip() if rest else f"!{_COMMAND_NAME}"
    try:
        parsed: ParsedCommand | None = parse_command(normalized, SPEC)
    except CommandUsageError:
        parsed = None

    command, item, target = _resolve_command(parsed, rest)

    log.info("inventory.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if item is not None:
        payload["item"] = item
    if target is not None:
        payload["target"] = target
    # Forward the normalized badge signal, if present -- see `fish`/`loyalty`/`count`/`lurk`'s
    # own identical forwarding comment for why absence must reach `dispatch` as absence, not
    # `False`.
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


def _resolve_command(
    parsed: ParsedCommand | None, rest: str
) -> tuple[str, str | None, str | None]:
    """Map `parse_command()`'s result (or its documented fallback) onto this bundle's commands.

    Returns `(command, item, target)` -- `item`/`target` are `None` when
    not applicable to `command`. See module docstring for the two grammar
    extensions (extensions 2 and 3) this function implements.
    """
    if parsed is not None:
        if parsed.sub_module is None and parsed.option is None:
            return "list_self", None, None
        if parsed.sub_module is None and parsed.option in ("add", "remove"):
            return _resolve_grant(parsed.option, parsed.args)
        return "usage", None, None

    # parse_command() rejected the input -- recognize the two extra shapes it cannot express
    # (see module docstring, extensions 2 and 3); anything else is a real usage error. `give` is
    # checked before the bare-single-token shape so `!inv give` (no item/user) is a usage error,
    # never misread as "list a user literally named give".
    if not rest:
        return "usage", None, None
    tok1, _, tail = rest.partition(" ")
    if tok1.lower() == "give":
        return _resolve_give(tail.strip() or None)
    if " " not in rest:
        return "list_other", None, rest
    return "usage", None, None


def _resolve_grant(verb: str, args: str | None) -> tuple[str, str | None, str | None]:
    """Parse `add`/`remove`'s own free-text `args` tail: `<item> <user>`."""
    if not args:
        return "usage", None, None
    parts = args.split()
    if len(parts) != 2:
        return "usage", None, None
    item, target = parts
    return verb, item, target


def _resolve_give(args: str | None) -> tuple[str, str | None, str | None]:
    """Parse `give`'s own free-text tail: `<item> <user>` -- see module docstring, extension 3."""
    if not args:
        return "usage", None, None
    parts = args.split()
    if len(parts) != 2:
        return "usage", None, None
    item, target = parts
    return "give", item, target


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
    """Fail-loud backend error path: log, reply an error to chat, then re-raise.

    Shared by both `kv` and `db` call sites -- see `loyalty`/`fish`'s own
    `_fail_backend()`/`_fail_kv()` for the identical structural-
    classification pattern this mirrors.
    """
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("inventory.backend_error", op=op, error=case_name)
    await relay.push(provider, {"channel": channel_id, "text": _UNAVAILABLE_MSG})
    raise RuntimeError(f"inventory {op} failed: {case_name}") from exc


async def _dir_get(
    community: str, pseudonym: str, *, provider: str, channel_id: str
) -> dict[str, str]:
    """Fetch `pseudonym`'s `item -> row_id` directory, `{}` if they have never owned anything."""
    try:
        raw = await community_kv.get(community, _dir_key(pseudonym))
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_backend`
        await _fail_backend(exc, provider=provider, channel_id=channel_id, op="kv_get")
    if raw is None:
        return {}
    try:
        decoded = json.loads(raw.decode())
    except (UnicodeDecodeError, ValueError) as exc:
        await _fail_backend(exc, provider=provider, channel_id=channel_id, op="dir_decode")
    if not isinstance(decoded, dict):
        await _fail_backend(
            ValueError(f"directory for {pseudonym!r} is not a JSON object"),
            provider=provider,
            channel_id=channel_id,
            op="dir_decode",
        )
    return {str(k): str(v) for k, v in decoded.items()}


async def _dir_set(
    community: str, pseudonym: str, directory: dict[str, str], *, provider: str, channel_id: str
) -> None:
    """Persist `pseudonym`'s full `item -> row_id` directory."""
    try:
        await community_kv.set(
            community, _dir_key(pseudonym), json.dumps(directory).encode(), ttl_seconds=0
        )
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_backend`
        await _fail_backend(exc, provider=provider, channel_id=channel_id, op="kv_set")


async def _db_get(row_id: str, *, provider: str, channel_id: str) -> dict[str, Any] | None:
    """`waddle_sdk.db.get`, fail-loud on a backend error (see `_fail_backend`).

    The explicit `result` annotation narrows `db.get()`'s return away from `Any` -- `waddle_sdk`
    ships no `py.typed` marker yet (`pyproject.toml`'s own mypy-override comment), so mypy
    --strict would otherwise flag every one of these wrappers as `no-any-return` despite the
    real function being fully typed at runtime.
    """
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


async def _db_delete(row_id: str, expected_version: int, *, provider: str, channel_id: str) -> None:
    """`waddle_sdk.db.delete`, fail-loud on a backend error (see `_fail_backend`/`_db_get`).

    A `db.ConflictError` (the row changed since `expected_version`) is NOT
    swallowed here -- it propagates to the caller, which treats "someone
    else touched this row first" as a benign skip-the-delete signal (see
    `_maybe_cleanup_zero_row`), never a backend failure.
    """
    try:
        await db.delete(row_id, expected_version)
    except db.ConflictError:
        raise
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_backend`
        await _fail_backend(exc, provider=provider, channel_id=channel_id, op="db_delete")


async def _db_update_with_retry(
    row_id: str,
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    provider: str,
    channel_id: str,
) -> dict[str, Any]:
    """Fetch-mutate-update loop with bounded optimistic-concurrency retry.

    `mutate` receives the current row dict (fresh from `db.get`) and
    returns the column-values to write (a partial update -- `db.update`
    patches only the given columns). Retried up to `_MAX_CONFLICT_RETRIES`
    times on `db.ConflictError` (another writer updated the row between our
    get and our update), re-fetching the row each time. `mutate` may raise
    `_InsufficientQuantityError` to abort the whole operation without
    retrying (see `_decrement_item`) -- that exception is never caught
    here, it propagates straight to the caller. A `row_id` the kv directory
    points at but `db.get` can no longer find is treated as directory
    corruption and fails loud (never silently re-created, which would
    orphan/duplicate the user's row) -- any other db error, or exhausting
    the retry budget, also fails loud via `_fail_backend`. See `loyalty`'s
    own identical helper.
    """
    for _attempt in range(_MAX_CONFLICT_RETRIES):
        row = await _db_get(row_id, provider=provider, channel_id=channel_id)
        if row is None:
            await _fail_backend(
                RuntimeError(f"kv directory points at missing db row {row_id!r}"),
                provider=provider,
                channel_id=channel_id,
                op="directory_stale",
            )
        new_values = mutate(row)
        try:
            updated: dict[str, Any] = await db.update(row_id, int(row["version"]), new_values)
        except db.ConflictError:
            continue
        except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_backend`
            await _fail_backend(exc, provider=provider, channel_id=channel_id, op="db_update")
        else:
            return updated
    await _fail_backend(
        RuntimeError("conflict retries exhausted"),
        provider=provider,
        channel_id=channel_id,
        op="db_update_retry",
    )


def _mutate_decrement(row: dict[str, Any]) -> dict[str, Any]:
    """Decrement `quantity` by 1 -- raises `_InsufficientQuantityError` if already at `0`."""
    current = int(row["quantity"])
    if current < 1:
        raise _InsufficientQuantityError()
    return {"quantity": current - 1}


async def _maybe_cleanup_zero_row(
    community: str,
    pseudonym: str,
    item: str,
    row_id: str,
    expected_version: int,
    *,
    provider: str,
    channel_id: str,
) -> None:
    """Best-effort: delete a just-zeroed row and drop it from the user's directory.

    Never destroys a concurrently-replenished row: if `expected_version` no
    longer matches (another writer updated the row after our decrement),
    this skips the delete and leaves the directory entry in place -- a
    stale zero-quantity row is a harmless storage/listing cosmetic
    (listing already filters `quantity > 0`, see `_handle_list`), never
    silent data loss. See module docstring's concurrency-limitation note.
    """
    try:
        await _db_delete(row_id, expected_version, provider=provider, channel_id=channel_id)
    except db.ConflictError:
        log.info("inventory.cleanup_skipped", row_id=row_id, reason="conflict")
        return
    directory = await _dir_get(community, pseudonym, provider=provider, channel_id=channel_id)
    if directory.get(item) == row_id:
        del directory[item]
        await _dir_set(community, pseudonym, directory, provider=provider, channel_id=channel_id)


async def _increment_item(
    community: str, pseudonym: str, item: str, *, provider: str, channel_id: str
) -> int:
    """Insert `item` at quantity `1`, or increment an existing row by `1`. Returns new quantity."""
    directory = await _dir_get(community, pseudonym, provider=provider, channel_id=channel_id)
    row_id = directory.get(item)
    if row_id is None:
        inserted = await _db_insert(
            {"actor_hash": pseudonym, "item": item, "quantity": 1},
            provider=provider,
            channel_id=channel_id,
        )
        # Re-fetch the directory immediately before writing to narrow the lost-update race
        # window documented in the module docstring.
        directory = await _dir_get(community, pseudonym, provider=provider, channel_id=channel_id)
        directory[item] = str(inserted["row_id"])
        await _dir_set(community, pseudonym, directory, provider=provider, channel_id=channel_id)
        return 1

    def _mutate(row: dict[str, Any]) -> dict[str, Any]:
        return {"quantity": int(row["quantity"]) + 1}

    updated = await _db_update_with_retry(
        row_id, _mutate, provider=provider, channel_id=channel_id
    )
    return int(updated["quantity"])


async def _decrement_item(
    community: str, pseudonym: str, item: str, *, provider: str, channel_id: str
) -> int | None:
    """Decrement `item` by `1` if the user owns at least one; deletes the row at `0`.

    Returns the new quantity (`0` if just deleted), or `None` if the user
    has no such item to decrement (no row, or its current quantity is
    already `0`) -- callers render `None` as a "nothing to give/remove"
    reply, never a backend failure.
    """
    directory = await _dir_get(community, pseudonym, provider=provider, channel_id=channel_id)
    row_id = directory.get(item)
    if row_id is None:
        return None
    try:
        updated = await _db_update_with_retry(
            row_id, _mutate_decrement, provider=provider, channel_id=channel_id
        )
    except _InsufficientQuantityError:
        return None
    new_quantity = int(updated["quantity"])
    if new_quantity == 0:
        await _maybe_cleanup_zero_row(
            community,
            pseudonym,
            item,
            row_id,
            int(updated["version"]),
            provider=provider,
            channel_id=channel_id,
        )
    return new_quantity


async def _handle_list(
    community: str, pseudonym: str, display_name: str, *, provider: str, channel_id: str
) -> str:
    """Render `display_name`'s full item collection, or a "no items" message."""
    directory = await _dir_get(community, pseudonym, provider=provider, channel_id=channel_id)
    if not directory:
        return f"{display_name} has no items."

    entries: list[str] = []
    for item in sorted(directory):
        row_id = directory[item]
        row = await _db_get(row_id, provider=provider, channel_id=channel_id)
        if row is None:
            await _fail_backend(
                RuntimeError(f"kv directory points at missing db row {row_id!r}"),
                provider=provider,
                channel_id=channel_id,
                op="directory_stale",
            )
        quantity = int(row["quantity"])
        if quantity > 0:
            entries.append(f"{item} x{quantity}")

    if not entries:
        return f"{display_name} has no items."

    shown = entries[:_MAX_LIST_ITEMS]
    text = f"{display_name}'s inventory: " + ", ".join(shown)
    remaining = len(entries) - len(shown)
    if remaining > 0:
        text += f" (and {remaining} more)"
    return text


def _format_grant_reply(verb: str, item: str, target_raw: str, new_quantity: int) -> str:
    """Render the `add`/`remove` chat reply -- `target_raw` is the live typed name, not stored."""
    if verb == "add":
        return f"Gave 1 {item} to {target_raw}. They now have {new_quantity}."
    return f"Removed 1 {item} from {target_raw}. They now have {new_quantity}."


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: all kv/db reads/writes, then relay the reply.

    Raises:
        ValueError: The envelope's payload has no `channel_id`; the
            envelope has no `community` (no tenant-wide fallback -- see
            module docstring's data-scoping section); or an unrecognized
            `command` (defensive -- `transform` only ever emits a member of
            `_KNOWN_COMMANDS`).
        RuntimeError: A `kv`/`db` backend call failed, or an update's
            optimistic-concurrency retries were exhausted (see
            `_fail_backend`/`_db_update_with_retry` -- a chat error reply
            and an ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("inventory reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized inventory command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("inventory.missing_community", command=command)
        raise ValueError("inventory requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    if command in ("add", "remove"):
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("inventory.grant_denied", command=command, role_signal=str(role_signal))
            await relay.push(provider, {"channel": channel_id, "text": _PERMISSION_DENIED_MSG})
            return DispatchResult(transport=provider, detail=f"{command}:denied")
        item_raw = payload.get("item")
        target_raw = payload.get("target")
        if not isinstance(item_raw, str) or not isinstance(target_raw, str):
            raise ValueError(
                f"malformed {command} payload: item={item_raw!r} target={target_raw!r}"
            )
        item = _normalize_item(item_raw)
        pseudonym = _pseudonym(_normalize_target(target_raw))
        if command == "add":
            new_quantity = await _increment_item(
                community, pseudonym, item, provider=provider, channel_id=channel_id
            )
            reply_text = _format_grant_reply("add", item, target_raw, new_quantity)
        else:
            decremented = await _decrement_item(
                community, pseudonym, item, provider=provider, channel_id=channel_id
            )
            if decremented is None:
                reply_text = f"{target_raw} has no {item} to remove."
            else:
                reply_text = _format_grant_reply("remove", item, target_raw, decremented)
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        log.info("inventory.dispatch adjusted", command=command)
        return DispatchResult(transport=provider, detail=command)

    if command == "give":
        item_raw = payload.get("item")
        target_raw = payload.get("target")
        if not isinstance(item_raw, str) or not isinstance(target_raw, str):
            raise ValueError(f"malformed give payload: item={item_raw!r} target={target_raw!r}")
        item = _normalize_item(item_raw)
        giver_pseudonym = _pseudonym(envelope.event.actor)
        target_pseudonym = _pseudonym(_normalize_target(target_raw))
        if giver_pseudonym == target_pseudonym:
            reply_text = "you cannot give an item to yourself."
            await relay.push(provider, {"channel": channel_id, "text": reply_text})
            log.info("inventory.give_denied", reason="self")
            return DispatchResult(transport=provider, detail="give:self")
        decremented = await _decrement_item(
            community, giver_pseudonym, item, provider=provider, channel_id=channel_id
        )
        if decremented is None:
            reply_text = f"{username} has no {item} to give."
            await relay.push(provider, {"channel": channel_id, "text": reply_text})
            log.info("inventory.give_denied", reason="insufficient")
            return DispatchResult(transport=provider, detail="give:insufficient")
        await _increment_item(
            community, target_pseudonym, item, provider=provider, channel_id=channel_id
        )
        reply_text = f"{username} gave 1 {item} to {target_raw}."
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        log.info("inventory.dispatch gave", command=command)
        return DispatchResult(transport=provider, detail="give")

    if command == "list_other":
        target_raw = payload.get("target")
        if not isinstance(target_raw, str):
            raise ValueError(f"malformed list_other payload: target={target_raw!r}")
        pseudonym = _pseudonym(_normalize_target(target_raw))
        reply_text = await _handle_list(
            community, pseudonym, target_raw, provider=provider, channel_id=channel_id
        )
    else:  # list_self
        pseudonym = _pseudonym(envelope.event.actor)
        reply_text = await _handle_list(
            community, pseudonym, username, provider=provider, channel_id=channel_id
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("inventory.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
