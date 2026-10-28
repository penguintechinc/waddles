"""Host-native tests for the `birthday` bundle (fake `kv`/`flags`/`relay`/`log` host)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest
from app import (
    DAY_PREFIX,
    MAX_PER_DAY,
    USER_PREFIX,
    dispatch,
    parse_month_day,
    transform,
)
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
        app_id="waddles.core.example.birthday",
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
    assert _run(transform(_event("!birthday today"))) is None
    assert host.kv.calls == []


def test_non_matching_ignored(host: _Host) -> None:
    assert _run(transform(_event("!birthdays"))) is None
    assert _run(transform(_event("hello"))) is None
    assert host.kv.calls == []


def test_kv_keys_dot_separated(host: _Host) -> None:
    _say("!birthday set 10-09")
    assert host.kv.store
    assert all(":" not in k for k in host.kv.store)


def test_dispatch_relays_and_validates(host: _Host) -> None:
    result = _run(transform(_event("!birthday today")))
    assert result is not None
    out = _run(dispatch(_envelope(result), {}, http_client=None))
    assert out.transport == "twitch"
    assert [(p, json.loads(m)) for p, m in host.relay_calls] == [
        ("twitch", {"channel": "chan-1", "text": result.payload["text"]})
    ]
    bad = PlatformEvent("twitch", "chat.message", "a", {"text": ""}, "t")
    with pytest.raises(ValueError):
        _run(dispatch(_envelope(bad), {}, http_client=None))


def _uuid_for(actor: str) -> str:
    import uuid

    ns = uuid.uuid5(uuid.NAMESPACE_URL, "https://waddles.penguintech.io/identity")
    return str(uuid.uuid5(ns, f"twitch:name:{actor}"))


def test_crud_lifecycle(host: _Host) -> None:
    assert "haven't set" in _say("!birthday")
    assert _say("!birthday set 10-09") == "Saved your birthday as 10-09."
    assert _say("!birthday") == "Your birthday is 10-09."
    me = _uuid_for("viewer-1")
    assert host.kv.store[f"{USER_PREFIX}{me}"] == b"10-09"
    assert json.loads(host.kv.store[f"{DAY_PREFIX}10-09"]) == [me]
    # a second user the same day
    assert "Saved" in _say("!birthday set 10-09", actor="viewer-2")
    today = _say("!birthday today")
    assert "(2)" in today and "(you!)" in today
    # changing the date moves the index entry
    assert _say("!birthday set 3-4") == "Saved your birthday as 03-04."
    assert json.loads(host.kv.store[f"{DAY_PREFIX}03-04"]) == [me]
    assert len(json.loads(host.kv.store[f"{DAY_PREFIX}10-09"])) == 1
    assert "(1)" in _say("!birthday today") and "(you!)" not in _say("!birthday today")
    assert _say("!birthday remove") == "Removed your birthday."
    assert f"{USER_PREFIX}{me}" not in host.kv.store
    assert f"{DAY_PREFIX}03-04" not in host.kv.store
    assert _say("!birthday remove") == "You don't have a birthday set."
    assert _say("!birthday today", occurred_at="2026-01-01T12:00:00Z") == "No birthdays today."


def test_keys_use_uuid_never_username(host: _Host) -> None:
    _say("!birthday set 10-09", actor="SomeUserName")
    assert all("someusername" not in k.lower() for k in host.kv.store)
    assert all(
        v.decode() == "10-09" or "someusername" not in v.decode().lower()
        for v in host.kv.store.values()
    )


def test_discord_author_id_is_the_identity(host: _Host) -> None:
    ev = {"author_id": "123456"}
    _say("!birthday set 06-01", platform="discord", actor="name-a", extra=ev)
    assert (
        _say("!birthday", platform="discord", actor="renamed", extra=ev)
        == "Your birthday is 06-01."
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("02-29", "02-29"),
        ("2-9", "02-09"),
        ("12-31", "12-31"),
        ("02-30", None),
        ("13-01", None),
        ("00-10", None),
        ("1-0", None),
        ("abc", None),
        ("", None),
        ("1990-10-09", None),
        ("10/09", None),
    ],
)
def test_parse_month_day(raw: str, expected: str | None) -> None:
    assert parse_month_day(raw) == expected


def test_invalid_set_and_unknown(host: _Host) -> None:
    assert _say("!birthday set 02-30").startswith("Usage:")
    assert _say("!birthday set").startswith("Usage:")
    assert _say("!birthday frobnicate").startswith("Unknown")
    assert host.kv.calls == []


def test_day_cap(host: _Host) -> None:
    host.kv.store[f"{DAY_PREFIX}10-09"] = json.dumps([f"u{i}" for i in range(MAX_PER_DAY)]).encode()
    assert "Too many" in _say("!birthday set 10-09")


def test_bad_event_time_fails_loud(host: _Host) -> None:
    assert "Something went wrong" in _say("!birthday today", occurred_at="not-a-time")
    assert any("kv_failure" in msg for _, msg, _ in host.log_calls)


@pytest.mark.parametrize(
    ("key", "value"),
    [(f"{DAY_PREFIX}10-09", b"{not json"), (USER_PREFIX + "{me}", b"banana")],
)
def test_corrupt_store_fails_loud(key: str, value: bytes, host: _Host) -> None:
    host.kv.store[key.format(me=_uuid_for("viewer-1"))] = value
    assert "Something went wrong" in _say(
        "!birthday today" if key.startswith(DAY_PREFIX) else "!birthday"
    )
    assert any("kv_failure" in msg for _, msg, _ in host.log_calls)


def test_logs_are_pii_free(host: _Host) -> None:
    _say("!birthday set 10-09", actor="SecretName")
    _say("!birthday")
    _say("!birthday today")
    _say("!birthday remove", actor="SecretName")
    blob = _blob(host)
    me = _uuid_for("secretname")
    assert "SecretName" not in blob and "secretname" not in blob.lower()
    assert me not in blob and "10-09" not in blob and "viewer-1" not in blob
