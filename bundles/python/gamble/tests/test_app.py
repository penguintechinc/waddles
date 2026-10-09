"""Host-native tests for the `gamble` bundle (fake `wit_world`, in-memory kv; see `slots`)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.command import CommandSpec, ParsedCommand, parse_command
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

import app
from app import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_ODDS_PERCENT,
    STARTING_BALANCE,
    _amount_bucket,
    _balance_key,
    _caller_role_signal,
    _resolve_command,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _pseud(actor: str = "viewer-1") -> str:
    return hashlib.sha256(actor.encode()).hexdigest()


def _scoped(key: str, community: str = "comm-1") -> str:
    return _scoped_key(community, key)


class _FakeHost:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[Any, str, str]] = []
        self.now_ms = 1_700_000_000_000
        self.fail_op: str | None = None

    def texts(self) -> list[str]:
        return [json.loads(m)["text"] for _, m in self.relay_calls]


def _install(mp: pytest.MonkeyPatch, host: _FakeHost, *, flag: bool = True) -> None:
    def _maybe_fail(op: str) -> None:
        if host.fail_op == op:
            raise RuntimeError("boom")

    def kv_get(key: str) -> bytes | None:
        _maybe_fail("get")
        return host.store.get(key)

    def kv_set(key: str, value: bytes, ttl: int) -> None:
        _maybe_fail("set")
        host.store[key] = bytes(value)

    def kv_delete(key: str) -> None:
        host.store.pop(key, None)

    def kv_increment(key: str, delta: int, ttl: int) -> int:
        _maybe_fail("increment")
        new = int(host.store.get(key, b"0").decode()) + delta
        host.store[key] = str(new).encode()
        return new

    ns = types.SimpleNamespace
    mod = types.ModuleType("wit_world")
    mod.imports = ns(  # type: ignore[attr-defined]
        flags=ns(enabled=lambda key, default_value: flag),
        kv=ns(get=kv_get, set=kv_set, delete=kv_delete, increment=kv_increment),
        relay=ns(push=lambda provider, msg: host.relay_calls.append((provider, msg))),
        log=ns(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, fj: host.log_calls.append((lvl, msg, fj)),
        ),
        clock=ns(
            now_millis=lambda: host.now_ms,
            now_rfc3339=lambda: "2026-10-05T00:00:00.000Z",
            monotonic_nanos=lambda: 0,
        ),
    )
    mp.setitem(sys.modules, "wit_world", mod)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    h = _FakeHost()
    _install(monkeypatch, h)
    return h


def _event(text: str, **extra: Any) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": "12345", **extra}
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload=payload,
        occurred_at="2026-10-05T00:00:00.000Z",
    )


def _env(
    command: str, *, arg: str | None = None, community: str | None = "comm-1", **extra: Any
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": "12345", **extra}
    if arg is not None:
        payload["arg"] = arg
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.gamble",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload=payload,
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )


def _bet(arg: str, host: _FakeHost, *, win: bool, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(app, "_roll", lambda odds: win)
    _run(dispatch(_env("bet", arg=arg), {}, http_client=None))
    return host.texts()[-1]


def _balance(host: _FakeHost) -> int:
    return int(host.store[_scoped(_balance_key(_pseud()))].decode())


@pytest.mark.parametrize(
    ("text", "command", "arg"),
    [
        ("!gamble 50", "bet", "50"),
        ("!GAMBLE all", "bet", "all"),
        ("  !gamble   7 ", "bet", "7"),
        ("!gamble", "balance", None),
        ("!gamble set odds 60", "config_set_odds", "odds 60"),
        ("!gamble bogus", "usage", None),
        ("!gamble -5", "usage", None),
        ("!gamble 5.5", "usage", None),
        ("!gamble enable ai", "usage", None),
    ],
)
def test_transform_routes(host: _FakeHost, text: str, command: str, arg: str | None) -> None:
    out = _run(transform(_event(text)))
    assert out is not None
    assert out.payload["command"] == command
    assert out.payload.get("arg") == arg


@pytest.mark.parametrize("text", ["!gamblerr 5", "gamble 5", "hello", ""])
def test_transform_ignores_other_text(host: _FakeHost, text: str) -> None:
    assert _run(transform(_event(text))) is None


def test_transform_non_string_text(host: _FakeHost) -> None:
    ev = PlatformEvent("twitch", "chat.message", "v", {"text": None}, "")
    assert _run(transform(ev)) is None


def test_transform_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeHost(), flag=False)
    assert _run(transform(_event("!gamble 5"))) is None


def test_transform_badge_forwarding(host: _FakeHost) -> None:
    out = _run(transform(_event("!gamble", is_mod=True, is_broadcaster=False)))
    assert out is not None
    assert out.payload["is_mod"] is True
    assert out.payload["is_broadcaster"] is False
    bare = _run(transform(_event("!gamble")))
    assert bare is not None
    assert "is_mod" not in bare.payload


def test_resolve_command_unit() -> None:
    assert _resolve_command(None) == "usage"
    assert _resolve_command(parse_command("!gamble", CommandSpec("gamble"))) == "balance"
    assert _resolve_command(ParsedCommand("gamble", None, "sub", None)) == "usage"


def test_role_signal() -> None:
    assert _caller_role_signal({}) is None
    assert _caller_role_signal({"is_mod": False}) is False
    assert _caller_role_signal({"is_broadcaster": True}) is True


@pytest.mark.parametrize(
    ("n", "b"), [(1, "le10"), (50, "le100"), (500, "le1000"), (5000, "gt1000")]
)
def test_amount_bucket(n: int, b: str) -> None:
    assert _amount_bucket(n) == b


def test_win_credits_amount(host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    text = _bet("40", host, win=True, monkeypatch=monkeypatch)
    assert "WON" in text
    assert _balance(host) == STARTING_BALANCE + 40


def test_loss_debits_amount(host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    text = _bet("40", host, win=False, monkeypatch=monkeypatch)
    assert "lost" in text
    assert _balance(host) == STARTING_BALANCE - 40


def test_all_in_loss_floors_at_zero_and_blocks_further_bets(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bet("all", host, win=False, monkeypatch=monkeypatch)
    assert _balance(host) == 0
    host.now_ms += (DEFAULT_COOLDOWN_SECONDS + 1) * 1000
    assert "only have 0" in _bet("5", host, win=True, monkeypatch=monkeypatch)
    assert "no points" in _bet("all", host, win=True, monkeypatch=monkeypatch)


def test_bet_more_than_balance_rejected(host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "only have" in _bet(str(STARTING_BALANCE + 1), host, win=True, monkeypatch=monkeypatch)
    assert _balance(host) == STARTING_BALANCE


def test_zero_and_garbage_bet(host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "at least 1" in _bet("0", host, win=True, monkeypatch=monkeypatch)
    assert "Usage" in _bet("abc", host, win=True, monkeypatch=monkeypatch)


def test_concurrent_overdraw_is_floored(host: _FakeHost) -> None:
    ledger = app._Ledger("comm-1", "twitch", "1")
    host.store[_scoped(_balance_key("p"))] = b"10"
    assert _run(ledger.debit("p", 25)) == 0
    assert _balance_for(host, "p") == 0


def _balance_for(host: _FakeHost, pseudonym: str) -> int:
    return int(host.store[_scoped(_balance_key(pseudonym))].decode())


def test_cooldown_then_allowed(host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _bet("10", host, win=True, monkeypatch=monkeypatch)
    host.now_ms += 1000
    assert "slow down" in _bet("10", host, win=True, monkeypatch=monkeypatch)
    host.now_ms += (DEFAULT_COOLDOWN_SECONDS + 1) * 1000
    assert "WON" in _bet("10", host, win=True, monkeypatch=monkeypatch)


def test_corrupt_cooldown_state_ignored(host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    host.store[_scoped(f"gamble.lastbet.{_pseud()}")] = b"junk"
    assert "WON" in _bet("10", host, win=True, monkeypatch=monkeypatch)


def test_balance_command_seeds_and_reports(host: _FakeHost) -> None:
    _run(dispatch(_env("balance"), {}, http_client=None))
    assert f"{STARTING_BALANCE} points" in host.texts()[-1]
    assert _balance(host) == STARTING_BALANCE


def test_corrupt_balance_fails_loud(host: _FakeHost) -> None:
    host.store[_scoped(_balance_key(_pseud()))] = b"xx"
    with pytest.raises(RuntimeError):
        _run(dispatch(_env("balance"), {}, http_client=None))
    assert "temporarily unavailable" in host.texts()[-1]


def test_set_odds_permissions_and_bounds(host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(dispatch(_env("config_set_odds", arg="odds 60"), {}, http_client=None))
    assert "only moderators" in host.texts()[-1]
    _run(dispatch(_env("config_set_odds", arg="odds 60", is_mod=False), {}, http_client=None))
    assert "only moderators" in host.texts()[-1]
    _run(dispatch(_env("config_set_odds", arg="odds 60", is_mod=True), {}, http_client=None))
    assert "set to 60%" in host.texts()[-1]
    seen: list[int] = []
    monkeypatch.setattr(app, "_roll", lambda odds: seen.append(odds) or True)
    _run(dispatch(_env("bet", arg="5"), {}, http_client=None))
    assert seen == [60]
    for arg, needle in [
        ("odds 0", "between"),
        ("odds 96", "between"),
        ("odds x", "isn't a whole"),
        ("odds", "Usage"),
        (None, "Usage"),
        ("cooldown 5", "Usage"),
    ]:
        _run(dispatch(_env("config_set_odds", arg=arg, is_broadcaster=True), {}, http_client=None))
        assert needle in host.texts()[-1]


@pytest.mark.parametrize("raw", [b"junk", b"500"])
def test_bad_odds_config_falls_back(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    host.store[_scoped("gamble.config.odds")] = raw
    seen: list[int] = []
    monkeypatch.setattr(app, "_roll", lambda odds: seen.append(odds) or False)
    _run(dispatch(_env("bet", arg="5"), {}, http_client=None))
    assert seen == [DEFAULT_ODDS_PERCENT]


def test_roll_is_bounded() -> None:
    assert all(app._roll(100) for _ in range(50))
    assert not any(app._roll(0) for _ in range(50))


def test_usage_command(host: _FakeHost) -> None:
    result = _run(dispatch(_env("usage"), {}, http_client=None))
    assert result.detail == "usage"
    assert "Usage" in host.texts()[-1]


def test_dispatch_validation_errors(host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="unrecognized"):
        _run(dispatch(_env("nope"), {}, http_client=None))
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(_env("balance", community=None), {}, http_client=None))
    env = _env("balance")
    env.event.payload["channel_id"] = None
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(env, {}, http_client=None))


@pytest.mark.parametrize("op", ["get", "set", "increment"])
def test_kv_failures_fail_loud(host: _FakeHost, op: str) -> None:
    host.fail_op = op
    with pytest.raises(RuntimeError, match=f"kv {op} failed"):
        _run(dispatch(_env("bet", arg="5"), {}, http_client=None))
    assert "temporarily unavailable" in host.texts()[-1]


def test_logs_are_pii_free(host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(transform(_event("!gamble 25")))
    _bet("25", host, win=True, monkeypatch=monkeypatch)
    blob = " ".join(f"{m} {fj}" for _, m, fj in host.log_calls)
    assert host.log_calls
    assert "viewer-1" not in blob
    assert _pseud() not in blob
    assert "25" not in json.dumps([fj for _, _, fj in host.log_calls])
    assert "le100" in blob


def test_dispatch_result_shape(host: _FakeHost) -> None:
    result = _run(dispatch(_env("balance"), {}, http_client=None))
    assert (result.transport, result.sub_type, result.http_status) == ("twitch", None, None)
