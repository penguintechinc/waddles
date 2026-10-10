"""Host-native tests for the `death` bundle (fake `kv`/`flags`/`relay`/`log` host)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

import app as bundle
from app import dispatch, transform


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
        app_id="waddles.core.example.death",
        stage="action",
        event=event,
        ts="2026-10-09T00:00:00.000Z",
    )


def _say(text: str, **kw: Any) -> str:
    result = _run(transform(_event(text, **kw)))
    assert result is not None
    return str(result.payload["text"])


def _log_text(host: _Host) -> str:
    return " ".join(f"{m} {f}" for _, m, f in host.log_calls).lower()


def test_non_command_and_prefix_collision_ignored(host: _Host) -> None:
    assert _run(transform(_event("hello"))) is None
    assert _run(transform(_event("!deaths"))) is None


def test_missing_text_ignored_and_wiring_exports(host: _Host) -> None:
    notext = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="a",
        payload={"channel_id": "chan-1"},
        occurred_at="t",
    )
    assert _run(transform(notext)) is None
    import _entry_wiring

    assert _entry_wiring.bundle_transform is transform
    assert _entry_wiring.bundle_dispatch is dispatch


def test_flag_off_returns_none(host: _Host) -> None:
    host.flag = False
    assert _run(transform(_event("!death"))) is None


def test_dispatch_relays_and_rejects_bad_payload(host: _Host) -> None:
    out = _run(transform(_event("!death")))
    assert out is not None
    result = _run(dispatch(_envelope(out), {}, http_client=None))
    assert result.detail == "relayed"
    assert host.relay_calls[0][0] == "twitch"
    assert json.loads(host.relay_calls[0][1])["channel"] == "chan-1"
    notext = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="a",
        payload={"channel_id": "chan-1"},
        occurred_at="t",
    )
    with pytest.raises(ValueError):
        _run(dispatch(_envelope(notext), {}, http_client=None))
    nochan = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="a",
        payload={"text": "hi"},
        occurred_at="t",
    )
    with pytest.raises(ValueError):
        _run(dispatch(_envelope(nochan), {}, http_client=None))


def test_bare_reads_for_viewer_increments_for_mod(host: _Host) -> None:
    assert _say("!death") == "\u2620\ufe0f Deaths: 0"
    assert host.kv.store == {}
    assert _say("!death", is_mod=True) == "\u2620\ufe0f Deaths: 1"
    assert _say("!death", is_mod=True) == "\u2620\ufe0f Deaths: 2"
    assert _say("!death") == "\u2620\ufe0f Deaths: 2"
    assert _say("!death list") == "\u2620\ufe0f Deaths: 2"
    assert list(host.kv.store) == ["death.count"]


def test_add_sub_set_reset_mod_only(host: _Host) -> None:
    for cmd in ("add", "sub", "reset", "set 5"):
        assert _say(f"!death {cmd}") == bundle._DENIED_MSG
    assert host.kv.store == {}
    assert _say("!death add", is_mod=True).endswith("1")
    assert _say("!death set 41", is_mod=True).endswith("41")
    assert _say("!death sub", is_mod=True).endswith("40")
    assert _say("!death reset", is_mod=True).endswith("0")


def test_sub_floor_and_set_bounds(host: _Host) -> None:
    assert "already 0" in _say("!death sub", is_mod=True)
    assert "between 0 and" in _say("!death set -3", is_mod=True)
    assert "between 0 and" in _say(f"!death set {bundle.MAX_DEATHS + 1}", is_mod=True)
    assert "whole number" in _say("!death set abc", is_mod=True)
    assert "whole number" in _say("!death set", is_mod=True)
    host.kv.store["death.count"] = str(bundle.MAX_DEATHS).encode()
    assert "capped" in _say("!death add", is_mod=True)


def test_fails_closed_without_role_info(host: _Host) -> None:
    assert _say("!death add", is_mod=None) == bundle._DENIED_MSG
    assert _say("!death", is_mod=None).endswith("0")
    assert host.kv.store == {}


def test_broadcaster_flag_counts_as_privileged(host: _Host) -> None:
    ev = _event("!death", is_mod=None)
    ev.payload["is_broadcaster"] = True
    out = _run(transform(ev))
    assert out is not None and out.payload["text"].endswith("1")


def test_usage_and_unknown_verb(host: _Host) -> None:
    assert "Unknown" in _say("!death frobnicate")
    assert _say("!death list extra") == bundle._USAGE
    assert _say("!death reset now", is_mod=True) == bundle._USAGE


def test_corrupt_value_fails_loud_not_zero(host: _Host) -> None:
    host.kv.store["death.count"] = b"not-a-number"
    assert _say("!death") == bundle._UNAVAILABLE_MSG
    host.kv.store["death.count"] = b"-4"
    assert _say("!death list") == bundle._UNAVAILABLE_MSG
    text = _log_text(host)
    assert "corrupt_count" in text and "valueerror" in text
    assert host.kv.store["death.count"] == b"-4"


def test_kv_backend_error_is_logged_and_answered(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(key: str) -> None:
        raise RuntimeError("backend down")

    monkeypatch.setattr(sys.modules["wit_world"].imports.kv, "get", boom)
    assert _say("!death") == bundle._UNAVAILABLE_MSG
    errors = [m for lvl, m, _ in host.log_calls if lvl == 0]
    assert errors == ["death.kv_failure"]
    assert "runtimeerror" in _log_text(host)


def test_keys_valid_and_logs_pii_free(host: _Host) -> None:
    _say("!death", is_mod=True, actor="SecretName")
    _say("!death set 7", is_mod=True, actor="SecretName")
    assert all(":" not in k for k in host.kv.store)
    assert "secretname" not in _log_text(host)
