"""`app.py` -- Quart application factory, startup/shutdown lifecycle, gRPC bootstrap.

Covers the two lightweight REST routes registered directly on `app.py`
(`/api/v1/status`, `/api/v1/health`), the `startup()`/`shutdown()`
`before_serving`/`after_serving` hooks (mocking every service constructor
so no real DB/Redis/license-server connection is attempted), and
`_run_grpc_server`'s thread-target body (mocking the gRPC server/event
loop so it never actually blocks on `run_forever()`).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app as app_module


class TestStatusAndHealthRoutes:
    @pytest.mark.asyncio
    async def test_status_route(self) -> None:
        async with app_module.app.test_client() as client:
            resp = await client.get("/api/v1/status")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["data"]["module"] == app_module.Config.MODULE_NAME

    @pytest.mark.asyncio
    async def test_health_route(self) -> None:
        async with app_module.app.test_client() as client:
            resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["data"]["healthy"] is True


class TestStartup:
    @pytest.mark.asyncio
    async def test_startup_wires_all_services(self) -> None:
        fake_async_dal = MagicMock()
        fake_async_dal.dal = MagicMock()
        fake_license_service = MagicMock()
        fake_license_service.connect = AsyncMock()
        fake_permission_service = MagicMock()
        fake_workflow_service = MagicMock()
        fake_workflow_engine = MagicMock()
        fake_thread_instance = MagicMock()

        with (
            patch.object(app_module, "init_database", return_value=fake_async_dal),
            patch.object(app_module, "LicenseService", return_value=fake_license_service),
            patch.object(app_module, "PermissionService", return_value=fake_permission_service),
            patch.object(app_module, "WorkflowService", return_value=fake_workflow_service),
            patch(
                "services.workflow_engine.WorkflowEngine",
                return_value=fake_workflow_engine,
            ),
            patch.object(app_module, "register_workflow_api") as mock_register_workflow,
            patch.object(app_module, "register_execution_api") as mock_register_execution,
            patch.object(app_module.threading, "Thread", return_value=fake_thread_instance),
        ):
            await app_module.startup()

        assert app_module.app.config["dal"] is fake_async_dal.dal
        assert app_module.app.config["permission_service"] is fake_permission_service
        assert app_module.app.config["workflow_service"] is fake_workflow_service
        assert app_module.app.config["workflow_engine"] is fake_workflow_engine
        fake_license_service.connect.assert_awaited_once()
        mock_register_workflow.assert_called_once()
        mock_register_execution.assert_called_once()
        fake_thread_instance.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_startup_grpc_thread_failure_does_not_fail_startup(self) -> None:
        fake_async_dal = MagicMock()
        fake_async_dal.dal = MagicMock()
        fake_license_service = MagicMock()
        fake_license_service.connect = AsyncMock()

        with (
            patch.object(app_module, "init_database", return_value=fake_async_dal),
            patch.object(app_module, "LicenseService", return_value=fake_license_service),
            patch.object(app_module, "PermissionService", return_value=MagicMock()),
            patch.object(app_module, "WorkflowService", return_value=MagicMock()),
            patch("services.workflow_engine.WorkflowEngine", return_value=MagicMock()),
            patch.object(app_module, "register_workflow_api"),
            patch.object(app_module, "register_execution_api"),
            patch.object(app_module.threading, "Thread", side_effect=Exception("thread boom")),
        ):
            await app_module.startup()  # must not raise -- REST API still works

    @pytest.mark.asyncio
    async def test_startup_reraises_on_database_failure(self) -> None:
        with (
            patch.object(app_module, "init_database", side_effect=Exception("db down")),
            pytest.raises(Exception, match="db down"),
        ):
            await app_module.startup()


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_stops_grpc_and_disconnects_license(self) -> None:
        fake_grpc_server = MagicMock()
        fake_grpc_server.stop = AsyncMock()
        fake_workflow_engine = MagicMock()
        fake_license_service = MagicMock()
        fake_license_service.disconnect = AsyncMock()

        app_module.grpc_server = fake_grpc_server
        app_module.workflow_engine = fake_workflow_engine
        app_module.license_service = fake_license_service
        app_module.dal = MagicMock()
        try:
            await app_module.shutdown()
        finally:
            app_module.grpc_server = None
            app_module.workflow_engine = None
            app_module.license_service = None
            app_module.dal = None

        fake_grpc_server.stop.assert_awaited_once_with(0)
        fake_workflow_engine.shutdown.assert_called_once()
        fake_license_service.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_swallows_grpc_stop_errors(self) -> None:
        fake_grpc_server = MagicMock()
        fake_grpc_server.stop = AsyncMock(side_effect=Exception("stop failed"))
        app_module.grpc_server = fake_grpc_server
        try:
            await app_module.shutdown()  # must not raise
        finally:
            app_module.grpc_server = None

    @pytest.mark.asyncio
    async def test_shutdown_noop_when_nothing_initialized(self) -> None:
        app_module.grpc_server = None
        app_module.workflow_engine = None
        app_module.license_service = None
        app_module.dal = None
        await app_module.shutdown()  # must not raise

    @pytest.mark.asyncio
    async def test_shutdown_logs_and_swallows_unexpected_errors(self) -> None:
        broken_engine = MagicMock()
        broken_engine.shutdown = MagicMock(side_effect=Exception("shutdown boom"))
        app_module.workflow_engine = broken_engine
        try:
            await app_module.shutdown()  # outer try/except logs, does not raise
        finally:
            app_module.workflow_engine = None


class TestRunGrpcServer:
    def test_run_grpc_server_starts_and_serves(self) -> None:
        fake_loop = MagicMock()
        fake_server = MagicMock()
        fake_server.start = AsyncMock()

        with (
            patch("asyncio.new_event_loop", return_value=fake_loop),
            patch("asyncio.set_event_loop"),
            patch("grpc.aio.server", return_value=fake_server),
            patch("flask_core.grpc_tls.bind_secure_port") as mock_bind,
            patch("flask_core.grpc_tls.default_server_options", return_value=[]),
        ):
            app_module._run_grpc_server(
                workflow_service=MagicMock(),
                permission_service=MagicMock(),
                workflow_engine=MagicMock(),
                secret_key="secret",
                grpc_port=50999,
            )

        mock_bind.assert_called_once()
        fake_loop.run_until_complete.assert_called_once()
        fake_loop.run_forever.assert_called_once()

    def test_run_grpc_server_logs_and_swallows_startup_failure(self) -> None:
        with patch("grpc.aio.server", side_effect=Exception("grpc init failed")):
            app_module._run_grpc_server(
                workflow_service=MagicMock(),
                permission_service=MagicMock(),
                workflow_engine=MagicMock(),
                secret_key="secret",
                grpc_port=50998,
            )  # must not raise -- caught and logged
