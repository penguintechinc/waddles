"""Smoke tests for svc-process's Quart app -- mirrors `core/svc_ingest/tests/test_app.py`."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from flask_core import get_bundle_dal, reset_bundle_dal_for_tests

from app import app as quart_app


@pytest.fixture
def client() -> Any:
    return quart_app.test_client()


@pytest.fixture(autouse=True)
def _reset_bundle_dal() -> Any:
    """`set_bundle_dal()` binds a process-wide singleton -- never leak it across test modules."""
    yield
    reset_bundle_dal_for_tests()


class TestHealthEndpoints:
    async def test_health(self, client: Any) -> None:
        async with client as c:
            response = await c.get("/health")
            assert response.status_code == 200
            body = await response.get_json()
            assert body["module"] == "svc-process"

    async def test_healthz(self, client: Any) -> None:
        """Assert `/healthz` returns 200 with mocked, deterministic `psutil` readings.

        `/healthz` (`flask_core.api_utils`) derives from real host CPU/memory via
        `psutil` -- mock both so this assertion is deterministic regardless of host
        load. Without this, `psutil.cpu_percent` reading a transient spike (e.g. a
        heavily loaded shared dev/CI host with unrelated concurrent processes) trips
        the >95% threshold and flips this test to a flaky, intermittent 503 with no
        relation to svc-process's own health.
        """
        with (
            patch("flask_core.api_utils.psutil.virtual_memory") as mock_vmem,
            patch("flask_core.api_utils.psutil.cpu_percent", return_value=10.0),
        ):
            mock_vmem.return_value.percent = 10.0
            async with client as c:
                response = await c.get("/healthz")
                assert response.status_code == 200

    async def test_healthz_reports_503_when_resources_degraded(self, client: Any) -> None:
        """Regression: the degraded-resource branch of `/healthz` must still 503.

        Pinned via mocked `psutil` (independent of real host state) so this can't
        silently bitrot into an always-200 endpoint while `test_healthz` above is
        also mocked healthy.
        """
        with (
            patch("flask_core.api_utils.psutil.virtual_memory") as mock_vmem,
            patch("flask_core.api_utils.psutil.cpu_percent", return_value=10.0),
        ):
            mock_vmem.return_value.percent = 95.0
            async with client as c:
                response = await c.get("/healthz")
                assert response.status_code == 503
                body = await response.get_json()
                assert body["status"] == "degraded"

    async def test_metrics(self, client: Any) -> None:
        async with client as c:
            response = await c.get("/metrics")
            assert response.status_code == 200


class TestLifespan:
    async def test_startup_wires_runner_and_shutdown_stops_it_cleanly(self) -> None:
        """`app.test_app()` (not `test_client()`) is Quart's lifespan-triggering context manager."""
        async with quart_app.test_app() as test_app:
            client = test_app.test_client()
            response = await client.get("/health")
            assert response.status_code == 200
            assert quart_app.config["runner"] is not None
            assert not quart_app.config["runner_task"].done()
        assert quart_app.config["runner_task"].done()

    async def test_startup_binds_dal_for_get_bundle_dal(self) -> None:
        """`startup()` calls `set_bundle_dal()`.

        A stateful process bundle's `get_bundle_dal()` must resolve to the
        exact same `AsyncDAL` instance `app.config["async_dal"]` holds.
        """
        async with quart_app.test_app():
            assert quart_app.config["async_dal"] is not None
            assert get_bundle_dal() is quart_app.config["async_dal"]

    async def test_startup_constructs_reflected_asyncdb(self) -> None:
        """startup() constructs AsyncDB, calls reflect(), and binds it via set_bundle_dal()."""
        reflected: list[bool] = []

        class _FakeAsyncDB:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def reflect(self) -> None:
                reflected.append(True)

            async def close_async(self) -> None:
                pass

        with (
            patch("app.AsyncDB", _FakeAsyncDB),
        ):
            async with quart_app.test_app():
                assert quart_app.config["async_dal"] is not None
                assert reflected == [True]
                assert get_bundle_dal() is quart_app.config["async_dal"]
