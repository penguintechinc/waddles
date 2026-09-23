"""Tests for `waddle_sdk._json_guard` -- the shared `*-json` object-shape guard."""

from __future__ import annotations

import json

import pytest

from waddle_sdk._json_guard import NonObjectJsonError, to_canonical_json_object


def test_dict_serializes_normally() -> None:
    """A `dict` value serializes to JSON exactly like `json.dumps`."""
    assert to_canonical_json_object({"a": 1, "b": "two"}) == json.dumps({"a": 1, "b": "two"})


def test_empty_dict_serializes_to_empty_object() -> None:
    """An empty dict is a valid JSON object and serializes to `{}`."""
    assert to_canonical_json_object({}) == "{}"


@pytest.mark.parametrize(
    "value",
    [["a", "list"], "a scalar string", 42, 3.14, True, None],
)
def test_non_dict_values_raise_non_object_json_error(value: object) -> None:
    """Lists and every scalar type raise -- only a `dict` is a JSON object."""
    with pytest.raises(NonObjectJsonError, match="expected a JSON object"):
        to_canonical_json_object(value)


def test_non_object_json_error_is_a_type_error() -> None:
    """NonObjectJsonError subclasses TypeError -- a shape/contract violation, not a value error."""
    assert issubclass(NonObjectJsonError, TypeError)
