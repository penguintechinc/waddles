"""`!guess` -> a per-community number-guessing game, kv-only.

v1 scope (2026-10-09, feature/bundles-wordgames-pack):

- `!guess` / `!guess start` picks a secret integer in
  [`RANGE_MIN`, `RANGE_MAX`] and stores the community's one active round
  (secret, narrowed bounds, attempt count) in `kv`. If a round is already
  active, bare/`start` does NOT overwrite it -- it re-shows the current
  bounds.
- `!guess <n>` submits a guess: higher/lower feedback narrows the shown
  bounds; the first exact match wins the round and clears state. Non-integer
  or out-of-range input replies with usage and mutates nothing.
- `!guess giveup` (alias `reset`) ends the round and reveals the number.

Verb vocabulary follows the core verbs where they apply (bare = start-or-
status, `start`, `giveup`/`reset`); any integer token is a guess. Classified
by hand rather than `parse_command()` (see `bundles/python/scramble`).

`transform` only classifies and forwards; `dispatch` performs every `kv`
access and the relay reply, mirroring `bundles/python/hangman`.

Data scoping: all state is community-scoped via `waddle_sdk.community_kv`
(one active round per community). No player identity is stored or logged;
logs carry only community id, op, and counts -- never the guessed values.

Gated behind the PostHog flag ``waddles.command-guess``, default OFF.
"""

from __future__ import annotations

import json
import random
from typing import Any, NoReturn

from waddle_sdk import community_kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-guess"
COMMAND = "guess"

RANGE_MIN = 1
RANGE_MAX = 100

#: An abandoned round auto-expires after 1 hour (under the host's 30-day KV cap).
_ACTIVE_TTL_SECONDS = 60 * 60
_ACTIVE_KEY = "guess.active"

_USAGE = f"Usage: !guess | !guess <number {RANGE_MIN}-{RANGE_MAX}> | !guess giveup"
_NO_ACTIVE_GAME = "No guessing round is active right now -- start one with !guess!"

_KNOWN_ACTIONS = frozenset({"start", "guess", "giveup", "usage"})
_GIVEUP_VERBS = frozenset({"giveup", "reset"})


def _classify(stripped: str) -> tuple[str, str | None]:
    """Map a full `!guess ...` message to `(action, arg)` -- never raises."""
    rest = stripped.partition(" ")[2].strip()
    if not rest:
        return "start", None
    lowered = rest.lower()
    if lowered == "start":
        return "start", None
    if lowered in _GIVEUP_VERBS:
        return "giveup", None
    if " " not in rest and len(rest) <= 12:
        return "guess", rest
    return "usage", _USAGE


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!guess` and its verbs.

    No `kv` access. Returns `None` for non-chat payloads, non-`!guess` text,
    and while `waddles.command-guess` is disabled.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head = stripped.split(maxsplit=1)[0].lower() if stripped else ""
    if head != f"!{COMMAND}":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    action, arg = _classify(stripped)
    log.info("guess.transform matched", action=action)
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
    """`waddle_transports.TransportResult`-shaped result -- see `first`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def _fail_kv(exc: Exception, *, provider: str, channel_id: str, op: str) -> NoReturn:
    """Fail-loud kv error path: log, reply an error to chat, then re-raise."""
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("guess.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "guess is temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"guess kv {op} failed: {case_name}") from exc


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


def _is_int(value: Any) -> bool:
    """True for a real int (bool excluded)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _load_active(raw: bytes | None, *, community: str) -> dict[str, Any] | None:
    """Decode the active-round JSON, self-healing to `None` on corruption."""
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.error("guess.state_corrupt", context="active", community=community)
        return None
    if not isinstance(data, dict) or not all(
        _is_int(data.get(k)) for k in ("secret", "low", "high", "attempts")
    ):
        log.error("guess.state_corrupt", context="active", community=community)
        return None
    return data


async def _handle_start(*, community: str, provider: str, channel_id: str) -> str:
    """Start a new round unless one is active; never overwrites an in-progress round."""
    raw = await _kv_get(_ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id)
    existing = _load_active(raw, community=community)
    if existing is not None:
        log.info("guess.start_already_active", community=community)
        return (
            f"A guessing round is already active: it's between {existing['low']} and "
            f"{existing['high']} (!guess <n>)"
        )

    secret = random.randint(RANGE_MIN, RANGE_MAX)  # noqa: S311 -- a game pick, not crypto
    active = {"secret": secret, "low": RANGE_MIN, "high": RANGE_MAX, "attempts": 0}
    await _kv_set(
        _ACTIVE_KEY,
        json.dumps(active).encode("utf-8"),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_ACTIVE_TTL_SECONDS,
    )
    log.info("guess.started", community=community)
    return (
        f"\U0001f522 I'm thinking of a number between {RANGE_MIN} and {RANGE_MAX} "
        "-- guess with !guess <n>"
    )


async def _handle_guess(guess_text: str, *, community: str, provider: str, channel_id: str) -> str:
    """Compare one guess to the secret; the first exact match wins and clears the round."""
    raw = await _kv_get(_ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id)
    active = _load_active(raw, community=community)
    if active is None:
        return _NO_ACTIVE_GAME

    try:
        value = int(guess_text.strip())
    except ValueError as exc:
        log.info("guess.invalid_guess", community=community, error=type(exc).__name__)
        return _USAGE
    if not RANGE_MIN <= value <= RANGE_MAX:
        return _USAGE

    attempts: int = active["attempts"] + 1
    secret: int = active["secret"]

    if value == secret:
        await _kv_delete(_ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id)
        log.info("guess.won", community=community, attempts=attempts)
        return f"\U0001f389 Correct! The number was {secret} (found on attempt {attempts})."

    if value < secret:
        active["low"] = max(active["low"], value + 1)
        hint = "higher"
    else:
        active["high"] = min(active["high"], value - 1)
        hint = "lower"
    active["attempts"] = attempts
    await _kv_set(
        _ACTIVE_KEY,
        json.dumps(active).encode("utf-8"),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_ACTIVE_TTL_SECONDS,
    )
    log.info("guess.wrong_guess", community=community, attempts=attempts, hint=hint)
    return f"Go {hint}! It's between {active['low']} and {active['high']}."


async def _handle_giveup(*, community: str, provider: str, channel_id: str) -> str:
    """End the active round, revealing the number."""
    raw = await _kv_get(_ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id)
    active = _load_active(raw, community=community)
    if active is None:
        return _NO_ACTIVE_GAME
    await _kv_delete(_ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id)
    log.info("guess.gave_up", community=community, attempts=active["attempts"])
    return f"Round over -- the number was {active['secret']}. Start a new one with !guess."


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: all `kv` state reads/writes, then relay the reply.

    Raises:
        ValueError: No `channel_id`, no `community` (no tenant-wide fallback), or an
            unrecognized `action` (defensive -- `transform` only emits `_KNOWN_ACTIONS`).
        RuntimeError: A `kv` backend call failed (a chat error reply and ERROR log come first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("guess reply requires a channel_id from the inbound chat.message")
    action = payload.get("action")
    if action not in _KNOWN_ACTIONS:
        raise ValueError(f"unrecognized guess action: {action!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("guess.missing_community", action=action)
        raise ValueError("guess requires a community context and cannot operate tenant-wide")

    arg = payload.get("arg")
    if action == "usage":
        reply_text = arg if isinstance(arg, str) and arg else _USAGE
    elif action == "start":
        reply_text = await _handle_start(
            community=community, provider=provider, channel_id=channel_id
        )
    elif action == "guess":
        reply_text = await _handle_guess(
            arg if isinstance(arg, str) else "",
            community=community,
            provider=provider,
            channel_id=channel_id,
        )
    else:  # giveup
        reply_text = await _handle_giveup(
            community=community, provider=provider, channel_id=channel_id
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("guess.dispatch relayed", platform=provider, action=action)
    return DispatchResult(transport=provider, detail=action)
