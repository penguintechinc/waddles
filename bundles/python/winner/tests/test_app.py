"""Host-native tests for the `winner` bundle (fake `kv`/`flags`/`relay`/`log` host)."""

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
        app_id="waddles.core.example.winner",
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
    assert _run(transform(_event("!winners"))) is None


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
    assert _run(transform(_event("!winner"))) is None


def test_dispatch_relays_and_rejects_bad_payload(host: _Host) -> None:
    out = _run(transform(_event("!winner")))
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


def _enter(n: int) -> list[str]:
    ids = []
    for i in range(n):
        assert "You're in" in _say("!winner enter", actor=f"viewer-{i}")
        ids.append(bundle._actor_uuid(_event("x", actor=f"viewer-{i}")))
    return ids


def test_enter_is_idempotent_and_list_counts(host: _Host) -> None:
    assert _say("!winner list") == "0 entrant(s) so far."
    _enter(3)
    assert "already" in _say("!winner enter", actor="viewer-0")
    assert _say("!winner list") == "3 entrant(s) so far."


def test_draw_picks_from_entrants_mod_only(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = _enter(4)
    assert _say("!winner") == bundle._DENIED_MSG
    monkeypatch.setattr(bundle, "_pick_winner", lambda e: e[2])
    assert _say("!winner", is_mod=True) == (
        f"\U0001f3c6 The winner is entrant {ids[2][:8]}! (out of 4)"
    )
    assert _say("!winner list") == "4 entrant(s) so far."  # draw leaves list intact


def test_real_random_draw_is_always_an_entrant(host: _Host) -> None:
    ids = _enter(5)
    tags = {i[:8] for i in ids}
    for _ in range(40):
        reply = _say("!winner", is_mod=True)
        assert any(t in reply for t in tags)


def test_empty_draw_is_explicit(host: _Host) -> None:
    assert _say("!winner", is_mod=True) == bundle._NO_ENTRANTS_MSG


def test_reset_mod_only_clears(host: _Host) -> None:
    _enter(2)
    assert _say("!winner reset") == bundle._DENIED_MSG
    assert _say("!winner list") == "2 entrant(s) so far."
    assert _say("!winner reset", is_mod=True) == "Entrant list cleared."
    assert _say("!winner list") == "0 entrant(s) so far."


def test_fails_closed_without_role_info(host: _Host) -> None:
    _enter(1)
    assert _say("!winner", is_mod=None) == bundle._DENIED_MSG
    assert _say("!winner reset", is_mod=None) == bundle._DENIED_MSG


def test_cap_usage_unknown(host: _Host) -> None:
    host.kv.store["winner.entrants"] = (
        '["' + '","'.join(str(uuid.uuid4()) for _ in range(bundle.MAX_ENTRANTS)) + '"]'
    ).encode()
    assert "full" in _say("!winner enter")
    assert _say("!winner enter now") == bundle._USAGE
    assert "Unknown" in _say("!winner frobnicate")


def test_stored_entrants_are_uuids_never_usernames(host: _Host) -> None:
    _say("!winner enter", actor="SecretName")
    stored = host.kv.store["winner.entrants"].decode()
    assert "secretname" not in stored.lower()
    assert uuid.UUID(__import__("json").loads(stored)[0])
    assert all(":" not in k for k in host.kv.store)
    assert "secretname" not in _log_text(host)


def test_corrupt_list_fails_loud_not_empty(host: _Host) -> None:
    for bad in (b"not json", b'{"a": 1}', b"[1, 2]"):
        host.kv.store["winner.entrants"] = bad
        assert _say("!winner list") == bundle._UNAVAILABLE_MSG
        assert _say("!winner enter") == bundle._UNAVAILABLE_MSG
        assert host.kv.store["winner.entrants"] == bad  # never overwritten with []
    assert "corrupt_entrants" in _log_text(host)


def test_kv_backend_error_is_logged_and_answered(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(key: str, value: bytes, ttl: int) -> None:
        raise RuntimeError("backend down")

    monkeypatch.setattr(sys.modules["wit_world"].imports.kv, "set", boom)
    assert _say("!winner enter") == bundle._UNAVAILABLE_MSG
    assert [m for lvl, m, _ in host.log_calls if lvl == 0] == ["winner.kv_failure"]
