"""Tests for `waddle_sdk.flask_core.bundle_runtime`."""

from __future__ import annotations

import asyncio

import pytest

from waddle_sdk.db import AsyncDB
from waddle_sdk.flask_core.bundle_runtime import (
    BundleRuntimeError,
    bundle_context,
    get_bundle_context,
    get_bundle_dal,
    raw_sql_rows,
    raw_sql_write,
    set_bundle_dal,
)


def test_get_bundle_dal_raises_when_unbound() -> None:
    """Calling get_bundle_dal() before set_bundle_dal() raises BundleRuntimeError."""
    with pytest.raises(BundleRuntimeError):
        get_bundle_dal()


def test_set_and_get_bundle_dal() -> None:
    """set_bundle_dal() binds exactly the object get_bundle_dal() returns."""
    sentinel = object()
    set_bundle_dal(sentinel)  # type: ignore[arg-type]
    assert get_bundle_dal() is sentinel


def test_bundle_context_scopes_correctly() -> None:
    """bundle_context() binds and then clears BundleContext around its block."""
    with pytest.raises(BundleRuntimeError):
        get_bundle_context()
    with bundle_context(tenant="acme", community="main", app_id="waddles.core.example.echo") as ctx:
        assert ctx.tenant == "acme"
        assert get_bundle_context().community == "main"
    with pytest.raises(BundleRuntimeError):
        get_bundle_context()


def test_raw_sql_rows_raises_not_implemented() -> None:
    """raw_sql_rows() is an explicit, named NotImplementedError gap (D21)."""
    with pytest.raises(NotImplementedError, match="raw_sql_rows"):
        asyncio.run(raw_sql_rows(AsyncDB(), "SELECT 1"))


def test_raw_sql_write_raises_not_implemented() -> None:
    """raw_sql_write() is an explicit, named NotImplementedError gap (D21)."""
    with pytest.raises(NotImplementedError, match="raw_sql_write"):
        asyncio.run(raw_sql_write(AsyncDB(), "DELETE FROM x"))
