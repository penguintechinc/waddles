"""Host-native tests for the `shoutout` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/fish/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors (extended with a `tier()` entry on the fake `flags`
module for the `ai` sub-module's license-gate tests).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.command import CommandSpec, ParsedCommand, parse_command
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

from app import (
    _AUTO_LIST_KEY,
    _TEMPLATE_KEY,
    DEFAULT_TEMPLATE,
    MAX_AUTO_LIST_SIZE,
    MAX_TARGET_LEN,
    MAX_TEMPLATE_LEN,
    _caller_role_signal,
    _map_parsed,
    _resolve,
    _target_pseudonym,
    _validate_target,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _expected_pseudonym(target: str) -> str:
    return hashlib.sha256(target.lower().encode()).hexdigest()


def _scoped(key: str, community: str = "comm-1") -> str:
    """`fake_host.store` is keyed by `community_kv`'s own `c:<community>:<key>` prefix."""
    return _scoped_key(community, key)


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
    """Fake WIT host: flags/kv/relay/log, with a real in-memory kv store + TTL ignored."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.kv_calls: list[tuple[Any, ...]] = []
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[str, str, str]] = []


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
    flag_enabled: bool = True,
    tier: str = "free",
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
        current = int(host.store.get(key, b"0").decode())
        new_value = current + delta
        host.store[key] = str(new_value).encode()
        return new_value

    flags_mod = types.SimpleNamespace(
        enabled=lambda key, default_value: flag_enabled, tier=lambda: tier
    )
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
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, kv=kv_mod, relay=relay_mod, log=log_mod
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
        app_id="waddles.core.example.shoutout",
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


def _last_reply(host: _FakeHost) -> str:
    _provider, message_json = host.relay_calls[-1]
    return json.loads(message_json)["text"]


# -- transform() parsing -------------------------------------------------------


@pytest.mark.parametrize("text", ["!so penguin", "!SO penguin", "  !so penguin  "])
def test_target_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "shoutout"
    assert result.payload["arg"] == "penguin"


def test_bare_so_with_no_target_is_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!so")))
    assert result is not None
    assert result.payload["command"] == "usage"
    assert "arg" not in result.payload


def test_set_template_matches_with_args(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!so set hi $(username)!")))
    assert result is not None
    assert result.payload["command"] == "config_set_template"
    assert result.payload["arg"] == "hi $(username)!"


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("!so enable auto", "config_enable_auto"),
        ("!so disable auto", "config_disable_auto"),
        ("!so enable ai", "config_enable_ai"),
        ("!so disable ai", "config_disable_ai"),
    ],
)
def test_submodule_toggles_match(text: str, command: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == command


@pytest.mark.parametrize(
    ("text", "command", "arg"),
    [
        ("!so auto add penguin", "auto_add", "penguin"),
        ("!so auto remove penguin", "auto_remove", "penguin"),
        ("!so auto list", "auto_list", None),
    ],
)
def test_auto_submodule_commands_match(
    text: str, command: str, arg: str | None, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == command
    assert result.payload.get("arg") == arg


@pytest.mark.parametrize(
    "text",
    [
        "!so auto list extra",
        "!so auto set 30",
        "!so ai add penguin",
        "!so auto",
        "!so ai",
        "!so enable foo",
        "!so disable foo",
        "!so reset",
        "!so penguin extra",
    ],
)
def test_unsupported_shapes_reply_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!social", "so penguin", "!sot", "hello", ""])
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
    assert _run(transform(_sample_event("!so penguin"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!so penguin", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!so penguin")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# -- _resolve() / _map_parsed() direct unit coverage ---------------------------


def test_resolve_empty_is_usage() -> None:
    assert _resolve("") == ("usage", None)


def test_resolve_bare_target() -> None:
    assert _resolve("penguin") == ("shoutout", "penguin")


def test_resolve_multiword_target_is_usage() -> None:
    assert _resolve("penguin extra") == ("usage", None)


def test_map_parsed_set_with_no_args_is_usage() -> None:
    parsed = ParsedCommand(command="so", sub_module=None, option="set", args=None)
    assert _map_parsed(parsed) == ("config_set_template", None)


def test_map_parsed_unknown_bare_option_is_usage() -> None:
    parsed = ParsedCommand(command="so", sub_module=None, option="reset", args=None)
    assert _map_parsed(parsed) == ("usage", None)


def test_map_parsed_auto_set_is_usage() -> None:
    spec = CommandSpec(name="so", sub_modules=frozenset({"auto", "ai"}))
    parsed = parse_command("!so auto set 30", spec)
    assert _map_parsed(parsed) == ("usage", None)


# -- _validate_target() ---------------------------------------------------------


def test_validate_target_strips_at_sign() -> None:
    target, error = _validate_target("@Penguin")
    assert target == "Penguin"
    assert error is None


def test_validate_target_rejects_empty() -> None:
    target, error = _validate_target("   ")
    assert target is None
    assert error is not None


def test_validate_target_rejects_too_long() -> None:
    target, error = _validate_target("p" * (MAX_TARGET_LEN + 1))
    assert target is None
    assert error is not None


def test_validate_target_rejects_bad_chars() -> None:
    target, error = _validate_target("penguin!")
    assert target is None
    assert error is not None


def test_target_pseudonym_is_case_insensitive() -> None:
    assert _target_pseudonym("Penguin") == _target_pseudonym("penguin")
    assert _target_pseudonym("penguin") == _expected_pseudonym("penguin")


# -- dispatch(): usage / permission gating --------------------------------------


def test_dispatch_usage_requires_no_permission(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "usage"), {}, http_client=None))
    assert result.detail == "usage"
    assert "Usage:" in _last_reply(fake_host)


@pytest.mark.parametrize(
    "command",
    [
        "shoutout",
        "config_set_template",
        "config_enable_auto",
        "config_disable_auto",
        "auto_add",
        "auto_remove",
        "auto_list",
        "config_enable_ai",
        "config_disable_ai",
    ],
)
def test_dispatch_denies_without_mod_or_broadcaster_signal(
    command: str, fake_host: _FakeHost
) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", command, arg="penguin"),
            {},
            http_client=None,
        )
    )
    assert result.detail == f"{command}:denied"
    assert "moderators/broadcasters" in _last_reply(fake_host)


def test_dispatch_denies_when_role_signal_absent_entirely(fake_host: _FakeHost) -> None:
    """Neither `is_mod` nor `is_broadcaster` present (e.g. Discord today) -- fail closed."""
    result = _run(
        dispatch(_sample_envelope("discord", "shoutout", arg="penguin"), {}, http_client=None)
    )
    assert result.detail == "shoutout:denied"


def test_dispatch_allows_moderator(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "shoutout", arg="penguin", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert result.detail == "shoutout"


def test_dispatch_allows_broadcaster(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope(
                "twitch", "shoutout", arg="penguin", is_mod=False, is_broadcaster=True
            ),
            {},
            http_client=None,
        )
    )
    assert result.detail == "shoutout"


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "shoutout", community=None, arg="penguin")
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv_calls == []


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.shoutout",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={"command": "shoutout", "channel_id": None},
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_on_unrecognized_command(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "not-a-real-command")
    with pytest.raises(ValueError, match="unrecognized shoutout command"):
        _run(dispatch(envelope, {}, http_client=None))


# -- dispatch(): posting a shoutout ----------------------------------------------


def test_shoutout_uses_default_template(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "shoutout", arg="penguin", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert result.detail == "shoutout"
    assert "penguin" in _last_reply(fake_host)


def test_shoutout_uses_configured_template(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_TEMPLATE_KEY)] = b"GO $(username) GO"
    _run(
        dispatch(
            _sample_envelope("twitch", "shoutout", arg="penguin", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert _last_reply(fake_host) == "GO penguin GO"


def test_shoutout_missing_target_is_usage_text(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(_sample_envelope("twitch", "shoutout", is_mod=True), {}, http_client=None)
    )
    assert result.detail == "shoutout"
    assert "Usage:" in _last_reply(fake_host)


def test_shoutout_rejects_invalid_target(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "shoutout", arg="bad name!", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert "only contain" in _last_reply(fake_host)


def test_shoutout_with_ai_enabled_still_uses_template_and_logs_pending(
    fake_host: _FakeHost,
) -> None:
    fake_host.store[_scoped("submodule:shoutout:ai")] = b"1"
    _run(
        dispatch(
            _sample_envelope("twitch", "shoutout", arg="penguin", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert "penguin" in _last_reply(fake_host)
    pending_logs = [
        m for _lvl, m, _f in fake_host.log_calls if m == "shoutout.ai_requested_but_pending"
    ]
    assert pending_logs


# -- dispatch(): template config --------------------------------------------------


def test_set_template_persists_and_confirms(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope(
                "twitch", "config_set_template", arg="hey $(username)!", is_mod=True
            ),
            {},
            http_client=None,
        )
    )
    assert result.detail == "config_set_template"
    assert fake_host.store[_scoped(_TEMPLATE_KEY)] == b"hey $(username)!"
    assert "updated" in _last_reply(fake_host)


def test_set_template_rejects_unknown_placeholder(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope(
                "twitch", "config_set_template", arg="hi $(bogus)", is_mod=True
            ),
            {},
            http_client=None,
        )
    )
    assert "unknown placeholder" in _last_reply(fake_host)
    assert _scoped(_TEMPLATE_KEY) not in fake_host.store


def test_set_template_rejects_too_long(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope(
                "twitch", "config_set_template", arg="x" * (MAX_TEMPLATE_LEN + 1), is_mod=True
            ),
            {},
            http_client=None,
        )
    )
    assert "characters or fewer" in _last_reply(fake_host)


def test_set_template_rejects_empty(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "config_set_template", is_mod=True), {}, http_client=None
        )
    )
    assert result.detail == "config_set_template"
    assert "Usage:" in _last_reply(fake_host)


def test_default_template_constant_has_username_placeholder() -> None:
    assert "$(username)" in DEFAULT_TEMPLATE


# -- dispatch(): auto sub-module toggle + list CRUD ------------------------------


def test_enable_then_disable_auto(fake_host: _FakeHost) -> None:
    enable = _run(
        dispatch(
            _sample_envelope("twitch", "config_enable_auto", is_mod=True), {}, http_client=None
        )
    )
    assert enable.detail == "config_enable_auto"
    assert fake_host.store[_scoped("submodule:shoutout:auto")] == b"1"

    disable = _run(
        dispatch(
            _sample_envelope("twitch", "config_disable_auto", is_mod=True), {}, http_client=None
        )
    )
    assert disable.detail == "config_disable_auto"
    assert _scoped("submodule:shoutout:auto") not in fake_host.store


def test_auto_commands_require_enabled_first(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "auto_add", arg="penguin", is_mod=True), {}, http_client=None
        )
    )
    assert result.detail == "auto_add"
    assert "disabled" in _last_reply(fake_host)
    assert _scoped(_AUTO_LIST_KEY) not in fake_host.store


def _enable_auto(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "config_enable_auto", is_mod=True), {}, http_client=None
        )
    )


def test_auto_add_then_list_reports_count(fake_host: _FakeHost) -> None:
    _enable_auto(fake_host)
    _run(
        dispatch(
            _sample_envelope("twitch", "auto_add", arg="penguin", is_mod=True), {}, http_client=None
        )
    )
    result = _run(
        dispatch(_sample_envelope("twitch", "auto_list", is_mod=True), {}, http_client=None)
    )
    assert result.detail == "auto_list"
    assert "1 user" in _last_reply(fake_host)


def test_auto_list_empty_before_any_adds(fake_host: _FakeHost) -> None:
    _enable_auto(fake_host)
    _run(dispatch(_sample_envelope("twitch", "auto_list", is_mod=True), {}, http_client=None))
    assert "empty" in _last_reply(fake_host)


def test_auto_add_is_idempotent(fake_host: _FakeHost) -> None:
    _enable_auto(fake_host)
    _run(
        dispatch(
            _sample_envelope("twitch", "auto_add", arg="penguin", is_mod=True), {}, http_client=None
        )
    )
    _run(
        dispatch(
            _sample_envelope("twitch", "auto_add", arg="penguin", is_mod=True), {}, http_client=None
        )
    )
    assert "already" in _last_reply(fake_host)
    stored = json.loads(fake_host.store[_scoped(_AUTO_LIST_KEY)].decode())
    assert stored == [_expected_pseudonym("penguin")]


def test_auto_add_case_insensitive_dedup(fake_host: _FakeHost) -> None:
    _enable_auto(fake_host)
    _run(
        dispatch(
            _sample_envelope("twitch", "auto_add", arg="Penguin", is_mod=True), {}, http_client=None
        )
    )
    result = _run(
        dispatch(
            _sample_envelope("twitch", "auto_add", arg="penguin", is_mod=True), {}, http_client=None
        )
    )
    assert "already" in _last_reply(fake_host)
    assert result.detail == "auto_add"


def test_auto_remove_present_and_absent(fake_host: _FakeHost) -> None:
    _enable_auto(fake_host)
    _run(
        dispatch(
            _sample_envelope("twitch", "auto_add", arg="penguin", is_mod=True), {}, http_client=None
        )
    )
    removed = _run(
        dispatch(
            _sample_envelope("twitch", "auto_remove", arg="penguin", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert "removed" in _last_reply(fake_host)
    assert removed.detail == "auto_remove"

    _run(
        dispatch(
            _sample_envelope("twitch", "auto_remove", arg="penguin", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert "not on the" in _last_reply(fake_host)


def test_auto_add_rejects_invalid_target(fake_host: _FakeHost) -> None:
    _enable_auto(fake_host)
    _run(
        dispatch(
            _sample_envelope("twitch", "auto_add", arg="bad name!", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert "only contain" in _last_reply(fake_host)


def test_auto_add_requires_arg(fake_host: _FakeHost) -> None:
    _enable_auto(fake_host)
    result = _run(
        dispatch(_sample_envelope("twitch", "auto_add", is_mod=True), {}, http_client=None)
    )
    assert result.detail == "auto_add"
    assert "Usage:" in _last_reply(fake_host)


def test_auto_list_is_full_at_capacity(fake_host: _FakeHost) -> None:
    _enable_auto(fake_host)
    full_list = [hashlib.sha256(f"user{i}".encode()).hexdigest() for i in range(MAX_AUTO_LIST_SIZE)]
    fake_host.store[_scoped(_AUTO_LIST_KEY)] = json.dumps(full_list).encode()
    result = _run(
        dispatch(
            _sample_envelope("twitch", "auto_add", arg="newuser", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert result.detail == "auto_add"
    assert "full" in _last_reply(fake_host)


def test_auto_list_scoped_per_community(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope(
                "twitch", "config_enable_auto", community="comm-1", is_mod=True
            ),
            {},
            http_client=None,
        )
    )
    _run(
        dispatch(
            _sample_envelope(
                "twitch", "auto_add", arg="penguin", community="comm-1", is_mod=True
            ),
            {},
            http_client=None,
        )
    )
    # comm-2 never enabled auto -- its own gate state is independent.
    _run(
        dispatch(
            _sample_envelope("twitch", "auto_list", community="comm-2", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert "disabled" in _last_reply(fake_host)


def test_auto_remove_requires_enabled_first(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "auto_remove", arg="penguin", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert result.detail == "auto_remove"
    assert "disabled" in _last_reply(fake_host)


def test_auto_remove_requires_arg(fake_host: _FakeHost) -> None:
    _enable_auto(fake_host)
    result = _run(
        dispatch(_sample_envelope("twitch", "auto_remove", is_mod=True), {}, http_client=None)
    )
    assert result.detail == "auto_remove"
    assert "Usage:" in _last_reply(fake_host)


def test_auto_remove_rejects_invalid_target(fake_host: _FakeHost) -> None:
    _enable_auto(fake_host)
    _run(
        dispatch(
            _sample_envelope("twitch", "auto_remove", arg="bad name!", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert "only contain" in _last_reply(fake_host)


def test_template_corrupt_bytes_fall_back_to_default(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_TEMPLATE_KEY)] = b"\xff\xfe not valid utf-8"
    result = _run(
        dispatch(
            _sample_envelope("twitch", "shoutout", arg="penguin", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert result.detail == "shoutout"
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "shoutout.template_corrupt"]
    assert corrupt_logs
    assert "penguin" in _last_reply(fake_host)


def test_auto_list_non_string_array_fails_loud(fake_host: _FakeHost) -> None:
    _enable_auto(fake_host)
    fake_host.store[_scoped(_AUTO_LIST_KEY)] = json.dumps([1, 2, 3]).encode()
    with pytest.raises(RuntimeError, match="corrupt"):
        _run(
            dispatch(_sample_envelope("twitch", "auto_list", is_mod=True), {}, http_client=None)
        )


def test_auto_list_corrupt_state_fails_loud(fake_host: _FakeHost) -> None:
    _enable_auto(fake_host)
    fake_host.store[_scoped(_AUTO_LIST_KEY)] = b"not-json"
    with pytest.raises(RuntimeError, match="corrupt"):
        _run(
            dispatch(
                _sample_envelope("twitch", "auto_list", is_mod=True), {}, http_client=None
            )
        )
    assert "corrupted" in _last_reply(fake_host)


# -- dispatch(): ai sub-module license gating ------------------------------------


def test_enable_ai_denied_on_free_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, tier="free")
    result = _run(
        dispatch(_sample_envelope("twitch", "config_enable_ai", is_mod=True), {}, http_client=None)
    )
    assert result.detail == "config_enable_ai"
    assert "professional" in _last_reply(host)
    assert _scoped("submodule:shoutout:ai") not in host.store


@pytest.mark.parametrize("tier", ["professional", "enterprise"])
def test_enable_ai_allowed_on_professional_or_above(
    tier: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, tier=tier)
    result = _run(
        dispatch(_sample_envelope("twitch", "config_enable_ai", is_mod=True), {}, http_client=None)
    )
    assert result.detail == "config_enable_ai"
    assert host.store[_scoped("submodule:shoutout:ai")] == b"1"
    assert "enabled" in _last_reply(host)


def test_disable_ai_requires_no_license(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, tier="enterprise")
    _run(
        dispatch(
            _sample_envelope("twitch", "config_enable_ai", is_mod=True), {}, http_client=None
        )
    )
    _install(monkeypatch, host, tier="free")  # tenant downgraded after enabling
    result = _run(
        dispatch(_sample_envelope("twitch", "config_disable_ai", is_mod=True), {}, http_client=None)
    )
    assert result.detail == "config_disable_ai"
    assert _scoped("submodule:shoutout:ai") not in host.store


def test_tier_binding_unavailable_degrades_to_free_and_denies(fake_host: _FakeHost) -> None:
    """No `flags` import at all on `wit_world.imports` -- same as a stale/unwizened component.

    `tier_at_least()` degrades to `"free"` (`feature_flags.tier`'s own
    documented behavior, see `sdk/waddle-sdk/tests/test_feature_flags.py`),
    so `enable ai` must still fail closed rather than crash.
    """
    fake_wit_world = sys.modules["wit_world"]
    del fake_wit_world.imports.flags  # type: ignore[attr-defined]
    _run(
        dispatch(
            _sample_envelope("twitch", "config_enable_ai", is_mod=True), {}, http_client=None
        )
    )
    assert "professional" in _last_reply(fake_host)
    assert _scoped("submodule:shoutout:ai") not in fake_host.store


# -- dispatch(): kv backend failures ----------------------------------------------


def test_kv_get_failure_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_get_raises=RuntimeError("backend down"))
    with pytest.raises(RuntimeError, match="shoutout kv get failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "shoutout", arg="penguin", is_mod=True),
                {},
                http_client=None,
            )
        )
    assert "unavailable" in _last_reply(host)


def test_kv_set_failure_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_set_raises=RuntimeError("backend down"))
    with pytest.raises(RuntimeError, match="shoutout kv set failed"):
        _run(
            dispatch(
                _sample_envelope(
                    "twitch", "config_set_template", arg="hi $(username)", is_mod=True
                ),
                {},
                http_client=None,
            )
        )
    assert "unavailable" in _last_reply(host)


# -- _caller_role_signal() direct unit coverage -----------------------------------


def test_caller_role_signal_none_when_absent() -> None:
    assert _caller_role_signal({}) is None


def test_caller_role_signal_true_for_mod() -> None:
    assert _caller_role_signal({"is_mod": True, "is_broadcaster": False}) is True


def test_caller_role_signal_false_for_neither() -> None:
    assert _caller_role_signal({"is_mod": False, "is_broadcaster": False}) is False
