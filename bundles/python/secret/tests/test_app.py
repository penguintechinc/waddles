"""Host-native tests for the `secret` bundle: ordered flow, unsupported platform, PII-free logs.

A fake `wit_world` stands in for the WIT host imports (`flags`, `log`, `relay`,
`kv`), same pattern as `bundles/python/slap/tests/test_app.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types

import pytest

from app import SecretFlowError, _target_key, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SECRET_TEXT = "the-launch-code-is-4242"
USERNAME = "TargetPerson"
TOKEN = "fake-link-value"
TARGET_UUID = "11111111-1111-1111-1111-111111111111"
CONFIG = {"hub_api_url": "https://hub-api.penguintech.cloud", "webui_url": "https://waddles.example"}


def _run(coro):
    return asyncio.run(coro)


def _event(text: str, platform: str = "discord") -> PlatformEvent:
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor="sender-1",
        payload={"text": text, "channel_id": "555", "message_id": "999"},
        occurred_at="2026-10-09T00:00:00.000Z",
    )


def _envelope(event: PlatformEvent, community: str | None = "7") -> StageEnvelope:
    return StageEnvelope(
        tenant="t", community=community, app_id="a", stage="action", event=event,
        ts="2026-10-09T00:00:00.000Z", target_app_id=None, trace_context=None,
    )


class FakeHttp:
    def __init__(self, status: int = 201, raises: Exception | None = None, events=None) -> None:
        self.status, self.raises, self.calls, self.events = status, raises, [], events

    async def post(self, url, **kw):
        self.calls.append((url, kw))
        if self.events is not None:
            self.events.append("store")
        if self.raises:
            raise self.raises
        return {"status": self.status, "body": json.dumps({"token": TOKEN}).encode()}


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch):
    """Fake host: flags on, kv identity index seeded, relay/log recorded in one ordered list."""
    events: list[str] = []
    relayed: list[dict] = []
    logs: list[tuple[str, str, str]] = []
    state = types.SimpleNamespace(
        events=events, relayed=relayed, logs=logs, fail_op=None, flags=True,
        kv={"c.7." + _target_key(USERNAME): json.dumps(
            {"uuid": TARGET_UUID, "platform_user_id": "424242"}).encode()},
    )

    def push(provider, msg):
        d = json.loads(msg)
        if state.fail_op == d["op"]:
            raise RuntimeError("boom")
        events.append(d["op"])
        relayed.append(d)

    wit = types.ModuleType("wit_world")
    wit.imports = types.SimpleNamespace(
        flags=types.SimpleNamespace(enabled=lambda key, default_value: state.flags),
        relay=types.SimpleNamespace(push=push),
        kv=types.SimpleNamespace(get=lambda k: state.kv.get(k)),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, fields: logs.append((lvl, msg, fields)),
        ),
    )
    monkeypatch.setitem(sys.modules, "wit_world", wit)
    return state


def _secret_event(platform: str = "discord") -> PlatformEvent:
    out = _run(transform(_event(f"!secret @{USERNAME} {SECRET_TEXT}", platform)))
    assert out is not None
    return out


def test_flow_order_store_then_delete_then_dm(host) -> None:
    http = FakeHttp(events=host.events)
    _run(dispatch(_envelope(_secret_event()), CONFIG, http_client=http))
    assert host.events == ["store", "chat.delete", "dm.send", "chat.send"]
    delete, dm, reply = host.relayed
    assert delete["channel"] == "555" and delete["message_id"] == "999"
    assert dm["user_id"] == "424242"
    assert f"https://waddles.example/secret#{TOKEN}" in dm["text"]
    assert SECRET_TEXT not in dm["text"] and reply["text"] == "Secret delivered by DM."
    url, kw = http.calls[0]
    assert url.endswith("/api/v1/one-time-secrets")
    body = json.loads(kw["body"])
    assert body == {"communityId": 7, "targetUserUuid": TARGET_UUID, "message": SECRET_TEXT}
    assert "Authorization" in kw["secret_refs"]


def test_store_failure_never_deletes_or_dms(host) -> None:
    http = FakeHttp(status=500, events=host.events)
    with pytest.raises(SecretFlowError):
        _run(dispatch(_envelope(_secret_event()), CONFIG, http_client=http))
    assert host.events == ["store", "chat.send"]
    assert "left as-is" in host.relayed[0]["text"]


def test_delete_failure_skips_dm(host) -> None:
    host.fail_op = "chat.delete"
    with pytest.raises(SecretFlowError):
        _run(dispatch(_envelope(_secret_event()), CONFIG, http_client=FakeHttp()))
    assert "dm.send" not in host.events
    assert "NOT delivered" in host.relayed[-1]["text"]


def test_dm_failure_fails_loud(host) -> None:
    host.fail_op = "dm.send"
    with pytest.raises(SecretFlowError):
        _run(dispatch(_envelope(_secret_event()), CONFIG, http_client=FakeHttp()))
    assert "DM failed" in host.relayed[-1]["text"]


def test_unsupported_platform_stores_and_deletes_nothing(host) -> None:
    http = FakeHttp()
    with pytest.raises(SecretFlowError) as ei:
        _run(dispatch(_envelope(_secret_event("twitch")), CONFIG, http_client=http))
    assert ei.value.step == "unsupported_platform"
    assert http.calls == []
    assert host.events == ["chat.send"]
    assert "isn't supported" in host.relayed[0]["text"]


def test_unlinked_target_fails_loud_before_store(host) -> None:
    host.kv.clear()
    http = FakeHttp()
    with pytest.raises(SecretFlowError) as ei:
        _run(dispatch(_envelope(_secret_event()), CONFIG, http_client=http))
    assert ei.value.step == "target_unlinked" and http.calls == []
    assert "chat.delete" not in host.events


@pytest.mark.parametrize("text", ["!secret", "!secret onlyuser", "!secret bad$name hi"])
def test_malformed_gets_usage_without_echo(host, text: str) -> None:
    out = _run(transform(_event(text)))
    assert out is not None and out.payload["command"] == "usage"
    _run(dispatch(_envelope(out), CONFIG, http_client=FakeHttp()))
    assert host.relayed[0]["text"] == "Usage: !secret <username> <message>"


def test_flag_off_and_non_match_drop(host) -> None:
    assert _run(transform(_event("hello"))) is None
    host.flags = False
    assert _run(transform(_event("!secret a b"))) is None


def test_logs_are_pii_free(host) -> None:
    ev = _secret_event()
    _run(dispatch(_envelope(ev), CONFIG, http_client=FakeHttp()))
    host.fail_op = "dm.send"
    with pytest.raises(SecretFlowError):
        _run(dispatch(_envelope(ev), CONFIG, http_client=FakeHttp(raises=None)))
    with pytest.raises(SecretFlowError):
        _run(dispatch(_envelope(ev), CONFIG, http_client=FakeHttp(raises=RuntimeError(SECRET_TEXT))))
    blob = repr(host.logs)
    assert host.logs
    for needle in (SECRET_TEXT, USERNAME, USERNAME.lower(), TOKEN, TARGET_UUID, "424242",
                   hashlib.sha256(USERNAME.lower().encode()).hexdigest()):
        assert needle not in blob
