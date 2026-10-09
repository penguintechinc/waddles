"""Host-native tests for the `faq` bundle (fake `kv`/`flags`/`relay`/`log` host)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest
from app import ENTRIES_KEY, MAX_ENTRIES, dispatch, transform, validate_answer
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
        app_id="waddles.core.example.faq",
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
    assert _run(transform(_event("!faq list"))) is None
    assert host.kv.calls == []


def test_non_matching_ignored(host: _Host) -> None:
    assert _run(transform(_event("!faqs"))) is None
    assert _run(transform(_event("hello"))) is None
    assert host.kv.calls == []


def test_kv_keys_dot_separated(host: _Host) -> None:
    _say("!faq set rules be kind", is_mod=True)
    assert host.kv.store
    assert all(":" not in k for k in host.kv.store)


def test_dispatch_relays_and_validates(host: _Host) -> None:
    result = _run(transform(_event("!faq list")))
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
    assert _say("!faq list") == "No FAQ entries yet."
    assert "Only the broadcaster or a moderator" in _say("!faq set rules be kind")
    assert ENTRIES_KEY not in host.kv.store
    assert _say("!faq set Rules be kind to everyone", is_mod=True) == "Saved FAQ entry 'rules'."
    assert _say("!faq set schedule Mon-Fri 8pm UTC", is_mod=True) == "Saved FAQ entry 'schedule'."
    assert _say("!faq rules") == "be kind to everyone"
    assert _say("!faq RULES") == "be kind to everyone"
    listing = _say("!faq list")
    assert "rules" in listing and "schedule" in listing and "(2)" in listing
    assert _say("!faq set rules new rules", is_mod=True) == "Saved FAQ entry 'rules'."
    assert _say("!faq rules") == "new rules"
    assert "Only the broadcaster" in _say("!faq remove rules")
    assert _say("!faq remove rules", is_mod=True) == "Removed FAQ entry 'rules'."
    assert _say("!faq rules") == "No such FAQ entry. Try !faq list."
    assert _say("!faq remove rules", is_mod=True) == "No FAQ entry 'rules'."


def test_set_fails_closed_without_role_info(host: _Host) -> None:
    assert "Only the broadcaster" in _say("!faq set a b", is_mod=None)
    assert "Only the broadcaster" in _say("!faq remove a", is_mod=None)
    assert host.kv.calls == []


@pytest.mark.parametrize("answer", ["", "!ban everyone", "/timeout x", "x" * 400, "bad\x07bell"])
def test_invalid_answers_rejected(answer: str, host: _Host) -> None:
    assert validate_answer(answer) is not None
    if answer and "\x07" not in answer:
        assert _say(f"!faq set k {answer}", is_mod=True).startswith("Can't set")
    assert ENTRIES_KEY not in host.kv.store


def test_invalid_and_reserved_keys(host: _Host) -> None:
    assert "reserved" in _say("!faq set list hi", is_mod=True)
    assert "reserved" in _say("!faq set remove hi", is_mod=True)
    assert "keys must be" in _say("!faq set bad/key hi", is_mod=True)
    assert "keys must be" in _say("!faq set " + "k" * 40 + " hi", is_mod=True)
    assert _say("!faq set onlykey", is_mod=True).startswith("Can't set")
    assert ENTRIES_KEY not in host.kv.store
    assert _say("!faq").startswith("Usage:")


def test_cap(host: _Host) -> None:
    for i in range(MAX_ENTRIES):
        assert "Saved" in _say(f"!faq set k{i} answer", is_mod=True)
    assert "full" in _say("!faq set extra answer", is_mod=True)
    assert "Saved" in _say("!faq set k0 changed", is_mod=True)


def test_corrupt_store_fails_loud(host: _Host) -> None:
    host.kv.store[ENTRIES_KEY] = b"[1, 2"
    assert "Something went wrong" in _say("!faq list")
    assert any("kv_failure" in msg for _, msg, _ in host.log_calls)


def test_logs_are_pii_free(host: _Host) -> None:
    _say("!faq set mysecretkey my-secret-answer here", is_mod=True)
    _say("!faq mysecretkey")
    _say("!faq list")
    blob = _blob(host)
    assert "mysecretkey" not in blob and "my-secret-answer" not in blob
    assert "viewer-1" not in blob
