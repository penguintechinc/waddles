"""Host-native tests for the `dadjoke` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/joke/tests/test_app.py`'s own
docstring for the fake-`wit_world` approach this mirrors. Uses the shared,
charset-enforcing `waddle_sdk.testing.install_fake_kv_host` fake for the
`kv` portion of the fake host (gh-631 -- see that module's own docstring),
extended here with fake `flags`/`relay`/`log` submodules for the rest of
`wit_world.imports` this bundle also touches.
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import types
from typing import Any, cast

import pytest
from waddle_sdk.command import CommandSpec, ParsedCommand, parse_command
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

from app import (
    _BUILTIN_DADJOKES,
    _CUSTOM_NEXT_ID_KEY,
    _CUSTOM_REGISTRY_KEY,
    _LAST_JOKE_KEY,
    _NO_CUSTOM_JOKES_MSG,
    MAX_JOKE_LEN,
    _caller_role_signal,
    _resolve_command,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _scoped(key: str, community: str = "comm-1") -> str:
    """`fake_host.kv.store` is keyed by `community_kv`'s own `c.<community>.<key>` prefix."""
    return cast(str, _scoped_key(community, key))


class _FakeHost:
    """Bundles the shared `FakeKvHost` with fake `flags`/`relay`/`log` submodules."""

    def __init__(self, kv: FakeKvHost) -> None:
        self.kv = kv
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag_state = {"enabled": True}


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    """Install the shared `FakeKvHost` (gh-631 charset-enforcing), plus flags/relay/log fakes."""
    kv_host = install_fake_kv_host(monkeypatch)
    host = _FakeHost(kv_host)

    wit_world = sys.modules["wit_world"]
    wit_world.imports.flags = types.SimpleNamespace(
        enabled=lambda key, default_value: host.flag_state["enabled"]
    )
    wit_world.imports.relay = types.SimpleNamespace(
        push=lambda provider, msg: host.relay_calls.append((provider, msg))
    )
    wit_world.imports.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: host.log_calls.append((lvl, msg, fields_json)),
    )
    return host


def _event(
    text: str,
    *,
    platform: str = "twitch",
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
    channel_id: str | None = "12345",
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": channel_id}
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor="viewer-1",
        payload=payload,
        occurred_at="2026-10-08T00:00:00.000Z",
    )


def _envelope(
    command: str,
    *,
    platform: str = "twitch",
    community: str | None = "comm-1",
    arg: str | None = None,
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
    channel_id: str | None = "12345",
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": channel_id}
    if arg is not None:
        payload["arg"] = arg
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.dadjoke",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload=payload,
            occurred_at="2026-10-08T00:00:00.000Z",
        ),
        ts="2026-10-08T00:00:00.000Z",
    )


# ---------------------------------------------------------------------------
# transform(): matching / flag / grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["!dadjoke", "!DADJOKE", "  !dadjoke  "])
def test_bare_dadjoke_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == "tell"


def test_dadjoke_list_matches(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!dadjoke list")))
    assert result is not None
    assert result.payload["command"] == "list"


def test_dadjoke_list_with_trailing_args_is_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!dadjoke list extra")))
    assert result is not None
    assert result.payload["command"] == "usage"


def test_dadjoke_add_forwards_arg(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!dadjoke add why did the chicken cross the road")))
    assert result is not None
    assert result.payload["command"] == "add"
    assert result.payload["arg"] == "why did the chicken cross the road"


def test_dadjoke_remove_forwards_arg(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!dadjoke remove 3")))
    assert result is not None
    assert result.payload["command"] == "remove"
    assert result.payload["arg"] == "3"


@pytest.mark.parametrize(
    "text",
    ["!dadjoke enable ai", "!dadjoke bogus", "!dadjoke set x", "!dadjoke reset", "!dadjoke disable foo"],
)
def test_unsupported_verbs_and_bad_grammar_reply_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!dadjokebook", "dadjoke", "!dadjokes", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host: _FakeHost) -> None:
    assert _run(transform(_event(text))) is None


def test_non_string_text_is_ignored(fake_host: _FakeHost) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": None, "channel_id": "1"},
        occurred_at="",
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(fake_host: _FakeHost) -> None:
    fake_host.flag_state["enabled"] = False
    assert _run(transform(_event("!dadjoke"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!dadjoke add hi", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!dadjoke")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# ---------------------------------------------------------------------------
# _resolve_command() direct unit coverage
# ---------------------------------------------------------------------------


def test_resolve_command_none_is_usage() -> None:
    assert _resolve_command(None) == "usage"


def test_resolve_command_bare_is_tell() -> None:
    spec = CommandSpec(name="dadjoke")
    parsed = parse_command("!dadjoke", spec)
    assert _resolve_command(parsed) == "tell"


def test_resolve_command_unimplemented_verb_is_usage() -> None:
    parsed = ParsedCommand(command="dadjoke", sub_module=None, option="set", args="x")
    assert _resolve_command(parsed) == "usage"


# ---------------------------------------------------------------------------
# bare !dadjoke -> tell
# ---------------------------------------------------------------------------


def test_tell_posts_a_builtin_joke_and_persists_last(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("tell"), {}, http_client=None))
    assert result.detail == "tell"
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert text in _BUILTIN_DADJOKES
    assert _scoped(_LAST_JOKE_KEY) in fake_host.kv.store


def test_tell_never_immediately_repeats(monkeypatch: pytest.MonkeyPatch, fake_host: _FakeHost) -> None:
    # Force the "random" pick to always prefer the first candidate -- proves the
    # excluded-last-ref filtering, not actual randomness, drives no-repeat.
    monkeypatch.setattr("random.choice", lambda candidates: candidates[0])

    _run(dispatch(_envelope("tell"), {}, http_client=None))
    first_last = fake_host.kv.store[_scoped(_LAST_JOKE_KEY)].decode()

    _run(dispatch(_envelope("tell"), {}, http_client=None))
    second_last = fake_host.kv.store[_scoped(_LAST_JOKE_KEY)].decode()

    assert first_last != second_last


def test_tell_with_single_joke_pool_still_replies(
    monkeypatch: pytest.MonkeyPatch, fake_host: _FakeHost
) -> None:
    monkeypatch.setattr("app._BUILTIN_DADJOKES", (_BUILTIN_DADJOKES[0],))
    _run(dispatch(_envelope("tell"), {}, http_client=None))
    result = _run(dispatch(_envelope("tell"), {}, http_client=None))
    assert result.detail == "tell"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _BUILTIN_DADJOKES[0]


def test_tell_draws_from_custom_pool_too(
    monkeypatch: pytest.MonkeyPatch, fake_host: _FakeHost
) -> None:
    _run(dispatch(_envelope("add", arg="a custom one", is_mod=True), {}, http_client=None))
    seen_refs: list[str] = []
    original_choice = random.choice

    def _spy(candidates: list[tuple[str, str]]) -> tuple[str, str]:
        seen_refs.extend(ref for ref, _ in candidates)
        return original_choice(candidates)

    monkeypatch.setattr("app.random.choice", _spy)
    _run(dispatch(_envelope("tell"), {}, http_client=None))
    assert "c1" in seen_refs


def test_different_communities_have_independent_last_joke(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("tell", community="comm-1"), {}, http_client=None))
    _run(dispatch(_envelope("tell", community="comm-2"), {}, http_client=None))
    assert _scoped(_LAST_JOKE_KEY, "comm-1") in fake_host.kv.store
    assert _scoped(_LAST_JOKE_KEY, "comm-2") in fake_host.kv.store


def test_tell_corrupt_custom_registry_fails_loud(fake_host: _FakeHost) -> None:
    fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)] = b"not json"
    with pytest.raises(RuntimeError, match="dadjoke load_registry failed"):
        _run(dispatch(_envelope("tell"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert "unavailable" in json.loads(message_json)["text"]
    assert any(m == "dadjoke.kv_error" for _lvl, m, _f in fake_host.log_calls)


def _fail_kv_op(kv_ns: types.SimpleNamespace, attr: str, *, only_for_key: str) -> Any:
    """Monkeypatch `kv_ns.<attr>` to raise only when called with `only_for_key`.

    Returns the original callable so a test can restore it (`fake_host`'s
    fixture-scoped `wit_world` is shared across a test's own helper calls).
    """
    original = getattr(kv_ns, attr)

    def _maybe_raise(key: str, *rest: Any) -> Any:
        if key == only_for_key:
            raise RuntimeError("backend down")
        return original(key, *rest)

    setattr(kv_ns, attr, _maybe_raise)
    return original


def test_tell_kv_get_host_failure_is_loud(fake_host: _FakeHost) -> None:
    kv_ns = sys.modules["wit_world"].imports.kv
    _fail_kv_op(kv_ns, "get", only_for_key=_scoped(_CUSTOM_REGISTRY_KEY))
    with pytest.raises(RuntimeError, match="dadjoke load_registry failed"):
        _run(dispatch(_envelope("tell"), {}, http_client=None))


def test_tell_kv_set_last_host_failure_is_loud(fake_host: _FakeHost) -> None:
    kv_ns = sys.modules["wit_world"].imports.kv
    _fail_kv_op(kv_ns, "set", only_for_key=_scoped(_LAST_JOKE_KEY))
    with pytest.raises(RuntimeError, match="dadjoke set_last failed"):
        _run(dispatch(_envelope("tell"), {}, http_client=None))


def test_tell_kv_get_last_host_failure_is_loud(fake_host: _FakeHost) -> None:
    kv_ns = sys.modules["wit_world"].imports.kv
    _fail_kv_op(kv_ns, "get", only_for_key=_scoped(_LAST_JOKE_KEY))
    with pytest.raises(RuntimeError, match="dadjoke get_last failed"):
        _run(dispatch(_envelope("tell"), {}, http_client=None))


def test_add_save_kv_failure_is_loud(fake_host: _FakeHost) -> None:
    kv_ns = sys.modules["wit_world"].imports.kv
    _fail_kv_op(kv_ns, "set", only_for_key=_scoped(_CUSTOM_REGISTRY_KEY))
    with pytest.raises(RuntimeError, match="dadjoke add_save failed"):
        _run(dispatch(_envelope("add", arg="x", is_mod=True), {}, http_client=None))


def test_remove_corrupt_registry_fails_loud(fake_host: _FakeHost) -> None:
    fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)] = b"not json"
    with pytest.raises(RuntimeError, match="dadjoke remove failed"):
        _run(dispatch(_envelope("remove", arg="1", is_mod=True), {}, http_client=None))


def test_remove_save_kv_failure_is_loud(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("add", arg="keep or not", is_mod=True), {}, http_client=None))
    kv_ns = sys.modules["wit_world"].imports.kv
    _fail_kv_op(kv_ns, "set", only_for_key=_scoped(_CUSTOM_REGISTRY_KEY))
    with pytest.raises(RuntimeError, match="dadjoke remove_save failed"):
        _run(dispatch(_envelope("remove", arg="1", is_mod=True), {}, http_client=None))


# ---------------------------------------------------------------------------
# !dadjoke add
# ---------------------------------------------------------------------------


def test_add_creates_custom_joke_with_sequential_id(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("add", arg="first joke", is_mod=True), {}, http_client=None))
    assert result.detail == "add"
    provider, message_json = fake_host.relay_calls[-1]
    assert "Added dad joke #1" in json.loads(message_json)["text"]
    registry = json.loads(fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)])
    assert registry == {"1": "first joke"}

    _run(dispatch(_envelope("add", arg="second joke", is_mod=True), {}, http_client=None))
    registry = json.loads(fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)])
    assert registry == {"1": "first joke", "2": "second joke"}


def test_add_requires_moderator_or_broadcaster(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _envelope("add", arg="nope", is_mod=False, is_broadcaster=False), {}, http_client=None
        )
    )
    assert result.detail == "add:denied"
    assert _scoped(_CUSTOM_REGISTRY_KEY) not in fake_host.kv.store


def test_add_allowed_for_broadcaster_without_mod(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _envelope("add", arg="ok", is_mod=False, is_broadcaster=True), {}, http_client=None
        )
    )
    assert result.detail == "add"


def test_add_rejected_when_role_signal_entirely_absent(fake_host: _FakeHost) -> None:
    """Discord today has no is_mod/is_broadcaster at all -- must deny, never implicitly allow."""
    result = _run(dispatch(_envelope("add", arg="nope"), {}, http_client=None))
    assert result.detail == "add:denied"
    denial_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "dadjoke.permission_denied"]
    assert denial_logs


def test_add_without_text_returns_usage(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("add", arg=None, is_mod=True), {}, http_client=None))
    assert result.detail == "add"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == "Usage: !dadjoke add <text>"


def test_add_rejects_overlong_text(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(_envelope("add", arg="x" * (MAX_JOKE_LEN + 1), is_mod=True), {}, http_client=None)
    )
    assert result.detail == "add"
    provider, message_json = fake_host.relay_calls[-1]
    assert "or fewer" in json.loads(message_json)["text"]


def test_add_kv_failure_is_loud(fake_host: _FakeHost) -> None:
    kv_ns = sys.modules["wit_world"].imports.kv
    monkeypatch_increment = kv_ns.increment

    def _raise_increment(key: str, delta: int, ttl: int) -> int:
        raise RuntimeError("backend down")

    kv_ns.increment = _raise_increment
    try:
        with pytest.raises(RuntimeError, match="dadjoke add failed"):
            _run(dispatch(_envelope("add", arg="x", is_mod=True), {}, http_client=None))
    finally:
        kv_ns.increment = monkeypatch_increment
    assert any(m == "dadjoke.kv_error" for _lvl, m, _f in fake_host.log_calls)


# ---------------------------------------------------------------------------
# !dadjoke remove
# ---------------------------------------------------------------------------


def test_remove_deletes_custom_joke(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("add", arg="to remove", is_mod=True), {}, http_client=None))
    result = _run(dispatch(_envelope("remove", arg="1", is_mod=True), {}, http_client=None))
    assert result.detail == "remove"
    provider, message_json = fake_host.relay_calls[-1]
    assert "Removed dad joke #1" in json.loads(message_json)["text"]
    registry = json.loads(fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)])
    assert registry == {}


def test_remove_requires_permission(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("add", arg="keep me", is_mod=True), {}, http_client=None))
    result = _run(dispatch(_envelope("remove", arg="1", is_mod=False), {}, http_client=None))
    assert result.detail == "remove:denied"
    registry = json.loads(fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)])
    assert "1" in registry


def test_remove_nonexistent_id_errors(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("remove", arg="99", is_mod=True), {}, http_client=None))
    assert result.detail == "remove"
    provider, message_json = fake_host.relay_calls[-1]
    assert "No custom dad joke #99 exists." in json.loads(message_json)["text"]


def test_remove_without_id_returns_usage(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("remove", arg=None, is_mod=True), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == "Usage: !dadjoke remove <id>"


def test_remove_rejects_non_numeric_id(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("remove", arg="abc", is_mod=True), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert "isn't a valid joke id" in json.loads(message_json)["text"]


# ---------------------------------------------------------------------------
# !dadjoke list
# ---------------------------------------------------------------------------


def test_list_empty(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("list"), {}, http_client=None))
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _NO_CUSTOM_JOKES_MSG


def test_list_open_to_anyone(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("add", arg="joke one", is_mod=True), {}, http_client=None))
    _run(dispatch(_envelope("add", arg="joke two", is_mod=True), {}, http_client=None))
    result = _run(
        dispatch(_envelope("list", is_mod=False, is_broadcaster=False), {}, http_client=None)
    )
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "#1: joke one" in text and "#2: joke two" in text


def test_list_corrupt_registry_fails_loud(fake_host: _FakeHost) -> None:
    fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)] = json.dumps([1, 2, 3]).encode()
    with pytest.raises(RuntimeError, match="dadjoke list failed"):
        _run(dispatch(_envelope("list"), {}, http_client=None))


# ---------------------------------------------------------------------------
# usage / dispatch validation
# ---------------------------------------------------------------------------


def test_usage_command_replies_with_usage_text(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("usage"), {}, http_client=None))
    assert result.detail == "usage"


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope("tell", channel_id=None), {}, http_client=None))


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(_envelope("tell", community=None), {}, http_client=None))


def test_dispatch_raises_on_unrecognized_command(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="unrecognized dadjoke command"):
        _run(dispatch(_envelope("not-a-real-command"), {}, http_client=None))


# ---------------------------------------------------------------------------
# _caller_role_signal() unit coverage
# ---------------------------------------------------------------------------


def test_caller_role_signal_absent_is_none() -> None:
    assert _caller_role_signal({}) is None


def test_caller_role_signal_true_from_mod() -> None:
    assert _caller_role_signal({"is_mod": True}) is True


def test_caller_role_signal_false_when_both_false() -> None:
    assert _caller_role_signal({"is_mod": False, "is_broadcaster": False}) is False


# ---------------------------------------------------------------------------
# kv key charset (gh-631) -- regression
# ---------------------------------------------------------------------------


def test_kv_key_constants_are_colon_free() -> None:
    for key in (_CUSTOM_REGISTRY_KEY, _CUSTOM_NEXT_ID_KEY, _LAST_JOKE_KEY):
        assert ":" not in key
        # Regression guard: the shared fake validates the exact host charset (gh-631), so a
        # key that would be host-rejected raises here too, never only in production.
        FakeKvHost().get(key)
