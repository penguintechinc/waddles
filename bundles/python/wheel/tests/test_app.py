"""Host-native tests for the `wheel` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/count/tests/test_app.py`'s own
docstring for the fake-`wit_world` approach this mirrors. Unlike `count`
(predates the shared fake), this suite uses `waddle_sdk.testing.
install_fake_kv_host` for the `kv` layer -- the charset-enforcing shared test
double (gh-631) -- layering bundle-local `flags`/`relay`/`log` fakes on top
of the same `wit_world.imports` namespace it installs.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest
from waddle_sdk.flask_core.bundle_runtime import BundleRuntimeError, bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import install_fake_kv_host

from app import (
    MAX_OPTION_LEN,
    MAX_OPTIONS,
    OPTIONS_KEY,
    dispatch,
    transform,
)

_APP_ID = "waddles.core.example.wheel"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch):
    """Fake WIT host: shared charset-validating `kv` double + local `flags`/`relay`/`log`."""
    fake_kv = install_fake_kv_host(monkeypatch)
    relay_calls: list[tuple[str, str]] = []
    log_calls: list[tuple[int, str, str]] = []
    flag_state = {"enabled": True}

    wit_world = sys.modules["wit_world"]
    wit_world.imports.flags = types.SimpleNamespace(  # type: ignore[attr-defined]
        enabled=lambda key, default_value: flag_state["enabled"]
    )
    wit_world.imports.relay = types.SimpleNamespace(  # type: ignore[attr-defined]
        push=lambda provider, msg: relay_calls.append((provider, msg))
    )
    wit_world.imports.log = types.SimpleNamespace(  # type: ignore[attr-defined]
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: log_calls.append((lvl, msg, fields_json)),
    )
    return types.SimpleNamespace(
        kv=fake_kv,
        relay_calls=relay_calls,
        log_calls=log_calls,
        flag_state=flag_state,
        wit_world=wit_world,
    )


def _event(
    text: str,
    *,
    platform: str = "twitch",
    is_mod: bool | None = False,
    is_broadcaster: bool | None = False,
    channel_id: str | None = "12345",
    actor: str | None = "viewer-1",
) -> PlatformEvent:
    payload: dict[str, object] = {"text": text, "channel_id": channel_id}
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


def _no_role_event(
    text: str, *, platform: str = "discord", channel_id: str = "guild-1"
):
    """A Discord-shaped event -- no `is_mod`/`is_broadcaster` keys at all (today's real gap)."""
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-05T00:00:00.000Z",
    )


def _ctx(community: str | None):
    return bundle_context(tenant="tenant-1", community=community, app_id=_APP_ID)


def _transform_in(community: str | None, text: str, **event_kwargs: object):
    """Run `transform()` for `text` inside a bound `bundle_context` -- see module docstring."""
    with _ctx(community):
        return _run(transform(_event(text, **event_kwargs)))


def _reply_text(result: PlatformEvent) -> str:
    return result.payload["text"]


def _seed_options(fake_host, community: str, options: list[str]) -> None:
    from waddle_sdk.community_kv import _scoped_key

    fake_host.kv.store[_scoped_key(community, OPTIONS_KEY)] = json.dumps(
        options
    ).encode()


def _stored_options(fake_host, community: str) -> list[str]:
    from waddle_sdk.community_kv import _scoped_key

    raw = fake_host.kv.store.get(_scoped_key(community, OPTIONS_KEY))
    return json.loads(raw) if raw is not None else []


# ---------------------------------------------------------------------------
# Basic routing / flag / non-matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["hello", "", "wheel spin", "!wheelbarrow", "!wheelbarrow add x"]
)
def test_non_matching_text_is_ignored(text: str, fake_host) -> None:
    assert _run(transform(_event(text))) is None
    assert fake_host.kv.calls == []  # cheap-skip: zero kv ops


def test_disabled_flag_suppresses_every_reply(fake_host) -> None:
    fake_host.flag_state["enabled"] = False
    assert _run(transform(_event("!wheel add tacos", is_mod=True))) is None
    assert fake_host.kv.calls == []


def test_non_chat_payload_is_ignored(fake_host) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="channel.follow",
        actor=None,
        payload={},
        occurred_at="",
    )
    assert _run(transform(event)) is None


@pytest.mark.parametrize("text", ["!Wheel", "!WHEEL list", "!WhEeL Add Tacos"])
def test_command_word_matches_case_insensitively(text: str, fake_host) -> None:
    result = _transform_in("comm-1", text, is_mod=True)
    assert result is not None


def test_missing_bundle_context_raises_runtime_error(fake_host) -> None:
    """A matched command outside any `bundle_context()` block fails loud, never silently."""
    with pytest.raises(BundleRuntimeError):
        _run(transform(_event("!wheel list")))


# ---------------------------------------------------------------------------
# community-context requirement
# ---------------------------------------------------------------------------


def test_no_community_context_replies_requires_community(fake_host) -> None:
    result = _transform_in(None, "!wheel add tacos", is_mod=True)
    assert result is not None
    assert "community context" in _reply_text(result)
    assert fake_host.kv.calls == []


# ---------------------------------------------------------------------------
# !wheel add
# ---------------------------------------------------------------------------


def test_add_requires_moderator_or_broadcaster(fake_host) -> None:
    result = _transform_in(
        "comm-1", "!wheel add tacos", is_mod=False, is_broadcaster=False
    )
    assert "Only the broadcaster or a moderator" in _reply_text(result)
    assert _stored_options(fake_host, "comm-1") == []


def test_add_allowed_for_moderator(fake_host) -> None:
    result = _transform_in("comm-1", "!wheel add tacos", is_mod=True)
    assert "Added 'tacos'" in _reply_text(result)
    assert _stored_options(fake_host, "comm-1") == ["tacos"]


def test_add_allowed_for_broadcaster_without_mod(fake_host) -> None:
    result = _transform_in(
        "comm-1", "!wheel add tacos", is_mod=False, is_broadcaster=True
    )
    assert "Added 'tacos'" in _reply_text(result)


def test_add_preserves_multi_word_option_text(fake_host) -> None:
    result = _transform_in("comm-1", "!wheel add Pizza Night", is_mod=True)
    assert "Added 'Pizza Night'" in _reply_text(result)
    assert _stored_options(fake_host, "comm-1") == ["Pizza Night"]


def test_add_rejects_case_insensitive_duplicate(fake_host) -> None:
    _transform_in("comm-1", "!wheel add Tacos", is_mod=True)
    result = _transform_in("comm-1", "!wheel add tacos", is_mod=True)
    assert "already on the wheel" in _reply_text(result)
    assert _stored_options(fake_host, "comm-1") == ["Tacos"]


def test_add_without_option_returns_usage(fake_host) -> None:
    result = _transform_in("comm-1", "!wheel add", is_mod=True)
    assert _reply_text(result) == "Usage: !wheel add <option>"


def test_add_rejects_option_over_max_length(fake_host) -> None:
    result = _transform_in(
        "comm-1", f"!wheel add {'x' * (MAX_OPTION_LEN + 1)}", is_mod=True
    )
    assert "characters or fewer" in _reply_text(result)
    assert _stored_options(fake_host, "comm-1") == []


def test_add_accepts_option_at_max_length(fake_host) -> None:
    option = "x" * MAX_OPTION_LEN
    result = _transform_in("comm-1", f"!wheel add {option}", is_mod=True)
    assert f"Added '{option}'" in _reply_text(result)


def test_add_rejects_when_wheel_is_full(fake_host) -> None:
    _seed_options(fake_host, "comm-1", [f"opt{i}" for i in range(MAX_OPTIONS)])
    result = _transform_in("comm-1", "!wheel add one-more", is_mod=True)
    assert f"{MAX_OPTIONS} options" in _reply_text(result)
    assert len(_stored_options(fake_host, "comm-1")) == MAX_OPTIONS


def test_add_increments_option_count_in_reply(fake_host) -> None:
    _transform_in("comm-1", "!wheel add tacos", is_mod=True)
    result = _transform_in("comm-1", "!wheel add pizza", is_mod=True)
    assert "(2 options)" in _reply_text(result)


# ---------------------------------------------------------------------------
# !wheel remove
# ---------------------------------------------------------------------------


def test_remove_requires_moderator_or_broadcaster(fake_host) -> None:
    _seed_options(fake_host, "comm-1", ["tacos"])
    result = _transform_in(
        "comm-1", "!wheel remove tacos", is_mod=False, is_broadcaster=False
    )
    assert "Only the broadcaster or a moderator" in _reply_text(result)
    assert _stored_options(fake_host, "comm-1") == ["tacos"]


def test_remove_existing_option_case_insensitive(fake_host) -> None:
    _seed_options(fake_host, "comm-1", ["Tacos", "Pizza"])
    result = _transform_in("comm-1", "!wheel remove tacos", is_mod=True)
    assert "Removed 'Tacos'" in _reply_text(result)
    assert _stored_options(fake_host, "comm-1") == ["Pizza"]


def test_remove_nonexistent_option_errors(fake_host) -> None:
    result = _transform_in("comm-1", "!wheel remove tacos", is_mod=True)
    assert "isn't on the wheel" in _reply_text(result)


def test_remove_without_option_returns_usage(fake_host) -> None:
    result = _transform_in("comm-1", "!wheel remove", is_mod=True)
    assert _reply_text(result) == "Usage: !wheel remove <option>"


# ---------------------------------------------------------------------------
# !wheel list
# ---------------------------------------------------------------------------


def test_list_empty_wheel(fake_host) -> None:
    result = _transform_in("comm-1", "!wheel list")
    assert "no options yet" in _reply_text(result)


def test_list_open_to_anyone(fake_host) -> None:
    _seed_options(fake_host, "comm-1", ["tacos", "pizza"])
    result = _transform_in("comm-1", "!wheel list", is_mod=False, is_broadcaster=False)
    assert "tacos" in _reply_text(result) and "pizza" in _reply_text(result)


# ---------------------------------------------------------------------------
# !wheel reset
# ---------------------------------------------------------------------------


def test_reset_requires_moderator_or_broadcaster(fake_host) -> None:
    _seed_options(fake_host, "comm-1", ["tacos"])
    result = _transform_in("comm-1", "!wheel reset", is_mod=False, is_broadcaster=False)
    assert "Only the broadcaster or a moderator" in _reply_text(result)
    assert _stored_options(fake_host, "comm-1") == ["tacos"]


def test_reset_clears_all_options(fake_host) -> None:
    _seed_options(fake_host, "comm-1", ["tacos", "pizza"])
    result = _transform_in("comm-1", "!wheel reset", is_mod=True)
    assert "cleared" in _reply_text(result)
    assert _stored_options(fake_host, "comm-1") == []


# ---------------------------------------------------------------------------
# bare `!wheel` / `!wheel spin`
# ---------------------------------------------------------------------------


def test_bare_wheel_spins_on_populated_wheel(fake_host) -> None:
    _seed_options(fake_host, "comm-1", ["tacos", "pizza"])
    result = _transform_in("comm-1", "!wheel")
    text = _reply_text(result)
    assert "tacos" in text or "pizza" in text


def test_explicit_spin_keyword_spins(fake_host) -> None:
    _seed_options(fake_host, "comm-1", ["tacos"])
    result = _transform_in("comm-1", "!wheel spin")
    assert "tacos" in _reply_text(result)


def test_bare_spin_on_empty_wheel_rejects(fake_host) -> None:
    result = _transform_in("comm-1", "!wheel")
    assert "no options yet" in _reply_text(result)


def test_spin_is_open_to_anyone(fake_host) -> None:
    _seed_options(fake_host, "comm-1", ["tacos"])
    result = _transform_in("comm-1", "!wheel spin", is_mod=False, is_broadcaster=False)
    assert "tacos" in _reply_text(result)


def test_spin_picks_uniformly_from_options(
    monkeypatch: pytest.MonkeyPatch, fake_host
) -> None:
    """Seeded-RNG spin: `random.choice` is monkeypatched to prove the exact option surfaces."""
    _seed_options(fake_host, "comm-1", ["tacos", "pizza", "sushi"])
    import app

    monkeypatch.setattr(app.random, "choice", lambda seq: seq[1])
    result = _transform_in("comm-1", "!wheel spin")
    assert "pizza" in _reply_text(result)


# ---------------------------------------------------------------------------
# unknown subcommand
# ---------------------------------------------------------------------------


def test_unknown_subcommand_returns_error(fake_host) -> None:
    result = _transform_in("comm-1", "!wheel bogus")
    assert "Unknown !wheel subcommand" in _reply_text(result)
    assert "Usage" in _reply_text(result)


# ---------------------------------------------------------------------------
# Permission fail-safe: role info unavailable (Discord today)
# ---------------------------------------------------------------------------


def test_discord_shaped_event_with_no_role_fields_rejects_mutation(fake_host) -> None:
    with _ctx("comm-1"):
        result = _run(transform(_no_role_event("!wheel add tacos")))
    assert "Only the broadcaster or a moderator" in _reply_text(result)
    assert _stored_options(fake_host, "comm-1") == []


def test_discord_shaped_event_still_allows_reads(fake_host) -> None:
    _seed_options(fake_host, "comm-1", ["tacos"])
    with _ctx("comm-1"):
        result = _run(transform(_no_role_event("!wheel list")))
    assert "tacos" in _reply_text(result)


def test_role_info_unavailable_is_logged(fake_host) -> None:
    with _ctx("comm-1"):
        _run(transform(_no_role_event("!wheel add tacos")))
    assert any(
        msg == "wheel.role_info_unavailable"
        for _lvl, msg, _fields in fake_host.log_calls
    )


# ---------------------------------------------------------------------------
# kv failure handling: fail loud, never silent
# ---------------------------------------------------------------------------


def test_kv_get_failure_produces_error_reply_and_logs(fake_host) -> None:
    def _boom(key: str) -> bytes:
        raise RuntimeError("backend unavailable")

    fake_host.wit_world.imports.kv.get = _boom
    result = _transform_in("comm-1", "!wheel list")
    assert "went wrong" in _reply_text(result)
    assert any(msg == "wheel.kv_failure" for _lvl, msg, _fields in fake_host.log_calls)


def test_kv_set_failure_on_add_produces_error_reply(fake_host) -> None:
    def _boom(key: str, value: bytes, ttl_seconds: int) -> None:
        raise RuntimeError("backend unavailable")

    fake_host.wit_world.imports.kv.set = _boom
    result = _transform_in("comm-1", "!wheel add tacos", is_mod=True)
    assert "went wrong" in _reply_text(result)
    assert any(msg == "wheel.kv_failure" for _lvl, msg, _fields in fake_host.log_calls)


def test_corrupt_options_value_is_kv_failure(fake_host) -> None:
    from waddle_sdk.community_kv import _scoped_key

    fake_host.kv.store[_scoped_key("comm-1", OPTIONS_KEY)] = b"not json"
    result = _transform_in("comm-1", "!wheel list")
    assert "went wrong" in _reply_text(result)


def test_options_value_not_a_list_of_strings_is_kv_failure(fake_host) -> None:
    from waddle_sdk.community_kv import _scoped_key

    fake_host.kv.store[_scoped_key("comm-1", OPTIONS_KEY)] = json.dumps(
        [1, 2, 3]
    ).encode()
    result = _transform_in("comm-1", "!wheel list")
    assert "went wrong" in _reply_text(result)


# ---------------------------------------------------------------------------
# No raw actor in kv keys or logs
# ---------------------------------------------------------------------------


def test_transform_never_stores_or_logs_the_raw_actor(fake_host) -> None:
    _transform_in("comm-1", "!wheel add tacos", is_mod=True, actor="viewer-1")
    _transform_in("comm-1", "!wheel spin", actor="viewer-1")

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
        app_id=_APP_ID,
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
    envelope = _envelope(
        "twitch", {"channel_id": "12345", "text": "Added 'tacos' (1 option)."}
    )
    result = _run(dispatch(envelope, {}, http_client=None))

    provider, message_json = fake_host.relay_calls[0]
    assert provider == "twitch"
    assert json.loads(message_json) == {
        "channel": "12345",
        "text": "Added 'tacos' (1 option).",
    }
    assert result.detail == "relayed"


def test_dispatch_raises_when_channel_id_is_missing(fake_host) -> None:
    envelope = _envelope("twitch", {"channel_id": None, "text": "hi"})
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_when_text_is_missing(fake_host) -> None:
    envelope = _envelope("twitch", {"channel_id": "12345", "text": None})
    with pytest.raises(ValueError, match="text"):
        _run(dispatch(envelope, {}, http_client=None))


# ---------------------------------------------------------------------------
# kv key charset regression (gh-631): OPTIONS_KEY, once community-scoped, must
# never contain ':'
# ---------------------------------------------------------------------------


def test_kv_key_constants_satisfy_host_guest_key_charset() -> None:
    from waddle_sdk.community_kv import _scoped_key
    from waddle_sdk.kv import validate_key

    scoped = _scoped_key("comm-1", OPTIONS_KEY)
    validate_key(scoped)
    assert ":" not in OPTIONS_KEY
    assert ":" not in scoped
