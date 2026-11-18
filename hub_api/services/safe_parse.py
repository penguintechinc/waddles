"""Validate-first parsers: no try/except-return-default, so a bad value is never silently coerced.

Each parser checks the text against a strict ASCII pattern and only then converts, so the
conversion cannot raise. Env helpers treat an unset variable as "use the default" but a SET
and malformed variable as a configuration error that names the variable (never its value).
"""

from __future__ import annotations

import os
import re
from uuid import UUID

_UUID_HEX_RE = re.compile(r"[0-9a-fA-F]{32}")
_INT_RE = re.compile(r"[+-]?[0-9]+", re.ASCII)
_DECIMAL_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", re.ASCII)
_MAX_INT_DIGITS = 18


class EnvConfigError(ValueError):
    """An env tunable is set but not a valid number (the message names the variable only)."""


def parse_uuid(value: object) -> UUID | None:
    """The `UUID` for a UUID string (dashed, bare 32-hex, braced or `urn:uuid:`), else `None`.

    Accepts exactly the textual forms `uuid.UUID(str)` accepts, minus its non-hex quirks
    (underscores, `0x`, non-ASCII digits).
    """
    if not isinstance(value, str):
        return None
    text = value.strip().replace("urn:", "").replace("uuid:", "").strip("{}").replace("-", "")
    if _UUID_HEX_RE.fullmatch(text) is None:
        return None
    return UUID(hex=text)


def parse_nonneg_int(value: object, max_digits: int = _MAX_INT_DIGITS) -> int | None:
    """A non-negative decimal integer of at most `max_digits` ASCII digits, else `None`."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not (text.isascii() and text.isdigit()) or len(text) > max_digits:
        return None
    return int(text)


def parse_int(value: object, max_digits: int = _MAX_INT_DIGITS) -> int | None:
    """A signed decimal integer bounded to `max_digits` digits, else `None`."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _INT_RE.fullmatch(text) is None or len(text.lstrip("+-")) > max_digits:
        return None
    return int(text)


def parse_decimal(value: object) -> float | None:
    """A finite decimal/scientific number (no nan/inf words, no underscores), else `None`."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _DECIMAL_RE.fullmatch(text) is None:
        return None
    return float(text)


def env_int(name: str, default: int) -> int:
    """Integer env var: unset/blank -> `default`; set and malformed -> `EnvConfigError`."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    parsed = parse_int(raw)
    if parsed is None:
        raise EnvConfigError(f"{name} must be an integer")
    return parsed


def env_float(name: str, default: float) -> float:
    """Numeric env var: unset/blank -> `default`; set and malformed -> `EnvConfigError`."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    parsed = parse_decimal(raw)
    if parsed is None:
        raise EnvConfigError(f"{name} must be a number")
    return parsed
