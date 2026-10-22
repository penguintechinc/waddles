"""`!steal @user` -> a risk/reward points-heist game over the shared community economy.

Original Waddles implementation; the *idea* is inspired by PenguinTwitchBot by SuperPenguinTV
(with permission) -- ideas/design only, no code copied.

Commands (grammar via `waddle_sdk.command`; the `@mention` target is not a declared verb and so
is recognized here, from the host-issued `{user:<token>}` placeholder, before the parser runs):

- `!steal @target` -- attempt a steal. Random success/fail (`SUCCESS_PERCENT`).
- `!steal list` -- show the caller's own steal record (attempts/successes).
- `!steal set cooldown <seconds>` -- broadcaster/moderator-only (fail-closed role signal).

## Economy semantics (read this)

The host's `economy.transfer` only ever moves the *invoking actor's* money -- a bundle can pay the
actor's funds TO any member but never pull another member's (`waddle_sdk.economy` docstring,
`actor_mismatch`). A literal target->actor transfer is therefore impossible by design, so:

- **Success**: actor stakes `STAKE` and `wager`s it back with `STAKE + loot` (net `+loot`), where
  `loot` is random, bounded by `min(LOOT_CAP, target_balance, remaining caps)` -- the victim's
  balance is only a *bound*, the victim loses nothing, and the payout is HOUSE-MINTED (subject to
  the host's own daily mint cap/quotas).
- **Failure**: the actor is "caught" and `transfer`s a penalty (`STAKE`) TO the target.

Because winnings are minted, an uncapped steal is +EV money farming, and two colluding accounts
(one holding the "loot bound", one farming it) turn it into a faucet. Three caps bound the mint,
all measured in GROSS loot minted by winning steals (a loss never replenishes any cap):

1. **Per-pair cap** -- at most `PAIR_WINDOW_CAP` loot per (actor -> target) pair per window.
2. **Reciprocal block** -- if the target stole from the actor (any loot reserved or won) in this
   or the previous window, the actor cannot steal back. Both directions of an unordered pair live
   in ONE kv counter (low/high 32-bit halves), so two simultaneous opposite steals cannot both
   pass: the second `kv.increment` sees the first.
3. **Per-actor budget** -- at most `ACTOR_WINDOW_BUDGET` loot per actor per window across all
   targets.

Windows are fixed buckets of `WINDOW_SECONDS` (the bucket index is in the key, so a window resets
without any cleanup and a refused attempt never extends it); a pair can therefore see up to twice
a cap across a bucket boundary. Caps are RESERVED atomically before the roll (`kv.increment` the
worst-case loot, clamp to what is left, give the excess back) and settled after it, so concurrent
steals cannot overshoot. Colluders are *bounded*, not stopped: N sybil accounts still mint
N x budget per window. A truly zero-sum steal needs a host capability that debits the victim
(an actor-from-target transfer); that is a follow-up.

Cap keys are TTL-bounded to their window (`_Bucket`): a budget key lapses at the end of its own
window, a pair key at the end of the NEXT one (the reciprocal check reads it), plus a short settle
grace -- so no cap key outlives its usefulness and, with the host's TTL-reclaiming key quota, none
can hold quota past that. The residual: kv keys are capped at 10,000 per (app, community), and an
attacker with many linked, funded accounts can still create live keys faster than they lapse (one
pair key per distinct target per steal, ~2 h each, plus a replay marker for 15 min). Each account
is bounded by the cooldown (default 60 s, so <= 60 steals/h/account) and, with `set cooldown 0`,
only by the platform's own chat rate limit -- so a moderator who disables the cooldown widens it.
A full quota fails CLOSED (new keys are refused, steals reply "unavailable", no money moves) and
heals as keys lapse. The proper bound is a host-level per-actor kv write-rate/new-key limit
(only the host knows the actor); that is a follow-up, this bundle cannot enforce it.

Fail-loud: no mention, self-target, an unresolved/ambiguous target, an empty victim, a too-poor
actor or an exhausted cap each reply a clear message (`CommandUsageError`) and move no money.
Economy/identity host refusals reply with a stable message and are logged by type only.

Cooldown: `kv.increment` is the only compare-and-swap, so the per-actor cooldown is an
`increment` on a TTL'd key -- exactly one concurrent steal sees `1` and wins the window; every
other attempt is refused. The host re-applies the TTL on every call, so a refused attempt restarts
the window (the reply says so); `set cooldown 0` disables the gate.

Replay safety (at-most-once): the pipeline is at-least-once. The inbound `message_id` (forwarded by
`transform`) is claimed via `waddle_sdk.event_dedupe` -- ONE atomic `kv.increment` on a per-event,
TTL'd marker (`1` = first delivery) -- AFTER the cooldown and BEFORE any mutation, and a redelivery
is skipped silently: no wager, no transfer, no stat or cap change, no reply (the host already
dedupes the money per event, but a re-roll on redelivery could otherwise pick the other money
mover). The increment is the race-free guard (two concurrent copies cannot both see `1`), it needs
no cooldown to be safe, and it has no 16-event horizon. The marker's TTL is the horizon, closed by
an age fence: an event ingested more than `event_dedupe.MAX_EVENT_AGE_SECONDS` ago is dropped
(silently, logged), never processed -- so a redelivery is deduped or dropped, never replayed. A
crash after the claim loses one attempt, never repeats it; a payload with no `message_id`, or no
valid `occurred_at`, fails loud. The claim comes AFTER the cooldown on purpose: an actor on
cooldown never creates a marker, so one chatter's marker rate is bounded by its own cooldown.

State (kv, community-scoped, `.`-separated keys -- never `:`): every key uses a SHA-256 pseudonym
(`_pseudonym`, or a hash of the sorted pair) or a SHA-256 digest of the event id -- no handle, uuid
or platform message id. The actor
handle appears only in the visible chat reply, never in a key or log. Logs carry
`op`/`outcome`/bucket fields only.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.

Output safety: `event.actor` is interpolated into most replies. The host-minted
`{user:<token>}` actor stays live (egress renders the display name); any other value is
untrusted and goes through `waddle_sdk.safe_text.sanitize_user_text` (see `_actor_label`),
as does every other typed echo.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from typing import Any

from waddle_sdk import clock, community_kv, economy, event_dedupe, identity, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text

SPEC = CommandSpec(name="steal")

SUCCESS_PERCENT = 40
STAKE = 10
LOOT_CAP = 100
DEFAULT_COOLDOWN_SECONDS = 60
MIN_COOLDOWN_SECONDS = 0
MAX_COOLDOWN_SECONDS = 3600

#: Anti-collusion window (fixed buckets) and the gross-loot caps enforced inside it; see the
#: module docstring. Conservative defaults: tune with data, never raise without re-reading it.
WINDOW_SECONDS = 3600
PAIR_WINDOW_CAP = 100
ACTOR_WINDOW_BUDGET = 300

#: Seconds a cap key outlives its window so a settle/refund landing just after the bucket rolls
#: still finds it instead of re-creating a stray key.
_SETTLE_GRACE_SECONDS = 60

_COOLDOWN_CONFIG_KEY = "steal.config.cooldown"
_USAGE = "Usage: !steal @user | !steal list | !steal set cooldown <seconds> (set is admin/mod only)"
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can configure !steal"
_KNOWN_COMMANDS = frozenset({"steal", "list", "config_set_cooldown", "usage"})
_DEDUPE_NAMESPACE = "steal"
_RECIPROCAL_MSG = "you can't steal back from someone who stole from you recently"
_PAIR_CAP_MSG = "you've already taken all you can from that member for now -- try again later"
_BUDGET_MSG = "you've hit your steal limit for now -- try again later"

#: A pair counter packs both directions into one kv integer (low/high halves) so a single atomic
#: `increment` both reserves this direction's budget and reveals the opposite direction's.
_HALF_BITS = 32
_HALF_MASK = (1 << _HALF_BITS) - 1
_HALF_LIMIT = 1 << (_HALF_BITS - 1)  # a half this large is corruption, never real traffic


@dataclass(slots=True, frozen=True)
class _Bucket:
    """The fixed anti-collusion window one steal is accounted in, and when its keys may lapse.

    Every cap key is written with a TTL computed from the window it belongs to, so it expires at an
    absolute instant (the end of the window it is last read in, plus a settle grace) instead of a
    fixed span that outlives the window it was created late in.
    """

    index: int
    now_ms: int

    @classmethod
    def at(cls, now_ms: int) -> _Bucket:
        """Return the bucket containing `now_ms`."""
        return cls(index=now_ms // (WINDOW_SECONDS * 1000), now_ms=now_ms)

    def _ttl(self, windows: int) -> int:
        """Seconds from now to the end of window `index + windows - 1`, plus the settle grace."""
        end_ms = (self.index + windows) * WINDOW_SECONDS * 1000
        return max(-(-(end_ms - self.now_ms) // 1000), 1) + _SETTLE_GRACE_SECONDS

    @property
    def budget_ttl(self) -> int:
        """TTL for the per-actor budget key: it is only ever read inside its own window."""
        return self._ttl(1)

    @property
    def pair_ttl(self) -> int:
        """TTL for a pair key: the NEXT window's reciprocal check still reads it as `previous`."""
        return self._ttl(2)


#: The pipeline delivers `event.actor` as its own host-minted placeholder; anchored so ONLY an actor
#: that is exactly one `{user:<token>}` is left live (never one embedded in other text).
_USER_TOKEN_RE = re.compile(r"^\{user:([A-Za-z0-9_:.-]+)\}$")
_FALLBACK_ACTOR = "someone"


def _actor_label(actor: object) -> str:
    """Return a reply-safe label for `event.actor`.

    The host-minted `{user:<token>}` stays live so egress renders the display name. Anything else
    (a raw handle when tokenization is off, a missing actor) is untrusted text and goes through
    `sanitize_user_text` in full, so a platform display name can never forge a mention.
    """
    if not isinstance(actor, str) or not actor.strip():
        return _FALLBACK_ACTOR
    label = actor.strip()
    match = _USER_TOKEN_RE.match(label)
    return sanitize_user_text(label, allow_tokens=(match.group(1),) if match else ())


def _pseudonym(community: str, actor_uuid: str) -> str:
    """Non-reversible per-(community, actor) key component; never the raw handle/uuid."""
    return hashlib.sha256(f"{community}:{actor_uuid}".encode()).hexdigest()


def _cooldown_key(pseudonym: str) -> str:
    """Per-(community, actor) cooldown claim key (an atomic counter with the cooldown as TTL)."""
    return f"steal.cd.{pseudonym}"


def _budget_key(window: int, pseudonym: str) -> str:
    """Per-(community, actor, window) gross-loot budget counter key."""
    return f"steal.budget.{window}.{pseudonym}"


def _pair_key(window: int, pair: str) -> str:
    """Per-(community, unordered pair, window) packed two-direction gross-loot counter key."""
    return f"steal.pair.{window}.{pair}"


def _pair_id(community: str, actor: str, target: str) -> tuple[str, bool]:
    """Return the unordered pair's hashed id and whether `actor` owns the low (first) half."""
    first, second = sorted((actor, target))
    digest = hashlib.sha256(f"{community}|{first}|{second}".encode()).hexdigest()
    return digest, actor == first


def _attempts_key(pseudonym: str) -> str:
    """Per-(community, actor) attempts counter key."""
    return f"steal.attempts.{pseudonym}"


def _wins_key(pseudonym: str) -> str:
    """Per-(community, actor) successful-steals counter key."""
    return f"steal.wins.{pseudonym}"


def _amount_bucket(amount: int) -> str:
    """Coarse amount bucket for PII-free logs."""
    for limit in (10, 100, 1000):
        if amount <= limit:
            return f"le{limit}"
    return "gt1000"


def _caller_role_signal(payload: dict[str, Any]) -> bool | None:
    """`True`/`False` from badge fields (strict `is True`), `None` if absent (absent denies)."""
    if "is_mod" not in payload and "is_broadcaster" not in payload:
        return None
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


def _roll(percent: int) -> bool:
    """Return True with probability `percent`/100."""
    return random.randint(1, 100) <= percent  # noqa: S311 - a game, not security


def _resolve_command(parsed: ParsedCommand | None) -> str:
    """Map a parsed grammar result onto this bundle's command set (else `usage`)."""
    if parsed is None:
        return "usage"
    if parsed.option is None:
        return "usage"  # bare `!steal` has no target
    if parsed.option == "list":
        return "list" if parsed.args is None else "usage"
    if parsed.option == "set":
        return "config_set_cooldown"
    return "usage"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!steal`."""
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.partition(" ")[0].lower() != "!steal":
        return None

    payload: dict[str, Any] = {"channel_id": event.payload.get("channel_id")}
    message_id = event.payload.get("message_id")
    if isinstance(message_id, str):  # replay-dedupe key; `dispatch` refuses a steal without it
        payload["message_id"] = message_id
    tokens = identity.mention_tokens(stripped)
    if tokens:
        payload["command"] = "steal"
        payload["tokens"] = tokens
    else:
        try:
            parsed: ParsedCommand | None = parse_command(stripped, SPEC)
        except CommandUsageError:
            parsed = None
        payload["command"] = _resolve_command(parsed)
        if payload["command"] == "config_set_cooldown" and parsed is not None:
            payload["arg"] = parsed.args
    log.info("steal.transform matched", command=payload["command"])
    if "is_mod" in event.payload:
        payload["is_mod"] = event.payload["is_mod"] is True
    if "is_broadcaster" in event.payload:
        payload["is_broadcaster"] = event.payload["is_broadcaster"] is True
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result (see `gamble`)."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record the provider relayed to and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


def _int_or_none(raw: bytes | None) -> int | None:
    """Decode a kv integer, or None if absent/corrupt (corrupt is logged, never raised)."""
    if raw is None:
        return None
    try:
        return int(raw.decode())
    except (UnicodeDecodeError, ValueError):
        log.error("steal.kv_value_corrupt")
        return None


async def _cooldown_seconds(community: str) -> int:
    """Return the configured cooldown, or the default if unset/corrupt/out of range."""
    value = _int_or_none(await community_kv.get(community, _COOLDOWN_CONFIG_KEY))
    if value is None or not MIN_COOLDOWN_SECONDS <= value <= MAX_COOLDOWN_SECONDS:
        return DEFAULT_COOLDOWN_SECONDS
    return value


async def _claim_cooldown(community: str, pseudonym: str, username: str) -> str | None:
    """Atomically claim the actor's cooldown slot; return a slow-down reply if it is taken.

    `kv.increment` is the only compare-and-swap: exactly one of any number of concurrent attempts
    sees `1` and wins the window, so a check-then-set race cannot let two steals through. The host
    re-applies the TTL on every call, hence a refused attempt restarts the window.
    """
    cooldown = await _cooldown_seconds(community)
    if cooldown <= 0:
        return None
    claims = await community_kv.increment(community, _cooldown_key(pseudonym), 1, cooldown)
    if claims == 1:
        return None
    return f"\U0001f9b9 lay low, {username}! try again in {cooldown}s."


class _StealStateError(RuntimeError):
    """A cap counter holds a value this bundle could not have written -- fail closed."""


def _split(value: int, *, mine_is_low: bool) -> tuple[int, int]:
    """Unpack a pair counter into `(mine, other)` gross loot; corruption raises, never clamps."""
    low, high = value & _HALF_MASK, value >> _HALF_BITS
    if value < 0 or low >= _HALF_LIMIT or high >= _HALF_LIMIT:
        raise _StealStateError("steal pair counter out of range")
    return (low, high) if mine_is_low else (high, low)


def _counter(raw: bytes | None) -> int:
    """Decode a cap counter (absent is 0); anything undecodable raises -- caps fail closed."""
    if raw is None:
        return 0
    try:
        return int(raw.decode())
    except (UnicodeDecodeError, ValueError) as exc:
        raise _StealStateError("steal cap counter is not an integer") from exc


async def _reserve_pair(
    community: str, bucket: _Bucket, pair: str, *, mine_is_low: bool, want: int
) -> int:
    """Atomically reserve up to `want` of this direction's pair cap; return what was granted.

    Refuses (after undoing its own add) when the OPPOSITE direction has any loot reserved or won in
    this or the previous window, or when this direction's cap is spent. The previous window is a
    plain read; the current one is a single `increment`, so two opposite steals racing cannot both
    pass -- whichever lands second sees the first in the packed value.
    """
    prior = _counter(await community_kv.get(community, _pair_key(bucket.index - 1, pair)))
    if _split(prior, mine_is_low=mine_is_low)[1] > 0:
        raise CommandUsageError(_RECIPROCAL_MSG)
    shift = 0 if mine_is_low else _HALF_BITS
    key, ttl = _pair_key(bucket.index, pair), bucket.pair_ttl
    after = await community_kv.increment(community, key, want << shift, ttl)
    mine_after, other = _split(after, mine_is_low=mine_is_low)
    if other > 0:
        await community_kv.increment(community, key, -(want << shift), ttl)
        raise CommandUsageError(_RECIPROCAL_MSG)
    granted = max(min(want, PAIR_WINDOW_CAP - (mine_after - want)), 0)
    if granted < want:
        await community_kv.increment(community, key, -((want - granted) << shift), ttl)
    if granted == 0:
        raise CommandUsageError(_PAIR_CAP_MSG)
    return granted


async def _reserve_budget(community: str, bucket: _Bucket, pseudonym: str, want: int) -> int:
    """Atomically reserve up to `want` of the actor's per-window budget; return what was granted."""
    key, ttl = _budget_key(bucket.index, pseudonym), bucket.budget_ttl
    after = await community_kv.increment(community, key, want, ttl)
    if after < want:
        raise _StealStateError("steal budget counter went negative")
    granted = max(min(want, ACTOR_WINDOW_BUDGET - (after - want)), 0)
    if granted < want:
        await community_kv.increment(community, key, -(want - granted), ttl)
    return granted


async def _release(
    community: str, bucket: _Bucket, pair: str, pseudonym: str, *, mine_is_low: bool, amount: int
) -> None:
    """Give back `amount` of a reservation on BOTH the pair and the budget counters."""
    if amount <= 0:
        return
    shift = 0 if mine_is_low else _HALF_BITS
    await community_kv.increment(
        community, _pair_key(bucket.index, pair), -(amount << shift), bucket.pair_ttl
    )
    await community_kv.increment(
        community, _budget_key(bucket.index, pseudonym), -amount, bucket.budget_ttl
    )


async def _resolve_parties(tokens: list[str]) -> tuple[str, str]:
    """Resolve actor + first mention to community uuids; usage-error on any refusal."""
    try:
        actor = await identity.resolve_actor()
    except identity.IdentityError as exc:
        log.info("steal.actor_unresolved", error=type(exc).__name__)
        raise CommandUsageError("link your account before stealing") from exc
    try:
        target = await identity.resolve_mention(tokens[0])
    except identity.AmbiguousError as exc:
        raise CommandUsageError("that name matches more than one member") from exc
    except identity.IdentityError as exc:
        log.info("steal.target_unresolved", error=type(exc).__name__)
        raise CommandUsageError("couldn't find that member to steal from") from exc
    if target == actor:
        raise CommandUsageError("you can't steal from yourself")
    return actor, target


async def _reserve_loot(
    community: str, bucket: _Bucket, pair: str, pseudonym: str, *, mine_is_low: bool, want: int
) -> int:
    """Reserve up to `want` gross loot on the pair AND the actor's budget; return what is granted.

    Raises `CommandUsageError` (reply text) when the pair is blocked/capped or the budget is spent;
    every partial reservation is given back before raising.
    """
    pair_granted = await _reserve_pair(community, bucket, pair, mine_is_low=mine_is_low, want=want)
    granted = await _reserve_budget(community, bucket, pseudonym, pair_granted)
    if granted < pair_granted:  # the budget was the tighter bound: return the pair's surplus
        shift = 0 if mine_is_low else _HALF_BITS
        key = _pair_key(bucket.index, pair)
        await community_kv.increment(
            community, key, -((pair_granted - granted) << shift), bucket.pair_ttl
        )
    if granted == 0:
        raise CommandUsageError(_BUDGET_MSG)
    return granted


async def _handle_steal(
    tokens: list[str],
    *,
    community: str,
    username: str,
    payload: dict[str, Any],
    platform: str,
    occurred_at: str,
) -> str | None:
    """Resolve, replay-guard, cooldown-gate, cap, roll and settle one steal; return the reply.

    Returns `None` for a redelivered or too-old event: nothing is mutated or sent.
    """
    if not tokens:
        raise CommandUsageError(_USAGE)
    actor, target = await _resolve_parties(tokens)
    pseudonym = _pseudonym(community, actor)
    event_id = event_dedupe.event_id_from_payload(payload, platform=platform)
    if not event_dedupe.is_fresh(occurred_at):
        log.info("steal.stale_event_skipped", op="fence")
        return None
    if await event_dedupe.already_seen(community, namespace=_DEDUPE_NAMESPACE, event_id=event_id):
        log.info("steal.redelivery_skipped", op="seen")
        return None
    try:
        actor_balance, target_balance = await economy.balance(actor), await economy.balance(target)
        if target_balance <= 0:
            raise CommandUsageError("that member has nothing to steal")
        cap = await economy.max_bet(actor)
        stake = min(STAKE, cap)
        if actor_balance < STAKE or stake < 1:
            raise CommandUsageError(f"you need at least {STAKE} points to try a steal")
        slow = await _claim_cooldown(community, pseudonym, username)
        if slow is not None:
            return slow
        if not await event_dedupe.claim(community, namespace=_DEDUPE_NAMESPACE, event_id=event_id):
            log.info("steal.redelivery_skipped", op="claim")
            return None
        bucket = _Bucket.at(clock.now_millis())
        pair, mine_is_low = _pair_id(community, actor, target)
        granted = await _reserve_loot(
            community,
            bucket,
            pair,
            pseudonym,
            mine_is_low=mine_is_low,
            want=min(LOOT_CAP, target_balance),
        )
        await community_kv.increment(community, _attempts_key(pseudonym), 1, 0)
        won = _roll(SUCCESS_PERCENT)
        loot = random.randint(1, granted) if won else 0  # noqa: S311 - a game
        try:
            if won:
                new_balance = await economy.wager(actor, stake, stake + loot)
            else:
                await economy.transfer(actor, target, stake)
        except economy.BackendError:
            raise  # may have committed: keep the reservation, never refund an ambiguous mint
        except economy.EconomyError:
            await _release(
                community, bucket, pair, pseudonym, mine_is_low=mine_is_low, amount=granted
            )
            raise
        await _release(
            community, bucket, pair, pseudonym, mine_is_low=mine_is_low, amount=granted - loot
        )
        if won:
            await community_kv.increment(community, _wins_key(pseudonym), 1, 0)
            log.info("steal.settled", outcome="win", amount_bucket=_amount_bucket(loot))
            return f"\U0001f9b9 {username} stole {loot} points! (balance: {new_balance})"
        log.info("steal.settled", outcome="caught", amount_bucket=_amount_bucket(stake))
        return f"\U0001f6a8 {username} got caught and paid {stake} points to the victim."
    except economy.InsufficientFundsError as exc:
        raise CommandUsageError(f"you only have {exc.balance} points") from exc
    except economy.OverCapError as exc:
        raise CommandUsageError(f"that exceeds the economy cap ({exc.cap})") from exc
    except economy.EconomyError as exc:
        log.error("steal.economy_error", error=type(exc).__name__)
        raise CommandUsageError("stealing is temporarily unavailable") from exc


async def _handle_list(*, community: str) -> str:
    """Render the caller's own attempts/successes record."""
    try:
        actor = await identity.resolve_actor()
    except identity.IdentityError as exc:
        raise CommandUsageError("link your account first") from exc
    pseudonym = _pseudonym(community, actor)
    attempts = _int_or_none(await community_kv.get(community, _attempts_key(pseudonym))) or 0
    wins = _int_or_none(await community_kv.get(community, _wins_key(pseudonym))) or 0
    if attempts == 0:
        return "You haven't tried stealing yet -- try !steal @someone!"
    return f"Steal attempts: {attempts}. Successful: {wins}."


async def _handle_set_cooldown(arg: str | None, *, community: str) -> str:
    """Parse+apply `set cooldown <seconds>`'s free-text tail."""
    parts = (arg or "").split()
    if len(parts) != 2 or parts[0].lower() != "cooldown":
        return _USAGE
    try:
        seconds = int(parts[1])
    except ValueError:
        log.debug("steal.set_cooldown.rejected", reason="not_a_whole_number")
        return f"'{sanitize_user_text(parts[1])}' isn't a whole number of seconds"
    if not MIN_COOLDOWN_SECONDS <= seconds <= MAX_COOLDOWN_SECONDS:
        return f"cooldown must be between {MIN_COOLDOWN_SECONDS} and {MAX_COOLDOWN_SECONDS} seconds"
    await community_kv.set(community, _COOLDOWN_CONFIG_KEY, str(seconds).encode(), 0)
    return f"steal cooldown set to {seconds}s"


async def _reply_for(
    command: str,
    payload: dict[str, Any],
    *,
    community: str,
    username: str,
    platform: str,
    occurred_at: str,
) -> str | None:
    """Compute the chat reply for one recognized command (`None` = a skipped redelivery)."""
    if command == "usage":
        return _USAGE
    if command == "config_set_cooldown":
        if _caller_role_signal(payload) is not True:
            log.info("steal.config_denied", command=command)
            return _PERMISSION_DENIED_MSG
        arg = payload.get("arg")
        return await _handle_set_cooldown(
            arg if isinstance(arg, str) else None, community=community
        )
    if command == "list":
        return await _handle_list(community=community)
    tokens = payload.get("tokens")
    return await _handle_steal(
        [t for t in tokens if isinstance(t, str)] if isinstance(tokens, list) else [],
        community=community,
        username=username,
        payload=payload,
        platform=platform,
        occurred_at=occurred_at,
    )


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: settle via the economy SDK, then relay the reply.

    Raises:
        ValueError: No `channel_id`, no `community`, or an unrecognized command.
        RuntimeError: A kv backend call failed (error reply + ERROR log emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("steal reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized steal command: {command!r}")
    provider = envelope.event.platform
    community = envelope.community
    if not community:
        log.error("steal.missing_community", command=command)
        raise ValueError("steal requires a community context and cannot operate tenant-wide")

    username = _actor_label(envelope.event.actor)
    try:
        reply = await _reply_for(
            command,
            payload,
            community=community,
            username=username,
            platform=provider,
            occurred_at=envelope.event.occurred_at,
        )
    except CommandUsageError as exc:
        reply = str(exc)
    except event_dedupe.EventIdError as exc:
        log.error("steal.missing_event_id", command=command)
        await relay.push(provider, {"channel": channel_id, "text": "stealing is unavailable."})
        raise RuntimeError(
            "steal requires the inbound message_id and a valid occurred_at to be replay-safe"
        ) from exc
    except Exception as exc:
        log.error("steal.kv_error", error=type(getattr(exc, "value", exc)).__name__)
        await relay.push(provider, {"channel": channel_id, "text": "stealing is unavailable."})
        raise RuntimeError("steal state backend failed") from exc

    if reply is None:
        log.info("steal.dispatch skipped duplicate", platform=provider, command=command)
        return DispatchResult(transport=provider, detail="duplicate")
    await relay.push(provider, {"channel": channel_id, "text": reply})
    log.info("steal.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
