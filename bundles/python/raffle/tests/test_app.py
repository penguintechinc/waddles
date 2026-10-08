"""Host-native tests for the `raffle` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/pyping/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors. Unlike `fish`/`music` (both predate
`waddle_sdk.testing`), this suite uses the SHARED, charset-enforcing `FakeKvHost`
(`waddle_sdk.testing.install_fake_kv_host`) for the `kv` import instead of hand-rolling a
second permissive fake -- a colon (or any other host-rejected byte) in this bundle's own keys
would fail these tests immediately (gh-631), not just in production. `flags`/`relay`/`log`
have no shared fake yet, so they're layered on top of the installed `wit_world` module the
same way `music`'s own hand-rolled fixture does.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from typing import Any, cast

import pytest
from waddle_sdk.command import ParsedCommand
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

from app import (
    MAX_ENTRANTS,
    _caller_role_signal,
    _map_parsed,
    _pick_winner,
    _pseudonym,
    _resolve_raffle,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _expected_pseudonym(actor: str | None) -> str:
    return hashlib.sha256((actor or "anonymous").encode()).hexdigest()


def _scoped(key: str, community: str = "comm-1") -> str:
    """`fake_kv.store` is keyed by `community_kv`'s own `c.<community>.<key>` prefix."""
    # `waddle_sdk` ships no `py.typed` marker, so mypy sees `Any` here -- cast back to the
    # real contract (see `music`'s own identical `_kv_get` for the same pattern).
    return cast(str, _scoped_key(community, key))


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


def _sample_envelope(
    platform: str,
    command: str,
    *,
    community: str | None = "comm-1",
    actor: str | None = "viewer-1",
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": "12345"}
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.raffle",
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


class _FakeExtras:
    """`flags`/`relay`/`log` call-recording stand-ins, layered on the shared `FakeKvHost`.

    No shared fake exists yet for these three imports (only `kv` does, gh-631) -- this
    mirrors `music`'s own hand-rolled fixture shape exactly, just split out from the `kv`
    fake it used to also provide.
    """

    def __init__(self) -> None:
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag_enabled = True


@pytest.fixture
def fake_kv(monkeypatch: pytest.MonkeyPatch) -> FakeKvHost:
    """Install the shared, charset-enforcing `kv` test double -- see module docstring."""
    return install_fake_kv_host(monkeypatch)


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch, fake_kv: FakeKvHost) -> _FakeExtras:
    """Layer `flags`/`relay`/`log` fakes onto the `wit_world` module `fake_kv` just installed."""
    extras = _FakeExtras()
    wit_world = sys.modules["wit_world"]
    wit_world.imports.flags = types.SimpleNamespace(
        enabled=lambda key, default_value: extras.flag_enabled
    )
    wit_world.imports.relay = types.SimpleNamespace(
        push=lambda provider, msg: extras.relay_calls.append((provider, msg))
    )
    wit_world.imports.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: extras.log_calls.append((lvl, msg, fields_json)),
    )
    return extras


def _set_kv_raises(monkeypatch: pytest.MonkeyPatch, *, op: str) -> None:
    """Monkeypatch the installed `wit_world.imports.kv.<op>` to raise a stand-in backend error.

    `FakeKvHost` is a `@dataclass(slots=True)` -- its bound methods were already captured
    into the installed `kv` namespace at `install_fake_kv_host()` time, so patching the
    *instance* (which has no free-form `__dict__` under `slots=True`) wouldn't reach calls
    made through that namespace. Patching the namespace attribute itself (a plain, mutable
    `types.SimpleNamespace`) does.
    """

    class _ErrorBackend:
        """Stand-in for the generated WIT `Error_Backend` variant case class."""

    class _KvError(Exception):
        """Stand-in for the generated `Err` wrapper -- `.value` holds the error union member."""

        def __init__(self) -> None:
            self.value = _ErrorBackend()

    def _raiser(*_args: Any, **_kwargs: Any) -> Any:
        raise _KvError()

    wit_world = sys.modules["wit_world"]
    monkeypatch.setattr(wit_world.imports.kv, op, _raiser)


# -- transform() parsing -------------------------------------------------------


@pytest.mark.parametrize("text", ["!raffle", "!RAFFLE", "  !raffle  "])
def test_bare_raffle_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeExtras
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "enter"


@pytest.mark.parametrize("text", ["!enter", "!ENTER", "  !enter  "])
def test_bare_enter_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeExtras
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "enter"


def test_enter_with_trailing_args_is_usage(fake_host: _FakeExtras) -> None:
    result = _run(transform(_sample_event("!enter please")))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("verb", ["open", "close", "draw"])
def test_domain_verbs_match(verb: str, fake_host: _FakeExtras) -> None:
    result = _run(transform(_sample_event(f"!raffle {verb}")))
    assert result is not None
    assert result.payload["command"] == verb


@pytest.mark.parametrize("verb", ["open", "close", "draw"])
def test_domain_verbs_with_trailing_args_are_usage(verb: str, fake_host: _FakeExtras) -> None:
    result = _run(transform(_sample_event(f"!raffle {verb} extra")))
    assert result is not None
    assert result.payload["command"] == "usage"


def test_raffle_list_matches(fake_host: _FakeExtras) -> None:
    result = _run(transform(_sample_event("!raffle list")))
    assert result is not None
    assert result.payload["command"] == "list"


def test_raffle_list_with_trailing_args_is_usage(fake_host: _FakeExtras) -> None:
    result = _run(transform(_sample_event("!raffle list extra")))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize(
    "text", ["!raffle enable ai", "!raffle bogus", "!raffle set x", "!raffle reset"]
)
def test_unsupported_verbs_and_bad_grammar_reply_usage(text: str, fake_host: _FakeExtras) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!raffled", "raffle", "!entering", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host: _FakeExtras) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_string_text_is_ignored(fake_host: _FakeExtras) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": None, "channel_id": "1"},
        occurred_at="",
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(fake_host: _FakeExtras) -> None:
    fake_host.flag_enabled = False
    assert _run(transform(_sample_event("!raffle"))) is None
    assert _run(transform(_sample_event("!enter"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeExtras) -> None:
    result = _run(transform(_sample_event("!raffle open", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeExtras) -> None:
    result = _run(transform(_sample_event("!raffle")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# -- _resolve_raffle() / _map_parsed() direct unit coverage ---------------------


def test_resolve_raffle_bare_is_enter() -> None:
    assert _resolve_raffle("") == "enter"


@pytest.mark.parametrize("verb", ["open", "close", "draw"])
def test_resolve_raffle_domain_verb(verb: str) -> None:
    assert _resolve_raffle(verb) == verb


def test_map_parsed_unimplemented_verb_is_usage() -> None:
    parsed = ParsedCommand(command="raffle", sub_module=None, option="sub", args=None)
    assert _map_parsed(parsed) == "usage"


# -- !raffle open -------------------------------------------------------------


def test_open_allowed_for_mod_clears_entrants_and_marks_open(
    fake_host: _FakeExtras, fake_kv: FakeKvHost
) -> None:
    envelope = _sample_envelope("twitch", "open", is_mod=True)
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "open"
    assert json.loads(fake_kv.store[_scoped("raffle.entrants")].decode()) == []
    assert fake_kv.store[_scoped("raffle.state")] == b"open"
    provider, message_json = fake_host.relay_calls[-1]
    assert "open" in json.loads(message_json)["text"]


def test_open_clears_prior_entrants(fake_host: _FakeExtras, fake_kv: FakeKvHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "enter"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))

    assert result.detail == "open"
    assert json.loads(fake_kv.store[_scoped("raffle.entrants")].decode()) == []


def test_open_rejected_for_non_mod(fake_host: _FakeExtras, fake_kv: FakeKvHost) -> None:
    envelope = _sample_envelope("twitch", "open", is_mod=False, is_broadcaster=False)
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "open:denied"
    assert _scoped("raffle.state") not in fake_kv.store
    provider, message_json = fake_host.relay_calls[-1]
    assert "only moderators/broadcasters" in json.loads(message_json)["text"]


def test_open_rejected_when_role_signal_entirely_absent(fake_host: _FakeExtras) -> None:
    """Discord today has no is_mod/is_broadcaster at all -- must deny, never implicitly allow."""
    envelope = _sample_envelope("discord", "open")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "open:denied"
    denial_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "raffle.permission_denied"]
    assert denial_logs


# -- !raffle close -------------------------------------------------------------


def test_close_allowed_for_broadcaster_marks_closed(
    fake_host: _FakeExtras, fake_kv: FakeKvHost
) -> None:
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    result = _run(
        dispatch(
            _sample_envelope("twitch", "close", is_mod=False, is_broadcaster=True),
            {},
            http_client=None,
        )
    )

    assert result.detail == "close"
    assert fake_kv.store[_scoped("raffle.state")] == b"closed"


def test_close_does_not_clear_entrants(fake_host: _FakeExtras, fake_kv: FakeKvHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "enter"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "close", is_mod=True), {}, http_client=None))

    entrants = json.loads(fake_kv.store[_scoped("raffle.entrants")].decode())
    assert len(entrants) == 1


def test_close_rejected_for_non_mod(fake_host: _FakeExtras) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "close", is_mod=False, is_broadcaster=False),
            {},
            http_client=None,
        )
    )
    assert result.detail == "close:denied"


# -- !raffle / !enter (enter) ---------------------------------------------------


def test_enter_rejected_when_raffle_never_opened(
    fake_host: _FakeExtras, fake_kv: FakeKvHost
) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "enter"), {}, http_client=None))
    assert result.detail == "enter"
    provider, message_json = fake_host.relay_calls[-1]
    assert "no raffle is open" in json.loads(message_json)["text"]
    assert _scoped("raffle.entrants") not in fake_kv.store


def test_enter_rejected_when_closed(fake_host: _FakeExtras) -> None:
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "close", is_mod=True), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "enter"), {}, http_client=None))

    assert result.detail == "enter"
    provider, message_json = fake_host.relay_calls[-1]
    assert "no raffle is open" in json.loads(message_json)["text"]


def test_enter_succeeds_when_open_and_persists_pseudonym(
    fake_host: _FakeExtras, fake_kv: FakeKvHost
) -> None:
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "enter"), {}, http_client=None))

    assert result.detail == "enter"
    pseudonym = _expected_pseudonym("viewer-1")
    entrants = json.loads(fake_kv.store[_scoped("raffle.entrants")].decode())
    assert entrants == [pseudonym]
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "entered the raffle" in text
    assert "viewer-1" in text  # the CALLER's own live username IS rendered, never logged


def test_duplicate_entry_is_ignored_but_still_replies(
    fake_host: _FakeExtras, fake_kv: FakeKvHost
) -> None:
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "enter"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "enter"), {}, http_client=None))

    assert result.detail == "enter"
    entrants = json.loads(fake_kv.store[_scoped("raffle.entrants")].decode())
    assert entrants == [_expected_pseudonym("viewer-1")]  # not duplicated
    provider, message_json = fake_host.relay_calls[-1]
    assert "already entered" in json.loads(message_json)["text"]


def test_different_actors_both_enter_independently(
    fake_host: _FakeExtras, fake_kv: FakeKvHost
) -> None:
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "enter", actor="viewer-1"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "enter", actor="viewer-2"), {}, http_client=None))

    entrants = json.loads(fake_kv.store[_scoped("raffle.entrants")].decode())
    assert set(entrants) == {_expected_pseudonym("viewer-1"), _expected_pseudonym("viewer-2")}


def test_enter_full_raffle_is_rejected(fake_host: _FakeExtras, fake_kv: FakeKvHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    full = json.dumps([f"p{i}" for i in range(MAX_ENTRANTS)]).encode()
    fake_kv.store[_scoped("raffle.entrants")] = full

    result = _run(dispatch(_sample_envelope("twitch", "enter"), {}, http_client=None))
    assert result.detail == "enter"
    provider, message_json = fake_host.relay_calls[-1]
    assert "full" in json.loads(message_json)["text"]
    unchanged = json.loads(fake_kv.store[_scoped("raffle.entrants")].decode())
    assert len(unchanged) == MAX_ENTRANTS


def test_different_communities_have_independent_raffles(fake_host: _FakeExtras) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "open", community="comm-1", is_mod=True),
            {},
            http_client=None,
        )
    )
    result = _run(
        dispatch(_sample_envelope("twitch", "enter", community="comm-2"), {}, http_client=None)
    )
    assert result.detail == "enter"
    provider, message_json = fake_host.relay_calls[-1]
    assert "no raffle is open" in json.loads(message_json)["text"]  # comm-2 was never opened


# -- !raffle draw ----------------------------------------------------------------


def test_draw_with_no_entrants_replies_empty_and_does_not_raise(fake_host: _FakeExtras) -> None:
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "draw", is_mod=True), {}, http_client=None))

    assert result.detail == "draw"
    provider, message_json = fake_host.relay_calls[-1]
    assert "no entrants yet" in json.loads(message_json)["text"]


def test_draw_never_opened_also_replies_empty(fake_host: _FakeExtras) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "draw", is_mod=True), {}, http_client=None))
    assert result.detail == "draw"
    provider, message_json = fake_host.relay_calls[-1]
    assert "no entrants yet" in json.loads(message_json)["text"]


def test_draw_picks_the_only_entrant(fake_host: _FakeExtras) -> None:
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "enter"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "draw", is_mod=True), {}, http_client=None))

    assert result.detail == "draw"
    pseudonym = _expected_pseudonym("viewer-1")
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert pseudonym[:8] in text
    assert "winner" in text


def test_draw_uses_seeded_random_choice(
    fake_host: _FakeExtras, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_pick_winner` is a thin `random.choice` wrapper -- deterministic under a seeded fake."""
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "enter", actor="viewer-1"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "enter", actor="viewer-2"), {}, http_client=None))

    expected = _expected_pseudonym("viewer-2")
    monkeypatch.setattr("app.random.choice", lambda seq: expected)
    result = _run(dispatch(_sample_envelope("twitch", "draw", is_mod=True), {}, http_client=None))

    assert result.detail == "draw"
    provider, message_json = fake_host.relay_calls[-1]
    assert expected[:8] in json.loads(message_json)["text"]


def test_draw_does_not_require_closed_state(fake_host: _FakeExtras) -> None:
    """Draw works on a still-OPEN raffle -- the mod's own call, module docstring."""
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "enter"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "draw", is_mod=True), {}, http_client=None))
    assert result.detail == "draw"


def test_draw_does_not_clear_entrants(fake_host: _FakeExtras, fake_kv: FakeKvHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "enter"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "draw", is_mod=True), {}, http_client=None))

    entrants = json.loads(fake_kv.store[_scoped("raffle.entrants")].decode())
    assert len(entrants) == 1


def test_draw_rejected_for_non_mod(fake_host: _FakeExtras) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "draw", is_mod=False, is_broadcaster=False),
            {},
            http_client=None,
        )
    )
    assert result.detail == "draw:denied"


def test_pick_winner_returns_a_member_of_the_list() -> None:
    entrants = ["a", "b", "c"]
    assert _pick_winner(entrants) in entrants


# -- !raffle list -------------------------------------------------------------


def test_list_before_any_open(fake_host: _FakeExtras) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "closed" in text
    assert "0 entrant" in text


def test_list_after_open_and_entries(fake_host: _FakeExtras) -> None:
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "enter", actor="viewer-1"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "enter", actor="viewer-2"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))

    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "open" in text
    assert "2 entrant" in text


def test_list_is_open_to_any_caller_even_without_role_signal(fake_host: _FakeExtras) -> None:
    result = _run(dispatch(_sample_envelope("discord", "list"), {}, http_client=None))
    assert result.detail == "list"


# -- usage -----------------------------------------------------------------


def test_usage_command_replies_with_usage_text(fake_host: _FakeExtras) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "usage"), {}, http_client=None))
    assert result.detail == "usage"
    provider, message_json = fake_host.relay_calls[-1]
    assert "Usage" in json.loads(message_json)["text"]


# -- dispatch() input validation -------------------------------------------------


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeExtras) -> None:
    envelope = _sample_envelope("twitch", "enter", community=None)
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeExtras) -> None:
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.raffle",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={"command": "enter", "channel_id": None},
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_on_unrecognized_command(fake_host: _FakeExtras) -> None:
    envelope = _sample_envelope("twitch", "not-a-real-command")
    with pytest.raises(ValueError, match="unrecognized raffle command"):
        _run(dispatch(envelope, {}, http_client=None))


# -- kv failure: fail loud, never silent ---------------------------------------


def test_open_replies_and_raises_on_kv_set_error(
    fake_kv: FakeKvHost, fake_host: _FakeExtras, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_kv_raises(monkeypatch, op="set")
    with pytest.raises(RuntimeError, match="kv set failed"):
        _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))

    provider, message_json = fake_host.relay_calls[-1]
    assert "unavailable" in json.loads(message_json)["text"]
    error_logs = [(lvl, m) for lvl, m, _f in fake_host.log_calls if m == "raffle.kv_error"]
    assert error_logs


def test_enter_replies_and_raises_on_kv_get_error(
    fake_kv: FakeKvHost, fake_host: _FakeExtras, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_kv_raises(monkeypatch, op="get")
    with pytest.raises(RuntimeError, match="kv get failed"):
        _run(dispatch(_sample_envelope("twitch", "enter"), {}, http_client=None))

    provider, message_json = fake_host.relay_calls[-1]
    assert "unavailable" in json.loads(message_json)["text"]


# -- corrupt stored state: fail loud, never silently reset -----------------------


def test_corrupt_entrants_json_fails_loud(fake_host: _FakeExtras, fake_kv: FakeKvHost) -> None:
    fake_kv.store[_scoped("raffle.entrants")] = b"{not json"
    with pytest.raises(RuntimeError, match="corrupt state"):
        _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))

    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "raffle.state_corrupt"]
    assert corrupt_logs
    provider, message_json = fake_host.relay_calls[-1]
    assert "corrupted" in json.loads(message_json)["text"]


def test_entrants_not_a_json_array_fails_loud(fake_host: _FakeExtras, fake_kv: FakeKvHost) -> None:
    fake_kv.store[_scoped("raffle.entrants")] = json.dumps({"not": "a list"}).encode()
    with pytest.raises(RuntimeError, match="corrupt state"):
        _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))


def test_entrants_with_non_string_item_fails_loud(
    fake_host: _FakeExtras, fake_kv: FakeKvHost
) -> None:
    fake_kv.store[_scoped("raffle.entrants")] = json.dumps(["ok", 123]).encode()
    with pytest.raises(RuntimeError, match="corrupt state"):
        _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))


def test_unexpected_state_value_is_treated_as_closed(
    fake_host: _FakeExtras, fake_kv: FakeKvHost
) -> None:
    fake_kv.store[_scoped("raffle.state")] = b"something-unexpected"
    result = _run(dispatch(_sample_envelope("twitch", "enter"), {}, http_client=None))
    assert result.detail == "enter"
    provider, message_json = fake_host.relay_calls[-1]
    assert "no raffle is open" in json.loads(message_json)["text"]


# -- PII: never leak the raw actor into kv keys or logs -------------------------


def test_pseudonym_is_a_non_reversible_hash_not_the_raw_actor() -> None:
    assert _pseudonym("viewer-1") == _expected_pseudonym("viewer-1")
    assert "viewer-1" not in _pseudonym("viewer-1")


def test_pseudonym_differs_per_actor() -> None:
    assert _pseudonym("viewer-1") != _pseudonym("viewer-2")
    assert _pseudonym(None) == _pseudonym(None)  # anonymous is stable, still never raw


def test_dispatch_never_logs_the_raw_actor(fake_host: _FakeExtras) -> None:
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "enter"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "actor" not in json.loads(fields_json)


def test_entrants_store_only_pseudonyms_never_raw_actor(
    fake_host: _FakeExtras, fake_kv: FakeKvHost
) -> None:
    _run(dispatch(_sample_envelope("twitch", "open", is_mod=True), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "enter"), {}, http_client=None))
    entrants = json.loads(fake_kv.store[_scoped("raffle.entrants")].decode())
    assert entrants == [_expected_pseudonym("viewer-1")]
    assert "viewer-1" not in entrants


# -- _caller_role_signal() unit coverage -----------------------------------------


def test_caller_role_signal_absent_is_none() -> None:
    assert _caller_role_signal({}) is None


def test_caller_role_signal_true_from_mod() -> None:
    assert _caller_role_signal({"is_mod": True}) is True


def test_caller_role_signal_false_when_both_false() -> None:
    assert _caller_role_signal({"is_mod": False, "is_broadcaster": False}) is False
