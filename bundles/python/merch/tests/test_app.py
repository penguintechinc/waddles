"""Host-native tests for the `merch` bundle's `transform`/`dispatch` logic.

No WASM -- a fake `wit_world` stands in for the WIT host imports (`kv`/`flags`/`relay`/`log`),
same approach as `bundles/python/uptime/tests` and `count/tests`.
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
    FLAG_KEY,
    MAX_URL_LEN,
    URL_KEY,
    DispatchResult,
    UrlRejectedError,
    _is_privileged,
    dispatch,
    transform,
    validate_url,
)

# Mirrors `core/bundle_host_kv/src/scope.rs::is_allowed_key_byte` -- notably NOT `:`.
_ALLOWED_KV_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)

_GOOD_URL = "https://merch.example.com/penguinzplays"
_MOD = {"is_mod": True, "is_broadcaster": False}


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _HostKvError(Exception):
    """Shaped like the generated WIT `Err` (`.value` holds the error union)."""

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value


class _FakeKv:
    """In-memory `kv` stand-in validating keys like the real host, with scripted failures."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_ops: set[str] = set()

    def _check(self, op: str, key: str) -> None:
        if not key or len(key) > 256 or any(c not in _ALLOWED_KV_KEY_CHARS for c in key):
            raise _HostKvError(f"backend: invalid key {key!r}")
        self.calls.append((op, key))
        if op in self.fail_ops:
            raise _HostKvError(f"backend: scripted {op} failure")

    def get(self, key: str) -> bytes | None:
        self._check("get", key)
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        self._check("set", key)
        self.store[key] = bytes(value)

    def delete(self, key: str) -> None:
        self._check("delete", key)
        self.store.pop(key, None)


class _Host:
    """Recorded fake-host state shared with the test body."""

    def __init__(self) -> None:
        self.kv = _FakeKv()
        self.relay_calls: list[tuple[str, dict[str, Any]]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag_enabled = True
        self.flag_keys: list[str] = []


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    """Fake WIT host: flag on, in-memory kv, recording relay/log."""
    h = _Host()

    def _flag(key: str, default_value: bool) -> bool:
        h.flag_keys.append(key)
        return h.flag_enabled

    wit = types.ModuleType("wit_world")
    wit.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=_flag),
        kv=h.kv,
        relay=types.SimpleNamespace(push=lambda p, m: h.relay_calls.append((p, json.loads(m)))),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, fields: h.log_calls.append((lvl, msg, fields)),
        ),
    )
    monkeypatch.setitem(sys.modules, "wit_world", wit)
    return h


def _event(
    text: Any,
    *,
    badges: dict[str, Any] | None = None,
    channel_id: str | None = "12345",
    platform: str = "twitch",
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": channel_id}
    payload.update(badges or {})
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor="viewer-1",
        payload=payload,
        occurred_at="2026-10-13T00:00:00.000Z",
    )


def _envelope(payload: dict[str, Any], platform: str = "twitch") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.merch",
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


def _reply(result: PlatformEvent | None) -> str:
    assert result is not None
    return str(result.payload["text"])


def _say(host: _Host, text: str, **kw: Any) -> str:
    return _reply(_run(transform(_event(text, **kw))))


# --- routing -----------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["hello", "", "!donate", "!merchs", "merch", "!count add x"])
def test_non_matching_text_returns_none_without_flag_or_kv(text: str, host: _Host) -> None:
    assert _run(transform(_event(text))) is None
    assert host.flag_keys == []
    assert host.kv.calls == []


@pytest.mark.parametrize("text", [None, 42, ["!merch"]])
def test_non_string_text_returns_none(text: Any, host: _Host) -> None:
    assert _run(transform(_event(text))) is None
    assert host.kv.calls == []


def test_missing_text_key_returns_none(host: _Host) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_flag_off_returns_none_and_touches_no_kv(host: _Host) -> None:
    host.flag_enabled = False
    assert _run(transform(_event("!merch set " + _GOOD_URL, badges=_MOD))) is None
    assert host.flag_keys == [FLAG_KEY]
    assert FLAG_KEY == "waddles.command-merch"
    assert host.kv.calls == []


def test_command_match_is_case_and_whitespace_insensitive(host: _Host) -> None:
    assert "No merch store link" in _say(host, "  !MeRcH  ")


def test_url_key_satisfies_host_charset() -> None:
    assert URL_KEY == "merch.url"
    assert ":" not in URL_KEY
    assert all(c in _ALLOWED_KV_KEY_CHARS for c in URL_KEY)


# --- bare read ---------------------------------------------------------------------------


def test_bare_read_unset_says_not_set(host: _Host) -> None:
    result = _run(transform(_event("!merch")))
    assert "No merch store link is set" in _reply(result)
    assert host.kv.calls == [("get", URL_KEY)]
    assert result is not None
    assert result.payload["channel_id"] == "12345"
    assert (result.platform, result.actor) == ("twitch", "viewer-1")


def test_bare_read_needs_no_badge(host: _Host) -> None:
    host.kv.store[URL_KEY] = _GOOD_URL.encode()
    assert _say(host, "!merch") == f"Merch store: {_GOOD_URL}"


# --- set / read / reset ------------------------------------------------------------------


def test_set_then_read(host: _Host) -> None:
    assert (
        _say(host, f"!merch set {_GOOD_URL}", badges=_MOD) == f"Merch store link set: {_GOOD_URL}"
    )
    assert host.kv.store == {URL_KEY: _GOOD_URL.encode()}
    assert _say(host, "!merch") == f"Merch store: {_GOOD_URL}"


def test_set_overwrites_existing(host: _Host) -> None:
    host.kv.store[URL_KEY] = b"https://old.example.com/x"
    _say(host, f"!merch set {_GOOD_URL}", badges=_MOD)
    assert host.kv.store[URL_KEY] == _GOOD_URL.encode()


def test_set_by_broadcaster_only_badge(host: _Host) -> None:
    badges = {"is_mod": False, "is_broadcaster": True}
    assert "Merch store link set" in _say(host, f"!merch set {_GOOD_URL}", badges=badges)


def test_reset_clears_link(host: _Host) -> None:
    host.kv.store[URL_KEY] = _GOOD_URL.encode()
    assert _say(host, "!merch reset", badges=_MOD) == "Merch store link cleared."
    assert host.kv.store == {}
    assert "No merch store link" in _say(host, "!merch")


def test_reset_when_unset_is_idempotent(host: _Host) -> None:
    assert _say(host, "!merch reset", badges=_MOD) == "Merch store link cleared."
    assert host.kv.calls == [("delete", URL_KEY)]


def test_set_without_url_replies_usage(host: _Host) -> None:
    assert _say(host, "!merch set", badges=_MOD) == "Usage: !merch set <url>"
    assert host.kv.calls == []


def test_reset_with_trailing_text_replies_usage(host: _Host) -> None:
    host.kv.store[URL_KEY] = _GOOD_URL.encode()
    assert _say(host, "!merch reset now", badges=_MOD) == "Usage: !merch reset"
    assert host.kv.store[URL_KEY] == _GOOD_URL.encode()


@pytest.mark.parametrize("text", ["!merch add x", "!merch list", "!merch enable ai"])
def test_other_verbs_reply_usage_without_kv(text: str, host: _Host) -> None:
    assert _say(host, text, badges=_MOD).startswith("Usage: !merch")
    assert host.kv.calls == []


def test_unknown_token_replies_with_parser_message(host: _Host) -> None:
    reply = _say(host, "!merch bogus")
    assert "unknown option" in reply
    assert host.kv.calls == []


# --- mod gate fails closed ---------------------------------------------------------------

_NON_MOD_BADGES = [
    pytest.param(None, id="no-badge-keys"),
    pytest.param({"is_mod": False, "is_broadcaster": False}, id="both-false"),
    pytest.param({"is_mod": "true", "is_broadcaster": False}, id="string-true"),
    pytest.param({"is_mod": "false"}, id="string-false"),
    pytest.param({"is_mod": 1, "is_broadcaster": 1}, id="int-one"),
    pytest.param({"is_mod": None, "is_broadcaster": None}, id="none"),
]


@pytest.mark.parametrize("badges", _NON_MOD_BADGES)
def test_set_denied_fail_closed(badges: dict[str, Any] | None, host: _Host) -> None:
    reply = _say(host, f"!merch set {_GOOD_URL}", badges=badges)
    assert reply == "Only the broadcaster or a moderator can change the merch store link."
    assert host.kv.calls == []
    assert host.kv.store == {}


@pytest.mark.parametrize("badges", _NON_MOD_BADGES)
def test_reset_denied_fail_closed(badges: dict[str, Any] | None, host: _Host) -> None:
    host.kv.store[URL_KEY] = _GOOD_URL.encode()
    reply = _say(host, "!merch reset", badges=badges)
    assert "Only the broadcaster or a moderator" in reply
    assert host.kv.calls == []
    assert host.kv.store[URL_KEY] == _GOOD_URL.encode()


def test_denied_logs_without_url_or_actor(host: _Host) -> None:
    _say(host, f"!merch set {_GOOD_URL}", badges=None)
    assert any(m == "merch.denied" for _, m, _f in host.log_calls)


def test_is_privileged_strict_bool() -> None:
    assert _is_privileged({"is_mod": True}) is True
    assert _is_privileged({"is_broadcaster": True}) is True
    assert _is_privileged({}) is False
    assert _is_privileged({"is_mod": "false", "is_broadcaster": False}) is False
    assert _is_privileged({"is_mod": 1}) is False


# --- URL validation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://merch.example.com/penguinzplays",
        "http://example.org",
        "HTTPS://Example.COM/Path?x=1&y=2#frag",
        "https://sub.domain.example.co.uk:8443/a/b",
        "https://streamlabs.com/user-name_1",
        "https://xn--bcher-kva.example/x",
        "https://" + "a" * 60 + ".com/" + "p" * 400,
    ],
)
def test_valid_urls_accepted(url: str, host: _Host) -> None:
    assert validate_url(url) == url


@pytest.mark.parametrize(
    ("url", "needle"),
    [
        ("ftp://example.com/x", "https://"),
        ("javascript:alert(1)", "https://"),
        ("example.com/merch", "https://"),
        ("//example.com/x", "https://"),
        ("https://user:pass@example.com/x", "username or password"),
        ("https://user@example.com/x", "username or password"),
        ("https://@example.com/x", "username or password"),
        ("https://example.com@evil.com/x", "username or password"),
        ("https://", "host name"),
        ("https:///path", "host name"),
        ("https://localhost/x", "host name"),
        ("https://-bad.example.com/x", "host name"),
        ("https://exa_mple.com/x", "host name"),
        ("https://example..com/x", "host name"),
        ("https://bücher.example/x", "host name"),
        ("https://" + "a" * 64 + ".com/x", "host name"),
        ("https://" + ".".join(["abcdefghi"] * 30) + "/x", "host name"),
        ("https://127.0.0.1/x", "IP address"),
        ("https://10.0.0.1:8080/x", "IP address"),
        ("https://example.com:notaport/x", "well-formed"),
        ("https://example.com:99999/x", "well-formed"),
        ("https://[::1/x", "well-formed"),
        ("https://exa mple.com/x", "spaces or control"),
        ("https://example.com/a b", "spaces or control"),
        ("https://example.com/\tx", "spaces or control"),
        ("https://example.com/\x00x", "spaces or control"),
        ("https://example.com/\x7fx", "spaces or control"),
        ("https://example.com/\u200bx", "spaces or control"),
        ("https://example.com\\@evil.com", "spaces or control"),
        ("", "empty"),
        ("https://example.com/" + "p" * MAX_URL_LEN, "too long"),
    ],
)
def test_invalid_urls_rejected_with_explicit_reason(url: str, needle: str, host: _Host) -> None:
    with pytest.raises(UrlRejectedError, match=needle):
        validate_url(url)


def test_max_length_boundary(host: _Host) -> None:
    ok = "https://example.com/" + "p" * (MAX_URL_LEN - len("https://example.com/"))
    assert len(ok) == MAX_URL_LEN
    assert validate_url(ok) == ok
    with pytest.raises(UrlRejectedError, match="too long"):
        validate_url(ok + "p")


@pytest.mark.parametrize(
    ("url", "needle"),
    [
        ("ftp://example.com/x", "https://"),
        ("https://user:pass@example.com/x", "username or password"),
        ("https://example.com/" + "p" * 600, "too long"),
        ("https://example.com:badport/x", "well-formed"),
    ],
)
def test_set_rejects_bad_url_with_explicit_reply_and_stores_nothing(
    url: str, needle: str, host: _Host
) -> None:
    reply = _say(host, f"!merch set {url}", badges=_MOD)
    assert needle in reply
    assert host.kv.calls == []
    assert host.kv.store == {}
    assert any(m == "merch.url_rejected" for _, m, _f in host.log_calls)


def test_set_rejects_url_with_embedded_space(host: _Host) -> None:
    reply = _say(host, "!merch set https://example.com/a b", badges=_MOD)
    assert "spaces or control" in reply
    assert host.kv.store == {}


def test_rejected_set_keeps_previous_link(host: _Host) -> None:
    host.kv.store[URL_KEY] = _GOOD_URL.encode()
    _say(host, "!merch set ftp://nope.example.com", badges=_MOD)
    assert host.kv.store[URL_KEY] == _GOOD_URL.encode()


def test_malformed_url_logs_pii_free_debug(host: _Host) -> None:
    _say(host, "!merch set https://secret-host.example.com:badport/x", badges=_MOD)
    malformed = [c for c in host.log_calls if c[1] == "merch.url_malformed"]
    assert len(malformed) == 1
    assert "secret-host" not in malformed[0][2]
    assert "ValueError" in malformed[0][2]


# --- kv failures fail loud ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "text", "badges"),
    [
        ("get", "!merch", None),
        ("set", f"!merch set {_GOOD_URL}", _MOD),
        ("delete", "!merch reset", _MOD),
    ],
)
def test_kv_failure_fails_loud_with_reply_and_error_log(
    op: str, text: str, badges: dict[str, Any] | None, host: _Host
) -> None:
    host.kv.fail_ops.add(op)
    reply = _say(host, text, badges=badges)
    assert "Something went wrong" in reply
    errors = [c for c in host.log_calls if c[0] == 0]
    assert len(errors) == 1
    assert errors[0][1] == "merch.kv_failure"
    assert "_HostKvError" in errors[0][2]
    assert host.kv.store == {}


@pytest.mark.parametrize("raw", [b"\xff\xfe", b"", b"ftp://example.com/x", b"not a url"])
def test_corrupt_stored_value_fails_loud_and_is_not_overwritten(raw: bytes, host: _Host) -> None:
    host.kv.store[URL_KEY] = raw
    reply = _say(host, "!merch")
    assert "Something went wrong" in reply
    assert host.kv.store[URL_KEY] == raw
    assert any(c[0] == 0 and c[1] == "merch.kv_failure" for c in host.log_calls)


# --- PII-free logs -----------------------------------------------------------------------


def test_logs_never_contain_url_text_or_actor(host: _Host) -> None:
    secret_url = "https://secret-tips.example.com/SECRETPATH-9f3"
    _say(host, f"!merch set {secret_url}", badges=_MOD)
    _say(host, "!merch")
    _say(host, f"!merch set {secret_url}", badges=None)
    _say(host, "!merch set https://u:SECRETPW@example.com/x", badges=_MOD)
    _say(host, "!merch reset", badges=_MOD)
    host.kv.fail_ops.add("get")
    _say(host, "!merch")
    assert host.log_calls  # denominator: logging actually happened
    blob = " ".join(f"{m} {f}" for _, m, f in host.log_calls)
    for forbidden in ("SECRETPATH", "SECRETPW", "secret-tips", "viewer-1", "set https"):
        assert forbidden not in blob


# --- eager imports -----------------------------------------------------------------------


def test_host_imports_are_eager_module_attributes() -> None:
    import app

    for name in ("kv", "log", "relay", "feature_enabled"):
        assert hasattr(app, name), f"{name} must be imported at module top (componentize-py)"


# --- dispatch ----------------------------------------------------------------------------


def test_dispatch_relays_text_to_channel(host: _Host) -> None:
    env = _envelope({"channel_id": "chan-9", "text": "Merch store: x"}, platform="discord")
    result = _run(dispatch(env, {}, http_client=None))
    assert host.relay_calls == [("discord", {"channel": "chan-9", "text": "Merch store: x"})]
    assert isinstance(result, DispatchResult)
    assert (result.transport, result.detail) == ("discord", "relayed")
    assert result.sub_type is None
    assert result.http_status is None


def test_dispatch_requires_channel_id(host: _Host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope({"text": "hi"}), {}, http_client=None))
    assert host.relay_calls == []


@pytest.mark.parametrize("text", [None, "", 5])
def test_dispatch_requires_text(text: Any, host: _Host) -> None:
    with pytest.raises(ValueError, match="text"):
        _run(dispatch(_envelope({"channel_id": "c", "text": text}), {}, http_client=None))
    assert host.relay_calls == []


def test_transform_then_dispatch_end_to_end(host: _Host) -> None:
    _say(host, f"!merch set {_GOOD_URL}", badges=_MOD)
    out = _run(transform(_event("!merch", channel_id="chan-1")))
    assert out is not None
    _run(dispatch(_envelope(dict(out.payload)), {}, http_client=None))
    assert host.relay_calls == [
        ("twitch", {"channel": "chan-1", "text": f"Merch store: {_GOOD_URL}"})
    ]
