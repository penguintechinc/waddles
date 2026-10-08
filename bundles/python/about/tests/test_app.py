"""Host-native tests for the `about` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/pyping/tests/test_app.py`'s own
docstring for the fake-`wit_world` approach this mirrors. Uses the shared,
charset-enforcing `waddle_sdk.testing.install_fake_kv_host` fake for the
`kv` portion of the fake host (gh-631 -- see that module's own docstring),
extended here with fake `flags`/`relay`/`log` submodules for the rest of
`wit_world.imports` this bundle also touches.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, cast

import pytest
from waddle_sdk.command import CommandSpec, ParsedCommand, parse_command
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

from app import (
    _BLURB_KEY,
    _DEFAULT_BLURB,
    BOT_NAME,
    BOT_VERSION,
    MAX_BLURB_LEN,
    _caller_role_signal,
    _resolve_command,
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
        app_id="waddles.core.example.about",
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
# transform(): matching / alias / flag / grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["!about", "!ABOUT", "  !about  ", "!bot", "!BOT"])
def test_bare_about_or_bot_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == "show"


def test_about_set_forwards_arg(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!about set we stream every day", is_mod=True)))
    assert result is not None
    assert result.payload["command"] == "set"
    assert result.payload["arg"] == "we stream every day"


def test_bot_alias_set_forwards_arg(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!bot set hello there", is_mod=True)))
    assert result is not None
    assert result.payload["command"] == "set"
    assert result.payload["arg"] == "hello there"


@pytest.mark.parametrize(
    "text",
    ["!about enable ai", "!about bogus", "!about list", "!about reset", "!about disable foo"],
)
def test_unsupported_verbs_and_bad_grammar_reply_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!aboutish", "about", "!bots", "hello", ""])
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
    assert _run(transform(_event("!about"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!about set hi", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!about")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# ---------------------------------------------------------------------------
# _resolve_command() direct unit coverage
# ---------------------------------------------------------------------------


def test_resolve_command_none_is_usage() -> None:
    assert _resolve_command(None) == "usage"


def test_resolve_command_bare_is_show() -> None:
    spec = CommandSpec(name="about")
    parsed = parse_command("!about", spec)
    assert _resolve_command(parsed) == "show"


def test_resolve_command_unimplemented_verb_is_usage() -> None:
    parsed = ParsedCommand(command="about", sub_module=None, option="add", args="x")
    assert _resolve_command(parsed) == "usage"


# ---------------------------------------------------------------------------
# bare !about / !bot -> show
# ---------------------------------------------------------------------------


def test_show_default_blurb_when_none_set(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("show"), {}, http_client=None))
    assert result.detail == "show"
    provider, message_json = fake_host.relay_calls[-1]
    import json

    text = json.loads(message_json)["text"]
    assert text == f"{BOT_NAME} v{BOT_VERSION} -- {_DEFAULT_BLURB}"


def test_show_uses_custom_blurb_once_set(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="custom blurb here", is_mod=True), {}, http_client=None))
    result = _run(dispatch(_envelope("show"), {}, http_client=None))
    assert result.detail == "show"
    provider, message_json = fake_host.relay_calls[-1]
    import json

    text = json.loads(message_json)["text"]
    assert text == f"{BOT_NAME} v{BOT_VERSION} -- custom blurb here"


def test_different_communities_have_independent_blurbs(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _envelope("set", arg="comm-1 blurb", is_mod=True, community="comm-1"),
            {},
            http_client=None,
        )
    )
    _run(
        dispatch(
            _envelope("set", arg="comm-2 blurb", is_mod=True, community="comm-2"),
            {},
            http_client=None,
        )
    )
    assert fake_host.kv.store[_scoped(_BLURB_KEY, "comm-1")] == b"comm-1 blurb"
    assert fake_host.kv.store[_scoped(_BLURB_KEY, "comm-2")] == b"comm-2 blurb"


def test_show_kv_get_host_failure_is_loud(fake_host: _FakeHost) -> None:
    kv_ns = sys.modules["wit_world"].imports.kv
    original = kv_ns.get

    def _raise(key: str) -> bytes | None:
        raise RuntimeError("backend down")

    kv_ns.get = _raise
    try:
        with pytest.raises(RuntimeError, match="about show failed"):
            _run(dispatch(_envelope("show"), {}, http_client=None))
    finally:
        kv_ns.get = original
    assert any(m == "about.kv_error" for _lvl, m, _f in fake_host.log_calls)
    provider, message_json = fake_host.relay_calls[-1]
    import json

    assert "unavailable" in json.loads(message_json)["text"]


# ---------------------------------------------------------------------------
# !about set / !bot set
# ---------------------------------------------------------------------------


def test_set_requires_moderator_or_broadcaster(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _envelope("set", arg="nope", is_mod=False, is_broadcaster=False),
            {},
            http_client=None,
        )
    )
    assert result.detail == "set:denied"
    assert _scoped(_BLURB_KEY) not in fake_host.kv.store


def test_set_allowed_for_broadcaster_without_mod(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _envelope("set", arg="ok blurb", is_mod=False, is_broadcaster=True),
            {},
            http_client=None,
        )
    )
    assert result.detail == "set"
    assert fake_host.kv.store[_scoped(_BLURB_KEY)] == b"ok blurb"


def test_set_rejected_when_role_signal_entirely_absent(fake_host: _FakeHost) -> None:
    """Discord today has no is_mod/is_broadcaster at all -- must deny, never implicitly allow."""
    result = _run(dispatch(_envelope("set", arg="nope"), {}, http_client=None))
    assert result.detail == "set:denied"
    denial_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "about.permission_denied"]
    assert denial_logs


def test_set_without_text_returns_usage(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("set", arg=None, is_mod=True), {}, http_client=None))
    assert result.detail == "set"
    provider, message_json = fake_host.relay_calls[-1]
    import json

    assert json.loads(message_json)["text"] == "Usage: !about set <text>"


def test_set_rejects_blank_text(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("set", arg="   ", is_mod=True), {}, http_client=None))
    assert result.detail == "set"
    provider, message_json = fake_host.relay_calls[-1]
    import json

    assert json.loads(message_json)["text"] == "Usage: !about set <text>"


def test_set_rejects_overlong_text(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(_envelope("set", arg="x" * (MAX_BLURB_LEN + 1), is_mod=True), {}, http_client=None)
    )
    assert result.detail == "set"
    provider, message_json = fake_host.relay_calls[-1]
    import json

    assert "or fewer" in json.loads(message_json)["text"]


def test_set_kv_failure_is_loud(fake_host: _FakeHost) -> None:
    kv_ns = sys.modules["wit_world"].imports.kv
    original = kv_ns.set

    def _raise(key: str, value: bytes, ttl: int) -> None:
        raise RuntimeError("backend down")

    kv_ns.set = _raise
    try:
        with pytest.raises(RuntimeError, match="about set failed"):
            _run(dispatch(_envelope("set", arg="x", is_mod=True), {}, http_client=None))
    finally:
        kv_ns.set = original
    assert any(m == "about.kv_error" for _lvl, m, _f in fake_host.log_calls)


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
    with pytest.raises(ValueError, match="unrecognized about command"):
        _run(dispatch(_envelope("not-a-real-command"), {}, http_client=None))


def test_none_community_is_a_valid_tenant_wide_sentinel_not_rejected(
    fake_host: _FakeHost,
) -> None:
    """Unlike `joke`/`wheel`, `None` community must NOT raise -- it's `community_kv`'s own

    tenant-wide sentinel (scoped under the literal `"0"` segment), alpha's only activation
    shape today (one static scope per `svc-ingest-rust` pod).
    """
    result = _run(
        dispatch(_envelope("set", arg="tenant blurb", is_mod=True, community=None), {}, http_client=None)
    )
    assert result.detail == "set"
    assert fake_host.kv.store[_scoped(_BLURB_KEY, None)] == b"tenant blurb"

    show_result = _run(dispatch(_envelope("show", community=None), {}, http_client=None))
    assert show_result.detail == "show"
    provider, message_json = fake_host.relay_calls[-1]
    import json

    assert json.loads(message_json)["text"] == f"{BOT_NAME} v{BOT_VERSION} -- tenant blurb"


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
    assert ":" not in _BLURB_KEY
    # Regression guard: the shared fake validates the exact host charset (gh-631), so a key
    # that would be host-rejected raises here too, never only in production.
    FakeKvHost().get(_BLURB_KEY)
