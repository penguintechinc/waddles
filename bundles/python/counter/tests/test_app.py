"""Host-native tests for the `counter` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/lurk/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors (fake `kv`/`relay`/`log`; there is deliberately NO `flags`
import, which proves the bundle is activation-gated only). The fake `kv` validates every key
against the real host's guest-key charset, so a `:` key can never pass silently again (gh-631).

Zero-width characters are built with `chr()` -- never written as escapes in this file.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import types
from pathlib import Path

import pytest
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text

import app
from app import (
    LOCK_KEY,
    MAX_AMOUNT,
    MAX_COUNTERS,
    MAX_LIST_VALUES,
    NAMES_KEY,
    PUBLIC_KEY,
    RESERVED_WORDS,
    VALUE_KEY_PREFIX,
    dispatch,
    transform,
)

ZWSP = chr(0x200B)

_ALLOWED_KV_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


def _run(coro):
    return asyncio.run(coro)


class _HostKvError(Exception):
    """Shaped like the generated WIT `Err` (`.value` holds the error union)."""

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value


class _FakeKv:
    """In-memory `kv` host stand-in: validates keys like the real host, scripted failures."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.calls: list[tuple[str, tuple]] = []
        self.fail_ops: set[str] = set()

    @staticmethod
    def _validate(key: str) -> None:
        if not key or len(key) > 256:
            raise _HostKvError(f"too-large: key length {len(key)}")
        if any(ch not in _ALLOWED_KV_KEY_CHARS for ch in key):
            raise _HostKvError(f"backend: invalid key {key!r}")

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_ops:
            raise _HostKvError(f"scripted {op} failure")

    def get(self, key: str):
        self._validate(key)
        self.calls.append(("get", (key,)))
        self._maybe_fail("get")
        return self.store.get(key)

    def set(self, key: str, value, ttl_seconds: int):
        self._validate(key)
        self.calls.append(("set", (key, bytes(value), ttl_seconds)))
        self._maybe_fail("set")
        self.store[key] = bytes(value)

    def delete(self, key: str):
        self._validate(key)
        self.calls.append(("delete", (key,)))
        self._maybe_fail("delete")
        self.store.pop(key, None)

    def increment(self, key: str, delta: int, ttl_seconds: int):
        self._validate(key)
        self.calls.append(("increment", (key, delta, ttl_seconds)))
        self._maybe_fail("increment")
        new_value = int(self.store.get(key, b"0")) + delta
        self.store[key] = str(new_value).encode()
        return new_value


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch):
    """Fake WIT host: real in-memory `kv`, recording `relay`/`log`. No `flags` on purpose."""
    fake_kv = _FakeKv()
    relay_calls: list[tuple[str, dict]] = []
    log_calls: list[tuple[str, str, dict]] = []

    relay_mod = types.SimpleNamespace(
        push=lambda provider, msg: relay_calls.append((provider, json.loads(msg)))
    )
    log_mod = types.SimpleNamespace(
        Level={"ERROR": "ERROR", "WARN": "WARN", "INFO": "INFO", "DEBUG": "DEBUG"},
        write=lambda lvl, msg, fields_json: log_calls.append((lvl, msg, json.loads(fields_json))),
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(kv=fake_kv, relay=relay_mod, log=log_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return types.SimpleNamespace(kv=fake_kv, relay_calls=relay_calls, log_calls=log_calls)


def _event(
    text: str,
    *,
    actor: str | None = "viewer-1",
    is_mod=False,
    is_broadcaster=False,
    platform: str = "twitch",
    channel_id="12345",
) -> PlatformEvent:
    payload: dict = {"text": text, "channel_id": channel_id}
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at="2026-10-05T00:00:00.000Z",
    )


def _say(text: str, **kw) -> str:
    """Run `transform` as a plain viewer and return the reply text (asserting there is one)."""
    result = _run(transform(_event(text, **kw)))
    assert result is not None, f"no reply for {text!r}"
    return result.payload["text"]


def _mod(text: str, **kw) -> str:
    """Run `transform` as a moderator and return the reply text."""
    return _say(text, is_mod=True, **kw)


def _value(fake_host, name: str) -> bytes | None:
    return fake_host.kv.store.get(f"{VALUE_KEY_PREFIX}{name}")


def _names(fake_host) -> list[str]:
    return json.loads(fake_host.kv.store.get(NAMES_KEY, b"[]"))


# ---------------------------------------------------------------------------
# Routing and cost
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["", "hello", "counter add x", "!count add x", "!counters", "!counterx list", "  hi !counter"],
)
def test_anything_but_the_counter_command_is_ignored_with_zero_kv_ops(text: str, fake_host) -> None:
    assert _run(transform(_event(text))) is None
    assert fake_host.kv.calls == []


def test_non_chat_payload_is_ignored(fake_host) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_command_matching_is_case_insensitive_and_whitespace_tolerant(fake_host) -> None:
    assert _mod("  !COUNTER   add   deaths  ") == "deaths: 1"
    assert _say("!Counter  DEATHS") == "deaths: 1"


def test_reply_keeps_channel_platform_and_event_shape(fake_host) -> None:
    result = _run(transform(_event("!counter list", platform="discord", channel_id="guild-9")))
    assert result is not None
    assert (result.platform, result.event_type) == ("discord", "chat.message")
    assert result.payload["channel_id"] == "guild-9"


@pytest.mark.parametrize("text", ["!counter", "!counter help", "!COUNTER HELP"])
def test_bare_and_help_return_usage(text: str, fake_host) -> None:
    assert _say(text).startswith("Usage: !counter <name> | !counter add|sub <name> [n]")
    assert fake_host.kv.calls == []


# ---------------------------------------------------------------------------
# Show / add / sub / set / reset / delete / list
# ---------------------------------------------------------------------------


def test_add_creates_at_one_then_accumulates(fake_host) -> None:
    assert _mod("!counter add deaths") == "deaths: 1"
    assert _mod("!counter add deaths") == "deaths: 2"
    assert _mod("!counter add deaths 5") == "deaths: 7"
    assert _value(fake_host, "deaths") == b"7"
    assert _names(fake_host) == ["deaths"]


def test_add_with_a_negative_amount_and_sub_can_go_below_zero(fake_host) -> None:
    _mod("!counter add wins 2")
    assert _mod("!counter add wins -5") == "wins: -3"
    assert _mod("!counter sub wins") == "wins: -4"
    assert _mod("!counter sub wins 6") == "wins: -10"
    assert _mod("!counter sub wins -10") == "wins: 0"


def test_add_and_sub_use_the_atomic_increment_with_the_signed_delta(fake_host) -> None:
    _mod("!counter add wins 3")
    fake_host.kv.calls.clear()
    _mod("!counter sub wins 2")
    assert ("increment", (f"{VALUE_KEY_PREFIX}wins", -2, 0)) in fake_host.kv.calls


def test_show_reads_one_key_and_is_open_to_everyone(fake_host) -> None:
    _mod("!counter set wins 9")
    fake_host.kv.calls.clear()
    assert _say("!counter wins", is_mod=False, is_broadcaster=False) == "wins: 9"
    assert fake_host.kv.calls == [("get", (f"{VALUE_KEY_PREFIX}wins",))]


@pytest.mark.parametrize("text", ["!counter ghost", "!counter !GHOST", "!counter  Ghost "])
def test_show_of_a_missing_counter_says_so_and_creates_nothing(text: str, fake_host) -> None:
    assert _say(text).startswith("Counter 'ghost' doesn't exist yet")
    assert NAMES_KEY not in fake_host.kv.store
    assert _value(fake_host, "ghost") is None


def test_show_with_extra_words_is_a_usage_error(fake_host) -> None:
    assert _say("!counter deaths now please").startswith("Usage:")


def test_sub_and_reset_of_a_missing_counter_do_not_create_it(fake_host) -> None:
    assert "doesn't exist" in _mod("!counter sub ghost")
    assert "doesn't exist" in _mod("!counter reset ghost")
    assert NAMES_KEY not in fake_host.kv.store
    assert _value(fake_host, "ghost") is None


def test_set_creates_and_overwrites(fake_host) -> None:
    assert _mod("!counter set wins 10") == "wins: 10"
    assert _mod("!counter set wins -3") == "wins: -3"
    assert _mod("!counter set wins +4") == "wins: 4"
    assert _names(fake_host) == ["wins"]


def test_set_without_a_number_asks_for_one(fake_host) -> None:
    assert _mod("!counter set wins") == "Usage: !counter set <name> <n>"
    assert NAMES_KEY not in fake_host.kv.store


def test_reset_zeroes_an_existing_counter(fake_host) -> None:
    _mod("!counter set wins 10")
    assert _mod("!counter reset wins") == "wins: 0"
    assert _value(fake_host, "wins") == b"0"


@pytest.mark.parametrize("verb", ["del", "delete", "remove", "DEL"])
def test_delete_spellings_remove_the_value_and_the_index_entry(verb: str, fake_host) -> None:
    _mod("!counter add wins")
    assert _mod(f"!counter {verb} wins") == "Removed counter 'wins'."
    assert _value(fake_host, "wins") is None
    assert _names(fake_host) == []
    assert "doesn't exist" in _mod(f"!counter {verb} wins")


def test_delete_is_idempotent_over_a_half_state(fake_host) -> None:
    fake_host.kv.store[NAMES_KEY] = json.dumps(["ghost"]).encode()  # index entry, no value
    assert _mod("!counter del ghost") == "Removed counter 'ghost'."
    assert _names(fake_host) == []
    fake_host.kv.store[f"{VALUE_KEY_PREFIX}orphan"] = b"3"  # value, no index entry
    assert _mod("!counter del orphan") == "Removed counter 'orphan'."


def test_add_self_heals_an_unindexed_value_without_losing_the_total(fake_host) -> None:
    fake_host.kv.store[f"{VALUE_KEY_PREFIX}orphan"] = b"3"
    assert _mod("!counter add orphan") == "orphan: 4"


def test_list_empty_then_sorted_with_values(fake_host) -> None:
    assert _say("!counter list") == "No counters have been created yet."
    _mod("!counter set zeta 3")
    _mod("!counter set alpha -1")
    assert _say("!counter list") == "Counters (2): alpha: -1, zeta: 3"


def test_list_shows_values_for_a_bounded_number_and_counts_the_rest(fake_host) -> None:
    names = [f"c{i:02d}" for i in range(40)]
    fake_host.kv.store[NAMES_KEY] = json.dumps(names).encode()
    for name in names:
        fake_host.kv.store[f"{VALUE_KEY_PREFIX}{name}"] = b"1"
    fake_host.kv.calls.clear()
    reply = _say("!counter list")
    assert reply.startswith("Counters (40): c00: 1, c01: 1")
    assert reply.endswith(f"... and {40 - MAX_LIST_VALUES} more")
    assert len(fake_host.kv.calls) <= 1 + MAX_LIST_VALUES <= 64


def test_list_reply_fits_one_chat_message(fake_host) -> None:
    names = ["n" * 30 + f"{i:02d}" for i in range(MAX_LIST_VALUES)]
    fake_host.kv.store[NAMES_KEY] = json.dumps(names).encode()
    for name in names:
        fake_host.kv.store[f"{VALUE_KEY_PREFIX}{name}"] = b"123456"
    assert len(_say("!counter list")) <= app.MAX_LIST_REPLY_LEN + 60


def test_list_treats_a_missing_value_as_zero(fake_host) -> None:
    fake_host.kv.store[NAMES_KEY] = json.dumps(["half"]).encode()
    assert _say("!counter list") == "Counters (1): half: 0"


# ---------------------------------------------------------------------------
# Names and amounts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("!", "name is required"),
        ("x" * 33, "32 characters or fewer"),
        ("De-aths!", "lowercase letters"),
        ("dea.ths", "lowercase letters"),
        ("-lead", "must start with"),
        ("_lead", "must start with"),
        ("add", "command word"),
        ("sub", "command word"),
        ("set", "command word"),
        ("reset", "command word"),
        ("list", "command word"),
        ("del", "command word"),
        ("public", "command word"),
        ("enable", "command word"),
    ],
)
def test_add_rejects_invalid_or_reserved_names_without_echoing_them(
    name: str, reason: str, fake_host
) -> None:
    reply = _mod(f"!counter add {name}")
    assert reason in reply
    typed = name.strip("!")
    if typed:
        assert not re.search(rf"(?<![A-Za-z]){re.escape(typed)}(?![A-Za-z])", reply)
    assert NAMES_KEY not in fake_host.kv.store


def test_every_reserved_word_is_a_grammar_word() -> None:
    assert {"add", "sub", "set", "reset", "remove", "delete", "list", "del", "public", "help"} <= (
        RESERVED_WORDS
    )


def test_a_counter_may_be_named_counter_or_any_charset_legal_word(fake_host) -> None:
    assert _mod("!counter add counter") == "counter: 1"
    assert _mod("!counter add a-b_c9") == "a-b_c9: 1"
    assert _mod("!counter add " + "x" * 32) == "x" * 32 + ": 1"


@pytest.mark.parametrize(
    "bad", ["abc", "1.5", "1e3", "0x10", "--1", "5x", chr(0x663), "1" * 14, str(MAX_AMOUNT + 1)]
)
def test_a_bad_amount_is_refused_without_echoing_it(bad: str, fake_host) -> None:
    for verb in ("add", "sub", "set"):
        reply = _mod(f"!counter {verb} wins {bad}")
        assert reply == "That isn't a whole number between -1000000000000 and 1000000000000."
        assert bad not in reply
    assert _value(fake_host, "wins") is None


def test_amount_bounds_are_inclusive(fake_host) -> None:
    assert _mod(f"!counter set wins {MAX_AMOUNT}") == f"wins: {MAX_AMOUNT}"
    assert _mod(f"!counter set wins -{MAX_AMOUNT}") == f"wins: -{MAX_AMOUNT}"


def test_more_than_name_and_amount_is_a_usage_error(fake_host) -> None:
    assert _mod("!counter add wins 1 2").startswith("Usage:")
    assert _mod("!counter reset wins 1").startswith("Usage:")
    assert _mod("!counter del wins now").startswith("Usage:")
    assert NAMES_KEY not in fake_host.kv.store


def test_add_with_no_name_is_usage(fake_host) -> None:
    for verb in ("add", "sub", "set", "reset", "del"):
        assert _mod(f"!counter {verb}").startswith("Usage:")


@pytest.mark.parametrize(
    "text", ["!counter enable", "!counter enable nothing", "!counter public add x"]
)
def test_malformed_toggle_forms_reply_with_usage(text: str, fake_host) -> None:
    assert _mod(text).startswith("Usage:")


def test_per_community_counter_cap_blocks_creation_only(fake_host) -> None:
    names = [f"c{i}" for i in range(MAX_COUNTERS)]
    fake_host.kv.store[NAMES_KEY] = json.dumps(names).encode()
    for name in names:
        fake_host.kv.store[f"{VALUE_KEY_PREFIX}{name}"] = b"1"
    assert f"maximum of {MAX_COUNTERS} counters" in _mod("!counter add overflow")
    assert f"maximum of {MAX_COUNTERS} counters" in _mod("!counter set overflow 5")
    assert _value(fake_host, "overflow") is None
    assert _mod("!counter add c0") == "c0: 2"  # existing counters keep working
    assert _mod("!counter del c0") == "Removed counter 'c0'."
    assert _mod("!counter add overflow") == "overflow: 1"  # a freed slot is reusable


# ---------------------------------------------------------------------------
# Permissions: strict badges, fail closed, configurable public mode
# ---------------------------------------------------------------------------

_NOT_PRIVILEGED = [
    {},
    {"is_mod": "false"},
    {"is_mod": "true"},
    {"is_mod": 1},
    {"is_broadcaster": "yes"},
    {"is_mod": None, "is_broadcaster": None},  # Discord today: no badge keys at all
]


@pytest.mark.parametrize("event_kwargs", _NOT_PRIVILEGED)
@pytest.mark.parametrize(
    "text",
    [
        "!counter add wins",
        "!counter sub wins",
        "!counter set wins 5",
        "!counter reset wins",
        "!counter del wins",
        "!counter enable public",
        "!counter disable public",
    ],
)
def test_mutation_requires_a_strict_true_badge_and_fails_closed(
    text: str, event_kwargs: dict, fake_host
) -> None:
    _mod("!counter set wins 7")
    snapshot = dict(fake_host.kv.store)
    assert _say(text, **event_kwargs) == "Only the broadcaster or a moderator can change counters."
    assert fake_host.kv.store == snapshot


def test_broadcaster_without_a_mod_badge_may_mutate(fake_host) -> None:
    assert _say("!counter add wins", is_mod=False, is_broadcaster=True) == "wins: 1"


def test_public_mode_is_off_by_default_and_reports_its_state(fake_host) -> None:
    assert _say("!counter public").startswith("Public add/sub is off.")
    assert PUBLIC_KEY not in fake_host.kv.store


def test_public_mode_lets_chatters_add_or_sub_exactly_one_on_existing_counters(fake_host) -> None:
    _mod("!counter set wins 5")
    assert _mod("!counter enable public").startswith("Public add/sub is now on")
    assert fake_host.kv.store[PUBLIC_KEY] == b"1"
    assert _say("!counter public").startswith("Public add/sub is on.")
    assert _say("!counter add wins") == "wins: 6"
    assert _say("!counter sub wins") == "wins: 5"


def test_public_mode_does_not_let_chatters_pick_amounts_create_set_reset_or_delete(
    fake_host,
) -> None:
    _mod("!counter set wins 5")
    _mod("!counter enable public")
    assert "custom amount" in _say("!counter add wins 100")
    assert "custom amount" in _say("!counter sub wins -100")
    assert "doesn't exist yet" in _say("!counter add brandnew")
    assert "doesn't exist yet" in _say("!counter sub brandnew")
    for text in ("!counter set wins 1", "!counter reset wins", "!counter del wins"):
        assert _say(text) == "Only the broadcaster or a moderator can change counters."
    assert _mod("!counter list") == "Counters (1): wins: 5"
    assert _names(fake_host) == ["wins"]


def test_disable_public_closes_it_again(fake_host) -> None:
    _mod("!counter set wins 5")
    _mod("!counter enable public")
    assert _mod("!counter disable public").startswith("Public add/sub is now off")
    assert PUBLIC_KEY not in fake_host.kv.store
    assert _say("!counter add wins") == "Only the broadcaster or a moderator can change counters."


def test_public_mode_does_not_gate_a_moderators_own_actions(fake_host) -> None:
    assert _mod("!counter add wins 3") == "wins: 3"  # no PUBLIC_KEY read needed for a mod
    assert ("get", (PUBLIC_KEY,)) not in fake_host.kv.calls


# ---------------------------------------------------------------------------
# Storage hygiene, cost, locking, failure handling
# ---------------------------------------------------------------------------


def test_every_kv_key_uses_the_host_charset_and_no_colon(fake_host) -> None:
    _mod("!counter add a-b_c9 4")
    _mod("!counter sub a-b_c9")
    _mod("!counter set a-b_c9 1")
    _mod("!counter reset a-b_c9")
    _mod("!counter enable public")
    _say("!counter list")
    _mod("!counter del a-b_c9")
    keys = {key for _, args in fake_host.kv.calls for key in args[:1]}
    assert keys
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+", key) for key in keys)
    assert not any(":" in key for key in keys)


def test_costs_stay_inside_the_host_op_budget(fake_host) -> None:
    _mod("!counter add wins")
    for text, limit in (
        ("!counter wins", 1),
        ("!counter add wins", 2),
        ("!counter sub wins", 2),
        ("!counter set wins 3", 2),
        ("!counter reset wins", 2),
        ("!counter add fresh", 6),
        ("!counter set fresh2 4", 7),
        ("!counter del fresh", 6),
        ("!counter list", 1 + MAX_LIST_VALUES),
    ):
        fake_host.kv.calls.clear()
        _mod(text)
        assert len(fake_host.kv.calls) <= limit <= 64, (text, fake_host.kv.calls)


def test_creation_lock_is_claimed_then_released(fake_host) -> None:
    _mod("!counter add wins")
    assert ("increment", (LOCK_KEY, 1, app.LOCK_TTL_SECONDS)) in fake_host.kv.calls
    assert fake_host.kv.calls.index(("delete", (LOCK_KEY,))) > fake_host.kv.calls.index(
        ("set", (NAMES_KEY, b'["wins"]', 0))
    )
    assert LOCK_KEY not in fake_host.kv.store


def test_a_concurrent_creation_is_told_to_retry_and_changes_nothing(fake_host) -> None:
    fake_host.kv.store[LOCK_KEY] = b"1"  # another moderator holds the claim
    assert _mod("!counter add wins") == "Another change is in progress - please try again."
    assert NAMES_KEY not in fake_host.kv.store
    assert _value(fake_host, "wins") is None
    assert fake_host.kv.store[LOCK_KEY] == b"2"  # the loser never deletes the holder's lock


@pytest.mark.parametrize(
    ("op", "text"),
    [
        ("get", "!counter wins"),
        ("get", "!counter list"),
        ("get", "!counter add wins"),
        ("increment", "!counter add wins"),
        ("set", "!counter set wins 4"),
        ("set", "!counter reset wins"),
        ("set", "!counter enable public"),
        ("delete", "!counter del wins"),
        ("delete", "!counter disable public"),
    ],
)
def test_a_kv_failure_replies_loudly_and_logs_the_exception_class_only(
    op: str, text: str, fake_host
) -> None:
    _mod("!counter add wins")  # healthy setup
    fake_host.kv.fail_ops = {op}
    fake_host.log_calls.clear()
    assert _mod(text) == "Counter storage is unavailable right now - please try again."
    errors = [c for c in fake_host.log_calls if c[0] == "ERROR"]
    assert [c[1] for c in errors] == ["counter.kv_failure"]
    assert errors[0][2] == {"error_type": "_HostKvError"}


def test_failed_creation_releases_the_lock(fake_host) -> None:
    fake_host.kv.fail_ops = {"set"}
    assert "unavailable" in _mod("!counter add wins")
    assert LOCK_KEY not in fake_host.kv.store


def test_corrupt_index_and_value_fail_loud_and_are_never_reset(fake_host) -> None:
    fake_host.kv.store[NAMES_KEY] = b"{not json"
    assert "unavailable" in _say("!counter list")
    assert "unavailable" in _mod("!counter add wins")
    assert fake_host.kv.store[NAMES_KEY] == b"{not json"
    fake_host.kv.store[NAMES_KEY] = b'{"a": 1}'
    assert "unavailable" in _say("!counter list")
    fake_host.kv.store[f"{VALUE_KEY_PREFIX}bad"] = b"not-a-number"
    assert "unavailable" in _say("!counter bad")
    fake_host.kv.store[f"{VALUE_KEY_PREFIX}bin"] = b"\xff\xfe"
    assert "unavailable" in _say("!counter bin")


# ---------------------------------------------------------------------------
# Output safety and PII-free
# ---------------------------------------------------------------------------


def test_every_reply_shape_is_a_sanitizer_fixed_point_and_never_leads_with_a_command(
    fake_host,
) -> None:
    texts = [
        "!counter",
        "!counter help",
        "!counter list",
        "!counter wins",
        "!counter ghost",
        "!counter add wins",
        "!counter add wins 9",
        "!counter sub wins",
        "!counter sub ghost",
        "!counter set wins 3",
        "!counter set wins x",
        "!counter reset wins",
        "!counter reset ghost",
        "!counter del wins",
        "!counter del wins",
        "!counter public",
        "!counter enable public",
        "!counter disable public",
        "!counter add add",
        "!counter add " + "x" * 40,
        "!counter add @everyone",
        "!counter add <@123>",
        "!counter @everyone",
        "!counter {user:a}",
        "!counter add wins 1 2",
        "!counter bogus a b",
        "!counter enable zzz",
    ]
    seen = 0
    for text in texts:
        for kwargs in ({"is_mod": True}, {}):
            reply = _say(text, **kwargs)
            seen += 1
            assert reply == sanitize_user_text(reply), reply
            assert ZWSP not in reply and "\\" not in reply
            assert not reply.startswith(("/", ".", "!")), reply
    assert seen == len(texts) * 2


def test_hostile_typed_names_are_rejected_not_echoed(fake_host) -> None:
    for hostile in ("@everyone", "<@123>", "{user:victim}", "<#9>", "<!here>", "a\\b"):
        for verb in ("", "add ", "sub ", "set ", "reset ", "del "):
            reply = _mod(f"!counter {verb}{hostile} 1".strip())
            assert hostile not in reply
            assert "{user:" not in reply and "<@" not in reply and "@everyone" not in reply
    assert NAMES_KEY not in fake_host.kv.store


def test_the_reply_is_built_by_transform_through_the_sdk_sanitizer(fake_host, monkeypatch) -> None:
    seen: list[str] = []

    def spy(text: str, *, allow_tokens=()) -> str:
        seen.append(text)
        return text + "!"

    monkeypatch.setattr(app, "sanitize_user_text", spy)
    assert _say("!counter list") == "No counters have been created yet.!"
    assert seen == ["No counters have been created yet."]


def test_event_actor_is_never_read_stored_or_logged(fake_host) -> None:
    secret_actor, secret_name = "rawactor99", "zzcounter"
    for text in (
        f"!counter add {secret_name} 3",
        f"!counter {secret_name}",
        "!counter list",
        f"!counter set {secret_name} 9",
        f"!counter reset {secret_name}",
        "!counter enable public",
        f"!counter del {secret_name}",
        "!counter add bad name",
    ):
        _mod(text, actor=secret_actor)
    blob = json.dumps(fake_host.log_calls)
    assert secret_actor not in blob and secret_name not in blob
    assert fake_host.log_calls, "expected at least one log line (denominator)"
    for _, _, fields in fake_host.log_calls:
        assert set(fields) <= {"op", "count", "platform", "error_type"}
    assert secret_actor.encode() not in b"".join(fake_host.kv.store.values())
    assert all(secret_actor not in args[0] for _, args in fake_host.kv.calls)


def test_bundle_is_activation_gated_only_no_feature_flag() -> None:
    source = Path(app.__file__).read_text()
    assert "feature_enabled" not in source
    assert "waddles.command-" not in source


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def _envelope(payload: dict, platform: str = "twitch") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.counter",
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


def test_transform_then_dispatch_relays_the_reply(fake_host) -> None:
    result = _run(transform(_event("!counter add wins", is_mod=True)))
    assert result is not None
    _run(dispatch(_envelope(result.payload, "twitch"), {}, http_client=None))
    assert fake_host.relay_calls == [("twitch", {"channel": "12345", "text": "wins: 1"})]


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_returns_a_transport_shaped_result(platform: str, fake_host) -> None:
    result = _run(
        dispatch(_envelope({"channel_id": "c1", "text": "hi"}, platform), {}, http_client=None)
    )
    assert (result.transport, result.detail, result.sub_type, result.http_status) == (
        platform,
        "relayed",
        None,
        None,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "hi"},
        {"channel_id": "c1"},
        {"channel_id": "c1", "text": ""},
        {"channel_id": "c1", "text": 5},
    ],
)
def test_dispatch_rejects_a_payload_missing_channel_or_text(payload: dict, fake_host) -> None:
    with pytest.raises(ValueError):
        _run(dispatch(_envelope(payload), {}, http_client=None))
    assert fake_host.relay_calls == []
