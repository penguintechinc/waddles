"""Tests for `waddle_sdk.reputation` (issue #726)."""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path
from uuid import uuid4

import pytest

import waddle_sdk.reputation as reputation
from waddle_sdk.testing import (
    FakeReputationHost,
    _Error_Backend,
    _Error_Denied,
    _Error_Invalid,
    _Error_Unavailable,
    _FakeWitError,
    install_fake_reputation_host,
)

MEMBER = str(uuid4())


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("reputation coroutine unexpectedly suspended")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeReputationHost:
    return install_fake_reputation_host(monkeypatch, {MEMBER})


def test_get_returns_zero_for_a_member_with_no_adjustments(host: FakeReputationHost) -> None:
    assert _run(reputation.get(MEMBER)) == 0


def test_adjust_returns_the_new_balance_and_records_a_ledger_row(host: FakeReputationHost) -> None:
    assert _run(reputation.adjust(MEMBER, 3, "game.win")) == 3
    assert _run(reputation.adjust(MEMBER, -2, "game.loss")) == 1
    assert _run(reputation.get(MEMBER)) == 1
    assert host.ledger == [(MEMBER, 3, "game.win"), (MEMBER, -2, "game.loss")]


def test_user_is_normalized_to_canonical_lowercase_uuid(host: FakeReputationHost) -> None:
    assert _run(reputation.adjust(MEMBER.upper(), 1, "r")) == 1
    assert host.ledger[0][0] == MEMBER


# --- fail-loud on every host error (never a swallowed default) -------------


def test_gate_denial_raises_denied_error_with_the_gate_code(host: FakeReputationHost) -> None:
    host.granted = False
    with pytest.raises(reputation.DeniedError) as ei:
        _run(reputation.adjust(MEMBER, 1, "r"))
    assert ei.value.code == "not_granted"
    with pytest.raises(reputation.DeniedError):
        _run(reputation.get(MEMBER))


def test_out_of_bounds_delta_is_a_denial(host: FakeReputationHost) -> None:
    with pytest.raises(reputation.DeniedError) as ei:
        _run(reputation.adjust(MEMBER, 6, "r"))
    assert ei.value.code == "delta_out_of_bounds"
    assert host.ledger == []


def test_non_member_raises_not_a_member(host: FakeReputationHost) -> None:
    with pytest.raises(reputation.NotAMemberError):
        _run(reputation.adjust(str(uuid4()), 1, "r"))


def test_daily_cap_raises_daily_cap_exceeded(host: FakeReputationHost) -> None:
    _run(reputation.adjust(MEMBER, 5, "a"))
    with pytest.raises(reputation.DailyCapExceededError):
        _run(reputation.adjust(MEMBER, 1, "b"))
    assert _run(reputation.get(MEMBER)) == 5


@pytest.mark.parametrize(
    ("variant", "exc_type"),
    [
        (_Error_Invalid("bad"), reputation.InvalidReputationArgError),
        (_Error_Unavailable("not wired"), reputation.UnavailableError),
        (_Error_Backend("db down"), reputation.BackendError),
    ],
)
def test_remaining_wit_error_variants_map_to_their_exceptions(
    monkeypatch: pytest.MonkeyPatch, variant: object, exc_type: type
) -> None:
    def boom(*_a: object) -> int:
        raise _FakeWitError(variant)

    fake = types.ModuleType("wit_world")
    fake.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        reputation=types.SimpleNamespace(get=boom, adjust=boom)
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake)
    with pytest.raises(exc_type):
        _run(reputation.adjust(MEMBER, 1, "r"))
    with pytest.raises(exc_type):
        _run(reputation.get(MEMBER))


def test_denied_error_carries_variant_payload_as_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_a: object) -> int:
        raise _FakeWitError(_Error_Denied("quota_exceeded"))

    fake = types.ModuleType("wit_world")
    fake.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        reputation=types.SimpleNamespace(get=deny, adjust=deny)
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake)
    with pytest.raises(reputation.DeniedError) as ei:
        _run(reputation.adjust(MEMBER, 1, "r"))
    assert ei.value.code == "quota_exceeded"


# --- argument validation happens before any host call ----------------------


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "1234", None, 5])
def test_bad_user_is_rejected_before_any_host_call(bad: object) -> None:
    with pytest.raises(reputation.InvalidReputationArgError):
        _run(reputation.get(bad))  # type: ignore[arg-type]
    with pytest.raises(reputation.InvalidReputationArgError):
        _run(reputation.adjust(bad, 1, "r"))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [2**31, -(2**31) - 1, 1.5, "3", True, None])
def test_bad_delta_is_rejected_before_any_host_call(bad: object) -> None:
    with pytest.raises(reputation.InvalidReputationArgError):
        _run(reputation.adjust(MEMBER, bad, "r"))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad", ["", "Has Upper", "has space", "user@example.com", "a" * 101, "semi;colon", None]
)
def test_bad_reason_is_rejected_before_any_host_call(bad: object) -> None:
    with pytest.raises(reputation.InvalidReputationArgError):
        _run(reputation.adjust(MEMBER, 1, bad))  # type: ignore[arg-type]


def test_valid_reason_codes_are_accepted() -> None:
    for ok in ["game.win", "loyalty:daily-bonus", "a", "x_y.z-1:2", "a" * 100]:
        assert reputation.validate_reason(ok) == ok


# --- eager import (componentize-py wizening) -------------------------------


def test_module_eagerly_attempts_the_wit_binding_import_at_load() -> None:
    """The module-level `from wit_world.imports import reputation` is what forces wizening.

    Host-side there is no `wit_world`, so the guarded import leaves the
    sentinel `None` -- but the attempt itself must exist at module top level
    (not inside a function), proven by reading the source.
    """
    source = Path(reputation.__file__).read_text()
    needle = "    from wit_world.imports import reputation"
    top_level_imports = [line for line in source.splitlines() if line.startswith(needle)]
    assert top_level_imports, "eager top-level import of wit_world.imports.reputation is missing"
    assert hasattr(reputation, "_reputation_binding")


def test_component_entry_eager_import_list_includes_reputation() -> None:
    entry = Path(reputation.__file__).with_name("_component_entry.py").read_text()
    assert '"reputation"' in entry


# --- cross-language pin to the real host crate -----------------------------

_LIB_RS = Path(__file__).resolve().parents[3] / "core" / "bundle_host_reputation" / "src" / "lib.rs"


def test_reason_rules_match_host_lib_rs() -> None:
    """Pins `MAX_REASON_LEN`/the reason charset to `core/bundle_host_reputation/src/lib.rs`."""
    rust = _LIB_RS.read_text()
    m = re.search(r"pub const MAX_REASON_LEN: usize = (\d+);", rust)
    assert m, "MAX_REASON_LEN not found in lib.rs"
    assert int(m.group(1)) == reputation.MAX_REASON_LEN
    # The accepted byte set in `validate_reason`.
    assert "b'.' | b'_' | b':' | b'-'" in rust
    assert "is_ascii_lowercase() || b.is_ascii_digit()" in rust
