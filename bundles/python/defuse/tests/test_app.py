"""Host-native tests for `defuse`: shared fake kv/economy/identity hosts, no WASM."""

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
from app import DEFAULT_STAKE, dispatch, transform

ACTOR = "11111111-1111-4111-8111-111111111111"
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
    eco = FakeEconomyHost(members={ACTOR}, actor=ACTOR)
    eco.balances = {ACTOR: 500}
    ident = FakeIdentityHost(members={ACTOR}, actor=ACTOR)
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
        app_id="waddles.core.example.defuse",
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


def _play(
    host: _Host, monkeypatch: pytest.MonkeyPatch, *, win: bool, arg: str | None = None
) -> str:
    monkeypatch.setattr(app, "_roll", lambda percent: win)
    extra = {"arg": arg} if arg is not None else {}
    _run(dispatch(_envelope("play", **extra), {}, http_client=None))
    return _reply(host)


# -- transform -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "command", "arg"),
    [
        ("!defuse", "play", None),
        ("!DEFUSE 25", "play", "25"),
        ("  !defuse all ", "play", "all"),
        ("!defuse list", "list", None),
        ("!defuse list x", "usage", None),
        ("!defuse set odds 50", "config_set_odds", "odds 50"),
        ("!defuse enable", "usage", None),
        ("!defuse banana", "usage", None),
    ],
)
def test_transform_grammar(host: _Host, text: str, command: str, arg: str | None) -> None:
    out = _run(transform(_event(text)))
    assert out is not None
    assert out.payload["command"] == command
    assert out.payload.get("arg") == arg


@pytest.mark.parametrize("text", ["!defused", "hello", "defuse"])
def test_transform_ignores_non_matching(host: _Host, text: str) -> None:
    assert _run(transform(_event(text))) is None


def test_transform_ignores_non_string_text(host: _Host) -> None:
    ev = _event("x")
    ev.payload["text"] = 5
    assert _run(transform(ev)) is None


def test_badges_strict_is_true(host: _Host) -> None:
    out = _run(transform(_event("!defuse list", is_mod="false", is_broadcaster=1)))
    assert out is not None
    assert out.payload["is_mod"] is False
    assert out.payload["is_broadcaster"] is False


# -- play ----------------------------------------------------------------------


def test_win_pays_double(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "DEFUSED" in _play(host, monkeypatch, win=True, arg="50")
    assert host.eco.balances[ACTOR] == 550


def test_loss_forfeits_stake(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "BOOM" in _play(host, monkeypatch, win=False, arg="50")
    assert host.eco.balances[ACTOR] == 450


def test_bare_uses_default_stake(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _play(host, monkeypatch, win=False)
    assert host.eco.balances[ACTOR] == 500 - DEFAULT_STAKE


def test_all_bets_max_bet(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.eco.max_bet_cap = 120
    _play(host, monkeypatch, win=False, arg="all")
    assert host.eco.balances[ACTOR] == 380


def test_all_with_empty_balance_refused(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.eco.balances[ACTOR] = 0
    assert "no points" in _play(host, monkeypatch, win=True, arg="all")
    assert host.eco.ledger == []


@pytest.mark.parametrize("arg", ["0", "abc", "-5"])
def test_bad_stake_refused(host: _Host, monkeypatch: pytest.MonkeyPatch, arg: str) -> None:
    _play(host, monkeypatch, win=True, arg=arg)
    assert host.eco.ledger == []


def test_insufficient_funds_refused(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.eco.balances[ACTOR] = 5
    assert "only have 5" in _play(host, monkeypatch, win=True, arg="50")
    assert host.eco.balances[ACTOR] == 5


def test_over_cap_refused(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.eco.max_bet_cap = 20
    assert "max bet is 20" in _play(host, monkeypatch, win=True, arg="50")
    assert host.eco.ledger == []


def test_unlinked_actor_refused(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.ident.actor = None
    assert "link your account" in _play(host, monkeypatch, win=True, arg="5")
    assert host.eco.ledger == []


def test_economy_unavailable_replies_not_crashes(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.eco.granted = False
    assert "temporarily unavailable" in _play(host, monkeypatch, win=True, arg="5")


def test_cooldown_blocks_then_expires(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _play(host, monkeypatch, win=False, arg="5")
    assert "easy" in _play(host, monkeypatch, win=False, arg="5")
    assert host.eco.balances[ACTOR] == 495
    host.now_ms += 11_000
    assert "BOOM" in _play(host, monkeypatch, win=False, arg="5")


def test_rejected_play_does_not_start_cooldown(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _play(host, monkeypatch, win=True, arg="9999")
    assert "DEFUSED" in _play(host, monkeypatch, win=True, arg="5")


def test_default_odds_roll(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    monkeypatch.setattr(app, "_roll", lambda p: seen.append(p) or True)
    _run(dispatch(_envelope("play", arg="5"), {}, http_client=None))
    assert seen == [app.DEFAULT_ODDS_PERCENT]


def test_roll_distribution_bounds() -> None:
    assert app._roll(100) is True
    assert app._roll(0) is False


# -- config / list / usage -----------------------------------------------------


def test_set_odds_applies_to_next_roll(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(dispatch(_envelope("config_set_odds", arg="odds 70", is_mod=True), {}, http_client=None))
    assert "set to 70%" in _reply(host)
    seen: list[int] = []
    monkeypatch.setattr(app, "_roll", lambda p: seen.append(p) or False)
    _run(dispatch(_envelope("play", arg="5"), {}, http_client=None))
    assert seen == [70]


def test_set_odds_denied_cases(host: _Host) -> None:
    for extra in ({}, {"is_mod": False}):
        _run(dispatch(_envelope("config_set_odds", arg="odds 70", **extra), {}, http_client=None))
        assert "only moderators" in _reply(host)


@pytest.mark.parametrize("arg", ["odds", "odds abc", "odds 0", "odds 96", "cooldown 5"])
def test_set_odds_bad_input(host: _Host, arg: str) -> None:
    _run(dispatch(_envelope("config_set_odds", arg=arg, is_broadcaster=True), {}, http_client=None))
    assert "win odds set" not in _reply(host)


def test_corrupt_odds_falls_back_to_default(host: _Host) -> None:
    host.kv.store[_scoped_key(COMMUNITY, "defuse.config.odds")] = b"junk"
    assert _run(app._get_odds(COMMUNITY)) == app.DEFAULT_ODDS_PERCENT


def test_list_before_and_after(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(dispatch(_envelope("list"), {}, http_client=None))
    assert "haven't defused" in _reply(host)
    _play(host, monkeypatch, win=True, arg="5")
    _run(dispatch(_envelope("list"), {}, http_client=None))
    assert "attempted: 1. Defused: 1" in _reply(host)


def test_usage_reply(host: _Host) -> None:
    _run(dispatch(_envelope("usage"), {}, http_client=None))
    assert _reply(host) == app._USAGE


# -- dispatch guards / PII -----------------------------------------------------


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


def test_keys_and_logs_pii_free(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _play(host, monkeypatch, win=True, arg="5")
    pseud = hashlib.sha256(f"{COMMUNITY}:{ACTOR}".encode()).hexdigest()
    assert _scoped_key(COMMUNITY, f"defuse.plays.{pseud}") in host.kv.store
    for key in host.kv.store:
        assert "viewer-1" not in key
        assert ACTOR not in key
    blob = " ".join(f"{m} {f}" for _, m, f in host.log_calls)
    assert "viewer-1" not in blob
    assert ACTOR not in blob


# hardening: output sanitizer / activation-only
ZWSP = "\u200b"


def _envelope_as(actor: str | None, command: str, **payload: Any) -> StageEnvelope:
    return StageEnvelope(
        tenant="t",
        community=COMMUNITY,
        app_id="waddles.core.example.defuse",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor=actor,
            payload={"command": command, "channel_id": "12345", **payload},
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )


def _win_reply_as(host: _Host, monkeypatch: pytest.MonkeyPatch, actor: str | None) -> str:
    monkeypatch.setattr(app, "_roll", lambda percent: True)
    _run(dispatch(_envelope_as(actor, "play"), {}, http_client=None))
    return _reply(host)


def test_minted_actor_token_stays_live_in_the_reply(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline's own `{user:<token>}` actor must still resolve to a display name."""
    assert _win_reply_as(host, monkeypatch, "{user:tok-actor}").startswith(
        "\U0001f4a3 {user:tok-actor} cuts the "
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
    assert _win_reply_as(host, monkeypatch, actor).startswith("\U0001f4a3 " + label + " cuts the ")


def test_missing_actor_falls_back_to_someone(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _win_reply_as(host, monkeypatch, None).startswith("\U0001f4a3 someone cuts the ")


def test_config_echo_of_typed_value_is_neutralized(host: _Host) -> None:
    _run(
        dispatch(_envelope("config_set_odds", arg="odds <@123>", is_mod=True), {}, http_client=None)
    )
    assert _reply(host) == "'<@" + ZWSP + "123>' isn't a whole number"


# hardening: activation-only
def test_runs_on_activation_alone_when_flag_backend_says_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activation-gated only: an OFF `waddles.command-defuse` flag must not drop the command."""
    _install(monkeypatch, flag=False)
    assert _run(transform(_event("!defuse"))) is not None
