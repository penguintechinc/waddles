"""`!lottery` -> a periodic ticket lottery: buy tickets with points, draw, winner takes the pot.

Viewers buy tickets (`TICKET_PRICE` points each, bounded per purchase / per player / per round),
a draw picks ONE winner weighted by tickets, and the winner collects the pot. Net-new Waddles
content (a generic ticket-lottery mechanic).

Commands:

- `!lottery buy <n>` -- buy `n` tickets (verb form `!lottery add <n>`); the first buy after a draw
  opens the next round
- `!lottery draw` -- draw now (moderators, any time -- it closes sales first); ANYONE may draw once
  the round's interval has elapsed (`DRAW_INTERVAL_SECONDS`), because the platform has no
  scheduler capability: an elapsed interval simply makes the draw callable by everyone
- `!lottery claim [round]` -- the winner collects the pot
- `!lottery` / `!lottery list` -- show the pot, ticket count, players and time left

## Why the pot is CLAIMED, not pushed

The host only moves the money of the account that triggered the invocation (`economy.wager`'s
`user` must be the invoking actor; see `waddle_sdk.economy`). The draw is triggered by someone
else, so it cannot credit the winner. Ticket money is debited when tickets are bought
(`wager(cost, 0)`; the points live on only as the `sold` counter in `community_kv`), and the
winner collects with their OWN `claim`: one `wager(f, chunk + f)` on their account, net `+chunk`,
where `f = ceil(chunk / 99)` is the smallest stake the host's `payout <= 100 x stake` rule lets
carry the payout (paid straight back in the same call -- the winner needs 1 point in hand). A pot
larger than `99 x max_bet(actor)` is collected over several claims (`paid` counter).

## Money integrity (the part that moves points)

- **Conserved, bounded payout.** `pot = sold x TICKET_PRICE`, where `sold` is incremented only
  AFTER the matching debit, so `pot <= points burned`: the bundle never mints more than was spent
  on tickets. A defence-in-depth clamp (`MAX_POT`) bounds even a corrupted counter. Bounds:
  `MAX_BUY` per purchase, `MAX_TICKETS_PER_USER` per round, `MAX_ROUND_TICKETS` per round (a soft
  cap: concurrent buyers can overshoot it by a few purchases, and the pot still equals what was
  paid), declared `max_bet: 500` per call, and the host's daily payout (mint) caps on top
  (`quota_exceeded` -> "try again later", reservation released).
- **Debit first.** A purchase debits, THEN allocates the ticket positions, THEN writes the
  entry, THEN counts the sale. A crash in between strands paid points or leaves a ghost position
  (skipped by the draw); it can never create an unpaid ticket or inflate the pot.
- **Time fence, not a lock.** kv has no transaction, so a purchase racing the draw's read of the
  ledger could be lost. Sales are refused from `close`; a draw is refused until `close +
  SETTLE_SECONDS` (longer than any in-flight 2 s purchase). A moderator's early `draw` closes sales
  and asks to repeat once the fence has passed.
- **Idempotent draw.** `lottery.drawing.<round>` is a 60 s `kv.increment` election (one drawer),
  the result is written once to `lottery.win.<round>`, and a later `draw` just reports the stored
  winner -- two moderators, a redelivered `draw`, or a draw typed twice change nothing.
- **Idempotent on redelivery.** `transform` forwards the platform `message_id`; every mutating
  command claims it via `waddle_sdk.event_dedupe` (ONE atomic `kv.increment` on a per-event TTL'd
  marker, `1` = first delivery) AFTER the cheap prechecks and the atomic per-actor cooldown and
  BEFORE any ticket money, payout or state change; a redelivery is skipped silently. An event older
  than `event_dedupe.MAX_EVENT_AGE_SECONDS` is dropped; a payload with no `message_id` / valid
  `occurred_at` fails loud and moves nothing. At-most-once: a crash after the claim loses that one
  operation, never repeats it.
- **Atomic CAS everywhere.** Opening a round, electing the drawer and reserving a claim chunk
  (`paid.<round>`) are `kv.increment` elections -- never read-then-set.
- **Ambiguous host failure.** A `BackendError` from `wager` retries the identical call once (the
  host dedupes by event), then fails loud WITHOUT releasing the claim reservation. Any other
  unexpected refusal (actor mismatch, idempotency conflict, unprovisioned economy) is logged at
  ERROR, answered generically and raised.
- **Fair draw.** A ticket position `r` is drawn uniformly from the allocated positions and mapped
  to its purchase by probing up to `MAX_BUY` entries (a purchase owns a contiguous range ending at
  its key); a ghost position (crash residue) is re-drawn, so the winner is uniform over PAID
  tickets and a draw costs at most `DRAW_ATTEMPTS x MAX_BUY` reads (kv budget: 64 ops).

PII-free: kv keys/logs carry only `lottery.*` names, round numbers, ticket positions and
`<pseudonym>` = SHA-256(community|resolved-uuid)[:32]; per-event markers carry a truncated SHA-256
of the platform-prefixed message id (never the id). A winner's host-minted `{user:<token>}`
placeholder (an opaque token, never a name) is kept in the entry so the announcement can mention
them; a raw handle is never stored. Logs carry `op`/`outcome`/`amount_bucket`/error-case only.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, NoReturn, TypeVar

from waddle_sdk import clock, community_kv, economy, event_dedupe, identity, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.community_kv import TENANT_WIDE_SENTINEL
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text

SPEC = CommandSpec(name="lottery")

TICKET_PRICE = 10
#: Most tickets one `!lottery buy` may take (also bounds the draw's probe window).
MAX_BUY = 10
MAX_TICKETS_PER_USER = 20
#: Soft cap on tickets sold in one round (bounds the kv footprint and the pot).
MAX_ROUND_TICKETS = 1000
#: Hard clamp on a payout, whatever a (corrupted) counter says.
MAX_POT = 2 * MAX_ROUND_TICKETS * TICKET_PRICE
MIN_BUYERS = 2
#: A round's sales close this long after it opens; after that anyone may draw.
DRAW_INTERVAL_SECONDS = 3600
#: A draw is refused until sales have been closed this long -- longer than any in-flight purchase.
SETTLE_SECONDS = 10
COOLDOWN_SECONDS = 5
#: Re-draws allowed when a drawn position falls in a ghost range (bounded by the 64 kv-op budget).
DRAW_ATTEMPTS = 4
#: `economy.MAX_PAYOUT_MULTIPLE - 1`: the most a `wager` can pay per point of (returned) stake.
_FLOAT_DIVISOR = economy.MAX_PAYOUT_MULTIPLE - 1
#: Every round key lives this long (claim window); the host caps a kv TTL at 30 days.
ROUND_TTL_SECONDS = 14 * 24 * 60 * 60
#: Election markers heal after this long if the elected writer crashed mid-way.
_ELECTION_TTL_SECONDS = 60

_DEDUPE_NAMESPACE = "lottery"
_PSEUDONYM_LEN = 32
#: Host denial codes that are ordinary limits (not anomalies): shown to the user, never raised.
_LIMIT_CODES = frozenset({"quota_exceeded", "rate_limited"})
_USAGE = (
    f"Usage: !lottery buy <1-{MAX_BUY}> (tickets are {TICKET_PRICE} points each) | !lottery | "
    "!lottery claim | !lottery draw (moderators any time, anyone once the round is due)"
)
_UNAVAILABLE = "the lottery is temporarily unavailable."
_KNOWN_COMMANDS = frozenset({"status", "usage", "buy", "draw", "claim"})
#: Shared-grammar verbs mapped onto actions (`!lottery add 3` == `!lottery buy 3`).
_VERB_ACTIONS = {"add": "buy", "list": "status"}
_DOMAIN_WORDS = frozenset({"buy", "draw", "claim"})

#: The pipeline delivers `event.actor` as its own host-minted placeholder; anchored so ONLY an
#: actor that is exactly one `{user:<token>}` is left live (never one embedded in other text).
_USER_TOKEN_RE = re.compile(r"^\{user:([A-Za-z0-9_:.-]+)\}$")
_FALLBACK_ACTOR = "someone"
_ROUND_NO_RE = re.compile(r"^[0-9]{1,9}$")
_MAX_TOKEN_LEN = 128

_T = TypeVar("_T")


class _Phase(IntEnum):
    """Lifecycle of the current round."""

    NONE = 0
    OPEN = 1
    CLOSED = 2
    DONE = 3


@dataclass(slots=True, frozen=True)
class _Win:
    """The frozen draw result of a round: written once by the elected drawer."""

    pseudonym: str  # the winner's per-(community, actor) key component
    token: str | None  # the winner's host-minted `{user:<token>}` token, for the announcement
    pot: int  # points the winner may collect
    tickets: int  # tickets sold (for the announcement)
    buyers: int  # distinct players (for the announcement)

    def to_bytes(self) -> bytes:
        """Serialize for kv."""
        doc = {
            "p": self.pseudonym,
            "k": self.token,
            "pot": self.pot,
            "t": self.tickets,
            "b": self.buyers,
        }
        return json.dumps(doc, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, raw: bytes) -> _Win:
        """Parse a stored result; any malformed value raises `ValueError` (fail loud)."""
        doc = json.loads(raw.decode())
        pseud, token = doc["p"], doc["k"]
        nums = (doc["pot"], doc["t"], doc["b"])
        if (
            not isinstance(pseud, str)
            or not (token is None or isinstance(token, str))
            or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in nums)
            or nums[0] > MAX_POT
        ):
            raise ValueError("corrupt lottery result")
        return cls(pseudonym=pseud, token=token, pot=nums[0], tickets=nums[1], buyers=nums[2])


@dataclass(slots=True, frozen=True)
class _Entry:
    """One purchase: `count` tickets ending at the entry's own key position."""

    count: int
    pseudonym: str
    token: str | None

    def to_bytes(self) -> bytes:
        """Serialize for kv."""
        doc = {"n": self.count, "p": self.pseudonym, "k": self.token}
        return json.dumps(doc, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, raw: bytes) -> _Entry:
        """Parse a stored entry; any malformed value raises `ValueError` (fail loud)."""
        doc = json.loads(raw.decode())
        count, pseud, token = doc["n"], doc["p"], doc["k"]
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= MAX_BUY
            or not isinstance(pseud, str)
            or not (token is None or isinstance(token, str))
        ):
            raise ValueError("corrupt lottery entry")
        return cls(count=count, pseudonym=pseud, token=token)


@dataclass(slots=True, frozen=True)
class _View:
    """A consistent-enough read of the current round (existence, close time, result, clock)."""

    number: int
    defined: bool
    close_ms: int
    win: _Win | None
    now_ms: int

    @property
    def phase(self) -> _Phase:
        """The round's lifecycle phase."""
        if not self.defined:
            return _Phase.NONE
        if self.win is not None:
            return _Phase.DONE
        return _Phase.CLOSED if self.now_ms >= self.close_ms else _Phase.OPEN

    @property
    def drawable_ms(self) -> int:
        """The earliest moment a draw is accepted (sales closed AND the settle window passed)."""
        return self.close_ms + SETTLE_SECONDS * 1000


@dataclass(slots=True, frozen=True)
class _Ctx:
    """Everything one dispatch needs about where it runs and who triggered it."""

    scope: str | None
    provider: str
    channel_id: str
    payload: dict[str, Any]
    occurred_at: str
    label: str
    token: str | None


def _actor_token(actor: object) -> str | None:
    """Return the host-minted `{user:<token>}` token when `actor` is exactly one, else `None`."""
    if not isinstance(actor, str):
        return None
    match = _USER_TOKEN_RE.match(actor.strip())
    if match is None or len(match.group(1)) > _MAX_TOKEN_LEN:
        return None
    return match.group(1)


def _actor_label(actor: object) -> str:
    """Return a reply-safe label for `event.actor`.

    The host-minted `{user:<token>}` stays live so egress renders the display name. Anything else
    (a raw handle when tokenization is off, a missing actor) is untrusted text and goes through
    `sanitize_user_text` in full, so a platform display name can never forge a mention.
    """
    if not isinstance(actor, str) or not actor.strip():
        return _FALLBACK_ACTOR
    token = _actor_token(actor)
    return sanitize_user_text(actor.strip(), allow_tokens=(token,) if token else ())


def _winner_label(token: str | None) -> str:
    """Render a stored winner token as a live placeholder (re-validated: kv is not trusted)."""
    if token is None or _USER_TOKEN_RE.match("{user:" + token + "}") is None:
        return "a ticket holder"
    return sanitize_user_text("{user:" + token + "}", allow_tokens=(token,))


def _pseudonym(scope: str | None, actor_uuid: str) -> str:
    """Non-reversible per-(community, actor) key component from the host-resolved uuid."""
    base = scope if scope is not None else TENANT_WIDE_SENTINEL
    return hashlib.sha256(f"{base}|{actor_uuid}".encode()).hexdigest()[:_PSEUDONYM_LEN]


def _amount_bucket(amount: int) -> str:
    """Coarse amount bucket for PII-free logs (never the exact value)."""
    for limit in (10, 100, 1000):
        if amount <= limit:
            return f"le{limit}"
    return "gt1000"


def _is_privileged(payload: dict[str, Any]) -> bool:
    """True only for a real boolean `True` `is_mod`/`is_broadcaster` badge (fails closed)."""
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


def _pick_ticket(total: int) -> int:
    """Draw one ticket position uniformly from `1..total` (patched in tests)."""
    return random.SystemRandom().randint(1, total)


def _route(text: str) -> tuple[str, str | None] | None:
    """Map one chat line to `(command, args)`; `None` when it is not ours."""
    stripped = text.strip()
    head, _, tail = stripped.partition(" ")
    if head.lower() != "!lottery":
        return None
    tail = tail.strip()
    if not tail:
        return "status", None
    first, _, rest = tail.partition(" ")
    if first.lower() in _DOMAIN_WORDS:
        return first.lower(), rest.strip() or None
    try:
        parsed = parse_command(stripped, SPEC)
    except CommandUsageError:
        log.debug("lottery.transform usage", reason="unknown_option")
        return "usage", None
    action = _VERB_ACTIONS.get(parsed.option or "")
    return (action, parsed.args) if action else ("usage", None)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!lottery ...`.

    Routing only (no kv): the mod badge is forwarded as a strict boolean and the platform
    `message_id` is forwarded for replay-dedupe. A recognized-but-malformed command replies usage.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    routed = _route(text)
    if routed is None:
        return None
    command, args = routed
    payload: dict[str, Any] = {"channel_id": event.payload.get("channel_id"), "command": command}
    if args is not None:
        payload["args"] = args
    message_id = event.payload.get("message_id")
    if isinstance(message_id, str):  # replay-dedupe key; `dispatch` refuses a mutation without it
        payload["message_id"] = message_id
    for badge in ("is_mod", "is_broadcaster"):
        if badge in event.payload:  # absence must reach dispatch as absence; identity check only
            payload[badge] = event.payload[badge] is True
    log.info("lottery.transform matched", command=command)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
        occurred_at=event.occurred_at,
    )


@dataclass(slots=True)
class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result (see `heist`)."""

    transport: str
    detail: str
    sub_type: str | None = None
    http_status: int | None = None


def _k(*parts: object) -> str:
    """Build a `.`-joined kv key under `lottery.` (kv forbids `:`)."""
    return ".".join(("lottery", *(str(p) for p in parts)))


class _Store:
    """Community kv access for one dispatch; every kv failure is fail-loud (log, reply, raise)."""

    def __init__(self, scope: str | None, provider: str, channel_id: str) -> None:
        """Bind to one community scope and the reply channel used on failure."""
        self._scope = scope
        self._provider = provider
        self._channel_id = channel_id

    async def fail(self, exc: Exception, op: str) -> NoReturn:
        """Log (error-case name only), reply an error to chat, then raise `RuntimeError`."""
        case_name = type(getattr(exc, "value", exc)).__name__
        log.error("lottery.error", op=op, error=case_name)
        await relay.push(self._provider, {"channel": self._channel_id, "text": _UNAVAILABLE})
        raise RuntimeError(f"lottery {op} failed: {case_name}") from exc

    async def _guard(self, op: str, call: Awaitable[_T]) -> _T:
        """Await a kv call, turning any failure into the fail-loud path."""
        try:
            return await call
        except Exception as exc:  # noqa: BLE001 - classified in `fail`
            await self.fail(exc, op)

    async def get_raw(self, key: str) -> bytes | None:
        """Read a raw value (`None` if unset)."""
        return await self._guard("get", community_kv.get(self._scope, key))

    async def get_int(self, key: str) -> int | None:
        """Read a decimal counter (`None` if unset; a non-integer value fails loud)."""
        raw = await self.get_raw(key)
        if raw is None:
            return None
        try:
            return int(raw.decode())
        except ValueError as exc:
            await self.fail(exc, "corrupt")

    async def count(self, key: str) -> int:
        """Read a counter as a never-negative int (0 if unset)."""
        return max(await self.get_int(key) or 0, 0)

    async def set_raw(self, key: str, value: bytes, ttl: int = ROUND_TTL_SECONDS) -> None:
        """Write a value with a TTL."""
        await self._guard("set", community_kv.set(self._scope, key, value, ttl))

    async def delete(self, key: str) -> None:
        """Delete a key."""
        await self._guard("delete", community_kv.delete(self._scope, key))

    async def incr(self, key: str, delta: int, ttl: int = ROUND_TTL_SECONDS) -> int:
        """Atomic `kv.increment` (the only CAS); returns the new value."""
        return await self._guard("increment", community_kv.increment(self._scope, key, delta, ttl))

    async def now_ms(self) -> int:
        """Host clock in epoch milliseconds."""
        try:
            return clock.now_millis()
        except Exception as exc:  # noqa: BLE001 - classified in `fail`
            await self.fail(exc, "clock")

    async def event_id(
        self, payload: dict[str, Any], platform: str, occurred_at: str
    ) -> str | None:
        """Return the replay-stable event id, or `None` if the event is too old to process.

        Fails loud (log, reply, raise) when there is no usable id or ingest stamp: an event whose
        identity or age cannot be established is never processed unguarded.
        """
        try:
            event_id = event_dedupe.event_id_from_payload(payload, platform=platform)
            fresh = event_dedupe.is_fresh(occurred_at)
        except event_dedupe.EventIdError as exc:
            await self.fail(exc, "event_id")
        return event_id if fresh else None

    async def seen(self, event_id: str) -> bool:
        """Read-only, advisory: True if this event was already claimed (a redelivery)."""
        return await self._guard(
            "dedupe",
            event_dedupe.already_seen(self._scope, namespace=_DEDUPE_NAMESPACE, event_id=event_id),
        )

    async def claim_event(self, event_id: str) -> bool:
        """Atomically claim the event BEFORE any mutation; False means a duplicate to skip."""
        return await self._guard(
            "dedupe",
            event_dedupe.claim(self._scope, namespace=_DEDUPE_NAMESPACE, event_id=event_id),
        )

    async def cooldown_claim(self, pseudonym: str) -> bool:
        """Return True if the caller may act now (first increment within the cooldown TTL)."""
        return await self.incr(_k("cd", pseudonym), 1, COOLDOWN_SECONDS) == 1

    async def elect(self, key: str) -> bool:
        """Win a short-lived single-writer election (`increment` returns 1 for exactly one)."""
        return await self.incr(key, 1, _ELECTION_TTL_SECONDS) == 1

    async def load_win(self, number: int) -> _Win | None:
        """Load a round's frozen result (`None` while undrawn; corrupt fails loud)."""
        raw = await self.get_raw(_k("win", number))
        if raw is None:
            return None
        try:
            return _Win.from_bytes(raw)
        except (ValueError, KeyError, TypeError) as exc:
            await self.fail(exc, "corrupt")

    async def load_entry(self, number: int, end: int) -> _Entry | None:
        """Load the purchase whose last ticket position is `end` (`None` if none/ghost)."""
        raw = await self.get_raw(_k("ent", number, end))
        if raw is None:
            return None
        try:
            return _Entry.from_bytes(raw)
        except (ValueError, KeyError, TypeError) as exc:
            await self.fail(exc, "corrupt")

    async def view(self) -> _View:
        """Read the current round's existence, close time and result plus the clock."""
        number = await self.get_int(_k("round")) or 0
        now = await self.now_ms()
        if number <= 0 or await self.get_raw(_k("def", number)) is None:
            return _View(max(number, 0), False, 0, None, now)
        close = await self.get_int(_k("close", number))
        if close is None:
            await self.fail(ValueError("round has no close time"), "corrupt")
        return _View(number, True, close, await self.load_win(number), now)


async def _wager(actor_uuid: str, stake: int, payout: int) -> int:
    """`economy.wager`, retrying the identical call once after an ambiguous `BackendError`."""
    try:
        return await economy.wager(actor_uuid, stake, payout)
    except economy.BackendError:
        log.error("lottery.error", op="wager", error="BackendError", retry=True)
        return await economy.wager(actor_uuid, stake, payout)


@dataclass(slots=True)
class _Game:
    """The command flows of one dispatch (kv store + context)."""

    store: _Store
    ctx: _Ctx

    async def _gate(self) -> str | None:
        """Event id + age fence + advisory seen-check; `None` means skip silently."""
        ctx = self.ctx
        event_id = await self.store.event_id(ctx.payload, ctx.provider, ctx.occurred_at)
        if event_id is None:
            log.info("lottery.stale_event_skipped", op="fence")
            return None
        if await self.store.seen(event_id):
            log.info("lottery.redelivery_skipped", op="seen")
            return None
        return event_id

    async def _claim(self, event_id: str) -> bool:
        """Claim the event before the first mutation; False = duplicate (skip silently)."""
        if await self.store.claim_event(event_id):
            return True
        log.info("lottery.redelivery_skipped", op="claim")
        return False

    async def _actor(self) -> tuple[str, str] | str:
        """Resolve `(actor_uuid, pseudonym)` or return a chat reply for a refusal."""
        label = self.ctx.label
        try:
            actor_uuid = await identity.resolve_actor()
        except identity.NotLinkedError:
            log.info("lottery.denied", op="identity", error="NotLinkedError")
            return f"{label}, link your account before playing the lottery."
        except (identity.IdentityError, economy.DeniedError, economy.NotAMemberError) as exc:
            log.info("lottery.denied", op="identity", error=type(exc).__name__)
            return f"{label}, you can't play the lottery right now."
        return actor_uuid, _pseudonym(self.ctx.scope, actor_uuid)

    async def _refuse_wager(self, exc: economy.EconomyError, what: str) -> str:
        """Reply for a host refusal of a wager; anything unexpected fails loud (log, reply, raise).

        A short balance, an over-cap amount and a daily quota/rate limit are ordinary refusals. A
        denied grant, actor mismatch, idempotency conflict or an unprovisioned economy are
        anomalies that must never be swallowed into a polite reply.
        """
        label = self.ctx.label
        if isinstance(exc, economy.InsufficientFundsError):
            log.info("lottery.denied", op="wager", error=type(exc).__name__)
            return f"{label}, you only have {exc.balance} points."
        if isinstance(exc, economy.OverCapError):
            log.info("lottery.denied", op="wager", error=type(exc).__name__)
            return f"{label}, the max {what} right now is {exc.cap} points."
        if isinstance(exc, economy.DeniedError) and exc.code in _LIMIT_CODES:
            log.info("lottery.denied", op="wager", error=exc.code)
            return f"{label}, the daily points limit is reached; try again later."
        await self.store.fail(exc, "wager")

    async def _pot(self, number: int) -> tuple[int, int, int]:
        """`(tickets_sold, pot_points, distinct_buyers)` of a round (never negative, clamped)."""
        sold = await self.store.count(_k("sold", number))
        return sold, min(sold * TICKET_PRICE, MAX_POT), await self.store.count(_k("buyers", number))

    async def status(self) -> str:
        """Show the pot, ticket count, players and time left."""
        view = await self.store.view()
        phase = view.phase
        if phase is _Phase.NONE:
            return (
                f"no lottery running. The first !lottery buy <n> starts one "
                f"(tickets are {TICKET_PRICE} points each)."
            )
        if view.win is not None:
            return (
                f"\U0001f39f\ufe0f Lottery #{view.number} was drawn: "
                f"{_winner_label(view.win.token)} won {view.win.pot} points. "
                "The next !lottery buy starts a new round."
            )
        sold, pot, buyers = await self._pot(view.number)
        head = (
            f"\U0001f39f\ufe0f Lottery #{view.number}: pot {pot} points "
            f"({sold} tickets, {buyers} players)"
        )
        if phase is _Phase.CLOSED:
            return f"{head} -- sales closed, the draw is due: !lottery draw."
        mins = max((view.close_ms - view.now_ms) // 60000, 0)
        return (
            f"{head} -- {TICKET_PRICE} points a ticket (max {MAX_TICKETS_PER_USER} each), "
            f"sales close in {mins}m. !lottery buy <n>."
        )

    async def _open_next(self, previous: int) -> bool:
        """Open round `previous + 1` (single-writer election); False if another opener won."""
        number = previous + 1
        if not await self.store.elect(_k("opening", number)):
            return False
        now = await self.store.now_ms()
        await self.store.set_raw(_k("def", number), json.dumps({"at": now}).encode())
        await self.store.set_raw(
            _k("close", number), str(now + DRAW_INTERVAL_SECONDS * 1000).encode()
        )
        if await self.store.incr(_k("round"), 1, 0) != number:
            await self.store.fail(RuntimeError("round counter moved"), "round")
        log.info("lottery.opened", outcome="open")
        return True

    async def buy(self, args: str | None) -> str | None:
        """Buy tickets: debit first, then allocate positions, write the entry, count the sale."""
        label = self.ctx.label
        text = (args or "").strip()
        if not (text.isascii() and text.isdigit()):
            return _USAGE
        count = int(text)
        if not 1 <= count <= MAX_BUY:
            return f"{label}, you can buy 1-{MAX_BUY} tickets at a time."
        who = await self._actor()
        if isinstance(who, str):
            return who
        actor_uuid, pseudonym = who
        event_id = await self._gate()
        if event_id is None:
            return None
        cost = count * TICKET_PRICE
        refusal = await self._buy_prechecks(actor_uuid, cost)
        if refusal is not None:
            return refusal
        if not await self.store.cooldown_claim(pseudonym):
            return f"slow down, {label}! try again in a few seconds."
        if not await self._claim(event_id):
            return None
        return await self._sell(actor_uuid, pseudonym, count, cost)

    async def _buy_prechecks(self, actor_uuid: str, cost: int) -> str | None:
        """Read-only checks (sales open, funds, cap); a reply when refused."""
        label = self.ctx.label
        view = await self.store.view()
        if view.phase is _Phase.CLOSED:
            return f"{label}, ticket sales are closed -- the draw is due (!lottery draw)."
        try:
            held = await economy.balance(actor_uuid)
            if cost > held:
                return f"{label}, you only have {held} points."
            cap = await economy.max_bet(actor_uuid)
        except (economy.DeniedError, economy.NotAMemberError) as exc:
            log.info("lottery.denied", op="precheck", error=type(exc).__name__)
            return f"{label}, you can't buy tickets right now."
        except economy.EconomyError as exc:
            await self.store.fail(exc, "precheck")
        if cost > cap:
            return f"{label}, the max you can spend right now is {cap} points."
        return None

    async def _sell(self, actor_uuid: str, pseudonym: str, count: int, cost: int) -> str:
        """Authoritative purchase under the cooldown + event claims."""
        label, store = self.ctx.label, self.store
        view = await store.view()  # re-read: the round may have closed or ended since the precheck
        opened = False
        if view.phase in (_Phase.NONE, _Phase.DONE):
            opened = await self._open_next(view.number)
            view = await store.view()
            if view.phase is not _Phase.OPEN:
                return f"{label}, the lottery is being set up -- try again in a moment."
        if view.phase is not _Phase.OPEN:
            return f"{label}, ticket sales are closed -- the draw is due (!lottery draw)."
        number = view.number
        held = await store.count(_k("tk", number, pseudonym))
        if held + count > MAX_TICKETS_PER_USER:
            return (
                f"{label}, you can hold at most {MAX_TICKETS_PER_USER} tickets per round "
                f"(you have {held})."
            )
        if await store.count(_k("tix", number)) + count > MAX_ROUND_TICKETS:
            return f"{label}, this round is sold out -- the draw is coming up."
        try:
            await _wager(actor_uuid, cost, 0)
        except economy.BackendError as exc:
            await store.fail(exc, "wager")  # ambiguous: never retry blind
        except economy.EconomyError as exc:
            log.debug("lottery.purchase refused", reason=type(exc).__name__)
            return await self._refuse_wager(exc, "purchase")
        await store.incr(_k("tk", number, pseudonym), count)
        end = await store.incr(_k("tix", number), count)
        entry = _Entry(count=count, pseudonym=pseudonym, token=self.ctx.token)
        await store.set_raw(_k("ent", number, end), entry.to_bytes())
        # Count the sale only now: `sold` (hence the pot) never exceeds what was debited.
        sold = await store.incr(_k("sold", number), count)
        if held == 0:
            await store.incr(_k("buyers", number), 1)
        log.info("lottery.bought", outcome="buy", amount_bucket=_amount_bucket(cost))
        started = f"Lottery #{number} is open! " if opened else ""
        return (
            f"\U0001f39f\ufe0f {started}{label} bought {count} ticket{'s' if count != 1 else ''} "
            f"(you hold {held + count}/{MAX_TICKETS_PER_USER}). "
            f"Pot: {min(sold * TICKET_PRICE, MAX_POT)} points."
        )

    async def draw(self) -> str | None:
        """Draw the winner: moderators any time, anyone once the interval has elapsed."""
        event_id = await self._gate()
        if event_id is None:
            return None
        view = await self.store.view()
        label = self.ctx.label
        if view.phase is _Phase.NONE:
            return f"{label}, there is no lottery running."
        if view.win is not None:
            return self._already_drawn(view)
        by_mod = _is_privileged(self.ctx.payload)
        if view.now_ms < view.close_ms:
            if not by_mod:
                mins = (view.close_ms - view.now_ms + 59999) // 60000
                return (
                    f"{label}, ticket sales are open for {mins} more minute(s); "
                    "the draw is due after."
                )
            if not await self._claim(event_id):
                return None
            await self.store.set_raw(_k("close", view.number), str(view.now_ms).encode())
            return (
                f"\U0001f512 Ticket sales are closed. Run !lottery draw again in {SETTLE_SECONDS}s "
                "so every in-flight purchase is counted."
            )
        if view.now_ms < view.drawable_ms:
            wait = (view.drawable_ms - view.now_ms + 999) // 1000
            return f"{label}, wait {wait}s for in-flight purchases to settle, then draw again."
        sold, pot, buyers = await self._pot(view.number)
        if buyers < MIN_BUYERS or sold <= 0:
            if not await self._claim(event_id):
                return None
            extended = view.now_ms + DRAW_INTERVAL_SECONDS * 1000
            await self.store.set_raw(_k("close", view.number), str(extended).encode())
            return (
                f"\U0001f39f\ufe0f The draw needs at least {MIN_BUYERS} players ({buyers} so far) "
                f"-- sales extended by {DRAW_INTERVAL_SECONDS // 60} minutes."
            )
        if not await self._claim(event_id):
            return None
        if not await self.store.elect(_k("drawing", view.number)):
            return f"{label}, the draw is already happening."
        win = await self._pick_winner(view.number, sold, pot, buyers)
        await self.store.set_raw(_k("win", view.number), win.to_bytes())
        log.info("lottery.drawn", outcome="draw", amount_bucket=_amount_bucket(win.pot))
        return (
            f"\U0001f389 Lottery #{view.number} drawn! {_winner_label(win.token)} wins the pot of "
            f"{win.pot} points ({win.tickets} tickets, {win.buyers} players). "
            "The winner collects with !lottery claim."
        )

    def _already_drawn(self, view: _View) -> str:
        """The idempotent reply for a draw of a round that already has a winner."""
        win = view.win
        if win is None:
            return "this lottery was already drawn."
        return (
            f"lottery #{view.number} was already drawn: {_winner_label(win.token)} won "
            f"{win.pot} points."
        )

    async def _pick_winner(self, number: int, sold: int, pot: int, buyers: int) -> _Win:
        """Draw a ticket position uniformly and map it to its purchase (ghosts are re-drawn)."""
        store = self.store
        positions = await store.count(_k("tix", number))
        if positions < 1:
            await store.fail(ValueError("tickets sold but no positions"), "corrupt")
        for _ in range(DRAW_ATTEMPTS):
            position = _pick_ticket(positions)
            for end in range(position, min(position + MAX_BUY, positions + 1)):
                entry = await store.load_entry(number, end)
                if entry is None:
                    continue
                if end - entry.count + 1 <= position:
                    return _Win(
                        pseudonym=entry.pseudonym,
                        token=entry.token,
                        pot=pot,
                        tickets=sold,
                        buyers=buyers,
                    )
                break  # the position sits in an unowned (ghost) range before this entry
        await store.delete(_k("drawing", number))  # let a moderator retry straight away
        await store.fail(RuntimeError("no owned ticket drawn"), "draw")

    async def claim(self, args: str | None) -> str | None:
        """Collect the pot (winner only), `99 x max_bet` per call."""
        label, store = self.ctx.label, self.store
        arg = (args or "").strip()
        if arg and not _ROUND_NO_RE.match(arg):
            return _USAGE
        who = await self._actor()
        if isinstance(who, str):
            return who
        actor_uuid, pseudonym = who
        event_id = await self._gate()
        if event_id is None:
            return None
        found = await self._collectable(int(arg) if arg else None, pseudonym)
        if found is None:
            return f"{label}, you have nothing to collect."
        number, due, paid = found
        try:
            cap = await economy.max_bet(actor_uuid)
        except (economy.DeniedError, economy.NotAMemberError) as exc:
            log.info("lottery.denied", op="claim", error=type(exc).__name__)
            return f"{label}, you can't collect right now."
        except economy.EconomyError as exc:
            await store.fail(exc, "precheck")
        if cap < 1:
            return f"{label}, hold at least 1 point to collect (it is paid straight back)."
        if not await store.cooldown_claim(pseudonym):
            return f"slow down, {label}! try again in a few seconds."
        if not await self._claim(event_id):
            return None
        chunk = min(due - paid, _FLOAT_DIVISOR * cap)
        paid_key = _k("paid", number)
        if await store.incr(paid_key, chunk) > due:  # a concurrent collector reserved first
            await store.incr(paid_key, -chunk)
            return f"{label}, you have nothing more to collect right now."
        stake = -(-chunk // _FLOAT_DIVISOR)  # ceil: the smallest stake whose payout cap fits
        try:
            await _wager(actor_uuid, stake, chunk + stake)
        except economy.BackendError as exc:
            await store.fail(exc, "wager")  # ambiguous: keep the reservation (no double payout)
        except economy.EconomyError as exc:
            await store.incr(paid_key, -chunk)  # refused: nothing moved, release the reservation
            return await self._refuse_wager(exc, "collect")
        log.info("lottery.collected", outcome="claim", amount_bucket=_amount_bucket(chunk))
        left = due - paid - chunk
        more = f" ({left} more -- claim again)" if left > 0 else ""
        return f"\U0001f3c6 {label} collected {chunk} points{more}."

    async def _collectable(
        self, explicit: int | None, pseudonym: str
    ) -> tuple[int, int, int] | None:
        """Find `(round, due, already_paid)` of the first drawn round this actor won."""
        store = self.store
        current = await store.get_int(_k("round")) or 0
        candidates = [explicit] if explicit else [n for n in (current, current - 1) if n >= 1]
        for number in candidates:
            win = await store.load_win(number)
            if win is None or win.pseudonym != pseudonym:
                continue
            paid = await store.count(_k("paid", number))
            if win.pot - paid > 0:
                return number, win.pot, paid
        return None


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: run the command, then relay the reply.

    Raises:
        ValueError: No `channel_id`, an empty community, or an unrecognized command.
        RuntimeError: A kv/economy backend call failed (error reply + ERROR log emitted first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("lottery reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized lottery command: {command!r}")
    if envelope.community == "":
        log.error("lottery.missing_community", command=command)
        raise ValueError("lottery requires a community context (None = tenant-wide, never '')")
    provider = envelope.event.platform
    actor = envelope.event.actor
    ctx = _Ctx(
        scope=envelope.community,
        provider=provider,
        channel_id=channel_id,
        payload=payload,
        occurred_at=envelope.event.occurred_at,
        label=_actor_label(actor),
        token=_actor_token(actor),
    )
    game = _Game(_Store(ctx.scope, provider, channel_id), ctx)
    arg = payload.get("args")
    args = arg if isinstance(arg, str) else None
    reply: str | None
    try:
        reply = await _run(game, command, args)
    except CommandUsageError as exc:
        reply = str(exc)
    if reply is None:
        log.info("lottery.dispatch skipped duplicate", platform=provider, command=command)
        return DispatchResult(transport=provider, detail="duplicate")
    await relay.push(provider, {"channel": channel_id, "text": reply})
    log.info("lottery.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)


async def _run(game: _Game, command: str, args: str | None) -> str | None:
    """Route one validated command to its flow."""
    if command == "status":
        return await game.status()
    if command == "usage":
        return _USAGE
    if command == "buy":
        return await game.buy(args)
    if command == "draw":
        return await game.draw()
    return await game.claim(args)
