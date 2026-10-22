"""Host-native tests for the `bingo` bundle -- fake `wit_world` (kv/relay/log/clock).

No WASM/wasmtime. The fake `kv` is the SDK's `FakeKvHost` (validates keys like the real host,
`scope.rs::is_allowed_key_byte`, gh-631; `increment` is the atomic compare-and-swap) behind a
failure-injection wrapper so the compensation paths run for real.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from waddle_sdk.flask_core.bundle_runtime import bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost

from app import (
    _LINES,
    NUMBERS,
    DispatchResult,
    _letter,
    _render_card,
    _Round,
    _StateCorruptError,
    call_order,
    dispatch,
    has_bingo,
    make_card,
    transform,
)

COMMUNITY = "comm-1"
ZWSP = chr(0x200B)
BACKSLASH = chr(92)
CUR_KEY = f"c.{COMMUNITY}.bingo.cur"
CALLS_KEY = f"c.{COMMUNITY}.bingo.calls.1"
WON_KEY = f"c.{COMMUNITY}.bingo.won.1"
WINNER_KEY = f"c.{COMMUNITY}.bingo.winner.1"
BUNDLE_DIR = Path(__file__).resolve().parent.parent


class _HostKvError(Exception):
    """Shaped like the generated WIT `Err` (`.value` holds the error union)."""

    def __init__(self, value: Any) -> None:
        super().__init__("host kv error")
        self.value = value


class Error_Backend:  # noqa: N801 -- mirrors the generated WIT case-class naming
    """Stand-in for the generated `Error_Backend` case."""


class _FailableKv:
    """`FakeKvHost` plus scripted failures: `fail.append([op, key_substring, times])`."""

    def __init__(self) -> None:
        self.inner = FakeKvHost()
        self.fail: list[list[Any]] = []

    @property
    def store(self) -> dict[str, bytes]:
        return self.inner.store

    @property
    def calls(self) -> list[tuple[str, tuple[Any, ...]]]:
        return self.inner.calls

    def _maybe_fail(self, op: str, key: str) -> None:
        for rule in self.fail:
            if rule[0] == op and rule[1] in key and rule[2] > 0:
                rule[2] -= 1
                raise _HostKvError(Error_Backend())

    def get(self, key: str) -> bytes | None:
        self._maybe_fail("get", key)
        return self.inner.get(key)

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        self._maybe_fail("set", key)
        self.inner.set(key, value, ttl_seconds)

    def delete(self, key: str) -> None:
        self._maybe_fail("delete", key)
        self.inner.delete(key)

    def increment(self, key: str, delta: int, ttl_seconds: int) -> int:
        self._maybe_fail("increment-" if delta < 0 else "increment", key)
        return self.inner.increment(key, delta, ttl_seconds)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Fake WIT host: empty kv, recording relay/log, settable clock."""
    fake_kv = _FailableKv()
    relay_calls: list[tuple[str, str]] = []
    log_calls: list[tuple[int, str, str]] = []
    state: dict[str, Any] = {"now": 1_000_000}

    imports = types.SimpleNamespace(
        kv=fake_kv,
        relay=types.SimpleNamespace(push=lambda provider, msg: relay_calls.append((provider, msg))),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, fields_json: log_calls.append((lvl, msg, fields_json)),
        ),
        clock=types.SimpleNamespace(now_millis=lambda: state["now"]),
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = imports  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return types.SimpleNamespace(
        kv=fake_kv, relay_calls=relay_calls, log_calls=log_calls, state=state
    )


def _event(
    text: Any,
    *,
    actor: str | None = "{user:u-alice}",
    author_id: str | None = None,
    platform: str = "twitch",
    extra: dict[str, Any] | None = None,
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": "chan-1"}
    if author_id is not None:
        payload["author_id"] = author_id
    payload.update(extra or {})
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at="2026-10-22T00:00:00.000Z",
    )


def _mod(text: str) -> PlatformEvent:
    return _event(text, actor="{user:u-mod}", extra={"is_mod": True, "is_broadcaster": False})


def _viewer(text: str, name: str = "alice") -> PlatformEvent:
    return _event(text, actor="{user:u-" + name + "}")


def _run(event: PlatformEvent, *, community: str | None = COMMUNITY) -> PlatformEvent | None:
    async def _go() -> PlatformEvent | None:
        with bundle_context(tenant="tenant-1", community=community, app_id="bingo"):
            return await transform(event)

    return asyncio.run(_go())


def _text(result: PlatformEvent | None) -> str:
    assert result is not None
    return str(result.payload["text"])


def _say(line: str, name: str = "alice") -> str:
    return _text(_run(_viewer(line, name)))


def _modsay(line: str) -> str:
    return _text(_run(_mod(line)))


def _logged(host: types.SimpleNamespace) -> str:
    return " ".join(f"{msg} {fields}" for _lvl, msg, fields in host.log_calls)


def _who(name: str, platform: str = "twitch") -> str:
    return hashlib.sha256(f"{platform}\0{{user:u-{name}}}".encode()).hexdigest()


def _round(host: types.SimpleNamespace) -> _Round:
    return _Round.load(host.kv.store[CUR_KEY])


def _calls_needed(seed: str, who: str) -> int:
    """Smallest number of calls after which `who`'s card holds a line."""
    card = make_card(seed, who)
    called: set[int] = set()
    for count, number in enumerate(call_order(seed), 1):
        called.add(number)
        if has_bingo(card, called):
            return count
    raise AssertionError("a card always completes by 75 calls")


def _set_calls(host: types.SimpleNamespace, count: int) -> None:
    host.kv.store[CALLS_KEY] = str(count).encode()


def test_a_card_is_5x5_with_a_free_centre_and_column_ranges() -> None:
    card = make_card("a" * 32, _who("alice"))
    assert len(card) == 5
    assert all(len(row) == 5 for row in card)
    assert card[2][2] == 0
    for col in range(5):
        for row in range(5):
            if (row, col) != (2, 2):
                assert 15 * col < card[row][col] <= 15 * (col + 1)
    numbers = [n for row in card for n in row if n]
    assert len(numbers) == 24
    assert len(set(numbers)) == 24


def test_cards_are_deterministic_and_vary_by_seed_and_viewer() -> None:
    seed = "b" * 32
    assert make_card(seed, _who("alice")) == make_card(seed, _who("alice"))
    cards = {json.dumps(make_card(seed, _who(f"u{i}"))) for i in range(40)}
    assert len(cards) == 40
    assert make_card(seed, _who("alice")) != make_card("c" * 32, _who("alice"))


def test_the_call_order_is_a_fixed_permutation_of_1_to_75() -> None:
    order = call_order("d" * 32)
    assert sorted(order) == list(range(1, NUMBERS + 1))
    assert order == call_order("d" * 32)
    assert order != call_order("e" * 32)


@pytest.mark.parametrize("line", _LINES, ids=[f"line{i}" for i in range(12)])
def test_every_one_of_the_12_lines_wins(line: tuple[tuple[int, int], ...]) -> None:
    card = make_card("f" * 32, _who("alice"))
    called = {card[r][c] for r, c in line if card[r][c]}
    assert has_bingo(card, called)


@pytest.mark.parametrize("line", _LINES, ids=[f"line{i}" for i in range(12)])
def test_a_line_missing_one_square_does_not_win(line: tuple[tuple[int, int], ...]) -> None:
    card = make_card("f" * 32, _who("alice"))
    cells = [card[r][c] for r, c in line if card[r][c]]
    # One real number short of the line (a centre line only holds 4 real numbers): at most 4
    # called numbers, and two lines share at most one square, so NO line is complete.
    assert not has_bingo(card, set(cells[:-1]))


def test_the_free_centre_completes_a_four_number_line() -> None:
    card = make_card("f" * 32, _who("alice"))
    middle_row = {n for n in card[2] if n}
    assert len(middle_row) == 4
    assert has_bingo(card, middle_row)
    assert not has_bingo(card, set())
    assert not has_bingo(card, set(list(middle_row)[:3]))


def test_no_line_is_complete_with_only_the_free_square() -> None:
    assert not has_bingo(make_card("f" * 32, _who("bob")), set())


@pytest.mark.parametrize(
    ("number", "letter"),
    [(1, "B"), (15, "B"), (16, "I"), (30, "I"), (31, "N"), (45, "N"), (46, "G"), (60, "G")]
    + [(61, "O"), (75, "O")],
)
def test_column_letters(number: int, letter: str) -> None:
    assert _letter(number) == letter


def test_render_card_marks_called_numbers_and_the_free_square() -> None:
    card = [[1, 16, 31, 46, 61], [2, 17, 32, 47, 62], [3, 18, 0, 48, 63]]
    card += [[4, 19, 33, 49, 64], [5, 20, 34, 50, 65]]
    line = _render_card(card, {1, 62})
    assert line.split(" / ")[0] == "[1] 16 31 46 61"
    assert line.split(" / ")[1] == "2 17 32 47 [62]"
    assert "[FREE]" in line.split(" / ")[2]


def test_start_creates_a_round_and_announces(host: types.SimpleNamespace) -> None:
    reply = _modsay("!bingo start")
    assert reply.startswith("\U0001f3b1 bingo round #1 started!")
    current = _round(host)
    assert (current.rid, current.ended) == (1, False)
    assert re.fullmatch(r"[0-9a-f]{32}", current.seed)
    sets = {c[1][0]: c[1][2] for c in host.kv.calls if c[0] == "set"}
    incs = {c[1][0]: c[1][2] for c in host.kv.calls if c[0] == "increment"}
    assert sets[CUR_KEY] == 3 * 24 * 3600
    assert incs[f"c.{COMMUNITY}.bingo.rid"] == 30 * 24 * 3600


def test_the_seed_depends_on_the_clock_and_round(host: types.SimpleNamespace) -> None:
    _modsay("!bingo start")
    first = _round(host).seed
    _modsay("!bingo end")
    host.state["now"] += 1
    _modsay("!bingo start")
    assert _round(host).seed != first
    assert _round(host).rid == 2


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"is_mod": False, "is_broadcaster": False},
        {"is_mod": "true"},
        {"is_mod": "false", "is_broadcaster": "false"},
        {"is_mod": 1},
        {"is_broadcaster": 1},
        {"is_mod": None},
    ],
)
@pytest.mark.parametrize("verb", ["start", "call", "end"])
def test_mod_verbs_fail_closed_before_any_kv_access(
    host: types.SimpleNamespace, extra: dict[str, Any], verb: str
) -> None:
    reply = _text(_run(_event(f"!bingo {verb}", extra=extra)))
    assert reply == "only moderators/broadcasters can do that"
    assert host.kv.calls == []


def test_the_broadcaster_can_run_a_round(host: types.SimpleNamespace) -> None:
    event = _event("!bingo start", extra={"is_mod": False, "is_broadcaster": True})
    assert "started" in _text(_run(event))


def test_start_is_refused_while_a_round_is_live(host: types.SimpleNamespace) -> None:
    _modsay("!bingo start")
    assert "already running" in _modsay("!bingo start")
    assert _round(host).rid == 1


def test_start_after_end_and_after_a_win(host: types.SimpleNamespace) -> None:
    _modsay("!bingo start")
    _modsay("!bingo end")
    assert "round #2 started" in _modsay("!bingo start")
    _set_calls_for_round(host, 2, NUMBERS)
    assert "BINGO!" in _say("!bingo bingo")
    assert "round #3 started" in _modsay("!bingo start")


def _set_calls_for_round(host: types.SimpleNamespace, rid: int, count: int) -> None:
    host.kv.store[f"c.{COMMUNITY}.bingo.calls.{rid}"] = str(count).encode()


def test_start_recovers_from_a_corrupt_round_record(host: types.SimpleNamespace) -> None:
    host.kv.store[CUR_KEY] = b"{broken"
    assert "started" in _modsay("!bingo start")
    assert _round(host).rid == 1


def test_call_walks_the_fixed_order(host: types.SimpleNamespace) -> None:
    _modsay("!bingo start")
    order = call_order(_round(host).seed)
    for index in range(1, 6):
        number = order[index - 1]
        assert _modsay("!bingo call") == (
            f"\U0001f3b1 {_letter(number)}-{number} (call #{index} of 75)"
        )
    assert host.kv.store[CALLS_KEY] == b"5"


def test_call_with_no_live_round(host: types.SimpleNamespace) -> None:
    assert _modsay("!bingo call") == "no bingo round is running -- ask a mod to !bingo start"
    _modsay("!bingo start")
    _modsay("!bingo end")
    assert "no bingo round is running" in _modsay("!bingo call")


def test_all_75_numbers_can_be_called_then_the_counter_is_rolled_back(
    host: types.SimpleNamespace,
) -> None:
    _modsay("!bingo start")
    seen = []
    for _ in range(NUMBERS):
        seen.append(re.search(r"([BINGO])-(\d+) ", _modsay("!bingo call")))
    assert all(seen)
    assert len({m.group(2) for m in seen if m}) == 75
    reply = _modsay("!bingo call")
    assert reply == "all 75 numbers have been called -- !bingo end to close the round"
    assert host.kv.store[CALLS_KEY] == b"75"


def test_the_card_command_shows_the_viewers_own_card(host: types.SimpleNamespace) -> None:
    _modsay("!bingo start")
    seed = _round(host).seed
    card = make_card(seed, _who("alice"))
    expected = f"{{user:u-alice}} your card: {_render_card(card, set())}"
    assert _say("!bingo") == expected
    assert _say("!bingo card") == expected
    assert _say("!BINGO CARD") == expected
    assert _say("!bingo", "bob") != expected


def test_the_card_marks_called_numbers(host: types.SimpleNamespace) -> None:
    _modsay("!bingo start")
    for _ in range(10):
        _modsay("!bingo call")
    seed = _round(host).seed
    called = set(call_order(seed)[:10])
    card = make_card(seed, _who("alice"))
    assert _say("!bingo") == f"{{user:u-alice}} your card: {_render_card(card, called)}"
    for _ in range(30):
        _modsay("!bingo call")
    marked = _say("!bingo").replace("[FREE]", "")
    assert "[" in marked  # 40 of 75 called: some are on this card


def test_the_card_is_stable_across_calls_and_users_stay_distinct(
    host: types.SimpleNamespace,
) -> None:
    _modsay("!bingo start")
    before = _say("!bingo")
    _modsay("!bingo call")
    after = _say("!bingo")
    strip = re.compile(r"[\[\]]")
    assert strip.sub("", before) == strip.sub("", after)


def test_the_card_with_no_round_or_a_finished_round(host: types.SimpleNamespace) -> None:
    assert _say("!bingo") == "no bingo round is running -- ask a mod to !bingo start"
    _modsay("!bingo start")
    _modsay("!bingo end")
    assert "round #1 is over" in _say("!bingo")


def test_an_unidentifiable_viewer_gets_no_card_and_cannot_claim(
    host: types.SimpleNamespace,
) -> None:
    _modsay("!bingo start")
    anonymous = _event("!bingo", actor=None)
    assert _text(_run(anonymous)) == "couldn't tell who you are, so you can't get a card"
    assert "couldn't tell who you are" in _text(_run(_event("!bingo bingo", actor=None)))


def test_an_author_id_gives_one_card_whatever_the_display_name(
    host: types.SimpleNamespace,
) -> None:
    _modsay("!bingo start")
    first = _text(_run(_event("!bingo", actor="alice", author_id="9001")))
    second = _text(_run(_event("!bingo", actor="alice_renamed", author_id="9001")))
    assert first.split("your card: ")[1] == second.split("your card: ")[1]


def test_a_claim_with_no_round(host: types.SimpleNamespace) -> None:
    assert _say("!bingo bingo") == "no bingo round is running -- ask a mod to !bingo start"
    assert host.kv.calls == [("get", (CUR_KEY,))]


def test_an_author_id_with_no_display_actor_is_labelled_someone_and_wins_untokened(
    host: types.SimpleNamespace,
) -> None:
    _modsay("!bingo start")
    _set_calls(host, NUMBERS)
    reply = _text(_run(_event("!bingo bingo", actor=None, author_id="5")))
    assert reply == "\U0001f3c6 BINGO! someone wins round #1 after 75 calls!"
    assert json.loads(host.kv.store[WINNER_KEY])["t"] is None


def test_a_false_claim_is_not_yet(host: types.SimpleNamespace) -> None:
    _modsay("!bingo start")
    reply = _say("!bingo bingo")
    assert reply == "{user:u-alice}, not yet -- you need a full row, column or diagonal (0 called)"
    assert _say("!bingo claim").endswith("(0 called)")
    assert WON_KEY not in host.kv.store
    assert WINNER_KEY not in host.kv.store


def test_the_first_valid_claim_wins_and_ends_the_round(host: types.SimpleNamespace) -> None:
    _modsay("!bingo start")
    seed = _round(host).seed
    needed = _calls_needed(seed, _who("alice"))
    _set_calls(host, needed - 1)
    assert "not yet" in _say("!bingo bingo")
    _set_calls(host, needed)
    reply = _say("!bingo bingo")
    assert reply == f"\U0001f3c6 BINGO! {{user:u-alice}} wins round #1 after {needed} calls!"
    assert json.loads(host.kv.store[WINNER_KEY])["t"] == "u-alice"
    # The round is over for everyone: later claims, cards and calls are refused.
    assert _say("!bingo bingo", "bob") == "bingo round #1 is already over"
    assert "is over" in _say("!bingo", "bob")
    assert "no bingo round is running" in _modsay("!bingo call")


def test_the_claim_is_verified_server_side_not_taken_from_chat(
    host: types.SimpleNamespace,
) -> None:
    _modsay("!bingo start")
    _set_calls(host, 5)
    reply = _say("!bingo bingo B1 I16 N31 G46 O61")  # extra text is a usage error, never a claim
    assert reply.startswith("Usage: !bingo")
    assert WON_KEY not in host.kv.store


def test_the_atomic_claim_stops_a_second_simultaneous_winner(host: types.SimpleNamespace) -> None:
    _modsay("!bingo start")
    _set_calls(host, NUMBERS)  # every card now holds a line
    host.kv.store[WON_KEY] = b"1"  # another claimant already took the claim, record not yet written
    reply = _say("!bingo bingo")
    assert reply == "{user:u-alice}, someone else claimed bingo first!"
    assert WINNER_KEY not in host.kv.store


def test_two_claims_with_every_card_valid_give_exactly_one_winner(
    host: types.SimpleNamespace,
) -> None:
    _modsay("!bingo start")
    _set_calls(host, NUMBERS)

    async def _go() -> list[PlatformEvent | None]:
        with bundle_context(tenant="t", community=COMMUNITY, app_id="bingo"):
            return await asyncio.gather(
                transform(_viewer("!bingo bingo", "alice")),
                transform(_viewer("!bingo bingo", "bob")),
            )

    replies = [_text(r) for r in asyncio.run(_go())]
    assert sum("BINGO!" in r for r in replies) == 1
    assert sum("already over" in r or "someone else" in r for r in replies) == 1


def test_a_failed_winner_write_releases_the_claim_so_the_next_claim_wins(
    host: types.SimpleNamespace,
) -> None:
    _modsay("!bingo start")
    _set_calls(host, NUMBERS)
    host.kv.fail.append(["set", "bingo.winner.", 1])
    assert _say("!bingo bingo") == "bingo is temporarily unavailable, try again shortly."
    assert host.kv.store[WON_KEY] == b"0"
    assert "Error_Backend" in _logged(host)
    assert "BINGO!" in _say("!bingo bingo", "bob")


def test_a_failed_claim_release_is_logged_not_masked(host: types.SimpleNamespace) -> None:
    _modsay("!bingo start")
    _set_calls(host, NUMBERS)
    host.kv.fail.append(["set", "bingo.winner.", 1])
    host.kv.fail.append(["increment-", "bingo.won.", 1])  # the compensating decrement
    assert "temporarily unavailable" in _say("!bingo bingo")
    assert "compensation_failed" in _logged(host)
    # A mod can still unstick the round: the claim counter is stuck, but `end` works.
    assert "ended with no winner" in _modsay("!bingo end")


def test_end_without_a_winner(host: types.SimpleNamespace) -> None:
    assert _modsay("!bingo end") == "no bingo round is running -- ask a mod to !bingo start"
    _modsay("!bingo start")
    assert _modsay("!bingo end") == "bingo round #1 ended with no winner"
    assert _modsay("!bingo end") == "bingo round #1 is already over"
    assert _say("!bingo bingo") == "bingo round #1 is already over"


def test_status_in_every_state(host: types.SimpleNamespace) -> None:
    assert _say("!bingo status") == "no bingo round is running -- ask a mod to !bingo start"
    _modsay("!bingo start")
    assert _say("!bingo status") == "bingo round #1 is live: 0/75 called"
    for _ in range(3):
        _modsay("!bingo call")
    order = call_order(_round(host).seed)
    latest = ", ".join(f"{_letter(n)}-{n}" for n in order[:3][::-1])
    assert _say("!bingo status") == f"bingo round #1 is live: 3/75 called; latest: {latest}"
    assert _say("!bingo list") == _say("!bingo status")
    _modsay("!bingo end")
    assert _say("!bingo status") == "bingo round #1 ended with no winner after 3 calls"


def test_status_lists_only_the_latest_eight_calls(host: types.SimpleNamespace) -> None:
    _modsay("!bingo start")
    for _ in range(20):
        _modsay("!bingo call")
    reply = _say("!bingo status")
    assert reply.startswith("bingo round #1 is live: 20/75 called; latest: ")
    assert len(reply.split("latest: ")[1].split(", ")) == 8


def test_status_after_a_win_names_the_winner_by_mention_token(
    host: types.SimpleNamespace,
) -> None:
    _modsay("!bingo start")
    _set_calls(host, NUMBERS)
    _say("!bingo bingo", "alice")
    assert _say("!bingo status", "bob") == "bingo round #1 was won by {user:u-alice} after 75 calls"


def test_a_winner_without_a_token_is_a_pseudonym_tag(host: types.SimpleNamespace) -> None:
    _modsay("!bingo start")
    _set_calls(host, NUMBERS)
    reply = _text(_run(_event("!bingo bingo", actor="plainname", author_id="777")))
    assert "BINGO!" in reply
    status = _say("!bingo status")
    assert re.search(r"was won by entrant [0-9a-f]{8} after 75 calls", status)
    assert "plainname" not in status
    assert "777" not in status


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xfe",
        b"not json",
        b"[]",
        json.dumps({"t": "bad token", "p": "abcd1234"}).encode(),
        json.dumps({"t": "", "p": "abcd1234"}).encode(),
        json.dumps({"t": "a" * 257, "p": "abcd1234"}).encode(),
        json.dumps({"t": 5, "p": "abcd1234"}).encode(),
        json.dumps({"t": None, "p": "ABCD1234"}).encode(),
        json.dumps({"t": None}).encode(),
    ],
)
def test_a_corrupt_winner_record_is_never_rendered(host: types.SimpleNamespace, raw: bytes) -> None:
    _modsay("!bingo start")
    host.kv.store[WINNER_KEY] = raw
    reply = _say("!bingo status")
    assert reply.startswith("bingo round #1 is live")  # an unreadable record is not a winner
    assert "winner_corrupt" in _logged(host)
    assert "ended with no winner" in _modsay("!bingo end")  # a mod can always unstick it


@pytest.mark.parametrize(
    "line",
    [
        "!bingo bogus",
        "!bingo card now",
        "!bingo bingo B1",
        "!bingo status please",
        "!bingo list extra",
        "!bingo start now",
        "!bingo call 5",
        "!bingo end it",
        "!bingo set x",
        "!bingo enable x",
    ],
)
def test_malformed_commands_reply_usage_never_silence(
    host: types.SimpleNamespace, line: str
) -> None:
    assert _text(_run(_mod(line))).startswith("Usage: !bingo")
    assert host.kv.calls == []


@pytest.mark.parametrize("text", [None, 5, ["!bingo"], b"!bingo", "", "   ", "bingo", "!bingox"])
def test_other_text_is_ignored_with_zero_kv_cost(host: types.SimpleNamespace, text: Any) -> None:
    assert _run(_viewer(text)) is None
    assert host.kv.calls == []


def test_a_corrupt_round_record_is_answered_loudly(host: types.SimpleNamespace) -> None:
    host.kv.store[CUR_KEY] = b"[1]"
    assert _say("!bingo status") == (
        "the bingo state is corrupted -- a mod can run !bingo end then !bingo start"
    )
    assert "state_corrupt" in _logged(host)


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xfe",
        b"not json",
        b"[]",
        json.dumps({"rid": True, "seed": "a" * 32, "ended": False}).encode(),
        json.dumps({"rid": 1, "seed": "a" * 31, "ended": False}).encode(),
        json.dumps({"rid": 1, "seed": "G" * 32, "ended": False}).encode(),
        json.dumps({"rid": 1, "seed": "a" * 32, "ended": "false"}).encode(),
        json.dumps({"rid": 1, "seed": "a" * 32}).encode(),
    ],
)
def test_round_load_is_strict(raw: bytes) -> None:
    with pytest.raises(_StateCorruptError):
        _Round.load(raw)


def test_round_record_round_trips() -> None:
    current = _Round(rid=4, seed="a" * 32, ended=True)
    assert _Round.load(current.dump()) == current


def test_a_corrupt_calls_counter_is_loud(host: types.SimpleNamespace) -> None:
    _modsay("!bingo start")
    host.kv.store[CALLS_KEY] = b"lots"
    assert "corrupted" in _say("!bingo")
    assert "calls_corrupt" in _logged(host)


def test_a_negative_or_huge_calls_counter_is_clamped(host: types.SimpleNamespace) -> None:
    _modsay("!bingo start")
    _set_calls(host, 10_000)
    assert "75/75 called" in _say("!bingo status")
    _set_calls(host, -4)
    assert "0/75 called" in _say("!bingo status")


def test_kv_failure_replies_and_logs_the_class_only(host: types.SimpleNamespace) -> None:
    host.kv.fail.append(["get", "bingo.cur", 1])
    assert _say("!bingo status") == "bingo is temporarily unavailable, try again shortly."
    logged = _logged(host)
    assert "bingo.kv_failure" in logged
    assert "Error_Backend" in logged
    assert "host kv error" not in logged


@pytest.mark.parametrize(
    ("op", "key"), [("increment", "bingo.rid"), ("set", "bingo.cur"), ("get", "bingo.cur")]
)
def test_start_kv_failures_reply_unavailable(
    host: types.SimpleNamespace, op: str, key: str
) -> None:
    host.kv.fail.append([op, key, 1])
    assert "temporarily unavailable" in _modsay("!bingo start")


def test_tenant_wide_sentinel_scopes_under_zero(host: types.SimpleNamespace) -> None:
    assert "started" in _text(_run(_mod("!bingo start"), community=None))
    assert "c.0.bingo.cur" in host.kv.store


def test_an_empty_community_id_fails_loud(host: types.SimpleNamespace) -> None:
    with pytest.raises(ValueError, match="community context"):
        _run(_viewer("!bingo"), community="")


def test_communities_are_isolated(host: types.SimpleNamespace) -> None:
    _run(_mod("!bingo start"), community="comm-a")
    assert "no bingo round is running" in _text(_run(_viewer("!bingo"), community="comm-b"))


def test_the_label_for_a_raw_name_is_sanitized(host: types.SimpleNamespace) -> None:
    _modsay("!bingo start")
    reply = _text(_run(_event("!bingo", actor="@everyone")))
    assert "@everyone" not in reply
    assert "@" + ZWSP + "everyone" in reply
    hostile = "{user:" + "a" * 300 + "}"
    escaped = _text(_run(_event("!bingo bingo", actor=hostile)))
    assert BACKSLASH + "{user:" in escaped
    assert not re.search(r"(?<!\\)\{user:", escaped)


def test_no_pii_in_keys_values_or_logs(host: types.SimpleNamespace) -> None:
    secret_name, secret_id = "RealStreamerName", "98765432"
    _modsay("!bingo start")
    _set_calls(host, NUMBERS)
    _text(_run(_event("!bingo", actor=secret_name, author_id=secret_id)))
    _text(_run(_event("!bingo bingo", actor=secret_name, author_id=secret_id)))
    _say("!bingo status")
    blob = " ".join(host.kv.store) + " ".join(v.decode() for v in host.kv.store.values())
    for needle in (secret_name, secret_id):
        assert needle not in blob
    for needle in (secret_name, secret_id):
        assert needle not in _logged(host)
    assert all(":" not in key for key in host.kv.store)


def test_every_invocation_stays_inside_the_op_budget(host: types.SimpleNamespace) -> None:
    for line, who in [
        ("!bingo start", "mod"),
        ("!bingo call", "mod"),
        ("!bingo", "alice"),
        ("!bingo bingo", "alice"),
        ("!bingo status", "alice"),
        ("!bingo end", "mod"),
    ]:
        host.kv.calls.clear()
        _run(_mod(line) if who == "mod" else _viewer(line))
        assert 0 < len(host.kv.calls) <= 8


def test_dispatch_relays_the_reply(host: types.SimpleNamespace) -> None:
    reply = _run(_viewer("!bingo status"))
    assert reply is not None
    result = asyncio.run(dispatch(_envelope(reply), {}, http_client=None))
    assert isinstance(result, DispatchResult)
    assert (result.transport, result.detail) == ("twitch", "relayed")
    provider, message = host.relay_calls[0]
    assert provider == "twitch"
    assert json.loads(message) == {"channel": "chan-1", "text": _text(reply)}


def test_dispatch_requires_a_channel(host: types.SimpleNamespace) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=None,
        payload={"text": "x", "channel_id": None},
        occurred_at="2026-10-22T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        asyncio.run(dispatch(_envelope(event), {}, http_client=None))


def _envelope(event: PlatformEvent) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=COMMUNITY,
        app_id="waddles.core.example.bingo",
        stage="action",
        event=event,
        ts="2026-10-22T00:00:00.000Z",
    )


def test_manifests_and_pyproject_agree() -> None:
    bundle = (BUNDLE_DIR / "bundle.yaml").read_text()
    hub = (BUNDLE_DIR / "hub-manifest.yaml").read_text()
    pyproject = (BUNDLE_DIR / "pyproject.toml").read_text()
    for text in (bundle, hub):
        assert re.search(r"^version: 1\.0\.0$", text, re.MULTILINE)
        assert "app_id: waddles.core.example.bingo" in text
        assert text.count('command_prefix: ["!bingo"]') == 2
    assert 'version = "1.0.0"' in pyproject
    assert 'tested_with: "1.0.0"' in bundle


def test_manifest_declares_only_kv_and_chat_send() -> None:
    hub = (BUNDLE_DIR / "hub-manifest.yaml").read_text()
    ids = re.findall(r"- id: \"?([\w.:]+)\"?", hub)
    assert ids == ["storage.kv", "chat.send:discord", "chat.send:twitch"]


def test_the_bundle_is_activation_gated_only() -> None:
    source = (BUNDLE_DIR / "src" / "app.py").read_text()
    assert "feature_enabled" not in source
    assert "FLAG_KEY" not in source
    assert "flags" not in source.split('"""', 2)[2]
