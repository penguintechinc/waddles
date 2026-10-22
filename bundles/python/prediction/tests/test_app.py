"""Host-native tests for the prediction bundle: money integrity first, then behaviour."""

from __future__ import annotations

import asyncio
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from harness import (
    COMM,
    NS,
    Host,
    envelope,
    make_event,
    run,
    say,
    to_envelope,
    yielding_kv,
)
from waddle_sdk import event_dedupe
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent

import app
from app import MAX_STAKE_PER_ROUND, OPEN_SECONDS, SETTLE_SECONDS, dispatch, transform

ZWSP = "​"
OPEN = '!prediction open "Will it rain?" yes no'


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> Host:
    return Host(monkeypatch)


def open_round(host: Host, text: str = OPEN) -> str | None:
    """A moderator opens a prediction."""
    return say(host, "mod", text, mod=True)


def settle(host: Host) -> None:
    """Close betting and wait out the resolve fence."""
    say(host, "mod", "!prediction lock", mod=True)
    host.advance(SETTLE_SECONDS + 1)


def resolve(host: Host, outcome: str = "yes") -> str | None:
    """Settle, then resolve (the normal moderator flow)."""
    settle(host)
    return say(host, "mod", f"!prediction resolve {outcome}", mod=True)


def standard_round(host: Host) -> None:
    """Alice 100 + bob 301 on yes, carol 200 on no; resolved yes. pool 601, winning pool 401."""
    open_round(host)
    say(host, "alice", "!predict yes 100")
    say(host, "bob", "!predict yes 301")
    say(host, "carol", "!predict no 200")
    assert "resolved: yes" in (resolve(host) or "")


def fin(host: Host, n: int = 1) -> dict[str, int]:
    """The stored frozen result of round `n`."""
    raw = json.loads(host.kv.store[host.key("fin", n)])
    return {"w": raw["w"], "p": raw["p"], "wp": raw["wp"]}


# --- routing / transform ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "command", "args"),
    [
        ("!prediction", "status", None),
        ("!PREDICTION list", "status", None),
        ("!predict", "status", None),
        ('!prediction open "Q?" a b', "open", '"Q?" a b'),
        ('!prediction add "Q?" a b', "open", '"Q?" a b'),
        ("!prediction lock", "lock", None),
        ("!prediction resolve yes", "resolve", "yes"),
        ("!prediction set yes", "resolve", "yes"),
        ("!prediction cancel", "cancel", None),
        ("!prediction remove", "cancel", None),
        ("!prediction claim", "claim", None),
        ("!prediction claim 3", "claim", "3"),
        ("!predict claim", "claim", None),
        ("!predict yes 100", "bet", "yes 100"),
        ('!predict "big win" 5', "bet", '"big win" 5'),
        ("!prediction bogus", "usage", None),
        ("!prediction enable x", "usage", None),
    ],
)
def test_transform_routes(host: Host, text: str, command: str, args: str | None) -> None:
    out = run(transform(PlatformEvent("twitch", "chat.message", "v", {"text": text}, "t")))
    assert out is not None and out.payload["command"] == command
    assert out.payload.get("args") == args


@pytest.mark.parametrize("payload", [{"text": "hello"}, {"text": 5}, {}, {"text": "!predictions"}])
def test_transform_ignores_non_matches(host: Host, payload: dict[str, Any]) -> None:
    assert run(transform(PlatformEvent("twitch", "chat.message", "v", payload, "t"))) is None


def test_usage_reply_never_echoes_the_unknown_token(host: Host) -> None:
    reply = say(host, "alice", "!prediction @everyone")
    assert reply == app._USAGE and "everyone" not in reply


def test_transform_forwards_message_id_and_strict_badges(host: Host) -> None:
    ev = PlatformEvent(
        "twitch",
        "chat.message",
        "v",
        {
            "text": "!prediction lock",
            "message_id": "abc",
            "is_mod": "false",
            "is_broadcaster": True,
        },
        "t",
    )
    out = run(transform(ev))
    assert out is not None
    assert out.payload["message_id"] == "abc"
    assert out.payload["is_mod"] is False and out.payload["is_broadcaster"] is True
    bare = run(
        transform(PlatformEvent("twitch", "chat.message", "v", {"text": "!prediction"}, "t"))
    )
    assert bare is not None and "is_mod" not in bare.payload and "message_id" not in bare.payload


@pytest.mark.parametrize("bad", [None, 12345, ["x"]])
def test_transform_drops_a_non_string_message_id(host: Host, bad: Any) -> None:
    ev = PlatformEvent(
        "twitch", "chat.message", "v", {"text": "!predict yes 1", "message_id": bad}, "t"
    )
    out = run(transform(ev))
    assert out is not None and "message_id" not in out.payload


# --- opening ----------------------------------------------------------------------------------


def test_mod_opens_and_everyone_sees_status(host: Host) -> None:
    reply = open_round(host)
    assert reply is not None and "Prediction #1 is open" in reply and "1) yes  2) no" in reply
    assert host.counter("round") == 1
    status = say(host, "alice", "!prediction")
    assert status is not None and '"Will it rain?"' in status and "1) yes 0 pts (0%)" in status


@pytest.mark.parametrize(
    "badge",
    [{}, {"is_mod": False}, {"is_mod": "true"}, {"is_mod": 1}, {"is_broadcaster": "yes"}],
)
def test_only_a_real_boolean_badge_may_open(host: Host, badge: dict[str, Any]) -> None:
    reply = say(host, "alice", OPEN, extra=badge)
    assert reply is not None and "only moderators" in reply
    assert host.counter("round") == 0 and not host.keys(".def.")


def test_broadcaster_badge_may_open(host: Host) -> None:
    reply = say(host, "alice", OPEN, extra={"is_broadcaster": True})
    assert reply is not None and "is open" in reply


@pytest.mark.parametrize(
    "text",
    [
        '!prediction open "Q?" onlyone',
        '!prediction open "Q?"',
        "!prediction open",
        '!prediction open "Q?" a a',
        '!prediction open "Q?" a A',
        '!prediction open "Q?" a b c d e f',
        '!prediction open "Q?" 1 2',
        f'!prediction open "{"x" * 101}" a b',
        f'!prediction open "Q?" {"y" * 25} b',
        '!prediction open "Q?" "a\x07b" c',
    ],
)
def test_invalid_open_is_refused_with_no_state(host: Host, text: str) -> None:
    reply = open_round(host, text)
    assert reply is not None and (
        "Usage" in reply or "must" in reply or "at most" in reply or "cannot" in reply
    )
    assert host.counter("round") == 0 and not host.keys(".def.")


def test_open_while_running_is_refused_then_allowed_after_resolve(host: Host) -> None:
    open_round(host)
    again = open_round(host, '!prediction open "Second?" a b')
    assert again is not None and "still running" in again
    assert host.counter("round") == 1
    resolve(host)
    reply = open_round(host, '!prediction open "Second?" a b')
    assert reply is not None and "Prediction #2 is open" in reply and host.counter("round") == 2


def test_concurrent_opens_create_exactly_one_round(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    yielding_kv(monkeypatch)

    envs = [envelope(host, "mod", f'!prediction open "Q{i}?" a b', mod=True) for i in range(4)]

    async def go() -> None:
        await asyncio.gather(*(dispatch(e, {}, http_client=None) for e in envs))

    run(go())
    assert host.counter("round") == 1
    assert len(host.keys(".prediction.def.")) == 1
    assert sum("is open" in m for m in host.relayed) == 1


def test_a_stale_opening_marker_heals_after_its_ttl(host: Host) -> None:
    """A crash between the election and the publish must not wedge the bundle forever."""
    host.kv.store[host.key("opening", 1)] = b"1"
    assert "another prediction is being opened" in (open_round(host) or "")
    host.expire(".opening.")  # the 60 s marker lapses
    assert "is open" in (open_round(host) or "")


def test_the_opening_election_has_a_short_ttl(host: Host) -> None:
    open_round(host)
    ttls = [c[1][2] for c in host.kv.calls if c[0] == "increment" and ".opening." in c[1][0]]
    assert ttls == [60]


# --- betting ----------------------------------------------------------------------------------


def test_bet_debits_and_records_pool_and_stake(host: Host) -> None:
    open_round(host)
    reply = say(host, "alice", "!predict yes 100")
    assert reply == "\U0001f3af {user:tok-alice} staked 100 points on yes (total 100)."
    assert host.bal("alice") == 900
    assert host.counter("pool", 1, 0) == 100 and host.counter("pool", 1, 1) == 0
    pseud = app._pseudonym(COMM, host.users["alice"])
    assert host.counter("stk", 1, 0, pseud) == 100 and host.counter("ent", 1) == 1
    assert "1) yes 100 pts (100%)" in (say(host, "bob", "!prediction") or "")


def test_outcome_by_number_and_case_insensitive(host: Host) -> None:
    open_round(host, '!prediction open "Winner?" "Team A" "Team B"')
    assert "on Team B" in (say(host, "alice", "!predict 2 10") or "")
    assert "on Team A" in (say(host, "bob", '!predict "team a" 10') or "")
    assert host.counter("pool", 1, 0) == 10 and host.counter("pool", 1, 1) == 10


def test_topping_up_the_same_outcome_accumulates(host: Host) -> None:
    open_round(host)
    say(host, "alice", "!predict yes 100")
    assert "(total 250)" in (say(host, "alice", "!predict yes 150") or "")
    assert host.counter("ent", 1) == 1, "a top-up is not a new entrant"
    assert host.bal("alice") == 750


def test_no_prediction_open(host: Host) -> None:
    assert "no prediction open" in (say(host, "alice", "!predict yes 10") or "")
    assert host.bal("alice") == 1000 and not host.kv.store


def test_betting_closed_by_lock_by_clock_and_after_finish(host: Host) -> None:
    open_round(host)
    say(host, "mod", "!prediction lock", mod=True)
    assert "betting on prediction #1 is closed" in (say(host, "alice", "!predict yes 10") or "")
    host.expire(".lock.")
    host.advance(OPEN_SECONDS + 1)
    assert "is closed" in (say(host, "alice", "!predict yes 10") or "")
    host.advance(SETTLE_SECONDS)
    say(host, "mod", "!prediction resolve yes", mod=True)
    assert "already finished" in (say(host, "alice", "!predict yes 10") or "")
    assert host.bal("alice") == 1000 and host.wagers() == 0


def test_invalid_outcome_lists_the_choices_and_moves_nothing(host: Host) -> None:
    open_round(host)
    reply = say(host, "alice", "!predict maybe 10")
    assert reply is not None and "pick one of: 1) yes  2) no" in reply
    assert say(host, "alice", "!predict 3 10") is not None
    assert host.bal("alice") == 1000 and host.wagers() == 0 and not host.keys(".pool.")


@pytest.mark.parametrize("amount", ["0", "-5", "1.5", "abc", "10x"])
def test_bad_amounts_are_usage_and_move_nothing(host: Host, amount: str) -> None:
    open_round(host)
    reply = say(host, "alice", f"!predict yes {amount}")
    assert reply is not None and ("Usage" in reply or "at least" in reply)
    assert host.bal("alice") == 1000 and host.wagers() == 0


def test_stake_above_the_per_round_cap_is_refused(host: Host) -> None:
    open_round(host)
    host.eco.balances[host.users["alice"]] = 10_000
    host.eco.max_bet_cap = 5_000
    assert f"most you can stake is {MAX_STAKE_PER_ROUND}" in (
        say(host, "alice", f"!predict yes {MAX_STAKE_PER_ROUND + 1}") or ""
    )
    say(host, "alice", f"!predict yes {MAX_STAKE_PER_ROUND - 100}")
    reply = say(host, "alice", "!predict yes 101")
    assert reply is not None and "at most" in reply
    assert host.bal("alice") == 10_000 - (MAX_STAKE_PER_ROUND - 100)


def test_insufficient_funds_moves_nothing_and_does_not_take_an_entrant_slot(host: Host) -> None:
    open_round(host)
    host.eco.balances[host.users["alice"]] = 50
    assert "you only have 50" in (say(host, "alice", "!predict yes 100") or "")
    assert host.counter("ent", 1) == 0 and host.bal("alice") == 50 and not host.keys(".pool.")


def test_over_max_bet_replies_the_cap(host: Host) -> None:
    open_round(host)
    host.eco.balances[host.users["alice"]] = 5000
    host.eco.max_bet_cap = 100
    assert "max stake right now is 100" in (say(host, "alice", "!predict yes 500") or "")
    assert host.bal("alice") == 5000


def test_funds_that_vanish_after_the_precheck_roll_the_entrant_slot_back(host: Host) -> None:
    open_round(host)
    real = host.eco.wager
    from waddle_sdk.testing import _Error_InsufficientFunds, _FakeWitError  # noqa: PLC2701

    def broke(user: str, stake: int, payout: int) -> int:
        raise _FakeWitError(_Error_InsufficientFunds(3))

    sys.modules["wit_world"].imports.economy.wager = broke  # type: ignore[attr-defined]
    assert "you only have 3" in (say(host, "alice", "!predict yes 10") or "")
    assert host.counter("ent", 1) == 0 and not host.keys(".pool.") and not host.keys(".stk.")
    sys.modules["wit_world"].imports.economy.wager = real  # type: ignore[attr-defined]
    assert "staked 10" in (say(host, "alice", "!predict yes 10") or "")


def test_a_bettor_backs_only_one_outcome(host: Host) -> None:
    open_round(host)
    say(host, "alice", "!predict yes 50")
    reply = say(host, "alice", "!predict no 50")
    assert reply is not None and "already backed a different outcome" in reply
    assert host.bal("alice") == 950 and host.counter("pool", 1, 1) == 0


def test_entrant_cap_refuses_and_rolls_back(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "MAX_ENTRANTS", 2)
    open_round(host)
    say(host, "alice", "!predict yes 10")
    say(host, "bob", "!predict no 10")
    assert "is full" in (say(host, "carol", "!predict yes 10") or "")
    assert host.counter("ent", 1) == 2 and host.bal("carol") == 1000
    assert "(total 20)" in (say(host, "alice", "!predict yes 10") or ""), (
        "existing entrants may top up"
    )


def test_cooldown_blocks_a_rapid_second_bet_but_precheck_failures_do_not_burn_it(
    host: Host,
) -> None:
    open_round(host)
    say(host, "alice", "!predict maybe 10")  # refused in the precheck
    assert not host.keys(f".{NS}.cd."), "a refused precheck must not burn the cooldown"
    say(host, "alice", "!predict yes 10", cooldown=True)
    assert "slow down" in (say(host, "alice", "!predict yes 10", cooldown=True) or "")
    assert host.bal("alice") == 990
    cd = [c for c in host.kv.calls if c[0] == "increment" and f".{NS}.cd." in c[1][0]]
    assert cd and all(c[1][2] == app.COOLDOWN_SECONDS for c in cd)


def test_unlinked_actor_is_told_to_link(host: Host) -> None:
    open_round(host)
    host.act_as(None)  # no resolved community identity
    env = envelope(host, "alice", "!predict yes 10")
    run(dispatch(env, {}, http_client=None))
    assert "link your account" in host.relayed[-1] and host.wagers() == 0
    host.act_as(None)
    env = envelope(host, "alice", "!predict claim")
    run(dispatch(env, {}, http_client=None))
    assert "link your account" in host.relayed[-1]


def test_actor_mismatch_fails_loud_and_moves_nothing(host: Host) -> None:
    open_round(host)
    host.act_as("alice")
    env = envelope(host, "alice", "!predict yes 10")
    host.eco.actor = host.users["bob"]  # anomalous: host-resolved payer != invoking account
    with pytest.raises(RuntimeError, match="prediction wager failed"):
        run(dispatch(env, {}, http_client=None))
    assert "temporarily unavailable" in host.relayed[-1]
    assert host.bal("alice") == 1000 and host.counter("ent", 1) == 0 and not host.keys(".pool.")


def test_a_bet_that_races_the_lock_is_refused_by_the_authoritative_reread(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The precheck passed, then the round closed: the post-claim re-read must stop the stake."""
    open_round(host)

    async def precheck_passes(self: Any, *_a: Any) -> None:
        host.kv.store[host.key("lock", 1)] = str(host.now_ms).encode()  # lock lands mid-bet
        return None

    monkeypatch.setattr(app._Game, "_bet_prechecks", precheck_passes)
    reply = say(host, "alice", "!predict yes 50")
    assert reply is not None and "is closed" in reply
    assert host.bal("alice") == 1000 and host.wagers() == 0 and not host.keys(".pool.")


def test_a_crash_between_pool_and_stake_never_inflates_a_share(host: Host) -> None:
    """Pool is written first: stranded stake under-pays, an inflated share is impossible."""
    open_round(host)
    say(host, "bob", "!predict yes 100")
    real = host.kv.increment

    def inc(key: str, delta: int, ttl: int) -> int:
        if f".{NS}.stk." in key:
            raise RuntimeError("down")
        return real(key, delta, ttl)

    sys.modules["wit_world"].imports.kv.increment = inc  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="prediction increment failed"):
        say(host, "alice", "!predict yes 100")
    sys.modules["wit_world"].imports.kv.increment = real  # type: ignore[attr-defined]
    say(host, "carol", "!predict no 100")
    resolve(host)
    total_before = host.total()
    claims = [say(host, n, "!predict claim") for n in ("alice", "bob", "carol")]
    assert "nothing to collect" in (claims[0] or "") and "nothing to collect" in (claims[2] or "")
    paid = host.total() - total_before
    assert paid == 150 <= fin(host)["p"] == 300, "winners can never be paid more than the pool"


# --- lock / resolve / fence / double-resolve ---------------------------------------------------


def test_lock_is_idempotent_and_mod_only(host: Host) -> None:
    open_round(host)
    assert "only moderators" in (say(host, "alice", "!prediction lock") or "")
    assert "is closed" in (say(host, "mod", "!prediction lock", mod=True) or "")
    assert "already closed" in (say(host, "mod", "!prediction lock", mod=True) or "")


def test_lock_without_or_after_a_prediction(host: Host) -> None:
    assert "no prediction running" in (say(host, "mod", "!prediction lock", mod=True) or "")
    open_round(host)
    resolve(host)
    assert "already finished" in (say(host, "mod", "!prediction lock", mod=True) or "")


def test_resolve_pays_the_pool_proportionally_and_conserves_points(host: Host) -> None:
    initial = host.total()
    standard_round(host)
    assert fin(host) == {"w": 0, "p": 601, "wp": 401}
    alice, bob, carol = (say(host, n, "!predict claim") or "" for n in ("alice", "bob", "carol"))
    assert "collected 149 points" in alice and "collected 451 points" in bob
    assert "nothing to collect" in carol
    assert (host.bal("alice"), host.bal("bob"), host.bal("carol")) == (1049, 1150, 800)
    assert host.total() == initial - 1, "floor division leaves 1 point of dust; never a mint"


def test_resolve_while_betting_is_open_closes_it_and_asks_to_repeat(host: Host) -> None:
    open_round(host)
    say(host, "alice", "!predict yes 10")
    reply = say(host, "mod", "!prediction resolve yes", mod=True)
    assert reply is not None and "Betting is now closed" in reply
    assert host.key("fin", 1) not in host.kv.store
    wait = say(host, "mod", "!prediction resolve yes", mod=True)
    assert wait is not None and "wait" in wait and host.key("fin", 1) not in host.kv.store
    host.advance(SETTLE_SECONDS + 1)
    assert "resolved: yes" in (say(host, "mod", "!prediction resolve yes", mod=True) or "")


def test_resolve_after_the_auto_close_needs_only_the_settle_window(host: Host) -> None:
    open_round(host)
    say(host, "alice", "!predict yes 10")
    host.advance(OPEN_SECONDS + 1)
    assert "wait" in (say(host, "mod", "!prediction resolve yes", mod=True) or "")
    host.advance(SETTLE_SECONDS)
    assert "resolved: yes" in (say(host, "mod", "!prediction resolve yes", mod=True) or "")


def test_resolve_with_a_bad_outcome_lists_the_choices_and_changes_nothing(host: Host) -> None:
    open_round(host)
    settle(host)
    reply = say(host, "mod", "!prediction resolve maybe", mod=True)
    assert reply is not None and "pick the winning outcome" in reply
    assert host.key("fin", 1) not in host.kv.store
    assert "resolved: no" in (say(host, "mod", "!prediction resolve 2", mod=True) or "")


def test_resolve_needs_a_mod_and_a_running_prediction(host: Host) -> None:
    assert "no prediction running" in (say(host, "mod", "!prediction resolve yes", mod=True) or "")
    open_round(host)
    settle(host)
    assert "only moderators" in (say(host, "alice", "!prediction resolve yes") or "")
    assert host.key("fin", 1) not in host.kv.store


def test_double_resolve_is_idempotent_and_changes_nothing(host: Host) -> None:
    standard_round(host)
    writes = [c for c in host.kv.calls if c[0] == "set" and ".fin." in c[1][0]]
    snapshot = dict(host.kv.store)
    again = say(host, "mod", "!prediction resolve yes", mod=True)
    other = say(host, "mod", "!prediction resolve no", mod=True)
    assert again is not None and "already resolved: yes" in again
    assert other is not None and "already resolved: yes" in other, "a later resolve cannot flip it"
    assert [k for k in host.kv.store if k not in snapshot and ".seen." not in k] == []
    assert fin(host) == {"w": 0, "p": 601, "wp": 401} and len(writes) == 1


def test_concurrent_resolves_finalize_exactly_once(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_round(host)
    say(host, "alice", "!predict yes 100")
    say(host, "bob", "!predict no 100")
    settle(host)
    yielding_kv(monkeypatch)

    envs = [
        envelope(host, "mod", f"!prediction resolve {o}", mod=True)
        for o in ("yes", "no", "yes", "no")
    ]

    async def go() -> None:
        await asyncio.gather(*(dispatch(e, {}, http_client=None) for e in envs))

    run(go())
    writes = [c for c in host.kv.calls if c[0] == "set" and ".fin." in c[1][0]]
    assert len(writes) == 1, "two finalizers wrote a result"
    assert sum("resolved:" in m and "already" not in m for m in host.relayed) == 1


def test_resolve_and_cancel_racing_finalize_exactly_once(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_round(host)
    say(host, "alice", "!predict yes 100")
    settle(host)
    envs = [
        envelope(host, "mod", "!prediction resolve yes", mod=True),
        envelope(host, "mod", "!prediction cancel", mod=True),
        envelope(host, "mod", "!prediction cancel", mod=True),
    ]
    yielding_kv(monkeypatch)

    async def go() -> None:
        await asyncio.gather(*(dispatch(e, {}, http_client=None) for e in envs))

    run(go())
    writes = [c for c in host.kv.calls if c[0] == "set" and ".fin." in c[1][0]]
    assert len(writes) == 1, "a result was written more than once"


@pytest.mark.parametrize("badge", ["false", "true", 1, [True]])
def test_dispatch_itself_rejects_a_non_boolean_badge(host: Host, badge: Any) -> None:
    """Defence in depth: `transform` strictifies badges, but `dispatch` must not trust that."""
    open_round(host)
    env = envelope(host, "mod", "!prediction lock", mod=True)
    env.event.payload["is_mod"] = badge
    host.act_as("mod")
    run(dispatch(env, {}, http_client=None))
    assert "only moderators" in host.relayed[-1] and not host.keys(".lock.")


def test_nobody_backed_the_winner_refunds_every_stake(host: Host) -> None:
    initial = host.total()
    open_round(host)
    say(host, "alice", "!predict yes 100")
    say(host, "bob", "!predict yes 50")
    reply = resolve(host, "no")
    assert reply is not None and "nobody backed it" in reply
    assert say(host, "alice", "!predict claim") and say(host, "bob", "!predict claim")
    assert (host.bal("alice"), host.bal("bob")) == (1000, 1000) and host.total() == initial
    assert "nothing to collect" in (say(host, "alice", "!predict claim") or "")


def test_cancel_refunds_everyone_and_is_idempotent(host: Host) -> None:
    initial = host.total()
    open_round(host)
    say(host, "alice", "!predict yes 100")
    say(host, "carol", "!predict no 40")
    assert "only moderators" in (say(host, "alice", "!prediction cancel") or "")
    assert "cancelled" in (say(host, "mod", "!prediction cancel", mod=True) or "")
    assert "already cancelled" in (say(host, "mod", "!prediction cancel", mod=True) or "")
    assert "already cancelled" in (say(host, "mod", "!prediction resolve yes", mod=True) or "")
    assert "already finished" in (say(host, "alice", "!predict yes 5") or "")
    for who in ("alice", "carol", "bob"):
        say(host, who, "!predict claim")
    assert host.total() == initial and host.bal("alice") == 1000 and host.bal("carol") == 1000


def test_cancel_after_resolve_does_not_undo_it(host: Host) -> None:
    standard_round(host)
    assert "already resolved: yes" in (say(host, "mod", "!prediction cancel", mod=True) or "")


def test_cancel_with_nothing_running(host: Host) -> None:
    assert "no prediction running" in (say(host, "mod", "!prediction cancel", mod=True) or "")


def test_status_in_every_phase(host: Host) -> None:
    assert "no prediction yet" in (say(host, "alice", "!prediction") or "")
    open_round(host)
    assert "betting open" in (say(host, "alice", "!prediction") or "")
    say(host, "mod", "!prediction lock", mod=True)
    assert "betting closed, awaiting" in (say(host, "alice", "!prediction") or "")
    host.advance(SETTLE_SECONDS + 1)
    say(host, "mod", "!prediction resolve yes", mod=True)
    assert "resolved: yes" in (say(host, "alice", "!prediction") or "")


# --- claiming ---------------------------------------------------------------------------------


def test_claim_before_resolve_or_for_non_bettors_is_empty(host: Host) -> None:
    assert "nothing to collect" in (say(host, "alice", "!predict claim") or "")
    open_round(host)
    say(host, "alice", "!predict yes 10")
    assert "nothing to collect" in (say(host, "alice", "!predict claim") or "")
    assert host.bal("alice") == 990


def test_double_claim_pays_once(host: Host) -> None:
    standard_round(host)
    assert "collected 149" in (say(host, "alice", "!predict claim") or "")
    assert "nothing to collect" in (say(host, "alice", "!predict claim") or "")
    assert (
        host.bal("alice") == 1049
        and host.counter("paid", 1, app._pseudonym(COMM, host.users["alice"])) == 149
    )


def test_a_big_share_is_collected_over_several_capped_claims(host: Host) -> None:
    host.eco.max_bet_cap = 2  # chunk = 99 x 2 = 198 per claim
    open_round(host)
    say(host, "alice", "!predict yes 2")
    host.eco.max_bet_cap = 1000
    say(host, "bob", "!predict no 598")
    host.eco.max_bet_cap = 2
    resolve(host)  # pool 600, winning pool 2 -> alice due 600
    bal = host.bal("alice")
    replies = [say(host, "alice", "!predict claim") or "" for _ in range(6)]
    assert "collected 198 points (402 more" in replies[0]
    assert "collected 6 points." in replies[3] and "nothing to collect" in replies[4]
    assert host.bal("alice") == bal + 600
    assert all("collected" in r for r in replies[:4])


def test_a_zero_balance_winner_needs_one_point_to_collect(host: Host) -> None:
    open_round(host)
    say(host, "alice", "!predict yes 1000")
    say(host, "bob", "!predict no 100")
    resolve(host)  # alice due 1100, balance 0
    assert host.bal("alice") == 0
    assert "hold at least 1 point" in (say(host, "alice", "!predict claim") or "")
    host.eco.balances[host.users["alice"]] = 1
    got = say(host, "alice", "!predict claim") or ""
    assert "collected 99 points" in got and host.bal("alice") == 100
    while "collected" in (say(host, "alice", "!predict claim") or ""):
        pass
    assert host.bal("alice") == 1101, "the 1 point of float plus the full 1100 share"


def test_claim_names_the_round_or_falls_back_to_the_previous_one(host: Host) -> None:
    standard_round(host)
    open_round(host, '!prediction open "Second?" a b')
    assert "collected 149" in (say(host, "alice", "!predict claim") or ""), "latest-1 is searched"
    assert "collected 451" in (say(host, "bob", "!predict claim 1") or "")
    assert "nothing to collect" in (say(host, "alice", "!predict claim 2") or "")
    assert say(host, "alice", "!predict claim abc") == app._USAGE


def test_a_host_refusal_releases_the_reservation_so_a_retry_works(host: Host) -> None:
    standard_round(host)
    from waddle_sdk.testing import _Error_Denied, _FakeWitError  # noqa: PLC2701

    real = host.eco.wager
    state = {"n": 0}

    def quota(user: str, stake: int, payout: int) -> int:
        state["n"] += 1
        if state["n"] == 1:
            raise _FakeWitError(_Error_Denied("quota_exceeded"))
        return real(user, stake, payout)

    sys.modules["wit_world"].imports.economy.wager = quota  # type: ignore[attr-defined]
    pseud = app._pseudonym(COMM, host.users["alice"])
    assert "daily points limit" in (say(host, "alice", "!predict claim") or "")
    assert host.counter("paid", 1, pseud) == 0 and host.bal("alice") == 900
    assert "collected 149" in (say(host, "alice", "!predict claim") or "")
    assert host.counter("paid", 1, pseud) == 149 and host.bal("alice") == 1049


def test_a_denied_grant_on_claim_is_a_polite_refusal_that_moves_nothing(host: Host) -> None:
    standard_round(host)
    host.eco.granted = False
    assert "can't collect right now" in (say(host, "alice", "!predict claim") or "")
    assert host.counter("paid", 1, app._pseudonym(COMM, host.users["alice"])) == 0


def test_an_unexpected_wager_denial_on_claim_fails_loud_but_releases(host: Host) -> None:
    standard_round(host)
    host.act_as("alice")
    env = envelope(host, "alice", "!predict claim")
    host.eco.actor = host.users["bob"]  # anomalous: payer != invoking account -> actor_mismatch
    with pytest.raises(RuntimeError, match="prediction wager failed"):
        run(dispatch(env, {}, http_client=None))
    assert host.counter("paid", 1, app._pseudonym(COMM, host.users["alice"])) == 0
    assert host.bal("alice") == 900 and "temporarily unavailable" in host.relayed[-1]


def test_ambiguous_backend_error_retries_the_identical_call(host: Host) -> None:
    open_round(host)
    real = host.eco.wager
    calls: list[tuple[str, int, int]] = []

    def flaky(user: str, stake: int, payout: int) -> int:
        calls.append((user, stake, payout))
        if len(calls) == 1:
            raise RuntimeError("storage down")  # classified as BackendError by the facade
        return real(user, stake, payout)

    sys.modules["wit_world"].imports.economy.wager = flaky  # type: ignore[attr-defined]
    assert "staked 10" in (say(host, "alice", "!predict yes 10") or "")
    assert len(calls) == 2 and calls[0] == calls[1] and host.bal("alice") == 990


def test_claim_backend_error_twice_fails_loud_and_keeps_the_reservation(host: Host) -> None:
    standard_round(host)

    def down(user: str, stake: int, payout: int) -> int:
        raise RuntimeError("storage down")

    sys.modules["wit_world"].imports.economy.wager = down  # type: ignore[attr-defined]
    pseud = app._pseudonym(COMM, host.users["alice"])
    with pytest.raises(RuntimeError, match="prediction wager failed"):
        say(host, "alice", "!predict claim")
    assert host.counter("paid", 1, pseud) == 149, "ambiguous commit: never risk a double payout"
    assert "nothing to collect" in (say(host, "alice", "!predict claim") or "")


def test_concurrent_claims_by_one_actor_pay_once(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    host.act_as("alice")
    yielding_kv(monkeypatch)

    envs = [envelope(host, "alice", "!predict claim") for _ in range(5)]

    async def go() -> None:
        await asyncio.gather(*(dispatch(e, {}, http_client=None) for e in envs))

    run(go())
    assert host.bal("alice") == 1049 and host.wagers() == 4  # 3 bets + exactly one claim


def test_the_reservation_cas_stops_two_overlapping_collectors(host: Host) -> None:
    """Defence in depth behind the cooldown: a second reservation past `due` is rolled back."""
    standard_round(host)
    pseud = app._pseudonym(COMM, host.users["alice"])
    host.kv.store[host.key("paid", 1, pseud)] = b"100"  # another collector already took 100
    host.act_as("alice")
    pre = host.counter("paid", 1, pseud)
    got = say(host, "alice", "!predict claim") or ""
    assert "collected 49 points" in got and host.counter("paid", 1, pseud) == pre + 49
    host.kv.store[host.key("paid", 1, pseud)] = b"149"
    assert "nothing to collect" in (say(host, "alice", "!predict claim") or "")


def test_claim_cannot_overshoot_when_the_counter_races_ahead(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    pseud = app._pseudonym(COMM, host.users["alice"])
    host.act_as("alice")
    real = host.kv.increment

    def racing(key: str, delta: int, ttl: int) -> int:
        if key.endswith(f".paid.1.{pseud}") and delta > 0:
            real(key, 120, ttl)  # a concurrent collector reserved first, between read and CAS
        return real(key, delta, ttl)

    sys.modules["wit_world"].imports.kv.increment = racing  # type: ignore[attr-defined]
    reply = say(host, "alice", "!predict claim") or ""
    assert "nothing more to collect" in reply
    assert host.bal("alice") == 900 and host.counter("paid", 1, pseud) == 120


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_randomized_rounds_never_pay_more_than_was_staked(host: Host, seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - deterministic test data, not security
    names = [f"u{i}" for i in range(25)]
    for n in names:
        host.add_user(n, 1000)
    initial = host.total()
    open_round(host, '!prediction open "Q?" a b c')
    staked: dict[str, tuple[int, int]] = {}
    for n in names:
        outcome, amount = rng.randrange(3), rng.randint(1, 400)
        say(host, n, f"!predict {outcome + 1} {amount}")
        staked[n] = (outcome, amount)
    winner = rng.randrange(3)
    resolve(host, str(winner + 1))
    for n in names:
        say(host, n, "!predict claim")
    pool = sum(a for _o, a in staked.values())
    wp = sum(a for o, a in staked.values() if o == winner)
    paid = host.total() - (initial - pool)
    if wp:
        assert paid == sum(a * pool // wp for o, a in staked.values() if o == winner)
        assert pool - len(names) < paid <= pool
    else:
        assert paid == pool
    assert host.total() <= initial


# --- redelivery / replay safety ----------------------------------------------------------------


def _wagers(host: Host) -> int:
    return host.wagers()


def test_redelivered_bet_is_applied_once_and_skipped_silently(host: Host) -> None:
    open_round(host)
    assert "staked 100" in (say(host, "alice", "!predict yes 100", msg_id="dup-1") or "")
    snapshot, sent = (dict(host.kv.store), host.bal("alice"), _wagers(host)), len(host.relayed)
    assert say(host, "alice", "!predict yes 100", msg_id="dup-1") is None
    assert (host.counter("pool", 1, 0), host.bal("alice"), _wagers(host)) == (100, 900, 1)
    assert len(host.relayed) == sent and host.bal("alice") == snapshot[1]


def test_redelivery_inside_the_cooldown_window_changes_nothing(host: Host) -> None:
    open_round(host)
    say(host, "alice", "!predict yes 100", msg_id="dup-2")
    markers = len(host.keys(".seen."))
    assert say(host, "alice", "!predict yes 100", msg_id="dup-2", cooldown=True) is None
    assert host.counter("pool", 1, 0) == 100 and len(host.keys(".seen.")) == markers


def test_redelivery_is_recognised_after_other_bets_in_between(host: Host) -> None:
    open_round(host)
    say(host, "alice", "!predict yes 10", msg_id="dup-3")
    for i in range(20):
        say(host, "bob", "!predict no 1", msg_id=f"other-{i}")
    assert say(host, "alice", "!predict yes 10", msg_id="dup-3") is None
    assert host.counter("pool", 1, 0) == 10 and host.bal("alice") == 990


def test_redelivered_claim_pays_once(host: Host) -> None:
    standard_round(host)
    assert "collected 149" in (say(host, "alice", "!predict claim", msg_id="c-1") or "")
    assert say(host, "alice", "!predict claim", msg_id="c-1") is None
    assert host.bal("alice") == 1049


def test_redelivered_lock_and_resolve_change_nothing(host: Host) -> None:
    open_round(host)
    say(host, "alice", "!predict yes 10")
    assert "is closed" in (say(host, "mod", "!prediction lock", mod=True, msg_id="l-1") or "")
    assert say(host, "mod", "!prediction lock", mod=True, msg_id="l-1") is None
    host.advance(SETTLE_SECONDS + 1)
    assert "resolved" in (say(host, "mod", "!prediction resolve yes", mod=True, msg_id="r-1") or "")
    base = dict(host.kv.store)
    assert say(host, "mod", "!prediction resolve yes", mod=True, msg_id="r-1") is None
    assert host.kv.store == base


def test_redelivered_cancel_changes_nothing(host: Host) -> None:
    open_round(host)
    assert "cancelled" in (say(host, "mod", "!prediction cancel", mod=True, msg_id="x-1") or "")
    base = dict(host.kv.store)
    assert say(host, "mod", "!prediction cancel", mod=True, msg_id="x-1") is None
    assert host.kv.store == base


def test_a_replayed_open_does_not_open_a_second_round_while_the_first_runs(host: Host) -> None:
    assert "is open" in (say(host, "mod", OPEN, mod=True, msg_id="o-1") or "")
    assert say(host, "mod", OPEN, mod=True, msg_id="o-1") is None
    assert host.counter("round") == 1


def test_a_redelivered_open_cannot_start_a_second_round_after_the_first_finished(
    host: Host,
) -> None:
    open_round_msg = "open-replay"
    assert "is open" in (say(host, "mod", OPEN, mod=True, msg_id=open_round_msg) or "")
    resolve(host)
    assert say(host, "mod", OPEN, mod=True, msg_id=open_round_msg) is None
    assert host.counter("round") == 1


def test_the_event_claim_is_recorded_before_the_mutation(host: Host) -> None:
    """At-most-once: a crash mid-bet is never replayed into a second debit or pool credit."""
    open_round(host)

    def down(user: str, stake: int, payout: int) -> int:
        raise RuntimeError("storage down")

    real = sys.modules["wit_world"].imports.economy.wager  # type: ignore[attr-defined]
    sys.modules["wit_world"].imports.economy.wager = down  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="prediction wager failed"):
        say(host, "alice", "!predict yes 10", msg_id="crash-1")
    sys.modules["wit_world"].imports.economy.wager = real  # type: ignore[attr-defined]
    sent = len(host.relayed)
    assert say(host, "alice", "!predict yes 10", msg_id="crash-1") is None
    assert (_wagers(host), host.bal("alice"), host.counter("pool", 1, 0)) == (0, 1000, 0)
    assert len(host.relayed) == sent


@pytest.mark.parametrize("copies", [2, 6])
def test_concurrent_duplicate_deliveries_process_exactly_once(
    host: Host, monkeypatch: pytest.MonkeyPatch, copies: int
) -> None:
    """Two consumers holding the same stream entry at once must not both reach the wager."""
    open_round(host)
    host.act_as("alice")
    yielding_kv(monkeypatch)
    env = envelope(host, "alice", "!predict yes 50", msg_id="race-1")

    async def go() -> None:
        await asyncio.gather(*(dispatch(env, {}, http_client=None) for _ in range(copies)))

    run(go())
    assert (host.counter("pool", 1, 0), host.bal("alice"), _wagers(host)) == (50, 950, 1)


@pytest.mark.parametrize("copies", [2, 6])
def test_concurrent_duplicates_process_once_even_with_no_cooldown_gate(
    host: Host, monkeypatch: pytest.MonkeyPatch, copies: int
) -> None:
    """The event claim alone (not the cooldown that usually fronts it) must dedupe a race."""

    async def no_cooldown(self: Any, pseudonym: str) -> bool:
        return True

    monkeypatch.setattr(app._Store, "cooldown_claim", no_cooldown)
    open_round(host)
    host.act_as("alice")
    yielding_kv(monkeypatch)
    env = envelope(host, "alice", "!predict yes 50", msg_id="race-2")

    async def go() -> None:
        await asyncio.gather(*(dispatch(env, {}, http_client=None) for _ in range(copies)))

    run(go())
    assert (host.counter("pool", 1, 0), host.bal("alice"), _wagers(host)) == (50, 950, 1)
    assert sum("staked 50" in m for m in host.relayed) == 1


@pytest.mark.parametrize("missing", [None, "", "   ", 12345])
def test_a_mutation_without_a_message_id_fails_loud_and_moves_nothing(
    host: Host, missing: Any
) -> None:
    open_round(host)
    before = dict(host.kv.store)
    for text, mod in (
        ("!predict yes 10", False),
        ("!predict claim", False),
        ("!prediction lock", True),
        ("!prediction cancel", True),
    ):
        host.act_as("alice")
        env = envelope(host, "alice", text, mod=mod)
        if missing is None:
            env.event.payload.pop("message_id")
        else:
            env.event.payload["message_id"] = missing
        with pytest.raises(RuntimeError, match="prediction event_id failed: EventIdError"):
            run(dispatch(env, {}, http_client=None))
        assert "temporarily unavailable" in host.relayed[-1]
    assert host.kv.store == before and host.bal("alice") == 1000 and _wagers(host) == 0
    assert any("prediction.error" in m and "EventIdError" in fj for m, fj in host.logs)


def test_status_and_usage_need_no_message_id(host: Host) -> None:
    for text in ("!prediction", "!prediction bogus"):
        env = envelope(host, "alice", text)
        env.event.payload.pop("message_id", None)
        run(dispatch(env, {}, http_client=None))
        assert host.relayed[-1]


def test_dedupe_kv_failure_is_loud(host: Host) -> None:
    open_round(host)
    real = host.kv.get

    def get(key: str) -> bytes | None:
        if ".seen." in key:
            raise RuntimeError("down")
        return real(key)

    sys.modules["wit_world"].imports.kv.get = get  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="prediction dedupe failed"):
        say(host, "alice", "!predict yes 5")
    assert (_wagers(host), host.bal("alice")) == (0, 1000)


def test_a_full_key_quota_fails_closed_and_moves_no_money(host: Host) -> None:
    open_round(host)
    real = host.kv.increment

    def inc(key: str, delta: int, ttl: int) -> int:
        if ".seen." in key:
            raise RuntimeError("quota exceeded")
        return real(key, delta, ttl)

    sys.modules["wit_world"].imports.kv.increment = inc  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="prediction dedupe failed"):
        say(host, "alice", "!predict yes 5", msg_id="q-1")
    assert (_wagers(host), host.bal("alice")) == (0, 1000)
    assert "temporarily unavailable" in host.relayed[-1]


@pytest.mark.parametrize("bad", [b"not-a-number", b"-5"])
def test_a_corrupt_event_marker_never_lets_the_event_settle_again(
    host: Host, monkeypatch: pytest.MonkeyPatch, bad: bytes
) -> None:
    """The peek sees an existing marker and skips; if the peek were raced past, `claim` raises."""
    open_round(host)
    markers_before = set(host.keys(".seen."))
    say(host, "alice", "!predict yes 5", msg_id="corrupt-1")
    (marker,) = set(host.keys(".seen.")) - markers_before
    host.kv.store[marker] = bad
    snapshot = (_wagers(host), host.bal("alice"))
    assert say(host, "alice", "!predict yes 5", msg_id="corrupt-1") is None

    async def peek_misses(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(app.event_dedupe, "already_seen", peek_misses)
    with pytest.raises(RuntimeError, match="prediction dedupe failed"):
        say(host, "alice", "!predict yes 5", msg_id="corrupt-1")
    assert (_wagers(host), host.bal("alice")) == snapshot


def test_dedupe_state_is_one_pii_free_marker_per_event(host: Host) -> None:
    open_round(host)
    base = len(host.keys(".seen."))
    for i in range(6):
        say(host, "alice", "!predict yes 1", msg_id=f"private-platform-id-{i}")
    keys = host.keys(".seen.")
    assert len(keys) == base + 6, "exactly one marker per processed event"
    for key in keys:
        assert re.fullmatch(rf"c\.{COMM}\.{NS}\.seen\.[0-9a-f]{{32}}", key)
    blob = " ".join(host.kv.store) + " ".join(v.decode() for v in host.kv.store.values())
    assert "private-platform-id" not in blob
    assert "private-platform-id" not in " ".join(m + fj for m, fj in host.logs)


@pytest.mark.parametrize("age", [event_dedupe.MAX_EVENT_AGE_SECONDS + 1, 3600])
def test_an_event_too_old_to_dedupe_is_dropped_silently_and_moves_nothing(
    host: Host, age: int
) -> None:
    open_round(host)
    before = dict(host.kv.store)
    env = envelope(host, "alice", "!predict yes 5")
    env.event.occurred_at = host.stamp(-age)
    host.act_as("alice")
    sent = len(host.relayed)
    run(dispatch(env, {}, http_client=None))
    assert len(host.relayed) == sent and host.kv.store == before and _wagers(host) == 0
    assert any("stale_event_skipped" in m for m, _fj in host.logs)


def test_an_event_at_the_age_limit_is_still_processed(host: Host) -> None:
    open_round(host)
    env = envelope(host, "alice", "!predict yes 5")
    env.event.occurred_at = host.stamp(-event_dedupe.MAX_EVENT_AGE_SECONDS)
    host.act_as("alice")
    run(dispatch(env, {}, http_client=None))
    assert "staked 5" in host.relayed[-1]


def test_a_replay_after_the_marker_lapsed_is_dropped_by_the_fence(host: Host) -> None:
    open_round(host)
    host.act_as("alice")
    first = envelope(host, "alice", "!predict yes 5", msg_id="late-1")
    run(dispatch(first, {}, http_client=None))
    host.expire(".seen.")  # the host expired the marker
    host.expire_cooldowns()
    host.advance(event_dedupe.SEEN_TTL_SECONDS + 1)
    snapshot, sent = (_wagers(host), host.bal("alice")), len(host.relayed)
    run(dispatch(first, {}, http_client=None))  # same envelope, same ingest stamp, much later
    assert (_wagers(host), host.bal("alice")) == snapshot and len(host.relayed) == sent


@pytest.mark.parametrize("stamp", ["", "   ", "yesterday", "2026-10-21T00:00:00", None, 12345])
def test_a_missing_or_malformed_occurred_at_fails_loud(host: Host, stamp: Any) -> None:
    open_round(host)
    env = envelope(host, "alice", "!predict yes 5")
    env.event.occurred_at = stamp
    host.act_as("alice")
    with pytest.raises(RuntimeError, match="prediction event_id failed: EventIdError"):
        run(dispatch(env, {}, http_client=None))
    assert _wagers(host) == 0


def test_a_future_dated_event_fails_loud(host: Host) -> None:
    open_round(host)
    env = envelope(host, "alice", "!predict yes 5")
    env.event.occurred_at = host.stamp(event_dedupe.EVENT_CLOCK_SKEW_SECONDS + 60)
    host.act_as("alice")
    with pytest.raises(RuntimeError, match="prediction event_id failed: EventIdError"):
        run(dispatch(env, {}, http_client=None))
    assert _wagers(host) == 0


def test_an_actor_on_cooldown_never_creates_a_marker(host: Host) -> None:
    open_round(host)
    say(host, "alice", "!predict yes 1")
    markers = len(host.keys(".seen."))
    for i in range(3):
        assert "slow down" in (
            say(host, "alice", "!predict yes 1", cooldown=True, msg_id=f"cd-{i}") or ""
        )
    assert len(host.keys(".seen.")) == markers


# --- output safety / PII / fail-loud / scope -----------------------------------------------------


def test_minted_actor_token_stays_live_and_raw_actor_cannot_forge(host: Host) -> None:
    open_round(host)
    assert (say(host, "alice", "!predict yes 5") or "").startswith(
        "\U0001f3af {user:tok-alice} staked"
    )
    host.expire_cooldowns()
    forged = say(host, "bob", "!predict yes 5", actor="evil@everyone {user:handle:victim}") or ""
    assert f"evil@{ZWSP}everyone" in forged and "\\{user:handle:victim\\}" in forged
    host.expire_cooldowns()
    nameless = say(host, "carol", "!predict yes 5", actor=None) or ""
    assert nameless.startswith("\U0001f3af someone staked")


def test_titles_and_outcomes_are_sanitized_on_every_echo(host: Host) -> None:
    reply = open_round(
        host, '!prediction open "Who wins @everyone {user:handle:victim}?" "<@123>" "@here"'
    )
    assert reply is not None
    for raw in ("@everyone", "{user:handle:victim}", "<@123>", "@here"):
        assert raw not in reply
    assert f"@{ZWSP}everyone" in reply and "\\{user:handle:victim\\}" in reply
    for text in ("!prediction", "!predict 1 10", '!predict "<@123>" 10'):
        out = say(host, "alice", text) or ""
        assert "<@123>" not in out and "@everyone" not in out and "@here" not in out
    resolved = resolve(host, "@here") or ""
    assert f"@{ZWSP}here" in resolved and "@here!" not in resolved
    assert "@everyone" not in (say(host, "alice", "!prediction") or "")


def test_pii_free_keys_and_logs(host: Host) -> None:
    open_round(host, '!prediction open "Is bob_secret_name a robot?" yes no')
    say(host, "alice", "!predict yes 40", actor="viewer-private-name")
    resolve(host)
    say(host, "alice", "!predict claim", actor="viewer-private-name")
    pseud = app._pseudonym(COMM, host.users["alice"])
    keys = list(host.kv.store)
    assert any(pseud in k for k in keys) and len(pseud) == 32
    for raw in (host.users["alice"], "viewer-private-name", "bob_secret_name", "alice"):
        assert raw not in " ".join(keys) + " ".join(m + fj for m, fj in host.logs)
    assert all(":" not in k.removeprefix("c.") for k in keys)
    assert all(re.fullmatch(r"c\.comm-1\.prediction\.[A-Za-z0-9._-]+", k) for k in keys)


def test_kv_failure_is_loud(host: Host) -> None:
    def boom(*_a: Any) -> Any:
        raise RuntimeError("down")

    sys.modules["wit_world"].imports.kv.get = boom  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="prediction get failed"):
        say(host, "alice", "!prediction")
    assert "temporarily unavailable" in host.relayed[-1]


@pytest.mark.parametrize("key_infix", ["def", "fin"])
def test_a_corrupt_stored_record_fails_loud(host: Host, key_infix: str) -> None:
    open_round(host)
    resolve(host)
    key = host.key(key_infix, 1)
    host.kv.store[key] = b'{"not": "valid"}'
    with pytest.raises(RuntimeError, match="prediction corrupt failed"):
        say(host, "alice", "!prediction")


def test_a_final_naming_an_outcome_that_does_not_exist_fails_loud(host: Host) -> None:
    open_round(host)
    resolve(host)
    host.kv.store[host.key("fin", 1)] = json.dumps({"w": 9, "p": 10, "wp": 5}).encode()
    with pytest.raises(RuntimeError, match="prediction corrupt failed"):
        say(host, "alice", "!prediction")


def test_a_non_integer_counter_fails_loud(host: Host) -> None:
    open_round(host)
    host.kv.store[host.key("pool", 1, 0)] = b"lots"
    with pytest.raises(RuntimeError, match="prediction corrupt failed"):
        say(host, "alice", "!prediction")


def test_missing_channel_unknown_command_and_empty_community_raise(host: Host) -> None:
    env = envelope(host, "alice", "!prediction")
    env.event.payload.pop("channel_id")
    with pytest.raises(ValueError, match="channel_id"):
        run(dispatch(env, {}, http_client=None))
    env = envelope(host, "alice", "!prediction")
    env.event.payload["command"] = "bogus"
    with pytest.raises(ValueError, match="unrecognized"):
        run(dispatch(env, {}, http_client=None))
    env = envelope(host, "alice", "!prediction")
    env.community = ""  # type: ignore[misc]
    with pytest.raises(ValueError, match="community"):
        run(dispatch(env, {}, http_client=None))


def test_tenant_wide_sentinel_scope_works_end_to_end(host: Host) -> None:
    host.act_as("mod")
    ev = run(transform(make_event(host, "mod", OPEN, mod=True)))
    assert ev is not None
    run(dispatch(to_envelope(ev, community=None), {}, http_client=None))
    assert "is open" in host.relayed[-1]
    assert any(k.startswith("c.0.prediction.") for k in host.kv.store)


def test_runs_on_activation_alone_no_feature_flag(host: Host) -> None:
    """No flags import exists in the host fake at all: activation is the only gate."""
    assert not hasattr(sys.modules["wit_world"].imports, "flags")
    assert "is open" in (open_round(host) or "")
    src = Path(app.__file__).read_text(encoding="utf-8")
    assert (
        "feature_enabled" not in src
        and "FLAG_KEY" not in src
        and "flags" not in src.split('"""')[2]
    )


def test_every_dispatch_stays_inside_the_host_kv_op_budget(host: Host) -> None:
    for n in [f"u{i}" for i in range(12)]:
        host.add_user(n, 1000)
    open_round(host)
    for i in range(12):
        say(host, f"u{i}", f"!predict {1 + i % 2} {10 + i}")
    resolve(host)
    for i in range(12):
        say(host, f"u{i}", "!predict claim")
    say(host, "alice", "!prediction")
    assert host.kv_ops and max(host.kv_ops) <= 64, max(host.kv_ops)


def test_economy_permission_is_declared_with_bounds() -> None:
    import yaml

    manifest = yaml.safe_load((Path(app.__file__).parents[1] / "hub-manifest.yaml").read_text())
    perms = {p["id"]: p for p in manifest["permissions"]}
    assert perms["economy.wager"]["params"] == {"max_bet": 500}
    assert "economy.transfer" not in perms, "least privilege: no transfer is ever issued"
    assert {"storage.kv", "economy.read", "identity.resolve"} <= set(perms)
    assert all(len(p["justification"]) <= 280 for p in manifest["permissions"])
    assert manifest["version"] == "1.0.0" and "notice" not in manifest
