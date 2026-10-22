"""Host-native tests for `blackjack`: shared fake kv/economy/identity hosts, no WASM.

The game runs in `transform` (the economy/identity imports are process-stage only), so most tests
drive `transform` directly with a scripted deck (`app._draw` patched) and read the reply text off
the returned event; `dispatch` is covered separately.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import types
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from waddle_sdk import community_kv, event_dedupe
from waddle_sdk import kv as kv_module
from waddle_sdk.flask_core.bundle_runtime import bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import (
    FakeEconomyHost,
    FakeIdentityHost,
    FakeKvHost,
    install_fake_kv_host,
    rfc3339_from_millis,
)

import app
from app import dispatch, transform

ACTOR = "11111111-1111-4111-8111-111111111111"
COMMUNITY = "comm-1"
ACTOR_TOKEN = "{user:tok-1}"  # noqa: S105 -- a host-minted placeholder, not a credential
RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
START_BALANCE = 500

_MSG_IDS = itertools.count(1)


def card(rank: str, suit: int = 0) -> int:
    """The card int for `rank` (`A`..`K`) of suit index `suit`."""
    return RANKS.index(rank) + 13 * suit


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _WitErr(Exception):  # noqa: N818 -- mirrors the generated `Err`
    """Shaped like the generated WIT `Err` (`.value` holds the error variant)."""

    def __init__(self, value: Any) -> None:
        super().__init__("host error")
        self.value = value


@dataclass
class Error_Denied:  # noqa: N801 -- mirrors the generated WIT case-class naming
    """Stand-in for the generated `Error_Denied` case."""

    value: str


class _Crash(BaseException):  # noqa: N818 -- models a killed invocation, not an error
    """A failure no `except Exception` handler can swallow."""


class Error_Backend:  # noqa: N801 -- mirrors the generated WIT case-class naming
    """Stand-in for the generated `Error_Backend` case."""

    value = "boom"


class _Host:
    def __init__(self, kv: FakeKvHost, eco: FakeEconomyHost, ident: FakeIdentityHost) -> None:
        self.kv, self.eco, self.ident = kv, eco, ident
        self.relay_calls: list[tuple[str, Any]] = []
        self.log_calls: list[tuple[Any, str, str]] = []
        self.now_ms = 1_700_000_000_000
        self.wager_calls: list[tuple[str, int, int]] = []
        self.wager_failure: Exception | None = None
        self.wager_failure_on: set[int] = set()
        self.stage = "process"
        self.stage_violations: list[str] = []

    def expire(self, marker: str) -> None:
        """Simulate the kv TTL lapsing for every key containing `marker` (the fake ignores TTL)."""
        for key in [k for k in self.kv.store if marker in k]:
            del self.kv.store[key]

    def stamp(self, offset_seconds: int = 0) -> str:
        """An ingest stamp for an event arriving now (or that long ago/ahead) on the fake clock."""
        return rfc3339_from_millis(self.now_ms + offset_seconds * 1000)

    def logged(self) -> str:
        """Every log message + field rendered as one string (for PII assertions)."""
        return json.dumps([(m, json.loads(f)) for _, m, f in self.log_calls], ensure_ascii=False)


def _install(monkeypatch: pytest.MonkeyPatch) -> _Host:
    kv = install_fake_kv_host(monkeypatch)
    eco = FakeEconomyHost(members={ACTOR}, actor=ACTOR)
    eco.balances = {ACTOR: START_BALANCE}
    ident = FakeIdentityHost(members={ACTOR}, actor=ACTOR)
    host = _Host(kv, eco, ident)
    real_wager = eco.wager

    def recording_wager(user: str, stake: int, payout: int) -> int:
        host.wager_calls.append((user, stake, payout))
        if host.wager_failure is not None and (
            not host.wager_failure_on or len(host.wager_calls) in host.wager_failure_on
        ):
            raise host.wager_failure
        return real_wager(user, stake, payout)

    def process_only(name: str, fn: Any) -> Any:
        """economy/identity exist in the PROCESS stage only (core/svc_process capabilities)."""

        def inner(*args: Any, **kwargs: Any) -> Any:
            if host.stage != "process":
                host.stage_violations.append(f"{name} outside the process stage")
            return fn(*args, **kwargs)

        return inner

    def push(provider: str, message: str) -> None:
        if host.stage != "action":  # relay is an ACTION-stage-only capability
            host.stage_violations.append("relay.push outside the action stage")
        host.relay_calls.append((provider, message))

    import wit_world

    global CURRENT
    CURRENT = host
    ns = wit_world.imports
    ns.economy = types.SimpleNamespace(
        balance=process_only("economy.balance", eco.balance),
        wager=process_only("economy.wager", recording_wager),
        transfer=process_only("economy.transfer", eco.transfer),
        max_bet=process_only("economy.max_bet", eco.max_bet),
        leaderboard=process_only("economy.leaderboard", eco.leaderboard),
    )
    ns.identity = types.SimpleNamespace(
        resolve_actor=process_only("identity.resolve_actor", ident.resolve_actor),
        resolve_mention=process_only("identity.resolve_mention", ident.resolve_mention),
    )
    ns.relay = types.SimpleNamespace(push=push)
    ns.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, f: host.log_calls.append((lvl, msg, f)),
    )
    ns.clock = types.SimpleNamespace(
        now_millis=lambda: host.now_ms, now_rfc3339=lambda: "x", monotonic_nanos=lambda: 0
    )
    return host


CURRENT: _Host | None = None


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Host]:
    h = _install(monkeypatch)
    yield h
    assert h.stage_violations == [], "a capability was used from a stage that does not have it"


class _Deck:
    """A scripted deck: `app._draw` replacement that fails loudly if the script runs out."""

    def __init__(self, *cards: int) -> None:
        self._cards = list(cards)
        self.drawn = 0

    def __call__(self) -> int:
        if not self._cards:
            raise AssertionError("scripted deck exhausted")
        self.drawn += 1
        return self._cards.pop(0)


def _deck(monkeypatch: pytest.MonkeyPatch, *cards: int) -> _Deck:
    deck = _Deck(*cards)
    monkeypatch.setattr(app, "_draw", deck)
    return deck


def _event(
    host: _Host,
    text: str,
    *,
    actor: str | None = ACTOR_TOKEN,
    platform: str = "twitch",
    occurred_at: str | None = None,
    drop: tuple[str, ...] = (),
    **extra: Any,
) -> PlatformEvent:
    payload: dict[str, Any] = {
        "text": text,
        "channel_id": "12345",
        "message_id": f"msg-{next(_MSG_IDS)}",
        **extra,
    }
    for key in drop:
        payload.pop(key, None)
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at=host.stamp() if occurred_at is None else occurred_at,
    )


def _transform(event: PlatformEvent, community: str | None = COMMUNITY) -> PlatformEvent | None:
    assert CURRENT is not None
    CURRENT.stage = "process"

    async def go() -> PlatformEvent | None:
        with bundle_context(
            tenant="t", community=community, app_id="waddles.core.example.blackjack"
        ):
            return await transform(event)

    result: PlatformEvent | None = _run(go())
    return result


def say(host: _Host, text: str, **kw: Any) -> str | None:
    """Run one chat line through `transform`; the reply text, or None (dropped/ignored)."""
    out = _transform(_event(host, text, **kw))
    return None if out is None else str(out.payload["text"])


def hand_key() -> str:
    return community_kv._scoped_key(COMMUNITY, app._hand_key(app._pseudonym(COMMUNITY, ACTOR)))


def lock_key() -> str:
    return community_kv._scoped_key(COMMUNITY, app._lock_key(app._pseudonym(COMMUNITY, ACTOR)))


def stored_hand(host: _Host) -> app.Hand | None:
    raw = host.kv.store.get(hand_key())
    return None if raw is None else app._hand_from_bytes(raw)


def deal(host: _Host, mp: pytest.MonkeyPatch, stake: int, *cards: int) -> str:
    """Deal with a scripted deck: cards are player1, player2, dealer-up, dealer-hole (+draws)."""
    _deck(mp, *cards)
    reply = say(host, f"!blackjack {stake}")
    assert reply is not None
    return reply


def balance(host: _Host) -> int:
    return host.eco.balances[ACTOR]


# -- pure rules ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("cards", "total"),
    [
        (["A", "K"], 21),
        (["A", "A"], 12),
        (["A", "A", "9"], 21),
        (["A", "6"], 17),
        (["A", "6", "10"], 17),
        (["A", "6", "10", "5"], 22),
        (["K", "Q"], 20),
        (["9", "9", "5"], 23),
        (["2", "3", "4"], 9),
        (["A", "A", "A", "A", "7"], 21),
    ],
)
def test_total(cards: list[str], total: int) -> None:
    assert app._total([card(r) for r in cards]) == total


def test_natural_only_on_two_cards() -> None:
    assert app._is_natural([card("A"), card("K")])
    assert not app._is_natural([card("7"), card("7"), card("7")])


@pytest.mark.parametrize("stake", range(1, app.MANIFEST_MAX_BET + 1))
def test_reserve_covers_the_largest_return_and_never_exceeds_the_stake(stake: int) -> None:
    reserve = app._reserve(stake)
    assert 1 <= reserve <= stake
    # economy.wager caps a payout at MAX_PAYOUT_MULTIPLE x its stake: the reserve is that stake.
    assert stake + stake * 3 // 2 <= reserve * 100


@pytest.mark.parametrize(
    ("player", "dealer", "label", "ret"),
    [
        (["10", "9"], ["10", "7"], "win", 100),
        (["10", "8"], ["10", "9"], "lose", 0),
        (["10", "8"], ["9", "9"], "push", 50),
        (["10", "6", "10"], ["5", "5"], "bust", 0),
        (["A", "K"], ["9", "7"], "blackjack", 125),
        (["A", "K"], ["A", "Q"], "push", 50),
        (["10", "9"], ["A", "K"], "dealer_blackjack", 0),
        (["10", "9"], ["10", "6", "9"], "dealer_bust", 100),
        (["7", "7", "7"], ["10", "A"], "dealer_blackjack", 0),
    ],
)
def test_return_table(player: list[str], dealer: list[str], label: str, ret: int) -> None:
    got = app._return_for([card(r) for r in player], [card(r) for r in dealer], 50)
    assert got == (label, ret)


def test_a_three_card_21_is_not_a_natural_and_pays_even_money() -> None:
    got = app._return_for([card("7"), card("7"), card("7")], [card("10"), card("9")], 50)
    assert got == ("win", 100)


# -- transform grammar ---------------------------------------------------------


@pytest.mark.parametrize("text", ["hello", "!blackjacks 5", "!hitme", "!standby", "", "blackjack"])
def test_non_matching_is_ignored(host: _Host, text: str) -> None:
    assert _transform(_event(host, text)) is None


def test_non_string_text_is_ignored(host: _Host) -> None:
    event = _event(host, "x")
    event.payload["text"] = 5
    assert _transform(event) is None


def test_missing_channel_is_loud(host: _Host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _transform(_event(host, "!blackjack 5", drop=("channel_id",)))


def test_tenant_wide_activation_is_refused_with_a_reply(host: _Host) -> None:
    out = _transform(_event(host, "!blackjack 5"), community=None)
    assert out is not None and "inside a community" in out.payload["text"]
    assert host.wager_calls == []


@pytest.mark.parametrize(
    "text", ["!blackjack banana", "!blackjack -5", "!blackjack list", "!hit now"]
)
def test_unknown_input_is_the_static_usage_line_never_an_echo(host: _Host, text: str) -> None:
    reply = say(host, text)
    assert reply == app._USAGE
    assert "banana" not in (reply or "")


def test_bare_blackjack_with_no_hand_is_usage(host: _Host) -> None:
    assert say(host, "!blackjack") == app._USAGE


# -- deal ----------------------------------------------------------------------


def test_deal_escrows_stake_minus_reserve_and_hides_the_hole_card(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = deal(host, monkeypatch, 50, card("10"), card("6"), card("9", 1), card("K", 2))
    assert host.wager_calls == [(ACTOR, 48, 0)]  # stake 50, reserve 2
    assert balance(host) == START_BALANCE - 48
    hand = stored_hand(host)
    assert hand is not None and hand.phase == app.PHASE_PLAYING and hand.stake == 50
    assert "10♠ 6♠ (16)" in reply and "dealer shows 9♥" in reply
    assert "K♦" not in reply  # the hole card is never shown mid-hand
    assert "!hit or !stand" in reply


def test_stake_of_one_needs_no_burn_and_keeps_the_whole_point_as_reserve(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 1, card("10"), card("6"), card("9"), card("8"))
    assert host.wager_calls == []
    assert balance(host) == START_BALANCE
    assert stored_hand(host) is not None


def test_deal_all_uses_the_lower_of_max_bet_and_the_community_cap(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.eco.max_bet_cap = 80
    _deck(monkeypatch, card("10"), card("6"), card("9"), card("8"))
    say(host, "!blackjack all")
    hand = stored_hand(host)
    assert hand is not None and hand.stake == 80
    host.eco.max_bet_cap = 1000


def test_deal_all_is_capped_by_the_community_maxbet(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deck(monkeypatch, card("10"), card("6"), card("9"), card("8"))
    say(host, "!blackjack all")
    hand = stored_hand(host)
    assert hand is not None and hand.stake == app.DEFAULT_MAX_BET


def test_deal_all_with_nothing_is_refused(host: _Host) -> None:
    host.eco.balances[ACTOR] = 0
    assert say(host, "!blackjack all") == "you have no points to bet"
    assert host.wager_calls == [] and stored_hand(host) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!blackjack 0", "minimum bet is 1"),
        ("!blackjack 101", "maximum bet is 100"),
        ("!blackjack " + "9" * 30, "maximum bet is 100"),
    ],
)
def test_bad_stakes_are_refused_without_moving_money(host: _Host, text: str, expected: str) -> None:
    assert expected in (say(host, text) or "")
    assert host.wager_calls == [] and stored_hand(host) is None


def test_insufficient_funds_is_refused_before_anything_is_written(host: _Host) -> None:
    host.eco.balances[ACTOR] = 30
    assert "only have 30" in (say(host, "!blackjack 50") or "")
    assert host.wager_calls == [] and stored_hand(host) is None
    assert not [k for k in host.kv.store if "seen" in k]  # a plain refusal mints no marker


def test_over_the_economy_cap_is_refused(host: _Host) -> None:
    host.eco.max_bet_cap = 20
    assert "max bet is 20" in (say(host, "!blackjack 50") or "")
    assert host.wager_calls == [] and stored_hand(host) is None


def test_unlinked_actor_is_told_to_link(host: _Host) -> None:
    host.ident.actor = None
    assert "link your account" in (say(host, "!blackjack 5") or "")
    assert host.wager_calls == []


def test_identity_outage_is_unavailable_not_a_link_prompt(host: _Host) -> None:
    host.ident.granted = False
    assert say(host, "!blackjack 5") == app._UNAVAILABLE_MSG


def test_economy_unavailable_replies_and_moves_nothing(host: _Host) -> None:
    host.eco.granted = False
    assert say(host, "!blackjack 5") == app._UNAVAILABLE_MSG
    assert stored_hand(host) is None


def test_a_refused_burn_deletes_the_hand_it_wrote_first(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deck(monkeypatch, card("10"), card("6"), card("9"), card("8"))
    host.wager_failure = _WitErr(Error_Denied("rate_limited"))
    assert say(host, "!blackjack 50") == app._UNAVAILABLE_MSG
    assert stored_hand(host) is None
    assert balance(host) == START_BALANCE


def test_a_full_kv_quota_fails_before_any_money_moves(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deck(monkeypatch, card("10"), card("6"), card("9"), card("8"))
    real_set = host.kv.set

    def full(key: str, value: bytes, ttl: int) -> None:
        if "blackjack.hand" in key:
            raise _WitErr(Error_Backend())
        real_set(key, value, ttl)

    monkeypatch.setattr(__import__("wit_world").imports.kv, "set", full)
    with pytest.raises(RuntimeError, match="state backend"):
        say(host, "!blackjack 50")
    assert host.wager_calls == [] and balance(host) == START_BALANCE


def test_dealing_again_while_a_hand_is_open_shows_it_and_moves_nothing(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("10"), card("6"), card("9"), card("8"))
    reply = say(host, "!blackjack 50")
    assert reply is not None and "has a hand open" in reply
    assert host.wager_calls == [(ACTOR, 48, 0)]  # still just the first deal's burn


def test_a_crashed_dealing_record_is_discarded_on_the_next_deal(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = app.Hand(
        stake=50,
        reserve=2,
        player=[card("2"), card("3")],
        dealer=[card("4"), card("5")],
        phase=app.PHASE_DEALING,
        expires_ms=host.now_ms + 60_000,
    )
    host.kv.store[hand_key()] = stale.to_bytes()
    reply = deal(host, monkeypatch, 10, card("10"), card("6"), card("9"), card("8"))
    assert "bets 10" in reply
    hand = stored_hand(host)
    assert hand is not None and hand.phase == app.PHASE_PLAYING and hand.stake == 10


# -- play: outcomes and exact money -------------------------------------------


def test_stand_and_win_pays_double_the_stake(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    deal(host, monkeypatch, 50, card("10"), card("9"), card("10", 1), card("7"))
    reply = say(host, "!stand")
    assert reply is not None and "wins" in reply and "+50" in reply
    assert host.wager_calls[-1] == (ACTOR, 2, 100)
    assert balance(host) == START_BALANCE + 50
    assert stored_hand(host) is None


def test_stand_and_lose_forfeits_the_stake(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    deal(host, monkeypatch, 50, card("10"), card("8"), card("10", 1), card("9"))
    reply = say(host, "!stand")
    assert reply is not None and "loses" in reply and "-50" in reply
    assert host.wager_calls[-1] == (ACTOR, 2, 0)
    assert balance(host) == START_BALANCE - 50
    assert stored_hand(host) is None


def test_push_returns_the_stake(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    deal(host, monkeypatch, 50, card("10"), card("8"), card("9"), card("9", 1))
    reply = say(host, "!stand")
    assert reply is not None and "push" in reply
    assert balance(host) == START_BALANCE


def test_hit_bust_loses_immediately(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    deal(host, monkeypatch, 50, card("10"), card("6"), card("9"), card("8"))
    _deck(monkeypatch, card("10", 1))
    reply = say(host, "!hit")
    assert reply is not None and "busts" in reply and "26" in reply
    assert balance(host) == START_BALANCE - 50
    assert stored_hand(host) is None


def test_hit_then_stand(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    deal(host, monkeypatch, 50, card("5"), card("6"), card("10", 1), card("7"))
    _deck(monkeypatch, card("4"))
    reply = say(host, "!hit")
    assert reply is not None and "hits" in reply and "(15)" in reply
    hand = stored_hand(host)
    assert hand is not None and len(hand.player) == 3 and hand.phase == app.PHASE_PLAYING
    _deck(monkeypatch)
    reply = say(host, "!stand")
    assert reply is not None and "loses" in reply  # 15 vs dealer 17
    assert balance(host) == START_BALANCE - 50


def test_hitting_to_21_stands_automatically_and_the_dealer_plays(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("5"), card("6"), card("10", 1), card("6", 1))
    _deck(monkeypatch, card("10"), card("5", 2))  # player 5+6+10 = 21; dealer 16 draws 5 -> 21
    reply = say(host, "!hit")
    assert reply is not None and "push" in reply and "(21)" in reply
    assert stored_hand(host) is None


def test_dealer_draws_to_seventeen_and_can_bust(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("10"), card("8"), card("5", 1), card("4"))
    _deck(monkeypatch, card("K"))  # dealer 9 -> 19? no: 5+4=9, draws K -> 19, then stands
    reply = say(host, "!stand")
    assert reply is not None and "loses" in reply  # player 18 < 19


def test_dealer_busting_pays_the_player(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    deal(host, monkeypatch, 50, card("10"), card("8"), card("10", 1), card("6"))
    _deck(monkeypatch, card("K"))  # dealer 16 draws a K -> 26
    reply = say(host, "!stand")
    assert reply is not None and "dealer busts" in reply
    assert balance(host) == START_BALANCE + 50


def test_dealer_stands_on_a_soft_seventeen(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    deal(host, monkeypatch, 50, card("10"), card("8"), card("A", 1), card("6"))
    deck = _deck(monkeypatch)  # no draw available: a soft-17 dealer must NOT draw
    reply = say(host, "!stand")
    assert deck.drawn == 0
    assert reply is not None and "wins" in reply  # 18 beats the dealer's soft 17


def test_player_natural_pays_three_to_two(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    reply = deal(host, monkeypatch, 50, card("A"), card("K"), card("9", 1), card("7"))
    assert "BLACKJACK" in reply and "+75" in reply
    assert host.wager_calls == [(ACTOR, 48, 0), (ACTOR, 2, 125)]
    assert balance(host) == START_BALANCE + 75
    assert stored_hand(host) is None


def test_odd_stake_natural_rounds_the_half_point_down(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 5, card("A"), card("K"), card("9", 1), card("7"))
    assert balance(host) == START_BALANCE + 7  # 5 * 3 // 2 = 7


def test_both_naturals_push(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    reply = deal(host, monkeypatch, 50, card("A"), card("K"), card("A", 1), card("Q"))
    assert "push" in reply
    assert balance(host) == START_BALANCE


def test_dealer_natural_ends_the_hand_at_the_deal(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = deal(host, monkeypatch, 50, card("10"), card("9"), card("A", 1), card("K"))
    assert "dealer has blackjack" in reply
    assert balance(host) == START_BALANCE - 50
    assert stored_hand(host) is None


def test_no_hand_to_hit_or_stand(host: _Host) -> None:
    for text in ("!hit", "!stand"):
        reply = say(host, text)
        assert reply is not None and "no open hand" in reply
    assert host.wager_calls == []
    assert not [k for k in host.kv.store if "seen" in k]  # read-only refusal mints no marker


def test_cannot_hit_after_the_hand_has_settled(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("10"), card("9"), card("10", 1), card("7"))
    say(host, "!stand")
    reply = say(host, "!hit")
    assert reply is not None and "no open hand" in reply


def test_bare_blackjack_shows_the_open_hand(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    deal(host, monkeypatch, 50, card("10"), card("6"), card("9", 1), card("8"))
    reply = say(host, "!blackjack")
    assert reply is not None and "has a hand open" in reply and "(16)" in reply


def test_every_stake_settles_a_natural_without_a_deferral(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whole-range check against the host's real caps: no stake can strand a natural."""
    for stake in range(1, app.MANIFEST_MAX_BET + 1):
        h = _install(monkeypatch)
        h.eco.balances[ACTOR] = 10_000
        h.eco.max_bet_cap = 1000
        h.kv.store[community_kv._scoped_key(COMMUNITY, app._cfg_key("maxbet"))] = b"500"
        reply = deal(h, monkeypatch, stake, card("A"), card("K"), card("9", 1), card("7"))
        assert "BLACKJACK" in reply, stake
        assert balance(h) == 10_000 + stake * 3 // 2, stake


# -- replay safety -------------------------------------------------------------


def test_redelivered_deal_burns_and_deals_once(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deck(monkeypatch, card("10"), card("6"), card("9"), card("8"))
    event = _event(host, "!blackjack 50")
    assert _transform(event) is not None
    first_hand = host.kv.store[hand_key()]
    assert _transform(event) is None  # the same message_id again: dropped silently
    assert host.wager_calls == [(ACTOR, 48, 0)]
    assert host.kv.store[hand_key()] == first_hand


def test_redelivered_hit_draws_one_card(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    deal(host, monkeypatch, 50, card("5"), card("6"), card("10", 1), card("7"))
    deck = _deck(monkeypatch, card("2"), card("3"))
    event = _event(host, "!hit")
    assert _transform(event) is not None
    assert _transform(event) is None
    assert deck.drawn == 1
    hand = stored_hand(host)
    assert hand is not None and len(hand.player) == 3


def test_redelivered_stand_pays_once(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    deal(host, monkeypatch, 50, card("10"), card("9"), card("10", 1), card("7"))
    event = _event(host, "!stand")
    assert _transform(event) is not None
    assert _transform(event) is None
    assert len([c for c in host.wager_calls if c[2] > 0]) == 1
    assert balance(host) == START_BALANCE + 50


def test_the_atomic_claim_is_the_guard_when_the_courtesy_read_races(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`already_seen` is advisory: with it blinded, the `claim` increment still stops a replay."""

    async def blind(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(event_dedupe, "already_seen", blind)
    deal(host, monkeypatch, 50, card("5"), card("6"), card("10", 1), card("7"))
    deck = _deck(monkeypatch, card("2"), card("3"))
    event = _event(host, "!hit")
    assert _transform(event) is not None
    assert _transform(event) is None
    assert deck.drawn == 1


def test_a_too_old_event_is_dropped_by_the_age_fence(host: _Host) -> None:
    old = host.stamp(-(event_dedupe.MAX_EVENT_AGE_SECONDS + 60))
    assert _transform(_event(host, "!blackjack 50", occurred_at=old)) is None
    assert host.wager_calls == [] and stored_hand(host) is None


@pytest.mark.parametrize("bad", ["", "not-a-time", None])
def test_a_missing_or_bad_stamp_fails_loud_and_moves_nothing(host: _Host, bad: str | None) -> None:
    event = _event(host, "!blackjack 50")
    event.occurred_at = bad  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="replay-safe"):
        _transform(event)
    assert host.wager_calls == []


def test_a_missing_message_id_fails_loud_and_moves_nothing(host: _Host) -> None:
    with pytest.raises(RuntimeError, match="replay-safe"):
        _transform(_event(host, "!blackjack 50", drop=("message_id",)))
    assert host.wager_calls == [] and stored_hand(host) is None


def test_marker_keys_never_carry_the_message_id_or_a_colon(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("10"), card("6"), card("9", 1), card("8"))
    keys = list(host.kv.store)
    assert any(".seen." in k for k in keys)
    assert not any("msg-" in k or ACTOR in k or ":" in k for k in keys)


# -- concurrency / atomic state transitions ------------------------------------


def _interleave_kv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every kv op yield to the event loop so concurrent coroutines genuinely interleave."""
    for name in ("get", "set", "delete", "increment"):
        original = getattr(kv_module, name)

        async def yielding(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
            await asyncio.sleep(0)
            return await _original(*args, **kwargs)

        monkeypatch.setattr(kv_module, name, yielding)


def _gather(*events: PlatformEvent) -> list[PlatformEvent | None]:
    assert CURRENT is not None
    CURRENT.stage = "process"

    async def go() -> list[PlatformEvent | None]:
        with bundle_context(
            tenant="t", community=COMMUNITY, app_id="waddles.core.example.blackjack"
        ):
            return list(await asyncio.gather(*(transform(e) for e in events)))

    result: list[PlatformEvent | None] = _run(go())
    return result


def test_concurrent_hits_cannot_both_draw(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    deal(host, monkeypatch, 50, card("2"), card("3"), card("10", 1), card("7"))
    deck = _deck(monkeypatch, card("4"), card("5"), card("6"))
    _interleave_kv(monkeypatch)
    out = _gather(_event(host, "!hit"), _event(host, "!hit"))
    texts = sorted(str(o.payload["text"]) for o in out if o is not None)
    assert deck.drawn == 1, "the per-player lock must serialize concurrent hits"
    assert sum(app._BUSY_MSG in t for t in texts) == 1
    hand = stored_hand(host)
    assert hand is not None and len(hand.player) == 3
    assert lock_key() not in host.kv.store  # released after the step


def test_concurrent_stands_pay_once(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    deal(host, monkeypatch, 50, card("10"), card("9"), card("10", 1), card("7"))
    _deck(monkeypatch)
    _interleave_kv(monkeypatch)
    _gather(_event(host, "!stand"), _event(host, "!stand"))
    assert len([c for c in host.wager_calls if c[2] > 0]) == 1
    assert balance(host) == START_BALANCE + 50


def test_concurrent_deals_burn_once(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _deck(monkeypatch, card("10"), card("6"), card("9"), card("8"), card("2"), card("3"))
    _interleave_kv(monkeypatch)
    _gather(_event(host, "!blackjack 50"), _event(host, "!blackjack 50"))
    assert host.wager_calls == [(ACTOR, 48, 0)]


def test_a_held_lock_refuses_the_step_and_leaves_the_hand_alone(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("2"), card("3"), card("10", 1), card("7"))
    before = host.kv.store[hand_key()]
    host.kv.store[lock_key()] = b"1"
    deck = _deck(monkeypatch, card("4"))
    assert say(host, "!hit") == app._BUSY_MSG
    assert deck.drawn == 0 and host.kv.store[hand_key()] == before


def test_the_lock_is_released_even_when_the_step_raises(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("2"), card("3"), card("10", 1), card("7"))

    def boom() -> int:
        raise AssertionError("draw failed")

    monkeypatch.setattr(app, "_draw", boom)
    with pytest.raises(RuntimeError, match="state backend"):
        say(host, "!hit")
    assert lock_key() not in host.kv.store


def test_the_lock_uses_a_short_ttl(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    deal(host, monkeypatch, 50, card("2"), card("3"), card("10", 1), card("7"))
    locks = [c for c in host.kv.calls if c[0] == "increment" and ".lock." in c[1][0]]
    assert locks and all(c[1][2] == app.LOCK_TTL_SECONDS for c in locks)


# -- abandoned hands -----------------------------------------------------------


def test_an_abandoned_hand_is_forfeited_on_the_next_hit(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("2"), card("3"), card("10", 1), card("7"))
    host.now_ms += (app.DEFAULT_TIMEOUT_SECONDS + 1) * 1000
    deck = _deck(monkeypatch, card("4"))
    reply = say(host, "!hit")
    assert reply is not None and "timed out and was forfeited" in reply
    assert stored_hand(host) is None and deck.drawn == 0
    assert balance(host) == START_BALANCE - 48 + 0  # the escrowed burn stands: no payout


def test_an_abandoned_hand_forfeit_moves_no_money(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("2"), card("3"), card("10", 1), card("7"))
    calls = list(host.wager_calls)
    host.now_ms += (app.DEFAULT_TIMEOUT_SECONDS + 1) * 1000
    say(host, "!stand")
    assert host.wager_calls == calls


def test_an_expired_hand_does_not_block_a_new_deal(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("2"), card("3"), card("10", 1), card("7"))
    host.now_ms += (app.DEFAULT_TIMEOUT_SECONDS + 1) * 1000
    reply = deal(host, monkeypatch, 10, card("10"), card("6"), card("9"), card("8"))
    assert "timed out and was forfeited" in reply and "bets 10" in reply


def test_bare_blackjack_reports_an_expired_hand_without_mutating(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("2"), card("3"), card("10", 1), card("7"))
    host.now_ms += (app.DEFAULT_TIMEOUT_SECONDS + 1) * 1000
    reply = say(host, "!blackjack")
    assert reply is not None and "forfeited" in reply


def test_a_hand_still_inside_the_timeout_plays_on(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("2"), card("3"), card("10", 1), card("7"))
    host.now_ms += (app.DEFAULT_TIMEOUT_SECONDS - 5) * 1000
    _deck(monkeypatch, card("4"))
    reply = say(host, "!hit")
    assert reply is not None and "hits" in reply


def test_the_hand_key_ttl_covers_the_timeout_plus_grace(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("2"), card("3"), card("10", 1), card("7"))
    ttls = [c[1][2] for c in host.kv.calls if c[0] == "set" and ".hand." in c[1][0]]
    assert ttls[-1] == app.DEFAULT_TIMEOUT_SECONDS + app.HAND_TTL_GRACE_SECONDS


def test_the_timeout_is_mod_configurable(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    assert say(host, "!blackjack set timeout 60", is_mod=True) == "blackjack timeout set to 60"
    deal(host, monkeypatch, 50, card("2"), card("3"), card("10", 1), card("7"))
    host.now_ms += 61_000
    reply = say(host, "!hit")
    assert reply is not None and "forfeited" in reply


# -- payouts the host refuses (deferred / ambiguous) ---------------------------


def _winning_hand(host: _Host, mp: pytest.MonkeyPatch) -> None:
    deal(host, mp, 50, card("10"), card("9"), card("10", 1), card("7"))
    _deck(mp)


def test_a_refused_payout_keeps_the_won_hand_due_and_it_can_be_collected(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _winning_hand(host, monkeypatch)
    host.wager_failure = _WitErr(Error_Denied("quota_exceeded"))
    host.wager_failure_on = {2}  # the settlement wager (the deal's burn was call 1)
    reply = say(host, "!stand")
    assert reply is not None and "daily payout limit reached" in reply
    hand = stored_hand(host)
    assert hand is not None and hand.phase == app.PHASE_DUE and hand.due == 100
    ttls = [c[1][2] for c in host.kv.calls if c[0] == "set" and ".hand." in c[1][0]]
    assert ttls[-1] == app.DUE_TTL_SECONDS
    assert balance(host) == START_BALANCE - 48  # nothing was paid

    host.wager_failure = None
    reply = say(host, "!blackjack")
    assert reply is not None and "wins" in reply
    assert balance(host) == START_BALANCE + 50
    assert stored_hand(host) is None
    assert say(host, "!blackjack") == app._USAGE  # collected: nothing left to pay twice


def test_hit_and_stand_point_at_a_due_payout(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _winning_hand(host, monkeypatch)
    host.wager_failure = _WitErr(Error_Denied("quota_exceeded"))
    host.wager_failure_on = {2}
    say(host, "!stand")
    for text in ("!hit", "!stand"):
        reply = say(host, text)
        assert reply is not None and "awaiting payout" in reply


def test_a_new_deal_collects_a_due_payout_first(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _winning_hand(host, monkeypatch)
    host.wager_failure = _WitErr(Error_Denied("quota_exceeded"))
    host.wager_failure_on = {2}
    say(host, "!stand")
    host.wager_failure = None
    reply = say(host, "!blackjack 10")
    assert reply is not None and "wins" in reply and "bets" not in reply
    assert balance(host) == START_BALANCE + 50


def test_spending_the_reserve_away_only_forfeits_the_winnings(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("10"), card("9"), card("10", 1), card("7"))
    _deck(monkeypatch)
    host.eco.balances[ACTOR] = 0  # the player moved everything away mid-hand
    reply = say(host, "!stand")
    assert reply is not None and "keep a few points" in reply
    hand = stored_hand(host)
    assert hand is not None and hand.phase == app.PHASE_DUE
    assert balance(host) == 0


def test_a_loss_with_the_reserve_spent_costs_the_house_at_most_the_reserve(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("10"), card("8"), card("10", 1), card("9"))
    _deck(monkeypatch)
    burned = START_BALANCE - balance(host)
    host.eco.balances[ACTOR] = 0
    reply = say(host, "!stand")
    assert reply is not None and "loses" in reply and "Balance" not in reply
    assert stored_hand(host) is None
    assert burned == 50 - app._reserve(50)  # only the reserve escaped the burn


def test_an_ambiguous_backend_failure_is_never_retried_or_double_paid(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _winning_hand(host, monkeypatch)
    host.wager_failure = _WitErr(Error_Backend())
    host.wager_failure_on = {2}
    reply = say(host, "!stand")
    assert reply is not None and "payout status is unclear" in reply
    assert stored_hand(host) is None  # deleted BEFORE the wager: at-most-once
    calls = len(host.wager_calls)
    assert say(host, "!blackjack") == app._USAGE
    assert len(host.wager_calls) == calls  # nothing retried
    assert "blackjack.payout_ambiguous" in host.logged()


def test_a_crash_between_delete_and_wager_loses_the_payout_never_repeats_it(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _winning_hand(host, monkeypatch)
    host.wager_failure = _Crash("process killed")  # not an Exception: nothing can handle it
    host.wager_failure_on = {2}
    with pytest.raises(_Crash):
        say(host, "!stand")
    assert stored_hand(host) is None  # no payable record survives the crash
    host.wager_failure = None
    assert say(host, "!blackjack") == app._USAGE
    assert balance(host) == START_BALANCE - 48


def test_a_failing_due_store_is_reported_loudly(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _winning_hand(host, monkeypatch)
    host.wager_failure = _WitErr(Error_Denied("quota_exceeded"))
    host.wager_failure_on = {2}
    real_set = host.kv.set
    wit_kv = __import__("wit_world").imports.kv

    def failing(key: str, value: bytes, ttl: int) -> None:
        if "blackjack.hand" in key:
            raise _WitErr(Error_Backend())
        real_set(key, value, ttl)

    monkeypatch.setattr(wit_kv, "set", failing)
    reply = say(host, "!stand")
    assert reply is not None and "ask a moderator" in reply
    assert "blackjack.due_store_failed" in host.logged()


def test_a_corrupt_hand_is_treated_as_absent_and_logged(host: _Host) -> None:
    host.kv.store[hand_key()] = b"{not json"
    reply = say(host, "!hit")
    assert reply is not None and "no open hand" in reply
    assert "blackjack.hand_corrupt" in host.logged()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(s=0),
        lambda d: d.update(s=app.MANIFEST_MAX_BET + 1),
        lambda d: d.update(ph="weird"),
        lambda d: d.update(p=[99, 1]),
        lambda d: d.update(p=[]),
        lambda d: d.update(v=2),
        lambda d: d.update(t=-1),
        lambda d: d.pop("x"),
    ],
)
def test_out_of_range_hand_records_are_rejected(host: _Host, mutate: Any) -> None:
    good = json.loads(
        app.Hand(
            stake=50,
            reserve=2,
            player=[1, 2],
            dealer=[3, 4],
            phase=app.PHASE_PLAYING,
            expires_ms=host.now_ms + 1000,
        ).to_bytes()
    )
    mutate(good)
    assert app._hand_from_bytes(json.dumps(good).encode()) is None


# -- mod config ----------------------------------------------------------------


def test_maxbet_and_minbet_apply_to_the_next_deal(host: _Host) -> None:
    assert say(host, "!blackjack set maxbet 20", is_mod=True) == "blackjack maxbet set to 20"
    assert say(host, "!blackjack set minbet 5", is_broadcaster=True) == "blackjack minbet set to 5"
    assert "maximum bet is 20" in (say(host, "!blackjack 50") or "")
    assert "minimum bet is 5" in (say(host, "!blackjack 2") or "")


@pytest.mark.parametrize("badge", [{}, {"is_mod": False}, {"is_mod": "true"}, {"is_mod": 1}])
def test_set_is_denied_without_a_strict_true_badge(host: _Host, badge: dict[str, Any]) -> None:
    assert say(host, "!blackjack set maxbet 20", **badge) == app._PERMISSION_DENIED_MSG
    assert say(host, "!blackjack 100") is not None
    assert not [k for k in host.kv.store if "cfg" in k]


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("maxbet 501", "between 1 and 500"),
        ("maxbet 0", "between 1 and 500"),
        ("timeout 5", "timeout must be"),
        ("timeout 99999", "timeout must be"),
        ("maxbet abc", "isn't a whole number"),
        ("maxbet", app._USAGE),
        ("color red", app._USAGE),
    ],
)
def test_set_validation(host: _Host, arg: str, expected: str) -> None:
    assert expected in (say(host, f"!blackjack set {arg}", is_mod=True) or "")


def test_minbet_cannot_exceed_maxbet_and_vice_versa(host: _Host) -> None:
    say(host, "!blackjack set maxbet 20", is_mod=True)
    assert "cannot exceed" in (say(host, "!blackjack set minbet 30", is_mod=True) or "")
    say(host, "!blackjack set minbet 10", is_mod=True)
    assert "cannot be below" in (say(host, "!blackjack set maxbet 5", is_mod=True) or "")


def test_a_hostile_set_value_is_not_echoed_live(host: _Host) -> None:
    reply = say(host, "!blackjack set maxbet {user:evil}", is_mod=True) or ""
    assert "{user:evil}" not in reply


def test_corrupt_limits_fall_back_to_the_defaults(host: _Host) -> None:
    for name in ("minbet", "maxbet", "timeout"):
        host.kv.store[community_kv._scoped_key(COMMUNITY, app._cfg_key(name))] = b"junk"
    limits = _run(app._get_limits(COMMUNITY))
    assert limits == app.Limits()


def test_the_declared_manifest_bound_is_the_hard_ceiling_for_mod_limits(host: _Host) -> None:
    host.kv.store[community_kv._scoped_key(COMMUNITY, app._cfg_key("maxbet"))] = b"9999"
    assert _run(app._get_limits(COMMUNITY)).max_bet == app.DEFAULT_MAX_BET


# -- output safety / privacy ---------------------------------------------------


def test_the_minted_actor_token_stays_live_in_replies(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = deal(host, monkeypatch, 50, card("10"), card("6"), card("9", 1), card("8"))
    assert ACTOR_TOKEN in reply


@pytest.mark.parametrize("hostile", ["@everyone", "<@12345>", "name {user:forged}", "evil\x00name"])
def test_a_raw_actor_is_sanitized(
    host: _Host, monkeypatch: pytest.MonkeyPatch, hostile: str
) -> None:
    _deck(monkeypatch, card("10"), card("6"), card("9"), card("8"))
    reply = say(host, "!blackjack 10", actor=hostile) or ""
    assert "@everyone" not in reply and "<@12345>" not in reply
    assert "{user:forged}" not in reply and "\x00" not in reply


def test_a_missing_actor_falls_back_to_a_neutral_label(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deck(monkeypatch, card("10"), card("6"), card("9"), card("8"))
    assert "someone" in (say(host, "!blackjack 10", actor=None) or "")


def test_logs_and_keys_carry_no_cards_actor_or_message_ids(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("A"), card("K"), card("9", 1), card("7"))  # natural -> settle
    deal(host, monkeypatch, 40, card("10"), card("6"), card("9", 1), card("8"))
    _deck(monkeypatch, card("10", 1))
    say(host, "!hit")
    blob = host.logged()
    for needle in (ACTOR, ACTOR_TOKEN, "tok-1", "msg-", *"♠♥♦♣", "12345"):
        assert needle not in blob, needle
    for key in host.kv.store:
        assert ACTOR not in key and ":" not in key and "tok-1" not in key


def test_a_full_hand_stays_inside_the_kv_op_budget(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("2"), card("3"), card("10", 1), card("7"))
    for _ in range(3):
        _deck(monkeypatch, card("2"))
        host.kv.calls.clear()
        say(host, "!hit")
        assert len(host.kv.calls) <= 64


# -- dispatch ------------------------------------------------------------------


def _envelope(platform: str = "twitch", **payload: Any) -> StageEnvelope:
    return StageEnvelope(
        tenant="t",
        community=COMMUNITY,
        app_id="waddles.core.example.blackjack",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor=ACTOR_TOKEN,
            payload=payload,
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_relays_the_prebuilt_reply(host: _Host, platform: str) -> None:
    host.stage = "action"
    result = _run(
        dispatch(_envelope(platform, text="hello", channel_id="999"), {}, http_client=None)
    )
    assert result.transport == platform and result.detail == "relayed"
    assert host.relay_calls == [
        (platform, json.dumps({"channel": "999", "text": "hello"}, sort_keys=True))
    ]


def test_dispatch_guards(host: _Host) -> None:
    host.stage = "action"
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope(text="x"), {}, http_client=None))
    for text in ("", None, 5):
        with pytest.raises(ValueError, match="non-empty text"):
            _run(dispatch(_envelope(text=text, channel_id="1"), {}, http_client=None))
    assert host.relay_calls == []


def test_end_to_end_transform_then_dispatch(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _deck(monkeypatch, card("10"), card("6"), card("9"), card("8"))
    out = _transform(_event(host, "!blackjack 50", platform="discord"))
    assert out is not None
    env = StageEnvelope(
        tenant="t",
        community=COMMUNITY,
        app_id="waddles.core.example.blackjack",
        stage="action",
        event=out,
        ts="x",
    )
    host.stage = "action"
    _run(dispatch(env, {}, http_client=None))
    provider, message = host.relay_calls[-1]
    assert provider == "discord"
    assert json.loads(message)["channel"] == "12345"
    assert "bets 50" in json.loads(message)["text"]


def test_a_deferred_push_says_the_stake_comes_back(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("10"), card("8"), card("9"), card("9", 1))
    _deck(monkeypatch)
    host.wager_failure = _WitErr(Error_Denied("quota_exceeded"))
    host.wager_failure_on = {2}
    reply = say(host, "!stand")
    assert reply is not None and "your stake comes back" in reply
    hand = stored_hand(host)
    assert hand is not None and hand.phase == app.PHASE_DUE and hand.due == 50


@dataclass
class Error_InsufficientFunds:  # noqa: N801 -- mirrors the generated WIT case-class naming
    """Stand-in for the generated `Error_InsufficientFunds` case."""

    value: int


@dataclass
class Error_OverCap:  # noqa: N801 -- mirrors the generated WIT case-class naming
    """Stand-in for the generated `Error_OverCap` case."""

    value: int


def test_a_burn_refused_for_funds_deletes_the_hand_and_says_so(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deck(monkeypatch, card("10"), card("6"), card("9"), card("8"))
    host.wager_failure = _WitErr(Error_InsufficientFunds(7))  # spent between the check and the burn
    reply = say(host, "!blackjack 50")
    assert reply is not None and "only have 7" in reply
    assert stored_hand(host) is None


def test_a_burn_refused_over_the_cap_deletes_the_hand_and_says_so(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deck(monkeypatch, card("10"), card("6"), card("9"), card("8"))
    host.wager_failure = _WitErr(Error_OverCap(20))
    reply = say(host, "!blackjack 50")
    assert reply is not None and "max bet is 20" in reply
    assert stored_hand(host) is None


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (_WitErr(Error_OverCap(5)), "over the payout cap"),
        (_WitErr(Error_Denied("rate_limited")), "the economy refused it"),
        (_WitErr(Error_InsufficientFunds(0)), "keep a few points"),
    ],
)
def test_deferred_payout_reasons_are_static_and_user_safe(
    host: _Host, monkeypatch: pytest.MonkeyPatch, failure: Exception, reason: str
) -> None:
    _winning_hand(host, monkeypatch)
    host.wager_failure = failure
    host.wager_failure_on = {2}
    reply = say(host, "!stand")
    assert reply is not None and reason in reply
    hand = stored_hand(host)
    assert hand is not None and hand.phase == app.PHASE_DUE


def test_a_stale_hit_is_dropped_before_any_work(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("2"), card("3"), card("10", 1), card("7"))
    deck = _deck(monkeypatch, card("4"))
    old = host.stamp(-(event_dedupe.MAX_EVENT_AGE_SECONDS + 60))
    assert _transform(_event(host, "!hit", occurred_at=old)) is None
    assert deck.drawn == 0


def test_the_claim_alone_stops_a_replayed_deal(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A natural settles at the deal (no hand left to see), so only the claim can stop a replay."""

    async def blind(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(event_dedupe, "already_seen", blind)
    _deck(monkeypatch, card("A"), card("K"), card("9"), card("7"))
    event = _event(host, "!blackjack 50")
    assert _transform(event) is not None
    assert balance(host) == START_BALANCE + 75
    assert _transform(event) is None
    assert host.wager_calls == [(ACTOR, 48, 0), (ACTOR, 2, 125)]  # one burn, one settlement
    assert balance(host) == START_BALANCE + 75


def _due_hand(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _winning_hand(host, monkeypatch)
    host.wager_failure = _WitErr(Error_Denied("quota_exceeded"))
    host.wager_failure_on = {2}
    say(host, "!stand")
    host.wager_failure = None


def test_collecting_a_due_hand_never_pays_twice(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _due_hand(host, monkeypatch)
    event = _event(host, "!blackjack")
    assert _transform(event) is not None
    _transform(event)  # the same message again: the hand is gone, so nothing more is paid
    assert len([c for c in host.wager_calls if c[2] > 0]) == 2  # the won hand's settlement tries
    assert balance(host) == START_BALANCE + 50


def test_a_redelivered_collect_that_did_not_pay_is_dropped(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _due_hand(host, monkeypatch)
    host.wager_failure = _WitErr(Error_Denied("quota_exceeded"))  # the collect is refused again
    host.wager_failure_on = set()  # ... on every call
    event = _event(host, "!blackjack")
    first = _transform(event)
    assert first is not None and "daily payout limit" in first.payload["text"]
    assert stored_hand(host) is not None  # still due
    assert _transform(event) is None  # the redelivery is recognised and dropped silently


def test_a_stale_collect_is_dropped(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _due_hand(host, monkeypatch)
    old = host.stamp(-(event_dedupe.MAX_EVENT_AGE_SECONDS + 60))
    assert _transform(_event(host, "!blackjack", occurred_at=old)) is None
    assert stored_hand(host) is not None


def test_the_claim_alone_stops_a_concurrent_duplicate_collect(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def blind(*args: Any, **kwargs: Any) -> bool:
        return False

    _due_hand(host, monkeypatch)
    payouts_before = len([c for c in host.wager_calls if c[2] > 0])
    monkeypatch.setattr(event_dedupe, "already_seen", blind)
    _interleave_kv(monkeypatch)
    event = _event(host, "!blackjack")
    results = _gather(event, event)  # both read the due hand before either claims
    assert sorted(r is None for r in results) == [False, True]
    assert len([c for c in host.wager_calls if c[2] > 0]) == payouts_before + 1
    assert balance(host) == START_BALANCE + 50


def test_a_lock_release_failure_is_logged_and_never_masks_the_reply(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    deal(host, monkeypatch, 50, card("2"), card("3"), card("10", 1), card("7"))
    wit_kv = __import__("wit_world").imports.kv
    real_delete = wit_kv.delete

    def flaky(key: str) -> None:
        if ".lock." in key:
            raise _WitErr(Error_Backend())
        real_delete(key)

    monkeypatch.setattr(wit_kv, "delete", flaky)
    _deck(monkeypatch, card("4"))
    reply = say(host, "!hit")
    assert reply is not None and "hits" in reply
    assert "blackjack.lock_release_failed" in host.logged()


async def _states(*states: app.Hand | None) -> Any:
    """A `_load_hand` stand-in answering `states` in order (the last one repeats)."""
    queue = list(states)

    async def fake(community: str, pseudonym: str) -> app.Hand | None:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return fake


def _open_hand(host: _Host, phase: str = app.PHASE_PLAYING) -> app.Hand:
    return app.Hand(
        stake=50,
        reserve=2,
        player=[card("2"), card("3")],
        dealer=[card("10"), card("7")],
        phase=phase,
        expires_ms=host.now_ms + 60_000,
        due=100 if phase == app.PHASE_DUE else 0,
    )


def test_a_hand_that_appears_between_the_check_and_the_lock_blocks_the_deal(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _run(_states(None, _open_hand(host)))
    monkeypatch.setattr(app, "_load_hand", fake)
    reply = say(host, "!blackjack 50")
    assert reply is not None and "has a hand open" in reply
    assert host.wager_calls == []


def test_a_due_hand_that_vanishes_before_the_lock_is_a_retry_not_a_deal(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _run(_states(_open_hand(host, app.PHASE_DUE), None))
    monkeypatch.setattr(app, "_load_hand", fake)
    assert say(host, "!blackjack 50") == app._BUSY_MSG
    assert host.wager_calls == []


def test_a_hand_that_vanishes_between_the_check_and_the_lock_is_no_hand(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _run(_states(_open_hand(host), None))
    monkeypatch.setattr(app, "_load_hand", fake)
    reply = say(host, "!hit")
    assert reply is not None and "no open hand" in reply


def test_a_due_hand_that_vanishes_before_a_collect_is_just_usage(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _run(_states(_open_hand(host, app.PHASE_DUE), None))
    monkeypatch.setattr(app, "_load_hand", fake)
    assert say(host, "!blackjack") == app._USAGE


@pytest.mark.parametrize(
    ("amount", "bucket"),
    [(1, "le10"), (10, "le10"), (11, "le100"), (500, "le1000"), (5000, "gt1000")],
)
def test_amount_buckets(amount: int, bucket: str) -> None:
    assert app._amount_bucket(amount) == bucket


# -- security review regressions: economy integrity ---------------------------------------------

_GRID_STAKES = sorted(
    {*range(1, 61), 99, 100, 101, 119, 120, 121, 199, 200, 201, 250, 333, 399, 400, 401, 499, 500}
)
_P_WIN = ("10", "9"), ("10", "7")  # 19 v 17
_P_LOSE = ("10", "8"), ("10", "9")  # 18 v 19
_P_PUSH = ("10", "8"), ("9", "9")  # 18 v 18
_P_DEALER_BUST = ("10", "9"), ("10", "6")  # dealer draws a 9 -> 25


def _grid_cards(outcome: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """`(player, dealer, extra draws)` ranks scripting one outcome of a settled hand."""
    return {
        "win": (*_P_WIN, ()),
        "lose": (*_P_LOSE, ()),
        "push": (*_P_PUSH, ()),
        "dealer_bust": (*_P_DEALER_BUST, ("9",)),
        "bust": (("10", "6"), ("5", "5"), ("10",)),
        "natural": (("A", "K"), ("9", "7"), ()),
        "natural_push": (("A", "K"), ("A", "Q"), ()),
        "dealer_natural": (("10", "9"), ("A", "K"), ()),
    }[outcome]


def _expected_return(outcome: str, stake: int) -> int:
    return {
        "win": 2 * stake,
        "dealer_bust": 2 * stake,
        "push": stake,
        "natural_push": stake,
        "natural": stake + stake * 3 // 2,
        "lose": 0,
        "bust": 0,
        "dealer_natural": 0,
    }[outcome]


def _play_out(host: _Host, mp: pytest.MonkeyPatch, stake: int, outcome: str) -> None:
    player, dealer, draws = _grid_cards(outcome)
    cards = [card(player[0]), card(player[1]), card(dealer[0], 1), card(dealer[1])]
    cards += [card(rank, 2) for rank in draws]
    _deck(mp, *cards)
    assert say(host, f"!blackjack {stake}") is not None
    if outcome == "bust":
        assert say(host, "!hit") is not None
    elif outcome not in ("natural", "natural_push", "dealer_natural"):
        assert say(host, "!stand") is not None


_OUTCOMES = (
    "win",
    "lose",
    "push",
    "dealer_bust",
    "bust",
    "natural",
    "natural_push",
    "dealer_natural",
)


def _set_max_bet(host: _Host, value: int) -> None:
    host.kv.store[community_kv._scoped_key(COMMUNITY, app._cfg_key("maxbet"))] = str(value).encode()


@pytest.mark.parametrize("outcome", _OUTCOMES)
@pytest.mark.parametrize("stake", _GRID_STAKES)
def test_every_hand_nets_exactly_return_minus_stake_and_never_mints_past_the_cap(
    host: _Host, monkeypatch: pytest.MonkeyPatch, stake: int, outcome: str
) -> None:
    """Proof over stake x outcome: money is conserved, nothing is paid twice, the cap holds.

    Whatever the stake (the integer rounding of the 3:2 natural and of the reserve is the risky
    part), the player ends with exactly `start - stake + total_return`; the wagers' debits sum to
    the stake (escrow + settlement) and their credits to the return; no single payout is above
    100 x its own stake (the host's mint bound); nothing is paid after the hand is settled.
    """
    host.eco.balances[ACTOR] = 100_000
    _set_max_bet(host, 500)
    start = balance(host)
    _play_out(host, monkeypatch, stake, outcome)
    total_return = _expected_return(outcome, stake)

    assert balance(host) == start - stake + total_return
    debits = sum(call[1] for call in host.wager_calls)
    credits = sum(call[2] for call in host.wager_calls)
    assert debits == stake
    assert credits == total_return
    for _user, wager_stake, payout in host.wager_calls:
        assert wager_stake >= 1
        assert payout <= wager_stake * app.economy.MAX_PAYOUT_MULTIPLE
    assert total_return <= app._top_return(stake) <= 2.5 * stake
    assert stored_hand(host) is None  # settled: nothing left to collect or replay

    calls = len(host.wager_calls)
    say(host, "!stand")
    say(host, "!hit")
    say(host, "!blackjack")
    assert len(host.wager_calls) == calls  # no further money moves, whatever the player sends
    assert balance(host) == start - stake + total_return


@pytest.mark.parametrize("stake", _GRID_STAKES)
def test_the_return_never_exceeds_what_the_settlement_stake_may_pay(stake: int) -> None:
    reserve = app._reserve(stake)
    assert 1 <= reserve <= stake
    assert app._top_return(stake) <= reserve * app.economy.MAX_PAYOUT_MULTIPLE
    for ret in (2 * stake, stake, stake + stake * 3 // 2, 0):
        assert ret <= app._top_return(stake)


def _stored(host: _Host, **overrides: Any) -> bytes:
    hand = app.Hand(
        stake=50,
        reserve=app._reserve(50),
        player=[card("10"), card("9")],
        dealer=[card("10", 1), card("7")],
        phase=app.PHASE_DUE,
        expires_ms=0,
        due=100,
    )
    for key, value in overrides.items():
        setattr(hand, key, value)
    return hand.to_bytes()


@pytest.mark.parametrize(
    "overrides",
    [
        {"due": app._top_return(50) + 1},  # more than any hand of this stake can return
        {"due": 10**9},
        {"due": 0},  # a `due` hand with nothing due
        {"phase": app.PHASE_PLAYING, "due": 100},  # only a settled-but-unpaid hand carries a payout
        {"phase": app.PHASE_DEALING, "due": 1},
        {"stake": 501},
        {"reserve": 0},
        {"due": -1},
    ],
)
def test_a_stored_hand_asking_for_more_than_it_can_return_is_corrupt_not_payable(
    host: _Host, overrides: dict[str, Any]
) -> None:
    # regression: only `due >= 0` was validated, so a corrupt/tampered record could have asked the
    # host for any payout up to `reserve x 100`.
    host.kv.store[hand_key()] = _stored(host, **overrides)
    before = balance(host)
    assert say(host, "!blackjack") == app._USAGE  # treated as no hand
    assert host.wager_calls == []
    assert balance(host) == before
    assert "blackjack.hand_corrupt" in host.logged()


def test_a_legitimate_due_hand_is_still_collected_in_full(host: _Host) -> None:
    host.kv.store[hand_key()] = _stored(host, due=app._top_return(50))
    before = balance(host)
    reply = say(host, "!blackjack")
    assert reply is not None and "Balance" in reply
    assert balance(host) == before - app._reserve(50) + app._top_return(50)
    assert stored_hand(host) is None


@pytest.mark.parametrize(
    "padded", [" {user:tok-1}", "{user:tok-1} ", "x{user:tok-1}", "{user:tok-1}\n"]
)
def test_a_padded_actor_is_never_left_live(padded: str) -> None:
    assert app._actor_label("{user:tok-1}") == "{user:tok-1}"
    assert "\\{user:tok-1" in app._actor_label(padded)
