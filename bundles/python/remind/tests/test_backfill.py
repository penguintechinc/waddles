"""Backfill coverage for the `remind` bundle: corrupt-store loudness, PII logs, edge parsing.

Complements `test_app.py` with what it does not pin: every flavor of corrupt reminder state logs at
ERROR with only context fields (and a later `set` heals the entry; `list` never mutates it), no
log line in any flow carries the reminder text / typed duration / raw actor, the duration parser
and formatter boundaries, the flag's fail-closed default, TTL on every write, per-user isolation of
`remove`, and the static `_entry_wiring` re-export. `remind` has no moderator-gated verb (every
caller manages only their own reminders), so there is no mod-gate matrix here.

Reuses `test_app.py`'s `fake_host` fixture (shared charset-enforcing `kv` fake + clock/relay/log).
"""

# F811: pytest fixtures re-exported from `test_app.py` are re-bound by the test parameters.
# ruff: noqa: F811
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
from test_app import (  # noqa: F401 - `fake_host` is a fixture re-exported for this module
    _NO_REMINDERS,
    _envelope,
    _FakeHost,
    _relay_text,
    _sample_event,
    _scoped,
    fake_host,
)

import _entry_wiring
import app
from app import (
    _STALE_AFTER_SECONDS,
    FLAG_KEY,
    _counter_key,
    _format_duration,
    _items_key,
    _parse_duration_seconds,
    _pseudonym,
    dispatch,
    transform,
)

CANARY = "CANARYuser9f3a"
ERROR_LEVEL = 0


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _error_logs(host: _FakeHost) -> list[dict[str, Any]]:
    return [
        {"msg": msg, **json.loads(fields)}
        for lvl, msg, fields in host.log_calls
        if lvl == ERROR_LEVEL
    ]


# -- corrupt store: loud (ERROR), pure-read list, heal-on-write
@pytest.mark.parametrize(
    "blob",
    [
        b"\xff\xfe\xfd",  # not UTF-8
        b"not json at all",
        b"{}",  # JSON object, not an array
        b'"a string"',
        b"42",
        json.dumps([1, 2, 3]).encode(),  # array of non-dicts
        json.dumps([{"id": "1", "text": "no due"}]).encode(),  # dict missing `due_ms`
        json.dumps([{"id": "1", "due_ms": 1}]).encode(),  # dict missing `text`
        json.dumps([{"due_ms": 1, "text": "t"}]).encode(),  # dict missing `id`
    ],
    ids=[
        "non-utf8",
        "non-json",
        "object",
        "string",
        "number",
        "non-dict-items",
        "missing-due",
        "missing-text",
        "missing-id",
    ],
)
def test_every_corrupt_blob_is_logged_at_error_and_never_crashes_list(
    fake_host: _FakeHost, blob: bytes
) -> None:
    key = _scoped(_items_key(_pseudonym("viewer-1")))
    fake_host.store[key] = blob

    _run(dispatch(_envelope("list"), {}, http_client=None))

    assert _relay_text(fake_host) == _NO_REMINDERS
    [log_line] = _error_logs(fake_host)
    assert log_line["msg"] == "remind.state_corrupt"
    assert log_line["context"] == "items"
    assert log_line["community"] == "comm-1"
    assert fake_host.store[key] == blob, "`list` is a pure read -- it must not rewrite state"
    assert not [c for c in fake_host.kv_calls if c[0] in {"set", "delete", "increment"}]


def test_corrupt_blob_is_replaced_by_the_next_set_not_merged_or_resurrected(
    fake_host: _FakeHost,
) -> None:
    key = _scoped(_items_key(_pseudonym("viewer-1")))
    fake_host.store[key] = b"garbage"

    _run(dispatch(_envelope("set", arg="1h fresh"), {}, http_client=None))

    stored = json.loads(fake_host.store[key])
    assert [item["text"] for item in stored] == ["fresh"]
    assert len(_error_logs(fake_host)) == 1


def test_corrupt_blob_on_remove_logs_error_and_reports_not_found(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_items_key(_pseudonym("viewer-1")))] = b"garbage"

    _run(dispatch(_envelope("remove", arg="1"), {}, http_client=None))

    assert _relay_text(fake_host) == "No reminder with id 1."
    assert [line["msg"] for line in _error_logs(fake_host)] == ["remind.state_corrupt"]


# -- stored shape, TTL, isolation
def test_set_stores_absolute_due_time_and_refreshes_ttl_on_every_write(
    fake_host: _FakeHost,
) -> None:
    _run(dispatch(_envelope("set", arg="2h30m do the thing"), {}, http_client=None))

    [item] = json.loads(fake_host.store[_scoped(_items_key(_pseudonym("viewer-1")))])
    assert item == {
        "id": "1",
        "due_ms": fake_host.now_ms + (2 * 3600 + 30 * 60) * 1000,
        "text": "do the thing",
        "created_ms": fake_host.now_ms,
    }
    writes = [c for c in fake_host.kv_calls if c[0] in {"set", "increment"}]
    assert [c[1][-1] for c in writes] == [_STALE_AFTER_SECONDS, _STALE_AFTER_SECONDS]
    assert _STALE_AFTER_SECONDS == 30 * 24 * 3600


def test_kv_keys_hold_only_the_pseudonym_never_the_raw_actor(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="5m x", actor=CANARY), {}, http_client=None))

    assert fake_host.store, "expected state to have been written (denominator must be non-zero)"
    for key in fake_host.store:
        assert CANARY.lower() not in key.lower()
    assert _pseudonym(CANARY) in "".join(fake_host.store)
    assert _items_key("p") == "remind.items.p" and _counter_key("p") == "remind.counter.p"


def test_remove_cannot_cancel_another_users_reminder_by_guessing_the_id(
    fake_host: _FakeHost,
) -> None:
    _run(dispatch(_envelope("set", arg="1h mine", actor="alice"), {}, http_client=None))

    _run(dispatch(_envelope("remove", arg="1", actor="mallory"), {}, http_client=None))
    assert _relay_text(fake_host) == "No reminder with id 1."

    _run(dispatch(_envelope("list", actor="alice"), {}, http_client=None))
    assert "mine" in _relay_text(fake_host)


def test_anonymous_actor_shares_one_pseudonymous_bucket(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="1h anon", actor=None), {}, http_client=None))
    _run(dispatch(_envelope("list", actor=None), {}, http_client=None))
    assert "anon" in _relay_text(fake_host)
    assert _pseudonym(None) == _pseudonym("anonymous")


def test_replies_go_to_the_events_own_origin_platform(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("list", platform="discord"), {}, http_client=None))
    assert result.transport == "discord"
    assert fake_host.relay_calls[-1][0] == "discord"


# -- parser / formatter edges
@pytest.mark.parametrize(
    ("token", "seconds"),
    [
        ("1s", 1),
        ("90s", 90),
        ("5m", 300),
        ("1h", 3600),
        ("1d", 86400),
        ("3d2h5m10s", 3 * 86400 + 2 * 3600 + 5 * 60 + 10),
        ("1D2H", 86400 + 7200),
        ("0s", 0),
        ("0d0h0m0s", 0),
    ],
)
def test_parse_duration_accepts_ordered_components_case_insensitively(
    token: str, seconds: int
) -> None:
    assert _parse_duration_seconds(token) == seconds


@pytest.mark.parametrize("token", ["", "d", "1x", "1h1d", "1m1h", "-5m", "5", "5 m", "1.5h", "m5"])
def test_parse_duration_rejects_malformed_or_misordered_tokens(token: str) -> None:
    assert _parse_duration_seconds(token) is None


@pytest.mark.parametrize(
    ("seconds", "rendered"),
    [
        (0, "0s"),
        (59, "59s"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h 0m"),
        (86399, "23h 59m"),
        (86400, "1d 0h 0m"),
        (90061, "1d 1h 1m"),
    ],
)
def test_format_duration_boundaries(seconds: int, rendered: str) -> None:
    assert _format_duration(seconds) == rendered


def test_set_exact_confirmation_text_names_the_no_delivery_limitation(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="45m stretch"), {}, http_client=None))
    assert _relay_text(fake_host) == (
        'Reminder #1 set for in 45m: "stretch" (no auto-delivery yet -- check !remind list).'
    )


def test_overdue_and_upcoming_reminders_are_labelled_distinctly(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="1m soon"), {}, http_client=None))
    _run(dispatch(_envelope("set", arg="2h later"), {}, http_client=None))
    fake_host.advance(300)  # `soon` is now 4m overdue; `later` is still due

    _run(dispatch(_envelope("list"), {}, http_client=None))
    reply = _relay_text(fake_host)
    assert '#1 "soon" -- overdue by 4m (not delivered)' in reply
    assert '#2 "later" -- due in 1h 55m' in reply
    assert reply.index("#1") < reply.index("#2")


# -- flag fail-closed
def test_flag_is_requested_with_a_fail_closed_default(fake_host: _FakeHost) -> None:
    seen: list[tuple[str, bool]] = []
    sys.modules["wit_world"].imports.flags.enabled = (
        lambda key, default_value: seen.append((key, default_value)) or default_value
    )
    assert _run(transform(_sample_event("!remind"))) is None
    assert seen == [(FLAG_KEY, False)]
    assert FLAG_KEY == "waddles.command-remind"


def test_missing_wit_world_keeps_the_command_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "wit_world", None)
    assert _run(transform(_sample_event("!remind"))) is None


def test_flag_off_does_no_io_and_logs_nothing(fake_host: _FakeHost) -> None:
    fake_host.flag_enabled = False
    assert _run(transform(_sample_event(f"!remind set 5m {CANARY}"))) is None
    assert fake_host.log_calls == [] and fake_host.kv_calls == []


# -- PII-free logs
def test_no_log_line_in_any_flow_contains_reminder_text_duration_or_actor(
    fake_host: _FakeHost,
) -> None:
    """PII-free-log regression: typed text, the duration token and the raw actor never log."""
    actor = f"{CANARY}_actor"
    for text in (
        f"!remind set 5m {CANARY} reminder body",
        f"!remind set {CANARY} body",  # invalid duration token holding the canary
        f"!remind remove {CANARY}",
        f"!remind bogus {CANARY}",
        "!remind list",
    ):
        event = _run(transform(_sample_event(text, actor=actor)))
        assert event is not None
        env = _envelope(str(event.payload["action"]), actor=actor, arg=event.payload.get("arg"))
        _run(dispatch(env, {}, http_client=None))
    # a corrupt blob path (which logs) must be PII-free too
    fake_host.store[_scoped(_items_key(_pseudonym(actor)))] = b"garbage"
    _run(dispatch(_envelope("list", actor=actor), {}, http_client=None))

    assert fake_host.log_calls, "expected log lines (denominator must be non-zero)"
    for _lvl, message, fields_json in fake_host.log_calls:
        assert CANARY.lower() not in message.lower()
        assert CANARY.lower() not in fields_json.lower()


def test_kv_backend_error_log_carries_only_op_and_exception_type(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError(f"backend echoed {CANARY}")

    monkeypatch.setattr(app.community_kv, "get", _boom)
    with pytest.raises(RuntimeError, match="remind kv get failed: RuntimeError"):
        _run(dispatch(_envelope("list"), {}, http_client=None))

    assert _error_logs(fake_host) == [
        {"msg": "remind.kv_error", "op": "get", "error": "RuntimeError"}
    ]
    assert CANARY not in _relay_text(fake_host)


def test_entry_wiring_exports_the_stage_functions() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.__all__ == ["bundle_dispatch", "bundle_transform"]
