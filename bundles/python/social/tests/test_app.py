"""Host-native tests for the `social` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/fish/tests/test_app.py`'s own
docstring for the fake-`wit_world` approach this mirrors.

Uses the shared `waddle_sdk.testing.install_fake_kv_host` fake (gh-631) for
the `kv` capability -- it enforces the exact same colon-free charset as the
real host (`waddle_sdk.kv.validate_key`), so a bundle key regression (a
stray `:` creeping back into `app.py`) fails here immediately instead of
only on the real host in production. `flags`/`relay`/`log`/`clock` have no
shared fake yet (only `kv` does, per `testing.py`'s own module docstring),
so this suite extends the same `wit_world.imports` namespace
`install_fake_kv_host` installs with hand-rolled stand-ins for those four,
same shape as `bundles/python/duel/tests/test_app.py`'s own `_install()`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import types
from dataclasses import dataclass, field
from typing import Any

import pytest
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

from app import (
    _INTERACTIONS,
    _NO_RECORD_YET,
    _SOCIAL_USAGE,
    _normalize_target,
    _pseudonym,
    _resolve_interaction_command,
    _resolve_social_command,
    _wins_style_key,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _expected_pseudonym(identity: str | None) -> str:
    return hashlib.sha256((identity or "anonymous").encode()).hexdigest()


def _scoped(key: str, community: str = "comm-1") -> str:
    """`FakeKvHost.store` is keyed by `community_kv`'s own `c.<community>.<key>` prefix."""
    return _scoped_key(community, key)


@dataclass(slots=True)
class _FakeHost:
    """Composed fake: `kv` from the shared `waddle_sdk.testing` fixture, the rest hand-rolled."""

    kv: FakeKvHost
    relay_calls: list[tuple[str, str]] = field(default_factory=list)
    log_calls: list[tuple[str, str, str]] = field(default_factory=list)
    now_ms: int = 1_700_000_000_000

    @property
    def store(self) -> dict[str, bytes]:
        """Delegate to the shared `FakeKvHost`'s own store -- see its own docstring."""
        return self.kv.store


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kv_get_raises: Exception | None = None,
    kv_increment_raises: Exception | None = None,
    flag_enabled: bool = True,
) -> _FakeHost:
    """Install the shared `kv` fake, then extend it with flags/relay/log/clock stand-ins."""
    kv_host = install_fake_kv_host(monkeypatch)
    host = _FakeHost(kv=kv_host)

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: flag_enabled)
    relay_mod = types.SimpleNamespace(
        push=lambda provider, msg: host.relay_calls.append((provider, msg))
    )
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: host.log_calls.append((lvl, msg, fields_json)),
    )
    clock_mod = types.SimpleNamespace(
        now_millis=lambda: host.now_ms,
        now_rfc3339=lambda: "2026-10-05T00:00:00.000Z",
        monotonic_nanos=lambda: 0,
    )

    import wit_world

    wit_world.imports.flags = flags_mod  # type: ignore[attr-defined]
    wit_world.imports.relay = relay_mod  # type: ignore[attr-defined]
    wit_world.imports.log = log_mod  # type: ignore[attr-defined]
    wit_world.imports.clock = clock_mod  # type: ignore[attr-defined]

    # `install_fake_kv_host` wires `wit_world.imports.kv` from bound methods captured at
    # call time, so overriding `kv_host.get`/`.increment` *after* that call has no effect on
    # the already-wired namespace -- replace the `kv` submodule's own attributes directly.
    if kv_get_raises is not None:

        def _get(key: str) -> bytes | None:
            raise kv_get_raises

        wit_world.imports.kv.get = _get  # type: ignore[attr-defined]
    if kv_increment_raises is not None:

        def _increment(key: str, delta: int, ttl_seconds: int) -> int:
            raise kv_increment_raises

        wit_world.imports.kv.increment = _increment  # type: ignore[attr-defined]

    return host


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    return _install(monkeypatch)


def _sample_event(text: str, *, channel_id: str | None = "12345") -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-05T00:00:00.000Z",
    )


def _sample_envelope(
    platform: str,
    command: str,
    *,
    community: str | None = "comm-1",
    actor: str | None = "viewer-1",
    interaction: str | None = None,
    target: str | None = None,
    channel_id: str | None = "12345",
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": channel_id}
    if interaction is not None:
        payload["interaction"] = interaction
    if target is not None:
        payload["target"] = target
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.social",
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

_ALL_INTERACTIONS = tuple(_INTERACTIONS)


@pytest.mark.parametrize("interaction", _ALL_INTERACTIONS)
@pytest.mark.parametrize("casing", ["!{}", "!{}  ", "!{}".upper()])
def test_bare_interaction_with_no_target_is_usage(
    interaction: str, casing: str, fake_host: _FakeHost
) -> None:
    text = casing.format(interaction) if "{}" in casing else casing
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"
    assert result.payload["interaction"] == interaction


@pytest.mark.parametrize("interaction", _ALL_INTERACTIONS)
def test_interaction_with_target_matches_as_interact(
    interaction: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(f"!{interaction} bob")))
    assert result is not None
    assert result.payload["command"] == "interact"
    assert result.payload["interaction"] == interaction
    assert result.payload["target"] == "bob"


@pytest.mark.parametrize("interaction", _ALL_INTERACTIONS)
def test_interaction_is_case_insensitive(interaction: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(f"!{interaction.upper()} @bob")))
    assert result is not None
    assert result.payload["command"] == "interact"
    assert result.payload["target"] == "@bob"


@pytest.mark.parametrize("interaction", _ALL_INTERACTIONS)
def test_interaction_with_multiword_target_is_usage(
    interaction: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(f"!{interaction} bob smith")))
    assert result is not None
    assert result.payload["command"] == "usage"


def test_social_stats_matches(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!social stats")))
    assert result is not None
    assert result.payload["command"] == "stats"


def test_social_stats_is_case_insensitive(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!SOCIAL STATS")))
    assert result is not None
    assert result.payload["command"] == "stats"


def test_bare_social_is_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!social")))
    assert result is not None
    assert result.payload["command"] == "usage"
    assert "interaction" not in result.payload


def test_social_with_unknown_subcommand_is_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!social leaderboard")))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!hugger bob", "!huggable", "!pats bob", "hello", "", "!social!"])
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
    _install(monkeypatch, flag_enabled=False)
    assert _run(transform(_sample_event("!hug bob"))) is None
    assert _run(transform(_sample_event("!social stats"))) is None


# -- _resolve_interaction_command() / _resolve_social_command() / _normalize_target() ----------


def test_resolve_interaction_command_bare_is_usage() -> None:
    assert _resolve_interaction_command("!hug", command_word="!hug") == ("usage", None)


def test_resolve_interaction_command_single_token_is_interact() -> None:
    assert _resolve_interaction_command("!hug bob", command_word="!hug") == ("interact", "bob")


def test_resolve_interaction_command_two_token_is_usage() -> None:
    assert _resolve_interaction_command("!hug bob smith", command_word="!hug") == ("usage", None)


def test_resolve_social_command_stats() -> None:
    assert _resolve_social_command("!social stats") == "stats"


def test_resolve_social_command_bare_is_usage() -> None:
    assert _resolve_social_command("!social") == "usage"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("bob", "bob"), ("@bob", "bob"), ("a" * 32, "a" * 32)],
)
def test_normalize_target_accepts_plausible_usernames(raw: str, expected: str) -> None:
    assert _normalize_target(raw) == expected


@pytest.mark.parametrize("raw", ["@", "", "a" * 33, "bob!", "bob bob", "$$$"])
def test_normalize_target_rejects_implausible_strings(raw: str) -> None:
    assert _normalize_target(raw) is None


# -- interact: self / unknown-target --------------------------------------------


@pytest.mark.parametrize("interaction", _ALL_INTERACTIONS)
def test_self_interaction_is_handled_gracefully(interaction: str, fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "interact", interaction=interaction, target="viewer-1", actor="viewer-1"
    )
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == f"interact:{interaction}:self"
    assert fake_host.kv.calls == []
    provider, message_json = fake_host.relay_calls[-1]
    assert "viewer-1" in message_json


@pytest.mark.parametrize("interaction", _ALL_INTERACTIONS)
def test_self_interaction_is_case_insensitive(interaction: str, fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "interact", interaction=interaction, target="Viewer-1", actor="viewer-1"
    )
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == f"interact:{interaction}:self"


@pytest.mark.parametrize("interaction", _ALL_INTERACTIONS)
def test_unknown_target_is_rejected_gracefully(interaction: str, fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "interact", interaction=interaction, target="@")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == f"interact:{interaction}:unknown_target"
    assert fake_host.kv.calls == []
    provider, message_json = fake_host.relay_calls[-1]
    assert "don't know who" in message_json


def test_dispatch_raises_when_interact_target_missing(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "interact", interaction="hug", target=None)
    with pytest.raises(ValueError, match="requires a target"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_when_interact_interaction_missing(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "interact", interaction=None, target="bob")
    with pytest.raises(ValueError, match="known interaction"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_when_interact_interaction_unrecognized(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "interact", interaction="not-a-real-one", target="bob")
    with pytest.raises(ValueError, match="known interaction"):
        _run(dispatch(envelope, {}, http_client=None))


# -- interact: resolved, given/received counters ---------------------------------


@pytest.mark.parametrize("interaction", _ALL_INTERACTIONS)
def test_first_interaction_persists_state_and_replies(
    interaction: str, fake_host: _FakeHost
) -> None:
    envelope = _sample_envelope("twitch", "interact", interaction=interaction, target="bob")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == f"interact:{interaction}:resolved"
    giver_pseudonym = _expected_pseudonym("viewer-1")
    receiver_pseudonym = _expected_pseudonym("bob")
    given_key = _scoped(_wins_style_key(interaction, "given", giver_pseudonym))
    received_key = _scoped(_wins_style_key(interaction, "received", receiver_pseudonym))
    assert int(fake_host.store[given_key].decode()) == 1
    assert int(fake_host.store[received_key].decode()) == 1
    provider, message_json = fake_host.relay_calls[-1]
    assert "viewer-1" in message_json
    assert "bob" in message_json


@pytest.mark.parametrize("interaction", _ALL_INTERACTIONS)
def test_repeated_interaction_accumulates_counts(interaction: str, fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "interact", interaction=interaction, target="bob")
    _run(dispatch(envelope, {}, http_client=None))
    _run(dispatch(envelope, {}, http_client=None))

    giver_pseudonym = _expected_pseudonym("viewer-1")
    receiver_pseudonym = _expected_pseudonym("bob")
    given_key = _scoped(_wins_style_key(interaction, "given", giver_pseudonym))
    received_key = _scoped(_wins_style_key(interaction, "received", receiver_pseudonym))
    assert int(fake_host.store[given_key].decode()) == 2
    assert int(fake_host.store[received_key].decode()) == 2


def test_different_interactions_track_independent_counters(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "interact", interaction="hug", target="bob"),
            {},
            http_client=None,
        )
    )
    _run(
        dispatch(
            _sample_envelope("twitch", "interact", interaction="highfive", target="bob"),
            {},
            http_client=None,
        )
    )

    giver_pseudonym = _expected_pseudonym("viewer-1")
    hug_given = _scoped(_wins_style_key("hug", "given", giver_pseudonym))
    highfive_given = _scoped(_wins_style_key("highfive", "given", giver_pseudonym))
    pat_given = _scoped(_wins_style_key("pat", "given", giver_pseudonym))
    assert int(fake_host.store[hug_given].decode()) == 1
    assert int(fake_host.store[highfive_given].decode()) == 1
    assert pat_given not in fake_host.store


def test_different_communities_have_independent_counters(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope(
                "twitch", "interact", interaction="hug", target="bob", community="comm-1"
            ),
            {},
            http_client=None,
        )
    )
    _run(
        dispatch(
            _sample_envelope(
                "twitch", "interact", interaction="hug", target="bob", community="comm-2"
            ),
            {},
            http_client=None,
        )
    )

    giver_pseudonym = _expected_pseudonym("viewer-1")
    comm1_key = _scoped(_wins_style_key("hug", "given", giver_pseudonym), community="comm-1")
    comm2_key = _scoped(_wins_style_key("hug", "given", giver_pseudonym), community="comm-2")
    assert int(fake_host.store[comm1_key].decode()) == 1
    assert int(fake_host.store[comm2_key].decode()) == 1


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "interact", interaction="hug", target="bob", community=None
    )
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv.calls == []


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "interact", interaction="hug", target="bob", channel_id=None
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_on_unrecognized_command(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "not-a-real-command")
    with pytest.raises(ValueError, match="unrecognized social command"):
        _run(dispatch(envelope, {}, http_client=None))


# -- !social stats -------------------------------------------------------------


def test_stats_before_any_interaction(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "stats"), {}, http_client=None))
    assert result.detail == "stats"
    provider, message_json = fake_host.relay_calls[-1]
    assert _NO_RECORD_YET in message_json


def test_stats_reflects_given_and_received_across_interactions(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "interact", interaction="hug", target="bob"),
            {},
            http_client=None,
        )
    )
    _run(
        dispatch(
            _sample_envelope("twitch", "interact", interaction="highfive", target="carol"),
            {},
            http_client=None,
        )
    )
    # carol hugs viewer-1 back -> viewer-1 receives a hug
    _run(
        dispatch(
            _sample_envelope(
                "twitch", "interact", interaction="hug", target="viewer-1", actor="carol"
            ),
            {},
            http_client=None,
        )
    )

    result = _run(dispatch(_sample_envelope("twitch", "stats"), {}, http_client=None))
    assert result.detail == "stats"
    provider, message_json = fake_host.relay_calls[-1]
    text = message_json
    assert "hug 1 given / 1 received" in text
    assert "highfive 1 given / 0 received" in text
    assert "pat 0 given / 0 received" in text


def test_stats_corrupt_counter_falls_back_to_zero(fake_host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    # A real (non-corrupt) count elsewhere so the overall total isn't zero -- otherwise
    # `_handle_stats` would short-circuit to `_NO_RECORD_YET` regardless of the corrupt value.
    fake_host.store[_scoped(_wins_style_key("pat", "given", pseudonym))] = b"3"
    fake_host.store[_scoped(_wins_style_key("hug", "given", pseudonym))] = b"not-a-number"
    result = _run(dispatch(_sample_envelope("twitch", "stats"), {}, http_client=None))

    provider, message_json = fake_host.relay_calls[-1]
    assert "hug 0 given / 0 received" in message_json
    assert "pat 3 given / 0 received" in message_json
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "social.stats_given_corrupt"]
    assert corrupt_logs
    assert result.detail == "stats"


# -- usage -----------------------------------------------------------------


@pytest.mark.parametrize("interaction", _ALL_INTERACTIONS)
def test_usage_command_replies_with_per_interaction_usage(
    interaction: str, fake_host: _FakeHost
) -> None:
    envelope = _sample_envelope("twitch", "usage", interaction=interaction)
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "usage"
    provider, message_json = fake_host.relay_calls[-1]
    assert f"!{interaction} <user>" in message_json


def test_usage_command_without_interaction_replies_social_usage(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "usage", interaction=None)
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "usage"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _SOCIAL_USAGE


# -- kv failure: fail loud, never silent ---------------------------------------


class _ErrorBackend:
    """Stand-in for the generated WIT `Error_Backend` variant case class."""


class _KvError(Exception):
    """Stand-in for the generated `Err` wrapper -- `.value` holds the error union member."""

    def __init__(self) -> None:
        self.value = _ErrorBackend()


def test_interact_replies_and_raises_on_kv_increment_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _install(monkeypatch, kv_increment_raises=_KvError())

    with pytest.raises(RuntimeError, match="kv increment failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "interact", interaction="hug", target="bob"),
                {},
                http_client=None,
            )
        )

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in message_json
    error_logs = [(lvl, m) for lvl, m, _f in host.log_calls if m == "social.kv_error"]
    assert error_logs


def test_stats_replies_and_raises_on_kv_get_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _install(monkeypatch, kv_get_raises=_KvError())

    with pytest.raises(RuntimeError, match="kv get failed"):
        _run(dispatch(_sample_envelope("twitch", "stats"), {}, http_client=None))

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in message_json


# -- PII: never leak the raw actor/target into kv keys or logs ------------------


def test_pseudonym_is_a_non_reversible_hash_not_the_raw_identity() -> None:
    assert _pseudonym("viewer-1") == _expected_pseudonym("viewer-1")
    assert "viewer-1" not in _pseudonym("viewer-1")


def test_dispatch_never_logs_the_raw_actor_or_target(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "interact", interaction="hug", target="bob"),
            {},
            http_client=None,
        )
    )
    _run(dispatch(_sample_envelope("twitch", "stats"), {}, http_client=None))
    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "bob" not in message
        assert "bob" not in fields_json


def test_pseudonym_differs_per_identity() -> None:
    assert _pseudonym("viewer-1") != _pseudonym("viewer-2")
    assert _pseudonym(None) == _pseudonym(None)  # anonymous is stable, still never raw


# -- kv key charset: colon-free (gh-631) -----------------------------------------


@pytest.mark.parametrize("interaction", _ALL_INTERACTIONS)
def test_counter_keys_contain_no_colons(interaction: str) -> None:
    """`waddle_sdk.kv.validate_key` would reject a `:`-bearing key -- assert the shape directly.

    The shared `FakeKvHost` already enforces this on every `kv` call made during the suite
    above (a regression would raise `InvalidKvKeyError` mid-test); this test additionally
    pins the exact key shape so the convention is documented, not just incidentally enforced.
    """
    pseudonym = _pseudonym("viewer-1")
    assert ":" not in _wins_style_key(interaction, "given", pseudonym)
    assert ":" not in _wins_style_key(interaction, "received", pseudonym)
