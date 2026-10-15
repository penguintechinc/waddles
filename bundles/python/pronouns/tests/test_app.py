"""Host-native tests for the `pronouns` bundle's `transform`/`dispatch` logic.

No WASM -- a fake `wit_world` stands in for the WIT host imports (`kv`/`flags`/`relay`/`log`),
same approach as `bundles/python/uptime/tests/test_app.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

from app import (
    KEY_PREFIX,
    MAX_PRONOUNS_LEN,
    DispatchResult,
    _validate_pronouns,
    dispatch,
    transform,
)

# Mirrors `core/bundle_host_kv/src/scope.rs::is_allowed_key_byte` -- notably NOT `:`.
_ALLOWED_KV_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _hash(basis: str) -> str:
    return KEY_PREFIX + hashlib.sha256(basis.encode()).hexdigest()


# The caller on Twitch is `viewer-1` (name basis); on Discord it is author id 111.
TWITCH_SELF = _hash("twitch:name:viewer-1")
TWITCH_OTHER = _hash("twitch:name:otheruser")
DISCORD_SELF = _hash("discord:111")
DISCORD_OTHER = _hash("discord:222")


class _HostKvError(Exception):
    """Shaped like the generated WIT `Err` (`.value` holds the error union)."""

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value


class _FakeKv:
    """In-memory `kv` stand-in validating keys like the real host, with scripted failures."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_ops: set[str] = set()

    def _check(self, op: str, key: str) -> None:
        if not key or len(key) > 256 or any(c not in _ALLOWED_KV_KEY_CHARS for c in key):
            raise _HostKvError(f"backend: invalid key {key!r}")
        self.calls.append((op, key))
        if op in self.fail_ops:
            raise _HostKvError(f"backend: scripted {op} failure")

    def get(self, key: str) -> bytes | None:
        self._check("get", key)
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        self._check("set", key)
        self.store[key] = bytes(value)

    def delete(self, key: str) -> None:
        self._check("delete", key)
        self.store.pop(key, None)


class _Host:
    """Recorded fake-host state shared with the test body."""

    def __init__(self) -> None:
        self.kv = _FakeKv()
        self.relay_calls: list[tuple[str, dict[str, Any]]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag_enabled = True
        self.flag_keys: list[str] = []


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    """Fake WIT host: flag on, in-memory kv, recording relay/log."""
    h = _Host()

    def _flag(key: str, default_value: bool) -> bool:
        h.flag_keys.append(key)
        return h.flag_enabled

    wit = types.ModuleType("wit_world")
    wit.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=_flag),
        kv=h.kv,
        relay=types.SimpleNamespace(push=lambda p, m: h.relay_calls.append((p, json.loads(m)))),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, fields: h.log_calls.append((lvl, msg, fields)),
        ),
    )
    monkeypatch.setitem(sys.modules, "wit_world", wit)
    return h


def _event(
    text: Any,
    *,
    platform: str = "twitch",
    actor: str | None = "viewer-1",
    author_id: str | None = None,
    channel_id: str | None = "12345",
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": channel_id}
    if author_id is not None:
        payload["author_id"] = author_id
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at="2026-10-13T00:00:00.000Z",
    )


def _discord(text: Any, author_id: str | None = "111") -> PlatformEvent:
    return _event(text, platform="discord", actor="Viewer One", author_id=author_id)


def _envelope(payload: dict[str, Any], platform: str = "twitch") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.pronouns",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload=payload,
            occurred_at="2026-10-13T00:00:00.000Z",
        ),
        ts="2026-10-13T00:00:00.000Z",
    )


def _reply(result: PlatformEvent | None) -> str:
    assert result is not None
    return str(result.payload["text"])


def _say(event: PlatformEvent) -> str:
    return _reply(_run(transform(event)))


# --- routing -----------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["hello", "", "!time", "!pronounsx", "pronouns", "!pronoun"])
def test_non_matching_text_returns_none_without_flag_or_kv(text: str, host: _Host) -> None:
    assert _run(transform(_event(text))) is None
    assert host.flag_keys == []
    assert host.kv.calls == []


@pytest.mark.parametrize("text", [None, 42, ["!pronouns"]])
def test_non_string_text_returns_none(text: Any, host: _Host) -> None:
    assert _run(transform(_event(text))) is None
    assert host.kv.calls == []


def test_command_match_is_case_and_whitespace_insensitive(host: _Host) -> None:
    assert "haven't set" in _say(_event("  !PRONOUNS  "))


# --- set own -----------------------------------------------------------------------------


def test_set_own_stores_under_hashed_key_and_replies(host: _Host) -> None:
    result = _run(transform(_event("!pronouns set he/him")))
    assert _reply(result) == "Your pronouns are now: he/him"
    assert host.kv.store == {TWITCH_SELF: b"he/him"}
    assert result is not None
    assert result.payload["channel_id"] == "12345"
    assert result.platform == "twitch"
    assert result.actor == "viewer-1"


def test_set_is_not_mod_gated_plain_user_succeeds(host: _Host) -> None:
    event = _event("!pronouns set she/her")
    event.payload["is_mod"] = False
    event.payload["is_broadcaster"] = False
    assert _say(event) == "Your pronouns are now: she/her"
    assert host.kv.store == {TWITCH_SELF: b"she/her"}


def test_set_overwrites_previous_value(host: _Host) -> None:
    _say(_event("!pronouns set he/him"))
    _say(_event("!pronouns set they/them"))
    assert host.kv.store == {TWITCH_SELF: b"they/them"}


def test_set_trims_surrounding_whitespace(host: _Host) -> None:
    assert _say(_event("!pronouns set   xe/xem  ")) == "Your pronouns are now: xe/xem"
    assert host.kv.store == {TWITCH_SELF: b"xe/xem"}


def test_set_accepts_exactly_max_length(host: _Host) -> None:
    text = "x" * MAX_PRONOUNS_LEN
    assert _say(_event(f"!pronouns set {text}")) == f"Your pronouns are now: {text}"


def test_set_preserves_free_text_with_spaces_and_unicode(host: _Host) -> None:
    assert "any pronouns :)" in _say(_event("!pronouns set any pronouns :)"))
    assert host.kv.store[TWITCH_SELF] == b"any pronouns :)"


def test_discord_set_uses_author_id_key(host: _Host) -> None:
    assert _say(_discord("!pronouns set they/them")) == "Your pronouns are now: they/them"
    assert host.kv.store == {DISCORD_SELF: b"they/them"}


# --- read own ----------------------------------------------------------------------------


def test_read_own_after_set(host: _Host) -> None:
    _say(_event("!pronouns set he/him"))
    assert _say(_event("!pronouns")) == "Your pronouns: he/him"


def test_read_own_when_unset(host: _Host) -> None:
    reply = _say(_event("!pronouns"))
    assert "haven't set pronouns" in reply
    assert "!pronouns set" in reply
    assert host.kv.calls == [("get", TWITCH_SELF)]


def test_read_own_is_case_insensitive_on_actor(host: _Host) -> None:
    _say(_event("!pronouns set he/him", actor="Viewer-1"))
    assert _say(_event("!pronouns", actor="VIEWER-1")) == "Your pronouns: he/him"


# --- read other --------------------------------------------------------------------------


def test_read_other_twitch_at_name_resolves_same_key_scheme(host: _Host) -> None:
    _say(_event("!pronouns set she/her", actor="OtherUser"))
    assert TWITCH_OTHER in host.kv.store
    assert _say(_event("!pronouns @OtherUser")) == "OtherUser's pronouns: she/her"


def test_read_other_bare_name(host: _Host) -> None:
    host.kv.store[TWITCH_OTHER] = b"he/him"
    assert _say(_event("!pronouns otheruser")) == "otheruser's pronouns: he/him"


def test_read_other_with_no_pronouns_set(host: _Host) -> None:
    assert _say(_event("!pronouns @nobody")) == "nobody hasn't set pronouns."
    assert host.kv.calls == [("get", _hash("twitch:name:nobody"))]


@pytest.mark.parametrize("mention", ["<@222>", "<@!222>"])
def test_read_other_discord_mention_resolves_same_key_as_author_id(
    mention: str, host: _Host
) -> None:
    _say(_discord("!pronouns set xe/xem", author_id="222"))
    assert DISCORD_OTHER in host.kv.store
    assert _say(_discord(f"!pronouns {mention}")) == "that user's pronouns: xe/xem"


def test_read_other_discord_mention_with_no_pronouns(host: _Host) -> None:
    assert _say(_discord("!pronouns <@222>")) == "that user hasn't set pronouns."


def test_read_other_does_not_write(host: _Host) -> None:
    _say(_event("!pronouns @someone"))
    assert all(op == "get" for op, _ in host.kv.calls)
    assert host.kv.store == {}


def test_discord_typed_name_is_explicit_not_a_guess(host: _Host) -> None:
    reply = _say(_discord("!pronouns @someone"))
    assert "real @mention" in reply
    assert host.kv.calls == []


@pytest.mark.parametrize("token", ["@", "!!!", "a" * 40, "@bad name"])
def test_unrecognizable_target_gets_explicit_reply(token: str, host: _Host) -> None:
    reply = _say(_event(f"!pronouns {token}"))
    assert reply in {
        "I couldn't identify that user - mention them (e.g. @name).",
        "Usage: !pronouns | !pronouns set <text> | !pronouns clear | !pronouns @user",
    }
    assert host.kv.calls == []


def test_discord_mention_on_twitch_is_rejected(host: _Host) -> None:
    assert "couldn't identify that user" in _say(_event("!pronouns <@222>"))
    assert host.kv.calls == []


# --- clear -------------------------------------------------------------------------------


def test_clear_removes_own_pronouns(host: _Host) -> None:
    _say(_event("!pronouns set he/him"))
    assert _say(_event("!pronouns clear")) == "Your pronouns have been cleared."
    assert host.kv.store == {}
    assert "haven't set" in _say(_event("!pronouns"))


@pytest.mark.parametrize("verb", ["remove", "delete", "reset", "CLEAR"])
def test_clear_aliases(verb: str, host: _Host) -> None:
    host.kv.store[TWITCH_SELF] = b"he/him"
    assert _say(_event(f"!pronouns {verb}")) == "Your pronouns have been cleared."
    assert host.kv.store == {}


def test_clear_when_unset_is_explicit_and_deletes_nothing(host: _Host) -> None:
    assert "nothing to clear" in _say(_event("!pronouns clear"))
    assert all(op != "delete" for op, _ in host.kv.calls)


def test_clear_only_removes_the_callers_own_key(host: _Host) -> None:
    host.kv.store[TWITCH_SELF] = b"he/him"
    host.kv.store[TWITCH_OTHER] = b"she/her"
    _say(_event("!pronouns clear"))
    assert host.kv.store == {TWITCH_OTHER: b"she/her"}


# --- invalid text rejected ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("x" * (MAX_PRONOUNS_LEN + 1), "at most 32 characters"),
        ("he/him\nsecond line", "single line"),
        ("he/\x00him", "printable"),
        ("he/\thim", "printable"),
        ("@everyone", "can't contain"),
        ("<@123>", "can't contain"),
        ("he > him", "can't contain"),
    ],
)
def test_invalid_text_is_rejected_with_explicit_reply_and_no_write(
    text: str, needle: str, host: _Host
) -> None:
    reply = _say(_event(f"!pronouns set {text}"))
    assert needle in reply
    assert host.kv.store == {}
    assert host.kv.calls == []
    assert text not in reply  # a rejected value is never echoed back


def test_invalid_text_does_not_overwrite_existing(host: _Host) -> None:
    host.kv.store[TWITCH_SELF] = b"he/him"
    _say(_event("!pronouns set " + "y" * 100))
    assert host.kv.store == {TWITCH_SELF: b"he/him"}


def test_whitespace_only_set_text_is_usage(host: _Host) -> None:
    assert _say(_event("!pronouns set    ")).startswith("Usage:")
    assert host.kv.calls == []


# --- usage / grammar ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "!pronouns set",
        "!pronouns list",
        "!pronouns add x",
        "!pronouns enable",
        "!pronouns clear now",
        "!pronouns remove now",
        "!pronouns foo bar",
        "!pronouns @a @b",
    ],
)
def test_malformed_replies_with_usage_and_does_not_touch_kv(text: str, host: _Host) -> None:
    assert _say(_event(text)).startswith("Usage: !pronouns")
    assert host.kv.calls == []


# --- no identity -------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["!pronouns", "!pronouns set he/him", "!pronouns clear"])
@pytest.mark.parametrize("actor", [None, "", "   "])
def test_caller_without_identity_gets_explicit_reply(
    text: str, actor: str | None, host: _Host
) -> None:
    assert "couldn't identify you" in _say(_event(text, actor=actor))
    assert host.kv.calls == []


def test_discord_invalid_author_id_falls_back_to_actor_name(host: _Host) -> None:
    _say(_event("!pronouns set he/him", platform="discord", actor="Name", author_id="bad id!"))
    assert host.kv.store == {_hash("discord:name:name"): b"he/him"}


def test_read_other_without_caller_identity_still_works(host: _Host) -> None:
    host.kv.store[TWITCH_OTHER] = b"he/him"
    assert _say(_event("!pronouns @otheruser", actor=None)) == "otheruser's pronouns: he/him"


# --- key hygiene -------------------------------------------------------------------------


def test_keys_are_hashed_colon_free_and_never_contain_the_raw_handle(host: _Host) -> None:
    _say(_event("!pronouns set he/him", actor="SomeRawHandle"))
    _say(_discord("!pronouns set he/him", author_id="987654321"))
    assert len(host.kv.store) == 2
    for key in host.kv.store:
        assert key.startswith("pronouns.")
        assert ":" not in key
        assert all(c in _ALLOWED_KV_KEY_CHARS for c in key)
        assert "somerawhandle" not in key.lower()
        assert "987654321" not in key
        assert len(key) == len("pronouns.") + 64


def test_same_handle_on_different_platforms_gets_different_keys(host: _Host) -> None:
    _say(_event("!pronouns set a", platform="twitch", actor="same"))
    _say(_event("!pronouns set b", platform="kick", actor="same"))
    assert len(host.kv.store) == 2


# --- kv failures fail loud ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "failing_op"),
    [
        ("!pronouns", "get"),
        ("!pronouns @someone", "get"),
        ("!pronouns set he/him", "set"),
        ("!pronouns clear", "get"),
    ],
)
def test_kv_failure_fails_loud_with_reply_and_error_log(
    text: str, failing_op: str, host: _Host
) -> None:
    host.kv.fail_ops.add(failing_op)
    reply = _say(_event(text))
    assert "Something went wrong" in reply
    errors = [c for c in host.log_calls if c[0] == 0]
    assert len(errors) == 1
    assert errors[0][1] == "pronouns.kv_failure"
    assert "_HostKvError" in errors[0][2]
    assert host.kv.store == {}


def test_kv_delete_failure_fails_loud_and_keeps_value(host: _Host) -> None:
    host.kv.store[TWITCH_SELF] = b"he/him"
    host.kv.fail_ops.add("delete")
    assert "Something went wrong" in _say(_event("!pronouns clear"))
    assert host.kv.store == {TWITCH_SELF: b"he/him"}
    assert any(c[0] == 0 and c[1] == "pronouns.kv_failure" for c in host.log_calls)


def test_kv_set_failure_does_not_claim_success(host: _Host) -> None:
    host.kv.fail_ops.add("set")
    reply = _say(_event("!pronouns set he/him"))
    assert "now:" not in reply
    assert "Something went wrong" in reply


@pytest.mark.parametrize("raw", [b"\xff\xfe", b"\xc3\x28"])
def test_corrupt_stored_value_fails_loud_and_is_not_overwritten(raw: bytes, host: _Host) -> None:
    host.kv.store[TWITCH_SELF] = raw
    assert "Something went wrong" in _say(_event("!pronouns"))
    assert host.kv.store[TWITCH_SELF] == raw
    errors = [c for c in host.log_calls if c[0] == 0]
    assert len(errors) == 1
    assert "UnicodeDecodeError" in errors[0][2]


# --- PII-free logs -----------------------------------------------------------------------


def test_logs_never_contain_pronouns_text_actor_or_target(host: _Host) -> None:
    _say(_event("!pronouns set SECRETPRONOUN-9f3", actor="RawActorName"))
    _say(_event("!pronouns", actor="RawActorName"))
    _say(_event("!pronouns @TargetHandleXYZ", actor="RawActorName"))
    _say(_event("!pronouns foo bar baz", actor="RawActorName"))  # classify-non-verb path
    _say(_event("!pronouns set " + "z" * 80 + "LONGSECRET", actor="RawActorName"))
    _say(_discord("!pronouns @TargetHandleXYZ"))
    _say(_event("!pronouns clear", actor="RawActorName"))
    host.kv.store[TWITCH_SELF] = b"\xff"
    host.kv.fail_ops.add("set")
    _say(_event("!pronouns set SECRETPRONOUN-again", actor="RawActorName"))
    host.kv.fail_ops.clear()
    _say(_event("!pronouns", actor="RawActorName"))
    assert host.log_calls  # denominator: logging actually happened
    levels = {c[0] for c in host.log_calls}
    assert {0, 2, 3} <= levels  # ERROR, INFO and DEBUG were all exercised
    blob = " ".join(f"{m} {f}" for _, m, f in host.log_calls).lower()
    for needle in ("secretpronoun", "rawactorname", "targethandlexyz", "longsecret", "viewer"):
        assert needle not in blob


# --- eager imports -----------------------------------------------------------------------


def test_host_imports_are_eager_module_attributes() -> None:
    import app

    for name in ("kv", "log", "relay"):
        assert hasattr(app, name), f"{name} must be imported at module top (componentize-py)"


def test_entry_wiring_exports_both_stage_entrypoints() -> None:
    import _entry_wiring

    assert _entry_wiring.bundle_transform is transform
    assert _entry_wiring.bundle_dispatch is dispatch


# --- dispatch ----------------------------------------------------------------------------


def test_dispatch_relays_text_to_channel(host: _Host) -> None:
    env = _envelope({"channel_id": "chan-9", "text": "Your pronouns: he/him"}, platform="discord")
    result = _run(dispatch(env, {}, http_client=None))
    assert host.relay_calls == [("discord", {"channel": "chan-9", "text": "Your pronouns: he/him"})]
    assert isinstance(result, DispatchResult)
    assert (result.transport, result.detail) == ("discord", "relayed")
    assert result.sub_type is None
    assert result.http_status is None


def test_dispatch_requires_channel_id(host: _Host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope({"text": "hi"}), {}, http_client=None))
    assert host.relay_calls == []


@pytest.mark.parametrize("text", [None, "", 5])
def test_dispatch_requires_text(text: Any, host: _Host) -> None:
    with pytest.raises(ValueError, match="text"):
        _run(dispatch(_envelope({"channel_id": "c", "text": text}), {}, http_client=None))
    assert host.relay_calls == []


def test_transform_then_dispatch_end_to_end(host: _Host) -> None:
    out = _run(transform(_event("!pronouns set he/him", channel_id="chan-1")))
    assert out is not None
    _run(dispatch(_envelope(dict(out.payload)), {}, http_client=None))
    assert host.relay_calls == [
        ("twitch", {"channel": "chan-1", "text": "Your pronouns are now: he/him"})
    ]


def test_validate_pronouns_rejects_empty_directly() -> None:
    # `_classify` never forwards empty text (it replies with usage first), so exercise the
    # validator's own defensive empty branch directly.
    from app import _UserError

    with pytest.raises(_UserError, match="can't be empty"):
        _validate_pronouns("   ")
    assert _validate_pronouns("  he/him ") == "he/him"
