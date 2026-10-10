"""Host-native tests for the `bookmark` bundle (fake `kv`/`flags`/`relay`/`log` host)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

from app import LIST_KEY, MAX_BOOKMARKS, SEQ_KEY, dispatch, transform, validate_url


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Host:
    def __init__(self, kv: FakeKvHost) -> None:
        self.kv = kv
        self.relay_calls: list[tuple[str, Any]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag = True


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    h = _Host(install_fake_kv_host(monkeypatch))
    wit = sys.modules["wit_world"]
    wit.imports.flags = types.SimpleNamespace(enabled=lambda key, default_value: h.flag)  # type: ignore[attr-defined]
    wit.imports.relay = types.SimpleNamespace(  # type: ignore[attr-defined]
        push=lambda provider, msg: h.relay_calls.append((provider, msg))
    )
    wit.imports.log = types.SimpleNamespace(  # type: ignore[attr-defined]
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: h.log_calls.append((lvl, msg, fields_json)),
    )
    return h


def _event(text: str, *, is_mod: bool | None = False) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": "chan-1"}
    if is_mod is not None:
        payload["is_mod"] = is_mod
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload=payload,
        occurred_at="2026-10-09T00:00:00.000Z",
    )


def _envelope(event: PlatformEvent) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.bookmark",
        stage="action",
        event=event,
        ts="2026-10-09T00:00:00.000Z",
    )


def _say(text: str, **kw: Any) -> str:
    result = _run(transform(_event(text, **kw)))
    assert result is not None
    return str(result.payload["text"])


def test_crud_lifecycle(host: _Host) -> None:
    assert _say("!bookmark list") == "No bookmarks saved yet."
    assert _say("!bookmark add https://example.com/a") == "Saved bookmark #1."
    assert _say("!bookmark add https://example.org/b") == "Saved bookmark #2."
    listing = _say("!bookmark list")
    assert (
        "#1 https://example.com/a" in listing and "#2 https://example.org/b" in listing
    )
    assert "Only the broadcaster or a moderator" in _say("!bookmark remove 1")
    assert _say("!bookmark remove 1", is_mod=True) == "Removed bookmark #1."
    assert "#1 " not in _say("!bookmark list")
    assert _say("!bookmark remove 1", is_mod=True) == "No bookmark #1."
    assert host.kv.store[SEQ_KEY] == b"2"


def test_remove_fails_closed_without_role_info(host: _Host) -> None:
    _say("!bookmark add https://example.com/a")
    assert "Only the broadcaster" in _say("!bookmark remove 1", is_mod=None)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "https://localhost/x",
        "https://user:pw@example.com/",
        "https://exa mple.com",
        "https://example.com:notaport/",
        "https://example.com/" + "a" * 600,
    ],
)
def test_invalid_urls_rejected(url: str, host: _Host) -> None:
    assert validate_url(url) is not None
    if url and " " not in url:
        assert _say(f"!bookmark add {url}").startswith("Can't add")
    assert LIST_KEY not in host.kv.store


def test_duplicate_and_cap(host: _Host) -> None:
    assert _say("!bookmark add https://example.com/a") == "Saved bookmark #1."
    assert "already bookmarked" in _say("!bookmark add https://example.com/a")
    for i in range(2, MAX_BOOKMARKS + 1):
        _say(f"!bookmark add https://example.com/{i}")
    assert "full" in _say("!bookmark add https://example.com/overflow")
    assert host.kv.store[SEQ_KEY] == str(MAX_BOOKMARKS).encode()


def test_usage_unknown_and_nonmatching(host: _Host) -> None:
    assert _say("!bookmark").startswith("Usage:")
    assert _say("!bookmark frobnicate").startswith("Unknown")
    assert _run(transform(_event("!bookmarks list"))) is None
    assert _run(transform(_event("hello"))) is None
    assert host.kv.calls == []


def test_flag_off_suppresses(host: _Host) -> None:
    host.flag = False
    assert _run(transform(_event("!bookmark list"))) is None


def test_kv_keys_dot_separated(host: _Host) -> None:
    _say("!bookmark add https://example.com/a")
    assert all(":" not in k for k in host.kv.store)


def test_corrupt_store_fails_loud(host: _Host) -> None:
    host.kv.store[LIST_KEY] = b"{not json"
    assert "Something went wrong" in _say("!bookmark list")
    assert any("kv_failure" in msg for _, msg, _ in host.log_calls)


def test_logs_are_pii_free(host: _Host) -> None:
    _say("!bookmark add https://secret-site.example/path?token=abc")
    _say("!bookmark list")
    blob = " ".join(f"{msg} {fields}" for _, msg, fields in host.log_calls)
    assert (
        "secret-site" not in blob and "token=abc" not in blob and "viewer-1" not in blob
    )


def test_dispatch_relays_and_validates(host: _Host) -> None:
    result = _run(transform(_event("!bookmark list")))
    assert result is not None
    out = _run(dispatch(_envelope(result), {}, http_client=None))
    assert out.transport == "twitch"
    assert [(p, json.loads(m)) for p, m in host.relay_calls] == [
        ("twitch", {"channel": "chan-1", "text": result.payload["text"]})
    ]
    bad = PlatformEvent("twitch", "chat.message", "a", {"text": ""}, "t")
    with pytest.raises(ValueError):
        _run(dispatch(_envelope(bad), {}, http_client=None))
