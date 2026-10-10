"""Host-native tests for the `compliment` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/pyping/tests/test_app.py`'s own
docstring for the fake-`wit_world` approach this mirrors. Uses the shared,
charset-enforcing `waddle_sdk.testing.install_fake_kv_host` fake for the
`kv` portion of the fake host (gh-631 -- see that module's own docstring),
extended here with fake `flags`/`relay`/`log` submodules for the rest of
`wit_world.imports` this bundle also touches.
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import types
from typing import Any, cast

import pytest
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

from app import (
    _BUILTIN_COMPLIMENTS,
    _CUSTOM_NEXT_ID_KEY,
    _CUSTOM_REGISTRY_KEY,
    _LAST_COMPLIMENT_KEY,
    MAX_COMPLIMENT_LEN,
    MAX_TARGET_LEN,
    _caller_role_signal,
    _resolve,
    _validate_target,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _scoped(key: str, community: str | None = "comm-1") -> str:
    """`fake_host.kv.store` is keyed by `community_kv`'s own `c.<community>.<key>` prefix."""
    return cast(str, _scoped_key(community, key))


class _FakeHost:
    """Bundles the shared `FakeKvHost` with fake `flags`/`relay`/`log` submodules."""

    def __init__(self, kv: FakeKvHost) -> None:
        self.kv = kv
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag_state = {"enabled": True}


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    """Install the shared `FakeKvHost` (gh-631 charset-enforcing), plus flags/relay/log fakes."""
    kv_host = install_fake_kv_host(monkeypatch)
    host = _FakeHost(kv_host)

    wit_world = sys.modules["wit_world"]
    wit_world.imports.flags = types.SimpleNamespace(
        enabled=lambda key, default_value: host.flag_state["enabled"]
    )
    wit_world.imports.relay = types.SimpleNamespace(
        push=lambda provider, msg: host.relay_calls.append((provider, msg))
    )
    wit_world.imports.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: host.log_calls.append((lvl, msg, fields_json)),
    )
    return host


def _last_reply_text(host: _FakeHost) -> str:
    _provider, message_json = host.relay_calls[-1]
    return cast(str, json.loads(message_json)["text"])


def _event(
    text: str,
    *,
    platform: str = "twitch",
    actor: str | None = "viewer-1",
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
    channel_id: str | None = "12345",
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": channel_id}
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at="2026-10-07T00:00:00.000Z",
    )


def _envelope(
    command: str,
    *,
    platform: str = "twitch",
    community: str | None = "comm-1",
    actor: str | None = "viewer-1",
    arg: str | None = None,
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
    channel_id: str | None = "12345",
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": channel_id}
    if arg is not None:
        payload["arg"] = arg
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.compliment",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor=actor,
            payload=payload,
            occurred_at="2026-10-07T00:00:00.000Z",
        ),
        ts="2026-10-07T00:00:00.000Z",
    )


# ---------------------------------------------------------------------------
# transform(): matching / flag / grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["!compliment", "!COMPLIMENT", "  !compliment  "])
def test_bare_compliment_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == "tell"
    assert "arg" not in result.payload


def test_compliment_with_target_forwards_target_as_arg(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!compliment penguin")))
    assert result is not None
    assert result.payload["command"] == "tell"
    assert result.payload["arg"] == "penguin"


def test_compliment_with_at_prefixed_target_forwards_raw_arg(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!compliment @penguin")))
    assert result is not None
    assert result.payload["command"] == "tell"
    assert result.payload["arg"] == "@penguin"


def test_compliment_multiword_target_is_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!compliment penguin tux")))
    assert result is not None
    assert result.payload["command"] == "usage"


def test_compliment_add_forwards_arg(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!compliment add you are great")))
    assert result is not None
    assert result.payload["command"] == "add"
    assert result.payload["arg"] == "you are great"


@pytest.mark.parametrize(
    "text", ["!compliment enable ai", "!compliment set x", "!compliment reset", "!compliment list"]
)
def test_unsupported_verbs_reply_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!complimentary", "compliment", "!compliments", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host: _FakeHost) -> None:
    assert _run(transform(_event(text))) is None


def test_non_string_text_is_ignored(fake_host: _FakeHost) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": None, "channel_id": "1"},
        occurred_at="",
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(fake_host: _FakeHost) -> None:
    fake_host.flag_state["enabled"] = False
    assert _run(transform(_event("!compliment"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!compliment add hi", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_event("!compliment")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# ---------------------------------------------------------------------------
# _resolve() direct unit coverage
# ---------------------------------------------------------------------------


def test_resolve_empty_is_tell_none() -> None:
    assert _resolve("") == ("tell", None)


def test_resolve_single_token_target_is_tell() -> None:
    assert _resolve("penguin") == ("tell", "penguin")


def test_resolve_add_forwards_args() -> None:
    assert _resolve("add nice work") == ("add", "nice work")


def test_resolve_unknown_verb_is_usage() -> None:
    assert _resolve("set x") == ("usage", None)


# ---------------------------------------------------------------------------
# _validate_target() direct unit coverage
# ---------------------------------------------------------------------------


def test_validate_target_strips_leading_at() -> None:
    assert _validate_target("@penguin") == ("penguin", None)


def test_validate_target_rejects_blank() -> None:
    cleaned, err = _validate_target("   @   ")
    assert cleaned is None
    assert err is not None


def test_validate_target_rejects_overlong() -> None:
    cleaned, err = _validate_target("x" * (MAX_TARGET_LEN + 1))
    assert cleaned is None
    assert "or fewer" in str(err)


def test_validate_target_rejects_bad_chars() -> None:
    cleaned, err = _validate_target("pen guin!")
    assert cleaned is None
    assert err is not None


# ---------------------------------------------------------------------------
# bare !compliment -> tell (self-addressed)
# ---------------------------------------------------------------------------


def test_tell_bare_addresses_the_caller_and_posts_a_builtin(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("tell", actor="viewer-1"), {}, http_client=None))
    assert result.detail == "tell"
    text = _last_reply_text(fake_host)
    assert text.startswith("viewer-1, ")
    assert text.split(", ", 1)[1] in _BUILTIN_COMPLIMENTS
    assert _scoped(_LAST_COMPLIMENT_KEY) in fake_host.kv.store


def test_tell_bare_falls_back_to_default_when_actor_absent(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("tell", actor=None), {}, http_client=None))
    assert result.detail == "tell"
    text = _last_reply_text(fake_host)
    assert text.startswith("friend, ")


def test_tell_never_immediately_repeats(monkeypatch: pytest.MonkeyPatch, fake_host: _FakeHost) -> None:
    monkeypatch.setattr("random.choice", lambda candidates: candidates[0])

    _run(dispatch(_envelope("tell"), {}, http_client=None))
    first_last = fake_host.kv.store[_scoped(_LAST_COMPLIMENT_KEY)].decode()

    _run(dispatch(_envelope("tell"), {}, http_client=None))
    second_last = fake_host.kv.store[_scoped(_LAST_COMPLIMENT_KEY)].decode()

    assert first_last != second_last


def test_tell_with_single_pool_entry_still_replies(
    monkeypatch: pytest.MonkeyPatch, fake_host: _FakeHost
) -> None:
    monkeypatch.setattr("app._BUILTIN_COMPLIMENTS", (_BUILTIN_COMPLIMENTS[0],))
    _run(dispatch(_envelope("tell"), {}, http_client=None))
    result = _run(dispatch(_envelope("tell"), {}, http_client=None))
    assert result.detail == "tell"
    assert _BUILTIN_COMPLIMENTS[0] in _last_reply_text(fake_host)


def test_tell_draws_from_custom_pool_too(
    monkeypatch: pytest.MonkeyPatch, fake_host: _FakeHost
) -> None:
    _run(dispatch(_envelope("add", arg="a custom one", is_mod=True), {}, http_client=None))
    seen_refs: list[str] = []
    original_choice = random.choice

    def _spy(candidates: list[tuple[str, str]]) -> tuple[str, str]:
        seen_refs.extend(ref for ref, _ in candidates)
        return original_choice(candidates)

    monkeypatch.setattr("app.random.choice", _spy)
    _run(dispatch(_envelope("tell"), {}, http_client=None))
    assert "c1" in seen_refs


def test_different_communities_have_independent_last_compliment(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("tell", community="comm-1"), {}, http_client=None))
    _run(dispatch(_envelope("tell", community="comm-2"), {}, http_client=None))
    assert _scoped(_LAST_COMPLIMENT_KEY, "comm-1") in fake_host.kv.store
    assert _scoped(_LAST_COMPLIMENT_KEY, "comm-2") in fake_host.kv.store


def test_tell_corrupt_custom_registry_fails_loud(fake_host: _FakeHost) -> None:
    fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)] = b"not json"
    with pytest.raises(RuntimeError, match="compliment load_registry failed"):
        _run(dispatch(_envelope("tell"), {}, http_client=None))
    assert "unavailable" in _last_reply_text(fake_host)
    assert any(m == "compliment.kv_error" for _lvl, m, _f in fake_host.log_calls)


# ---------------------------------------------------------------------------
# !compliment <user> -> tell (targeted)
# ---------------------------------------------------------------------------


def test_tell_with_target_addresses_the_target(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("tell", arg="penguin"), {}, http_client=None))
    assert result.detail == "tell"
    assert _last_reply_text(fake_host).startswith("penguin, ")


def test_tell_with_at_prefixed_target_strips_at(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("tell", arg="@penguin"), {}, http_client=None))
    assert _last_reply_text(fake_host).startswith("penguin, ")


def test_tell_with_invalid_target_returns_error_without_kv_mutation(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("tell", arg="bad name!"), {}, http_client=None))
    assert result.detail == "tell"
    assert "may only contain" in _last_reply_text(fake_host)
    assert _scoped(_LAST_COMPLIMENT_KEY) not in fake_host.kv.store


def test_tell_with_overlong_target_returns_error(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(_envelope("tell", arg="x" * (MAX_TARGET_LEN + 1)), {}, http_client=None)
    )
    assert result.detail == "tell"
    assert "or fewer" in _last_reply_text(fake_host)


# ---------------------------------------------------------------------------
# !compliment add
# ---------------------------------------------------------------------------


def test_add_creates_custom_compliment_with_sequential_id(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(_envelope("add", arg="first compliment", is_mod=True), {}, http_client=None)
    )
    assert result.detail == "add"
    assert "Added compliment #1" in _last_reply_text(fake_host)
    registry = json.loads(fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)])
    assert registry == {"1": "first compliment"}

    _run(dispatch(_envelope("add", arg="second compliment", is_mod=True), {}, http_client=None))
    registry = json.loads(fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)])
    assert registry == {"1": "first compliment", "2": "second compliment"}


def test_add_requires_moderator_or_broadcaster(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _envelope("add", arg="nope", is_mod=False, is_broadcaster=False), {}, http_client=None
        )
    )
    assert result.detail == "add:denied"
    assert _scoped(_CUSTOM_REGISTRY_KEY) not in fake_host.kv.store


def test_add_allowed_for_broadcaster_without_mod(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _envelope("add", arg="ok", is_mod=False, is_broadcaster=True), {}, http_client=None
        )
    )
    assert result.detail == "add"


def test_add_rejected_when_role_signal_entirely_absent(fake_host: _FakeHost) -> None:
    """Discord today has no is_mod/is_broadcaster at all -- must deny, never implicitly allow."""
    result = _run(dispatch(_envelope("add", arg="nope"), {}, http_client=None))
    assert result.detail == "add:denied"
    denial_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "compliment.permission_denied"]
    assert denial_logs


def test_add_without_text_returns_usage(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("add", arg=None, is_mod=True), {}, http_client=None))
    assert result.detail == "add"
    assert _last_reply_text(fake_host) == "Usage: !compliment add <text>"


def test_add_rejects_overlong_text(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _envelope("add", arg="x" * (MAX_COMPLIMENT_LEN + 1), is_mod=True), {}, http_client=None
        )
    )
    assert result.detail == "add"
    assert "or fewer" in _last_reply_text(fake_host)


def test_add_kv_failure_is_loud(fake_host: _FakeHost) -> None:
    kv_ns = sys.modules["wit_world"].imports.kv
    original_increment = kv_ns.increment

    def _raise_increment(key: str, delta: int, ttl: int) -> int:
        raise RuntimeError("backend down")

    kv_ns.increment = _raise_increment
    try:
        with pytest.raises(RuntimeError, match="compliment add failed"):
            _run(dispatch(_envelope("add", arg="x", is_mod=True), {}, http_client=None))
    finally:
        kv_ns.increment = original_increment
    assert any(m == "compliment.kv_error" for _lvl, m, _f in fake_host.log_calls)


def test_add_save_kv_failure_is_loud(fake_host: _FakeHost) -> None:
    kv_ns = sys.modules["wit_world"].imports.kv
    original_set = kv_ns.set

    def _raise(key: str, value: bytes, ttl: int) -> None:
        if key == _scoped(_CUSTOM_REGISTRY_KEY):
            raise RuntimeError("backend down")
        return original_set(key, value, ttl)

    kv_ns.set = _raise
    try:
        with pytest.raises(RuntimeError, match="compliment add_save failed"):
            _run(dispatch(_envelope("add", arg="x", is_mod=True), {}, http_client=None))
    finally:
        kv_ns.set = original_set


# ---------------------------------------------------------------------------
# usage / dispatch validation / tenant-wide sentinel
# ---------------------------------------------------------------------------


def test_usage_command_replies_with_usage_text(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("usage"), {}, http_client=None))
    assert result.detail == "usage"


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope("tell", channel_id=None), {}, http_client=None))


def test_dispatch_raises_on_unrecognized_command(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="unrecognized compliment command"):
        _run(dispatch(_envelope("not-a-real-command"), {}, http_client=None))


def test_none_community_is_a_valid_tenant_wide_sentinel_not_rejected(
    fake_host: _FakeHost,
) -> None:
    """Unlike `joke`/`wheel`, `None` community must NOT raise -- it's `community_kv`'s own

    tenant-wide sentinel (scoped under the literal `"0"` segment), alpha's only activation
    shape today (one static scope per `svc-ingest-rust` pod).
    """
    result = _run(dispatch(_envelope("tell", community=None), {}, http_client=None))
    assert result.detail == "tell"
    assert _scoped(_LAST_COMPLIMENT_KEY, None) in fake_host.kv.store


# ---------------------------------------------------------------------------
# _caller_role_signal() unit coverage
# ---------------------------------------------------------------------------


def test_caller_role_signal_absent_is_none() -> None:
    assert _caller_role_signal({}) is None


def test_caller_role_signal_true_from_mod() -> None:
    assert _caller_role_signal({"is_mod": True}) is True


def test_caller_role_signal_false_when_both_false() -> None:
    assert _caller_role_signal({"is_mod": False, "is_broadcaster": False}) is False


# ---------------------------------------------------------------------------
# kv key charset (gh-631) -- regression
# ---------------------------------------------------------------------------


def test_kv_key_constants_are_colon_free() -> None:
    for key in (_CUSTOM_REGISTRY_KEY, _CUSTOM_NEXT_ID_KEY, _LAST_COMPLIMENT_KEY):
        assert ":" not in key
        # Regression guard: the shared fake validates the exact host charset (gh-631), so a
        # key that would be host-rejected raises here too, never only in production.
        FakeKvHost().get(key)


# ---------------------------------------------------------------------------
# fail-loud: every kv op, corrupt-store shapes, grammar errors never leak the input
# ---------------------------------------------------------------------------


def _fail_kv_op(attr: str, *, only_for_key: str | None = None) -> Any:
    """Make one `kv` host op raise (optionally only for `only_for_key`); returns a restore fn."""
    kv_ns = sys.modules["wit_world"].imports.kv
    original = getattr(kv_ns, attr)

    def _raise(key: str, *args: Any) -> Any:
        if only_for_key is None or key == only_for_key:
            raise RuntimeError("backend down")
        return original(key, *args)

    setattr(kv_ns, attr, _raise)
    return lambda: setattr(kv_ns, attr, original)


def test_tell_kv_get_failure_on_registry_is_loud(fake_host: _FakeHost) -> None:
    restore = _fail_kv_op("get")
    try:
        with pytest.raises(RuntimeError, match="compliment load_registry failed"):
            _run(dispatch(_envelope("tell"), {}, http_client=None))
    finally:
        restore()
    assert "unavailable" in _last_reply_text(fake_host)
    assert any(m == "compliment.kv_error" for _lvl, m, _f in fake_host.log_calls)


def test_tell_kv_get_failure_on_last_ref_is_loud(fake_host: _FakeHost) -> None:
    restore = _fail_kv_op("get", only_for_key=_scoped(_LAST_COMPLIMENT_KEY))
    try:
        with pytest.raises(RuntimeError, match="compliment get_last failed"):
            _run(dispatch(_envelope("tell"), {}, http_client=None))
    finally:
        restore()
    assert "unavailable" in _last_reply_text(fake_host)


def test_tell_kv_set_failure_on_last_ref_is_loud(fake_host: _FakeHost) -> None:
    restore = _fail_kv_op("set", only_for_key=_scoped(_LAST_COMPLIMENT_KEY))
    try:
        with pytest.raises(RuntimeError, match="compliment set_last failed"):
            _run(dispatch(_envelope("tell"), {}, http_client=None))
    finally:
        restore()
    assert "unavailable" in _last_reply_text(fake_host)
    assert _scoped(_LAST_COMPLIMENT_KEY) not in fake_host.kv.store


@pytest.mark.parametrize(
    "bad_registry",
    [json.dumps(["not", "an", "object"]), json.dumps({"1": 2}), json.dumps({"1": None}), "\"str\""],
    ids=["list", "non-str-value", "null-value", "bare-string"],
)
def test_corrupt_registry_shapes_fail_loud_and_are_never_reset(
    bad_registry: str, fake_host: _FakeHost
) -> None:
    fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)] = bad_registry.encode()
    with pytest.raises(RuntimeError, match="compliment load_registry failed"):
        _run(dispatch(_envelope("tell"), {}, http_client=None))
    with pytest.raises(RuntimeError, match="compliment add failed"):
        _run(dispatch(_envelope("add", arg="x", is_mod=True), {}, http_client=None))
    assert fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)] == bad_registry.encode()


def test_corrupt_registry_invalid_utf8_fails_loud(fake_host: _FakeHost) -> None:
    fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)] = b"\xff\xfe\x00"
    with pytest.raises(RuntimeError, match="compliment load_registry failed"):
        _run(dispatch(_envelope("tell"), {}, http_client=None))


@pytest.mark.parametrize(
    "text",
    [
        "!compliment enable",
        "!compliment disable",
        "!compliment disable PIIREST_a PIIREST_b",
        "!compliment enable ai",
        "!compliment list",
        "!compliment set x",
    ],
)
def test_toggle_and_unimplemented_verbs_reply_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


# regression: gh-674 -- the grammar-error DEBUG log must carry only the exception class,
# never the raw user-typed remainder (it used to log `rest=rest, error=str(exc)`).
def test_grammar_error_log_carries_only_the_exception_class(fake_host: _FakeHost) -> None:
    _run(transform(_event("!compliment disable PIIREST_a PIIREST_b")))
    grammar_logs = [f for _lvl, m, f in fake_host.log_calls if m == "compliment.invalid_grammar"]
    assert len(grammar_logs) == 1
    assert json.loads(grammar_logs[0]) == {"error": "CommandUsageError"}


# ---------------------------------------------------------------------------
# PII-free logs (gh-674)
# ---------------------------------------------------------------------------

_PII_ACTOR = "PIIACTOR_alice"
_PII_TARGET = "PIITARGET_bob"
_PII_TEXT = "PIITEXT_secret_phrase"


def _roundtrip(text: str, **role: bool) -> Any:
    """Run `transform()` then `dispatch()` -- the real two-stage path -- with PII sentinels."""
    event = _event(text, actor=_PII_ACTOR, **role)
    out = _run(transform(event))
    assert out is not None
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.compliment",
        stage="action",
        event=out,
        ts="2026-10-07T00:00:00.000Z",
    )
    return _run(dispatch(envelope, {}, http_client=None))


def _assert_logs_pii_free(host: _FakeHost, *sentinels: str) -> int:
    """Assert no sentinel appears in any recorded log call; return how many were examined."""
    assert host.log_calls, "no log calls recorded -- the PII check would pass vacuously"
    for _lvl, message, fields_json in host.log_calls:
        blob = f"{message} {fields_json}".lower()
        for sentinel in sentinels:
            assert sentinel.lower() not in blob
    return len(host.log_calls)


# regression: gh-674 -- no actor, `<user>` target or typed text may reach any log call.
def test_logs_never_contain_actor_target_or_typed_text(fake_host: _FakeHost) -> None:
    _roundtrip("!compliment")  # self-addressed: actor lands in the reply only
    _roundtrip(f"!compliment @{_PII_TARGET}")
    _roundtrip(f"!compliment {_PII_TARGET}!!")  # invalid target chars -> error reply
    _roundtrip(f"!compliment {_PII_TARGET} {_PII_TEXT}")  # multi-word -> usage
    _roundtrip(f"!compliment add {_PII_TEXT}", is_mod=True)  # applied
    _roundtrip(f"!compliment add {_PII_TEXT}")  # denied: no role signal
    _roundtrip(f"!compliment add {_PII_TEXT}", is_mod=False, is_broadcaster=False)  # denied
    _roundtrip(f"!compliment disable {_PII_TARGET} {_PII_TEXT}")  # grammar error
    assert actor_in_reply(fake_host)  # sanity: the actor is in a reply, not in a log
    assert _assert_logs_pii_free(fake_host, _PII_ACTOR, _PII_TARGET, _PII_TEXT) >= 12


def actor_in_reply(host: _FakeHost) -> bool:
    return any(_PII_ACTOR in json.loads(msg)["text"] for _prov, msg in host.relay_calls)


# regression: gh-674 -- error-path logs carry the host error text, never the typed text.
def test_kv_error_logs_never_contain_actor_target_or_typed_text(fake_host: _FakeHost) -> None:
    restore = _fail_kv_op("increment")
    try:
        with pytest.raises(RuntimeError, match="compliment add failed"):
            _roundtrip(f"!compliment add {_PII_TEXT}", is_mod=True)
    finally:
        restore()
    fake_host.kv.store[_scoped(_CUSTOM_REGISTRY_KEY)] = b"not json"
    with pytest.raises(RuntimeError, match="compliment load_registry failed"):
        _roundtrip(f"!compliment @{_PII_TARGET}")
    assert sum(1 for _lvl, m, _f in fake_host.log_calls if m == "compliment.kv_error") == 2
    _assert_logs_pii_free(fake_host, _PII_ACTOR, _PII_TARGET, _PII_TEXT)
