"""Regression: alpha community pages (leaderboard 500 + inconsistent licensed-feature 402s).

* `$N` placeholders reached psycopg2 verbatim -> `there is no parameter $1` (HTTP 500 on
  `GET /community/<id>/leaderboard/messages`). SQLite tolerates `$N`; the unit suite never saw it.
* Every community feature gate answers 402 with ONE body (`error.code == FEATURE_NOT_ENABLED`) that
  hub-webui keys its "Upgrade to Premium" state on.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.community_activity import activity_member_bp
from blueprints.v1.community_announcements import announcements_bp
from services.community_activity import get_message_leaderboard, get_watch_time_leaderboard
from services.community_common import positional_sql
from tests.community_ids import cpath, install_community_ids


def _fake_dal(engine: str) -> Any:
    """Minimal stand-in exposing only the adapter's `dbengine`."""
    return SimpleNamespace(_adapter=SimpleNamespace(dbengine=engine))


class TestPositionalSql:
    def test_postgres_gets_percent_s_and_no_dollar_markers(self) -> None:
        sql, values = positional_sql(
            _fake_dal("postgres"), "SELECT 1 WHERE a = $1 AND b >= $2 LIMIT $3", [7, 2, 5]
        )
        assert sql == "SELECT 1 WHERE a = %s AND b >= %s LIMIT %s"
        assert values == [7, 2, 5]

    def test_sqlite_gets_question_mark(self) -> None:
        sql, _ = positional_sql(_fake_dal("sqlite"), "SELECT $1", [1])
        assert sql == "SELECT ?"

    def test_reused_and_out_of_order_markers_expand_per_occurrence(self) -> None:
        sql, values = positional_sql(
            _fake_dal("postgres"), "VALUES ($2, $1, $2) ON CONFLICT SET x = $1", ["a", "b"]
        )
        assert sql == "VALUES (%s, %s, %s) ON CONFLICT SET x = %s"
        assert values == ["b", "a", "b", "a"]


class _RecordingDal:
    """Postgres-flavoured dal that records executesql calls and answers with empty result sets."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._adapter = SimpleNamespace(dbengine="postgres")
        self.calls: list[tuple[str, list[Any]]] = []

    def executesql(self, sql: str, placeholders: list[Any] | None = None) -> list[Any]:
        if "FROM activity_stats_daily" in sql:
            self.calls.append((sql, list(placeholders or [])))
            return [(0,)] if "COUNT(*)" in sql else []
        return self._inner.executesql(sql, placeholders=placeholders)  # config lookups

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.mark.parametrize(
    "fn", [get_message_leaderboard, get_watch_time_leaderboard], ids=["messages", "watch-time"]
)
def test_leaderboards_send_postgres_paramstyle_never_dollar(community_db: Any, fn: Any) -> None:
    """Regression gh alpha: the SQL handed to a Postgres driver must not contain `$N`."""
    dal, community_id = community_db
    recording = _RecordingDal(dal)

    fn(recording, community_id, period="weekly", limit=5, offset=0)

    assert len(recording.calls) == 2  # page query + count query
    for sql, values in recording.calls:
        assert "$" not in sql
        assert sql.count("%s") == len(values)
        assert values[0] == community_id


GATED = [
    (
        announcements_bp,
        "waddles.community.announcements",
        "community_announcements",
        "/api/v1/admin/{community_id}/announcements?status=published&limit=5",
        "community.announcements:read",
    ),
    (
        activity_member_bp,
        "waddles.community.activity",
        "community_activity",
        "/api/v1/community/{community_id}/leaderboard/watch-time?period=weekly&limit=5&offset=0",
        "community.activity:read",
    ),
]


@pytest.mark.parametrize(
    ("bp", "flag", "module", "path", "scope"), GATED, ids=[g[2] for g in GATED]
)
async def test_gate_off_answers_standard_licensed_feature_402(
    community_db: Any,
    auth_headers: Any,
    monkeypatch: pytest.MonkeyPatch,
    bp: Any,
    flag: str,
    module: str,
    path: str,
    scope: str,
) -> None:
    """Every gate returns 402 + `{success: false, error: {code: FEATURE_NOT_ENABLED, message}}`."""
    import importlib

    dal, community_id = community_db
    mod = importlib.import_module(f"blueprints.v1.{module}")
    gate = AsyncMock(return_value=False)
    monkeypatch.setattr(mod, "feature_enabled", gate)

    app = Quart(__name__)
    QuartSchema(app)
    install_community_ids(app, dal)
    app.register_blueprint(bp)
    app.config["dal"] = dal

    response = await app.test_client().get(
        cpath(dal, path, community_id), headers=auth_headers(scope=scope)
    )

    assert response.status_code == 402
    body = await response.get_json()
    assert body["success"] is False
    assert body["error"]["code"] == "FEATURE_NOT_ENABLED"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert gate.await_args.args[0] == flag


pytestmark = pytest.mark.usefixtures("bypass_route_guards")
