"""`!scramble` -> a per-community word-unscramble game, kv-only.

v1 scope (2026-10-09, feature/bundles-wordgames-pack):

- `!scramble` / `!scramble start` picks a word from an embedded list
  (`WORD_BANK`), scrambles it (never equal to the word), and stores the
  community's one active round (word, scrambled form, attempt count) in
  `kv`. If a round is already active, bare/`start` does NOT overwrite it --
  it re-shows the current scrambled word, so a careless double `start`
  can never silently discard an in-progress round.
- `!scramble <word>` is a guess. The first correct guess wins the round
  and clears state; a wrong guess increments the attempt count only.
- `!scramble giveup` (alias `reset`) ends the round and reveals the word.

Verb vocabulary follows the core verbs where they apply (bare = start-or-
status, `start`, `giveup`/`reset`); every other token is a guess. This
module classifies by hand rather than via `parse_command()` because the
shared grammar would swallow a guess that happens to be a grammar verb
(e.g. a word equal to `set`/`list`) as a command.

`transform` only classifies and forwards; `dispatch` performs every `kv`
access and the relay reply, mirroring `bundles/python/hangman`.

Data scoping: all state is community-scoped via `waddle_sdk.community_kv`
(one active round per community). No player identity is stored or logged;
logs carry only community id, op, and counts -- never guess text.

Gated behind the PostHog flag ``waddles.command-scramble``, default OFF.
"""

from __future__ import annotations

import json
import random
from typing import Any, NoReturn

from waddle_sdk import community_kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-scramble"
COMMAND = "scramble"

#: Fixed, auditable embedded list: lowercase alphabetic words with >=2 distinct letters.
WORD_BANK: tuple[str, ...] = (
    "penguin",
    "waddle",
    "python",
    "kubernetes",
    "component",
    "iceberg",
    "twitch",
    "discord",
    "community",
    "bundle",
    "sandbox",
    "gateway",
)

#: An abandoned round auto-expires after 1 hour (under the host's 30-day KV cap).
_ACTIVE_TTL_SECONDS = 60 * 60
_ACTIVE_KEY = "scramble.active"
_MAX_GUESS_LEN = 64

_USAGE = "Usage: !scramble | !scramble <word> | !scramble giveup"
_NO_ACTIVE_GAME = "No scramble round is active right now -- start one with !scramble!"

_KNOWN_ACTIONS = frozenset({"start", "guess", "giveup", "usage"})
_GIVEUP_VERBS = frozenset({"giveup", "reset"})


def _scramble_word(word: str) -> str:
    """Return a shuffle of `word` that differs from it (bounded retries, then a rotation)."""
    letters = list(word)
    for _ in range(20):
        shuffled = "".join(random.sample(letters, len(letters)))
        if shuffled != word:
            return shuffled
    return word[1:] + word[0]


def _classify(stripped: str) -> tuple[str, str | None]:
    """Map a full `!scramble ...` message to `(action, arg)` -- never raises."""
    rest = stripped.partition(" ")[2].strip()
    if not rest:
        return "start", None
    lowered = rest.lower()
    if lowered == "start":
        return "start", None
    if lowered in _GIVEUP_VERBS:
        return "giveup", None
    if len(rest) > _MAX_GUESS_LEN or " " in rest:
        return "usage", _USAGE
    return "guess", rest


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!scramble` and its verbs.

    No `kv` access. Returns `None` for non-chat payloads, non-`!scramble` text,
    and while `waddles.command-scramble` is disabled.
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
    log.info("scramble.transform matched", action=action)
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
    log.error("scramble.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "scramble is temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"scramble kv {op} failed: {case_name}") from exc


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


def _load_active(raw: bytes | None, *, community: str) -> dict[str, Any] | None:
    """Decode the active-round JSON, self-healing to `None` on corruption."""
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.error("scramble.state_corrupt", context="active", community=community)
        return None
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("word"), str)
        or not isinstance(data.get("scrambled"), str)
        or not isinstance(data.get("attempts"), int)
    ):
        log.error("scramble.state_corrupt", context="active", community=community)
        return None
    return data


async def _handle_start(*, community: str, provider: str, channel_id: str) -> str:
    """Start a new round unless one is active; never overwrites an in-progress round."""
    raw = await _kv_get(_ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id)
    existing = _load_active(raw, community=community)
    if existing is not None:
        log.info("scramble.start_already_active", community=community)
        return f"A scramble is already active: {existing['scrambled']} (!scramble <word>)"

    word = random.choice(WORD_BANK)  # noqa: S311 -- a game pick, not crypto
    scrambled = _scramble_word(word)
    active = {"word": word, "scrambled": scrambled, "attempts": 0}
    await _kv_set(
        _ACTIVE_KEY,
        json.dumps(active).encode("utf-8"),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_ACTIVE_TTL_SECONDS,
    )
    log.info("scramble.started", community=community, length=len(word))
    return f"\U0001f9e9 Unscramble this word: {scrambled} ({len(word)} letters) -- !scramble <word>"


async def _handle_guess(guess: str, *, community: str, provider: str, channel_id: str) -> str:
    """Check a word guess; the first correct guess wins and clears the round."""
    raw = await _kv_get(_ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id)
    active = _load_active(raw, community=community)
    if active is None:
        return _NO_ACTIVE_GAME

    word: str = active["word"]
    attempts: int = active["attempts"] + 1

    if guess.strip().lower() == word:
        await _kv_delete(_ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id)
        log.info("scramble.won", community=community, attempts=attempts)
        return f"\U0001f389 Correct! The word was {word} (solved on attempt {attempts})."

    active["attempts"] = attempts
    await _kv_set(
        _ACTIVE_KEY,
        json.dumps(active).encode("utf-8"),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_ACTIVE_TTL_SECONDS,
    )
    log.info("scramble.wrong_guess", community=community, attempts=attempts)
    return f"Nope, not that one. {active['scrambled']} -- try again!"


async def _handle_giveup(*, community: str, provider: str, channel_id: str) -> str:
    """End the active round, revealing the word."""
    raw = await _kv_get(_ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id)
    active = _load_active(raw, community=community)
    if active is None:
        return _NO_ACTIVE_GAME
    await _kv_delete(_ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id)
    log.info("scramble.gave_up", community=community, attempts=active["attempts"])
    return f"Round over -- the word was {active['word']}. Start a new one with !scramble."


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
        raise ValueError("scramble reply requires a channel_id from the inbound chat.message")
    action = payload.get("action")
    if action not in _KNOWN_ACTIONS:
        raise ValueError(f"unrecognized scramble action: {action!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("scramble.missing_community", action=action)
        raise ValueError("scramble requires a community context and cannot operate tenant-wide")

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
    log.info("scramble.dispatch relayed", platform=provider, action=action)
    return DispatchResult(transport=provider, detail=action)
