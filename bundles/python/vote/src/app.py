"""`!vote <option>` -> a quick per-community tally (lighter than `bundles/python/poll`).

Verbs: bare `!vote` shows current standings; `!vote start` opens a fresh tally (broadcaster/
moderator only, fails CLOSED when role info is absent -- same contract as `count`);
`!vote close` closes it and shows the final standings (same privilege); `!vote <option>` casts
(or changes) the caller's single vote while the tally is open. Options are lowercased single
words (<=32 chars, <=20 distinct options).

State: ONE community-scoped JSON document at kv key `vote.state` (the host `kv` capability scopes
every key by tenant/community/app_id; kv keys never contain ':', gh-631). Voters are stored only
as a truncated SHA-256 of the actor id (one vote each, re-vote moves it) -- never the raw actor.
The document is read-modify-written non-atomically; concurrent votes in the same instant can lose
an update (acceptable for a quick chat tally). Logging is PII-free: only op / counts / exception
type are logged -- never option text or actor ids.

Gated behind the PostHog flag ``waddles.command-vote``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-vote"
COMMAND = "!vote"
STATE_KEY = "vote.state"
MAX_OPTIONS = 20
MAX_OPTION_LEN = 32
MAX_VOTERS = 5000
_RESERVED = frozenset({"start", "close"})


class _KvFailure(Exception):
    """Internal-only: a `kv` host-call or stored-state failure. Always caught in `transform`."""


async def _kv_get(key: str) -> bytes | None:
    """`kv.get`, reclassifying the generated WIT `Err` into `_KvFailure` (fail-loud)."""
    try:
        result = await kv.get(key)
    except Exception as exc:
        raise _KvFailure(f"kv.get failed: {getattr(exc, 'value', exc)}") from exc
    return cast("bytes | None", result)


async def _kv_set(key: str, value: bytes) -> None:
    """`kv.set` (no TTL), reclassifying `Err` into `_KvFailure`."""
    try:
        await kv.set(key, value, ttl_seconds=0)
    except Exception as exc:
        raise _KvFailure(f"kv.set failed: {getattr(exc, 'value', exc)}") from exc


def _voter_id(actor: object) -> str | None:
    """Return a truncated SHA-256 of the actor id, or None if the event carries no actor."""
    if not isinstance(actor, str) or not actor:
        return None
    return hashlib.sha256(actor.encode("utf-8")).hexdigest()[:16]


def _empty_state() -> dict[str, Any]:
    """Return a closed, empty tally document."""
    return {"open": False, "tally": {}, "voters": {}}


async def _load() -> dict[str, Any]:
    """Load the tally document (empty/closed if never written); fail loud on corruption."""
    raw = await _kv_get(STATE_KEY)
    if raw is None:
        return _empty_state()
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _KvFailure("corrupt vote state") from exc
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("open"), bool)
        or not isinstance(state.get("tally"), dict)
        or not isinstance(state.get("voters"), dict)
    ):
        raise _KvFailure("malformed vote state")
    return cast("dict[str, Any]", state)


async def _save(state: dict[str, Any]) -> None:
    """Persist the tally document."""
    await _kv_set(STATE_KEY, json.dumps(state, separators=(",", ":")).encode("utf-8"))


def _is_privileged(event: PlatformEvent) -> bool:
    """Broadcaster/moderator check -- fails CLOSED when role info isn't on the event."""
    is_mod = event.payload.get("is_mod")
    is_broadcaster = event.payload.get("is_broadcaster")
    if not isinstance(is_mod, bool) and not isinstance(is_broadcaster, bool):
        log.debug("vote.role_info_unavailable", platform=event.platform)
        return False
    return is_mod is True or is_broadcaster is True


def _standings(state: dict[str, Any]) -> str:
    """Render the tally, highest first (ties alphabetical)."""
    tally: dict[str, int] = state["tally"]
    if not tally:
        return "no votes yet"
    ranked = sorted(tally.items(), key=lambda kv_: (-kv_[1], kv_[0]))
    return ", ".join(f"{name}: {count}" for name, count in ranked)


async def _handle(args: list[str], event: PlatformEvent) -> str:
    """Run one `!vote` invocation and return the reply text."""
    op = args[0].lower() if args else ""
    state = await _load()

    if op == "":
        status = "OPEN" if state["open"] else "closed"
        log.debug("vote.read", op="read", count=len(state["voters"]))
        return f"Vote ({status}): {_standings(state)}"

    if op in _RESERVED:
        if not _is_privileged(event):
            log.info("vote.permission_denied", op=op)
            return "Only the broadcaster or a moderator can start or close a vote."
        if op == "start":
            fresh = _empty_state()
            fresh["open"] = True
            await _save(fresh)
            log.info("vote.started", op="start")
            return "Vote started! Cast yours with !vote <option>."
        if not state["open"]:
            return "No vote is open."
        state["open"] = False
        await _save(state)
        log.info("vote.closed", op="close", count=len(state["voters"]))
        return f"Vote closed. Final: {_standings(state)}"

    if not state["open"]:
        return "No vote is open. A moderator can start one with !vote start."
    if len(args) > 1 or len(op) > MAX_OPTION_LEN:
        return f"Usage: !vote <option> (one word, up to {MAX_OPTION_LEN} characters)"
    voter = _voter_id(event.actor)
    if voter is None:
        log.info("vote.no_actor", op="cast")
        return "Could not identify you, so your vote was not counted."

    tally: dict[str, int] = state["tally"]
    voters: dict[str, str] = state["voters"]
    previous = voters.get(voter)
    if previous == op:
        return f"You already voted for {op}."
    if op not in tally and len(tally) >= MAX_OPTIONS:
        return f"Too many options (max {MAX_OPTIONS}); pick an existing one."
    if previous is None and len(voters) >= MAX_VOTERS:
        log.warn("vote.voter_cap", op="cast", count=len(voters))
        return "This vote is full."
    if previous is not None:
        tally[previous] -= 1
        if tally[previous] <= 0:
            del tally[previous]
    tally[op] = tally.get(op, 0) + 1
    voters[voter] = op
    await _save(state)
    log.info("vote.cast", op="change" if previous else "cast", count=len(voters))
    return f"Vote counted for {op}. Standings: {_standings(state)}"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!vote` and reply with tally state.

    Returns `None` for non-chat payloads, non-`!vote` text, or while the flag is off.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    parts = text.strip().split()
    if not parts or parts[0].lower() != COMMAND:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    try:
        reply = await _handle(parts[1:], event)
    except _KvFailure as exc:
        log.error("vote.kv_failure", error_type=type(exc.__cause__ or exc).__name__)
        reply = "Something went wrong with the vote storage - please try again."

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
        raise ValueError(
            "vote reply requires a channel_id from the inbound chat.message"
        )
    if not isinstance(text, str) or not text:
        raise ValueError("vote reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("vote.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
