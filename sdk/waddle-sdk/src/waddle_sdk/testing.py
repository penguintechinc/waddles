"""Shared, charset-enforcing test double for the `kv` host capability (gh-631).

**Why this module exists.** Every bundle test suite needs a stand-in for
``wit_world.imports.kv`` (no real componentize-py/wasmtime build runs in
unit tests). Before this module, each bundle hand-rolled its own in-memory
fake (`count`, `lurk`, `sdk/waddle-sdk/tests/test_community_kv.py`,
`test_sub_modules.py` all did), and every one of them accepted *any* string
as a key. The real host capability
(`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`) rejects any key
containing a byte outside ASCII alnum + ``_``/``-``/``.`` -- most
importantly ``:``, its own reserved namespace separator. `count`'s
`"count:registry"` and `lurk`'s `"lurk:state:{community}:{pseudonym}"` both
passed every test against a permissive fake and then failed on the real
host in production with `kv.error::backend` (gh-631).

Use :func:`install_fake_kv_host` (or the exported :class:`FakeKvHost`
directly) instead of hand-rolling a fake: it enforces the exact same
charset as ``waddle_sdk.kv.validate_key`` -- which is itself mirrored from
``scope.rs`` and pinned to it by
``sdk/waddle-sdk/tests/test_kv.py::test_allowed_key_charset_matches_host_scope_rs``
-- so a colon (or any other host-rejected byte) in a bundle's key fails the
test suite immediately instead of hiding a production-only bug.

Usage (pytest)::

    from waddle_sdk.testing import install_fake_kv_host

    @pytest.fixture
    def fake_kv(monkeypatch):
        return install_fake_kv_host(monkeypatch)

    def test_something(fake_kv):
        ...
        assert fake_kv.store["my.key"] == b"value"
"""

from __future__ import annotations

import dataclasses
import json
import sys
import types
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from waddle_sdk.kv import InvalidKvKeyError, validate_key

__all__ = [
    "ACTION_ONLY_CAPABILITIES",
    "PROCESS_ONLY_CAPABILITIES",
    "FakeAiHost",
    "FakeEconomyHost",
    "FakeIdentityHost",
    "FakeKvHost",
    "FakeReputationHost",
    "InvalidKvKeyError",
    "StageGuard",
    "StageViolationError",
    "install_fake_ai_host",
    "install_fake_economy_host",
    "install_fake_identity_host",
    "install_fake_kv_host",
    "install_fake_reputation_host",
    "rfc3339_from_millis",
]


def rfc3339_from_millis(millis: int) -> str:
    """Render epoch milliseconds as the RFC 3339 ``...Z`` stamp ``svc-ingest`` puts on an event.

    For tests that must give an event an ``occurred_at`` consistent with a fake clock (the age
    fence in ``waddle_sdk.event_dedupe`` compares it to ``clock.now_millis()``).
    """
    stamp = datetime.fromtimestamp(millis // 1000, tz=UTC)
    return f"{stamp:%Y-%m-%dT%H:%M:%S}.{millis % 1000:03d}Z"


@dataclass(slots=True)
class FakeKvHost:
    """In-memory stand-in for `wit_world.imports.kv`, validating every key like the real host.

    `store`/`calls` are public so a test can seed state or assert on exact
    host calls, matching the shape every hand-rolled fake already used
    (`.store[key]`, `.calls == [("get", (key,)), ...]`) -- migrating an
    existing bundle test suite onto this fake is a near drop-in swap.
    """

    store: dict[str, bytes] = field(default_factory=dict)
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def get(self, key: str) -> bytes | None:
        """Return the stored value for `key`, or `None` if unset. Validates `key` first."""
        validate_key(key)
        self.calls.append(("get", (key,)))
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        """Store `value` under `key` (TTL recorded but not enforced). Validates `key` first."""
        validate_key(key)
        self.calls.append(("set", (key, bytes(value), ttl_seconds)))
        self.store[key] = bytes(value)

    def delete(self, key: str) -> None:
        """Delete `key` if present. Validates `key` first."""
        validate_key(key)
        self.calls.append(("delete", (key,)))
        self.store.pop(key, None)

    def increment(self, key: str, delta: int, ttl_seconds: int) -> int:
        """Add `delta` to the value under `key`, return the new total. Validates `key` first."""
        validate_key(key)
        self.calls.append(("increment", (key, delta, ttl_seconds)))
        current = int(self.store.get(key, b"0"))
        new_value = current + delta
        self.store[key] = str(new_value).encode()
        return new_value


def install_fake_kv_host(monkeypatch: Any) -> FakeKvHost:
    """Install a fresh `FakeKvHost` as `wit_world.imports.kv` and return it.

    `monkeypatch` takes `pytest.MonkeyPatch` (left untyped so importing this
    module never requires pytest to be installed outside test environments).
    """
    host = FakeKvHost()
    kv_mod = types.SimpleNamespace(
        get=host.get, set=host.set, delete=host.delete, increment=host.increment
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(kv=kv_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return host


@dataclass(frozen=True)
class _FakeWitError(Exception):
    """Stand-in for componentize-py's generated ``Err`` wrapper (``.value`` = the error variant)."""

    value: Any


def _variant(name: str, with_payload: bool) -> type:
    """Build a class named exactly like a generated ``reputation.Error_*`` variant case."""
    if with_payload:

        @dataclass
        class _Case:
            value: str

    else:

        @dataclass
        class _Case:  # type: ignore[no-redef]
            pass

    _Case.__name__ = name
    _Case.__qualname__ = name
    return _Case


_Error_Denied = _variant("Error_Denied", True)
_Error_NotAMember = _variant("Error_NotAMember", False)
_Error_DailyCapExceeded = _variant("Error_DailyCapExceeded", False)
_Error_Invalid = _variant("Error_Invalid", True)
_Error_Unavailable = _variant("Error_Unavailable", True)
_Error_Backend = _variant("Error_Backend", True)


@dataclass(slots=True)
class FakeReputationHost:
    """In-memory stand-in for `wit_world.imports.reputation`, enforcing the real host's rules.

    Mirrors what the real stage does (gate + store): the target must be in
    `members`; `granted` False denies every call `not_granted`; one adjust may
    not exceed `per_call_abs_max`; the rolling total per user may not exceed
    `daily_abs_cap` (the catalog's `reputation.community.write` ceilings are 5
    and 5 -- the defaults here). Every failure raises the same-named
    `Error_*` variant the generated binding would, wrapped in an `Err`-shaped
    exception, so a bundle's error handling is exercised for real.
    """

    members: set[str] = field(default_factory=set)
    balances: dict[str, int] = field(default_factory=dict)
    ledger: list[tuple[str, int, str]] = field(default_factory=list)
    granted: bool = True
    per_call_abs_max: int = 5
    daily_abs_cap: int = 5
    _used: dict[str, int] = field(default_factory=dict)

    def _check(self, user: str) -> None:
        if not self.granted:
            raise _FakeWitError(_Error_Denied("not_granted"))
        if user not in self.members:
            raise _FakeWitError(_Error_NotAMember())

    def get(self, user: str) -> int:
        """Return `user`'s balance (0 for a member with no adjustments)."""
        self._check(user)
        return self.balances.get(user, 0)

    def adjust(self, user: str, delta: int, reason: str) -> int:
        """Apply `delta` and return the new balance, enforcing bounds and the daily cap."""
        self._check(user)
        if abs(delta) > self.per_call_abs_max:
            raise _FakeWitError(_Error_Denied("delta_out_of_bounds"))
        if self._used.get(user, 0) + abs(delta) > self.daily_abs_cap:
            raise _FakeWitError(_Error_DailyCapExceeded())
        self._used[user] = self._used.get(user, 0) + abs(delta)
        self.balances[user] = self.balances.get(user, 0) + delta
        self.ledger.append((user, delta, reason))
        return self.balances[user]


def install_fake_reputation_host(
    monkeypatch: Any, members: set[str] | None = None
) -> FakeReputationHost:
    """Install a fresh `FakeReputationHost` as `wit_world.imports.reputation` and return it."""
    host = FakeReputationHost(members=set(members or ()))
    rep_mod = types.SimpleNamespace(get=host.get, adjust=host.adjust)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(reputation=rep_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return host


_TOOL_CASES = {
    "not_granted": ("ToolError_NotGranted", False),
    "forbidden": ("ToolError_Forbidden", False),
    "timeout": ("ToolError_Timeout", False),
    "depth_exceeded": ("ToolError_DepthExceeded", False),
    "pool_exhausted": ("ToolError_PoolExhausted", False),
    "target_unavailable": ("ToolError_TargetUnavailable", False),
    "invalid_args": ("ToolError_InvalidArgs", True),
}


@dataclass(frozen=True)
class FakeToolCall:
    """One recorded `tool.invoke` host call."""

    target: str
    tool: str
    args: bytes
    idempotency_key: str | None


@dataclass
class FakeToolHost:
    """Stand-in for `wit_world.imports.tool`: records calls, replays scripted responses.

    Like the real executor import, a scripted success is returned as JSON bytes and a
    scripted error raises an `Err`-shaped exception carrying the `ToolError_*` variant.
    An unscripted call raises `target_unavailable`, as an unknown target would.
    """

    calls: list[FakeToolCall] = field(default_factory=list)
    _script: dict[tuple[str, str], bytes] = field(default_factory=dict)
    _errors: dict[tuple[str, str], str] = field(default_factory=dict)

    def script(self, target: str, tool: str, value: Any) -> None:
        """Script `target`/`tool` to succeed with JSON-serializable `value`."""
        self._script[(target, tool)] = json.dumps(value).encode()

    def script_raw(self, target: str, tool: str, raw: bytes) -> None:
        """Script `target`/`tool` to succeed with raw (possibly non-JSON) bytes."""
        self._script[(target, tool)] = raw

    def script_error(self, target: str, tool: str, code: str) -> None:
        """Script `target`/`tool` to fail with the snake_case `code`."""
        if code not in _TOOL_CASES:
            raise ValueError(f"unknown tool error code {code!r}")
        self._errors[(target, tool)] = code

    def invoke(self, target: str, tool: str, args: bytes, idempotency_key: str | None) -> bytes:
        """Record the call, then return the scripted bytes or raise the scripted error."""
        self.calls.append(FakeToolCall(target, tool, bytes(args), idempotency_key))
        code = self._errors.get((target, tool), None)
        if code is None and (target, tool) not in self._script:
            code = "target_unavailable"
        if code is not None:
            name, has_payload = _TOOL_CASES[code]
            case = _variant(name, has_payload)
            raise _FakeWitError(case("fake") if has_payload else case())
        return self._script[(target, tool)]


def install_fake_tool_host(monkeypatch: Any) -> FakeToolHost:
    """Install a fresh `FakeToolHost` as `wit_world.imports.tool` and return it."""
    host = FakeToolHost()
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        tool=types.SimpleNamespace(invoke=host.invoke)
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return host


_Error_InsufficientFunds = _variant("Error_InsufficientFunds", True)
_Error_OverCap = _variant("Error_OverCap", True)


@dataclass(frozen=True)
class _FakeEntry:
    """Stand-in for the generated ``economy.Entry`` record."""

    user: str
    balance: int


@dataclass(slots=True)
class FakeEconomyHost:
    """In-memory stand-in for `wit_world.imports.economy`, enforcing the real host's rules.

    Mirrors what the real stage does (gate + store): every named user must be
    in `members`; `granted` False denies every call `not_granted`; a wager's
    stake must be `1..=max_bet` (else `Error_OverCap(max_bet)`), held by the
    user (else `Error_InsufficientFunds(balance)`), and its payout at most
    `stake * 100` (else `Error_OverCap(stake * 100)`); a transfer needs two
    distinct members and a held, in-cap amount. Money moves atomically (the
    check and the write are one step, so the balance can never go negative)
    and every movement appends a `ledger` row. Every refusal raises the
    same-named `Error_*` variant the generated binding would, wrapped in an
    `Err`-shaped exception, so a bundle's error handling is exercised for real.

    **Actor binding.** The real host only moves the money of the account that
    triggered the invocation: a wager's `user` and a transfer's `from_user` must
    equal `actor` (the user `identity.resolve_actor()` returns), else
    `Error_Denied("actor_mismatch")`; with `actor` unset (`None`, an unlinked
    account) every money mover is `Error_Denied("not_linked")`. Reads are not
    bound. Set `actor` before exercising `wager`/`transfer`.
    """

    members: set[str] = field(default_factory=set)
    balances: dict[str, int] = field(default_factory=dict)
    ledger: list[tuple[str, str, int]] = field(default_factory=list)
    granted: bool = True
    max_bet_cap: int = 1_000
    max_amount_cap: int = 1_000
    payout_multiple: int = 100
    actor: str | None = None

    def _check(self, *users: str) -> None:
        if not self.granted:
            raise _FakeWitError(_Error_Denied("not_granted"))
        for user in users:
            if user not in self.members:
                raise _FakeWitError(_Error_NotAMember())

    def _bind(self, payer: str) -> None:
        """Refuse a money mover whose payer is not the invocation's actor."""
        if self.actor is None:
            raise _FakeWitError(_Error_Denied("not_linked"))
        if payer != self.actor:
            raise _FakeWitError(_Error_Denied("actor_mismatch"))

    def balance(self, user: str) -> int:
        """Return `user`'s balance (0 for a member who holds nothing)."""
        self._check(user)
        return self.balances.get(user, 0)

    def max_bet(self, user: str) -> int:
        """Return `min(max_bet_cap, balance)`."""
        self._check(user)
        return min(self.max_bet_cap, self.balances.get(user, 0))

    def wager(self, user: str, stake: int, payout: int) -> int:
        """Atomically debit `stake`, credit `payout`; enforce actor, cap, funds, payout bound."""
        self._check(user)
        self._bind(user)
        if stake < 1:
            raise _FakeWitError(_Error_Invalid("stake must be >= 1"))
        if stake > self.max_bet_cap:
            raise _FakeWitError(_Error_OverCap(self.max_bet_cap))
        if payout > stake * self.payout_multiple:
            raise _FakeWitError(_Error_OverCap(stake * self.payout_multiple))
        held = self.balances.get(user, 0)
        if held < stake:
            raise _FakeWitError(_Error_InsufficientFunds(held))
        self.balances[user] = held - stake + payout
        self.ledger.append(("wager", user, payout - stake))
        return self.balances[user]

    def transfer(self, from_user: str, to_user: str, amount: int) -> None:
        """Atomically move `amount` from the actor to another distinct member."""
        self._check(from_user, to_user)
        self._bind(from_user)
        if from_user == to_user or amount < 1:
            raise _FakeWitError(_Error_Invalid("bad transfer"))
        if amount > self.max_amount_cap:
            raise _FakeWitError(_Error_OverCap(self.max_amount_cap))
        held = self.balances.get(from_user, 0)
        if held < amount:
            raise _FakeWitError(_Error_InsufficientFunds(held))
        self.balances[from_user] = held - amount
        self.balances[to_user] = self.balances.get(to_user, 0) + amount
        self.ledger.append(("transfer_out", from_user, -amount))
        self.ledger.append(("transfer_in", to_user, amount))

    def leaderboard(self, limit: int) -> list[_FakeEntry]:
        """Return the top `limit` members by balance, highest first (ties by user id)."""
        self._check()
        if not 1 <= limit <= 100:
            raise _FakeWitError(_Error_Invalid("limit must be 1..=100"))
        ranked = sorted(
            ((u, b) for u, b in self.balances.items() if u in self.members),
            key=lambda ub: (-ub[1], ub[0]),
        )
        return [_FakeEntry(user=u, balance=b) for u, b in ranked[:limit]]


def install_fake_economy_host(monkeypatch: Any, members: set[str] | None = None) -> FakeEconomyHost:
    """Install a fresh `FakeEconomyHost` as `wit_world.imports.economy` and return it."""
    host = FakeEconomyHost(members=set(members or ()))
    eco_mod = types.SimpleNamespace(
        balance=host.balance,
        wager=host.wager,
        transfer=host.transfer,
        max_bet=host.max_bet,
        leaderboard=host.leaderboard,
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(economy=eco_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return host


_Error_NotLinked = _variant("Error_NotLinked", False)
_Error_NotFound = _variant("Error_NotFound", False)
_Error_Ambiguous = _variant("Error_Ambiguous", False)


@dataclass(slots=True)
class FakeIdentityHost:
    """In-memory stand-in for `wit_world.imports.identity`, enforcing the real host's rules.

    Mirrors what the real stage does (gate + resolver): `granted` False denies
    every call `not_granted`; `resolve_actor` answers the triggering actor's
    community uuid (`actor`; `None` = identity not linked -> `Error_NotLinked`;
    an actor outside `members` -> `Error_NotAMember`); `resolve_mention` answers
    ONLY tokens registered for this "message" (an unknown token -- including a
    raw handle -- is `Error_NotFound`, so a bundle cannot probe for identities),
    accepting a bare token or a full `{user:<token>}` placeholder, case-insensitive.
    A mention registered as unlinked / ambiguous raises `Error_NotLinked` /
    `Error_Ambiguous`; a resolved uuid outside `members` is `Error_NotAMember`.
    Every refusal raises the same-named `Error_*` variant the generated binding
    would, wrapped in an `Err`-shaped exception, so a bundle's error handling is
    exercised for real. `calls` records every call (`"actor"` / `"mention:<token>"`).
    """

    members: set[str] = field(default_factory=set)
    actor: str | None = None
    granted: bool = True
    calls: list[str] = field(default_factory=list)
    _mentions: dict[str, tuple[str, str | None]] = field(default_factory=dict)

    @staticmethod
    def _norm(token: str) -> str:
        text = token.strip()
        if text.startswith("{user:") and text.endswith("}"):
            text = text[len("{user:") : -1].strip()
        return text.lower()

    def add_mention(self, token: str, user: str) -> None:
        """Register `token` (as shown in the message) as resolving to community uuid `user`."""
        self._mentions[self._norm(token)] = ("ok", user)

    def add_unlinked_mention(self, token: str) -> None:
        """Register `token` as a target whose identity is not linked yet."""
        self._mentions[self._norm(token)] = ("not_linked", None)

    def add_ambiguous_mention(self, token: str) -> None:
        """Register `token` as a handle matching more than one identity."""
        self._mentions[self._norm(token)] = ("ambiguous", None)

    def _gate(self) -> None:
        if not self.granted:
            raise _FakeWitError(_Error_Denied("not_granted"))

    def resolve_actor(self) -> str:
        """Return the triggering actor's community uuid, or raise the host's refusal."""
        self._gate()
        self.calls.append("actor")
        if self.actor is None:
            raise _FakeWitError(_Error_NotLinked())
        if self.actor not in self.members:
            raise _FakeWitError(_Error_NotAMember())
        return self.actor

    def resolve_mention(self, token: str) -> str:
        """Return the community uuid a registered mention token names, or raise the refusal."""
        self._gate()
        key = self._norm(token)
        self.calls.append(f"mention:{key}")
        if not key or len(key.encode("utf-8")) > 256:
            raise _FakeWitError(_Error_Invalid("mention token must be 1..=256 bytes"))
        entry = self._mentions.get(key)
        if entry is None:
            raise _FakeWitError(_Error_NotFound())
        kind, user = entry
        if kind == "not_linked":
            raise _FakeWitError(_Error_NotLinked())
        if kind == "ambiguous":
            raise _FakeWitError(_Error_Ambiguous())
        if user not in self.members:
            raise _FakeWitError(_Error_NotAMember())
        return str(user)


def install_fake_identity_host(
    monkeypatch: Any, members: set[str] | None = None, actor: str | None = None
) -> FakeIdentityHost:
    """Install a fresh `FakeIdentityHost` as `wit_world.imports.identity` and return it."""
    host = FakeIdentityHost(members=set(members or ()), actor=actor)
    identity_mod = types.SimpleNamespace(
        resolve_actor=host.resolve_actor, resolve_mention=host.resolve_mention
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(identity=identity_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return host


_Error_TooManyTokens = _variant("Error_TooManyTokens", True)
_Error_ContentFiltered = _variant("Error_ContentFiltered", False)


@dataclass
class _FakeAiOptions:
    """Stand-in for the generated ``ai.Options`` record."""

    max_tokens: int | None
    temperature: float | None
    json: bool


@dataclass(slots=True)
class FakeAiHost:
    """In-memory stand-in for `wit_world.imports.ai`, refusing what the real host refuses.

    Mirrors the stage's guard order: `granted` False denies `not_granted`;
    `max_tokens` over 512 is `Error_TooManyTokens(512)`; a prompt over ~2000
    tokens (3 bytes each, the host's conservative estimate) is
    `Error_TooManyTokens(2000)`; a prompt containing an `@` (a crude stand-in for
    the host's email/handle/PII patterns) is `Error_Denied("contains_pii")`; more
    than `rate_limit` calls is `Error_Denied("rate_limited")`. Replies with
    `reply` (or raises `Error_ContentFiltered` when it is empty) and records
    every accepted call in `calls` as `(prompt, max_tokens, temperature, json)`.
    Every refusal raises the same-named `Error_*` variant the generated binding
    would, wrapped in an `Err`-shaped exception.
    """

    granted: bool = True
    reply: str = "a generated line"
    rate_limit: int = 10
    calls: list[tuple[str, int | None, float | None, bool]] = field(default_factory=list)
    _attempts: int = 0

    def Options(  # noqa: N802 - mirrors the generated record's constructor name
        self, max_tokens: int | None, temperature: float | None, json: bool
    ) -> _FakeAiOptions:
        """Build the options record, like `wit_world.imports.ai.Options(...)`."""
        return _FakeAiOptions(max_tokens=max_tokens, temperature=temperature, json=json)

    def generate(self, prompt: str, opts: _FakeAiOptions) -> str:
        """Apply the host's guard order, then answer `reply`."""
        if not self.granted:
            raise _FakeWitError(_Error_Denied("not_granted"))
        self._attempts += 1
        if self._attempts > self.rate_limit:
            raise _FakeWitError(_Error_Denied("rate_limited"))
        if opts.max_tokens is not None and opts.max_tokens > 512:
            raise _FakeWitError(_Error_TooManyTokens(512))
        if len(prompt.encode()) > 2_000 * 3:
            raise _FakeWitError(_Error_TooManyTokens(2_000))
        if "@" in prompt:
            raise _FakeWitError(_Error_Denied("contains_pii"))
        if not self.reply.strip():
            raise _FakeWitError(_Error_ContentFiltered())
        self.calls.append((prompt, opts.max_tokens, opts.temperature, opts.json))
        return self.reply


def install_fake_ai_host(monkeypatch: Any) -> FakeAiHost:
    """Install a fresh `FakeAiHost` as `wit_world.imports.ai` and return it."""
    host = FakeAiHost()
    ai_mod = types.SimpleNamespace(generate=host.generate, Options=host.Options)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(ai=ai_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return host


class StageViolationError(Exception):
    """A host capability was called from a pipeline stage whose host does not serve it."""


#: Capabilities the real host serves ONLY to the process stage (``core/svc_process/src/
#: capabilities.rs``): ``svc_action`` has no handler for them, so a call from ``dispatch`` is
#: denied/unwired on a real host even though it works against a stage-blind fake.
PROCESS_ONLY_CAPABILITIES = frozenset({"economy", "identity", "reputation", "ai"})

#: Capabilities served ONLY to the action stage (the process stage hard-denies ``relay``).
ACTION_ONLY_CAPABILITIES = frozenset({"relay"})

_STAGE: ContextVar[str | None] = ContextVar("waddle_sdk_testing_stage", default=None)


@dataclass(slots=True)
class StageGuard:
    """Makes a bundle test's fake host stage-faithful: process-only calls fail from ``dispatch``.

    **Why it exists.** The real pipeline serves ``economy`` / ``identity`` / ``reputation`` /
    ``ai`` to ``transform`` (the process stage) and ``relay`` to ``dispatch`` (the action stage)
    -- never the other way round. A stage-blind fake accepts every call from either entry point,
    so a bundle that settles money inside ``dispatch`` passes every host-native test and then fails
    on a real host. Wrapping the fakes with a guard turns that into a test failure.

    **Use**::

        GUARD = StageGuard()
        transform = GUARD.process_stage(app.transform)   # sets the stage while it runs
        dispatch = GUARD.action_stage(app.dispatch)

        ns.economy = types.SimpleNamespace(
            balance=GUARD.process_only("economy.balance", eco.balance), ...
        )
        ns.relay = types.SimpleNamespace(push=GUARD.action_only("relay.push", push))

        @pytest.fixture
        def host(monkeypatch):
            GUARD.reset()
            ...
            yield h
            GUARD.assert_clean()

    A call made with NO stage set (a test poking the host directly, outside both entry points)
    is not a violation -- only a call made from the WRONG stage is. Every violation is recorded
    (so it fails the test even if the bundle swallows the error it is answered with) and the
    call raises :class:`StageViolationError`.
    """

    violations: list[str] = field(default_factory=list)

    def reset(self) -> None:
        """Forget recorded violations (call from the per-test fixture, before the test runs)."""
        self.violations.clear()

    def assert_clean(self) -> None:
        """Fail if any capability was called from a stage that does not serve it."""
        if self.violations:
            raise AssertionError(
                "capability called from a stage that does not serve it (process: "
                f"{sorted(PROCESS_ONLY_CAPABILITIES)}, action: {sorted(ACTION_ONLY_CAPABILITIES)}"
                f"): {self.violations}"
            )

    @property
    def stage(self) -> str | None:
        """The stage the current task is simulating, or ``None`` outside both entry points."""
        return _STAGE.get()

    @contextmanager
    def in_stage(self, stage: str) -> Iterator[None]:
        """Run the body as the ``process`` or ``action`` stage."""
        if stage not in ("process", "action"):
            raise ValueError(f"unknown stage {stage!r}")
        token = _STAGE.set(stage)
        try:
            yield
        finally:
            _STAGE.reset(token)

    def process_stage(self, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        """Wrap an entry point (``transform``) so it runs as the process stage."""

        async def run(*args: Any, **kwargs: Any) -> Any:
            with self.in_stage("process"):
                return await fn(*args, **kwargs)

        return run

    def action_stage(self, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        """Wrap an entry point (``dispatch``) so it runs as the action stage."""

        async def run(*args: Any, **kwargs: Any) -> Any:
            with self.in_stage("action"):
                return await fn(*args, **kwargs)

        return run

    def two_stage(
        self,
        settle: Callable[..., Awaitable[Any]],
        dispatch: Callable[..., Awaitable[Any]],
    ) -> Callable[..., Awaitable[Any]]:
        """Return a ``dispatch``-shaped driver that runs the REAL two-stage split.

        For a bundle whose ``transform`` is ``recognize -> settle(intent, event, community=)``:
        ``settle`` (the economy/identity work) runs as the process stage on the envelope's
        ``intent`` payload, then ``dispatch`` runs as the action stage on the event ``settle``
        returned. When ``settle`` drops the event (``None`` -- a redelivery), no dispatch happens
        and the result's ``detail`` is ``"duplicate"``. Lets a suite written against the old
        single-stage ``dispatch(envelope, config, http_client=)`` shape keep its assertions while
        every host call is stage-checked.
        """

        async def run(envelope: Any, config: Any, *, http_client: Any = None) -> Any:
            with self.in_stage("process"):
                out = await settle(
                    envelope.event.payload, envelope.event, community=envelope.community
                )
            if out is None:
                return types.SimpleNamespace(
                    transport=envelope.event.platform,
                    detail="duplicate",
                    sub_type=None,
                    http_status=None,
                )
            action_envelope = dataclasses.replace(envelope, stage="action", event=out)
            with self.in_stage("action"):
                return await dispatch(action_envelope, config, http_client=http_client)

        return run

    def _wrap(self, name: str, fn: Callable[..., Any], served_by: str) -> Callable[..., Any]:
        def guarded(*args: Any, **kwargs: Any) -> Any:
            current = _STAGE.get()
            if current is not None and current != served_by:
                self.violations.append(f"{name} called from the {current} stage")
                raise StageViolationError(
                    f"{name} is served by the {served_by} stage only (called from {current})"
                )
            return fn(*args, **kwargs)

        return guarded

    def process_only(self, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a host fake that only the process stage serves (economy/identity/...)."""
        return self._wrap(name, fn, "process")

    def action_only(self, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a host fake that only the action stage serves (``relay``)."""
        return self._wrap(name, fn, "action")
