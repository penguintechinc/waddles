"""Host-native tests for the `lurk` bundle's stateful `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/pyping/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors, extended with a fake `kv`/`clock` import and a `flags.tier`
stub (license gating) alongside `flags.enabled`/`relay`/`log`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.community_kv import TENANT_WIDE_SENTINEL
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.kv import validate_key

from app import (
    DEFAULT_LURK_TEMPLATE,
    NOT_LURKING_REPLY,
    _ai_key,
    _format_duration,
    _license_tier,
    _message_key,
    _render_template,
    _state_key,
    _validate_template,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _expected_state_key(community: str, actor: str | None) -> str:
    pseudonym = hashlib.sha256((actor or "anonymous").encode()).hexdigest()
    return f"lurk.state.{community}.{pseudonym}"


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
    """Fake WIT host: flags/kv/relay/log/clock, with a real in-memory kv store + TTL ignored."""

    def __init__(self, *, tier: str = "free") -> None:
        self.store: dict[str, bytes] = {}
        self.kv_calls: list[tuple[Any, ...]] = []
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[str, str, str]] = []
        self.now_ms = 1_700_000_000_000
        self.tier = tier

    def advance(self, seconds: int) -> None:
        self.now_ms += seconds * 1000


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
    kv_delete_raises: Exception | None = None,
) -> None:
    def kv_get(key: str) -> bytes | None:
        # regression: gh-631 -- validates exactly like the real `bundle_host_kv`
        # host capability (`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`,
        # mirrored by `waddle_sdk.kv.validate_key`). The old fake accepted any
        # key, including the `:`-containing ones lurk originally shipped, so a
        # production-breaking key passed every test here -- never again.
        validate_key(key)
        host.kv_calls.append(("get", key))
        if kv_get_raises is not None:
            raise kv_get_raises
        return host.store.get(key)

    def kv_set(key: str, value: bytes, ttl: int) -> None:
        validate_key(key)
        host.kv_calls.append(("set", key, bytes(value), ttl))
        if kv_set_raises is not None:
            raise kv_set_raises
        host.store[key] = bytes(value)

    def kv_delete(key: str) -> None:
        validate_key(key)
        host.kv_calls.append(("delete", key))
        if kv_delete_raises is not None:
            raise kv_delete_raises
        host.store.pop(key, None)

    def kv_increment(key: str, delta: int, ttl: int) -> int:
        validate_key(key)
        return 1

    flags_mod = types.SimpleNamespace(
        enabled=lambda key, default_value: True, tier=lambda: host.tier
    )
    kv_mod = types.SimpleNamespace(get=kv_get, set=kv_set, delete=kv_delete, increment=kv_increment)
    # `waddle_sdk.relay.push` already serializes `message` to canonical JSON text
    # before calling this import -- `msg` here is already a JSON string, not a dict.
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
        app_id="waddles.core.example.lurk",
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


# -- transform() parsing -----------------------------------------------------


@pytest.mark.parametrize("text", ["!lurk", "!LURK", "  !lurk  "])
def test_lurk_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "lurk"


def test_unlurk_matches(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!unlurk")))
    assert result is not None
    assert result.payload["command"] == "unlurk"


@pytest.mark.parametrize(
    ("text", "expected_command", "expected_arg"),
    [
        ("!lurk set hello $(username)", "config_set_message", "hello $(username)"),
        ("!lurk SET hello", "config_set_message", "hello"),
        ("!lurk set", "config_set_message", ""),
        ("!lurk enable ai", "config_enable_ai", None),
        ("!lurk disable ai", "config_disable_ai", None),
        ("!lurk reset", "config_reset", None),
        ("!lurk bogus", "usage", None),
    ],
)
def test_transform_parses_admin_subcommands(
    text: str, expected_command: str, expected_arg: str | None, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == expected_command
    assert result.payload.get("arg") == expected_arg


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!lurk set hi", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!lurk set hi")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


@pytest.mark.parametrize("text", ["!lurking", "lurk", "!unlurked", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host: _FakeHost) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_chat_payload_is_ignored_rather_than_erroring(fake_host: _FakeHost) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="channel.follow",
        actor=None,
        payload={},
        occurred_at="",
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: False)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(flags=flags_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert _run(transform(_sample_event("!lurk"))) is None


# -- lurk / unlurk state machine ---------------------------------------------


def test_dispatch_lurk_stores_state_and_replies_with_default_template(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "lurk")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "lurk"
    op, key, value, ttl = fake_host.kv_calls[0]
    assert op == "set"
    assert key == _expected_state_key("comm-1", "viewer-1")
    assert "viewer-1" not in key
    assert int(value.decode()) == fake_host.now_ms
    assert ttl == 24 * 60 * 60
    provider, message_json = fake_host.relay_calls[0]
    assert json.loads(message_json) == {
        "channel": "12345",
        "text": "viewer-1 is now lurking \U0001f440",
    }


def test_relurk_resets_the_start_timestamp(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "lurk"), {}, http_client=None))
    fake_host.advance(60)
    _run(dispatch(_sample_envelope("twitch", "lurk"), {}, http_client=None))

    set_calls = [c for c in fake_host.kv_calls if c[0] == "set"]
    assert len(set_calls) == 2
    first_value, second_value = int(set_calls[0][2].decode()), int(set_calls[1][2].decode())
    assert second_value > first_value


def test_unlurk_reports_elapsed_duration_and_clears_state(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "lurk"), {}, http_client=None))
    fake_host.advance(2 * 3600 + 13 * 60)  # 2h 13m

    result = _run(dispatch(_sample_envelope("twitch", "unlurk"), {}, http_client=None))

    assert result.detail == "unlurk"
    key = _expected_state_key("comm-1", "viewer-1")
    assert key not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert text == "Welcome back, viewer-1! You lurked for 2h 13m."


def test_unlurk_when_never_lurking_is_friendly_and_does_not_delete(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "unlurk"), {}, http_client=None))

    assert result.detail == "unlurk"
    assert not any(c[0] == "delete" for c in fake_host.kv_calls)
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == NOT_LURKING_REPLY


def test_unlurk_after_simulated_ttl_expiry_reports_not_lurking(fake_host: _FakeHost) -> None:
    """24h TTL expiry has no separate code path -- it's the same as the key never existing."""
    _run(dispatch(_sample_envelope("twitch", "lurk"), {}, http_client=None))
    key = _expected_state_key("comm-1", "viewer-1")
    del fake_host.store[key]  # simulate the host's kv TTL having evicted the entry

    result = _run(dispatch(_sample_envelope("twitch", "unlurk"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == NOT_LURKING_REPLY
    assert result.detail == "unlurk"


def test_dispatch_uses_the_communitys_custom_lurk_template(fake_host: _FakeHost) -> None:
    fake_host.store[_message_key("comm-1")] = b"$(username) tiptoes into the shadows"
    result = _run(dispatch(_sample_envelope("twitch", "lurk"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == "viewer-1 tiptoes into the shadows"
    assert result.detail == "lurk"


# -- community scoping --------------------------------------------------------


def test_dispatch_functions_under_the_tenant_wide_sentinel(fake_host: _FakeHost) -> None:
    """Regression: this task -- alpha's tenant-wide activation must make `!lurk` reply.

    Alpha's only activation (`community_id: null` -> `envelope.community=None`) must make
    `!lurk` actually reply, not merely fail less silently. Supersedes gh-655/1.0.5, which
    replied with an error and still raised.
    """
    envelope = _sample_envelope("twitch", "lurk", community=None)
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "lurk"
    op, key, _value, _ttl = fake_host.kv_calls[0]
    assert op == "set"
    assert key == _expected_state_key(TENANT_WIDE_SENTINEL, "viewer-1")
    provider, message_json = fake_host.relay_calls[0]
    assert provider == "twitch"
    assert json.loads(message_json) == {
        "channel": "12345",
        "text": "viewer-1 is now lurking \U0001f440",
    }


def test_dispatch_raises_and_replies_when_community_is_empty_string(
    fake_host: _FakeHost,
) -> None:
    """An empty-string `community` is still fatal, unlike `None`.

    The host never emits an empty string (only `None`, the tenant-wide sentinel, does), so
    this can only be a caller-side bug.

    Regression: gh-655 -- `!lurk` was completely silent in chat under this path.
    The tenant-wide sentinel (`community=None`) used to hit this exact guard and
    log `lurk.missing_community` without ever calling `relay.push`, so the caller
    saw nothing at all; `None` now takes the tenant-wide path above instead, but
    `""` still exercises this fail-loud guard (same shape as `_fail_kv`): log AND
    reply, then raise.
    """
    envelope = _sample_envelope("twitch", "lurk", community="")
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(envelope, {}, http_client=None))

    assert fake_host.kv_calls == []
    assert fake_host.relay_calls, "lurk must never fail silently -- a chat reply is required"
    provider, message_json = fake_host.relay_calls[-1]
    assert provider == "twitch"
    assert "community" in json.loads(message_json)["text"].lower()
    error_logs = [(lvl, m) for lvl, m, _f in fake_host.log_calls if m == "lurk.missing_community"]
    assert error_logs


def test_different_communities_never_share_lurk_state(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "lurk", community="comm-1"), {}, http_client=None))
    result = _run(
        dispatch(_sample_envelope("twitch", "unlurk", community="comm-2"), {}, http_client=None)
    )
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == NOT_LURKING_REPLY
    assert result.detail == "unlurk"


# -- admin config: permission gate --------------------------------------------


@pytest.mark.parametrize(
    ("is_mod", "is_broadcaster"),
    [(True, None), (None, True), (True, True)],
)
def test_set_message_allowed_for_mod_or_broadcaster(
    is_mod: bool | None, is_broadcaster: bool | None, fake_host: _FakeHost
) -> None:
    envelope = _sample_envelope(
        "twitch",
        "config_set_message",
        arg="hi $(username)",
        is_mod=is_mod,
        is_broadcaster=is_broadcaster,
    )
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_message"
    assert fake_host.store[_message_key("comm-1")] == b"hi $(username)"
    provider, message_json = fake_host.relay_calls[-1]
    assert "updated" in json.loads(message_json)["text"]


def test_set_message_rejected_for_non_mod(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "config_set_message", arg="hi", is_mod=False, is_broadcaster=False
    )
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_message:denied"
    assert _message_key("comm-1") not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    assert "only moderators/broadcasters" in json.loads(message_json)["text"]


def test_set_message_rejected_when_role_signal_entirely_absent(fake_host: _FakeHost) -> None:
    """Discord today has no is_mod/is_broadcaster at all -- must deny, never implicitly allow."""
    envelope = _sample_envelope("discord", "config_set_message", arg="hi")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_message:denied"
    assert _message_key("comm-1") not in fake_host.store
    denial_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "lurk.config_denied"]
    assert denial_logs


def test_set_message_rejects_unknown_placeholder(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "config_set_message", arg="hi $(foo)", is_mod=True)
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_message"
    assert _message_key("comm-1") not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    assert "unknown placeholder" in json.loads(message_json)["text"]


def test_reset_restores_defaults(fake_host: _FakeHost) -> None:
    fake_host.store[_message_key("comm-1")] = b"custom"
    fake_host.store[_ai_key("comm-1")] = b"1"
    envelope = _sample_envelope("twitch", "config_reset", is_mod=True)
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_reset"
    assert _message_key("comm-1") not in fake_host.store
    assert _ai_key("comm-1") not in fake_host.store


# -- AI toggle: Enterprise license gate ----------------------------------------


def test_enable_ai_allowed_on_enterprise_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost(tier="enterprise")
    _install(monkeypatch, host)
    envelope = _sample_envelope("twitch", "config_enable_ai", is_mod=True)
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_enable_ai"
    assert host.store[_ai_key("comm-1")] == b"1"


@pytest.mark.parametrize("tier", ["free", "professional"])
def test_enable_ai_rejected_below_enterprise(tier: str, monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost(tier=tier)
    _install(monkeypatch, host)
    envelope = _sample_envelope("twitch", "config_enable_ai", is_mod=True)
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_enable_ai"
    assert _ai_key("comm-1") not in host.store
    provider, message_json = host.relay_calls[-1]
    assert "Enterprise" in json.loads(message_json)["text"]


def test_enable_ai_rejected_for_non_mod_even_on_enterprise_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost(tier="enterprise")
    _install(monkeypatch, host)
    envelope = _sample_envelope("twitch", "config_enable_ai", is_mod=False)
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_enable_ai:denied"
    assert _ai_key("comm-1") not in host.store


def test_disable_ai_requires_no_license_check(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost(tier="free")
    _install(monkeypatch, host)
    host.store[_ai_key("comm-1")] = b"1"
    envelope = _sample_envelope("twitch", "config_disable_ai", is_mod=True)
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_disable_ai"
    assert _ai_key("comm-1") not in host.store


def test_ai_enabled_falls_back_to_template_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1: ai_enabled=on still gets the template reply -- gh #610 builds the real AI call."""
    host = _FakeHost(tier="enterprise")
    _install(monkeypatch, host)
    host.store[_ai_key("comm-1")] = b"1"
    result = _run(dispatch(_sample_envelope("twitch", "lurk"), {}, http_client=None))

    assert result.detail == "lurk"
    provider, message_json = host.relay_calls[-1]
    assert json.loads(message_json)["text"] == "viewer-1 is now lurking \U0001f440"
    pending_logs = [m for _lvl, m, _f in host.log_calls if m == "lurk.ai_requested_but_pending"]
    assert pending_logs


# -- kv failure: fail loud, never silent --------------------------------------


class _ErrorBackend:
    """Stand-in for the generated WIT `Error_Backend` variant case class."""


class _KvError(Exception):
    """Stand-in for the generated `Err` wrapper -- `.value` holds the error union member."""

    def __init__(self) -> None:
        self.value = _ErrorBackend()


def test_dispatch_lurk_replies_and_raises_on_kv_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_set_raises=_KvError())

    with pytest.raises(RuntimeError, match="kv set failed"):
        _run(dispatch(_sample_envelope("twitch", "lurk"), {}, http_client=None))

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in json.loads(message_json)["text"]
    error_logs = [(lvl, m) for lvl, m, _f in host.log_calls if m == "lurk.kv_error"]
    assert error_logs


def test_dispatch_unlurk_replies_and_raises_on_kv_get_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_get_raises=_KvError())

    with pytest.raises(RuntimeError, match="kv get failed"):
        _run(dispatch(_sample_envelope("twitch", "unlurk"), {}, http_client=None))

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in json.loads(message_json)["text"]


def test_dispatch_unlurk_replies_and_raises_on_kv_delete_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_delete_raises=_KvError())
    host.store[_expected_state_key("comm-1", "viewer-1")] = b"1700000000000"

    with pytest.raises(RuntimeError, match="kv delete failed"):
        _run(dispatch(_sample_envelope("twitch", "unlurk"), {}, http_client=None))

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in json.loads(message_json)["text"]


# -- corrupt stored state: degrade, never crash --------------------------------


def test_corrupt_custom_template_falls_back_to_default(fake_host: _FakeHost) -> None:
    fake_host.store[_message_key("comm-1")] = b"\xff\xfe not valid utf-8"
    result = _run(dispatch(_sample_envelope("twitch", "lurk"), {}, http_client=None))

    assert result.detail == "lurk"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == "viewer-1 is now lurking \U0001f440"
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "lurk.template_corrupt"]
    assert corrupt_logs


def test_corrupt_lurk_state_is_treated_as_not_lurking(fake_host: _FakeHost) -> None:
    key = _expected_state_key("comm-1", "viewer-1")
    fake_host.store[key] = b"not-a-timestamp"
    result = _run(dispatch(_sample_envelope("twitch", "unlurk"), {}, http_client=None))

    assert result.detail == "unlurk"
    assert key not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == NOT_LURKING_REPLY
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "lurk.state_corrupt"]
    assert corrupt_logs


def test_set_message_with_no_argument_replies_usage(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "config_set_message", arg="", is_mod=True)
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_message"
    assert _message_key("comm-1") not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    assert "Usage" in json.loads(message_json)["text"]


# -- _license_tier(): fails closed, never implicitly enterprise ----------------


def test_license_tier_fails_closed_when_wit_world_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "wit_world", raising=False)
    assert _license_tier() == "free"


def test_license_tier_fails_closed_when_flags_import_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    assert _license_tier() == "free"


def test_license_tier_fails_closed_when_tier_fn_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=lambda k, d: True)
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    assert _license_tier() == "free"


def test_license_tier_reads_the_real_binding(fake_host: _FakeHost) -> None:
    fake_host.tier = "enterprise"
    assert _license_tier() == "enterprise"


# -- misc ----------------------------------------------------------------------


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.lurk",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={"command": "lurk", "channel_id": None},
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv_calls == []


def test_dispatch_raises_on_an_unrecognized_command(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "not-a-real-command")
    with pytest.raises(ValueError, match="unrecognized lurk command"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_never_logs_the_raw_actor(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "lurk"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "unlurk"), {}, http_client=None))
    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "actor" not in json.loads(fields_json)


def test_kv_key_is_a_non_reversible_hash_not_the_raw_actor() -> None:
    assert _state_key("comm-1", "viewer-1") == _expected_state_key("comm-1", "viewer-1")
    assert "viewer-1" not in _state_key("comm-1", "viewer-1")


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (45, "45s"), (13 * 60, "13m"), (2 * 3600 + 13 * 60, "2h 13m"), (90000, "1d 1h")],
)
def test_format_duration(seconds: int, expected: str) -> None:
    assert _format_duration(seconds) == expected


def test_render_template_substitutes_both_placeholders() -> None:
    assert _render_template("$(username): $(duration)", username="bob", duration="5m") == "bob: 5m"


def test_render_template_blanks_unused_duration_placeholder() -> None:
    assert (
        _render_template(DEFAULT_LURK_TEMPLATE, username="bob") == "bob is now lurking \U0001f440"
    )


@pytest.mark.parametrize(
    ("template", "expected_error_fragment"),
    [
        ("", "empty"),
        ("x" * 201, "too long"),
        ("hi $(foo)", "unknown placeholder"),
    ],
)
def test_validate_template_rejects_bad_input(template: str, expected_error_fragment: str) -> None:
    error = _validate_template(template)
    assert error is not None
    assert expected_error_fragment in error


@pytest.mark.parametrize(
    "template", ["$(username) is lurking", "$(username) for $(duration)", "static"]
)
def test_validate_template_accepts_known_placeholders(template: str) -> None:
    assert _validate_template(template) is None


def test_unlurk_usage_reply_for_unrecognized_subcommand(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "usage"), {}, http_client=None))
    assert result.detail == "usage"
    provider, message_json = fake_host.relay_calls[-1]
    assert "Usage" in json.loads(message_json)["text"]


# regression: gh-631 -- `_state_key`/`_message_key`/`_ai_key` originally used `:` as their
# segment separator (`"lurk:state:{community}:{pseudonym}"`, etc). The real `bundle_host_kv`
# host capability (`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`) reserves `:` as
# its own namespace separator and rejects any guest key containing one -- every real `kv`
# call this bundle made failed in production with `kv.error::backend`, invisible to this
# whole test suite because the old fake `kv` host accepted any key. `fake_host` (via
# `_install`'s `validate_key()` calls) now enforces the same charset as the real host, so a
# regression back to `:` fails here, not in production.
def test_kv_key_helpers_contain_no_colon() -> None:
    assert ":" not in _state_key("comm-1", "viewer-1")
    assert ":" not in _message_key("comm-1")
    assert ":" not in _ai_key("comm-1")


def test_kv_key_helpers_satisfy_host_guest_key_charset() -> None:
    # Raises if any byte falls outside the real host's allowed charset.
    validate_key(_state_key("comm-1", "viewer-1"))
    validate_key(_message_key("comm-1"))
    validate_key(_ai_key("comm-1"))


def test_colon_key_is_rejected_before_any_host_call() -> None:
    """A colon-containing key fails fast at the SDK boundary, exactly like the real host."""
    from waddle_sdk import kv
    from waddle_sdk.kv import InvalidKvKeyError

    with pytest.raises(InvalidKvKeyError, match="characters outside"):
        _run(kv.get(f"lurk:state:comm-1:{'x' * 10}"))


# -- PII-free logs -- regression: gh-674 (bundle-logs-must-be-pii-free) ---------

_SENTINEL = "SENTINELpii9f3a"

#: Strict per-message allowlist: a log line may carry ONLY these fields. A new field (e.g. a
#: raw `text=`/`arg=`/`actor=`) fails here instead of silently shipping user input to telemetry.
_ALLOWED_LOG_FIELDS: dict[str, frozenset[str]] = {
    "lurk.transform matched": frozenset({"command"}),
    "lurk.dispatch relayed": frozenset({"platform", "command"}),
    "lurk.dispatch config applied": frozenset({"command"}),
    "lurk.config_denied": frozenset({"command", "role_signal"}),
    "lurk.enable_ai_denied": frozenset({"tier"}),
    "lurk.ai_requested_but_pending": frozenset({"community"}),
    "lurk.template_corrupt": frozenset({"community"}),
    "lurk.state_corrupt": frozenset({"community"}),
    "lurk.kv_error": frozenset({"op", "error"}),
    "lurk.missing_community": frozenset({"command"}),
}


def _assert_logs_pii_free(host: _FakeHost, *, minimum_lines: int) -> set[str]:
    """Every captured log line is allow-listed field-by-field and free of the sentinel.

    Asserts a non-empty denominator first -- a check that examined zero log lines proves nothing.
    Returns the set of distinct messages seen so callers can prove each branch was exercised.
    """
    assert len(host.log_calls) >= minimum_lines
    for _lvl, message, fields_json in host.log_calls:
        assert _SENTINEL not in message
        assert _SENTINEL not in fields_json
        assert message in _ALLOWED_LOG_FIELDS, f"unexpected log message {message!r}"
        assert set(json.loads(fields_json)) <= _ALLOWED_LOG_FIELDS[message]
    return {message for _lvl, message, _f in host.log_calls}


def test_transform_logs_never_carry_user_input(fake_host: _FakeHost) -> None:
    # regression: gh-674
    for text in (
        "!lurk",
        "!unlurk",
        f"!lurk set {_SENTINEL} $(username)",
        f"!lurk bogus {_SENTINEL}",
        f"!lurk enable ai {_SENTINEL}",
        "!lurk enable ai",
        "!lurk disable ai",
        "!lurk reset",
    ):
        event = _sample_event(text, is_mod=True)
        event.actor = _SENTINEL
        assert _run(transform(event)) is not None
    seen = _assert_logs_pii_free(fake_host, minimum_lines=8)
    assert seen == {"lurk.transform matched"}


def test_dispatch_logs_never_carry_user_input_on_any_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # regression: gh-674 -- every dispatch branch, with a sentinel as BOTH the actor and (for
    # `set`) the message text; the stored kv KEYS must not contain the raw actor either.
    host = _FakeHost(tier="enterprise")
    _install(monkeypatch, host)

    def go(command: str, *, arg: str | None = None, role: bool | None = None) -> None:
        envelope = _sample_envelope("twitch", command, actor=_SENTINEL, arg=arg, is_mod=role)
        _run(dispatch(envelope, {}, http_client=None))

    go("lurk")
    go("unlurk")
    go("unlurk")  # no longer lurking
    go("config_set_message", arg=f"{_SENTINEL} $(username)", role=True)
    go("config_set_message", arg=f"{_SENTINEL} $(nope)", role=True)  # rejected template
    go("config_enable_ai", role=True)
    go("lurk")  # ai toggle on -> "pending" DEBUG line
    go("config_disable_ai", role=True)
    go("config_reset", role=True)
    go("config_reset")  # denied, no role signal
    go("usage")
    host.tier = "free"
    go("config_enable_ai", role=True)  # license-denied
    host.store[_message_key("comm-1")] = b"\xff\xfe"
    go("lurk")  # corrupt template
    host.store[_expected_state_key("comm-1", _SENTINEL)] = b"garbage"
    go("unlurk")  # corrupt state

    seen = _assert_logs_pii_free(host, minimum_lines=12)
    assert seen == {
        "lurk.dispatch relayed",
        "lurk.dispatch config applied",
        "lurk.config_denied",
        "lurk.enable_ai_denied",
        "lurk.ai_requested_but_pending",
        "lurk.template_corrupt",
        "lurk.state_corrupt",
    }
    assert all(_SENTINEL not in key for _op, key, *_rest in host.kv_calls)


def test_failure_paths_never_log_user_input(monkeypatch: pytest.MonkeyPatch) -> None:
    # regression: gh-674 -- a kv failure whose own exception text echoes user-ish data must still
    # log only the classified error-case NAME, and the empty-community guard only the command.
    host = _FakeHost()
    _install(monkeypatch, host, kv_set_raises=RuntimeError(f"backend detail {_SENTINEL}"))
    with pytest.raises(RuntimeError) as excinfo:
        _run(dispatch(_sample_envelope("twitch", "lurk", actor=_SENTINEL), {}, http_client=None))
    assert _SENTINEL not in str(excinfo.value)

    with pytest.raises(ValueError):
        _run(
            dispatch(
                _sample_envelope("twitch", "lurk", actor=_SENTINEL, community=""),
                {},
                http_client=None,
            )
        )

    seen = _assert_logs_pii_free(host, minimum_lines=2)
    assert seen == {"lurk.kv_error", "lurk.missing_community"}
    (kv_error,) = [json.loads(f) for _lvl, m, f in host.log_calls if m == "lurk.kv_error"]
    assert kv_error == {"op": "set", "error": "RuntimeError"}


# -- mod gate: every config command fails closed, before any kv/license access --------

_CONFIG_COMMANDS = [
    pytest.param("config_set_message", "hi $(username)", id="set"),
    pytest.param("config_enable_ai", None, id="enable-ai"),
    pytest.param("config_disable_ai", None, id="disable-ai"),
    pytest.param("config_reset", None, id="reset"),
]


@pytest.mark.parametrize(("command", "arg"), _CONFIG_COMMANDS)
@pytest.mark.parametrize(
    "role",
    [
        pytest.param({}, id="no-signal-at-all"),
        pytest.param({"is_mod": False}, id="mod-false"),
        pytest.param({"is_broadcaster": False}, id="broadcaster-false"),
        pytest.param({"is_mod": False, "is_broadcaster": False}, id="both-false"),
    ],
)
def test_every_config_command_is_denied_without_any_kv_or_license_access(
    command: str, arg: str | None, role: dict[str, bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _FakeHost(tier="enterprise")
    _install(monkeypatch, host)
    tier_calls: list[int] = []
    flags = sys.modules["wit_world"].imports.flags
    flags.tier = lambda: tier_calls.append(1) or "enterprise"

    result = _run(
        dispatch(_sample_envelope("twitch", command, arg=arg, **role), {}, http_client=None)
    )

    assert result.detail == f"{command}:denied"
    assert (
        json.loads(host.relay_calls[-1][1])["text"]
        == "only moderators/broadcasters can configure !lurk"
    )
    assert host.kv_calls == []
    assert tier_calls == []


@pytest.mark.parametrize(("command", "arg"), _CONFIG_COMMANDS)
@pytest.mark.parametrize(
    "role",
    [{"is_mod": True}, {"is_broadcaster": True}, {"is_mod": True, "is_broadcaster": True}],
)
def test_either_badge_alone_opens_every_config_command(
    command: str, arg: str | None, role: dict[str, bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _FakeHost(tier="enterprise")
    _install(monkeypatch, host)
    result = _run(
        dispatch(_sample_envelope("twitch", command, arg=arg, **role), {}, http_client=None)
    )
    assert result.detail == command


def test_present_but_null_badge_fields_are_denied(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "config_reset")
    envelope.event.payload["is_mod"] = None
    envelope.event.payload["is_broadcaster"] = None
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "config_reset:denied"
    assert fake_host.kv_calls == []


def test_lurk_and_unlurk_never_need_a_role(fake_host: _FakeHost) -> None:
    for command in ("lurk", "unlurk"):
        result = _run(dispatch(_sample_envelope("discord", command), {}, http_client=None))
        assert result.detail == command


# -- transform(): grammar boundaries -----------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_command"),
    [
        ("!lurk ENABLE AI", "config_enable_ai"),
        ("!lurk Disable Ai", "config_disable_ai"),
        ("!lurk RESET", "config_reset"),
        ("!lurk enable", "usage"),
        ("!lurk disable", "usage"),
        ("!lurk enable ai please", "usage"),
        ("!lurk resetnow", "usage"),
        ("!lurk setx hi", "usage"),
        ("!lurk   ", "lurk"),
    ],
)
def test_transform_subcommand_boundaries(
    text: str, expected_command: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == expected_command


def test_transform_forwards_only_command_channel_arg_and_badges(fake_host: _FakeHost) -> None:
    event = _sample_event("!lurk set hi", is_mod=True)
    event.payload["email"] = "x@example.com"
    result = _run(transform(event))
    assert result is not None
    assert set(result.payload) == {"command", "channel_id", "arg", "is_mod"}


def test_flag_is_checked_by_key_and_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, bool]] = []

    def _enabled(key: str, default_value: bool) -> bool:
        seen.append((key, default_value))
        return False

    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=_enabled)
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    assert _run(transform(_sample_event("!lurk"))) is None
    assert seen == [("waddles.command-lurk", False)]


# -- kv failure on every op of every command: fail loud, never silent -----------------


def _kv_error_scenarios() -> list[Any]:
    return [
        pytest.param("lurk", None, "set", "free", None, id="lurk-set"),
        pytest.param("lurk", None, "get", "free", None, id="lurk-ai-flag-get"),
        pytest.param("unlurk", None, "get", "free", None, id="unlurk-get"),
        pytest.param("unlurk", None, "delete", "free", b"1700000000000", id="unlurk-delete"),
        pytest.param("unlurk", None, "delete", "free", b"garbage", id="corrupt-state-delete"),
        pytest.param("config_set_message", "hi", "set", "free", None, id="set-message"),
        pytest.param("config_enable_ai", None, "set", "enterprise", None, id="enable-ai"),
        pytest.param("config_disable_ai", None, "delete", "free", None, id="disable-ai"),
        pytest.param("config_reset", None, "delete", "free", None, id="reset"),
    ]


@pytest.mark.parametrize(("command", "arg", "op", "tier", "state"), _kv_error_scenarios())
def test_every_kv_op_failure_replies_logs_error_and_raises(
    command: str,
    arg: str | None,
    op: str,
    tier: str,
    state: bytes | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost(tier=tier)
    _install(monkeypatch, host, **{f"kv_{op}_raises": _KvError()})
    if state is not None:
        host.store[_expected_state_key("comm-1", "viewer-1")] = state
    is_mod = command.startswith("config_") or None

    with pytest.raises(RuntimeError, match=f"lurk kv {op} failed: _ErrorBackend"):
        _run(
            dispatch(
                _sample_envelope("twitch", command, arg=arg, is_mod=is_mod), {}, http_client=None
            )
        )

    # Exactly one relay: the error reply -- never a success message ahead of/after the failure.
    assert len(host.relay_calls) == 1
    assert "temporarily unavailable" in json.loads(host.relay_calls[0][1])["text"]
    errors = [(lvl, json.loads(f)) for lvl, m, f in host.log_calls if m == "lurk.kv_error"]
    assert errors == [(0, {"op": op, "error": "_ErrorBackend"})]


def test_failed_lurk_set_stores_no_state(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_set_raises=_KvError())
    with pytest.raises(RuntimeError):
        _run(dispatch(_sample_envelope("twitch", "lurk"), {}, http_client=None))
    assert host.store == {}


def test_community_id_with_a_reserved_character_fails_loud_not_silent(
    fake_host: _FakeHost,
) -> None:
    """A `:` in the community id would build a host-rejected key -- must raise, never no-op."""
    with pytest.raises(RuntimeError, match="lurk kv set failed: InvalidKvKeyError"):
        _run(
            dispatch(
                _sample_envelope("twitch", "lurk", community="bad:community"), {}, http_client=None
            )
        )
    assert "temporarily unavailable" in json.loads(fake_host.relay_calls[-1][1])["text"]


def test_relay_failure_propagates_and_is_not_logged_as_relayed(
    fake_host: _FakeHost,
) -> None:
    def _boom(provider: str, msg: str) -> None:
        raise RuntimeError("relay down")

    sys.modules["wit_world"].imports.relay = types.SimpleNamespace(push=_boom)
    with pytest.raises(RuntimeError, match="relay down"):
        _run(dispatch(_sample_envelope("twitch", "lurk"), {}, http_client=None))
    assert not any(m == "lurk.dispatch relayed" for _lvl, m, _f in fake_host.log_calls)


# -- corrupt stored state: ERROR-logged (loud), self-healing, never a crash -------------


@pytest.mark.parametrize("payload", [b"\xff\xfe", b"\x80abc", b"ok\xc3("])
def test_every_corrupt_template_is_error_logged_and_falls_back_to_default(
    payload: bytes, fake_host: _FakeHost
) -> None:
    fake_host.store[_message_key("comm-1")] = payload
    _run(dispatch(_sample_envelope("twitch", "lurk"), {}, http_client=None))
    assert json.loads(fake_host.relay_calls[-1][1])["text"] == "viewer-1 is now lurking \U0001f440"
    corrupt = [
        (lvl, json.loads(f)) for lvl, m, f in fake_host.log_calls if m == "lurk.template_corrupt"
    ]
    assert corrupt == [(0, {"community": "comm-1"})]  # Level.ERROR


@pytest.mark.parametrize("payload", [b"", b"12abc", b"\xff", b"1.5", b" ", b"NaN"])
def test_every_corrupt_state_is_error_logged_cleared_and_reads_as_not_lurking(
    payload: bytes, fake_host: _FakeHost
) -> None:
    key = _expected_state_key("comm-1", "viewer-1")
    fake_host.store[key] = payload
    _run(dispatch(_sample_envelope("twitch", "unlurk"), {}, http_client=None))
    assert json.loads(fake_host.relay_calls[-1][1])["text"] == NOT_LURKING_REPLY
    assert key not in fake_host.store
    corrupt = [
        (lvl, json.loads(f)) for lvl, m, f in fake_host.log_calls if m == "lurk.state_corrupt"
    ]
    assert corrupt == [(0, {"community": "comm-1"})]


def test_a_future_start_timestamp_clamps_to_zero_elapsed(fake_host: _FakeHost) -> None:
    """Clock skew (start in the future) must never render a negative or absurd duration."""
    key = _expected_state_key("comm-1", "viewer-1")
    fake_host.store[key] = str(fake_host.now_ms + 3_600_000).encode()
    _run(dispatch(_sample_envelope("twitch", "unlurk"), {}, http_client=None))
    assert (
        json.loads(fake_host.relay_calls[-1][1])["text"]
        == "Welcome back, viewer-1! You lurked for 0s."
    )


# -- ai toggle: the v1 fallback is announced, never silent -- regression: gh-610 ----------


def test_ai_pending_fallback_is_announced_at_debug_with_community_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # regression: gh-610 -- AI responses are not built yet; the template fallback must stay
    # visible in telemetry (DEBUG, community only) rather than be a silent stub.
    host = _FakeHost(tier="enterprise")
    _install(monkeypatch, host)
    host.store[_ai_key("comm-1")] = b"1"
    _run(dispatch(_sample_envelope("twitch", "lurk"), {}, http_client=None))
    pending = [
        (lvl, json.loads(f)) for lvl, m, f in host.log_calls if m == "lurk.ai_requested_but_pending"
    ]
    assert pending == [(3, {"community": "comm-1"})]  # Level.DEBUG


# -- scoping, TTLs, identity edges --------------------------------------------------------


def test_config_under_the_tenant_wide_sentinel_is_isolated_from_a_community(
    fake_host: _FakeHost,
) -> None:
    _run(
        dispatch(
            _sample_envelope(
                "twitch",
                "config_set_message",
                community=None,
                arg="tenant $(username)",
                is_mod=True,
            ),
            {},
            http_client=None,
        )
    )
    assert fake_host.store[_message_key(TENANT_WIDE_SENTINEL)] == b"tenant $(username)"
    _run(dispatch(_sample_envelope("twitch", "lurk", community="comm-1"), {}, http_client=None))
    assert json.loads(fake_host.relay_calls[-1][1])["text"] == "viewer-1 is now lurking \U0001f440"


def test_config_keys_are_durable_and_lurk_state_expires(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "lurk"), {}, http_client=None))
    _run(
        dispatch(
            _sample_envelope("twitch", "config_set_message", arg="hi $(username)", is_mod=True),
            {},
            http_client=None,
        )
    )
    ttls = {call[1]: call[3] for call in fake_host.kv_calls if call[0] == "set"}
    assert ttls[_expected_state_key("comm-1", "viewer-1")] == 24 * 60 * 60
    assert ttls[_message_key("comm-1")] == 0


def test_missing_actor_still_replies_without_crashing(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "lurk", actor=None), {}, http_client=None))
    assert result.detail == "lurk"
    assert json.loads(fake_host.relay_calls[-1][1])["text"] == "someone is now lurking \U0001f440"
    assert _expected_state_key("comm-1", None) in fake_host.store


def test_template_length_boundary_is_inclusive_at_the_cap(fake_host: _FakeHost) -> None:
    assert _validate_template("x" * 200) is None
    assert _validate_template("x" * 201) is not None


def test_dispatch_result_reports_provider_and_detail(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("discord", "usage"), {}, http_client=None))
    assert (result.transport, result.detail) == ("discord", "usage")
    assert result.sub_type is None
    assert result.http_status is None
