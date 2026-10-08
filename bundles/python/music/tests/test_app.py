"""Host-native tests for the `music` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/fish/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors (extended with a fake `kv`/`clock` import, same shape as
`bundles/python/fish`/`bundles/python/shoutout`'s own harnesses -- `community_kv` is a thin prefix
wrapper over the same `kv` WIT import, so the same fake serves both).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.command import ParsedCommand
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

from app import (
    _MAX_PER_USER_CONFIG_KEY,
    _QUEUE_KEY,
    _SEQ_KEY,
    DEFAULT_MAX_PER_USER,
    MAX_MAX_PER_USER,
    MAX_QUEUE_SIZE,
    MAX_REQUEST_TEXT_LEN,
    MIN_MAX_PER_USER,
    SHOW_LIMIT,
    QueueEntry,
    _caller_role_signal,
    _map_parsed,
    _pseudonym,
    _resolve,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _expected_pseudonym(actor: str | None) -> str:
    return hashlib.sha256((actor or "anonymous").encode()).hexdigest()


def _scoped(key: str, community: str = "comm-1") -> str:
    """`fake_host.store` is keyed by `community_kv`'s own `c:<community>:<key>` prefix."""
    return _scoped_key(community, key)


def _sample_event(
    text: str,
    *,
    channel_id: str | None = "12345",
    actor: str | None = "viewer-1",
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": channel_id}
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at="2026-10-05T00:00:00.000Z",
    )


class _FakeHost:
    """Fake WIT host: flags/kv/relay/log/clock, with a real in-memory kv store + TTL ignored."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.kv_calls: list[tuple[Any, ...]] = []
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[str, str, str]] = []
        self.now_ms = 1_700_000_000_000

    def advance(self, seconds: float) -> None:
        self.now_ms += int(seconds * 1000)


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    host = _FakeHost()
    _install(monkeypatch, host)
    return host


def _install(
    monkeypatch: pytest.MonkeyPatch,
    host: _FakeHost,
    *,
    kv_get_raises: Exception | None = None,
    kv_set_raises: Exception | None = None,
    kv_increment_raises: Exception | None = None,
    flag_enabled: bool = True,
) -> None:
    def kv_get(key: str) -> bytes | None:
        host.kv_calls.append(("get", key))
        if kv_get_raises is not None:
            raise kv_get_raises
        return host.store.get(key)

    def kv_set(key: str, value: bytes, ttl: int) -> None:
        host.kv_calls.append(("set", key, bytes(value), ttl))
        if kv_set_raises is not None:
            raise kv_set_raises
        host.store[key] = bytes(value)

    def kv_delete(key: str) -> None:
        host.kv_calls.append(("delete", key))
        host.store.pop(key, None)

    def kv_increment(key: str, delta: int, ttl: int) -> int:
        host.kv_calls.append(("increment", key, delta, ttl))
        if kv_increment_raises is not None:
            raise kv_increment_raises
        current = int(host.store.get(key, b"0").decode())
        new_value = current + delta
        host.store[key] = str(new_value).encode()
        return new_value

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: flag_enabled)
    kv_mod = types.SimpleNamespace(
        get=kv_get, set=kv_set, delete=kv_delete, increment=kv_increment
    )
    relay_mod = types.SimpleNamespace(
        push=lambda provider, msg: host.relay_calls.append((provider, msg))
    )
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: host.log_calls.append((lvl, msg, fields_json)),
    )
    clock_mod = types.SimpleNamespace(
        now_millis=lambda: host.now_ms,
        now_rfc3339=lambda: "2026-10-05T00:00:00.000Z",
        monotonic_nanos=lambda: 0,
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, kv=kv_mod, relay=relay_mod, log=log_mod, clock=clock_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)


def _sample_envelope(
    platform: str,
    command: str,
    *,
    community: str | None = "comm-1",
    actor: str | None = "viewer-1",
    arg: str | None = None,
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": "12345"}
    if arg is not None:
        payload["arg"] = arg
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.music",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor=actor,
            payload=payload,
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )


def _reply_text(host: _FakeHost) -> str:
    _provider, message_json = host.relay_calls[-1]
    return str(json.loads(message_json)["text"])


# -- transform() parsing: !sr / !songrequest -----------------------------------


@pytest.mark.parametrize("head", ["!sr", "!SR", "!songrequest", "!SongRequest"])
def test_request_heads_match_case_insensitively(head: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(f"{head} never gonna give you up")))
    assert result is not None
    assert result.payload["command"] == "add"
    assert result.payload["arg"] == "never gonna give you up"


@pytest.mark.parametrize("text", ["!sr", "!sr   ", "!songrequest"])
def test_request_head_with_no_text_is_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


# -- transform() parsing: !music / !queue --------------------------------------


@pytest.mark.parametrize("text", ["!music", "!MUSIC", "  !music  ", "!queue", "!QUEUE"])
def test_bare_music_and_queue_alias_match_as_show(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "show"


def test_music_list_matches_as_show(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!music list")))
    assert result is not None
    assert result.payload["command"] == "show"


def test_music_list_with_trailing_args_is_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!music list extra")))
    assert result is not None
    assert result.payload["command"] == "usage"


def test_music_remove_matches_with_id(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!music remove 7")))
    assert result is not None
    assert result.payload["command"] == "remove"
    assert result.payload["arg"] == "7"


def test_music_remove_without_id_is_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!music remove")))
    assert result is not None
    assert result.payload["command"] == "usage"


def test_music_set_max_per_user_matches_with_args(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!music set max-per-user 5")))
    assert result is not None
    assert result.payload["command"] == "config_set"
    assert result.payload["arg"] == "max-per-user 5"


@pytest.mark.parametrize("text", ["!music next", "!music skip", "!music NEXT", "!music SKIP"])
def test_music_next_and_skip_match_as_advance(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "advance"


@pytest.mark.parametrize("text", ["!music next now", "!music skip please"])
def test_music_next_skip_with_trailing_args_is_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize(
    "text", ["!music enable ai", "!music bogus", "!music add", "!music reset", "!music disable foo"]
)
def test_unsupported_verbs_and_bad_grammar_reply_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!musical", "music", "!musics", "!queued", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host: _FakeHost) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_string_text_is_ignored(fake_host: _FakeHost) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": None, "channel_id": "1"},
        occurred_at="",
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, flag_enabled=False)
    assert _run(transform(_sample_event("!music"))) is None
    assert _run(transform(_sample_event("!sr some song"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!music", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!music")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# -- _resolve() / _map_parsed() direct unit coverage ---------------------------


def test_resolve_empty_rest_is_show() -> None:
    assert _resolve("") == ("show", None)


def test_resolve_unknown_leading_token_is_usage(fake_host: _FakeHost) -> None:
    """`fake_host` wires a fake `wit_world.imports.log`.

    `_resolve`'s `CommandUsageError` path now logs (this PR's no-stubs fix, see
    `_map_parsed`'s own `log.debug` call) before returning the usage tuple.
    """
    assert _resolve("bogus") == ("usage", None)


def test_map_parsed_unimplemented_verb_is_usage() -> None:
    parsed = ParsedCommand(command="music", sub_module=None, option="add", args=None)
    assert _map_parsed(parsed) == ("usage", None)


# -- !sr / !songrequest add -----------------------------------------------------


def test_first_request_persists_state_and_replies(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "add", arg="some great song")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "add"
    pseudonym = _expected_pseudonym("viewer-1")
    queue = json.loads(fake_host.store[_scoped(_QUEUE_KEY)].decode())
    assert len(queue) == 1
    assert queue[0]["requester_pseudonym"] == pseudonym
    assert queue[0]["text"] == "some great song"
    assert queue[0]["id"] == 1
    assert "added to the queue at position 1 (#1)" in _reply_text(fake_host)


def test_second_request_increments_sequence_and_position(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "add", arg="song a"), {}, http_client=None))
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="song b", actor="viewer-2"),
            {},
            http_client=None,
        )
    )
    assert "added to the queue at position 2 (#2)" in _reply_text(fake_host)


def test_add_with_no_arg_is_usage_reply(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "add", arg=None)
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "add"
    assert "Usage:" in _reply_text(fake_host)


def test_add_with_whitespace_only_arg_is_usage_reply(fake_host: _FakeHost) -> None:
    """`transform()` never forwards whitespace-only args, but `dispatch()` must still fail loud."""
    envelope = _sample_envelope("twitch", "add", arg="   ")
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "add"
    assert "Usage:" in _reply_text(fake_host)


def test_add_rejects_overlong_text(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "add", arg="x" * (MAX_REQUEST_TEXT_LEN + 1))
    _run(dispatch(envelope, {}, http_client=None))
    assert f"{MAX_REQUEST_TEXT_LEN} characters or fewer" in _reply_text(fake_host)
    assert _scoped(_QUEUE_KEY) not in fake_host.store


def test_add_enforces_default_max_per_user_limit(fake_host: _FakeHost) -> None:
    for i in range(DEFAULT_MAX_PER_USER):
        _run(dispatch(_sample_envelope("twitch", "add", arg=f"song {i}"), {}, http_client=None))
    result = _run(
        dispatch(_sample_envelope("twitch", "add", arg="one too many"), {}, http_client=None)
    )

    assert result.detail == "add"
    assert f"the limit is {DEFAULT_MAX_PER_USER}" in _reply_text(fake_host)
    queue = json.loads(fake_host.store[_scoped(_QUEUE_KEY)].decode())
    assert len(queue) == DEFAULT_MAX_PER_USER


def test_different_users_have_independent_pending_limits(fake_host: _FakeHost) -> None:
    for i in range(DEFAULT_MAX_PER_USER):
        _run(dispatch(_sample_envelope("twitch", "add", arg=f"song {i}"), {}, http_client=None))
    result = _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="viewer-2's song", actor="viewer-2"),
            {},
            http_client=None,
        )
    )
    assert "added to the queue" in _reply_text(fake_host)
    queue = json.loads(fake_host.store[_scoped(_QUEUE_KEY)].decode())
    assert len(queue) == DEFAULT_MAX_PER_USER + 1
    assert result.detail == "add"


def test_add_rejects_when_queue_is_full(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.MAX_QUEUE_SIZE", 1)
    monkeypatch.setattr("app.DEFAULT_MAX_PER_USER", 5)
    _run(dispatch(_sample_envelope("twitch", "add", arg="first"), {}, http_client=None))
    result = _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="second", actor="viewer-2"), {}, http_client=None
        )
    )
    assert result.detail == "add"
    assert "the queue is full" in _reply_text(fake_host)
    queue = json.loads(fake_host.store[_scoped(_QUEUE_KEY)].decode())
    assert len(queue) == 1


def test_max_queue_size_constant_is_sane() -> None:
    assert MAX_QUEUE_SIZE > 0


# -- !music / !queue show -------------------------------------------------------


def test_show_on_empty_queue(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "show"), {}, http_client=None))
    assert result.detail == "show"
    assert _reply_text(fake_host) == "the queue is empty."


def test_show_marks_callers_own_entries(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "add", arg="my song"), {}, http_client=None))
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="their song", actor="viewer-2"),
            {},
            http_client=None,
        )
    )
    _run(dispatch(_sample_envelope("twitch", "show"), {}, http_client=None))
    text = _reply_text(fake_host)
    assert "#1 my song (yours)" in text
    assert "#2 their song" in text
    assert "#2 their song (yours)" not in text
    assert "Queue (2):" in text


def test_show_truncates_beyond_show_limit(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.SHOW_LIMIT", 2)
    monkeypatch.setattr("app.DEFAULT_MAX_PER_USER", 5)
    for i in range(3):
        _run(dispatch(_sample_envelope("twitch", "add", arg=f"song {i}"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "show"), {}, http_client=None))
    text = _reply_text(fake_host)
    assert "Queue (3):" in text
    assert "(+1 more)" in text
    assert "song 2" not in text


def test_show_limit_constant_is_sane() -> None:
    assert SHOW_LIMIT > 0


# -- !music remove <id> ----------------------------------------------------------


def test_owner_can_remove_own_request(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "add", arg="my song"), {}, http_client=None))
    result = _run(
        dispatch(_sample_envelope("twitch", "remove", arg="1"), {}, http_client=None)
    )
    assert result.detail == "remove"
    assert "removed request #1" in _reply_text(fake_host)
    assert json.loads(fake_host.store[_scoped(_QUEUE_KEY)].decode()) == []


def test_non_owner_non_mod_cannot_remove(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "add", arg="my song"), {}, http_client=None))
    result = _run(
        dispatch(
            _sample_envelope("twitch", "remove", arg="1", actor="viewer-2"), {}, http_client=None
        )
    )
    assert result.detail == "remove"
    assert "you can only remove your own requests" in _reply_text(fake_host)
    assert len(json.loads(fake_host.store[_scoped(_QUEUE_KEY)].decode())) == 1


def test_mod_can_remove_someone_elses_request(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "add", arg="my song"), {}, http_client=None))
    result = _run(
        dispatch(
            _sample_envelope(
                "twitch", "remove", arg="1", actor="viewer-2", is_mod=True
            ),
            {},
            http_client=None,
        )
    )
    assert result.detail == "remove"
    assert "removed request #1" in _reply_text(fake_host)
    assert json.loads(fake_host.store[_scoped(_QUEUE_KEY)].decode()) == []


def test_remove_nonexistent_id(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "remove", arg="99"), {}, http_client=None))
    assert result.detail == "remove"
    assert "no request #99 found" in _reply_text(fake_host)


def test_remove_non_integer_id(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "remove", arg="abc"), {}, http_client=None))
    assert result.detail == "remove"
    assert "isn't a valid request id" in _reply_text(fake_host)


def test_remove_with_no_arg_is_usage_reply(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "remove", arg=None), {}, http_client=None))
    assert result.detail == "remove"
    assert "Usage:" in _reply_text(fake_host)


# -- !music next / !music skip (advance) -----------------------------------------


def test_advance_denied_without_mod_signal(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "advance"), {}, http_client=None))
    assert result.detail == "advance:denied"
    assert "only moderators/broadcasters can do that" in _reply_text(fake_host)


def test_advance_denied_with_explicit_non_mod_signal(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "advance", is_mod=False, is_broadcaster=False),
            {},
            http_client=None,
        )
    )
    assert result.detail == "advance:denied"


def test_mod_can_advance_queue(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "add", arg="song a"), {}, http_client=None))
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="song b", actor="viewer-2"), {}, http_client=None
        )
    )
    result = _run(
        dispatch(_sample_envelope("twitch", "advance", is_mod=True), {}, http_client=None)
    )
    assert result.detail == "advance"
    text = _reply_text(fake_host)
    assert "now playing: song a" in text
    assert "up next: song b" in text
    remaining = json.loads(fake_host.store[_scoped(_QUEUE_KEY)].decode())
    assert len(remaining) == 1
    assert remaining[0]["text"] == "song b"


def test_broadcaster_can_advance_last_entry_to_empty(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "add", arg="only song"), {}, http_client=None))
    result = _run(
        dispatch(
            _sample_envelope("twitch", "advance", is_broadcaster=True), {}, http_client=None
        )
    )
    assert result.detail == "advance"
    assert "queue is now empty" in _reply_text(fake_host)
    assert json.loads(fake_host.store[_scoped(_QUEUE_KEY)].decode()) == []


def test_advance_on_empty_queue(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(_sample_envelope("twitch", "advance", is_mod=True), {}, http_client=None)
    )
    assert result.detail == "advance"
    assert _reply_text(fake_host) == "the queue is empty."


# -- !music set max-per-user <n> --------------------------------------------------


def test_config_set_denied_without_mod_signal(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "config_set", arg="max-per-user 5"), {}, http_client=None
        )
    )
    assert result.detail == "config_set:denied"


def test_mod_can_set_max_per_user(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope(
                "twitch", "config_set", arg="max-per-user 5", is_broadcaster=True
            ),
            {},
            http_client=None,
        )
    )
    assert result.detail == "config_set"
    assert "max-per-user set to 5" in _reply_text(fake_host)
    assert fake_host.store[_scoped(_MAX_PER_USER_CONFIG_KEY)] == b"5"


@pytest.mark.parametrize("value", [str(MIN_MAX_PER_USER - 1), str(MAX_MAX_PER_USER + 1)])
def test_set_max_per_user_out_of_bounds(value: str, fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope(
                "twitch", "config_set", arg=f"max-per-user {value}", is_mod=True
            ),
            {},
            http_client=None,
        )
    )
    assert result.detail == "config_set"
    assert "must be between" in _reply_text(fake_host)


def test_set_max_per_user_non_integer(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "config_set", arg="max-per-user abc", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert "isn't a whole number" in _reply_text(fake_host)


@pytest.mark.parametrize("arg", [None, "", "bogus-option 5", "max-per-user"])
def test_set_malformed_args_are_usage(arg: str | None, fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "config_set", arg=arg, is_mod=True), {}, http_client=None
        )
    )
    assert result.detail == "config_set"
    assert "Usage:" in _reply_text(fake_host)


def test_configured_max_per_user_is_applied_to_subsequent_adds(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "config_set", arg="max-per-user 1", is_mod=True),
            {},
            http_client=None,
        )
    )
    _run(dispatch(_sample_envelope("twitch", "add", arg="first"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "add", arg="second"), {}, http_client=None))
    assert "the limit is 1" in _reply_text(fake_host)
    assert result.detail == "add"


def test_corrupt_max_per_user_config_falls_back_to_default(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_MAX_PER_USER_CONFIG_KEY)] = b"not-a-number"
    for i in range(DEFAULT_MAX_PER_USER):
        _run(dispatch(_sample_envelope("twitch", "add", arg=f"song {i}"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "add", arg="overflow"), {}, http_client=None))
    assert f"the limit is {DEFAULT_MAX_PER_USER}" in _reply_text(fake_host)
    corrupt_logs = [
        m for _lvl, m, _f in fake_host.log_calls if m == "music.max_per_user_config_corrupt"
    ]
    assert corrupt_logs
    assert result.detail == "add"


# -- !music usage -----------------------------------------------------------------


def test_usage_command_replies_with_usage_text(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "usage"), {}, http_client=None))
    assert result.detail == "usage"
    assert "Usage:" in _reply_text(fake_host)


# -- dispatch() input validation ---------------------------------------------------


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "show", community=None)
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv_calls == []


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.music",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={"command": "show", "channel_id": None},
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_on_unrecognized_command(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "not-a-real-command")
    with pytest.raises(ValueError, match="unrecognized music command"):
        _run(dispatch(envelope, {}, http_client=None))


# -- community isolation -----------------------------------------------------------


def test_different_communities_have_independent_queues(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="comm-1 song", community="comm-1"),
            {},
            http_client=None,
        )
    )
    result = _run(
        dispatch(_sample_envelope("twitch", "show", community="comm-2"), {}, http_client=None)
    )
    assert result.detail == "show"
    assert _reply_text(fake_host) == "the queue is empty."


# -- kv-backend fail-loud -----------------------------------------------------------


def test_add_fails_loud_on_kv_get_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_get_raises=RuntimeError("backend down"))
    with pytest.raises(RuntimeError, match="music kv get failed"):
        _run(dispatch(_sample_envelope("twitch", "add", arg="song"), {}, http_client=None))
    assert "temporarily unavailable" in _reply_text(host)
    error_logs = [m for _lvl, m, _f in host.log_calls if m == "music.kv_error"]
    assert error_logs


def test_add_fails_loud_on_kv_set_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_set_raises=RuntimeError("backend down"))
    with pytest.raises(RuntimeError, match="music kv set failed"):
        _run(dispatch(_sample_envelope("twitch", "add", arg="song"), {}, http_client=None))
    assert "temporarily unavailable" in _reply_text(host)


def test_add_fails_loud_on_kv_increment_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_increment_raises=RuntimeError("backend down"))
    with pytest.raises(RuntimeError, match="music kv increment failed"):
        _run(dispatch(_sample_envelope("twitch", "add", arg="song"), {}, http_client=None))
    assert "temporarily unavailable" in _reply_text(host)


def test_remove_fails_loud_on_kv_error(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(dispatch(_sample_envelope("twitch", "add", arg="song"), {}, http_client=None))
    _install(monkeypatch, fake_host, kv_set_raises=RuntimeError("backend down"))
    with pytest.raises(RuntimeError, match="music kv set failed"):
        _run(dispatch(_sample_envelope("twitch", "remove", arg="1"), {}, http_client=None))


# -- corrupt-state fail-loud ---------------------------------------------------------


def test_corrupt_queue_json_fails_loud(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_QUEUE_KEY)] = b"not valid json"
    with pytest.raises(RuntimeError, match="music corrupt state"):
        _run(dispatch(_sample_envelope("twitch", "show"), {}, http_client=None))
    assert "corrupted" in _reply_text(fake_host)
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "music.state_corrupt"]
    assert corrupt_logs


def test_corrupt_queue_not_a_list_fails_loud(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_QUEUE_KEY)] = json.dumps({"not": "a list"}).encode()
    with pytest.raises(RuntimeError, match="music corrupt state"):
        _run(dispatch(_sample_envelope("twitch", "show"), {}, http_client=None))


def test_corrupt_queue_entry_not_an_object_fails_loud(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_QUEUE_KEY)] = json.dumps([1, 2, 3]).encode()
    with pytest.raises(RuntimeError, match="music corrupt state"):
        _run(dispatch(_sample_envelope("twitch", "show"), {}, http_client=None))


def test_corrupt_queue_entry_missing_field_fails_loud(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_QUEUE_KEY)] = json.dumps([{"id": 1}]).encode()
    with pytest.raises(RuntimeError, match="music corrupt state"):
        _run(dispatch(_sample_envelope("twitch", "show"), {}, http_client=None))


# -- QueueEntry / _caller_role_signal direct unit coverage ---------------------------


def test_queue_entry_is_a_frozen_slots_dataclass() -> None:
    entry = QueueEntry(id=1, requester_pseudonym="abc", text="song", ts=123)
    assert entry.id == 1
    with pytest.raises(AttributeError):
        entry.id = 2  # type: ignore[misc]


def test_caller_role_signal_none_when_absent() -> None:
    assert _caller_role_signal({}) is None


def test_caller_role_signal_true_when_mod() -> None:
    assert _caller_role_signal({"is_mod": True}) is True


def test_caller_role_signal_false_when_neither_flag_set() -> None:
    assert _caller_role_signal({"is_mod": False, "is_broadcaster": False}) is False


def test_pseudonym_is_stable_and_handles_none_actor() -> None:
    assert _pseudonym("viewer-1") == _expected_pseudonym("viewer-1")
    assert _pseudonym(None) == _expected_pseudonym(None)


def test_seq_key_increments_independently_per_community(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="song", community="comm-1"),
            {},
            http_client=None,
        )
    )
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="song", community="comm-2"),
            {},
            http_client=None,
        )
    )
    assert int(fake_host.store[_scoped(_SEQ_KEY, "comm-1")].decode()) == 1
    assert int(fake_host.store[_scoped(_SEQ_KEY, "comm-2")].decode()) == 1
