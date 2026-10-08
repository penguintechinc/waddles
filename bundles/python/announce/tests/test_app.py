"""Host-native tests for the `announce` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/pyping/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors. The `kv` import is faked with the SHARED,
charset-enforcing `waddle_sdk.testing.FakeKvHost` (gh-631) rather than a hand-rolled in-memory
dict, so a colon (or any other host-rejected byte) in this bundle's own kv key would fail here
immediately -- flags/relay/log are still hand-wired onto the same fake `wit_world.imports`
namespace since `waddle_sdk.testing` only covers `kv`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.kv import InvalidKvKeyError, validate_key
from waddle_sdk.testing import FakeKvHost

from app import (
    _EMPTY_LIST_MSG,
    _KEY_ALLOWED_CHARS,
    _PERMISSION_DENIED_MSG,
    _REGISTRY_KEY,
    _USAGE,
    MAX_KEY_LEN,
    MAX_MESSAGE_LEN,
    MAX_REGISTRY_SIZE,
    _normalize_key,
    _validate_key,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _scoped_registry_key(community: str = "comm-1") -> str:
    """The real `community_kv`-scoped kv key the bundle itself reads/writes under.

    `waddle_sdk.community_kv._scoped_key` prefixes every guest key with
    `c.<community_id>.` -- a test seeding/asserting on `fake_host.store`
    directly (rather than only through `dispatch()`) must use this same
    scoped form, not the bundle's own bare `_REGISTRY_KEY` constant, or it
    silently reads/writes a different kv entry than the code under test.
    """
    return f"c.{community}.{_REGISTRY_KEY}"


def _sample_event(
    text: str,
    *,
    channel_id: str | None = "12345",
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
        actor="viewer-1",
        payload=payload,
        occurred_at="2026-10-05T00:00:00.000Z",
    )


class _FakeHost:
    """Fake WIT host: flags/relay/log hand-wired, `kv` delegated to the shared `FakeKvHost`."""

    def __init__(self, *, flag_enabled: bool = True) -> None:
        self.kv = FakeKvHost()
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[str, str, str]] = []
        self.flag_enabled = flag_enabled

    @property
    def store(self) -> dict[str, bytes]:
        return self.kv.store


def _install(
    monkeypatch: pytest.MonkeyPatch,
    host: _FakeHost,
    *,
    kv_get_raises: Exception | None = None,
    kv_set_raises: Exception | None = None,
) -> None:
    def kv_get(key: str) -> bytes | None:
        if kv_get_raises is not None:
            validate_key(key)
            raise kv_get_raises
        return host.kv.get(key)

    def kv_set(key: str, value: bytes, ttl_seconds: int) -> None:
        if kv_set_raises is not None:
            validate_key(key)
            raise kv_set_raises
        host.kv.set(key, value, ttl_seconds)

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: host.flag_enabled)
    kv_mod = types.SimpleNamespace(
        get=kv_get, set=kv_set, delete=host.kv.delete, increment=host.kv.increment
    )
    # `waddle_sdk.relay.push` already serializes `message` to canonical JSON text
    # before calling this import -- `msg` here is already a JSON string, not a dict.
    relay_mod = types.SimpleNamespace(
        push=lambda provider, msg: host.relay_calls.append((provider, msg))
    )
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: host.log_calls.append((lvl, msg, fields_json)),
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, kv=kv_mod, relay=relay_mod, log=log_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    host = _FakeHost()
    _install(monkeypatch, host)
    return host


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
        app_id="waddles.core.example.announce",
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


# -- transform() parsing -------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["!announce welcome", "!ANNOUNCE welcome", "  !announce welcome  "]
)
def test_matches_case_insensitively_and_with_whitespace(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "get"
    assert result.payload["arg"] == "welcome"


@pytest.mark.parametrize(
    ("text", "expected_command", "expected_arg"),
    [
        ("!announce", "usage", None),
        ("!announce welcome", "get", "welcome"),
        ("!announce welcome back", "usage", None),
        ("!announce set welcome hi there", "set", "welcome hi there"),
        ("!announce SET welcome hi", "set", "welcome hi"),
        ("!announce set", "usage", None),
        ("!announce remove welcome", "remove", "welcome"),
        ("!announce remove", "usage", None),
        ("!announce list", "list", None),
        ("!announce list extra", "usage", None),
        ("!announce add", "usage", None),
        ("!announce sub", "usage", None),
        ("!announce enable foo", "usage", None),
        ("!announce disable foo", "usage", None),
        ("!announce delete foo", "usage", None),
        ("!announce reset", "usage", None),
    ],
)
def test_transform_parses_grammar(
    text: str, expected_command: str, expected_arg: str | None, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == expected_command
    assert result.payload.get("arg") == expected_arg


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(
        transform(_sample_event("!announce set hi there", is_mod=True, is_broadcaster=False))
    )
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!announce set hi there")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


@pytest.mark.parametrize("text", ["!announced", "announce", "!announcement", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host: _FakeHost) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_chat_payload_is_ignored_rather_than_erroring(fake_host: _FakeHost) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost(flag_enabled=False)
    _install(monkeypatch, host)
    assert _run(transform(_sample_event("!announce welcome"))) is None


# -- dispatch(): get -----------------------------------------------------------


def test_dispatch_get_returns_saved_message(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped_registry_key()] = json.dumps({"welcome": "hello there!"}).encode()
    result = _run(dispatch(_sample_envelope("twitch", "get", arg="welcome"), {}, http_client=None))

    assert result.detail == "get"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json) == {"channel": "12345", "text": "hello there!"}


def test_dispatch_get_missing_key_is_friendly(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "get", arg="nope"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == "no saved announcement for 'nope'"
    assert result.detail == "get"


def test_dispatch_get_is_open_to_any_caller(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped_registry_key()] = json.dumps({"welcome": "hi"}).encode()
    envelope = _sample_envelope("discord", "get", arg="welcome")  # no badge fields at all
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "get"


def test_dispatch_get_normalizes_key_case(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped_registry_key()] = json.dumps({"welcome": "hi"}).encode()
    result = _run(dispatch(_sample_envelope("twitch", "get", arg="WELCOME"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == "hi"
    assert result.detail == "get"


# -- dispatch(): list -----------------------------------------------------------


def test_dispatch_list_empty(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _EMPTY_LIST_MSG
    assert result.detail == "list"


def test_dispatch_list_renders_sorted_keys(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped_registry_key()] = json.dumps({"zeta": "z", "alpha": "a"}).encode()
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == "Announcements: alpha, zeta"
    assert result.detail == "list"


def test_dispatch_list_is_open_to_any_caller(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("discord", "list")  # no badge fields at all
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "list"


# -- dispatch(): set -- permission gate + persistence --------------------------


@pytest.mark.parametrize(("is_mod", "is_broadcaster"), [(True, None), (None, True), (True, True)])
def test_set_allowed_for_mod_or_broadcaster(
    is_mod: bool | None, is_broadcaster: bool | None, fake_host: _FakeHost
) -> None:
    envelope = _sample_envelope(
        "twitch", "set", arg="welcome hi there", is_mod=is_mod, is_broadcaster=is_broadcaster
    )
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "set"
    registry = json.loads(fake_host.store[_scoped_registry_key()].decode())
    assert registry == {"welcome": "hi there"}
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == "saved announcement 'welcome'"


def test_set_rejected_for_non_mod(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "set", arg="welcome hi", is_mod=False, is_broadcaster=False
    )
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "set:denied"
    assert _scoped_registry_key() not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _PERMISSION_DENIED_MSG


def test_set_rejected_when_role_signal_entirely_absent(fake_host: _FakeHost) -> None:
    """Discord today has no is_mod/is_broadcaster at all -- must deny, never implicitly allow."""
    envelope = _sample_envelope("discord", "set", arg="welcome hi")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "set:denied"
    assert _scoped_registry_key() not in fake_host.store
    denial_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "announce.permission_denied"]
    assert denial_logs


def test_set_overwrites_existing_key(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped_registry_key()] = json.dumps({"welcome": "old"}).encode()
    envelope = _sample_envelope("twitch", "set", arg="welcome new message", is_mod=True)
    _run(dispatch(envelope, {}, http_client=None))
    saved = json.loads(fake_host.store[_scoped_registry_key()].decode())
    assert saved == {"welcome": "new message"}


def test_set_with_no_argument_replies_usage(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "set", arg="", is_mod=True)
    result = _run(dispatch(envelope, {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _USAGE
    assert result.detail == "set"


def test_set_requires_a_message(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "set", arg="welcome", is_mod=True)
    result = _run(dispatch(envelope, {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert "message is required" in json.loads(message_json)["text"]
    assert _scoped_registry_key() not in fake_host.store
    assert result.detail == "set"


def test_set_rejects_message_too_long(fake_host: _FakeHost) -> None:
    long_message = "x" * (MAX_MESSAGE_LEN + 1)
    envelope = _sample_envelope("twitch", "set", arg=f"welcome {long_message}", is_mod=True)
    _run(dispatch(envelope, {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert f"{MAX_MESSAGE_LEN} characters or fewer" in json.loads(message_json)["text"]
    assert _scoped_registry_key() not in fake_host.store


def test_set_rejects_key_too_long(fake_host: _FakeHost) -> None:
    long_key = "k" * (MAX_KEY_LEN + 1)
    envelope = _sample_envelope("twitch", "set", arg=f"{long_key} hi", is_mod=True)
    _run(dispatch(envelope, {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert f"{MAX_KEY_LEN} characters or fewer" in json.loads(message_json)["text"]


def test_set_rejects_key_with_bad_charset(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "set", arg="wel$come hi there", is_mod=True)
    _run(dispatch(envelope, {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert "letters, digits" in json.loads(message_json)["text"]


def test_set_rejects_when_registry_full_for_a_new_key(fake_host: _FakeHost) -> None:
    existing = {f"k{i}": "msg" for i in range(MAX_REGISTRY_SIZE)}
    fake_host.store[_scoped_registry_key()] = json.dumps(existing).encode()
    envelope = _sample_envelope("twitch", "set", arg="newkey hi there", is_mod=True)
    _run(dispatch(envelope, {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert "full" in json.loads(message_json)["text"]
    assert "newkey" not in json.loads(fake_host.store[_scoped_registry_key()].decode())


def test_set_allows_overwrite_even_when_registry_full(fake_host: _FakeHost) -> None:
    existing = {f"k{i}": "msg" for i in range(MAX_REGISTRY_SIZE)}
    fake_host.store[_scoped_registry_key()] = json.dumps(existing).encode()
    envelope = _sample_envelope("twitch", "set", arg="k0 updated message", is_mod=True)
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "set"
    assert json.loads(fake_host.store[_scoped_registry_key()].decode())["k0"] == "updated message"


# -- dispatch(): remove ---------------------------------------------------------


def test_remove_deletes_existing_key(fake_host: _FakeHost) -> None:
    seeded = {"welcome": "hi", "rules": "be nice"}
    fake_host.store[_scoped_registry_key()] = json.dumps(seeded).encode()
    envelope = _sample_envelope("twitch", "remove", arg="welcome", is_mod=True)
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "remove"
    assert json.loads(fake_host.store[_scoped_registry_key()].decode()) == {"rules": "be nice"}
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == "removed announcement 'welcome'"


def test_remove_missing_key_is_friendly(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "remove", arg="nope", is_mod=True)
    result = _run(dispatch(envelope, {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == "no saved announcement for 'nope'"
    assert result.detail == "remove"


def test_remove_rejected_for_non_mod(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped_registry_key()] = json.dumps({"welcome": "hi"}).encode()
    envelope = _sample_envelope("twitch", "remove", arg="welcome", is_mod=False)
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "remove:denied"
    assert json.loads(fake_host.store[_scoped_registry_key()].decode()) == {"welcome": "hi"}


def test_remove_with_no_argument_replies_usage(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "remove", arg="", is_mod=True)
    _run(dispatch(envelope, {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _USAGE


def test_remove_rejects_multi_word_argument(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "remove", arg="welcome extra", is_mod=True)
    _run(dispatch(envelope, {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert "single word" in json.loads(message_json)["text"]


# -- usage ----------------------------------------------------------------------


def test_usage_command_replies_usage_text(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "usage"), {}, http_client=None))
    assert result.detail == "usage"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _USAGE


# -- community scoping -----------------------------------------------------------


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "list", community=None)
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv.calls == []


def test_different_communities_never_share_announcements(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "set", community="comm-1", arg="welcome hi", is_mod=True),
            {},
            http_client=None,
        )
    )
    envelope = _sample_envelope("twitch", "get", community="comm-2", arg="welcome")
    result = _run(dispatch(envelope, {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == "no saved announcement for 'welcome'"
    assert result.detail == "get"


# -- misc / defensive -------------------------------------------------------------


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.announce",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={"command": "list", "channel_id": None},
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv.calls == []


def test_dispatch_raises_on_an_unrecognized_command(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "not-a-real-command")
    with pytest.raises(ValueError, match="unrecognized announce command"):
        _run(dispatch(envelope, {}, http_client=None))


# -- kv failure: fail loud, never silent -----------------------------------------


class _ErrorBackend:
    """Stand-in for the generated WIT `Error_Backend` variant case class."""


class _KvError(Exception):
    """Stand-in for the generated `Err` wrapper -- `.value` holds the error union member."""

    def __init__(self) -> None:
        self.value = _ErrorBackend()


def test_dispatch_list_replies_and_raises_on_kv_get_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_get_raises=_KvError())

    with pytest.raises(RuntimeError, match="kv get failed"):
        _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in json.loads(message_json)["text"]
    error_logs = [(lvl, m) for lvl, m, _f in host.log_calls if m == "announce.kv_error"]
    assert error_logs


def test_dispatch_set_replies_and_raises_on_kv_set_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_set_raises=_KvError())

    envelope = _sample_envelope("twitch", "set", arg="welcome hi", is_mod=True)
    with pytest.raises(RuntimeError, match="kv set failed"):
        _run(dispatch(envelope, {}, http_client=None))

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in json.loads(message_json)["text"]


# -- corrupt stored state: fail loud, never silent --------------------------------


def test_corrupt_registry_json_fails_loud(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped_registry_key()] = b"\xff\xfe not valid utf-8"
    with pytest.raises(RuntimeError, match="corrupt registry"):
        _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert "corrupted" in json.loads(message_json)["text"]
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "announce.state_corrupt"]
    assert corrupt_logs


def test_corrupt_registry_wrong_shape_fails_loud(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped_registry_key()] = json.dumps(["not", "a", "dict"]).encode()
    with pytest.raises(RuntimeError, match="corrupt registry"):
        _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))


def test_corrupt_registry_non_string_values_fails_loud(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped_registry_key()] = json.dumps({"welcome": 123}).encode()
    with pytest.raises(RuntimeError, match="corrupt registry"):
        _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))


# -- kv key charset (gh-631) -------------------------------------------------------


def test_registry_key_is_colon_free() -> None:
    assert ":" not in _REGISTRY_KEY


def test_registry_key_satisfies_host_guest_key_charset() -> None:
    # Raises if any byte falls outside the real host's allowed charset.
    validate_key(_REGISTRY_KEY)


def test_colon_key_is_rejected_before_any_host_call() -> None:
    """A colon-containing key fails fast at the SDK boundary, exactly like the real host."""
    from waddle_sdk import kv

    with pytest.raises(InvalidKvKeyError, match="characters outside"):
        _run(kv.get("announce:registry"))


# -- pure helpers -------------------------------------------------------------------


def test_normalize_key_lowercases_and_strips() -> None:
    assert _normalize_key("  WELCOME  ") == "welcome"


@pytest.mark.parametrize(
    ("key", "expected_error_fragment"),
    [
        ("", "required"),
        ("k" * (MAX_KEY_LEN + 1), "characters or fewer"),
        ("bad key!", "letters, digits"),
    ],
)
def test_validate_key_rejects_bad_input(key: str, expected_error_fragment: str) -> None:
    error = _validate_key(key)
    assert error is not None
    assert expected_error_fragment in error


@pytest.mark.parametrize("key", ["welcome", "WELCOME-1_ok", "a" * MAX_KEY_LEN])
def test_validate_key_accepts_good_input(key: str) -> None:
    assert _validate_key(key) is None


def test_key_allowed_chars_excludes_space_and_colon() -> None:
    assert " " not in _KEY_ALLOWED_CHARS
    assert ":" not in _KEY_ALLOWED_CHARS
