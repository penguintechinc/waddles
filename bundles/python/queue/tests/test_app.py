"""Host-native tests for the `queue` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- `kv` is the shared charset-enforcing fake
(`waddle_sdk.testing.install_fake_kv_host`, gh-631), so any key containing `:` fails a test
immediately; `relay`/`log` are small call-recording stand-ins layered onto the `wit_world` module
that fake installs.
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
    _ENTRY_RE,
    _LOCK_ATTEMPTS,
    _QUEUE_KEY,
    _USAGE,
    DEFAULT_LIST,
    MAX_LIST,
    MAX_QUEUE,
    _caller_role_signal,
    _entry_for,
    _label,
    _pseudonym,
    dispatch,
    transform,
)

COMM = "comm-1"
ZWSP = "​"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _scoped(key: str, community: str | None = COMM) -> str:
    """`fake_kv.store` is keyed by `community_kv`'s own `c.<community>.<key>` prefix."""
    return cast(str, _scoped_key(community, key))


class _Extras:
    """`relay`/`log` call-recording stand-ins layered on the shared `FakeKvHost`."""

    def __init__(self) -> None:
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[int, str, str]] = []

    def replies(self) -> list[str]:
        return [str(json.loads(msg)["text"]) for _provider, msg in self.relay_calls]


@pytest.fixture
def fake_kv(monkeypatch: pytest.MonkeyPatch) -> FakeKvHost:
    return install_fake_kv_host(monkeypatch)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, fake_kv: FakeKvHost) -> _Extras:
    extras = _Extras()
    wit_world = sys.modules["wit_world"]
    wit_world.imports.relay = types.SimpleNamespace(  # type: ignore[attr-defined]
        push=lambda provider, msg: extras.relay_calls.append((provider, msg))
    )
    wit_world.imports.log = types.SimpleNamespace(  # type: ignore[attr-defined]
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: extras.log_calls.append((lvl, msg, fields_json)),
    )
    return extras


def _event(text: Any, **payload_extra: Any) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": "12345", **payload_extra}
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
    actor: str | None = "{user:abc123}",
    community: str | None = COMM,
    arg: str | None = None,
    channel_id: str | None = "12345",
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
        app_id="waddles.core.example.queue",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor=actor,
            payload=payload,
            occurred_at="2026-10-22T00:00:00.000Z",
        ),
        ts="2026-10-22T00:00:00.000Z",
    )


def _send(command: str, **kwargs: Any) -> Any:
    return _run(dispatch(_envelope(command, **kwargs), {}, http_client=None))


def _stored(fake_kv: FakeKvHost, community: str | None = COMM) -> list[str]:
    raw = fake_kv.store.get(_scoped(_QUEUE_KEY, community))
    return cast(list[str], json.loads(raw)) if raw is not None else []


def _mod(**extra: Any) -> dict[str, Any]:
    return {"is_mod": True, **extra}


def _join_users(host: _Extras, *tokens: str) -> None:
    for tok in tokens:
        _send("join", actor="{user:" + tok + "}")


# -- transform ----------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["!vqueue", "!VQUEUE", "  !vqueue  ", "!Vqueue"])
def test_bare_queue_is_a_position_check(text: str, host: _Extras) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == "position"
    assert "arg" not in result.payload


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("!vqueue join", "join"),
        ("!vqueue JOIN", "join"),
        ("!vqueue  leave ", "leave"),
        ("!vqueue next", "next"),
        ("!vqueue clear", "clear"),
        ("!vqueue reset", "clear"),
        ("!vqueue list", "list"),
    ],
)
def test_each_verb_maps_to_its_command(text: str, command: str, host: _Extras) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == command


def test_list_forwards_the_count_argument(host: _Extras) -> None:
    result = _run(transform(_event("!vqueue list 5")))
    assert result is not None
    assert result.payload["command"] == "list"
    assert result.payload["arg"] == "5"


@pytest.mark.parametrize("text", ["!vqueue join now", "!vqueue leave please", "!vqueue next 2"])
def test_domain_verbs_reject_trailing_arguments_with_usage(text: str, host: _Extras) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"
    assert "takes no arguments" in result.payload["arg"]


@pytest.mark.parametrize(
    "text",
    ["!vqueue bogus", "!vqueue add x", "!vqueue remove 1", "!vqueue enable thing", "!vqueue set"],
)
def test_unsupported_grammar_replies_usage_never_drops(text: str, host: _Extras) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


def test_reset_with_arguments_is_usage(host: _Extras) -> None:
    result = _run(transform(_event("!vqueue reset now")))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "!vqueuex",
        "!vqueues join",
        "queue join",
        "",
        "!que",
        "!quote",
        "!queue",
        "!queue join",
        "!queue next",
    ],
)
def test_non_matching_text_is_dropped(text: str, host: _Extras) -> None:
    assert _run(transform(_event(text))) is None


@pytest.mark.parametrize("text", [None, 7, ["!vqueue"], {"a": 1}])
def test_non_string_text_is_dropped(text: Any, host: _Extras) -> None:
    assert _run(transform(_event(text))) is None


def test_transform_forwards_actor_and_channel(host: _Extras) -> None:
    result = _run(transform(_event("!vqueue join")))
    assert result is not None
    assert result.actor == "{user:abc123}"
    assert result.payload["channel_id"] == "12345"
    assert result.platform == "twitch"


def test_badges_are_forwarded_strictly_and_absence_stays_absent(host: _Extras) -> None:
    absent = _run(transform(_event("!vqueue next")))
    assert absent is not None
    assert "is_mod" not in absent.payload
    assert "is_broadcaster" not in absent.payload

    strict = _run(transform(_event("!vqueue next", is_mod="false", is_broadcaster="true")))
    assert strict is not None
    assert strict.payload["is_mod"] is False
    assert strict.payload["is_broadcaster"] is False

    real = _run(transform(_event("!vqueue next", is_mod=True, is_broadcaster=False)))
    assert real is not None
    assert real.payload["is_mod"] is True
    assert real.payload["is_broadcaster"] is False


# -- identity -----------------------------------------------------------------------------------


def test_tokenized_actor_becomes_a_user_entry_and_label() -> None:
    entry = _entry_for(COMM, "{user:abc123}", platform="twitch")
    assert entry == "u:abc123"
    assert _label("u:abc123") == "{user:abc123}"


def test_raw_actor_is_hashed_and_never_kept() -> None:
    entry = _entry_for(COMM, "RawStreamerHandle", platform="twitch")
    assert entry is not None
    assert entry.startswith("h:")
    assert "RawStreamerHandle" not in entry
    assert _ENTRY_RE.match(entry)
    assert _label(entry) == "viewer-" + entry[2:8]


def test_pseudonym_is_per_community_and_deterministic() -> None:
    assert _pseudonym("a", "twitch", "x") == _pseudonym("a", "twitch", "x")
    assert _pseudonym("a", "twitch", "x") != _pseudonym("b", "twitch", "x")
    assert _pseudonym(None, "twitch", "x") == _pseudonym("0", "twitch", "x")


@pytest.mark.parametrize("actor", [None, "", "   ", 5, ["{user:a}"]])
def test_no_usable_actor_has_no_entry(actor: Any) -> None:
    assert _entry_for(COMM, actor, platform="twitch") is None


@pytest.mark.parametrize(
    "actor",
    [
        "{user:}",
        "{user:a b}",
        "{user:a}}",
        "x{user:abc}",
        "{user:abc}x",
        "{user:" + "a" * 129 + "}",
    ],
)
def test_malformed_placeholders_are_hashed_not_trusted_as_tokens(actor: str) -> None:
    entry = _entry_for(COMM, actor, platform="twitch")
    assert entry is not None
    assert entry.startswith("h:")


def test_role_signal_is_strict() -> None:
    assert _caller_role_signal({}) is None
    assert _caller_role_signal({"is_mod": True}) is True
    assert _caller_role_signal({"is_broadcaster": True}) is True
    assert _caller_role_signal({"is_mod": False}) is False
    assert _caller_role_signal({"is_mod": "true"}) is False
    assert _caller_role_signal({"is_mod": 1}) is False


# -- join / leave / position / list -------------------------------------------------------------


def test_join_appends_in_order_and_reports_position(host: _Extras, fake_kv: FakeKvHost) -> None:
    _join_users(host, "aaa", "bbb", "ccc")
    assert _stored(fake_kv) == ["u:aaa", "u:bbb", "u:ccc"]
    assert host.replies() == [
        "{user:aaa} joined the queue at position 1.",
        "{user:bbb} joined the queue at position 2.",
        "{user:ccc} joined the queue at position 3.",
    ]


def test_double_join_is_rejected_with_the_existing_position(
    host: _Extras, fake_kv: FakeKvHost
) -> None:
    _join_users(host, "aaa", "bbb")
    _send("join", actor="{user:bbb}")
    assert host.replies()[-1] == "{user:bbb}, you're already in the queue at position 2."
    assert _stored(fake_kv) == ["u:aaa", "u:bbb"]


def test_join_without_identity_is_refused_and_writes_nothing(
    host: _Extras, fake_kv: FakeKvHost
) -> None:
    _send("join", actor=None)
    assert "couldn't identify you" in host.replies()[-1]
    assert not fake_kv.store
    assert not fake_kv.calls


def test_join_when_full_is_refused(host: _Extras, fake_kv: FakeKvHost) -> None:
    fake_kv.store[_scoped(_QUEUE_KEY)] = json.dumps([f"u:t{i}" for i in range(MAX_QUEUE)]).encode()
    _send("join", actor="{user:late}")
    assert host.replies()[-1] == f"The queue is full ({MAX_QUEUE} waiting)."
    assert len(_stored(fake_kv)) == MAX_QUEUE


def test_a_full_queue_of_maximum_size_entries_fits_the_host_value_cap() -> None:
    biggest = "u:" + "a" * 128
    assert len(json.dumps([biggest] * MAX_QUEUE)) < 64 * 1024


def test_leave_removes_only_the_caller(host: _Extras, fake_kv: FakeKvHost) -> None:
    _join_users(host, "aaa", "bbb", "ccc")
    _send("leave", actor="{user:bbb}")
    assert host.replies()[-1] == "{user:bbb} left the queue."
    assert _stored(fake_kv) == ["u:aaa", "u:ccc"]


def test_leave_when_not_queued_is_a_friendly_reply_and_changes_nothing(
    host: _Extras, fake_kv: FakeKvHost
) -> None:
    _join_users(host, "aaa")
    before = dict(fake_kv.store)
    _send("leave", actor="{user:zzz}")
    assert host.replies()[-1] == "{user:zzz}, you're not in the queue."
    assert fake_kv.store == before


def test_leave_without_identity_is_refused(host: _Extras) -> None:
    _send("leave", actor=None)
    assert "couldn't identify you" in host.replies()[-1]


def test_position_for_a_queued_viewer(host: _Extras) -> None:
    _join_users(host, "aaa", "bbb", "ccc")
    _send("position", actor="{user:ccc}")
    assert host.replies()[-1] == "{user:ccc}, you're #3 of 3 in the queue."


def test_position_when_not_queued_with_others_waiting(host: _Extras) -> None:
    _join_users(host, "aaa")
    _send("position", actor="{user:zzz}")
    assert (
        host.replies()[-1] == "{user:zzz}, you're not in the queue (1 waiting). Use !vqueue join."
    )


def test_position_on_an_empty_queue(host: _Extras) -> None:
    _send("position")
    assert host.replies()[-1] == (
        "{user:abc123}, the queue is empty. Use !vqueue join to get in line."
    )


def test_position_without_identity_is_refused(host: _Extras) -> None:
    _send("position", actor=None)
    assert "couldn't identify you" in host.replies()[-1]


def test_position_is_a_pure_read(host: _Extras, fake_kv: FakeKvHost) -> None:
    _join_users(host, "aaa")
    fake_kv.calls.clear()
    _send("position", actor="{user:aaa}")
    assert [name for name, _args in fake_kv.calls] == ["get"]


def test_list_shows_the_front_in_order(host: _Extras) -> None:
    _join_users(host, "aaa", "bbb", "ccc")
    _send("list")
    assert host.replies()[-1] == "Queue (3): 1. {user:aaa} 2. {user:bbb} 3. {user:ccc}"


def test_list_respects_n_and_reports_the_remainder(host: _Extras) -> None:
    _join_users(host, "aaa", "bbb", "ccc", "ddd")
    _send("list", arg="2")
    assert host.replies()[-1] == "Queue (4): 1. {user:aaa} 2. {user:bbb} (+2 more)"


def test_list_defaults_to_ten(host: _Extras, fake_kv: FakeKvHost) -> None:
    fake_kv.store[_scoped(_QUEUE_KEY)] = json.dumps([f"u:t{i}" for i in range(25)]).encode()
    _send("list")
    reply = host.replies()[-1]
    assert reply.startswith("Queue (25): 1. {user:t0} 2. {user:t1}")
    assert f"{DEFAULT_LIST}. {{user:t{DEFAULT_LIST - 1}}}" in reply
    assert f"{DEFAULT_LIST + 1}." not in reply
    assert reply.endswith(f"(+{25 - DEFAULT_LIST} more)")


def test_list_on_an_empty_queue(host: _Extras) -> None:
    _send("list")
    assert host.replies()[-1] == "The queue is empty."


@pytest.mark.parametrize("arg", ["0", "-1", str(MAX_LIST + 1), "abc", "1.5", "1 2", "٣"])
def test_list_rejects_a_bad_count(arg: str, host: _Extras) -> None:
    _send("list", arg=arg)
    assert host.replies()[-1] == f"Usage: !vqueue list [n] (n is 1-{MAX_LIST})"


def test_list_accepts_the_maximum_count(host: _Extras, fake_kv: FakeKvHost) -> None:
    fake_kv.store[_scoped(_QUEUE_KEY)] = json.dumps([f"u:t{i}" for i in range(20)]).encode()
    _send("list", arg=str(MAX_LIST))
    assert host.replies()[-1].endswith(f"(+{20 - MAX_LIST} more)")


# -- next / clear (moderator gate) --------------------------------------------------------------


def test_next_pops_the_front_fifo(host: _Extras, fake_kv: FakeKvHost) -> None:
    _join_users(host, "aaa", "bbb", "ccc")
    _send("next", **_mod())
    assert host.replies()[-1] == "Next up: {user:aaa}. 2 still waiting."
    _send("next", **_mod())
    assert host.replies()[-1] == "Next up: {user:bbb}. 1 still waiting."
    assert _stored(fake_kv) == ["u:ccc"]


def test_next_on_the_last_entry_says_the_queue_is_empty(host: _Extras, fake_kv: FakeKvHost) -> None:
    _join_users(host, "aaa")
    _send("next", **_mod())
    assert host.replies()[-1] == "Next up: {user:aaa}. The queue is now empty."
    assert _stored(fake_kv) == []


def test_next_on_an_empty_queue(host: _Extras) -> None:
    _send("next", **_mod())
    assert host.replies()[-1] == "The queue is empty."


def test_clear_empties_and_counts(host: _Extras, fake_kv: FakeKvHost) -> None:
    _join_users(host, "aaa", "bbb")
    _send("clear", is_broadcaster=True)
    assert host.replies()[-1] == "Cleared the queue (2 removed)."
    assert _scoped(_QUEUE_KEY) not in fake_kv.store


def test_clear_on_an_empty_queue(host: _Extras) -> None:
    _send("clear", **_mod())
    assert host.replies()[-1] == "The queue is already empty."


@pytest.mark.parametrize("command", ["next", "clear"])
@pytest.mark.parametrize(
    "badges",
    [{}, {"is_mod": False}, {"is_mod": "true"}, {"is_mod": 1}, {"is_broadcaster": "yes"}],
    ids=["absent", "false", "string-true", "int-one", "string-broadcaster"],
)
def test_mod_commands_are_denied_without_a_real_true_badge(
    command: str, badges: dict[str, Any], host: _Extras, fake_kv: FakeKvHost
) -> None:
    _join_users(host, "aaa")
    snapshot = dict(fake_kv.store)
    fake_kv.calls.clear()
    _send(command, **badges)
    assert host.replies()[-1] == f"Only the broadcaster or a moderator can use !vqueue {command}."
    assert fake_kv.store == snapshot
    assert not fake_kv.calls  # denied BEFORE any kv access, not even the lock


def test_viewer_commands_need_no_badge(host: _Extras) -> None:
    _send("join")
    _send("position")
    _send("list")
    _send("leave")
    assert not any("Only the broadcaster" in r for r in host.replies())


# -- scoping and keys ---------------------------------------------------------------------------


def test_queues_are_isolated_per_community(host: _Extras, fake_kv: FakeKvHost) -> None:
    _send("join", community="comm-a", actor="{user:aaa}")
    _send("join", community="comm-b", actor="{user:bbb}")
    assert _stored(fake_kv, "comm-a") == ["u:aaa"]
    assert _stored(fake_kv, "comm-b") == ["u:bbb"]
    _send("clear", community="comm-a", **_mod())
    assert _stored(fake_kv, "comm-b") == ["u:bbb"]


def test_tenant_wide_sentinel_community_works(host: _Extras, fake_kv: FakeKvHost) -> None:
    _send("join", community=None, actor="{user:aaa}")
    assert _stored(fake_kv, None) == ["u:aaa"]
    assert _scoped(_QUEUE_KEY, None) == "c.0.queue.entries"


def test_empty_string_community_fails_loud_without_touching_kv(
    host: _Extras, fake_kv: FakeKvHost
) -> None:
    with pytest.raises(ValueError, match="empty-string community"):
        _send("join", community="")
    assert "isn't available here yet" in host.replies()[-1]
    assert not fake_kv.calls


def test_every_key_is_colon_free_and_the_ttl_is_refreshed(
    host: _Extras, fake_kv: FakeKvHost
) -> None:
    _join_users(host, "aaa")
    keys = [args[0] for _name, args in fake_kv.calls]
    assert keys
    assert all(":" not in k for k in keys)
    sets = [args for name, args in fake_kv.calls if name == "set"]
    assert sets
    assert all(ttl == 7 * 24 * 60 * 60 for _key, _val, ttl in sets)


# -- PII ----------------------------------------------------------------------------------------


def test_a_raw_handle_never_reaches_kv_logs_or_replies(host: _Extras, fake_kv: FakeKvHost) -> None:
    _send("join", actor="RawStreamerHandle")
    _send("position", actor="RawStreamerHandle")
    _send("list", actor="RawStreamerHandle")
    _send("next", actor="RawStreamerHandle", **_mod())
    haystack = repr(fake_kv.store) + repr(fake_kv.calls) + repr(host.log_calls)
    haystack += repr(host.relay_calls)
    assert "RawStreamerHandle" not in haystack


def test_a_raw_handle_viewer_can_still_find_their_place(host: _Extras, fake_kv: FakeKvHost) -> None:
    _send("join", actor="alice")
    _send("join", actor="bob")
    _send("position", actor="bob")
    assert "you're #2 of 2" in host.replies()[-1]
    entry = _entry_for(COMM, "bob", platform="twitch")
    assert entry is not None
    assert _label(entry) in host.replies()[-1]
    _send("list")
    assert "viewer-" in host.replies()[-1]


def test_no_log_line_carries_a_token_or_label(host: _Extras) -> None:
    _join_users(host, "secrettoken")
    _send("next", **_mod())
    assert "secrettoken" not in repr(host.log_calls)


# -- usage replies echo user text only after sanitizing -----------------------------------------


def test_usage_reply_defangs_a_mass_mention(host: _Extras) -> None:
    out = _run(transform(_event("!vqueue @everyone")))
    assert out is not None
    _send("usage", arg=out.payload["arg"])
    reply = host.replies()[-1]
    assert reply.startswith(_USAGE)
    assert "@everyone" not in reply
    assert f"@{ZWSP}everyone" in reply


def test_usage_reply_escapes_a_forged_egress_token(host: _Extras) -> None:
    out = _run(transform(_event("!vqueue {user:victim}")))
    assert out is not None
    _send("usage", arg=out.payload["arg"])
    reply = host.replies()[-1]
    assert "{user:victim}" not in reply
    assert "\\{user:victim\\}" in reply


def test_usage_reply_never_starts_with_a_command_character(host: _Extras) -> None:
    for text in ("!vqueue /ban x", "!vqueue .timeout y", "!vqueue !other"):
        out = _run(transform(_event(text)))
        assert out is not None
        _send("usage", arg=out.payload["arg"])
        assert host.replies()[-1][0] not in "/.!"


def test_bare_usage_without_an_argument(host: _Extras) -> None:
    _send("usage")
    assert host.replies()[-1] == _USAGE


# -- lock ---------------------------------------------------------------------------------------


def test_a_held_lock_yields_busy_and_changes_nothing(host: _Extras, fake_kv: FakeKvHost) -> None:
    _join_users(host, "aaa")
    fake_kv.store[_scoped("queue.lock")] = b"1"  # another invocation holds it
    snapshot = dict(fake_kv.store)
    fake_kv.calls.clear()
    _send("join", actor="{user:bbb}")
    assert host.replies()[-1] == "The queue is busy right now, please try again in a moment."
    increments = [c for c in fake_kv.calls if c[0] == "increment"]
    assert len(increments) == _LOCK_ATTEMPTS
    assert _stored(fake_kv) == ["u:aaa"]
    # The other holder's lock is untouched: we never took it, so we must not release it.
    assert snapshot[_scoped("queue.lock")] == b"1"
    assert fake_kv.store[_scoped("queue.lock")] == str(1 + _LOCK_ATTEMPTS).encode()


@pytest.mark.parametrize("command", ["join", "leave", "next", "clear"])
def test_every_mutation_holds_and_releases_the_lock(
    command: str, host: _Extras, fake_kv: FakeKvHost
) -> None:
    _join_users(host, "aaa")
    fake_kv.calls.clear()
    _send(command, actor="{user:aaa}", **_mod())
    names = [name for name, _args in fake_kv.calls]
    assert names[0] == "increment"
    assert names[-1] == "delete"
    assert _scoped("queue.lock") not in fake_kv.store


def test_reads_take_no_lock(host: _Extras, fake_kv: FakeKvHost) -> None:
    _join_users(host, "aaa")
    fake_kv.calls.clear()
    _send("position", actor="{user:aaa}")
    _send("list")
    assert not [c for c in fake_kv.calls if c[0] in {"increment", "delete", "set"}]


def test_lock_is_released_when_the_save_fails(
    host: _Extras, fake_kv: FakeKvHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_kv_raises(monkeypatch, op="set")
    with pytest.raises(RuntimeError, match="queue kv set failed: _ErrorBackend"):
        _send("join")
    assert _scoped("queue.lock") not in fake_kv.store


def test_every_command_stays_inside_the_host_op_budget(host: _Extras, fake_kv: FakeKvHost) -> None:
    for command in ("join", "leave", "position", "list", "next", "clear"):
        fake_kv.calls.clear()
        _send(command, **_mod())
        assert len(fake_kv.calls) <= 5


# -- failure paths ------------------------------------------------------------------------------


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


@pytest.mark.parametrize("op", ["get", "set", "delete", "increment"])
def test_kv_backend_errors_reply_and_raise(
    op: str, host: _Extras, fake_kv: FakeKvHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _join_users(host, "aaa")
    _set_kv_raises(monkeypatch, op=op)
    command = "clear" if op == "delete" else "join"
    with pytest.raises(RuntimeError, match=r"queue kv \w+ failed: _ErrorBackend"):
        _send(command, actor="{user:zzz}", **_mod())
    assert host.replies()[-1] == "The queue is temporarily unavailable, try again shortly."
    errors = [c for c in host.log_calls if c[0] == 0]
    assert errors
    assert "zzz" not in repr(errors)


@pytest.mark.parametrize(
    "blob",
    [b"\xff\xfe", b"not json", b'{"a": 1}', b'["u:ok", 5]', b'["u:ok", "garbage"]', b'["h:short"]'],
    ids=["non-utf8", "non-json", "object", "non-string", "bad-entry", "bad-hash"],
)
def test_corrupt_state_fails_loud_and_is_never_overwritten(
    blob: bytes, host: _Extras, fake_kv: FakeKvHost
) -> None:
    fake_kv.store[_scoped(_QUEUE_KEY)] = blob
    with pytest.raises(RuntimeError, match="queue corrupt state"):
        _send("position")
    assert "corrupted" in host.replies()[-1]
    assert fake_kv.store[_scoped(_QUEUE_KEY)] == blob


def test_an_over_cap_stored_list_is_corrupt(host: _Extras, fake_kv: FakeKvHost) -> None:
    too_many = [f"u:t{i}" for i in range(MAX_QUEUE + 1)]
    fake_kv.store[_scoped(_QUEUE_KEY)] = json.dumps(too_many).encode()
    with pytest.raises(RuntimeError, match="over the entry cap"):
        _send("list")


def test_missing_channel_id_raises(host: _Extras) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _send("join", channel_id=None)
    assert not host.relay_calls


def test_unknown_command_raises(host: _Extras) -> None:
    with pytest.raises(ValueError, match="unrecognized queue command"):
        _send("bogus")
    assert not host.relay_calls


def test_dispatch_returns_a_transport_shaped_result(host: _Extras) -> None:
    result = _send("join", platform="discord")
    assert result.transport == "discord"
    assert result.detail == "join"
    assert result.sub_type is None
    assert result.http_status is None
    assert host.relay_calls[-1][0] == "discord"
    assert json.loads(host.relay_calls[-1][1])["channel"] == "12345"


def test_module_exposes_no_feature_flag_guard() -> None:
    source = open(app.__file__, encoding="utf-8").read()
    assert "feature_enabled" not in source
    assert "FLAG_KEY" not in source


# -- security review regressions: identity-spoofing / parser-differential ------------------------


def test_the_same_handle_on_two_platforms_is_two_different_viewers(
    host: _Extras, fake_kv: FakeKvHost
) -> None:
    # regression: the raw-handle pseudonym hashed `community|handle` only, so a Discord `bob` and a
    # Twitch `bob` shared one queue entry -- one could evict or impersonate the other.
    _send("join", actor="bob", platform="twitch")
    _send("position", actor="bob", platform="discord")
    assert "you're not in the queue (1 waiting)" in host.replies()[-1]
    _send("leave", actor="bob", platform="discord")
    assert "you're not in the queue" in host.replies()[-1]
    assert len(_stored(fake_kv)) == 1  # the Twitch viewer is still in line
    _send("join", actor="bob", platform="discord")
    assert "joined the queue at position 2" in host.replies()[-1]
    assert len(set(_stored(fake_kv))) == 2


def test_the_raw_pseudonym_is_per_platform() -> None:
    assert _pseudonym(COMM, "twitch", "bob") != _pseudonym(COMM, "discord", "bob")
    assert _pseudonym(COMM, "twitch", "bob") == _pseudonym(COMM, "twitch", "bob")


@pytest.mark.parametrize(
    "actor",
    [
        " {user:abc123}",
        "{user:abc123} ",
        "{user:abc123}\n",
        "\n{user:abc123}",
        "\t{user:abc123}\t",
        chr(0xA0) + "{user:abc123}",
        chr(0x2028) + "{user:abc123}" + chr(0x2028),
    ],
)
def test_a_padded_placeholder_is_a_raw_handle_never_another_viewers_token(actor: str) -> None:
    # regression: the actor was `.strip()`ped before the anchored match, so a handle that only
    # LOOKED like a minted placeholder (the pipeline never pads one) resolved to that viewer.
    entry = _entry_for(COMM, actor, platform="twitch")
    assert entry is not None and entry.startswith("h:")
    assert entry != "u:abc123"


def test_a_forged_placeholder_handle_cannot_evict_the_real_viewer(
    host: _Extras, fake_kv: FakeKvHost
) -> None:
    _send("join", actor="{user:abc123}")
    _send("leave", actor="{user:abc123}\n")
    assert "you're not in the queue" in host.replies()[-1]
    assert _stored(fake_kv) == ["u:abc123"]


@pytest.mark.parametrize(
    "text", ["!vqueue join @bob", "!vqueue leave {user:abc123}", "!vqueue join {user:x}"]
)
def test_a_viewer_can_never_name_another_viewer(
    text: str, host: _Extras, fake_kv: FakeKvHost
) -> None:
    result = _run(transform(_event(text)))
    assert result is not None and result.payload["command"] == "usage"
    assert _stored(fake_kv) == []


def test_anchored_validators_reject_a_trailing_newline(host: _Extras, fake_kv: FakeKvHost) -> None:
    # regression: `re.match` + `$` accepts "u:abc\n"; every validator is `fullmatch` now.
    assert _ENTRY_RE.fullmatch("u:abc\n") is None
    assert _ENTRY_RE.fullmatch("h:" + "a" * 24 + "\n") is None
    assert app._COUNT_RE.fullmatch("5\n") is None
    blob = json.dumps(["u:abc\n"]).encode()
    fake_kv.store[_scoped(_QUEUE_KEY)] = blob
    with pytest.raises(RuntimeError, match="queue corrupt state"):
        _send("position")
    result = _run(transform(_event("!vqueue list 5\n")))  # the transform strips the line itself
    assert result is not None and result.payload["arg"] == "5"
    _send("list", arg="5\n")
    assert "Usage: !vqueue list [n]" in host.replies()[-1]


def test_the_renamed_trigger_is_the_only_one_this_bundle_answers(host: _Extras) -> None:
    # `!queue` belongs to `music` (its alias) -- answering it too would double every reply.
    for text in ("!queue", "!queue join", "!queue next", "!QUEUE list"):
        assert _run(transform(_event(text))) is None, text
    for text in ("!vqueue", "!VQUEUE join"):
        assert _run(transform(_event(text))) is not None, text


def test_a_padded_raw_handle_is_the_same_viewer_as_the_bare_one() -> None:
    # A raw handle is trimmed before it is hashed, so incidental padding never forks one viewer
    # into two entries -- only a PLACEHOLDER-shaped actor is refused the token treatment.
    assert _entry_for(COMM, "  bob\n", platform="twitch") == _entry_for(
        COMM, "bob", platform="twitch"
    )
