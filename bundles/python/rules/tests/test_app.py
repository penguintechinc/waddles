"""Host-native tests for the `rules` bundle's `transform`/`dispatch` logic.

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
import sys
import types
from typing import Any, cast

import pytest
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

from app import (
    MAX_RULES_LEN,
    _NO_RULES_MSG,
    _RULES_KEY,
    _caller_role_signal,
    _resolve,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _scoped(key: str, community: str | None = "comm-1") -> str:
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


def _last_reply_text(host: _FakeHost) -> str:
    _provider, message_json = host.relay_calls[-1]
    return cast(str, json.loads(message_json)["text"])


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
        occurred_at="2026-10-07T00:00:00.000Z",
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
        app_id="waddles.core.example.rules",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload=payload,
            occurred_at="2026-10-07T00:00:00.000Z",
        ),
        ts="2026-10-07T00:00:00.000Z",
    )


# ---------------------------------------------------------------------------
# transform(): matching / flag / grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["!rules", "!RULES", "  !rules  "])
def test_bare_rules_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == "show"


def test_rules_set_forwards_arg(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!rules set be excellent to each other", is_mod=True)))
    assert result is not None
    assert result.payload["command"] == "set"
    assert result.payload["arg"] == "be excellent to each other"


@pytest.mark.parametrize("text", ["!rules clear", "!rules CLEAR", "!rules remove"])
def test_rules_clear_and_remove_alias_both_resolve_to_clear(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_event(text, is_mod=True)))
    assert result is not None
    assert result.payload["command"] == "clear"


def test_rules_clear_with_trailing_args_is_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!rules clear now", is_mod=True)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize(
    "text", ["!rules enable ai", "!rules bogus", "!rules list", "!rules reset", "!rules delete"]
)
def test_unsupported_verbs_reply_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!rulesbook", "rules", "!ruleset", "hello", ""])
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
    assert _run(transform(_event("!rules"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!rules set hi", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!rules")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# ---------------------------------------------------------------------------
# _resolve() direct unit coverage
# ---------------------------------------------------------------------------


def test_resolve_empty_is_show() -> None:
    assert _resolve("") == ("show", None)


def test_resolve_clear_normalizes_to_clear_action() -> None:
    assert _resolve("clear") == ("clear", None)


def test_resolve_remove_also_resolves_to_clear_action() -> None:
    assert _resolve("remove") == ("clear", None)


def test_resolve_set_forwards_args() -> None:
    assert _resolve("set be nice") == ("set", "be nice")


def test_resolve_unknown_verb_is_usage(fake_host: _FakeHost) -> None:
    """Needs `fake_host`: an unknown verb now logs via `log.debug` before the usage fallback."""
    assert _resolve("bogus") == ("usage", None)


# ---------------------------------------------------------------------------
# bare !rules -> show
# ---------------------------------------------------------------------------


def test_show_no_rules_set_yet(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("show"), {}, http_client=None))
    assert result.detail == "show"
    assert _last_reply_text(fake_host) == _NO_RULES_MSG


def test_show_returns_saved_rules_text(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="rule one, rule two", is_mod=True), {}, http_client=None))
    result = _run(dispatch(_envelope("show"), {}, http_client=None))
    assert result.detail == "show"
    assert _last_reply_text(fake_host) == "rule one, rule two"


def test_different_communities_have_independent_rules(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _envelope("set", arg="comm-1 rules", is_mod=True, community="comm-1"),
            {},
            http_client=None,
        )
    )
    _run(
        dispatch(
            _envelope("set", arg="comm-2 rules", is_mod=True, community="comm-2"),
            {},
            http_client=None,
        )
    )
    assert fake_host.kv.store[_scoped(_RULES_KEY, "comm-1")] == b"comm-1 rules"
    assert fake_host.kv.store[_scoped(_RULES_KEY, "comm-2")] == b"comm-2 rules"


def test_show_kv_get_host_failure_is_loud(fake_host: _FakeHost) -> None:
    kv_ns = sys.modules["wit_world"].imports.kv
    original = kv_ns.get

    def _raise(key: str) -> bytes | None:
        raise RuntimeError("backend down")

    kv_ns.get = _raise
    try:
        with pytest.raises(RuntimeError, match="rules show failed"):
            _run(dispatch(_envelope("show"), {}, http_client=None))
    finally:
        kv_ns.get = original
    assert any(m == "rules.kv_error" for _lvl, m, _f in fake_host.log_calls)
    assert "unavailable" in _last_reply_text(fake_host)


# ---------------------------------------------------------------------------
# !rules set
# ---------------------------------------------------------------------------


def test_set_requires_moderator_or_broadcaster(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _envelope("set", arg="nope", is_mod=False, is_broadcaster=False), {}, http_client=None
        )
    )
    assert result.detail == "set:denied"
    assert _scoped(_RULES_KEY) not in fake_host.kv.store


def test_set_allowed_for_broadcaster_without_mod(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _envelope("set", arg="ok rules", is_mod=False, is_broadcaster=True), {}, http_client=None
        )
    )
    assert result.detail == "set"
    assert fake_host.kv.store[_scoped(_RULES_KEY)] == b"ok rules"


def test_set_rejected_when_role_signal_entirely_absent(fake_host: _FakeHost) -> None:
    """Discord today has no is_mod/is_broadcaster at all -- must deny, never implicitly allow."""
    result = _run(dispatch(_envelope("set", arg="nope"), {}, http_client=None))
    assert result.detail == "set:denied"
    denial_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "rules.permission_denied"]
    assert denial_logs


def test_set_without_text_returns_usage(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("set", arg=None, is_mod=True), {}, http_client=None))
    assert result.detail == "set"
    assert _last_reply_text(fake_host) == "Usage: !rules set <text>"


def test_set_rejects_blank_text(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("set", arg="   ", is_mod=True), {}, http_client=None))
    assert result.detail == "set"
    assert _last_reply_text(fake_host) == "Usage: !rules set <text>"


def test_set_rejects_overlong_text(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(_envelope("set", arg="x" * (MAX_RULES_LEN + 1), is_mod=True), {}, http_client=None)
    )
    assert result.detail == "set"
    assert "or fewer" in _last_reply_text(fake_host)


def test_set_kv_failure_is_loud(fake_host: _FakeHost) -> None:
    kv_ns = sys.modules["wit_world"].imports.kv
    original = kv_ns.set

    def _raise(key: str, value: bytes, ttl: int) -> None:
        raise RuntimeError("backend down")

    kv_ns.set = _raise
    try:
        with pytest.raises(RuntimeError, match="rules set failed"):
            _run(dispatch(_envelope("set", arg="x", is_mod=True), {}, http_client=None))
    finally:
        kv_ns.set = original
    assert any(m == "rules.kv_error" for _lvl, m, _f in fake_host.log_calls)


# ---------------------------------------------------------------------------
# !rules clear
# ---------------------------------------------------------------------------


def test_clear_deletes_rules(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="to be cleared", is_mod=True), {}, http_client=None))
    result = _run(dispatch(_envelope("clear", is_mod=True), {}, http_client=None))
    assert result.detail == "clear"
    assert "cleared" in _last_reply_text(fake_host)
    assert _scoped(_RULES_KEY) not in fake_host.kv.store


def test_clear_requires_moderator_or_broadcaster(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="keep me", is_mod=True), {}, http_client=None))
    result = _run(dispatch(_envelope("clear", is_mod=False), {}, http_client=None))
    assert result.detail == "clear:denied"
    assert _scoped(_RULES_KEY) in fake_host.kv.store


def test_clear_when_no_rules_set_is_still_a_no_op_success(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("clear", is_mod=True), {}, http_client=None))
    assert result.detail == "clear"
    assert "cleared" in _last_reply_text(fake_host)


def test_clear_kv_failure_is_loud(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="x", is_mod=True), {}, http_client=None))
    kv_ns = sys.modules["wit_world"].imports.kv
    original = kv_ns.delete

    def _raise(key: str) -> None:
        raise RuntimeError("backend down")

    kv_ns.delete = _raise
    try:
        with pytest.raises(RuntimeError, match="rules clear failed"):
            _run(dispatch(_envelope("clear", is_mod=True), {}, http_client=None))
    finally:
        kv_ns.delete = original
    assert any(m == "rules.kv_error" for _lvl, m, _f in fake_host.log_calls)


# ---------------------------------------------------------------------------
# usage / dispatch validation / tenant-wide sentinel
# ---------------------------------------------------------------------------


def test_usage_command_replies_with_usage_text(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("usage"), {}, http_client=None))
    assert result.detail == "usage"


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope("show", channel_id=None), {}, http_client=None))


def test_dispatch_raises_on_unrecognized_command(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="unrecognized rules command"):
        _run(dispatch(_envelope("not-a-real-command"), {}, http_client=None))


def test_none_community_is_a_valid_tenant_wide_sentinel_not_rejected(
    fake_host: _FakeHost,
) -> None:
    """Unlike `joke`/`wheel`, `None` community must NOT raise -- it's `community_kv`'s own

    tenant-wide sentinel (scoped under the literal `"0"` segment), alpha's only activation
    shape today (one static scope per `svc-ingest-rust` pod).
    """
    result = _run(
        dispatch(
            _envelope("set", arg="tenant rules", is_mod=True, community=None), {}, http_client=None
        )
    )
    assert result.detail == "set"
    assert fake_host.kv.store[_scoped(_RULES_KEY, None)] == b"tenant rules"

    show_result = _run(dispatch(_envelope("show", community=None), {}, http_client=None))
    assert show_result.detail == "show"
    assert _last_reply_text(fake_host) == "tenant rules"


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


def test_kv_key_constant_is_colon_free() -> None:
    assert ":" not in _RULES_KEY
    # Regression guard: the shared fake validates the exact host charset (gh-631), so a key
    # that would be host-rejected raises here too, never only in production.
    FakeKvHost().get(_RULES_KEY)
