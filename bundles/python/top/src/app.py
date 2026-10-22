"""`!top [N]` -> the community's top-N members by economy balance, read-only.

Idea inspired by superpenguintv (Psychoboy)'s PenguinTwitchBot (with permission) --
ideas/design only, no code copied.

Grammar: bare `!top` (default `DEFAULT_LIMIT`), `!top <N>` or the fused `!top<N>` form
(`!top5`, `!top10`), `1 <= N <= MAX_LIMIT`. Anything else is a usage reply, never a silent
drop. No `toptime`/watchtime variant: no watchtime signal exists in the SDK (only a balance
`economy.leaderboard`), and `kv` has no scan primitive to build one.

**PII-free.** `economy.leaderboard` returns community user UUIDs only. The reply embeds each as
a `{user:<uuid>}` placeholder that the egress detokenizer renders to a display name; this bundle
never sees, stores or logs a name. Logs carry op/platform/count/exception type only.

**Read-only.** `economy.read` only: no wager/transfer, no `storage.kv`, no state. A host refusal
(not granted/unavailable/backend) is logged by type and answered with a generic reply, then
re-raised -- never swallowed into an empty board.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.
"""

from __future__ import annotations

import re
from typing import Any

from waddle_sdk import economy, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SPEC = CommandSpec(name="top")
DEFAULT_LIMIT = 5
MAX_LIMIT = 10

_USAGE = (
    f"usage: !top [N] -- the top N (1-{MAX_LIMIT}, default {DEFAULT_LIMIT}) by balance"
)
_UNAVAILABLE = "the leaderboard is temporarily unavailable, try again shortly."
_EMPTY = "nobody has a balance yet."
_FUSED = re.compile(r"^!top(\d+)$", re.IGNORECASE)
_DIGITS = re.compile(r"^\d+$")


def _parse_limit(text: str) -> int:
    """Return the requested N for a `!top` message, or raise `CommandUsageError`.

    Numeric forms are peeled off before `parse_command` (which rejects non-verb tokens), then the
    bare `!top` is still run through it so any extra/verb tokens fail loudly.
    """
    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    rest = rest.strip()
    fused = _FUSED.match(head)
    raw: str | None = None
    if fused:
        if rest:
            raise CommandUsageError(_USAGE)
        raw = fused.group(1)
        parse_command("!top", SPEC)
    elif rest and _DIGITS.match(rest):
        raw = rest
        parse_command(head, SPEC)
    else:
        try:
            parsed = parse_command(stripped, SPEC)
        except CommandUsageError as exc:
            raise CommandUsageError(_USAGE) from exc
        if parsed.option is not None or parsed.sub_module is not None:
            raise CommandUsageError(_USAGE)
    if raw is None:
        return DEFAULT_LIMIT
    limit = int(raw)
    if not 1 <= limit <= MAX_LIMIT:
        raise CommandUsageError(_USAGE)
    return limit


def _is_top(text: str) -> bool:
    """Cheap match: the leading token is `!top` or `!top<digits>` (so `!topic` never matches)."""
    head = text.strip().partition(" ")[0].lower()
    return head == "!top" or bool(_FUSED.match(head))


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!top [N]`, parse N."""
    text = event.payload.get("text")
    if not isinstance(text, str) or not _is_top(text):
        return None

    payload: dict[str, Any] = {"channel_id": event.payload.get("channel_id")}
    try:
        payload["limit"] = _parse_limit(text)
        payload["command"] = "top"
    except CommandUsageError:
        payload["command"] = "usage"
    log.info(
        "top.transform matched", platform=event.platform, command=payload["command"]
    )
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
        occurred_at=event.occurred_at,
    )


def render(entries: list[economy.LeaderboardEntry]) -> str:
    """Render ranked rows as `1. {user:<uuid>} - <balance>`; names resolve at egress."""
    if not entries:
        return _EMPTY
    rows = [
        f"{i}. {{user:{e.user}}} - {e.balance}" for i, e in enumerate(entries, start=1)
    ]
    return "Top balances: " + " | ".join(rows)


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
    """Implement `action-stage.dispatch`: read the leaderboard and relay it to the origin platform.

    Raises:
        ValueError: No `channel_id`, or a `top` payload whose limit is not an int in range.
        economy.EconomyError: The host refused/failed the read (a generic reply is sent first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError(
            "top reply requires a channel_id from the inbound chat.message"
        )
    provider = envelope.event.platform

    if payload.get("command") == "usage":
        text = _USAGE
    else:
        limit = payload.get("limit")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_LIMIT
        ):
            raise ValueError("top payload requires an integer limit in range")
        try:
            entries = await economy.leaderboard(limit)
        except economy.EconomyError as exc:
            log.error("top.economy_error", op="leaderboard", error=type(exc).__name__)
            await relay.push(provider, {"channel": channel_id, "text": _UNAVAILABLE})
            raise
        text = render(entries)
        log.info("top.dispatch", op="leaderboard", count=len(entries), limit=limit)

    await relay.push(provider, {"channel": channel_id, "text": text})
    return DispatchResult(transport=provider, detail="relayed")
