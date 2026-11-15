"""Shared fixtures/helpers for the OpenTelemetry real-path tests (registered as a pytest plugin).

Provides the in-process OTLP/gRPC receiver, env/global cleanup, the real hub-api app plus a few
test-only routes, and an ASGI client factory that goes through `app.asgi_app` (never
`app.test_client()`, which bypasses the telemetry shim).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent import futures
from typing import Any

import grpc
import httpx
import pytest
from flask_core.tenancy import TenantContext
from opentelemetry import metrics, trace
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2, logs_service_pb2_grpc
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2, metrics_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc
from quart import Quart, request
from sqlalchemy import create_engine, text

from app import create_app
from tests.test_app_factory import _test_config

TENANT_ID = 7
COMMUNITY_ID = 42
PII_EMAIL = "pii-user-7f3a@example.invalid"
PII_USERNAME = "sentinel_username_x9"
# Synthetic values assembled at runtime so secret scanners never see a literal credential.
PII_TOKEN = "_".join(["tok", "SENTINEL123456"])
CLIENT_IP = "198.51.100.23"
PII_BEARER = " ".join(["Bearer", "sentinelbearer" + "0123456789abcdef"])
PII_JWT = ".".join(["eyJ" + "hbGciOiJFUzI1NiJ9", "eyJ" + "zdWIiOiJzZW50aW5lbCJ9", "c2lnbmF0dXJl"])
ITEM_ROUTE = "/api/v1/_otel/items/<int:item_id>"


# ---------------------------------------------------------------------------
# in-process OTLP/gRPC receiver
# ---------------------------------------------------------------------------


class _Receiver:
    """Collects every OTLP export request that reaches it."""

    def __init__(self) -> None:
        self.trace_requests: list[Any] = []
        self.metric_requests: list[Any] = []
        self.log_requests: list[Any] = []
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        receiver = self

        class _Trace(trace_service_pb2_grpc.TraceServiceServicer):
            def Export(self, request: Any, context: Any) -> Any:  # noqa: N802
                receiver.trace_requests.append(request)
                return trace_service_pb2.ExportTraceServiceResponse()

        class _Metrics(metrics_service_pb2_grpc.MetricsServiceServicer):
            def Export(self, request: Any, context: Any) -> Any:  # noqa: N802
                receiver.metric_requests.append(request)
                return metrics_service_pb2.ExportMetricsServiceResponse()

        class _Logs(logs_service_pb2_grpc.LogsServiceServicer):
            def Export(self, request: Any, context: Any) -> Any:  # noqa: N802
                receiver.log_requests.append(request)
                return logs_service_pb2.ExportLogsServiceResponse()

        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(_Trace(), self._server)
        metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(_Metrics(), self._server)
        logs_service_pb2_grpc.add_LogsServiceServicer_to_server(_Logs(), self._server)
        self.port = self._server.add_insecure_port("127.0.0.1:0")
        self._server.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.stop(grace=0)

    def spans(self) -> list[Any]:
        return [
            s
            for req in list(self.trace_requests)
            for rs in req.resource_spans
            for ss in rs.scope_spans
            for s in ss.spans
        ]

    def metric_points(self) -> list[tuple[str, str, Any]]:
        """``(metric name, kind, data point)`` for every exported point."""
        out: list[tuple[str, str, Any]] = []
        for req in list(self.metric_requests):
            for rm in req.resource_metrics:
                for sm in rm.scope_metrics:
                    for m in sm.metrics:
                        kind = m.WhichOneof("data")
                        if kind is not None:
                            out.extend((m.name, kind, dp) for dp in getattr(m, kind).data_points)
        return out

    def log_records(self) -> list[Any]:
        return [
            lr
            for req in list(self.log_requests)
            for rl in req.resource_logs
            for sl in rl.scope_logs
            for lr in sl.log_records
        ]

    def resources(self) -> list[dict[str, str]]:
        return [
            {a.key: a.value.string_value for a in rs.resource.attributes}
            for req in list(self.trace_requests)
            for rs in req.resource_spans
        ]

    def wire_bytes(self) -> bytes:
        """Every received request serialized -- for substring leak checks."""
        reqs = [*self.trace_requests, *self.metric_requests, *self.log_requests]
        return b"".join(r.SerializeToString() for r in reqs)


def _attrs(pb_attributes: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kv in pb_attributes:
        which = kv.value.WhichOneof("value")
        out[kv.key] = getattr(kv.value, which) if which else None
    return out


def _reset_otel_globals() -> None:
    """Allow the next test to install its own global providers (the API is set-once per process)."""
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    metrics._internal._METER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    metrics._internal._METER_PROVIDER = None  # type: ignore[attr-defined]


async def _wait_for(condition: Callable[[], bool], limit_s: float = 15.0) -> bool:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return condition()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def _clean_otel_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No OTEL_*/flag env and fresh globals for a test; restores logging after.

    Opt-in per module (`pytestmark = pytest.mark.usefixtures("_clean_otel_env")`): this file is a
    pytest plugin, so an autouse fixture here would leak into the whole suite.
    """
    import os

    for name in [
        n
        for n in os.environ
        if n.startswith("OTEL_") or n.startswith("FLAG_WADDLES_DISABLE_HUB_API")
    ]:
        monkeypatch.delenv(name, raising=False)
    root = logging.getLogger()
    before = (list(root.handlers), root.level)
    _reset_otel_globals()
    yield
    _reset_otel_globals()
    root.handlers[:] = before[0]
    root.setLevel(before[1])


@pytest.fixture
def receiver() -> Iterator[_Receiver]:
    rec = _Receiver()
    yield rec
    rec.stop()


def _enable(monkeypatch: pytest.MonkeyPatch, endpoint: str, **extra: str) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "hub-api")
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES", "deployment.environment=alpha,service.namespace=waddlebot"
    )
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "200")
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "100")
    monkeypatch.setenv("OTEL_BLRP_SCHEDULE_DELAY", "100")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def _build_app() -> Quart:
    """The real hub-api app plus a few test-only routes (before first request, so legal)."""
    app = create_app(_test_config())
    probe = logging.getLogger("hub_api.test_probe")

    @app.get("/api/v1/_otel/items/<int:item_id>")
    async def item(item_id: int) -> dict[str, int]:
        probe.info("otel-test item fetched item_id=%s", item_id)
        app.config["logger"].info("otel-test aaa line", user="42", community="42")
        return {"id": item_id}

    @app.get("/api/v1/_otel/c/<int:community_id>")
    async def community(community_id: int) -> dict[str, int]:
        ctx = TenantContext(tenant_id=TENANT_ID, tenant_slug="acme")
        request.tenant_context = ctx  # type: ignore[attr-defined]
        return {"id": community_id}

    @app.get("/api/v1/_otel/pii/<name>")
    async def pii(name: str) -> dict[str, str]:
        probe.warning("login failed for %s token=%s", PII_EMAIL, PII_TOKEN)
        app.config["logger"].info(
            f"audit for {PII_EMAIL}", user=PII_USERNAME, community="42", detail=PII_EMAIL
        )
        try:
            raise ValueError(f"no such user {PII_EMAIL}")
        except ValueError:
            probe.exception("lookup failed")
        return {"ok": "1"}

    @app.get("/api/v1/_otel/db")
    async def db() -> dict[str, int]:
        def query() -> int:
            engine = create_engine("sqlite://")  # created after instrument() -> traced
            with engine.connect() as conn:
                row = conn.execute(text("SELECT 1")).scalar_one()
            engine.dispose()
            return int(row)

        return {"one": await asyncio.to_thread(query)}

    return app


@pytest.fixture
async def client_for() -> AsyncIterator[Callable[[Quart], httpx.AsyncClient]]:
    clients: list[httpx.AsyncClient] = []

    def make(app: Quart) -> httpx.AsyncClient:
        # Through `app.__call__` -> `app.asgi_app` (the telemetry shim), NOT app.test_client().
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(CLIENT_IP, 54321)),
            base_url="http://hub.test",
        )
        clients.append(client)
        return client

    yield make
    for c in clients:
        await c.aclose()
