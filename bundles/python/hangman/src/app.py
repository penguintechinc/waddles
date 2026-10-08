"""`!hangman` -> a per-community hangman word-guessing game, kv-only.

v1 scope (2026-10-07, feature/bundle-hangman):

- `!hangman start` picks a word from an embedded list (`WORD_BANK`, via
  `random.choice`) and stores the community's one active game (the word,
  guessed letters, and wrong-guess count) in `kv`. If a game is already
  active, `start` does NOT overwrite it -- it re-shows the current masked
  state instead, so a careless double `start` can never silently discard
  an in-progress game.
- `!hangman guess <letter>` checks a single letter against the active
  word. A letter already guessed is a no-op reply (never double-penalized
  or double-credited). A correct letter reveals it everywhere it appears;
  once every letter is revealed the game ends in a win. An incorrect
  letter costs one life (`_MAX_WRONG_GUESSES`); running out ends the game
  in a loss, revealing the word. `guess` with no/invalid input (not
  exactly one alphabetic character), or when no game is active, both
  reply without mutating state.
- `!hangman reveal` (bare `!hangman` is equivalent) shows the current
  masked word, guessed letters, and remaining lives -- a pure read, never
  ends the game itself (only `guess` can).

Command grammar: parsed via `waddle_sdk.command.parse_command()` against a
declared `CommandSpec` (`sdk/waddle-sdk/AUTHORING.md` Sec1), exactly like
`bundles/python/first`. `start`/`reveal` are modeled as the command's own
declared sub-modules (bare, no further args). `guess <letter>` takes a
free-text tail the shared grammar's sub-module shape can't express -- so,
mirroring `bundles/python/rps`'s own documented fallback, this module
treats a `parse_command()` grammar-shape failure as a possible
`guess <letter>` invocation rather than an immediate error: `guess` is
this bundle's own vocabulary, not the shared grammar's.

Business logic split: `transform` only recognizes the command/verb shape
and forwards the normalized action signal (no `kv` access) -- `dispatch`
performs every `kv` read/write and the relay reply, mirroring `first`'s own
process/action-stage split.

Data scoping: every kv entry here is community-scoped via
`waddle_sdk.community_kv` (never global/tenant-wide) -- `dispatch` checks
`envelope.community` up front for a clear, command-specific error message,
mirroring `first`'s own "Data scoping" section. No player PII is ever
stored in this bundle's state: the active-game state tracks only the word,
guessed letters, and wrong-guess count, none of which are tied to any one
player's identity.

Gated behind the PostHog flag ``waddles.command-hangman``, default OFF --
see `bundles/python/first/src/app.py`'s own docstring for the flag-gate
rationale and ordering.
"""

from __future__ import annotations

import json
import random
from typing import Any, NoReturn

from waddle_sdk import community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-hangman"

SPEC = CommandSpec(name="hangman", sub_modules=frozenset({"start", "reveal"}))

#: Deliberately small and static: a fixed, auditable embedded word list, no external data
#: source or scheduler. All lowercase, alphabetic-only (matches `_normalize_letter`'s charset).
WORD_BANK: tuple[str, ...] = (
    "python",
    "penguin",
    "waddle",
    "hangman",
    "kubernetes",
    "component",
    "trivia",
    "wasmtime",
)

#: How many wrong guesses end the game in a loss.
_MAX_WRONG_GUESSES = 6

#: The active game auto-expires after 1 hour of inactivity -- storage hygiene AND a
#: reasonable round timeout (an abandoned game shouldn't block a community forever).
#: Comfortably under the host's 30-day `KV_MAX_TTL_S` cap.
_ACTIVE_TTL_SECONDS = 60 * 60

_ACTIVE_KEY = "hangman.active"

_USAGE = "Usage: !hangman start | !hangman guess <letter> | !hangman reveal"
_NO_ACTIVE_GAME = "No hangman game is active right now -- start one with !hangman start!"
_INVALID_LETTER = "Usage: !hangman guess <a single letter, a-z>"

_KNOWN_ACTIONS = frozenset({"start", "guess", "reveal", "usage"})


def _normalize_letter(text: str) -> str | None:
    """Return a single lowercase a-z letter from `text`, or `None` if `text` isn't exactly one."""
    stripped = text.strip().lower()
    if len(stripped) == 1 and stripped.isalpha() and stripped.isascii():
        return stripped
    return None


def _mask(word: str, guessed: list[str]) -> str:
    """Render `word` with every letter not in `guessed` replaced by `_`, space-separated."""
    return " ".join(letter if letter in guessed else "_" for letter in word)


def _classify(stripped: str) -> tuple[str, str | None]:
    """Map a full `!hangman ...` message to `(action, arg)` -- never raises.

    Tries the shared grammar first (`start`/`reveal` as declared
    sub-modules); on a grammar-shape failure, falls back to this bundle's
    own `guess <letter>` vocabulary (mirrors `bundles/python/rps`'s own
    documented fallback for a free-text command argument the shared
    grammar can't express). Anything else -- a grammar-legal but
    meaningless verb (`!hangman list`), an unknown token, or a missing
    letter -- degrades to `"usage"` rather than a crash or a silent drop.
    """
    try:
        parsed = parse_command(stripped, SPEC)
    except CommandUsageError:
        parsed = None

    if parsed is not None:
        if parsed.option is not None:
            return "usage", _USAGE
        if parsed.sub_module in ("start", "reveal"):
            return parsed.sub_module, None
        # Bare `!hangman` (no sub-module, no option) -- the command's own default read.
        return "reveal", None

    rest = stripped.partition(" ")[2].strip()
    tok1, _, tail = rest.partition(" ")
    if tok1.lower() == "guess":
        letter_text = tail.strip()
        if not letter_text:
            return "usage", _USAGE
        return "guess", letter_text
    return "usage", _USAGE


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!hangman` and its verbs.

    No `kv` access here -- only command recognition and forwarding the
    normalized action signal. Returns `None` for any non-chat payload, text
    that isn't `!hangman`-shaped (cheap-skip, zero `kv` cost), and while
    `waddles.command-hangman` is disabled.
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

    action, arg = _classify(stripped)

    log.info("hangman.transform matched", action=action)
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
    """Fail-loud kv error path: log, reply an error to chat, then re-raise.

    Mirrors `first`'s own `_fail_kv` -- `community_kv`'s underlying
    `kv.get/set/delete/increment` raise the generated WIT `Err` (`.value`
    holds `Error_TooLarge`/`Error_Backend`) on a real backend failure.
    Never silent: the caller always sees a chat reply AND the pipeline
    still sees a real failure (the re-raise).
    """
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("hangman.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "hangman is temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"hangman kv {op} failed: {case_name}") from exc


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
    """Decode the active-game JSON, self-healing to `None` on corruption.

    Corrupt active-game state degrades to "no active game" rather than
    crashing `dispatch` -- mirrors `first`'s own corruption stance for its
    winner key.
    """
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.error("hangman.state_corrupt", context="active", community=community)
        return None
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("word"), str)
        or not isinstance(data.get("guessed"), list)
        or not all(isinstance(letter, str) for letter in data.get("guessed", []))
        or not isinstance(data.get("wrong"), int)
    ):
        log.error("hangman.state_corrupt", context="active", community=community)
        return None
    return data


async def _handle_start(*, community: str, provider: str, channel_id: str) -> str:
    """Pick a new word unless a game is already active; never overwrites an in-progress game."""
    existing_raw = await _kv_get(
        _ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id
    )
    existing = _load_active(existing_raw, community=community)
    if existing is not None:
        log.info("hangman.start_already_active", community=community)
        mask = _mask(existing["word"], existing["guessed"])
        return f"A hangman game is already active: {mask} (!hangman guess <letter>)"

    word = random.choice(WORD_BANK)  # noqa: S311 -- a game pick, not crypto
    active = {"word": word, "guessed": [], "wrong": 0}
    await _kv_set(
        _ACTIVE_KEY,
        json.dumps(active).encode("utf-8"),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_ACTIVE_TTL_SECONDS,
    )
    log.info("hangman.started", community=community, length=len(word))
    mask = _mask(word, [])
    return (
        f"\U0001f480 Hangman started! {mask} ({len(word)} letters, {_MAX_WRONG_GUESSES} lives) "
        "-- guess with !hangman guess <letter>"
    )


async def _handle_guess(letter_text: str, *, community: str, provider: str, channel_id: str) -> str:
    """Check one letter against the active word; may end the game in a win or loss."""
    raw = await _kv_get(_ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id)
    active = _load_active(raw, community=community)
    if active is None:
        return _NO_ACTIVE_GAME

    letter = _normalize_letter(letter_text)
    if letter is None:
        return _INVALID_LETTER

    word: str = active["word"]
    guessed: list[str] = active["guessed"]
    wrong: int = active["wrong"]

    if letter in guessed:
        log.info("hangman.guess_repeat", community=community)
        return f"You already guessed {letter!r} -- {_mask(word, guessed)}"

    guessed = [*guessed, letter]

    if letter in word:
        if all(ch in guessed for ch in word):
            await _kv_delete(
                _ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id
            )
            log.info("hangman.won", community=community)
            return f"\U0001f389 You win! The word was {word}."
        await _kv_set(
            _ACTIVE_KEY,
            json.dumps({"word": word, "guessed": guessed, "wrong": wrong}).encode("utf-8"),
            community=community,
            provider=provider,
            channel_id=channel_id,
            ttl_seconds=_ACTIVE_TTL_SECONDS,
        )
        lives_left = _MAX_WRONG_GUESSES - wrong
        return f"Nice! {_mask(word, guessed)} ({lives_left} lives left)"

    wrong += 1
    if wrong >= _MAX_WRONG_GUESSES:
        await _kv_delete(_ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id)
        log.info("hangman.lost", community=community)
        return f"\U0001f480 Out of lives! The word was {word}."
    await _kv_set(
        _ACTIVE_KEY,
        json.dumps({"word": word, "guessed": guessed, "wrong": wrong}).encode("utf-8"),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_ACTIVE_TTL_SECONDS,
    )
    lives_left = _MAX_WRONG_GUESSES - wrong
    return f"Nope, no {letter!r}. {_mask(word, guessed)} ({lives_left} lives left)"


async def _handle_reveal(*, community: str, provider: str, channel_id: str) -> str:
    """Show the current masked word, guessed letters, and remaining lives -- never mutates."""
    raw = await _kv_get(_ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id)
    active = _load_active(raw, community=community)
    if active is None:
        return _NO_ACTIVE_GAME

    word: str = active["word"]
    guessed: list[str] = active["guessed"]
    wrong: int = active["wrong"]
    lives_left = _MAX_WRONG_GUESSES - wrong
    guessed_display = ", ".join(sorted(guessed)) if guessed else "none yet"
    return f"{_mask(word, guessed)} -- guessed: {guessed_display} ({lives_left} lives left)"


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: all `kv` state reads/writes, then relay the reply.

    Raises:
        ValueError: The envelope's payload has no `channel_id`; the
            envelope has no `community` (there is no tenant-wide fallback
            -- see module docstring's Data scoping section); or an
            unrecognized `action` (defensive -- `transform` only ever
            emits a member of `_KNOWN_ACTIONS`).
        RuntimeError: A `kv` backend call failed (see `_fail_kv` -- a chat
            error reply and an ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("hangman reply requires a channel_id from the inbound chat.message")
    action = payload.get("action")
    if action not in _KNOWN_ACTIONS:
        raise ValueError(f"unrecognized hangman action: {action!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("hangman.missing_community", action=action)
        raise ValueError("hangman requires a community context and cannot operate tenant-wide")

    if action == "usage":
        text = payload.get("arg")
        reply_text = text if isinstance(text, str) and text else _USAGE
    elif action == "start":
        reply_text = await _handle_start(
            community=community, provider=provider, channel_id=channel_id
        )
    elif action == "guess":
        letter_text = payload.get("arg")
        reply_text = await _handle_guess(
            str(letter_text) if isinstance(letter_text, str) else "",
            community=community,
            provider=provider,
            channel_id=channel_id,
        )
    else:  # reveal
        reply_text = await _handle_reveal(
            community=community, provider=provider, channel_id=channel_id
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("hangman.dispatch relayed", platform=provider, action=action)
    return DispatchResult(transport=provider, detail=action)
