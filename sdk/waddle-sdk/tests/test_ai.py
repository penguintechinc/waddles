"""Tests for `waddle_sdk.ai` (the `ai.generate` host capability)."""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

import waddle_sdk.ai as ai
from waddle_sdk.testing import (
    FakeAiHost,
    _Error_Backend,
    _Error_ContentFiltered,
    _Error_Denied,
    _Error_Invalid,
    _Error_TooManyTokens,
    _Error_Unavailable,
    _FakeWitError,
    install_fake_ai_host,
)


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("ai coroutine unexpectedly suspended")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeAiHost:
    return install_fake_ai_host(monkeypatch)


# --- happy path --------------------------------------------------------------


def test_generate_returns_the_completion_and_forwards_the_options(host: FakeAiHost) -> None:
    host.reply = "Welcome aboard!"
    out = _run(ai.generate("say hi", max_tokens=64, temperature=0.25, json=True))
    assert out == "Welcome aboard!"
    assert host.calls == [("say hi", 64, 0.25, True)]


def test_defaults_pass_none_options_and_json_false(host: FakeAiHost) -> None:
    _run(ai.generate("say hi"))
    assert host.calls == [("say hi", None, None, False)]


def test_an_integer_temperature_is_sent_as_a_float(host: FakeAiHost) -> None:
    _run(ai.generate("x", temperature=1))
    assert host.calls[0][2] == 1.0
    assert isinstance(host.calls[0][2], float)


# --- fail-loud on every host refusal (never a swallowed empty completion) ----


def test_gate_denial_raises_denied_error_with_the_code(host: FakeAiHost) -> None:
    host.granted = False
    with pytest.raises(ai.DeniedError) as ei:
        _run(ai.generate("x"))
    assert ei.value.code == "not_granted"
    assert host.calls == []


def test_pii_is_rejected_with_contains_pii_never_redacted(host: FakeAiHost) -> None:
    with pytest.raises(ai.DeniedError) as ei:
        _run(ai.generate("mail alice@example.com"))
    assert ei.value.code == "contains_pii"
    assert host.calls == []


def test_the_rate_limit_is_ten_calls_then_rate_limited(host: FakeAiHost) -> None:
    for _ in range(10):
        _run(ai.generate("x"))
    with pytest.raises(ai.DeniedError) as ei:
        _run(ai.generate("x"))
    assert ei.value.code == "rate_limited"
    assert len(host.calls) == 10


def test_an_over_bound_prompt_carries_the_prompt_bound(host: FakeAiHost) -> None:
    with pytest.raises(ai.TooManyTokensError) as ei:
        _run(ai.generate("a" * (ai.MAX_PROMPT_TOKENS * 3 + 1)))
    assert ei.value.bound == ai.MAX_PROMPT_TOKENS


def test_an_empty_completion_is_content_filtered(host: FakeAiHost) -> None:
    host.reply = "  "
    with pytest.raises(ai.ContentFilteredError):
        _run(ai.generate("x"))


@pytest.mark.parametrize(
    ("variant", "exc_type"),
    [
        (_Error_Denied("quota_exceeded"), ai.DeniedError),
        (_Error_TooManyTokens(512), ai.TooManyTokensError),
        (_Error_ContentFiltered(), ai.ContentFilteredError),
        (_Error_Invalid("bad"), ai.InvalidAiArgError),
        (_Error_Unavailable("model timed out"), ai.UnavailableError),
        (_Error_Backend("boom"), ai.BackendError),
    ],
)
def test_every_wit_error_variant_maps_to_its_exception(
    monkeypatch: pytest.MonkeyPatch, variant: object, exc_type: type
) -> None:
    def boom(*_a: object) -> str:
        raise _FakeWitError(variant)

    fake = types.ModuleType("wit_world")
    fake.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        ai=types.SimpleNamespace(generate=boom, Options=lambda **kw: types.SimpleNamespace(**kw))
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake)
    with pytest.raises(exc_type):
        _run(ai.generate("x"))


def test_the_payload_of_a_variant_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object) -> str:
        raise _FakeWitError(_Error_TooManyTokens(2000))

    fake = types.ModuleType("wit_world")
    fake.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        ai=types.SimpleNamespace(generate=boom, Options=lambda **kw: types.SimpleNamespace(**kw))
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake)
    with pytest.raises(ai.TooManyTokensError) as ei:
        _run(ai.generate("x"))
    assert ei.value.bound == 2000


def test_an_unclassifiable_host_exception_is_a_backend_error_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: object) -> str:
        raise RuntimeError("wasm trap")

    fake = types.ModuleType("wit_world")
    fake.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        ai=types.SimpleNamespace(generate=boom, Options=lambda **kw: types.SimpleNamespace(**kw))
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake)
    with pytest.raises(ai.BackendError):
        _run(ai.generate("x"))


# --- argument validation happens before any host call ------------------------


@pytest.mark.parametrize("bad", ["", "   ", None, 5, b"x", ["x"]])
def test_bad_prompt_is_rejected_before_any_host_call(bad: object) -> None:
    with pytest.raises(ai.InvalidAiArgError):
        _run(ai.generate(bad))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, 1.5, "3", True])
def test_bad_max_tokens_is_rejected_before_any_host_call(bad: object) -> None:
    with pytest.raises((ai.InvalidAiArgError, ai.TooManyTokensError)):
        _run(ai.generate("x", max_tokens=bad))  # type: ignore[arg-type]


def test_max_tokens_over_the_bound_is_too_many_tokens_before_any_host_call() -> None:
    with pytest.raises(ai.TooManyTokensError) as ei:
        _run(ai.generate("x", max_tokens=ai.MAX_COMPLETION_TOKENS + 1))
    assert ei.value.bound == ai.MAX_COMPLETION_TOKENS
    assert ai.validate_max_tokens(ai.MAX_COMPLETION_TOKENS) == ai.MAX_COMPLETION_TOKENS
    assert ai.validate_max_tokens(None) is None


@pytest.mark.parametrize("bad", [-0.1, 2.5, float("nan"), float("inf"), "0.5", True])
def test_bad_temperature_is_rejected_before_any_host_call(bad: object) -> None:
    with pytest.raises(ai.InvalidAiArgError):
        _run(ai.generate("x", temperature=bad))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_non_bool_json_is_rejected_before_any_host_call(bad: object) -> None:
    with pytest.raises(ai.InvalidAiArgError):
        _run(ai.generate("x", json=bad))  # type: ignore[arg-type]


# --- eager import (componentize-py wizening) ---------------------------------


def test_module_eagerly_attempts_the_wit_binding_import_at_load() -> None:
    """The module-level `from wit_world.imports import ai` is what forces wizening.

    Host-side there is no `wit_world`, so the guarded import leaves the
    sentinel `None` -- but the attempt itself must exist at module top level
    (not inside a function), proven by reading the source.
    """
    source = Path(ai.__file__).read_text()
    needle = "    from wit_world.imports import ai"
    top_level_imports = [line for line in source.splitlines() if line.startswith(needle)]
    assert top_level_imports, "eager top-level import of wit_world.imports.ai is missing"
    assert hasattr(ai, "_ai_binding")


def test_component_entry_eager_import_list_includes_ai() -> None:
    entry = Path(ai.__file__).with_name("_component_entry.py").read_text()
    assert '"ai"' in entry


def test_verify_script_pins_every_ai_binding_shape() -> None:
    script = (Path(ai.__file__).parents[2] / "scripts" / "verify_wit_bindings.sh").read_text()
    for needle in (
        "def generate(prompt: str, opts: Options) -> str:",
        "class Options:",
        "class Error_TooManyTokens:",
        "class Error_ContentFiltered:",
        "    max_tokens: Optional[int]",
        "    temperature: Optional[float]",
        "    json: bool",
    ):
        assert needle in script, needle


# --- cross-language pin to the real stage ------------------------------------

_STAGE_AI_RS = Path(__file__).resolve().parents[3] / "core" / "svc_process" / "src" / "ai.rs"


def test_limits_match_the_stage_ai_rs() -> None:
    """Pins the token/temperature bounds to `core/svc_process/src/ai.rs`."""
    rust = _STAGE_AI_RS.read_text()
    for name, mirrored in (
        ("MAX_PROMPT_TOKENS: u32", ai.MAX_PROMPT_TOKENS),
        ("MAX_COMPLETION_TOKENS: u32", ai.MAX_COMPLETION_TOKENS),
    ):
        m = re.search(rf"pub const {name} = ([\d_]+);", rust)
        assert m, f"{name} not found in ai.rs"
        assert int(m.group(1).replace("_", "")) == mirrored, name
    m = re.search(r"pub const MAX_TEMPERATURE: f64 = ([\d.]+);", rust)
    assert m, "MAX_TEMPERATURE not found in ai.rs"
    assert float(m.group(1)) == ai.MAX_TEMPERATURE


def test_wire_op_matches_the_executor_and_stage() -> None:
    """The op name the SDK's WIT import produces is the one the stage dispatches on."""
    root = Path(__file__).resolve().parents[3]
    executor = (root / "core" / "bundle_executor" / "src" / "host" / "stage_next_ai.rs").read_text()
    stage = (root / "core" / "svc_process" / "src" / "capabilities.rs").read_text()
    assert '"ai.generate"' in executor
    assert '"ai.generate"' in stage
    assert 'starts_with("ai.")' in stage
