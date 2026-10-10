"""Host-native tests for the `label` bundle (fake `kv`/`flags`/`relay`/`log` host)."""

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

from app import MAX_LABELS_PER_USER, USER_PREFIX, dispatch, transform


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
        app_id="waddles.core.example.label",
        stage="action",
        event=event,
        ts="2026-10-09T00:00:00.000Z",
    )


def _say(text: str, **kw: Any) -> str:
    result = _run(transform(_event(text, **kw)))
    assert result is not None
    return str(result.payload["text"])


def _stored_keys(host: _Host) -> list[str]:
    return [k for k in host.kv.store if USER_PREFIX in k]


def test_crud_lifecycle_by_uuid(host: _Host) -> None:
    target = str(uuid.uuid4())
    short = target[:8]
    assert _say(f"!label list {target}") == f"User {short} has no labels."
    assert "Only the broadcaster" in _say(f"!label add {target} vip")
    assert (
        _say(f"!label add {target} VIP", is_mod=True) == f"Added label to user {short}."
    )
    assert "already has" in _say(f"!label add {target} vip", is_mod=True)
    _say(f"!label add {target} regular", is_mod=True)
    assert _say(f"!label list {target}") == f"User {short} labels: vip, regular"
    assert "Only the broadcaster" in _say(f"!label remove {target} vip")
    assert (
        _say(f"!label remove {target} vip", is_mod=True)
        == f"Removed label from user {short}."
    )
    assert "doesn't have" in _say(f"!label remove {target} vip", is_mod=True)
    _say(f"!label remove {target} regular", is_mod=True)
    assert _stored_keys(host) == []  # key deleted when the last label goes


def test_target_is_stored_as_uuid_never_username(host: _Host) -> None:
    _say("!label add @SecretName trusted", is_mod=True)
    keys = _stored_keys(host)
    assert len(keys) == 1
    assert uuid.UUID(keys[0].split(USER_PREFIX)[1])
    assert "secretname" not in " ".join(host.kv.store).lower()
    assert b"secretname" not in b"".join(host.kv.store.values()).lower()


def test_handle_mention_and_self_resolve_to_same_uuid(host: _Host) -> None:
    _say("!label add @Bob friend", is_mod=True)
    assert "friend" in _say("!label list @bob")
    assert "friend" in _say("!label list", actor="Bob")  # self-list, name-derived
    assert "friend" not in _say("!label list @someoneelse")


def test_discord_mention_matches_author_id(host: _Host) -> None:
    _say("!label add <@42> mod-pick", is_mod=True, platform="discord")
    assert "mod-pick" in _say("!label list <@!42>", platform="discord")
    assert "mod-pick" in _say(
        "!label list", platform="discord", author_id="42", actor="x"
    )


def test_replies_never_echo_target(host: _Host) -> None:
    replies = [
        _say("!label add @SecretName trusted", is_mod=True),
        _say("!label list @SecretName"),
        _say("!label remove @SecretName trusted", is_mod=True),
    ]
    assert all("secretname" not in r.lower() for r in replies)


@pytest.mark.parametrize(
    "cmd",
    [
        "!label add",
        "!label add @bob",
        "!label add @bob " + "x" * 33,
        "!label add @bob bad$chars",
        "!label add !!! vip",
        "!label list ###",
    ],
)
def test_validation(cmd: str, host: _Host) -> None:
    reply = _say(cmd, is_mod=True)
    assert reply.startswith(("Usage", "Can't add", "I couldn't identify"))
    assert _stored_keys(host) == []


def test_label_cap(host: _Host) -> None:
    for i in range(MAX_LABELS_PER_USER):
        assert "Added" in _say(f"!label add @bob l{i}", is_mod=True)
    assert "at most" in _say("!label add @bob extra", is_mod=True)


def test_add_remove_fail_closed_without_role_info(host: _Host) -> None:
    assert "Only the broadcaster" in _say("!label add @bob vip", is_mod=None)
    assert "Only the broadcaster" in _say("!label remove @bob vip", is_mod=None)


def test_usage_unknown_nonmatching_and_flag(host: _Host) -> None:
    assert _say("!label").startswith("Usage:")
    assert _say("!label wat").startswith("Unknown")
    assert _run(transform(_event("!labels list"))) is None
    host.flag = False
    assert _run(transform(_event("!label list"))) is None


def test_key_charset(host: _Host) -> None:
    _say("!label add @bob vip", is_mod=True)
    assert all(":" not in k for k in host.kv.store)


def test_corrupt_store_fails_loud(host: _Host) -> None:
    _say("!label add @bob vip", is_mod=True)
    key = next(iter(host.kv.store))
    host.kv.store[key] = b'{"not": "a list"}'
    assert "Something went wrong" in _say("!label list @bob")
    assert any("kv_failure" in msg for _, msg, _ in host.log_calls)


def test_logs_are_pii_free(host: _Host) -> None:
    _say("!label add @SecretName topsecretlabel", is_mod=True, actor="ModName")
    _say("!label list @SecretName", actor="ModName")
    _say("!label remove @SecretName topsecretlabel", is_mod=True, actor="ModName")
    blob = " ".join(f"{msg} {fields}" for _, msg, fields in host.log_calls).lower()
    assert (
        "secretname" not in blob
        and "topsecretlabel" not in blob
        and "modname" not in blob
    )


def test_dispatch_relays_and_validates(host: _Host) -> None:
    result = _run(transform(_event("!label")))
    assert result is not None
    out = _run(dispatch(_envelope(result), {}, http_client=None))
    assert out.transport == "twitch"
    assert [(p, json.loads(m)) for p, m in host.relay_calls] == [
        ("twitch", {"channel": "chan-1", "text": result.payload["text"]})
    ]
    bad = PlatformEvent("twitch", "chat.message", "a", {"text": ""}, "t")
    with pytest.raises(ValueError):
        _run(dispatch(_envelope(bad), {}, http_client=None))
