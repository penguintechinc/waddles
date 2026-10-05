"""`!raffle` (+ `!enter` alias) -> a per-community giveaway/raffle, kv-only.

Original Waddles content -- not a port, not inspired by any third-party bot (contrast
`fish`/`music`'s own superpenguintv-inspired module docstrings). `author`/`license` in
`bundle.yaml` follow `bundles/python/count`/`bundles/python/pyping`'s own
`PenguinTech/waddles` convention for first-party, non-ported bundles.

v1 scope (KV-ONLY, no `db`/`http`/egress capability -- a single per-community entrant list
and an open/closed flag, nothing more):

- `!raffle open` -- moderator/broadcaster only: CLEARS any prior entrant list and marks the
  raffle OPEN for this community. Same `_caller_role_signal()` fail-closed pattern as every
  other admin-only command in this codebase (`fish`/`music`/`lurk`/`count`/`shoutout`'s own
  identical helper): absent badge fields (e.g. Discord's normalizer today) deny, never
  implicit allow.
- `!raffle close` -- moderator/broadcaster only: marks the raffle CLOSED. The entrant list is
  left untouched (a mod may still `!raffle draw` from a closed raffle -- see `_handle_draw`'s
  own docstring for why draw is deliberately independent of open/closed state).
- Bare `!raffle` **or** bare `!enter` -- ENTER the caller into the community's currently open
  raffle, once. A caller already in the entrant list gets a friendly "already entered" reply
  (never a silent no-op -- AUTHORING.md Sec4 fail-loud rule); entering a CLOSED (or never-
  opened) raffle is rejected with a reply, never silently dropped. `!enter <anything>` (extra
  trailing text) is a usage error -- `!enter` takes no arguments, unlike `!sr`'s own bare
  free-text "add" command.
- `!raffle draw` -- moderator/broadcaster only: picks one uniformly-random entrant and
  announces it. Does **not** require the raffle to be closed first (a mod's own call on when
  "enough" entries have accumulated); does **not** clear the entrant list or state afterward
  (a mod re-opens via `!raffle open` to start the next round -- see that command's own
  docstring). An empty entrant list replies with a no-entrants message rather than raising.
- `!raffle list` -- anyone: the current entrant COUNT (and open/closed state), reached
  through the shared grammar's `list` verb.

**Why `open`/`close`/`draw` are NOT run through the shared `waddle_sdk.command.VERBS`
vocabulary.** `VERBS` (`set`/`add`/`sub`/`enable`/`disable`/`remove`/`delete`/`list`/`reset`)
is a fixed, cross-bundle vocabulary -- a bundle cannot add its own verb to it. This bundle's
three domain verbs are special-cased ahead of the formal `parse_command()` call in
`_resolve_raffle()`, exactly mirroring `music`'s own documented `next`/`skip` "bare advance
keywords" extension (`bundles/python/music/src/app.py`'s module docstring) -- NOT smuggled in
as fake `CommandSpec.sub_modules` (`sub_modules` names opt-in feature sub-modules, a different
concept -- `waddle_sdk.sub_modules`'s own docstring). The bare case and `list` still go
through the real shared grammar, so `!raffle list extra` correctly resolves to a usage reply
exactly like `fish`'s own `!fish list extra`.

**PII: winner announcement is pseudonym-only, by design.** Every entrant is stored as a
SHA-256 hash of the caller's own `event.actor` (`_pseudonym()`, identical pattern to
`fish`/`music`/`shoutout`/`count`/`lurk`'s own helper of the same name) -- never a raw
username, ahead of the PII-tokenization pipeline (#429) in case `event.actor` is still a raw
username today. Because only the pseudonym is ever retained, `!raffle draw` genuinely cannot
resolve a past entrant back to a display name the channel would recognize (unlike `!fish`/
`!music`, which can show the CALLER's own live username because that information comes from
the *current* event, not a stored one). The announced winner is therefore a short, non-
identifying pseudonym tag (`winner[:8]`) that the actual winner can self-recognize by
comparing against their own freshly computed pseudonym -- the same "count/tag, not identity"
trade-off `music`'s own `(yours)` queue-listing marker and `shoutout`'s own `auto` sub-module
both document. Resolving a tag to a real, pingable identity is a documented, NOT-built
extension that needs either the tokenization pipeline (#429) or the platform's own
reputation/user-details cross-community lookup (`community_kv`'s module docstring: the
platform's only two cross-community exceptions) -- never built here, never stubbed.

Gated behind the PostHog flag ``waddles.command-raffle`` -- checked in `transform()` after the
cheap command-head match and before any grammar resolution (`eightball`'s documented ordering
rationale, reused verbatim by every bundle in this codebase).

DO NOT BUILD in v1 -- clean, documented extension points, never a silent stub:

- **Resolvable winner identity / DM-the-winner.** See the PII note above -- needs either
  #429 or a cross-community user-details lookup, neither of which this bundle depends on.
- **Multiple concurrent raffles per community, prize tiers, ticket weighting (multiple
  entries per viewer), or a raffle history/audit log.** This is a single open/closed flag and
  one flat entrant list per community -- exactly the v1 slice the spec calls for, nothing more.
- **Cross-community anything.** The entrant list and state flag are keyed by `community_id`
  only (`waddle_sdk.community_kv` -- reputation/user-details are the platform's only two
  cross-community exceptions, and this bundle is neither).
"""

from __future__ import annotations

import hashlib
import json
import random
from typing import Any, NoReturn, cast

from waddle_sdk import community_kv, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-raffle"

#: Chat-invoked heads. `!raffle` carries the full grammar (open/close/draw/list/bare);
#: `!enter` is a pure bare alias for the bare-`!raffle` ENTER action -- see module docstring.
_RAFFLE_HEAD = "!raffle"
_ENTER_HEAD = "!enter"

#: No sub-modules declared -- `!raffle` has no opt-in feature toggles.
SPEC = CommandSpec(name="raffle")

#: Domain verbs special-cased ahead of the formal grammar parse -- none is in
#: `waddle_sdk.command.VERBS` (see module docstring's own rationale).
_RAFFLE_VERBS = frozenset({"open", "close", "draw"})

#: Caps unbounded growth of one community's entrant list (mirrors `music`'s own
#: `MAX_QUEUE_SIZE` convention) -- protects against a `kv.error::too_large` on the stored
#: JSON array rather than discovering that limit in production.
MAX_ENTRANTS = 5000

#: Durable per-community state -- never expires (`ttl_seconds=0`); an in-progress raffle
#: should survive indefinitely until a mod explicitly opens/closes/draws it.
_ENTRANTS_KEY = "raffle.entrants"
_STATE_KEY = "raffle.state"
_STATE_OPEN = b"open"
_STATE_CLOSED = b"closed"

_USAGE = "Usage: !raffle | !enter | !raffle list | !raffle open|close|draw (mod only)"
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can do that"
_NOT_OPEN_MSG = "no raffle is open right now -- ask a mod to !raffle open"
_NO_ENTRANTS_MSG = "no entrants yet -- have viewers !enter once the raffle is open"

_KNOWN_COMMANDS = frozenset({"enter", "open", "close", "draw", "list", "usage"})


def _pseudonym(actor: str | None) -> str:
    """Non-reversible per-caller key component -- see module docstring's PII note.

    `event.actor` may currently be a raw username (tokenization pipeline #429 not yet
    merged); hashing it before it ever reaches `community_kv` keeps this bundle PII-safe
    today and after #429 lands unchanged.
    """
    return hashlib.sha256((actor or "anonymous").encode()).hexdigest()


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from the normalized event's own badge fields, or `None` if absent.

    See `count`/`lurk`/`fish`/`shoutout`/`music`'s own identical helper -- `None` (neither
    `is_mod`/`is_broadcaster` present, e.g. Discord's normalizer today) must be treated as
    denied, never as an implicit allow.
    """
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return bool(payload.get("is_mod")) or bool(payload.get("is_broadcaster"))


def _resolve_raffle(rest: str) -> str:
    """Map the text after `!raffle ` onto this bundle's own command set.

    `rest` is already stripped and may be empty. `open`/`close`/`draw` are handled here,
    before the formal grammar parse -- see module docstring. Every other shape is normalized
    onto `!raffle ...` and delegated to `parse_command` unchanged.
    """
    if rest:
        first_tok, _, remainder = rest.partition(" ")
        if first_tok.lower() in _RAFFLE_VERBS:
            return "usage" if remainder.strip() else first_tok.lower()

    normalized = f"!raffle {rest}" if rest else "!raffle"
    try:
        parsed = parse_command(normalized, SPEC)
    except CommandUsageError:
        return "usage"
    return _map_parsed(parsed)


def _map_parsed(parsed: ParsedCommand) -> str:
    """Map a successfully parsed `ParsedCommand` onto this bundle's own command set.

    Only `option in (None, "list")` is implemented -- every other grammar-legal verb
    (`set`/`add`/`sub`/`enable`/`disable`/`remove`/`delete`/`reset`, none of which this
    bundle declares sub-modules or behavior for) resolves to `"usage"`, same fail-loud-
    never-silent rule as `fish`/`music`'s own identical mapping function.
    """
    if parsed.option is None:
        return "enter"
    if parsed.option == "list":
        return "usage" if parsed.args is not None else "list"
    return "usage"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!raffle`/`!enter`, via `parse_command`.

    Cheap-skip first (no matching head -- `None`, zero cost), flag check second, real
    grammar resolution last -- same ordering as `eightball`/`fish`/`music`'s own documented
    rationale. A recognized-but-malformed command still produces a reply (`"usage"`), never
    a silent drop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    head_lower = head.lower()

    if head_lower == _ENTER_HEAD:
        is_enter_alias = True
    elif head_lower == _RAFFLE_HEAD:
        is_enter_alias = False
    else:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    if is_enter_alias:
        # `!enter` takes no further arguments -- it is a pure bare alias, never a free-text
        # add like `!sr`'s own bare command (module docstring).
        command = "usage" if rest.strip() else "enter"
    else:
        command = _resolve_raffle(rest.strip())

    log.info("raffle.transform matched", command=command)
    payload: dict[str, Any] = {"command": command, "channel_id": event.payload.get("channel_id")}
    # Forward the normalized badge signal, if present -- see `count`/`lurk`/`fish`/`shoutout`/
    # `music`'s own identical forwarding comment for why absence must reach `dispatch` as
    # absence, not `False`.
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


async def _fail_kv(exc: Exception, *, provider: str, channel_id: str, op: str) -> NoReturn:
    """Fail-loud kv backend-error path: log, reply an error to chat, then re-raise.

    See `fish`/`music`'s own identical `_fail_kv`.
    """
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("raffle.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {
            "channel": channel_id,
            "text": "the raffle is temporarily unavailable, try again shortly.",
        },
    )
    raise RuntimeError(f"raffle kv {op} failed: {case_name}") from exc


async def _fail_state(reason: str, *, provider: str, channel_id: str) -> NoReturn:
    """Fail-loud corrupt-stored-entrants path -- see `music`'s own identical `_fail_state`."""
    log.error("raffle.state_corrupt", reason=reason)
    await relay.push(
        provider,
        {"channel": channel_id, "text": "the raffle state is corrupted, please contact support."},
    )
    raise RuntimeError(f"raffle corrupt state: {reason}")


async def _kv_get(community: str, key: str, *, provider: str, channel_id: str) -> bytes | None:
    """`community_kv.get`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        result = await community_kv.get(community, key)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="get")
    # `waddle_sdk` ships no `py.typed` marker, so mypy sees `Any` here -- cast back to the
    # real contract (see `music`'s own identical `_kv_get` for the same pattern).
    return cast("bytes | None", result)


async def _kv_set(
    community: str, key: str, value: bytes, *, ttl_seconds: int, provider: str, channel_id: str
) -> None:
    """`community_kv.set`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        await community_kv.set(community, key, value, ttl_seconds)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="set")


async def _is_open(community: str, *, provider: str, channel_id: str) -> bool:
    """`True` iff the community's raffle state is exactly `_STATE_OPEN`.

    Unset (never opened) or any corrupt/unexpected value both resolve to `False` -- a
    fail-closed default, same posture as `_caller_role_signal`'s own "absence means denied".
    """
    raw = await _kv_get(community, _STATE_KEY, provider=provider, channel_id=channel_id)
    return raw == _STATE_OPEN


async def _load_entrants(community: str, *, provider: str, channel_id: str) -> list[str]:
    """Return the community's entrant pseudonym list.

    Raises (via `_fail_state`) on corrupt stored JSON -- see that helper's own docstring for
    why this is fail-loud rather than a silent reset. Mirrors `music`'s own `_load_queue`.
    """
    raw = await _kv_get(community, _ENTRANTS_KEY, provider=provider, channel_id=channel_id)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        await _fail_state(f"corrupt entrants: {exc}", provider=provider, channel_id=channel_id)
    if not isinstance(data, list):
        await _fail_state(
            "corrupt entrants: expected a JSON array", provider=provider, channel_id=channel_id
        )

    entrants: list[str] = []
    for item in data:
        if not isinstance(item, str):
            await _fail_state(
                "corrupt entrants: expected string pseudonyms",
                provider=provider,
                channel_id=channel_id,
            )
        entrants.append(item)
    return entrants


async def _save_entrants(
    community: str, entrants: list[str], *, provider: str, channel_id: str
) -> None:
    """Persist the community's entrant pseudonym list."""
    await _kv_set(
        community,
        _ENTRANTS_KEY,
        json.dumps(entrants).encode("utf-8"),
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )


def _pick_winner(entrants: list[str]) -> str:
    """Pick one uniformly-random entrant pseudonym. Isolated for seeded-RNG testing."""
    return random.choice(entrants)  # noqa: S311 - a game, not a security decision


async def _handle_open(community: str, *, provider: str, channel_id: str) -> str:
    """`!raffle open` -- clear any prior entrant list and mark the raffle OPEN."""
    await _save_entrants(community, [], provider=provider, channel_id=channel_id)
    await _kv_set(
        community, _STATE_KEY, _STATE_OPEN, ttl_seconds=0, provider=provider, channel_id=channel_id
    )
    log.info("raffle.opened", community=community)
    return "\U0001f3df️ the raffle is open! use !raffle or !enter to join."


async def _handle_close(community: str, *, provider: str, channel_id: str) -> str:
    """`!raffle close` -- mark the raffle CLOSED.

    Entrants are left untouched (module docstring).
    """
    await _kv_set(
        community,
        _STATE_KEY,
        _STATE_CLOSED,
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )
    log.info("raffle.closed", community=community)
    return "the raffle is now closed -- no more entries."


async def _handle_enter(
    community: str, actor: str | None, username: str, *, provider: str, channel_id: str
) -> str:
    """`!raffle`/`!enter` bare -- enter the caller into the community's open raffle, once."""
    if not await _is_open(community, provider=provider, channel_id=channel_id):
        return _NOT_OPEN_MSG

    entrants = await _load_entrants(community, provider=provider, channel_id=channel_id)
    pseudonym = _pseudonym(actor)
    if pseudonym in entrants:
        return f"{username}, you're already entered!"
    if len(entrants) >= MAX_ENTRANTS:
        return f"the raffle is full ({MAX_ENTRANTS} max entrants)"

    entrants.append(pseudonym)
    await _save_entrants(community, entrants, provider=provider, channel_id=channel_id)
    log.info("raffle.entered", community=community)
    return f"\U0001f389 {username} entered the raffle! ({len(entrants)} entered)"


async def _handle_draw(community: str, *, provider: str, channel_id: str) -> str:
    """`!raffle draw` -- pick+announce one random entrant. See module docstring's PII note."""
    entrants = await _load_entrants(community, provider=provider, channel_id=channel_id)
    if not entrants:
        return _NO_ENTRANTS_MSG

    winner = _pick_winner(entrants)
    log.info("raffle.drawn", community=community)
    return f"\U0001f389 the winner is entrant {winner[:8]}! DM a mod to claim your prize."


async def _handle_list(community: str, *, provider: str, channel_id: str) -> str:
    """`!raffle list` -- the current entrant count and open/closed state."""
    entrants = await _load_entrants(community, provider=provider, channel_id=channel_id)
    is_open = await _is_open(community, provider=provider, channel_id=channel_id)
    state = "open" if is_open else "closed"
    return f"the raffle is {state} with {len(entrants)} entrant(s)."


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: permission gate where required, then the raffle op.

    Raises:
        ValueError: The envelope's payload has no `channel_id`; the envelope has no
            `community` (no tenant-wide fallback -- see module docstring's data-scoping
            section); or an unrecognized `command` (defensive -- `transform` only ever
            emits a member of `_KNOWN_COMMANDS`).
        RuntimeError: A `kv` backend call failed, or stored entrant state was corrupt (see
            `_fail_kv`/`_fail_state` -- a chat error reply and an ERROR log line are always
            emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("raffle reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized raffle command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("raffle.missing_community", command=command)
        raise ValueError("raffle requires a community context and cannot operate tenant-wide")

    if command == "usage":
        await relay.push(provider, {"channel": channel_id, "text": _USAGE})
        return DispatchResult(transport=provider, detail="usage")

    # `enter`/`list` are open to any caller; `open`/`close`/`draw` are moderator/broadcaster
    # only (module docstring).
    role_signal = _caller_role_signal(payload)
    if command in ("open", "close", "draw") and role_signal is not True:
        log.info("raffle.permission_denied", command=command, role_signal=str(role_signal))
        await relay.push(provider, {"channel": channel_id, "text": _PERMISSION_DENIED_MSG})
        return DispatchResult(transport=provider, detail=f"{command}:denied")

    actor = envelope.event.actor
    username = actor or "someone"

    if command == "open":
        reply_text = await _handle_open(community, provider=provider, channel_id=channel_id)
    elif command == "close":
        reply_text = await _handle_close(community, provider=provider, channel_id=channel_id)
    elif command == "draw":
        reply_text = await _handle_draw(community, provider=provider, channel_id=channel_id)
    elif command == "list":
        reply_text = await _handle_list(community, provider=provider, channel_id=channel_id)
    else:  # enter
        reply_text = await _handle_enter(
            community, actor, username, provider=provider, channel_id=channel_id
        )

    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("raffle.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
