"""`services.sql_util` delegates `$N` rewriting to `flask_core.positional_sql.convert_positional`.

Covers the regressions the old bare regex had: markers inside literals/comments were rewritten,
`$0` silently indexed `placeholders[-1]`, and an out-of-range marker raised a bare IndexError.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from services.sql_util import PositionalSQLError, execute_positional, positional_sql


def _dal(engine: str) -> Any:
    """Minimal stand-in exposing only the adapter's `dbengine`."""
    return SimpleNamespace(_adapter=SimpleNamespace(dbengine=engine))


class _Recorder:
    """dal double that records the `executesql` call."""

    def __init__(self, engine: str) -> None:
        self._adapter = SimpleNamespace(dbengine=engine)
        self.calls: list[tuple[str, list[Any]]] = []

    def executesql(self, sql: str, placeholders: list[Any] | None = None) -> list[Any]:
        self.calls.append((sql, list(placeholders or [])))
        return [("ok",)]


class TestMarkerPerAdapter:
    @pytest.mark.parametrize(
        ("engine", "marker"),
        [("postgres", "%s"), ("sqlite", "?"), ("", "%s"), ("PostgreSQL", "%s")],
    )
    def test_marker_selected_by_dbengine(self, engine: str, marker: str) -> None:
        sql, values = positional_sql(_dal(engine), "SELECT $1, $2", [1, 2])
        assert sql == f"SELECT {marker}, {marker}"
        assert values == [1, 2]

    def test_dal_without_adapter_defaults_to_percent_s(self) -> None:
        sql, _ = positional_sql(SimpleNamespace(), "SELECT $1", [1])
        assert sql == "SELECT %s"


class TestTokenizerBehaviour:
    def test_marker_inside_string_literal_untouched(self) -> None:
        sql, values = positional_sql(_dal("postgres"), "SELECT '$1 costs' , $1", ["x"])
        assert sql == "SELECT '$1 costs' , %s"
        assert values == ["x"]

    def test_marker_inside_line_and_block_comment_untouched(self) -> None:
        sql, values = positional_sql(
            _dal("postgres"), "SELECT $1 -- uses $2\n/* also $2 */ , $2", ["a", "b"]
        )
        assert sql == "SELECT %s -- uses $2\n/* also $2 */ , %s"
        assert values == ["a", "b"]

    def test_marker_inside_dollar_quoted_body_untouched(self) -> None:
        sql, values = positional_sql(_dal("postgres"), "DO $f$ SELECT $1 $f$; SELECT $1", [9])
        assert sql == "DO $f$ SELECT $1 $f$; SELECT %s"
        assert values == [9]

    def test_repeated_marker_expands_per_occurrence(self) -> None:
        sql, values = positional_sql(_dal("postgres"), "WHERE a = $1 OR b = $1", ["v"])
        assert sql == "WHERE a = %s OR b = %s"
        assert values == ["v", "v"]

    def test_out_of_order_markers(self) -> None:
        sql, values = positional_sql(_dal("sqlite"), "VALUES ($3, $1, $2)", ["a", "b", "c"])
        assert sql == "VALUES (?, ?, ?)"
        assert values == ["c", "a", "b"]

    def test_two_digit_marker_not_confused_with_one(self) -> None:
        params = list(range(1, 11))
        sql, values = positional_sql(_dal("postgres"), "SELECT $10, $1", params)
        assert sql == "SELECT %s, %s"
        assert values == [10, 1]

    def test_unused_extra_params_are_dropped(self) -> None:
        _, values = positional_sql(_dal("postgres"), "SELECT $2", ["a", "b", "c"])
        assert values == ["b"]

    def test_no_markers_no_params(self) -> None:
        assert positional_sql(_dal("postgres"), "SELECT 1", []) == ("SELECT 1", [])


class TestErrors:
    def test_dollar_zero_raises(self) -> None:
        with pytest.raises(PositionalSQLError):
            positional_sql(_dal("postgres"), "SELECT $0", ["a"])

    def test_out_of_range_raises_positional_error_not_index_error(self) -> None:
        with pytest.raises(PositionalSQLError, match=r"\$3"):
            positional_sql(_dal("postgres"), "SELECT $1, $3", ["a", "b"])

    def test_missing_params_raises(self) -> None:
        with pytest.raises(PositionalSQLError):
            positional_sql(_dal("postgres"), "SELECT $1", [])

    def test_error_is_a_value_error_not_index_error(self) -> None:
        assert issubclass(PositionalSQLError, ValueError)
        assert not issubclass(PositionalSQLError, IndexError)

    def test_execute_positional_does_not_hit_db_on_error(self) -> None:
        dal = _Recorder("postgres")
        with pytest.raises(PositionalSQLError):
            execute_positional(dal, "SELECT $0", ["a"])
        assert dal.calls == []


class TestExecutePositional:
    @pytest.mark.parametrize(("engine", "marker"), [("postgres", "%s"), ("sqlite", "?")])
    def test_executes_converted_sql_with_expanded_values(self, engine: str, marker: str) -> None:
        dal = _Recorder(engine)
        rows = execute_positional(dal, "SELECT $2, $1, $2", ["a", "b"])
        assert rows == [("ok",)]
        assert dal.calls == [(f"SELECT {marker}, {marker}, {marker}", ["b", "a", "b"])]
