"""Host-native tests for the `roulette` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/pyping/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors. Unlike `slots`/`fish` (which each hand-roll their own
in-memory `kv` fake), this suite uses the shared, charset-enforcing
`waddle_sdk.testing.install_fake_kv_host` (gh-631): it validates every key exactly like the real
`kv` host capability (`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`), so a `:` typo in
this bundle's own key-building helpers would fail this suite immediately instead of only
surfacing on the real host in production. `flags`/`relay`/`log`/`clock` have no shared fake yet
(only `kv` does, per `testing.py`'s own module docstring) -- those four are attached directly to
the same fake `wit_world.imports` namespace `install_fake_kv_host` installs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import types
from typing import Any

import pytest
from waddle_sdk.command import CommandSpec, ParsedCommand, parse_command
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

from app import (
    _NOTHING_PULLED_YET,
    _USAGE,
    DEFAULT_COOLDOWN_SECONDS,
    MAX_COOLDOWN_SECONDS,
    MIN_COOLDOWN_SECONDS,
    _caller_role_signal,
    _format_duration,
    _lastpull_key,
    _outs_key,
    _pseudonym,
    _pull_trigger,
    _pulls_key,
    _resolve_command,
    _survives_key,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _expected_pseudonym(actor: str | None) -> str:
    return hashlib.sha256((actor or "anonymous").encode()).hexdigest()


def _scoped(key: str, community: str = "comm-1") -> str:
    """`fake_host.store` is keyed by `community_kv`'s own `c.<community>.<key>` prefix."""
    return _scoped_key(community, key)


def _sample_event(
    text: str,
    *,
    channel_id: str | None = "12345",
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": channel_id}
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload=payload,
        occurred_at="2026-10-05T00:00:00.000Z",
    )


class _FakeHost:
    """Fake WIT host state: wraps the shared `FakeKvHost` + local flags/relay/log/clock fakes."""

    def __init__(self, kv: FakeKvHost) -> None:
        self.kv = kv
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[str, str, str]] = []
        self.now_ms = 1_700_000_000_000

    @property
    def store(self) -> dict[str, bytes]:
        """The shared fake's own in-memory store -- validated-key-only, see module docstring."""
        return self.kv.store

    def advance(self, seconds: float) -> None:
        self.now_ms += int(seconds * 1000)


def _attach_non_kv_fakes(host: _FakeHost, *, flag_enabled: bool) -> None:
    """Attach `flags`/`relay`/`log`/`clock` fakes onto `wit_world.imports`.

    `install_fake_kv_host` must already have installed the fake `wit_world`
    into `sys.modules` -- no shared fake exists yet for these four (only
    `kv` does), so they're attached directly here, same shape as
    `slots`/`fish`'s own hand-rolled fakes.
    """
    import wit_world

    wit_world.imports.flags = types.SimpleNamespace(  # type: ignore[attr-defined]
        enabled=lambda key, default_value: flag_enabled
    )
    wit_world.imports.relay = types.SimpleNamespace(  # type: ignore[attr-defined]
        push=lambda provider, msg: host.relay_calls.append((provider, msg))
    )
    wit_world.imports.log = types.SimpleNamespace(  # type: ignore[attr-defined]
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: host.log_calls.append((lvl, msg, fields_json)),
    )
    wit_world.imports.clock = types.SimpleNamespace(  # type: ignore[attr-defined]
        now_millis=lambda: host.now_ms,
        now_rfc3339=lambda: "2026-10-05T00:00:00.000Z",
        monotonic_nanos=lambda: 0,
    )


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    kv = install_fake_kv_host(monkeypatch)
    host = _FakeHost(kv)
    _attach_non_kv_fakes(host, flag_enabled=True)
    return host


def _break_kv(monkeypatch: pytest.MonkeyPatch, op: str, exc: Exception) -> None:
    """Make `wit_world.imports.kv.<op>` raise `exc` -- simulates a real host-backend failure."""
    import wit_world

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr(wit_world.imports.kv, op, _raise)


def _sample_envelope(
    platform: str,
    command: str,
    *,
    community: str | None = "comm-1",
    actor: str | None = "viewer-1",
    arg: str | None = None,
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": "12345"}
    if arg is not None:
        payload["arg"] = arg
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.roulette",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor=actor,
            payload=payload,
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )


# -- transform() parsing -------------------------------------------------------


@pytest.mark.parametrize("text", ["!roulette", "!ROULETTE", "  !roulette  "])
def test_bare_roulette_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "pull"


def test_roulette_list_matches(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!roulette list")))
    assert result is not None
    assert result.payload["command"] == "list"


def test_roulette_list_with_trailing_args_is_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!roulette list extra")))
    assert result is not None
    assert result.payload["command"] == "usage"


def test_roulette_set_cooldown_matches_with_args(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!roulette set cooldown 10")))
    assert result is not None
    assert result.payload["command"] == "config_set_cooldown"
    assert result.payload["arg"] == "cooldown 10"


@pytest.mark.parametrize(
    "text",
    [
        "!roulette enable ai",
        "!roulette bogus",
        "!roulette add",
        "!roulette reset",
        "!roulette disable foo",
    ],
)
def test_unsupported_verbs_and_bad_grammar_reply_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!roulettewheel", "roulette", "!roulett", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host: _FakeHost) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_string_text_is_ignored(fake_host: _FakeHost) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": None, "channel_id": "1"},
        occurred_at="",
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    kv = install_fake_kv_host(monkeypatch)
    host = _FakeHost(kv)
    _attach_non_kv_fakes(host, flag_enabled=False)
    assert _run(transform(_sample_event("!roulette"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!roulette", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!roulette")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# -- _resolve_command() direct unit coverage -----------------------------------


def test_resolve_command_none_is_usage() -> None:
    assert _resolve_command(None) == "usage"


def test_resolve_command_bare_is_pull() -> None:
    spec = CommandSpec(name="roulette")
    parsed = parse_command("!roulette", spec)
    assert _resolve_command(parsed) == "pull"


def test_resolve_command_unimplemented_verb_is_usage() -> None:
    parsed = ParsedCommand(command="roulette", sub_module=None, option="sub", args=None)
    assert _resolve_command(parsed) == "usage"


# -- pull / cooldown state machine ----------------------------------------------


def test_first_pull_persists_state_and_replies(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "pull")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "pull"
    pseudonym = _expected_pseudonym("viewer-1")
    assert _scoped(_lastpull_key(pseudonym)) in fake_host.store
    assert int(fake_host.store[_scoped(_pulls_key(pseudonym))].decode()) == 1
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "pull #1" in text
    assert "viewer-1" in text  # username IS rendered in the visible chat reply, never logged


def test_second_pull_within_cooldown_is_rejected(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))
    fake_host.advance(1)
    result = _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))

    assert result.detail == "pull"
    pseudonym = _expected_pseudonym("viewer-1")
    # not incremented again
    assert int(fake_host.store[_scoped(_pulls_key(pseudonym))].decode()) == 1
    provider, message_json = fake_host.relay_calls[-1]
    assert "slow down" in json.loads(message_json)["text"]


def test_pull_allowed_again_after_cooldown_elapses(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))
    fake_host.advance(DEFAULT_COOLDOWN_SECONDS + 1)
    result = _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))

    assert result.detail == "pull"
    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_pulls_key(pseudonym))].decode()) == 2
    provider, message_json = fake_host.relay_calls[-1]
    assert "pull #2" in json.loads(message_json)["text"]


def test_cooldown_corrupt_state_is_treated_as_no_cooldown(fake_host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_lastpull_key(pseudonym))] = b"not-a-timestamp"
    result = _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))

    assert result.detail == "pull"
    corrupt_logs = [
        m for _lvl, m, _f in fake_host.log_calls if m == "roulette.cooldown_state_corrupt"
    ]
    assert corrupt_logs


def test_different_communities_have_independent_cooldowns(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "pull", community="comm-1"), {}, http_client=None))
    result = _run(
        dispatch(_sample_envelope("twitch", "pull", community="comm-2"), {}, http_client=None)
    )
    assert result.detail == "pull"
    provider, message_json = fake_host.relay_calls[-1]
    assert "slow down" not in json.loads(message_json)["text"]


def test_zero_cooldown_never_blocks_and_never_persists_lastpull(fake_host: _FakeHost) -> None:
    """`cooldown == 0` is a deliberate lower bound -- disables the gate and skips the key."""
    envelope = _sample_envelope(
        "twitch", "config_set_cooldown", arg="cooldown 0", is_mod=True
    )
    _run(dispatch(envelope, {}, http_client=None))

    _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))

    assert result.detail == "pull"
    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_pulls_key(pseudonym))].decode()) == 2
    assert _scoped(_lastpull_key(pseudonym)) not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    assert "slow down" not in json.loads(message_json)["text"]


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "pull", community=None)
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv.calls == []


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.roulette",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={"command": "pull", "channel_id": None},
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_on_unrecognized_command(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "not-a-real-command")
    with pytest.raises(ValueError, match="unrecognized roulette command"):
        _run(dispatch(envelope, {}, http_client=None))


# -- survive/out outcome + stats tracking ----------------------------------------


def test_out_increments_outs_and_not_survives(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._pull_trigger", lambda: True)

    result = _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))

    assert result.detail == "pull"
    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_outs_key(pseudonym))].decode()) == 1
    assert _scoped(_survives_key(pseudonym)) not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "BANG" in text


def test_survive_increments_survives_and_not_outs(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._pull_trigger", lambda: False)

    result = _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))

    assert result.detail == "pull"
    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_survives_key(pseudonym))].decode()) == 1
    assert _scoped(_outs_key(pseudonym)) not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "click" in text


# -- !roulette list ------------------------------------------------------------


def test_list_before_any_pull(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _NOTHING_PULLED_YET


def test_list_after_a_survive_shows_pulls_survives_and_rate(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._pull_trigger", lambda: False)
    _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))

    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "Pulls: 1" in text
    assert "Survived: 1" in text
    assert "Out: 0" in text
    assert "100% survival rate" in text


def test_list_after_an_out_shows_zero_survives(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._pull_trigger", lambda: True)
    _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))

    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "Pulls: 1" in text
    assert "Survived: 0" in text
    assert "Out: 1" in text
    assert "0% survival rate" in text


def test_list_corrupt_pulls_falls_back_to_nothing_pulled(fake_host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_pulls_key(pseudonym))] = b"not-a-number"
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _NOTHING_PULLED_YET


def test_list_corrupt_survives_falls_back_to_zero(fake_host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_pulls_key(pseudonym))] = b"3"
    fake_host.store[_scoped(_survives_key(pseudonym))] = b"not-a-number"
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "Survived: 0" in text
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "roulette.survives_corrupt"]
    assert corrupt_logs


def test_list_corrupt_outs_falls_back_to_zero(fake_host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_pulls_key(pseudonym))] = b"3"
    fake_host.store[_scoped(_outs_key(pseudonym))] = b"not-a-number"
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "Out: 0" in text
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "roulette.outs_corrupt"]
    assert corrupt_logs


# -- admin config: cooldown -----------------------------------------------------


@pytest.mark.parametrize(
    ("is_mod", "is_broadcaster"),
    [(True, None), (None, True), (True, True)],
)
def test_set_cooldown_allowed_for_mod_or_broadcaster(
    is_mod: bool | None, is_broadcaster: bool | None, fake_host: _FakeHost
) -> None:
    envelope = _sample_envelope(
        "twitch",
        "config_set_cooldown",
        arg="cooldown 45",
        is_mod=is_mod,
        is_broadcaster=is_broadcaster,
    )
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_cooldown"
    assert fake_host.store[_scoped("roulette.config.cooldown")] == b"45"
    provider, message_json = fake_host.relay_calls[-1]
    assert "45" in json.loads(message_json)["text"]


def test_set_cooldown_rejected_for_non_mod(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "config_set_cooldown", arg="cooldown 45", is_mod=False, is_broadcaster=False
    )
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_cooldown:denied"
    assert _scoped("roulette.config.cooldown") not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    assert "only moderators/broadcasters" in json.loads(message_json)["text"]


def test_set_cooldown_rejected_when_role_signal_entirely_absent(fake_host: _FakeHost) -> None:
    """Discord today has no is_mod/is_broadcaster at all -- must deny, never implicitly allow."""
    envelope = _sample_envelope("discord", "config_set_cooldown", arg="cooldown 45")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_cooldown:denied"
    denial_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "roulette.config_denied"]
    assert denial_logs


@pytest.mark.parametrize(
    ("arg", "expected_fragment"),
    [
        (None, "Usage"),
        ("", "Usage"),
        ("badkey 45", "Usage"),
        ("cooldown", "Usage"),
        ("cooldown notanumber", "whole number"),
        (f"cooldown {MIN_COOLDOWN_SECONDS - 1}", "must be between"),
        (f"cooldown {MAX_COOLDOWN_SECONDS + 1}", "must be between"),
    ],
)
def test_set_cooldown_rejects_bad_input(
    arg: str | None, expected_fragment: str, fake_host: _FakeHost
) -> None:
    envelope = _sample_envelope("twitch", "config_set_cooldown", arg=arg, is_mod=True)
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_cooldown"
    assert _scoped("roulette.config.cooldown") not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    assert expected_fragment in json.loads(message_json)["text"]


@pytest.mark.parametrize(
    "seconds", [MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS, DEFAULT_COOLDOWN_SECONDS]
)
def test_set_cooldown_accepts_boundary_values(seconds: int, fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "config_set_cooldown", arg=f"cooldown {seconds}", is_mod=True
    )
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "config_set_cooldown"
    assert fake_host.store[_scoped("roulette.config.cooldown")] == str(seconds).encode()


def test_configured_cooldown_is_honored_on_next_pull(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "config_set_cooldown", arg="cooldown 5", is_mod=True
    )
    _run(dispatch(envelope, {}, http_client=None))

    _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))
    fake_host.advance(6)
    result = _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))

    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_pulls_key(pseudonym))].decode()) == 2
    assert result.detail == "pull"


def test_cooldown_config_corrupt_falls_back_to_default(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped("roulette.config.cooldown")] = b"not-a-number"
    _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))
    fake_host.advance(1)
    _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))

    corrupt_logs = [
        m for _lvl, m, _f in fake_host.log_calls if m == "roulette.cooldown_config_corrupt"
    ]
    assert corrupt_logs
    provider, message_json = fake_host.relay_calls[-1]
    assert "slow down" in json.loads(message_json)["text"]  # default cooldown still applied


# -- usage -----------------------------------------------------------------


def test_usage_command_replies_with_usage_text(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "usage"), {}, http_client=None))
    assert result.detail == "usage"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _USAGE


# -- kv failure: fail loud, never silent, and the real-host key charset -------


class _ErrorBackend:
    """Stand-in for the generated WIT `Error_Backend` variant case class."""


class _KvError(Exception):
    """Stand-in for the generated `Err` wrapper -- `.value` holds the error union member."""

    def __init__(self) -> None:
        self.value = _ErrorBackend()


def test_pull_replies_and_raises_on_kv_set_error(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_kv(monkeypatch, "set", _KvError())

    with pytest.raises(RuntimeError, match="kv set failed"):
        _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))

    provider, message_json = fake_host.relay_calls[-1]
    assert "jammed" in json.loads(message_json)["text"]
    error_logs = [(lvl, m) for lvl, m, _f in fake_host.log_calls if m == "roulette.kv_error"]
    assert error_logs


def test_pull_replies_and_raises_on_kv_get_error(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_kv(monkeypatch, "get", _KvError())

    with pytest.raises(RuntimeError, match="kv get failed"):
        _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))

    provider, message_json = fake_host.relay_calls[-1]
    assert "jammed" in json.loads(message_json)["text"]


def test_pull_replies_and_raises_on_kv_increment_error(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_kv(monkeypatch, "increment", _KvError())

    with pytest.raises(RuntimeError, match="kv increment failed"):
        _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))

    provider, message_json = fake_host.relay_calls[-1]
    assert "jammed" in json.loads(message_json)["text"]


def test_all_state_keys_are_colon_free(fake_host: _FakeHost) -> None:
    """Every key this bundle builds passes the shared fake's charset validation (gh-631)."""
    pseudonym = _expected_pseudonym("viewer-1")
    for key in (
        _pulls_key(pseudonym),
        _survives_key(pseudonym),
        _outs_key(pseudonym),
        _lastpull_key(pseudonym),
        "roulette.config.cooldown",
    ):
        assert ":" not in key


# -- PII: never leak the raw actor into kv keys or logs -------------------------


def test_pseudonym_is_a_non_reversible_hash_not_the_raw_actor() -> None:
    assert _pseudonym("viewer-1") == _expected_pseudonym("viewer-1")
    assert "viewer-1" not in _pseudonym("viewer-1")


def test_dispatch_never_logs_the_raw_actor(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "pull"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "actor" not in json.loads(fields_json)


def test_pseudonym_differs_per_actor() -> None:
    assert _pseudonym("viewer-1") != _pseudonym("viewer-2")
    assert _pseudonym(None) == _pseudonym(None)  # anonymous is stable, still never raw


# -- _pull_trigger() / _format_duration() unit coverage --------------------------


def test_pull_trigger_is_out_on_chamber_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("random.randint", lambda lo, hi: 1)
    assert _pull_trigger() is True


@pytest.mark.parametrize("chamber", [2, 3, 4, 5, 6])
def test_pull_trigger_survives_on_other_chambers(
    chamber: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("random.randint", lambda lo, hi: chamber)
    assert _pull_trigger() is False


def test_pull_trigger_returns_a_bool() -> None:
    assert isinstance(_pull_trigger(), bool)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (1, "1s"), (65, "1m 5s"), (3661, "1h 1m")],
)
def test_format_duration(seconds: int, expected: str) -> None:
    assert _format_duration(seconds) == expected


# -- _caller_role_signal() unit coverage -----------------------------------------


def test_caller_role_signal_absent_is_none() -> None:
    assert _caller_role_signal({}) is None


def test_caller_role_signal_true_from_mod() -> None:
    assert _caller_role_signal({"is_mod": True}) is True


def test_caller_role_signal_false_when_both_false() -> None:
    assert _caller_role_signal({"is_mod": False, "is_broadcaster": False}) is False
