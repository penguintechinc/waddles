"""Host-native tests for the `socials` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- mirrors `bundles/python/count/tests/test_app.py`: a fake `wit_world`
(in-memory host-key-validating `kv`, `flags`, `relay`, `log`) is installed into `sys.modules`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

from app import (
    LINK_KEY_PREFIX,
    MAX_LINKS,
    MAX_PLATFORM_LEN,
    MAX_URL_LEN,
    REGISTRY_KEY,
    _link_key,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# Mirrors `core/bundle_host_kv/src/scope.rs::is_allowed_key_byte` (ASCII alnum + `_`/`-`/`.`,
# notably NOT `:`) so a key the real host would reject fails here too (gh-631).
_ALLOWED_KV_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


class _HostKvError(Exception):
    """Shaped like the generated WIT `Err` (`.value` holds the error union)."""

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value


def _validate_guest_key(key: str) -> None:
    if not key or len(key) > 256:
        raise _HostKvError(f"too-large: key length {len(key)}")
    if any(ch not in _ALLOWED_KV_KEY_CHARS for ch in key):
        raise _HostKvError(f"backend: invalid key {key!r}")


class _FakeKv:
    """In-memory `kv` host stand-in -- validates keys like the real host, scripted failures."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_ops: set[str] = set()

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_ops:
            raise _HostKvError(f"backend: scripted {op} failure")

    def get(self, key: str) -> bytes | None:
        _validate_guest_key(key)
        self.calls.append(("get", key))
        self._maybe_fail("get")
        return self.store.get(key)

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        _validate_guest_key(key)
        self.calls.append(("set", key))
        self._maybe_fail("set")
        self.store[key] = bytes(value)

    def delete(self, key: str) -> None:
        _validate_guest_key(key)
        self.calls.append(("delete", key))
        self._maybe_fail("delete")
        self.store.pop(key, None)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Fake WIT host: flag ON by default, real in-memory `kv`, recording `relay`/`log`."""
    fake_kv = _FakeKv()
    relay_calls: list[tuple[str, str]] = []
    log_calls: list[tuple[Any, str, str]] = []
    flag_state = {"enabled": True}

    flags_mod = types.SimpleNamespace(
        enabled=lambda key, default_value: flag_state["enabled"]
    )
    relay_mod = types.SimpleNamespace(
        push=lambda provider, msg: relay_calls.append((provider, msg))
    )
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: log_calls.append((lvl, msg, fields_json)),
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, kv=fake_kv, relay=relay_mod, log=log_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return types.SimpleNamespace(
        kv=fake_kv, relay_calls=relay_calls, log_calls=log_calls, flag_state=flag_state
    )


def _event(text: str, **badges: Any) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": "12345", **badges}
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload=payload,
        occurred_at="2026-10-13T00:00:00.000Z",
    )


def _mod(text: str) -> PlatformEvent:
    return _event(text, is_mod=True, is_broadcaster=False)


def _say(text: str, **badges: Any) -> str:
    result = _run(transform(_event(text, **badges)))
    assert result is not None
    return str(result.payload["text"])


def _envelope(payload: dict[str, Any], platform: str = "twitch") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.socials",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload=payload,
            occurred_at="2026-10-13T00:00:00.000Z",
        ),
        ts="2026-10-13T00:00:00.000Z",
    )


TWITTER = "https://twitter.com/waddles"
YOUTUBE = "https://youtube.com/@waddles"


# ---- routing / flag ------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["hello", "", "!social stats", "!socialsx", "socials"])
def test_non_socials_text_is_not_ours(text: str, host: types.SimpleNamespace) -> None:
    assert _run(transform(_event(text))) is None
    assert host.kv.calls == []


def test_non_string_text_is_ignored(host: types.SimpleNamespace) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=None,
        payload={"text": 5},
        occurred_at="",
    )
    assert _run(transform(event)) is None


def test_flag_off_suppresses_reply_and_touches_no_kv(
    host: types.SimpleNamespace,
) -> None:
    host.flag_state["enabled"] = False
    assert _run(transform(_mod(f"!socials set twitter {TWITTER}"))) is None
    assert host.kv.calls == []


def test_uppercase_command_head_is_recognized(host: types.SimpleNamespace) -> None:
    assert _say("!SOCIALS") == "No socials have been set yet."


# ---- read ----------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["!socials", "!socials list"])
def test_bare_read_and_list_empty_friendly_message(
    text: str, host: types.SimpleNamespace
) -> None:
    assert _say(text) == "No socials have been set yet."


def test_set_then_read_lists_sorted_links(host: types.SimpleNamespace) -> None:
    assert (
        _say(f"!socials set youtube {YOUTUBE}", is_mod=True)
        == f"Set youtube → {YOUTUBE}"
    )
    assert (
        _say(f"!socials set twitter {TWITTER}", is_broadcaster=True)
        == f"Set twitter → {TWITTER}"
    )
    expected = f"Follow us: twitter → {TWITTER}, youtube → {YOUTUBE}"
    assert _say("!socials") == expected
    assert _say("!socials list") == expected
    assert json.loads(host.kv.store[REGISTRY_KEY]) == ["twitter", "youtube"]
    assert host.kv.store["socials.link.twitter"] == TWITTER.encode()


def test_list_with_extra_args_is_usage(host: types.SimpleNamespace) -> None:
    assert _say("!socials list now").startswith("Usage: !socials")


# ---- set / update --------------------------------------------------------------------------


def test_set_updates_existing_without_duplicating_registry(
    host: types.SimpleNamespace,
) -> None:
    _say(f"!socials set twitter {TWITTER}", is_mod=True)
    new = "https://x.com/waddles"
    assert (
        _say(f"!socials set TWITTER {new}", is_mod=True) == f"Updated twitter → {new}"
    )
    assert json.loads(host.kv.store[REGISTRY_KEY]) == ["twitter"]
    assert _say("!socials") == f"Follow us: twitter → {new}"


def test_http_url_accepted_and_case_preserved(host: types.SimpleNamespace) -> None:
    url = "http://Example.com/Path?Q=1"
    _say(f"!socials set site {url}", is_mod=True)
    assert _say("!socials") == f"Follow us: site → {url}"


@pytest.mark.parametrize("args", ["", "twitter", f"twitter {TWITTER} extra"])
def test_set_wrong_arity_is_usage(args: str, host: types.SimpleNamespace) -> None:
    assert (
        _say(f"!socials set {args}".strip(), is_mod=True)
        == "Usage: !socials set <platform> <url>"
    )
    assert REGISTRY_KEY not in host.kv.store


# ---- remove --------------------------------------------------------------------------------


def test_remove_deletes_link_and_registry_entry(host: types.SimpleNamespace) -> None:
    _say(f"!socials set twitter {TWITTER}", is_mod=True)
    _say(f"!socials set youtube {YOUTUBE}", is_mod=True)
    assert _say("!socials remove Twitter", is_mod=True) == "Removed twitter."
    assert "socials.link.twitter" not in host.kv.store
    assert json.loads(host.kv.store[REGISTRY_KEY]) == ["youtube"]
    assert _say("!socials") == f"Follow us: youtube → {YOUTUBE}"


def test_remove_last_link_returns_to_empty_message(host: types.SimpleNamespace) -> None:
    _say(f"!socials set twitter {TWITTER}", is_mod=True)
    _say("!socials remove twitter", is_broadcaster=True)
    assert _say("!socials") == "No socials have been set yet."


def test_remove_unknown_platform_reports_missing(host: types.SimpleNamespace) -> None:
    assert _say("!socials remove nope", is_mod=True) == "No social link set for 'nope'."


@pytest.mark.parametrize("args", ["", "a b"])
def test_remove_wrong_arity_is_usage(args: str, host: types.SimpleNamespace) -> None:
    assert (
        _say(f"!socials remove {args}".strip(), is_mod=True)
        == "Usage: !socials remove <platform>"
    )


# ---- mod gate (fail closed) ----------------------------------------------------------------


def _seed(host: types.SimpleNamespace) -> None:
    _say(f"!socials set twitter {TWITTER}", is_mod=True)
    host.kv.calls.clear()


_GATE_BADGES = [
    {},  # no role info at all (Discord today)
    {"is_mod": False},
    {"is_mod": False, "is_broadcaster": False},
    {"is_mod": "true"},  # truthy non-bool must NOT pass
    {"is_mod": 1, "is_broadcaster": "yes"},
    {"is_mod": None, "is_broadcaster": None},
]


@pytest.mark.parametrize("badges", _GATE_BADGES)
def test_set_fails_closed_without_strict_true_badge(
    badges: dict[str, Any], host: types.SimpleNamespace
) -> None:
    _seed(host)
    reply = _say("!socials set twitter https://evil.example/x", **badges)
    assert "Only the broadcaster or a moderator" in reply
    assert host.kv.store["socials.link.twitter"] == TWITTER.encode()
    assert host.kv.calls == []


@pytest.mark.parametrize("badges", _GATE_BADGES)
def test_remove_fails_closed_without_strict_true_badge(
    badges: dict[str, Any], host: types.SimpleNamespace
) -> None:
    _seed(host)
    reply = _say("!socials remove twitter", **badges)
    assert "Only the broadcaster or a moderator" in reply
    assert "socials.link.twitter" in host.kv.store
    assert json.loads(host.kv.store[REGISTRY_KEY]) == ["twitter"]
    assert host.kv.calls == []


def test_gate_runs_before_argument_validation(host: types.SimpleNamespace) -> None:
    assert "Only the broadcaster" in _say("!socials set", is_mod=False)
    assert "Only the broadcaster" in _say("!socials remove", is_mod=False)


def test_viewers_can_still_read(host: types.SimpleNamespace) -> None:
    _seed(host)
    assert _say("!socials", is_mod=False) == f"Follow us: twitter → {TWITTER}"


# ---- URL / platform validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "twitter.com/waddles",  # no scheme
        "ftp://example.com/x",
        "javascript:alert(1)",
        "https://",
        "https:///path",
        "https:/example.com",
        "https://user:pw@example.com/x",
        "https://user@example.com/x",
        "https://[::1",  # urlsplit ValueError
        "https://example.com:99999/x",  # port ValueError
        "https://example.com:abc/x",
        "https://exa\x00mple.com",
        "https://" + "a" * MAX_URL_LEN + ".com",
    ],
)
def test_invalid_url_rejected_and_not_stored(
    url: str, host: types.SimpleNamespace
) -> None:
    reply = _say(f"!socials set twitter {url}", is_mod=True)
    assert "Usage" not in reply and "Set" not in reply and "Updated" not in reply
    assert REGISTRY_KEY not in host.kv.store
    assert "socials.link.twitter" not in host.kv.store


def test_url_error_messages_are_specific(host: types.SimpleNamespace) -> None:
    assert "https://" in _say("!socials set twitter ftp://x.com", is_mod=True)
    assert "no host" in _say("!socials set twitter https://", is_mod=True)
    assert "username or password" in _say(
        "!socials set twitter https://u:p@x.com", is_mod=True
    )
    assert "valid URL" in _say("!socials set twitter https://[::1", is_mod=True)
    assert "control characters" in _say(
        "!socials set twitter https://a\x01b.com", is_mod=True
    )
    assert "characters or fewer" in _say(
        f"!socials set twitter https://x.com/{'a' * MAX_URL_LEN}", is_mod=True
    )


@pytest.mark.parametrize(
    "platform", ["tw:itter", "twit/ter", "ünï", "a" * (MAX_PLATFORM_LEN + 1)]
)
def test_invalid_platform_rejected(platform: str, host: types.SimpleNamespace) -> None:
    reply = _say(f"!socials set {platform} {TWITTER}", is_mod=True)
    assert "platform names" in reply
    assert REGISTRY_KEY not in host.kv.store


def test_max_links_enforced_but_update_still_allowed(
    host: types.SimpleNamespace,
) -> None:
    for i in range(MAX_LINKS):
        _say(f"!socials set p{i} {TWITTER}", is_mod=True)
    assert "at most" in _say(f"!socials set extra {TWITTER}", is_mod=True)
    assert "socials.link.extra" not in host.kv.store
    assert _say(f"!socials set p0 {YOUTUBE}", is_mod=True).startswith("Updated p0")


def test_kv_keys_satisfy_host_charset(host: types.SimpleNamespace) -> None:
    for key in (REGISTRY_KEY, LINK_KEY_PREFIX + "twitter", _link_key("my-site_2")):
        _validate_guest_key(key)
        assert ":" not in key


# ---- grammar -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["!socials twitter", "!socials enable ai", "!socials add x y"]
)
def test_unknown_or_unsupported_grammar_gets_usage_reply(
    text: str, host: types.SimpleNamespace
) -> None:
    reply = _say(text, is_mod=True)
    assert "Usage: !socials" in reply


@pytest.mark.parametrize("text", ["!socials add x", "!socials reset"])
def test_verbs_this_bundle_does_not_implement_get_usage(
    text: str, host: types.SimpleNamespace
) -> None:
    assert _say(text, is_mod=True).startswith("Usage: !socials |")


# ---- kv failures (fail loud) ---------------------------------------------------------------


@pytest.mark.parametrize("op", ["get", "set", "delete"])
def test_kv_failure_replies_and_logs_error(
    op: str, host: types.SimpleNamespace
) -> None:
    _say(f"!socials set twitter {TWITTER}", is_mod=True)
    host.kv.fail_ops = {op}
    text = {
        "get": "!socials",
        "set": f"!socials set youtube {YOUTUBE}",
        "delete": "!socials remove twitter",
    }[op]
    reply = _say(text, is_mod=True)
    assert "went wrong" in reply
    assert any(
        lvl == 0 and msg == "socials.kv_failure" for lvl, msg, _f in host.log_calls
    )


def test_registry_write_failure_after_link_write_fails_loud(
    host: types.SimpleNamespace,
) -> None:
    class _FailRegistrySet(_FakeKv):
        def set(self, key: str, value: Any, ttl_seconds: int) -> None:
            if key == REGISTRY_KEY:
                raise _HostKvError("backend: boom")
            super().set(key, value, ttl_seconds)

    host.kv.fail_ops = set()
    sys.modules["wit_world"].imports.kv = _FailRegistrySet()
    assert "went wrong" in _say(f"!socials set twitter {TWITTER}", is_mod=True)


@pytest.mark.parametrize("raw", [b"not json", b"\xff\xfe", b'{"a": 1}', b"[1, 2]"])
def test_corrupt_registry_fails_loud_and_is_untouched(
    raw: bytes, host: types.SimpleNamespace
) -> None:
    host.kv.store[REGISTRY_KEY] = raw
    assert "went wrong" in _say("!socials")
    assert "went wrong" in _say(f"!socials set twitter {TWITTER}", is_mod=True)
    assert host.kv.store[REGISTRY_KEY] == raw
    assert "socials.link.twitter" not in host.kv.store


def test_registered_platform_with_missing_link_fails_loud(
    host: types.SimpleNamespace,
) -> None:
    host.kv.store[REGISTRY_KEY] = b'["twitter"]'
    assert "went wrong" in _say("!socials")


def test_non_utf8_link_fails_loud(host: types.SimpleNamespace) -> None:
    host.kv.store[REGISTRY_KEY] = b'["twitter"]'
    host.kv.store["socials.link.twitter"] = b"\xff\xfe"
    assert "went wrong" in _say("!socials")


# ---- PII-free logs -------------------------------------------------------------------------


def test_logs_never_contain_urls_actor_or_raw_text(host: types.SimpleNamespace) -> None:
    secret_url = "https://example.com/PIIURL_token"
    event = _mod(f"!socials set twitter {secret_url}")
    event.actor = "PIIACTOR_alice"
    _run(transform(event))
    _say("!socials", is_mod=True)
    _say("!socials set twitter PIIRAW_junk", is_mod=True)
    _say("!socials set twitter https://x.com/y")  # denied
    _say("!socials PIIRAW_sub", is_mod=True)
    _say("!socials remove twitter", is_mod=True)
    host.kv.store[REGISTRY_KEY] = b"PIIRAW_corrupt"
    _say("!socials")
    assert host.log_calls, "vacuous: no log calls recorded"
    for _lvl, msg, fields in host.log_calls:
        blob = f"{msg} {fields}".lower()
        for sentinel in ("piiurl_", "piiactor_", "piiraw_", "https://", "x.com"):
            assert sentinel not in blob
    assert len(host.log_calls) >= 8


# ---- dispatch ------------------------------------------------------------------------------


def test_dispatch_relays_reply(host: types.SimpleNamespace) -> None:
    result = _run(
        dispatch(
            _envelope({"channel_id": "c1", "text": "Follow us: x"}, "discord"),
            {},
            http_client=None,
        )
    )
    assert [(p, json.loads(m)) for p, m in host.relay_calls] == [
        ("discord", {"channel": "c1", "text": "Follow us: x"})
    ]
    assert (result.transport, result.detail, result.sub_type, result.http_status) == (
        "discord",
        "relayed",
        None,
        None,
    )


def test_dispatch_requires_channel_id(host: types.SimpleNamespace) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope({"text": "hi"}), {}, http_client=None))
    assert host.relay_calls == []


@pytest.mark.parametrize("text", [None, "", 5])
def test_dispatch_requires_text(text: Any, host: types.SimpleNamespace) -> None:
    with pytest.raises(ValueError, match="text"):
        _run(
            dispatch(
                _envelope({"channel_id": "c1", "text": text}), {}, http_client=None
            )
        )


def test_transform_output_round_trips_through_dispatch(
    host: types.SimpleNamespace,
) -> None:
    out = _run(transform(_mod(f"!socials set twitter {TWITTER}")))
    assert out is not None
    _run(dispatch(_envelope(out.payload), {}, http_client=None))
    assert json.loads(host.relay_calls[0][1])["text"] == f"Set twitter → {TWITTER}"
