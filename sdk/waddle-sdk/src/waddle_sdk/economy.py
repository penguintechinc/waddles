"""Community currency over the WIT ``economy`` import (issue #714).

``stage-next``-only: ``wit/waddle-bundle/stage.wit``'s ``interface economy``
exists only in ``world stage-next`` (a bundle must be built against that
world and hold ``economy.read`` for :func:`balance`/:func:`leaderboard`,
``economy.wager`` for :func:`wager`/:func:`max_bet`, ``economy.transfer`` for
:func:`transfer`). The community, tenant and app are always server-derived --
a bundle names only the TARGET user(s) (UUID strings) and amounts. The host
verifies every named user is an active member of the invocation's community,
bounds amounts with the economy's own quotas and the community-declared
``max_bet``/``max_amount``, and moves money in single atomic statements that
can never overdraw a balance.

**Only the invocation's own actor's money moves.** The account whose funds a
call moves -- :func:`wager`'s ``user``, :func:`transfer`'s ``from_user`` -- must
be the account that triggered the invocation (the user
``waddle_sdk.identity.resolve_actor()`` returns); anything else raises
:class:`DeniedError` with ``code == "actor_mismatch"`` and writes nothing. A
bundle can pay the actor's money TO any member, never pull another member's.
The total a user's wagers may PAY OUT per day is capped (the mint cap is on the
payout, not the stake): :class:`DeniedError` ``quota_exceeded``.

**Idempotent per event.** The host keys every ``wager``/``transfer`` by the
event being handled plus the call's ordinal, so an event the platform
redelivers credits ONCE (the replayed call returns the original result and
moves nothing). A replay whose parameters differ from the original raises
:class:`DeniedError` ``idempotency_conflict`` and is never applied. After a
:class:`BackendError` -- the one outcome that may or may not have committed --
retry the SAME call; it presents the same key and applies at most once.

**Eager import (componentize-py).** componentize-py only wizens a WIT
host-import submodule that something imports at Python MODULE-LOAD time (see
``_component_entry.py`` and the project note on eager wizening): a capability
referenced only inside function bodies is absent from ``wit_world.imports`` at
runtime despite the component declaring it. The ``from wit_world.imports
import economy`` just below forces that binding to be wizened; it is
try/except-guarded because host-side unit tests (and a ``stage`` 1.0.0 build)
legitimately have no such module.

**Binding shapes** (componentize-py ``bindings`` for the committed WIT):
``balance(user: str) -> int``, ``wager(user: str, stake: int, payout: int) ->
int``, ``transfer(from_user: str, to_user: str, amount: int) -> None``,
``max_bet(user: str) -> int``, ``leaderboard(limit: int) -> List[Entry]``
(``Entry(user: str, balance: int)``), each raising the generated ``Err``
(``.value`` = the ``economy.error`` variant: ``Error_Denied(value: str)``,
``Error_InsufficientFunds(value: int)``, ``Error_OverCap(value: int)``,
``Error_NotAMember``, ``Error_Invalid(value: str)``,
``Error_Unavailable(value: str)``, ``Error_Backend(value: str)``). This module
classifies it structurally via ``getattr(exc, "value", exc)`` (same pattern as
``waddle_sdk.reputation``) and re-raises one of this module's exceptions -- a
refusal is NEVER swallowed into a default balance or a silent no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

try:  # eager: forces componentize-py to wizen the binding -- see module docstring
    from wit_world.imports import economy as _economy_binding  # noqa: F401
except (ImportError, AttributeError):  # host-side tests / stage-1.0.0 world: no such binding
    _economy_binding = None

#: Mirrors ``core/bundle_host_economy/src/lib.rs::MAX_LEADERBOARD_LIMIT``
#: (pinned by ``tests/test_economy.py::test_limits_match_host_lib_rs``).
MAX_LEADERBOARD_LIMIT = 100

#: Mirrors ``core/bundle_host_economy/src/lib.rs::MAX_PAYOUT_MULTIPLE``: a
#: wager's payout may be at most this many times its stake (the host enforces
#: it and answers :class:`OverCapError`; the constant is exposed for bundles
#: that want to pre-compute a legal payout).
MAX_PAYOUT_MULTIPLE = 100

#: Largest amount the host accepts (balances are ``BIGINT``).
MAX_AMOUNT = 2**63 - 1


class EconomyError(Exception):
    """Base for every error this facade raises."""


class InvalidEconomyArgError(EconomyError, ValueError):
    """An argument is malformed (raised before any host call, or by the host as ``invalid``)."""


class DeniedError(EconomyError):
    """The host gate denied the call; ``code`` is the gate's stable denial code.

    e.g. ``not_granted``, ``amount_out_of_bounds``, ``quota_exceeded``,
    ``rate_limited``, ``instance_denied`` -- and the money-safety refusals
    ``actor_mismatch`` (the payer is not the invocation's actor),
    ``idempotency_conflict`` (a replay's parameters differ from the original)
    and ``not_linked`` (the actor has no resolved community identity).
    """

    def __init__(self, message: str, code: str) -> None:
        """Store the gate's stable denial ``code`` alongside the message."""
        super().__init__(message)
        self.code = code


class InsufficientFundsError(EconomyError):
    """The debited user holds less than the stake/amount; ``balance`` is what they hold."""

    def __init__(self, message: str, balance: int) -> None:
        """Store the balance the user actually holds."""
        super().__init__(message)
        self.balance = balance


class OverCapError(EconomyError):
    """The stake/amount (or a payout) exceeds the server-enforced cap; ``cap`` is that cap."""

    def __init__(self, message: str, cap: int) -> None:
        """Store the cap the host enforces."""
        super().__init__(message)
        self.cap = cap


class NotAMemberError(EconomyError):
    """A named user is not an active member of this invocation's community."""


class UnavailableError(EconomyError):
    """The capability is unprovisioned or its feature flag is off on this host."""


class BackendError(EconomyError):
    """Host-side storage failure (details are deliberately not exposed to bundles)."""


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    """One leaderboard row: a member (UUID string only -- no names) and their balance."""

    user: str
    balance: int


def validate_user(user: str) -> str:
    """Return ``user`` normalized to canonical lowercase hyphenated UUID text, or raise."""
    try:
        return str(UUID(str(user)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidEconomyArgError(f"user must be a UUID string, got {user!r}") from exc


def validate_amount(name: str, value: int, *, minimum: int) -> int:
    """Return ``value`` if it is a non-bool int in ``minimum..=MAX_AMOUNT``, else raise."""
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= MAX_AMOUNT:
        raise InvalidEconomyArgError(
            f"{name} must be an integer in {minimum}..={MAX_AMOUNT}, got {value!r}"
        )
    return value


def _raise_for(exc: Exception, op: str) -> None:
    """Classify a raised WIT ``economy.error`` and re-raise as this module's own exception."""
    detail = getattr(exc, "value", exc)
    type_name = type(detail).__name__
    payload = getattr(detail, "value", None)
    message = f"economy.{op} failed: {type_name}" + (f": {payload}" if payload is not None else "")
    if type_name == "Error_Denied":
        raise DeniedError(message, code=str(payload)) from exc
    if type_name == "Error_InsufficientFunds":
        raise InsufficientFundsError(message, balance=int(payload or 0)) from exc
    if type_name == "Error_OverCap":
        raise OverCapError(message, cap=int(payload or 0)) from exc
    if type_name == "Error_NotAMember":
        raise NotAMemberError(message) from exc
    if type_name == "Error_Invalid":
        raise InvalidEconomyArgError(message) from exc
    if type_name == "Error_Unavailable":
        raise UnavailableError(message) from exc
    raise BackendError(message) from exc


async def balance(user: str) -> int:
    """Return ``user``'s community balance (0 for a member who holds nothing yet)."""
    canonical = validate_user(user)
    import wit_world

    try:
        return int(wit_world.imports.economy.balance(canonical))
    except EconomyError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        _raise_for(exc, "balance")
        raise  # unreachable -- _raise_for always raises


async def wager(user: str, stake: int, payout: int) -> int:
    """Atomically debit ``stake`` and credit ``payout`` (0 for a loss); return the NEW balance.

    The bundle decides the game outcome; the host bounds it: ``user`` must be
    the invocation's actor, ``1 <= stake <= max_bet``, the user must hold
    ``stake``, ``payout <= stake * MAX_PAYOUT_MULTIPLE``, and the payout counts
    against the daily mint cap. Replaying the same event returns the original
    balance and credits once. Raises one of this module's :class:`EconomyError`
    subclasses on any refusal -- notably :class:`InsufficientFundsError`,
    :class:`OverCapError` and :class:`DeniedError`.
    """
    canonical = validate_user(user)
    stake = validate_amount("stake", stake, minimum=1)
    payout = validate_amount("payout", payout, minimum=0)
    import wit_world

    try:
        return int(wit_world.imports.economy.wager(canonical, stake, payout))
    except EconomyError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        _raise_for(exc, "wager")
        raise  # unreachable -- _raise_for always raises


async def transfer(from_user: str, to_user: str, amount: int) -> None:
    """Atomically move ``amount`` from ``from_user`` to ``to_user`` (distinct members).

    ``from_user`` must be the invocation's actor (``DeniedError`` ``actor_mismatch``
    otherwise); ``to_user`` may be any member. Replaying the same event moves
    nothing a second time.
    """
    sender = validate_user(from_user)
    recipient = validate_user(to_user)
    if sender == recipient:
        raise InvalidEconomyArgError("cannot transfer to oneself")
    amount = validate_amount("amount", amount, minimum=1)
    import wit_world

    try:
        wit_world.imports.economy.transfer(sender, recipient, amount)
    except EconomyError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        _raise_for(exc, "transfer")
        raise  # unreachable -- _raise_for always raises


async def max_bet(user: str) -> int:
    """Return the largest stake ``user`` may place now: the server cap, bounded by their balance."""
    canonical = validate_user(user)
    import wit_world

    try:
        return int(wit_world.imports.economy.max_bet(canonical))
    except EconomyError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        _raise_for(exc, "max_bet")
        raise  # unreachable -- _raise_for always raises


async def leaderboard(limit: int) -> list[LeaderboardEntry]:
    """Return the community's top ``limit`` (1..=100) members by balance, highest first."""
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_LEADERBOARD_LIMIT
    ):
        raise InvalidEconomyArgError(f"limit must be 1..={MAX_LEADERBOARD_LIMIT}, got {limit!r}")
    import wit_world

    try:
        rows = wit_world.imports.economy.leaderboard(limit)
    except EconomyError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        _raise_for(exc, "leaderboard")
        raise  # unreachable -- _raise_for always raises
    return [LeaderboardEntry(user=str(r.user), balance=int(r.balance)) for r in rows]
