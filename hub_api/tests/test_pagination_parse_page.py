"""`parse_page`: explicit validation, typed 400 on bad input, no exception-driven default."""

from __future__ import annotations

import pytest

from services.errors import ApiError
from services.pagination import parse_page


@pytest.mark.parametrize(("raw", "expected"), [(None, 1), ("", 1), ("1", 1), ("7", 7), ("0", 1)])
def test_missing_empty_and_valid_values(raw: str | None, expected: int) -> None:
    assert parse_page(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "-1", "1.5", " 2", "1e3", "9" * 10, "٣"])
def test_bad_values_raise_a_typed_400(raw: str) -> None:
    with pytest.raises(ApiError) as caught:
        parse_page(raw)
    assert caught.value.status_code == 400
    assert caught.value.code == "BAD_REQUEST"
