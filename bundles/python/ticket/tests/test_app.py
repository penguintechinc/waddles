"""Host-native tests for the `ticket` bundle (fake `kv`/`flags`/`relay`/`log` host)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from typing import Any

import pytest
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

from app import MAX_OPEN_PER_USER, OPEN_KEY, SEQ_KEY, dispatch, transform


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
    author_id: str | None = None,
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": "chan-1"}
    if author_id is not None:
        payload["author_id"] = author_id
    if is_mod is not None:
        payload["is_mod"] = is_mod
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at="2026-10-09T00:00:00.000Z",
    )


def _envelope(event: PlatformEvent) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.ticket",
        stage="action",
        event=event,
        ts="2026-10-09T00:00:00.000Z",
    )


def _say(text: str, **kw: Any) -> str:
    result = _run(transform(_event(text, **kw)))
    assert result is not None
    return str(result.payload["text"])


def test_crud_lifecycle(host: _Host) -> None:
    assert "Ticket #1 created" in _say("!ticket printer on fire", actor="alice")
    assert _say("!ticket status 1", actor="alice") == "Ticket #1: open."
    assert "No ticket #1" in _say(
        "!ticket status 1", actor="mallory"
    )  # not the creator
    assert _say("!ticket status 1", actor="mod", is_mod=True) == "Ticket #1: open."
    assert "Only the broadcaster" in _say("!ticket list")
    assert "#1 printer on fire" in _say("!ticket list", is_mod=True)
    assert "Only the broadcaster" in _say("!ticket close 1")
    assert _say("!ticket close 1", is_mod=True) == "Closed ticket #1."
    assert "already closed" in _say("!ticket close 1", is_mod=True)
    assert _say("!ticket status 1", actor="alice") == "Ticket #1: closed."
    assert _say("!ticket list", is_mod=True) == "No open tickets."
    assert host.kv.store[SEQ_KEY] == b"1"


def test_closed_ticket_gets_ttl(host: _Host) -> None:
    _say("!ticket broken")
    _say("!ticket close 1", is_mod=True)
    ttls = [
        c[1][2] for c in host.kv.calls if c[0] == "set" and c[1][0] == "ticket.item.1"
    ]
    assert ttls == [0, 30 * 24 * 60 * 60]


def test_per_user_cap_frees_on_close(host: _Host) -> None:
    for i in range(MAX_OPEN_PER_USER):
        assert "created" in _say(f"!ticket issue {i}", actor="alice")
    assert "already have" in _say("!ticket another", actor="alice")
    assert "created" in _say("!ticket from bob", actor="bob")
    _say("!ticket close 1", is_mod=True)
    assert "created" in _say("!ticket another", actor="alice")


def test_close_and_list_fail_closed_without_role_info(host: _Host) -> None:
    _say("!ticket broken")
    assert "Only the broadcaster" in _say("!ticket close 1", is_mod=None)
    assert "Only the broadcaster" in _say("!ticket list", is_mod=None)


@pytest.mark.parametrize(
    "cmd",
    ["!ticket status", "!ticket status abc", "!ticket close", "!ticket x" + "y" * 301],
)
def test_validation(cmd: str, host: _Host) -> None:
    reply = _say(cmd, is_mod=True)
    assert reply.startswith(("Usage", "Can't create"))
    assert OPEN_KEY not in host.kv.store


def test_unknown_ticket_usage_and_nonmatching(host: _Host) -> None:
    assert "No ticket #9" in _say("!ticket close 9", is_mod=True)
    assert "No ticket #9" in _say("!ticket status 9")
    assert _say("!ticket").startswith("Usage:")
    assert _run(transform(_event("!tickets list"))) is None
    assert (
        len(host.kv.calls) == 2
    )  # only the two lookups above; non-matching text costs none


def test_flag_off_and_key_charset(host: _Host) -> None:
    _say("!ticket broken")
    assert all(":" not in k for k in host.kv.store)
    host.flag = False
    assert _run(transform(_event("!ticket again"))) is None


def test_creator_stored_as_uuid_not_username(host: _Host) -> None:
    _say("!ticket broken", actor="SecretName")
    raw = host.kv.store["ticket.item.1"].decode()
    assert "secretname" not in raw.lower()
    assert uuid.UUID(json.loads(raw)["creator"])


def test_corrupt_store_fails_loud(host: _Host) -> None:
    host.kv.store[OPEN_KEY] = b"nope"
    assert "Something went wrong" in _say("!ticket list", is_mod=True)
    assert any("kv_failure" in msg for _, msg, _ in host.log_calls)


def test_logs_are_pii_free(host: _Host) -> None:
    _say("!ticket my password is hunter2", actor="SecretName")
    _say("!ticket status 1", actor="SecretName")
    _say("!ticket close 1", is_mod=True)
    blob = " ".join(f"{msg} {fields}" for _, msg, fields in host.log_calls).lower()
    assert "hunter2" not in blob and "secretname" not in blob


def test_dispatch_relays_and_validates(host: _Host) -> None:
    result = _run(transform(_event("!ticket")))
    assert result is not None
    out = _run(dispatch(_envelope(result), {}, http_client=None))
    assert out.transport == "twitch"
    assert [(p, json.loads(m)) for p, m in host.relay_calls] == [
        ("twitch", {"channel": "chan-1", "text": result.payload["text"]})
    ]
    bad = PlatformEvent("twitch", "chat.message", "a", {"text": ""}, "t")
    with pytest.raises(ValueError):
        _run(dispatch(_envelope(bad), {}, http_client=None))
