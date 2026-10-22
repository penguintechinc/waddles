"""Host-native tests for `steal`: shared fake kv/economy/identity hosts, no WASM."""

from __future__ import annotations

import asyncio
import hashlib
import json
import types
from typing import Any

import pytest
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import (
    FakeEconomyHost,
    FakeIdentityHost,
    FakeKvHost,
    install_fake_kv_host,
)

import app
from app import STAKE, dispatch, transform

ACTOR = "11111111-1111-4111-8111-111111111111"
TARGET = "22222222-2222-4222-8222-222222222222"
COMMUNITY = "comm-1"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Host:
    def __init__(self, kv: FakeKvHost, eco: FakeEconomyHost, ident: FakeIdentityHost) -> None:
        self.kv, self.eco, self.ident = kv, eco, ident
        self.relay_calls: list[tuple[str, Any]] = []
        self.log_calls: list[tuple[Any, str, str]] = []
        self.now_ms = 1_700_000_000_000


def _install(monkeypatch: pytest.MonkeyPatch, *, flag: bool = True) -> _Host:
    kv = install_fake_kv_host(monkeypatch)
    eco = FakeEconomyHost(members={ACTOR, TARGET}, actor=ACTOR)
    eco.balances = {ACTOR: 500, TARGET: 300}
    ident = FakeIdentityHost(members={ACTOR, TARGET}, actor=ACTOR)
    ident.add_mention("victim", TARGET)
    host = _Host(kv, eco, ident)
    import wit_world

    ns = wit_world.imports
    ns.economy = types.SimpleNamespace(
        balance=eco.balance,
        wager=eco.wager,
        transfer=eco.transfer,
        max_bet=eco.max_bet,
        leaderboard=eco.leaderboard,
    )
    ns.identity = types.SimpleNamespace(
        resolve_actor=ident.resolve_actor, resolve_mention=ident.resolve_mention
    )
    ns.flags = types.SimpleNamespace(enabled=lambda key, default_value: flag)
    ns.relay = types.SimpleNamespace(push=lambda p, m: host.relay_calls.append((p, m)))
    ns.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, f: host.log_calls.append((lvl, msg, f)),
    )
    ns.clock = types.SimpleNamespace(
        now_millis=lambda: host.now_ms, now_rfc3339=lambda: "x", monotonic_nanos=lambda: 0
    )
    return host


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    return _install(monkeypatch)


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
            payload={"command": command, "channel_id": "12345", **payload},
            occurred_at="2026-10-05T00:00:00.000Z",
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


def test_transform_forwards_mention_tokens(host: _Host) -> None:
    out = _run(transform(_event("!steal {user:victim}")))
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
    out = _run(transform(_event(text)))
    assert out is not None
    assert out.payload["command"] == command


@pytest.mark.parametrize("text", ["!stealth", "hello", "!steals {user:victim}"])
def test_transform_ignores_non_matching(host: _Host, text: str) -> None:
    assert _run(transform(_event(text))) is None


def test_transform_ignores_non_string_text(host: _Host) -> None:
    ev = _event("x")
    ev.payload["text"] = 5
    assert _run(transform(ev)) is None


def test_flag_off_drops_after_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, flag=False)
    assert _run(transform(_event("!steal {user:victim}"))) is None


def test_badges_strict_is_true(host: _Host) -> None:
    out = _run(transform(_event("!steal list", is_mod="false", is_broadcaster=1)))
    assert out is not None
    assert out.payload["is_mod"] is False
    assert out.payload["is_broadcaster"] is False
    bare = _run(transform(_event("!steal list")))
    assert bare is not None
    assert "is_mod" not in bare.payload


# -- steal flow ----------------------------------------------------------------


def test_success_pays_loot_bounded_by_target_balance(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.eco.balances[TARGET] = 3
    monkeypatch.setattr(app.random, "randint", lambda a, b: b)
    reply = _steal(host, monkeypatch, win=True)
    assert "stole 3 points" in reply
    assert host.eco.balances[ACTOR] == 503


def test_success_loot_capped(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.eco.balances[TARGET] = 10_000
    monkeypatch.setattr(app.random, "randint", lambda a, b: b)
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
