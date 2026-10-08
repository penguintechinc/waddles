"""`!trivia` -> a per-community trivia Q&A game, kv-only.

v1 scope (2026-10-07, feature/bundle-trivia):

- `!trivia start` poses one question from an embedded Q&A bank
  (`QUESTION_BANK`, selected via `random.choice`) and stores it as the
  community's one active question in `kv`. If a question is already
  active for the community, `start` does NOT overwrite it -- it re-shows
  the existing question instead, so a careless double `start` can never
  silently discard an in-progress round.
- `!trivia answer <text>` checks `<text>` against the active question's
  answer (normalized: lowercased, whitespace-collapsed). The first correct
  answer wins: the round ends (the active question is cleared), the
  answerer's per-community score is incremented, and they're registered in
  the community's score registry. An incorrect answer replies "not quite"
  and leaves the round running for the next guess. `!trivia answer` with no
  text, or when no question is active, both reply without mutating state.
- `!trivia score` (bare `!trivia` is equivalent) reports the caller's own
  score plus the community's top scorers, both pure reads.

Command grammar: parsed via `waddle_sdk.command.parse_command()` against a
declared `CommandSpec` (`sdk/waddle-sdk/AUTHORING.md` Sec1), exactly like
`bundles/python/first`. `start`/`score` are modeled as the command's own
declared sub-modules (bare, no further args, same shape as `first`'s own
`leaderboard`). `answer <text>` takes a free-text tail the shared grammar's
sub-module shape can't express (it only allows a trailing *verb* from the
shared vocabulary, never arbitrary text) -- so, mirroring
`bundles/python/rps`'s own documented fallback, this module treats a
`parse_command()` grammar-shape failure as a possible `answer <text>`
invocation rather than an immediate error: `answer` is this bundle's own
vocabulary, not the shared grammar's.

Business logic split: `transform` only recognizes the command/verb shape
and forwards the normalized action signal (no `kv` access) -- `dispatch`
performs every `kv` read/write and the relay reply, mirroring `first`'s own
process/action-stage split.

PII -- pseudonymized throughout, never a raw identifier in state, a reply
to anyone other than the caller themselves, or a log line: `_pseudonym()`
SHA-256-hashes `event.actor` before it ever reaches `community_kv`, exactly
like `first`'s own `_pseudonym()`. The win-claiming reply addresses the
caller directly with their own raw name (safe -- it's the normal, visible
chat response to the same user who triggered it, never stored or logged);
the `score` command's "top scorers" surface only ever shows a short,
non-reversible `_display_handle()` for anyone other than the caller.

Data scoping: every kv entry here is community-scoped via
`waddle_sdk.community_kv` (never global/tenant-wide) -- `dispatch` checks
`envelope.community` up front for a clear, command-specific error message,
mirroring `first`'s own "Data scoping" section.

Gated behind the PostHog flag ``waddles.command-trivia``, default OFF --
see `bundles/python/first/src/app.py`'s own docstring for the flag-gate
rationale and ordering.
"""

from __future__ import annotations

import hashlib
import json
import random
from typing import Any, NoReturn

from waddle_sdk import community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-trivia"

SPEC = CommandSpec(name="trivia", sub_modules=frozenset({"start", "score"}))

#: One question/answer pair per entry -- `answer` is the canonical (not yet
#: normalized) text a correct guess must match after `_normalize_answer()`.
#: Deliberately small and static: a fixed, auditable embedded bank, no
#: external data source or scheduler.
QUESTION_BANK: tuple[tuple[str, str], ...] = (
    ("What is the capital of France?", "Paris"),
    ("How many continents are there on Earth?", "7"),
    ("What is the chemical symbol for gold?", "Au"),
    ("What planet is known as the Red Planet?", "Mars"),
    ("How many legs does a spider have?", "8"),
    ("What is the largest ocean on Earth?", "Pacific"),
    ("Who wrote Romeo and Juliet?", "Shakespeare"),
    ("What gas do plants absorb from the atmosphere for photosynthesis?", "carbon dioxide"),
)

#: The active question auto-expires after 1 hour of inactivity -- storage hygiene AND a
#: reasonable round timeout (an abandoned round shouldn't block a community forever).
#: Comfortably under the host's 30-day `KV_MAX_TTL_S` cap.
_ACTIVE_TTL_SECONDS = 60 * 60

#: Per-community per-user scores and the score registry persist indefinitely -- durable
#: history, not session/window state.
_PERSISTENT_TTL_SECONDS = 0

_ACTIVE_KEY = "trivia.active"
_SCORE_REGISTRY_KEY = "trivia.score.registry"
_SCORE_TOP_N = 5

_USAGE = "Usage: !trivia start | !trivia answer <text> | !trivia score"
_NO_ACTIVE_QUESTION = "No trivia question is active right now -- start one with !trivia start!"

_KNOWN_ACTIONS = frozenset({"start", "answer", "score", "usage"})


def _score_key(pseudonym: str) -> str:
    """The `kv` key holding one pseudonym's all-time trivia score."""
    return f"trivia.score.{pseudonym}"


def _pseudonym(actor: str | None) -> str:
    """SHA-256 pseudonym for `actor` -- mirrors `first`'s own `_pseudonym` hashing convention."""
    return hashlib.sha256((actor or "anonymous").encode()).hexdigest()


def _display_handle(pseudonym: str) -> str:
    """Short, non-reversible display form of a pseudonym -- never the full hash, never PII."""
    return f"user-{pseudonym[:8]}"


def _normalize_answer(text: str) -> str:
    """Lowercase + collapse whitespace, for forgiving answer comparison."""
    return " ".join(text.split()).lower()


def _classify(stripped: str) -> tuple[str, str | None]:
    """Map a full `!trivia ...` message to `(action, arg)` -- never raises.

    Tries the shared grammar first (`start`/`score` as declared
    sub-modules); on a grammar-shape failure, falls back to this bundle's
    own `answer <text>` vocabulary (mirrors `bundles/python/rps`'s own
    documented fallback for a free-text command argument the shared
    grammar can't express). Anything else -- a grammar-legal but
    meaningless verb (`!trivia list`), an unknown token, or an empty
    `answer` -- degrades to `"usage"` rather than a crash or a silent drop.
    """
    try:
        parsed = parse_command(stripped, SPEC)
    except CommandUsageError:
        parsed = None

    if parsed is not None:
        if parsed.option is not None:
            return "usage", _USAGE
        if parsed.sub_module in ("start", "score"):
            return parsed.sub_module, None
        # Bare `!trivia` (no sub-module, no option) -- the command's own default read.
        return "score", None

    rest = stripped.partition(" ")[2].strip()
    tok1, _, tail = rest.partition(" ")
    if tok1.lower() == "answer":
        answer_text = tail.strip()
        if not answer_text:
            return "usage", _USAGE
        return "answer", answer_text
    return "usage", _USAGE


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!trivia` and its verbs.

    No `kv` access here -- only command recognition and forwarding the
    normalized action signal. Returns `None` for any non-chat payload, text
    that isn't `!trivia`-shaped (cheap-skip, zero `kv` cost), and while
    `waddles.command-trivia` is disabled.
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

    log.info("trivia.transform matched", action=action)
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
    log.error("trivia.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "trivia is temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"trivia kv {op} failed: {case_name}") from exc


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


async def _kv_increment(
    key: str, *, community: str, provider: str, channel_id: str, ttl_seconds: int
) -> int:
    """Community-scoped `kv.increment` by 1, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        return await community_kv.increment(community, key, 1, ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="increment")


def _parse_int(raw: bytes | None, *, context: str, community: str) -> int:
    """Decode an integer `kv` value, self-healing (logging + treating as `0`) on corruption.

    Mirrors `first`'s own `_parse_int` self-heal convention -- never
    expected in normal operation (every writer here only ever stores
    `kv.increment`'s own return value).
    """
    if raw is None:
        return 0
    try:
        return int(raw.decode())
    except (UnicodeDecodeError, ValueError):
        log.error("trivia.state_corrupt", context=context, community=community)
        return 0


def _load_active(raw: bytes | None, *, community: str) -> dict[str, str] | None:
    """Decode the active-question JSON, self-healing to `None` on corruption.

    Corrupt active-question state degrades to "no active question" rather
    than crashing `dispatch` -- mirrors `first`'s own corruption stance for
    its winner key.
    """
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.error("trivia.state_corrupt", context="active", community=community)
        return None
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("question"), str)
        or not isinstance(data.get("answer_raw"), str)
        or not isinstance(data.get("answer_norm"), str)
    ):
        log.error("trivia.state_corrupt", context="active", community=community)
        return None
    return data


async def _load_score_registry(*, community: str, provider: str, channel_id: str) -> list[str]:
    """Return every pseudonym that has ever scored in this community, or `[]`.

    Mirrors `first`'s own `_load_leaderboard_registry` -- corrupt registry
    content self-heals to `[]` and logs.
    """
    raw = await _kv_get(
        _SCORE_REGISTRY_KEY, community=community, provider=provider, channel_id=channel_id
    )
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.error("trivia.state_corrupt", context="score_registry", community=community)
        return []
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        log.error("trivia.state_corrupt", context="score_registry", community=community)
        return []
    return data


async def _register_scorer(
    pseudonym: str, *, community: str, provider: str, channel_id: str
) -> None:
    """Add `pseudonym` to the community's score registry, if not already present."""
    registry = await _load_score_registry(
        community=community, provider=provider, channel_id=channel_id
    )
    if pseudonym in registry:
        return
    registry.append(pseudonym)
    await _kv_set(
        _SCORE_REGISTRY_KEY,
        json.dumps(sorted(registry)).encode("utf-8"),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_PERSISTENT_TTL_SECONDS,
    )


async def _handle_start(*, community: str, provider: str, channel_id: str) -> str:
    """Pose a new question unless one is already active; never overwrites an in-progress round."""
    existing_raw = await _kv_get(
        _ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id
    )
    existing = _load_active(existing_raw, community=community)
    if existing is not None:
        log.info("trivia.start_already_active", community=community)
        return f"A trivia question is already active: {existing['question']} (!trivia answer ...)"

    question, answer_raw = random.choice(QUESTION_BANK)  # noqa: S311 -- a game pick, not crypto
    active = {
        "question": question,
        "answer_raw": answer_raw,
        "answer_norm": _normalize_answer(answer_raw),
    }
    await _kv_set(
        _ACTIVE_KEY,
        json.dumps(active).encode("utf-8"),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_ACTIVE_TTL_SECONDS,
    )
    log.info("trivia.started", community=community)
    return f"\U0001f4dd Trivia time! {question} (answer with !trivia answer <your answer>)"


async def _handle_answer(
    text: str, *, community: str, actor: str | None, username: str, provider: str, channel_id: str
) -> str:
    """Check `text` against the active question; first correct guess wins and ends the round."""
    raw = await _kv_get(_ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id)
    active = _load_active(raw, community=community)
    if active is None:
        return _NO_ACTIVE_QUESTION

    if _normalize_answer(text) != active["answer_norm"]:
        log.info("trivia.answer_incorrect", community=community)
        return "Not quite -- try again!"

    pseudonym = _pseudonym(actor)
    await _kv_delete(_ACTIVE_KEY, community=community, provider=provider, channel_id=channel_id)
    new_score = await _kv_increment(
        _score_key(pseudonym),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_PERSISTENT_TTL_SECONDS,
    )
    await _register_scorer(pseudonym, community=community, provider=provider, channel_id=channel_id)
    log.info("trivia.answer_correct", community=community, score=new_score)
    return (
        f"\U0001f389 Correct, {username}! The answer was {active['answer_raw']}. "
        f"Your score: {new_score}"
    )


async def _handle_score(
    *, community: str, actor: str | None, username: str, provider: str, channel_id: str
) -> str:
    """Report the caller's own score plus the community's top scorers -- never mutates state."""
    pseudonym = _pseudonym(actor)
    own_raw = await _kv_get(
        _score_key(pseudonym), community=community, provider=provider, channel_id=channel_id
    )
    own_score = _parse_int(own_raw, context="own_score", community=community)

    registry = await _load_score_registry(
        community=community, provider=provider, channel_id=channel_id
    )
    scored: list[tuple[int, str]] = []
    for other_pseudonym in registry:
        other_raw = await _kv_get(
            _score_key(other_pseudonym),
            community=community,
            provider=provider,
            channel_id=channel_id,
        )
        other_score = _parse_int(other_raw, context="other_score", community=community)
        if other_score > 0:
            scored.append((other_score, other_pseudonym))

    if not scored:
        return f"{username}, your score: {own_score}. No one has scored yet -- try !trivia start!"

    scored.sort(key=lambda item: (-item[0], item[1]))
    top = scored[:_SCORE_TOP_N]
    entries = ", ".join(f"{_display_handle(p)} ({s})" for s, p in top)
    return f"{username}, your score: {own_score}. Top scorers: {entries}"


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
        raise ValueError("trivia reply requires a channel_id from the inbound chat.message")
    action = payload.get("action")
    if action not in _KNOWN_ACTIONS:
        raise ValueError(f"unrecognized trivia action: {action!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("trivia.missing_community", action=action)
        raise ValueError("trivia requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"

    if action == "usage":
        text = payload.get("arg")
        reply_text = text if isinstance(text, str) and text else _USAGE
    elif action == "start":
        reply_text = await _handle_start(
            community=community, provider=provider, channel_id=channel_id
        )
    elif action == "answer":
        answer_text = payload.get("arg")
        reply_text = await _handle_answer(
            str(answer_text) if isinstance(answer_text, str) else "",
            community=community,
            actor=envelope.event.actor,
            username=username,
            provider=provider,
            channel_id=channel_id,
        )
    else:  # score
        reply_text = await _handle_score(
            community=community,
            actor=envelope.event.actor,
            username=username,
            provider=provider,
            channel_id=channel_id,
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("trivia.dispatch relayed", platform=provider, action=action)
    return DispatchResult(transport=provider, detail=action)
