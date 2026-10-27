"""Unit tests for `services.activation_gate.is_app_activated`.

Integration-level proof that this gate actually changes `ProcessRunner`
dispatch (routed/not-routed, the toggle flipping live, the fail-open
defaults keeping currently-working bundles alive) lives in
`tests/test_runner.py::TestActivationGate` -- this file isolates the pure
decision function itself, independent of the runner/Valkey/distribution
plumbing.
"""

from __future__ import annotations

from typing import Any

from services.activation_gate import is_app_activated

APP_ID = "waddles.community.loyalty.default"


class _FakeDal:
    """Records every `.execute()` call it receives; answers with a caller-supplied result."""

    def __init__(self, *, rows: list[Any] | None = None, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, list[Any] | None]] = []
        self._rows = rows if rows is not None else []
        self._raises = raises

    async def execute(self, sql: str, params: list[Any] | None = None) -> list[Any]:
        self.calls.append((sql, params))
        if self._raises is not None:
            raise self._raises
        return self._rows


class TestCommunityNone:
    async def test_tenant_wide_envelope_allows_without_any_db_call(self) -> None:
        dal = _FakeDal(rows=[{"enabled": False}])  # would block if ever consulted
        assert await is_app_activated(community=None, app_id=APP_ID, dal=dal) is True
        assert dal.calls == [], "community=None must short-circuit before any DB query"


class TestUnparseableCommunity:
    async def test_non_numeric_community_allows_without_any_db_call(self) -> None:
        dal = _FakeDal(rows=[{"enabled": False}])
        assert await is_app_activated(community="not-a-number", app_id=APP_ID, dal=dal) is True
        assert dal.calls == []


class TestRowFound:
    async def test_enabled_row_allows(self) -> None:
        dal = _FakeDal(rows=[{"enabled": True}])
        assert await is_app_activated(community="42", app_id=APP_ID, dal=dal) is True
        expected_sql = (
            "SELECT enabled FROM app_activations WHERE community_id = $1 AND app_id = $2 LIMIT 1"
        )
        assert dal.calls == [(expected_sql, [42, APP_ID])]

    async def test_disabled_row_blocks(self) -> None:
        dal = _FakeDal(rows=[{"enabled": False}])
        assert await is_app_activated(community="42", app_id=APP_ID, dal=dal) is False


class TestNoRow:
    async def test_no_activation_row_fails_open(self) -> None:
        """Unonboarded app_id (no `app_activations` row at all) -- always on."""
        dal = _FakeDal(rows=[])
        assert await is_app_activated(community="42", app_id=APP_ID, dal=dal) is True


class TestDbFailure:
    async def test_db_error_fails_open(self) -> None:
        dal = _FakeDal(raises=RuntimeError("connection reset"))
        assert await is_app_activated(community="42", app_id=APP_ID, dal=dal) is True

    async def test_no_dal_bound_fails_open(self) -> None:
        """`dal=None` with nothing bound via `flask_core.set_bundle_dal()`.

        `get_bundle_dal()` raises `BundleRuntimeError`, caught the same as
        any other DB failure.
        """
        from flask_core import reset_bundle_dal_for_tests

        reset_bundle_dal_for_tests()
        assert await is_app_activated(community="42", app_id=APP_ID, dal=None) is True
