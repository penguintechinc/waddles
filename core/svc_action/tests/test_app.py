"""svc-action app.py boot test -- health blueprint wiring + lifespan-triggered runner startup."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest


@pytest.fixture
def client() -> Any:
    from app import app as quart_app

    return quart_app.test_client()


@pytest.fixture(autouse=True)
def _reset_bundle_dal() -> Any:
    """`set_bundle_dal()` binds a process-wide singleton -- never leak it across test modules."""
    from flask_core import reset_bundle_dal_for_tests

    yield
    reset_bundle_dal_for_tests()


class TestHealthEndpoints:
    async def test_health(self, client: Any) -> None:
        """flask_core's health blueprint wiring boots and reports healthy.

        Deliberately does not trigger `before_serving`/`after_serving`
        (which open real Valkey/DB connections and start the poll loop) --
        plain `test_client()` request dispatch never fires them, matching
        core/svc_streaming's own scaffold boot-test pattern. `TestLifespan`
        below covers the lifespan-triggered path.
        """
        response = await client.get("/health")
        assert response.status_code == 200
        data = await response.get_json()
        assert data["status"] == "healthy"
        assert data["module"] == "svc-action"

    async def test_healthz(self, client: Any) -> None:
        """Assert `/healthz` returns 200 with mocked, deterministic `psutil` readings.

        `/healthz` (`flask_core.api_utils`) derives from real host CPU/memory via
        `psutil` -- mock both so this assertion is deterministic regardless of host
        load. Without this, `psutil.cpu_percent` reading a transient spike (e.g. a
        heavily loaded shared dev/CI host with unrelated concurrent processes) trips
        the >95% threshold and flips this test to a flaky, intermittent 503 with no
        relation to svc-action's own health. regression: gh-314
        """
        with (
            patch("flask_core.api_utils.psutil.virtual_memory") as mock_vmem,
            patch("flask_core.api_utils.psutil.cpu_percent", return_value=10.0),
        ):
            mock_vmem.return_value.percent = 10.0
            response = await client.get("/healthz")
            assert response.status_code == 200

    async def test_healthz_reports_503_when_resources_degraded(self, client: Any) -> None:
        """Regression: the degraded-resource branch of `/healthz` must still 503.

        Pinned via mocked `psutil` (independent of real host state) so this can't
        silently bitrot into an always-200 endpoint while `test_healthz` above is
        also mocked healthy. regression: gh-314
        """
        with (
            patch("flask_core.api_utils.psutil.virtual_memory") as mock_vmem,
            patch("flask_core.api_utils.psutil.cpu_percent", return_value=10.0),
        ):
            mock_vmem.return_value.percent = 95.0
            response = await client.get("/healthz")
            assert response.status_code == 503
            body = await response.get_json()
            assert body["status"] == "degraded"

    async def test_metrics(self, client: Any) -> None:
        response = await client.get("/metrics")
        assert response.status_code == 200


class TestLifespan:
    async def test_startup_wires_runner_and_shutdown_stops_it_cleanly(self) -> None:
        """`app.test_app()` (not `test_client()`) is Quart's lifespan-triggering context manager.

        Proves the real `startup()`/`shutdown()` wiring (poller, Valkey
        client, DAL, `ActionRunner`, background `run_forever()` task) boots
        and tears down cleanly -- the distribution poll itself fails
        against no real hub-api (network-unreachable in a test sandbox),
        which `flask_core.stage_runner.BundlePoller.poll_once()` is
        designed to swallow (never raises, degrades to an empty bundle
        set) -- exactly mirrors `core/svc_process/tests/test_app.py`'s own
        lifespan test.
        """
        from app import app as quart_app

        async with quart_app.test_app() as test_app:
            client = test_app.test_client()
            response = await client.get("/health")
            assert response.status_code == 200
            assert quart_app.config["runner"] is not None
            assert not quart_app.config["runner_task"].done()
        assert quart_app.config["runner_task"].done()

    async def test_startup_binds_dal_for_get_bundle_dal(self) -> None:
        """`startup()` calls `set_bundle_dal()`.

        An action bundle's `get_bundle_dal()` (for DB access beyond the
        audit log) must resolve to the same `AsyncDAL`
        `app.config["async_dal"]` holds.
        """
        from flask_core import get_bundle_dal

        from app import app as quart_app

        async with quart_app.test_app():
            assert quart_app.config["async_dal"] is not None
            assert get_bundle_dal() is quart_app.config["async_dal"]

    async def test_startup_constructs_reflected_asyncdb(self) -> None:
        """startup() constructs AsyncDB, calls reflect(), and binds it via set_bundle_dal()."""
        from flask_core import get_bundle_dal

        from app import app as quart_app

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
