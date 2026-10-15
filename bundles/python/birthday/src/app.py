"""`!birthday` -> per-user birthday (month-day only, no year), set by the user themself.

Commands (grammar via `waddle_sdk.command` for the `set` verb, hand-resolved for the rest):

- ``!birthday set <MM-DD>``  caller sets their OWN birthday (never mod-gated; no way to set
  anyone else's). Validated: month 01-12, day valid for that month (``02-29`` is accepted --
  there is no year to disqualify a leap day). A bad date replies with an explicit message.
- ``!birthday``              read the caller's own birthday (or "not set").
- ``!birthday @user`` / ``<@id>``  read another user's birthday (public by design).
- ``!birthday clear``        caller removes their own birthday.

Privacy: ONLY month and day are stored -- never a year -- as ASCII ``MM-DD``. The per-user
`kv` key is ``birthday.<sha256(platform:identity)>`` (never a raw username; same rationale
as `duel`/`social`'s `_pseudonym()`: `event.actor` may be a raw username while the
tokenization pipeline is pending). The identity is lower-cased so the caller's own actor and
a typed ``@Name`` mention resolve to the same key. The key uses ``.``, never ``:`` (the host's
reserved separator, gh-631). The `kv` host scopes every key by `(tenant, community, app_id)`.

Ordering inside `transform()`: cheap command match first, then grammar, then `kv`. A `kv` failure or
a corrupt stored value becomes a private `_KvError`, logged at ERROR and answered with an explicit
reply -- never a silent no-op. A bad date is a `CommandUsageError` answered with its own explicit
message. Logs are PII-free: only op names, platform and exception types -- never the birthday value,
the actor, or the ``@target``.

`kv`, `log` and `relay` are imported at MODULE LOAD on purpose:
componentize-py only wizens a WIT host-import that is imported eagerly
(`docs/reference/bundle-host-imports.md`).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, cast

from waddle_sdk import kv, log, relay
from waddle_sdk.command import VERBS, CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: Prefix of the per-user `kv` key (community-scoped by the host; `.`, never `:`).
KEY_PREFIX = "birthday."

_SPEC = CommandSpec(name="birthday")
_COMMAND = "!birthday"

_USAGE = (
    "Usage: !birthday | !birthday set <MM-DD> | !birthday @user | !birthday clear "
    "(month and day only, no year)"
)
_KV_FAILURE_REPLY = "Something went wrong with birthday storage - please try again."
_NO_IDENTITY_REPLY = "I couldn't tell who you are, so I can't use your birthday."

_MONTH_NAMES: tuple[str, ...] = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
#: Days per month; February is 29 because a month-day-only birthday has no year to rule out
#: a leap day.
_DAYS_IN_MONTH: tuple[int, ...] = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

_MONTH_DAY_RE = re.compile(r"^([0-9]{2})-([0-9]{2})$")
#: ``<@id>`` / ``<@!id>`` (Discord mention, group 1) or ``@name`` (group 2) -- nothing else.
_TARGET_RE = re.compile(r"^(?:<@!?([0-9]{1,32})>|@([A-Za-z0-9_][A-Za-z0-9_.-]{0,31}))$")


class _KvError(Exception):
    """Internal-only: a `kv` host-call failed or the stored value is corrupt.

    Always caught inside `transform`, never leaked. Mirrors `waddle_sdk.db`'s documented
    pattern ("Err is structurally classified, never imported") since `waddle_sdk.kv` does
    not classify the generated WIT `Err` itself.
    """


@dataclass(slots=True, frozen=True)
class _Request:
    """A resolved `!birthday` request: the operation plus its already-validated input.

    `month`/`day` are set for `set`; `target`/`target_id` (the typed token and the id inside
    it) are set for `read_other`.
    """

    op: str
    month: int = 0
    day: int = 0
    target: str = ""
    target_id: str = ""


def _pseudonym(identity: str) -> str:
    """Non-reversible per-identity key component -- see `duel`'s own `_pseudonym()` for why."""
    return hashlib.sha256(identity.encode()).hexdigest()


def _identity(platform: str, user_id: str) -> str:
    """Canonical `platform:id` string that gets hashed (id lower-cased for stable matching)."""
    return f"{platform}:{user_id.lower()}"


def _key(platform: str, user_id: str) -> str:
    """Per-(community, user) `kv` key: ``birthday.<sha256(platform:id)>`` -- never raw."""
    return f"{KEY_PREFIX}{_pseudonym(_identity(platform, user_id))}"


def parse_month_day(raw: str) -> tuple[int, int]:
    """Validate a ``MM-DD`` string and return ``(month, day)``.

    Raises:
        CommandUsageError: Not ``MM-DD``, month outside 01-12, or day invalid for the month
            (``02-29`` is valid; ``02-30`` / ``04-31`` are not). The message is the reply.
    """
    match = _MONTH_DAY_RE.match(raw)
    if match is None:
        raise CommandUsageError(
            f"'{raw}' is not a valid date - use !birthday set MM-DD (e.g. 03-25)"
        )
    month, day = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise CommandUsageError(f"'{raw}' has an invalid month - the month must be 01-12")
    if not 1 <= day <= _DAYS_IN_MONTH[month - 1]:
        raise CommandUsageError(
            f"'{raw}' is not a real date - {_MONTH_NAMES[month - 1]} has "
            f"{_DAYS_IN_MONTH[month - 1]} days at most"
        )
    return month, day


def format_month_day(month: int, day: int) -> str:
    """Render ``(3, 25)`` as ``March 25``."""
    return f"{_MONTH_NAMES[month - 1]} {day}"


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvError` (fail loud)."""
    try:
        result = await kv.get(key)
    except Exception as exc:
        raise _KvError(f"kv.get failed: {getattr(exc, 'value', exc)}") from exc
    # `waddle_sdk` ships no `py.typed`, so mypy sees `Any` -- cast back to the real contract.
    return cast("bytes | None", result)


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL -- a birthday persists), reclassifying `Err` into `_KvError`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:
        raise _KvError(f"kv.set failed: {getattr(exc, 'value', exc)}") from exc


async def _kv_delete(key: str) -> None:
    """`kv.delete`, reclassifying `Err` into `_KvError`."""
    try:
        await kv.delete(key)
    except Exception as exc:
        raise _KvError(f"kv.delete failed: {getattr(exc, 'value', exc)}") from exc


async def _load(key: str) -> str | None:
    """Return the stored birthday as a display string, or `None` if unset.

    Raises:
        _KvError: The `kv` get failed or the stored value is not a valid ``MM-DD``.
    """
    raw = await _kv_get(key)
    if raw is None:
        return None
    try:
        month, day = parse_month_day(raw.decode("utf-8"))
    except (UnicodeDecodeError, CommandUsageError) as exc:
        raise _KvError(f"corrupt stored birthday: {type(exc).__name__}") from exc
    return format_month_day(month, day)


def _resolve(text: str) -> _Request:
    """Turn the message into a `_Request`, or raise `CommandUsageError` with the reply.

    Verbs go through the shared grammar parser (only `set` is meaningful); `clear` and an
    ``@target`` / ``<@id>`` token are resolved here. A bare word that is neither is a usage
    reply -- so a forgotten ``set`` (``!birthday 03-25``) is never mistaken for a username.
    """
    rest = text.strip().partition(" ")[2].strip()
    if not rest:
        return _Request(op="read_own")
    token, _, tail = rest.partition(" ")
    token_lower = token.lower()
    if token_lower in VERBS:
        parsed = parse_command(text, _SPEC)
        if parsed.option != "set":
            raise CommandUsageError(_USAGE)
        if parsed.args is None or " " in parsed.args:
            raise CommandUsageError("Usage: !birthday set <MM-DD> (e.g. !birthday set 03-25)")
        month, day = parse_month_day(parsed.args)
        return _Request(op="set", month=month, day=day)
    if tail.strip():
        raise CommandUsageError(_USAGE)
    if token_lower == "clear":
        return _Request(op="clear")
    target = _TARGET_RE.match(token)
    if target is None:
        raise CommandUsageError(_USAGE)
    return _Request(op="read_other", target=token, target_id=target.group(1) or target.group(2))


async def _run_request(request: _Request, platform: str, actor: str | None) -> str:
    """Execute `request` against `kv` and return the reply text.

    Raises:
        _KvError: A `kv` round trip failed or stored data is corrupt.
    """
    if request.op == "read_other":
        shown = await _load(_key(platform, request.target_id))
        if shown is None:
            return f"{request.target} hasn't set a birthday."
        return f"{request.target}'s birthday is {shown}."

    if not actor:
        return _NO_IDENTITY_REPLY
    own_key = _key(platform, actor)

    if request.op == "set":
        await _kv_set(own_key, f"{request.month:02d}-{request.day:02d}".encode())
        return (
            f"Birthday saved: {format_month_day(request.month, request.day)} (month and day only)."
        )
    if request.op == "clear":
        if await _load(own_key) is None:
            return "You don't have a birthday set."
        await _kv_delete(own_key)
        return "Your birthday has been cleared."
    shown = await _load(own_key)
    if shown is None:
        return "You haven't set your birthday - use !birthday set MM-DD."
    return f"Your birthday is {shown}."


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!birthday` and build the reply.

    Returns `None` for a non-chat payload, any message that is not `!birthday`, and while
    `waddles.command-birthday` is off. A grammar/date error replies with its own message;
    a `kv` failure logs at ERROR and replies with an explicit failure notice.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head = text.strip().partition(" ")[0]
    if head.lower() != _COMMAND:
        return None

    try:
        request = _resolve(text)
        log.debug("birthday.resolved", op=request.op)
        reply = await _run_request(request, event.platform, event.actor)
    except CommandUsageError as exc:
        log.debug("birthday.usage_error")
        reply = str(exc)
    except _KvError as exc:
        log.error("birthday.kv_failure", error_type=type(exc.__cause__).__name__)
        reply = _KV_FAILURE_REPLY

    log.info("birthday.transform matched")
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"channel_id": event.payload.get("channel_id"), "text": reply},
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("detail", "http_status", "sub_type", "transport")

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

    Raises:
        ValueError: The envelope's payload is missing `channel_id` or `text`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError("birthday reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("birthday reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("birthday.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
