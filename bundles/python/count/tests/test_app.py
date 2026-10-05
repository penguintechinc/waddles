"""Host-native tests for the `count` bundle's dynamic-counter `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/lurk/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors (fake `kv`/`relay`/`flags`/`log`).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from app import REGISTRY_KEY, VALUE_KEY_PREFIX, _value_key, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def _run(coro):
    return asyncio.run(coro)


# regression: gh alpha-count-kv-colon -- mirrors
# `core/bundle_host_kv/src/scope.rs::is_allowed_key_byte` exactly (ASCII
# alnum + `_`/`-`/`.`, notably NOT `:`). Production rejected every real
# `count` kv call with `error_kind: invalid_key` because `REGISTRY_KEY`/
# `VALUE_KEY_PREFIX` used `:` as a namespace separator -- a byte the host's
# own guest-key validator has always forbidden (colon is reserved there as
# the *server-side* namespace escape). The in-memory fake below previously
# accepted any key, including one `:` would have rejected, so this bug was
# invisible to the whole test suite. Validating here closes that blind spot.
_ALLOWED_KV_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


class _HostKvError(Exception):
    """Shaped like the generated WIT `Err` (`.value` holds the error union) -- see `waddle_sdk/kv.py`."""

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value


def _validate_guest_key(key: str) -> None:
    """Reject exactly what the real `bundle_host_kv` host capability rejects -- fail loud, not silent."""
    if not key or len(key) > 256:
        raise _HostKvError(f"too-large: key length {len(key)}")
    if any(ch not in _ALLOWED_KV_KEY_CHARS for ch in key):
        raise _HostKvError(f"backend: invalid key {key!r}")


class _FakeKv:
    """In-memory `kv` host stand-in -- validates keys like the real host, with an optional scripted failure."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.calls: list[tuple[str, tuple]] = []
        self.fail_next: Exception | None = None
        self.fail_ops: set[str] = set()

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_ops:
            raise RuntimeError(f"scripted {op} failure")
        if self.fail_next is not None:
            exc = self.fail_next
            self.fail_next = None
            raise exc

    def get(self, key: str):
        _validate_guest_key(key)
        self.calls.append(("get", (key,)))
        self._maybe_fail("get")
        return self.store.get(key)

    def set(self, key: str, value, ttl_seconds: int):
        _validate_guest_key(key)
        self.calls.append(("set", (key, bytes(value), ttl_seconds)))
        self._maybe_fail("set")
        self.store[key] = bytes(value)

    def delete(self, key: str):
        _validate_guest_key(key)
        self.calls.append(("delete", (key,)))
        self._maybe_fail("delete")
        self.store.pop(key, None)

    def increment(self, key: str, delta: int, ttl_seconds: int):
        _validate_guest_key(key)
        self.calls.append(("increment", (key, delta, ttl_seconds)))
        self._maybe_fail("increment")
        current = int(self.store.get(key, b"0"))
        new_value = current + delta
        self.store[key] = str(new_value).encode()
        return new_value


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch):
    """Fake WIT host: `flags.enabled` True by default, real in-memory `kv`, recording `relay`/`log`."""
    fake_kv = _FakeKv()
    relay_calls: list[tuple[str, str]] = []
    log_calls: list[tuple[int, str, str]] = []
    flag_state = {"enabled": True}

    flags_mod = types.SimpleNamespace(
        enabled=lambda key, default_value: flag_state["enabled"]
    )
    relay_mod = types.SimpleNamespace(
        push=lambda provider, msg: relay_calls.append((provider, msg))
    )
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: log_calls.append((lvl, msg, fields_json)),
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
    text: str,
    *,
    platform: str = "twitch",
    is_mod=False,
    is_broadcaster=False,
    channel_id="12345",
):
    payload = {"text": text, "channel_id": channel_id}
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor="viewer-1",
        payload=payload,
        occurred_at="2026-10-05T00:00:00.000Z",
    )


def _no_role_event(text: str, *, platform: str = "discord", channel_id="guild-1"):
    """A Discord-shaped event -- no `is_mod`/`is_broadcaster` keys at all (today's real gap)."""
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-05T00:00:00.000Z",
    )


def _reply_text(result: PlatformEvent) -> str:
    return result.payload["text"]


# ---------------------------------------------------------------------------
# Basic routing / flag / non-matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["hello", "", "count add die", "die"])
def test_messages_without_leading_bang_are_ignored(text: str, fake_host) -> None:
    assert _run(transform(_event(text))) is None
    assert fake_host.kv.calls == []  # cheap-skip: zero kv ops


def test_disabled_flag_suppresses_every_reply(fake_host) -> None:
    fake_host.flag_state["enabled"] = False
    assert _run(transform(_event("!count add !die", is_mod=True))) is None


def test_non_chat_payload_is_ignored(fake_host) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="channel.follow",
        actor=None,
        payload={},
        occurred_at="",
    )
    assert _run(transform(event)) is None


def test_unregistered_bang_token_returns_none(fake_host) -> None:
    assert _run(transform(_event("!notacounter"))) is None
    # exactly one registry read -- the per-message kv cost for an unmatched `!` token
    assert fake_host.kv.calls == [("get", ("count.registry",))]


def test_bare_count_returns_usage(fake_host) -> None:
    result = _run(transform(_event("!count")))
    assert "Usage" in _reply_text(result)


def test_unknown_count_subcommand_returns_error(fake_host) -> None:
    result = _run(transform(_event("!count bogus")))
    assert "Unknown" in _reply_text(result)


# ---------------------------------------------------------------------------
# !count add / remove / list
# ---------------------------------------------------------------------------


def test_count_add_creates_counter_at_zero(fake_host) -> None:
    result = _run(transform(_event("!count add !die", is_mod=True)))
    assert "Created counter 'die'" in _reply_text(result)
    assert json.loads(fake_host.kv.store["count.registry"]) == ["die"]
    assert fake_host.kv.store["count.value.die"] == b"0"


def test_count_add_accepts_name_without_bang(fake_host) -> None:
    result = _run(transform(_event("!count add die", is_mod=True)))
    assert "Created counter 'die'" in _reply_text(result)


def test_count_add_requires_moderator_or_broadcaster(fake_host) -> None:
    result = _run(
        transform(_event("!count add !die", is_mod=False, is_broadcaster=False))
    )
    assert "Only the broadcaster or a moderator" in _reply_text(result)
    assert "count.registry" not in fake_host.kv.store


def test_count_add_allowed_for_broadcaster_without_mod(fake_host) -> None:
    result = _run(
        transform(_event("!count add !die", is_mod=False, is_broadcaster=True))
    )
    assert "Created counter 'die'" in _reply_text(result)


def test_count_add_duplicate_name_errors(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    result = _run(transform(_event("!count add !die", is_mod=True)))
    assert "already exists" in _reply_text(result)


@pytest.mark.parametrize(
    "name,reason",
    [
        ("!", "name is required"),  # normalizes to "" after stripping the leading '!'
        ("x" * 33, "or fewer"),
        ("Die!", "lowercase letters"),
        ("count", "reserved"),
    ],
)
def test_count_add_rejects_invalid_names(name: str, reason: str, fake_host) -> None:
    result = _run(transform(_event(f"!count add {name}", is_mod=True)))
    assert reason in _reply_text(result)


def test_count_add_with_no_name_argument_returns_usage(fake_host) -> None:
    result = _run(transform(_event("!count add", is_mod=True)))
    assert _reply_text(result) == "Usage: !count add <name>"


def test_count_remove_deletes_counter(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    result = _run(transform(_event("!count remove !die", is_mod=True)))
    assert "Removed counter 'die'" in _reply_text(result)
    assert json.loads(fake_host.kv.store["count.registry"]) == []
    assert "count.value.die" not in fake_host.kv.store


def test_count_remove_requires_permission(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    result = _run(transform(_event("!count remove !die", is_mod=False)))
    assert "Only the broadcaster or a moderator" in _reply_text(result)
    assert "die" in json.loads(fake_host.kv.store["count.registry"])


def test_count_remove_without_name_returns_usage(fake_host) -> None:
    result = _run(transform(_event("!count remove", is_mod=True)))
    assert _reply_text(result) == "Usage: !count remove <name>"


def test_count_remove_nonexistent_errors(fake_host) -> None:
    result = _run(transform(_event("!count remove !die", is_mod=True)))
    assert "doesn't exist" in _reply_text(result)


def test_count_list_empty(fake_host) -> None:
    result = _run(transform(_event("!count list")))
    assert "No counters" in _reply_text(result)


def test_count_list_open_to_anyone(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    _run(transform(_event("!count add !wins", is_mod=True)))
    result = _run(transform(_event("!count list", is_mod=False, is_broadcaster=False)))
    assert "die" in _reply_text(result) and "wins" in _reply_text(result)


# ---------------------------------------------------------------------------
# Dynamic counter invocation: read / add / sub / set
# ---------------------------------------------------------------------------


def test_bare_counter_read_is_open_to_anyone(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    result = _run(transform(_event("!die", is_mod=False, is_broadcaster=False)))
    assert _reply_text(result) == "die: 0"


def test_counter_add_default_increment_is_one(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    result = _run(transform(_event("!die add", is_mod=True)))
    assert _reply_text(result) == "die: 1"


def test_counter_add_explicit_amount(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    result = _run(transform(_event("!die add 5", is_mod=True)))
    assert _reply_text(result) == "die: 5"


def test_counter_sub_default_decrement_is_one(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    _run(transform(_event("!die set 10", is_mod=True)))
    result = _run(transform(_event("!die sub", is_mod=True)))
    assert _reply_text(result) == "die: 9"


def test_counter_set_explicit_value(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    result = _run(transform(_event("!die set 3", is_mod=True)))
    assert _reply_text(result) == "die: 3"


def test_counter_set_without_amount_errors(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    result = _run(transform(_event("!die set", is_mod=True)))
    assert "a number is required" in _reply_text(result)


@pytest.mark.parametrize("op", ["add", "sub", "set"])
def test_counter_mutations_require_permission(op: str, fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    text = f"!die {op} 1"
    result = _run(transform(_event(text, is_mod=False, is_broadcaster=False)))
    assert "Only the broadcaster or a moderator" in _reply_text(result)
    assert fake_host.kv.store["count.value.die"] == b"0"


@pytest.mark.parametrize(
    "amount,expected_fragment",
    [
        ("abc", "isn't a whole number"),
        ("1.5", "isn't a whole number"),
        (str(2**63), "out of range"),
        ("-" + str(2**63 + 1), "out of range"),
    ],
)
def test_counter_invalid_amount_errors_not_crashes(
    amount: str, expected_fragment: str, fake_host
) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    result = _run(transform(_event(f"!die set {amount}", is_mod=True)))
    assert result is not None
    assert expected_fragment in _reply_text(result)
    assert fake_host.kv.store["count.value.die"] == b"0"  # rejected before any kv write


def test_counter_add_invalid_amount_errors(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    result = _run(transform(_event("!die add abc", is_mod=True)))
    assert "isn't a whole number" in _reply_text(result)
    assert fake_host.kv.store["count.value.die"] == b"0"


def test_counter_unknown_operation_errors(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    result = _run(transform(_event("!die frobnicate", is_mod=True)))
    assert "Unknown operation" in _reply_text(result)


def test_unregistered_counter_name_with_subcommand_shape_is_still_not_ours(
    fake_host,
) -> None:
    # "!wins add 1" where "wins" was never created -- not a registered counter, not !count.
    assert _run(transform(_event("!wins add 1", is_mod=True))) is None


# ---------------------------------------------------------------------------
# Permission fail-safe: role info unavailable (Discord today)
# ---------------------------------------------------------------------------


def test_discord_shaped_event_with_no_role_fields_rejects_mutation(fake_host) -> None:
    result = _run(transform(_no_role_event("!count add !die")))
    assert "Only the broadcaster or a moderator" in _reply_text(result)
    assert "count.registry" not in fake_host.kv.store


def test_discord_shaped_event_still_allows_reads(fake_host) -> None:
    _run(
        transform(_event("!count add !die", is_mod=True))
    )  # created via a privileged Twitch event
    result = _run(transform(_no_role_event("!die")))
    assert _reply_text(result) == "die: 0"


def test_role_info_unavailable_is_logged(fake_host) -> None:
    _run(transform(_no_role_event("!count add !die")))
    assert any(
        msg == "count.role_info_unavailable"
        for _lvl, msg, _fields in fake_host.log_calls
    )


# ---------------------------------------------------------------------------
# kv failure handling: fail loud, never silent
# ---------------------------------------------------------------------------


def test_kv_failure_on_registry_read_produces_error_reply_and_logs(fake_host) -> None:
    fake_host.kv.fail_next = RuntimeError("backend unavailable")
    result = _run(transform(_event("!count list")))
    assert result is not None
    assert "went wrong" in _reply_text(result)
    assert any(msg == "count.kv_failure" for _lvl, msg, _fields in fake_host.log_calls)


def test_corrupt_registry_is_treated_as_kv_failure(fake_host) -> None:
    fake_host.kv.store["count.registry"] = b"not json"
    result = _run(transform(_event("!count list")))
    assert "went wrong" in _reply_text(result)


def test_registry_not_a_list_of_strings_is_kv_failure(fake_host) -> None:
    fake_host.kv.store["count.registry"] = json.dumps([1, 2, 3]).encode()
    result = _run(transform(_event("!count list")))
    assert "went wrong" in _reply_text(result)


def test_corrupt_counter_value_is_kv_failure(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    fake_host.kv.store["count.value.die"] = b"not-a-number"
    result = _run(transform(_event("!die")))
    assert "went wrong" in _reply_text(result)


def test_counter_registered_with_no_value_key_reads_as_zero(fake_host) -> None:
    # Registry entry present but its value key was never written -- _get_counter_value's
    # own `raw is None` default-to-zero path, distinct from the normal !count add flow
    # (which always writes b"0" immediately).
    fake_host.kv.store["count.registry"] = json.dumps(["die"]).encode()
    result = _run(transform(_event("!die")))
    assert _reply_text(result) == "die: 0"


def test_kv_set_failure_on_registry_save_produces_error_reply(fake_host) -> None:
    fake_host.kv.fail_ops = {"set"}
    result = _run(transform(_event("!count add !die", is_mod=True)))
    assert "went wrong" in _reply_text(result)
    assert any(msg == "count.kv_failure" for _lvl, msg, _fields in fake_host.log_calls)


def test_kv_delete_failure_on_remove_produces_error_reply(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    fake_host.kv.fail_ops = {"delete"}
    result = _run(transform(_event("!count remove !die", is_mod=True)))
    assert "went wrong" in _reply_text(result)


def test_kv_increment_failure_produces_error_reply(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    fake_host.kv.fail_ops = {"increment"}
    result = _run(transform(_event("!die add 1", is_mod=True)))
    assert "went wrong" in _reply_text(result)


# ---------------------------------------------------------------------------
# No raw actor in kv keys or logs
# ---------------------------------------------------------------------------


def test_transform_never_stores_or_logs_the_raw_actor(fake_host) -> None:
    _run(transform(_event("!count add !die", is_mod=True)))
    _run(transform(_event("!die add 1", is_mod=True)))

    for key in fake_host.kv.store:
        assert "viewer-1" not in key
    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "actor" not in json.loads(fields_json)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def _envelope(platform: str, payload: dict) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.count",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload=payload,
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )


def test_dispatch_relays_the_text_transform_built(fake_host) -> None:
    envelope = _envelope("twitch", {"channel_id": "12345", "text": "die: 1"})
    result = _run(dispatch(envelope, {}, http_client=None))

    provider, message_json = fake_host.relay_calls[0]
    assert provider == "twitch"
    assert json.loads(message_json) == {"channel": "12345", "text": "die: 1"}
    assert result.detail == "relayed"


def test_dispatch_raises_when_channel_id_is_missing(fake_host) -> None:
    envelope = _envelope("twitch", {"channel_id": None, "text": "die: 1"})
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_when_text_is_missing(fake_host) -> None:
    envelope = _envelope("twitch", {"channel_id": "12345", "text": None})
    with pytest.raises(ValueError, match="text"):
        _run(dispatch(envelope, {}, http_client=None))


# ---------------------------------------------------------------------------
# Regression: gh alpha-count-kv-colon -- every kv key this bundle constructs
# must satisfy the host's guest-key charset, or every counter mutation on
# alpha fails with "Something went wrong updating the counter storage".
# ---------------------------------------------------------------------------


def test_kv_key_constants_satisfy_host_guest_key_charset() -> None:
    """`REGISTRY_KEY`/`VALUE_KEY_PREFIX` (and prefixed counter-name keys) must never contain `:`.

    This previously failed in production (not in this test suite, since
    `_FakeKv` didn't validate keys): `REGISTRY_KEY = "count:registry"` and
    `VALUE_KEY_PREFIX = "count:value:"` used `:` as a human-readable
    namespace separator, but `core/bundle_host_kv/src/scope.rs::
    is_allowed_key_byte` reserves `:` as the *server-side* namespace escape
    and rejects any guest key containing one (`error_kind: invalid_key`,
    surfaced to the bundle as a `kv.error`). Every `!count`/`!<counter>`
    mutation hit this unconditionally, caught by `transform`'s `_KvFailure`
    handler and replied with the generic storage-error message -- a total
    outage for this bundle's write path, not an edge case.
    """
    assert set(REGISTRY_KEY) <= _ALLOWED_KV_KEY_CHARS
    assert set(VALUE_KEY_PREFIX) <= _ALLOWED_KV_KEY_CHARS
    assert set(_value_key("die")) <= _ALLOWED_KV_KEY_CHARS
    assert ":" not in REGISTRY_KEY
    assert ":" not in VALUE_KEY_PREFIX


def test_counter_create_and_mutate_round_trip_against_host_key_validation(
    fake_host,
) -> None:
    """End-to-end: create, add, set, read a counter -- every kv call must pass `_FakeKv`'s charset check.

    Before the fix this raised `_HostKvError` on the very first
    `kv.get(REGISTRY_KEY)` and the whole flow degraded to the generic
    "Something went wrong" reply instead of ever reaching a real value.
    """
    create = _run(transform(_event("!count add !die", is_mod=True)))
    assert _reply_text(create) == "Created counter 'die' (starting at 0). Use !die to read it."

    added = _run(transform(_event("!die add 5", is_mod=True)))
    assert _reply_text(added) == "die: 5"

    was_set = _run(transform(_event("!die set 42", is_mod=True)))
    assert _reply_text(was_set) == "die: 42"

    read = _run(transform(_event("!die")))
    assert _reply_text(read) == "die: 42"
