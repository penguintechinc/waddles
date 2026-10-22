"""Host-native tests for the `timer` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- `kv` is the shared charset-enforcing fake
(`waddle_sdk.testing.install_fake_kv_host`, gh-631), so any key containing `:` fails a test
immediately; `relay`/`log`/`clock` are small stand-ins layered onto the `wit_world` module that
fake installs. The clock is a settable counter, so every due-time scenario is deterministic.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any, cast

import pytest
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

import app
from app import (
    _ADD_USAGE,
    _CFG_KEY,
    _LOCK_ATTEMPTS,
    _USAGE,
    MAX_INTERVAL_MIN,
    MAX_MESSAGE_LEN,
    MAX_TIMERS,
    MIN_INTERVAL_MIN,
    Timer,
    _caller_role_signal,
    _leads_with_command,
    _parse_config,
    _scope_of,
    dispatch,
    transform,
)

COMM = "comm-1"
CHANNEL = "12345"
ZWSP = "​"
T0 = 1_760_000_000_000
MIN = 60_000


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _scoped(key: str, community: str | None = COMM) -> str:
    """`fake_kv.store` is keyed by `community_kv`'s own `c.<community>.<key>` prefix."""
    return cast(str, _scoped_key(community, key))


class _Host:
    """`relay`/`log`/`clock` stand-ins layered on the shared `FakeKvHost`."""

    def __init__(self, kv: FakeKvHost) -> None:
        self.kv = kv
        self.now = T0
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[int, str, str]] = []

    def replies(self) -> list[str]:
        return [str(json.loads(msg)["text"]) for _provider, msg in self.relay_calls]

    def last_reply(self) -> str:
        return self.replies()[-1]

    def advance(self, *, minutes: int = 0, ms: int = 0) -> None:
        self.now += minutes * MIN + ms


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    fake_kv = install_fake_kv_host(monkeypatch)
    h = _Host(fake_kv)
    wit_world = sys.modules["wit_world"]
    wit_world.imports.relay = types.SimpleNamespace(  # type: ignore[attr-defined]
        push=lambda provider, msg: h.relay_calls.append((provider, msg))
    )
    wit_world.imports.log = types.SimpleNamespace(  # type: ignore[attr-defined]
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: h.log_calls.append((lvl, msg, fields_json)),
    )
    wit_world.imports.clock = types.SimpleNamespace(  # type: ignore[attr-defined]
        now_millis=lambda: h.now
    )
    return h


def _event(text: Any, **payload_extra: Any) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": CHANNEL, **payload_extra}
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="{user:abc123}",
        payload=payload,
        occurred_at="2026-10-22T00:00:00.000Z",
    )


def _envelope(
    command: str,
    *,
    community: str | None = COMM,
    arg: str | None = None,
    channel_id: str | None = CHANNEL,
    platform: str = "twitch",
    **badges: Any,
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": channel_id}
    if arg is not None:
        payload["arg"] = arg
    payload.update(badges)
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.timer",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="{user:abc123}",
            payload=payload,
            occurred_at="2026-10-22T00:00:00.000Z",
        ),
        ts="2026-10-22T00:00:00.000Z",
    )


def _send(command: str, **kwargs: Any) -> Any:
    return _run(dispatch(_envelope(command, **kwargs), {}, http_client=None))


def _admin(command: str, arg: str | None = None, **kwargs: Any) -> Any:
    kwargs.setdefault("is_mod", True)
    return _send(command, arg=arg, **kwargs)


def _tick(**kwargs: Any) -> Any:
    return _send("tick", **kwargs)


def _add(name: str, interval: int, message: str, **kwargs: Any) -> Any:
    return _admin("add", f"{name} {interval} {message}", **kwargs)


def _timers(host: _Host, community: str | None = COMM) -> list[Timer]:
    raw = host.kv.store.get(_scoped(_CFG_KEY, community))
    return _parse_config(raw) if raw is not None else []


def _seed(host: _Host, timers: list[Timer], community: str | None = COMM) -> None:
    host.kv.store[_scoped(_CFG_KEY, community)] = app._dump_config(timers)


def _timer(name: str = "promo", **overrides: Any) -> Timer:
    base: dict[str, Any] = {
        "name": name,
        "interval_min": 30,
        "message": "follow the socials",
        "enabled": True,
        "scope": _scope_of("twitch", CHANNEL),
        "anchor_ms": T0,
    }
    base.update(overrides)
    return Timer(**base)


def _errors(host: _Host) -> list[tuple[int, str, str]]:
    return [c for c in host.log_calls if c[0] == 0]


def _set_kv_raises(monkeypatch: pytest.MonkeyPatch, *, op: str) -> None:
    """Patch the installed `wit_world.imports.kv.<op>` to raise a stand-in backend error."""

    class _ErrorBackend:
        """Stand-in for the generated WIT `Error_Backend` variant case class."""

    class _KvError(Exception):
        def __init__(self) -> None:
            self.value = _ErrorBackend()

    def _raiser(*_args: Any, **_kwargs: Any) -> Any:
        raise _KvError()

    monkeypatch.setattr(sys.modules["wit_world"].imports.kv, op, _raiser)


# -- transform: commands ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "command", "arg"),
    [
        ("!timer add promo 30 follow us", "add", "promo 30 follow us"),
        ("!TIMER  add  Promo 30 hi", "add", "Promo 30 hi"),
        ("!timer del promo", "delete", "promo"),
        ("!timer DEL promo", "delete", "promo"),
        ("!timer delete promo", "delete", "promo"),
        ("!timer remove promo", "delete", "promo"),
        ("!timer list", "list", None),
        ("!timer reset", "reset", None),
        ("!timer enable promo", "enable", "promo"),
        ("!timer disable Promo", "disable", "promo"),
    ],
)
def test_each_verb_maps_to_its_command(
    text: str, command: str, arg: str | None, host: _Host
) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == command
    assert result.payload.get("arg") == arg
    assert result.payload["channel_id"] == CHANNEL


@pytest.mark.parametrize(
    "text",
    [
        "!timer",
        "!timer del",
        "!timer list extra",
        "!timer reset now",
        "!timer enable",
        "!timer enable a b",
        "!timer bogus",
        "!timer set a b",
        "!timer sub x",
    ],
)
def test_malformed_commands_reply_usage_never_drop(text: str, host: _Host) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    if text == "!timer del":
        assert result.payload["command"] == "delete"  # dispatch answers its own usage line
    else:
        assert result.payload["command"] == "usage"


def test_badges_are_forwarded_strictly_and_absence_stays_absent(host: _Host) -> None:
    absent = _run(transform(_event("!timer list")))
    assert absent is not None
    assert "is_mod" not in absent.payload
    assert "is_broadcaster" not in absent.payload

    strict = _run(transform(_event("!timer list", is_mod="true", is_broadcaster="yes")))
    assert strict is not None
    assert strict.payload["is_mod"] is False
    assert strict.payload["is_broadcaster"] is False

    real = _run(transform(_event("!timer list", is_mod=True, is_broadcaster=False)))
    assert real is not None
    assert real.payload["is_mod"] is True
    assert real.payload["is_broadcaster"] is False


# -- transform: the chat-activity tick ----------------------------------------------------------


@pytest.mark.parametrize("text", ["hello chat", "!quote 3", "!timers", "timer add x", "!time", ""])
def test_any_other_chat_line_becomes_a_bare_tick(text: str, host: _Host) -> None:
    result = _run(transform(_event(text, is_mod=True, extra="x")))
    assert result is not None
    assert result.payload == {"command": "tick", "channel_id": CHANNEL}
    assert result.platform == "twitch"


def test_a_tick_is_dropped_without_a_channel(host: _Host) -> None:
    assert _run(transform(_event("hello", channel_id=None))) is None
    assert _run(transform(_event("hello", channel_id=""))) is None


@pytest.mark.parametrize("text", [None, 7, ["!timer list"], {"a": 1}])
def test_non_string_text_is_dropped(text: Any, host: _Host) -> None:
    assert _run(transform(_event(text))) is None


def test_the_tick_path_logs_nothing(host: _Host) -> None:
    for _ in range(5):
        _run(transform(_event("hello")))
    assert not host.log_calls


# -- helpers ------------------------------------------------------------------------------------


def test_role_signal_is_strict() -> None:
    assert _caller_role_signal({}) is None
    assert _caller_role_signal({"is_mod": True}) is True
    assert _caller_role_signal({"is_broadcaster": True}) is True
    assert _caller_role_signal({"is_mod": False}) is False
    assert _caller_role_signal({"is_mod": "true"}) is False
    assert _caller_role_signal({"is_broadcaster": 1}) is False


def test_scope_is_a_non_reversible_per_channel_hash() -> None:
    scope = _scope_of("twitch", "some-channel")
    assert len(scope) == 16
    assert "some-channel" not in scope
    assert scope != _scope_of("discord", "some-channel")
    assert scope != _scope_of("twitch", "other")
    assert scope == _scope_of("twitch", "some-channel")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/ban x", True),
        (".timeout y", True),
        ("!other", True),
        ("  /me waves", True),
        (f"{ZWSP}/ban x", True),
        ("⁠!x", True),
        ("hello /not-a-lead", False),
        ("Follow us!", False),
        ("", False),
        (f"{ZWSP}  ", False),
    ],
)
def test_command_lead_detection_sees_through_invisible_prefixes(text: str, expected: bool) -> None:
    assert _leads_with_command(text) is expected


# -- admin: gate --------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["add", "delete", "list", "enable", "disable", "reset"])
@pytest.mark.parametrize(
    "badges",
    [
        {"is_mod": None},
        {"is_mod": False},
        {"is_mod": "true"},
        {"is_mod": 1},
        {"is_broadcaster": "y"},
    ],
    ids=["absent", "false", "string-true", "int-one", "string-broadcaster"],
)
def test_admin_commands_are_denied_without_a_real_true_badge(
    command: str, badges: dict[str, Any], host: _Host
) -> None:
    badges = {k: v for k, v in badges.items() if v is not None}
    _seed(host, [_timer()])
    snapshot = dict(host.kv.store)
    host.kv.calls.clear()
    _send(command, arg="promo 30 hello", **badges)
    assert host.last_reply() == "Only the broadcaster or a moderator can manage timers."
    assert host.kv.store == snapshot
    assert not host.kv.calls  # denied BEFORE any kv access, not even the lock


def test_broadcaster_badge_alone_is_enough(host: _Host) -> None:
    _send("list", is_broadcaster=True)
    assert host.last_reply().startswith("No timers set")


# -- admin: add ---------------------------------------------------------------------------------


def test_add_stores_a_channel_bound_enabled_timer_anchored_now(host: _Host) -> None:
    _add("Promo", 30, "follow the socials")
    assert _timers(host) == [_timer("promo", message="follow the socials")]
    reply = host.last_reply()
    assert "Timer 'promo' added" in reply
    assert "every 30 min" in reply
    assert "silent channel stays silent" in reply


def test_add_keeps_the_message_text_verbatim(host: _Host) -> None:
    _add("promo", 30, "  Drink   water,  stay hydrated!  ")
    assert _timers(host)[0].message == "Drink   water,  stay hydrated!"


def test_add_never_stores_the_raw_channel_id_or_author(host: _Host) -> None:
    _add("promo", 30, "hello")
    blob = host.kv.store[_scoped(_CFG_KEY)].decode()
    assert CHANNEL not in blob
    assert "abc123" not in blob
    assert "abc123" not in repr(host.log_calls)
    assert CHANNEL not in repr(host.log_calls)


@pytest.mark.parametrize(
    "arg",
    [None, "", "promo", "promo 30", "promo   "],
    ids=["none", "empty", "name-only", "no-message", "trailing-space"],
)
def test_add_with_missing_parts_replies_usage(arg: str | None, host: _Host) -> None:
    _admin("add", arg)
    assert host.last_reply() == _ADD_USAGE
    assert not _timers(host)


@pytest.mark.parametrize("name", ["bad name!", "x" * 25, "naïve", "a/b", "a.b", "a:b"])
def test_add_rejects_a_bad_name(name: str, host: _Host) -> None:
    _admin("add", f"{name.replace(' ', '')} 30 hello")
    assert "Timer names are" in host.last_reply()
    assert not _timers(host)


@pytest.mark.parametrize(
    ("interval", "reply"),
    [
        ("abc", _ADD_USAGE),
        ("-5", _ADD_USAGE),
        ("5.5", _ADD_USAGE),
        ("٣٠", _ADD_USAGE),
        ("00030000", _ADD_USAGE),
        (str(MIN_INTERVAL_MIN - 1), f"The interval must be {MIN_INTERVAL_MIN}-{MAX_INTERVAL_MIN}"),
        ("0", f"The interval must be {MIN_INTERVAL_MIN}-{MAX_INTERVAL_MIN}"),
        (str(MAX_INTERVAL_MIN + 1), f"The interval must be {MIN_INTERVAL_MIN}-{MAX_INTERVAL_MIN}"),
    ],
)
def test_add_rejects_a_bad_interval(interval: str, reply: str, host: _Host) -> None:
    _admin("add", f"promo {interval} hello")
    assert host.last_reply().startswith(reply)
    assert not _timers(host)


@pytest.mark.parametrize("interval", [MIN_INTERVAL_MIN, MAX_INTERVAL_MIN])
def test_add_accepts_the_interval_bounds(interval: int, host: _Host) -> None:
    _add("promo", interval, "hello")
    assert _timers(host)[0].interval_min == interval


def test_add_message_length_boundary(host: _Host) -> None:
    _add("ok", 30, "x" * MAX_MESSAGE_LEN)
    assert len(_timers(host)[0].message) == MAX_MESSAGE_LEN
    _add("toolong", 30, "x" * (MAX_MESSAGE_LEN + 1))
    assert host.last_reply() == f"Timer messages must be {MAX_MESSAGE_LEN} characters or fewer."
    assert [t.name for t in _timers(host)] == ["ok"]


def test_add_rejects_a_mention(host: _Host) -> None:
    _add("promo", 30, "thanks {user:tok123} for the follow")
    assert "can't @mention people" in host.last_reply()
    assert not _timers(host)


@pytest.mark.parametrize(
    "message", ["/ban victim", ".timeout x 600", "!lurk", f"{ZWSP}/clear", "  /me hi"]
)
def test_add_rejects_a_command_lead(message: str, host: _Host) -> None:
    _add("promo", 30, message)
    assert "can't start with /, . or !" in host.last_reply()
    assert not _timers(host)


def test_add_allows_a_command_character_that_is_not_the_lead(host: _Host) -> None:
    _add("promo", 30, "Join us! Use !discord for the link")
    assert _timers(host)[0].message == "Join us! Use !discord for the link"


def test_add_rejects_a_duplicate_name_case_insensitively(host: _Host) -> None:
    _add("promo", 30, "one")
    _add("PROMO", 60, "two")
    assert host.last_reply() == "A timer named 'promo' already exists -- !timer del promo first."
    assert [t.message for t in _timers(host)] == ["one"]


def test_add_enforces_the_timer_cap(host: _Host) -> None:
    for i in range(MAX_TIMERS):
        _add(f"t{i}", 30, f"message {i}")
    _add("one-too-many", 30, "nope")
    assert host.last_reply() == f"Too many timers ({MAX_TIMERS} max) -- delete one first."
    assert len(_timers(host)) == MAX_TIMERS


# -- admin: delete / enable / disable / list / reset --------------------------------------------


def test_delete_removes_the_timer_and_its_last_fired_marker(host: _Host) -> None:
    _seed(host, [_timer("a"), _timer("b")])
    host.kv.store[_scoped("timer.last.a")] = b"123"
    _admin("delete", "A")
    assert host.last_reply() == "Timer 'a' deleted."
    assert [t.name for t in _timers(host)] == ["b"]
    assert _scoped("timer.last.a") not in host.kv.store


def test_delete_unknown_and_unusable_names(host: _Host) -> None:
    _admin("delete", "ghost")
    assert host.last_reply() == "No timer named 'ghost'."
    _admin("delete", None)
    assert host.last_reply() == "Usage: !timer del <name>"
    _admin("delete", "bad name")
    assert host.last_reply() == "Usage: !timer del <name>"


def test_a_failed_last_marker_cleanup_is_logged_not_fatal(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(host, [_timer("a")])
    real_delete = host.kv.delete

    def flaky_delete(key: str) -> None:
        if key.endswith("timer.last.a"):
            raise RuntimeError("boom")
        real_delete(key)

    monkeypatch.setattr(sys.modules["wit_world"].imports.kv, "delete", flaky_delete)
    _admin("delete", "a")
    assert host.last_reply() == "Timer 'a' deleted."
    assert [c for c in _errors(host) if "last_cleanup_failed" in c[1]]
    assert not _timers(host)


def test_disable_then_enable_restarts_the_countdown(host: _Host) -> None:
    _seed(host, [_timer("promo")])
    _admin("disable", "promo")
    assert host.last_reply() == "Timer 'promo' disabled."
    assert _timers(host)[0].enabled is False
    assert _timers(host)[0].anchor_ms == T0

    host.advance(minutes=500)
    _admin("enable", "promo")
    assert host.last_reply() == "Timer 'promo' enabled."
    assert _timers(host)[0].enabled is True
    assert _timers(host)[0].anchor_ms == T0 + 500 * MIN


def test_toggle_to_the_current_state_and_unknown_names(host: _Host) -> None:
    _seed(host, [_timer("promo")])
    _admin("enable", "promo")
    assert host.last_reply() == "Timer 'promo' is already enabled."
    _admin("disable", "promo")
    _admin("disable", "promo")
    assert host.last_reply() == "Timer 'promo' is already disabled."
    _admin("enable", "ghost")
    assert host.last_reply() == "No timer named 'ghost'."
    _admin("enable", None)
    assert host.last_reply() == "Usage: !timer enable <name>"
    _admin("disable", "bad name")
    assert host.last_reply() == "Usage: !timer disable <name>"


def test_toggle_leaves_the_other_timers_alone(host: _Host) -> None:
    _seed(host, [_timer("a"), _timer("b")])
    _admin("disable", "b")
    assert [(t.name, t.enabled) for t in _timers(host)] == [("a", True), ("b", False)]


def test_list_on_no_timers(host: _Host) -> None:
    _admin("list")
    assert (
        host.last_reply()
        == "No timers set. Add one with !timer add <name> <interval-min> <message>."
    )


def test_list_shows_name_interval_state_and_a_preview(host: _Host) -> None:
    _seed(
        host,
        [
            _timer("a", interval_min=30, message="short"),
            _timer("b", interval_min=60, enabled=False, message="x" * 100),
        ],
    )
    _admin("list")
    assert host.last_reply() == f"Timers (2): a (30m, on): short | b (60m, off): {'x' * 29}…"


def test_list_sanitizes_the_stored_message(host: _Host) -> None:
    _seed(host, [_timer("a", message="@everyone look {user:victim}")])
    _admin("list")
    reply = host.last_reply()
    assert "@everyone" not in reply
    assert f"@{ZWSP}everyone" in reply
    assert "{user:victim}" not in reply
    assert "\\{user:victim" in reply


def test_list_truncates_before_sanitizing_so_an_escape_is_never_split(host: _Host) -> None:
    _seed(host, [_timer("a", message="{" * 100)])
    _admin("list")
    reply = host.last_reply()
    body = reply.split(": ", 1)[1].split(": ", 1)[1]
    assert body.rstrip("…").count("\\{") == body.rstrip("…").count("{")


def test_list_stops_at_the_reply_budget_with_a_remainder(host: _Host) -> None:
    _seed(host, [_timer(f"t{i}", message="m" * 100) for i in range(MAX_TIMERS)])
    _admin("list")
    reply = host.last_reply()
    assert len(reply) <= 500
    assert reply.startswith(f"Timers ({MAX_TIMERS}): t0 (30m, on):")
    assert reply.endswith("more)")
    shown = reply.count("(30m, on)")
    assert reply.endswith(f"(+{MAX_TIMERS - shown} more)")


def test_list_always_shows_at_least_the_first_timer(host: _Host) -> None:
    _seed(host, [_timer("only", message="m" * 300)])
    _admin("list")
    assert "only (30m, on)" in host.last_reply()


def test_reset_deletes_everything_and_never_reads_the_blob(host: _Host) -> None:
    host.kv.store[_scoped(_CFG_KEY)] = b"\xff corrupt"
    host.kv.calls.clear()
    _admin("reset")
    assert host.last_reply() == "All timers deleted."
    assert _scoped(_CFG_KEY) not in host.kv.store
    assert ("get", (_scoped(_CFG_KEY),)) not in host.kv.calls


# -- tick: firing -------------------------------------------------------------------------------


def test_idle_tick_with_no_config_is_one_read_and_no_reply(host: _Host) -> None:
    result = _tick()
    assert result.detail == "tick:idle"
    assert [name for name, _args in host.kv.calls] == ["get"]
    assert not host.relay_calls


def test_tick_before_the_interval_posts_nothing(host: _Host) -> None:
    _seed(host, [_timer()])
    host.advance(minutes=29, ms=59_999)
    assert _tick().detail == "tick:idle"
    assert not host.relay_calls


def test_tick_at_the_interval_posts_the_message_to_this_channel(host: _Host) -> None:
    _seed(host, [_timer()])
    host.advance(minutes=30)
    result = _tick()
    assert result.detail == "tick:fired"
    assert result.transport == "twitch"
    assert host.relay_calls[-1][0] == "twitch"
    assert json.loads(host.relay_calls[-1][1]) == {"channel": CHANNEL, "text": "follow the socials"}
    assert host.kv.store[_scoped("timer.last.promo")] == str(host.now).encode()


def test_a_timer_is_not_due_again_until_a_full_interval_after_it_fired(host: _Host) -> None:
    _seed(host, [_timer()])
    host.advance(minutes=30)
    _tick()
    host.advance(minutes=29, ms=59_999)
    assert _tick().detail == "tick:idle"
    host.advance(ms=1)
    assert _tick().detail == "tick:fired"
    assert host.replies() == ["follow the socials", "follow the socials"]


def test_a_long_silence_posts_once_not_once_per_missed_interval(host: _Host) -> None:
    _seed(host, [_timer(interval_min=30)])
    host.advance(minutes=30 * 20)
    assert _tick().detail == "tick:fired"
    assert _tick().detail == "tick:idle"
    assert len(host.relay_calls) == 1


def test_due_time_is_the_later_of_anchor_and_last_fired(host: _Host) -> None:
    _seed(host, [_timer(anchor_ms=T0)])
    host.kv.store[_scoped("timer.last.promo")] = str(T0 + 100 * MIN).encode()
    host.advance(minutes=120)  # past anchor+30 but before last+30
    assert _tick().detail == "tick:idle"
    host.advance(minutes=10)  # now T0+130m == last+30m
    assert _tick().detail == "tick:fired"


def test_a_timer_only_fires_in_the_channel_and_platform_it_was_added_in(host: _Host) -> None:
    _seed(host, [_timer()])
    host.advance(minutes=60)
    assert _tick(channel_id="other-channel").detail == "tick:idle"
    assert _tick(platform="discord").detail == "tick:idle"
    assert not host.relay_calls
    assert _tick().detail == "tick:fired"


def test_a_disabled_timer_never_fires(host: _Host) -> None:
    _seed(host, [_timer(enabled=False)])
    host.advance(minutes=600)
    assert _tick().detail == "tick:idle"
    assert [name for name, _args in host.kv.calls] == ["get"]  # no per-timer reads either


def test_end_to_end_add_then_time_passes_then_chat_posts(host: _Host) -> None:
    _add("promo", 10, "drink water")
    _tick()
    host.advance(minutes=9)
    _tick()
    assert host.replies() == [host.replies()[0]]  # only the add confirmation so far
    host.advance(minutes=1)
    _tick()
    assert host.replies()[-1] == "drink water"
    assert len(host.relay_calls) == 2


def test_only_one_timer_posts_per_chat_line_most_overdue_first(host: _Host) -> None:
    _seed(
        host,
        [
            _timer("later", interval_min=10, message="later msg", anchor_ms=T0 + 5 * MIN),
            _timer("earlier", interval_min=10, message="earlier msg", anchor_ms=T0),
        ],
    )
    host.advance(minutes=60)
    _tick()
    assert host.replies() == ["earlier msg"]
    _tick()
    assert host.replies() == ["earlier msg", "later msg"]
    _tick()
    assert len(host.relay_calls) == 2


def test_equal_due_times_break_ties_by_name(host: _Host) -> None:
    _seed(host, [_timer("zeta", message="z"), _timer("alpha", message="a")])
    host.advance(minutes=30)
    _tick()
    assert host.replies() == ["a"]


def test_losing_the_claim_means_someone_else_posted_so_this_line_does_not(host: _Host) -> None:
    _seed(host, [_timer()])
    host.advance(minutes=30)
    due_at = T0 + 30 * MIN
    host.kv.store[_scoped(f"timer.claim.promo.{due_at}")] = b"1"  # a concurrent winner
    assert _tick().detail == "tick:idle"
    assert not host.relay_calls
    assert _scoped("timer.last.promo") not in host.kv.store  # the loser writes nothing
    assert [c for c in host.log_calls if c[0] == 3]  # logged at DEBUG


def test_a_lost_claim_falls_through_to_the_next_due_timer(host: _Host) -> None:
    _seed(host, [_timer("a", message="a msg"), _timer("b", message="b msg")])
    host.advance(minutes=30)
    host.kv.store[_scoped(f"timer.claim.a.{T0 + 30 * MIN}")] = b"1"
    _tick()
    assert host.replies() == ["b msg"]


def test_two_racing_lines_with_stale_state_post_exactly_once(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stale_last(_community: str | None, _name: str) -> int | None:
        return None  # both racers read the state from before either wrote

    _seed(host, [_timer()])
    host.advance(minutes=30)
    monkeypatch.setattr(app, "_read_last", stale_last)
    first = _tick()
    second = _tick()
    assert (first.detail, second.detail) == ("tick:fired", "tick:idle")
    assert len(host.relay_calls) == 1


def test_the_new_last_fired_time_is_recorded_before_the_relay(host: _Host) -> None:
    _seed(host, [_timer()])
    host.advance(minutes=30)
    order: list[str] = []
    real_set = host.kv.set

    def spy_set(key: str, value: bytes, ttl: int) -> None:
        order.append("set_last" if "timer.last." in key else "set")
        real_set(key, value, ttl)

    sys.modules["wit_world"].imports.kv.set = spy_set
    sys.modules["wit_world"].imports.relay.push = lambda p, m: order.append("relay")
    _tick()
    assert order == ["set_last", "relay"]


def test_a_failed_relay_is_a_missed_announcement_not_a_repeat(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(host, [_timer()])
    host.advance(minutes=30)

    def failing_push(_provider: str, _msg: str) -> None:
        raise RuntimeError("relay down")

    monkeypatch.setattr(sys.modules["wit_world"].imports.relay, "push", failing_push)
    with pytest.raises(RuntimeError, match="relay down"):
        _tick()
    monkeypatch.setattr(
        sys.modules["wit_world"].imports.relay,
        "push",
        lambda p, m: host.relay_calls.append((p, m)),
    )
    assert _tick().detail == "tick:idle"  # recorded as fired: no duplicate
    assert not host.relay_calls


def test_tick_works_under_the_tenant_wide_sentinel(host: _Host) -> None:
    _seed(host, [_timer()], community=None)
    host.advance(minutes=30)
    assert _tick(community=None).detail == "tick:fired"
    assert _scoped(_CFG_KEY, None) == "c.0.timer.cfg"


def test_timers_are_isolated_per_community(host: _Host) -> None:
    _seed(host, [_timer()], community="comm-a")
    host.advance(minutes=30)
    assert _tick(community="comm-b").detail == "tick:idle"
    assert _tick(community="comm-a").detail == "tick:fired"


def test_an_idle_tick_never_logs(host: _Host) -> None:
    _tick()  # no config at all
    _seed(host, [_timer(enabled=False), _timer("other", scope=_scope_of("twitch", "elsewhere"))])
    _tick()  # config present, but nothing enabled for this channel
    host.advance(minutes=5)
    _tick()  # an enabled timer that is simply not due yet
    assert not host.log_calls


def test_every_tick_key_carries_its_intended_ttl(host: _Host) -> None:
    _seed(host, [_timer()])
    host.advance(minutes=30)
    host.kv.calls.clear()
    _tick()
    ttl_by_prefix = {}
    for name, args in host.kv.calls:
        if name == "increment":
            ttl_by_prefix["claim"] = args[2]
        if name == "set":
            ttl_by_prefix["last"] = args[2]
    assert ttl_by_prefix == {"claim": app._CLAIM_TTL_SECONDS, "last": app._LAST_TTL_SECONDS}
    assert 0 < app._CLAIM_TTL_SECONDS < app._LAST_TTL_SECONDS <= 30 * 24 * 60 * 60


def test_config_is_durable_and_the_lock_expires(host: _Host) -> None:
    host.kv.calls.clear()
    _add("promo", 30, "hello")
    increments = [args for name, args in host.kv.calls if name == "increment"]
    sets = [args for name, args in host.kv.calls if name == "set"]
    assert [args[2] for args in increments] == [app._LOCK_TTL_SECONDS]
    assert [args[2] for args in sets] == [0]  # no expiry: timers live until a mod deletes them


def test_tick_ops_stay_inside_the_host_budget(host: _Host) -> None:
    _seed(host, [_timer(f"t{i}") for i in range(MAX_TIMERS)])
    host.advance(minutes=60)
    host.kv.calls.clear()
    _tick()
    assert len(host.kv.calls) <= 64
    assert len(host.kv.calls) == 1 + MAX_TIMERS + 1 + 1  # cfg + last reads + claim + set last


# -- tick: output safety ------------------------------------------------------------------------


def test_the_fired_message_is_sanitized(host: _Host) -> None:
    _seed(host, [_timer(message="@everyone <@123> {user:victim} <!here> end")])
    host.advance(minutes=30)
    _tick()
    text = host.last_reply()
    assert "@everyone" not in text
    assert "<@123>" not in text
    assert "<!here>" not in text
    assert "{user:victim}" not in text
    assert f"@{ZWSP}everyone" in text
    assert "\\{user:victim\\}" in text


def test_ordinary_text_passes_the_sanitizer_unchanged(host: _Host) -> None:
    msg = "Follow us at https://example.com/@name (it's free!) -- see you there."
    _seed(host, [_timer(message=msg)])
    host.advance(minutes=30)
    _tick()
    assert host.last_reply() == msg


@pytest.mark.parametrize("stored", ["/ban victim", ".clear", "!lurk", f"{ZWSP}/raid x"])
def test_a_stored_command_lead_is_never_posted(stored: str, host: _Host) -> None:
    _seed(host, [_timer(message=stored)])
    host.advance(minutes=30)
    assert _tick().detail == "tick:idle"
    assert not host.relay_calls
    assert [c for c in _errors(host) if "unsafe_message_skipped" in c[1]]


def test_an_unsafe_message_does_not_block_the_next_timer(host: _Host) -> None:
    _seed(host, [_timer("a", message="/ban x"), _timer("b", message="fine")])
    host.advance(minutes=30)
    _tick()
    assert host.replies() == ["fine"]


def test_a_stored_message_that_sanitizes_to_nothing_is_not_posted(host: _Host) -> None:
    _seed(host, [_timer(message="\x00\x01\x02")])
    host.advance(minutes=30)
    assert _tick().detail == "tick:idle"


def test_a_corrupt_last_fired_value_falls_back_to_the_anchor_and_is_logged(host: _Host) -> None:
    _seed(host, [_timer()])
    host.kv.store[_scoped("timer.last.promo")] = b"not-a-number"
    host.advance(minutes=30)
    assert _tick().detail == "tick:fired"
    assert [c for c in _errors(host) if "last_corrupt" in c[1]]


def test_a_negative_last_fired_value_is_ignored(host: _Host) -> None:
    _seed(host, [_timer()])
    host.kv.store[_scoped("timer.last.promo")] = b"-5"
    host.advance(minutes=30)
    assert _tick().detail == "tick:fired"


# -- locking / failure paths --------------------------------------------------------------------


def test_a_held_lock_yields_busy_and_changes_nothing(host: _Host) -> None:
    _seed(host, [_timer()])
    host.kv.store[_scoped("timer.lock")] = b"1"  # another invocation holds it
    snapshot = dict(host.kv.store)
    host.kv.calls.clear()
    _add("new", 30, "hello")
    assert host.last_reply() == "Timers are busy right now, please try again in a moment."
    assert len([c for c in host.kv.calls if c[0] == "increment"]) == _LOCK_ATTEMPTS
    assert host.kv.store[_scoped(_CFG_KEY)] == snapshot[_scoped(_CFG_KEY)]
    assert host.kv.store[_scoped("timer.lock")] == str(1 + _LOCK_ATTEMPTS).encode()


@pytest.mark.parametrize(
    ("command", "arg"),
    [
        ("add", "n 30 hello"),
        ("delete", "promo"),
        ("enable", "promo"),
        ("disable", "promo"),
        ("reset", None),
    ],
)
def test_every_mutation_holds_and_releases_the_lock(
    command: str, arg: str | None, host: _Host
) -> None:
    _seed(host, [_timer(enabled=command != "enable")])
    host.kv.calls.clear()
    _admin(command, arg)
    names = [name for name, _args in host.kv.calls]
    assert names[0] == "increment"
    assert "delete" in names
    assert _scoped("timer.lock") not in host.kv.store


def test_read_only_list_takes_no_lock(host: _Host) -> None:
    _seed(host, [_timer()])
    host.kv.calls.clear()
    _admin("list")
    assert [name for name, _args in host.kv.calls] == ["get"]


def test_the_lock_is_released_when_the_save_fails(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_kv_raises(monkeypatch, op="set")
    with pytest.raises(RuntimeError, match="timer kv set failed: _ErrorBackend"):
        _add("promo", 30, "hello")
    assert _scoped("timer.lock") not in host.kv.store


def test_a_failed_lock_release_is_logged_and_does_not_mask_success(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_delete(key: str) -> None:
        raise RuntimeError("delete down")

    monkeypatch.setattr(sys.modules["wit_world"].imports.kv, "delete", failing_delete)
    _add("promo", 30, "hello")
    assert [c for c in _errors(host) if "lock_release_failed" in c[1]]
    assert [t.name for t in _timers(host)] == ["promo"]


@pytest.mark.parametrize("op", ["get", "set", "increment"])
def test_admin_kv_errors_reply_and_raise(
    op: str, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(host, [_timer()])
    _set_kv_raises(monkeypatch, op=op)
    command = "list" if op == "get" else "add"
    with pytest.raises(RuntimeError, match=r"timer kv \w+ failed: _ErrorBackend"):
        _admin(command, "n 30 hello")
    assert host.last_reply() == "Timers are temporarily unavailable, try again shortly."
    assert _errors(host)
    assert "hello" not in repr(_errors(host))


def test_reset_delete_error_replies_and_raises(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_delete = host.kv.delete

    def flaky(key: str) -> None:
        if key.endswith(_CFG_KEY):
            raise RuntimeError("down")
        real_delete(key)

    monkeypatch.setattr(sys.modules["wit_world"].imports.kv, "delete", flaky)
    with pytest.raises(RuntimeError, match="timer kv delete failed: RuntimeError"):
        _admin("reset")
    assert host.last_reply() == "Timers are temporarily unavailable, try again shortly."


@pytest.mark.parametrize("op", ["get", "increment", "set"])
def test_tick_kv_errors_raise_without_spamming_chat(
    op: str, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(host, [_timer()])
    host.advance(minutes=30)
    _set_kv_raises(monkeypatch, op=op)
    with pytest.raises(RuntimeError, match=r"timer kv \w+ failed: _ErrorBackend"):
        _tick()
    assert not host.relay_calls  # the silent path never replies, even on failure
    assert _errors(host)


def test_a_failing_last_fired_read_raises_silently(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(host, [_timer()])
    host.advance(minutes=30)
    real_get = host.kv.get

    def flaky_get(key: str) -> bytes | None:
        if "timer.last." in key:
            raise RuntimeError("down")
        return real_get(key)

    monkeypatch.setattr(sys.modules["wit_world"].imports.kv, "get", flaky_get)
    with pytest.raises(RuntimeError, match="timer kv get_last failed"):
        _tick()
    assert not host.relay_calls


@pytest.mark.parametrize(
    "blob",
    [
        b"\xff\xfe",
        b"not json",
        b"[]",
        b'{"v": 2, "timers": []}',
        b'{"v": 1}',
        b'{"v": 1, "timers": "x"}',
        b'{"v": 1, "timers": [5]}',
    ],
)
def test_a_corrupt_config_fails_loud_and_is_never_overwritten(blob: bytes, host: _Host) -> None:
    host.kv.store[_scoped(_CFG_KEY)] = blob
    with pytest.raises(RuntimeError, match="timer corrupt state"):
        _admin("list")
    assert "run !timer reset" in host.last_reply()
    assert host.kv.store[_scoped(_CFG_KEY)] == blob
    with pytest.raises(RuntimeError, match="timer corrupt state"):
        _add("promo", 30, "hello")
    assert host.kv.store[_scoped(_CFG_KEY)] == blob


def test_a_corrupt_config_on_the_tick_path_is_loud_but_never_replies_to_chat(host: _Host) -> None:
    host.kv.store[_scoped(_CFG_KEY)] = b"garbage"
    before = len(host.relay_calls)
    with pytest.raises(RuntimeError, match="timer corrupt state"):
        _tick()
    assert len(host.relay_calls) == before
    assert _errors(host)


def test_reset_recovers_a_corrupt_config(host: _Host) -> None:
    host.kv.store[_scoped(_CFG_KEY)] = b"garbage"
    _admin("reset")
    _add("promo", 30, "hello")
    assert [t.name for t in _timers(host)] == ["promo"]


def _good(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "n": "promo",
        "i": 30,
        "m": "hello",
        "e": True,
        "c": "0123456789abcdef",
        "a": 0,
    }
    item.update(overrides)
    return item


def _blob(*items: dict[str, Any]) -> bytes:
    return json.dumps({"v": 1, "timers": list(items)}).encode()


def test_a_well_formed_config_round_trips() -> None:
    timers = _parse_config(_blob(_good()))
    assert timers == [Timer("promo", 30, "hello", True, "0123456789abcdef", 0)]
    assert _parse_config(app._dump_config(timers)) == timers


@pytest.mark.parametrize(
    "bad",
    [
        {"n": "Bad Name"},
        {"n": 5},
        {"i": True},
        {"i": "30"},
        {"i": 4},
        {"i": 1441},
        {"m": ""},
        {"m": "x" * 301},
        {"m": 5},
        {"e": "true"},
        {"e": 1},
        {"c": "short"},
        {"c": "0123456789ABCDEF"},
        {"a": -1},
        {"a": True},
        {"a": "0"},
    ],
)
def test_strict_config_validation_rejects_each_malformed_field(bad: dict[str, Any]) -> None:
    with pytest.raises(app._ConfigError):
        _parse_config(_blob(_good(**bad)))


def test_config_validation_rejects_duplicates_and_overflow() -> None:
    with pytest.raises(app._ConfigError, match="duplicate"):
        _parse_config(_blob(_good(), _good()))
    with pytest.raises(app._ConfigError, match="cap"):
        _parse_config(_blob(*[_good(n=f"t{i}") for i in range(MAX_TIMERS + 1)]))
    assert len(_parse_config(_blob(*[_good(n=f"t{i}") for i in range(MAX_TIMERS)]))) == MAX_TIMERS


# -- envelope / usage ---------------------------------------------------------------------------


def test_usage_reply_defangs_echoed_user_text(host: _Host) -> None:
    out = _run(transform(_event("!timer @everyone {user:victim}")))
    assert out is not None
    _send("usage", arg=out.payload["arg"])
    reply = host.last_reply()
    assert reply.startswith(_USAGE)
    assert "@everyone" not in reply
    assert "{user:victim}" not in reply


def test_bare_usage(host: _Host) -> None:
    _send("usage")
    assert host.last_reply() == _USAGE


def test_empty_string_community_fails_loud(host: _Host) -> None:
    with pytest.raises(ValueError, match="empty-string community"):
        _admin("list", community="")
    assert "aren't available here yet" in host.last_reply()
    assert not host.kv.calls


def test_empty_string_community_on_the_tick_path_does_not_reply_to_chat(host: _Host) -> None:
    with pytest.raises(ValueError, match="empty-string community"):
        _tick(community="")
    assert not host.relay_calls
    assert not host.kv.calls


def test_missing_channel_id_raises(host: _Host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _send("tick", channel_id=None)
    assert not host.relay_calls


def test_unknown_command_raises(host: _Host) -> None:
    with pytest.raises(ValueError, match="unrecognized timer command"):
        _send("bogus")
    assert not host.relay_calls


def test_dispatch_returns_a_transport_shaped_result(host: _Host) -> None:
    result = _admin("list", platform="discord")
    assert result.transport == "discord"
    assert result.detail == "list"
    assert result.sub_type is None
    assert result.http_status is None
    assert host.relay_calls[-1][0] == "discord"


def test_every_key_is_colon_free(host: _Host) -> None:
    _add("promo", 30, "hello")
    host.advance(minutes=30)
    _tick()
    _admin("delete", "promo")
    keys = [args[0] for _name, args in host.kv.calls]
    assert keys
    assert all(":" not in k for k in keys)


def test_module_exposes_no_feature_flag_guard() -> None:
    source = open(app.__file__, encoding="utf-8").read()
    assert "feature_enabled" not in source
    assert "FLAG_KEY" not in source
