"""Host-native tests for the lottery bundle: money integrity first, then behaviour."""

from __future__ import annotations

import asyncio
import json
import random
import re
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from harness import (
    COMM,
    GUARD,
    NS,
    Host,
    action_dispatch,
    dispatch,
    envelope,
    make_event,
    run,
    say,
    to_envelope,
    transform,
    yielding_kv,
)
from waddle_sdk import event_dedupe
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent

import app
from app import (
    DRAW_INTERVAL_SECONDS,
    MAX_BUY,
    MAX_TICKETS_PER_USER,
    SETTLE_SECONDS,
    TICKET_PRICE,
)

ZWSP = "\u200b"


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    h = Host(monkeypatch)
    yield h
    GUARD.assert_clean()


def pin_draw(mp: pytest.MonkeyPatch, *positions: int) -> None:
    """Force the drawn ticket position(s), in order (then repeat the last)."""
    queue = list(positions)
    mp.setattr(app, "_pick_ticket", lambda total: queue.pop(0) if len(queue) > 1 else queue[0])


def standard_round(host: Host) -> None:
    """Alice 3 tickets (1-3), bob 5 (4-8), carol 2 (9-10): 10 sold, pot 100, 3 players."""
    assert "Lottery #1 is open!" in (say(host, "alice", "!lottery buy 3") or "")
    say(host, "bob", "!lottery buy 5")
    say(host, "carol", "!lottery buy 2")


def make_due(host: Host) -> None:
    """Let the interval elapse and the settle window pass: anyone may draw."""
    host.advance(DRAW_INTERVAL_SECONDS + SETTLE_SECONDS + 1)


def draw_now(host: Host, who: str = "mod", *, mod: bool = True, msg_id: str | None = None) -> str:
    """Draw once the round is due; returns the reply."""
    make_due(host)
    return say(host, who, "!lottery draw", mod=mod, msg_id=msg_id) or ""


def pseud(host: Host, who: str) -> str:
    """The pseudonym key component of a member."""
    return app._pseudonym(COMM, host.users[who])


def win(host: Host, n: int = 1) -> dict[str, Any]:
    """The stored draw result of round `n`."""
    return json.loads(host.kv.store[host.key("win", n)])


def writes_to(host: Host, infix: str) -> list[Any]:
    """Every kv `set` call that touched a key containing `infix`."""
    return [c for c in host.kv.calls if c[0] == "set" and infix in c[1][0]]


# --- routing / transform ----------------------------------------------------------------------


def route(event: PlatformEvent) -> types.SimpleNamespace | None:
    """The routing intent `transform` derives from a chat line (pure, no host call)."""
    intent = app._recognize(event)
    return None if intent is None else types.SimpleNamespace(payload=intent)


@pytest.mark.parametrize(
    ("text", "command", "args"),
    [
        ("!lottery", "status", None),
        ("!LOTTERY list", "status", None),
        ("!lottery buy 3", "buy", "3"),
        ("!lottery add 3", "buy", "3"),
        ("!lottery buy", "buy", None),
        ("!lottery draw", "draw", None),
        ("!lottery claim", "claim", None),
        ("!lottery claim 2", "claim", "2"),
        ("!lottery bogus", "usage", None),
        ("!lottery enable x", "usage", None),
    ],
)
def test_transform_routes(host: Host, text: str, command: str, args: str | None) -> None:
    out = route(PlatformEvent("twitch", "chat.message", "v", {"text": text}, "t"))
    assert out is not None and out.payload["command"] == command
    assert out.payload.get("args") == args


@pytest.mark.parametrize("payload", [{"text": "hello"}, {"text": 5}, {}, {"text": "!lotteries"}])
def test_transform_ignores_non_matches(host: Host, payload: dict[str, Any]) -> None:
    assert run(transform(PlatformEvent("twitch", "chat.message", "v", payload, "t"))) is None


def test_usage_reply_never_echoes_the_unknown_token(host: Host) -> None:
    reply = say(host, "alice", "!lottery @everyone")
    assert reply == app._USAGE and "everyone" not in reply


def test_transform_forwards_message_id_and_strict_badges(host: Host) -> None:
    ev = PlatformEvent(
        "twitch",
        "chat.message",
        "v",
        {"text": "!lottery draw", "message_id": "abc", "is_mod": "false", "is_broadcaster": True},
        "t",
    )
    out = route(ev)
    assert out is not None and out.payload["message_id"] == "abc"
    assert out.payload["is_mod"] is False and out.payload["is_broadcaster"] is True
    bare = route(PlatformEvent("twitch", "chat.message", "v", {"text": "!lottery"}, "t"))
    assert bare is not None and "is_mod" not in bare.payload and "message_id" not in bare.payload


@pytest.mark.parametrize("bad", [None, 12345, ["x"]])
def test_transform_drops_a_non_string_message_id(host: Host, bad: Any) -> None:
    ev = PlatformEvent(
        "twitch", "chat.message", "v", {"text": "!lottery buy 1", "message_id": bad}, "t"
    )
    out = route(ev)
    assert out is not None and "message_id" not in out.payload


# --- buying -----------------------------------------------------------------------------------


def test_first_buy_opens_the_round_and_debits(host: Host) -> None:
    reply = say(host, "alice", "!lottery buy 3")
    assert reply == (
        "\U0001f39f\ufe0f Lottery #1 is open! {user:tok-alice} bought 3 tickets "
        f"(you hold 3/{MAX_TICKETS_PER_USER}). Pot: 30 points."
    )
    assert host.bal("alice") == 1000 - 3 * TICKET_PRICE and host.counter("round") == 1
    p = pseud(host, "alice")
    assert (host.counter("tk", 1, p), host.counter("tix", 1), host.counter("sold", 1)) == (3, 3, 3)
    assert host.counter("buyers", 1) == 1
    assert json.loads(host.kv.store[host.key("ent", 1, 3)]) == {
        "n": 3,
        "p": p,
        "k": "tok-alice",
    }


def test_a_second_purchase_accumulates_without_a_new_buyer(host: Host) -> None:
    say(host, "alice", "!lottery buy 3")
    reply = say(host, "alice", "!lottery buy 2") or ""
    assert "bought 2 tickets" in reply and "5/20" in reply and "Pot: 50" in reply
    assert "is open" not in reply
    assert host.counter("buyers", 1) == 1 and host.counter("tix", 1) == 5
    assert host.kv.store.get(host.key("ent", 1, 5)) is not None
    assert host.bal("alice") == 950


def test_singular_ticket_wording(host: Host) -> None:
    assert "bought 1 ticket (" in (say(host, "alice", "!lottery buy 1") or "")


@pytest.mark.parametrize("arg", ["abc", "-1", "1.5", "3 4", "\u0663"])
def test_malformed_counts_are_usage_and_move_nothing(host: Host, arg: str) -> None:
    assert say(host, "alice", f"!lottery buy {arg}") == app._USAGE
    assert host.bal("alice") == 1000 and not host.kv.store


def test_a_missing_count_is_usage(host: Host) -> None:
    assert say(host, "alice", "!lottery buy") == app._USAGE


@pytest.mark.parametrize("arg", ["0", str(MAX_BUY + 1), "99999"])
def test_out_of_range_counts_are_refused(host: Host, arg: str) -> None:
    assert f"1-{MAX_BUY} tickets" in (say(host, "alice", f"!lottery buy {arg}") or "")
    assert host.bal("alice") == 1000 and not host.kv.store


def test_per_player_cap_is_enforced_across_purchases(host: Host) -> None:
    say(host, "alice", f"!lottery buy {MAX_BUY}")
    say(host, "alice", f"!lottery buy {MAX_BUY}")
    reply = say(host, "alice", "!lottery buy 1") or ""
    assert f"at most {MAX_TICKETS_PER_USER} tickets" in reply and "you have 20" in reply
    assert host.bal("alice") == 1000 - MAX_TICKETS_PER_USER * TICKET_PRICE
    assert host.counter("sold", 1) == 20


def test_round_cap_sold_out(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "MAX_ROUND_TICKETS", 6)
    say(host, "alice", "!lottery buy 4")
    assert "sold out" in (say(host, "bob", "!lottery buy 3") or "")
    assert "bought 2 tickets" in (say(host, "bob", "!lottery buy 2") or "")
    assert "sold out" in (say(host, "carol", "!lottery buy 1") or "")
    assert (host.bal("bob"), host.bal("carol")) == (980, 1000)


def test_insufficient_funds_moves_nothing(host: Host) -> None:
    host.eco.balances[host.users["alice"]] = 25
    assert "you only have 25" in (say(host, "alice", "!lottery buy 3") or "")
    assert host.bal("alice") == 25 and not host.keys(".tk.") and not host.keys(".tix.")


def test_over_max_bet_replies_the_cap(host: Host) -> None:
    host.eco.max_bet_cap = 40
    assert "max you can spend right now is 40" in (say(host, "alice", "!lottery buy 5") or "")
    assert host.bal("alice") == 1000 and host.wagers() == 0


def test_funds_that_vanish_after_the_precheck_leave_no_tickets(host: Host) -> None:
    from waddle_sdk.testing import _Error_InsufficientFunds, _FakeWitError  # noqa: PLC2701

    real = host.eco.wager

    def broke(user: str, stake: int, payout: int) -> int:
        raise _FakeWitError(_Error_InsufficientFunds(3))

    sys.modules["wit_world"].imports.economy.wager = broke  # type: ignore[attr-defined]
    assert "you only have 3" in (say(host, "alice", "!lottery buy 2") or "")
    assert not host.keys(".tk.") and not host.keys(".tix.") and not host.keys(".ent.")
    assert not host.keys(".sold.") and not host.keys(".buyers.")
    sys.modules["wit_world"].imports.economy.wager = real  # type: ignore[attr-defined]
    assert "bought 2 tickets" in (say(host, "alice", "!lottery buy 2") or "")


def test_a_daily_limit_refusal_is_polite_and_leaves_no_tickets(host: Host) -> None:
    from waddle_sdk.testing import _Error_Denied, _FakeWitError  # noqa: PLC2701

    def limit(user: str, stake: int, payout: int) -> int:
        raise _FakeWitError(_Error_Denied("quota_exceeded"))

    sys.modules["wit_world"].imports.economy.wager = limit  # type: ignore[attr-defined]
    assert "daily points limit" in (say(host, "alice", "!lottery buy 2") or "")
    assert not host.keys(".tix.") and not host.keys(".sold.")


def test_sales_closed_after_the_interval(host: Host) -> None:
    say(host, "alice", "!lottery buy 1")
    host.advance(DRAW_INTERVAL_SECONDS + 1)
    assert "sales are closed" in (say(host, "bob", "!lottery buy 1") or "")
    assert host.bal("bob") == 1000 and host.counter("sold", 1) == 1


def test_cooldown_blocks_a_rapid_second_buy_but_precheck_failures_do_not_burn_it(
    host: Host,
) -> None:
    say(host, "alice", "!lottery buy 99")  # refused before any claim
    assert not host.keys(f".{NS}.cd.")
    say(host, "alice", "!lottery buy 1", cooldown=True)
    assert "slow down" in (say(host, "alice", "!lottery buy 1", cooldown=True) or "")
    assert host.bal("alice") == 990
    cd = [c for c in host.kv.calls if c[0] == "increment" and f".{NS}.cd." in c[1][0]]
    assert cd and all(c[1][2] == app.COOLDOWN_SECONDS for c in cd)


def test_unlinked_actor_is_told_to_link(host: Host) -> None:
    for text in ("!lottery buy 1", "!lottery claim"):
        host.act_as(None)
        run(dispatch(envelope(host, "alice", text), {}, http_client=None))
        assert "link your account" in host.relayed[-1]
    assert host.wagers() == 0 and not host.kv.store


def test_actor_mismatch_fails_loud_and_moves_nothing(host: Host) -> None:
    host.act_as("alice")
    env = envelope(host, "alice", "!lottery buy 2")
    host.eco.actor = host.users["bob"]  # anomalous: host-resolved payer != invoking account
    with pytest.raises(RuntimeError, match="lottery wager failed"):
        run(dispatch(env, {}, http_client=None))
    assert "temporarily unavailable" in host.relayed[-1]
    assert host.bal("alice") == 1000 and not host.keys(".tix.") and not host.keys(".sold.")


def test_ambiguous_backend_error_retries_the_identical_call(host: Host) -> None:
    real = host.eco.wager
    calls: list[tuple[str, int, int]] = []

    def flaky(user: str, stake: int, payout: int) -> int:
        calls.append((user, stake, payout))
        if len(calls) == 1:
            raise RuntimeError("storage down")  # classified as BackendError by the facade
        return real(user, stake, payout)

    sys.modules["wit_world"].imports.economy.wager = flaky  # type: ignore[attr-defined]
    assert "bought 2 tickets" in (say(host, "alice", "!lottery buy 2") or "")
    assert len(calls) == 2 and calls[0] == calls[1] and host.bal("alice") == 980


def test_backend_error_twice_fails_loud_and_records_no_tickets(host: Host) -> None:
    def down(user: str, stake: int, payout: int) -> int:
        raise RuntimeError("storage down")

    sys.modules["wit_world"].imports.economy.wager = down  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="lottery wager failed"):
        say(host, "alice", "!lottery buy 2")
    assert not host.keys(".tix.") and not host.keys(".sold.") and host.bal("alice") == 1000


def test_a_crash_before_the_sale_is_counted_never_inflates_the_pot(host: Host) -> None:
    """`sold` (the pot) is written last: a crash strands paid points, never mints an unpaid pot."""
    say(host, "alice", "!lottery buy 3")
    real = host.kv.increment

    def inc(key: str, delta: int, ttl: int) -> int:
        if f".{NS}.sold." in key:
            raise RuntimeError("down")
        return real(key, delta, ttl)

    sys.modules["wit_world"].imports.kv.increment = inc  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="lottery increment failed"):
        say(host, "bob", "!lottery buy 5")
    sys.modules["wit_world"].imports.kv.increment = real  # type: ignore[attr-defined]
    say(host, "carol", "!lottery buy 2")
    assert host.bal("bob") == 950, "bob's points were debited"
    assert host.counter("sold", 1) == 5, "the pot excludes the purchase whose count was lost"
    initial_debited = 3 * 10 + 5 * 10 + 2 * 10
    pot = host.counter("sold", 1) * TICKET_PRICE
    assert pot <= initial_debited


def test_a_ghost_range_from_a_crash_is_redrawn_never_won(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    say(host, "alice", "!lottery buy 3")  # positions 1-3
    real = host.kv.set

    def st(key: str, value: bytes, ttl: int) -> None:
        if f".{NS}.ent.1.8" in key:
            raise RuntimeError("down")  # bob's entry (positions 4-8) is never written
        real(key, value, ttl)

    sys.modules["wit_world"].imports.kv.set = st  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="lottery set failed"):
        say(host, "bob", "!lottery buy 5")
    sys.modules["wit_world"].imports.kv.set = real  # type: ignore[attr-defined]
    say(host, "carol", "!lottery buy 2")  # positions 9-10
    assert host.counter("tix", 1) == 10 and host.counter("sold", 1) == 5
    pin_draw(monkeypatch, 5, 2)  # 5 lies in the ghost range 4-8 -> redraw -> 2 is alice
    reply = draw_now(host)
    assert "wins the pot of 50 points" in reply and "tok-alice" in reply
    assert win(host)["p"] == pseud(host, "alice"), "a ghost position must never pick its neighbour"


def test_every_ghost_draw_fails_loud_and_frees_the_election(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    say(host, "alice", "!lottery buy 3")
    say(host, "bob", "!lottery buy 3")
    host.kv.store[host.key("tix", 1)] = b"1000"  # positions 7..1000 have no entries (ghosts)
    pin_draw(monkeypatch, 500)
    make_due(host)
    with pytest.raises(RuntimeError, match="lottery draw failed"):
        say(host, "mod", "!lottery draw", mod=True)
    assert not host.keys(".win.") and not host.keys(".drawing.")
    assert max(host.kv_ops) <= 64, host.kv_ops
    pin_draw(monkeypatch, 5)
    assert "wins the pot of 60 points" in (say(host, "mod", "!lottery draw", mod=True) or "")


# --- drawing ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("position", "winner"),
    [(1, "alice"), (3, "alice"), (4, "bob"), (8, "bob"), (9, "carol"), (10, "carol")],
)
def test_the_draw_maps_a_ticket_position_to_its_buyer(
    host: Host, monkeypatch: pytest.MonkeyPatch, position: int, winner: str
) -> None:
    standard_round(host)
    pin_draw(monkeypatch, position)
    reply = draw_now(host)
    assert f"{{user:tok-{winner}}} wins the pot of 100 points (10 tickets, 3 players)" in reply
    assert win(host) == {
        "p": pseud(host, winner),
        "k": f"tok-{winner}",
        "pot": 100,
        "t": 10,
        "b": 3,
    }


def test_the_draw_is_exactly_ticket_weighted(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every position wins for exactly its owner: wins per player == tickets held."""
    standard_round(host)
    game = app._Game(app._Store(COMM), None)  # type: ignore[arg-type]
    tally: dict[str, int] = {}
    for position in range(1, 11):
        pin_draw(monkeypatch, position)
        winner = run(game._pick_winner(1, 10, 100, 3))
        tally[winner.pseudonym] = tally.get(winner.pseudonym, 0) + 1
    assert tally == {pseud(host, "alice"): 3, pseud(host, "bob"): 5, pseud(host, "carol"): 2}


def test_the_real_rng_draws_a_uniform_position_in_range(host: Host) -> None:
    picks = {app._pick_ticket(5) for _ in range(300)}
    assert picks == {1, 2, 3, 4, 5}


def test_a_moderator_can_draw_early_after_a_close_and_a_settle_wait(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 4)
    closing = say(host, "mod", "!lottery draw", mod=True) or ""
    assert "Ticket sales are closed" in closing and not host.keys(".win.")
    assert "sales are closed" in (say(host, "alice", "!lottery buy 1") or "")
    assert "wait" in (say(host, "mod", "!lottery draw", mod=True) or "")
    host.advance(SETTLE_SECONDS + 1)
    assert "{user:tok-bob} wins the pot of 100 points" in (
        say(host, "mod", "!lottery draw", mod=True) or ""
    )


def test_a_non_moderator_cannot_draw_early_but_can_once_due(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    state = {k: v for k, v in host.kv.store.items() if ".seen." not in k and ".cd." not in k}
    assert "sales are open for" in (say(host, "alice", "!lottery draw") or "")
    after = {k: v for k, v in host.kv.store.items() if ".seen." not in k and ".cd." not in k}
    assert after == state, "an early draw by a non-moderator must change nothing"
    assert not host.keys(".win.") and host.counter("sold", 1) == 10
    pin_draw(monkeypatch, 1)
    assert "wins the pot" in draw_now(host, "alice", mod=False)


@pytest.mark.parametrize("badge", [{}, {"is_mod": False}, {"is_mod": "true"}, {"is_mod": 1}])
def test_only_a_real_boolean_badge_may_close_sales_early(host: Host, badge: dict[str, Any]) -> None:
    standard_round(host)
    reply = say(host, "alice", "!lottery draw", extra=badge) or ""
    assert "sales are open for" in reply
    assert int(host.kv.store[host.key("close", 1)]) > host.now_ms, "sales must stay open"


@pytest.mark.parametrize("badge", ["false", "true", 1, [True]])
def test_dispatch_itself_rejects_a_non_boolean_badge(host: Host, badge: Any) -> None:
    standard_round(host)
    env = envelope(host, "mod", "!lottery draw", mod=True)
    env.event.payload["is_mod"] = badge
    host.act_as("mod")
    run(dispatch(env, {}, http_client=None))
    assert "sales are open for" in host.relayed[-1]
    assert int(host.kv.store[host.key("close", 1)]) > host.now_ms


def test_a_broadcaster_may_close_sales_early(host: Host) -> None:
    standard_round(host)
    reply = say(host, "alice", "!lottery draw", extra={"is_broadcaster": True}) or ""
    assert "Ticket sales are closed" in reply


def test_drawing_with_no_lottery_running(host: Host) -> None:
    assert "no lottery running" in (say(host, "mod", "!lottery draw", mod=True) or "")


def test_one_player_cannot_draw_sales_are_extended_instead(host: Host) -> None:
    say(host, "alice", "!lottery buy 4")
    make_due(host)
    reply = say(host, "mod", "!lottery draw", mod=True) or ""
    assert "needs at least 2 players (1 so far)" in reply and not host.keys(".win.")
    assert int(host.kv.store[host.key("close", 1)]) > host.now_ms, "sales reopened"
    assert "bought 1 ticket" in (say(host, "bob", "!lottery buy 1") or "")
    assert "wins the pot of 50 points" in draw_now(host)


def test_double_draw_is_idempotent_and_changes_nothing(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 9)
    assert "tok-carol} wins" in draw_now(host)
    snapshot = dict(host.kv.store)
    pin_draw(monkeypatch, 1)  # a second draw that WOULD pick alice if it were allowed to run
    again = say(host, "mod", "!lottery draw", mod=True) or ""
    other = say(host, "alice", "!lottery draw") or ""
    assert "already drawn: {user:tok-carol} won 100" in again and "tok-carol" in other
    assert [k for k in host.kv.store if k not in snapshot and ".seen." not in k] == []
    assert len(writes_to(host, ".win.")) == 1 and win(host)["p"] == pseud(host, "carol")


def test_concurrent_draws_elect_exactly_one_drawer(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    make_due(host)
    envs = [envelope(host, "mod", "!lottery draw", mod=True) for _ in range(4)]
    yielding_kv(monkeypatch)

    async def go() -> None:
        await asyncio.gather(*(dispatch(e, {}, http_client=None) for e in envs))

    run(go())
    assert len(writes_to(host, ".win.")) == 1, "two drawers wrote a result"
    assert sum("wins the pot" in m for m in host.relayed) == 1


def test_a_stale_drawing_election_heals_after_its_ttl(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    host.kv.store[host.key("drawing", 1)] = b"1"
    make_due(host)
    assert "already happening" in (say(host, "mod", "!lottery draw", mod=True) or "")
    host.expire(".drawing.")
    pin_draw(monkeypatch, 1)
    assert "wins the pot" in (say(host, "mod", "!lottery draw", mod=True) or "")
    ttls = [c[1][2] for c in host.kv.calls if c[0] == "increment" and ".drawing." in c[1][0]]
    assert ttls and set(ttls) == {60}


def test_the_next_buy_after_a_draw_opens_a_fresh_round(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 1)
    draw_now(host)
    reply = say(host, "bob", "!lottery buy 1") or ""
    assert "Lottery #2 is open!" in reply and "Pot: 10 points" in reply
    assert host.counter("round") == 2 and host.counter("sold", 2) == 1
    assert host.counter("sold", 1) == 10 and win(host)["pot"] == 100, "round 1 is untouched"
    assert host.counter("tk", 2, pseud(host, "bob")) == 1, "ticket caps are per round"


def test_concurrent_first_buys_open_exactly_one_round(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    envs = []
    for who in ("alice", "bob", "carol"):
        host.act_as(who)
        envs.append(envelope(host, who, "!lottery buy 1"))
    yielding_kv(monkeypatch)

    async def go() -> None:
        for env, who in zip(envs, ("alice", "bob", "carol"), strict=True):
            host.act_as(who)
            await dispatch(env, {}, http_client=None)

    run(go())
    assert host.counter("round") == 1 and len(host.keys(".def.")) == 1


def test_a_lost_opening_election_asks_the_buyer_to_retry(host: Host) -> None:
    host.kv.store[host.key("opening", 1)] = b"1"  # someone else is mid-open
    reply = say(host, "alice", "!lottery buy 1") or ""
    assert "being set up" in reply and host.bal("alice") == 1000
    host.expire(".opening.")
    assert "Lottery #1 is open!" in (say(host, "alice", "!lottery buy 1") or "")


def test_status_in_every_phase(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "no lottery running" in (say(host, "alice", "!lottery") or "")
    say(host, "alice", "!lottery buy 3")
    status = say(host, "bob", "!lottery") or ""
    assert "Lottery #1: pot 30 points (3 tickets, 1 players)" in status
    assert "sales close in 60m" in status
    host.advance(DRAW_INTERVAL_SECONDS + 1)
    assert "sales closed, the draw is due" in (say(host, "bob", "!lottery") or "")
    say(host, "bob", "!lottery buy 1")  # refused: closed
    pin_draw(monkeypatch, 1)
    standard_due = say(host, "mod", "!lottery draw", mod=True) or ""
    assert "wait" in standard_due
    host.advance(SETTLE_SECONDS)
    say(host, "carol", "!lottery draw")  # one player only: sales extended, nothing drawn
    assert "sales close in" in (say(host, "bob", "!lottery") or "")
    say(host, "bob", "!lottery buy 1")
    make_due(host)
    assert "wins the pot" in (say(host, "bob", "!lottery draw") or "")
    drawn = say(host, "bob", "!lottery") or ""
    assert "was drawn: {user:tok-alice} won 40 points" in drawn


# --- claiming ---------------------------------------------------------------------------------


def test_the_winner_collects_the_pot_and_points_are_conserved(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = host.total()
    standard_round(host)
    pin_draw(monkeypatch, 5)  # bob
    draw_now(host)
    assert host.total() == initial - 100, "the pot is held back until it is collected"
    assert "nothing to collect" in (say(host, "alice", "!lottery claim") or "")
    assert "collected 100 points." in (say(host, "bob", "!lottery claim") or "")
    assert host.bal("bob") == 1000 - 50 + 100 and host.total() == initial


def test_claim_before_the_draw_or_by_a_non_player_is_empty(host: Host) -> None:
    assert "nothing to collect" in (say(host, "alice", "!lottery claim") or "")
    standard_round(host)
    assert "nothing to collect" in (say(host, "alice", "!lottery claim") or "")
    assert host.bal("alice") == 970


def test_double_claim_pays_once(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 9)
    draw_now(host)
    assert "collected 100" in (say(host, "carol", "!lottery claim") or "")
    assert "nothing to collect" in (say(host, "carol", "!lottery claim") or "")
    assert host.bal("carol") == 1000 - 20 + 100 and host.counter("paid", 1) == 100


def test_a_big_pot_is_collected_over_several_capped_claims(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 1)
    draw_now(host)
    host.eco.max_bet_cap = 1  # chunk = 99 per claim
    bal = host.bal("alice")
    replies = [say(host, "alice", "!lottery claim") or "" for _ in range(3)]
    assert "collected 99 points (1 more" in replies[0]
    assert "collected 1 points." in replies[1] and "nothing to collect" in replies[2]
    assert host.bal("alice") == bal + 100


def test_a_zero_balance_winner_needs_one_point_to_collect(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    say(host, "alice", "!lottery buy 10")
    say(host, "bob", "!lottery buy 10")
    host.eco.balances[host.users["alice"]] = 0
    pin_draw(monkeypatch, 1)
    draw_now(host)
    assert "hold at least 1 point" in (say(host, "alice", "!lottery claim") or "")
    host.eco.balances[host.users["alice"]] = 1
    assert "collected 99 points" in (say(host, "alice", "!lottery claim") or "")
    while "collected" in (say(host, "alice", "!lottery claim") or ""):
        pass
    assert host.bal("alice") == 1 + 200, "the 1 point of float plus the whole pot"


def test_claim_finds_the_previous_round_or_an_explicit_one(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 5)
    draw_now(host)
    say(host, "carol", "!lottery buy 1")  # round 2 opens
    assert "collected 100" in (say(host, "bob", "!lottery claim") or ""), "round N-1 is searched"
    assert "nothing to collect" in (say(host, "bob", "!lottery claim 2") or "")
    assert say(host, "bob", "!lottery claim abc") == app._USAGE


def test_explicit_round_claim(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 5)
    draw_now(host)
    assert "collected 100" in (say(host, "bob", "!lottery claim 1") or "")


def test_a_host_refusal_releases_the_reservation_so_a_retry_works(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 1)
    draw_now(host)
    from waddle_sdk.testing import _Error_Denied, _FakeWitError  # noqa: PLC2701

    real = host.eco.wager
    state = {"n": 0}

    def quota(user: str, stake: int, payout: int) -> int:
        state["n"] += 1
        if state["n"] == 1:
            raise _FakeWitError(_Error_Denied("quota_exceeded"))
        return real(user, stake, payout)

    sys.modules["wit_world"].imports.economy.wager = quota  # type: ignore[attr-defined]
    assert "daily points limit" in (say(host, "alice", "!lottery claim") or "")
    assert host.counter("paid", 1) == 0 and host.bal("alice") == 970
    assert "collected 100" in (say(host, "alice", "!lottery claim") or "")
    assert host.counter("paid", 1) == 100 and host.bal("alice") == 1070


def test_a_denied_grant_on_claim_is_a_polite_refusal_that_moves_nothing(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 1)
    draw_now(host)
    host.eco.granted = False
    assert "can't collect right now" in (say(host, "alice", "!lottery claim") or "")
    assert host.counter("paid", 1) == 0


def test_an_unexpected_wager_denial_on_claim_fails_loud_but_releases(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 1)
    draw_now(host)
    host.act_as("alice")
    env = envelope(host, "alice", "!lottery claim")
    host.eco.actor = host.users["bob"]  # anomalous payer -> actor_mismatch
    with pytest.raises(RuntimeError, match="lottery wager failed"):
        run(dispatch(env, {}, http_client=None))
    assert host.counter("paid", 1) == 0 and host.bal("alice") == 970


def test_claim_backend_error_twice_fails_loud_and_keeps_the_reservation(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 1)
    draw_now(host)

    def down(user: str, stake: int, payout: int) -> int:
        raise RuntimeError("storage down")

    sys.modules["wit_world"].imports.economy.wager = down  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="lottery wager failed"):
        say(host, "alice", "!lottery claim")
    assert host.counter("paid", 1) == 100, "ambiguous commit: never risk a double payout"
    assert "nothing to collect" in (say(host, "alice", "!lottery claim") or "")


def test_concurrent_claims_by_one_actor_pay_once(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 1)
    draw_now(host)
    host.act_as("alice")
    envs = [envelope(host, "alice", "!lottery claim") for _ in range(5)]
    yielding_kv(monkeypatch)

    async def go() -> None:
        await asyncio.gather(*(dispatch(e, {}, http_client=None) for e in envs))

    run(go())
    assert host.bal("alice") == 970 + 100 and host.wagers() == 3 + 1  # 3 buys + ONE claim


def test_the_reservation_cas_stops_overlapping_collectors(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 1)
    draw_now(host)
    host.act_as("alice")
    real = host.kv.increment

    def racing(key: str, delta: int, ttl: int) -> int:
        if key.endswith(".paid.1") and delta > 0:
            real(key, 120, ttl)  # a concurrent collector reserved first, between read and CAS
        return real(key, delta, ttl)

    sys.modules["wit_world"].imports.kv.increment = racing  # type: ignore[attr-defined]
    assert "nothing more to collect" in (say(host, "alice", "!lottery claim") or "")
    assert host.bal("alice") == 970 and host.counter("paid", 1) == 120


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_randomized_rounds_pay_exactly_what_was_spent(
    host: Host, monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    rng = random.Random(seed)  # noqa: S311 - deterministic test data, not security
    names = [f"u{i}" for i in range(20)]
    for n in names:
        host.add_user(n, 1000)
    initial = host.total()
    spent = 0
    for n in names:
        for _ in range(rng.randint(1, 2)):
            count = rng.randint(1, MAX_BUY)
            say(host, n, f"!lottery buy {count}")
            spent += count * TICKET_PRICE
    sold = host.counter("sold", 1)
    assert sold * TICKET_PRICE == spent - (spent - sold * TICKET_PRICE)
    pin_draw(monkeypatch, rng.randint(1, host.counter("tix", 1)))
    draw_now(host)
    winner_pseud = win(host)["p"]
    winner = next(n for n in names if pseud(host, n) == winner_pseud)
    pot = win(host)["pot"]
    assert pot == sold * TICKET_PRICE
    while "collected" in (say(host, winner, "!lottery claim") or ""):
        pass
    assert host.total() == initial - spent + pot and pot <= spent
    for n in names:
        if n != winner:
            assert "nothing to collect" in (say(host, n, "!lottery claim") or "")


# --- redelivery / replay safety ----------------------------------------------------------------


def test_redelivered_buy_is_applied_once_and_skipped_silently(host: Host) -> None:
    assert "bought 3" in (say(host, "alice", "!lottery buy 3", msg_id="dup-1") or "")
    sent = len(host.relayed)
    assert say(host, "alice", "!lottery buy 3", msg_id="dup-1") is None
    assert (host.counter("sold", 1), host.bal("alice"), host.wagers()) == (3, 970, 1)
    assert host.counter("tix", 1) == 3 and len(host.relayed) == sent


def test_redelivery_inside_the_cooldown_window_changes_nothing(host: Host) -> None:
    say(host, "alice", "!lottery buy 3", msg_id="dup-2")
    markers = len(host.keys(".seen."))
    assert say(host, "alice", "!lottery buy 3", msg_id="dup-2", cooldown=True) is None
    assert host.counter("sold", 1) == 3 and len(host.keys(".seen.")) == markers


def test_redelivery_is_recognised_after_other_purchases_in_between(host: Host) -> None:
    say(host, "alice", "!lottery buy 1", msg_id="dup-3")
    for i in range(10):
        say(host, "bob", "!lottery buy 1", msg_id=f"other-{i}")
    assert say(host, "alice", "!lottery buy 1", msg_id="dup-3") is None
    assert host.counter("sold", 1) == 11 and host.bal("alice") == 990


def test_redelivered_draw_changes_nothing(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 1)
    assert "wins the pot" in draw_now(host, msg_id="d-1")
    base = dict(host.kv.store)
    assert say(host, "mod", "!lottery draw", mod=True, msg_id="d-1") is None
    assert host.kv.store == base


def test_redelivered_early_close_is_a_noop(host: Host) -> None:
    standard_round(host)
    assert "Ticket sales are closed" in (
        say(host, "mod", "!lottery draw", mod=True, msg_id="c-1") or ""
    )
    base = dict(host.kv.store)
    assert say(host, "mod", "!lottery draw", mod=True, msg_id="c-1") is None
    assert host.kv.store == base


def test_redelivered_claim_pays_once(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 1)
    draw_now(host)
    assert "collected 100" in (say(host, "alice", "!lottery claim", msg_id="k-1") or "")
    assert say(host, "alice", "!lottery claim", msg_id="k-1") is None
    assert host.bal("alice") == 1070


def test_the_event_claim_is_recorded_before_the_mutation(host: Host) -> None:
    """At-most-once: a crash mid-purchase is never replayed into a second debit."""

    def down(user: str, stake: int, payout: int) -> int:
        raise RuntimeError("storage down")

    real = sys.modules["wit_world"].imports.economy.wager  # type: ignore[attr-defined]
    sys.modules["wit_world"].imports.economy.wager = down  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="lottery wager failed"):
        say(host, "alice", "!lottery buy 2", msg_id="crash-1")
    sys.modules["wit_world"].imports.economy.wager = real  # type: ignore[attr-defined]
    sent = len(host.relayed)
    assert say(host, "alice", "!lottery buy 2", msg_id="crash-1") is None
    assert (host.wagers(), host.bal("alice"), host.counter("sold", 1)) == (0, 1000, 0)
    assert len(host.relayed) == sent


@pytest.mark.parametrize("copies", [2, 6])
def test_concurrent_duplicate_deliveries_process_exactly_once(
    host: Host, monkeypatch: pytest.MonkeyPatch, copies: int
) -> None:
    host.act_as("alice")
    yielding_kv(monkeypatch)
    env = envelope(host, "alice", "!lottery buy 3", msg_id="race-1")

    async def go() -> None:
        await asyncio.gather(*(dispatch(env, {}, http_client=None) for _ in range(copies)))

    run(go())
    assert (host.counter("sold", 1), host.bal("alice"), host.wagers()) == (3, 970, 1)


@pytest.mark.parametrize("copies", [2, 6])
def test_concurrent_duplicates_process_once_even_with_no_cooldown_gate(
    host: Host, monkeypatch: pytest.MonkeyPatch, copies: int
) -> None:
    """The event claim alone (not the cooldown that usually fronts it) must dedupe a race."""

    async def no_cooldown(self: Any, pseudonym: str) -> bool:
        return True

    monkeypatch.setattr(app._Store, "cooldown_claim", no_cooldown)
    say(host, "bob", "!lottery buy 1")  # the round already exists: no opening race here
    host.act_as("alice")
    yielding_kv(monkeypatch)
    env = envelope(host, "alice", "!lottery buy 3", msg_id="race-2")

    async def go() -> None:
        await asyncio.gather(*(dispatch(env, {}, http_client=None) for _ in range(copies)))

    run(go())
    assert (host.counter("sold", 1), host.bal("alice"), host.wagers()) == (4, 970, 2)
    assert sum("bought 3 tickets" in m for m in host.relayed) == 1


@pytest.mark.parametrize("missing", [None, "", "   ", 12345])
def test_a_mutation_without_a_message_id_fails_loud_and_moves_nothing(
    host: Host, missing: Any
) -> None:
    standard_round(host)
    before = dict(host.kv.store)
    for text, mod in (
        ("!lottery buy 1", False),
        ("!lottery claim", False),
        ("!lottery draw", True),
    ):
        host.act_as("alice")
        env = envelope(host, "alice", text, mod=mod)
        if missing is None:
            env.event.payload.pop("message_id")
        else:
            env.event.payload["message_id"] = missing
        with pytest.raises(RuntimeError, match="lottery event_id failed: EventIdError"):
            run(dispatch(env, {}, http_client=None))
        assert "temporarily unavailable" in host.relayed[-1]
    assert host.kv.store == before and host.bal("alice") == 970
    assert any("lottery.error" in m and "EventIdError" in fj for m, fj in host.logs)


def test_status_and_usage_need_no_message_id(host: Host) -> None:
    for text in ("!lottery", "!lottery bogus"):
        env = envelope(host, "alice", text)
        env.event.payload.pop("message_id", None)
        run(dispatch(env, {}, http_client=None))
        assert host.relayed[-1]


def test_dedupe_kv_failure_is_loud(host: Host) -> None:
    real = host.kv.get

    def get(key: str) -> bytes | None:
        if ".seen." in key:
            raise RuntimeError("down")
        return real(key)

    sys.modules["wit_world"].imports.kv.get = get  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="lottery dedupe failed"):
        say(host, "alice", "!lottery buy 1")
    assert (host.wagers(), host.bal("alice")) == (0, 1000)


def test_a_full_key_quota_fails_closed_and_moves_no_money(host: Host) -> None:
    real = host.kv.increment

    def inc(key: str, delta: int, ttl: int) -> int:
        if ".seen." in key:
            raise RuntimeError("quota exceeded")
        return real(key, delta, ttl)

    sys.modules["wit_world"].imports.kv.increment = inc  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="lottery dedupe failed"):
        say(host, "alice", "!lottery buy 1", msg_id="q-1")
    assert (host.wagers(), host.bal("alice")) == (0, 1000)
    assert "temporarily unavailable" in host.relayed[-1]


@pytest.mark.parametrize("bad", [b"not-a-number", b"-5"])
def test_a_corrupt_event_marker_never_lets_the_event_settle_again(
    host: Host, monkeypatch: pytest.MonkeyPatch, bad: bytes
) -> None:
    before = set(host.keys(".seen."))
    say(host, "alice", "!lottery buy 1", msg_id="corrupt-1")
    (marker,) = set(host.keys(".seen.")) - before
    host.kv.store[marker] = bad
    snapshot = (host.wagers(), host.bal("alice"))
    assert say(host, "alice", "!lottery buy 1", msg_id="corrupt-1") is None

    async def peek_misses(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(app.event_dedupe, "already_seen", peek_misses)
    with pytest.raises(RuntimeError, match="lottery dedupe failed"):
        say(host, "alice", "!lottery buy 1", msg_id="corrupt-1")
    assert (host.wagers(), host.bal("alice")) == snapshot


def test_dedupe_state_is_one_pii_free_marker_per_event(host: Host) -> None:
    say(host, "alice", "!lottery buy 1")
    base = len(host.keys(".seen."))
    for i in range(6):
        say(host, "alice", "!lottery buy 1", msg_id=f"private-platform-id-{i}")
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
    env = envelope(host, "alice", "!lottery buy 1")
    env.event.occurred_at = host.stamp(-age)
    host.act_as("alice")
    run(dispatch(env, {}, http_client=None))
    assert not host.relayed and not host.kv.store and host.wagers() == 0
    assert any("stale_event_skipped" in m for m, _fj in host.logs)


def test_an_event_at_the_age_limit_is_still_processed(host: Host) -> None:
    env = envelope(host, "alice", "!lottery buy 1")
    env.event.occurred_at = host.stamp(-event_dedupe.MAX_EVENT_AGE_SECONDS)
    host.act_as("alice")
    run(dispatch(env, {}, http_client=None))
    assert "bought 1 ticket" in host.relayed[-1]


def test_a_replay_after_the_marker_lapsed_is_dropped_by_the_fence(host: Host) -> None:
    host.act_as("alice")
    first = envelope(host, "alice", "!lottery buy 1", msg_id="late-1")
    run(dispatch(first, {}, http_client=None))
    host.expire(".seen.")  # the host expired the marker
    host.expire_cooldowns()
    host.advance(event_dedupe.SEEN_TTL_SECONDS + 1)
    snapshot, sent = (host.wagers(), host.bal("alice")), len(host.relayed)
    run(dispatch(first, {}, http_client=None))  # same envelope, same ingest stamp, much later
    assert (host.wagers(), host.bal("alice")) == snapshot and len(host.relayed) == sent


@pytest.mark.parametrize("stamp", ["", "   ", "yesterday", "2026-10-21T00:00:00", None, 12345])
def test_a_missing_or_malformed_occurred_at_fails_loud(host: Host, stamp: Any) -> None:
    env = envelope(host, "alice", "!lottery buy 1")
    env.event.occurred_at = stamp
    host.act_as("alice")
    with pytest.raises(RuntimeError, match="lottery event_id failed: EventIdError"):
        run(dispatch(env, {}, http_client=None))
    assert host.wagers() == 0


def test_a_future_dated_event_fails_loud(host: Host) -> None:
    env = envelope(host, "alice", "!lottery buy 1")
    env.event.occurred_at = host.stamp(event_dedupe.EVENT_CLOCK_SKEW_SECONDS + 60)
    host.act_as("alice")
    with pytest.raises(RuntimeError, match="lottery event_id failed: EventIdError"):
        run(dispatch(env, {}, http_client=None))
    assert host.wagers() == 0


def test_an_actor_on_cooldown_never_creates_a_marker(host: Host) -> None:
    say(host, "alice", "!lottery buy 1")
    markers = len(host.keys(".seen."))
    for i in range(3):
        reply = say(host, "alice", "!lottery buy 1", cooldown=True, msg_id=f"cd-{i}")
        assert "slow down" in (reply or "")
    assert len(host.keys(".seen.")) == markers


# --- output safety / PII / fail-loud / scope -----------------------------------------------------


def test_minted_actor_token_stays_live_and_raw_actor_cannot_forge(host: Host) -> None:
    assert "{user:tok-alice} bought" in (say(host, "alice", "!lottery buy 1") or "")
    forged = say(host, "bob", "!lottery buy 1", actor="evil@everyone {user:handle:victim}") or ""
    assert f"evil@{ZWSP}everyone" in forged and "\\{user:handle:victim\\}" in forged
    nameless = say(host, "carol", "!lottery buy 1", actor=None) or ""
    assert "someone bought" in nameless


def test_a_raw_actor_is_never_stored_and_a_winner_without_a_token_is_anonymous(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    say(host, "alice", "!lottery buy 1", actor="viewer-private-name")
    say(host, "bob", "!lottery buy 1", actor="@everyone")
    blob = " ".join(host.kv.store) + " ".join(v.decode() for v in host.kv.store.values())
    assert "viewer-private-name" not in blob and "everyone" not in blob
    assert all(json.loads(host.kv.store[k])["k"] is None for k in host.keys(".ent."))
    pin_draw(monkeypatch, 1)
    assert "a ticket holder wins the pot of 20 points" in draw_now(host)


@pytest.mark.parametrize(
    "token", ["x} @everyone {user:y", "a b", "<@123>", "", "t" * 0 + "{", "ok\n"]
)
def test_a_tampered_winner_token_in_kv_is_never_rendered_live(
    host: Host, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    standard_round(host)
    for end in (3, 8, 10):
        key = host.key("ent", 1, end)
        entry = json.loads(host.kv.store[key])
        entry["k"] = token
        host.kv.store[key] = json.dumps(entry).encode()
    pin_draw(monkeypatch, 1)
    reply = draw_now(host)
    assert "a ticket holder wins the pot" in reply and "everyone" not in reply
    assert "{user:" not in reply.replace("{user:tok-", "")


def test_pii_free_keys_and_logs(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    say(host, "alice", "!lottery buy 3", actor="viewer-private-name")
    say(host, "bob", "!lottery buy 3")
    pin_draw(monkeypatch, 1)
    draw_now(host)
    say(host, "alice", "!lottery claim", actor="viewer-private-name")
    p = pseud(host, "alice")
    keys = list(host.kv.store)
    assert any(p in k for k in keys) and len(p) == 32
    text = " ".join(keys) + " ".join(m + fj for m, fj in host.logs)
    for raw in (host.users["alice"], "viewer-private-name", "alice"):
        assert raw not in text
    assert all(":" not in k.removeprefix("c.") for k in keys)
    assert all(re.fullmatch(r"c\.comm-1\.lottery\.[A-Za-z0-9._-]+", k) for k in keys)


def test_kv_failure_is_loud(host: Host) -> None:
    def boom(*_a: Any) -> Any:
        raise RuntimeError("down")

    sys.modules["wit_world"].imports.kv.get = boom  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="lottery get failed"):
        say(host, "alice", "!lottery")
    assert "temporarily unavailable" in host.relayed[-1]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (("win", 1), b'{"not": "valid"}'),
        (("win", 1), json.dumps({"p": "x", "k": None, "pot": 10**9, "t": 1, "b": 1}).encode()),
        (("close", 1), b"soon"),
    ],
)
def test_a_corrupt_stored_record_fails_loud(
    host: Host, monkeypatch: pytest.MonkeyPatch, key: tuple[Any, ...], value: bytes
) -> None:
    standard_round(host)
    pin_draw(monkeypatch, 1)
    draw_now(host)
    host.kv.store[host.key(*key)] = value
    with pytest.raises(RuntimeError, match="lottery (corrupt|get) failed"):
        say(host, "alice", "!lottery")


def test_a_non_integer_counter_fails_loud(host: Host) -> None:
    standard_round(host)
    host.kv.store[host.key("sold", 1)] = b"lots"
    with pytest.raises(RuntimeError, match="lottery corrupt failed"):
        say(host, "alice", "!lottery")


def test_a_corrupt_entry_fails_the_draw_loud(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    standard_round(host)
    host.kv.store[host.key("ent", 1, 3)] = b'{"n": 99, "p": "x", "k": null}'
    pin_draw(monkeypatch, 1)
    make_due(host)
    with pytest.raises(RuntimeError, match="lottery corrupt failed"):
        say(host, "mod", "!lottery draw", mod=True)
    assert not host.keys(".win.")


def test_tickets_sold_without_positions_fails_loud(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard_round(host)
    host.kv.store[host.key("tix", 1)] = b"0"
    make_due(host)
    with pytest.raises(RuntimeError, match="lottery corrupt failed"):
        say(host, "mod", "!lottery draw", mod=True)


def test_missing_channel_unknown_command_and_empty_community_raise(host: Host) -> None:
    env = envelope(host, "alice", "!lottery")
    env.event.payload.pop("channel_id")
    with pytest.raises(ValueError, match="channel_id"):
        run(dispatch(env, {}, http_client=None))
    env = envelope(host, "alice", "!lottery")
    env.event.payload["command"] = "bogus"
    with pytest.raises(ValueError, match="unrecognized"):
        run(dispatch(env, {}, http_client=None))
    env = envelope(host, "alice", "!lottery")
    env.community = ""  # type: ignore[misc]
    with pytest.raises(ValueError, match="community"):
        run(dispatch(env, {}, http_client=None))


def test_tenant_wide_sentinel_scope_works_end_to_end(host: Host) -> None:
    host.act_as("alice")
    ev = run(transform(make_event(host, "alice", "!lottery buy 1"), community=None))
    assert ev is not None
    run(action_dispatch(to_envelope(ev, community=None), {}, http_client=None))
    assert "Lottery #1 is open!" in host.relayed[-1]
    assert any(k.startswith("c.0.lottery.") for k in host.kv.store)


def test_runs_on_activation_alone_no_feature_flag(host: Host) -> None:
    assert not hasattr(sys.modules["wit_world"].imports, "flags")
    assert "Lottery #1 is open!" in (say(host, "alice", "!lottery buy 1") or "")
    src = Path(app.__file__).read_text(encoding="utf-8")
    assert "feature_enabled" not in src and "FLAG_KEY" not in src


def test_every_dispatch_stays_inside_the_host_kv_op_budget(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(12):
        host.add_user(f"u{i}", 1000)
        say(host, f"u{i}", f"!lottery buy {1 + i % MAX_BUY}")
    pin_draw(monkeypatch, 3)
    draw_now(host)
    winner = next(f"u{i}" for i in range(12) if pseud(host, f"u{i}") == win(host)["p"])
    say(host, winner, "!lottery claim")
    say(host, "alice", "!lottery")
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
    assert MAX_BUY * TICKET_PRICE <= 500, "one purchase must fit inside the declared max_bet"


def test_pot_clamp_bounds_a_payout_whatever_the_counter_says(host: Host) -> None:
    standard_round(host)
    host.kv.store[host.key("sold", 1)] = str(10**9).encode()
    status = say(host, "alice", "!lottery") or ""
    assert f"pot {app.MAX_POT} points" in status


# --- stage split: economy/identity in transform (process stage), relay only in dispatch ---------


def test_the_whole_purchase_settles_in_transform_and_dispatch_only_relays(host: Host) -> None:
    """A real chat line: economy/identity calls run in the process stage, relay in the action."""
    host.act_as("alice")
    before, sent = len(host.eco.ledger), len(host.relayed)
    out = run(transform(make_event(host, "alice", "!lottery buy 2")))
    assert out is not None
    assert len(host.eco.ledger) == before + 1, "the tickets were paid for in transform"
    assert host.ident.calls == ["actor"]
    assert len(host.relayed) == sent, "transform cannot relay (process stage)"
    assert set(out.payload) == {"channel_id", "text", "command"}
    relayed, ledger, calls = len(host.relayed), list(host.eco.ledger), list(host.ident.calls)

    result = run(action_dispatch(to_envelope(out), {}, http_client=None))
    assert result.detail == "buy"
    assert host.relayed[relayed:] == [out.payload["text"]]
    assert host.eco.ledger == ledger and host.ident.calls == calls, "dispatch moved no money"
    assert GUARD.violations == []


@pytest.mark.parametrize("command", ["status", "usage", "buy", "draw", "claim"])
def test_dispatch_makes_no_economy_or_identity_call(host: Host, command: str) -> None:
    """`svc_action` serves no economy/identity: dispatch relays the given text and nothing else."""
    event = PlatformEvent(
        "twitch",
        "chat.message",
        "viewer-1",
        {"channel_id": "123", "text": "already computed", "command": command},
        host.stamp(),
    )
    run(action_dispatch(to_envelope(event), {}, http_client=None))
    assert host.relayed == ["already computed"]
    assert host.ident.calls == [] and host.eco.ledger == []
    assert GUARD.violations == []


def test_transform_failure_is_deferred_to_dispatch_not_relayed(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kv outage in transform cannot relay: it is marked, and dispatch relays then raises."""

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(sys.modules["wit_world"].imports.kv, "get", boom)
    host.act_as("alice")
    out = run(transform(make_event(host, "alice", "!lottery")))
    assert out is not None and out.payload["failure"].startswith("lottery get failed")
    assert host.relayed == []
    with pytest.raises(RuntimeError, match="lottery get failed"):
        run(action_dispatch(to_envelope(out), {}, http_client=None))
    assert "temporarily unavailable" in host.relayed[-1]
