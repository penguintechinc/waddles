"""Host-native tests for the `event` bundle (`!event` / `!rsvp`) -- fake `wit_world`, shared kv fake."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any, cast

import pytest
from app import (
    _NO_IDENTITY_MSG,
    _PERMISSION_DENIED_MSG,
    _USAGE,
    MAX_EVENTS,
    MAX_NAME_LEN,
    MAX_RSVPS,
    MAX_WHEN_LEN,
    dispatch,
    transform,
)
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

UUID_A = "11111111-2222-3333-4444-555555555555"
UUID_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Host:
    """Shared kv fake plus recorded relay/log calls and a flag switch."""

    def __init__(self, kv: FakeKvHost) -> None:
        self.kv = kv
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag_enabled = True

    def last_reply(self) -> str:
        return cast(str, json.loads(self.relay_calls[-1][1])["text"])


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    kv_host = install_fake_kv_host(monkeypatch)
    state = _Host(kv_host)
    wit = sys.modules["wit_world"]
    wit.imports.flags = types.SimpleNamespace(enabled=lambda key, default_value: state.flag_enabled)
    wit.imports.relay = types.SimpleNamespace(push=lambda p, m: state.relay_calls.append((p, m)))
    wit.imports.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields: state.log_calls.append((lvl, msg, fields)),
    )
    return state


def _event(
    text: str, *, actor: str | None = UUID_A, mod: bool | None = None, channel_id: str | None = "c1"
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": channel_id}
    if mod is not None:
        payload["is_mod"] = mod
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at="2026-10-09T00:00:00.000Z",
    )


def _say(
    host: _Host,
    text: str,
    *,
    actor: str | None = UUID_A,
    mod: bool | None = None,
    community: str | None = "comm-1",
) -> str:
    """Run transform then dispatch; return the relayed reply text."""
    out = _run(transform(_event(text, actor=actor, mod=mod)))
    assert out is not None, text
    env = StageEnvelope(
        tenant="t",
        community=community,
        app_id="waddles.core.example.event",
        stage="action",
        event=out,
        ts="2026-10-09T00:00:00.000Z",
    )
    _run(dispatch(env, {}, http_client=None))
    return host.last_reply()


def _key(key: str, community: str | None = "comm-1") -> str:
    return cast(str, _scoped_key(community, key))


def test_create_list_view(host: _Host) -> None:
    assert "Created event #1" in _say(host, '!event create "Movie Night" Fri 8pm', mod=True)
    assert "Created event #2" in _say(host, "!event add picnic Sat noon", mod=True)
    listing = _say(host, "!event")
    assert "#2 picnic (Sat noon)" in listing and "#1 Movie Night (Fri 8pm)" in listing
    assert _say(host, "!event list") == listing
    assert _say(host, "!event view 1").startswith("#1 Movie Night @ Fri 8pm -- yes: 0, no: 0")


def test_empty_list(host: _Host) -> None:
    assert _say(host, "!event list").startswith("No events yet")


def test_rsvp_yes_no_change_and_counts(host: _Host) -> None:
    _say(host, "!event create party tonight", mod=True)
    assert "RSVP recorded" in _say(host, "!rsvp 1 yes", actor=UUID_A)
    _say(host, "!rsvp 1 no", actor=UUID_B)
    assert "yes: 1, no: 1" in _say(host, "!event view 1")
    _say(host, "!rsvp 1 no", actor=UUID_A)  # change answer, not double count
    assert "yes: 0, no: 2" in _say(host, "!event view 1")


def test_rsvp_stored_by_uuid_never_username(host: _Host) -> None:
    _say(host, "!event create party tonight", mod=True)
    _say(host, "!rsvp 1 yes", actor=UUID_A.upper())  # canonicalized
    stored = json.loads(host.kv.store[_key("event.rsvp.1")])
    assert list(stored) == [UUID_A]


@pytest.mark.parametrize("actor", ["viewer-1", "SomeUser", None, ""])
def test_rsvp_non_uuid_actor_is_refused_not_stored(host: _Host, actor: str | None) -> None:
    _say(host, "!event create party tonight", mod=True)
    assert _say(host, "!rsvp 1 yes", actor=actor) == _NO_IDENTITY_MSG
    assert _key("event.rsvp.1") not in host.kv.store
    blob = b"".join(host.kv.store.values()).decode()
    assert "viewer-1" not in blob and "SomeUser" not in blob


def test_rsvp_unknown_event(host: _Host) -> None:
    assert _say(host, "!rsvp 9 yes") == "No event #9"
    assert _say(host, "!event view 9") == "No event #9"


def test_create_and_remove_are_mod_only_fail_closed(host: _Host) -> None:
    assert _say(host, "!event create a b", mod=False) == _PERMISSION_DENIED_MSG
    assert _say(host, "!event create a b") == _PERMISSION_DENIED_MSG  # no badge field at all
    assert _say(host, "!event remove 1", mod=False) == _PERMISSION_DENIED_MSG
    assert _key("event.index") not in host.kv.store


def test_remove_deletes_record_rsvps_and_index(host: _Host) -> None:
    _say(host, "!event create party tonight", mod=True)
    _say(host, "!rsvp 1 yes")
    assert _say(host, "!event delete 1", mod=True) == "Removed event #1"
    assert _key("event.rec.1") not in host.kv.store
    assert _key("event.rsvp.1") not in host.kv.store
    assert _say(host, "!event list").startswith("No events yet")
    assert _say(host, "!event remove 1", mod=True) == "No event #1"


def test_ids_are_not_reused_after_remove(host: _Host) -> None:
    _say(host, "!event create a b", mod=True)
    _say(host, "!event remove 1", mod=True)
    assert "Created event #2" in _say(host, "!event create c d", mod=True)


def test_event_cap(host: _Host) -> None:
    for i in range(MAX_EVENTS):
        _say(host, f"!event create e{i} soon", mod=True)
    assert "limit reached" in _say(host, "!event create more soon", mod=True)


def test_rsvp_cap_blocks_new_voters_only(host: _Host) -> None:
    _say(host, "!event create party tonight", mod=True)
    full = {f"00000000-0000-0000-0000-{i:012d}": "yes" for i in range(MAX_RSVPS)}
    host.kv.store[_key("event.rsvp.1")] = json.dumps(full).encode()
    assert "is full" in _say(host, "!rsvp 1 yes", actor=UUID_A)
    existing = next(iter(full))
    assert "RSVP recorded" in _say(host, "!rsvp 1 no", actor=existing)


@pytest.mark.parametrize(
    "text",
    [
        "!event create",
        "!event create onlyname",
        '!event create "unterminated when',
        f"!event create {'n' * (MAX_NAME_LEN + 1)} soon",
        f"!event create n {'w' * (MAX_WHEN_LEN + 1)}",
        "!event view",
        "!event view abc",
        "!event view 0",
        "!event view 1234567890",
        "!event bogus",
        "!event list extra",
        "!rsvp",
        "!rsvp 1",
        "!rsvp 1 maybe",
        "!rsvp x yes",
    ],
)
def test_malformed_commands_reply_usage(host: _Host, text: str) -> None:
    assert _say(host, text, mod=True) == _USAGE


def test_control_chars_rejected(host: _Host) -> None:
    assert _say(host, "!event create a b\x00c", mod=True) == _USAGE


@pytest.mark.parametrize("text", ["!eventful", "event", "!rsvps 1 yes", "", "hi !event"])
def test_non_matching_ignored(host: _Host, text: str) -> None:
    assert _run(transform(_event(text))) is None


def test_non_chat_payload_ignored(host: _Host) -> None:
    ev = PlatformEvent(platform="twitch", event_type="x", actor=None, payload={}, occurred_at="")
    assert _run(transform(ev)) is None


def test_flag_off_suppresses(host: _Host) -> None:
    host.flag_enabled = False
    assert _run(transform(_event("!event list"))) is None


def test_none_community_is_valid_sentinel(host: _Host) -> None:
    assert "Created event #1" in _say(host, "!event create a b", mod=True, community=None)
    assert _key("event.index", None) in host.kv.store


def test_kv_keys_are_colon_free(host: _Host) -> None:
    _say(host, "!event create a b", mod=True)
    _say(host, "!rsvp 1 yes")
    assert host.kv.store
    for key in host.kv.store:
        assert ":" not in key


def test_corrupt_state_fails_loud_with_chat_reply(host: _Host) -> None:
    host.kv.store[_key("event.index")] = b"not json"
    out = _run(transform(_event("!event list")))
    env = StageEnvelope(tenant="t", community="comm-1", app_id="x", stage="action", event=out, ts="")
    with pytest.raises(RuntimeError, match="event list failed"):
        _run(dispatch(env, {}, http_client=None))
    assert "temporarily unavailable" in host.last_reply()
    assert any(lvl == 0 for lvl, _m, _f in host.log_calls)


def test_dispatch_requires_channel_and_known_command(host: _Host) -> None:
    def env(payload: dict[str, Any]) -> StageEnvelope:
        ev = PlatformEvent(
            platform="twitch", event_type="x", actor=UUID_A, payload=payload, occurred_at=""
        )
        return StageEnvelope(tenant="t", community=None, app_id="x", stage="action", event=ev, ts="")

    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(env({"command": "list"}), {}, http_client=None))
    with pytest.raises(ValueError, match="unrecognized"):
        _run(dispatch(env({"command": "nope", "channel_id": "c"}), {}, http_client=None))


def test_logs_are_pii_free(host: _Host) -> None:
    _say(host, '!event create "SecretName" SecretWhen', mod=True)
    _say(host, "!rsvp 1 yes", actor=UUID_A)
    _say(host, "!rsvp 1 yes", actor="viewer-1")
    assert host.log_calls
    for _lvl, msg, fields in host.log_calls:
        blob = msg + fields
        for secret in ("SecretName", "SecretWhen", UUID_A, "viewer-1"):
            assert secret not in blob
