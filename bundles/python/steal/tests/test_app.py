"""Host-native tests for `steal`: shared fake kv/economy/identity hosts, no WASM."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import re
import types
from collections.abc import Iterator
from typing import Any

import pytest
from waddle_sdk import event_dedupe
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.bundle_runtime import bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import (
    FakeEconomyHost,
    FakeIdentityHost,
    FakeKvHost,
    StageGuard,
    install_fake_kv_host,
    rfc3339_from_millis,
)

import app
from app import STAKE

#: Makes every host fake stage-faithful: economy/identity answer the PROCESS stage only, `relay`
#: the ACTION stage only (the real host's split). The `host` fixture fails a test that breaks it.
GUARD = StageGuard()


async def _transform_in_context(event: PlatformEvent) -> PlatformEvent | None:
    """The real `transform` entry point, inside the bundle context the host binds around it."""
    with bundle_context(tenant="t", community=COMMUNITY, app_id="waddles.core.example.steal"):
        return await app.transform(event)


transform = GUARD.process_stage(_transform_in_context)
#: Runs one command through the real stage split: `app._settle` as the process stage on the
#: envelope's intent payload, then `app.dispatch` as the action stage on what it returned.
dispatch = GUARD.two_stage(app._settle, app.dispatch)
action_dispatch = GUARD.action_stage(app.dispatch)

ACTOR = "11111111-1111-4111-8111-111111111111"
TARGET = "22222222-2222-4222-8222-222222222222"
COMMUNITY = "comm-1"


_MSG_IDS = itertools.count(1)
_CURRENT_HOST: list[Any] = []


def _next_msg_id() -> str:
    """A fresh platform message id per delivery, like the real pipeline."""
    return f"msg-{next(_MSG_IDS)}"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _now_stamp(offset_seconds: int = 0) -> str:
    """The ingest stamp of an event arriving NOW on the fake host clock (or that long ago/ahead)."""
    return rfc3339_from_millis(_CURRENT_HOST[-1].now_ms + offset_seconds * 1000)


class _Host:
    def __init__(self, kv: FakeKvHost, eco: FakeEconomyHost, ident: FakeIdentityHost) -> None:
        self.kv, self.eco, self.ident = kv, eco, ident
        self.relay_calls: list[tuple[str, Any]] = []
        self.log_calls: list[tuple[Any, str, str]] = []
        self.now_ms = 1_700_000_000_000

    def expire(self, marker: str) -> None:
        """Simulate the kv TTL lapsing for every key containing `marker` (the fake ignores TTL)."""
        for key in [k for k in self.kv.store if marker in k]:
            del self.kv.store[key]


def _install(monkeypatch: pytest.MonkeyPatch, *, flag: bool = True) -> _Host:
    kv = install_fake_kv_host(monkeypatch)
    eco = FakeEconomyHost(members={ACTOR, TARGET}, actor=ACTOR)
    eco.balances = {ACTOR: 500, TARGET: 300}
    ident = FakeIdentityHost(members={ACTOR, TARGET}, actor=ACTOR)
    ident.add_mention("victim", TARGET)
    host = _Host(kv, eco, ident)
    _CURRENT_HOST.append(host)
    import wit_world

    ns = wit_world.imports
    GUARD.reset()
    ns.economy = types.SimpleNamespace(
        balance=GUARD.process_only("economy.balance", eco.balance),
        wager=GUARD.process_only("economy.wager", eco.wager),
        transfer=GUARD.process_only("economy.transfer", eco.transfer),
        max_bet=GUARD.process_only("economy.max_bet", eco.max_bet),
        leaderboard=GUARD.process_only("economy.leaderboard", eco.leaderboard),
    )
    ns.identity = types.SimpleNamespace(
        resolve_actor=GUARD.process_only("identity.resolve_actor", ident.resolve_actor),
        resolve_mention=GUARD.process_only("identity.resolve_mention", ident.resolve_mention),
    )
    ns.flags = types.SimpleNamespace(enabled=lambda key, default_value: flag)
    ns.relay = types.SimpleNamespace(
        push=GUARD.action_only("relay.push", lambda p, m: host.relay_calls.append((p, m)))
    )
    ns.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, f: host.log_calls.append((lvl, msg, f)),
    )
    ns.clock = types.SimpleNamespace(
        now_millis=lambda: host.now_ms, now_rfc3339=lambda: "x", monotonic_nanos=lambda: 0
    )
    return host


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Host]:
    h = _install(monkeypatch)
    yield h
    GUARD.assert_clean()


def _event(text: str, **extra: Any) -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": "12345", **extra},
        occurred_at="2026-10-05T00:00:00.000Z",
    )


def _envelope(command: str, **payload: Any) -> StageEnvelope:
    return StageEnvelope(
        tenant="t",
        community=COMMUNITY,
        app_id="waddles.core.example.steal",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={
                "command": command,
                "channel_id": "12345",
                "message_id": _next_msg_id(),
                **payload,
            },
            occurred_at=_now_stamp(),
        ),
        ts="2026-10-05T00:00:00.000Z",
    )


def _reply(host: _Host) -> str:
    return str(json.loads(host.relay_calls[-1][1])["text"])


def _steal(host: _Host, monkeypatch: pytest.MonkeyPatch, *, win: bool) -> str:
    monkeypatch.setattr(app, "_roll", lambda percent: win)
    _run(dispatch(_envelope("steal", tokens=["victim"]), {}, http_client=None))
    return _reply(host)


# -- transform -----------------------------------------------------------------


def _recognize(event: PlatformEvent) -> types.SimpleNamespace | None:
    """The routing intent `transform` derives from a chat line (pure, no host call)."""
    intent = app._recognize(event)
    return None if intent is None else types.SimpleNamespace(payload=intent)


def test_transform_forwards_mention_tokens(host: _Host) -> None:
    out = _recognize(_event("!steal {user:victim}"))
    assert out is not None
    assert out.payload["command"] == "steal"
    assert out.payload["tokens"] == ["victim"]


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("!steal", "usage"),
        ("!steal list", "list"),
        ("!steal list x", "usage"),
        ("!steal set cooldown 5", "config_set_cooldown"),
        ("!steal enable", "usage"),
        ("!steal bogus", "usage"),
    ],
)
def test_transform_grammar(host: _Host, text: str, command: str) -> None:
    out = _recognize(_event(text))
    assert out is not None
    assert out.payload["command"] == command


@pytest.mark.parametrize("text", ["!stealth", "hello", "!steals {user:victim}"])
def test_transform_ignores_non_matching(host: _Host, text: str) -> None:
    assert _run(transform(_event(text))) is None


def test_transform_ignores_non_string_text(host: _Host) -> None:
    ev = _event("x")
    ev.payload["text"] = 5
    assert _run(transform(ev)) is None


def test_badges_strict_is_true(host: _Host) -> None:
    out = _recognize(_event("!steal list", is_mod="false", is_broadcaster=1))
    assert out is not None
    assert out.payload["is_mod"] is False
    assert out.payload["is_broadcaster"] is False
    bare = _recognize(_event("!steal list"))
    assert bare is not None
    assert "is_mod" not in bare.payload


# -- steal flow ----------------------------------------------------------------


def test_success_pays_loot_bounded_by_target_balance(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.eco.balances[TARGET] = 3
    monkeypatch.setattr(app._RNG, "randint", lambda a, b: b)
    reply = _steal(host, monkeypatch, win=True)
    assert "stole 3 points" in reply
    assert host.eco.balances[ACTOR] == 503


def test_success_loot_capped(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.eco.balances[TARGET] = 10_000
    monkeypatch.setattr(app._RNG, "randint", lambda a, b: b)
    _steal(host, monkeypatch, win=True)
    assert host.eco.balances[ACTOR] == 500 + app.LOOT_CAP


def test_failure_transfers_penalty_to_target(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    reply = _steal(host, monkeypatch, win=False)
    assert "caught" in reply
    assert host.eco.balances[ACTOR] == 500 - STAKE
    assert host.eco.balances[TARGET] == 300 + STAKE


def test_no_target_replies_usage(host: _Host) -> None:
    _run(dispatch(_envelope("steal"), {}, http_client=None))
    assert _reply(host).startswith("Usage:")


def test_self_target_refused_no_money_moves(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.ident.add_mention("me", ACTOR)
    _run(dispatch(_envelope("steal", tokens=["me"]), {}, http_client=None))
    assert "yourself" in _reply(host)
    assert host.eco.ledger == []


@pytest.mark.parametrize("kind", ["unknown", "unlinked", "ambiguous"])
def test_unresolvable_target_refused(host: _Host, kind: str) -> None:
    if kind == "unlinked":
        host.ident.add_unlinked_mention("t")
    elif kind == "ambiguous":
        host.ident.add_ambiguous_mention("t")
    _run(dispatch(_envelope("steal", tokens=["t"]), {}, http_client=None))
    assert host.eco.ledger == []
    assert "member" in _reply(host)


def test_unlinked_actor_refused(host: _Host) -> None:
    host.ident.actor = None
    _run(dispatch(_envelope("steal", tokens=["victim"]), {}, http_client=None))
    assert "link your account" in _reply(host)
    assert host.eco.ledger == []


def test_empty_victim_refused(host: _Host) -> None:
    host.eco.balances[TARGET] = 0
    _run(dispatch(_envelope("steal", tokens=["victim"]), {}, http_client=None))
    assert "nothing to steal" in _reply(host)
    assert host.eco.ledger == []


def test_insufficient_funds_refused(host: _Host) -> None:
    host.eco.balances[ACTOR] = STAKE - 1
    _run(dispatch(_envelope("steal", tokens=["victim"]), {}, http_client=None))
    assert f"at least {STAKE}" in _reply(host)
    assert host.eco.ledger == []


def test_economy_unavailable_replies_not_crashes(host: _Host) -> None:
    host.eco.granted = False
    _run(dispatch(_envelope("steal", tokens=["victim"]), {}, http_client=None))
    assert "temporarily unavailable" in _reply(host)


def test_cooldown_blocks_second_steal_then_expires(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _steal(host, monkeypatch, win=False)
    before = dict(host.eco.balances)
    assert "lay low" in _steal(host, monkeypatch, win=False)
    assert host.eco.balances == before
    host.now_ms += 61_000
    host.expire(".steal.cd.")  # the kv TTL lapsing is the host's job; the fake does not model it
    assert "caught" in _steal(host, monkeypatch, win=False)


def test_cooldown_zero_disables_gate(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(
        dispatch(
            _envelope("config_set_cooldown", arg="cooldown 0", is_mod=True), {}, http_client=None
        )
    )
    _steal(host, monkeypatch, win=False)
    assert "caught" in _steal(host, monkeypatch, win=False)


# -- config / list / usage -----------------------------------------------------


def test_set_cooldown_mod_ok_and_denied_cases(host: _Host) -> None:
    _run(
        dispatch(
            _envelope("config_set_cooldown", arg="cooldown 30", is_mod=True), {}, http_client=None
        )
    )
    assert "set to 30s" in _reply(host)
    for extra in ({}, {"is_mod": False}):
        _run(
            dispatch(
                _envelope("config_set_cooldown", arg="cooldown 9", **extra), {}, http_client=None
            )
        )
        assert "only moderators" in _reply(host)


@pytest.mark.parametrize("arg", ["cooldown", "cooldown abc", "cooldown 99999", "odds 3"])
def test_set_cooldown_bad_input(host: _Host, arg: str) -> None:
    _run(
        dispatch(
            _envelope("config_set_cooldown", arg=arg, is_broadcaster=True), {}, http_client=None
        )
    )
    assert not _reply(host).startswith("steal cooldown set")


def test_list_before_and_after(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(dispatch(_envelope("list"), {}, http_client=None))
    assert "haven't tried" in _reply(host)
    _steal(host, monkeypatch, win=True)
    _run(dispatch(_envelope("list"), {}, http_client=None))
    assert "attempts: 1. Successful: 1" in _reply(host)


def test_usage_reply(host: _Host) -> None:
    _run(dispatch(_envelope("usage"), {}, http_client=None))
    assert _reply(host) == app._USAGE


# -- dispatch guards -----------------------------------------------------------


def test_dispatch_guards(host: _Host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope("usage", channel_id=None), {}, http_client=None))
    with pytest.raises(ValueError, match="unrecognized"):
        _run(dispatch(_envelope("nope"), {}, http_client=None))
    env = _envelope("usage")
    env.community = None  # type: ignore[misc]
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(env, {}, http_client=None))


def test_kv_failure_is_loud(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    import wit_world

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(wit_world.imports.kv, "get", boom)
    with pytest.raises(RuntimeError, match="backend failed"):
        _run(dispatch(_envelope("list"), {}, http_client=None))
    assert "unavailable" in _reply(host)


# -- PII-free ------------------------------------------------------------------


def test_keys_and_logs_pii_free(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _steal(host, monkeypatch, win=True)
    pseud = hashlib.sha256(f"{COMMUNITY}:{ACTOR}".encode()).hexdigest()
    assert host.kv.store, "steal must persist state"
    for key in host.kv.store:
        assert "viewer-1" not in key
        assert ACTOR not in key
    assert _scoped_key(COMMUNITY, f"steal.attempts.{pseud}") in host.kv.store
    blob = " ".join(f"{m} {f}" for _, m, f in host.log_calls)
    assert "viewer-1" not in blob
    assert ACTOR not in blob
    assert TARGET not in blob


# hardening: output sanitizer / activation-only
ZWSP = "\u200b"


def _envelope_as(actor: str | None, command: str, **payload: Any) -> StageEnvelope:
    return StageEnvelope(
        tenant="t",
        community=COMMUNITY,
        app_id="waddles.core.example.steal",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor=actor,
            payload={
                "command": command,
                "channel_id": "12345",
                "message_id": _next_msg_id(),
                **payload,
            },
            occurred_at=_now_stamp(),
        ),
        ts="2026-10-05T00:00:00.000Z",
    )


def _win_reply_as(host: _Host, monkeypatch: pytest.MonkeyPatch, actor: str | None) -> str:
    monkeypatch.setattr(app, "_roll", lambda percent: True)
    _run(dispatch(_envelope_as(actor, "steal", tokens=["victim"]), {}, http_client=None))
    return _reply(host)


def test_minted_actor_token_stays_live_in_the_reply(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline's own `{user:<token>}` actor must still resolve to a display name."""
    assert _win_reply_as(host, monkeypatch, "{user:tok-actor}").startswith(
        "\U0001f9b9 {user:tok-actor} stole "
    )


@pytest.mark.parametrize(
    ("actor", "label"),
    [
        ("evil@everyone", "evil@" + ZWSP + "everyone"),
        ("{user:handle:victim} <@123>", "\\{user:handle:victim\\} <@" + ZWSP + "123>"),
        ("{user:tok-1} @here", "\\{user:tok-1\\} @" + ZWSP + "here"),
    ],
)
def test_raw_actor_cannot_forge_mentions(
    host: _Host, monkeypatch: pytest.MonkeyPatch, actor: str, label: str
) -> None:
    """A raw handle (tokenization off) or an actor with text around a placeholder is untrusted."""
    assert _win_reply_as(host, monkeypatch, actor).startswith("\U0001f9b9 " + label + " stole ")


def test_missing_actor_falls_back_to_someone(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _win_reply_as(host, monkeypatch, None).startswith("\U0001f9b9 someone stole ")


def test_config_echo_of_typed_value_is_neutralized(host: _Host) -> None:
    _run(
        dispatch(
            _envelope("config_set_cooldown", arg="cooldown @everyone", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert _reply(host) == "'@" + ZWSP + "everyone' isn't a whole number of seconds"


# hardening: activation-only
def test_runs_on_activation_alone_when_flag_backend_says_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activation-gated only: an OFF `waddles.command-steal` flag must not drop the command."""
    _install(monkeypatch, flag=False)
    assert _run(transform(_event("!steal list"))) is not None


# hardening: economy integrity (replay safety, atomic cooldown, anti-collusion caps)
T2 = "33333333-3333-4333-8333-333333333333"
T3 = "44444444-4444-4444-8444-444444444444"
T4 = "55555555-5555-4555-8555-555555555555"
COOLDOWN_CFG_KEY = _scoped_key(COMMUNITY, "steal.config.cooldown")


def _world(host: _Host) -> None:
    """Four members (ACTOR/TARGET/T2/T3), each holding enough to be worth 100 loot."""
    members = {ACTOR, TARGET, T2, T3, T4}
    host.eco.members, host.ident.members = set(members), set(members)
    host.eco.balances = {ACTOR: 500, TARGET: 300, T2: 300, T3: 300, T4: 300}
    host.eco.max_amount_cap = 10_000
    for name, user in (("victim", TARGET), ("ally", ACTOR), ("t2", T2), ("t3", T3), ("t4", T4)):
        host.ident.add_mention(name, user)


def _no_cooldown(host: _Host) -> None:
    host.kv.store[COOLDOWN_CFG_KEY] = b"0"


def _as(host: _Host, user: str) -> None:
    host.eco.actor = user
    host.ident.actor = user


def _go(
    host: _Host,
    mp: pytest.MonkeyPatch,
    mention: str,
    *,
    win: bool,
    actor: str | None = None,
    msg_id: str | None = None,
) -> str | None:
    """One steal delivery; returns the reply, or None when nothing was sent (a skipped dup)."""
    if actor is not None:
        _as(host, actor)
    mp.setattr(app, "_roll", lambda percent: win)
    mp.setattr(app._RNG, "randint", lambda a, b: b)  # worst case for the house: max loot
    extra: dict[str, Any] = {"message_id": msg_id} if msg_id else {}
    before = len(host.relay_calls)
    _run(dispatch(_envelope("steal", tokens=[mention], **extra), {}, http_client=None))
    return _reply(host) if len(host.relay_calls) > before else None


def _minted(host: _Host) -> int:
    """Total points the economy minted: wager payouts net of stakes (transfers conserve)."""
    return sum(delta for kind, _user, delta in host.eco.ledger if kind == "wager")


def _kv_int(host: _Host, key: str) -> int:
    return int(host.kv.store.get(_scoped_key(COMMUNITY, key), b"0"))


def _pair_value(host: _Host, user_a: str, user_b: str, window: int | None = None) -> int:
    pair, _low = app._pair_id(COMMUNITY, user_a, user_b)
    w = host.now_ms // (app.WINDOW_SECONDS * 1000) if window is None else window
    return _kv_int(host, f"steal.pair.{w}.{pair}")


def _budget_value(host: _Host, user: str) -> int:
    w = host.now_ms // (app.WINDOW_SECONDS * 1000)
    return _kv_int(host, f"steal.budget.{w}.{app._pseudonym(COMMUNITY, user)}")


def _yielding_kv_all(mp: pytest.MonkeyPatch) -> None:
    """Make every kv facade call a suspension point so concurrent dispatches really interleave."""
    real_get, real_set, real_inc = (
        app.community_kv.get,
        app.community_kv.set,
        app.community_kv.increment,
    )

    async def get(c: str | None, k: str) -> bytes | None:
        await asyncio.sleep(0)
        return await real_get(c, k)

    async def set_(c: str | None, k: str, v: bytes, ttl: int = 0) -> None:
        await asyncio.sleep(0)
        await real_set(c, k, v, ttl)

    async def inc(c: str | None, k: str, d: int, ttl: int = 0) -> int:
        await asyncio.sleep(0)
        return await real_inc(c, k, d, ttl)

    mp.setattr(app.community_kv, "get", get)
    mp.setattr(app.community_kv, "set", set_)
    mp.setattr(app.community_kv, "increment", inc)


def test_transform_forwards_the_platform_message_id(host: _Host) -> None:
    out = _recognize(_event("!steal {user:victim}", message_id="m-9"))
    assert out is not None and out.payload["message_id"] == "m-9"


@pytest.mark.parametrize("bad", [None, 7, ["m"]])
def test_transform_does_not_forward_a_non_string_message_id(host: _Host, bad: Any) -> None:
    out = _recognize(_event("!steal {user:victim}", message_id=bad))
    assert out is not None and "message_id" not in out.payload


# -- atomic cooldown (CAS) -----------------------------------------------------


def test_cooldown_is_one_atomic_increment_never_check_then_set(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _steal(host, monkeypatch, win=False)
    cd = [c for c in host.kv.calls if ".steal.cd." in c[1][0]]
    assert [c[0] for c in cd] == ["increment"], "the cooldown claim must be a single increment"
    assert cd[0][1][1:] == (1, app.DEFAULT_COOLDOWN_SECONDS)
    assert not any(".steal.last." in c[1][0] for c in host.kv.calls), "no timestamp read/set race"


@pytest.mark.parametrize("win", [False, True])
@pytest.mark.parametrize("racers", [2, 8])
def test_concurrent_steals_by_one_actor_exactly_one_wins(
    host: _Host, monkeypatch: pytest.MonkeyPatch, win: bool, racers: int
) -> None:
    """regression: check-then-set let every concurrent steal pass the cooldown (cap bypass)."""
    _world(host)
    _yielding_kv_all(monkeypatch)
    monkeypatch.setattr(app, "_roll", lambda percent: win)

    async def race() -> None:
        await asyncio.gather(
            *(
                dispatch(_envelope("steal", tokens=["victim"]), {}, http_client=None)
                for _ in range(racers)
            )
        )

    _run(race())
    replies = [json.loads(m)["text"] for _p, m in host.relay_calls]
    assert sum("lay low" in r for r in replies) == racers - 1, replies
    assert len(host.eco.ledger) == (1 if win else 2), host.eco.ledger  # one wager | one transfer
    assert (_minted(host) > 0) is win


def test_a_refused_attempt_restarts_the_window_and_says_so(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _steal(host, monkeypatch, win=False)
    reply = _steal(host, monkeypatch, win=False)
    assert f"try again in {app.DEFAULT_COOLDOWN_SECONDS}s" in reply
    incs = [c for c in host.kv.calls if c[0] == "increment" and ".steal.cd." in c[1][0]]
    assert [c[1][2] for c in incs] == [app.DEFAULT_COOLDOWN_SECONDS] * 2  # TTL re-applied


def test_cooldown_zero_never_touches_the_claim_key(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_cooldown(host)
    _steal(host, monkeypatch, win=False)
    assert not any(".steal.cd." in c[1][0] for c in host.kv.calls)


def test_cooldown_is_per_actor(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _world(host)
    assert "caught" in (_go(host, monkeypatch, "victim", win=False) or "")
    assert "lay low" in (_go(host, monkeypatch, "victim", win=False) or "")
    assert "caught" in (_go(host, monkeypatch, "ally", win=False, actor=TARGET) or "")


# -- replay safety -------------------------------------------------------------


def _snapshot(host: _Host) -> tuple[Any, ...]:
    return (dict(host.eco.balances), list(host.eco.ledger), dict(host.kv.store.items()))


@pytest.mark.parametrize("win", [False, True])
def test_redelivery_after_the_cooldown_changes_nothing(
    host: _Host, monkeypatch: pytest.MonkeyPatch, win: bool
) -> None:
    """regression: a redelivered steal re-rolled and re-settled (a second mint / second penalty)."""
    _world(host)
    assert _go(host, monkeypatch, "victim", win=win, msg_id="dup-1") is not None
    host.expire(".steal.cd.")
    snap = _snapshot(host)
    for _ in range(3):
        assert _go(host, monkeypatch, "victim", win=win, msg_id="dup-1") is None
    assert _snapshot(host) == snap, "no money, stat, cap or cooldown state may change"


def test_a_redelivery_that_would_roll_the_other_way_is_still_skipped(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host keys wager and transfer separately, so a flipped re-roll could apply BOTH."""
    _world(host)
    _go(host, monkeypatch, "victim", win=False, msg_id="flip-1")  # caught: penalty transferred
    host.expire(".steal.cd.")
    snap = _snapshot(host)
    assert _go(host, monkeypatch, "victim", win=True, msg_id="flip-1") is None
    assert _snapshot(host) == snap
    assert _minted(host) == 0


def test_a_redelivery_inside_the_cooldown_is_silent_and_does_not_rearm_it(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(host)
    _go(host, monkeypatch, "victim", win=True, msg_id="dup-2")
    snap = _snapshot(host)
    assert _go(host, monkeypatch, "victim", win=True, msg_id="dup-2") is None
    assert _snapshot(host) == snap
    assert len([c for c in host.kv.calls if c[0] == "increment" and ".steal.cd." in c[1][0]]) == 1


def test_distinct_messages_each_settle(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _world(host)
    _no_cooldown(host)
    for i in range(4):
        assert "caught" in (_go(host, monkeypatch, "victim", win=False, msg_id=f"d-{i}") or "")
    assert host.eco.balances[ACTOR] == 500 - 4 * STAKE
    assert _kv_int(host, f"steal.attempts.{app._pseudonym(COMMUNITY, ACTOR)}") == 4


@pytest.mark.parametrize("win", [False, True])
@pytest.mark.parametrize("copies", [2, 6])
def test_concurrent_duplicate_deliveries_settle_exactly_once(
    host: _Host, monkeypatch: pytest.MonkeyPatch, win: bool, copies: int
) -> None:
    """Two consumers holding the same stream entry at once: the cooldown CAS lets one through."""
    _world(host)
    _yielding_kv_all(monkeypatch)
    monkeypatch.setattr(app, "_roll", lambda percent: win)

    async def race() -> None:
        await asyncio.gather(
            *(
                dispatch(
                    _envelope("steal", tokens=["victim"], message_id="race-1"), {}, http_client=None
                )
                for _ in range(copies)
            )
        )

    _run(race())
    assert len(host.eco.ledger) == (1 if win else 2), host.eco.ledger  # one wager | one transfer
    assert _kv_int(host, f"steal.attempts.{app._pseudonym(COMMUNITY, ACTOR)}") == 1


@pytest.mark.parametrize("win", [False, True])
@pytest.mark.parametrize("copies", [2, 6])
def test_concurrent_duplicates_settle_once_even_with_no_cooldown_gate(
    host: _Host, monkeypatch: pytest.MonkeyPatch, win: bool, copies: int
) -> None:
    """regression: the old ring was a get-then-set (TOCTOU) only safe behind the cooldown CAS.

    With the cooldown disabled nothing else serializes the copies: only the atomic per-event
    `increment` can let exactly one through.
    """
    _world(host)
    _no_cooldown(host)
    # Lift the anti-collusion caps so they cannot incidentally refuse the losing copies: this test
    # must be decided by the event claim alone.
    monkeypatch.setattr(app, "PAIR_WINDOW_CAP", 10_000)
    monkeypatch.setattr(app, "ACTOR_WINDOW_BUDGET", 10_000)
    _yielding_kv_all(monkeypatch)
    monkeypatch.setattr(app, "_roll", lambda percent: win)

    async def race() -> None:
        await asyncio.gather(
            *(
                dispatch(
                    _envelope("steal", tokens=["victim"], message_id="race-2"), {}, http_client=None
                )
                for _ in range(copies)
            )
        )

    _run(race())
    assert len(host.eco.ledger) == (1 if win else 2), host.eco.ledger  # one wager | one transfer
    assert _kv_int(host, f"steal.attempts.{app._pseudonym(COMMUNITY, ACTOR)}") == 1
    assert len(host.relay_calls) == 1, "exactly one copy replies; every duplicate is silent"
    assert len(_markers(host)) == 1


def test_a_redelivery_older_than_any_ring_horizon_is_still_skipped(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """regression: the last-16-ids ring forgot an event after 16 later steals (a replay)."""
    _world(host)
    _no_cooldown(host)
    assert "caught" in (_go(host, monkeypatch, "victim", win=False, msg_id="oldest") or "")
    for i in range(40):
        assert "caught" in (_go(host, monkeypatch, "victim", win=False, msg_id=f"later-{i}") or "")
    snap = _snapshot(host)
    for win in (False, True):  # the same message, even if it would now roll the other way
        assert _go(host, monkeypatch, "victim", win=win, msg_id="oldest") is None
    assert _snapshot(host) == snap
    assert _minted(host) == 0


def _markers(host: _Host) -> list[str]:
    return [k for k in host.kv.store if ".steal.seen." in k]


def test_the_claim_is_one_ttl_increment_on_a_per_event_marker(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(host)
    _go(host, monkeypatch, "victim", win=False, msg_id="m-ttl")
    marker_calls = [c for c in host.kv.calls if ".steal.seen." in c[1][0]]
    assert [c[0] for c in marker_calls] == ["get", "increment"]  # advisory peek, then the claim
    assert marker_calls[1][1][1:] == (1, event_dedupe.SEEN_TTL_SECONDS)
    assert not [c for c in host.kv.calls if c[0] == "set" and ".seen." in c[1][0]]


def test_an_actor_on_cooldown_never_creates_a_marker(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One chatter's marker rate is bounded by its cooldown: refused attempts mint no keys."""
    _world(host)
    _go(host, monkeypatch, "victim", win=False)
    assert len(_markers(host)) == 1
    for _ in range(20):
        assert "lay low" in (_go(host, monkeypatch, "victim", win=False) or "")
    assert len(_markers(host)) == 1


@pytest.mark.parametrize("age", [event_dedupe.MAX_EVENT_AGE_SECONDS + 1, 3600, 86_400 * 7])
def test_an_event_too_old_to_dedupe_is_dropped_silently_and_moves_nothing(
    host: _Host, monkeypatch: pytest.MonkeyPatch, age: int
) -> None:
    """The marker TTL is the horizon, so an event past the age fence must never be processed."""
    _world(host)
    env = _envelope("steal", tokens=["victim"])
    env.event.occurred_at = _now_stamp(-age)
    snap = _snapshot(host)
    _run(dispatch(env, {}, http_client=None))
    assert host.relay_calls == [] and _snapshot(host) == snap
    assert any("steal.stale_event_skipped" in m for _l, m, _f in host.log_calls)


def test_an_event_at_the_age_limit_is_still_processed(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(host)
    monkeypatch.setattr(app, "_roll", lambda percent: False)
    env = _envelope("steal", tokens=["victim"])
    env.event.occurred_at = _now_stamp(-event_dedupe.MAX_EVENT_AGE_SECONDS)
    _run(dispatch(env, {}, http_client=None))
    assert "caught" in _reply(host)


def test_a_replay_after_the_marker_lapsed_is_dropped_by_the_fence(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outage > marker TTL: the marker is gone, but the event is too old to be re-processed."""
    _world(host)
    _go(host, monkeypatch, "victim", win=True, msg_id="late-1")
    host.expire(".steal.seen.")
    host.expire(".steal.cd.")
    host.now_ms += (event_dedupe.SEEN_TTL_SECONDS + 1) * 1000
    snap = _snapshot(host)
    env = _envelope("steal", tokens=["victim"], message_id="late-1")
    env.event.occurred_at = rfc3339_from_millis(
        host.now_ms - (event_dedupe.SEEN_TTL_SECONDS + 1) * 1000
    )
    _run(dispatch(env, {}, http_client=None))
    assert _snapshot(host) == snap and len(host.eco.ledger) == 1


@pytest.mark.parametrize("stamp", ["", "   ", "yesterday", "2026-10-05T00:00:00", None, 12345])
def test_a_missing_or_malformed_occurred_at_fails_loud_and_moves_nothing(
    host: _Host, stamp: Any
) -> None:
    _world(host)
    env = _envelope("steal", tokens=["victim"])
    env.event.occurred_at = stamp
    with pytest.raises(RuntimeError, match="occurred_at"):
        _run(dispatch(env, {}, http_client=None))
    assert "unavailable" in _reply(host)
    assert host.eco.ledger == [] and not host.kv.store


def test_a_future_dated_event_fails_loud_and_moves_nothing(host: _Host) -> None:
    _world(host)
    env = _envelope("steal", tokens=["victim"])
    env.event.occurred_at = _now_stamp(event_dedupe.EVENT_CLOCK_SKEW_SECONDS + 60)
    with pytest.raises(RuntimeError, match="occurred_at"):
        _run(dispatch(env, {}, http_client=None))
    assert host.eco.ledger == [] and not host.kv.store


def test_a_full_key_quota_fails_closed_and_moves_no_money(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host refuses a NEW key once 10,000 are live: the claim raises, so nothing settles."""
    _world(host)
    _no_cooldown(host)
    import wit_world

    real = wit_world.imports.kv.increment

    def quota_full(key: str, delta: int, ttl: int) -> int:
        if ".seen." in key:
            raise RuntimeError("quota exceeded")
        return real(key, delta, ttl)

    wit_world.imports.kv.increment = quota_full
    with pytest.raises(RuntimeError, match="backend failed"):
        _go(host, monkeypatch, "victim", win=True, msg_id="q-1")
    assert (
        host.eco.ledger == []
        and _kv_int(host, f"steal.wins.{app._pseudonym(COMMUNITY, ACTOR)}") == 0
    )
    assert "unavailable" in _reply(host)


def test_the_claim_is_recorded_before_the_mutation(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At-most-once: an ambiguous host failure is never retried into a second settlement."""
    _world(host)

    def down(user: str, stake: int, payout: int) -> int:
        raise RuntimeError("storage down")  # classified as BackendError (may have committed)

    import wit_world

    real = wit_world.imports.economy.wager
    wit_world.imports.economy.wager = down
    _go(host, monkeypatch, "victim", win=True, msg_id="crash-1")
    wit_world.imports.economy.wager = real
    host.expire(".steal.cd.")
    sent = len(host.relay_calls)
    assert _go(host, monkeypatch, "victim", win=True, msg_id="crash-1") is None
    assert len(host.relay_calls) == sent and host.eco.ledger == []


@pytest.mark.parametrize("missing", [None, "", "  ", 12345])
def test_a_steal_without_a_message_id_fails_loud_and_moves_nothing(
    host: _Host, monkeypatch: pytest.MonkeyPatch, missing: Any
) -> None:
    _world(host)
    env = _envelope("steal", tokens=["victim"])
    if missing is None:
        env.event.payload.pop("message_id")
    else:
        env.event.payload["message_id"] = missing
    with pytest.raises(RuntimeError, match="message_id"):
        _run(dispatch(env, {}, http_client=None))
    assert "unavailable" in _reply(host)
    assert host.eco.ledger == [] and not host.kv.store
    assert any("steal.missing_event_id" in m for _l, m, _f in host.log_calls)


def test_list_usage_and_config_need_no_message_id(host: _Host) -> None:
    for command, extra in (
        ("list", {}),
        ("usage", {}),
        ("config_set_cooldown", {"arg": "cooldown 5", "is_mod": True}),
    ):
        env = _envelope(command, **extra)
        env.event.payload.pop("message_id")
        _run(dispatch(env, {}, http_client=None))
    assert len(host.relay_calls) == 3


def test_dedupe_state_is_one_pii_free_marker_per_event(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(host)
    _no_cooldown(host)
    for i in range(6):
        _go(host, monkeypatch, "victim", win=False, msg_id=f"private-platform-id-{i}")
    assert len(_markers(host)) == 6
    for key in _markers(host):
        assert re.fullmatch(rf"c\.{COMMUNITY}\.steal\.seen\.[0-9a-f]{{32}}", key)
    blob = " ".join(host.kv.store) + " ".join(v.decode() for v in host.kv.store.values())
    assert "private-platform-id" not in blob
    assert "private-platform-id" not in " ".join(f"{m} {f}" for _l, m, f in host.log_calls)


@pytest.mark.parametrize("bad", [b"garbage", b"-5"])
def test_a_corrupt_event_marker_never_lets_the_event_settle_again(
    host: _Host, monkeypatch: pytest.MonkeyPatch, bad: bytes
) -> None:
    """The peek sees an existing marker and skips; if the peek were raced past, `claim` raises."""
    _world(host)
    _no_cooldown(host)
    _go(host, monkeypatch, "victim", win=False, msg_id="corrupt-1")
    (key,) = _markers(host)
    host.kv.store[key] = bad
    ledger, replies = list(host.eco.ledger), len(host.relay_calls)
    assert _go(host, monkeypatch, "victim", win=False, msg_id="corrupt-1") is None
    assert host.eco.ledger == ledger and len(host.relay_calls) == replies

    async def peek_misses(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(app.event_dedupe, "already_seen", peek_misses)
    with pytest.raises(RuntimeError, match="backend failed"):
        _go(host, monkeypatch, "victim", win=False, msg_id="corrupt-1")
    assert host.eco.ledger == ledger


# -- anti-collusion: pair cap, reciprocal block, per-actor budget ---------------


def test_pair_cap_bounds_what_one_actor_can_mint_from_one_target(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(host)
    _no_cooldown(host)
    monkeypatch.setattr(app, "PAIR_WINDOW_CAP", 150)
    replies = [_go(host, monkeypatch, "victim", win=True) for _ in range(4)]
    assert "stole 100 points" in (replies[0] or "")
    assert "stole 50 points" in (replies[1] or "")  # clamped to what the pair cap has left
    assert all(r is not None and "all you can" in r for r in replies[2:])
    assert _minted(host) == 150
    assert host.eco.balances[ACTOR] == 650
    assert _pair_value(host, ACTOR, TARGET) == 150


def test_a_refused_steal_moves_no_money_and_burns_no_budget(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(host)
    _no_cooldown(host)
    monkeypatch.setattr(app, "PAIR_WINDOW_CAP", 100)
    _go(host, monkeypatch, "victim", win=True)
    before = (dict(host.eco.balances), list(host.eco.ledger), _budget_value(host, ACTOR))
    assert "all you can" in (_go(host, monkeypatch, "victim", win=True) or "")
    assert (dict(host.eco.balances), list(host.eco.ledger), _budget_value(host, ACTOR)) == before


def test_losses_and_unused_reservation_never_consume_a_cap(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(host)
    _no_cooldown(host)
    for _ in range(5):
        assert "caught" in (_go(host, monkeypatch, "victim", win=False) or "")
    assert _pair_value(host, ACTOR, TARGET) == 0 and _budget_value(host, ACTOR) == 0
    monkeypatch.setattr(app, "_roll", lambda percent: True)
    monkeypatch.setattr(app._RNG, "randint", lambda a, b: 40)  # wins 40 of a reserved 100
    _run(dispatch(_envelope("steal", tokens=["victim"]), {}, http_client=None))
    assert _pair_value(host, ACTOR, TARGET) == 40 and _budget_value(host, ACTOR) == 40


def test_the_reciprocal_steal_is_blocked_this_window_and_the_next(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(host)
    _no_cooldown(host)
    assert "stole" in (_go(host, monkeypatch, "victim", win=True, actor=ACTOR) or "")
    snap = (dict(host.eco.balances), list(host.eco.ledger))
    for _ in range(2):
        assert "steal back" in (_go(host, monkeypatch, "ally", win=True, actor=TARGET) or "")
    assert (dict(host.eco.balances), list(host.eco.ledger)) == snap
    host.now_ms += app.WINDOW_SECONDS * 1000  # next bucket: the previous one still counts
    assert "steal back" in (_go(host, monkeypatch, "ally", win=True, actor=TARGET) or "")
    host.now_ms += app.WINDOW_SECONDS * 1000  # two buckets on: the old win has aged out
    assert "stole" in (_go(host, monkeypatch, "ally", win=True, actor=TARGET) or "")


def test_a_caught_steal_mints_nothing_so_it_does_not_block_the_reverse(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(host)
    _no_cooldown(host)
    assert "caught" in (_go(host, monkeypatch, "victim", win=False, actor=ACTOR) or "")
    assert "stole" in (_go(host, monkeypatch, "ally", win=True, actor=TARGET) or "")


def test_the_reverse_block_is_per_pair_not_global(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(host)
    _no_cooldown(host)
    _go(host, monkeypatch, "victim", win=True, actor=ACTOR)
    assert "stole" in (_go(host, monkeypatch, "t2", win=True, actor=TARGET) or "")
    assert "stole" in (_go(host, monkeypatch, "victim", win=True, actor=T2) or "")


def test_simultaneous_opposite_steals_cannot_both_pass(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two directions share ONE counter, so the second increment always sees the first."""
    import contextvars

    import wit_world

    _world(host)
    _no_cooldown(host)
    _yielding_kv_all(monkeypatch)
    current: contextvars.ContextVar[str] = contextvars.ContextVar("actor")
    wit_world.imports.identity.resolve_actor = lambda: current.get()
    monkeypatch.setattr(type(host.eco), "_bind", lambda self, payer: None)
    monkeypatch.setattr(app, "_roll", lambda percent: True)
    monkeypatch.setattr(app._RNG, "randint", lambda a, b: b)

    async def side(user: str, mention: str) -> None:
        current.set(user)
        await dispatch(_envelope("steal", tokens=[mention]), {}, http_client=None)

    async def race() -> None:
        await asyncio.gather(side(ACTOR, "victim"), side(TARGET, "ally"))

    _run(race())
    replies = [json.loads(m)["text"] for _p, m in host.relay_calls]
    assert sum("stole 100 points" in r for r in replies) == 1, replies
    assert _minted(host) <= app.LOOT_CAP


def test_the_actor_budget_spans_targets_and_clamps_the_last_grant(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(host)
    _no_cooldown(host)
    monkeypatch.setattr(app, "ACTOR_WINDOW_BUDGET", 250)
    replies = [_go(host, monkeypatch, m, win=True) for m in ("victim", "t2", "t3", "t4")]
    assert "stole 100" in (replies[0] or "") and "stole 100" in (replies[1] or "")
    assert "stole 50 points" in (replies[2] or "")  # only 50 of the 250 budget was left
    assert "steal limit" in (replies[3] or "")
    assert _minted(host) == 250 and _budget_value(host, ACTOR) == 250
    assert _pair_value(host, ACTOR, T3) == 50, "the pair's surplus over the budget was returned"


def test_caps_reset_in_the_next_window(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _world(host)
    _no_cooldown(host)
    monkeypatch.setattr(app, "PAIR_WINDOW_CAP", 100)
    _go(host, monkeypatch, "victim", win=True)
    assert "all you can" in (_go(host, monkeypatch, "victim", win=True) or "")
    host.now_ms += app.WINDOW_SECONDS * 1000
    assert "stole" in (_go(host, monkeypatch, "victim", win=True) or "")


@pytest.mark.parametrize("racers", [3, 10])
def test_concurrent_steals_cannot_overshoot_the_pair_cap(
    host: _Host, monkeypatch: pytest.MonkeyPatch, racers: int
) -> None:
    """Caps are reserved atomically BEFORE the roll, so a burst cannot all read 'room left'."""
    _world(host)
    _no_cooldown(host)
    _yielding_kv_all(monkeypatch)
    monkeypatch.setattr(app, "PAIR_WINDOW_CAP", 150)
    monkeypatch.setattr(app, "_roll", lambda percent: True)
    monkeypatch.setattr(app._RNG, "randint", lambda a, b: b)

    async def race() -> None:
        await asyncio.gather(
            *(
                dispatch(_envelope("steal", tokens=["victim"]), {}, http_client=None)
                for _ in range(racers)
            )
        )

    _run(race())
    assert 0 < _minted(host) <= 150, _minted(host)


def test_concurrent_steals_cannot_overshoot_the_actor_budget_across_targets(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(host)
    _no_cooldown(host)
    _yielding_kv_all(monkeypatch)
    monkeypatch.setattr(app, "ACTOR_WINDOW_BUDGET", 220)
    monkeypatch.setattr(app, "_roll", lambda percent: True)
    monkeypatch.setattr(app._RNG, "randint", lambda a, b: b)

    async def race() -> None:
        await asyncio.gather(
            *(
                dispatch(_envelope("steal", tokens=[m]), {}, http_client=None)
                for m in ("victim", "t2", "t3", "victim", "t2", "t3")
            )
        )

    _run(race())
    assert 0 < _minted(host) <= 220, _minted(host)


def test_two_colluding_accounts_cannot_net_mint_beyond_one_pair_cap_per_window(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worst case for the house: every roll wins at max loot, both accounts farm each other."""
    _world(host)
    _no_cooldown(host)
    windows = 3
    for w in range(windows):
        minted_before = _minted(host)
        for _ in range(12):
            _go(host, monkeypatch, "victim", win=True, actor=ACTOR)
            _go(host, monkeypatch, "ally", win=True, actor=TARGET)
        assert _minted(host) - minted_before <= app.PAIR_WINDOW_CAP, (w, _minted(host))
        host.now_ms += app.WINDOW_SECONDS * 1000
    assert _minted(host) <= windows * app.PAIR_WINDOW_CAP


def test_a_host_refusal_gives_the_whole_reservation_back(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(host)
    _no_cooldown(host)
    host.eco.payout_multiple = 1  # payout cap == stake: any loot is an OverCap refusal
    assert "exceeds the economy cap" in (_go(host, monkeypatch, "victim", win=True) or "")
    assert host.eco.ledger == []
    assert _pair_value(host, ACTOR, TARGET) == 0 and _budget_value(host, ACTOR) == 0


def test_an_ambiguous_backend_error_keeps_the_reservation(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wager that may have committed must never hand its cap back (a refund could re-mint)."""
    import wit_world

    _world(host)
    _no_cooldown(host)

    def down(user: str, stake: int, payout: int) -> int:
        raise RuntimeError("storage down")

    wit_world.imports.economy.wager = down
    assert "temporarily unavailable" in (_go(host, monkeypatch, "victim", win=True) or "")
    assert _pair_value(host, ACTOR, TARGET) == app.LOOT_CAP
    assert _budget_value(host, ACTOR) == app.LOOT_CAP


@pytest.mark.parametrize("bad", [b"not-a-number", b"-5", str(1 << 31).encode()])
def test_corrupt_cap_counters_fail_closed(
    host: _Host, monkeypatch: pytest.MonkeyPatch, bad: bytes
) -> None:
    _world(host)
    _no_cooldown(host)
    w = host.now_ms // (app.WINDOW_SECONDS * 1000)
    pair, _low = app._pair_id(COMMUNITY, ACTOR, TARGET)
    host.kv.store[_scoped_key(COMMUNITY, f"steal.pair.{w - 1}.{pair}")] = bad
    monkeypatch.setattr(app, "_roll", lambda percent: True)
    with pytest.raises(RuntimeError, match="backend failed"):
        _run(dispatch(_envelope("steal", tokens=["victim"]), {}, http_client=None))
    assert host.eco.ledger == []


def test_cap_keys_are_pii_free_and_colon_free(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _world(host)
    _no_cooldown(host)
    _go(host, monkeypatch, "victim", win=True, msg_id="platform-private-id")
    for key in host.kv.store:
        body = key.removeprefix("c.")
        assert ":" not in body
        for raw in (ACTOR, TARGET, "viewer-1", "victim", "platform-private-id"):
            assert raw not in key
    blob = " ".join(f"{m} {f}" for _l, m, f in host.log_calls)
    for raw in (ACTOR, TARGET, "viewer-1", "platform-private-id"):
        assert raw not in blob


# -- cap keys are TTL-bounded to their window ----------------------------------------------------

_WIN_MS = app.WINDOW_SECONDS * 1000
_GRACE_MS = app._SETTLE_GRACE_SECONDS * 1000


def _increments(host: _Host) -> list[tuple[str, int, int]]:
    """Every `(key, delta, ttl)` the fake kv saw an `increment` for."""
    return [args for name, args in host.kv.calls if name == "increment"]


def _cap_expiries(host: _Host) -> dict[str, list[int]]:
    """Absolute expiry (ms) every cap-key `increment` asked for, by key family."""
    out: dict[str, list[int]] = {"pair": [], "budget": []}
    for key, _delta, ttl in _increments(host):
        for family in out:
            if f".steal.{family}." in key:
                assert ttl > 0, f"{family} key written without a TTL (would hold quota forever)"
                out[family].append(host.now_ms + ttl * 1000)
    return out


@pytest.mark.parametrize("into_window_s", [0, 1, 1800, 3599])
def test_cap_keys_expire_at_the_end_of_their_window_not_a_fixed_span(
    host: _Host, monkeypatch: pytest.MonkeyPatch, into_window_s: int
) -> None:
    """A budget key lapses at the end of ITS window, a pair key at the end of the NEXT one."""
    _world(host)
    _no_cooldown(host)
    host.now_ms = 700 * _WIN_MS + into_window_s * 1000
    index = host.now_ms // _WIN_MS
    _go(host, monkeypatch, "victim", win=True)
    got = _cap_expiries(host)
    assert got["pair"] and got["budget"], "the steal must have touched both cap families"
    budget_end, pair_end = (index + 1) * _WIN_MS, (index + 2) * _WIN_MS
    assert all(budget_end <= e <= budget_end + _GRACE_MS + 1000 for e in got["budget"])
    assert all(pair_end <= e <= pair_end + _GRACE_MS + 1000 for e in got["pair"])


def test_a_budget_key_made_late_in_a_window_is_not_given_a_fresh_full_window(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(host)
    _no_cooldown(host)
    host.now_ms = 700 * _WIN_MS + (app.WINDOW_SECONDS - 1) * 1000
    _go(host, monkeypatch, "victim", win=True)
    budget_ttls = [ttl for key, _d, ttl in _increments(host) if ".steal.budget." in key]
    assert budget_ttls and max(budget_ttls) <= 1 + app._SETTLE_GRACE_SECONDS + 1


def test_every_cap_key_write_carries_a_ttl_through_reserve_refund_and_release(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Win (reserve + refund of the unused loot), loss (reserve + full release), clamp, refusal."""
    _world(host)
    _no_cooldown(host)
    monkeypatch.setattr(app, "PAIR_WINDOW_CAP", 150)
    for win in (True, False, True, True):
        _go(host, monkeypatch, "victim", win=win)
    writes = [
        (key, ttl)
        for key, _d, ttl in _increments(host)
        if ".steal.pair." in key or ".steal.budget." in key
    ]
    assert len(writes) >= 12
    assert all(ttl > 0 for _key, ttl in writes)
    assert all(ttl <= 2 * app.WINDOW_SECONDS + app._SETTLE_GRACE_SECONDS for _key, ttl in writes)


@pytest.mark.parametrize("now_ms", [0, 1, _WIN_MS - 1, _WIN_MS, 5 * _WIN_MS + 123])
def test_bucket_ttls_are_positive_and_bounded(now_ms: int) -> None:
    bucket = app._Bucket.at(now_ms)
    assert bucket.index == now_ms // _WIN_MS
    assert 0 < bucket.budget_ttl <= app.WINDOW_SECONDS + app._SETTLE_GRACE_SECONDS
    assert bucket.budget_ttl < bucket.pair_ttl <= 2 * app.WINDOW_SECONDS + app._SETTLE_GRACE_SECONDS


# -- stage split: economy/identity in transform (process stage), relay only in dispatch -----------


def test_the_whole_steal_settles_in_transform_and_dispatch_only_relays(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real chat line: economy/identity calls run in the process stage, relay in the action."""
    monkeypatch.setattr(app, "_roll", lambda percent: False)
    chat = _event("!steal {user:victim}", message_id="stage-1")
    chat.occurred_at = _now_stamp()  # inside the replay age fence
    out = _run(transform(chat))
    assert out is not None
    assert host.eco.ledger != [], out.payload
    assert host.ident.calls == ["actor", "mention:victim"]
    assert host.relay_calls == [], "transform cannot relay (process stage)"
    assert set(out.payload) == {"channel_id", "text", "command"}
    ledger, calls = list(host.eco.ledger), list(host.ident.calls)

    env = StageEnvelope(
        tenant="t",
        community=COMMUNITY,
        app_id="waddles.core.example.steal",
        stage="action",
        event=out,
        ts="2026-10-05T00:00:00.000Z",
    )
    result = _run(action_dispatch(env, {}, http_client=None))
    assert result.detail == "steal"
    assert "caught" in _reply(host)
    assert host.eco.ledger == ledger and host.ident.calls == calls, "dispatch moved no money"
    assert GUARD.violations == []


@pytest.mark.parametrize("command", ["steal", "list", "usage", "config_set_cooldown"])
def test_dispatch_makes_no_economy_or_identity_call(host: _Host, command: str) -> None:
    """`svc_action` serves no economy/identity: dispatch relays the given text and nothing else."""
    event = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"channel_id": "12345", "text": "already computed", "command": command},
        occurred_at=_now_stamp(),
    )
    env = StageEnvelope(
        tenant="t",
        community=COMMUNITY,
        app_id="waddles.core.example.steal",
        stage="action",
        event=event,
        ts="2026-10-05T00:00:00.000Z",
    )
    _run(action_dispatch(env, {}, http_client=None))
    assert _reply(host) == "already computed"
    assert host.ident.calls == [] and host.eco.ledger == []
    assert GUARD.violations == []


def test_transform_failure_is_deferred_to_dispatch_not_relayed(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kv outage in transform cannot relay: it is marked, and dispatch relays then raises."""
    import wit_world

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(wit_world.imports.kv, "get", boom)
    out = _run(transform(_event("!steal list")))
    assert out is not None and out.payload["failure"] == "steal state backend failed"
    assert host.relay_calls == []
    env = StageEnvelope(
        tenant="t",
        community=COMMUNITY,
        app_id="waddles.core.example.steal",
        stage="action",
        event=out,
        ts="2026-10-05T00:00:00.000Z",
    )
    with pytest.raises(RuntimeError, match="backend failed"):
        _run(action_dispatch(env, {}, http_client=None))
    assert "unavailable" in _reply(host)
