"""`!prediction` / `!predict` -> community predictions on a pari-mutuel point pool.

A moderator opens a question with 2-5 named outcomes, viewers stake points on ONE outcome, betting
closes, a moderator resolves it, and the winners split the whole pool in proportion to their stake.
Net-new Waddles content (the open / bet / resolve shape is a generic stream-community mechanic).

Commands (`!prediction` is the control surface, `!predict` the viewer shortcut):

- `!prediction open "Will X?" yes no` (mod) -- verb form `!prediction add ...`
- `!predict <outcome> <points>` -- stake on an outcome (name or 1-based number)
- `!prediction lock` (mod) -- close betting now (betting also auto-closes `OPEN_SECONDS` after open)
- `!prediction resolve <outcome>` (mod) -- verb form `!prediction set <outcome>`
- `!prediction cancel` (mod) -- verb form `!prediction remove`; every stake becomes refundable
- `!predict claim [round]` / `!prediction claim [round]` -- collect winnings (or a refund)
- `!prediction` / `!prediction list` -- show the current prediction

## Why payouts are CLAIMED, not pushed

The host only moves the money of the account that triggered the invocation (`economy.wager`'s
`user` must be the invoking actor; see `waddle_sdk.economy`). A moderator's `resolve` therefore
cannot credit the winners. Stakes are debited when placed (`wager(stake, 0)`: the points leave the
bettor and live on only as pool counters in `community_kv`), and each winner collects their share
with their OWN `claim`, which is one `wager(float, share + float)` on the winner's own account:
net `+share`. `float = ceil(share / 99)` is the host's `payout <= 100 x stake` rule solved for the
smallest stake that may carry the payout; it is paid straight back inside the same call, so a
winner needs only 1 point in hand. A share larger than `99 x max_bet(actor)` is collected over
several claims (a per-round `paid` counter tracks what is already out).

## Money integrity (the part that moves points)

- **Conserved, bounded payouts.** `share = floor(stake x pool / winning_pool)` against totals
  FROZEN at resolve time, so `sum(shares) <= pool <= sum(stakes burned)`: the bundle never mints
  more than was staked, and each call mints at most `99 x max_bet`. The host's daily payout (mint)
  caps still apply on top (`quota_exceeded` -> "try again later", reservation released). Caps:
  `MAX_STAKE_PER_ROUND` per bettor, `MAX_ENTRANTS` per round.
- **Pool-first write order.** A bet debits first, then raises the outcome pool, then the bettor's
  stake. A crash in between can only strand stake (pool > sum of stakes -> under-payout), never
  inflate a share. Each bettor backs ONE outcome (no hedging).
- **Time fence instead of a lock.** `kv` has no transaction, so a bet racing the resolver's
  snapshot would break conservation. Bets are refused from `betting_end`; resolve is refused until
  `betting_end + SETTLE_SECONDS`, longer than any in-flight (2 s timeout) bet. Resolving an open
  round just closes betting and asks the moderator to repeat once the fence has passed.
- **Idempotent on redelivery.** The inbound `message_id` (forwarded by `transform`) is claimed via
  `waddle_sdk.event_dedupe` (ONE atomic `kv.increment`, `1` = first delivery) AFTER the cheap
  prechecks and the atomic per-actor cooldown and BEFORE any stake, payout or state change -- for
  every mutating command, moderator ones included. A redelivery is skipped silently; a payload with
  no `message_id` / valid `occurred_at` fails loud and moves nothing. A crash after the claim loses
  one operation (never repeats one). The claim comes after the cooldown, so one chatter's marker
  rate is bounded by its cooldown; a full 10,000-key quota fails CLOSED and heals as markers lapse
  (residual: a swarm of funded accounts -- a host per-actor kv rate limit is the follow-up).
- **Atomic state changes.** Opening, finalizing (resolve/cancel) and claiming reservations are
  single `kv.increment` elections (`opening.<n>`, `finalizing.<n>`, `paid.<n>.<who>`); never a
  read-then-set. A double `resolve` (two events, or two moderators at once) finalizes exactly one
  result: the second sees the stored result and changes nothing. Election markers carry a 60 s TTL
  so a crash mid-way heals itself.
- **Ambiguous host failure.** A `BackendError` from `wager` may or may not have committed: the
  identical call is retried once (the host dedupes by event), after which it fails loud WITHOUT
  releasing the claim reservation (never risk a double payout).

PII-free: kv keys/logs carry only `prediction.*` names, round numbers, outcome indexes and
`<pseudonym>` = SHA-256(community|resolved-uuid)[:32]; per-event markers carry a truncated SHA-256
of the platform-prefixed message id (never the id). Logs carry `op`/`outcome`/`amount_bucket`/
error-case only -- never a title, outcome, name or message text.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs.

Output safety: `event.actor` is interpolated into replies. The host-minted `{user:<token>}` actor
stays live (egress renders the display name); any other value, and every echoed title/outcome,
goes through `waddle_sdk.safe_text.sanitize_user_text`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, NoReturn, TypeVar

from waddle_sdk import clock, community_kv, economy, event_dedupe, identity, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.community_kv import TENANT_WIDE_SENTINEL
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text, strip_control_chars

SPEC = CommandSpec(name="prediction")

MIN_OUTCOMES = 2
MAX_OUTCOMES = 5
MAX_TITLE_LEN = 100
MAX_OUTCOME_LEN = 24
MIN_STAKE = 1
#: Most points one bettor may put on one round (cumulative over their bets).
MAX_STAKE_PER_ROUND = 1000
#: Most distinct bettors per round: bounds the per-round kv footprint (~2 keys per bettor).
MAX_ENTRANTS = 200
#: Betting auto-closes this long after a prediction opens (a moderator may lock earlier).
OPEN_SECONDS = 3600
#: A resolve is refused until betting has been closed this long -- longer than any in-flight bet.
SETTLE_SECONDS = 10
COOLDOWN_SECONDS = 3
#: `economy.MAX_PAYOUT_MULTIPLE - 1`: the most a `wager` can pay per point of (returned) stake.
_FLOAT_DIVISOR = economy.MAX_PAYOUT_MULTIPLE - 1
#: Every round key lives this long (claim window); the host caps a kv TTL at 30 days.
ROUND_TTL_SECONDS = 14 * 24 * 60 * 60
#: Election markers heal after this long if the elected writer crashed mid-way.
_ELECTION_TTL_SECONDS = 60

_DEDUPE_NAMESPACE = "prediction"
_PSEUDONYM_LEN = 32
#: Host denial codes that are ordinary limits (not anomalies): shown to the user, never raised.
_LIMIT_CODES = frozenset({"quota_exceeded", "rate_limited"})
_USAGE = (
    "Usage: !predict <outcome> <points> | !predict claim | !prediction | "
    '!prediction open "question" <outcome> <outcome> ... | lock | resolve <outcome> | cancel '
    "(open/lock/resolve/cancel: moderators only)"
)
_UNAVAILABLE = "predictions are temporarily unavailable."
_KNOWN_COMMANDS = frozenset(
    {"status", "usage", "open", "lock", "resolve", "cancel", "bet", "claim"}
)
_MOD_COMMANDS = frozenset({"open", "lock", "resolve", "cancel"})
#: Domain words matched before the shared verb grammar (`open` etc. are not in `VERBS`).
_DOMAIN_WORDS = {
    "open": "open",
    "lock": "lock",
    "resolve": "resolve",
    "cancel": "cancel",
    "claim": "claim",
}
#: Shared-grammar verbs mapped onto the same actions (`!prediction add "q" a b` == open).
_VERB_ACTIONS = {"add": "open", "set": "resolve", "remove": "cancel", "list": "status"}

#: The pipeline delivers `event.actor` as its own host-minted placeholder; anchored so ONLY an
#: actor that is exactly one `{user:<token>}` is left live (never one embedded in other text).
_USER_TOKEN_RE = re.compile(r"^\{user:([A-Za-z0-9_:.-]+)\}$")
_FALLBACK_ACTOR = "someone"
_ROUND_NO_RE = re.compile(r"^[0-9]{1,9}$")

_T = TypeVar("_T")


class _Phase(IntEnum):
    """Lifecycle of the current round."""

    NONE = 0
    OPEN = 1
    LOCKED = 2
    DONE = 3


@dataclass(slots=True, frozen=True)
class _Round:
    """Immutable definition of one prediction, written once when it is opened."""

    number: int
    title: str
    outcomes: tuple[str, ...]
    opened_ms: int

    def to_bytes(self) -> bytes:
        """Serialize for kv (the round number is the key, not part of the value)."""
        doc = {"t": self.title, "o": list(self.outcomes), "at": self.opened_ms}
        return json.dumps(doc, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, number: int, raw: bytes) -> _Round:
        """Parse a stored definition; any malformed value raises `ValueError` (fail loud)."""
        doc = json.loads(raw.decode())
        title, outs, opened = doc["t"], doc["o"], doc["at"]
        if (
            not isinstance(title, str)
            or not isinstance(outs, list)
            or not all(isinstance(o, str) for o in outs)
            or isinstance(opened, bool)
            or not isinstance(opened, int)
            or not MIN_OUTCOMES <= len(outs) <= MAX_OUTCOMES
        ):
            raise ValueError("corrupt prediction definition")
        return cls(number=number, title=title, outcomes=tuple(outs), opened_ms=opened)


@dataclass(slots=True, frozen=True)
class _Final:
    """The frozen result of a round: written once by the elected finalizer."""

    winner: int  # outcome index; -1 = cancelled
    pool: int  # total staked, frozen at finalization
    winning_pool: int  # staked on the winner; 0 means nobody won -> every stake is refundable

    @property
    def refund_mode(self) -> bool:
        """True when each bettor simply gets their own stake back (cancel / no winners)."""
        return self.winner < 0 or self.winning_pool <= 0

    def to_bytes(self) -> bytes:
        """Serialize for kv."""
        doc = {"w": self.winner, "p": self.pool, "wp": self.winning_pool}
        return json.dumps(doc, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, raw: bytes) -> _Final:
        """Parse a stored result; any malformed value raises `ValueError` (fail loud)."""
        doc = json.loads(raw.decode())
        values = (doc["w"], doc["p"], doc["wp"])
        if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
            raise ValueError("corrupt prediction result")
        winner, pool, winning = values
        if pool < 0 or winning < 0 or winning > pool:
            raise ValueError("corrupt prediction result")
        return cls(winner=winner, pool=pool, winning_pool=winning)


@dataclass(slots=True, frozen=True)
class _View:
    """A consistent-enough read of the current round (definition, lock, result, clock)."""

    number: int
    rnd: _Round | None
    lock_ms: int | None
    final: _Final | None
    now_ms: int

    @property
    def betting_end_ms(self) -> int:
        """When betting closes: the earlier of the auto-close and a moderator lock."""
        if self.rnd is None:
            return 0
        auto = self.rnd.opened_ms + OPEN_SECONDS * 1000
        return auto if self.lock_ms is None else min(auto, self.lock_ms)

    @property
    def phase(self) -> _Phase:
        """The round's lifecycle phase."""
        if self.rnd is None:
            return _Phase.NONE
        if self.final is not None:
            return _Phase.DONE
        return _Phase.LOCKED if self.now_ms >= self.betting_end_ms else _Phase.OPEN


@dataclass(slots=True, frozen=True)
class _Ctx:
    """Everything one dispatch needs about where it runs and who triggered it."""

    scope: str | None
    provider: str
    channel_id: str
    payload: dict[str, Any]
    occurred_at: str
    label: str


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


def _safe(text: str) -> str:
    """Neutralize user-supplied text (title / outcome) before it is echoed into a reply."""
    return sanitize_user_text(text)


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


def _parse_quoted_args(args: str) -> list[str]:
    """Split `args` on unquoted whitespace; quotes group words and a backslash escapes one char."""
    result: list[str] = []
    current = ""
    in_quotes = False
    escaped = False
    for char in args:
        if escaped:
            current += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
        elif char in (" ", "\t") and not in_quotes:
            if current:
                result.append(current)
                current = ""
        else:
            current += char
    if current:
        result.append(current)
    return result


def _outcome_index(text: str, outcomes: tuple[str, ...]) -> int | None:
    """Resolve a typed outcome (case-insensitive name, or 1-based number) to its index."""
    wanted = text.strip().casefold()
    for index, name in enumerate(outcomes):
        if name.casefold() == wanted:
            return index
    if wanted.isascii() and wanted.isdigit() and 1 <= int(wanted) <= len(outcomes):
        return int(wanted) - 1
    return None


def _outcome_list(outcomes: tuple[str, ...]) -> str:
    """Render `1) yes  2) no` for a reply (names sanitized)."""
    return "  ".join(f"{i}) {_safe(name)}" for i, name in enumerate(outcomes, start=1))


def _validate_new_round(args: str | None) -> tuple[str, tuple[str, ...]]:
    """Validate `open` arguments; return `(title, outcomes)` or raise `CommandUsageError`."""
    tokens = _parse_quoted_args(args or "")
    if len(tokens) < 1 + MIN_OUTCOMES:
        raise CommandUsageError(_USAGE)
    title, outcomes = tokens[0].strip(), tuple(t.strip() for t in tokens[1:])
    if len(outcomes) > MAX_OUTCOMES:
        raise CommandUsageError(f"a prediction takes at most {MAX_OUTCOMES} outcomes.")
    if not title or len(title) > MAX_TITLE_LEN:
        raise CommandUsageError(f"the question must be 1-{MAX_TITLE_LEN} characters.")
    folded = [o.casefold() for o in outcomes]
    if len(set(folded)) != len(folded):
        raise CommandUsageError("outcomes must be different from each other.")
    for text in (title, *outcomes):
        if strip_control_chars(text) != text or "\n" in text or "\t" in text:
            raise CommandUsageError("the question and outcomes must be plain text.")
    for out in outcomes:
        if not out or len(out) > MAX_OUTCOME_LEN:
            raise CommandUsageError(f"each outcome must be 1-{MAX_OUTCOME_LEN} characters.")
        if out.isascii() and out.isdigit():
            raise CommandUsageError("an outcome name cannot be only a number.")
    return title, outcomes


def _route(text: str) -> tuple[str, str | None] | None:
    """Map one chat line to `(command, args)`; `None` when it is not ours."""
    stripped = text.strip()
    head, _, tail = stripped.partition(" ")
    head_l, tail = head.lower(), tail.strip()
    if head_l == "!predict":
        if not tail:
            return "status", None
        first, _, rest = tail.partition(" ")
        if first.lower() == "claim":
            return "claim", rest.strip() or None
        return "bet", tail
    if head_l != "!prediction":
        return None
    if not tail:
        return "status", None
    first, _, rest = tail.partition(" ")
    if first.lower() in _DOMAIN_WORDS:
        return _DOMAIN_WORDS[first.lower()], rest.strip() or None
    try:
        parsed = parse_command(stripped, SPEC)
    except CommandUsageError:
        log.debug("prediction.transform usage", reason="unknown_option")
        return "usage", None
    action = _VERB_ACTIONS.get(parsed.option or "")
    return (action, parsed.args) if action else ("usage", None)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!prediction ...` / `!predict ...`.

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
    log.info("prediction.transform matched", command=command)
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
    """Build a `.`-joined kv key under `prediction.` (kv forbids `:`)."""
    return ".".join(("prediction", *(str(p) for p in parts)))


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
        log.error("prediction.error", op=op, error=case_name)
        await relay.push(self._provider, {"channel": self._channel_id, "text": _UNAVAILABLE})
        raise RuntimeError(f"prediction {op} failed: {case_name}") from exc

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

    async def set_raw(self, key: str, value: bytes, ttl: int = ROUND_TTL_SECONDS) -> None:
        """Write a value with a TTL."""
        await self._guard("set", community_kv.set(self._scope, key, value, ttl))

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
        count = await self.incr(_k("cd", pseudonym), 1, COOLDOWN_SECONDS)
        return count == 1

    async def elect(self, key: str) -> bool:
        """Win a short-lived single-writer election (`increment` returns 1 for exactly one)."""
        return await self.incr(key, 1, _ELECTION_TTL_SECONDS) == 1

    async def load_round(self, number: int) -> _Round | None:
        """Load a round definition (`None` if absent/expired; corrupt fails loud)."""
        raw = await self.get_raw(_k("def", number))
        if raw is None:
            return None
        try:
            return _Round.from_bytes(number, raw)
        except (ValueError, KeyError, TypeError) as exc:
            await self.fail(exc, "corrupt")

    async def load_final(self, number: int) -> _Final | None:
        """Load a round's frozen result (`None` while unresolved; corrupt fails loud)."""
        raw = await self.get_raw(_k("fin", number))
        if raw is None:
            return None
        try:
            return _Final.from_bytes(raw)
        except (ValueError, KeyError, TypeError) as exc:
            await self.fail(exc, "corrupt")

    async def view(self) -> _View:
        """Read the current round's definition, lock and result plus the clock."""
        number = await self.get_int(_k("round")) or 0
        now = await self.now_ms()
        if number <= 0:
            return _View(0, None, None, None, now)
        rnd = await self.load_round(number)
        if rnd is None:
            return _View(number, None, None, None, now)
        lock = await self.get_int(_k("lock", number))
        final = await self.load_final(number)
        if final is not None and final.winner >= len(rnd.outcomes):
            await self.fail(ValueError("result names a missing outcome"), "corrupt")
        return _View(number, rnd, lock, final, now)

    async def pools(self, number: int, count: int) -> list[int]:
        """Points staked on each outcome (never negative)."""
        return [max(await self.get_int(_k("pool", number, k)) or 0, 0) for k in range(count)]

    async def stakes(self, number: int, pseudonym: str, count: int) -> list[int]:
        """This bettor's stake on each outcome (never negative)."""
        return [
            max(await self.get_int(_k("stk", number, k, pseudonym)) or 0, 0) for k in range(count)
        ]


async def _wager(actor_uuid: str, stake: int, payout: int) -> int:
    """`economy.wager`, retrying the identical call once after an ambiguous `BackendError`."""
    try:
        return await economy.wager(actor_uuid, stake, payout)
    except economy.BackendError:
        log.error("prediction.error", op="wager", error="BackendError", retry=True)
        return await economy.wager(actor_uuid, stake, payout)


def _due(rnd: _Round, final: _Final, stakes: list[int]) -> int:
    """What this bettor may collect: their own stake (refund) or their proportional share."""
    if final.refund_mode:
        return sum(stakes)
    if not 0 <= final.winner < len(rnd.outcomes) or final.winner >= len(stakes):
        return 0
    return stakes[final.winner] * final.pool // final.winning_pool


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
            log.info("prediction.stale_event_skipped", op="fence")
            return None
        if await self.store.seen(event_id):
            log.info("prediction.redelivery_skipped", op="seen")
            return None
        return event_id

    async def _claim(self, event_id: str) -> bool:
        """Claim the event before the first mutation; False = duplicate (skip silently)."""
        if await self.store.claim_event(event_id):
            return True
        log.info("prediction.redelivery_skipped", op="claim")
        return False

    async def _actor(self) -> tuple[str, str] | str:
        """Resolve `(actor_uuid, pseudonym)` or return a chat reply for a refusal."""
        label = self.ctx.label
        try:
            actor_uuid = await identity.resolve_actor()
        except identity.NotLinkedError:
            log.info("prediction.denied", op="identity", error="NotLinkedError")
            return f"{label}, link your account before using predictions."
        except (identity.IdentityError, economy.DeniedError, economy.NotAMemberError) as exc:
            log.info("prediction.denied", op="identity", error=type(exc).__name__)
            return f"{label}, you can't use predictions right now."
        return actor_uuid, _pseudonym(self.ctx.scope, actor_uuid)

    async def status(self) -> str:
        """Show the current prediction with live pools and shares."""
        view = await self.store.view()
        rnd = view.rnd
        if rnd is None or view.phase is _Phase.NONE:
            return "no prediction yet. A moderator can start one with !prediction open."
        pools = await self.store.pools(view.number, len(rnd.outcomes))
        total = sum(pools)
        parts = []
        for i, name in enumerate(rnd.outcomes):
            share = pools[i] * 100 // total if total else 0
            parts.append(f"{i + 1}) {_safe(name)} {pools[i]} pts ({share}%)")
        board = " | ".join(parts)
        head = f'\U0001f52e Prediction #{view.number}: "{_safe(rnd.title)}"'
        if view.final is not None:
            if view.final.winner < 0:
                state = "cancelled -- stakes refundable with !predict claim"
            else:
                state = (
                    f"resolved: {_safe(rnd.outcomes[view.final.winner])} -- "
                    "winners collect with !predict claim"
                )
        elif view.phase is _Phase.LOCKED:
            state = "betting closed, awaiting the result"
        else:
            mins = max((view.betting_end_ms - view.now_ms) // 60000, 0)
            state = f"betting open ({mins}m left) -- !predict <outcome> <points>"
        return f"{head} -- {state} | {board} | pool {total} pts"

    async def open(self, args: str | None) -> str | None:
        """Open a new prediction (moderators only)."""
        title, outcomes = _validate_new_round(args)
        event_id = await self._gate()
        if event_id is None:
            return None
        view = await self.store.view()
        if view.phase in (_Phase.OPEN, _Phase.LOCKED):
            return f"{self.ctx.label}, prediction #{view.number} is still running."
        if not await self._claim(event_id):
            return None
        view = await self.store.view()  # authoritative re-read after the claim
        if view.phase in (_Phase.OPEN, _Phase.LOCKED):
            return f"{self.ctx.label}, prediction #{view.number} is still running."
        number = view.number + 1
        if not await self.store.elect(_k("opening", number)):
            return f"{self.ctx.label}, another prediction is being opened right now."
        rnd = _Round(number=number, title=title, outcomes=outcomes, opened_ms=view.now_ms)
        await self.store.set_raw(_k("def", number), rnd.to_bytes())
        if await self.store.incr(_k("round"), 1, 0) != number:
            await self.store.fail(RuntimeError("round counter moved"), "round")
        log.info("prediction.opened", outcome="open", count=len(outcomes))
        return (
            f'\U0001f52e Prediction #{number} is open: "{_safe(title)}" -- '
            f"{_outcome_list(outcomes)}. Bet with !predict <outcome> <points>."
        )

    async def lock(self) -> str | None:
        """Close betting now (moderators only); idempotent."""
        event_id = await self._gate()
        if event_id is None:
            return None
        view = await self.store.view()
        refused = self._not_running(view)
        if refused is not None:
            return refused
        if view.phase is _Phase.LOCKED:
            return f"betting on prediction #{view.number} is already closed."
        if not await self._claim(event_id):
            return None
        await self.store.set_raw(_k("lock", view.number), str(view.now_ms).encode())
        log.info("prediction.locked", outcome="lock")
        return f"\U0001f512 Betting on prediction #{view.number} is closed."

    def _not_running(self, view: _View) -> str | None:
        """A refusal reply when there is no live (unfinished) prediction, else `None`."""
        if view.phase is _Phase.NONE:
            return f"{self.ctx.label}, there is no prediction running."
        if view.phase is _Phase.DONE:
            return f"{self.ctx.label}, prediction #{view.number} is already finished."
        return None

    async def resolve(self, args: str | None) -> str | None:
        """Resolve the prediction to one outcome (moderators only); a double resolve is a no-op."""
        event_id = await self._gate()
        if event_id is None:
            return None
        view = await self.store.view()
        rnd, label = view.rnd, self.ctx.label
        if view.phase is _Phase.DONE and rnd is not None and view.final is not None:
            return self._already_finished(view, rnd, view.final)
        if rnd is None or view.phase is _Phase.NONE:
            return f"{label}, there is no prediction running."
        index = _outcome_index(" ".join(_parse_quoted_args(args or "")), rnd.outcomes)
        if index is None:
            return f"{label}, pick the winning outcome: {_outcome_list(rnd.outcomes)}"
        if not await self._claim(event_id):
            return None
        fence_ms = view.betting_end_ms + SETTLE_SECONDS * 1000
        if view.now_ms < view.betting_end_ms:
            await self.store.set_raw(_k("lock", view.number), str(view.now_ms).encode())
            return (
                f"\U0001f512 Betting is now closed. Run the resolve again in {SETTLE_SECONDS}s "
                "so every in-flight bet is counted."
            )
        if view.now_ms < fence_ms:
            wait = (fence_ms - view.now_ms + 999) // 1000
            return f"{label}, wait {wait}s for in-flight bets to settle, then resolve again."
        if not await self.store.elect(_k("finalizing", view.number)):
            return f"{label}, this prediction is being finalized right now."
        pools = await self.store.pools(view.number, len(rnd.outcomes))
        final = _Final(winner=index, pool=sum(pools), winning_pool=pools[index])
        await self.store.set_raw(_k("fin", view.number), final.to_bytes())
        log.info("prediction.resolved", outcome="resolve", amount_bucket=_amount_bucket(final.pool))
        if final.refund_mode:
            tail = "nobody backed it -- every stake is refundable with !predict claim."
        else:
            tail = f"{final.pool} points go to the winners -- collect with !predict claim."
        return f"\u2705 Prediction #{view.number} resolved: {_safe(rnd.outcomes[index])}! {tail}"

    def _already_finished(self, view: _View, rnd: _Round, final: _Final) -> str:
        """The idempotent reply for resolving/cancelling a round that already has a result."""
        if final.winner < 0:
            return f"prediction #{view.number} was already cancelled."
        return (
            f"prediction #{view.number} is already resolved: {_safe(rnd.outcomes[final.winner])}."
        )

    async def cancel(self) -> str | None:
        """Cancel the prediction (moderators only): every stake becomes refundable."""
        event_id = await self._gate()
        if event_id is None:
            return None
        view = await self.store.view()
        if view.phase is _Phase.DONE and view.rnd is not None and view.final is not None:
            return self._already_finished(view, view.rnd, view.final)
        refused = self._not_running(view)
        if refused is not None:
            return refused
        if not await self._claim(event_id):
            return None
        if not await self.store.elect(_k("finalizing", view.number)):
            return f"{self.ctx.label}, this prediction is being finalized right now."
        await self.store.set_raw(_k("fin", view.number), _Final(-1, 0, 0).to_bytes())
        log.info("prediction.cancelled", outcome="cancel")
        return (
            f"\U0001f6d1 Prediction #{view.number} cancelled -- "
            "collect your stake with !predict claim."
        )

    async def bet(self, args: str | None) -> str | None:
        """Stake points on one outcome; the debit happens before any pool state changes."""
        label = self.ctx.label
        tokens = _parse_quoted_args(args or "")
        if len(tokens) < 2 or not (tokens[-1].isascii() and tokens[-1].isdigit()):
            return _USAGE
        amount, text = int(tokens[-1]), " ".join(tokens[:-1])
        if amount < MIN_STAKE:
            return f"{label}, stake at least {MIN_STAKE} point."
        if amount > MAX_STAKE_PER_ROUND:
            return f"{label}, the most you can stake is {MAX_STAKE_PER_ROUND} points."
        who = await self._actor()
        if isinstance(who, str):
            return who
        actor_uuid, pseudonym = who
        event_id = await self._gate()
        if event_id is None:
            return None
        refusal = await self._bet_prechecks(actor_uuid, amount, text)
        if isinstance(refusal, str):
            return refusal
        if not await self.store.cooldown_claim(pseudonym):
            return f"slow down, {label}! try again in a few seconds."
        if not await self._claim(event_id):
            return None
        return await self._place_bet(actor_uuid, pseudonym, amount, text)

    async def _bet_prechecks(self, actor_uuid: str, amount: int, text: str) -> str | None:
        """Read-only checks (round open, outcome valid, funds, cap); a reply when refused."""
        label = self.ctx.label
        view = await self.store.view()
        refused = self._bettable(view)
        if refused is not None or view.rnd is None:
            return refused
        if _outcome_index(text, view.rnd.outcomes) is None:
            return f"{label}, pick one of: {_outcome_list(view.rnd.outcomes)}"
        try:
            held = await economy.balance(actor_uuid)
            if amount > held:
                return f"{label}, you only have {held} points."
            cap = await economy.max_bet(actor_uuid)
        except (economy.DeniedError, economy.NotAMemberError) as exc:
            log.info("prediction.denied", op="precheck", error=type(exc).__name__)
            return f"{label}, you can't stake right now."
        except economy.EconomyError as exc:
            await self.store.fail(exc, "precheck")
        if amount > cap:
            return f"{label}, the max stake right now is {cap} points."
        return None

    def _bettable(self, view: _View) -> str | None:
        """A refusal reply unless the round is open for bets."""
        label = self.ctx.label
        if view.phase is _Phase.NONE:
            return f"{label}, there is no prediction open."
        if view.phase is _Phase.DONE:
            return f"{label}, prediction #{view.number} is already finished."
        if view.phase is _Phase.LOCKED:
            return f"{label}, betting on prediction #{view.number} is closed."
        return None

    async def _place_bet(self, actor_uuid: str, pseudonym: str, amount: int, text: str) -> str:
        """Authoritative bet: re-check under the cooldown claim, debit, then pool, then stake."""
        label, store = self.ctx.label, self.store
        view = await store.view()  # re-read: the round may have closed since the precheck
        refused = self._bettable(view)
        if refused is not None or view.rnd is None:
            return refused or f"{label}, there is no prediction open."
        rnd = view.rnd
        index = _outcome_index(text, rnd.outcomes)
        if index is None:
            return f"{label}, pick one of: {_outcome_list(rnd.outcomes)}"
        mine = await store.stakes(view.number, pseudonym, len(rnd.outcomes))
        if any(s > 0 for j, s in enumerate(mine) if j != index):
            return f"{label}, you already backed a different outcome of this prediction."
        if mine[index] + amount > MAX_STAKE_PER_ROUND:
            return f"{label}, you can stake at most {MAX_STAKE_PER_ROUND} points per prediction."
        first = sum(mine) == 0
        if first and await store.incr(_k("ent", view.number), 1) > MAX_ENTRANTS:
            await store.incr(_k("ent", view.number), -1)
            return f"{label}, this prediction is full."
        try:
            await _wager(actor_uuid, amount, 0)
        except economy.BackendError as exc:
            await store.fail(exc, "wager")  # ambiguous: keep the entrant slot, never retry blind
        except economy.EconomyError as exc:
            if first:
                await store.incr(_k("ent", view.number), -1)
            return await self._refuse_wager(exc, "stake")
        # Pool first, stake second: a crash between them strands stake, never inflates a share.
        await store.incr(_k("pool", view.number, index), amount)
        await store.incr(_k("stk", view.number, index, pseudonym), amount)
        log.info("prediction.bet", outcome="bet", amount_bucket=_amount_bucket(amount))
        return (
            f"\U0001f3af {label} staked {amount} points on {_safe(rnd.outcomes[index])} "
            f"(total {mine[index] + amount})."
        )

    async def _refuse_wager(self, exc: economy.EconomyError, what: str) -> str:
        """Reply for a host refusal of a wager; anything unexpected fails loud (log, reply, raise).

        A short balance, an over-cap amount and a daily quota/rate limit are ordinary refusals. A
        denied grant, actor mismatch, idempotency conflict or an unprovisioned economy are
        anomalies that must never be swallowed into a polite reply.
        """
        label = self.ctx.label
        if isinstance(exc, economy.InsufficientFundsError):
            log.info("prediction.denied", op="wager", error=type(exc).__name__)
            return f"{label}, you only have {exc.balance} points."
        if isinstance(exc, economy.OverCapError):
            log.info("prediction.denied", op="wager", error=type(exc).__name__)
            return f"{label}, the max {what} right now is {exc.cap} points."
        if isinstance(exc, economy.DeniedError) and exc.code in _LIMIT_CODES:
            log.info("prediction.denied", op="wager", error=exc.code)
            return f"{label}, the daily points limit is reached; try again later."
        await self.store.fail(exc, "wager")

    async def claim(self, args: str | None) -> str | None:
        """Collect winnings (or a refund) from a finished prediction, `99 x max_bet` per call."""
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
            log.info("prediction.denied", op="claim", error=type(exc).__name__)
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
        paid_key = _k("paid", number, pseudonym)
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
        log.info("prediction.collected", outcome="claim", amount_bucket=_amount_bucket(chunk))
        left = due - paid - chunk
        more = f" ({left} more -- claim again)" if left > 0 else ""
        return f"\U0001f3c6 {label} collected {chunk} points{more}."

    async def _collectable(
        self, explicit: int | None, pseudonym: str
    ) -> tuple[int, int, int] | None:
        """Find `(round, due, already_paid)` of the first finished round this actor can collect."""
        store = self.store
        current = await store.get_int(_k("round")) or 0
        candidates = [explicit] if explicit else [n for n in (current, current - 1) if n >= 1]
        for number in candidates:
            rnd = await store.load_round(number)
            final = await store.load_final(number)
            if rnd is None or final is None:
                continue
            stakes = await store.stakes(number, pseudonym, len(rnd.outcomes))
            due = _due(rnd, final, stakes)
            paid = max(await store.get_int(_k("paid", number, pseudonym)) or 0, 0)
            if due - paid > 0:
                return number, due, paid
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
        raise ValueError("prediction reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f"unrecognized prediction command: {command!r}")
    if envelope.community == "":
        log.error("prediction.missing_community", command=command)
        raise ValueError("prediction requires a community context (None = tenant-wide, never '')")
    provider = envelope.event.platform
    ctx = _Ctx(
        scope=envelope.community,
        provider=provider,
        channel_id=channel_id,
        payload=payload,
        occurred_at=envelope.event.occurred_at,
        label=_actor_label(envelope.event.actor),
    )
    game = _Game(_Store(ctx.scope, provider, channel_id), ctx)
    arg = payload.get("args")
    args = arg if isinstance(arg, str) else None
    reply: str | None
    if command in _MOD_COMMANDS and not _is_privileged(payload):
        log.info("prediction.denied", op="permission", command=command)
        reply = f"{ctx.label}, only moderators and the broadcaster can do that."
    else:
        try:
            reply = await _run(game, command, args)
        except CommandUsageError as exc:
            reply = str(exc)
    if reply is None:
        log.info("prediction.dispatch skipped duplicate", platform=provider, command=command)
        return DispatchResult(transport=provider, detail="duplicate")
    await relay.push(provider, {"channel": channel_id, "text": reply})
    log.info("prediction.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)


async def _run(game: _Game, command: str, args: str | None) -> str | None:
    """Route one validated command to its flow."""
    if command == "status":
        return await game.status()
    if command == "usage":
        return _USAGE
    if command == "open":
        return await game.open(args)
    if command == "lock":
        return await game.lock()
    if command == "resolve":
        return await game.resolve(args)
    if command == "cancel":
        return await game.cancel()
    if command == "bet":
        return await game.bet(args)
    return await game.claim(args)
