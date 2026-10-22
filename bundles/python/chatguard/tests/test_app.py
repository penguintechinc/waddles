"""Host-native tests for `chatguard`: shared fake kv/identity hosts, no WASM.

The fakes model the real host's stage split: `identity` exists in the PROCESS stage only and
`relay` in the ACTION stage only (`core/svc_process` / `core/svc_action` capabilities), so a
capability used from the wrong stage is recorded in `host.stage_violations` and fails the test at
teardown -- the bundle cannot lean on a capability its stage does not have.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import random
import time
import types
from collections.abc import Iterator
from typing import Any

import pytest
from waddle_sdk import community_kv
from waddle_sdk.flask_core.bundle_runtime import bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeIdentityHost, FakeKvHost, install_fake_kv_host

import app
from app import GuardConfig, dispatch, transform

ACTOR = "11111111-1111-4111-8111-111111111111"
TARGET = "22222222-2222-4222-8222-222222222222"
COMMUNITY = "comm-1"
ACTOR_TOKEN = "{user:tok-author}"  # noqa: S105 -- a host-minted placeholder, not a credential
SNOWFLAKE = "123456789012345678"
CHANNEL = "999888777666555444"
ZWSP = chr(0x200B)

_MSG_IDS = itertools.count(100000000000000000)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _WitErr(Exception):  # noqa: N818 -- mirrors the generated `Err`
    """Shaped like the generated WIT `Err` (`.value` holds the error variant)."""

    def __init__(self, value: Any) -> None:
        super().__init__("host error")
        self.value = value


class Error_Denied:  # noqa: N801 -- mirrors the generated WIT case-class naming
    """Stand-in for the generated `Error_Denied` case."""

    value = "unsupported_op"


class _Host:
    def __init__(self, kv: FakeKvHost, ident: FakeIdentityHost) -> None:
        self.kv, self.ident = kv, ident
        self.stage = "process"
        self.stage_violations: list[str] = []
        self.relay_calls: list[tuple[str, dict[str, Any]]] = []
        self.relay_failure: Exception | None = None
        self.relay_failure_ops: set[str] = set()
        self.log_calls: list[tuple[Any, str, str]] = []
        self.now_ms = 1_700_000_000_000

    def expire(self, marker: str) -> None:
        """Simulate the kv TTL lapsing for every key containing `marker` (the fake ignores TTL)."""
        for key in [k for k in self.kv.store if marker in k]:
            del self.kv.store[key]

    def logged(self) -> str:
        """Every log message + field rendered as one string (for PII assertions)."""
        return json.dumps([(m, json.loads(f)) for _, m, f in self.log_calls], ensure_ascii=False)

    def pushes(self, op: str) -> list[dict[str, Any]]:
        """The relay payloads of one op (`chat.delete`; plain sends are `chat.send`)."""
        return [m for _, m in self.relay_calls if m.get("op", "chat.send") == op]


def _install(monkeypatch: pytest.MonkeyPatch) -> _Host:
    kv = install_fake_kv_host(monkeypatch)
    ident = FakeIdentityHost(members={ACTOR, TARGET}, actor=ACTOR)
    host = _Host(kv, ident)

    def resolve_actor() -> str:
        if host.stage != "process":
            host.stage_violations.append("identity.resolve_actor outside the process stage")
        return ident.resolve_actor()

    def resolve_mention(token: str) -> str:
        if host.stage != "process":
            host.stage_violations.append("identity.resolve_mention outside the process stage")
        return ident.resolve_mention(token)

    def push(provider: str, message_json: str) -> None:
        if host.stage != "action":
            host.stage_violations.append("relay.push outside the action stage")
        message = json.loads(message_json)
        if host.relay_failure is not None and (
            not host.relay_failure_ops or message.get("op", "chat.send") in host.relay_failure_ops
        ):
            raise host.relay_failure
        host.relay_calls.append((provider, message))

    import wit_world

    ns = wit_world.imports
    ns.identity = types.SimpleNamespace(
        resolve_actor=resolve_actor, resolve_mention=resolve_mention
    )
    ns.relay = types.SimpleNamespace(push=push)
    ns.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, f: host.log_calls.append((lvl, msg, f)),
    )
    ns.clock = types.SimpleNamespace(
        now_millis=lambda: host.now_ms, now_rfc3339=lambda: "x", monotonic_nanos=lambda: 0
    )
    return host


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Host]:
    h = _install(monkeypatch)
    yield h
    assert h.stage_violations == []


def _event(
    text: str,
    *,
    actor: str | None = ACTOR_TOKEN,
    platform: str = "discord",
    drop: tuple[str, ...] = (),
    **extra: Any,
) -> PlatformEvent:
    payload: dict[str, Any] = {
        "text": text,
        "channel_id": CHANNEL,
        "message_id": str(next(_MSG_IDS)),
        **extra,
    }
    for key in drop:
        payload.pop(key, None)
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at="2026-10-05T00:00:00.000Z",
    )


def _transform(host: _Host, event: PlatformEvent, community: str | None = COMMUNITY) -> Any:
    host.stage = "process"

    async def go() -> PlatformEvent | None:
        with bundle_context(
            tenant="t", community=community, app_id="waddles.core.example.chatguard"
        ):
            return await transform(event)

    return _run(go())


def say(host: _Host, text: str, **kw: Any) -> PlatformEvent | None:
    """Run one chat line through `transform`."""
    return _transform(host, _event(text, **kw))


def reply(host: _Host, text: str, **kw: Any) -> str:
    """Run a command line and return its reply text (asserts a reply event came back)."""
    out = say(host, text, **kw)
    assert out is not None and out.payload["command"] == "reply"
    return str(out.payload["text"])


def mod(host: _Host, text: str, **kw: Any) -> str:
    """A moderator's command reply."""
    return reply(host, text, is_mod=True, **kw)


def _envelope(event: PlatformEvent, community: str | None = COMMUNITY) -> StageEnvelope:
    return StageEnvelope(
        tenant="t",
        community=community,
        app_id="waddles.core.example.chatguard",
        stage="action",
        event=event,
        ts="2026-10-05T00:00:00.000Z",
    )


def send(host: _Host, event: PlatformEvent, community: str | None = COMMUNITY) -> Any:
    """Run an event through `dispatch` in the action stage."""
    host.stage = "action"
    return _run(dispatch(_envelope(event, community), {}, http_client=None))


def configure(host: _Host, **changes: Any) -> GuardConfig:
    """Write a config straight into kv (enabled rules / thresholds / words)."""
    enabled = frozenset(changes.pop("enabled", RULES_ALL))
    cfg = GuardConfig(enabled=enabled, **changes)
    host.kv.store[community_kv._scoped_key(COMMUNITY, app._CONFIG_KEY)] = cfg.to_bytes()
    return cfg


RULES_ALL = frozenset(app.RULES)


def stored_config(host: _Host) -> GuardConfig:
    raw = host.kv.store[community_kv._scoped_key(COMMUNITY, app._CONFIG_KEY)]
    cfg = app._config_from_bytes(raw)
    assert cfg is not None
    return cfg


def pseudonym(user: str = ACTOR) -> str:
    return hashlib.sha256(f"{COMMUNITY}:{user}".encode()).hexdigest()


def permit_key(user: str) -> str:
    return community_kv._scoped_key(COMMUNITY, app._permit_key(pseudonym(user)))


def warn_key(ps: str) -> str:
    return community_kv._scoped_key(COMMUNITY, app._warn_key(ps))


def violation(rule: str = "caps", platform: str = "discord", **overrides: Any) -> PlatformEvent:
    payload: dict[str, Any] = {
        "command": "violation",
        "rule": rule,
        "channel_id": CHANNEL,
        "message_id": SNOWFLAKE,
        "ps": pseudonym(),
        "warn_s": 30,
    }
    payload.update(overrides)
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=ACTOR_TOKEN,
        payload=payload,
        occurred_at="2026-10-05T00:00:00.000Z",
    )


# -- pure detection ------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "http://x.y",
        "https://evil.com/a?b=c",
        "HTTPS://EVIL.COM",
        "www.foo.bar",
        "evil.com",
        "EVIL.COM",
        "visit evil.com now",
        "check evil[.]com",
        "evil(.)com",
        "evil{.}com",
        "evil dot com",
        "evil(dot)com",
        "evil" + chr(0xFF0E) + "com",
        "evil" + chr(0x3002) + "com",
        "evi" + ZWSP + "l.com",
        "".join(chr(0xFF00 + ord(c) - 0x20) for c in "evil") + ".com",
        "discord.gg/abc123",
        "1.2.3.4",
        "go to 192.168.0.1:8080",
        "bit.ly/xyz",
        "twitch.tv/somebody",
        "ok " + "a.b.c.d.e.xyz",
    ],
)
def test_links_are_detected(text: str) -> None:
    assert app.has_link(text), text


@pytest.mark.parametrize(
    "text",
    [
        "hello world",
        "e.g. this one",
        "I scored 3.5 today",
        "version 1.2.3",
        "main.py and notes.txt and readme.md",
        "user@example.com",
        "Hello.Thanks for coming",
        "999.999.999.999",
        "wait... what?",
        "5 p.m.",
        "{user:abc.com} hi",  # a minted placeholder is not a link
        "x" * 300,
    ],
)
def test_ordinary_text_is_not_a_link(text: str) -> None:
    assert not app.has_link(text), text


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("HELLO EVERYONE HOW ARE YOU", True),
        ("Hello everyone how are you", False),
        ("HI", False),  # under the minimum letter count
        ("HELLO WORLD", True),
        ("hello WORLD again friends", False),
        ("{user:ABCDEFGHIJKLMNOP} hello there friend", False),
        ("HTTPS://EXAMPLE.COM/PATH THIS IS fine really", False),
        ("<:POGGERS:123456789012345678> okay then nice", False),
        ("ПРИВЕТ КАК ДЕЛА", True),
        ("こんにちは世界こんにちは", False),
        ("1234567890 !!! ???", False),
    ],
)
def test_caps_rule(text: str, flagged: bool) -> None:
    assert app.caps_violation(text, GuardConfig()) is flagged


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("!!!!!!!!!!!!!!!!", True),
        ("?!?!?!?!?!?!?!", True),
        ("hello, world! how are you?", False),
        ("\U0001f600" * 12, True),
        ("<:pog:123456789012345678>" * 12, True),
        ("<:pog:123456789012345678> nice play", False),
        ("short !!", False),  # under the minimum visible length
        ("a" * 40 + "!!!", False),
        ("https://example.com/!!!!!!!!!!!!!!!!!! hello there friend", False),
    ],
)
def test_symbols_rule(text: str, flagged: bool) -> None:
    assert app.symbols_violation(text, GuardConfig()) is flagged


def test_thresholds_are_respected() -> None:
    loose = GuardConfig(caps_percent=100, caps_min=5)
    assert not app.caps_violation("HELLO there", loose)
    assert app.caps_violation("HELLO", loose)
    tight = GuardConfig(caps_percent=10, caps_min=5)
    assert app.caps_violation("Hello Wor", tight)


@pytest.mark.parametrize(
    ("words", "text", "flagged"),
    [
        (("badword",), "this is a badword ok", True),
        (("badword",), "this is a BADWORD ok", True),
        (("badword",), "badword!", True),
        (("badword",), "badwordy", False),
        (("badword",), "abadword", False),
        (("badword",), "bad word", False),
        (("badword",), "b" + ZWSP + "adword", True),
        (("badword",), "".join(chr(0xFF00 + ord(c) - 0x20) for c in "badword"), True),
        (("bad phrase",), "a BAD    Phrase here", True),
        (("bad phrase",), "bad", False),
        (("bad",), "bad_word", True),
        (("café",), "a CAFE here", True),
        (("badword",), "{user:badword} hello", False),
        ((), "anything at all", False),
        (("a.b", "x+y"), "a.b and x+y", True),  # regex metacharacters are literal
        (("a.b",), "axb", False),
    ],
)
def test_blacklist_rule(words: tuple[str, ...], text: str, flagged: bool) -> None:
    folded = tuple(app.normalize_word(w) or "" for w in words)
    assert app.blacklist_hit(text, folded) is flagged


@pytest.mark.parametrize("raw", ["", "a", "x" * 41, "!!!", "{user:x}", "  ", "}{"])
def test_unusable_blacklist_entries_are_rejected(raw: str) -> None:
    assert app.normalize_word(raw) is None


def test_blacklist_entries_are_stored_folded() -> None:
    assert app.normalize_word("  BAD   Phrase ") == "bad phrase"


_ADVERSARIAL = {
    "dotted": "a." * 1000,
    "hyphenated": "a-" * 1000,
    "digits": "1." * 1000,
    "long-label": "a" * 2000,
    "max-labels": ("a" * 62 + ".") * 30,
    "only-dots": "." * 2000,
    "obfuscation": "[.](.){.}" * 220,
    "spoken-dots": "a dot " * 330,
    "ipv4-prefix": "1.1.1." * 330,
    "emote-prefix": "<:a:1>" * 300,
    "placeholder-prefix": "{user:" * 300,
    "caps": "A" * 2000,
    "symbols": "!" * 2000,
}


@pytest.mark.parametrize("text", list(_ADVERSARIAL.values()), ids=list(_ADVERSARIAL))
def test_adversarial_input_cannot_stall_the_filters(text: str) -> None:
    """Every filter finishes a worst-case 2000-char line well inside the invocation budget."""
    cfg = GuardConfig(words=tuple(f"word{i}" for i in range(app.MAX_WORDS)))
    started = time.perf_counter()
    app.has_link(text)
    app.caps_violation(text, cfg)
    app.symbols_violation(text, cfg)
    app.blacklist_hit(text, cfg.words)
    assert time.perf_counter() - started < 0.5


# -- config blob ---------------------------------------------------------------


def test_config_round_trips() -> None:
    cfg = GuardConfig(
        enabled=frozenset({"caps", "links"}), caps_percent=55, words=("one", "two words")
    )
    assert app._config_from_bytes(cfg.to_bytes()) == cfg


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(v=2),
        lambda d: d.update(on=["nope"]),
        lambda d: d.update(cp=0),
        lambda d: d.update(cp=101),
        lambda d: d.update(cm=0),
        lambda d: d.update(sp=101),
        lambda d: d.update(pm=601),
        lambda d: d.update(ws=-1),
        lambda d: d.update(ws=3601),
        lambda d: d.update(w=["x"]),  # too short
        lambda d: d.update(w=["ab"] * (app.MAX_WORDS + 1)),
        lambda d: d.pop("w"),
        lambda d: d.update(cp="high"),
    ],
)
def test_out_of_range_configs_are_rejected(host: _Host, mutate: Any) -> None:
    data = json.loads(GuardConfig().to_bytes())
    mutate(data)
    assert app._config_from_bytes(json.dumps(data).encode()) is None


def test_non_json_config_is_rejected(host: _Host) -> None:
    assert app._config_from_bytes(b"{nope") is None
    assert app._config_from_bytes(b"\xff\xfe") is None


# -- screening ordinary chat ---------------------------------------------------


def test_an_unconfigured_community_passes_everything_with_one_kv_read(host: _Host) -> None:
    assert say(host, "HELLO EVERYONE evil.com !!!!!!!!!!!!") is None
    assert [c[0] for c in host.kv.calls] == ["get"]
    assert host.ident.calls == []


def test_configured_but_all_rules_off_passes(host: _Host) -> None:
    configure(host, enabled=frozenset())
    assert say(host, "HELLO EVERYONE evil.com") is None


@pytest.mark.parametrize(
    "badge",
    [{"is_mod": True}, {"is_broadcaster": True}, {"is_mod": True, "is_broadcaster": False}],
)
def test_moderators_and_broadcasters_are_exempt_without_any_kv_read(
    host: _Host, badge: dict[str, Any]
) -> None:
    configure(host)
    host.kv.calls.clear()
    assert say(host, "HELLO EVERYONE evil.com", **badge) is None
    assert host.kv.calls == []


@pytest.mark.parametrize(
    "badge",
    [
        {"is_mod": "true"},
        {"is_mod": 1},
        {"is_mod": "yes", "is_broadcaster": "true"},
        {"is_broadcaster": [True]},
        {"is_mod": False},
        {},
    ],
)
def test_only_a_strict_true_badge_is_exempt(host: _Host, badge: dict[str, Any]) -> None:
    configure(host)
    out = say(host, "HELLO EVERYONE HOW ARE YOU", **badge)
    assert out is not None and out.payload["rule"] == "caps"


def test_a_violation_event_carries_no_message_text(host: _Host) -> None:
    configure(host)
    out = say(host, "SECRETSHOUT EVERYONE HOW ARE YOU", message_id=SNOWFLAKE)
    assert out is not None
    assert set(out.payload) == {"command", "rule", "channel_id", "message_id", "ps", "warn_s"}
    assert out.payload["command"] == "violation" and out.payload["rule"] == "caps"
    assert out.payload["channel_id"] == CHANNEL and out.payload["message_id"] == SNOWFLAKE
    assert out.payload["ps"] == pseudonym() and out.payload["warn_s"] == app.DEFAULT_WARN_SECONDS
    assert "SECRETSHOUT" not in json.dumps(out.payload)
    assert out.actor == ACTOR_TOKEN and out.platform == "discord"


def test_an_unlinked_author_gets_the_shared_anonymous_bucket(host: _Host) -> None:
    configure(host)
    host.ident.actor = None
    out = say(host, "HELLO EVERYONE HOW ARE YOU")
    assert out is not None and out.payload["ps"] == app._ANON_PSEUDONYM


def test_rules_fire_in_a_fixed_order(host: _Host) -> None:
    configure(host, words=("badword",))
    text = "BADWORD EVIL.COM HELLO EVERYONE " + "!" * 60
    order = []
    for _ in app.RULES:
        out = say(host, text)
        assert out is not None
        order.append(out.payload["rule"])
        configure(host, enabled=RULES_ALL - set(order), words=("badword",))
    assert order == list(app.RULES)


@pytest.mark.parametrize(
    ("rule", "text"),
    [
        ("caps", "HELLO EVERYONE HOW ARE YOU"),
        ("symbols", "!!!!!!!!!!!!!!!!"),
        ("links", "visit evil.com"),
        ("blacklist", "that is a badword indeed"),
    ],
)
def test_only_enabled_rules_fire(host: _Host, rule: str, text: str) -> None:
    configure(host, enabled=frozenset({rule}), words=("badword",))
    out = say(host, text)
    assert out is not None and out.payload["rule"] == rule
    for other in app.RULES:
        if other != rule:
            configure(host, enabled=frozenset({other}), words=("badword",))
            blocked = say(host, text)
            assert blocked is None or blocked.payload["rule"] == other


def test_clean_chat_passes_every_rule(host: _Host) -> None:
    configure(host, words=("badword",))
    for line in ("hello everyone", "great stream today!", "gg wp :)", "see you tomorrow, folks"):
        assert say(host, line) is None


def test_a_permit_lets_a_link_through(host: _Host) -> None:
    configure(host, enabled=frozenset({"links"}))
    host.kv.store[permit_key(ACTOR)] = str(host.now_ms + 60_000).encode()
    assert say(host, "visit evil.com") is None


def test_an_expired_permit_no_longer_lets_a_link_through(host: _Host) -> None:
    configure(host, enabled=frozenset({"links"}))
    host.kv.store[permit_key(ACTOR)] = str(host.now_ms + 60_000).encode()
    host.now_ms += 61_000  # the stored expiry has passed even if the kv TTL had not fired yet
    out = say(host, "visit evil.com")
    assert out is not None and out.payload["rule"] == "links"


def test_a_lapsed_kv_ttl_ends_the_permit(host: _Host) -> None:
    configure(host, enabled=frozenset({"links"}))
    host.kv.store[permit_key(ACTOR)] = str(host.now_ms + 60_000).encode()
    host.expire(".permit.")
    assert say(host, "visit evil.com") is not None


def test_one_users_permit_does_not_cover_another(host: _Host) -> None:
    configure(host, enabled=frozenset({"links"}))
    host.kv.store[permit_key(TARGET)] = str(host.now_ms + 60_000).encode()
    out = say(host, "visit evil.com")  # the author is ACTOR, not TARGET
    assert out is not None and out.payload["rule"] == "links"


def test_a_permit_does_not_lift_the_other_rules(host: _Host) -> None:
    configure(host, enabled=frozenset({"links", "caps"}))
    host.kv.store[permit_key(ACTOR)] = str(host.now_ms + 60_000).encode()
    out = say(host, "VISIT EVIL.COM RIGHT NOW FRIENDS")
    assert out is not None and out.payload["rule"] == "caps"


def test_an_unlinked_author_cannot_hold_a_permit(host: _Host) -> None:
    configure(host, enabled=frozenset({"links"}))
    host.ident.actor = None
    assert say(host, "visit evil.com") is not None


def test_an_identity_outage_fails_closed_and_is_logged(host: _Host) -> None:
    configure(host, enabled=frozenset({"links"}))
    host.ident.granted = False
    out = say(host, "visit evil.com")
    assert out is not None and out.payload["rule"] == "links"
    assert "chatguard.identity_error" in host.logged()


def test_identity_is_only_consulted_when_a_link_is_present(host: _Host) -> None:
    configure(host, enabled=frozenset({"links"}))
    assert say(host, "no link in here") is None
    assert host.ident.calls == []
    say(host, "visit evil.com")
    assert host.ident.calls == ["actor"]


def test_screening_costs_at_most_three_kv_reads(host: _Host) -> None:
    configure(host, enabled=frozenset({"links"}))
    host.kv.calls.clear()
    say(host, "visit evil.com")
    assert len(host.kv.calls) <= 3
    assert all(c[0] == "get" for c in host.kv.calls)


def test_a_corrupt_config_is_logged_and_treated_as_no_rules(host: _Host) -> None:
    host.kv.store[community_kv._scoped_key(COMMUNITY, app._CONFIG_KEY)] = b"{nope"
    assert say(host, "HELLO EVERYONE HOW ARE YOU") is None
    assert "chatguard.config_corrupt" in host.logged()


def test_a_kv_failure_while_screening_is_loud(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(key: str) -> bytes:
        raise _WitErr(Error_Denied())

    monkeypatch.setattr(__import__("wit_world").imports.kv, "get", boom)
    with pytest.raises(RuntimeError, match="state backend"):
        say(host, "HELLO EVERYONE HOW ARE YOU")
    assert "chatguard.kv_error" in host.logged()


def test_a_violation_without_a_channel_is_loud(host: _Host) -> None:
    configure(host)
    with pytest.raises(ValueError, match="channel_id"):
        say(host, "HELLO EVERYONE HOW ARE YOU", drop=("channel_id",))


def test_a_missing_message_id_still_produces_a_violation(host: _Host) -> None:
    configure(host)
    out = say(host, "HELLO EVERYONE HOW ARE YOU", drop=("message_id",))
    assert out is not None and out.payload["message_id"] is None


@pytest.mark.parametrize("text", [None, 5, "", "   "])
def test_non_text_lines_are_ignored(host: _Host, text: Any) -> None:
    event = _event("x")
    event.payload["text"] = text
    assert _transform(host, event) is None


def test_tenant_wide_activation_screens_under_the_sentinel_scope(host: _Host) -> None:
    host.kv.store[community_kv._scoped_key(None, app._CONFIG_KEY)] = GuardConfig(
        enabled=frozenset({"caps"})
    ).to_bytes()
    out = _transform(host, _event("HELLO EVERYONE HOW ARE YOU"), community=None)
    assert out is not None and out.payload["rule"] == "caps"
    assert out.payload["ps"] == hashlib.sha256(f"0:{ACTOR}".encode()).hexdigest()


def test_the_bots_own_warnings_never_trip_a_filter(host: _Host) -> None:
    """A warning relayed into chat must not itself be a violation (no moderation loop)."""
    configure(host, words=("badword",))
    for rule in app.RULES:
        for deleted in (True, False):
            text = app._warn_text(ACTOR_TOKEN, rule, deleted)
            assert say(host, text, actor="{user:tok-bot}") is None, text
    full = GuardConfig(enabled=RULES_ALL, words=("badword",))
    replies = [
        app._USAGE,
        app._PERMIT_USAGE,
        app._DENIED_MSG,
        app._UNAVAILABLE_MSG,
        app._list_reply(full, "discord"),
        app._list_reply(full, "twitch"),
        app._list_reply(GuardConfig(), "discord"),
        "{user:tok-target} may post links for 600s.",
        "chatguard reset to defaults (all rules off, blacklist cleared)",
    ]
    for reply_text in replies:
        assert say(host, reply_text, actor="{user:tok-bot}") is None, reply_text


# -- commands ------------------------------------------------------------------


def test_bare_chatguard_is_the_usage_line_for_anyone(host: _Host) -> None:
    assert reply(host, "!chatguard") == app._USAGE
    assert reply(host, "!chatguard", is_mod=True) == app._USAGE


@pytest.mark.parametrize(
    "text",
    [
        "!chatguard enable caps",
        "!chatguard set caps 50",
        "!chatguard add blacklist foo",
        "!chatguard list",
        "!chatguard reset",
        "!permit {user:tok-target}",
        "!chatguard permit {user:tok-target}",
    ],
)
@pytest.mark.parametrize("badge", [{}, {"is_mod": False}, {"is_mod": "true"}, {"is_mod": 1}])
def test_commands_are_denied_without_a_strict_true_badge(
    host: _Host, text: str, badge: dict[str, Any]
) -> None:
    host.ident.add_mention("tok-target", TARGET)
    assert reply(host, text, **badge) == app._DENIED_MSG
    assert community_kv._scoped_key(COMMUNITY, app._CONFIG_KEY) not in host.kv.store
    assert not [k for k in host.kv.store if "permit" in k]


@pytest.mark.parametrize("rule", app.RULES)
def test_enable_and_disable_each_rule(host: _Host, rule: str) -> None:
    assert mod(host, f"!chatguard enable {rule}") == f"chatguard {rule} enabled"
    assert rule in stored_config(host).enabled
    assert mod(host, f"!chatguard disable {rule}") == f"chatguard {rule} disabled"
    assert rule not in stored_config(host).enabled


def test_sub_module_toggle_form_works_too(host: _Host) -> None:
    assert mod(host, "!chatguard caps enable") == "chatguard caps enabled"
    assert "caps" in stored_config(host).enabled


def test_enabling_keeps_the_other_settings(host: _Host) -> None:
    configure(host, enabled=frozenset({"links"}), caps_percent=55, words=("badword",))
    mod(host, "!chatguard enable caps")
    cfg = stored_config(host)
    assert cfg.enabled == {"links", "caps"} and cfg.caps_percent == 55 and cfg.words == ("badword",)


@pytest.mark.parametrize(
    "text",
    [
        "!chatguard enable",
        "!chatguard enable nothing",
        "!chatguard enable caps links",
        "!chatguard banana",
        "!chatguard list extra",
        "!chatguard reset extra",
        "!chatguard sub caps",
        "!chatguard caps",
    ],
)
def test_bad_grammar_is_the_static_usage_line_never_an_echo(host: _Host, text: str) -> None:
    out = mod(host, text)
    assert out == app._USAGE
    assert "banana" not in out and "nothing" not in out


@pytest.mark.parametrize(
    ("key", "field", "value"),
    [
        ("caps", "caps_percent", 55),
        ("capsmin", "caps_min", 20),
        ("symbols", "symbols_percent", 40),
        ("symbolsmin", "symbols_min", 15),
        ("permit", "permit_seconds", 120),
        ("warn", "warn_seconds", 0),
    ],
)
def test_set_each_threshold(host: _Host, key: str, field: str, value: int) -> None:
    assert mod(host, f"!chatguard set {key} {value}") == f"chatguard {key} set to {value}"
    assert getattr(stored_config(host), field) == value


def test_set_via_the_sub_module_form(host: _Host) -> None:
    assert mod(host, "!chatguard caps set 45") == "chatguard caps set to 45"
    assert stored_config(host).caps_percent == 45


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("caps 0", "between 1 and 100"),
        ("caps 101", "between 1 and 100"),
        ("permit 601", "between 1 and 600"),
        ("warn 3601", "between 0 and 3600"),
        ("capsmin 0", "between 1 and 200"),
        ("caps abc", "isn't a whole number"),
        ("caps -5", "isn't a whole number"),
        ("caps 1234567", "isn't a whole number"),
        ("caps", app._USAGE),
        ("caps 1 2", app._USAGE),
        ("color 5", app._USAGE),
    ],
)
def test_set_validation(host: _Host, arg: str, expected: str) -> None:
    assert expected in mod(host, f"!chatguard set {arg}")
    assert community_kv._scoped_key(COMMUNITY, app._CONFIG_KEY) not in host.kv.store


def test_a_hostile_set_value_is_not_echoed_live(host: _Host) -> None:
    out = mod(host, "!chatguard set caps {user:evil}")
    assert "{user:evil}" not in out


def test_new_thresholds_apply_to_the_next_message(host: _Host) -> None:
    mod(host, "!chatguard enable caps")
    assert say(host, "Hello Everyone How Are You") is None
    mod(host, "!chatguard set caps 10")
    out = say(host, "Hello Everyone How Are You")
    assert out is not None and out.payload["rule"] == "caps"


@pytest.mark.parametrize(
    "form", ["!chatguard add blacklist Bad Phrase", "!chatguard blacklist add Bad Phrase"]
)
def test_add_blacklist_both_grammar_forms(host: _Host, form: str) -> None:
    out = mod(host, form)
    assert out == "added -- blacklist now has 1 words"
    assert stored_config(host).words == ("bad phrase",)
    assert "Bad" not in out and "phrase" not in out  # never echoed


def test_blacklist_remove_duplicates_and_limits(host: _Host) -> None:
    mod(host, "!chatguard add blacklist foo")
    assert "already on the blacklist" in mod(host, "!chatguard add blacklist FOO")
    assert mod(host, "!chatguard remove blacklist foo") == "removed -- blacklist now has 0 words"
    assert "not on the blacklist" in mod(host, "!chatguard remove blacklist foo")
    assert "not on the blacklist" in mod(host, "!chatguard delete blacklist foo")
    for i in range(app.MAX_WORDS):
        assert "added" in mod(host, f"!chatguard add blacklist word{i}")
    assert "blacklist is full" in mod(host, "!chatguard add blacklist onetoomany")
    assert len(stored_config(host).words) == app.MAX_WORDS


@pytest.mark.parametrize(
    "text",
    [
        "!chatguard add blacklist",
        "!chatguard add blacklist x",
        "!chatguard add blacklist " + "y" * 41,
        "!chatguard add blacklist !!!",
        "!chatguard add blacklist {user:tok-x}",
    ],
)
def test_unusable_blacklist_entries_are_refused(host: _Host, text: str) -> None:
    assert "blacklist entries must be" in mod(host, text)
    assert community_kv._scoped_key(COMMUNITY, app._CONFIG_KEY) not in host.kv.store


def test_add_without_the_blacklist_keyword_is_usage(host: _Host) -> None:
    assert mod(host, "!chatguard add links foo") == app._USAGE
    assert mod(host, "!chatguard caps add foo") == app._USAGE


def test_added_words_take_effect(host: _Host) -> None:
    mod(host, "!chatguard enable blacklist")
    mod(host, "!chatguard add blacklist spoiler alert")
    out = say(host, "this contains a SPOILER   alert sorry")
    assert out is not None and out.payload["rule"] == "blacklist"
    assert say(host, "spoilers are fine") is None


def test_list_never_reveals_the_blacklist_words(host: _Host) -> None:
    configure(host, enabled=frozenset({"caps", "links"}), words=("secretword", "other one"))
    out = mod(host, "!chatguard list")
    assert "rules on: links, caps" in out and "blacklist 2 words" in out
    assert "secretword" not in out and "other one" not in out
    assert "mode: delete+warn" in out


def test_list_states_the_warn_only_mode_off_discord(host: _Host) -> None:
    out = mod(host, "!chatguard list", platform="twitch")
    assert "rules on: none" in out and "warn-only" in out


def test_reset_restores_the_defaults(host: _Host) -> None:
    configure(host, caps_percent=10, words=("badword",))
    assert "reset to defaults" in mod(host, "!chatguard reset")
    assert stored_config(host) == GuardConfig()


def test_a_command_reply_needs_a_channel(host: _Host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        say(host, "!chatguard", drop=("channel_id",))


def test_a_kv_failure_in_a_command_is_a_reply_not_a_crash(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(key: str, value: bytes, ttl: int) -> None:
        raise _WitErr(Error_Denied())

    monkeypatch.setattr(__import__("wit_world").imports.kv, "set", boom)
    assert mod(host, "!chatguard enable caps") == app._UNAVAILABLE_MSG
    assert "chatguard.kv_error" in host.logged()


# -- permit --------------------------------------------------------------------


def _permit(host: _Host, text: str, **kw: Any) -> str:
    host.ident.add_mention("tok-target", TARGET)
    return mod(host, text, **kw)


def test_permit_grants_a_time_boxed_permit(host: _Host) -> None:
    out = _permit(host, "!permit {user:tok-target} 90")
    assert out == "{user:tok-target} may post links for 90s."  # the mention stays live
    assert host.kv.store[permit_key(TARGET)] == str(host.now_ms + 90_000).encode()
    sets = [c for c in host.kv.calls if c[0] == "set" and ".permit." in c[1][0]]
    assert sets[-1][1][2] == 90  # the kv TTL matches the permit


def test_permit_defaults_to_the_configured_seconds(host: _Host) -> None:
    configure(host, enabled=frozenset({"links"}), permit_seconds=45)
    assert "45s" in _permit(host, "!permit {user:tok-target}")


def test_the_chatguard_alias_form_works(host: _Host) -> None:
    out = _permit(host, "!chatguard permit {user:tok-target} 30")
    assert "30s" in out and permit_key(TARGET) in host.kv.store


@pytest.mark.parametrize(
    "text",
    [
        "!permit",
        "!permit bob",
        "!permit {user:tok-target} {user:tok-target}",
        "!permit {user:tok-target} abc",
        "!permit {user:tok-target} 10 20",
        "!permit {user:tok-target} -5",
        "!permit {user:tok-target} 1234567",
    ],
)
def test_permit_grammar_errors(host: _Host, text: str) -> None:
    assert _permit(host, text) == app._PERMIT_USAGE
    assert permit_key(TARGET) not in host.kv.store


@pytest.mark.parametrize("seconds", [0, 601])
def test_permit_seconds_are_bounded(host: _Host, seconds: int) -> None:
    assert "between 1 and 600" in _permit(host, f"!permit {{user:tok-target}} {seconds}")
    assert permit_key(TARGET) not in host.kv.store


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda h: h.ident.add_unlinked_mention("tok-target"), "hasn't linked"),
        (lambda h: h.ident.add_ambiguous_mention("tok-target"), "more than one person"),
        (lambda h: h.ident.members.discard(TARGET), "isn't a member"),
        (lambda h: setattr(h.ident, "granted", False), "temporarily unavailable"),
    ],
)
def test_permit_resolution_failures_are_replies_and_grant_nothing(
    host: _Host, setup: Any, expected: str
) -> None:
    host.ident.add_mention("tok-target", TARGET)
    setup(host)
    assert expected in mod(host, "!permit {user:tok-target} 30")
    assert not [k for k in host.kv.store if "permit" in k]


def test_a_token_the_message_did_not_carry_is_not_found(host: _Host) -> None:
    out = mod(host, "!permit {user:tok-unknown} 30")
    assert "couldn't find that user" in out


def test_permit_end_to_end_then_expiry(host: _Host) -> None:
    mod(host, "!chatguard enable links")
    assert say(host, "visit evil.com") is not None  # blocked
    _permit(host, "!permit {user:tok-target} 60")
    host.ident.actor = TARGET
    assert say(host, "visit evil.com", actor="{user:tok-t}") is None  # permitted
    host.now_ms += 61_000
    assert say(host, "visit evil.com", actor="{user:tok-t}") is not None  # expired
    host.ident.actor = ACTOR
    assert say(host, "visit evil.com") is not None  # never permitted


# -- dispatch: relay a reply ---------------------------------------------------


def _reply_event(text: Any = "hi", **extra: Any) -> PlatformEvent:
    return PlatformEvent(
        platform="discord",
        event_type="chat.message",
        actor=ACTOR_TOKEN,
        payload={"command": "reply", "text": text, "channel_id": CHANNEL, **extra},
        occurred_at="x",
    )


def test_dispatch_relays_a_reply(host: _Host) -> None:
    result = send(host, _reply_event("hello"))
    assert result.detail == "relayed" and result.transport == "discord"
    assert host.relay_calls == [("discord", {"channel": CHANNEL, "text": "hello"})]


def test_dispatch_guards(host: _Host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        send(host, _reply_event(channel_id=None))
    for text in ("", None, 5):
        with pytest.raises(ValueError, match="non-empty text"):
            send(host, _reply_event(text))
    bad = _reply_event()
    bad.payload["command"] = "nope"
    with pytest.raises(ValueError, match="unrecognized chatguard command"):
        send(host, bad)
    assert host.relay_calls == []


# -- dispatch: enforcement -----------------------------------------------------


def test_discord_violation_deletes_then_warns(host: _Host) -> None:
    result = send(host, violation("links"))
    assert result.detail == "delete"
    assert [m.get("op", "chat.send") for _, m in host.relay_calls] == ["chat.delete", "chat.send"]
    assert host.pushes("chat.delete") == [
        {
            "v": 1,
            "platform": "discord",
            "op": "chat.delete",
            "channel": CHANNEL,
            "message_id": SNOWFLAKE,
        }
    ]
    warning = host.pushes("chat.send")[0]
    assert warning["channel"] == CHANNEL
    assert warning["text"] == f"{ACTOR_TOKEN}, your message was removed: {app._REASONS['links']}."


@pytest.mark.parametrize("rule", app.RULES)
def test_every_rule_has_a_warning(host: _Host, rule: str) -> None:
    send(host, violation(rule))
    assert app._REASONS[rule] in host.pushes("chat.send")[0]["text"]


def test_twitch_can_only_warn_and_never_asks_for_a_delete(host: _Host) -> None:
    result = send(host, violation("caps", platform="twitch"))
    assert result.detail == "warn_only"
    assert host.pushes("chat.delete") == []
    warning = host.pushes("chat.send")[0]["text"]
    assert warning == f"{ACTOR_TOKEN}, heads up: {app._REASONS['caps']}."  # no claim of removal
    assert "chatguard.enforce_unsupported" in host.logged()


def test_a_failed_delete_still_warns_without_claiming_removal_then_raises(host: _Host) -> None:
    host.relay_failure = _WitErr(Error_Denied())
    host.relay_failure_ops = {"chat.delete"}
    with pytest.raises(RuntimeError, match="could not delete"):
        send(host, violation("caps"))
    warning = host.pushes("chat.send")[0]["text"]
    assert "heads up" in warning and "removed" not in warning
    assert "chatguard.delete_failed" in host.logged()
    assert "Error_Denied" in host.logged()  # the error CLASS only


@pytest.mark.parametrize("bad", [None, "", "abc", "12 34", "1" * 21, 5, "../x"])
def test_a_non_snowflake_message_id_is_never_sent_to_the_host(host: _Host, bad: Any) -> None:
    with pytest.raises(RuntimeError, match="could not delete"):
        send(host, violation("caps", message_id=bad))
    assert host.pushes("chat.delete") == []
    assert "chatguard.no_message_id" in host.logged()
    assert "heads up" in host.pushes("chat.send")[0]["text"]


def test_warnings_are_limited_to_one_per_cooldown_per_user(host: _Host) -> None:
    send(host, violation("caps"))
    send(host, violation("caps", message_id="123456789012345679"))
    assert len(host.pushes("chat.delete")) == 2  # every offending message is still deleted
    assert len(host.pushes("chat.send")) == 1
    incs = [c for c in host.kv.calls if c[0] == "increment" and ".warn." in c[1][0]]
    assert incs and all(c[1][2] == 30 for c in incs)


def test_a_different_user_is_warned_independently(host: _Host) -> None:
    send(host, violation("caps"))
    send(host, violation("caps", ps=pseudonym(TARGET)))
    assert len(host.pushes("chat.send")) == 2


def test_the_warning_returns_once_the_cooldown_lapses(host: _Host) -> None:
    send(host, violation("caps"))
    host.expire(".warn.")
    send(host, violation("caps"))
    assert len(host.pushes("chat.send")) == 2


def test_a_zero_cooldown_warns_every_time_without_touching_kv(host: _Host) -> None:
    send(host, violation("caps", warn_s=0))
    send(host, violation("caps", warn_s=0))
    assert len(host.pushes("chat.send")) == 2
    assert not [c for c in host.kv.calls if c[0] == "increment"]


@pytest.mark.parametrize("warn_s", [None, "30", -1, 99999, True])
def test_an_invalid_cooldown_in_the_payload_falls_back_to_warning(host: _Host, warn_s: Any) -> None:
    result = send(host, violation("caps", warn_s=warn_s))
    assert result.detail == "delete"
    assert len(host.pushes("chat.send")) == 1


@pytest.mark.parametrize("ps", [None, "short", "g" * 64, 5])
def test_an_invalid_pseudonym_uses_the_anonymous_bucket(host: _Host, ps: Any) -> None:
    send(host, violation("caps", ps=ps))
    assert warn_key(app._ANON_PSEUDONYM) in host.kv.store


def test_a_redelivered_violation_retries_the_delete_but_not_the_warning(host: _Host) -> None:
    event = violation("caps")
    send(host, event)
    send(host, event)
    assert len(host.pushes("chat.delete")) == 2
    assert len(host.pushes("chat.send")) == 1


def test_unknown_rules_and_missing_channels_are_loud(host: _Host) -> None:
    for rule in ("nope", None, ["caps"], 5):
        event = violation("caps")
        event.payload["rule"] = rule
        with pytest.raises(ValueError, match="unrecognized chatguard rule"):
            send(host, event)
    with pytest.raises(ValueError, match="channel_id"):
        send(host, violation("caps", channel_id=None))
    assert host.relay_calls == []


def test_tenant_wide_enforcement_uses_the_sentinel_scope(host: _Host) -> None:
    send(host, violation("caps"), community=None)
    assert community_kv._scoped_key(None, app._warn_key(pseudonym())) in host.kv.store


def test_a_raw_actor_is_sanitized_in_the_warning(host: _Host) -> None:
    event = violation("caps")
    event.actor = "@everyone"
    send(host, event)
    assert "@everyone" not in host.pushes("chat.send")[0]["text"]
    event2 = violation("caps", ps=pseudonym(TARGET))
    event2.actor = "x {user:forged}"
    send(host, event2)
    assert "{user:forged}" not in host.pushes("chat.send")[1]["text"]


# -- end to end + privacy ------------------------------------------------------


def test_end_to_end_discord_flow(host: _Host) -> None:
    mod(host, "!chatguard enable links")
    out = say(host, "go to evil.com now", message_id=SNOWFLAKE)
    assert out is not None
    host.relay_calls.clear()
    send(host, out)
    assert [m.get("op", "chat.send") for _, m in host.relay_calls] == ["chat.delete", "chat.send"]
    assert host.relay_calls[0][1]["message_id"] == SNOWFLAKE


def test_end_to_end_twitch_flow_only_warns(host: _Host) -> None:
    mod(host, "!chatguard enable caps", platform="twitch")
    out = say(host, "HELLO EVERYONE HOW ARE YOU", platform="twitch", message_id="abc-uuid")
    assert out is not None
    send(host, out)
    assert [m.get("op", "chat.send") for _, m in host.relay_calls] == ["chat.send"]


def test_the_offending_message_never_reaches_logs_keys_or_relays(host: _Host) -> None:
    secret = "ZXQSECRETPHRASE"  # noqa: S105 -- a canary string, not a credential
    configure(host, words=(secret.lower(),))
    lines = [
        f"{secret} is a badword",
        f"HELLO {secret} EVERYONE HOW ARE YOU {ACTOR_TOKEN}",
        f"visit evil.com/{secret}",
        "!!!!!!!!!!!!!!!!!!!! " + secret,
    ]
    for line in lines:
        out = say(host, line, message_id=SNOWFLAKE)
        assert out is not None
        send(host, out)
    mod(host, f"!chatguard add blacklist {secret}more")
    private = host.logged() + json.dumps(list(host.kv.store.items()), default=repr)
    for needle in (secret, "evil.com", ACTOR, ACTOR_TOKEN, "tok-author", SNOWFLAKE, CHANNEL):
        assert needle not in private, needle
    relayed = json.dumps(host.relay_calls)
    for needle in (secret, "evil.com", ACTOR):  # the warning names the author by token only
        assert needle not in relayed, needle
    for key in host.kv.store:
        assert ":" not in key and ACTOR not in key


def test_logs_only_carry_the_documented_fields(host: _Host) -> None:
    configure(host)
    out = say(host, "HELLO EVERYONE HOW ARE YOU")
    assert out is not None
    send(host, out)
    allowed = {
        "rule",
        "action",
        "warned",
        "platform",
        "community_id",
        "op",
        "error",
        "exc",
        "command",
    }
    for _, message, fields in host.log_calls:
        assert message.startswith("chatguard"), message
        assert set(json.loads(fields)) <= allowed, (message, fields)


# -- security review regressions: the filter must see what the platform renders -----------------

HANGUL_FILLER = chr(0x3164)
CYRILLIC_I = chr(0x456)


@pytest.mark.parametrize(
    "text",
    [
        r"\{user:evil.com\}",
        r"look \{user: free nitro evil.com \} ok",
        r"\\\{user:evil.com\}",  # a typed backslash + a typed placeholder
        r"\{user:https://evil.com/x\}",
        r"a \{user:tok-1\} b evil.com",
    ],
)
def test_a_typed_placeholder_cannot_hide_a_link(text: str) -> None:
    # regression: the view un-escaped braces BEFORE stripping placeholders, so a literal
    # `{user:...}` the author TYPED (the host escapes it to `\{user:...\}`) was blanked out.
    assert app.has_link(text), text


@pytest.mark.parametrize("text", [r"\{user:badword\}", r"x \{user: bad word badword \} y"])
def test_a_typed_placeholder_cannot_hide_a_blacklisted_word(text: str) -> None:
    assert app.blacklist_hit(text, ("badword",)), text


def test_a_host_minted_placeholder_is_blanked_but_the_words_around_it_are_not() -> None:
    assert not app.has_link("{user:abc.com} hi")
    assert app.has_link("{user:tok-1} evil.com")
    assert app.blacklist_hit("{user:tok-1} badword", ("badword",))
    assert not app.blacklist_hit("{user:badword} hello", ("badword",))
    assert app.blacklist_hit(r"\\{user:tok-1} badword", ("badword",))  # escaped backslash + mention


def test_a_typed_placeholder_still_counts_for_caps_and_symbols() -> None:
    assert app.caps_violation(r"\{user:" + "A" * 40 + r"\}", GuardConfig())
    assert app.symbols_violation(r"\{user:" + "!" * 40 + r"\}", GuardConfig())
    assert not app.caps_violation("{user:" + "A" * 40 + "} hello there", GuardConfig())


@pytest.mark.parametrize("seed", range(6))
def test_the_view_agrees_with_the_egress_detokenizer_grammar(seed: int) -> None:
    """A fuzzed differential against the SDK's port of `egress_detokenizer::detokenize`."""
    from waddle_sdk import safe_text

    rng = random.Random(seed)  # noqa: S311 -- deterministic fuzz, not a secret
    atoms = ["{", "}", "\\", "{user:", "user:", "tok-1", "a.b", "x", " ", "}}", "\\\\", ":", "ZZ"]
    for _ in range(400):
        text = "".join(rng.choice(atoms) for _ in range(rng.randint(1, 14)))
        expected = []
        for piece, live in safe_text._split_live(text, None):
            if live:
                expected.append(" ")
                continue
            out, i = [], 0
            while i < len(piece):
                if piece[i] == "\\" and i + 1 < len(piece) and piece[i + 1] in "{}\\":
                    out.append(piece[i + 1])
                    i += 2
                else:
                    out.append(piece[i])
                    i += 1
            expected.append("".join(out))
        assert app._plain_view(text) == "".join(expected), text


@pytest.mark.parametrize(
    "text",
    [
        "evil" + HANGUL_FILLER + ".com",
        "ev" + HANGUL_FILLER * 3 + "il.com",
        "eV" + CYRILLIC_I + "l.com",
        "".join(map(chr, (0x43F, 0x440, 0x438, 0x43C, 0x435, 0x440))) + ".com",  # a Cyrillic label
        r"evil\.com",
    ],
)
def test_unicode_and_markdown_tricks_do_not_hide_a_link(text: str) -> None:
    assert app.has_link(text), text


@pytest.mark.parametrize(
    "text",
    [
        "".join(map(chr, (0x43F, 0x440, 0x438, 0x432, 0x435, 0x442))) + ", how are you",
        "version 2.0",
        "caf" + chr(0xE9) + ".Thanks",
    ],
)
def test_the_wider_domain_labels_do_not_flag_ordinary_text(text: str) -> None:
    assert not app.has_link(text), text


def test_blank_filler_characters_cannot_split_a_blacklisted_word() -> None:
    assert app.blacklist_hit("bad" + HANGUL_FILLER + "word", ("badword",))
    assert app.blacklist_hit("b" + chr(0x2800) + "adword", ("badword",))
    assert app.blacklist_hit("bad" + chr(0xFFA0) + chr(0x115F) + "word", ("badword",))


def test_blank_filler_characters_cannot_dilute_the_caps_or_symbol_share() -> None:
    cfg = GuardConfig()
    assert app.caps_violation("SHOUTING LOUDLY" + HANGUL_FILLER * 80, cfg)
    assert app.symbols_violation("!" * 12 + HANGUL_FILLER * 80, cfg)
    assert app.symbols_violation("!" * 12 + chr(0x2800) * 80, cfg)
    letters, total = app.caps_stats("AB" + HANGUL_FILLER * 5)
    assert (letters, total) == (2, 2)


def test_a_long_fake_url_does_not_hide_caps_or_symbols() -> None:
    cfg = GuardConfig()
    # regression: `https://` + `\S+` was exempt from the statistics without any bound.
    assert app.caps_violation("https://" + "A" * 1900, cfg)
    assert app.symbols_violation("https://" + "!" * 1900, cfg)
    assert not app.caps_violation("https://EXAMPLE.COM/PATH?ID=ABC thanks for the invite", cfg)


def test_a_link_past_the_old_2000_character_cut_off_is_still_flagged(host: _Host) -> None:
    # regression: only `text[:2000]` was analysed, so padding pushed the link out of view
    # (Discord Nitro users can send 4000 characters).
    configure(host, enabled=frozenset({"links"}))
    out = say(host, "x" * 2500 + " visit evil.com")
    assert out is not None and out.payload["rule"] == "links"


@pytest.mark.parametrize(
    ("rule", "tail"),
    [("blacklist", " badword"), ("caps", " " + "SHOUTING" * 70), ("symbols", " " + "!" * 300)],
)
def test_content_past_the_old_cut_off_is_still_analysed(host: _Host, rule: str, tail: str) -> None:
    configure(
        host, enabled=frozenset({rule}), words=("badword",), caps_percent=15, symbols_percent=5
    )
    filler = "lowercase words keep the share down " * 60  # 2160 characters: past the old cut-off
    assert len(filler) > 2000
    out = say(host, filler + tail)
    assert out is not None and out.payload["rule"] == rule


def test_a_message_over_the_ceiling_is_a_violation_of_the_first_enabled_rule(host: _Host) -> None:
    configure(host, enabled=frozenset({"links", "caps"}))
    huge = "ok " * (app.MAX_ANALYSIS_CHARS // 3 + 1)
    assert len(huge) > app.MAX_ANALYSIS_CHARS
    out = say(host, huge)
    assert out is not None and out.payload["rule"] == "links"
    assert "ok ok" not in json.dumps(out.payload)


def test_the_ceiling_is_exact_and_clean_text_at_it_still_passes(host: _Host) -> None:
    configure(host, enabled=frozenset({"links", "caps"}))
    at_limit = ("ok " * app.MAX_ANALYSIS_CHARS)[: app.MAX_ANALYSIS_CHARS - 1] + "k"
    assert len(at_limit) == app.MAX_ANALYSIS_CHARS
    assert say(host, at_limit) is None
    over = at_limit + "k"
    out = say(host, over)
    assert out is not None


def test_an_over_long_message_from_a_permitted_author_skips_the_links_rule(host: _Host) -> None:
    configure(host, enabled=frozenset({"links", "caps"}))
    host.kv.store[permit_key(ACTOR)] = str(host.now_ms + 60_000).encode()
    out = say(host, "ok " * app.MAX_ANALYSIS_CHARS)
    assert out is not None and out.payload["rule"] == "caps"


def test_an_over_long_message_is_not_a_blacklist_violation_when_no_word_is_listed(
    host: _Host,
) -> None:
    configure(host, enabled=frozenset({"blacklist"}))
    assert say(host, "ok " * app.MAX_ANALYSIS_CHARS) is None


@pytest.mark.parametrize(
    "text",
    [
        "!permit visit evil.com",
        "!chatguard visit evil.com",
        "!chatguard enable caps and then evil.com",
        "!PERMIT evil.com",
        "!permit " + "x" * 10 + " badword",
    ],
)
def test_prefixing_a_message_with_a_command_does_not_skip_the_filters(
    host: _Host, text: str
) -> None:
    # regression: a non-moderator's `!permit ...` line became a "denied" reply and was never
    # screened, so any message could dodge every rule by starting with the command word.
    configure(host, enabled=frozenset({"links", "blacklist"}), words=("badword",))
    out = say(host, text)
    assert out is not None and out.payload["command"] == "violation", text
    assert out.payload["rule"] in {"links", "blacklist"}
    assert "evil" not in json.dumps(out.payload)


def test_a_clean_command_line_from_a_non_moderator_is_still_denied_not_flagged(
    host: _Host,
) -> None:
    configure(host)
    assert reply(host, "!permit {user:tok-target}") == app._DENIED_MSG
    assert reply(host, "!chatguard") == app._USAGE


def test_a_moderators_command_lines_are_never_screened(host: _Host) -> None:
    configure(host)
    assert mod(host, "!chatguard add blacklist evil.com") == "added -- blacklist now has 1 words"
    assert mod(host, "!chatguard list").startswith("chatguard rules on:")


def test_a_command_line_without_a_channel_still_fails_loud(host: _Host) -> None:
    configure(host)
    with pytest.raises(ValueError, match="channel_id"):
        say(host, "!permit evil.com", drop=("channel_id",))


def test_a_trailing_newline_cannot_pass_the_anchored_validators() -> None:
    # regression: `re.match` with a `$` anchor accepts "123\n"; every validator is `fullmatch` now.
    assert app._SNOWFLAKE_RE.fullmatch("123\n") is None
    assert app._mention("tok-1\n") == "that user"
    assert app._actor_label("{user:tok-1}") == "{user:tok-1}"  # exactly one placeholder: live
    for padded in ("{user:tok-1}\n", " {user:tok-1}", "x {user:tok-1}", "{user:tok-1}{user:b}"):
        label = app._actor_label(padded)
        assert "\\{user:tok-1" in label, padded  # escaped (inert), never a live placeholder
    assert app._PSEUDONYM_RE.fullmatch("a" * 64 + "\n") is None


@pytest.mark.parametrize("bad", ["123456789012345678\n", "123456789012345678 "])
def test_a_message_id_with_a_trailing_newline_is_never_sent_to_the_host(
    host: _Host, bad: str
) -> None:
    with pytest.raises(RuntimeError, match="could not delete"):
        send(host, violation("caps", message_id=bad))
    assert host.pushes("chat.delete") == []


_AT_CEILING = {
    "dotted": "a." * (app.MAX_ANALYSIS_CHARS // 2),
    "max-labels": (("a" * 62 + ".") * 70)[: app.MAX_ANALYSIS_CHARS],
    "obfuscation": ("[.](.){.}" * 500)[: app.MAX_ANALYSIS_CHARS],
    "placeholder-prefix": ("{user:" * 700)[: app.MAX_ANALYSIS_CHARS],
    "backslashes": "\\" * app.MAX_ANALYSIS_CHARS,
    "nfkd-expansion": chr(0xFDFA) * app.MAX_ANALYSIS_CHARS,
    "fake-url": "https://" + "A" * (app.MAX_ANALYSIS_CHARS - 8),
    "fillers": HANGUL_FILLER * app.MAX_ANALYSIS_CHARS,
}


@pytest.mark.parametrize("text", list(_AT_CEILING.values()), ids=list(_AT_CEILING))
def test_adversarial_input_at_the_ceiling_cannot_stall_the_filters(text: str) -> None:
    """The longest line the filters analyse in full still finishes well inside the budget."""
    assert len(text) <= app.MAX_ANALYSIS_CHARS
    cfg = GuardConfig(words=tuple(f"word{i}" for i in range(app.MAX_WORDS)))
    app._plain_view.cache_clear()
    started = time.perf_counter()
    app.has_link(text)
    app.caps_violation(text, cfg)
    app.symbols_violation(text, cfg)
    app.blacklist_hit(text, cfg.words)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize("actor", [None, "", "   ", 5])
def test_a_missing_actor_is_labelled_neutrally(actor: Any) -> None:
    assert app._actor_label(actor) == "someone"


def test_a_corrupt_kv_integer_is_logged_and_treated_as_absent(host: _Host) -> None:
    assert app._int_or_none(b"12") == 12
    assert app._int_or_none(None) is None
    assert app._int_or_none(b"\xff") is None
    assert app._int_or_none(b"twelve") is None
    assert "chatguard.kv_value_corrupt" in host.logged()
