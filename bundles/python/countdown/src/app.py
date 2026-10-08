"""`!countdown` -> a per-community single countdown target, kv-only.

v1 scope (2026-10-07, feature/bundle-countdown):

- `!countdown set <ISO-or-relative-time> [label]` stores one target time for
  the whole community (not per-user) in `kv`, overwriting any existing
  target. `<ISO-or-relative-time>` accepts either an ISO-8601 timestamp
  (`datetime.fromisoformat`, e.g. `2026-12-25T00:00:00Z`; a naive value with
  no offset is treated as UTC) or a relative duration token made of `d`/`h`/
  `m`/`s` components (`3d`, `2h30m`, `1d12h5m`, case-insensitive, at least
  one component) measured from "now" (the host `clock` capability). The
  optional `[label]` is everything after the time token, stored verbatim and
  echoed back in replies.
- Bare `!countdown` reports time remaining until the stored target (e.g.
  "2d 3h 14m until New Year"), or, once the target has passed, how long ago
  it happened -- it never goes silent or shows a negative duration.
- `!countdown reset`, anyone, clears the stored target early so the next
  `!countdown set` starts fresh. **Note on command wording:** the shared SDK
  verb vocabulary (`waddle_sdk.command.VERBS`) has no `clear` verb -- `reset`
  is the closest declared verb and is what this bundle actually parses;
  "clear" is the common description of what `reset` does, not a second
  accepted spelling.

No scheduler, ever (spec requirement, same as `first`): there's nothing to
tick. The stored target is a single absolute millisecond timestamp; "time
remaining" is recomputed fresh on every bare `!countdown` from the host
`clock` capability, so there is no window/expiry mechanism to get wrong.

PII: pseudonymization doesn't apply here -- this bundle never stores or
replies with any caller identity, only a community-wide target time and an
operator-supplied label (never `event.actor`).

Data scoping: the stored target is community-scoped via
`waddle_sdk.community_kv` (never global/tenant-wide, mirrors `first`'s own
"Data scoping" section) -- `dispatch` checks `envelope.community` itself up
front for a clear, command-specific error message if it's missing.

Command grammar: parsed via `waddle_sdk.command.parse_command()` against a
declared `CommandSpec` (`sdk/waddle-sdk/AUTHORING.md` Sec1), no sub-modules.
`set`/`reset` are the grammar's own declared verbs; bare `!countdown` (no
verb) is the command's own default behavior (report remaining/elapsed time),
exactly the shape the grammar doc calls out for `!count`/`!first`.

Business logic split: `transform` only recognizes the command/verb shape and
forwards the normalized action + raw argument text (no `kv` access) --
`dispatch` performs every `kv` read/write, the time-token parsing, and the
relay reply, mirroring `first`'s own process/action-stage split.

Gated behind the PostHog flag ``waddles.command-countdown``, default OFF --
see `bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (checked only after the command-text match).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, NoReturn

from waddle_sdk import clock, community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-countdown"

SPEC = CommandSpec(name="countdown", sub_modules=frozenset())

#: The single community-wide target: `{"target_ms": int, "label": str | None}` as JSON.
#: Persistent (no TTL) -- a countdown target is durable state, not a session/window value.
_TARGET_KEY = "countdown.target"
_PERSISTENT_TTL_SECONDS = 0

_USAGE = (
    "Usage: !countdown | !countdown set <ISO-or-relative-time> [label] | !countdown reset"
)
_USAGE_SET = (
    "Usage: !countdown set <ISO-or-relative-time e.g. 3d2h or 2026-12-25T00:00:00Z> [label]"
)

_KNOWN_ACTIONS = frozenset({"report", "set", "clear", "usage"})

#: Relative duration token: at least one of d/h/m/s, case-insensitive, no internal whitespace.
_DURATION_RE = re.compile(
    r"^(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$",
    re.IGNORECASE,
)


class _InvalidTimeTokenError(ValueError):
    """Raised by `_parse_time_token` for a token that is neither valid ISO-8601 nor a duration.

    Caught by `_handle_set`, which turns it into a chat usage reply rather
    than letting it propagate -- malformed user input is normal operation,
    never a fail-loud backend error (contrast `_fail_kv`, below).
    """


def _parse_duration_seconds(token: str) -> int | None:
    """Parse a `3d2h5m10s`-shaped token into total seconds, or `None` if it doesn't match.

    Requires at least one component present (an empty string or a string
    with no `d`/`h`/`m`/`s` suffix matches the regex but yields zero
    components, which is rejected as meaningless rather than a 0-second
    countdown).
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


def _parse_time_token(token: str, *, now_ms: int) -> int:
    """Resolve `token` (ISO-8601 or relative duration) to an absolute epoch-millisecond target.

    Tries the relative-duration grammar first (cheap, no exceptions on the
    common case), then falls back to `datetime.fromisoformat`. Raises
    `_InvalidTimeTokenError` if neither matches -- never guesses.
    """
    duration_seconds = _parse_duration_seconds(token)
    if duration_seconds is not None:
        return now_ms + duration_seconds * 1000

    try:
        parsed = datetime.fromisoformat(token)
    except ValueError as exc:
        raise _InvalidTimeTokenError(
            f"{token!r} is not a valid ISO-8601 timestamp or relative duration (e.g. 3d2h)"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _format_duration(total_seconds: int) -> str:
    """Render a non-negative second count as `"Xd Xh Xm"`/`"Xh Xm"`/`"Xm"`/`"Xs"`.

    Always includes minutes once the duration reaches an hour or more (never
    drops a middle zero unit), and only falls back to seconds-only display
    under one minute -- matches the module docstring's `"2d 3h 14m"` example.
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
    """Map a successfully parsed `!countdown ...` command to one of `_KNOWN_ACTIONS`.

    Returns `(action, arg)` where `arg` carries a ready-to-send usage string
    for `action == "usage"`, or the raw post-verb text for `action ==
    "set"`. Never raises -- a structurally valid but meaningless combination
    (e.g. `!countdown list`, a real grammar verb with no meaning for this
    command) degrades to `"usage"` rather than a crash, mirroring `first`'s
    own `_classify`.
    """
    if parsed.option is None:
        return "report", None
    if parsed.option == "set":
        if parsed.args is None:
            return "usage", _USAGE_SET
        return "set", parsed.args
    if parsed.option == "reset":
        if parsed.args is not None:
            return "usage", _USAGE
        return "clear", None
    return "usage", _USAGE


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!countdown` and its verbs.

    No `kv` access here -- only command recognition and forwarding the
    normalized action + raw argument text for `dispatch`. Returns `None` for
    any non-chat payload, text that isn't `!countdown`-shaped (cheap-skip,
    zero `kv` cost), and while `waddles.command-countdown` is disabled.
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
        log.info("countdown.transform usage_error", error=str(exc))
        action, arg = "usage", str(exc)
    else:
        action, arg = _classify(parsed)

    log.info("countdown.transform matched", action=action)
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
    log.error("countdown.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {
            "channel": channel_id,
            "text": "countdown is temporarily unavailable, try again shortly.",
        },
    )
    raise RuntimeError(f"countdown kv {op} failed: {case_name}") from exc


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


async def _kv_delete(key: str, *, community: str, provider: str, channel_id: str) -> None:
    """Community-scoped `kv.delete`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        await community_kv.delete(community, key)
    except Exception as exc:  # noqa: BLE001
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="delete")


def _load_target(raw: bytes | None, *, community: str) -> tuple[int, str | None] | None:
    """Decode the stored `{"target_ms", "label"}` JSON, or `None` if unset/corrupt.

    Corrupt content (not the expected JSON shape) self-heals to "no target
    set" and logs -- never crashes `dispatch`, mirrors `first`'s own
    `_parse_int`/leaderboard-registry corruption stance.
    """
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.error("countdown.state_corrupt", context="target", community=community)
        return None
    if not isinstance(data, dict) or "target_ms" not in data:
        log.error("countdown.state_corrupt", context="target", community=community)
        return None
    target_ms = data["target_ms"]
    if not isinstance(target_ms, int):
        log.error("countdown.state_corrupt", context="target", community=community)
        return None
    label = data.get("label")
    if label is not None and not isinstance(label, str):
        label = None
    return target_ms, label


async def _handle_set(
    arg: str, *, community: str, provider: str, channel_id: str
) -> str:
    """Parse `<time-token> [label]` and overwrite the community's stored countdown target."""
    token, _, label_rest = arg.partition(" ")
    label = label_rest.strip() or None

    now_ms = clock.now_millis()
    try:
        target_ms = _parse_time_token(token, now_ms=now_ms)
    except _InvalidTimeTokenError as exc:
        log.info("countdown.set invalid_time_token", token=token)
        return f"{exc} {_USAGE_SET}"

    await _kv_set(
        _TARGET_KEY,
        json.dumps({"target_ms": target_ms, "label": label}).encode("utf-8"),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_PERSISTENT_TTL_SECONDS,
    )
    log.info("countdown.set", community=community, target_ms=target_ms)

    remaining_seconds = max(0, (target_ms - now_ms) // 1000)
    label_text = label or "the countdown"
    if target_ms > now_ms:
        return f"Countdown set: {_format_duration(remaining_seconds)} until {label_text}."
    return f"Countdown set for a time already in the past -- {label_text} already happened."


async def _handle_report(*, community: str, provider: str, channel_id: str) -> str:
    """Report remaining/elapsed time against the stored target -- never mutates state."""
    raw = await _kv_get(_TARGET_KEY, community=community, provider=provider, channel_id=channel_id)
    loaded = _load_target(raw, community=community)
    if loaded is None:
        return "No countdown set -- use !countdown set <time> [label] to start one."

    target_ms, label = loaded
    label_text = label or "the countdown"
    now_ms = clock.now_millis()
    diff_ms = target_ms - now_ms
    if diff_ms > 0:
        return f"{_format_duration(diff_ms // 1000)} until {label_text}."
    elapsed_seconds = (-diff_ms) // 1000
    return f"{label_text} happened {_format_duration(elapsed_seconds)} ago."


async def _handle_clear(*, community: str, provider: str, channel_id: str) -> str:
    """Delete the stored countdown target early."""
    await _kv_delete(_TARGET_KEY, community=community, provider=provider, channel_id=channel_id)
    log.info("countdown.reset", community=community)
    return "Countdown cleared -- the next !countdown set starts a fresh one."


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
        raise ValueError("countdown reply requires a channel_id from the inbound chat.message")
    action = payload.get("action")
    if action not in _KNOWN_ACTIONS:
        raise ValueError(f"unrecognized countdown action: {action!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("countdown.missing_community", action=action)
        raise ValueError("countdown requires a community context and cannot operate tenant-wide")

    if action == "usage":
        text = payload.get("arg")
        reply_text = text if isinstance(text, str) and text else _USAGE
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        return DispatchResult(transport=provider, detail="usage")

    if action == "set":
        arg = payload.get("arg")
        if not isinstance(arg, str) or not arg:
            raise ValueError("countdown set action missing its forwarded arg")
        reply_text = await _handle_set(
            arg, community=community, provider=provider, channel_id=channel_id
        )
    elif action == "report":
        reply_text = await _handle_report(
            community=community, provider=provider, channel_id=channel_id
        )
    else:  # clear
        reply_text = await _handle_clear(
            community=community, provider=provider, channel_id=channel_id
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("countdown.dispatch relayed", platform=provider, action=action)
    return DispatchResult(transport=provider, detail=action)
