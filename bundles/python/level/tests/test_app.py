"""Host-native tests for the `level` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime: `waddle_sdk.testing.install_fake_kv_host` stands in for the `kv` host
capability (it enforces the real host's key charset, so a `:` key fails here, gh-631) and the
remaining imports (`log`, `relay`) are recording fakes. Event time is driven through
`occurred_at`, so every cooldown assertion is deterministic and needs no sleeping.
"""

from __future__ import annotations

import asyncio
import hashlib
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
from app import dispatch, level_for_xp, transform, xp_for_level

BASE = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
ALICE = "{user:alice-uuid}"
BOB = "{user:bob-uuid}"
CAROL = "{user:carol:handle.1}"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _at(seconds: float) -> str:
    """RFC 3339 ingest stamp `seconds` after `BASE`, millisecond precision like ingest."""
    stamp = BASE + timedelta(seconds=seconds)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


def _event(
    text: str = "hello chat",
    *,
    actor: str | None = ALICE,
    at: float = 0,
    event_type: str = "chat.message",
    platform: str = "twitch",
    channel_id: str | None = "chan-1",
    occurred_at: str | None = None,
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text}
    if channel_id is not None:
        payload["channel_id"] = channel_id
    return PlatformEvent(
        platform=platform,
        event_type=event_type,
        actor=actor,
        payload=payload,
        occurred_at=_at(at) if occurred_at is None else occurred_at,
    )


def _pseudonym(user_id: str) -> str:
    """The key component the bundle derives: SHA-256 of `level|<bare id>`."""
    bare = user_id.removeprefix("{user:").removesuffix("}")
    return hashlib.sha256(f"level|{bare}".encode()).hexdigest()


def _xp_key(user_id: str) -> str:
    return f"level.xp.{_pseudonym(user_id)}"


def _ts_key(user_id: str) -> str:
    return f"level.ts.{_pseudonym(user_id)}"


def _lock_key(user_id: str) -> str:
    return f"level.lock.{_pseudonym(user_id)}"


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


def _xp(host: types.SimpleNamespace, user_id: str) -> int:
    raw = host.kv.store.get(_xp_key(user_id))
    return int(raw) if raw is not None else 0


def _text(result: PlatformEvent | None) -> str:
    assert result is not None
    text = result.payload["text"]
    assert isinstance(text, str)
    return text


def _logs(host: types.SimpleNamespace) -> str:
    return " ".join(f"{msg} {fields}" for _lvl, msg, fields in host.log_calls)


def _board(host: types.SimpleNamespace) -> list[list[Any]]:
    raw = host.kv.store.get("level.board")
    assert raw is not None
    board = json.loads(raw)
    assert isinstance(board, list)
    return board


def _chat(user_id: str, at: float, text: str = "hello chat") -> PlatformEvent:
    return _event(text, actor=user_id, at=at)


def _seed_board(host: types.SimpleNamespace, entries: list[tuple[str, int]]) -> None:
    host.kv.store["level.board"] = json.dumps([[u, x] for u, x in entries]).encode()


def test_tuning_constants_are_pinned() -> None:
    assert app.XP_PER_MESSAGE == 5
    assert app.COOLDOWN_SECONDS == 30
    assert app.LEVEL_BASE_XP == 100
    assert app.LEADERBOARD_SIZE == 5


@pytest.mark.parametrize(
    ("xp", "level"),
    [
        (-5, 1),
        (0, 1),
        (99, 1),
        (100, 2),
        (399, 2),
        (400, 3),
        (899, 3),
        (900, 4),
        (1599, 4),
    ],
)
def test_level_curve_boundaries(xp: int, level: int) -> None:
    assert level_for_xp(xp) == level


@pytest.mark.parametrize("level", list(range(1, 60)))
def test_xp_for_level_is_the_exact_inverse(level: int) -> None:
    needed = xp_for_level(level)
    assert level_for_xp(needed) == level
    if level > 1:
        assert level_for_xp(needed - 1) == level - 1


def test_first_message_earns_xp_and_stays_silent(host: types.SimpleNamespace) -> None:
    assert _run(transform(_chat(ALICE, 0))) is None
    assert _xp(host, ALICE) == 5
    assert host.relay_calls == []


def test_rapid_messages_inside_the_cooldown_do_not_farm_xp(
    host: types.SimpleNamespace,
) -> None:
    for second in range(0, 30):
        _run(transform(_chat(ALICE, second)))
    assert _xp(host, ALICE) == 5


def test_hundreds_of_messages_in_one_second_award_once(
    host: types.SimpleNamespace,
) -> None:
    for _ in range(300):
        _run(transform(_chat(ALICE, 0)))
    assert _xp(host, ALICE) == 5


def test_cooldown_spam_path_costs_one_kv_read(host: types.SimpleNamespace) -> None:
    _run(transform(_chat(ALICE, 0)))
    host.kv.calls.clear()
    _run(transform(_chat(ALICE, 1)))
    assert [op for op, _ in host.kv.calls] == ["get"]


def test_cooldown_boundary_is_exactly_thirty_seconds(
    host: types.SimpleNamespace,
) -> None:
    _run(transform(_chat(ALICE, 0)))
    _run(transform(_chat(ALICE, 29.9)))
    assert _xp(host, ALICE) == 5
    _run(transform(_chat(ALICE, 30)))
    assert _xp(host, ALICE) == 10


def test_a_steady_chatter_keeps_earning_every_cooldown(
    host: types.SimpleNamespace,
) -> None:
    # regression: a TTL'd kv claim re-applies its TTL on every refused call, so a chatter
    # posting every 20s against a 30s cooldown would be starved after the first award.
    # Messages land every 20s, so awards fall every 40s: t=0, 40, ..., 560 -> 15 awards.
    for second in range(0, 600, 20):
        _run(transform(_chat(ALICE, second)))
    assert _xp(host, ALICE) == 5 * 15


def test_a_replayed_older_event_neither_awards_nor_rewinds_the_stamp(
    host: types.SimpleNamespace,
) -> None:
    _run(transform(_chat(ALICE, 1000)))
    _run(transform(_chat(ALICE, 0)))
    assert _xp(host, ALICE) == 5
    assert (
        host.kv.store[_ts_key(ALICE)]
        == str(int((BASE + timedelta(seconds=1000)).timestamp())).encode()
    )
    _run(transform(_chat(ALICE, 1010)))
    assert _xp(host, ALICE) == 5


def test_a_redelivered_event_is_idempotent(host: types.SimpleNamespace) -> None:
    event = _chat(ALICE, 5)
    _run(transform(event))
    _run(transform(event))
    _run(transform(event))
    assert _xp(host, ALICE) == 5


def test_chatters_accrue_independently(host: types.SimpleNamespace) -> None:
    _run(transform(_chat(ALICE, 0)))
    _run(transform(_chat(BOB, 1)))
    _run(transform(_chat(ALICE, 31)))
    assert _xp(host, ALICE) == 10
    assert _xp(host, BOB) == 5


def test_lock_key_is_released_after_every_award(host: types.SimpleNamespace) -> None:
    _run(transform(_chat(ALICE, 0)))
    _run(transform(_chat(ALICE, 31)))
    assert not [k for k in host.kv.store if k.startswith("level.lock.")]


def test_a_held_lock_defers_the_award(host: types.SimpleNamespace) -> None:
    host.kv.store[_lock_key(ALICE)] = b"1"
    _run(transform(_chat(ALICE, 0)))
    assert _xp(host, ALICE) == 0
    assert _ts_key(ALICE) not in host.kv.store


def test_the_lock_carries_a_crash_backstop_ttl(host: types.SimpleNamespace) -> None:
    _run(transform(_chat(ALICE, 0)))
    locks = [
        c for c in host.kv.calls if c[0] == "increment" and c[1][0] == _lock_key(ALICE)
    ]
    assert [c[1] for c in locks] == [(_lock_key(ALICE), 1, app.LOCK_TTL_SECONDS)]
    xp_calls = [
        c for c in host.kv.calls if c[0] == "increment" and c[1][0] == _xp_key(ALICE)
    ]
    assert [c[1][2] for c in xp_calls] == [0]


def test_concurrent_messages_from_one_chatter_award_exactly_once(
    host: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = {
        name: getattr(sdk_kv, name) for name in ("get", "set", "delete", "increment")
    }

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
        await asyncio.gather(*(transform(_chat(ALICE, 0)) for _ in range(8)))

    _run(burst())
    assert _xp(host, ALICE) == 5
    assert not [k for k in host.kv.store if k.startswith("level.lock.")]


def test_a_late_lock_winner_rechecks_the_cooldown_it_read_before_the_lock(
    host: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reads a stale stamp, B awards and unlocks, A then wins the lock -- A must not award."""
    real_get = sdk_kv.get

    async def main() -> None:
        gate = asyncio.Event()
        paused = {"done": False}

        async def gated_get(key: str) -> bytes | None:
            value = await real_get(key)
            if key.startswith("level.ts.") and not paused["done"]:
                paused["done"] = True
                await gate.wait()
            return value

        monkeypatch.setattr(sdk_kv, "get", gated_get)
        first = asyncio.create_task(transform(_chat(ALICE, 0)))
        for _ in range(3):
            await asyncio.sleep(0)
        assert paused["done"], "task A should be parked after its stale read"
        await transform(_chat(ALICE, 0))
        gate.set()
        await first

    _run(main())
    assert _xp(host, ALICE) == 5
    assert not [k for k in host.kv.store if k.startswith("level.lock.")]


def test_non_chat_events_and_payloads_are_ignored(host: types.SimpleNamespace) -> None:
    assert _run(transform(_event(event_type="chat.edit"))) is None
    no_text = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=ALICE,
        payload={},
        occurred_at=_at(0),
    )
    assert _run(transform(no_text)) is None
    non_str = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=ALICE,
        payload={"text": 12},
        occurred_at=_at(0),
    )
    assert _run(transform(non_str)) is None
    assert host.kv.store == {}


@pytest.mark.parametrize(
    "actor", [None, "", "alice", "Alice_Streams", "{user:}", "\\{user:x\\}"]
)
def test_unidentifiable_actors_earn_nothing_and_touch_no_key(
    host: types.SimpleNamespace, actor: str | None
) -> None:
    assert _run(transform(_event(actor=actor))) is None
    assert host.kv.store == {}


@pytest.mark.parametrize("stamp", ["", "not-a-date", "2026-13-45T99:00:00Z"])
def test_a_missing_or_malformed_stamp_is_logged_not_awarded(
    host: types.SimpleNamespace, stamp: str
) -> None:
    assert _run(transform(_event(occurred_at=stamp))) is None
    assert host.kv.store == {}
    assert "level.bad_timestamp" in _logs(host)


def test_a_naive_stamp_is_read_as_utc(host: types.SimpleNamespace) -> None:
    _run(transform(_event(occurred_at="2026-10-20T12:00:00")))
    assert host.kv.store[_ts_key(ALICE)] == str(int(BASE.timestamp())).encode()


def test_level_command_shows_xp_and_level_for_the_caller(
    host: types.SimpleNamespace,
) -> None:
    for second in range(0, 30 * 25, 30):
        _run(transform(_chat(ALICE, second)))
    assert _xp(host, ALICE) == 125
    reply = _text(_run(transform(_event("!level", at=900))))
    assert reply == f"{ALICE} is level 2 with 125 XP (275 XP to level 3)."


def test_level_command_with_no_xp_yet(host: types.SimpleNamespace) -> None:
    reply = _text(_run(transform(_event("!level"))))
    assert reply == f"{ALICE} is level 1 with 0 XP (100 XP to level 2)."


def test_level_command_reads_before_it_accrues_and_then_accrues(
    host: types.SimpleNamespace,
) -> None:
    reply = _text(_run(transform(_event("!level", at=0))))
    assert "with 0 XP" in reply
    assert _xp(host, ALICE) == 5


def test_level_of_a_mentioned_user(host: types.SimpleNamespace) -> None:
    for second in (0, 30, 60):
        _run(transform(_chat(BOB, second)))
    reply = _text(_run(transform(_event(f"!level {BOB}", at=70))))
    assert reply == f"{BOB} is level 1 with 15 XP (85 XP to level 2)."
    assert _xp(host, ALICE) == 5


def test_a_mention_token_with_colons_and_dots_is_a_valid_target(
    host: types.SimpleNamespace,
) -> None:
    _run(transform(_chat(CAROL, 0)))
    reply = _text(_run(transform(_event(f"!level {CAROL}", at=1))))
    assert reply.startswith(f"{CAROL} is level 1 with 5 XP")


@pytest.mark.parametrize(
    "arg",
    ["bob", "@bob", "@everyone", "@here", "<@123456789>", "\\{user:bob\\}", "{user:}"],
)
def test_a_non_token_target_is_refused_without_echo(
    host: types.SimpleNamespace, arg: str
) -> None:
    reply = _text(_run(transform(_event(f"!level {arg}"))))
    assert reply == app._NO_NAME_LOOKUP
    assert arg not in reply


def test_level_for_an_unidentifiable_caller_says_so(
    host: types.SimpleNamespace,
) -> None:
    reply = _text(_run(transform(_event("!level", actor=None))))
    assert reply == app._NO_SELF_ID
    assert host.kv.store == {}


@pytest.mark.parametrize(
    "text",
    [
        "!level add 5",
        "!level reset",
        "!level set 100 @x",
        "!level leaderboard reset",
        "!level a b",
        "!level top extra",
        "!level enable ai",
        "!level {user:a b}",
    ],
)
def test_unsupported_shapes_get_usage_and_no_echo(
    host: types.SimpleNamespace, text: str
) -> None:
    reply = _text(_run(transform(_event(text))))
    assert reply == app._USAGE
    assert "@x" not in reply


def test_command_matching_is_case_insensitive_and_exact(
    host: types.SimpleNamespace,
) -> None:
    assert _text(_run(transform(_event("!LEVEL")))).startswith(ALICE)
    assert _run(transform(_event("!levels", at=100))) is None
    assert _run(transform(_event("!levelup now", at=200))) is None
    assert _run(transform(_event("level", at=300))) is None


def test_command_lines_still_earn_xp_but_other_bundles_commands_do_too(
    host: types.SimpleNamespace,
) -> None:
    _run(transform(_event("!rank", at=0)))
    assert _xp(host, ALICE) == 5


def test_empty_board_message(host: types.SimpleNamespace) -> None:
    reply = _text(_run(transform(_event("!level leaderboard", actor=None))))
    assert reply == app._EMPTY_BOARD


def test_leaderboard_orders_by_xp_and_renders_mention_tokens(
    host: types.SimpleNamespace,
) -> None:
    for second in (0, 30, 60):
        _run(transform(_chat(ALICE, second)))
    for second in (0, 30, 60, 90, 120):
        _run(transform(_chat(BOB, second)))
    _run(transform(_chat(CAROL, 0)))
    reply = _text(_run(transform(_event("!level leaderboard", actor=None, at=1000))))
    assert reply == (
        f"Top levels: 1. {BOB} L1 (25 XP) | 2. {ALICE} L1 (15 XP) | 3. {CAROL} L1 (5 XP)"
    )


def test_top_is_an_alias_for_leaderboard(host: types.SimpleNamespace) -> None:
    _run(transform(_chat(ALICE, 0)))
    board = _text(_run(transform(_event("!level leaderboard", actor=None))))
    assert _text(_run(transform(_event("!level top", actor=None)))) == board


def test_leaderboard_is_capped_and_evicts_the_floor(
    host: types.SimpleNamespace,
) -> None:
    users = [f"{{user:u{i}}}" for i in range(7)]
    for index, user in enumerate(users):
        for award in range(index + 1):
            _run(transform(_chat(user, award * 30)))
    board = _board(host)
    assert len(board) == app.LEADERBOARD_SIZE == 5
    assert [entry[0] for entry in board] == ["u6", "u5", "u4", "u3", "u2"]
    assert [entry[1] for entry in board] == [35, 30, 25, 20, 15]


def test_a_chatter_below_the_floor_never_writes_the_board(
    host: types.SimpleNamespace,
) -> None:
    _seed_board(host, [(f"top{i}", 1000 - i) for i in range(5)])
    before = host.kv.store["level.board"]
    host.kv.calls.clear()
    _run(transform(_chat(ALICE, 0)))
    assert host.kv.store["level.board"] == before
    assert not [c for c in host.kv.calls if c[0] == "set" and c[1][0] == "level.board"]


def test_a_chatter_already_on_the_board_is_updated_in_place(
    host: types.SimpleNamespace,
) -> None:
    _seed_board(host, [("alice-uuid", 5), ("zed", 3)])
    host.kv.store[_xp_key(ALICE)] = b"5"
    _run(transform(_chat(ALICE, 0)))
    assert _board(host) == [["alice-uuid", 10], ["zed", 3]]


def test_ties_break_by_id_deterministically(host: types.SimpleNamespace) -> None:
    _run(transform(_chat(BOB, 0)))
    _run(transform(_chat(ALICE, 1)))
    assert [entry[0] for entry in _board(host)] == ["alice-uuid", "bob-uuid"]


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"{}",
        b'[["a"]]',
        b'[["a", "x"]]',
        b'[["a", -1]]',
        b'[["a", true]]',
        b'[["has space", 1]]',
        b"[[1, 1]]",
        b"\xff\xfe",
    ],
)
def test_a_corrupt_board_is_logged_loudly_and_heals_on_the_next_award(
    host: types.SimpleNamespace, raw: bytes
) -> None:
    host.kv.store["level.board"] = raw
    assert (
        _text(_run(transform(_event("!level leaderboard", actor=None))))
        == app._EMPTY_BOARD
    )
    assert "level.board_corrupt" in _logs(host)
    _run(transform(_chat(ALICE, 0)))
    assert _board(host) == [["alice-uuid", 5]]


def test_board_entries_are_rendered_as_tokens_only(host: types.SimpleNamespace) -> None:
    _seed_board(host, [("abc", 50)])
    reply = _text(_run(transform(_event("!level top", actor=None))))
    assert reply == "Top levels: 1. {user:abc} L1 (50 XP)"


def test_kv_keys_are_pseudonymous_and_charset_clean(
    host: types.SimpleNamespace,
) -> None:
    for user in (ALICE, BOB, CAROL):
        _run(transform(_chat(user, 0)))
    pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
    assert host.kv.store
    for key in host.kv.store:
        assert pattern.match(key), key
        for needle in ("alice", "bob", "carol", "uuid", "handle", "user"):
            assert needle not in key
    assert _xp_key(CAROL) in host.kv.store


def test_only_the_leaderboard_value_holds_an_id_and_logs_hold_none(
    host: types.SimpleNamespace,
) -> None:
    _run(transform(_chat(ALICE, 0, text="my secret message")))
    _run(transform(_event("!level", at=40)))
    stored = b" ".join(v for k, v in host.kv.store.items() if k != "level.board")
    assert b"alice" not in stored
    logs = _logs(host)
    for needle in ("alice", "uuid", "secret", "{user"):
        assert needle not in logs


def test_a_raw_username_actor_is_never_keyed_or_logged(
    host: types.SimpleNamespace,
) -> None:
    _run(transform(_event("hi", actor="SomeStreamer99")))
    assert host.kv.store == {}
    assert "SomeStreamer99" not in _logs(host)


def test_replies_never_echo_chatter_text(host: types.SimpleNamespace) -> None:
    hostile = "@everyone <@123> {user:victim} <!here> \\{user:x\\}"
    for text in (
        f"!level {hostile}",
        f"!level leaderboard {hostile}",
        f"!level {hostile}x",
    ):
        reply = _text(_run(transform(_event(text))))
        assert "everyone" not in reply
        assert "victim" not in reply
        assert "<@" not in reply


def test_the_final_sanitizer_is_a_no_op_on_the_real_templates(
    host: types.SimpleNamespace,
) -> None:
    _seed_board(host, [("abc", 50), ("d:e.f-g_h", 40)])
    reply = _text(_run(transform(_event("!level top", actor=None))))
    assert reply.count("{user:") == 2
    assert "​" not in reply
    assert "\\{" not in reply


def test_safe_reply_neutralizes_anything_that_is_not_an_allow_listed_token() -> None:
    cooked = app._safe_reply("hi {user:victim} @everyone {user:mine}", ("mine",))
    assert "{user:victim}" not in cooked
    assert "\\{user:victim\\}" in cooked
    assert "@​everyone" in cooked
    assert "{user:mine}" in cooked


def test_command_path_kv_failure_is_answered_and_logged(
    host: types.SimpleNamespace,
) -> None:
    def boom(*_args: Any) -> Any:
        raise RuntimeError("scripted kv failure for alice-uuid")

    host.wit.imports.kv.get = boom
    reply = _text(_run(transform(_event("!level"))))
    assert reply == app._UNAVAILABLE
    logs = _logs(host)
    assert "level.kv_failure" in logs
    assert "alice-uuid" not in logs
    assert "scripted" not in logs


def test_passive_kv_failure_is_logged_at_error_and_never_raises(
    host: types.SimpleNamespace,
) -> None:
    def boom(*_args: Any) -> Any:
        raise RuntimeError("down")

    host.wit.imports.kv.increment = boom
    assert _run(transform(_chat(ALICE, 0))) is None
    assert any(lvl == 0 and msg == "level.kv_failure" for lvl, msg, _ in host.log_calls)


def test_a_failing_stamp_write_is_logged_and_releases_the_lock(
    host: types.SimpleNamespace,
) -> None:
    def boom(*_args: Any) -> Any:
        raise RuntimeError("down")

    host.wit.imports.kv.set = boom
    assert _run(transform(_chat(ALICE, 0))) is None
    assert any(lvl == 0 and msg == "level.kv_failure" for lvl, msg, _ in host.log_calls)
    assert _xp(host, ALICE) == 0
    assert not [k for k in host.kv.store if k.startswith("level.lock.")]


def test_a_failing_lock_release_is_logged_and_leaves_only_the_ttl_backstop(
    host: types.SimpleNamespace,
) -> None:
    def boom(*_args: Any) -> Any:
        raise RuntimeError("down")

    host.wit.imports.kv.delete = boom
    assert _run(transform(_chat(ALICE, 0))) is None
    assert _xp(host, ALICE) == 5
    assert _lock_key(ALICE) in host.kv.store
    assert any(lvl == 0 and msg == "level.kv_failure" for lvl, msg, _ in host.log_calls)
    # the stranded lock defers (never double-awards) until its 10s TTL lapses
    assert _run(transform(_chat(ALICE, 100))) is None
    assert _xp(host, ALICE) == 5


@pytest.mark.parametrize("raw", [b"abc", b"1.5", b"", b"\xff"])
def test_a_corrupt_stamp_is_logged_and_never_awards(
    host: types.SimpleNamespace, raw: bytes
) -> None:
    host.kv.store[_ts_key(ALICE)] = raw
    assert _run(transform(_chat(ALICE, 0))) is None
    assert _xp(host, ALICE) == 0
    assert any(lvl == 0 and msg == "level.kv_failure" for lvl, msg, _ in host.log_calls)


def test_a_corrupt_xp_counter_is_answered_not_guessed(
    host: types.SimpleNamespace,
) -> None:
    host.kv.store[_xp_key(ALICE)] = b"not-a-number"
    assert _text(_run(transform(_event("!level")))) == app._UNAVAILABLE


def test_the_lock_is_released_when_the_award_fails(host: types.SimpleNamespace) -> None:
    real_increment = host.wit.imports.kv.increment

    def selective(key: str, delta: int, ttl: int) -> int:
        if key.startswith("level.xp."):
            raise RuntimeError("down")
        return int(real_increment(key, delta, ttl))

    host.wit.imports.kv.increment = selective
    _run(transform(_chat(ALICE, 0)))
    assert not [k for k in host.kv.store if k.startswith("level.lock.")]
    # a failed award stamps first, so it under-awards rather than over-awards
    assert _xp(host, ALICE) == 0
    assert _ts_key(ALICE) in host.kv.store


def test_a_failed_award_does_not_suppress_the_command_reply(
    host: types.SimpleNamespace,
) -> None:
    def boom(*_args: Any) -> Any:
        raise RuntimeError("down")

    host.wit.imports.kv.increment = boom
    reply = _text(_run(transform(_event("!level"))))
    assert reply == f"{ALICE} is level 1 with 0 XP (100 XP to level 2)."


def test_award_path_stays_far_inside_the_host_op_budget(
    host: types.SimpleNamespace,
) -> None:
    host.kv.calls.clear()
    _run(transform(_event("!level top", at=0)))
    assert len(host.kv.calls) <= 12 < 64


def test_every_award_uses_only_the_documented_key_namespaces(
    host: types.SimpleNamespace,
) -> None:
    for second in range(0, 300, 10):
        _run(transform(_chat(ALICE, second)))
    prefixes = {
        key.rsplit(".", 1)[0] + "." for key in host.kv.store if key != "level.board"
    }
    assert prefixes <= {app.XP_KEY_PREFIX, app.TS_KEY_PREFIX}


def _envelope(event: PlatformEvent) -> StageEnvelope:
    return StageEnvelope(
        tenant="t",
        community="c",
        app_id="waddles.core.example.level",
        stage="action",
        event=event,
        ts=_at(0),
    )


def test_dispatch_relays_the_reply_to_the_channel(host: types.SimpleNamespace) -> None:
    reply_event = _run(transform(_event("!level")))
    result = _run(dispatch(_envelope(reply_event), {}, http_client=None))
    assert result.transport == "twitch"
    assert result.detail == "relayed"
    assert host.relay_calls == [
        ("twitch", {"channel": "chan-1", "text": _text(reply_event)})
    ]


def test_dispatch_rejects_a_payload_without_channel_or_text(
    host: types.SimpleNamespace,
) -> None:
    no_channel = PlatformEvent(
        platform="twitch", event_type="chat.message", actor=ALICE, payload={"text": "x"}
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope(no_channel), {}, http_client=None))
    no_text = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=ALICE,
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
        host.kv.get("level:bad")
