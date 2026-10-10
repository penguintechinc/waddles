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
from waddle_sdk.kv import validate_key

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


# -- PII-free logs -- regression: gh-674 (bundle-logs-must-be-pii-free) -----------------

_SENTINEL = "SENTINELpii9f3a"

#: Strict per-message allowlist: a log line may carry ONLY these fields. A new field (e.g. a
#: raw `text=`/`raw=`/`actor=`) fails here instead of silently shipping user input to telemetry
#: (#674 was exactly `raw=parts[1]` on three of these lines).
_ALLOWED_LOG_FIELDS: dict[str, frozenset[str]] = {
    "music.transform matched": frozenset({"command"}),
    "music.invalid_command": frozenset({"error_type"}),
    "music.invalid_request_id": frozenset({"error_type"}),
    "music.invalid_max_per_user": frozenset({"error_type"}),
    "music.request_added": frozenset({"community"}),
    "music.request_removed": frozenset({"community"}),
    "music.advanced": frozenset({"community"}),
    "music.permission_denied": frozenset({"command", "role_signal"}),
    "music.max_per_user_config_corrupt": frozenset({"community"}),
    "music.kv_error": frozenset({"op", "error"}),
    "music.state_corrupt": frozenset({"reason"}),
    "music.dispatch relayed": frozenset({"platform", "command"}),
    "music.missing_community": frozenset({"command"}),
}


def _assert_logs_pii_free(host: _FakeHost, *, minimum_lines: int) -> set[str]:
    """Every captured log line is allow-listed field-by-field and free of the sentinel.

    Asserts a non-empty denominator first -- a check that examined zero log lines proves nothing.
    Returns the distinct messages seen so callers can prove each branch was exercised.
    """
    assert len(host.log_calls) >= minimum_lines
    for _lvl, message, fields_json in host.log_calls:
        assert _SENTINEL not in message
        assert _SENTINEL not in fields_json
        assert message in _ALLOWED_LOG_FIELDS, f"unexpected log message {message!r}"
        assert set(json.loads(fields_json)) <= _ALLOWED_LOG_FIELDS[message]
    return {message for _lvl, message, _f in host.log_calls}


def _go(
    command: str,
    *,
    arg: str | None = None,
    actor: str | None = "viewer-1",
    role: bool | None = None,
    community: str | None = "comm-1",
) -> Any:
    envelope = _sample_envelope(
        "twitch", command, actor=actor, arg=arg, is_mod=role, community=community
    )
    return _run(dispatch(envelope, {}, http_client=None))


def test_transform_logs_never_carry_user_input(fake_host: _FakeHost) -> None:
    # regression: gh-674
    for text in (
        f"!sr {_SENTINEL}",
        f"!songrequest {_SENTINEL} {_SENTINEL}",
        f"!music remove {_SENTINEL}",
        f"!music set {_SENTINEL}",
        f"!music set max-per-user {_SENTINEL}",
        f"!music next {_SENTINEL}",
        f"!music bogus {_SENTINEL}",
        f"!music enable {_SENTINEL}",
        f"!queue list {_SENTINEL}",
        "!music",
        "!sr",
    ):
        event = _sample_event(text, is_mod=True, actor=_SENTINEL)
        assert _run(transform(event)) is not None
    seen = _assert_logs_pii_free(fake_host, minimum_lines=11)
    assert seen <= {"music.transform matched", "music.invalid_command"}
    assert "music.transform matched" in seen


def test_dispatch_logs_never_carry_user_input_on_any_command(fake_host: _FakeHost) -> None:
    # regression: gh-674 -- sentinel as actor AND as every free-text argument. The sentinel also
    # lives in the stored queue TEXT by design (it is the request), so only logs are checked.
    _go("add", arg=f"song {_SENTINEL}", actor=_SENTINEL)
    _go("add", arg=f"another {_SENTINEL}", actor=_SENTINEL)
    _go("show", actor=_SENTINEL)
    _go("remove", arg=_SENTINEL, actor=_SENTINEL)  # not an id -> invalid_request_id
    _go("remove", arg="999", actor=_SENTINEL)  # unknown id
    _go("remove", arg="1", actor=_SENTINEL)  # own request
    _go("config_set", arg=f"max-per-user {_SENTINEL}", role=True)  # invalid_max_per_user
    _go("config_set", arg="max-per-user 5", role=True)
    _go("config_set", arg=f"bogus {_SENTINEL}", role=True)  # usage text only
    _go("advance", role=True)
    _go("config_set", arg="max-per-user 5", role=False)  # denied
    _go("advance")  # denied, no role signal at all
    _go("usage", actor=_SENTINEL)
    fake_host.store[_scoped(_MAX_PER_USER_CONFIG_KEY)] = _SENTINEL.encode()
    _go("add", arg="after corrupt config", actor=_SENTINEL)  # max_per_user_config_corrupt

    seen = _assert_logs_pii_free(fake_host, minimum_lines=12)
    assert seen == {
        "music.dispatch relayed",
        "music.request_added",
        "music.request_removed",
        "music.advanced",
        "music.permission_denied",
        "music.invalid_request_id",
        "music.invalid_max_per_user",
        "music.max_per_user_config_corrupt",
    }
    assert all(_SENTINEL not in call[1] for call in fake_host.kv_calls)
    queue = json.loads(fake_host.store[_scoped(_QUEUE_KEY)].decode())
    assert all(_SENTINEL not in entry["requester_pseudonym"] for entry in queue)


def test_failure_paths_never_log_user_input(monkeypatch: pytest.MonkeyPatch) -> None:
    # regression: gh-674 -- a kv failure whose own exception text echoes user-ish data, corrupt
    # queue JSON containing user-ish bytes, and the missing-community guard.
    host = _FakeHost()
    _install(monkeypatch, host, kv_set_raises=RuntimeError(f"backend detail {_SENTINEL}"))
    with pytest.raises(RuntimeError) as excinfo:
        _go("add", arg=f"song {_SENTINEL}", actor=_SENTINEL)
    assert _SENTINEL not in str(excinfo.value)

    _install(monkeypatch, host)
    host.store[_scoped(_QUEUE_KEY)] = ("{" + _SENTINEL).encode()
    with pytest.raises(RuntimeError) as corrupt:
        _go("show", actor=_SENTINEL)
    assert _SENTINEL not in str(corrupt.value)
    with pytest.raises(ValueError):
        _go("show", actor=_SENTINEL, community=None)

    seen = _assert_logs_pii_free(host, minimum_lines=3)
    assert seen == {"music.kv_error", "music.state_corrupt", "music.missing_community"}
    (kv_error,) = [json.loads(f) for _lvl, m, f in host.log_calls if m == "music.kv_error"]
    assert kv_error == {"op": "set", "error": "RuntimeError"}


# -- mod gate: config/advance fail closed, before any kv access -----------------------

_NO_MOD_ROLE = [
    pytest.param({}, id="no-signal-at-all"),
    pytest.param({"is_mod": False}, id="mod-false"),
    pytest.param({"is_broadcaster": False}, id="broadcaster-false"),
    pytest.param({"is_mod": False, "is_broadcaster": False}, id="both-false"),
]


@pytest.mark.parametrize(("command", "arg"), [("config_set", "max-per-user 5"), ("advance", None)])
@pytest.mark.parametrize("role", _NO_MOD_ROLE)
def test_mod_only_commands_are_denied_without_any_kv_access(
    command: str, arg: str | None, role: dict[str, bool], fake_host: _FakeHost
) -> None:
    result = _run(
        dispatch(_sample_envelope("twitch", command, arg=arg, **role), {}, http_client=None)
    )
    assert result.detail == f"{command}:denied"
    assert _reply_text(fake_host) == "only moderators/broadcasters can do that"
    assert fake_host.kv_calls == []


@pytest.mark.parametrize(("command", "arg"), [("config_set", "max-per-user 5"), ("advance", None)])
@pytest.mark.parametrize(
    "role",
    [{"is_mod": True}, {"is_broadcaster": True}, {"is_mod": True, "is_broadcaster": True}],
)
def test_either_badge_alone_opens_the_mod_gate(
    command: str, arg: str | None, role: dict[str, bool], fake_host: _FakeHost
) -> None:
    result = _run(
        dispatch(_sample_envelope("twitch", command, arg=arg, **role), {}, http_client=None)
    )
    assert result.detail == command


def test_present_but_null_badge_fields_are_denied(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "advance")
    envelope.event.payload["is_mod"] = None
    envelope.event.payload["is_broadcaster"] = None
    assert _run(dispatch(envelope, {}, http_client=None)).detail == "advance:denied"
    assert fake_host.kv_calls == []


def test_a_denied_advance_leaves_the_queue_untouched(fake_host: _FakeHost) -> None:
    _go("add", arg="keep me")
    before = fake_host.store[_scoped(_QUEUE_KEY)]
    _go("advance", role=False)
    assert fake_host.store[_scoped(_QUEUE_KEY)] == before


def test_removal_by_a_non_owner_needs_the_mod_signal_and_either_badge_works(
    fake_host: _FakeHost,
) -> None:
    _go("add", arg="a song", actor="owner")
    _go("remove", arg="1", actor="stranger", role=False)
    assert _reply_text(fake_host) == "you can only remove your own requests"
    envelope = _sample_envelope("twitch", "remove", arg="1", actor="stranger", is_broadcaster=True)
    assert _run(dispatch(envelope, {}, http_client=None)).detail == "remove"
    assert _reply_text(fake_host) == "removed request #1"


def test_read_and_add_commands_never_need_a_role(fake_host: _FakeHost) -> None:
    for command, arg in (("add", "song"), ("show", None)):
        assert _go(command, arg=arg, role=False).detail == command


# -- corrupt store: ERROR-logged and fail loud on every queue-reading command ---------------

_CORRUPT_QUEUES = [
    pytest.param(b"\xff\xfe", id="not-utf8"),
    pytest.param(b"{", id="truncated-json"),
    pytest.param(b'{"a": 1}', id="json-object"),
    pytest.param(b"null", id="json-null"),
    pytest.param(b'"str"', id="json-string"),
    pytest.param(b"[1]", id="entry-not-an-object"),
    pytest.param(b'[{"id": 1}]', id="entry-missing-fields"),
    pytest.param(
        b'[{"id": "x", "requester_pseudonym": "p", "text": "t", "ts": 1}]', id="id-not-int"
    ),
    pytest.param(
        b'[{"id": 1, "requester_pseudonym": "p", "text": "t", "ts": "x"}]', id="ts-not-int"
    ),
    pytest.param(b'[{"id": 1, "requester_pseudonym": "p", "text": "t", "ts": null}]', id="ts-null"),
]


@pytest.mark.parametrize("payload", _CORRUPT_QUEUES)
@pytest.mark.parametrize(
    ("command", "arg", "role"),
    [("show", None, None), ("add", "song", None), ("remove", "1", None), ("advance", None, True)],
)
def test_every_corrupt_queue_shape_fails_loud_and_is_never_overwritten(
    payload: bytes, command: str, arg: str | None, role: bool | None, fake_host: _FakeHost
) -> None:
    fake_host.store[_scoped(_QUEUE_KEY)] = payload
    with pytest.raises(RuntimeError, match="music corrupt state"):
        _go(command, arg=arg, role=role)
    assert len(fake_host.relay_calls) == 1
    assert "corrupted" in _reply_text(fake_host)
    corrupt = [
        (lvl, set(json.loads(f))) for lvl, m, f in fake_host.log_calls if m == "music.state_corrupt"
    ]
    assert corrupt == [(0, {"reason"})]  # Level.ERROR
    assert fake_host.store[_scoped(_QUEUE_KEY)] == payload


def test_corrupt_max_per_user_config_is_error_logged_and_uses_the_default(
    fake_host: _FakeHost,
) -> None:
    fake_host.store[_scoped(_MAX_PER_USER_CONFIG_KEY)] = b"\xff"
    for i in range(DEFAULT_MAX_PER_USER):
        _go("add", arg=f"song {i}")
    _go("add", arg="too many")
    assert f"the limit is {DEFAULT_MAX_PER_USER}" in _reply_text(fake_host)
    corrupt = [
        (lvl, json.loads(f))
        for lvl, m, f in fake_host.log_calls
        if m == "music.max_per_user_config_corrupt"
    ]
    assert corrupt and all(entry == (0, {"community": "comm-1"}) for entry in corrupt)


def test_corrupt_sequence_counter_fails_loud(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_SEQ_KEY)] = b"not-a-number"
    with pytest.raises(RuntimeError, match="music kv increment failed"):
        _go("add", arg="song")
    assert "temporarily unavailable" in _reply_text(fake_host)
    assert _scoped(_QUEUE_KEY) not in fake_host.store


# -- no silent fallback: failed writes leave state untouched ---------------------------------


def _fail_kv_for(op: str) -> None:
    """Make `wit_world.imports.kv.<op>` raise a WIT-shaped error."""

    class _ErrorBackend:
        """Stand-in for the generated WIT `Error_Backend` variant case class."""

    class _KvError(Exception):
        def __init__(self) -> None:
            self.value = _ErrorBackend()

    def _raise(*_args: Any) -> Any:
        raise _KvError()

    setattr(sys.modules["wit_world"].imports.kv, op, _raise)


@pytest.mark.parametrize(
    ("command", "arg", "role"),
    [("remove", "1", True), ("advance", None, True)],
)
def test_failed_queue_save_leaves_the_queue_intact(
    command: str, arg: str | None, role: bool, fake_host: _FakeHost
) -> None:
    _go("add", arg="keep me")
    before = fake_host.store[_scoped(_QUEUE_KEY)]
    _fail_kv_for("set")
    with pytest.raises(RuntimeError, match="music kv set failed: _ErrorBackend"):
        _go(command, arg=arg, role=role)
    assert fake_host.store[_scoped(_QUEUE_KEY)] == before
    assert "temporarily unavailable" in _reply_text(fake_host)


def test_failed_config_write_leaves_the_previous_limit(fake_host: _FakeHost) -> None:
    _go("config_set", arg="max-per-user 7", role=True)
    _fail_kv_for("set")
    with pytest.raises(RuntimeError, match="music kv set failed"):
        _go("config_set", arg="max-per-user 2", role=True)
    assert fake_host.store[_scoped(_MAX_PER_USER_CONFIG_KEY)] == b"7"


@pytest.mark.parametrize(
    ("command", "arg", "role"),
    [("show", None, None), ("add", "song", None), ("remove", "1", None), ("advance", None, True)],
)
def test_every_queue_reading_command_fails_loud_on_a_kv_get_error(
    command: str, arg: str | None, role: bool | None, fake_host: _FakeHost
) -> None:
    _fail_kv_for("get")
    with pytest.raises(RuntimeError, match="music kv get failed: _ErrorBackend"):
        _go(command, arg=arg, role=role)
    assert len(fake_host.relay_calls) == 1
    errors = [(lvl, json.loads(f)) for lvl, m, f in fake_host.log_calls if m == "music.kv_error"]
    assert errors == [(0, {"op": "get", "error": "_ErrorBackend"})]


def test_relay_failure_propagates_and_is_not_logged_as_relayed(fake_host: _FakeHost) -> None:
    def _boom(provider: str, msg: str) -> None:
        raise RuntimeError("relay down")

    sys.modules["wit_world"].imports.relay = types.SimpleNamespace(push=_boom)
    with pytest.raises(RuntimeError, match="relay down"):
        _go("show")
    assert not any(m == "music.dispatch relayed" for _lvl, m, _f in fake_host.log_calls)


def test_missing_community_logs_error_and_touches_no_state(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="community"):
        _go("add", arg="song", community=None)
    assert fake_host.kv_calls == []
    assert fake_host.relay_calls == []
    errors = [
        (lvl, json.loads(f)) for lvl, m, f in fake_host.log_calls if m == "music.missing_community"
    ]
    assert errors == [(0, {"command": "add"})]


# -- queue semantics --------------------------------------------------------------------------


def test_request_ids_are_never_reused_after_a_removal(fake_host: _FakeHost) -> None:
    _go("add", arg="one", actor="a")
    _go("add", arg="two", actor="b")
    _go("remove", arg="1", actor="a")
    _go("add", arg="three", actor="c")
    queue = json.loads(fake_host.store[_scoped(_QUEUE_KEY)].decode())
    assert [(entry["id"], entry["text"]) for entry in queue] == [(2, "two"), (3, "three")]


def test_request_text_boundary_is_inclusive_at_the_cap(fake_host: _FakeHost) -> None:
    _go("add", arg="x" * MAX_REQUEST_TEXT_LEN)
    queue = json.loads(fake_host.store[_scoped(_QUEUE_KEY)].decode())
    assert len(queue[0]["text"]) == MAX_REQUEST_TEXT_LEN


def test_show_with_exactly_the_limit_has_no_remainder_note(fake_host: _FakeHost) -> None:
    for i in range(SHOW_LIMIT):
        _go("add", arg=f"song {i}", actor=f"user{i}")
    _go("show")
    assert "more)" not in _reply_text(fake_host)
    _go("add", arg="one more", actor="extra")
    _go("show")
    assert _reply_text(fake_host).endswith("(+1 more)")


@pytest.mark.parametrize("value", [MIN_MAX_PER_USER, MAX_MAX_PER_USER])
def test_max_per_user_bounds_are_inclusive(value: int, fake_host: _FakeHost) -> None:
    _go("config_set", arg=f"max-per-user {value}", role=True)
    assert fake_host.store[_scoped(_MAX_PER_USER_CONFIG_KEY)] == str(value).encode()


def test_advance_walks_the_whole_queue_in_order(fake_host: _FakeHost) -> None:
    for text in ("first", "second", "third"):
        _go("add", arg=text, actor=text)
    for expected in ("first", "second", "third"):
        _go("advance", role=True)
        assert expected in _reply_text(fake_host)
    _go("advance", role=True)
    assert _reply_text(fake_host) == "the queue is empty."


# -- transform(): payload shape + flag gate ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "keys"),
    [
        ("!sr some song", {"command", "channel_id", "arg"}),
        ("!sr", {"command", "channel_id"}),
        ("!music", {"command", "channel_id"}),
        ("!music remove 3", {"command", "channel_id", "arg"}),
        ("!music set max-per-user 4", {"command", "channel_id", "arg"}),
        ("!music skip", {"command", "channel_id"}),
        ("!QUEUE List", {"command", "channel_id"}),
    ],
)
def test_transform_forwards_only_the_fields_dispatch_needs(
    text: str, keys: set[str], fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert set(result.payload) == keys


def test_flag_is_checked_by_key_and_defaults_off(fake_host: _FakeHost) -> None:
    seen: list[tuple[str, bool]] = []

    def _enabled(key: str, default_value: bool) -> bool:
        seen.append((key, default_value))
        return False

    sys.modules["wit_world"].imports.flags = types.SimpleNamespace(enabled=_enabled)
    assert _run(transform(_sample_event("!sr a song"))) is None
    assert seen == [("waddles.command-music", False)]
    assert fake_host.kv_calls == []


# -- kv key charset -- regression: gh-631 ---------------------------------------------------------


def test_every_kv_key_the_bundle_touches_satisfies_the_host_charset(fake_host: _FakeHost) -> None:
    # regression: gh-631 -- the host rejects any guest key outside ASCII alnum + `_`/`-`/`.`
    # (notably `:`); music originally built `music:queue`-style keys. This suite's own kv fake is
    # permissive, so check the keys directly.
    _go("add", arg="song")
    _go("config_set", arg="max-per-user 4", role=True)
    _go("show")
    _go("remove", arg="1")
    keys = [call[1] for call in fake_host.kv_calls]
    assert keys
    for key in keys:
        validate_key(key)
        assert ":" not in key
    for constant in (_QUEUE_KEY, _SEQ_KEY, _MAX_PER_USER_CONFIG_KEY):
        validate_key(constant)
