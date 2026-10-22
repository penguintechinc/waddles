"""Host-native tests for `defuse`: shared fake kv/economy/identity hosts, no WASM."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import re
import types
from typing import Any

import pytest
from waddle_sdk import event_dedupe
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import (
    FakeEconomyHost,
    FakeIdentityHost,
    FakeKvHost,
    install_fake_kv_host,
    rfc3339_from_millis,
)

import app
from app import DEFAULT_STAKE, dispatch, transform

ACTOR = "11111111-1111-4111-8111-111111111111"
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
    eco = FakeEconomyHost(members={ACTOR}, actor=ACTOR)
    eco.balances = {ACTOR: 500}
    ident = FakeIdentityHost(members={ACTOR}, actor=ACTOR)
    host = _Host(kv, eco, ident)
    _CURRENT_HOST.append(host)
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
    host.expire(".defuse.cd.")  # the kv TTL lapsing is the host's job; the fake does not model it
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


# hardening: economy integrity (replay safety, atomic cooldown)
def _wagers(host: _Host) -> int:
    return sum(1 for kind, _user, _delta in host.eco.ledger if kind == "wager")


def _go(
    host: _Host,
    mp: pytest.MonkeyPatch,
    *,
    win: bool,
    arg: str | None = "5",
    msg_id: str | None = None,
) -> str | None:
    """One play delivery; returns the reply, or None when nothing was sent (a skipped dup)."""
    mp.setattr(app, "_roll", lambda percent: win)
    extra: dict[str, Any] = {"message_id": msg_id} if msg_id else {}
    if arg is not None:
        extra["arg"] = arg
    before = len(host.relay_calls)
    _run(dispatch(_envelope("play", **extra), {}, http_client=None))
    return _reply(host) if len(host.relay_calls) > before else None


def _stat(host: _Host, name: str) -> int:
    pseud = app._pseudonym(COMMUNITY, ACTOR)
    return int(host.kv.store.get(_scoped_key(COMMUNITY, f"defuse.{name}.{pseud}"), b"0"))


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
    out = _run(transform(_event("!defuse 5", message_id="m-9")))
    assert out is not None and out.payload["message_id"] == "m-9"


@pytest.mark.parametrize("bad", [None, 7, ["m"]])
def test_transform_does_not_forward_a_non_string_message_id(host: _Host, bad: Any) -> None:
    out = _run(transform(_event("!defuse 5", message_id=bad)))
    assert out is not None and "message_id" not in out.payload


def test_cooldown_is_one_atomic_increment_never_check_then_set(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _go(host, monkeypatch, win=False)
    cd = [c for c in host.kv.calls if ".defuse.cd." in c[1][0]]
    assert [c[0] for c in cd] == ["increment"], "the cooldown claim must be a single increment"
    assert cd[0][1][1:] == (1, app.DEFAULT_COOLDOWN_SECONDS)
    assert not any(".defuse.last." in c[1][0] for c in host.kv.calls)


@pytest.mark.parametrize("win", [False, True])
@pytest.mark.parametrize("racers", [2, 8])
def test_concurrent_plays_by_one_actor_exactly_one_wagers(
    host: _Host, monkeypatch: pytest.MonkeyPatch, win: bool, racers: int
) -> None:
    """regression: check-then-set let every concurrent play pass the cooldown."""
    _yielding_kv_all(monkeypatch)
    monkeypatch.setattr(app, "_roll", lambda percent: win)

    async def race() -> None:
        await asyncio.gather(
            *(dispatch(_envelope("play", arg="5"), {}, http_client=None) for _ in range(racers))
        )

    _run(race())
    replies = [json.loads(m)["text"] for _p, m in host.relay_calls]
    assert sum("easy" in r for r in replies) == racers - 1, replies
    assert _wagers(host) == 1 and _stat(host, "plays") == 1


def test_a_refused_attempt_restarts_the_window_and_says_so(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _go(host, monkeypatch, win=False)
    reply = _go(host, monkeypatch, win=False)
    assert f"try again in {app.DEFAULT_COOLDOWN_SECONDS}s" in (reply or "")
    incs = [c for c in host.kv.calls if c[0] == "increment" and ".defuse.cd." in c[1][0]]
    assert [c[1][2] for c in incs] == [app.DEFAULT_COOLDOWN_SECONDS] * 2


@pytest.mark.parametrize("arg", ["50", "9999"])
def test_a_refused_stake_does_not_burn_the_cooldown_or_the_event(
    host: _Host, monkeypatch: pytest.MonkeyPatch, arg: str
) -> None:
    host.eco.balances[ACTOR] = 20
    host.eco.max_bet_cap = 20
    assert _go(host, monkeypatch, win=True, arg=arg, msg_id="poor-1") is not None
    assert not any(".defuse.cd." in k or ".defuse.seen." in k for k in host.kv.store)
    assert "DEFUSED" in (_go(host, monkeypatch, win=True, arg="5", msg_id="poor-2") or "")


@pytest.mark.parametrize("win", [False, True])
def test_redelivery_after_the_cooldown_changes_nothing(
    host: _Host, monkeypatch: pytest.MonkeyPatch, win: bool
) -> None:
    """regression: a redelivered play re-rolled, re-wagered and double-counted plays/wins."""
    assert _go(host, monkeypatch, win=win, msg_id="dup-1") is not None
    host.expire(".defuse.cd.")
    snap = (dict(host.eco.balances), list(host.eco.ledger), dict(host.kv.store))
    for _ in range(3):
        assert _go(host, monkeypatch, win=win, msg_id="dup-1") is None
    assert (dict(host.eco.balances), list(host.eco.ledger), dict(host.kv.store)) == snap
    assert _stat(host, "plays") == 1 and _stat(host, "wins") == (1 if win else 0)


def test_a_redelivery_that_would_roll_the_other_way_is_still_skipped(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _go(host, monkeypatch, win=False, msg_id="flip-1")
    host.expire(".defuse.cd.")
    snap = (dict(host.eco.balances), list(host.eco.ledger))
    assert _go(host, monkeypatch, win=True, msg_id="flip-1") is None
    assert (dict(host.eco.balances), list(host.eco.ledger)) == snap


def test_a_redelivery_inside_the_cooldown_is_silent_and_does_not_rearm_it(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _go(host, monkeypatch, win=True, msg_id="dup-2")
    assert _go(host, monkeypatch, win=True, msg_id="dup-2") is None
    assert len([c for c in host.kv.calls if c[0] == "increment" and ".defuse.cd." in c[1][0]]) == 1
    assert _wagers(host) == 1


def test_distinct_messages_each_play(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(5):
        host.expire(".defuse.cd.")
        assert _go(host, monkeypatch, win=False, msg_id=f"d-{i}") is not None
    assert _wagers(host) == 5 and _stat(host, "plays") == 5


def test_redelivery_is_recognised_after_other_plays_in_between(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _go(host, monkeypatch, win=False, msg_id="old-1")
    for i in range(10):
        host.expire(".defuse.cd.")
        _go(host, monkeypatch, win=False, msg_id=f"between-{i}")
    snap = (dict(host.eco.balances), _wagers(host))
    host.expire(".defuse.cd.")
    assert _go(host, monkeypatch, win=False, msg_id="old-1") is None
    assert (dict(host.eco.balances), _wagers(host)) == snap


@pytest.mark.parametrize("copies", [2, 6])
def test_concurrent_duplicate_deliveries_play_exactly_once(
    host: _Host, monkeypatch: pytest.MonkeyPatch, copies: int
) -> None:
    _yielding_kv_all(monkeypatch)
    monkeypatch.setattr(app, "_roll", lambda percent: True)

    async def race() -> None:
        await asyncio.gather(
            *(
                dispatch(_envelope("play", arg="5", message_id="race-1"), {}, http_client=None)
                for _ in range(copies)
            )
        )

    _run(race())
    assert _wagers(host) == 1 and _stat(host, "plays") == 1 and _stat(host, "wins") == 1


def test_the_claim_is_recorded_before_the_mutation(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At-most-once: an ambiguous host failure is never retried into a second play."""
    import wit_world

    def down(user: str, stake: int, payout: int) -> int:
        raise RuntimeError("storage down")  # classified as BackendError (may have committed)

    real = wit_world.imports.economy.wager
    wit_world.imports.economy.wager = down
    assert "temporarily unavailable" in (_go(host, monkeypatch, win=True, msg_id="crash-1") or "")
    wit_world.imports.economy.wager = real
    host.expire(".defuse.cd.")
    assert _go(host, monkeypatch, win=True, msg_id="crash-1") is None
    assert host.eco.ledger == [] and _stat(host, "plays") == 0


@pytest.mark.parametrize("missing", [None, "", "  ", 12345])
def test_a_play_without_a_message_id_fails_loud_and_moves_nothing(
    host: _Host, monkeypatch: pytest.MonkeyPatch, missing: Any
) -> None:
    monkeypatch.setattr(app, "_roll", lambda percent: True)
    env = _envelope("play", arg="5")
    if missing is None:
        env.event.payload.pop("message_id")
    else:
        env.event.payload["message_id"] = missing
    with pytest.raises(RuntimeError, match="message_id"):
        _run(dispatch(env, {}, http_client=None))
    assert "unavailable" in _reply(host)
    assert host.eco.ledger == [] and not host.kv.store
    assert any("defuse.missing_event_id" in m for _l, m, _f in host.log_calls)


def test_list_usage_and_config_need_no_message_id(host: _Host) -> None:
    cases = (
        ("list", {}),
        ("usage", {}),
        ("config_set_odds", {"arg": "odds 50", "is_mod": True}),
    )
    for command, extra in cases:
        env = _envelope(command, **extra)
        env.event.payload.pop("message_id")
        _run(dispatch(env, {}, http_client=None))
    assert len(host.relay_calls) == 3


def _markers(host: _Host) -> list[str]:
    return [k for k in host.kv.store if ".defuse.seen." in k]


def test_dedupe_state_is_one_pii_free_marker_per_event(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(6):
        host.expire(".defuse.cd.")
        _go(host, monkeypatch, win=False, msg_id=f"private-platform-id-{i}")
    assert len(_markers(host)) == 6, "exactly one marker per processed event"
    for key in _markers(host):
        assert re.fullmatch(rf"c\.{COMMUNITY}\.defuse\.seen\.[0-9a-f]{{32}}", key)
    blob = " ".join(host.kv.store) + " ".join(v.decode() for v in host.kv.store.values())
    assert "private-platform-id" not in blob
    assert "private-platform-id" not in " ".join(f"{m} {f}" for _l, m, f in host.log_calls)


@pytest.mark.parametrize("bad", [b"garbage", b"-5"])
def test_a_corrupt_event_marker_never_lets_the_event_play_again(
    host: _Host, monkeypatch: pytest.MonkeyPatch, bad: bytes
) -> None:
    """The peek sees an existing marker and skips; if the peek were raced past, `claim` raises."""
    _go(host, monkeypatch, win=False, msg_id="corrupt-1")
    (key,) = _markers(host)
    host.kv.store[key] = bad
    host.expire(".defuse.cd.")
    ledger, replies = list(host.eco.ledger), len(host.relay_calls)
    assert _go(host, monkeypatch, win=False, msg_id="corrupt-1") is None
    assert host.eco.ledger == ledger and len(host.relay_calls) == replies

    async def peek_misses(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(app.event_dedupe, "already_seen", peek_misses)
    host.expire(".defuse.cd.")
    with pytest.raises(RuntimeError, match="backend failed"):
        _go(host, monkeypatch, win=False, msg_id="corrupt-1")
    assert host.eco.ledger == ledger


def test_a_redelivery_older_than_any_ring_horizon_is_still_skipped(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """regression: the last-16-ids ring forgot an event after 16 later plays (a replayed wager)."""
    _go(host, monkeypatch, win=False, msg_id="oldest")
    for i in range(40):
        host.expire(".defuse.cd.")
        _go(host, monkeypatch, win=False, msg_id=f"later-{i}")
    snap = (dict(host.eco.balances), _wagers(host), _stat(host, "plays"))
    for win in (False, True):  # even a redelivery that would now roll the other way
        host.expire(".defuse.cd.")
        assert _go(host, monkeypatch, win=win, msg_id="oldest") is None
    assert (dict(host.eco.balances), _wagers(host), _stat(host, "plays")) == snap


@pytest.mark.parametrize("copies", [2, 6])
def test_concurrent_duplicates_play_once_even_with_no_cooldown_gate(
    host: _Host, monkeypatch: pytest.MonkeyPatch, copies: int
) -> None:
    """regression: the ring was a get-then-set (TOCTOU) only safe behind the cooldown CAS.

    With the cooldown gate out of the picture only the atomic per-event `increment` can let
    exactly one copy through.
    """
    _yielding_kv_all(monkeypatch)
    monkeypatch.setattr(app, "_roll", lambda percent: True)
    real_inc = app.community_kv.increment

    async def inc(c: str | None, k: str, d: int, ttl: int = 0) -> int:
        if ".defuse.cd." in k or "defuse.cd." in k:
            await asyncio.sleep(0)
            return 1  # the cooldown always lets the caller through
        return await real_inc(c, k, d, ttl)

    monkeypatch.setattr(app.community_kv, "increment", inc)

    async def race() -> None:
        await asyncio.gather(
            *(
                dispatch(_envelope("play", arg="5", message_id="race-2"), {}, http_client=None)
                for _ in range(copies)
            )
        )

    _run(race())
    assert _wagers(host) == 1 and _stat(host, "plays") == 1 and len(_markers(host)) == 1


def test_the_claim_is_one_ttl_increment_on_a_per_event_marker(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _go(host, monkeypatch, win=False, msg_id="m-ttl")
    marker_calls = [c for c in host.kv.calls if ".defuse.seen." in c[1][0]]
    assert [c[0] for c in marker_calls] == ["get", "increment"]  # advisory peek, then the claim
    assert marker_calls[1][1][1:] == (1, event_dedupe.SEEN_TTL_SECONDS)


def test_an_actor_on_cooldown_never_creates_a_marker(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One chatter's marker rate is bounded by its cooldown: refused attempts mint no keys."""
    _go(host, monkeypatch, win=False)
    assert len(_markers(host)) == 1
    for _ in range(20):
        assert "easy" in (_go(host, monkeypatch, win=False) or "")
    assert len(_markers(host)) == 1


@pytest.mark.parametrize("age", [event_dedupe.MAX_EVENT_AGE_SECONDS + 1, 3600, 86_400 * 7])
def test_an_event_too_old_to_dedupe_is_dropped_silently_and_moves_nothing(
    host: _Host, monkeypatch: pytest.MonkeyPatch, age: int
) -> None:
    """The marker TTL is the horizon, so an event past the age fence must never be processed."""
    monkeypatch.setattr(app, "_roll", lambda percent: True)
    env = _envelope("play", arg="5")
    env.event.occurred_at = _now_stamp(-age)
    _run(dispatch(env, {}, http_client=None))
    assert host.relay_calls == [] and not host.kv.store and host.eco.ledger == []
    assert any("defuse.stale_event_skipped" in m for _l, m, _f in host.log_calls)


def test_an_event_at_the_age_limit_is_still_processed(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app, "_roll", lambda percent: True)
    env = _envelope("play", arg="5")
    env.event.occurred_at = _now_stamp(-event_dedupe.MAX_EVENT_AGE_SECONDS)
    _run(dispatch(env, {}, http_client=None))
    assert "DEFUSED" in _reply(host)


def test_a_replay_after_the_marker_lapsed_is_dropped_by_the_fence(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outage > marker TTL: the marker is gone, but the event is too old to be re-processed."""
    monkeypatch.setattr(app, "_roll", lambda percent: True)
    first = _envelope("play", arg="5", message_id="late-1")
    _run(dispatch(first, {}, http_client=None))
    host.expire(".defuse.seen.")
    host.expire(".defuse.cd.")
    host.now_ms += (event_dedupe.SEEN_TTL_SECONDS + 1) * 1000
    snap = (dict(host.eco.balances), list(host.eco.ledger), len(host.relay_calls))
    _run(dispatch(first, {}, http_client=None))  # same envelope, same ingest stamp, much later
    assert (dict(host.eco.balances), list(host.eco.ledger), len(host.relay_calls)) == snap


@pytest.mark.parametrize("stamp", ["", "   ", "yesterday", "2026-10-05T00:00:00", None, 12345])
def test_a_missing_or_malformed_occurred_at_fails_loud_and_moves_nothing(
    host: _Host, stamp: Any
) -> None:
    env = _envelope("play", arg="5")
    env.event.occurred_at = stamp
    with pytest.raises(RuntimeError, match="occurred_at"):
        _run(dispatch(env, {}, http_client=None))
    assert "unavailable" in _reply(host)
    assert host.eco.ledger == [] and not host.kv.store


def test_a_future_dated_event_fails_loud_and_moves_nothing(host: _Host) -> None:
    env = _envelope("play", arg="5")
    env.event.occurred_at = _now_stamp(event_dedupe.EVENT_CLOCK_SKEW_SECONDS + 60)
    with pytest.raises(RuntimeError, match="occurred_at"):
        _run(dispatch(env, {}, http_client=None))
    assert host.eco.ledger == [] and not host.kv.store


def test_a_full_key_quota_fails_closed_and_moves_no_money(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host refuses a NEW key once 10,000 are live: the claim raises, so nothing settles."""
    import wit_world

    real = wit_world.imports.kv.increment

    def quota_full(key: str, delta: int, ttl: int) -> int:
        if ".defuse.seen." in key:
            raise RuntimeError("quota exceeded")
        return real(key, delta, ttl)

    wit_world.imports.kv.increment = quota_full
    with pytest.raises(RuntimeError, match="backend failed"):
        _go(host, monkeypatch, win=True, msg_id="q-1")
    assert "unavailable" in _reply(host)
    assert host.eco.ledger == [] and _stat(host, "plays") == 0
