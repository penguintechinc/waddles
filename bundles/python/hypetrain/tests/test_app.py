"""Host-native tests for the `hypetrain` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime: `waddle_sdk.testing.install_fake_kv_host` stands in for the `kv` host
capability (it enforces the real host's key charset, so a `:` key fails here, gh-631) and the
remaining imports (`log`, `relay`) are recording fakes. Event time is driven through
`occurred_at`, so every window/gap assertion is deterministic and needs no sleeping.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from waddle_sdk import kv as sdk_kv
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

import app
from app import dispatch, transform

BASE = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
ANNOUNCE_10 = "Hype x10! Keep the chat moving!"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _at(seconds: float) -> str:
    """RFC 3339 ingest stamp `seconds` after `BASE`, millisecond precision like ingest."""
    stamp = BASE + timedelta(seconds=seconds)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


def _event(
    text: str = "hello chat",
    *,
    actor: str | None = "{user:viewer-0}",
    at: float = 0,
    event_type: str = "chat.message",
    platform: str = "twitch",
    extra: dict[str, Any] | None = None,
    occurred_at: str | None = None,
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": "chan-1"}
    payload.update(extra or {})
    return PlatformEvent(
        platform=platform,
        event_type=event_type,
        actor=actor,
        payload=payload,
        occurred_at=_at(at) if occurred_at is None else occurred_at,
    )


def _epoch(seconds: float) -> int:
    """Whole-second epoch `seconds` after `BASE` -- what the bundle stores for event times."""
    return int((BASE + timedelta(seconds=seconds)).timestamp())


def _distinct_users(count: int) -> list[str]:
    """`count` tokens that hash into pairwise-distinct guard slots (no collision noise)."""
    users: list[str] = []
    slots: set[str] = set()
    for index in range(10_000):
        token = f"{{user:viewer-{index}}}"
        slot = app._slot_key(token)
        if slot not in slots:
            slots.add(slot)
            users.append(token)
        if len(users) == count:
            break
    assert len(users) == count, "could not find enough tokens with distinct guard slots"
    return users


USERS = _distinct_users(40)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Charset-enforcing `kv` fake plus recording `log`/`relay` imports."""
    fake_kv = install_fake_kv_host(monkeypatch)
    wit = sys.modules["wit_world"]
    relay_calls: list[tuple[str, dict[str, Any]]] = []
    log_calls: list[tuple[int, str, str]] = []
    wit.imports.relay = types.SimpleNamespace(  # type: ignore[attr-defined]
        push=lambda provider, msg: relay_calls.append((provider, json.loads(msg)))
    )
    wit.imports.log = types.SimpleNamespace(  # type: ignore[attr-defined]
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields: log_calls.append((lvl, msg, fields)),
    )
    return types.SimpleNamespace(
        kv=fake_kv, relay_calls=relay_calls, log_calls=log_calls, wit=wit
    )


def _say(
    user: str, at: float, text: str = "hello chat", **extra: Any
) -> PlatformEvent | None:
    result: PlatformEvent | None = _run(
        transform(_event(text, actor=user, at=at, **extra))
    )
    return result


def _state(host: types.SimpleNamespace) -> dict[str, Any]:
    raw = host.kv.store.get("hypetrain.state")
    assert raw is not None
    state = json.loads(raw)
    assert isinstance(state, dict)
    return state


def _text(result: PlatformEvent | None) -> str:
    assert result is not None
    text = result.payload["text"]
    assert isinstance(text, str)
    return text


def _logs(host: types.SimpleNamespace) -> str:
    return " ".join(f"{msg} {fields}" for _lvl, msg, fields in host.log_calls)


def _command(
    host: types.SimpleNamespace, text: str = "!hypetrain", at: float = 0, **extra: Any
) -> str:
    return _text(_run(transform(_event(text, at=at, extra=extra or None))))


def test_tuning_constants_are_pinned() -> None:
    assert app.WINDOW_SECONDS == 30
    assert app.ACTOR_GAP_SECONDS == 5
    assert app.MILESTONE_STEP == 10
    assert app.ANNOUNCE_GAP_SECONDS == 20
    assert app.KEYWORD_WEIGHT == 3
    assert app.MESSAGE_WEIGHT == 1
    assert app.SLOT_COUNT == 256


def test_first_message_starts_a_silent_train(host: types.SimpleNamespace) -> None:
    assert _say(USERS[0], 0) is None
    assert _state(host)["c"] == 1
    assert host.relay_calls == []


def test_distinct_chatters_build_one_shared_combo(host: types.SimpleNamespace) -> None:
    for index in range(9):
        assert _say(USERS[index], index) is None
    assert _state(host)["c"] == 9
    assert _state(host)["b"] == 9


def test_the_tenth_contribution_announces_hype_x10(host: types.SimpleNamespace) -> None:
    replies = [_say(USERS[index], index) for index in range(10)]
    assert [r for r in replies[:9] if r is not None] == []
    assert _text(replies[9]) == ANNOUNCE_10


def test_a_milestone_is_announced_once_not_on_every_message(
    host: types.SimpleNamespace,
) -> None:
    replies = [_say(USERS[index], index) for index in range(19)]
    announced = [_text(r) for r in replies if r is not None]
    assert announced == [ANNOUNCE_10]


def test_milestones_are_rate_limited_then_announced_once_the_gap_passes(
    host: types.SimpleNamespace,
) -> None:
    announced: list[tuple[int, str]] = []
    for index in range(30):
        reply = _say(USERS[index], index)
        if reply is not None:
            announced.append((index, _text(reply)))
    # x10 at t=9; x20 is reached at t=19 but only 10s later -> suppressed. By t=29 the gap has
    # passed and the combo is 30, so the HIGHEST milestone reached (x30) is the one announced.
    assert announced == [(9, ANNOUNCE_10), (29, "Hype x30! Keep the chat moving!")]


def test_a_suppressed_milestone_announces_the_highest_one_reached(
    host: types.SimpleNamespace,
) -> None:
    announced: list[str] = []
    for index in range(40):
        reply = _say(USERS[index], index)
        if reply is not None:
            announced.append(_text(reply))
    # x20 (t=19) is suppressed by the gap and superseded by x30 (t=29); x40 (t=39) is only 10s
    # after that announcement, so it stays pending.
    assert announced == [ANNOUNCE_10, "Hype x30! Keep the chat moving!"]


def test_announcement_spacing_is_exactly_the_gap(host: types.SimpleNamespace) -> None:
    for index in range(10):
        _say(USERS[index], index)  # x10 announced at t=9
    state = _state(host)
    assert state["t"] == _epoch(9)
    for index in range(10, 20):
        reply = _say(USERS[index], index)
        assert reply is None  # x20 at t=19 is 10s after the x10 announcement
    assert _say(USERS[20], 28) is None  # 19s after: still inside the gap
    assert _text(_say(USERS[21], 29)) == "Hype x20! Keep the chat moving!"


def test_an_announced_milestone_is_not_repeated_once_the_gap_has_passed(
    host: types.SimpleNamespace,
) -> None:
    for index in range(10):
        _say(USERS[index], index)  # x10 announced at t=9
    # 26s later (gap long over, train still inside its window): x10 is already announced
    assert _say(USERS[10], 35) is None
    assert _say(USERS[11], 60) is None
    state = _state(host)
    assert state["c"] == 12
    assert state["m"] == 10


def test_reset_clears_the_announced_milestone_so_x10_can_fire_again(
    host: types.SimpleNamespace,
) -> None:
    for index in range(10):
        _say(USERS[index], index)
    assert _state(host)["m"] == 10
    _command(host, "!hypetrain reset", at=10, is_mod=True)
    assert _state(host)["m"] == 0
    second = [_say(USERS[index], 40 + index) for index in range(10)]
    assert [_text(r) for r in second if r is not None] == [ANNOUNCE_10]


def test_the_actor_guard_refuses_an_older_line_even_inside_the_window(
    host: types.SimpleNamespace,
) -> None:
    _say(USERS[0], 100)
    _say(USERS[0], 90)  # 10s OLDER than the claimed slot time: not 'a gap', a replay
    assert _state(host)["c"] == 1


def test_the_same_chatter_counts_once_per_gap(host: types.SimpleNamespace) -> None:
    for second in range(0, 5):
        _say(USERS[0], second)
    assert _state(host)["c"] == 1
    _say(USERS[0], 5)
    assert _state(host)["c"] == 2
    _say(USERS[0], 9.9)
    assert _state(host)["c"] == 2


def test_a_lone_spammer_is_rate_capped_to_one_count_per_gap(
    host: types.SimpleNamespace,
) -> None:
    # 10 lines per second for a minute: only one line per 5s counts (t=0,5,...,55 -> 12), so the
    # combo grows at 12/min at most and the single milestone it reaches is announced exactly once.
    replies = [_say(USERS[0], tenth / 10) for tenth in range(0, 600)]
    announced = [_text(r) for r in replies if r is not None]
    assert _state(host)["c"] == 12
    assert announced == [ANNOUNCE_10]


def test_the_guard_is_signed_so_a_replayed_older_line_is_refused(
    host: types.SimpleNamespace,
) -> None:
    _say(USERS[0], 100)
    _say(USERS[0], 0)
    assert _state(host)["c"] == 1


def test_two_chatters_in_one_slot_under_count_never_over_count(
    host: types.SimpleNamespace,
) -> None:
    first = "{user:collide-0}"
    twin = next(
        f"{{user:collide-{i}}}"
        for i in range(1, 5000)
        if app._slot_key(f"{{user:collide-{i}}}") == app._slot_key(first)
    )
    _say(first, 0)
    _say(twin, 1)
    assert _state(host)["c"] == 1


def test_the_train_survives_exactly_the_window_then_resets(
    host: types.SimpleNamespace,
) -> None:
    _say(USERS[0], 0)
    _say(USERS[1], 30)
    assert _state(host)["c"] == 2
    _say(USERS[2], 61)  # 31s after the last counted contribution -> a new train
    state = _state(host)
    assert state["c"] == 1
    assert state["b"] == 2


def test_a_new_train_can_announce_the_same_milestone_again(
    host: types.SimpleNamespace,
) -> None:
    first = [_say(USERS[index], index) for index in range(10)]
    assert _text(first[9]) == ANNOUNCE_10
    # silence for longer than the window, and past the announcement gap
    second = [_say(USERS[index], 100 + index) for index in range(10)]
    assert [_text(r) for r in second if r is not None] == [ANNOUNCE_10]
    assert _state(host)["b"] == 10


def test_a_milestone_announcement_gap_also_spans_trains(
    host: types.SimpleNamespace,
) -> None:
    for index in range(10):
        _say(USERS[index], index)  # announced at t=9
    # the next train starts 31s later and reaches x10 within 20s of... no: 31s > gap
    replies = [_say(USERS[index], 41 + index) for index in range(10)]
    assert [_text(r) for r in replies if r is not None] == [ANNOUNCE_10]


def test_the_best_combo_survives_resets(host: types.SimpleNamespace) -> None:
    for index in range(5):
        _say(USERS[index], index)
    _say(USERS[6], 500)
    state = _state(host)
    assert (state["c"], state["b"]) == (1, 5)


def test_a_stale_replayed_event_is_ignored(host: types.SimpleNamespace) -> None:
    _say(USERS[0], 1000)
    _say(USERS[1], 0)
    state = _state(host)
    assert state["c"] == 1
    assert state["l"] == _epoch(1000)
    assert "hypetrain.skip" in _logs(host)


def test_a_slightly_older_event_inside_the_window_still_counts(
    host: types.SimpleNamespace,
) -> None:
    _say(USERS[0], 100)
    _say(USERS[1], 90)
    state = _state(host)
    assert state["c"] == 2
    assert state["l"] == _epoch(100)


def test_a_redelivered_event_does_not_double_count(host: types.SimpleNamespace) -> None:
    event = _event(actor=USERS[0], at=3)
    for _ in range(5):
        _run(transform(event))
    assert _state(host)["c"] == 1


@pytest.mark.parametrize(
    "keyword", ["!hype", "!HYPE", "!Hype", "!hype now", "  !hype  "]
)
def test_the_keyword_counts_as_a_boosted_contribution(
    host: types.SimpleNamespace, keyword: str
) -> None:
    assert _say(USERS[0], 0, keyword) is None
    assert _state(host)["c"] == app.KEYWORD_WEIGHT == 3


@pytest.mark.parametrize("text", ["!hypez", "!hyper", "hype", "!hype!", "I love !hype"])
def test_only_the_exact_leading_keyword_boosts(
    host: types.SimpleNamespace, text: str
) -> None:
    _say(USERS[0], 0, text)
    assert _state(host)["c"] == 1


def test_a_keyword_can_jump_across_a_milestone(host: types.SimpleNamespace) -> None:
    for index in range(8):
        _say(USERS[index], index)
    reply = _say(USERS[9], 8, "!hype")
    assert _text(reply) == ANNOUNCE_10
    assert _state(host)["c"] == 11


def test_the_keyword_does_not_bypass_the_actor_gap(host: types.SimpleNamespace) -> None:
    _say(USERS[0], 0, "!hype")
    _say(USERS[0], 1, "!hype")
    assert _state(host)["c"] == 3


def test_status_before_any_train(host: types.SimpleNamespace) -> None:
    assert _command(host) == "No hype train yet - chat (or say !hype) to start one!"
    assert "hypetrain.state" not in host.kv.store


def test_status_of_a_running_train(host: types.SimpleNamespace) -> None:
    for index in range(4):
        _say(USERS[index], index)
    reply = _command(host, at=13)
    assert reply == "Hype train: x4 (best x4) - 20s left, keep chatting!"


def test_status_at_the_last_moment_of_the_window(host: types.SimpleNamespace) -> None:
    _say(USERS[0], 0)
    assert _command(host, at=30) == "Hype train: x1 (best x1) - 0s left, keep chatting!"
    expired = _command(host, at=31)
    assert expired == "No hype train running. Best so far: x1. Chat to start one!"


def test_status_does_not_count_as_a_contribution(host: types.SimpleNamespace) -> None:
    _say(USERS[0], 0)
    before = host.kv.store["hypetrain.state"]
    for second in range(1, 20):
        _command(host, at=second)
    assert host.kv.store["hypetrain.state"] == before
    assert not [k for k in host.kv.store if k.startswith("hypetrain.slot.")][1:]


@pytest.mark.parametrize("extra", [{"is_mod": True}, {"is_broadcaster": True}])
def test_mods_and_the_broadcaster_can_reset(
    host: types.SimpleNamespace, extra: dict[str, Any]
) -> None:
    for index in range(6):
        _say(USERS[index], index)
    assert _command(host, "!hypetrain reset", at=7, **extra) == "Hype train reset."
    state = _state(host)
    assert (state["c"], state["m"], state["b"]) == (0, 0, 6)
    assert _command(host, at=8).startswith("No hype train running")


def test_reset_keeps_announcement_spacing_and_the_next_line_starts_fresh(
    host: types.SimpleNamespace,
) -> None:
    for index in range(10):
        _say(USERS[index], index)
    _command(host, "!hypetrain reset", at=10, is_mod=True)
    assert _state(host)["t"] == _epoch(9)
    _say(USERS[11], 12)
    assert _state(host)["c"] == 1


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"is_mod": False},
        {"is_mod": "false"},
        {"is_mod": "true"},
        {"is_mod": 1},
        {"is_broadcaster": "yes"},
        {"is_mod": None, "is_broadcaster": None},
        {"is_mod": [True]},
    ],
)
def test_reset_denies_everything_but_a_strict_true_badge(
    host: types.SimpleNamespace, extra: dict[str, Any]
) -> None:
    _say(USERS[0], 0)
    before = host.kv.store["hypetrain.state"]
    assert _command(host, "!hypetrain reset", at=1, **extra) == app._DENIED
    assert host.kv.store["hypetrain.state"] == before


@pytest.mark.parametrize(
    "text",
    [
        "!hypetrain reset now",
        "!hypetrain boost",
        "!hypetrain set 5",
        "!hypetrain add 3",
        "!hypetrain enable ai",
        "!hypetrain @everyone",
        "!hypetrain <@123> {user:victim}",
    ],
)
def test_unsupported_shapes_get_usage_and_no_echo(
    host: types.SimpleNamespace, text: str
) -> None:
    reply = _command(host, text, is_mod=True)
    assert reply == app._USAGE
    for needle in ("everyone", "victim", "<@", "boost"):
        assert needle not in reply


def test_command_matching_is_case_insensitive_and_exact(
    host: types.SimpleNamespace,
) -> None:
    assert _command(host, "!HYPETRAIN").startswith("No hype train")
    assert _say(USERS[0], 0, "!hypetrains") is None
    assert _state(host)["c"] == 1


@pytest.mark.parametrize(
    "actor",
    [None, "", "viewer", "SomeStreamer99", "{user:}", "\\{user:x\\}", "{user:a b}"],
)
def test_unidentifiable_actors_are_not_counted_and_touch_no_key(
    host: types.SimpleNamespace, actor: str | None
) -> None:
    assert _run(transform(_event(actor=actor))) is None
    assert host.kv.store == {}
    assert "SomeStreamer99" not in _logs(host)


def test_non_chat_events_and_payloads_are_ignored(host: types.SimpleNamespace) -> None:
    assert _run(transform(_event(event_type="chat.edit"))) is None
    no_text = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=USERS[0],
        payload={},
        occurred_at=_at(0),
    )
    assert _run(transform(no_text)) is None
    non_str = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=USERS[0],
        payload={"text": 7},
        occurred_at=_at(0),
    )
    assert _run(transform(non_str)) is None
    assert host.kv.store == {}


@pytest.mark.parametrize("stamp", ["", "not-a-date", "2026-13-45T99:00:00Z"])
def test_a_missing_or_malformed_stamp_is_logged_not_counted(
    host: types.SimpleNamespace, stamp: str
) -> None:
    assert _run(transform(_event(actor=USERS[0], occurred_at=stamp))) is None
    assert host.kv.store == {}
    assert "hypetrain.bad_timestamp" in _logs(host)


def test_status_with_an_unreadable_stamp_says_so(host: types.SimpleNamespace) -> None:
    reply = _text(_run(transform(_event("!hypetrain", occurred_at="garbage"))))
    assert reply == app._NO_CLOCK
    assert "hypetrain.bad_timestamp" in _logs(host)


def test_a_naive_stamp_is_read_as_utc(host: types.SimpleNamespace) -> None:
    _run(transform(_event(actor=USERS[0], occurred_at="2026-10-20T12:00:00")))
    assert _state(host)["l"] == _epoch(0)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"c": 1}',
        b'{"c": -1, "l": 0, "b": 0, "m": 0, "t": null}',
        b'{"c": true, "l": 0, "b": 0, "m": 0, "t": null}',
        b'{"c": "3", "l": 0, "b": 0, "m": 0, "t": null}',
        b'{"c": 1, "l": 0, "b": 0, "m": 0, "t": "x"}',
        b"\xff\xfe",
    ],
)
def test_corrupt_state_is_logged_loudly_and_replaced(
    host: types.SimpleNamespace, raw: bytes
) -> None:
    host.kv.store["hypetrain.state"] = raw
    assert _command(host).startswith("No hype train yet")
    assert "hypetrain.state_corrupt" in _logs(host)
    _say(USERS[0], 0)
    assert _state(host)["c"] == 1


def test_a_corrupt_slot_value_fails_loud_without_counting(
    host: types.SimpleNamespace,
) -> None:
    host.kv.store[app._slot_key(USERS[0])] = b"garbage"
    assert _say(USERS[0], 0) is None
    assert "hypetrain.state" not in host.kv.store
    assert "hypetrain.kv_failure" in _logs(host)


def test_command_path_kv_failure_is_answered_and_logged(
    host: types.SimpleNamespace,
) -> None:
    def boom(*_args: Any) -> Any:
        raise RuntimeError("scripted kv failure for viewer-0")

    host.wit.imports.kv.get = boom
    assert _command(host) == app._UNAVAILABLE
    logs = _logs(host)
    assert "hypetrain.kv_failure" in logs
    assert "viewer-0" not in logs
    assert "scripted" not in logs


def test_passive_kv_failure_is_logged_at_error_and_never_raises(
    host: types.SimpleNamespace,
) -> None:
    def boom(*_args: Any) -> Any:
        raise RuntimeError("down")

    host.wit.imports.kv.set = boom
    assert _say(USERS[0], 0) is None
    assert any(
        lvl == 0 and msg == "hypetrain.kv_failure" for lvl, msg, _ in host.log_calls
    )


def test_kv_keys_and_values_carry_no_identity(host: types.SimpleNamespace) -> None:
    for index in range(12):
        _say(USERS[index], index, "my secret message")
    pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
    keys = set(host.kv.store)
    assert "hypetrain.state" in keys
    for key in keys:
        assert pattern.match(key), key
        assert "viewer" not in key
    blob = b" ".join(host.kv.store.values())
    for needle in (b"viewer", b"secret", b"user", b"{"):
        if needle == b"{":
            continue  # the state value is a JSON object of integers
        assert needle not in blob
    assert re.fullmatch(
        rb"\{(?:\"[clbmt]\":(?:null|\d+),?)+\}", host.kv.store["hypetrain.state"]
    )
    logs = _logs(host)
    for needle in ("viewer", "secret", "{user"):
        assert needle not in logs


def test_the_key_footprint_is_bounded_by_the_slot_count(
    host: types.SimpleNamespace,
) -> None:
    for index in range(2000):
        _say(f"{{user:crowd-{index}}}", index * 10)
    slots = [k for k in host.kv.store if k.startswith(app.SLOT_KEY_PREFIX)]
    assert len(slots) <= app.SLOT_COUNT
    assert len(host.kv.store) <= app.SLOT_COUNT + 1


def test_counting_stays_far_inside_the_host_op_budget(
    host: types.SimpleNamespace,
) -> None:
    host.kv.calls.clear()
    _say(USERS[0], 0)
    assert len(host.kv.calls) <= 6 < 64


def test_a_gated_line_costs_one_kv_read(host: types.SimpleNamespace) -> None:
    _say(USERS[0], 0)
    host.kv.calls.clear()
    _say(USERS[0], 1)
    assert [op for op, _ in host.kv.calls] == ["get"]


def test_every_write_is_durable_no_ttl(host: types.SimpleNamespace) -> None:
    for index in range(12):
        _say(USERS[index], index)
    sets = [call for call in host.kv.calls if call[0] == "set"]
    assert sets
    assert {call[1][2] for call in sets} == {0}
    assert not [call for call in host.kv.calls if call[0] == "increment"]


def test_concurrent_contributions_never_corrupt_the_state(
    host: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = {name: getattr(sdk_kv, name) for name in ("get", "set")}

    def yielding(name: str) -> Any:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(0)
            result = await real[name](*args, **kwargs)
            await asyncio.sleep(0)
            return result

        return wrapper

    for name in real:
        monkeypatch.setattr(sdk_kv, name, yielding(name))

    async def burst() -> None:
        await asyncio.gather(*(transform(_event(actor=u, at=1)) for u in USERS[:12]))

    _run(burst())
    state = _state(host)
    assert 1 <= state["c"] <= 12  # lost updates only ever under-count (documented)
    assert state["b"] == state["c"]


def test_replies_never_echo_chatter_text(host: types.SimpleNamespace) -> None:
    hostile = "@everyone <@123> {user:victim} <!here> \\{user:x\\}"
    for text in (f"!hypetrain {hostile}", f"!hypetrain reset {hostile}"):
        reply = _command(host, text, is_mod=True)
        assert "everyone" not in reply
        assert "victim" not in reply


def test_the_final_sanitizer_is_a_no_op_on_the_real_templates(
    host: types.SimpleNamespace,
) -> None:
    replies = [
        app._USAGE,
        app._DENIED,
        app._UNAVAILABLE,
        app._NO_CLOCK,
        ANNOUNCE_10,
        "Hype train: x4 (best x4) - 20s left, keep chatting!",
        "No hype train yet - chat (or say !hype) to start one!",
    ]
    for text in replies:
        event = app._reply_event(_event(), text)
        assert event.payload["text"] == text


def test_reply_event_neutralizes_forged_markup() -> None:
    cooked = app._reply_event(_event(), "x {user:victim} @everyone").payload["text"]
    assert "{user:victim}" not in cooked
    assert "@​everyone" in cooked


def _envelope(event: PlatformEvent) -> StageEnvelope:
    return StageEnvelope(
        tenant="t",
        community="c",
        app_id="waddles.core.example.hypetrain",
        stage="action",
        event=event,
        ts=_at(0),
    )


def test_dispatch_relays_an_announcement_to_the_channel(
    host: types.SimpleNamespace,
) -> None:
    replies = [_say(USERS[index], index) for index in range(10)]
    announcement = replies[9]
    result = _run(dispatch(_envelope(announcement), {}, http_client=None))
    assert result.transport == "twitch"
    assert result.detail == "relayed"
    assert host.relay_calls == [("twitch", {"channel": "chan-1", "text": ANNOUNCE_10})]


def test_dispatch_rejects_a_payload_without_channel_or_text(
    host: types.SimpleNamespace,
) -> None:
    no_channel = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=USERS[0],
        payload={"text": "x"},
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope(no_channel), {}, http_client=None))
    no_text = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=USERS[0],
        payload={"channel_id": "c"},
    )
    with pytest.raises(ValueError, match="text"):
        _run(dispatch(_envelope(no_text), {}, http_client=None))
    assert host.relay_calls == []


def test_entry_wiring_exports_the_stage_functions() -> None:
    import _entry_wiring

    assert _entry_wiring.bundle_transform is transform
    assert _entry_wiring.bundle_dispatch is dispatch


def test_fake_host_is_the_charset_enforcing_one(host: types.SimpleNamespace) -> None:
    assert isinstance(host.kv, FakeKvHost)
    with pytest.raises(Exception, match="characters outside"):
        host.kv.get("hypetrain:bad")
