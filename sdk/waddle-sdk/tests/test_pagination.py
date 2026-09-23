"""Tests for `waddle_sdk.pagination` -- explicit NotImplementedError stubs (D21)."""

from __future__ import annotations

import pytest

from waddle_sdk.pagination import Cursor, Page


def test_page_raises_not_implemented() -> None:
    """Page() names itself in the NotImplementedError message."""
    with pytest.raises(NotImplementedError, match="Page"):
        Page()


def test_cursor_raises_not_implemented() -> None:
    """Cursor() names itself in the NotImplementedError message."""
    with pytest.raises(NotImplementedError, match="Cursor"):
        Cursor()
