"""Host-native tests for the `lfg` bundle (fake `kv`/`flags`/`relay`/`log` host)."""

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

from app import GROUPS_KEY, MAX_MEMBERS, SEQ_KEY, dispatch, transform


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
        app_id="waddles.core.example.lfg",
        stage="action",
        event=event,
        ts="2026-10-09T00:00:00.000Z",
    )


def _say(text: str, **kw: Any) -> str:
    result = _run(transform(_event(text, **kw)))
    assert result is not None
    return str(result.payload["text"])


def test_crud_lifecycle(host: _Host) -> None:
    assert "No open groups" in _say("!lfg list")
    assert "Created group #1" in _say("!lfg create need 2 for raid", actor="alice")
    assert "#1 need 2 for raid (1/20)" in _say("!lfg list")
    assert "Joined group #1 (2/20)" in _say("!lfg join 1", actor="bob")
    assert "already in" in _say("!lfg join 1", actor="bob")
    assert "Left group #1" in _say("!lfg leave 1", actor="bob")
    assert "not in group" in _say("!lfg leave 1", actor="bob")
    assert "Group #1 disbanded" in _say("!lfg leave 1", actor="alice")
    assert "No open groups" in _say("!lfg list")
    assert host.kv.store[SEQ_KEY] == b"1"


def test_remove_owner_or_mod_only(host: _Host) -> None:
    _say("!lfg create raid", actor="alice")
    assert "Only the group's owner" in _say("!lfg remove 1", actor="bob")
    assert "Removed group #1" in _say("!lfg remove 1", actor="bob", is_mod=True)
    _say("!lfg create raid again", actor="alice")
    assert "Removed group #2" in _say("!lfg remove 2", actor="alice")


def test_remove_fails_closed_without_role_info(host: _Host) -> None:
    _say("!lfg create raid", actor="alice")
    assert "Only the group's owner" in _say("!lfg remove 1", actor="bob", is_mod=None)


def test_one_group_per_owner_and_full_group(host: _Host) -> None:
    _say("!lfg create a", actor="alice")
    assert "already have an open group" in _say("!lfg create b", actor="alice")
    for i in range(MAX_MEMBERS - 1):
        _say("!lfg join 1", actor=f"user{i}")
    assert "is full" in _say("!lfg join 1", actor="late")


@pytest.mark.parametrize(
    "cmd", ["!lfg create", "!lfg create " + "x" * 201, "!lfg join", "!lfg join abc"]
)
def test_validation(cmd: str, host: _Host) -> None:
    reply = _say(cmd)
    assert reply.startswith(("Can't create", "Usage"))
    assert GROUPS_KEY not in host.kv.store


def test_missing_group_usage_and_nonmatching(host: _Host) -> None:
    assert "No open group #9" in _say("!lfg join 9")
    assert _say("!lfg").startswith("Usage:")
    assert _say("!lfg wat").startswith("Unknown")
    assert _run(transform(_event("!lfgx list"))) is None


def test_flag_off_and_key_charset(host: _Host) -> None:
    _say("!lfg create a")
    assert all(":" not in k for k in host.kv.store)
    host.flag = False
    assert _run(transform(_event("!lfg list"))) is None


def test_members_stored_as_uuid_not_username(host: _Host) -> None:
    _say("!lfg create a", actor="SecretName")
    raw = host.kv.store[GROUPS_KEY].decode()
    assert "secretname" not in raw.lower()
    assert uuid.UUID(json.loads(raw)[0]["owner"])


def test_discord_actor_uses_author_id(host: _Host) -> None:
    _say("!lfg create a", actor="NameA", platform="discord", author_id="42")
    reply = _say("!lfg join 1", actor="RenamedA", platform="discord", author_id="42")
    assert "already in" in reply


def test_corrupt_store_fails_loud(host: _Host) -> None:
    host.kv.store[GROUPS_KEY] = b"[[1]]"
    assert "Something went wrong" in _say("!lfg list")
    assert any("kv_failure" in msg for _, msg, _ in host.log_calls)


def test_logs_are_pii_free(host: _Host) -> None:
    _say("!lfg create top secret plans", actor="SecretName")
    _say("!lfg join 1", actor="OtherName")
    blob = " ".join(f"{msg} {fields}" for _, msg, fields in host.log_calls).lower()
    assert "secret" not in blob and "othername" not in blob


def test_dispatch_relays_and_validates(host: _Host) -> None:
    result = _run(transform(_event("!lfg list")))
    assert result is not None
    out = _run(dispatch(_envelope(result), {}, http_client=None))
    assert out.transport == "twitch"
    assert [(p, json.loads(m)) for p, m in host.relay_calls] == [
        ("twitch", {"channel": "chan-1", "text": result.payload["text"]})
    ]
    bad = PlatformEvent("twitch", "chat.message", "a", {"text": ""}, "t")
    with pytest.raises(ValueError):
        _run(dispatch(_envelope(bad), {}, http_client=None))
