"""`!remind` -> per-user, per-community reminder store/list/cancel, kv-only.

v1 scope (2026-10-07, feature/bundle-remind):

- `!remind set <duration> <text>` stores one reminder for the calling user
  in `kv`: `<duration>` is a relative token of `d`/`h`/`m`/`s` components
  (`3d`, `2h30m`, case-insensitive, at least one component, strictly
  positive), `<text>` is everything after it, stored verbatim. The due time
  (`now + duration`, via the host `clock` capability) is recorded as an
  absolute millisecond timestamp.
- Bare `!remind` (equivalently `!remind list`) lists the caller's own stored
  reminders, each showing its id, text, and either "due in ..." or "overdue
  by ... (not delivered)" -- never mutates state.
- `!remind remove <id>`, anyone, cancels one of the caller's own reminders
  by id. **Note on command wording:** the shared SDK verb vocabulary
  (`waddle_sdk.command.VERBS`) has no `cancel` verb -- `remove` is the
  closest declared verb and is what this bundle actually parses; "cancel"
  is the common description of what `remove` does, not a second accepted
  spelling.

**Delivery is NOT implemented in this release -- this is the honest,
documented scope, not a gap papered over.** The `stage` WIT world
(`wit/waddle-bundle/stage.wit`) gives a bundle no scheduler/cron hook and no
way to proactively push a message outside of relaying a reply to an inbound
chat event it is already handling -- there is nothing in this bundle's
available capabilities that could fire a reminder the moment it comes due.
Per this bundle's own spec ("do NOT fake delivery... fail loud on anything
unimplemented rather than pretending"), `!remind set`'s confirmation and
every `!remind list` line involving an overdue reminder say explicitly that
nothing was or will be auto-delivered -- a caller must re-check `!remind
list` themselves. Wiring real due-time delivery is future work tracked
against adding a scheduler/webhook capability to the `stage` world; it is
out of scope here, not silently stubbed.

PII: reminder storage is scoped per-caller via a SHA-256 pseudonym of
`event.actor` (mirrors `first`'s own `_pseudonym` convention) -- never a raw
identifier in a `kv` key or a log line. Reminder *text* is the caller's own
free-form content, relayed back only to the same chat channel the caller
typed in (never echoed to a different user), exactly like `first`'s own
direct-to-caller win reply.

Data scoping: every kv entry here is community- AND pseudonym-scoped via
`waddle_sdk.community_kv` plus a pseudonym segment baked into the key text
(never global/tenant-wide) -- `dispatch` checks `envelope.community` itself
up front for a clear, command-specific error message if it's missing,
mirroring `first`'s own "Data scoping" section.

Command grammar: parsed via `waddle_sdk.command.parse_command()` against a
declared `CommandSpec` (`sdk/waddle-sdk/AUTHORING.md` Sec1), no sub-modules.
`set`/`list`/`remove` are the grammar's own declared verbs; bare `!remind`
(no verb) is the command's own default behavior (list), exactly the shape
the grammar doc calls out for `!count`/`!first`.

Business logic split: `transform` only recognizes the command/verb shape and
forwards the normalized action + raw argument text (no `kv` access) --
`dispatch` performs every `kv` read/write and the relay reply, mirroring
`first`'s own process/action-stage split.

Storage hygiene: a reminder more than `_STALE_AFTER_SECONDS` (30 days,
comfortably under the host's documented 30-day `KV_MAX_TTL_S` cap) past its
due time is dropped the next time the caller's list is written to (`set`/
`remove`) -- cleanup of abandoned entries nobody will ever act on, never a
claim that it was delivered. `!remind list` itself never mutates state (a
pure read, matching `first`'s own "list is a pure read" test), so it still
shows an ancient-but-not-yet-pruned overdue reminder rather than silently
hiding it.

Gated behind the PostHog flag ``waddles.command-remind``, default OFF --
see `bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (checked only after the command-text match).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, NoReturn

from waddle_sdk import clock, community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-remind"

SPEC = CommandSpec(name="remind", sub_modules=frozenset())

#: Per-user (pseudonym-scoped) JSON array of reminder dicts: `{"id", "due_ms", "text",
#: "created_ms"}`. Refreshed to `_STALE_AFTER_SECONDS` on every write -- see module docstring
#: "Storage hygiene".
_STALE_AFTER_SECONDS = 30 * 24 * 60 * 60

_USAGE = "Usage: !remind | !remind list | !remind set <duration> <text> | !remind remove <id>"
_USAGE_SET = "Usage: !remind set <duration e.g. 3d2h or 45m> <text>"
_USAGE_REMOVE = "Usage: !remind remove <id>"

_KNOWN_ACTIONS = frozenset({"list", "set", "remove", "usage"})

#: Relative duration token: at least one of d/h/m/s, case-insensitive, no internal whitespace.
#: Mirrors `bundles/python/countdown/src/app.py`'s own `_DURATION_RE` -- each bundle is a
#: self-contained artifact, so this is intentionally duplicated rather than cross-imported.
_DURATION_RE = re.compile(
    r"^(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$",
    re.IGNORECASE,
)


def _items_key(pseudonym: str) -> str:
    """The `kv` key holding one caller's reminder list."""
    return f"remind.items.{pseudonym}"


def _counter_key(pseudonym: str) -> str:
    """The `kv` key holding one caller's next-reminder-id counter."""
    return f"remind.counter.{pseudonym}"


def _pseudonym(actor: str | None) -> str:
    """SHA-256 pseudonym for `actor` -- mirrors `first`'s own `_pseudonym` convention.

    Never reversible, never logged in full, never used as a display name.
    """
    return hashlib.sha256((actor or "anonymous").encode()).hexdigest()


def _parse_duration_seconds(token: str) -> int | None:
    """Parse a `3d2h5m10s`-shaped token into total seconds, or `None` if it doesn't match.

    Requires at least one component present (an empty string or a string
    with no `d`/`h`/`m`/`s` suffix matches the regex but yields zero
    components, which is rejected as meaningless rather than a 0-second
    duration).
    """
    match = _DURATION_RE.match(token)
    if match is None:
        return None
    groups = match.groupdict()
    if all(v is None for v in groups.values()):
        return None
    days = int(groups["days"] or 0)
    hours = int(groups["hours"] or 0)
    minutes = int(groups["minutes"] or 0)
    seconds = int(groups["seconds"] or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _format_duration(total_seconds: int) -> str:
    """Render a non-negative second count as `"Xd Xh Xm"`/`"Xh Xm"`/`"Xm"`/`"Xs"`.

    Identical shape to `bundles/python/countdown/src/app.py`'s own
    `_format_duration` -- see that module's docstring for the rationale.
    """
    if total_seconds < 60:
        return f"{total_seconds}s"
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _classify(parsed: ParsedCommand) -> tuple[str, str | None]:
    """Map a successfully parsed `!remind ...` command to one of `_KNOWN_ACTIONS`.

    Returns `(action, arg)` where `arg` carries a ready-to-send usage string
    for `action == "usage"`, or the raw post-verb text for `"set"`/
    `"remove"`. Never raises -- a structurally valid but meaningless
    combination (e.g. `!remind reset`, a real grammar verb with no meaning
    for this command) degrades to `"usage"` rather than a crash, mirroring
    `first`'s own `_classify`.
    """
    if parsed.option is None:
        return "list", None
    if parsed.option == "list":
        if parsed.args is not None:
            return "usage", _USAGE
        return "list", None
    if parsed.option == "set":
        if parsed.args is None:
            return "usage", _USAGE_SET
        return "set", parsed.args
    if parsed.option == "remove":
        if parsed.args is None or " " in parsed.args:
            return "usage", _USAGE_REMOVE
        return "remove", parsed.args
    return "usage", _USAGE


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!remind` and its verbs.

    No `kv` access here -- only command recognition and forwarding the
    normalized action + raw argument text for `dispatch`. Returns `None` for
    any non-chat payload, text that isn't `!remind`-shaped (cheap-skip, zero
    `kv` cost), and while `waddles.command-remind` is disabled.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head = stripped.split(maxsplit=1)[0].lower() if stripped else ""
    if head != f"!{SPEC.name}":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    action: str
    arg: str | None
    try:
        parsed = parse_command(stripped, SPEC)
    except CommandUsageError as exc:
        log.info("remind.transform usage_error", error=str(exc))
        action, arg = "usage", str(exc)
    else:
        action, arg = _classify(parsed)

    log.info("remind.transform matched", action=action)
    payload: dict[str, Any] = {"action": action, "channel_id": event.payload.get("channel_id")}
    if arg is not None:
        payload["arg"] = arg

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


async def _fail_kv(exc: Exception, *, provider: str, channel_id: str, op: str) -> NoReturn:
    """Fail-loud kv error path: log, reply an error to chat, then re-raise.

    Classifies the raised exception structurally (`getattr(exc, "value",
    exc)`), same pattern as `first`'s own `_fail_kv`/`waddle_sdk.db`'s
    documented convention. Never silent: the caller always sees a chat reply
    AND the pipeline still sees a real failure (the re-raise).
    """
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("remind.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "remind is temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"remind kv {op} failed: {case_name}") from exc


async def _kv_get(key: str, *, community: str, provider: str, channel_id: str) -> bytes | None:
    """Community-scoped `kv.get`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        return await community_kv.get(community, key)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="get")


async def _kv_set(
    key: str, value: bytes, *, community: str, provider: str, channel_id: str, ttl_seconds: int
) -> None:
    """Community-scoped `kv.set`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        await community_kv.set(community, key, value, ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="set")


async def _kv_increment(
    key: str, *, community: str, provider: str, channel_id: str, ttl_seconds: int
) -> int:
    """Community-scoped `kv.increment` by 1, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        return await community_kv.increment(community, key, 1, ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="increment")


def _load_items(raw: bytes | None, *, community: str, pseudonym: str) -> list[dict[str, Any]]:
    """Decode the stored reminder list, self-healing (logging + `[]`) on corruption.

    Corrupt content (not a JSON array of the expected dict shape) self-heals
    to an empty list and logs -- mirrors `first`'s own leaderboard-registry
    corruption stance, never crashes `dispatch`.
    """
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.error("remind.state_corrupt", context="items", community=community, user=pseudonym)
        return []
    if not isinstance(data, list) or not all(
        isinstance(item, dict) and "id" in item and "due_ms" in item and "text" in item
        for item in data
    ):
        log.error("remind.state_corrupt", context="items", community=community, user=pseudonym)
        return []
    return data


def _prune_stale(items: list[dict[str, Any]], *, now_ms: int) -> list[dict[str, Any]]:
    """Drop reminders more than `_STALE_AFTER_SECONDS` past due -- storage hygiene only.

    Never a claim of delivery (see module docstring "Storage hygiene") --
    only called from write paths (`set`/`remove`), never from the pure-read
    `list` path.
    """
    cutoff_ms = _STALE_AFTER_SECONDS * 1000
    return [item for item in items if (now_ms - item["due_ms"]) < cutoff_ms]


def _format_item_line(item: dict[str, Any], *, now_ms: int) -> str:
    """Render one reminder list line -- explicit about overdue-but-undelivered status."""
    due_ms = item["due_ms"]
    diff_ms = due_ms - now_ms
    if diff_ms > 0:
        status = f"due in {_format_duration(diff_ms // 1000)}"
    else:
        status = f"overdue by {_format_duration((-diff_ms) // 1000)} (not delivered)"
    return f'#{item["id"]} "{item["text"]}" -- {status}'


async def _handle_set(
    arg: str, *, community: str, pseudonym: str, provider: str, channel_id: str
) -> str:
    """Parse `<duration> <text>`, append a new reminder, and confirm (no fake delivery)."""
    token, _, text = arg.partition(" ")
    text = text.strip()
    if not text:
        return f"Reminder text is required. {_USAGE_SET}"

    duration_seconds = _parse_duration_seconds(token)
    if duration_seconds is None or duration_seconds <= 0:
        return f"{token!r} is not a valid positive duration (e.g. 3d2h). {_USAGE_SET}"

    now_ms = clock.now_millis()
    raw = await _kv_get(
        _items_key(pseudonym), community=community, provider=provider, channel_id=channel_id
    )
    items = _prune_stale(
        _load_items(raw, community=community, pseudonym=pseudonym), now_ms=now_ms
    )

    new_id = await _kv_increment(
        _counter_key(pseudonym),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_STALE_AFTER_SECONDS,
    )
    due_ms = now_ms + duration_seconds * 1000
    items.append({"id": str(new_id), "due_ms": due_ms, "text": text, "created_ms": now_ms})
    await _kv_set(
        _items_key(pseudonym),
        json.dumps(items).encode("utf-8"),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_STALE_AFTER_SECONDS,
    )
    log.info("remind.set", community=community, reminder_id=new_id)
    return (
        f'Reminder #{new_id} set for in {_format_duration(duration_seconds)}: "{text}" '
        "(no auto-delivery yet -- check !remind list)."
    )


async def _handle_list(*, community: str, pseudonym: str, provider: str, channel_id: str) -> str:
    """List the caller's own stored reminders -- never mutates state (pure read)."""
    raw = await _kv_get(
        _items_key(pseudonym), community=community, provider=provider, channel_id=channel_id
    )
    items = _load_items(raw, community=community, pseudonym=pseudonym)
    if not items:
        return "You have no reminders. Set one with !remind set <duration> <text>."

    now_ms = clock.now_millis()
    ordered = sorted(items, key=lambda item: item["due_ms"])
    lines = [_format_item_line(item, now_ms=now_ms) for item in ordered]
    return "; ".join(lines)


async def _handle_remove(
    id_arg: str, *, community: str, pseudonym: str, provider: str, channel_id: str
) -> str:
    """Cancel one of the caller's own reminders by id."""
    if not id_arg.isdigit():
        return f"{id_arg!r} is not a valid reminder id. {_USAGE_REMOVE}"

    now_ms = clock.now_millis()
    raw = await _kv_get(
        _items_key(pseudonym), community=community, provider=provider, channel_id=channel_id
    )
    items = _prune_stale(
        _load_items(raw, community=community, pseudonym=pseudonym), now_ms=now_ms
    )

    remaining = [item for item in items if item["id"] != id_arg]
    if len(remaining) == len(items):
        return f"No reminder with id {id_arg}."

    await _kv_set(
        _items_key(pseudonym),
        json.dumps(remaining).encode("utf-8"),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_STALE_AFTER_SECONDS,
    )
    log.info("remind.remove", community=community, reminder_id=id_arg)
    return f"Reminder #{id_arg} removed."


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: all `kv` state reads/writes, then relay the reply.

    Raises:
        ValueError: The envelope's payload has no `channel_id`; the envelope
            has no `community` (there is no tenant-wide fallback -- see
            module docstring's Data scoping section); or an unrecognized
            `action` (defensive -- `transform` only ever emits a member of
            `_KNOWN_ACTIONS`).
        RuntimeError: A `kv` backend call failed (see `_fail_kv` -- a chat
            error reply and an ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("remind reply requires a channel_id from the inbound chat.message")
    action = payload.get("action")
    if action not in _KNOWN_ACTIONS:
        raise ValueError(f"unrecognized remind action: {action!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("remind.missing_community", action=action)
        raise ValueError("remind requires a community context and cannot operate tenant-wide")

    pseudonym = _pseudonym(envelope.event.actor)

    if action == "usage":
        text = payload.get("arg")
        reply_text = text if isinstance(text, str) and text else _USAGE
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        return DispatchResult(transport=provider, detail="usage")

    if action == "set":
        arg = payload.get("arg")
        if not isinstance(arg, str) or not arg:
            raise ValueError("remind set action missing its forwarded arg")
        reply_text = await _handle_set(
            arg, community=community, pseudonym=pseudonym, provider=provider, channel_id=channel_id
        )
    elif action == "list":
        reply_text = await _handle_list(
            community=community, pseudonym=pseudonym, provider=provider, channel_id=channel_id
        )
    else:  # remove
        arg = payload.get("arg")
        if not isinstance(arg, str) or not arg:
            raise ValueError("remind remove action missing its forwarded arg")
        reply_text = await _handle_remove(
            arg, community=community, pseudonym=pseudonym, provider=provider, channel_id=channel_id
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("remind.dispatch relayed", platform=provider, action=action)
    return DispatchResult(transport=provider, detail=action)
