"""Host-native tests for the `hug` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- same pattern as `bundles/python/eightball/tests/
test_app.py`: a fake `wit_world` module stands in for the WIT host imports
(`flags`, `log`, `relay`), isolating "does this bundle build the right
calls" from "does the WIT import actually work" (already covered by
`waddle_sdk`'s own tests).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from app import _SOLO_HUGS, _TARGETED_HUGS, _USAGE, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def _run(coro):
    return asyncio.run(coro)


def _sample_event(text: str, *, channel_id: str | None = "12345") -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-08T00:00:00.000Z",
    )


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch):
    """Fake WIT host: `flags.enabled` always True, `relay.push`/`log.write` recorded."""
    relay_calls: list[tuple[str, str]] = []
    log_calls: list[tuple[int, str, str]] = []

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: True)
    relay_mod = types.SimpleNamespace(push=lambda provider, msg: relay_calls.append((provider, msg)))
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields: log_calls.append((lvl, msg, fields)),
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, relay=relay_mod, log=log_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return types.SimpleNamespace(relay_calls=relay_calls, log_calls=log_calls)


def test_bare_hug_posts_a_solo_flavor_text(fake_host) -> None:
    result = _run(transform(_sample_event("!hug")))
    assert result is not None
    assert result.payload["text"] in _SOLO_HUGS


def test_hug_with_valid_target_posts_a_targeted_flavor_text(fake_host) -> None:
    result = _run(transform(_sample_event("!hug someviewer")))
    assert result is not None
    assert any(result.payload["text"] == t.format(target="someviewer") for t in _TARGETED_HUGS)


def test_hug_with_at_prefixed_target_strips_the_at(fake_host) -> None:
    result = _run(transform(_sample_event("!hug @someviewer")))
    assert result is not None
    assert "someviewer" in result.payload["text"]
    assert "@" not in result.payload["text"]


def test_hug_with_invalid_target_shape_replies_usage(fake_host) -> None:
    result = _run(transform(_sample_event("!hug !!!bad")))
    assert result is not None
    assert result.payload["text"] == _USAGE


def test_hug_with_bare_at_sign_replies_usage(fake_host) -> None:
    """An `@` alone normalizes to an empty candidate -- must reject, not crash."""
    result = _run(transform(_sample_event("!hug @")))
    assert result is not None
    assert result.payload["text"] == _USAGE


def test_hug_with_multiple_tokens_replies_usage(fake_host) -> None:
    result = _run(transform(_sample_event("!hug two words here")))
    assert result is not None
    assert result.payload["text"] == _USAGE


@pytest.mark.parametrize("text", ["!hugping", "hug", "!hugs", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_chat_payload_is_ignored_rather_than_erroring() -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """`waddles.command-hug` OFF -> a matching command still produces no reply."""
    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: False)
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(flags=flags_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert _run(transform(_sample_event("!hug"))) is None


def _sample_envelope(platform: str, text: str, *, channel_id: str | None = "12345") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.hug",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload={"text": text, "channel_id": channel_id},
            occurred_at="2026-10-08T00:00:00.000Z",
        ),
        ts="2026-10-08T00:00:00.000Z",
    )


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_relays_to_the_events_own_origin_platform(platform: str, fake_host) -> None:
    envelope = _sample_envelope(platform, "Hug reply.")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.transport == platform
    provider, message_json = fake_host.relay_calls[0]
    assert provider == platform
    assert json.loads(message_json) == {
        "channel": "12345",
        "text": "Hug reply.",
    }


def test_dispatch_raises_when_channel_id_is_missing(fake_host) -> None:
    envelope = _sample_envelope("twitch", "some hug", channel_id=None)

    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.relay_calls == []


def test_transform_and_dispatch_never_log_the_raw_target_or_actor(fake_host) -> None:
    _run(transform(_sample_event("!hug secretviewer")))
    envelope = _sample_envelope("twitch", "some hug")
    _run(dispatch(envelope, {}, http_client=None))

    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "secretviewer" not in message
        assert "secretviewer" not in fields_json
        fields = json.loads(fields_json)
        assert "actor" not in fields
        assert "target" not in fields


# ---------------------------------------------------------------------------
# PII-free logs -- regression: gh-674 (bundle-logs-must-be-pii-free)
# ---------------------------------------------------------------------------

_SENTINEL = "SENTINELpii9f3a"

#: The only field names any log line in this bundle may carry.
_ALLOWED_LOG_FIELDS = frozenset({"platform", "shape"})


def _event_from(actor: str | None, text: str) -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=actor,
        payload={"text": text, "channel_id": "12345"},
        occurred_at="2026-10-08T00:00:00.000Z",
    )


@pytest.mark.parametrize(
    ("text", "shape"),
    [
        ("!hug", "solo"),
        (f"!hug {_SENTINEL}", "targeted"),
        (f"!hug @{_SENTINEL}", "targeted"),
        (f"!hug !!!{_SENTINEL}", "usage"),
        (f"!hug {_SENTINEL} extra words", "usage"),
        ("!hug @", "usage"),
        (f"!hug {_SENTINEL}{{0}}", "usage"),
    ],
)
def test_transform_log_carries_only_platform_and_shape(text: str, shape: str, fake_host) -> None:
    # regression: gh-674 -- every branch (solo/targeted/usage) is exercised with a sentinel
    # actor AND a sentinel in the typed text; the denominator is asserted so zero lines != pass.
    assert _run(transform(_event_from(_SENTINEL, text))) is not None
    assert len(fake_host.log_calls) == 1
    _level, message, fields_json = fake_host.log_calls[0]
    assert _SENTINEL not in message
    assert _SENTINEL not in fields_json
    assert json.loads(fields_json) == {"platform": "twitch", "shape": shape}


def test_dispatch_log_carries_only_the_platform(fake_host) -> None:
    # regression: gh-674
    envelope = _sample_envelope("twitch", f"reply for {_SENTINEL}")
    envelope.event.actor = _SENTINEL
    _run(dispatch(envelope, {}, http_client=None))
    assert len(fake_host.log_calls) == 1
    _level, message, fields_json = fake_host.log_calls[0]
    assert _SENTINEL not in message
    assert set(json.loads(fields_json)) <= _ALLOWED_LOG_FIELDS


def test_reply_text_never_names_the_actor(fake_host) -> None:
    for text in ("!hug", "!hug buddy"):
        result = _run(transform(_event_from(_SENTINEL, text)))
        assert result is not None
        assert _SENTINEL not in result.payload["text"]


# ---------------------------------------------------------------------------
# transform(): command matching, target shape, flag gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["!HUG", "!Hug", "  !hug  ", "\t!hug\n"])
def test_command_head_is_case_insensitive_and_whitespace_tolerant(text: str, fake_host) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["text"] in _SOLO_HUGS


@pytest.mark.parametrize(
    "target",
    ["a", "A1", "_under", "with-hyphen", "with.dot", "x" * 32, "@buddy", "  buddy  "],
)
def test_valid_target_shapes_are_accepted(target: str, fake_host) -> None:
    result = _run(transform(_sample_event(f"!hug {target}")))
    assert result is not None
    assert result.payload["text"] != _USAGE
    assert target.strip().lstrip("@") in result.payload["text"]


@pytest.mark.parametrize(
    "target",
    [
        "x" * 33,
        "-lead",
        ".lead",
        "@@double",
        "has/slash",
        "emoji\U0001f600",
        "{target}",
        "{0}",
        "%s",
    ],
)
def test_invalid_target_shapes_get_usage_and_never_reach_format(target: str, fake_host) -> None:
    """Braces/percent in a target would be a format-string injection vector -- shape-rejected."""
    result = _run(transform(_sample_event(f"!hug {target}")))
    assert result is not None
    assert result.payload["text"] == _USAGE


def test_every_flavor_bank_entry_is_well_formed() -> None:
    assert _SOLO_HUGS
    assert all("{" not in text and "}" not in text for text in _SOLO_HUGS)
    assert _TARGETED_HUGS
    for template in _TARGETED_HUGS:
        assert template.count("{target}") == 1
        assert template.format(target="x") == template.replace("{target}", "x")


def test_every_flavor_bank_entry_is_reachable(monkeypatch: pytest.MonkeyPatch, fake_host) -> None:
    for solo in _SOLO_HUGS:
        monkeypatch.setattr("app.random.choice", lambda seq, solo=solo: solo)
        result = _run(transform(_sample_event("!hug")))
        assert result is not None
        assert result.payload["text"] == solo
    for template in _TARGETED_HUGS:
        monkeypatch.setattr("app.random.choice", lambda seq, template=template: template)
        result = _run(transform(_sample_event("!hug buddy")))
        assert result is not None
        assert result.payload["text"] == template.format(target="buddy")


@pytest.mark.parametrize("text", [None, 42, ["!hug"], {"a": 1}])
def test_non_string_text_is_ignored_without_consulting_the_flag(text: object) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": "12345"},
        occurred_at="2026-10-08T00:00:00.000Z",
    )
    # No `wit_world` installed at all: any host-import access would raise ImportError/
    # AttributeError rather than quietly returning None.
    assert _run(transform(event)) is None


def _install_flags(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> list[tuple[str, bool]]:
    """Install a recording `flags` fake; return its `(key, default_value)` call list."""
    calls: list[tuple[str, bool]] = []

    def _enabled(key: str, default_value: bool) -> bool:
        calls.append((key, default_value))
        return enabled

    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=_enabled),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3}, write=lambda *a: None
        ),
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return calls


def test_flag_is_checked_by_key_and_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_flags(monkeypatch, enabled=False)
    assert _run(transform(_sample_event("!hug buddy"))) is None
    assert calls == [("waddles.command-hug", False)]


def test_unrelated_chat_never_consults_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_flags(monkeypatch, enabled=True)
    for text in ("hello", "!hugs", "!highfive buddy", ""):
        assert _run(transform(_sample_event(text))) is None
    assert calls == []


def test_flag_host_without_a_flags_import_degrades_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale component with no `flags` import fails closed (default OFF), never crashes."""
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    assert _run(transform(_sample_event("!hug"))) is None


def test_transform_forwards_only_text_and_channel_id(fake_host) -> None:
    event = PlatformEvent(
        platform="discord",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": "!hug buddy", "channel_id": "c-9", "email": "x@example.com"},
        occurred_at="2026-10-08T01:02:03.000Z",
    )
    result = _run(transform(event))
    assert result is not None
    assert set(result.payload) == {"text", "channel_id"}
    assert result.payload["channel_id"] == "c-9"
    assert (result.platform, result.event_type, result.actor) == (
        "discord",
        "chat.message",
        "viewer-1",
    )
    assert result.occurred_at == "2026-10-08T01:02:03.000Z"


# ---------------------------------------------------------------------------
# Stateless + fail-loud relay
# ---------------------------------------------------------------------------


def test_bundle_never_touches_kv_or_db(monkeypatch: pytest.MonkeyPatch, fake_host) -> None:
    """Stateless by contract: the manifest grants no `storage.kv`/`db`, so neither may be used."""

    class _Forbidden:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"stateless bundle touched host import {name!r}")

    wit_imports = sys.modules["wit_world"].imports
    wit_imports.kv = _Forbidden()
    wit_imports.db = _Forbidden()
    for text in ("!hug", "!hug buddy", "!hug !!!"):
        _run(transform(_sample_event(text)))
    _run(dispatch(_sample_envelope("twitch", "reply"), {}, http_client=None))


def test_dispatch_with_empty_channel_id_raises(fake_host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_sample_envelope("twitch", "reply", channel_id=""), {}, http_client=None))
    assert fake_host.relay_calls == []


def test_relay_failure_propagates_and_is_not_logged_as_relayed(fake_host) -> None:
    """A failed `relay.push` is never swallowed: the error surfaces, no success line is logged."""

    class _RelayErr(Exception):
        value = "backend"

    def _boom(provider: str, msg: str) -> None:
        raise _RelayErr("relay down")

    sys.modules["wit_world"].imports.relay = types.SimpleNamespace(push=_boom)
    with pytest.raises(_RelayErr):
        _run(dispatch(_sample_envelope("twitch", "reply"), {}, http_client=None))
    assert fake_host.log_calls == []


def test_dispatch_result_reports_provider_and_detail(fake_host) -> None:
    result = _run(dispatch(_sample_envelope("discord", "reply"), {}, http_client=None))
    assert (result.transport, result.detail) == ("discord", "relayed")
    assert result.sub_type is None
    assert result.http_status is None
