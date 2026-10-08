"""`!hug`/`!highfive`/`!pat` -> fun social interaction commands, community-scoped, kv-only.

Several small interaction commands sharing one bundle, one registry
(`_INTERACTIONS`), and one dispatch path -- adding a fourth (`!fistbump`,
`!highifve` typo aside) is a new `_InteractionSpec` entry plus one line in
`bundle.yaml`'s `consumes` filters, never a structural change to
`transform`/`dispatch` themselves.

Inspiration credit (not a literal port -- see `bundle.yaml`'s `author`/
`notice`, same convention as `bundles/python/fish`): the general concept of
lightweight "fun" social/interaction commands (hug-a-user, high-five,
pat-on-the-back) is a staple feature category of superpenguintv
(Psychoboy)'s `PenguinTwitchBot` (https://github.com/Psychoboy/PenguinTwitchBot)
and of Twitch chat bots generally. No original source code or text is reused
here -- the specific command set, flavor text, and all dispatch logic below
are written fresh for this bundle, so no MIT notice reproduction is required
(`sdk/waddle-sdk/AUTHORING.md`'s attribution convention: verbatim-port vs.
inspired-by, same split as `fish` vs. `bundles/csharp/superpenguin-roll`).

Each interaction follows the exact same shape, so only one is walked through
here (`!hug`, the others are identical modulo flavor text):

- `!hug <user>` -- INTERACT. Validates `<user>` looks like a plausible
  username (`_normalize_target()`, copied from `bundles/python/duel`'s own
  helper of the same name -- no shared SDK utility exists yet for this
  shape-check, see that bundle's own docstring for why it's a shape check
  only, never a roster lookup). An unrecognizable target replies with a
  clear "I don't know who that is" message and touches no state -- never a
  silent drop, never a crash. A self-target (`<user>` normalizes to the
  caller's own `actor`, case-insensitive) replies with a dedicated,
  interaction-specific message and also touches no state. Otherwise: posts a
  random flavor-text reply and increments **both** the caller's "given" and
  the target's "received" counter for that interaction.
- `!hug` (bare, no target) -- usage reply, never a silent no-op.
- `!social stats` -- reads the caller's own given/received tally across
  *every* registered interaction from `kv` and replies with one summary
  line. `"stats"` doesn't fit `waddle_sdk.command.VERBS` (no bare-noun verb
  in that vocabulary reads naturally as a cross-command rollup), so this one
  top-level command is parsed by hand (`_resolve_social_command()`) rather
  than through `waddle_sdk.command.parse_command()` -- a deliberate,
  documented deviation from the "always use the shared grammar parser"
  convention, same spirit as `duel`'s own documented fallback for a bare
  challenge-target token the grammar parser can't express either.

**No cooldown, no admin-configurable state.** Unlike `duel`'s competitive
coin-flip (which gates spam behind a per-caller cooldown because the
*outcome* matters), a hug/high-five/pat has no win/loss stake -- the counters
are a novelty tally, not a resource. Spamming `!hug` only inflates the
spammer's own "given" count and the target's "received" count, which is
harmless by design. No `!hug set ...`/`!social set ...` admin command is
declared in v1; nothing here needs broadcaster/moderator configuration.

**Identity/pseudonymization**: both participants' per-user state keys are
SHA-256 pseudonyms (`_pseudonym()`), never the raw username/actor id -- same
rationale as `duel`/`fish`'s own `_pseudonym()`: `event.actor` may currently
be a raw username (tokenization pipeline #429 not yet merged), so hashing
before anything ever reaches `community_kv` keeps this bundle PII-safe today
and unchanged after #429 lands. The initiator is pseudonymized from
`event.actor`; the target has no `actor` of their own in this inbound event
(only their chat-typed name), so their pseudonym is derived from the
case-folded, validated target string instead -- stable across callers typing
the same name in different casing, but NOT guaranteed to collide with that
user's own `event.actor`-derived pseudonym on a platform where the two forms
differ. Documented trade-off, not a stand-in for a real user directory
(`kv` alone cannot provide one -- see "DO NOT BUILD" below).

**kv keys are colon-free (gh-631).** Every key below uses `.` as its
namespace separator (`social.<interaction>.given.<pseudonym>` /
`social.<interaction>.received.<pseudonym>`) -- `waddle_sdk.kv.validate_key()`
now rejects `:` outright (the real host's own reserved namespace separator,
`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`), so this bundle
never repeats the mistake `count`/`lurk` shipped pre-gh-631.

DO NOT BUILD in v1 -- clean, documented extension points, never a silent
stub:

- **Cross-community leaderboards / global rankings.** Every key here is
  scoped by `community_id` only (`waddle_sdk.community_kv` -- reputation +
  user-details are the platform's only two cross-community exceptions, and
  this bundle is neither). `kv` has no scan/list-keys primitive -- a
  leaderboard needs a real `db` capability with an `order_by`/pagination
  surface. **Deferred to a v2**, same deferral `duel`/`fish` document for
  their own leaderboards -- no `!social leaderboard` command declared here.
- **Real username resolution / mention validation against a roster.**
  `_normalize_target()` is a shape check, not a lookup against an actual
  community member list. A target that is shape-valid but does not
  correspond to any real community member still resolves as a normal
  interaction; known v1 limitation, not a bug.
- Reaction/combo mechanics (e.g. hugging back within N seconds for a
  bonus) -- out of scope for this kv-only v1 entirely, not partially
  stubbed.

Gated behind the PostHog flag ``waddles.command-social`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (cheap command-match first, flag check second, real
classification last). One flag gates all four commands (`!hug`,
`!highfive`, `!pat`, `!social`) -- they ship and roll back together.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from typing import Any, NoReturn

from waddle_sdk import community_kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-social"

#: A plausible username/mention shape: an optional leading `@` (stripped),
#: then 1-32 chars of letters/digits/underscore/dot/hyphen. Not a lookup
#: against a real roster -- see module docstring's "DO NOT BUILD" section.
#: Copied from `bundles/python/duel/src/app.py::_USERNAME_RE` -- no shared
#: SDK utility exists yet for this shape check.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$")

_SOCIAL_USAGE = "Usage: !social stats"
_NO_RECORD_YET = "You haven't interacted with anyone yet -- try !hug, !highfive, or !pat!"

_KNOWN_COMMANDS = frozenset({"interact", "usage", "stats"})


@dataclass(slots=True, frozen=True)
class _InteractionSpec:
    """One registered `!<command> <user>` interaction -- add a new one here only.

    `usage` and `unknown_target`/`self_message` are plain `str.format()`
    templates (`{raw}` / `{giver}` as applicable) rather than
    `waddle_sdk.command.substitute_placeholders()`'s `$(name)` syntax --
    that helper is for bundle-author-facing reply *templates* configured at
    runtime (e.g. `!sr`'s announcement text); these are fixed, code-owned
    strings, so plain f-string-shaped `.format()` is simpler and avoids
    importing a templating helper for a fully static set of strings.
    """

    command: str
    emoji: str
    templates: tuple[str, ...]
    self_message: str


def _wins_style_key(interaction: str, direction: str, pseudonym: str) -> str:
    """Build a colon-free, interaction-scoped counter key (gh-631 -- see module docstring)."""
    return f"social.{interaction}.{direction}.{pseudonym}"


#: Original flavor text, written fresh for this bundle (module docstring) --
#: no PenguinTwitchBot source or text reused.
_INTERACTIONS: dict[str, _InteractionSpec] = {
    "hug": _InteractionSpec(
        command="hug",
        emoji="\U0001f917",
        templates=(
            "{giver} wraps {target} in a big warm hug! {emoji}",
            "{giver} gives {target} the comfiest hug around. {emoji}",
            "{giver} squeezes {target} tight for a hug! {emoji}",
            "{giver} sneaks up and hugs {target} from behind! {emoji}",
        ),
        self_message="{giver} hugs themselves. Self-care counts too! {emoji}",
    ),
    "highfive": _InteractionSpec(
        command="highfive",
        emoji="\U0001f91a",
        templates=(
            "{giver} and {target} high-five with a satisfying *SMACK*! {emoji}",
            "{giver} raises a hand and {target} meets it perfectly. {emoji}",
            "{giver} high-fives {target} -- nothing but net! {emoji}",
            "{giver} and {target} trade an epic high-five! {emoji}",
        ),
        self_message="{giver} high-fives themselves. Impressively flexible! {emoji}",
    ),
    "pat": _InteractionSpec(
        command="pat",
        emoji="\U0001f427",
        templates=(
            "{giver} gives {target} a gentle pat on the head. {emoji}",
            "{giver} pats {target} on the back -- well done! {emoji}",
            "{giver} pats {target}'s head approvingly. {emoji}",
            "{giver} gives {target} a reassuring pat. {emoji}",
        ),
        self_message="{giver} pats their own head. Good job, {giver}! {emoji}",
    ),
}

#: `!<command>` -> interaction name, derived once from `_INTERACTIONS` so the
#: two can never drift (e.g. a typo'd command string in one but not the
#: other).
_COMMAND_TO_INTERACTION: dict[str, str] = {
    f"!{spec.command}": name for name, spec in _INTERACTIONS.items()
}


def _pseudonym(identity: str | None) -> str:
    """Non-reversible per-identity key component -- see `duel`'s own `_pseudonym()` for why.

    `identity` may be a raw username (tokenization pipeline #429 not yet
    merged); hashing it before it ever reaches `community_kv` keeps this
    bundle PII-safe today and after #429 lands unchanged.
    """
    return hashlib.sha256((identity or "anonymous").encode()).hexdigest()


def _normalize_target(raw: str) -> str | None:
    """Strip an optional leading `@` and validate the shape; `None` if not a plausible username.

    Shape check only -- not a lookup against a real community roster (`kv`
    has no such primitive; see module docstring's "DO NOT BUILD" section).
    """
    candidate = raw[1:] if raw.startswith("@") else raw
    if not candidate or not _USERNAME_RE.match(candidate):
        return None
    return candidate


def _resolve_interaction_command(stripped: str, *, command_word: str) -> tuple[str, str | None]:
    """Classify a full `!hug ...`/`!highfive ...`/`!pat ...` message.

    Returns `(command, target)`: `target` is the raw (unvalidated) chat-typed
    target for `"interact"`, `None` for `"usage"`. A bare command or one with
    more than one trailing token is `"usage"` -- never silently dropped.
    """
    rest = stripped.partition(" ")[2].strip()
    if not rest:
        return "usage", None
    tok1, _, tail = rest.partition(" ")
    if tail.strip():
        return "usage", None
    return "interact", tok1


def _resolve_social_command(stripped: str) -> str:
    """Classify a full `!social ...` message into `"stats"` or `"usage"`.

    `"stats"` is not a member of `waddle_sdk.command.VERBS` (see module
    docstring) so this is hand-parsed rather than routed through
    `waddle_sdk.command.parse_command()`.
    """
    rest = stripped.partition(" ")[2].strip()
    if rest.lower() == "stats":
        return "stats"
    return "usage"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!hug`/`!highfive`/`!pat`/`!social`.

    Cheap-skip first (no leading recognized command token -- `None`, zero
    cost), flag check second, real classification last -- same ordering as
    `duel`/`fish`/`eightball`'s own documented rationale. A recognized-but-
    malformed message still produces a reply (`"usage"`) since the caller
    did invoke a known command -- never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head = stripped.partition(" ")[0]
    head_lower = head.lower()

    is_social = head_lower == "!social"
    interaction = _COMMAND_TO_INTERACTION.get(head_lower)
    if not is_social and interaction is None:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    payload: dict[str, Any] = {"channel_id": event.payload.get("channel_id")}
    if is_social:
        command = _resolve_social_command(stripped)
        payload["command"] = command
    else:
        command, target = _resolve_interaction_command(stripped, command_word=head_lower)
        payload["command"] = command
        payload["interaction"] = interaction
        if command == "interact":
            payload["target"] = target

    log.info("social.transform matched", command=payload["command"])
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
    """Fail-loud kv error path: log, reply an error to chat, then re-raise -- see `duel`'s own."""
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    log.error("social.kv_error", op=op, error=case_name)
    await relay.push(
        provider,
        {
            "channel": channel_id,
            "text": "that interaction is temporarily unavailable, try again shortly.",
        },
    )
    raise RuntimeError(f"social kv {op} failed: {case_name}") from exc


async def _kv_get(community: str, key: str, *, provider: str, channel_id: str) -> bytes | None:
    """`community_kv.get`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        return await community_kv.get(community, key)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="get")


async def _kv_increment(
    community: str, key: str, delta: int, *, ttl_seconds: int, provider: str, channel_id: str
) -> int:
    """`community_kv.increment`, fail-loud on a backend error (see `_fail_kv`)."""
    try:
        return await community_kv.increment(community, key, delta, ttl_seconds)
    except Exception as exc:  # noqa: BLE001 -- structurally classified, see `_fail_kv`
        await _fail_kv(exc, provider=provider, channel_id=channel_id, op="increment")


async def _read_int(
    community: str, key: str, *, provider: str, channel_id: str, corrupt_log: str
) -> int:
    """Read an int counter from `kv`, treating a missing/corrupt value as `0` (never a crash)."""
    raw = await _kv_get(community, key, provider=provider, channel_id=channel_id)
    if raw is None:
        return 0
    try:
        return int(raw.decode())
    except (UnicodeDecodeError, ValueError):
        log.error(corrupt_log, community=community)
        return 0


async def _handle_interact(
    interaction: str,
    raw_target: str,
    *,
    community: str,
    actor: str | None,
    username: str,
    provider: str,
    channel_id: str,
) -> tuple[str, str]:
    """Validate the target, update given/received counts, and build the reply.

    Returns `(reply_text, detail)` -- `detail` feeds `DispatchResult.detail`
    as `interact:<interaction>:<detail>` (`"unknown_target"`, `"self"`, or
    `"resolved"`).
    """
    spec = _INTERACTIONS[interaction]
    normalized = _normalize_target(raw_target)
    if normalized is None:
        return (
            f"I don't know who '{raw_target}' is -- try !{spec.command} @username.",
            "unknown_target",
        )

    if actor is not None and normalized.lower() == actor.lower():
        return spec.self_message.format(giver=username, emoji=spec.emoji), "self"

    giver_pseudonym = _pseudonym(actor)
    receiver_pseudonym = _pseudonym(normalized.lower())

    await _kv_increment(
        community,
        _wins_style_key(interaction, "given", giver_pseudonym),
        1,
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )
    await _kv_increment(
        community,
        _wins_style_key(interaction, "received", receiver_pseudonym),
        1,
        ttl_seconds=0,
        provider=provider,
        channel_id=channel_id,
    )

    flavor = random.choice(spec.templates)  # noqa: S311 -- flavor text, not a security decision
    reply = flavor.format(giver=username, target=normalized, emoji=spec.emoji)
    return reply, "resolved"


async def _handle_stats(
    *, community: str, actor: str | None, provider: str, channel_id: str
) -> str:
    """Read+render the caller's own given/received tally across every registered interaction."""
    pseudonym = _pseudonym(actor)
    lines: list[str] = []
    total_given = 0
    total_received = 0
    for name, spec in _INTERACTIONS.items():
        given = await _read_int(
            community,
            _wins_style_key(name, "given", pseudonym),
            provider=provider,
            channel_id=channel_id,
            corrupt_log="social.stats_given_corrupt",
        )
        received = await _read_int(
            community,
            _wins_style_key(name, "received", pseudonym),
            provider=provider,
            channel_id=channel_id,
            corrupt_log="social.stats_received_corrupt",
        )
        total_given += given
        total_received += received
        lines.append(f"{spec.command} {given} given / {received} received")

    if total_given == 0 and total_received == 0:
        return _NO_RECORD_YET
    return "Your social stats -- " + ", ".join(lines) + "."


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: all kv state reads/writes, then relay the reply.

    Raises:
        ValueError: The envelope's payload has no `channel_id`; the
            envelope has no `community` (no tenant-wide fallback -- see
            module docstring's data-scoping section); an unrecognized
            `command` (defensive -- `transform` only ever emits a member of
            `_KNOWN_COMMANDS`); or an `"interact"` command with no
            `target`/`interaction` in its forwarded payload (defensive --
            `transform` always sets both for that command).
        RuntimeError: A `kv` backend call failed (see `_fail_kv` -- a chat
            error reply and an ERROR log line are always emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("social reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized social command: {command!r}")

    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("social.missing_community", command=command)
        raise ValueError("social requires a community context and cannot operate tenant-wide")

    username = envelope.event.actor or "someone"

    if command == "usage":
        interaction = payload.get("interaction")
        if isinstance(interaction, str) and interaction in _INTERACTIONS:
            usage_text = f"Usage: !{_INTERACTIONS[interaction].command} <user>"
        else:
            usage_text = _SOCIAL_USAGE
        await relay.push(provider, {"channel": channel_id, "text": usage_text})
        return DispatchResult(transport=provider, detail="usage")

    if command == "stats":
        reply_text = await _handle_stats(
            community=community,
            actor=envelope.event.actor,
            provider=provider,
            channel_id=channel_id,
        )
        await relay.push(provider, {"channel": channel_id, "text": reply_text})
        log.info("social.dispatch relayed", platform=provider, command=command)
        return DispatchResult(transport=provider, detail="stats")

    # interact
    interaction = payload.get("interaction")
    target = payload.get("target")
    if not isinstance(interaction, str) or interaction not in _INTERACTIONS:
        raise ValueError("social interact requires a known interaction in the forwarded payload")
    if not isinstance(target, str) or not target:
        raise ValueError("social interact requires a target in the forwarded payload")

    reply_text, detail = await _handle_interact(
        interaction,
        target,
        community=community,
        actor=envelope.event.actor,
        username=username,
        provider=provider,
        channel_id=channel_id,
    )
    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("social.dispatch relayed", platform=provider, command=command, detail=detail)
    return DispatchResult(transport=provider, detail=f"interact:{interaction}:{detail}")
