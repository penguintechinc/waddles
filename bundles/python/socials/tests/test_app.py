"""Host-native tests for the `socials` bundle (fake `kv`/`flags`/`relay`/`log` host)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest
from app import LINKS_KEY, MAX_LINKS, dispatch, transform, validate_url
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host


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


def _event(
    text: str,
    *,
    is_mod: bool | None = False,
    actor: str = "viewer-1",
    platform: str = "twitch",
    occurred_at: str = "2026-10-09T00:00:00.000Z",
    extra: dict[str, Any] | None = None,
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": "chan-1"}
    if is_mod is not None:
        payload["is_mod"] = is_mod
    payload.update(extra or {})
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at=occurred_at,
    )


def _envelope(event: PlatformEvent) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.socials",
        stage="action",
        event=event,
        ts="2026-10-09T00:00:00.000Z",
    )


def _say(text: str, **kw: Any) -> str:
    result = _run(transform(_event(text, **kw)))
    assert result is not None
    return str(result.payload["text"])


def _blob(host: _Host) -> str:
    return " ".join(f"{msg} {fields}" for _, msg, fields in host.log_calls)


def test_flag_off_suppresses(host: _Host) -> None:
    host.flag = False
    assert _run(transform(_event("!socials list"))) is None
    assert host.kv.calls == []


def test_non_matching_ignored(host: _Host) -> None:
    assert _run(transform(_event("!socialss"))) is None
    assert _run(transform(_event("hello"))) is None
    assert host.kv.calls == []


def test_kv_keys_dot_separated(host: _Host) -> None:
    _say("!socials set twitch https://twitch.tv/x", is_mod=True)
    assert host.kv.store
    assert all(":" not in k for k in host.kv.store)


def test_dispatch_relays_and_validates(host: _Host) -> None:
    result = _run(transform(_event("!socials list")))
    assert result is not None
    out = _run(dispatch(_envelope(result), {}, http_client=None))
    assert out.transport == "twitch"
    assert [(p, json.loads(m)) for p, m in host.relay_calls] == [
        ("twitch", {"channel": "chan-1", "text": result.payload["text"]})
    ]
    bad = PlatformEvent("twitch", "chat.message", "a", {"text": ""}, "t")
    with pytest.raises(ValueError):
        _run(dispatch(_envelope(bad), {}, http_client=None))


def test_crud_lifecycle(host: _Host) -> None:
    assert _say("!socials") == "No social links configured yet."
    assert "Only the broadcaster or a moderator" in _say("!socials set twitch https://twitch.tv/x")
    assert LINKS_KEY not in host.kv.store
    assert _say("!socials set Twitch https://twitch.tv/x", is_mod=True) == "Saved the twitch link."
    assert (
        _say("!socials set youtube https://youtube.com/@x", is_mod=True)
        == "Saved the youtube link."
    )
    listing = _say("!socials list")
    assert "twitch: https://twitch.tv/x" in listing and "youtube: https://youtube.com/@x" in listing
    assert _say("!socials set twitch https://twitch.tv/y", is_mod=True) == "Saved the twitch link."
    assert "twitch: https://twitch.tv/y" in _say("!socials")
    assert "Only the broadcaster" in _say("!socials remove twitch")
    assert _say("!socials remove twitch", is_mod=True) == "Removed the twitch link."
    assert "twitch:" not in _say("!socials")
    assert _say("!socials remove twitch", is_mod=True) == "No twitch link is set."


def test_mod_verbs_fail_closed_without_role_info(host: _Host) -> None:
    assert "Only the broadcaster" in _say("!socials set twitch https://twitch.tv/x", is_mod=None)
    assert "Only the broadcaster" in _say("!socials remove twitch", is_mod=None)
    assert host.kv.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "https://localhost/x",
        "https://user:pw@example.com/",
        "https://example.com:notaport/",
        "https://example.com/" + "a" * 600,
    ],
)
def test_invalid_urls_rejected(url: str, host: _Host) -> None:
    assert validate_url(url) is not None
    if url:
        assert _say(f"!socials set twitch {url}", is_mod=True).startswith("Can't set")
    assert LINKS_KEY not in host.kv.store


def test_invalid_platform_and_usage(host: _Host) -> None:
    assert "Platform must be" in _say("!socials set bad/name https://example.com", is_mod=True)
    assert _say("!socials set twitch", is_mod=True).startswith("Usage:")
    assert _say("!socials frobnicate").startswith("Unknown")


def test_cap(host: _Host) -> None:
    for i in range(MAX_LINKS):
        assert "Saved" in _say(f"!socials set p{i} https://example.com/{i}", is_mod=True)
    assert "full" in _say("!socials set extra https://example.com/x", is_mod=True)
    assert "Saved" in _say("!socials set p0 https://example.com/new", is_mod=True)


def test_corrupt_store_fails_loud(host: _Host) -> None:
    host.kv.store[LINKS_KEY] = b"{not json"
    assert "Something went wrong" in _say("!socials")
    assert any("kv_failure" in msg for _, msg, _ in host.log_calls)


def test_logs_are_pii_free(host: _Host) -> None:
    _say("!socials set mysecretplat https://secret-site.example/path?token=abc", is_mod=True)
    _say("!socials list")
    blob = _blob(host)
    assert "secret-site" not in blob and "token=abc" not in blob and "mysecretplat" not in blob
    assert "viewer-1" not in blob
