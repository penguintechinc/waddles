"""Host-native tests for the `customcommand` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/lurk/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors (fake `kv`/`relay`/`log`; there is deliberately NO `flags`
import, which proves the bundle is activation-gated only). The fake `kv` validates every key
against the real host's guest-key charset, so a `:` key can never pass silently again (gh-631).

Zero-width / control characters are built with `chr()` -- never written as escapes in this file.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import sys
import types
from pathlib import Path

import pytest
from waddle_sdk import identity
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

import app
from app import (
    CMD_KEY_PREFIX,
    LOCK_KEY,
    MAX_COMMANDS,
    MAX_RESPONSE_LEN,
    NAMES_KEY,
    RESERVED_NAMES,
    USES_KEY_PREFIX,
    dispatch,
    transform,
)

ZWSP = chr(0x200B)
NUL = chr(0)
SAFE_LEAD = chr(0x1F4AC) + " "
ACTOR = "{user:actortok}"
ACTOR_TOKEN = "actortok"
TARGET = "{user:targettok}"
TARGET_TOKEN = "targettok"

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
    actor: str | None = ACTOR,
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


def _mod(text: str, **kw) -> PlatformEvent:
    return _event(text, is_mod=True, **kw)


def _say(text: str, **kw) -> str:
    """Run `transform` and return the reply text (asserting there is one)."""
    result = _run(transform(_event(text, **kw)))
    assert result is not None, f"no reply for {text!r}"
    return result.payload["text"]


def _admin(text: str, **kw) -> str:
    """Run `transform` as a moderator and return the reply text."""
    result = _run(transform(_mod(text, **kw)))
    assert result is not None, f"no reply for {text!r}"
    return result.payload["text"]


def _seed(fake_host, name: str, response: str) -> None:
    """Store a command directly in kv (bypasses add-time validation: tampered/old data)."""
    fake_host.kv.store[CMD_KEY_PREFIX + name] = response.encode()
    names = json.loads(fake_host.kv.store.get(NAMES_KEY, b"[]"))
    fake_host.kv.store[NAMES_KEY] = json.dumps(sorted({*names, name})).encode()


def _live_tokens(text: str) -> list[str]:
    """Tokens of every `{user:<tok>}` the egress detokenizer would substitute (own oracle)."""
    live: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2 if i + 1 < len(text) and text[i + 1] in "{}\\" else 1
            continue
        if text.startswith("{user:", i):
            j = i + len("{user:")
            k = j
            while k < len(text) and (
                text[k].isascii() and (text[k].isalnum() or text[k] in "-_:.")
            ):
                k += 1
            if k > j and k < len(text) and text[k] == "}":
                live.append(text[j:k])
                i = k + 1
                continue
        i += 1
    return live


def _without_live(text: str) -> str:
    """`text` with every live placeholder replaced by a NUL wildcard marker."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            step = 2 if i + 1 < len(text) and text[i + 1] in "{}\\" else 1
            out.append(text[i : i + step])
            i += step
            continue
        if text.startswith("{user:", i):
            j = i + len("{user:")
            k = j
            while k < len(text) and (
                text[k].isascii() and (text[k].isalnum() or text[k] in "-_:.")
            ):
                k += 1
            if k > j and k < len(text) and text[k] == "}":
                out.append(NUL)
                i = k + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


_FORMING_MENTION_RE = re.compile(
    r"@(?:everyone|here)|<[@#]&?[\w\x00]|<![\w\x00]|(?<!\w)@[\w\x00]", re.IGNORECASE
)


def _assert_no_mention(text: str) -> None:
    view = _without_live(text).replace("\t", "").replace("\n", "").replace("\r", "")
    match = _FORMING_MENTION_RE.search(view)
    assert match is None, f"mention-forming markup survived: {match.group(0)!r} in {text!r}"


# ---------------------------------------------------------------------------
# Routing, cost, cheap-skip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["welcome", "", "cc add x y", "  welcome !cc", "?welcome"])
def test_text_without_leading_bang_is_ignored_with_zero_kv_ops(text: str, fake_host) -> None:
    assert _run(transform(_event(text))) is None
    assert fake_host.kv.calls == []


def test_non_chat_payload_is_ignored(fake_host) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None
    assert fake_host.kv.calls == []


def test_unknown_bang_token_costs_exactly_one_get(fake_host) -> None:
    assert _run(transform(_event("!nothingstoredhere"))) is None
    assert fake_host.kv.calls == [("get", (f"{CMD_KEY_PREFIX}nothingstoredhere",))]


@pytest.mark.parametrize("token", ["!", "!!x", "!foo:bar", "!x" + "y" * 40, "!-lead", "!UP:case"])
def test_tokens_that_cannot_be_a_command_name_cost_zero_kv_ops(token: str, fake_host) -> None:
    assert _run(transform(_event(token))) is None
    assert fake_host.kv.calls == []


def test_reserved_names_never_reach_kv(fake_host) -> None:
    # `!hello` / `!count` etc. belong to real bundles: skipped before any kv op.
    for token in ("!hello", "!count", "!counter", "!quote"):
        assert _run(transform(_event(token))) is None
    assert fake_host.kv.calls == []


def test_a_command_name_prefix_is_not_the_management_command(fake_host) -> None:
    # `!ccx` / `!customcommands` are unknown tokens (1 kv.get), not `!cc` management.
    assert _run(transform(_event("!ccx add a b", is_mod=True))) is None
    assert _run(transform(_event("!customcommands list"))) is None


def test_reply_keeps_channel_platform_and_event_shape(fake_host) -> None:
    result = _run(transform(_mod("!cc list", platform="discord", channel_id="guild-9")))
    assert result is not None
    assert result.platform == "discord"
    assert result.event_type == "chat.message"
    assert result.payload["channel_id"] == "guild-9"


# ---------------------------------------------------------------------------
# Grammar: add / edit / del / list, aliases, permissions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["!cc", "!cc help", "!CC HELP", "!customcommand"])
def test_bare_and_help_return_usage_and_variables(text: str, fake_host) -> None:
    reply = _say(text)
    assert reply.startswith("Usage: !cc add <name> <response>")
    assert "$(user)" in reply and "$(random <min> <max>)" in reply


@pytest.mark.parametrize("text", ["!cc bogus", "!cc enable x", "!cc sub a b", "!cc reset a"])
def test_unknown_subcommand_replies_with_usage_never_echoing_input(text: str, fake_host) -> None:
    reply = _say(text, is_mod=True)
    assert reply.startswith("Usage: !cc add")
    assert "bogus" not in reply


def test_add_then_invoke_roundtrip(fake_host) -> None:
    assert _admin("!cc add welcome Hi there!") == "Added !welcome. Try it with !welcome"
    assert fake_host.kv.store[CMD_KEY_PREFIX + "welcome"] == b"Hi there!"
    assert json.loads(fake_host.kv.store[NAMES_KEY]) == ["welcome"]
    assert _say("!welcome") == "Hi there!"


def test_management_alias_customcommand_and_bang_name_case_folding(fake_host) -> None:
    assert _admin("!customcommand add !WeLCome Hi") == "Added !welcome. Try it with !welcome"
    assert _say("!WELCOME") == "Hi"


@pytest.mark.parametrize("verb", ["edit", "set", "EDIT"])
def test_edit_spellings_replace_the_response(verb: str, fake_host) -> None:
    _admin("!cc add welcome one")
    assert _admin(f"!cc {verb} welcome two") == "Updated !welcome."
    assert _say("!welcome") == "two"
    assert json.loads(fake_host.kv.store[NAMES_KEY]) == ["welcome"]


@pytest.mark.parametrize("verb", ["del", "delete", "remove", "DEL"])
def test_delete_spellings_remove_command_counter_and_index(verb: str, fake_host) -> None:
    _admin("!cc add welcome $(count)")
    _say("!welcome")
    assert (USES_KEY_PREFIX + "welcome") in fake_host.kv.store
    assert _admin(f"!cc {verb} welcome") == "Removed !welcome."
    assert CMD_KEY_PREFIX + "welcome" not in fake_host.kv.store
    assert USES_KEY_PREFIX + "welcome" not in fake_host.kv.store
    assert json.loads(fake_host.kv.store[NAMES_KEY]) == []
    assert _run(transform(_event("!welcome"))) is None


def test_add_existing_name_refuses_and_keeps_old_response(fake_host) -> None:
    _admin("!cc add welcome one")
    assert "already exists" in _admin("!cc add welcome two")
    assert _say("!welcome") == "one"


def test_edit_and_delete_of_a_missing_command_say_so(fake_host) -> None:
    assert "doesn't exist" in _admin("!cc edit nope text")
    assert "doesn't exist" in _admin("!cc del nope")


def test_edit_self_heals_an_index_entry_lost_to_a_partial_failure(fake_host) -> None:
    fake_host.kv.store[CMD_KEY_PREFIX + "ghost"] = b"boo"  # command present, index entry missing
    assert _admin("!cc edit ghost boo2") == "Updated !ghost."
    assert json.loads(fake_host.kv.store[NAMES_KEY]) == ["ghost"]
    # and delete is idempotent over the same half-state
    fake_host.kv.store.pop(NAMES_KEY)
    assert _admin("!cc del ghost") == "Removed !ghost."


def test_list_is_open_to_everyone_sorted_and_empty_case(fake_host) -> None:
    assert _say("!cc list") == "No custom commands have been added yet."
    _admin("!cc add zeta z")
    _admin("!cc add alpha a")
    assert _say("!cc list") == "Custom commands (2): !alpha, !zeta"


def test_list_is_shortened_to_one_chat_message(fake_host) -> None:
    names = [f"cmd{i:03d}" + "x" * 20 for i in range(60)]
    fake_host.kv.store[NAMES_KEY] = json.dumps(names).encode()
    reply = _say("!cc list")
    assert len(reply) <= app.MAX_LIST_REPLY_LEN + 40
    assert "more" in reply and reply.startswith("Custom commands (60): !cmd000")


@pytest.mark.parametrize(
    "event_kwargs",
    [
        {},  # plain viewer
        {"is_mod": "false"},  # string badge: truthy but not True
        {"is_mod": "true"},
        {"is_mod": 1},
        {"is_broadcaster": "yes"},
        {"is_mod": None, "is_broadcaster": None},  # Discord today: no badge keys at all
    ],
)
@pytest.mark.parametrize("text", ["!cc add x hi", "!cc edit x hi", "!cc del x"])
def test_mutation_requires_a_strict_true_badge_and_fails_closed(
    text: str, event_kwargs: dict, fake_host
) -> None:
    reply = _say(text, **event_kwargs)
    assert reply == "Only the broadcaster or a moderator can change custom commands."
    assert NAMES_KEY not in fake_host.kv.store
    assert CMD_KEY_PREFIX + "x" not in fake_host.kv.store
    assert not [c for c in fake_host.kv.calls if c[0] in ("set", "increment", "delete")]


def test_broadcaster_without_mod_badge_is_allowed(fake_host) -> None:
    assert _say("!cc add x hi", is_mod=False, is_broadcaster=True).startswith("Added")


# ---------------------------------------------------------------------------
# Validation: names, responses, cap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("!", "name is required"),
        ("x" * 33, "32 characters or fewer"),
        ("Hi!", "lowercase letters"),
        ("-lead", "must start with"),
        ("sp ace", None),  # splits into name `sp` + response `ace` -> valid add
        ("cc", "existing command"),
        ("hello", "existing command"),
        ("counter", "existing command"),
    ],
)
def test_add_name_validation(name: str, reason: str | None, fake_host) -> None:
    reply = _admin(f"!cc add {name} some text")
    if reason is None:
        assert reply.startswith("Added")
    else:
        assert reason in reply
        assert NAMES_KEY not in fake_host.kv.store


def test_reserved_names_cover_every_bundle_command_in_the_repo() -> None:
    """A custom command named like a real command would reply on top of it -- keep the list fresh."""
    bundles_root = Path(__file__).resolve().parents[3]
    wanted: set[str] = set()
    for manifest in bundles_root.glob("*/*/bundle.yaml"):
        wanted.add(manifest.parent.name.lower())
        for match in re.finditer(r"command_prefix:\s*\[([^\]]*)\]", manifest.read_text()):
            wanted.update(
                p.strip().strip("\"'").lstrip("!").lower() for p in match.group(1).split(",")
            )
    wanted.discard("")
    assert len(wanted) > 50, "scanned too few bundle manifests -- the repo-root path is wrong"
    missing = sorted(wanted - RESERVED_NAMES)
    assert not missing, f"add these bundle commands to customcommand's RESERVED_NAMES: {missing}"


def test_every_reserved_name_is_a_legal_charset_name() -> None:
    legal = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
    assert all(legal.match(n) for n in RESERVED_NAMES)


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        ("", "response is required"),
        ("x" * (MAX_RESPONSE_LEN + 1), f"{MAX_RESPONSE_LEN} characters or fewer"),
        ("bad" + chr(1) + "ctl", "control characters"),
        ("bad" + chr(0) + "nul", "control characters"),
        ("hi {user:abc} there", "can't contain user mentions"),
        ("/ban someone", "can't start with"),
        (".timeout someone", "can't start with"),
        ("!quote add x", "can't start with"),
        (ZWSP + "/ban hidden", "can't start with"),
        ("   /ban padded", "can't start with"),
        ("hi $(nope)", "unsupported variable"),
        ("hi $()", "unsupported variable"),
        ("hi $(user x)", "unsupported variable"),
        ("hi $(random 1)", "unsupported variable"),
        ("hi $(random a b)", "unsupported variable"),
        ("hi $(random 5 1)", "unsupported variable"),
        ("hi $(random 1 1000000001)", "unsupported variable"),
        ("hi $(random 1 2 3)", "unsupported variable"),
        ("$(user) " * 6, "at most 5 variables"),
    ],
)
@pytest.mark.parametrize("verb", ["add", "edit"])
def test_response_validation_rejects_and_never_stores(
    verb: str, response: str, reason: str, fake_host
) -> None:
    if verb == "edit":
        _admin("!cc add x original")
    before = dict(fake_host.kv.store)
    reply = _admin(f"!cc {verb} x {response}")
    assert reason in reply
    assert fake_host.kv.store == before  # nothing written, lock released


def test_add_edit_with_no_response_asks_for_one(fake_host) -> None:
    assert "response is required" in _admin("!cc add x")
    assert "name is required" in _admin("!cc add")
    assert "name is required" in _admin("!cc edit")
    assert _admin("!cc del").startswith("A command name is required")


def test_five_variables_are_allowed(fake_host) -> None:
    assert _admin("!cc add many $(user) $(target) $(count) $(random 1 2) $(touser)").startswith(
        "Added"
    )


def test_per_community_command_cap(fake_host) -> None:
    names = [f"c{i}" for i in range(MAX_COMMANDS)]
    fake_host.kv.store[NAMES_KEY] = json.dumps(names).encode()
    assert f"maximum of {MAX_COMMANDS} custom commands" in _admin("!cc add overflow text")
    assert CMD_KEY_PREFIX + "overflow" not in fake_host.kv.store
    # editing/deleting at the cap still works
    fake_host.kv.store[CMD_KEY_PREFIX + "c0"] = b"x"
    assert _admin("!cc edit c0 y") == "Updated !c0."
    assert _admin("!cc del c0") == "Removed !c0."
    assert _admin("!cc add overflow text").startswith("Added")


# ---------------------------------------------------------------------------
# Variable substitution
# ---------------------------------------------------------------------------


def test_user_variable_is_the_invoker_as_a_live_mention(fake_host) -> None:
    _admin("!cc add greet Welcome $(user)!")
    assert _say("!greet", actor=ACTOR) == "Welcome {user:actortok}!"


def test_user_variable_with_a_raw_handle_actor_is_plain_text(fake_host) -> None:
    _admin("!cc add greet Welcome $(user)!")
    assert _say("!greet", actor="rawhandle") == "Welcome rawhandle!"


@pytest.mark.parametrize("actor", [None, "", "   "])
def test_user_variable_without_an_actor_falls_back_to_someone(actor, fake_host) -> None:
    _admin("!cc add greet Welcome $(user)!")
    assert _say("!greet", actor=actor) == "Welcome someone!"


@pytest.mark.parametrize("var", ["$(target)", "$(touser)", "$(TARGET)"])
def test_target_variable_is_the_first_mention_in_the_invocation(var: str, fake_host) -> None:
    _admin("!cc add cuddle " + var + " gets a hug from $(user)")
    reply = _say(f"!cuddle some words {TARGET} and {{user:second}}")
    assert reply == f"{TARGET} gets a hug from {ACTOR}"


def test_target_variable_falls_back_to_first_word_then_to_the_invoker(fake_host) -> None:
    _admin("!cc add cuddle $(target) is hugged")
    assert _say("!cuddle  bob   alice") == "bob is hugged"
    assert _say("!cuddle") == f"{ACTOR} is hugged"
    assert _say("!cuddle", actor="rawhandle") == "rawhandle is hugged"


def test_target_word_is_capped(fake_host) -> None:
    _admin("!cc add cuddle $(target)")
    assert _say("!cuddle " + "a" * 100) == "a" * app.MAX_PLAIN_VALUE_LEN


def test_count_variable_counts_every_run_and_survives_edits(fake_host) -> None:
    _admin("!cc add n Used $(count) times")
    assert [_say("!n") for _ in range(3)] == [f"Used {i} times" for i in (1, 2, 3)]
    _admin("!cc edit n Now at $(count)")
    assert _say("!n") == "Now at 4"
    assert fake_host.kv.store[USES_KEY_PREFIX + "n"] == b"4"


def test_count_is_per_command(fake_host) -> None:
    _admin("!cc add a $(count)")
    _admin("!cc add b $(count)")
    assert (_say("!a"), _say("!a"), _say("!b")) == ("1", "2", "1")


def test_random_variable_stays_in_range_and_uses_stdlib_random(fake_host) -> None:
    _admin("!cc add dicey You rolled $(random 3 5)")
    seen = {int(_say("!dicey").rsplit(" ", 1)[1]) for _ in range(120)}
    assert seen <= {3, 4, 5}
    assert len(seen) > 1  # not constant


def test_random_negative_and_single_point_ranges(fake_host) -> None:
    _admin("!cc add neg $(random -5 -5)")
    assert _say("!neg") == "-5"
    _admin("!cc add span $(random -2 2)")
    assert {int(_say("!span")) for _ in range(80)} <= {-2, -1, 0, 1, 2}


def test_random_calls_randint_with_the_declared_bounds(fake_host, monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(random, "randint", lambda a, b: calls.append((a, b)) or a)
    _admin("!cc add r $(random 10 20)")
    assert _say("!r") == "10"
    assert calls == [(10, 20)]


def test_variables_are_not_rescanned_in_substituted_values(fake_host) -> None:
    _admin("!cc add parrot got $(target)")
    assert _say("!parrot $(count)") == "got $(count)"
    assert _say("!parrot $(user)") == "got $(user)"
    assert fake_host.kv.store[USES_KEY_PREFIX + "parrot"] == b"2"


def test_variable_case_and_inner_whitespace_are_tolerated(fake_host) -> None:
    _admin("!cc add v $( USER ) $(Random  1   1 ) $(COUNT)")
    assert _say("!v") == f"{ACTOR} 1 1"


def test_literal_text_around_variables_is_preserved_byte_for_byte(fake_host) -> None:
    _admin("!cc add keep Price: $5 (cheap) 50% off ~ok~ <b>x</b> a|b")
    assert _say("!keep") == "Price: $5 (cheap) 50% off ~ok~ <b>x</b> a|b"


def test_running_a_command_costs_two_kv_ops(fake_host) -> None:
    _admin("!cc add greet $(count)")
    fake_host.kv.calls.clear()
    _say("!greet")
    assert [c[0] for c in fake_host.kv.calls] == ["get", "increment"]


# ---------------------------------------------------------------------------
# Output safety: the stored response and every substituted value are untrusted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        "@everyone look",
        "ping @here now",
        "<@123456> hi",
        "<@&99> role",
        "<#555> chan",
        "<!here> go",
    ],
)
def test_stored_mass_mention_markup_is_defanged_on_output(stored: str, fake_host) -> None:
    _admin(f"!cc add shout {stored}")
    assert fake_host.kv.store[CMD_KEY_PREFIX + "shout"] == stored.encode()  # stored verbatim
    reply = _say("!shout")
    assert ZWSP in reply
    _assert_no_mention(reply)


def test_a_stored_twitch_style_mention_is_defanged_too(fake_host) -> None:
    _admin("!cc add pinger hey @somebody")
    reply = _say("!pinger")
    assert reply == "hey @" + ZWSP + "somebody"
    _assert_no_mention(reply)


def test_tampered_stored_token_never_goes_live(fake_host) -> None:
    # A `{user:..}` can't be added through `!cc add`, but kv could hold one (old data/tampering).
    _seed(fake_host, "forged", "hi {user:victim} and $(user)")
    reply = _say("!forged", actor=ACTOR)
    assert _live_tokens(reply) == [ACTOR_TOKEN]
    assert "\\{user:victim\\}" in reply


def test_a_stored_token_equal_to_the_invokers_own_is_still_escaped(fake_host) -> None:
    _seed(fake_host, "self", "hi {user:actortok}")
    reply = _say("!self", actor=ACTOR)
    assert _live_tokens(reply) == []
    assert reply == "hi \\{user:actortok\\}"


def test_mod_typed_braces_arrive_host_escaped_and_render_as_typed(fake_host) -> None:
    # The host escapes typed braces on the way in: the stored text already holds `\{`/`\}`.
    _admin("!cc add braces use \\{user:x\\} literally")
    assert _say("!braces") == "use \\{user:x\\} literally"  # detokenizer shows `{user:x}`


def test_trailing_backslash_cannot_neutralise_the_adjacent_live_mention(fake_host) -> None:
    _admin("!cc add bs path\\$(user)")
    reply = _say("!bs", actor=ACTOR)
    assert _live_tokens(reply) == [ACTOR_TOKEN]
    prefix = reply.split("{user:")[0]
    assert (len(prefix) - len(prefix.rstrip("\\"))) % 2 == 0  # even backslash run: not an escape


def test_plain_actor_handle_cannot_forge_a_mention_or_token(fake_host) -> None:
    _admin("!cc add greet welcome $(user)")
    for hostile in ("@everyone", "<@123>", "a\\", "x{user:y", "{user:a b}", "{user:}"):
        reply = _say("!greet", actor=hostile)
        assert _live_tokens(reply) == []
        _assert_no_mention(reply)


def test_plain_target_word_cannot_forge_a_mention_or_token(fake_host) -> None:
    _admin("!cc add greet welcome $(target)")
    for hostile in ("@everyone", "@here", "<@123>", "<#9>", "<!here>", "{user:a b}", "{user:}"):
        reply = _say(f"!greet {hostile}")
        assert _live_tokens(reply) == []
        _assert_no_mention(reply)


def test_host_escaped_typed_token_in_the_arguments_stays_literal(fake_host) -> None:
    _admin("!cc add greet welcome $(target)")
    reply = _say("!greet \\{user:victim\\}")
    assert _live_tokens(reply) == []
    assert reply == "welcome \\{user:victim\\}"


@pytest.mark.parametrize("lead", ["/ban", ".timeout", "!quote", ZWSP + "/ban", NUL + "!x"])
def test_substituted_value_cannot_make_the_reply_lead_with_a_command(lead: str, fake_host) -> None:
    _admin("!cc add parrot $(target) is cool")
    reply = _say(f"!parrot {lead}")
    assert reply.startswith(SAFE_LEAD)
    assert reply.endswith(" is cool")


def test_leading_command_guard_is_idempotent_and_leaves_safe_text_alone(fake_host) -> None:
    assert app._guard_leading_command("welcome") == "welcome"
    assert app._guard_leading_command(" /x") == SAFE_LEAD + " /x"
    guarded = app._guard_leading_command("/ban")
    assert app._guard_leading_command(guarded) == guarded


def test_mention_forming_across_a_live_placeholder_is_broken(fake_host) -> None:
    # `<!` + live placeholder: the resolved display name could complete `<!here>` / `<@id>`.
    _admin("!cc add edge <!$(target)> and <@$(user)>")
    reply = _say(f"!edge {TARGET}")
    assert _live_tokens(reply) == [TARGET_TOKEN, ACTOR_TOKEN]
    _assert_no_mention(reply)


def test_control_characters_in_the_arguments_are_stripped(fake_host) -> None:
    _admin("!cc add greet $(target)")
    reply = _say(f"!greet ab{NUL}c{chr(7)}d")
    assert reply == "abcd"


def test_end_to_end_fuzz_never_leaks_a_forged_mention_or_foreign_token(fake_host) -> None:
    rng = random.Random(20261022)  # noqa: S311 - deterministic test fuzz, not security
    atoms = [
        "@everyone",
        "@here",
        "@bob",
        "<@123>",
        "<@&7>",
        "<#4>",
        "<!here>",
        "<!",
        "<@",
        "@",
        "\\",
        "\\\\",
        "\\{",
        "\\}",
        "{user:evil}",
        "{user:actortok}",
        "{user:targettok}",
        "{",
        "}",
        "$(user)",
        "$(target)",
        "$(touser)",
        "$(count)",
        "$(random 1 9)",
        "word",
        "x",
        " ",
        "/ban",
        ".x",
        "!x",
        ">",
        "&",
        "#",
    ]
    rendered = live_seen = guarded = 0
    for case in range(400):
        body = "".join(rng.choice(atoms) for _ in range(rng.randint(1, 9))).strip() or "x"
        name = f"f{case}"
        fake_host.kv.store.pop(NAMES_KEY, None)
        _seed(fake_host, name, body)  # kv-seeded: bypasses the add-time rules on purpose
        arg_bits = "".join(rng.choice(atoms) for _ in range(rng.randint(0, 5)))
        actor = rng.choice([ACTOR, "rawhandle", "@everyone", "{user:evil}", None])
        result = _run(transform(_event(f"!{name} {arg_bits}".strip(), actor=actor)))
        assert result is not None
        reply = result.payload["text"]
        if reply.startswith("Custom command storage is unavailable"):
            continue  # a body with more than MAX_VARIABLES variables is refused, loudly
        rendered += 1
        # Only the host-minted actor token and the FIRST mention token in the invocation may be
        # live; every other `{user:..}` (stored text, later mentions) must come out escaped.
        actor_match = re.fullmatch(r"\{user:([A-Za-z0-9_:.-]+)\}", actor or "")
        allowed = {actor_match.group(1)} if actor_match else set()
        allowed.update(identity.mention_tokens(arg_bits)[:1])
        live = _live_tokens(reply)
        assert set(live) <= allowed, (body, arg_bits, actor, reply)
        live_seen += bool(live)
        _assert_no_mention(reply)
        if reply.startswith(SAFE_LEAD):
            guarded += 1
        else:
            visible = "".join(c for c in reply if c not in (ZWSP, NUL)).lstrip()
            assert not visible.startswith(("/", ".", "!")), reply
    assert rendered > 250, f"fuzz examined too few rendered replies ({rendered})"
    assert live_seen > 40, f"fuzz never produced a live mention ({live_seen})"
    assert guarded > 5, f"fuzz never exercised the leading-command guard ({guarded})"


def test_transform_output_is_finalised_dispatch_does_not_resanitize(fake_host) -> None:
    _admin("!cc add greet Welcome $(user)")
    result = _run(transform(_event("!greet", actor=ACTOR)))
    assert result is not None
    envelope = StageEnvelope(
        tenant="t",
        community="c",
        app_id="waddles.core.example.customcommand",
        stage="action",
        event=result,
        ts="2026-10-05T00:00:00.000Z",
    )
    _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.relay_calls == [
        ("twitch", {"channel": "12345", "text": "Welcome {user:actortok}"})
    ]


# ---------------------------------------------------------------------------
# Storage hygiene, locking, failure handling, PII-free logs
# ---------------------------------------------------------------------------


def test_every_kv_key_uses_the_host_charset_and_no_colon(fake_host) -> None:
    _admin("!cc add welcome-world_2 $(count)")
    _say("!welcome-world_2")
    _admin("!cc edit welcome-world_2 x")
    _admin("!cc del welcome-world_2")
    keys = {key for _, args in fake_host.kv.calls for key in args[:1]}
    assert keys
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+", key) for key in keys)
    assert not any(":" in key for key in keys)


def test_mutation_lock_is_claimed_and_released(fake_host) -> None:
    _admin("!cc add welcome hi")
    calls = fake_host.kv.calls
    assert calls[0] == ("increment", (LOCK_KEY, 1, app.LOCK_TTL_SECONDS))
    assert calls[-1] == ("delete", (LOCK_KEY,))
    assert LOCK_KEY not in fake_host.kv.store


def test_a_concurrent_mutation_is_told_to_retry_and_changes_nothing(fake_host) -> None:
    fake_host.kv.store[LOCK_KEY] = b"1"  # another moderator holds the claim
    assert _admin("!cc add welcome hi") == "Another change is in progress - please try again."
    assert NAMES_KEY not in fake_host.kv.store
    assert CMD_KEY_PREFIX + "welcome" not in fake_host.kv.store
    # the holder's lock is untouched by the loser (it is not ours to delete)
    assert fake_host.kv.store[LOCK_KEY] == b"2"


@pytest.mark.parametrize(
    ("op", "text"),
    [
        ("get", "!cc list"),
        ("get", "!welcome"),
        ("increment", "!welcome"),
        ("increment", "!cc add other text"),
        ("set", "!cc add other text"),
        ("set", "!cc edit welcome text2"),
        ("delete", "!cc del welcome"),
    ],
)
def test_a_kv_failure_replies_loudly_and_logs_the_exception_class_only(
    op: str, text: str, fake_host
) -> None:
    _admin("!cc add welcome hi")  # healthy setup
    fake_host.kv.fail_ops = {op}
    fake_host.log_calls.clear()
    result = _run(transform(_mod(text)))
    assert result is not None
    assert result.payload["text"] == (
        "Custom command storage is unavailable right now - please try again."
    )
    errors = [c for c in fake_host.log_calls if c[0] == "ERROR"]
    assert [c[1] for c in errors] == ["customcommand.kv_failure"]
    assert errors[0][2] == {"error_type": "_HostKvError"}


def test_kv_get_failure_on_invoke_replies_unavailable(fake_host) -> None:
    fake_host.kv.fail_ops = {"get"}
    result = _run(transform(_event("!welcome")))
    assert result is not None
    assert (
        result.payload["text"]
        == "Custom command storage is unavailable right now - please try again."
    )


def test_failed_mutation_releases_the_lock(fake_host) -> None:
    fake_host.kv.fail_ops = {"set"}
    assert "unavailable" in _admin("!cc add welcome hi")
    assert LOCK_KEY not in fake_host.kv.store


def test_corrupt_index_fails_loud_and_is_never_reset(fake_host) -> None:
    fake_host.kv.store[NAMES_KEY] = b"{not json"
    assert "unavailable" in _admin("!cc list")
    assert "unavailable" in _admin("!cc add a b")
    assert fake_host.kv.store[NAMES_KEY] == b"{not json"
    fake_host.kv.store[NAMES_KEY] = b'{"a": 1}'
    assert "unavailable" in _admin("!cc list")


def test_corrupt_stored_command_fails_loud_without_counting_a_run(fake_host) -> None:
    fake_host.kv.store[CMD_KEY_PREFIX + "bad"] = b"hi $(nope)"
    assert "unavailable" in _say("!bad")
    fake_host.kv.store[CMD_KEY_PREFIX + "bin"] = b"\xff\xfe"
    assert "unavailable" in _say("!bin")
    assert USES_KEY_PREFIX + "bad" not in fake_host.kv.store


def test_logs_and_keys_never_carry_names_responses_arguments_or_actors(fake_host) -> None:
    secret_name, secret_text, secret_arg, secret_actor = (
        "zzname",
        "topsecretresponse",
        "argzzz",
        "rawactor99",
    )
    _admin(f"!cc add {secret_name} {secret_text} $(user) $(target)", actor=secret_actor)
    _say(f"!{secret_name} {secret_arg}", actor=secret_actor)
    _admin(f"!cc edit {secret_name} {secret_text}2", actor=secret_actor)
    _say("!cc list", actor=secret_actor)
    _admin(f"!cc del {secret_name}", actor=secret_actor)
    _admin("!cc add " + "bad name" + chr(0), actor=secret_actor)
    blob = json.dumps(fake_host.log_calls)
    for forbidden in (secret_name, secret_text, secret_arg, secret_actor):
        assert forbidden not in blob
    assert fake_host.log_calls, "expected at least one log line (denominator)"
    for _, _, fields in fake_host.log_calls:
        assert set(fields) <= {"op", "count", "platform", "error_type"}
    for _, args in fake_host.kv.calls:
        assert secret_actor not in args[0]


def test_no_actor_identity_is_persisted(fake_host) -> None:
    _admin("!cc add greet $(user)", actor="rawactor99")
    _say("!greet", actor="rawactor99")
    stored = b"".join(fake_host.kv.store.values())
    assert b"rawactor99" not in stored


def test_bundle_is_activation_gated_only_no_feature_flag() -> None:
    source = Path(app.__file__).read_text()
    assert "feature_enabled" not in source
    assert "waddles.command-" not in source
    assert "flags" not in {m for m in dir(app) if not m.startswith("_")}


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def _envelope(payload: dict, platform: str = "twitch") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.customcommand",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor=ACTOR,
            payload=payload,
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_relays_the_finalised_text(platform: str, fake_host) -> None:
    result = _run(
        dispatch(_envelope({"channel_id": "c1", "text": "greet"}, platform), {}, http_client=None)
    )
    assert fake_host.relay_calls == [(platform, {"channel": "c1", "text": "greet"})]
    assert (result.transport, result.detail, result.sub_type, result.http_status) == (
        platform,
        "relayed",
        None,
        None,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "greet"},
        {"channel_id": "c1"},
        {"channel_id": "c1", "text": ""},
        {"channel_id": "c1", "text": 5},
    ],
)
def test_dispatch_rejects_a_payload_missing_channel_or_text(payload: dict, fake_host) -> None:
    with pytest.raises(ValueError):
        _run(dispatch(_envelope(payload), {}, http_client=None))
    assert fake_host.relay_calls == []


def test_interior_whitespace_in_the_response_is_preserved_and_tabs_separate_words(
    fake_host,
) -> None:
    assert _admin("!cc add spaced   two  spaces   inside").startswith("Added")
    assert fake_host.kv.store[CMD_KEY_PREFIX + "spaced"] == b"two  spaces   inside"
    assert _admin("!cc add tabbed" + chr(9) + "after tab").startswith("Added !tabbed")
    assert fake_host.kv.store[CMD_KEY_PREFIX + "tabbed"] == b"after tab"
    assert _admin("!cc edit spaced" + chr(9) + "x") == "Updated !spaced."


def test_an_oversized_or_odd_actor_token_is_not_treated_as_host_minted(fake_host) -> None:
    _admin("!cc add greet $(user)")
    assert _live_tokens(_say("!greet", actor="{user:" + "a" * 129 + "}")) == []
    assert _live_tokens(_say("!greet", actor="{user:tok:1.a-b_c}")) == ["tok:1.a-b_c"]
    assert _live_tokens(_say("!greet", actor="{user:a}b")) == []


def test_only_the_first_mention_is_the_target(fake_host) -> None:
    _admin("!cc add pair $(target)")
    assert _say("!pair {user:one} {user:two}") == "{user:one}"
