"""Host-native tests for the `keywordgiveaway` bundle -- fake `wit_world` (kv/relay/log/clock).

No WASM/wasmtime. The fake `kv` is the SDK's `FakeKvHost` (validates keys like the real host,
`scope.rs::is_allowed_key_byte`, gh-631; `increment` is the atomic compare-and-swap) behind a
failure-injection wrapper so the compensation paths run for real.
"""

from __future__ import annotations

import asyncio
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

import app
from app import (
    MAX_DRAW_PROBES,
    MAX_PRIZE_LEN,
    DispatchResult,
    _KvFailureError,
    _pick_index,
    _State,
    _StateCorruptError,
    dispatch,
    transform,
)

COMMUNITY = "comm-1"
ZWSP = chr(0x200B)
BACKSLASH = chr(92)
KEYWORD = "winwin"
STATE_KEY = f"c.{COMMUNITY}.kwgiveaway.state"
BUNDLE_DIR = Path(__file__).resolve().parent.parent


class _HostKvError(Exception):
    """Shaped like the generated WIT `Err` (`.value` holds the error union)."""

    def __init__(self, value: Any) -> None:
        super().__init__("host kv error")
        self.value = value


class Error_Backend:  # noqa: N801 -- mirrors the generated WIT case-class naming
    """Stand-in for the generated `Error_Backend` case."""


class _FailableKv:
    """`FakeKvHost` plus scripted failures: `fail.append((op, key_substring, times))`."""

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
        self._maybe_fail("increment", key)
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
        with bundle_context(tenant="tenant-1", community=community, app_id="keywordgiveaway"):
            return await transform(event)

    return asyncio.run(_go())


def _text(result: PlatformEvent | None) -> str:
    assert result is not None
    return str(result.payload["text"])


def _open(host: types.SimpleNamespace, keyword: str = KEYWORD, prize: str = "") -> str:
    return _text(_run(_mod(f"!kgiveaway open {keyword} {prize}".strip())))


def _enter_many(names: list[str]) -> None:
    for name in names:
        assert _run(_viewer(KEYWORD, name)) is None


def _state(host: types.SimpleNamespace) -> dict[str, Any]:
    return json.loads(host.kv.store[STATE_KEY])


def _logged(host: types.SimpleNamespace) -> str:
    return " ".join(f"{msg} {fields}" for _lvl, msg, fields in host.log_calls)


def _entry_keys(host: types.SimpleNamespace) -> list[str]:
    return sorted(k for k in host.kv.store if ".kwgiveaway.e." in k)


def test_open_stores_state_and_announces(host: types.SimpleNamespace) -> None:
    reply = _open(host, "!Win", "a signed poster")
    assert "giveaway open" in reply
    assert "!win" in reply  # keyword is stored/echoed lower-cased
    assert "a signed poster" in reply
    assert _state(host) == {
        "rid": 1,
        "kw": "!win",
        "prize": "a signed poster",
        "open": True,
        "drawn": [],
    }
    # State carries a 3-day TTL; the round counter the 30-day max.
    sets = {c[1][0]: c[1][2] for c in host.kv.calls if c[0] == "set"}
    incs = {c[1][0]: c[1][2] for c in host.kv.calls if c[0] == "increment"}
    assert sets[STATE_KEY] == 3 * 24 * 3600
    assert incs[f"c.{COMMUNITY}.kwgiveaway.rid"] == 30 * 24 * 3600


def test_broadcaster_can_open(host: types.SimpleNamespace) -> None:
    event = _event(f"!kgiveaway open {KEYWORD}", extra={"is_mod": False, "is_broadcaster": True})
    assert "giveaway open" in _text(_run(event))


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
@pytest.mark.parametrize("verb", ["open winwin", "close", "draw", "reroll"])
def test_mod_verbs_fail_closed_before_any_kv_access(
    host: types.SimpleNamespace, extra: dict[str, Any], verb: str
) -> None:
    reply = _text(_run(_event(f"!kgiveaway {verb}", extra=extra)))
    assert reply == "only moderators/broadcasters can do that"
    assert host.kv.calls == []


@pytest.mark.parametrize(
    "args",
    [
        "",
        "x",
        "has-dash",
        "a" * 25,
        "unic" + chr(0xF6) + "de",
        "!",
        "!!win",
        "with.dot",
    ],
)
def test_open_rejects_a_bad_keyword(host: types.SimpleNamespace, args: str) -> None:
    reply = _text(_run(_mod(f"!kgiveaway open {args}")))
    assert reply.startswith("Usage: !kgiveaway open")
    assert STATE_KEY not in host.kv.store


def test_open_rejects_an_overlong_prize(host: types.SimpleNamespace) -> None:
    reply = _text(_run(_mod(f"!kgiveaway open {KEYWORD} {'p' * (MAX_PRIZE_LEN + 1)}")))
    assert "too long" in reply
    assert host.kv.calls == []


def test_open_collapses_prize_whitespace(host: types.SimpleNamespace) -> None:
    _open(host, KEYWORD, "a   big \t  prize")
    assert _state(host)["prize"] == "a big prize"


def test_open_is_refused_while_open_and_allowed_after_close(host: types.SimpleNamespace) -> None:
    _open(host)
    again = _text(_run(_mod("!kgiveaway open other")))
    assert "already open" in again
    assert _state(host)["kw"] == KEYWORD
    assert "closed" in _text(_run(_mod("!kgiveaway close")))
    assert "giveaway open" in _open(host, "other")
    assert _state(host)["rid"] == 2


def test_a_new_round_does_not_inherit_old_entrants(host: types.SimpleNamespace) -> None:
    _open(host)
    _enter_many(["alice"])
    _run(_mod("!kgiveaway close"))
    _open(host, "round2")
    assert _run(_viewer("round2", "alice")) is None  # allowed to enter again, no "duplicate"
    assert _text(_run(_mod("!kgiveaway status"))).count("1 entered") == 1
    assert len(_entry_keys(host)) == 2  # one slot in rid 1, one in rid 2


def test_open_recovers_from_a_corrupt_state(host: types.SimpleNamespace) -> None:
    host.kv.store[STATE_KEY] = b"{not json"
    assert "giveaway open" in _open(host)
    assert _state(host)["open"] is True


def test_keyword_entry_is_silent_and_stores_a_pseudonymous_slot(
    host: types.SimpleNamespace,
) -> None:
    _open(host)
    assert _run(_viewer(KEYWORD, "alice")) is None
    keys = _entry_keys(host)
    assert keys == [f"c.{COMMUNITY}.kwgiveaway.e.1.1"]
    entry = json.loads(host.kv.store[keys[0]])
    assert entry["t"] == "u-alice"
    assert re.fullmatch(r"[0-9a-f]{8}", entry["p"])
    assert host.kv.store[f"c.{COMMUNITY}.kwgiveaway.n.1"] == b"1"


@pytest.mark.parametrize("typed", [KEYWORD.upper(), f"  {KEYWORD}  ", "WinWin"])
def test_keyword_matches_case_insensitively_and_trimmed(
    host: types.SimpleNamespace, typed: str
) -> None:
    _open(host)
    assert _run(_viewer(typed)) is None
    assert len(_entry_keys(host)) == 1


@pytest.mark.parametrize(
    "typed", [f"{KEYWORD} please", f"i want to {KEYWORD}", "winwi", f"{KEYWORD}!", "x" * 400]
)
def test_only_the_whole_line_enters(host: types.SimpleNamespace, typed: str) -> None:
    _open(host)
    assert _run(_viewer(typed)) is None
    assert _entry_keys(host) == []


def test_long_lines_cost_no_kv_read(host: types.SimpleNamespace) -> None:
    _open(host)
    host.kv.calls.clear()
    assert _run(_viewer("this is just a normal chat line that is longer than a keyword")) is None
    assert host.kv.calls == []


def test_a_short_line_with_no_giveaway_is_one_kv_read(host: types.SimpleNamespace) -> None:
    assert _run(_viewer("hello")) is None
    assert host.kv.calls == [("get", (STATE_KEY,))]


def test_duplicate_keyword_entry_is_silent_and_not_counted(host: types.SimpleNamespace) -> None:
    _open(host)
    _enter_many(["alice", "alice", "alice"])
    assert len(_entry_keys(host)) == 1
    assert host.kv.store[f"c.{COMMUNITY}.kwgiveaway.n.1"] == b"1"


def test_one_entry_per_author_id_even_with_a_changing_display_name(
    host: types.SimpleNamespace,
) -> None:
    _open(host)
    assert _run(_event(KEYWORD, actor="alice", author_id="9001")) is None
    assert _run(_event(KEYWORD, actor="alice_renamed", author_id="9001")) is None
    assert len(_entry_keys(host)) == 1


def test_same_viewer_on_two_platforms_is_two_pseudonyms(host: types.SimpleNamespace) -> None:
    _open(host)
    assert _run(_event(KEYWORD, author_id="42", platform="twitch")) is None
    assert _run(_event(KEYWORD, author_id="42", platform="discord")) is None
    assert len(_entry_keys(host)) == 2


def test_explicit_enter_replies_and_dedupes(host: types.SimpleNamespace) -> None:
    _open(host)
    first = _text(_run(_viewer("!kgiveaway enter")))
    assert first == "\U0001f389 {user:u-alice} entered the giveaway! (entry #1)"
    assert _text(_run(_viewer("!kgiveaway enter"))) == "{user:u-alice}, you're already entered!"
    assert _text(_run(_viewer("!kgiveaway ENTER", "bob"))).endswith("(entry #2)")


def test_explicit_enter_with_nothing_open_or_closed(host: types.SimpleNamespace) -> None:
    assert "no giveaway is open" in _text(_run(_viewer("!kgiveaway enter")))
    _open(host)
    _run(_mod("!kgiveaway close"))
    assert _text(_run(_viewer("!kgiveaway enter"))) == "entries are closed for this giveaway"


def test_closed_giveaway_ignores_the_keyword(host: types.SimpleNamespace) -> None:
    _open(host)
    _run(_mod("!kgiveaway close"))
    assert _run(_viewer(KEYWORD)) is None
    assert _entry_keys(host) == []


def test_unidentifiable_author_cannot_enter(host: types.SimpleNamespace) -> None:
    _open(host)
    assert _run(_event(KEYWORD, actor=None)) is None
    reply = _text(_run(_event("!kgiveaway enter", actor=None)))
    assert reply == "couldn't tell who you are, so you can't be entered"
    assert _entry_keys(host) == []


def test_raw_username_actor_is_sanitized_in_the_ack_and_stored_without_a_token(
    host: types.SimpleNamespace,
) -> None:
    _open(host)
    reply = _text(_run(_event("!kgiveaway enter", actor="@everyone")))
    assert "@everyone" not in reply
    assert "@" + ZWSP + "everyone" in reply
    entry = json.loads(host.kv.store[_entry_keys(host)[0]])
    assert entry["t"] is None


def test_the_entrant_cap_rolls_the_counters_back(
    host: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app, "MAX_ENTRANTS", 2)
    _open(host)
    _enter_many(["a", "b"])
    reply = _text(_run(_viewer("!kgiveaway enter", "c")))
    assert reply == "the giveaway is full (2 entrants max)"
    assert host.kv.store[f"c.{COMMUNITY}.kwgiveaway.n.1"] == b"2"
    assert _run(_viewer(KEYWORD, "d")) is None  # ambient: silent
    assert len(_entry_keys(host)) == 2
    # The rejected viewer's marker was released (not left blocking them forever).
    assert not [k for k in host.kv.store if ".kwgiveaway.m." in k and k.endswith(_marker_of("c"))]


def _marker_of(name: str) -> str:
    import hashlib

    digest = hashlib.sha256(f"twitch\0{{user:u-{name}}}".encode()).hexdigest()
    return digest[:32]


def test_a_failed_entry_write_releases_the_marker_so_a_retry_works(
    host: types.SimpleNamespace,
) -> None:
    _open(host)
    host.kv.fail.append(["set", ".kwgiveaway.e.", 1])
    reply = _text(_run(_viewer("!kgiveaway enter")))
    assert reply == "the giveaway is temporarily unavailable, try again shortly."
    assert [k for k in host.kv.store if ".kwgiveaway.m." in k] == []
    assert "Error_Backend" in _logged(host)
    # Retry succeeds; the orphaned slot 1 is a hole, the retry lands in slot 2.
    assert _text(_run(_viewer("!kgiveaway enter"))).endswith("(entry #2)")


def test_a_failed_entry_write_on_the_ambient_path_raises(host: types.SimpleNamespace) -> None:
    _open(host)
    host.kv.fail.append(["set", ".kwgiveaway.e.", 1])
    with pytest.raises(_KvFailureError):
        _run(_viewer(KEYWORD))
    assert [k for k in host.kv.store if ".kwgiveaway.m." in k] == []


def test_a_failed_compensation_is_logged_not_masked(host: types.SimpleNamespace) -> None:
    _open(host)
    host.kv.fail.append(["set", ".kwgiveaway.e.", 1])
    host.kv.fail.append(["delete", ".kwgiveaway.m.", 1])
    with pytest.raises(_KvFailureError):
        _run(_viewer(KEYWORD))
    assert "compensation_failed" in _logged(host)


def test_concurrent_entries_get_distinct_slots(host: types.SimpleNamespace) -> None:
    _open(host)

    async def _go() -> None:
        with bundle_context(tenant="t", community=COMMUNITY, app_id="keywordgiveaway"):
            await asyncio.gather(
                *(transform(_viewer(KEYWORD, f"user{i}")) for i in range(50)),
                *(transform(_viewer(KEYWORD, "same")) for _ in range(5)),
            )

    asyncio.run(_go())
    entries = [json.loads(host.kv.store[k])["t"] for k in _entry_keys(host)]
    assert len(entries) == 51
    assert len(set(entries)) == 51


def _envelope(event: PlatformEvent) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=COMMUNITY,
        app_id="waddles.core.example.keywordgiveaway",
        stage="action",
        event=event,
        ts="2026-10-22T00:00:00.000Z",
    )


def _entered_giveaway(host: types.SimpleNamespace, names: list[str]) -> None:
    _open(host, KEYWORD, "a prize")
    _enter_many(names)


def test_draw_announces_a_winner_via_the_mention_token_and_closes(
    host: types.SimpleNamespace,
) -> None:
    _entered_giveaway(host, ["alice", "bob", "carol"])
    reply = _text(_run(_mod("!kgiveaway draw")))
    winner = r"\{user:u-(alice|bob|carol)\}"
    assert re.fullmatch(
        rf"\U0001f3c6 the winner: {winner} -- prize: a prize -- congratulations!", reply
    )
    state = _state(host)
    assert state["open"] is False
    assert len(state["drawn"]) == 1
    assert _run(_viewer(KEYWORD, "dave")) is None  # pool is fixed after the draw
    assert len(_entry_keys(host)) == 3


def test_draw_with_no_entrants_keeps_the_giveaway_open(host: types.SimpleNamespace) -> None:
    _open(host)
    reply = _text(_run(_mod("!kgiveaway draw")))
    assert reply == "no entrants yet -- have viewers type the keyword first"
    assert _state(host)["open"] is True
    assert _state(host)["drawn"] == []


def test_draw_with_nothing_open(host: types.SimpleNamespace) -> None:
    assert "no giveaway is open" in _text(_run(_mod("!kgiveaway draw")))
    assert "no giveaway is open" in _text(_run(_mod("!kgiveaway close")))
    assert "no giveaway is open" in _text(_run(_mod("!kgiveaway reroll")))


def test_a_second_draw_points_at_reroll(host: types.SimpleNamespace) -> None:
    _entered_giveaway(host, ["alice", "bob"])
    _run(_mod("!kgiveaway draw"))
    reply = _text(_run(_mod("!kgiveaway draw")))
    assert reply == "a winner was already drawn -- use !kgiveaway reroll for another"
    assert len(_state(host)["drawn"]) == 1


def test_reroll_before_any_draw(host: types.SimpleNamespace) -> None:
    _entered_giveaway(host, ["alice"])
    reply = _text(_run(_mod("!kgiveaway reroll")))
    assert reply == "nobody has been drawn yet -- use !kgiveaway draw first"
    assert _state(host)["drawn"] == []


def test_reroll_never_repeats_a_winner_until_the_pool_is_exhausted(
    host: types.SimpleNamespace,
) -> None:
    _entered_giveaway(host, ["alice", "bob", "carol"])
    winners = [_text(_run(_mod("!kgiveaway draw")))]
    for _ in range(2):
        host.state["now"] += 17
        winners.append(_text(_run(_mod("!kgiveaway reroll"))))
    seen = [re.search(r"\{user:(u-\w+)\}", w) for w in winners]
    assert all(seen)
    assert {m.group(1) for m in seen if m} == {"u-alice", "u-bob", "u-carol"}
    assert winners[1].startswith("\U0001f501 new winner:")
    assert _text(_run(_mod("!kgiveaway reroll"))) == "no more entrants to draw from"
    assert len(_state(host)["drawn"]) == 3


def test_winner_without_a_mention_token_is_a_pseudonym_tag(host: types.SimpleNamespace) -> None:
    _open(host)
    assert _run(_event(KEYWORD, actor="plainname", author_id="777")) is None
    reply = _text(_run(_mod("!kgiveaway draw")))
    assert re.search(r"the winner: entrant [0-9a-f]{8} ", reply)
    assert "plainname" not in reply
    assert "777" not in reply


def test_draw_skips_holes_and_corrupt_entries(
    host: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pick is OS-entropy random: pin it to the first remaining slot so the probe order (hole,
    # then the two corrupt entries, then the valid one) -- and so the logged corruption -- is fixed.
    monkeypatch.setattr(app, "_pick_index", lambda size: 0)
    _entered_giveaway(host, ["alice", "bob", "carol", "dave"])
    prefix = f"c.{COMMUNITY}.kwgiveaway.e.1."
    del host.kv.store[prefix + "1"]  # hole (expired / never written)
    host.kv.store[prefix + "2"] = b"not json"
    host.kv.store[prefix + "3"] = json.dumps({"t": "x}{user:y", "p": "abcd1234"}).encode()
    reply = _text(_run(_mod("!kgiveaway draw")))
    assert "{user:u-dave}" in reply  # the only valid entry
    assert "x}{user:y" not in reply
    assert "entry_corrupt" in _logged(host)


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xfe",
        b"not json",
        b"[]",
        b"{}",
        json.dumps({"t": "bad token", "p": "abcd1234"}).encode(),
        json.dumps({"t": "", "p": "abcd1234"}).encode(),
        json.dumps({"t": "a" * 257, "p": "abcd1234"}).encode(),
        json.dumps({"t": 5, "p": "abcd1234"}).encode(),
        json.dumps({"t": None, "p": "ABCD1234"}).encode(),
        json.dumps({"t": None, "p": "abcd"}).encode(),
        json.dumps({"t": None}).encode(),
    ],
)
def test_each_kind_of_corrupt_entry_is_a_hole_never_a_winner(
    host: types.SimpleNamespace, raw: bytes
) -> None:
    _entered_giveaway(host, ["alice"])
    host.kv.store[f"c.{COMMUNITY}.kwgiveaway.e.1.1"] = raw
    reply = _text(_run(_mod("!kgiveaway draw")))
    assert reply == "no entrants yet -- have viewers type the keyword first"
    assert "entry_corrupt" in _logged(host)
    assert _state(host)["drawn"] == []


def test_an_author_id_with_no_display_actor_still_enters_and_is_tagged(
    host: types.SimpleNamespace,
) -> None:
    _open(host)
    reply = _text(_run(_event("!kgiveaway enter", actor=None, author_id="31337")))
    assert reply == "\U0001f389 someone entered the giveaway! (entry #1)"
    assert json.loads(host.kv.store[_entry_keys(host)[0]])["t"] is None


def test_an_overlong_placeholder_actor_is_not_stored_as_a_token(
    host: types.SimpleNamespace,
) -> None:
    _open(host)
    actor = "{user:" + "a" * 300 + "}"
    reply = _text(_run(_event("!kgiveaway enter", actor=actor)))
    assert BACKSLASH + "{user:" in reply  # escaped, not a live placeholder
    assert not re.search(r"(?<!\\)\{user:", reply)
    assert json.loads(host.kv.store[_entry_keys(host)[0]])["t"] is None


def test_draw_with_only_holes_reports_no_entrants_and_stays_inside_the_op_budget(
    host: types.SimpleNamespace,
) -> None:
    _open(host)
    host.kv.store[f"c.{COMMUNITY}.kwgiveaway.n.1"] = b"500"  # slots handed out, none written
    host.kv.calls.clear()
    reply = _text(_run(_mod("!kgiveaway draw")))
    assert reply == "no entrants yet -- have viewers type the keyword first"
    assert len(host.kv.calls) <= 64
    assert sum(1 for c in host.kv.calls if ".kwgiveaway.e." in c[1][0]) == MAX_DRAW_PROBES


def test_every_invocation_stays_inside_the_64_op_budget(host: types.SimpleNamespace) -> None:
    _entered_giveaway(host, [f"u{i}" for i in range(60)])
    for line in ["!kgiveaway draw", "!kgiveaway reroll", "!kgiveaway status", "!kgiveaway close"]:
        host.kv.calls.clear()
        _run(_mod(line))
        assert len(host.kv.calls) <= 64


def test_the_pick_comes_from_os_entropy_not_the_clock(
    host: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """regression: a sha256(now_millis, ...) pick let whoever chose the draw moment steer it."""
    _entered_giveaway(host, ["alice", "bob", "carol", "dave"])
    snapshot = dict(host.kv.store)
    seen = []
    for index in range(4):
        host.kv.inner.store.clear()
        host.kv.inner.store.update(snapshot)
        monkeypatch.setattr(app._RNG, "randrange", lambda size, i=index: i)
        match = re.search(r"\{user:(u-\w+)\}", _text(_run(_mod("!kgiveaway draw"))))
        assert match
        seen.append(match.group(1))
    assert seen == ["u-alice", "u-bob", "u-carol", "u-dave"]


def test_the_pick_ignores_the_clock(
    host: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app._RNG, "randrange", lambda size: 1)
    _entered_giveaway(host, ["alice", "bob", "carol"])
    snapshot = dict(host.kv.store)
    picks = set()
    for now in (0, 7, 999_999):
        host.kv.inner.store.clear()
        host.kv.inner.store.update(snapshot)
        host.state["now"] = now
        match = re.search(r"\{user:(u-\w+)\}", _text(_run(_mod("!kgiveaway draw"))))
        assert match
        picks.add(match.group(1))
    assert picks == {"u-bob"}


def test_the_pick_index_is_in_range_and_unseeded() -> None:
    assert all(0 <= _pick_index(4) < 4 for _ in range(200))
    assert _pick_index(1) == 0


def test_status_in_every_state(host: types.SimpleNamespace) -> None:
    assert "no giveaway is open" in _text(_run(_viewer("!kgiveaway status")))
    _open(host, KEYWORD, "a prize")
    _enter_many(["alice", "bob"])
    assert (
        _text(_run(_viewer("!kgiveaway status")))
        == f"giveaway open: type {KEYWORD} to enter -- 2 entered -- prize: a prize"
    )
    _run(_mod("!kgiveaway close"))
    assert (
        _text(_run(_viewer("!kgiveaway status")))
        == "giveaway closed: 2 entered, waiting for a !kgiveaway draw"
    )
    _run(_mod("!kgiveaway draw"))
    assert (
        _text(_run(_viewer("!kgiveaway status"))) == "giveaway closed: 2 entered, 1 winner(s) drawn"
    )


@pytest.mark.parametrize(
    "line", ["!kgiveaway", "!kgiveaway status", "!kgiveaway list", "!KGIVEAWAY"]
)
def test_read_forms_are_the_status(host: types.SimpleNamespace, line: str) -> None:
    _open(host)
    assert _text(_run(_viewer(line))).startswith("giveaway open: type")


@pytest.mark.parametrize(
    "line",
    [
        "!kgiveaway bogus",
        "!kgiveaway list extra",
        "!kgiveaway enter now",
        "!kgiveaway close it",
        "!kgiveaway draw 3",
        "!kgiveaway reroll twice",
        "!kgiveaway status please",
        "!kgiveaway set x",
        "!kgiveaway enable x",
    ],
)
def test_malformed_commands_reply_usage_never_silence(
    host: types.SimpleNamespace, line: str
) -> None:
    assert _text(_run(_mod(line))).startswith("Usage: !kgiveaway")
    assert host.kv.calls == []


@pytest.mark.parametrize("text", [None, 5, ["!kgiveaway"], b"!kgiveaway", "", "   "])
def test_non_text_payloads_are_ignored(host: types.SimpleNamespace, text: Any) -> None:
    assert _run(_viewer(text)) is None
    assert host.kv.calls == []


def test_other_commands_are_ignored_unless_they_are_the_keyword(
    host: types.SimpleNamespace,
) -> None:
    _open(host)
    assert _run(_viewer("!raffle")) is None
    assert _run(_viewer("!kgiveawayx")) is None
    assert _entry_keys(host) == []


def test_prize_and_keyword_echoes_are_sanitized(host: types.SimpleNamespace) -> None:
    hostile = "@everyone {user:evil} <@123> done"
    announce = _open(host, KEYWORD, hostile)
    _enter_many(["alice"])
    status = _text(_run(_viewer("!kgiveaway status")))
    winner = _text(_run(_mod("!kgiveaway draw")))
    for reply in (announce, status, winner):
        assert "@everyone" not in reply
        assert "{user:evil}" not in reply
        assert "<@123>" not in reply
        assert "@" + ZWSP + "everyone" in reply
        assert BACKSLASH + "{user:evil" + BACKSLASH + "}" in reply
    # The winner mention itself is the one live placeholder.
    assert "{user:u-alice}" in winner


def test_a_tampered_state_keyword_is_never_trusted(host: types.SimpleNamespace) -> None:
    host.kv.store[STATE_KEY] = json.dumps(
        {"rid": 1, "kw": "@everyone", "prize": "", "open": True, "drawn": []}
    ).encode()
    # `@everyone` fails the keyword grammar, so a tampered state is corrupt (never echoed, never
    # matched against chat); a mod's `open` is the recovery.
    assert "corrupted" in _text(_run(_viewer("!kgiveaway status")))
    assert "giveaway open" in _text(_run(_mod("!kgiveaway open other")))


def test_a_corrupt_state_is_answered_on_commands_and_raises_on_the_ambient_path(
    host: types.SimpleNamespace,
) -> None:
    host.kv.store[STATE_KEY] = b"[1, 2]"
    assert "corrupted" in _text(_run(_viewer("!kgiveaway status")))
    with pytest.raises(_StateCorruptError):
        _run(_viewer(KEYWORD))
    assert "state_corrupt" in _logged(host)


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xfe",
        b"not json",
        b"[]",
        json.dumps({"rid": True, "kw": "win", "prize": "", "open": True, "drawn": []}).encode(),
        json.dumps({"rid": 1, "kw": "win", "prize": "", "open": "true", "drawn": []}).encode(),
        json.dumps({"rid": 1, "kw": "BAD KW", "prize": "", "open": True, "drawn": []}).encode(),
        json.dumps({"rid": 1, "kw": "win", "prize": 3, "open": True, "drawn": []}).encode(),
        json.dumps({"rid": 1, "kw": "win", "prize": "", "open": True, "drawn": [True]}).encode(),
        json.dumps({"rid": 1, "kw": "win", "prize": "", "open": True, "drawn": "x"}).encode(),
        json.dumps({"rid": 1, "kw": "win", "prize": "", "open": True}).encode(),
    ],
)
def test_state_load_is_strict(raw: bytes) -> None:
    with pytest.raises(_StateCorruptError):
        _State.load(raw)


def test_state_round_trips() -> None:
    state = _State(rid=3, keyword="!win", prize="p", open=False, drawn=[2, 5])
    assert _State.load(state.dump()) == state


def test_a_corrupt_slot_counter_is_loud(host: types.SimpleNamespace) -> None:
    _open(host)
    host.kv.store[f"c.{COMMUNITY}.kwgiveaway.n.1"] = b"lots"
    assert "corrupted" in _text(_run(_viewer("!kgiveaway status")))
    assert "count_corrupt" in _logged(host)


def test_kv_failure_on_the_command_path_replies_and_logs_the_class_only(
    host: types.SimpleNamespace,
) -> None:
    host.kv.fail.append(["get", "kwgiveaway.state", 1])
    reply = _text(_run(_viewer("!kgiveaway status")))
    assert reply == "the giveaway is temporarily unavailable, try again shortly."
    logged = _logged(host)
    assert "kwgiveaway.kv_failure" in logged
    assert "Error_Backend" in logged
    assert "host kv error" not in logged


def test_kv_failure_on_the_ambient_path_raises(host: types.SimpleNamespace) -> None:
    host.kv.fail.append(["get", "kwgiveaway.state", 1])
    with pytest.raises(_KvFailureError):
        _run(_viewer(KEYWORD))


@pytest.mark.parametrize(
    ("op", "line"),
    [
        ("increment", "!kgiveaway open winwin"),
        ("set", "!kgiveaway open winwin"),
    ],
)
def test_open_kv_failures_reply_unavailable(
    host: types.SimpleNamespace, op: str, line: str
) -> None:
    host.kv.fail.append([op, "kwgiveaway", 1])
    assert "temporarily unavailable" in _text(_run(_mod(line)))


def test_tenant_wide_sentinel_scopes_under_zero(host: types.SimpleNamespace) -> None:
    assert "giveaway open" in _text(_run(_mod(f"!kgiveaway open {KEYWORD}"), community=None))
    assert "c.0.kwgiveaway.state" in host.kv.store


def test_an_empty_community_id_fails_loud(host: types.SimpleNamespace) -> None:
    with pytest.raises(ValueError, match="community context"):
        _run(_viewer("hello"), community="")


def test_communities_are_isolated(host: types.SimpleNamespace) -> None:
    _run(_mod(f"!kgiveaway open {KEYWORD}"), community="comm-a")
    assert "no giveaway is open" in _text(_run(_viewer("!kgiveaway status"), community="comm-b"))


def test_no_pii_in_keys_values_or_logs(host: types.SimpleNamespace) -> None:
    secret_name, secret_id = "RealStreamerName", "98765432"
    _open(host, "secretkw", "secret prize text")
    assert _run(_event("secretkw", actor=secret_name, author_id=secret_id)) is None
    _run(_mod("!kgiveaway draw"))
    _run(_event("!kgiveaway enter", actor=secret_name, author_id=secret_id))
    blob = " ".join(host.kv.store) + " ".join(v.decode() for v in host.kv.store.values())
    for needle in (secret_name, secret_id):
        assert needle not in blob
        assert needle not in _logged(host)
    for needle in ("secretkw", "secret prize"):
        assert needle not in _logged(host)
    assert all(":" not in key for key in host.kv.store)


def test_every_kv_key_passes_the_real_host_charset(host: types.SimpleNamespace) -> None:
    _entered_giveaway(host, ["alice", "bob"])
    for line in ["!kgiveaway draw", "!kgiveaway reroll", "!kgiveaway status"]:
        _run(_mod(line))
    assert host.kv.calls  # FakeKvHost validates every key and raised on none


def test_dispatch_relays_the_reply(host: types.SimpleNamespace) -> None:
    reply = _run(_viewer("!kgiveaway status"))
    assert reply is not None
    envelope = _envelope(reply)
    result = asyncio.run(dispatch(envelope, {}, http_client=None))
    assert isinstance(result, DispatchResult)
    assert (result.transport, result.detail) == ("twitch", "relayed")
    assert host.relay_calls
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
    envelope = _envelope(event)
    with pytest.raises(ValueError, match="channel_id"):
        asyncio.run(dispatch(envelope, {}, http_client=None))


def test_manifests_and_pyproject_agree() -> None:
    bundle = (BUNDLE_DIR / "bundle.yaml").read_text()
    hub = (BUNDLE_DIR / "hub-manifest.yaml").read_text()
    pyproject = (BUNDLE_DIR / "pyproject.toml").read_text()
    for text in (bundle, hub):
        assert re.search(r"^version: 1\.0\.0$", text, re.MULTILINE)
        assert "app_id: waddles.core.example.keywordgiveaway" in text
    assert 'version = "1.0.0"' in pyproject
    assert 'tested_with: "1.0.0"' in bundle


def test_manifest_consumes_every_chat_line_and_declares_only_kv_and_chat_send() -> None:
    hub = (BUNDLE_DIR / "hub-manifest.yaml").read_text()
    consumes = hub.split("consumes:")[1].split("action:")[0]
    # The keyword path needs every chat line: no `filters:` key may constrain the stage.
    assert "filters:" not in consumes
    ids = re.findall(r"- id: \"?([\w.:]+)\"?", hub)
    assert ids == ["storage.kv", "chat.send:discord", "chat.send:twitch"]


def test_the_bundle_is_activation_gated_only() -> None:
    source = (BUNDLE_DIR / "src" / "app.py").read_text()
    assert "feature_enabled" not in source
    assert "FLAG_KEY" not in source
    assert "flags" not in source.split('"""', 2)[2]


# -- security review regressions: token forgery / parser differential / collisions --------------


def _live_tokens(text: str) -> list[str]:
    """Tokens the real egress detokenizer would substitute in `text` (the SDK port)."""
    from waddle_sdk import safe_text

    return [piece[len("{user:") : -1] for piece, live in safe_text._split_live(text, None) if live]


_HOSTILE_ACTORS = [
    " {user:u-victim}",
    "{user:u-victim} ",
    "{user:u-victim}\n",
    "\t{user:u-victim}",
    "{user:u-victim}{user:u-other}",
    "x{user:u-victim}",
    "{user:u-victim}x",
    "{user:u-victim",
    "{user:}",
    "{user:u victim}",
    "{user:u-victim\\}",
    "\\{user:u-victim\\}",
    "{user:" + chr(0x430) + "}",  # a Cyrillic `a` is not a token character
    chr(0xFF5B) + "user:u-victim" + chr(0xFF5D),  # fullwidth braces
    "{user:u-victim}" + chr(0x200B),
    "{user:" + "a" * 257 + "}",
]


@pytest.mark.parametrize("actor", _HOSTILE_ACTORS)
def test_a_hostile_actor_is_never_stored_or_announced_as_a_live_token(
    host: types.SimpleNamespace, actor: str
) -> None:
    # regression: the winner label is built as `{user:<token>}`; only an actor that is EXACTLY one
    # host-minted placeholder may become a token, so no hostile actor can make the bundle announce
    # (and so ping) someone else.
    _open(host)
    ack = _text(_run(_event("!kgiveaway enter", actor=actor, author_id="777")))
    assert _live_tokens(ack) == []
    assert json.loads(host.kv.store[_entry_keys(host)[0]])["t"] is None
    winner = _text(_run(_mod("!kgiveaway draw")))
    assert _live_tokens(winner) == []
    assert re.search(r"the winner: entrant [0-9a-f]{8} ", winner)


def test_a_genuine_actor_is_the_one_live_token_in_every_reply(host: types.SimpleNamespace) -> None:
    _open(host, KEYWORD, "{user:u-evil} " + BACKSLASH)
    ack = _text(_run(_viewer("!kgiveaway enter", "alice")))
    again = _text(_run(_viewer("!kgiveaway enter", "alice")))
    status = _text(_run(_viewer("!kgiveaway status")))
    winner = _text(_run(_mod("!kgiveaway draw")))
    assert _live_tokens(ack) == _live_tokens(again) == ["u-alice"]
    assert _live_tokens(status) == []  # the prize is sanitized: nothing in it can be live
    assert _live_tokens(winner) == ["u-alice"]


@pytest.mark.parametrize("seed", range(5))
def test_a_tampered_stored_token_is_either_a_hole_or_exactly_the_one_live_token(
    host: types.SimpleNamespace, seed: int
) -> None:
    """Fuzz the stored entry token: whatever kv holds, the announcement never forges a mention."""
    import random

    rng = random.Random(seed)  # noqa: S311 -- deterministic fuzz, not a secret
    atoms = [
        "{",
        "}",
        "\\",
        "{user:",
        "user:",
        "u-x",
        ":",
        ".",
        " ",
        "\n",
        "}{",
        "a" * 60,
        chr(0x430),
    ]
    _entered_giveaway(host, ["alice"])
    key = f"c.{COMMUNITY}.kwgiveaway.e.1.1"
    for _ in range(150):
        token = "".join(rng.choice(atoms) for _ in range(rng.randint(1, 6)))
        host.kv.store[key] = json.dumps({"t": token, "p": "abcd1234"}).encode()
        state = _state(host)
        state["drawn"], state["open"] = [], True
        host.kv.store[STATE_KEY] = json.dumps(state).encode()
        reply = _text(_run(_mod("!kgiveaway draw")))
        live = _live_tokens(reply)
        if "the winner" in reply:
            assert live in ([], [token])
            if live:
                assert re.fullmatch(r"[A-Za-z0-9_:.-]{1,256}", token)
            assert reply.count("{user:") <= 1
        else:
            assert live == []


@pytest.mark.parametrize("keyword", ["!enter", "!raffle", "!giveaway", "!kgiveaway", "!ENTER"])
def test_open_refuses_another_giveaways_own_command_as_a_keyword(
    host: types.SimpleNamespace, keyword: str
) -> None:
    # `raffle` answers `!raffle`/`!enter` and `giveaway` (PR #853) `!giveaway`/`!enter`: a keyword
    # equal to one would make typing that command ALSO enter this giveaway.
    reply = _text(_run(_mod(f"!kgiveaway open {keyword}")))
    assert reply == app._RESERVED_KEYWORD_MSG
    assert STATE_KEY not in host.kv.store


@pytest.mark.parametrize("keyword", ["enter", "raffle", "!win", "win", "!entry"])
def test_the_bare_words_and_other_bang_keywords_stay_allowed(
    host: types.SimpleNamespace, keyword: str
) -> None:
    assert "giveaway open!" in _text(_run(_mod(f"!kgiveaway open {keyword}")))


def test_this_bundles_own_trigger_does_not_overlap_raffle_or_giveaway(
    host: types.SimpleNamespace,
) -> None:
    _open(host)
    for line in ("!raffle", "!raffle enter", "!enter", "!giveaway", "!giveaway enter"):
        assert _run(_viewer(line)) is None, line
    assert _entry_keys(host) == []


@pytest.mark.parametrize(
    "actor", ["/ban victim", ".timeout victim", "!kgiveaway draw", chr(0x200B) + "/ban x"]
)
def test_a_reply_that_would_lead_with_a_command_is_guarded(
    host: types.SimpleNamespace, actor: str
) -> None:
    # regression: an explicit-entry reply LEADS with the entrant's label, and a raw handle that
    # starts with `/`, `.` or `!` would have run as the bot's own chat command.
    _open(host)
    _run(_event("!kgiveaway enter", actor=actor, author_id="9"))
    again = _text(_run(_event("!kgiveaway enter", actor=actor, author_id="9")))
    assert again.startswith("\U0001f4ac ")
    assert not app.guard_leading_command(again).startswith(("/", ".", "!"))
    assert "already entered" in again
