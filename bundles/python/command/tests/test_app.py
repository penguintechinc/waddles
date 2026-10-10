"""Host-native tests for the `command` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/lurk/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors (fake `kv`/`relay`/`flags`/`log`), extended with a real
dict-backed `kv.get`/`set`/`delete` (not a scripted sequence) since this bundle needs genuine
read-modify-write round trips for its registry/timer JSON blobs.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest
from waddle_sdk.flask_core.bundle_runtime import bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.kv import validate_key

from app import (
    _dispatch_list,
    _dispatch_remove,
    _dispatch_set,
    _dispatch_timer_set,
    _dispatch_timer_toggle,
    dispatch,
    transform,
)


def _run(coro):
    return asyncio.run(coro)


def _event(
    text: str,
    *,
    actor: str | None = "viewer-1",
    channel_id: str | None = "12345",
    is_mod: bool = False,
    is_broadcaster: bool = False,
    platform: str = "twitch",
) -> PlatformEvent:
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor,
        payload={
            "text": text,
            "channel_id": channel_id,
            "is_mod": is_mod,
            "is_broadcaster": is_broadcaster,
        },
        occurred_at="2026-10-05T00:00:00.000Z",
    )


def _envelope(
    event: PlatformEvent, *, community: str | None = "comm-1", tenant: str = "tenant-1"
) -> StageEnvelope:
    return StageEnvelope(
        tenant=tenant,
        community=community,
        app_id="waddles.core.example.command",
        stage="action",
        event=event,
        ts="2026-10-05T00:00:00.000Z",
    )


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch):
    """Fake WIT host: `flags.enabled` True, a dict-backed `kv`, call-capturing `relay`/`log`."""
    kv_store: dict[str, bytes] = {}
    relay_calls: list[tuple[str, str]] = []
    log_calls: list[tuple[int, str, str]] = []

    def _get(key: str):
        # regression: gh-631 -- validates exactly like the real `bundle_host_kv` host
        # capability (`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`).
        validate_key(key)
        return kv_store.get(key)

    def _set(key: str, value, ttl) -> None:
        validate_key(key)
        kv_store[key] = bytes(value)

    def _delete(key: str) -> None:
        validate_key(key)
        kv_store.pop(key, None)

    def _increment(key: str, delta: int, ttl: int) -> int:
        validate_key(key)
        return 0

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: True)
    kv_mod = types.SimpleNamespace(get=_get, set=_set, delete=_delete, increment=_increment)
    relay_mod = types.SimpleNamespace(
        push=lambda provider, msg: relay_calls.append((provider, msg))
    )
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: log_calls.append((lvl, msg, fields_json)),
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, kv=kv_mod, relay=relay_mod, log=log_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return types.SimpleNamespace(
        kv_store=kv_store, relay_calls=relay_calls, log_calls=log_calls, wit_world=fake_wit_world
    )


def _ctx(community: str | None):
    return bundle_context(
        tenant="tenant-1", community=community, app_id="waddles.core.example.command"
    )


def _seed_registry(fake_host, community: str, registry: dict[str, str]) -> None:
    fake_host.kv_store[f"command.registry.{community}"] = json.dumps(registry).encode()


def _seed_timers(fake_host, community: str, timers: dict[str, dict]) -> None:
    fake_host.kv_store[f"command.timers.{community}"] = json.dumps(timers).encode()


# ---------------------------------------------------------------------------
# transform(): non-matching / disabled-flag / usage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["hello", "count", "", "   "])
def test_non_bang_text_produces_no_reply(text: str, fake_host) -> None:
    assert _run(transform(_event(text))) is None


def test_non_chat_payload_is_ignored_rather_than_erroring() -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_every_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: False)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(flags=flags_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert _run(transform(_event("!command set !greet hi $(username)"))) is None


def test_bare_command_replies_with_usage(fake_host) -> None:
    result = _run(transform(_event("!command")))
    assert result is not None
    assert result.payload["action"] == "reply"
    assert "Usage:" in result.payload["text"]


def test_unrecognized_command_subcommand_replies_with_usage(fake_host) -> None:
    result = _run(transform(_event("!command bogus")))
    assert result is not None
    assert "Usage:" in result.payload["text"]


# ---------------------------------------------------------------------------
# !command set -- permission, validation, storage
# ---------------------------------------------------------------------------


def test_set_denied_for_non_privileged_caller(fake_host) -> None:
    event = _event("!command set !greet hi there", is_mod=False, is_broadcaster=False)
    result = _run(transform(event))
    assert result is not None
    assert result.payload == {
        "action": "reply",
        "text": "only broadcasters/mods can manage custom commands",
        "channel_id": "12345",
    }


@pytest.mark.parametrize("flag", ["is_mod", "is_broadcaster"])
def test_set_allowed_for_mod_or_broadcaster(flag: str, fake_host) -> None:
    result = _run(transform(_event("!command set !greet hi there", **{flag: True})))
    assert result is not None
    assert result.payload["action"] == "set_command"
    assert result.payload["name"] == "greet"
    assert result.payload["text"] == "hi there"


def test_set_rejects_invalid_name(fake_host) -> None:
    result = _run(transform(_event("!command set !bad!name hi", is_mod=True)))
    assert result is not None
    assert "letters, numbers" in result.payload["text"]


def test_set_rejects_reserved_name(fake_host) -> None:
    result = _run(transform(_event("!command set !command hi", is_mod=True)))
    assert result is not None
    assert "letters, numbers" in result.payload["text"]


def test_set_missing_text_replies_with_usage(fake_host) -> None:
    result = _run(transform(_event("!command set !greet", is_mod=True)))
    assert result is not None
    assert "Usage: !command set" in result.payload["text"]


def test_set_command_stores_and_dispatch_relays_confirmation(fake_host) -> None:
    transformed = _run(transform(_event("!command set !greet hi $(username)", is_mod=True)))
    assert transformed is not None
    envelope = _envelope(transformed)

    result = _run(dispatch(envelope, {}, http_client=None))

    stored = json.loads(fake_host.kv_store["command.registry.comm-1"])
    assert stored == {"greet": "hi $(username)"}
    provider, message_json = fake_host.relay_calls[0]
    assert provider == "twitch"
    assert json.loads(message_json) == {"channel": "12345", "text": "command saved: !greet"}
    assert result.detail == "set_command"


def test_set_without_community_replies_requires_community(fake_host) -> None:
    transformed = _run(transform(_event("!command set !greet hi", is_mod=True)))
    assert transformed is not None
    envelope = _envelope(transformed, community=None)

    _run(dispatch(envelope, {}, http_client=None))

    _, message_json = fake_host.relay_calls[0]
    assert "community context" in json.loads(message_json)["text"]
    assert "command.registry" not in "".join(fake_host.kv_store.keys())


# ---------------------------------------------------------------------------
# !command remove
# ---------------------------------------------------------------------------


def test_remove_denied_for_non_privileged_caller(fake_host) -> None:
    result = _run(transform(_event("!command remove !greet")))
    assert result is not None
    assert result.payload["text"] == "only broadcasters/mods can manage custom commands"


def test_remove_existing_command(fake_host) -> None:
    _seed_registry(fake_host, "comm-1", {"greet": "hi"})
    transformed = _run(transform(_event("!command remove !greet", is_mod=True)))
    assert transformed is not None
    assert transformed.payload == {
        "action": "remove_command",
        "name": "greet",
        "channel_id": "12345",
    }

    _run(dispatch(_envelope(transformed), {}, http_client=None))

    assert json.loads(fake_host.kv_store["command.registry.comm-1"]) == {}
    _, message_json = fake_host.relay_calls[0]
    assert json.loads(message_json)["text"] == "command removed: !greet"


def test_remove_nonexistent_command_replies_not_found(fake_host) -> None:
    transformed = _run(transform(_event("!command remove !ghost", is_mod=True)))
    assert transformed is not None
    _run(dispatch(_envelope(transformed), {}, http_client=None))

    _, message_json = fake_host.relay_calls[0]
    assert json.loads(message_json)["text"] == "no custom command named !ghost"


# ---------------------------------------------------------------------------
# !command list
# ---------------------------------------------------------------------------


def test_list_empty_registry(fake_host) -> None:
    transformed = _run(transform(_event("!command list")))
    assert transformed is not None
    _run(dispatch(_envelope(transformed), {}, http_client=None))

    _, message_json = fake_host.relay_calls[0]
    assert "no custom commands set" in json.loads(message_json)["text"]


def test_list_shows_sorted_entries(fake_host) -> None:
    _seed_registry(fake_host, "comm-1", {"zeta": "z", "alpha": "a"})
    transformed = _run(transform(_event("!command list")))
    assert transformed is not None
    _run(dispatch(_envelope(transformed), {}, http_client=None))

    _, message_json = fake_host.relay_calls[0]
    text = json.loads(message_json)["text"]
    assert text == "custom commands: !alpha, !zeta"


def test_list_does_not_require_privilege(fake_host) -> None:
    """Unlike `set`/`remove`/`timer`, `list` is read-only and open to any caller."""
    result = _run(transform(_event("!command list", is_mod=False, is_broadcaster=False)))
    assert result is not None
    assert result.payload["action"] == "list_commands"


# ---------------------------------------------------------------------------
# dynamic `!<name>` invocation -- the one path where transform() reads kv
# ---------------------------------------------------------------------------


def test_created_command_responds_with_stored_text(fake_host) -> None:
    _seed_registry(fake_host, "comm-1", {"greet": "hi there!"})
    with _ctx("comm-1"):
        result = _run(transform(_event("!greet")))
    assert result is not None
    assert result.payload == {"action": "reply", "text": "hi there!", "channel_id": "12345"}


def test_created_command_substitutes_username_placeholder(fake_host) -> None:
    _seed_registry(fake_host, "comm-1", {"greet": "hi $(username), welcome!"})
    with _ctx("comm-1"):
        result = _run(transform(_event("!greet", actor="penguin42")))
    assert result is not None
    assert result.payload["text"] == "hi penguin42, welcome!"


def test_unknown_token_returns_none(fake_host) -> None:
    _seed_registry(fake_host, "comm-1", {"greet": "hi"})
    with _ctx("comm-1"):
        assert _run(transform(_event("!nosuchcommand"))) is None


def test_dynamic_lookup_without_community_returns_none(fake_host) -> None:
    _seed_registry(fake_host, "comm-1", {"greet": "hi"})
    with _ctx(None):
        assert _run(transform(_event("!greet"))) is None


# ---------------------------------------------------------------------------
# !command timer
# ---------------------------------------------------------------------------


def test_timer_set_denied_for_non_privileged_caller(fake_host) -> None:
    result = _run(transform(_event("!command timer !greet set 5m")))
    assert result is not None
    assert result.payload["text"] == "only broadcasters/mods can manage custom commands"


def test_timer_set_invalid_interval_replies_with_error(fake_host) -> None:
    result = _run(transform(_event("!command timer !greet set 5x", is_mod=True)))
    assert result is not None
    assert result.payload["action"] == "reply"
    assert "invalid interval" in result.payload["text"]


@pytest.mark.parametrize(
    ("raw", "expected_seconds"), [("30s", 30), ("5m", 300), ("1h", 3600)]
)
def test_timer_set_parses_supported_units(raw: str, expected_seconds: int, fake_host) -> None:
    result = _run(transform(_event(f"!command timer !greet set {raw}", is_mod=True)))
    assert result is not None
    assert result.payload["action"] == "timer_set"
    assert result.payload["interval_seconds"] == expected_seconds


@pytest.mark.parametrize("raw", ["5", "5x", "0s", "999h", "5s5m"])
def test_timer_set_rejects_malformed_or_out_of_range_intervals(raw: str, fake_host) -> None:
    result = _run(transform(_event(f"!command timer !greet set {raw}", is_mod=True)))
    assert result is not None
    assert "invalid interval" in result.payload["text"]


def test_timer_set_requires_existing_command(fake_host) -> None:
    transformed = _run(transform(_event("!command timer !greet set 5m", is_mod=True)))
    assert transformed is not None
    _run(dispatch(_envelope(transformed), {}, http_client=None))

    _, message_json = fake_host.relay_calls[0]
    assert "set it first" in json.loads(message_json)["text"]
    assert "command.timers" not in "".join(fake_host.kv_store.keys())


def test_timer_set_stores_interval_and_mentions_pending_scheduler(fake_host) -> None:
    _seed_registry(fake_host, "comm-1", {"greet": "hi"})
    transformed = _run(transform(_event("!command timer !greet set 5m", is_mod=True)))
    assert transformed is not None

    _run(dispatch(_envelope(transformed), {}, http_client=None))

    stored = json.loads(fake_host.kv_store["command.timers.comm-1"])
    assert stored == {"greet": {"interval_seconds": 300, "enabled": False}}
    _, message_json = fake_host.relay_calls[0]
    text = json.loads(message_json)["text"]
    assert "every 300s" in text
    assert "gh-613" in text  # loud, not silent -- see module docstring


def test_timer_enable_requires_existing_timer(fake_host) -> None:
    transformed = _run(transform(_event("!command timer !greet enable", is_mod=True)))
    assert transformed is not None
    _run(dispatch(_envelope(transformed), {}, http_client=None))

    _, message_json = fake_host.relay_calls[0]
    assert "no timer configured" in json.loads(message_json)["text"]


def test_timer_enable_and_disable_toggle_stored_state(fake_host) -> None:
    _seed_registry(fake_host, "comm-1", {"greet": "hi"})
    _seed_timers(fake_host, "comm-1", {"greet": {"interval_seconds": 300, "enabled": False}})

    enable_result = _run(transform(_event("!command timer !greet enable", is_mod=True)))
    assert enable_result is not None
    _run(dispatch(_envelope(enable_result), {}, http_client=None))
    assert json.loads(fake_host.kv_store["command.timers.comm-1"])["greet"]["enabled"] is True

    disable_result = _run(transform(_event("!command timer !greet disable", is_mod=True)))
    assert disable_result is not None
    _run(dispatch(_envelope(disable_result), {}, http_client=None))
    assert json.loads(fake_host.kv_store["command.timers.comm-1"])["greet"]["enabled"] is False


# ---------------------------------------------------------------------------
# kv failure -- fail loud, never silent
# ---------------------------------------------------------------------------


def test_dispatch_kv_error_on_list_replies_loudly_and_logs(fake_host) -> None:
    def _boom(key: str) -> bytes:
        raise RuntimeError("kv backend unavailable")

    fake_host.wit_world.imports.kv.get = _boom

    transformed = _run(transform(_event("!command list")))
    assert transformed is not None
    _run(dispatch(_envelope(transformed), {}, http_client=None))

    _, message_json = fake_host.relay_calls[0]
    assert "temporarily unavailable" in json.loads(message_json)["text"]
    assert any("kv_error" in msg for _lvl, msg, _fields in fake_host.log_calls)


def test_transform_kv_error_on_dynamic_lookup_replies_loudly_not_none(fake_host) -> None:
    def _boom(key: str) -> bytes:
        raise RuntimeError("kv backend unavailable")

    fake_host.wit_world.imports.kv.get = _boom

    with _ctx("comm-1"):
        result = _run(transform(_event("!greet")))

    assert result is not None  # never silently drop a backend failure
    assert "temporarily unavailable" in result.payload["text"]
    assert any("kv_error" in msg for _lvl, msg, _fields in fake_host.log_calls)


# ---------------------------------------------------------------------------
# dispatch(): defensive raises
# ---------------------------------------------------------------------------


def test_dispatch_raises_when_channel_id_is_missing(fake_host) -> None:
    event = _event("!command list", channel_id=None)
    event.payload["action"] = "list_commands"
    envelope = _envelope(event)

    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_on_unrecognized_action(fake_host) -> None:
    event = _event("!command list")
    event.payload = {"action": "not_a_real_action", "channel_id": "12345"}
    envelope = _envelope(event)

    with pytest.raises(ValueError, match="unrecognized command action"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_plain_reply_action_relays_text_unchanged(fake_host) -> None:
    """A denied-permission `transform()` reply still flows through `dispatch()` to relay."""
    transformed = _run(transform(_event("!command set !greet hi")))
    assert transformed is not None
    assert transformed.payload["action"] == "reply"

    _run(dispatch(_envelope(transformed), {}, http_client=None))

    _, message_json = fake_host.relay_calls[0]
    assert json.loads(message_json)["text"] == "only broadcasters/mods can manage custom commands"


# ---------------------------------------------------------------------------
# additional subcommand-parsing / community-guard branches
# ---------------------------------------------------------------------------


def test_bare_bang_alone_produces_no_reply(fake_host) -> None:
    assert _run(transform(_event("!"))) is None


def test_remove_with_no_name_replies_with_usage(fake_host) -> None:
    result = _run(transform(_event("!command remove", is_mod=True)))
    assert result is not None
    assert result.payload["text"] == "Usage: !command remove !<name>"


@pytest.mark.parametrize(
    "text",
    ["!command timer", "!command timer !greet", "!command timer !greet set"],
)
def test_timer_incomplete_args_reply_with_usage(text: str, fake_host) -> None:
    result = _run(transform(_event(text, is_mod=True)))
    assert result is not None
    assert "Usage: !command timer" in result.payload["text"]


def test_timer_invalid_name_replies_with_error(fake_host) -> None:
    result = _run(transform(_event("!command timer !bad!name set 5m", is_mod=True)))
    assert result is not None
    assert "letters, numbers" in result.payload["text"]


def test_timer_unrecognized_subcommand_replies_with_usage(fake_host) -> None:
    result = _run(transform(_event("!command timer !greet bogus", is_mod=True)))
    assert result is not None
    assert "Usage: !command timer" in result.payload["text"]


def test_dispatch_list_without_community(fake_host) -> None:
    result = _run(_dispatch_list(None))
    assert "community context" in result


def test_dispatch_set_without_community(fake_host) -> None:
    assert "community context" in _run(_dispatch_set(None, "greet", "hi"))


def test_dispatch_remove_without_community(fake_host) -> None:
    assert "community context" in _run(_dispatch_remove(None, "greet"))


def test_dispatch_timer_set_without_community(fake_host) -> None:
    assert "community context" in _run(_dispatch_timer_set(None, "greet", 300))


def test_dispatch_timer_toggle_without_community(fake_host) -> None:
    result = _run(_dispatch_timer_toggle(None, "greet", enabled=True))
    assert "community context" in result


def test_dispatch_set_rejects_non_string_name(fake_host) -> None:
    result = _run(_dispatch_set("comm-1", None, "hi"))
    assert result == "Usage: !command set !<name> <message text>"


def test_dispatch_remove_rejects_non_string_name(fake_host) -> None:
    assert _run(_dispatch_remove("comm-1", None)) == "Usage: !command remove !<name>"


def test_dispatch_timer_set_rejects_non_int_interval(fake_host) -> None:
    result = _run(_dispatch_timer_set("comm-1", "greet", "not-an-int"))
    assert "Usage: !command timer" in result


def test_dispatch_timer_toggle_rejects_non_string_name(fake_host) -> None:
    result = _run(_dispatch_timer_toggle("comm-1", None, enabled=True))
    assert "Usage: !command timer" in result


# regression: gh-631 -- `_registry_key`/`_timers_key` originally used `:`
# (`"command:registry:{community}"`, `"command:timers:{community}"`), rejected by the real
# `kv` host capability (`core/bundle_host_kv/src/scope.rs` reserves `:` as its own namespace
# separator). Every real `kv` call this bundle made failed in production with
# `kv.error::backend`, invisible to this whole test suite because the old fake `kv` host
# accepted any key -- `fake_host` now validates like the real host (see the fixture above).
def test_kv_key_helpers_contain_no_colon() -> None:
    from app import _registry_key, _timers_key

    assert ":" not in _registry_key("comm-1")
    assert ":" not in _timers_key("comm-1")


# ---------------------------------------------------------------------------
# mod-gate fail-closed: absent role fields (Discord today) must deny, never allow
# ---------------------------------------------------------------------------


def _no_role_event(text: str) -> PlatformEvent:
    """A Discord-shaped event: no `is_mod`/`is_broadcaster` keys at all."""
    event = _event(text)
    del event.payload["is_mod"]
    del event.payload["is_broadcaster"]
    return event


@pytest.mark.parametrize(
    "text",
    [
        "!command set !greet hi",
        "!command remove !greet",
        "!command timer !greet set 5m",
        "!command timer !greet enable",
        "!command timer !greet disable",
    ],
)
def test_every_management_write_is_denied_when_role_fields_are_absent(
    text: str, fake_host
) -> None:
    result = _run(transform(_no_role_event(text)))
    assert result is not None
    assert result.payload["action"] == "reply"
    assert result.payload["text"] == "only broadcasters/mods can manage custom commands"
    assert fake_host.kv_store == {}


def test_list_and_direct_invocation_stay_open_without_role_fields(fake_host) -> None:
    _seed_registry(fake_host, "comm-1", {"greet": "hi there"})
    listed = _run(transform(_no_role_event("!command list")))
    assert listed is not None and listed.payload["action"] == "list_commands"
    with _ctx("comm-1"):
        invoked = _run(transform(_no_role_event("!greet")))
    assert invoked is not None and invoked.payload["text"] == "hi there"


# ---------------------------------------------------------------------------
# remaining branches: invalid dynamic token, >15 list cap, kv failures on every write path
# ---------------------------------------------------------------------------


def test_dynamic_lookup_with_invalid_name_returns_none(fake_host) -> None:
    """A token that can never be a valid command name is 'not ours' -- no kv read at all."""
    with _ctx("comm-1"):
        assert _run(transform(_event("!hello!world"))) is None
        assert _run(transform(_event("!" + "x" * 40))) is None
    assert fake_host.kv_store == {}


def test_list_caps_display_at_fifteen_and_reports_the_remainder(fake_host) -> None:
    _seed_registry(fake_host, "comm-1", {f"cmd{i:02d}": "x" for i in range(18)})
    transformed = _run(transform(_event("!command list")))
    assert transformed is not None
    _run(dispatch(_envelope(transformed), {}, http_client=None))

    text = json.loads(fake_host.relay_calls[0][1])["text"]
    assert text.startswith("custom commands: !cmd00, !cmd01")
    assert "!cmd14" in text and "!cmd15" not in text
    assert text.endswith("…and 3 more")


def _fail_kv(fake_host, op: str) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("kv backend unavailable")

    setattr(fake_host.wit_world.imports.kv, op, _boom)


@pytest.mark.parametrize(
    ("action", "payload", "seed"),
    [
        ("set_command", {"name": "greet", "text": "hi"}, None),
        ("remove_command", {"name": "greet"}, {"greet": "hi"}),
        ("timer_set", {"name": "greet", "interval_seconds": 300}, {"greet": "hi"}),
    ],
)
def test_dispatch_kv_failure_on_every_registry_write_path_is_loud(
    action: str, payload: dict, seed: dict | None, fake_host
) -> None:
    if seed is not None:
        _seed_registry(fake_host, "comm-1", seed)
    _fail_kv(fake_host, "set")
    event = _event("!command x", is_mod=True)
    event.payload = {"action": action, "channel_id": "12345", **payload}
    _run(dispatch(_envelope(event), {}, http_client=None))

    assert "temporarily unavailable" in json.loads(fake_host.relay_calls[-1][1])["text"]
    assert any("dispatch.kv_error" in msg for _lvl, msg, _f in fake_host.log_calls)


def test_dispatch_kv_failure_on_registry_read_during_set_is_loud(fake_host) -> None:
    _fail_kv(fake_host, "get")
    event = _event("!command x", is_mod=True)
    event.payload = {"action": "set_command", "channel_id": "12345", "name": "g", "text": "hi"}
    _run(dispatch(_envelope(event), {}, http_client=None))
    assert "temporarily unavailable" in json.loads(fake_host.relay_calls[-1][1])["text"]
    assert fake_host.kv_store == {}


@pytest.mark.parametrize("action", ["remove_command", "timer_set"])
def test_dispatch_kv_failure_on_registry_read_is_loud(action: str, fake_host) -> None:
    _fail_kv(fake_host, "get")
    event = _event("!command x", is_mod=True)
    event.payload = {
        "action": action,
        "channel_id": "12345",
        "name": "greet",
        "interval_seconds": 300,
    }
    _run(dispatch(_envelope(event), {}, http_client=None))
    assert "temporarily unavailable" in json.loads(fake_host.relay_calls[-1][1])["text"]
    assert any("dispatch.kv_error" in msg for _lvl, msg, _f in fake_host.log_calls)


@pytest.mark.parametrize("action", ["timer_enable", "timer_disable"])
def test_dispatch_kv_failure_on_timer_toggle_is_loud(action: str, fake_host) -> None:
    _seed_timers(fake_host, "comm-1", {"greet": {"interval_seconds": 300, "enabled": False}})
    _fail_kv(fake_host, "set")
    event = _event("!command x", is_mod=True)
    event.payload = {"action": action, "channel_id": "12345", "name": "greet"}
    _run(dispatch(_envelope(event), {}, http_client=None))

    assert "temporarily unavailable" in json.loads(fake_host.relay_calls[-1][1])["text"]
    assert any("timer_toggle" in fields for _lvl, _msg, fields in fake_host.log_calls)
    # the failed write never reached the store: the persisted flag is unchanged
    assert json.loads(fake_host.kv_store["command.timers.comm-1"])["greet"]["enabled"] is False


# ---------------------------------------------------------------------------
# corrupt-store fail-loud
# ---------------------------------------------------------------------------


def test_corrupt_registry_json_is_loud_on_list_lookup_and_never_overwritten(fake_host) -> None:
    """Invalid JSON in the registry blob: replies unavailable + ERROR log, bytes left intact."""
    fake_host.kv_store["command.registry.comm-1"] = b"\xff\xfe not json"

    listed = _run(transform(_event("!command list")))
    assert listed is not None
    _run(dispatch(_envelope(listed), {}, http_client=None))
    assert "temporarily unavailable" in json.loads(fake_host.relay_calls[-1][1])["text"]

    with _ctx("comm-1"):
        looked_up = _run(transform(_event("!greet")))
    assert looked_up is not None  # never silently dropped
    assert "temporarily unavailable" in looked_up.payload["text"]

    event = _event("!command x", is_mod=True)
    event.payload = {"action": "set_command", "channel_id": "12345", "name": "g", "text": "hi"}
    _run(dispatch(_envelope(event), {}, http_client=None))
    assert "temporarily unavailable" in json.loads(fake_host.relay_calls[-1][1])["text"]

    assert fake_host.kv_store["command.registry.comm-1"] == b"\xff\xfe not json"
    assert sum(1 for _lvl, m, _f in fake_host.log_calls if "kv_error" in m) >= 3


def test_corrupt_timers_json_is_loud_and_never_overwritten(fake_host) -> None:
    _seed_registry(fake_host, "comm-1", {"greet": "hi"})
    fake_host.kv_store["command.timers.comm-1"] = b"{broken"
    event = _event("!command x", is_mod=True)
    event.payload = {"action": "timer_enable", "channel_id": "12345", "name": "greet"}
    _run(dispatch(_envelope(event), {}, http_client=None))
    assert "temporarily unavailable" in json.loads(fake_host.relay_calls[-1][1])["text"]
    assert fake_host.kv_store["command.timers.comm-1"] == b"{broken"


# ---------------------------------------------------------------------------
# PII-free logs (gh-674)
# ---------------------------------------------------------------------------


def _assert_logs_pii_free(host, *sentinels: str) -> int:
    """Assert no sentinel appears in any recorded log call; return how many were examined."""
    assert host.log_calls, "no log calls recorded -- the PII check would pass vacuously"
    for _lvl, message, fields_json in host.log_calls:
        blob = f"{message} {fields_json}".lower()
        for sentinel in sentinels:
            assert sentinel.lower() not in blob
    return len(host.log_calls)


# regression: gh-674 -- no raw actor and no typed/stored text may reach a log call. The stored
# text embeds `$(username)`, so the rendered reply carries the actor -- but never the logs.
def test_logs_never_contain_actor_or_command_text(fake_host) -> None:
    actor = "PIIACTOR_alice"
    secret = "PIITEXT_secret_phrase"

    def say(text: str, *, is_mod: bool = True, community: str | None = "comm-1") -> None:
        event = _event(text, actor=actor, is_mod=is_mod)
        with _ctx(community):
            out = _run(transform(event))
        if out is not None:
            _run(dispatch(_envelope(out, community=community), {}, http_client=None))

    say(f"!command set !greet hi $(username) {secret}")
    say(f"!command set !greet hi $(username) {secret}", is_mod=False)  # denied
    say("!greet")  # renders the actor into the reply only
    say("!command list")
    say(f"!command timer !greet set 5m {secret}")  # malformed trailing text
    say("!command timer !greet set 5m")
    say("!command timer !greet enable")
    say(f"!command bogus {secret}")  # usage
    say(f"!command remove !greet {secret}")
    say("!command remove !greet")
    rendered = json.loads(fake_host.relay_calls[2][1])["text"]
    assert actor in rendered  # sanity: the reply (not the log) is where the actor appears
    assert _assert_logs_pii_free(fake_host, actor, secret) >= 10


# regression: gh-674 -- error-path logs carry the host error text, never the typed text.
def test_kv_error_logs_never_contain_actor_or_command_text(fake_host) -> None:
    secret = "PIITEXT_secret_phrase"
    _fail_kv(fake_host, "set")
    event = _event(f"!command set !greet {secret}", actor="PIIACTOR_alice", is_mod=True)
    out = _run(transform(event))
    assert out is not None
    _run(dispatch(_envelope(out), {}, http_client=None))
    assert any("dispatch.kv_error" in m for _lvl, m, _f in fake_host.log_calls)
    _assert_logs_pii_free(fake_host, "PIIACTOR_alice", secret)


# ---------------------------------------------------------------------------
# gh-613: timers persist config only -- no replies may claim periodic firing
# ---------------------------------------------------------------------------


# regression: gh-613 -- no scheduled/periodic trigger exists in the `stage` WIT world, so every
# timer reply that implies firing must say so loudly; the config is saved, nothing fires.
def test_timer_replies_cite_gh_613_and_never_claim_periodic_firing(fake_host) -> None:
    _seed_registry(fake_host, "comm-1", {"greet": "hi"})
    texts: list[str] = []
    for text in (
        "!command timer !greet set 5m",
        "!command timer !greet enable",
        "!command timer !greet disable",
    ):
        out = _run(transform(_event(text, is_mod=True)))
        assert out is not None
        _run(dispatch(_envelope(out), {}, http_client=None))
        texts.append(json.loads(fake_host.relay_calls[-1][1])["text"])

    set_reply, enable_reply, disable_reply = texts
    assert "gh-613" in set_reply and "not wired yet" in set_reply
    assert "gh-613" in enable_reply and "not wired yet" in enable_reply
    assert disable_reply == "timer disabled for !greet"  # disabling needs no firing caveat
    assert json.loads(fake_host.kv_store["command.timers.comm-1"])["greet"] == {
        "interval_seconds": 300,
        "enabled": False,
    }
