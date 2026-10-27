"""Host-native tests for the `discord` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- mirrors `bundles/python/count/tests/test_app.py`'s fake-`wit_world`
approach (fake `kv`/`relay`/`flags`/`log`), with the fake `kv` validating keys exactly like the
real host so a `:` key can never pass again (gh-631).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest

from app import INVITE_KEY, MAX_URL_LEN, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

_ALLOWED_KV_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)
_URL = "https://discord.gg/abc123"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FakeKv:
    """In-memory `kv` host stand-in with real-host key validation and scripted failures."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_ops: set[str] = set()

    def _check(self, op: str, key: str) -> None:
        if (
            not key
            or len(key) > 256
            or any(c not in _ALLOWED_KV_KEY_CHARS for c in key)
        ):
            raise RuntimeError(f"backend: invalid key {key!r}")
        self.calls.append((op, key))
        if op in self.fail_ops:
            raise RuntimeError(f"scripted {op} failure")

    def get(self, key: str) -> bytes | None:
        self._check("get", key)
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        self._check("set", key)
        self.store[key] = bytes(value)

    def delete(self, key: str) -> None:
        self._check("delete", key)
        self.store.pop(key, None)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Fake WIT host: flag ON by default, real in-memory `kv`, recording `relay`/`log`."""
    fake_kv = _FakeKv()
    relay_calls: list[tuple[str, str]] = []
    log_calls: list[tuple[int, str, str]] = []
    flag_state = {"enabled": True}

    flags_mod = types.SimpleNamespace(
        enabled=lambda key, default_value: flag_state["enabled"]
    )
    relay_mod = types.SimpleNamespace(push=lambda p, msg: relay_calls.append((p, msg)))
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields: log_calls.append((lvl, msg, fields)),
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, kv=fake_kv, relay=relay_mod, log=log_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return types.SimpleNamespace(
        kv=fake_kv, relay_calls=relay_calls, log_calls=log_calls, flag_state=flag_state
    )


def _event(
    text: str, *, mod: Any = False, broadcaster: Any = False, **extra: Any
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": "chan-1", **extra}
    if mod is not None:
        payload["is_mod"] = mod
    if broadcaster is not None:
        payload["is_broadcaster"] = broadcaster
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="PIIACTOR_alice",
        payload=payload,
        occurred_at="2026-10-13T00:00:00.000Z",
    )


def _say(text: str, **role: Any) -> str | None:
    out = _run(transform(_event(text, **role)))
    return None if out is None else str(out.payload["text"])


# --- routing / flag -----------------------------------------------------------------


@pytest.mark.parametrize("text", ["hello", "", "   ", "!discordx", "!count", "discord"])
def test_non_matching_messages_return_none_without_kv(text: str, host: Any) -> None:
    assert _say(text) is None
    assert host.kv.calls == []


def test_non_string_text_returns_none(host: Any) -> None:
    event = PlatformEvent("twitch", "channel.follow", None, {}, "")
    assert _run(transform(event)) is None


def test_command_match_is_case_insensitive(host: Any) -> None:
    assert "No Discord link" in (_say("!DISCORD") or "")


# --- read / set / reset -------------------------------------------------------------


def test_bare_read_unset_is_friendly(host: Any) -> None:
    reply = _say("!discord") or ""
    assert "No Discord link has been set" in reply
    assert host.kv.calls == [("get", INVITE_KEY)]


def test_set_then_read(host: Any) -> None:
    assert _say(f"!discord set {_URL}", mod=True) == "Discord link updated."
    assert host.kv.store[INVITE_KEY] == _URL.encode()
    assert _say("!discord") == f"Join our Discord: {_URL}"


def test_set_preserves_url_case_and_broadcaster_may_set(host: Any) -> None:
    url = "https://discord.com/invite/AbC123"
    assert _say(f"!discord SET {url}", broadcaster=True) == "Discord link updated."
    assert host.kv.store[INVITE_KEY] == url.encode()


def test_set_overwrites_previous(host: Any) -> None:
    _say(f"!discord set {_URL}", mod=True)
    _say("!discord set https://discord.gg/zzz", mod=True)
    assert _say("!discord") == "Join our Discord: https://discord.gg/zzz"


def test_reset_clears(host: Any) -> None:
    _say(f"!discord set {_URL}", mod=True)
    assert _say("!discord reset", mod=True) == "Discord link cleared."
    assert INVITE_KEY not in host.kv.store
    assert "No Discord link" in (_say("!discord") or "")


def test_reset_when_unset_is_idempotent(host: Any) -> None:
    assert _say("!discord reset", broadcaster=True) == "Discord link cleared."


def test_reset_with_trailing_args_is_usage(host: Any) -> None:
    assert _say("!discord reset now", mod=True) == "Usage: !discord reset"
    assert ("delete", INVITE_KEY) not in host.kv.calls


def test_set_without_url_is_usage(host: Any) -> None:
    assert _say("!discord set", mod=True) == "Usage: !discord set <url>"


@pytest.mark.parametrize("text", ["!discord add x", "!discord list", "!discord remove"])
def test_other_verbs_reply_usage(text: str, host: Any) -> None:
    assert (_say(text, mod=True) or "").startswith("Usage: !discord")


def test_unknown_option_replies_with_parser_error(host: Any) -> None:
    reply = _say("!discord bogus") or ""
    assert "unknown option" in reply
    assert "Usage: !discord" in reply


# --- mod gate: fail closed ----------------------------------------------------------

_DENIED_ROLES = [
    {"mod": False, "broadcaster": False},
    {"mod": None, "broadcaster": None},  # no role keys at all (Discord today)
    {"mod": "true", "broadcaster": None},  # strings are not bools
    {"mod": 1, "broadcaster": 1},  # truthy non-bool
    {"mod": None, "broadcaster": "yes"},
]


@pytest.mark.parametrize("role", _DENIED_ROLES)
def test_set_refused_without_strict_bool_badge(role: dict[str, Any], host: Any) -> None:
    assert "Only the broadcaster or a moderator" in (
        _say(f"!discord set {_URL}", **role) or ""
    )
    assert INVITE_KEY not in host.kv.store
    assert host.kv.calls == []


@pytest.mark.parametrize("role", _DENIED_ROLES)
def test_reset_refused_without_strict_bool_badge(
    role: dict[str, Any], host: Any
) -> None:
    host.kv.store[INVITE_KEY] = _URL.encode()
    assert "Only the broadcaster or a moderator" in (
        _say("!discord reset", **role) or ""
    )
    assert host.kv.store[INVITE_KEY] == _URL.encode()
    assert host.kv.calls == []


def test_read_is_public(host: Any) -> None:
    host.kv.store[INVITE_KEY] = _URL.encode()
    assert _say("!discord", mod=None, broadcaster=None) == f"Join our Discord: {_URL}"


# --- URL validation -----------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "notaurl",
        "discord.gg/abc",
        "ftp://discord.gg/abc",
        "javascript:alert(1)",
        "https://",
        "https:///path",
        "https://discord.gg:notaport/x",
        "https://user:pw@discord.gg/x",
        "https://discord.gg@evil.example/x",
        "https://discord.gg/a b",
        "https://discord.gg/" + "x" * MAX_URL_LEN,
    ],
)
def test_invalid_url_rejected_and_not_stored(bad: str, host: Any) -> None:
    reply = _say(f"!discord set {bad}", mod=True) or ""
    assert reply != "Discord link updated."
    assert not reply.startswith("Discord link updated")
    assert INVITE_KEY not in host.kv.store
    assert host.kv.calls == []


def test_control_char_url_rejected(host: Any) -> None:
    assert "single URL" in (
        _say("!discord set https://x.example/a\x07b", mod=True) or ""
    )
    assert INVITE_KEY not in host.kv.store


@pytest.mark.parametrize(
    "url",
    [
        "http://discord.gg/abc",
        "https://discord.com/invite/x",
        "https://www.discord.gg/x",
    ],
)
def test_discord_domains_accepted_without_warning(url: str, host: Any) -> None:
    assert _say(f"!discord set {url}", mod=True) == "Discord link updated."


def test_vanity_domain_accepted_with_soft_warning(host: Any) -> None:
    reply = _say("!discord set https://chat.example.org/join", mod=True) or ""
    assert reply.startswith("Discord link updated.")
    assert "doesn't look like a Discord link" in reply
    assert host.kv.store[INVITE_KEY] == b"https://chat.example.org/join"


def test_lookalike_host_gets_soft_warning(host: Any) -> None:
    reply = _say("!discord set https://notdiscord.gg/x", mod=True) or ""
    assert "doesn't look like a Discord link" in reply


# --- kv failures fail loud ----------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "text", "role"),
    [
        ("get", "!discord", {}),
        ("set", f"!discord set {_URL}", {"mod": True}),
        ("delete", "!discord reset", {"mod": True}),
    ],
)
def test_kv_failure_fails_loud(
    op: str, text: str, role: dict[str, Any], host: Any
) -> None:
    host.kv.fail_ops.add(op)
    reply = _say(text, **role) or ""
    assert "Something went wrong" in reply
    errors = [c for c in host.log_calls if c[0] == 0]
    assert len(errors) == 1
    assert errors[0][1] == "discord.kv_failure"
    assert "RuntimeError" in errors[0][2]


@pytest.mark.parametrize("raw", [b"\xff\xfe", b""])
def test_corrupt_stored_value_fails_loud(raw: bytes, host: Any) -> None:
    host.kv.store[INVITE_KEY] = raw
    assert "Something went wrong" in (_say("!discord") or "")
    assert any(c[1] == "discord.kv_failure" for c in host.log_calls)


# --- kv key hygiene -----------------------------------------------------------------


def test_invite_key_is_literal_dot_separated_and_host_valid() -> None:
    assert INVITE_KEY == "discord.invite"
    assert ":" not in INVITE_KEY
    assert all(ch in _ALLOWED_KV_KEY_CHARS for ch in INVITE_KEY)


# --- dispatch -----------------------------------------------------------------------


def _envelope(platform: str, payload: dict[str, Any]) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.discord",
        stage="action",
        event=PlatformEvent(
            platform, "chat.message", "viewer-1", payload, "2026-10-13T00:00:00Z"
        ),
        ts="2026-10-13T00:00:00Z",
    )


def test_dispatch_relays_text_transform_built(host: Any) -> None:
    out = _run(transform(_event("!discord", mod=None, broadcaster=None)))
    assert out is not None
    envelope = _envelope("twitch", dict(out.payload))
    result = _run(dispatch(envelope, {}, http_client=None))

    provider, message_json = host.relay_calls[0]
    assert provider == "twitch"
    assert json.loads(message_json) == {
        "channel": "chan-1",
        "text": out.payload["text"],
    }
    assert (result.transport, result.detail) == ("twitch", "relayed")
    assert result.sub_type is None
    assert result.http_status is None


def test_dispatch_raises_without_channel_id(host: Any) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(
            dispatch(
                _envelope("twitch", {"channel_id": None, "text": "x"}),
                {},
                http_client=None,
            )
        )
    assert host.relay_calls == []


@pytest.mark.parametrize("text", [None, ""])
def test_dispatch_raises_without_text(text: Any, host: Any) -> None:
    with pytest.raises(ValueError, match="text"):
        _run(
            dispatch(
                _envelope("twitch", {"channel_id": "c", "text": text}),
                {},
                http_client=None,
            )
        )
    assert host.relay_calls == []


# --- PII-free logs ------------------------------------------------------------------


def test_logs_never_contain_url_actor_or_message_text(host: Any) -> None:
    secret_url = "https://vanity.PIIURL.example/PIIPATH"
    _say(f"!discord set {secret_url}", mod=True)
    _say(f"!discord set {secret_url}", mod=False)  # denied
    _say("!discord set PIIRAW_notaurl", mod=True)  # invalid
    _say("!discord PIIRAW_bogus")  # parser error
    _say("!discord reset PIIRAW_extra", mod=True)
    _say("!discord reset", mod=True)
    _say("!discord")
    host.kv.fail_ops.add("get")
    _say("!discord")
    out = _run(transform(_event("!discord")))
    assert out is not None
    _run(dispatch(_envelope("twitch", dict(out.payload)), {}, http_client=None))

    assert host.log_calls, "no log calls recorded -- the PII check would pass vacuously"
    for _lvl, message, fields in host.log_calls:
        blob = f"{message} {fields}".lower()
        for sentinel in ("piiurl", "piipath", "piiraw", "piiactor", "alice", "notaurl"):
            assert sentinel not in blob


def test_malformed_port_raises_internally_and_logs_exc_type_only(host: Any) -> None:
    """The `ValueError` from a bad port is reclassified + propagated (not swallowed to a default)."""
    import app

    with pytest.raises(app._InvalidUrl) as info:
        app._validate_url("https://PIIHOST.example:notaport/x")
    assert isinstance(info.value.__cause__, ValueError)

    reply = _say("!discord set https://PIIHOST.example:notaport/x", mod=True) or ""
    assert "valid URL" in reply
    assert INVITE_KEY not in host.kv.store
    invalid = [f for _l, m, f in host.log_calls if "invalid_url" in m]
    assert invalid and "ValueError" in str(invalid[-1])
    assert "piihost" not in str(host.log_calls).lower()
