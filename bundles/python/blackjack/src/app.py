"""`!blackjack <stake>` / `!hit` / `!stand` -> a solo blackjack game against the dealer-bot.

Original Waddles implementation (rules, text and code written fresh for this bundle).

Commands (grammar via `waddle_sdk.command`; the `<stake>` shape is recognized here before the
parser runs, same as `defuse`):

- `!blackjack <stake>` / `!blackjack all` -- deal a hand (`all` = the host's `max_bet`, capped by
  the community's max bet).
- `!hit` / `!stand` -- continue the open hand.
- `!blackjack` -- show the open hand (or collect a won-but-unpaid one, or print usage).
- `!blackjack set minbet|maxbet|timeout <n>` -- broadcaster/moderator only (fail-closed badges).

Rules: one infinite deck (every draw independent), the dealer stands on every 17, a natural 21
pays 3:2 (a dealer natural against a player natural is a push), no split/double/insurance. An
abandoned hand (no action for `timeout` seconds, default 180) is forfeited.

**Where the code runs.** The `economy` and `identity` host imports are served by the PROCESS stage
only (`core/svc_process::capabilities`; `svc_action` has no handler for either -- its `db` arm
takes the `economy.*` op for a `storage.tables` call), and `relay` by the ACTION stage only. So
every game step runs inside `transform` and `dispatch` just relays the reply `transform` built
(the `greeter` shape) -- a hand played from `dispatch` would hit `unavailable` on a real host.

**Money (escrow without a house account).** The economy is mint/burn only: `economy.wager(user,
stake, payout)` always debits `stake >= 1` and credits `payout <= stake * 100` in one atomic step.
Settling a hand only at the end would let a player spend or transfer their stake away mid-hand
and dodge every loss, so the stake is escrowed at the deal: `stake - reserve` is burned
(`wager(actor, stake - reserve, 0)`) and the player keeps a small `reserve` (`ceil(max return /
100)`, at most 13 points at the 500 cap) that the final settlement debits as its own stake:
`wager(actor, reserve, total_return)` where `total_return` is `0` (loss), `stake` (push), `2 *
stake` (win) or `stake + stake * 3 // 2` (natural). The player's net is exactly `total_return -
stake`, the payout counts against the daily mint cap like any other (`DeniedError`
`quota_exceeded`), and the only thing a player can still dodge is the `reserve` (<= 2.6% of the
stake) -- spending it just forfeits the winnings it was meant to settle.

**Settlement is at-most-once.** The hand record is deleted BEFORE the settling wager, so a crash
between the two loses a payout but can never pay one twice. A refusal the host guarantees did not
apply (insufficient reserve, over the cap, daily mint cap, denied) re-stores the hand as `due`
(kept `DUE_TTL_SECONDS`) and the player collects it with `!blackjack` later; `BackendError` (the
one outcome that may or may not have committed) is never retried -- it is logged loud and the
reply says the payout status is unclear.

**Concurrency and replay.** `kv.increment` is the only compare-and-swap, so a per-player lock
(`increment` on a TTL'd key; exactly one caller sees `1`) serializes every mutating step and the
hand is re-read under it -- two concurrent `!hit`s cannot both draw (the loser gets a "still
processing" reply). The pipeline is at-least-once: the inbound `message_id` is claimed through
`waddle_sdk.event_dedupe` (one atomic TTL'd `increment`) AFTER the read-only prechecks and BEFORE
any stake, state change or payout; a redelivery is dropped silently, a too-old event is dropped
by the age fence, and a payload with no `message_id`/valid `occurred_at` fails loud.

**State (kv, community-scoped, `.`-separated, never `:`):** `blackjack.hand.<p>`,
`blackjack.lock.<p>`, `blackjack.seen.<d>` and `blackjack.cfg.<setting>`, where `p =
sha256(community:actor_uuid)` and `d` is a truncated hash of the platform message id. Every key is
TTL'd except the three mod settings, so no per-player key outlives its hand. Logs carry only
`op`/`outcome`/bucket/error-class fields -- never a handle, uuid, card or message text.
Activation-gated only -- no feature flag.

**Output safety:** `event.actor` is interpolated into replies. The host-minted `{user:<token>}`
stays live (egress renders the display name); any other value goes through
`waddle_sdk.safe_text.sanitize_user_text` (see `_actor_label`). Replies otherwise carry only
integers, card glyphs and static text; parser errors are never echoed.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from waddle_sdk import clock, community_kv, economy, event_dedupe, identity, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, ParsedCommand, parse_command
from waddle_sdk.flask_core import get_bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text

SPEC = CommandSpec(name="blackjack")
_HIT_SPEC = CommandSpec(name="hit")
_STAND_SPEC = CommandSpec(name="stand")
_HEADS = frozenset({"!blackjack", "!hit", "!stand"})

DEFAULT_MIN_BET = 1
DEFAULT_MAX_BET = 100
#: Mirrors the manifest's `economy.wager` `params.max_bet` (pinned by tests/test_manifest_bounds).
MANIFEST_MAX_BET = 500
DEFAULT_TIMEOUT_SECONDS = 180
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 3600
#: A hand that was dealt but whose payout the host refused is kept this long for `!blackjack`.
DUE_TTL_SECONDS = 86_400
#: A `dealing` record only exists inside one invocation; its TTL bounds a crashed deal.
DEALING_TTL_SECONDS = 60
#: Slack on the hand key's TTL past `expires_ms`, so the forfeit notice can still be shown.
HAND_TTL_GRACE_SECONDS = 60
LOCK_TTL_SECONDS = 15

PHASE_DEALING = "dealing"
PHASE_PLAYING = "playing"
PHASE_DUE = "due"
_PHASES = frozenset({PHASE_DEALING, PHASE_PLAYING, PHASE_DUE})

_DEDUPE_NAMESPACE = "blackjack"
_HAND_VERSION = 1
_MAX_CARDS = 12
_DECK_SIZE = 52
_SUITS = "♠♥♦♣"
_RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
_DEALER_STANDS_ON = 17
_USAGE = (
    "Usage: !blackjack <stake|all> to deal, then !hit or !stand "
    "(mods: !blackjack set minbet|maxbet|timeout <n>)"
)
_PERMISSION_DENIED_MSG = "only moderators/broadcasters can configure !blackjack"
_BUSY_MSG = "easy, your last move is still being processed -- try again in a moment."
_UNAVAILABLE_MSG = "blackjack is temporarily unavailable"
_NO_HAND_MSG = "you have no open hand -- start one with !blackjack <stake>."

_SYSTEM_RANDOM = random.SystemRandom()

#: The pipeline delivers `event.actor` as its own host-minted placeholder; anchored so ONLY an actor
#: that is exactly one `{user:<token>}` is left live (never one embedded in other text).
_USER_TOKEN_RE = re.compile(r"^\{user:([A-Za-z0-9_:.-]+)\}$")
_FALLBACK_ACTOR = "someone"
_SET_KEYS = frozenset({"minbet", "maxbet", "timeout"})


@dataclass(slots=True)
class Hand:
    """One player's open (or awaiting-payout) hand, persisted as a single kv value."""

    stake: int
    reserve: int
    player: list[int]
    dealer: list[int]
    phase: str
    expires_ms: int
    due: int = 0

    def to_bytes(self) -> bytes:
        """Serialize to the compact JSON stored in kv."""
        return json.dumps(
            {
                "v": _HAND_VERSION,
                "s": self.stake,
                "r": self.reserve,
                "p": self.player,
                "d": self.dealer,
                "ph": self.phase,
                "x": self.expires_ms,
                "t": self.due,
            },
            separators=(",", ":"),
        ).encode()


@dataclass(slots=True, frozen=True)
class Limits:
    """The community's bet limits and abandoned-hand timeout (mod-set, defaults otherwise)."""

    min_bet: int = DEFAULT_MIN_BET
    max_bet: int = DEFAULT_MAX_BET
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


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


def _hand_key(pseudonym: str) -> str:
    """Per-(community, actor) open-hand key."""
    return f"blackjack.hand.{pseudonym}"


def _lock_key(pseudonym: str) -> str:
    """Per-(community, actor) action-lock key (an atomic counter with a short TTL)."""
    return f"blackjack.lock.{pseudonym}"


def _cfg_key(setting: str) -> str:
    """Community setting key (`minbet` / `maxbet` / `timeout`)."""
    return f"blackjack.cfg.{setting}"


def _amount_bucket(amount: int) -> str:
    """Coarse amount bucket for PII-free logs."""
    for limit in (10, 100, 1000):
        if amount <= limit:
            return f"le{limit}"
    return "gt1000"


def _is_mod(payload: dict[str, Any]) -> bool:
    """True only for a real `True` badge (strict `is True`; absent/stringy badges deny)."""
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


def _draw() -> int:
    """Draw one card (0..51) from the infinite deck with the host-seeded system RNG.

    `random.SystemRandom` reads the OS entropy source on every call and keeps no generator state,
    so nothing can be frozen into a wizened component snapshot (the module-level `random`
    generator is seeded once at import).
    """
    return _SYSTEM_RANDOM.randrange(_DECK_SIZE)


def _card(card: int) -> str:
    """Render one card, e.g. `A♠`."""
    return f"{_RANKS[card % 13]}{_SUITS[card // 13]}"


def _cards(cards: list[int]) -> str:
    """Render a hand as space-separated cards."""
    return " ".join(_card(c) for c in cards)


def _total(cards: list[int]) -> int:
    """Blackjack total: aces count 11 unless that busts, face cards 10."""
    total = sum(min(c % 13 + 1, 10) for c in cards)
    if any(c % 13 == 0 for c in cards) and total + 10 <= 21:
        total += 10
    return total


def _is_natural(cards: list[int]) -> bool:
    """A two-card 21."""
    return len(cards) == 2 and _total(cards) == 21


def _reserve(stake: int) -> int:
    """Points the player keeps during a hand so the settlement wager has a stake to debit.

    `economy.wager` caps a payout at `MAX_PAYOUT_MULTIPLE` x its stake, so the settlement stake
    must be at least `ceil(largest possible return / 100)` (at least 1).
    """
    top_return = stake + stake * 3 // 2
    return max(1, -(-top_return // economy.MAX_PAYOUT_MULTIPLE))


def _return_for(player: list[int], dealer: list[int], stake: int) -> tuple[str, int]:
    """Outcome label and the total points returned to the player for a finished hand."""
    p, d = _total(player), _total(dealer)
    if p > 21:
        return "bust", 0
    if _is_natural(player):
        return ("push", stake) if _is_natural(dealer) else ("blackjack", stake + stake * 3 // 2)
    if _is_natural(dealer):
        return "dealer_blackjack", 0
    if d > 21:
        return "dealer_bust", stake * 2
    if p > d:
        return "win", stake * 2
    if p == d:
        return "push", stake
    return "lose", 0


def _int_or_none(raw: bytes | None) -> int | None:
    """Decode a kv integer, or None if absent/corrupt (corrupt is logged, never raised)."""
    if raw is None:
        return None
    try:
        return int(raw.decode())
    except (UnicodeDecodeError, ValueError):
        log.error("blackjack.kv_value_corrupt", op="int")
        return None


def _hand_from_bytes(raw: bytes) -> Hand | None:
    """Parse a stored hand; a corrupt or out-of-range record is logged and treated as absent."""
    try:
        data = json.loads(raw.decode())
        hand = Hand(
            stake=int(data["s"]),
            reserve=int(data["r"]),
            player=[int(c) for c in data["p"]],
            dealer=[int(c) for c in data["d"]],
            phase=str(data["ph"]),
            expires_ms=int(data["x"]),
            due=int(data["t"]),
        )
        valid = (
            data["v"] == _HAND_VERSION
            and 1 <= hand.stake <= MANIFEST_MAX_BET
            and 1 <= hand.reserve <= economy.MAX_PAYOUT_MULTIPLE
            and hand.phase in _PHASES
            and hand.due >= 0
            and 1 <= len(hand.player) <= _MAX_CARDS
            and 1 <= len(hand.dealer) <= _MAX_CARDS
            and all(0 <= c < _DECK_SIZE for c in (*hand.player, *hand.dealer))
        )
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        log.error("blackjack.hand_corrupt", op="load")
        return None
    return hand


async def _load_hand(community: str, pseudonym: str) -> Hand | None:
    """Read the player's stored hand, or None (absent or corrupt)."""
    raw = await community_kv.get(community, _hand_key(pseudonym))
    return None if raw is None else _hand_from_bytes(raw)


async def _store_hand(community: str, pseudonym: str, hand: Hand, ttl_seconds: int) -> None:
    """Persist `hand` under its key with `ttl_seconds`."""
    await community_kv.set(community, _hand_key(pseudonym), hand.to_bytes(), ttl_seconds)


def _playing_ttl(hand: Hand) -> int:
    """TTL for a playing hand: the time left to `expires_ms` plus the forfeit-notice grace."""
    remaining = max(0, (hand.expires_ms - clock.now_millis() + 999) // 1000)
    return remaining + HAND_TTL_GRACE_SECONDS


async def _get_limits(community: str) -> Limits:
    """Read the mod-set limits; a missing/corrupt/out-of-range value falls back to its default."""
    min_bet = _int_or_none(await community_kv.get(community, _cfg_key("minbet")))
    max_bet = _int_or_none(await community_kv.get(community, _cfg_key("maxbet")))
    timeout = _int_or_none(await community_kv.get(community, _cfg_key("timeout")))
    if min_bet is None or not 1 <= min_bet <= MANIFEST_MAX_BET:
        min_bet = DEFAULT_MIN_BET
    if max_bet is None or not 1 <= max_bet <= MANIFEST_MAX_BET:
        max_bet = DEFAULT_MAX_BET
    if timeout is None or not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
        timeout = DEFAULT_TIMEOUT_SECONDS
    return Limits(min_bet=min(min_bet, max_bet), max_bet=max_bet, timeout_seconds=timeout)


async def _actor() -> str:
    """Resolve the invoking actor's community uuid; usage-error if unlinked."""
    try:
        return await identity.resolve_actor()
    except (identity.NotLinkedError, identity.NotAMemberError) as exc:
        log.info("blackjack.actor_unresolved", error=type(exc).__name__)
        raise CommandUsageError("link your account before playing") from exc
    except identity.IdentityError as exc:
        log.error("blackjack.identity_error", error=type(exc).__name__)
        raise CommandUsageError(_UNAVAILABLE_MSG) from exc


async def _acquire(community: str, pseudonym: str) -> bool:
    """Take the per-player action lock; exactly one concurrent caller sees `increment == 1`."""
    return await community_kv.increment(community, _lock_key(pseudonym), 1, LOCK_TTL_SECONDS) == 1


async def _release(community: str, pseudonym: str) -> None:
    """Drop the action lock; a failure is logged and the lock's TTL frees it."""
    try:
        await community_kv.delete(community, _lock_key(pseudonym))
    except Exception as exc:
        log.error("blackjack.lock_release_failed", error=type(getattr(exc, "value", exc)).__name__)


async def _resolve_stake(arg: str, actor: str, limits: Limits) -> int:
    """Turn the stake argument into a validated stake or a `CommandUsageError` reply."""
    if arg == "all":
        stake = min(await economy.max_bet(actor), limits.max_bet)
        if stake <= 0:
            raise CommandUsageError("you have no points to bet")
    elif len(arg) > len(str(MANIFEST_MAX_BET)):
        raise CommandUsageError(f"maximum bet is {limits.max_bet} points.")
    else:
        stake = int(arg)
    if stake < limits.min_bet:
        raise CommandUsageError(f"minimum bet is {limits.min_bet} points.")
    if stake > limits.max_bet:
        raise CommandUsageError(f"maximum bet is {limits.max_bet} points.")
    held = await economy.balance(actor)
    if stake > held:
        raise CommandUsageError(f"you only have {held} points.")
    cap = await economy.max_bet(actor)
    if stake > cap:
        raise CommandUsageError(f"max bet is {cap} points.")
    return stake


def _view(hand: Hand, username: str, lead: str) -> str:
    """The in-progress hand line: the player's cards and the dealer's up card."""
    return (
        f"\U0001f0cf {username} {lead}: {_cards(hand.player)} ({_total(hand.player)}) "
        f"| dealer shows {_card(hand.dealer[0])} -- !hit or !stand"
    )


def _result_line(hand: Hand, username: str, label: str, balance: int | None) -> str:
    """The finished-hand line for `label` (the outcome from `_return_for`)."""
    table = (
        f"{_cards(hand.player)} ({_total(hand.player)}) "
        f"vs {_cards(hand.dealer)} ({_total(hand.dealer)})"
    )
    tail = "" if balance is None else f" Balance: {balance}"
    if label == "bust":
        return f"\U0001f4a5 {username} busts: {table}. -{hand.stake} points.{tail}"
    if label == "blackjack":
        gain = hand.stake * 3 // 2
        return f"\U0001f0cf BLACKJACK! {username}: {table}. +{gain} points.{tail}"
    if label == "dealer_blackjack":
        return f"\U0001f0cf dealer has blackjack: {username}, {table}. -{hand.stake} points.{tail}"
    if label == "dealer_bust":
        return f"\U0001f0cf dealer busts! {username} wins: {table}. +{hand.stake} points.{tail}"
    if label == "win":
        return f"\U0001f0cf {username} wins: {table}. +{hand.stake} points.{tail}"
    if label == "push":
        return f"\U0001f0cf push: {username}, {table}. Stake returned.{tail}"
    return f"\U0001f0cf {username} loses: {table}. -{hand.stake} points.{tail}"


def _deferred_line(username: str, hand: Hand, total_return: int, reason: str) -> str:
    """The reply for a settled hand whose payout the host refused (kept for `!blackjack`)."""
    net = total_return - hand.stake
    outcome = f"won ({net:+d} points)" if net > 0 else "settled (your stake comes back)"
    return (
        f"\U0001f0cf {username}, your hand is {outcome} but the payout could not be paid right "
        f"now ({reason}). Use !blackjack to collect it later."
    )


def _code_of(exc: economy.EconomyError) -> str:
    """The host's stable denial code for logs (`""` when the error carries none)."""
    return exc.code if isinstance(exc, economy.DeniedError) else ""


def _refusal_reason(exc: economy.EconomyError) -> str:
    """A static, user-safe reason for a refused settlement wager."""
    if isinstance(exc, economy.InsufficientFundsError):
        return "keep a few points in your balance"
    if isinstance(exc, economy.OverCapError):
        return "over the payout cap"
    if isinstance(exc, economy.DeniedError) and exc.code == "quota_exceeded":
        return "daily payout limit reached"
    return "the economy refused it"


async def _settle(
    community: str,
    pseudonym: str,
    actor: str,
    hand: Hand,
    total_return: int,
    label: str,
    username: str,
) -> str:
    """Settle a finished hand at-most-once and return the chat reply.

    The hand record is deleted first (so a crash can lose a payout but never repeat one); a loss
    then burns the reserve best-effort, and a win/push/natural runs `wager(actor, reserve,
    total_return)`. A refusal the host guarantees did not apply re-stores the hand as `due`.
    """
    await community_kv.delete(community, _hand_key(pseudonym))
    if total_return == 0:
        balance: int | None = None
        try:
            balance = await economy.wager(actor, hand.reserve, 0)
        except economy.EconomyError as exc:
            log.warn("blackjack.reserve_burn_skipped", error=type(exc).__name__)
        log.info("blackjack.settled", outcome=label, amount_bucket=_amount_bucket(hand.stake))
        return _result_line(hand, username, label, balance)
    try:
        balance = await economy.wager(actor, hand.reserve, total_return)
    except economy.BackendError as exc:
        log.error("blackjack.payout_ambiguous", error=type(exc).__name__)
        return (
            f"\U0001f0cf {username}, your hand is settled but the payout status is unclear -- "
            "ask a moderator to check your balance."
        )
    except economy.EconomyError as exc:
        log.warn("blackjack.payout_deferred", error=type(exc).__name__, code=_code_of(exc))
        due = Hand(
            stake=hand.stake,
            reserve=hand.reserve,
            player=hand.player,
            dealer=hand.dealer,
            phase=PHASE_DUE,
            expires_ms=0,
            due=total_return,
        )
        try:
            await _store_hand(community, pseudonym, due, DUE_TTL_SECONDS)
        except Exception as store_exc:
            log.error(
                "blackjack.due_store_failed",
                error=type(getattr(store_exc, "value", store_exc)).__name__,
            )
            return (
                f"\U0001f0cf {username}, your hand is won but the payout could not be paid or "
                "saved -- ask a moderator."
            )
        return _deferred_line(username, hand, total_return, _refusal_reason(exc))
    log.info("blackjack.settled", outcome=label, amount_bucket=_amount_bucket(hand.stake))
    return _result_line(hand, username, label, balance)


async def _finish_dealer(
    community: str, pseudonym: str, actor: str, hand: Hand, username: str
) -> str:
    """The dealer draws to 17 (stands on all 17s), then the hand is settled."""
    while _total(hand.dealer) < _DEALER_STANDS_ON and len(hand.dealer) < _MAX_CARDS:
        hand.dealer.append(_draw())
    label, total_return = _return_for(hand.player, hand.dealer, hand.stake)
    return await _settle(community, pseudonym, actor, hand, total_return, label, username)


def _forfeit_note(username: str) -> str:
    """The notice for an abandoned hand that timed out."""
    return f"{username}, your last hand timed out and was forfeited."


async def _start_hand(
    community: str, pseudonym: str, actor: str, stake: int, limits: Limits, username: str
) -> str:
    """Deal a new hand under the lock: store it as `dealing`, escrow the stake, then play on.

    The record is written BEFORE the burn so a full kv quota fails before any money moves; the
    burn failing deletes it. Naturals settle immediately.
    """
    reserve = _reserve(stake)
    hand = Hand(
        stake=stake,
        reserve=reserve,
        player=[_draw(), _draw()],
        dealer=[_draw(), _draw()],
        phase=PHASE_DEALING,
        expires_ms=clock.now_millis() + limits.timeout_seconds * 1000,
    )
    await _store_hand(community, pseudonym, hand, DEALING_TTL_SECONDS)
    burn = stake - reserve
    if burn > 0:
        try:
            await economy.wager(actor, burn, 0)
        except economy.InsufficientFundsError as exc:
            await community_kv.delete(community, _hand_key(pseudonym))
            raise CommandUsageError(f"{username}, you only have {exc.balance} points.") from exc
        except economy.OverCapError as exc:
            await community_kv.delete(community, _hand_key(pseudonym))
            raise CommandUsageError(f"max bet is {exc.cap} points.") from exc
        except economy.EconomyError as exc:
            await community_kv.delete(community, _hand_key(pseudonym))
            log.error("blackjack.deal_burn_failed", error=type(exc).__name__, code=_code_of(exc))
            raise CommandUsageError(_UNAVAILABLE_MSG) from exc
    hand.phase = PHASE_PLAYING
    await _store_hand(community, pseudonym, hand, _playing_ttl(hand))
    log.info("blackjack.dealt", amount_bucket=_amount_bucket(stake))
    if _is_natural(hand.player) or _is_natural(hand.dealer):
        label, total_return = _return_for(hand.player, hand.dealer, stake)
        return await _settle(community, pseudonym, actor, hand, total_return, label, username)
    return _view(hand, username, f"bets {stake}")


async def _collect_due(
    community: str, pseudonym: str, actor: str, hand: Hand, username: str
) -> str:
    """Retry paying a won-but-unpaid hand (under the lock), at-most-once like any settlement."""
    label, _ = _return_for(hand.player, hand.dealer, hand.stake)
    return await _settle(community, pseudonym, actor, hand, hand.due, label, username)


async def _locked(community: str, pseudonym: str, body: Callable[[], Awaitable[str]]) -> str:
    """Run `body()` under the per-player lock, or answer `_BUSY_MSG` when another step holds it."""
    if not await _acquire(community, pseudonym):
        log.info("blackjack.busy", op="lock")
        return _BUSY_MSG
    try:
        return await body()
    finally:
        await _release(community, pseudonym)


async def _cmd_deal(arg: str, event: PlatformEvent, community: str, username: str) -> str | None:
    """`!blackjack <stake|all>`: replay-guard, validate, claim, then deal under the lock."""
    actor = await _actor()
    pseudonym = _pseudonym(community, actor)
    event_id = event_dedupe.event_id_from_payload(event.payload, platform=event.platform)
    if not event_dedupe.is_fresh(event.occurred_at):
        log.info("blackjack.stale_event_skipped", op="fence")
        return None
    if await event_dedupe.already_seen(community, namespace=_DEDUPE_NAMESPACE, event_id=event_id):
        log.info("blackjack.redelivery_skipped", op="seen")
        return None
    limits = await _get_limits(community)
    existing = await _load_hand(community, pseudonym)
    if existing is not None and existing.phase == PHASE_PLAYING:
        if clock.now_millis() < existing.expires_ms:
            return _view(existing, username, "has a hand open")
    due_pending = existing is not None and existing.phase == PHASE_DUE
    stake = 0 if due_pending else await _resolve_stake(arg, actor, limits)
    if not await event_dedupe.claim(community, namespace=_DEDUPE_NAMESPACE, event_id=event_id):
        log.info("blackjack.redelivery_skipped", op="claim")
        return None

    async def body() -> str:
        hand = await _load_hand(community, pseudonym)
        if hand is not None and hand.phase == PHASE_DUE:
            return await _collect_due(community, pseudonym, actor, hand, username)
        note = ""
        if hand is not None and hand.phase == PHASE_PLAYING:
            if clock.now_millis() < hand.expires_ms:
                return _view(hand, username, "has a hand open")
            note = _forfeit_note(username) + " "
            log.info("blackjack.forfeited", op="deal")
        elif hand is not None:
            log.warn("blackjack.dealing_discarded", op="deal")
        if stake == 0:  # a due hand was seen before the lock, but it is gone now
            return _BUSY_MSG
        return note + await _start_hand(community, pseudonym, actor, stake, limits, username)

    return await _locked(community, pseudonym, body)


async def _cmd_action(
    action: str, event: PlatformEvent, community: str, username: str
) -> str | None:
    """`!hit` / `!stand`: replay-guard, claim, then step the open hand under the lock."""
    actor = await _actor()
    pseudonym = _pseudonym(community, actor)
    event_id = event_dedupe.event_id_from_payload(event.payload, platform=event.platform)
    if not event_dedupe.is_fresh(event.occurred_at):
        log.info("blackjack.stale_event_skipped", op="fence")
        return None
    if await event_dedupe.already_seen(community, namespace=_DEDUPE_NAMESPACE, event_id=event_id):
        log.info("blackjack.redelivery_skipped", op="seen")
        return None
    peek = await _load_hand(community, pseudonym)
    if peek is None or peek.phase == PHASE_DEALING:
        return f"{username}, {_NO_HAND_MSG}"
    if peek.phase == PHASE_DUE:
        return (
            f"{username}, your last hand is won and awaiting payout -- use !blackjack to collect."
        )
    if not await event_dedupe.claim(community, namespace=_DEDUPE_NAMESPACE, event_id=event_id):
        log.info("blackjack.redelivery_skipped", op="claim")
        return None

    async def body() -> str:
        hand = await _load_hand(community, pseudonym)
        if hand is None or hand.phase != PHASE_PLAYING:
            return f"{username}, {_NO_HAND_MSG}"
        if clock.now_millis() >= hand.expires_ms:
            await community_kv.delete(community, _hand_key(pseudonym))
            log.info("blackjack.forfeited", op=action)
            return _forfeit_note(username) + " Start a new one with !blackjack <stake>."
        if action == "stand":
            return await _finish_dealer(community, pseudonym, actor, hand, username)
        hand.player.append(_draw())
        total = _total(hand.player)
        if total > 21:
            label, total_return = _return_for(hand.player, hand.dealer, hand.stake)
            return await _settle(community, pseudonym, actor, hand, total_return, label, username)
        if total == 21 or len(hand.player) >= _MAX_CARDS:
            return await _finish_dealer(community, pseudonym, actor, hand, username)
        await _store_hand(community, pseudonym, hand, _playing_ttl(hand))
        return _view(hand, username, "hits")

    return await _locked(community, pseudonym, body)


async def _cmd_status(event: PlatformEvent, community: str, username: str) -> str | None:
    """Bare `!blackjack`: show the open hand, collect a due payout, or print usage."""
    actor = await _actor()
    pseudonym = _pseudonym(community, actor)
    hand = await _load_hand(community, pseudonym)
    if hand is None or hand.phase == PHASE_DEALING:
        return _USAGE
    if hand.phase == PHASE_PLAYING:
        if clock.now_millis() >= hand.expires_ms:
            return f"{_forfeit_note(username)} Start a new one with !blackjack <stake>."
        return _view(hand, username, "has a hand open")
    event_id = event_dedupe.event_id_from_payload(event.payload, platform=event.platform)
    if not event_dedupe.is_fresh(event.occurred_at):
        return None
    if await event_dedupe.already_seen(community, namespace=_DEDUPE_NAMESPACE, event_id=event_id):
        return None
    if not await event_dedupe.claim(community, namespace=_DEDUPE_NAMESPACE, event_id=event_id):
        return None

    async def body() -> str:
        current = await _load_hand(community, pseudonym)
        if current is None or current.phase != PHASE_DUE:
            return _USAGE
        return await _collect_due(community, pseudonym, actor, current, username)

    return await _locked(community, pseudonym, body)


async def _cmd_set(arg: str | None, payload: dict[str, Any], community: str) -> str:
    """`!blackjack set minbet|maxbet|timeout <n>` -- moderator/broadcaster only."""
    if not _is_mod(payload):
        log.info("blackjack.config_denied", op="set")
        return _PERMISSION_DENIED_MSG
    parts = (arg or "").split()
    if len(parts) != 2 or parts[0].lower() not in _SET_KEYS:
        return _USAGE
    key = parts[0].lower()
    raw = parts[1]
    if not raw.isascii() or not raw.isdigit() or len(raw) > 6:
        return f"'{sanitize_user_text(raw[:32])}' isn't a whole number"
    value = int(raw)
    limits = await _get_limits(community)
    if key == "timeout":
        if not MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS:
            return f"timeout must be {MIN_TIMEOUT_SECONDS}-{MAX_TIMEOUT_SECONDS} seconds"
    else:
        if not 1 <= value <= MANIFEST_MAX_BET:
            return f"bets must be between 1 and {MANIFEST_MAX_BET} points"
        if key == "minbet" and value > limits.max_bet:
            return f"minbet cannot exceed the current maxbet ({limits.max_bet})"
        if key == "maxbet" and value < limits.min_bet:
            return f"maxbet cannot be below the current minbet ({limits.min_bet})"
    await community_kv.set(community, _cfg_key(key), str(value).encode(), 0)
    return f"blackjack {key} set to {value}"


def _parse(text: str, spec: CommandSpec) -> ParsedCommand:
    """`parse_command`, but a grammar error is the static usage line, never an echo of the input."""
    try:
        return parse_command(text, spec)
    except CommandUsageError as exc:
        raise CommandUsageError(_USAGE) from exc


async def _run_command(
    head: str, stripped: str, event: PlatformEvent, community: str | None
) -> str | None:
    """Route one recognized command to its handler and return the chat reply (None = drop)."""
    if community is None:
        raise CommandUsageError("blackjack is only available inside a community")
    username = _actor_label(event.actor)
    if head in ("!hit", "!stand"):
        _parse(stripped, _HIT_SPEC if head == "!hit" else _STAND_SPEC)
        return await _cmd_action(head[1:], event, community, username)
    word = stripped.partition(" ")[2].strip().lower()
    if word == "all" or (word.isascii() and word.isdigit()):
        return await _cmd_deal(word, event, community, username)
    parsed = _parse(stripped, SPEC)
    if parsed.option is None:
        return await _cmd_status(event, community, username)
    if parsed.option == "set":
        return await _cmd_set(parsed.args, event.payload, community)
    return _USAGE


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: play one `!blackjack`/`!hit`/`!stand` command.

    The whole game step runs here (the economy/identity imports are process-stage only); the
    returned event carries just the reply text and channel for `dispatch` to relay. `None` for a
    non-matching line and for a redelivered/too-old event (nothing is mutated or sent).

    Raises:
        ValueError: A matching command arrived with no `channel_id` to answer in.
        RuntimeError: The payload has no `message_id`/valid `occurred_at` (cannot be made
            replay-safe), or the kv backend failed (an ERROR log is emitted first).
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    head = stripped.partition(" ")[0].lower()
    if head not in _HEADS:
        return None
    channel_id = event.payload.get("channel_id")
    if not channel_id:
        raise ValueError("blackjack reply requires a channel_id from the inbound chat.message")
    community = get_bundle_context().community
    try:
        reply = await _run_command(head, stripped, event, community)
    except CommandUsageError as exc:
        reply = str(exc)
    except economy.EconomyError as exc:
        log.error("blackjack.economy_error", error=type(exc).__name__, code=_code_of(exc))
        reply = _UNAVAILABLE_MSG
    except event_dedupe.EventIdError as exc:
        log.error("blackjack.missing_event_id", op=head)
        raise RuntimeError(
            "blackjack requires the inbound message_id and a valid occurred_at to be replay-safe"
        ) from exc
    except Exception as exc:
        log.error("blackjack.kv_error", error=type(getattr(exc, "value", exc)).__name__)
        raise RuntimeError("blackjack state backend failed") from exc
    if reply is None:
        log.info("blackjack.transform skipped duplicate", command=head)
        return None
    log.info("blackjack.transform replied", command=head)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": reply, "channel_id": channel_id},
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result (see `defuse`)."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record the provider relayed to and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay the reply `transform()` already built.

    Raises:
        ValueError: No `channel_id`, or no reply text, in the payload.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("blackjack reply requires a channel_id from the inbound chat.message")
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("blackjack reply requires non-empty text")
    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("blackjack.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
