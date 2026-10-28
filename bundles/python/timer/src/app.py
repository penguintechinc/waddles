"""`!timer` -> recurring auto-announcement messages, driven by chat activity, kv-only.

Original Waddles content: recurring channel announcements ("follow the socials", "hydrate") are a
common stream-tool idea, but no third-party source, code or text is used here, so no attribution
applies. `author`/`license` in `bundle.yaml` follow the `PenguinTech/waddles` first-party
convention.

Commands (all moderator/broadcaster only):

- `!timer add <name> <interval-min> <message>` -- register a timer bound to THIS channel. The
  name is 1-24 of `[a-z0-9_-]` (case-insensitive), the interval 5-1440 minutes, the message
  1-300 characters; at most 10 timers per channel (40 per community).
- `!timer del <name>` (also `delete` / `remove`) -- delete a timer.
- `!timer list` -- show this channel's timers: name, interval, on/off and a message preview.
- `!timer enable <name>` / `!timer disable <name>` -- pause or resume. Enabling restarts the
  countdown, so a long-disabled timer does not fire the instant it is switched back on.
- `!timer reset` -- delete all of THIS channel's timers (also the recovery path for a corrupted
  config blob, which is dropped whole because it cannot be split per channel).

**A moderator badge is valid in ONE channel, so every command is scoped to it.** A mod's
`is_mod` comes from the chat the message was sent in, but the config blob is shared by every
channel linked to the community. `add`/`del`/`enable`/`disable`/`list`/`reset` therefore only see
and change timers whose `scope` (the channel hash below) is the invoking channel's: another
channel's timer answers "No timer named ..." exactly like a missing one. Names are unique per
channel, and the `last-fired` / `claim` keys carry the scope too, so two channels may each own a
`hydrate` timer without touching one another's markers.

`del`/`delete`/`remove` all reach the shared `waddle_sdk.command` grammar's `delete`/`remove`
verb (`del` is normalised to `delete` before parsing); `add`/`list`/`enable`/`disable`/`reset`
are native verbs, so the whole command goes through the real `parse_command()`.

**Scheduler verdict: there is NO tick/timer event to subscribe to.** The data plane delivers
`chat.*`, `stream.online`/`stream.offline` and a few `channel.*` events -- nothing periodic -- and
the WIT `stage` worlds expose no scheduler/cron capability and no way to push a message other than
as a reply to an inbound event (the same gap `remind` documents as gh-613). So this bundle does
NOT fire on a wall-clock schedule. What it does instead is the chat-activity model most stream
timers actually use: it consumes every `chat.message` in its channel (no `command_prefix`
filter, like `greeter`), and the first chat line AFTER a timer is due posts that timer's message.
Consequences, all intentional: a silent channel posts nothing (no one to read it, and no spam into
the void); a timer can post up to one chat line late, never early; and after a long quiet spell it
posts ONCE, not once per missed interval. A true scheduler host capability is the follow-up (see
README); when it lands, `_handle_tick` is the single function to point it at.

**Exactly-once under concurrent chat.** Many chat lines can be processed at once, so the due
check alone would double-post. Whoever wins a `kv.increment` claim keyed by `(timer, due-time)`
(atomic, `1` only for the first caller -- the one compare-and-swap the host offers) posts; losers
move on. The winner records the new last-fired time BEFORE relaying, so a failed relay is a
missed announcement, never a repeat. At most ONE timer posts per chat line (the most overdue);
the rest follow on later lines. Cost: one `kv.get` per chat line when nothing is enabled for the
channel, plus one `get` per enabled timer of that channel (<= 10), so well under the host's 64 ops.

**Timers are bound to a channel.** `scope` is a 16-hex SHA-256 of `platform|channel_id` taken
when the timer is added; only chat lines from that same platform+channel can trigger it, and the
message is relayed back to that channel. The raw channel id is never stored or logged.

**Output safety.** The stored message is mod-authored text that is echoed later, so it is
`waddle_sdk.safe_text.sanitize_user_text`-ed at every output (the post and the `list` preview),
with no `allow_tokens`: nothing stored may stay a live mention or egress token. It is also
refused at `add` time if it contains a mention (a stored `{user:<token>}` would resolve to a
different person later) or leads with `/`, `.` or `!` -- the host relays a bot line verbatim, and
a Twitch line starting `/ban x` would run as the bot, which could exceed the author's own mod
rights. The same lead check runs again at post time as defence in depth.

**PII.** A timer holds only a name, an interval, an enabled flag, an anchor time, a channel hash
and the message. The author is never recorded; logs carry only `command`, counts and exception
class names -- never names, messages or channel ids.

**Concurrency of admin edits.** `add`/`del`/`enable`/`disable`/`reset` read-modify-write one JSON
config blob (`timer.cfg`), serialized by a short-TTL `kv.increment` lock; a mod that cannot take
it after three immediate attempts gets "busy, try again" instead of a silently lost edit.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.

DO NOT BUILD in v1 -- documented extension points, never a silent stub:

- Wall-clock firing in a silent channel, "only while live" gating, minimum-chat-lines gating --
  all need a scheduler / stream-state host capability (README follow-up).
- `$(variable)` templating in messages, per-timer channel re-binding, a rotating message list.
- Cross-community anything: state is keyed by `community_id` only (`waddle_sdk.community_kv`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any, NoReturn

from waddle_sdk import clock, community_kv, identity, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import leads_with_command, sanitize_user_text

_HEAD = "!timer"
SPEC = CommandSpec(name="timer")

#: Timers one CHANNEL may hold (the scope a mod's badge is valid in).
MAX_TIMERS = 10
#: Timers across every channel of one community -- bounds the single config blob.
MAX_TIMERS_TOTAL = 40
#: Hard ceiling on the serialized config blob (the host's kv value cap is 64 KiB).
MAX_CONFIG_BYTES = 60_000
MIN_INTERVAL_MIN = 5
MAX_INTERVAL_MIN = 1440
MAX_MESSAGE_LEN = 300
#: `!timer list` prints at most this many message characters per timer, and stops adding timers
#: once the reply would pass `_LIST_BUDGET` (a Twitch line is 500 characters).
_PREVIEW_LEN = 30
_LIST_BUDGET = 420

#: `.`-separated, never `:` -- the real `kv` host capability reserves `:` (gh-631).
_CFG_KEY = "timer.cfg"
_LOCK_KEY = "timer.lock"
_LAST_PREFIX = "timer.last."
_CLAIM_PREFIX = "timer.claim."
#: `last` / `claim` keys are per CHANNEL scope as well as per name: two channels may each own a
#: timer called `hydrate` and neither can touch the other's markers.
_LOCK_TTL_SECONDS = 10
_LOCK_ATTEMPTS = 3
#: A claim only has to outlive the few milliseconds between winning it and recording the
#: last-fired time; if the winner crashes in between, the timer is retried after this.
_CLAIM_TTL_SECONDS = 300
#: Last-fired times expire after 30 days (the host cap); an expired one falls back to the anchor.
_LAST_TTL_SECONDS = 30 * 24 * 60 * 60

_NAME_RE = re.compile(r"[a-z0-9_-]{1,24}")
_INTERVAL_RE = re.compile(r"[0-9]{1,4}")
_SCOPE_RE = re.compile(r"[0-9a-f]{16}")

_USAGE = (
    "Usage: !timer add <name> <interval-min> <message> | !timer del <name> | !timer list | "
    "!timer enable <name> | !timer disable <name> | !timer reset (mods only)"
)
_ADD_USAGE = (
    f"Usage: !timer add <name> <interval-min> <message> "
    f"(interval {MIN_INTERVAL_MIN}-{MAX_INTERVAL_MIN} minutes)"
)
_PERMISSION_DENIED_MSG = "Only the broadcaster or a moderator can manage timers."
_BUSY_MSG = "Timers are busy right now, please try again in a moment."
_UNAVAILABLE_MSG = "Timers are temporarily unavailable, try again shortly."
_CORRUPT_MSG = "The timer config is corrupted -- a moderator can run !timer reset."

#: `(provider, channel_id, text)` -- where and what to reply if an admin command's storage fails.
_Reply = tuple[str, str, str]

_ADMIN_COMMANDS = frozenset({"add", "delete", "list", "enable", "disable", "reset"})
_KNOWN_COMMANDS = _ADMIN_COMMANDS | {"tick", "usage"}


@dataclass(slots=True, frozen=True)
class Timer:
    """One recurring announcement, as persisted in the community's config blob."""

    name: str
    interval_min: int
    message: str
    enabled: bool
    scope: str
    anchor_ms: int


class _ConfigError(ValueError):
    """The stored config blob failed validation (the message is a static, PII-free reason)."""


class _BusyError(Exception):
    """The config lock could not be taken -- surfaced as a "busy, try again" reply."""


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    A real `True` only -- a string `"false"` is truthy and must never pass the mod gate. `None`
    (neither badge present, e.g. Discord's normalizer today) is treated as denied by the caller.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


def _scope_of(platform: str, channel_id: object) -> str:
    """16-hex non-reversible id of a platform+channel -- the raw channel id is never stored."""
    return hashlib.sha256(f"{platform}|{channel_id}".encode()).hexdigest()[:16]


def _preview(message: str) -> str:
    """Shorten a stored message for `!timer list` BEFORE sanitizing (never cut an escape pair)."""
    if len(message) <= _PREVIEW_LEN:
        return message
    return message[: _PREVIEW_LEN - 1].rstrip() + "…"


def _resolve(rest: str) -> tuple[str, str | None]:
    """Map the text after `!timer` onto `(command, arg)`; `arg` is raw args or usage text.

    `rest` is already stripped and may be empty. `del` is normalised to the grammar's `delete`
    verb; everything else goes through `parse_command` unchanged.
    """
    first, _, remainder = rest.partition(" ")
    if first.lower() == "del":
        rest = f"delete {remainder}".strip()

    if not rest:
        return "usage", None
    try:
        parsed = parse_command(f"{_HEAD} {rest}", SPEC)
    except CommandUsageError as exc:
        # The grammar error quotes the offending token verbatim -- sanitized at the reply; only
        # the exception class is logged.
        log.debug("timer.grammar_rejected", error_type=type(exc).__name__)
        return "usage", str(exc)
    return _map_parsed(parsed)


def _map_parsed(parsed: ParsedCommand) -> tuple[str, str | None]:
    """Map a successful `parse_command()` result onto this bundle's command set."""
    option = parsed.option
    if option == "add":
        return "add", parsed.args
    if option in ("delete", "remove"):
        return "delete", parsed.args
    if option in ("list", "reset"):
        if parsed.args is not None:
            return "usage", f"!timer {option} takes no arguments"
        return str(option), None
    if option in ("enable", "disable"):
        # `parse_command` returns the single validated (lower-cased) name as `sub_module`.
        return option, parsed.sub_module
    return "usage", f"unknown option '{option}'" if option else None


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!timer ...` commands, else a silent chat tick.

    A `!timer` line is parsed into a command. ANY other chat line is forwarded as a `tick` so
    `dispatch` (which alone knows the community and owns `kv`) can post a due timer -- see the
    module docstring's scheduler verdict. No `kv` access here, and no log on the tick path: it
    runs for every chat line.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    channel_id = event.payload.get("channel_id")

    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    if head.lower() != _HEAD:
        if not channel_id:
            return None
        return PlatformEvent(
            platform=event.platform,
            event_type=event.event_type,
            actor=event.actor,
            payload={"command": "tick", "channel_id": channel_id},
            occurred_at=event.occurred_at,
        )

    command, arg = _resolve(rest.strip())
    log.info("timer.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": channel_id}
    if arg is not None:
        payload["arg"] = arg
    # Forward the normalized badge signal only if the platform emitted one -- absence must reach
    # `dispatch` as absence, not as an implicit `False`.
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


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


def _timer_to_json(timer: Timer) -> dict[str, Any]:
    """Serialize one timer to its stored JSON object."""
    return {
        "n": timer.name,
        "i": timer.interval_min,
        "m": timer.message,
        "e": timer.enabled,
        "c": timer.scope,
        "a": timer.anchor_ms,
    }


def _timer_from_json(raw: object) -> Timer:
    """Validate one stored timer object (strict types); raise `_ConfigError` if malformed."""
    if not isinstance(raw, dict):
        raise _ConfigError("timer entry is not an object")
    name, interval, message = raw.get("n"), raw.get("i"), raw.get("m")
    enabled, scope, anchor = raw.get("e"), raw.get("c"), raw.get("a")
    if not (isinstance(name, str) and _NAME_RE.fullmatch(name)):
        raise _ConfigError("bad timer name")
    if not (isinstance(interval, int) and not isinstance(interval, bool)):
        raise _ConfigError("interval is not an integer")
    if not MIN_INTERVAL_MIN <= interval <= MAX_INTERVAL_MIN:
        raise _ConfigError("interval out of range")
    if not (isinstance(message, str) and 1 <= len(message) <= MAX_MESSAGE_LEN):
        raise _ConfigError("bad message")
    if not isinstance(enabled, bool):
        raise _ConfigError("enabled is not a boolean")
    if not (isinstance(scope, str) and _SCOPE_RE.fullmatch(scope)):
        raise _ConfigError("bad channel scope")
    if not (isinstance(anchor, int) and not isinstance(anchor, bool) and anchor >= 0):
        raise _ConfigError("bad anchor time")
    return Timer(name, interval, message, enabled, scope, anchor)


def _parse_config(raw: bytes) -> list[Timer]:
    """Parse the stored config blob; raise `_ConfigError` unless it is valid and duplicate-free."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ConfigError("not valid JSON") from exc
    if not isinstance(data, dict) or data.get("v") != 1:
        raise _ConfigError("unsupported config version")
    items = data.get("timers")
    if not isinstance(items, list) or len(items) > MAX_TIMERS_TOTAL:
        raise _ConfigError("timers is not a list within the cap")
    timers: list[Timer] = []
    for item in items:
        timer = _timer_from_json(item)
        if any(t.name == timer.name and t.scope == timer.scope for t in timers):
            raise _ConfigError("duplicate timer name")
        timers.append(timer)
    return timers


def _dump_config(timers: list[Timer]) -> bytes:
    """Serialize the config blob (`v`ersioned so a future schema can be told apart)."""
    return json.dumps(
        {"v": 1, "timers": [_timer_to_json(t) for t in timers]},
        ensure_ascii=False,  # raw UTF-8 is at most 4 bytes per char; JSON escapes cost 6-12
        separators=(",", ":"),
    ).encode("utf-8")


async def _kv_error(exc: Exception, *, op: str, reply: _Reply | None) -> NoReturn:
    """Log (op + exception class only), reply to chat when `reply` is given, then raise.

    `reply` is `(provider, channel_id, text)` for admin commands. The tick path passes `None`:
    it runs for every chat line, so a chat reply there would spam the channel on every message
    while storage is down -- it logs and raises instead.
    """
    case_name = type(getattr(exc, "value", exc)).__name__
    log.error("timer.kv_error", op=op, error=case_name)
    if reply is not None:
        await relay.push(reply[0], {"channel": reply[1], "text": reply[2]})
    raise RuntimeError(f"timer kv {op} failed: {case_name}") from exc


async def _state_error(reason: str, *, reply: _Reply | None) -> NoReturn:
    """Fail loud on a corrupt stored config. The blob is never overwritten except by `reset`."""
    log.error("timer.state_corrupt", reason=reason)
    if reply is not None:
        await relay.push(reply[0], {"channel": reply[1], "text": reply[2]})
    raise RuntimeError(f"timer corrupt state: {reason}")


async def _load_config(community: str | None, *, reply: _Reply | None) -> list[Timer]:
    """Return the community's timers, failing loud on a backend error or corrupt blob."""
    try:
        raw = await community_kv.get(community, _CFG_KEY)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_kv_error`
        await _kv_error(exc, op="get", reply=reply)
    if raw is None:
        return []
    try:
        return _parse_config(raw)
    except _ConfigError as exc:
        corrupt = (reply[0], reply[1], _CORRUPT_MSG) if reply is not None else None
        await _state_error(str(exc), reply=corrupt)


async def _save_config(community: str | None, timers: list[Timer], *, reply: _Reply) -> None:
    """Persist the config blob (durable: no TTL -- timers live until a mod deletes them)."""
    try:
        await community_kv.set(community, _CFG_KEY, _dump_config(timers), 0)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_kv_error`
        await _kv_error(exc, op="set", reply=reply)


@contextlib.asynccontextmanager
async def _config_lock(community: str | None, *, reply: _Reply) -> AsyncIterator[None]:
    """Hold the community's config lock for a read-modify-write; raise `_BusyError` if contended.

    `kv.increment` returns `1` only to the caller that created the key. Released in `finally`; a
    failed release is logged (never swallowed silently) and the TTL expires the lock.
    """
    acquired = False
    for _ in range(_LOCK_ATTEMPTS):
        try:
            count = await community_kv.increment(community, _LOCK_KEY, 1, _LOCK_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_kv_error`
            await _kv_error(exc, op="lock", reply=reply)
        if count == 1:
            acquired = True
            break
    if not acquired:
        log.warn("timer.lock_busy")
        raise _BusyError
    try:
        yield
    finally:
        try:
            await community_kv.delete(community, _LOCK_KEY)
        except Exception as exc:  # noqa: BLE001 -- logged, never silent; the TTL expires the lock
            log.error("timer.lock_release_failed", error=type(getattr(exc, "value", exc)).__name__)


def _validate_new_timer(arg: object) -> tuple[str, int, str] | str:
    """Parse + validate `!timer add` arguments: `(name, interval, message)` or an error reply."""
    parts = arg.split(None, 2) if isinstance(arg, str) else []
    if len(parts) < 3:
        return _ADD_USAGE
    raw_name, raw_interval, message = parts[0].lower(), parts[1], parts[2].strip()
    if not _NAME_RE.fullmatch(raw_name):
        return "Timer names are 1-24 characters of a-z, 0-9, _ and -."
    if not _INTERVAL_RE.fullmatch(raw_interval):
        return _ADD_USAGE
    interval = int(raw_interval)
    if not MIN_INTERVAL_MIN <= interval <= MAX_INTERVAL_MIN:
        return f"The interval must be {MIN_INTERVAL_MIN}-{MAX_INTERVAL_MIN} minutes."
    if len(message) > MAX_MESSAGE_LEN:
        return f"Timer messages must be {MAX_MESSAGE_LEN} characters or fewer."
    if identity.mention_tokens(message):
        return "Timer messages can't @mention people (the mention would point at the wrong user)."
    if leads_with_command(message):
        return "Timer messages can't start with /, . or ! (the bot would run it as a command)."
    return raw_name, interval, message


async def _handle_add(community: str | None, arg: object, *, scope: str, reply: _Reply) -> str:
    """Validate and store a new timer bound to THIS channel (`scope`), anchored at now."""
    validated = _validate_new_timer(arg)
    if isinstance(validated, str):
        return validated
    name, interval, message = validated
    async with _config_lock(community, reply=reply):
        timers = await _load_config(community, reply=reply)
        mine = [t for t in timers if t.scope == scope]
        if any(t.name == name for t in mine):
            return f"A timer named '{name}' already exists -- !timer del {name} first."
        if len(mine) >= MAX_TIMERS:
            return f"Too many timers ({MAX_TIMERS} per channel max) -- delete one first."
        if len(timers) >= MAX_TIMERS_TOTAL:
            return f"This community has reached its timer limit ({MAX_TIMERS_TOTAL})."
        timers.append(
            Timer(
                name=name,
                interval_min=interval,
                message=message,
                enabled=True,
                scope=scope,
                anchor_ms=clock.now_millis(),
            )
        )
        if len(_dump_config(timers)) > MAX_CONFIG_BYTES:
            return "This community's timer storage is full -- delete a timer first."
        await _save_config(community, timers, reply=reply)
    log.info("timer.added", count=len(mine) + 1)
    return (
        f"Timer '{name}' added: posts here every {interval} min once chat is active "
        f"(it needs a chat line to post, so a silent channel stays silent)."
    )


async def _forget_last(community: str | None, scope: str, name: str) -> None:
    """Best-effort removal of a deleted timer's last-fired marker (it expires on its own)."""
    try:
        await community_kv.delete(community, _last_key(scope, name))
    except Exception as exc:  # noqa: BLE001 -- logged, never silent; the TTL expires the key
        log.error("timer.last_cleanup_failed", error=type(getattr(exc, "value", exc)).__name__)


async def _handle_delete(community: str | None, arg: object, *, scope: str, reply: _Reply) -> str:
    """Delete one of THIS channel's timers by name (another channel's timer is invisible here)."""
    name = arg.strip().lower() if isinstance(arg, str) else ""
    if not _NAME_RE.fullmatch(name):
        return "Usage: !timer del <name>"
    async with _config_lock(community, reply=reply):
        timers = await _load_config(community, reply=reply)
        remaining = [t for t in timers if not (t.scope == scope and t.name == name)]
        if len(remaining) == len(timers):
            return f"No timer named '{name}'."
        await _save_config(community, remaining, reply=reply)
    await _forget_last(community, scope, name)
    log.info("timer.deleted", count=len(remaining))
    return f"Timer '{name}' deleted."


async def _handle_toggle(
    community: str | None, arg: object, *, enable: bool, scope: str, reply: _Reply
) -> str:
    """Enable (restarting its countdown) or disable one of THIS channel's timers by name."""
    verb = "enable" if enable else "disable"
    name = arg.strip().lower() if isinstance(arg, str) else ""
    if not _NAME_RE.fullmatch(name):
        return f"Usage: !timer {verb} <name>"
    async with _config_lock(community, reply=reply):
        timers = await _load_config(community, reply=reply)
        target = next((t for t in timers if t.scope == scope and t.name == name), None)
        if target is None:
            return f"No timer named '{name}'."
        if target.enabled is enable:
            return f"Timer '{name}' is already {verb}d."
        changed = replace(
            target, enabled=enable, anchor_ms=clock.now_millis() if enable else target.anchor_ms
        )
        await _save_config(community, [changed if t is target else t for t in timers], reply=reply)
    log.info("timer.toggled", enabled=enable)
    return f"Timer '{name}' {verb}d."


async def _handle_list(community: str | None, *, scope: str, reply: _Reply) -> str:
    """Summarize THIS channel's timers (name, interval, state, sanitized message preview)."""
    timers = [t for t in await _load_config(community, reply=reply) if t.scope == scope]
    if not timers:
        return "No timers set. Add one with !timer add <name> <interval-min> <message>."
    header = f"Timers ({len(timers)}): "
    parts: list[str] = []
    used = len(header)
    for shown, timer in enumerate(timers):
        state = "on" if timer.enabled else "off"
        part = (
            f"{timer.name} ({timer.interval_min}m, {state}): "
            f"{sanitize_user_text(_preview(timer.message))}"
        )
        if shown and used + len(part) + 3 > _LIST_BUDGET:
            parts.append(f"(+{len(timers) - shown} more)")
            break
        parts.append(part)
        used += len(part) + 3
    return header + " | ".join(parts)


async def _handle_reset(community: str | None, *, scope: str, reply: _Reply) -> str:
    """Delete THIS channel's timers; a corrupt blob (unreadable, so unscopable) is dropped whole.

    The blob is the one shared record of every channel in the community: a valid one only loses
    the invoking channel's entries. A corrupt one cannot be told apart per channel, so `reset`
    stays the documented recovery and removes it entirely (logged at WARN).
    """
    async with _config_lock(community, reply=reply):
        try:
            raw = await community_kv.get(community, _CFG_KEY)
        except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_kv_error`
            await _kv_error(exc, op="get", reply=reply)
        gone: list[Timer] = []
        keep: list[Timer] = []
        corrupt = False
        if raw is not None:
            try:
                timers = _parse_config(raw)
            except _ConfigError as exc:
                corrupt = True
                log.warn("timer.reset_dropped_corrupt_config", reason=str(exc))
            else:
                gone = [t for t in timers if t.scope == scope]
                keep = [t for t in timers if t.scope != scope]
        if corrupt or (raw is not None and not keep):
            try:
                await community_kv.delete(community, _CFG_KEY)
            except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_kv_error`
                await _kv_error(exc, op="delete", reply=reply)
        elif raw is not None:
            await _save_config(community, keep, reply=reply)
    for timer in gone:
        await _forget_last(community, timer.scope, timer.name)
    log.info("timer.reset", removed=len(gone), corrupt=corrupt)
    return "All timers for this channel deleted."


def _last_key(scope: str, name: str) -> str:
    """The last-fired marker key of one channel's timer."""
    return f"{_LAST_PREFIX}{scope}.{name}"


async def _read_last(community: str | None, scope: str, name: str) -> int | None:
    """Last-fired time of `name` in ms, or `None` if unset/expired/corrupt (anchor is used)."""
    try:
        raw = await community_kv.get(community, _last_key(scope, name))
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_kv_error`
        await _kv_error(exc, op="get_last", reply=None)
    if raw is None:
        return None
    try:
        value = int(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        log.error("timer.last_corrupt")
        return None
    return value if value >= 0 else None


async def _claim(community: str | None, scope: str, name: str, due_at: int) -> bool:
    """Win the right to post `name` for the occurrence due at `due_at` (`True` for one caller)."""
    try:
        count = await community_kv.increment(
            community, f"{_CLAIM_PREFIX}{scope}.{name}.{due_at}", 1, _CLAIM_TTL_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_kv_error`
        await _kv_error(exc, op="claim", reply=None)
    return count == 1


async def _handle_tick(community: str | None, *, provider: str, channel_id: str) -> str | None:
    """On an ordinary chat line, return the text of ONE due timer for this channel, else `None`.

    Runs for every chat line, so the common idle path is a single `kv.get`. At most one timer
    posts per line (the most overdue). The winner of the `(timer, due-time)` claim records the
    new last-fired time before returning the text -- see the module docstring.
    """
    timers = await _load_config(community, reply=None)
    scope = _scope_of(provider, channel_id)
    mine = [t for t in timers if t.enabled and t.scope == scope]
    if not mine:
        return None

    now = clock.now_millis()
    due: list[tuple[int, Timer]] = []
    for timer in mine:
        last = await _read_last(community, scope, timer.name)
        due_at = max(timer.anchor_ms, last or 0) + timer.interval_min * 60_000
        if now >= due_at:
            due.append((due_at, timer))
    due.sort(key=lambda pair: (pair[0], pair[1].name))

    for due_at, timer in due:
        if not await _claim(community, scope, timer.name, due_at):
            continue
        try:
            await community_kv.set(
                community, _last_key(scope, timer.name), str(now).encode(), _LAST_TTL_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_kv_error`
            await _kv_error(exc, op="set_last", reply=None)
        text = sanitize_user_text(timer.message)
        if not text.strip() or leads_with_command(text):
            log.error("timer.unsafe_message_skipped")
            continue
        log.info("timer.fired", due=len(due))
        return text
    if due:
        log.debug("timer.tick lost every claim", due=len(due))
    return None


async def _run_admin(
    command: str,
    payload: dict[str, Any],
    community: str | None,
    *,
    provider: str,
    channel_id: str,
) -> str:
    """Run one mod-gated admin command and return its chat reply.

    A moderator badge is valid in the channel it was earned in, so every command here is scoped
    to that channel's timers: it can neither see, change nor delete another channel's.
    """
    if _caller_role_signal(payload) is not True:
        log.info("timer.permission_denied", command=command)
        return _PERMISSION_DENIED_MSG
    reply = (provider, channel_id, _UNAVAILABLE_MSG)
    scope = _scope_of(provider, channel_id)
    arg = payload.get("arg")
    if command == "add":
        return await _handle_add(community, arg, scope=scope, reply=reply)
    if command == "delete":
        return await _handle_delete(community, arg, scope=scope, reply=reply)
    if command == "enable":
        return await _handle_toggle(community, arg, enable=True, scope=scope, reply=reply)
    if command == "disable":
        return await _handle_toggle(community, arg, enable=False, scope=scope, reply=reply)
    if command == "list":
        return await _handle_list(community, scope=scope, reply=reply)
    return await _handle_reset(community, scope=scope, reply=reply)


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: admin commands, or a silent due-timer check per line.

    `config`/`http_client` are accepted but unused -- `kv` + the WIT `relay`/`clock` imports only.

    Raises:
        ValueError: The payload has no `channel_id`; the envelope's `community` is an empty
            string (a caller-side bug -- the host's tenant-wide sentinel is `None`, which works);
            or an unrecognized `command` (defensive -- `transform` only emits a known one).
        RuntimeError: A `kv` backend call failed or the stored config is corrupt. Admin commands
            reply to chat first; the tick path only logs (it runs for every chat line).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("timer requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized timer command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if community == "":
        log.error("timer.missing_community", command=command)
        if command != "tick":
            await relay.push(
                provider,
                {
                    "channel": channel_id,
                    "text": "Timers aren't available here yet -- ask an admin to link this "
                    "server/channel to a community.",
                },
            )
        raise ValueError("timer requires a community context (empty-string community)")

    if command == "tick":
        text = await _handle_tick(community, provider=provider, channel_id=channel_id)
        if text is None:
            return DispatchResult(transport=provider, detail="tick:idle")
        await relay.push(provider, {"channel": channel_id, "text": text})
        return DispatchResult(transport=provider, detail="tick:fired")

    if command == "usage":
        arg = payload.get("arg")
        reply_text = f"{_USAGE} ({sanitize_user_text(arg)})" if isinstance(arg, str) else _USAGE
    else:
        try:
            reply_text = await _run_admin(
                command, payload, community, provider=provider, channel_id=channel_id
            )
        except _BusyError:
            reply_text = _BUSY_MSG

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("timer.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
