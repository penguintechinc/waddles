"""`services/analytics_service.py` -- unit tests for the pure envelope-unwrapping helpers.

`tests/test_v1_analytics_blueprint.py` exercises `_unwrap`/`_relay`
indirectly through every route's happy/failure path; this file closes the
gap on `_unwrap`'s defensive fallback branches (a non-enveloped dict body,
and a non-dict/`None` body) that no route naturally produces since
`analytics-core` always returns a proper envelope in practice.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.analytics_proxy import ProxyResult
from services.analytics_service import _unwrap, _upstream_error, reputation_stats


class TestUnwrap:
    def test_enveloped_dict_unwraps_data_key(self) -> None:
        assert _unwrap({"success": True, "data": {"total_users": 5}}) == {"total_users": 5}

    def test_non_enveloped_dict_passes_through(self) -> None:
        """No `data` key -- Node's `data.data || data` falls back to the body itself."""
        assert _unwrap({"total_users": 5}) == {"total_users": 5}

    def test_none_body_is_empty_dict(self) -> None:
        assert _unwrap(None) == {}

    def test_non_dict_body_is_empty_dict(self) -> None:
        assert _unwrap("not a dict") == {}


class _EmptyDal:
    """Fake async DAL answering as an EMPTY `bundle_reputation_scores` table."""

    async def executesql_async(self, sql: str) -> list[Any]:
        if sql.startswith("SELECT COUNT(*)"):
            return [(0, None, None, None)]  # aggregates over zero rows are NULL
        return []


@pytest.mark.asyncio
async def test_reputation_stats_empty_table_is_zeros_not_error() -> None:
    result = await reputation_stats(_EmptyDal())
    assert result == {
        "stats": {"total": 0, "avg": 0.0, "median": 0.0, "min": 0, "max": 0},
        "histogram": [],
    }


class TestUpstreamError:
    def test_url_unset_is_503_unavailable(self) -> None:
        body, status = _upstream_error(
            ProxyResult(
                ok=False,
                status_code=503,
                body={"error": "service_unavailable", "service": "analytics-core"},
            )
        )
        assert status == 503
        assert body["success"] is False
        assert body["error"]["code"] == "ANALYTICS_UNAVAILABLE"

    def test_transport_error_is_503_unavailable(self) -> None:
        body, status = _upstream_error(
            ProxyResult(ok=False, status_code=502, body=None, transport_error=True)
        )
        assert (status, body["error"]["code"]) == (503, "ANALYTICS_UNAVAILABLE")

    def test_upstream_500_stays_a_real_502(self) -> None:
        body, status = _upstream_error(ProxyResult(ok=False, status_code=500, body=None))
        assert (status, body["error"]["code"]) == (502, "ANALYTICS_UPSTREAM_ERROR")
