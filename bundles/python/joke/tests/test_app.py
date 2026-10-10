"""Host-native tests for the `joke` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/pyping/tests/test_app.py`'s own
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
    _BUILTIN_JOKES,
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
        occurred_at="2026-10-05T00:00:00.000Z",
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
        app_id="waddles.core.example.joke",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload=payload,
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )


# ---------------------------------------------------------------------------
# transform(): matching / flag / grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["!joke", "!JOKE", "  !joke  "])
def test_bare_joke_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == "tell"


def test_joke_list_matches(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!joke list")))
    assert result is not None
    assert result.payload["command"] == "list"


def test_joke_list_with_trailing_args_is_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!joke list extra")))
    assert result is not None
    assert result.payload["command"] == "usage"


def test_joke_add_forwards_arg(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!joke add why did the chicken cross the road")))
    assert result is not None
    assert result.payload["command"] == "add"
    assert result.payload["arg"] == "why did the chicken cross the road"


def test_joke_remove_forwards_arg(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!joke remove 3")))
    assert result is not None
    assert result.payload["command"] == "remove"
    assert result.payload["arg"] == "3"


@pytest.mark.parametrize(
    "text", ["!joke enable ai", "!joke bogus", "!joke set x", "!joke reset", "!joke disable foo"]
)
def test_unsupported_verbs_and_bad_grammar_reply_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!jokebook", "joke", "!jokes", "hello", ""])
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
    assert _run(transform(_event("!joke"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!joke add hi", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!joke")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# ---------------------------------------------------------------------------
# _resolve_command() direct unit coverage
# ---------------------------------------------------------------------------


def test_resolve_command_none_is_usage() -> None:
    assert _resolve_command(None) == "usage"


def test_resolve_command_bare_is_tell() -> None:
    spec = CommandSpec(name="joke")
    parsed = parse_command("!joke", spec)
    assert _resolve_command(parsed) == "tell"


def test_resolve_command_unimplemented_verb_is_usage() -> None:
    parsed = ParsedCommand(command="joke", sub_module=None, option="set", args="x")
    assert _resolve_command(parsed) == "usage"


# ---------------------------------------------------------------------------
# bare !joke -> tell
# ---------------------------------------------------------------------------


def test_tell_posts_a_builtin_joke_and_persists_last(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("tell"), {}, http_client=None))
    assert result.detail == "tell"
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert text in _BUILTIN_JOKES
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
    monkeypatch.setattr("app._BUILTIN_JOKES", (_BUILTIN_JOKES[0],))
    _run(dispatch(_envelope("tell"), {}, http_client=None))
    result = _run(dispatch(_envelope("tell"), {}, http_client=None))
    assert result.detail == "tell"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _BUILTIN_JOKES[0]


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
    with pytest.raises(RuntimeError, match="joke load_registry failed"):
        _run(dispatch(_envelope("tell"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert "unavailable" in json.loads(message_json)["text"]
    assert any(m == "joke.kv_error" for _lvl, m, _f in fake_host.log_calls)


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
    with pytest.raises(RuntimeError, match="joke load_registry failed"):
        _run(dispatch(_envelope("tell"), {}, http_client=None))


def test_tell_kv_set_last_host_failure_is_loud(fake_host: _FakeHost) -> None:
    kv_ns = sys.modules["wit_world"].imports.kv
    _fail_kv_op(kv_ns, "set", only_for_key=_scoped(_LAST_JOKE_KEY))
    with pytest.raises(RuntimeError, match="joke set_last failed"):
        _run(dispatch(_envelope("tell"), {}, http_client=None))


def test_tell_kv_get_last_host_failure_is_loud(fake_host: _FakeHost) -> None:
    kv_ns = sys.modules["wit_world"].imports.kv
    _fail_kv_op(kv_ns, "get", only_for_key=_scoped(_LAST_JOKE_KEY))
    with pytest.raises(RuntimeError, match="joke get_last failed"):
        _run(dispatch(_envelope("tell"), {}, http_client=None))


def test_add_save_kv_failure_is_loud(fake_host: _FakeHost) -> None:
    kv_ns = sys.modules["wit_world"].imports.kv
    _fail_kv_op(kv_ns, "set", only_for_key=_scoped(_CUSTOM_REGISTRY_KEY))
    with pytest.raises(RuntimeError, match="joke add_save failed"):
        _run(dispatch(_envelope("add", arg="x", is_mod=True), {}, http_client=None))


def test_remove_corrupt_registry_fails_loud(fake_host: _FakeHost) -> None:
    fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)] = b"not json"
    with pytest.raises(RuntimeError, match="joke remove failed"):
        _run(dispatch(_envelope("remove", arg="1", is_mod=True), {}, http_client=None))


def test_remove_save_kv_failure_is_loud(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("add", arg="keep or not", is_mod=True), {}, http_client=None))
    kv_ns = sys.modules["wit_world"].imports.kv
    _fail_kv_op(kv_ns, "set", only_for_key=_scoped(_CUSTOM_REGISTRY_KEY))
    with pytest.raises(RuntimeError, match="joke remove_save failed"):
        _run(dispatch(_envelope("remove", arg="1", is_mod=True), {}, http_client=None))


# ---------------------------------------------------------------------------
# !joke add
# ---------------------------------------------------------------------------


def test_add_creates_custom_joke_with_sequential_id(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("add", arg="first joke", is_mod=True), {}, http_client=None))
    assert result.detail == "add"
    provider, message_json = fake_host.relay_calls[-1]
    assert "Added joke #1" in json.loads(message_json)["text"]
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
    denial_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "joke.permission_denied"]
    assert denial_logs


def test_add_without_text_returns_usage(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("add", arg=None, is_mod=True), {}, http_client=None))
    assert result.detail == "add"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == "Usage: !joke add <text>"


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
        with pytest.raises(RuntimeError, match="joke add failed"):
            _run(dispatch(_envelope("add", arg="x", is_mod=True), {}, http_client=None))
    finally:
        kv_ns.increment = monkeypatch_increment
    assert any(m == "joke.kv_error" for _lvl, m, _f in fake_host.log_calls)


# ---------------------------------------------------------------------------
# !joke remove
# ---------------------------------------------------------------------------


def test_remove_deletes_custom_joke(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("add", arg="to remove", is_mod=True), {}, http_client=None))
    result = _run(dispatch(_envelope("remove", arg="1", is_mod=True), {}, http_client=None))
    assert result.detail == "remove"
    provider, message_json = fake_host.relay_calls[-1]
    assert "Removed joke #1" in json.loads(message_json)["text"]
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
    assert "No custom joke #99 exists." in json.loads(message_json)["text"]


def test_remove_without_id_returns_usage(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("remove", arg=None, is_mod=True), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == "Usage: !joke remove <id>"


def test_remove_rejects_non_numeric_id(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("remove", arg="abc", is_mod=True), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert "isn't a valid joke id" in json.loads(message_json)["text"]


# ---------------------------------------------------------------------------
# !joke list
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
    with pytest.raises(RuntimeError, match="joke list failed"):
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
    with pytest.raises(ValueError, match="unrecognized joke command"):
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


# ---------------------------------------------------------------------------
# PII-free logs -- regression: gh-674 (bundle-logs-must-be-pii-free)
# ---------------------------------------------------------------------------

_SENTINEL = "SENTINELpii9f3a"

#: Strict per-message allowlist: a log line may carry ONLY these fields. A new field (e.g. a
#: raw `text=`/`arg=`/`actor=`) fails here instead of silently shipping user input to telemetry.
_ALLOWED_LOG_FIELDS: dict[str, frozenset[str]] = {
    "joke.transform matched": frozenset({"command"}),
    "joke.dispatch applied": frozenset({"command"}),
    "joke.dispatch relayed": frozenset({"platform", "command"}),
    "joke.custom_added": frozenset({"joke_id"}),
    "joke.custom_removed": frozenset({"joke_id"}),
    "joke.permission_denied": frozenset({"command", "role_signal"}),
    "joke.kv_error": frozenset({"op", "error"}),
    "joke.missing_community": frozenset({"command"}),
}


def _assert_logs_pii_free(fake_host: _FakeHost, *, minimum_lines: int) -> None:
    """Every captured log line is allow-listed field-by-field and free of the sentinel.

    Asserts a non-empty denominator first -- a check that examined zero log lines proves nothing.
    """
    assert len(fake_host.log_calls) >= minimum_lines
    for _lvl, message, fields_json in fake_host.log_calls:
        assert _SENTINEL not in message
        assert _SENTINEL not in fields_json
        assert message in _ALLOWED_LOG_FIELDS, f"unexpected log message {message!r}"
        assert set(json.loads(fields_json)) <= _ALLOWED_LOG_FIELDS[message]


def test_transform_logs_never_carry_user_input(fake_host: _FakeHost) -> None:
    # regression: gh-674
    for text in (
        "!joke",
        f"!joke add {_SENTINEL}",
        f"!joke remove {_SENTINEL}",
        f"!joke list {_SENTINEL}",
        f"!joke bogus {_SENTINEL}",
        f"!joke enable {_SENTINEL}",
    ):
        event = _event(text, is_mod=True)
        event.actor = _SENTINEL
        assert _run(transform(event)) is not None
    _assert_logs_pii_free(fake_host, minimum_lines=6)


def test_dispatch_logs_never_carry_user_input_on_any_verb(fake_host: _FakeHost) -> None:
    # regression: gh-674 -- jokes themselves are user content, so a stored custom joke that
    # contains the sentinel must never reach a log line either.
    def _go(command: str, arg: str | None = None, **role: bool) -> None:
        envelope = _envelope(command, arg=arg, **role)
        envelope.event.actor = _SENTINEL
        _run(dispatch(envelope, {}, http_client=None))

    _go("add", f"joke about {_SENTINEL}", is_mod=True)
    _go("add", f"denied {_SENTINEL}", is_mod=False)
    _go("add", None, is_mod=True)
    _go("list")
    _go("tell")
    _go("remove", _SENTINEL, is_mod=True)  # non-numeric id
    _go("remove", "1", is_mod=True)
    _go("remove", "1", is_mod=True)  # already gone
    _go("usage")
    _assert_logs_pii_free(fake_host, minimum_lines=6)
    assert {m for _lvl, m, _f in fake_host.log_calls} >= {
        "joke.custom_added",
        "joke.custom_removed",
        "joke.permission_denied",
        "joke.dispatch relayed",
        "joke.dispatch applied",
    }


def test_failure_paths_never_log_user_input(fake_host: _FakeHost) -> None:
    # regression: gh-674 -- the kv-failure and corrupt-registry paths log `op` + a fixed reason.
    kv_ns = sys.modules["wit_world"].imports.kv
    _fail_kv_op(kv_ns, "set", only_for_key=_scoped(_CUSTOM_REGISTRY_KEY))
    with pytest.raises(RuntimeError):
        _run(dispatch(_envelope("add", arg=f"joke {_SENTINEL}", is_mod=True), {}, http_client=None))

    fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)] = ("{" + _SENTINEL).encode("utf-8")
    for command in ("tell", "list"):
        with pytest.raises(RuntimeError):
            _run(dispatch(_envelope(command), {}, http_client=None))
    _assert_logs_pii_free(fake_host, minimum_lines=3)


def test_denial_log_carries_only_the_role_signal_state(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("add", arg=_SENTINEL), {}, http_client=None))
    _run(dispatch(_envelope("remove", arg="1", is_mod=False), {}, http_client=None))
    denials = [json.loads(f) for _lvl, m, f in fake_host.log_calls if m == "joke.permission_denied"]
    assert denials == [
        {"command": "add", "role_signal": "None"},
        {"command": "remove", "role_signal": "False"},
    ]


# ---------------------------------------------------------------------------
# Mod gate: fail-closed, before any kv access
# ---------------------------------------------------------------------------


def _last_reply(fake_host: _FakeHost) -> str:
    _provider, message_json = fake_host.relay_calls[-1]
    return cast(str, json.loads(message_json)["text"])


@pytest.mark.parametrize("command", ["add", "remove"])
@pytest.mark.parametrize(
    "role",
    [
        pytest.param({}, id="no-signal-at-all"),
        pytest.param({"is_mod": False}, id="mod-false"),
        pytest.param({"is_broadcaster": False}, id="broadcaster-false"),
        pytest.param({"is_mod": False, "is_broadcaster": False}, id="both-false"),
    ],
)
def test_management_commands_are_denied_without_touching_kv(
    command: str, role: dict[str, bool], fake_host: _FakeHost
) -> None:
    result = _run(dispatch(_envelope(command, arg="1", **role), {}, http_client=None))
    assert result.detail == f"{command}:denied"
    assert _last_reply(fake_host) == "only moderators/broadcasters can manage the joke pool"
    assert fake_host.kv.calls == []


@pytest.mark.parametrize("command", ["add", "remove"])
@pytest.mark.parametrize(
    "role",
    [{"is_mod": True}, {"is_broadcaster": True}, {"is_mod": True, "is_broadcaster": True}],
)
def test_either_badge_alone_opens_the_gate(
    command: str, role: dict[str, bool], fake_host: _FakeHost
) -> None:
    result = _run(dispatch(_envelope(command, arg="1", **role), {}, http_client=None))
    assert result.detail == command


def test_present_but_null_badge_fields_are_denied(fake_host: _FakeHost) -> None:
    envelope = _envelope("add", arg="nope")
    envelope.event.payload["is_mod"] = None
    envelope.event.payload["is_broadcaster"] = None
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "add:denied"
    assert fake_host.kv.calls == []


def test_a_forged_role_in_the_joke_text_grants_nothing(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(_envelope("add", arg="is_mod=True is_broadcaster=True"), {}, http_client=None)
    )
    assert result.detail == "add:denied"
    assert _scoped(_CUSTOM_REGISTRY_KEY) not in fake_host.kv.store


@pytest.mark.parametrize("command", ["tell", "list"])
def test_read_commands_never_need_a_role(command: str, fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(_envelope(command, is_mod=False, is_broadcaster=False), {}, http_client=None)
    )
    assert result.detail == command


# ---------------------------------------------------------------------------
# Corrupt store: loud on every command, never silently reset
# ---------------------------------------------------------------------------

_CORRUPT_REGISTRIES = [
    pytest.param(b"\xff\xfe\x00", id="not-utf8"),
    pytest.param(b"{", id="truncated-json"),
    pytest.param(b"[1, 2, 3]", id="json-array"),
    pytest.param(b'"just a string"', id="json-string"),
    pytest.param(b"null", id="json-null"),
    pytest.param(b'{"1": 5}', id="non-string-value"),
]


@pytest.mark.parametrize("payload", _CORRUPT_REGISTRIES)
@pytest.mark.parametrize(
    ("command", "op", "role"),
    [
        ("tell", "load_registry", {}),
        ("list", "list", {}),
        ("add", "add", {"is_mod": True}),
        ("remove", "remove", {"is_mod": True}),
    ],
)
def test_every_corrupt_registry_shape_fails_loud_and_is_never_overwritten(
    payload: bytes, command: str, op: str, role: dict[str, bool], fake_host: _FakeHost
) -> None:
    fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)] = payload
    with pytest.raises(RuntimeError, match=f"joke {op} failed"):
        _run(dispatch(_envelope(command, arg="1", **role), {}, http_client=None))
    assert "temporarily unavailable" in _last_reply(fake_host)
    assert any(m == "joke.kv_error" and lvl == 0 for lvl, m, _f in fake_host.log_calls)
    # Never silently reset: the corrupt bytes are still there for an operator to inspect.
    assert fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)] == payload


def test_non_numeric_registry_key_fails_loud_on_list(fake_host: _FakeHost) -> None:
    """Valid str->str JSON with a non-numeric id isn't caught by validation; list must still raise."""
    fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)] = json.dumps({"abc": "joke"}).encode()
    with pytest.raises((ValueError, RuntimeError)):
        _run(dispatch(_envelope("list"), {}, http_client=None))
    assert fake_host.relay_calls == []


def test_non_utf8_last_joke_pointer_fails_loud_instead_of_picking(fake_host: _FakeHost) -> None:
    fake_host.kv.store[_scoped(_LAST_JOKE_KEY)] = b"\xff\xfe"
    with pytest.raises((ValueError, RuntimeError)):
        _run(dispatch(_envelope("tell"), {}, http_client=None))
    assert fake_host.relay_calls == []


def test_stale_last_joke_pointer_self_heals(fake_host: _FakeHost) -> None:
    """`joke.last` naming a custom joke that was since removed is harmless: pool is unfiltered."""
    fake_host.kv.store[_scoped(_LAST_JOKE_KEY)] = b"c99"
    result = _run(dispatch(_envelope("tell"), {}, http_client=None))
    assert result.detail == "tell"
    assert _last_reply(fake_host) in _BUILTIN_JOKES


# ---------------------------------------------------------------------------
# No silent fallback: failed writes leave state untouched; relay errors propagate
# ---------------------------------------------------------------------------


def test_failed_add_increment_leaves_registry_untouched(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("add", arg="keep", is_mod=True), {}, http_client=None))
    before = dict(fake_host.kv.store)
    kv_ns = sys.modules["wit_world"].imports.kv
    _fail_kv_op(kv_ns, "increment", only_for_key=_scoped(_CUSTOM_NEXT_ID_KEY))
    with pytest.raises(RuntimeError, match="joke add failed"):
        _run(dispatch(_envelope("add", arg="lost", is_mod=True), {}, http_client=None))
    assert fake_host.kv.store == before
    assert "temporarily unavailable" in _last_reply(fake_host)


def test_failed_remove_save_leaves_the_joke_in_place(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("add", arg="keep", is_mod=True), {}, http_client=None))
    before = fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)]
    kv_ns = sys.modules["wit_world"].imports.kv
    _fail_kv_op(kv_ns, "set", only_for_key=_scoped(_CUSTOM_REGISTRY_KEY))
    with pytest.raises(RuntimeError, match="joke remove_save failed"):
        _run(dispatch(_envelope("remove", arg="1", is_mod=True), {}, http_client=None))
    assert fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)] == before


def test_relay_failure_propagates_and_is_not_logged_as_relayed(fake_host: _FakeHost) -> None:
    def _boom(provider: str, msg: str) -> None:
        raise RuntimeError("relay down")

    sys.modules["wit_world"].imports.relay = types.SimpleNamespace(push=_boom)
    with pytest.raises(RuntimeError, match="relay down"):
        _run(dispatch(_envelope("list"), {}, http_client=None))
    assert not any(m == "joke.dispatch relayed" for _lvl, m, _f in fake_host.log_calls)


def test_missing_community_logs_error_and_touches_no_state(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(_envelope("add", community=None, arg="x", is_mod=True), {}, http_client=None))
    assert fake_host.kv.calls == []
    assert fake_host.relay_calls == []
    errors = [json.loads(f) for lvl, m, f in fake_host.log_calls if m == "joke.missing_community"]
    assert errors == [{"command": "add"}]


# ---------------------------------------------------------------------------
# CRUD edges
# ---------------------------------------------------------------------------


def test_ids_are_never_reused_after_a_removal(fake_host: _FakeHost) -> None:
    for text in ("one", "two"):
        _run(dispatch(_envelope("add", arg=text, is_mod=True), {}, http_client=None))
    _run(dispatch(_envelope("remove", arg="1", is_mod=True), {}, http_client=None))
    _run(dispatch(_envelope("add", arg="three", is_mod=True), {}, http_client=None))
    assert json.loads(fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)]) == {
        "2": "two",
        "3": "three",
    }


def test_list_orders_ids_numerically_not_lexically(fake_host: _FakeHost) -> None:
    for i in range(1, 12):
        _run(dispatch(_envelope("add", arg=f"joke-{i}", is_mod=True), {}, http_client=None))
    _run(dispatch(_envelope("list"), {}, http_client=None))
    text = _last_reply(fake_host)
    assert text.index("#2:") < text.index("#10:") < text.index("#11:")


def test_add_accepts_exactly_the_max_length_and_strips_whitespace(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _envelope("add", arg="  " + "x" * MAX_JOKE_LEN + "  ", is_mod=True),
            {},
            http_client=None,
        )
    )
    registry = json.loads(fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)])
    assert registry == {"1": "x" * MAX_JOKE_LEN}


def test_whitespace_only_add_text_is_usage_and_stores_nothing(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("add", arg="   ", is_mod=True), {}, http_client=None))
    assert _last_reply(fake_host) == "Usage: !joke add <text>"
    assert fake_host.kv.calls == []


@pytest.mark.parametrize("raw_id", ["01", "²", "1.5", "-1", "1 2"])
def test_remove_with_non_canonical_id_never_removes_anything(
    raw_id: str, fake_host: _FakeHost
) -> None:
    _run(dispatch(_envelope("add", arg="keep", is_mod=True), {}, http_client=None))
    _run(dispatch(_envelope("remove", arg=raw_id, is_mod=True), {}, http_client=None))
    registry = json.loads(fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)])
    assert registry == {"1": "keep"}


def test_communities_never_share_custom_jokes(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("add", arg="comm-1 only", is_mod=True), {}, http_client=None))
    _run(dispatch(_envelope("list", community="comm-2"), {}, http_client=None))
    assert _last_reply(fake_host) == _NO_CUSTOM_JOKES_MSG


def test_flag_is_checked_by_key_and_defaults_off(fake_host: _FakeHost) -> None:
    seen: list[tuple[str, bool]] = []

    def _enabled(key: str, default_value: bool) -> bool:
        seen.append((key, default_value))
        return False

    sys.modules["wit_world"].imports.flags = types.SimpleNamespace(enabled=_enabled)
    assert _run(transform(_event("!joke"))) is None
    assert seen == [("waddles.command-joke", False)]
    assert fake_host.kv.calls == []
