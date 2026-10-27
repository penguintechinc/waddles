"""Host-native tests for the `greeter` bundle -- fake `wit_world` (kv/relay/flags/log/clock).

No WASM/wasmtime: mirrors `bundles/python/count/tests/test_app.py`'s fake-host approach. The fake
`kv` validates keys exactly like the real host (`scope.rs::is_allowed_key_byte`, gh-631).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.command import CommandUsageError
from waddle_sdk.flask_core.bundle_runtime import bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.sub_modules import SubModuleGate

from app import (
    COOLDOWN_SECONDS,
    REPLY_MENTION,
    REPLY_THANKS,
    classify,
    dispatch,
    transform,
)

COMMUNITY = "comm-1"
_ALLOWED_KV_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


class _HostKvError(Exception):
    """Shaped like the generated WIT `Err` (`.value` holds the error union)."""

    def __init__(self, value: Any) -> None:
        super().__init__("host kv error")
        self.value = value


class Error_Backend:  # noqa: N801 -- mirrors the generated WIT case-class naming
    """Stand-in for the generated `Error_Backend` case."""


class _FakeKv:
    """In-memory `kv` host with real key validation and per-op scripted failures."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail_ops: set[str] = set()

    def _check(self, op: str, key: str) -> None:
        if not key or len(key) > 256 or any(c not in _ALLOWED_KV_KEY_CHARS for c in key):
            raise _HostKvError(f"invalid key {key!r}")
        if op in self.fail_ops:
            raise _HostKvError(Error_Backend())

    def get(self, key: str) -> bytes | None:
        self.calls.append(("get", (key,)))
        self._check("get", key)
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        self.calls.append(("set", (key, bytes(value), ttl_seconds)))
        self._check("set", key)
        self.store[key] = bytes(value)

    def delete(self, key: str) -> None:
        self.calls.append(("delete", (key,)))
        self._check("delete", key)
        self.store.pop(key, None)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Fake WIT host: flag ON, empty kv, recording relay/log, settable clock."""
    fake_kv = _FakeKv()
    relay_calls: list[tuple[str, str]] = []
    log_calls: list[tuple[int, str, str]] = []
    state: dict[str, Any] = {"flag": True, "now": 1_000_000}

    imports = types.SimpleNamespace(
        flags=types.SimpleNamespace(enabled=lambda key, default_value: state["flag"]),
        kv=fake_kv,
        relay=types.SimpleNamespace(push=lambda provider, msg: relay_calls.append((provider, msg))),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, fields_json: log_calls.append((lvl, msg, fields_json)),
        ),
        clock=types.SimpleNamespace(now_millis=lambda: state["now"]),
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = imports  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return types.SimpleNamespace(
        kv=fake_kv, relay_calls=relay_calls, log_calls=log_calls, state=state
    )


def _event(
    text: Any,
    *,
    actor: str | None = "alice",
    platform: str = "twitch",
    extra: dict[str, Any] | None = None,
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": "chan-1"}
    payload.update(extra or {})
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at="2026-10-13T00:00:00.000Z",
    )


def _mod_event(text: str) -> PlatformEvent:
    return _event(text, actor="modbob", extra={"is_mod": True, "is_broadcaster": False})


def _run(event: PlatformEvent, *, community: str | None = COMMUNITY) -> PlatformEvent | None:
    async def _go() -> PlatformEvent | None:
        with bundle_context(tenant="tenant-1", community=community, app_id="greeter"):
            return await transform(event)

    return asyncio.run(_go())


def _text(result: PlatformEvent | None) -> str:
    assert result is not None
    return str(result.payload["text"])


def _enable(host: types.SimpleNamespace, *subs: str, community: str = COMMUNITY) -> None:
    for sub in subs:
        host.kv.store[f"c.{community}.submodule.greeter.{sub}"] = b"1"


def _logged(host: types.SimpleNamespace) -> str:
    return " ".join(f"{msg} {fields}" for _lvl, msg, fields in host.log_calls)


# --------------------------------------------------------------------------- responders


@pytest.mark.parametrize(
    ("category", "text", "expected"),
    [
        ("greet", "hello everyone", "Hey @alice! \U0001f44b"),
        ("greet", "Hi!", "Hey @alice! \U0001f44b"),
        ("greet", "HEY, how are you", "Hey @alice! \U0001f44b"),
        ("greet", "yo", "Hey @alice! \U0001f44b"),
        ("greet", "howdy partner", "Hey @alice! \U0001f44b"),
        ("mention", "@waddles are you there", REPLY_MENTION),
        ("mention", "@WaddleBot hello? no", REPLY_MENTION),
        ("mention", "is @waddle around", REPLY_MENTION),
        ("mention", "waddles, help", REPLY_MENTION),
        ("thanks", "thanks!", REPLY_THANKS),
        ("thanks", "ok ty", REPLY_THANKS),
        ("thanks", "Thank   you so much", REPLY_THANKS),
        ("thanks", "thx", REPLY_THANKS),
    ],
)
def test_category_replies_when_enabled(
    host: types.SimpleNamespace, category: str, text: str, expected: str
) -> None:
    _enable(host, category)
    result = _run(_event(text))
    assert _text(result) == expected
    assert result is not None
    assert result.payload["channel_id"] == "chan-1"


@pytest.mark.parametrize("text", ["hello", "@waddles hi", "thanks", "hey @waddles thanks"])
def test_default_off_no_reply_and_no_writes(host: types.SimpleNamespace, text: str) -> None:
    assert _run(_event(text)) is None
    assert not [c for c in host.kv.calls if c[0] == "set"]


def test_only_the_enabled_category_replies(host: types.SimpleNamespace) -> None:
    _enable(host, "thanks")
    assert _run(_event("hello")) is None
    assert _text(_run(_event("thanks", actor="bob"))) == REPLY_THANKS


@pytest.mark.parametrize(
    "text",
    [
        "ship it",
        "this is a ship",
        "hiking today",
        "history lesson",
        "heyyy",
        "hello123",
        "yoga time",
        "hi-fi audio",
        "hiya",
        "say hi",  # greeting not at message start
        "party time",
        "tying a knot",
        "thankful",
        "thanksgiving",
        "waddlesworth",
        "mail bob@waddles.com",
        "just chatting",
        "",
    ],
)
def test_token_boundary_non_matches(host: types.SimpleNamespace, text: str) -> None:
    _enable(host, "greet", "mention", "thanks")
    assert _run(_event(text)) is None


def test_classify_returns_precedence_order() -> None:
    assert classify("hey @waddles thanks") == ["greet", "mention", "thanks"]
    assert classify("nothing here") == []


def test_precedence_greet_wins_when_all_enabled(host: types.SimpleNamespace) -> None:
    _enable(host, "greet", "mention", "thanks")
    assert _text(_run(_event("hey @waddles thanks"))) == "Hey @alice! \U0001f44b"


def test_falls_through_to_first_enabled_category(host: types.SimpleNamespace) -> None:
    _enable(host, "mention")
    assert _text(_run(_event("hey @waddles thanks"))) == REPLY_MENTION


def test_missing_actor_uses_fallback_name(host: types.SimpleNamespace) -> None:
    _enable(host, "greet")
    assert _text(_run(_event("hello", actor=None))) == "Hey @friend! \U0001f44b"


# --------------------------------------------------------------------------- early returns


@pytest.mark.parametrize("text", [None, 5, b"hello", ["hello"], "   ", "!ping", "!greeterx list"])
def test_not_ours_returns_none(host: types.SimpleNamespace, text: Any) -> None:
    _enable(host, "greet")
    assert _run(_event(text)) is None


def test_communities_are_isolated(host: types.SimpleNamespace) -> None:
    _enable(host, "greet", community="comm-2")
    assert _run(_event("hello"), community="comm-1") is None
    assert _text(_run(_event("hello"), community="comm-2")).startswith("Hey")


def test_tenant_wide_sentinel_scope(host: types.SimpleNamespace) -> None:
    _enable(host, "greet", community="0")
    assert _text(_run(_event("hello"), community=None)).startswith("Hey")
    assert any(k.startswith("c.0.greeter.cd.greet.") for k in host.kv.store)


def test_empty_community_fails_loud(host: types.SimpleNamespace) -> None:
    with pytest.raises(ValueError, match="community"):
        _run(_event("hello"), community="")
    assert "greeter.missing_community" in _logged(host)


# --------------------------------------------------------------------------- bot / self guard


@pytest.mark.parametrize(
    "extra",
    [
        {"is_bot": True},
        {"is_bot": "false"},  # ambiguous string -> skip (fail closed)
        {"is_bot": None},
        {"author_is_bot": 1},
        {"bot": True},
        {"author_id": "42", "bot_id": "42"},
        {"author_id": 42, "self_id": "42"},
        {"author_id": "42", "bot_user_id": "42"},
    ],
)
def test_bot_authors_are_skipped(host: types.SimpleNamespace, extra: dict[str, Any]) -> None:
    _enable(host, "greet", "mention", "thanks")
    assert _run(_event("hello", extra=extra)) is None
    assert not [c for c in host.kv.calls if c[0] == "set"]


@pytest.mark.parametrize("actor", ["waddles", "WaddleBot", "@waddles"])
def test_bot_named_actor_is_skipped(host: types.SimpleNamespace, actor: str) -> None:
    _enable(host, "greet")
    assert _run(_event("hello", actor=actor)) is None


def test_explicit_non_bot_and_other_ids_still_reply(host: types.SimpleNamespace) -> None:
    _enable(host, "greet")
    extra = {"is_bot": False, "author_id": "7", "bot_id": "42"}
    assert _text(_run(_event("hello", extra=extra))).startswith("Hey")


def test_bot_guard_runs_before_any_kv(host: types.SimpleNamespace) -> None:
    host.kv.calls.clear()
    _run(_event("hello", extra={"is_bot": True}))
    assert host.kv.calls == []


# --------------------------------------------------------------------------- cooldown


def test_cooldown_suppresses_second_greeting(host: types.SimpleNamespace) -> None:
    _enable(host, "greet")
    assert _run(_event("hello")) is not None
    assert _run(_event("hello again")) is None


def test_cooldown_is_per_user(host: types.SimpleNamespace) -> None:
    _enable(host, "greet")
    assert _run(_event("hello", actor="alice")) is not None
    assert _text(_run(_event("hello", actor="bob"))) == "Hey @bob! \U0001f44b"


def test_cooldown_is_per_category(host: types.SimpleNamespace) -> None:
    _enable(host, "greet", "thanks")
    assert _run(_event("hello")) is not None
    assert _text(_run(_event("thanks"))) == REPLY_THANKS


def test_cooldown_expires_after_window(host: types.SimpleNamespace) -> None:
    _enable(host, "greet")
    assert _run(_event("hello")) is not None
    host.state["now"] += COOLDOWN_SECONDS["greet"] * 1000 - 1
    assert _run(_event("hello")) is None
    host.state["now"] += 1
    assert _run(_event("hello")) is not None


def test_cooldown_key_is_hashed_and_carries_ttl(host: types.SimpleNamespace) -> None:
    _enable(host, "greet")
    _run(_event("hello", actor="alice", extra={"author_id": "user-9"}))
    sets = [c[1] for c in host.kv.calls if c[0] == "set"]
    assert len(sets) == 1
    key, value, ttl = sets[0]
    assert key.startswith(f"c.{COMMUNITY}.greeter.cd.greet.")
    assert "alice" not in key and "user-9" not in key and ":" not in key
    assert len(key.rsplit(".", 1)[1]) == 64
    assert ttl == COOLDOWN_SECONDS["greet"]
    assert value == b"1000000"


def test_cooldown_key_falls_back_to_anonymous_identity(host: types.SimpleNamespace) -> None:
    _enable(host, "greet")
    assert _run(_event("hello", actor=None)) is not None
    assert _run(_event("hello", actor=None)) is None


def test_cooldown_blocks_without_falling_through(host: types.SimpleNamespace) -> None:
    _enable(host, "greet", "mention")
    assert _text(_run(_event("hey @waddles"))).startswith("Hey")
    assert _run(_event("hey @waddles")) is None


def test_corrupt_cooldown_value_self_heals(host: types.SimpleNamespace) -> None:
    _enable(host, "thanks")
    assert _run(_event("thanks")) is not None
    (key,) = [k for k in host.kv.store if ".greeter.cd." in k]
    host.kv.store[key] = b"not-a-number"
    assert _text(_run(_event("thanks"))) == REPLY_THANKS
    assert "greeter.cooldown_corrupt" in _logged(host)
    assert host.kv.store[key] == b"1000000"


# --------------------------------------------------------------------------- !greeter command


def test_mod_can_enable_and_disable(host: types.SimpleNamespace) -> None:
    assert _text(_run(_mod_event("!greeter enable greet"))) == "greeter greet enabled."
    assert host.kv.store[f"c.{COMMUNITY}.submodule.greeter.greet"] == b"1"
    assert _text(_run(_event("hello"))).startswith("Hey")
    assert _text(_run(_mod_event("!greeter disable greet"))) == "greeter greet disabled."
    assert f"c.{COMMUNITY}.submodule.greeter.greet" not in host.kv.store
    assert _run(_event("hello", actor="bob")) is None


def test_broadcaster_can_toggle_and_case_is_ignored(host: types.SimpleNamespace) -> None:
    event = _event("!GREETER Enable THANKS", extra={"is_broadcaster": True})
    assert _text(_run(event)) == "greeter thanks enabled."


@pytest.mark.parametrize(
    "extra",
    [
        {"is_mod": False, "is_broadcaster": False},
        {},  # Discord today: no role info at all
        {"is_mod": "true"},  # non-boolean badge must not pass
        {"is_mod": 1},
        {"is_mod": None, "is_broadcaster": None},
    ],
)
@pytest.mark.parametrize("verb", ["enable", "disable"])
def test_toggle_fails_closed_without_mod(
    host: types.SimpleNamespace, extra: dict[str, Any], verb: str
) -> None:
    result = _run(_event(f"!greeter {verb} greet", extra=extra))
    assert "only moderators" in _text(result)
    assert not [c for c in host.kv.calls if c[0] in ("set", "delete")]


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("!greeter enable", "requires a sub-module"),
        ("!greeter enable greet mention", "exactly one"),
        ("!greeter enable nope", "unknown sub-module"),
        ("!greeter bogus", "unknown option"),
        ("!greeter set x", "Usage:"),
        ("!greeter greet frobnicate", "unknown option"),
    ],
)
def test_usage_errors_are_the_reply(host: types.SimpleNamespace, text: str, needle: str) -> None:
    assert needle in _text(_run(_mod_event(text)))
    assert not [c for c in host.kv.calls if c[0] == "set"]


def test_list_shows_state_publicly(host: types.SimpleNamespace) -> None:
    assert _text(_run(_event("!greeter list"))) == (
        "greeter responders: greet=off, mention=off, thanks=off"
    )
    _enable(host, "greet", "thanks")
    assert _text(_run(_event("!greeter list"))) == (
        "greeter responders: greet=on, mention=off, thanks=on"
    )
    assert _text(_run(_event("!greeter greet list"))).endswith("greet=on, mention=off, thanks=on")


def test_bare_shows_help_and_state(host: types.SimpleNamespace) -> None:
    reply = _text(_run(_event("!greeter")))
    assert reply.startswith("Usage: !greeter")
    assert reply.endswith("now: greet=off, mention=off, thanks=off")


def test_single_sub_module_state(host: types.SimpleNamespace) -> None:
    assert _text(_run(_event("!greeter greet"))) == "greeter greet is off."
    _enable(host, "greet")
    assert _text(_run(_event("!greeter greet"))) == "greeter greet is on."


def test_command_path_ignores_bot_flag_and_cooldown(host: types.SimpleNamespace) -> None:
    # The mod command must stay usable; only the free-text path is bot/cooldown guarded.
    assert _run(_mod_event("!greeter list")) is not None
    assert _run(_mod_event("!greeter list")) is not None


# --------------------------------------------------------------------------- kv failures


def test_gate_read_failure_fails_loud_on_free_text(host: types.SimpleNamespace) -> None:
    host.kv.fail_ops = {"get"}
    with pytest.raises(RuntimeError, match="gate_get failed: Error_Backend"):
        _run(_event("hello"))
    assert "greeter.kv_failure" in _logged(host)


def test_cooldown_read_failure_fails_loud(host: types.SimpleNamespace) -> None:
    _enable(host, "greet")
    real_get = host.kv.get

    def flaky_get(key: str) -> bytes | None:
        if ".greeter.cd." in key:
            raise _HostKvError(Error_Backend())
        return real_get(key)

    host.kv.get = flaky_get  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="cooldown_get failed"):
        _run(_event("hello"))


def test_cooldown_write_failure_fails_loud_and_sends_nothing(host: types.SimpleNamespace) -> None:
    _enable(host, "greet")
    host.kv.fail_ops = {"set"}
    with pytest.raises(RuntimeError, match="cooldown_set failed"):
        _run(_event("hello"))
    assert host.relay_calls == []


def test_wit_err_without_value_is_still_classified(host: types.SimpleNamespace) -> None:
    host.kv.get = lambda key: (_ for _ in ()).throw(KeyError("boom"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="KeyError"):
        _run(_event("hello"))


def test_toggle_write_failure_replies_unavailable(host: types.SimpleNamespace) -> None:
    host.kv.fail_ops = {"set"}
    result = _run(_mod_event("!greeter enable greet"))
    assert "temporarily unavailable" in _text(result)
    assert "greeter.kv_failure" in _logged(host)


def test_list_read_failure_replies_unavailable(host: types.SimpleNamespace) -> None:
    host.kv.fail_ops = {"get"}
    assert "temporarily unavailable" in _text(_run(_event("!greeter list")))


# --------------------------------------------------------------------------- PII


def test_logs_never_contain_text_actor_or_ids(host: types.SimpleNamespace) -> None:
    _enable(host, "greet", "thanks")
    probe_actor = "SecretUserName"
    probe_id = "id-987654"
    for text in ("hello super-secret-phrase", "thanks super-secret-phrase", "nothing"):
        _run(_event(text, actor=probe_actor, extra={"author_id": probe_id}))
    _run(_event("hello", actor=probe_actor, extra={"author_id": probe_id}))  # cooldown path
    _run(_event("hello", actor=probe_actor, extra={"is_bot": True}))
    _run(_event("!greeter enable greet", actor=probe_actor))  # denied path
    _run(_mod_event("!greeter enable mention"))
    logged = _logged(host)
    assert logged  # the denominator: logs were actually emitted
    for needle in (probe_actor, probe_id, "super-secret-phrase", "modbob"):
        assert needle not in logged


# --------------------------------------------------------------------------- dispatch


def _envelope(platform: str, payload: dict[str, Any]) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=COMMUNITY,
        app_id="waddles.core.example.greeter",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="alice",
            payload=payload,
            occurred_at="2026-10-13T00:00:00.000Z",
        ),
        ts="2026-10-13T00:00:00.000Z",
    )


def test_dispatch_relays_the_text_transform_built(host: types.SimpleNamespace) -> None:
    envelope = _envelope("discord", {"channel_id": "chan-1", "text": REPLY_THANKS})
    result = asyncio.run(dispatch(envelope, {}, http_client=None))
    provider, message_json = host.relay_calls[0]
    assert provider == "discord"
    assert json.loads(message_json) == {"channel": "chan-1", "text": REPLY_THANKS}
    assert (result.transport, result.detail) == ("discord", "relayed")
    assert result.http_status is None and result.sub_type is None


def test_dispatch_raises_when_channel_id_is_missing(host: types.SimpleNamespace) -> None:
    envelope = _envelope("twitch", {"channel_id": None, "text": "x"})
    with pytest.raises(ValueError, match="channel_id"):
        asyncio.run(dispatch(envelope, {}, http_client=None))
    assert host.relay_calls == []


def test_dispatch_defaults_missing_text_to_empty(host: types.SimpleNamespace) -> None:
    asyncio.run(dispatch(_envelope("twitch", {"channel_id": "c"}), {}, http_client=None))
    assert json.loads(host.relay_calls[0][1])["text"] == ""


def test_defensive_toggle_usage_error_becomes_the_reply(
    host: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(self: Any, community_id: str, parsed: Any) -> bool | None:
        raise CommandUsageError("!greeter enable: no sub-module to toggle")

    monkeypatch.setattr(SubModuleGate, "apply_toggle", _boom)
    assert "no sub-module" in _text(_run(_mod_event("!greeter enable greet")))
    assert [c for c in host.log_calls if "greeter.usage_error" in c[1]]


@pytest.mark.parametrize("text", ["!greeter bogus-secret-token", "!greeter enable nope-secret"])
def test_usage_error_is_logged_at_debug_pii_free(host: types.SimpleNamespace, text: str) -> None:
    """The usage catch is explicit: one DEBUG line, never the raw message text or the actor."""
    _run(_event(text, actor="alice-raw-handle", extra={"is_mod": True}))
    usage_logs = [c for c in host.log_calls if "greeter.usage_error" in c[1]]
    assert len(usage_logs) == 1
    assert usage_logs[0][0] == 3  # DEBUG
    blob = json.dumps(usage_logs)
    assert "secret" not in blob
    assert "alice-raw-handle" not in blob


@pytest.mark.parametrize(
    "badges",
    [{"is_mod": "true", "is_broadcaster": "true"}, {"is_mod": "false", "is_broadcaster": "false"}],
)
def test_string_badges_are_never_laundered_into_mod(
    host: types.SimpleNamespace, badges: dict[str, Any]
) -> None:
    """A string badge (even "true") must not let a viewer toggle a responder."""
    reply = _text(_run(_event("!greeter enable greet", actor="viewer", extra=badges)))
    assert "only moderators" in reply
    assert not [c for c in host.kv.calls if c[0] == "set"]
