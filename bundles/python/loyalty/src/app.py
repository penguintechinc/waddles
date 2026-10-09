"""`!points` -> a community-scoped points/currency ledger, DB-backed (v1).

Inspiration credit (not a literal port -- see `bundle.yaml`'s `author`/
`notice`): the general shape of "every viewer accrues points, mods can
adjust balances, a leaderboard ranks the community" is inspired by
superpenguintv (Psychoboy)'s `PenguinTwitchBot` loyalty-points feature
(https://github.com/Psychoboy/PenguinTwitchBot). No original source code or
text is reused here -- the command grammar, data model, optimistic-
concurrency update loop, and leaderboard rendering below are written fresh
for Waddles, so no MIT notice reproduction is required (same convention
`bundles/python/fish/src/app.py`'s own docstring documents, contrast
`bundles/csharp/superpenguin-roll`, an actual line-for-line port which does
carry the full verbatim notice).

Uses `waddle_sdk.command.parse_command`/`CommandSpec` for the `top`/`add`/
`sub` grammar shapes. **One documented, deliberate extension beyond the
shared grammar:** `!points <user>` (a bare chat-typed name/handle with no
verb) is not expressible through `parse_command()` alone -- an arbitrary
single token is neither a declared verb nor a declared sub-module, so
`parse_command()` correctly raises `CommandUsageError` for it. `transform()`
catches exactly that shape (a `CommandUsageError` *and* the input is a
single whitespace-free token) and treats it as a target-user balance query;
anything else that fails to parse is a real usage error. See
`_resolve_command()`.

## Data model

One app-owned `db` table (`loyalty_balances`, declared in `bundle.yaml`'s
`data.tables` -- the signal that grants this bundle the `db` capability,
`hub_api/services/bundle_approval_service.py::_derive_capabilities`), rows
scoped per-community automatically by the host (`sdk/waddle-sdk/src/
waddle_sdk/db.py`'s own docstring: "Tenant/community/app scoping is applied
server-side from the invocation's own authenticated scope"). Two columns:
`actor_hash` (SHA-256 hex pseudonym of the balance-holder -- never a raw
username, same `_pseudonym()` convention as `fish`/`lurk`/`count`) and
`balance` (integer point total).

**Why this bundle also uses `kv` (`storage.kv`) alongside `db`.** The
committed `wit/waddle-bundle/stage.wit` `db` interface exposes exactly
`insert`/`get`/`query`/`update`/`delete` -- `get` takes only a `row-id`,
and `query` has no column-equality filter at all (only `limit`/`offset`/
`order-by`, confirmed against the WIT text directly, not assumed from an
older design-doc revision that described a richer `indexed-column` filter
that was not what actually landed). There is therefore no way to look up
"the row for this specific user" by anything other than its platform-
assigned `row_id`. This bundle keeps a `community_kv` index,
`loyalty.rowid.<pseudonym> -> row_id`, so a single user's balance lookup/
adjustment is an O(1) kv read followed by an exact `db.get`/`db.update`,
never an unbounded table scan (which would also be wrong at scale: `db
.query()`'s `limit` is host-clamped to 200 rows,
`core/bundle_host_db/src/limits.rs::MAX_QUERY_LIMIT`, so scanning for one
user would silently miss rows in any community with more members than
that). The leaderboard (`!points top`) is the one operation that
genuinely needs `db.query(order_by="balance", descending=True)` --
`kv` has no scan/order primitive, which is exactly `fish`'s own documented
reason for deferring its leaderboard to "the in-flight #623 db API" that
this bundle now depends on.

**Leaderboard entries cannot show real display names.** `actor_hash` is a
one-way SHA-256 hash (per this bundle's own PII rule, see below) -- there
is no reverse lookup from a leaderboard row back to a chat-visible name
without a hub-side detokenization step this bundle has no access to
(`critical-rules.md` PII Tokenization: raw PII lives only inside the
hub/API server; everything outside it, bundles included, references users
by UUID/pseudonym only). `!points` and `!points <user>` CAN show a real
name because the caller's/target's typed name is live in the *current*
chat event and is only ever echoed back into that same reply, never
persisted (identical to `fish`'s own documented convention: "username IS
rendered in the visible chat reply, never logged"). `!points top` has no
such live name for each ranked row, so it renders a short, stable
`player-<hash prefix>` tag instead -- a deliberate, PII-compliant scope
decision, not a stub.

**Known, pre-existing platform gap this bundle does not fix.** The richer
declarative `data.table.columns[]` schema (`hub_api/services/
bundle_data_schema.py`) that would let hub-api provision this bundle's
table columns automatically is, by that module's own docstring, pending
onboarding integration -- wiring `bundle_manifest_v2.py`/
`bundle_approval_service.py` to it is a separate, already-tracked follow-on
phase, not something this bundle's own PR is positioned to fix.

Gated behind the PostHog flag ``waddles.command-loyalty`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (cheap command-match first, flag check second, real
grammar parse last).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any, NoReturn

from waddle_sdk import community_kv, db, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-loyalty"

#: `top` is this bundle's only sub-module -- see `_resolve_command()` for the extra,
#: documented `!points <user>` shape the shared grammar alone cannot express.
SPEC = CommandSpec(name="points", sub_modules=frozenset({"top"}))

_INDEX_KEY_PREFIX = "loyalty.rowid."
_LEADERBOARD_SIZE = 10
#: Bounded optimistic-concurrency retry budget for `!points add/sub` -- see
#: `_db_update_with_retry()`. Five attempts absorbs ordinary concurrent-writer
#: contention without looping forever on a genuinely stuck row.
_MAX_CONFLICT_RETRIES = 5

_USAGE = (
    "Usage: !points | !points <user> | !points top | "
    "!points add <amount> <user> | !points sub <amount> <user> "
    "(add/sub are broadcaster/mod only)"
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can adjust points"
_UNAVAILABLE_MSG = "points are temporarily unavailable, try again shortly."

_KNOWN_COMMANDS = frozenset(
    {"balance_self", "balance_other", "leaderboard", "add", "sub", "usage"}
)


def _pseudonym(identity: str | None) -> str:
    """Non-reversible per-identity key component -- see `fish`'s own `_pseudonym()`.

    `event.actor` (and any chat-typed target name) may currently be a raw
    username (tokenization pipeline #429 not yet merged); hashing it before
    it ever reaches `community_kv`/`db` keeps this bundle PII-safe today and
    after #429 lands unchanged.
    """
    return hashlib.sha256((identity or "anonymous").encode()).hexdigest()


def _normalize_target(raw: str) -> str:
    """Normalize a chat-typed `!points <user>`/`... add <amount> <user>` target.

    Strips one leading `@` (common mention syntax) and lower-cases, so
    `!points @Alice` and `!points alice` resolve to the same pseudonym.
    Without a tokenization/identity-resolution service (#429 not yet
    merged) a typed display name is the only identity signal this bundle
    receives for someone other than the current caller -- this is the best
    normalization available today, not a claim of perfect identity
    resolution.
    """
    cleaned = raw.strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    return cleaned.lower()


def _index_key(pseudonym: str) -> str:
    """Per-(community, user) `kv` key holding that user's `db` `row_id` -- see module docstring."""
    return f"{_INDEX_KEY_PREFIX}{pseudonym}"


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `fish`/`count`/`lurk`'s own identical helper -- `None` (neither
    `is_mod`/`is_broadcaster` present, e.g. Discord's normalizer today) must
    be treated as denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!points` and its grammar.

    Cheap-skip first (no leading `!points` token -- `None`, zero cost),
    flag check second, real grammar parse last -- same ordering as
    `eightball`/`fish`'s own documented rationale. A recognized-but-
    malformed `!points ...` still produces a reply (`"usage"`) since the
    caller did invoke this command -- never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head = stripped.partition(" ")[0]
    if head.lower() != "!points":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    try:
        parsed: ParsedCommand | None = parse_command(stripped, SPEC)
    except CommandUsageError:
        parsed = None

    command, target, amount = _resolve_command(parsed, stripped)

    log.info("loyalty.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    if target is not None:
        payload["target"] = target
    if amount is not None:
        payload["amount"] = amount
    # Forward the normalized badge signal, if present -- see `fish`/`count`/`lurk`'s own
    # identical forwarding comment for why absence must reach `dispatch` as absence, not `False`.
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
    parsed: ParsedCommand | None, stripped: str
) -> tuple[str, str | None, int | None]:
    """Map `parse_command()`'s result (or its documented fallback) onto this bundle's commands.

    Returns `(command, target, amount)` -- `target`/`amount` are `None`
    when not applicable to `command`. See module docstring for the
    `!points <user>` grammar extension this function implements.
    """
    if parsed is not None:
        if parsed.sub_module is None and parsed.option is None:
            return "balance_self", None, None
        if parsed.sub_module == "top" and parsed.option is None:
            return "leaderboard", None, None
        if parsed.sub_module is None and parsed.option in ("add", "sub"):
            return _resolve_adjust(parsed.option, parsed.args)
        return "usage", None, None

    # parse_command() rejected the input -- the one shape it cannot express is a bare
    # target-user argument (`!points <user>`), since an arbitrary typed name is neither a
    # declared verb nor a declared sub-module. Recognize exactly that shape (a single
    # whitespace-free token) here; anything else (multi-word, etc.) is a real usage error.
    _, _, rest = stripped.partition(" ")
    rest = rest.strip()
    if rest and " " not in rest:
        return "balance_other", rest, None
    return "usage", None, None


def _resolve_adjust(verb: str, args: str | None) -> tuple[str, str | None, int | None]:
    """Parse `add`/`sub`'s own free-text `args` tail: `<amount> <user>`."""
    if not args:
        return "usage", None, None
    parts = args.split()
    if len(parts) != 2:
        return "usage", None, None
    amount_text, target = parts
    try:
        amount = int(amount_text)
    except ValueError:
        log.debug("loyalty.adjust_invalid_amount", command=verb)
        return "usage", None, None
    if amount <= 0:
        return "usage", None, None
    return verb, target, amount


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

    Shared by both `kv` and `db` call sites -- see `fish`'s own `_fail_kv()`
    for the identical structural-classification pattern this mirrors.
    """
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("loyalty.backend_error", op=op, error=case_name)
    await relay.push(provider, {"channel": channel_id, "text": _UNAVAILABLE_MSG})
    raise RuntimeError(f"loyalty {op} failed: {case_name}") from exc


async def _kv_get_rowid(
    community: str, pseudonym: str, *, provider: str, channel_id: str
) -> str | None:
    """Look up the `db` `row_id` for `pseudonym`'s balance row, or `None` if they have none yet."""
    try:
        raw = await community_kv.get(community, _index_key(pseudonym))
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_backend`
        await _fail_backend(exc, provider=provider, channel_id=channel_id, op="kv_get")
    return raw.decode() if raw is not None else None


async def _kv_set_rowid(
    community: str, pseudonym: str, row_id: str, *, provider: str, channel_id: str
) -> None:
    """Persist `pseudonym`'s `db` `row_id` into the lookup index."""
    try:
        await community_kv.set(community, _index_key(pseudonym), row_id.encode(), ttl_seconds=0)
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


async def _db_query(
    *, order_by: str | None, descending: bool, limit: int, provider: str, channel_id: str
) -> list[dict[str, Any]]:
    """`waddle_sdk.db.query`, fail-loud on a backend error (see `_fail_backend`/`_db_get`)."""
    try:
        rows: list[dict[str, Any]] = await db.query(
            limit=limit, order_by=order_by, descending=descending
        )
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_backend`
        await _fail_backend(exc, provider=provider, channel_id=channel_id, op="db_query")
    return rows


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
    patches only the given columns, see `core/bundle_host_db/src/
    backend.rs::update_impl`). Retried up to `_MAX_CONFLICT_RETRIES` times
    on `db.ConflictError` (another writer updated the row between our get
    and our update), re-fetching the row each time. A `row_id` the kv index
    points at but `db.get` can no longer find is treated as index
    corruption and fails loud (never silently re-created, which would
    orphan/duplicate the user's row) -- any other db error, or exhausting
    the retry budget, also fails loud via `_fail_backend`.
    """
    for _attempt in range(_MAX_CONFLICT_RETRIES):
        row = await _db_get(row_id, provider=provider, channel_id=channel_id)
        if row is None:
            await _fail_backend(
                RuntimeError(f"kv index points at missing db row {row_id!r}"),
                provider=provider,
                channel_id=channel_id,
                op="index_stale",
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


async def _handle_balance(
    community: str, pseudonym: str, display_name: str, *, provider: str, channel_id: str
) -> str:
    """Render `display_name`'s current balance -- `0` if they have no row yet."""
    row_id = await _kv_get_rowid(community, pseudonym, provider=provider, channel_id=channel_id)
    if row_id is None:
        return f"{display_name} has 0 points."
    row = await _db_get(row_id, provider=provider, channel_id=channel_id)
    if row is None:
        await _fail_backend(
            RuntimeError(f"kv index points at missing db row {row_id!r}"),
            provider=provider,
            channel_id=channel_id,
            op="index_stale",
        )
    return f"{display_name} has {int(row['balance'])} points."


async def _handle_adjust(
    community: str,
    verb: str,
    target_raw: str,
    amount: int,
    *,
    provider: str,
    channel_id: str,
) -> str:
    """Apply `!points add/sub <amount> <user>` -- permission already checked by the caller."""
    pseudonym = _pseudonym(_normalize_target(target_raw))
    delta = amount if verb == "add" else -amount
    row_id = await _kv_get_rowid(community, pseudonym, provider=provider, channel_id=channel_id)

    if row_id is None:
        new_balance = max(0, delta)
        inserted = await _db_insert(
            {"actor_hash": pseudonym, "balance": new_balance},
            provider=provider,
            channel_id=channel_id,
        )
        await _kv_set_rowid(
            community, pseudonym, str(inserted["row_id"]), provider=provider, channel_id=channel_id
        )
        return _format_adjust_reply(verb, target_raw, amount, new_balance)

    def _mutate(row: dict[str, Any]) -> dict[str, Any]:
        return {"balance": max(0, int(row["balance"]) + delta)}

    updated = await _db_update_with_retry(
        row_id, _mutate, provider=provider, channel_id=channel_id
    )
    return _format_adjust_reply(verb, target_raw, amount, int(updated["balance"]))


def _format_adjust_reply(verb: str, target_raw: str, amount: int, new_balance: int) -> str:
    """Render the chat reply for a completed `add`/`sub` -- `target_raw` is the live typed name."""
    if verb == "add":
        return f"Added {amount} points to {target_raw}. New balance: {new_balance}."
    return f"Removed {amount} points from {target_raw}. New balance: {new_balance}."


async def _handle_leaderboard(community: str, *, provider: str, channel_id: str) -> str:
    """Render the top `_LEADERBOARD_SIZE` balances, highest first.

    See module docstring for why entries show a short `player-<hash
    prefix>` tag rather than a resolved display name.
    """
    rows = await _db_query(
        order_by="balance",
        descending=True,
        limit=_LEADERBOARD_SIZE,
        provider=provider,
        channel_id=channel_id,
    )
    if not rows:
        return "no one has any points yet."
    entries = [
        f"{i}. player-{str(row['actor_hash'])[:8]}: {int(row['balance'])}"
        for i, row in enumerate(rows, start=1)
    ]
    return "Top points: " + ", ".join(entries)


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
        raise ValueError("points reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized points command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("loyalty.missing_community", command=command)
        raise ValueError("points requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    if command in ("add", "sub"):
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("loyalty.adjust_denied", command=command, role_signal=str(role_signal))
            await relay.push(provider, {"channel": channel_id, "text": _PERMISSION_DENIED_MSG})
            return DispatchResult(transport=provider, detail=f"{command}:denied")
        target = payload.get("target")
        amount = payload.get("amount")
        if not isinstance(target, str) or not isinstance(amount, int):
            raise ValueError(f"malformed {command} payload: target={target!r} amount={amount!r}")
        reply_text = await _handle_adjust(
            community, command, target, amount, provider=provider, channel_id=channel_id
        )
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        log.info("loyalty.dispatch adjusted", command=command)
        return DispatchResult(transport=provider, detail=command)

    if command == "leaderboard":
        reply_text = await _handle_leaderboard(community, provider=provider, channel_id=channel_id)
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        log.info("loyalty.dispatch relayed", platform=provider, command=command)
        return DispatchResult(transport=provider, detail=command)

    if command == "balance_other":
        target = payload.get("target")
        if not isinstance(target, str):
            raise ValueError(f"malformed balance_other payload: target={target!r}")
        pseudonym = _pseudonym(_normalize_target(target))
        reply_text = await _handle_balance(
            community, pseudonym, target, provider=provider, channel_id=channel_id
        )
    else:  # balance_self
        pseudonym = _pseudonym(envelope.event.actor)
        reply_text = await _handle_balance(
            community, pseudonym, username, provider=provider, channel_id=channel_id
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("loyalty.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
