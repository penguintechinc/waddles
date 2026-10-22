"""Host-native + kv-integration tests for `wordcount` (charset-enforcing `FakeKvHost`).

Integration here = the real `waddle_sdk.kv` wrappers driving the shared charset-validating
fake kv host (no mocks of the bundle's own logic): increment correctness, pseudonym keying,
isolation between users, and PII-free keys/logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
import uuid
from typing import Any

import app as bundle
import pytest
from app import dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Host:
    def __init__(self, kv: FakeKvHost) -> None:
        self.kv = kv
        self.relay_calls: list[tuple[str, Any]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag_calls: list[str] = []
        self.flag = True


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    h = _Host(install_fake_kv_host(monkeypatch))
    wit = sys.modules["wit_world"]

    def _enabled(key: str, default_value: bool) -> bool:
        h.flag_calls.append(key)
        return h.flag

    wit.imports.flags = types.SimpleNamespace(enabled=_enabled)  # type: ignore[attr-defined]
    wit.imports.relay = types.SimpleNamespace(  # type: ignore[attr-defined]
        push=lambda p, m: h.relay_calls.append((p, m))
    )
    wit.imports.log = types.SimpleNamespace(  # type: ignore[attr-defined]
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, f: h.log_calls.append((lvl, msg, f)),
    )
    return h


def _uid(n: int) -> str:
    return str(uuid.UUID(int=n))


def _tok(n: int) -> str:
    return "{user:" + _uid(n) + "}"


def _ev(
    text: str, n: int = 1, platform: str = "twitch", actor: str | None = None
) -> PlatformEvent:
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor if actor is not None else _tok(n),
        payload={"text": text, "channel_id": "chan-1"},
        occurred_at="2026-10-21T00:00:00.000Z",
    )


def _say(text: str, n: int = 1, **kw: Any) -> str:
    out = _run(transform(_ev(text, n, **kw)))
    assert out is not None
    return str(out.payload["text"])


def _counter(host: _Host, key: str) -> int:
    return int(host.kv.store[key].decode())


def _logs(h: _Host) -> str:
    return " ".join(f"{m} {f}" for _, m, f in h.log_calls).lower()


def test_pseudonym_is_sha256_and_keys_charset_safe(host: _Host) -> None:
    expected = hashlib.sha256(f"wordcount|{_uid(1)}".encode()).hexdigest()
    assert bundle._pseudonym(_uid(1)) == expected
    assert bundle._msgs_key(_uid(1)) == f"wordcount.msgs.{expected}"
    assert bundle._words_key(_uid(1)) == f"wordcount.words.{expected}"
    assert ":" not in bundle._msgs_key(_uid(1)) + bundle._words_key(_uid(1))


def test_each_message_increments_msgs_and_words(host: _Host) -> None:
    assert _run(transform(_ev("hello there world"))) is None
    assert _run(transform(_ev("one two"))) is None
    assert _counter(host, bundle._msgs_key(_uid(1))) == 2
    assert _counter(host, bundle._words_key(_uid(1))) == 5


def test_counters_are_per_user(host: _Host) -> None:
    _run(transform(_ev("a b c", 1)))
    _run(transform(_ev("a", 2)))
    assert _counter(host, bundle._words_key(_uid(1))) == 3
    assert _counter(host, bundle._words_key(_uid(2))) == 1


def test_self_report_counts_the_command_itself(host: _Host) -> None:
    _run(transform(_ev("hi there")))
    assert _say("!wordcount") == "You have sent 2 message(s) and 3 word(s)."


def test_target_report_reads_target_not_caller(host: _Host) -> None:
    _run(transform(_ev("x y z w", 2)))
    reply = _say(f"!wordcount {_tok(2)}", 1)
    assert reply == f"{_tok(2)} has sent 1 message(s) and 4 word(s)."
    assert _tok(2) in reply


def test_unseen_target_reads_zero(host: _Host) -> None:
    assert (
        _say(f"!wordcount {_tok(9)}")
        == f"{_tok(9)} has sent 0 message(s) and 0 word(s)."
    )


def test_raw_handle_rejected_never_hashed_or_echoed(host: _Host) -> None:
    before = dict(host.kv.store)
    reply = _say("!wordcount @SecretName")
    assert reply == bundle._BAD_TARGET_MSG
    assert "secretname" not in reply.lower() + _logs(host)
    assert set(host.kv.store) - set(before) <= {
        bundle._msgs_key(_uid(1)),
        bundle._words_key(_uid(1)),
    }


def test_extra_args_usage(host: _Host) -> None:
    assert _say("!wordcount a b") == bundle._USAGE


def test_flag_off_no_count_no_kv(host: _Host) -> None:
    host.flag = False
    assert _run(transform(_ev("!wordcount"))) is None
    assert _run(transform(_ev("hello"))) is None
    assert host.kv.store == {}
    assert host.kv.calls == []
    assert set(host.flag_calls) == {"waddles.command-wordcount"}


def test_non_token_actor_not_counted_and_self_report_explicit(host: _Host) -> None:
    assert _run(transform(_ev("hello", actor="rawname"))) is None
    assert host.kv.store == {}
    assert _say("!wordcount", actor="rawname") == bundle._NO_SELF_ID
    assert "rawname" not in _logs(host)


def test_non_string_text_ignored(host: _Host) -> None:
    ev = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=_tok(1),
        payload={"channel_id": "c"},
        occurred_at="t",
    )
    assert _run(transform(ev)) is None
    assert host.kv.calls == []


def test_prefix_collision_not_command_but_counted(host: _Host) -> None:
    assert _run(transform(_ev("!wordcounts please"))) is None
    assert _counter(host, bundle._msgs_key(_uid(1))) == 1


def test_kv_keys_and_logs_are_pii_free(host: _Host) -> None:
    _run(transform(_ev("secret words here", 1)))
    _say("!wordcount")
    for key in host.kv.store:
        assert _uid(1) not in key and "secret" not in key
    assert _uid(1) not in _logs(host)
    assert "secret words" not in _logs(host)


def test_kv_failure_on_count_logged_and_command_still_answered(host: _Host) -> None:
    def _boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("scripted increment failure")

    sys.modules["wit_world"].imports.kv.increment = _boom
    reply = _say("!wordcount")
    assert reply == "You have sent 0 message(s) and 0 word(s)."
    assert "wordcount.kv_failure" in _logs(host)


def test_dispatch_relays_and_rejects_bad_payload(host: _Host) -> None:
    out = _run(transform(_ev("!wordcount")))
    assert out is not None
    env = StageEnvelope(
        tenant="t",
        community="c",
        app_id="waddles.core.example.wordcount",
        stage="action",
        event=out,
        ts="2026-10-21T00:00:00.000Z",
    )
    assert _run(dispatch(env, {}, http_client=None)).detail == "relayed"
    assert json.loads(host.relay_calls[0][1])["channel"] == "chan-1"
    import _entry_wiring

    assert _entry_wiring.bundle_transform is transform
    for payload in ({"channel_id": "c"}, {"text": "x"}):
        bad = PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="a",
            payload=payload,
            occurred_at="t",
        )
        with pytest.raises(ValueError):
            _run(
                dispatch(
                    StageEnvelope(
                        tenant="t",
                        community="c",
                        app_id="x",
                        stage="action",
                        event=bad,
                        ts="t",
                    ),
                    {},
                    http_client=None,
                )
            )
