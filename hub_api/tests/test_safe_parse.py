"""Unit tests for the validate-first parsers and the fail-loud env helpers."""

from __future__ import annotations

import pytest

from services.safe_parse import (
    EnvConfigError,
    env_float,
    env_int,
    parse_decimal,
    parse_int,
    parse_nonneg_int,
    parse_uuid,
)

_U = "123e4567-e89b-12d3-a456-426614174000"
_HEX = "123e4567e89b12d3a456426614174000"


@pytest.mark.parametrize(
    "raw",
    [_U, _U.upper(), f"  {_U}  ", _HEX, f"{{{_U}}}", f"urn:uuid:{_U}"],
)
def test_parse_uuid_accepts_the_stdlib_textual_forms(raw: str) -> None:
    parsed = parse_uuid(raw)
    assert parsed is not None and str(parsed) == _U


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "123",
        _U[:-1],
        _U + "0",
        "zz3e4567-e89b-12d3-a456-426614174000",
        "１２３e4567-e89b-12d3-a456-426614174000",
        "123e4567_e89b12d3a456426614174000",
        "0x" + _HEX[2:],
        _U + "\x00",
        None,
        5,
        b"x",
    ],
)
def test_parse_uuid_rejects_everything_else(raw: object) -> None:
    assert parse_uuid(raw) is None


@pytest.mark.parametrize(("raw", "out"), [("0", 0), ("42", 42), (" 7 ", 7), ("0012", 12)])
def test_parse_nonneg_int_valid(raw: str, out: int) -> None:
    assert parse_nonneg_int(raw) == out


@pytest.mark.parametrize(
    "raw", ["", "-1", "+1", "1.5", "٣", "１２", "1_0", "9" * 19, "1\x00", None, 3]
)
def test_parse_nonneg_int_invalid(raw: object) -> None:
    assert parse_nonneg_int(raw) is None


def test_parse_nonneg_int_max_digits() -> None:
    assert parse_nonneg_int("123", max_digits=3) == 123
    assert parse_nonneg_int("1234", max_digits=3) is None


def test_parse_int_signed() -> None:
    assert parse_int("-5") == -5 and parse_int("+5") == 5
    assert parse_int("--5") is None and parse_int("５") is None and parse_int("") is None


@pytest.mark.parametrize(
    ("raw", "out"), [("5", 5.0), ("0.5", 0.5), (".5", 0.5), ("1e3", 1000.0), ("-2.", -2.0)]
)
def test_parse_decimal_valid(raw: str, out: float) -> None:
    assert parse_decimal(raw) == out


@pytest.mark.parametrize("raw", ["", "nan", "inf", "1_0", "abc", "1e", "٣", None])
def test_parse_decimal_invalid(raw: object) -> None:
    assert parse_decimal(raw) is None


def test_env_helpers_default_when_unset_or_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_TUNABLE", raising=False)
    assert env_int("X_TUNABLE", 9) == 9 and env_float("X_TUNABLE", 1.5) == 1.5
    monkeypatch.setenv("X_TUNABLE", "  ")
    assert env_int("X_TUNABLE", 9) == 9


def test_env_helpers_parse_valid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_TUNABLE", "12")
    assert env_int("X_TUNABLE", 9) == 12 and env_float("X_TUNABLE", 1.5) == 12.0


def test_env_helpers_fail_loud_without_echoing_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_TUNABLE", "s3cret-garbage")
    for fn in (env_int, env_float):
        with pytest.raises(EnvConfigError, match="X_TUNABLE") as info:
            fn("X_TUNABLE", 1)  # type: ignore[operator]
        assert "s3cret" not in str(info.value)
