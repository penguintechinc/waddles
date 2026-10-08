"""`!first` -> a per-community, per-day "who typed first" claim race, kv-only.

v1 scope (2026-10-05, feature/bundle-first):

- Bare `!first` claims "first" for the current day. The first caller to claim
  in a given UTC day wins: they're recorded as the day's winner and get a
  congratulatory reply. Every subsequent `!first` caller that same day gets a
  "someone already claimed it, you're #N to try" reply instead -- `#N` is the
  community's total attempt count for the day, atomically incremented via
  `kv.increment` on every bare `!first` call (winner's own claim included, so
  the winner is always attempt #1).
- `!first list` reports the day's winner (pseudonymized -- see below) and how
  many have tried, without itself counting as an attempt. `!first leaderboard`
  reports the community's all-time top claimers by total wins, also
  pseudonymized. Neither mutates any state.
- `!first reset`, broadcaster/moderator only: clears the *current* day's
  winner + attempt count early, so the next bare `!first` claims it again.
  Never touches the all-time win counts or leaderboard registry.

Window mechanism -- no scheduler, ever (spec requirement): the UTC calendar
date (`_period_token()`, derived from the always-available `clock` import) is
baked directly into the winner/attempt kv keys (`first.winner.<period>`,
`first.attempts.<period>`). A new day is automatically a fresh, empty window
the moment the stage's own clock ticks past UTC midnight -- nothing resets
it, there is simply no data yet under the new day's keys. `!first reset`
only ever needs to delete the *current* period's two keys early; it does not
and could not reach into a different day. Per-day keys carry a modest TTL
(`_WINDOW_TTL_SECONDS`, 3 days) purely so old days' keys don't accumulate in
the backing store forever -- that TTL is storage hygiene, never the window
boundary itself (the date token in the key already is).

PII -- pseudonymized throughout, never a raw identifier in state, a reply to
anyone other than the caller themselves, or a log line: `_pseudonym()`
SHA-256-hashes `event.actor` (which, pending the tokenization pipeline #429,
may still be a raw username today -- same caveat `lurk`'s own module
docstring documents) before it ever reaches `community_kv`. The win-claiming
reply addresses the caller directly with their own raw name (exactly like
`lurk`'s `$(username)` substitution -- safe because it's the normal, visible
chat response to the same user who triggered it, never stored or logged);
every OTHER reply (the "you're #N" reply to a non-winner, `!first list`,
`!first leaderboard`) only ever surfaces a short, non-reversible
`_display_handle()` derived from the stored pseudonym -- see spec's own
"kv counts, pseudonymized" requirement for `!first leaderboard`, applied
consistently to every surface that names a *different* person than the
caller.

Data scoping: every kv entry here is community-scoped via
`waddle_sdk.community_kv` (never global/tenant-wide) -- this is the first
bundle in the repo to use that helper instead of the older convention of
relying on the host's own `(tenant, community, app_id)` kv scoping
(`count`'s own module docstring) or hand-rolling a `community` segment into
the key text (`lurk`'s `_state_key`). `community_kv` raises `ValueError`
before any host call if `community_id` is falsy -- `dispatch` also checks
`envelope.community` itself up front for a clearer, command-specific error
message, mirroring `lurk`'s own "Data scoping" section.

Command grammar: parsed via `waddle_sdk.command.parse_command()` against a
declared `CommandSpec` (`sdk/waddle-sdk/AUTHORING.md` Sec1) instead of
hand-rolling `text.split()` the way `count`/`lurk` still do -- `leaderboard`
is modeled as the command's one named sub-module (bare `!first leaderboard`,
no further args), `list`/`reset` are the grammar's own `list`/`reset` verbs,
and bare `!first` (no verb, no sub-module) is the command's own default
behavior (the claim-or-report race), exactly the shape the grammar doc
calls out for `!count`.

Business logic split: `transform` only recognizes the command/subcommand
shape and forwards the normalized badge signal (no `kv` access) -- `dispatch`
performs every `kv` read/write and the relay reply, mirroring `lurk`'s own
process/action-stage split (as opposed to `count`'s "transform does
everything" shape, which only applies there because `count`'s own routing
decision itself requires a `kv` read).

Gated behind the PostHog flag ``waddles.command-first``, default OFF -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, NoReturn

from waddle_sdk import clock, community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-first"

SPEC = CommandSpec(name="first", sub_modules=frozenset({"leaderboard"}))

#: Per-day window keys auto-expire after 3 days of inactivity -- storage hygiene only, never
#: the window boundary itself (the UTC date token baked into the key IS the boundary; see
#: `_period_token`/module docstring). Comfortably under the host's 30-day `KV_MAX_TTL_S` cap.
_WINDOW_TTL_SECONDS = 3 * 24 * 60 * 60

#: All-time win counts and the leaderboard registry persist indefinitely -- durable
#: per-community history, not session/window state.
_PERSISTENT_TTL_SECONDS = 0

#: One registry key per community: a JSON array of every pseudonym that has ever won `!first`
#: at least once -- mirrors `count`'s own `REGISTRY_KEY` pattern (no `kv` "list keys"
#: capability exists, so the set of leaderboard-eligible pseudonyms has to be tracked
#: explicitly to enumerate `!first leaderboard`).
_LEADERBOARD_REGISTRY_KEY = "first.leaderboard.registry"

#: How many entries `!first leaderboard` reports, highest wins first.
_LEADERBOARD_TOP_N = 10

_USAGE = "Usage: !first | !first list | !first leaderboard | !first reset (mod/broadcaster only)"
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can reset !first"

_KNOWN_ACTIONS = frozenset({"claim", "list", "leaderboard", "reset", "usage"})


def _winner_key(period: str) -> str:
    """The `kv` key holding the current day's winning pseudonym."""
    return f"first.winner.{period}"


def _attempts_key(period: str) -> str:
    """The `kv` key holding the current day's total attempt count."""
    return f"first.attempts.{period}"


def _wins_key(pseudonym: str) -> str:
    """The `kv` key holding one pseudonym's all-time `!first` win count."""
    return f"first.wins.{pseudonym}"


def _pseudonym(actor: str | None) -> str:
    """SHA-256 pseudonym for `actor` -- mirrors `lurk`'s own `_state_key` hashing convention.

    Never reversible, never logged or replied with in full -- see
    `_display_handle()` for the short, safe-to-display form.
    """
    return hashlib.sha256((actor or "anonymous").encode()).hexdigest()


def _display_handle(pseudonym: str) -> str:
    """Short, non-reversible display form of a pseudonym -- never the full hash, never PII."""
    return f"user-{pseudonym[:8]}"


def _period_token() -> str:
    """Return today's UTC date as `YYYYMMDD` -- the per-day claim window token.

    See module docstring "Window mechanism" -- no scheduler/cron resets
    `!first` at midnight; the date token embedded in `_winner_key`/
    `_attempts_key` IS the reset.
    """
    now = datetime.fromtimestamp(clock.now_millis() / 1000, tz=UTC)
    return now.strftime("%Y%m%d")


def _classify(parsed: ParsedCommand) -> tuple[str, str | None]:
    """Map a successfully parsed `!first ...` command to one of `_KNOWN_ACTIONS`.

    Returns `(action, arg)` where `arg` carries a ready-to-send usage/error
    string for `action == "usage"`, else `None`. Never raises -- an
    unrecognized-but-parseable combination (e.g. `!first leaderboard list`,
    structurally valid per the grammar but meaningless for this command)
    degrades to a `"usage"` action rather than a crash.
    """
    if parsed.sub_module == "leaderboard":
        if parsed.option is not None:
            return "usage", _USAGE
        return "leaderboard", None
    if parsed.option is None:
        return "claim", None
    if parsed.option == "list":
        return "list", None
    if parsed.option == "reset":
        return "reset", None
    return "usage", _USAGE


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!first` and its verbs/sub-module.

    No `kv` access here -- only command recognition and forwarding the
    normalized badge signal (`is_mod`/`is_broadcaster`, if the platform's
    normalizer emits them) for `dispatch`'s own `reset` permission gate.
    Returns `None` for any non-chat payload, text that isn't `!first`-shaped
    (cheap-skip, zero `kv` cost), and while `waddles.command-first` is
    disabled.
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
        log.info("first.transform usage_error", error_type=type(exc).__name__)
        action, arg = "usage", str(exc)
    else:
        action, arg = _classify(parsed)

    log.info("first.transform matched", action=action)
    payload: dict[str, Any] = {"action": action, "channel_id": event.payload.get("channel_id")}
    if arg is not None:
        payload["arg"] = arg
    # Forward the normalized badge signal, if the platform's own normalizer emitted one --
    # absence (e.g. Discord today) must reach `dispatch` as absence, not an implicit `False`.
    if "is_mod" in event.payload:
        payload["is_mod"] = bool(event.payload["is_mod"])
    if "is_broadcaster" in event.payload:
        payload["is_broadcaster"] = bool(event.payload["is_broadcaster"])

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


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    `None` means the platform's normalizer emitted neither `is_mod` nor
    `is_broadcaster` at all (e.g. Discord's `normalize_discord` today) --
    `!first reset` must treat `None` as denied, exactly like `False`, never
    as an implicit allow. Mirrors `lurk`'s own `_caller_role_signal`.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


async def _fail_kv(exc: Exception, *, provider: str, channel_id: str, op: str) -> NoReturn:
    """Fail-loud kv error path: log, reply an error to chat, then re-raise.

    Classifies the raised exception structurally (`getattr(exc, "value",
    exc)`), same pattern as `lurk`'s own `_fail_kv`/`waddle_sdk.db`'s
    documented convention -- `community_kv`'s underlying `kv.get/set/delete/
    increment` raise the generated WIT `Err` (`.value` holds `Error_TooLarge`/
    `Error_Backend`) on a real backend failure. Never silent: the caller
    always sees a chat reply AND the pipeline still sees a real failure (the
    re-raise).
    """
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("first.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "first is temporarily unavailable, try again shortly."},
    )
    raise RuntimeError(f"first kv {op} failed: {case_name}") from exc


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

    Corruption here means the stored bytes are not a plain decimal integer --
    never expected in normal operation (every writer in this module only
    ever stores `kv.increment`'s own return value), so this is defensive,
    not a path any test needs to contrive beyond proving it degrades
    gracefully rather than crashing `dispatch`. Mirrors `lurk`'s own
    `lurk.state_corrupt` self-heal convention rather than `count`'s harder
    `_KvFailure` (an attempt/win *counter* misreading as `0` is recoverable;
    a `count` user-facing counter value is not this module's own invention
    to redefine).
    """
    if raw is None:
        return 0
    try:
        return int(raw.decode())
    except (UnicodeDecodeError, ValueError):
        log.error("first.state_corrupt", context=context, community=community)
        return 0


async def _load_leaderboard_registry(
    *, community: str, provider: str, channel_id: str
) -> list[str]:
    """Return every pseudonym that has ever won `!first` in this community, or `[]`.

    Raises via `_fail_kv` on a real backend failure; corrupt registry
    content (not a JSON array of strings) self-heals to `[]` and logs --
    mirrors `_parse_int`'s own corruption stance, not `count`'s harder
    `_KvFailure` (a corrupt leaderboard registry degrades the leaderboard
    display, it does not make any `!first` claim incorrect).
    """
    raw = await _kv_get(
        _LEADERBOARD_REGISTRY_KEY, community=community, provider=provider, channel_id=channel_id
    )
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.error("first.state_corrupt", context="leaderboard_registry", community=community)
        return []
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        log.error("first.state_corrupt", context="leaderboard_registry", community=community)
        return []
    return data


async def _register_leaderboard_entry(
    pseudonym: str, *, community: str, provider: str, channel_id: str
) -> None:
    """Add `pseudonym` to the community's leaderboard registry, if not already present."""
    registry = await _load_leaderboard_registry(
        community=community, provider=provider, channel_id=channel_id
    )
    if pseudonym in registry:
        return
    registry.append(pseudonym)
    await _kv_set(
        _LEADERBOARD_REGISTRY_KEY,
        json.dumps(sorted(registry)).encode("utf-8"),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_PERSISTENT_TTL_SECONDS,
    )


async def _handle_claim(
    *, community: str, actor: str | None, username: str, provider: str, channel_id: str
) -> str:
    """Claim-or-report the day's `!first`: win if nobody has claimed yet today, else report Nth.

    Every bare `!first` call increments the day's attempt counter first --
    including the eventual winner's own call, so the winner is always
    attempt #1 -- then checks whether a winner is already recorded. `None`
    means this call wins; recording the winner and the all-time win count
    happen atomically-enough for a single-threaded stage invocation (no
    concurrent claims within one invocation).
    """
    period = _period_token()
    pseudonym = _pseudonym(actor)

    attempt_number = await _kv_increment(
        _attempts_key(period),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_WINDOW_TTL_SECONDS,
    )
    existing = await _kv_get(
        _winner_key(period), community=community, provider=provider, channel_id=channel_id
    )

    if existing is not None:
        log.info("first.already_claimed", community=community, attempt=attempt_number)
        return (
            f"First was already claimed today -- {username}, you're #{attempt_number} to try. "
            "Try `!first list` for details or `!first leaderboard` for the hall of fame."
        )

    await _kv_set(
        _winner_key(period),
        pseudonym.encode("utf-8"),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_WINDOW_TTL_SECONDS,
    )
    new_wins = await _kv_increment(
        _wins_key(pseudonym),
        community=community,
        provider=provider,
        channel_id=channel_id,
        ttl_seconds=_PERSISTENT_TTL_SECONDS,
    )
    await _register_leaderboard_entry(
        pseudonym, community=community, provider=provider, channel_id=channel_id
    )
    log.info("first.claimed", community=community, attempt=attempt_number, wins=new_wins)
    return f"\U0001f389 {username} claimed first today! (win #{new_wins} for them)"


async def _handle_list(*, community: str, provider: str, channel_id: str) -> str:
    """Report today's winner (pseudonymized) and how many have tried -- never mutates state."""
    period = _period_token()
    winner_raw = await _kv_get(
        _winner_key(period), community=community, provider=provider, channel_id=channel_id
    )
    if winner_raw is None:
        return "No one has claimed first today yet -- be the first with !first!"

    try:
        pseudonym = winner_raw.decode("utf-8")
    except UnicodeDecodeError:
        log.error("first.state_corrupt", context="winner", community=community)
        return "No one has claimed first today yet -- be the first with !first!"

    attempts_raw = await _kv_get(
        _attempts_key(period), community=community, provider=provider, channel_id=channel_id
    )
    attempts = _parse_int(attempts_raw, context="attempts", community=community)
    tries_word = "try" if attempts == 1 else "tries"
    return f"First today: {_display_handle(pseudonym)}. {attempts} {tries_word} so far."


async def _handle_leaderboard(*, community: str, provider: str, channel_id: str) -> str:
    """Report the community's all-time top claimers, pseudonymized -- never mutates state."""
    registry = await _load_leaderboard_registry(
        community=community, provider=provider, channel_id=channel_id
    )
    scored: list[tuple[int, str]] = []
    for pseudonym in registry:
        raw = await _kv_get(
            _wins_key(pseudonym), community=community, provider=provider, channel_id=channel_id
        )
        wins = _parse_int(raw, context="wins", community=community)
        if wins > 0:
            scored.append((wins, pseudonym))

    if not scored:
        return "No one has claimed first yet -- be the first with !first!"

    scored.sort(key=lambda item: (-item[0], item[1]))
    top = scored[:_LEADERBOARD_TOP_N]
    entries = ", ".join(f"{_display_handle(pseudonym)} ({wins})" for wins, pseudonym in top)
    return f"First leaderboard: {entries}"


async def _handle_reset(*, community: str, provider: str, channel_id: str) -> str:
    """Clear the *current* day's winner + attempts only -- never the all-time wins/leaderboard."""
    period = _period_token()
    await _kv_delete(
        _winner_key(period), community=community, provider=provider, channel_id=channel_id
    )
    await _kv_delete(
        _attempts_key(period), community=community, provider=provider, channel_id=channel_id
    )
    log.info("first.reset", community=community, period=period)
    return "First has been reset for today -- the next !first claims it."


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
        raise ValueError("first reply requires a channel_id from the inbound chat.message")
    action = payload.get("action")
    if action not in _KNOWN_ACTIONS:
        raise ValueError(f"unrecognized first action: {action!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("first.missing_community", action=action)
        raise ValueError("first requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"

    if action == "usage":
        text = payload.get("arg")
        reply_text = text if isinstance(text, str) and text else _USAGE
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        return DispatchResult(transport=provider, detail="usage")

    if action == "reset":
        role_signal = _caller_role_signal(payload)
        if role_signal is not True:
            log.info("first.reset_denied", role_signal=str(role_signal))
            await relay.push(provider, {"channel": channel_id, "text": _PERMISSION_DENIED_MSG})
            return DispatchResult(transport=provider, detail="reset:denied")
        reply_text = await _handle_reset(
            community=community, provider=provider, channel_id=channel_id
        )
    elif action == "claim":
        reply_text = await _handle_claim(
            community=community,
            actor=envelope.event.actor,
            username=username,
            provider=provider,
            channel_id=channel_id,
        )
    elif action == "list":
        reply_text = await _handle_list(
            community=community, provider=provider, channel_id=channel_id
        )
    else:  # leaderboard
        reply_text = await _handle_leaderboard(
            community=community, provider=provider, channel_id=channel_id
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("first.dispatch relayed", platform=provider, action=action)
    return DispatchResult(transport=provider, detail=action)
