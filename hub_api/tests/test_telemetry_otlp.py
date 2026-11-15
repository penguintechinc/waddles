"""Real-path OpenTelemetry tests: a real app, a real in-process OTLP/gRPC receiver, counted signals.

Per `testing.md` Telemetry Validation: zero spans / metric data points / log records received is
a FAIL and counts are printed. The receiver is a genuine `grpc.server` implementing the OTLP
Trace/Metrics/Logs services, so everything below exercises the real exporters, batch processors
and wire format -- nothing is mocked on the export path. Covers: three-signal export with
trace-correlated logs, PII scrub, a dead exporter (bounded queues, bounded latency/shutdown),
the kill switch, the endpoint-unset no-op, and gRPC server+client W3C propagation with handled
counter/histogram.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import grpc
import pytest
from opentelemetry import metrics, propagate, trace
from opentelemetry.instrumentation.grpc import GrpcAioInstrumentorClient
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

import telemetry
from grpc_internal.interceptors import OTelInterceptor
from tests.telemetry_support import (
    CLIENT_IP,
    COMMUNITY_ID,
    ITEM_ROUTE,
    PII_BEARER,
    PII_EMAIL,
    PII_JWT,
    PII_TOKEN,
    PII_USERNAME,
    TENANT_ID,
    _attrs,
    _build_app,
    _enable,
    _free_port,
    _Receiver,
    _wait_for,
)

pytestmark = pytest.mark.usefixtures("_clean_otel_env")

# ---------------------------------------------------------------------------
# three signals, correlated
# ---------------------------------------------------------------------------


async def test_real_request_exports_traces_metrics_and_correlated_logs(
    monkeypatch: pytest.MonkeyPatch, receiver: _Receiver, client_for: Any
) -> None:
    _enable(monkeypatch, receiver.endpoint)
    app = _build_app()
    async with app.test_app():
        runtime = app.config["telemetry_runtime"]
        assert runtime.enabled, runtime.reason
        client = client_for(app)
        assert (await client.get("/api/v1/_otel/items/12345")).status_code == 200
        assert (await client.get(f"/api/v1/_otel/c/{COMMUNITY_ID}")).status_code == 200
        assert (await client.get("/api/v1/_otel/db")).status_code == 200
        assert (await client.get("/health")).status_code == 200  # excluded probe
        await asyncio.to_thread(runtime.force_flush, 10.0)
        assert await _wait_for(
            lambda: (
                bool(receiver.spans())
                and bool(receiver.metric_points())
                and bool(receiver.log_records())
            )
        )

    spans = receiver.spans()
    points = receiver.metric_points()
    hist = [p for p in points if p[1] == "histogram"]
    logs = receiver.log_records()
    print(
        f"RECEIVED spans={len(spans)} metric_points={len(points)} "
        f"histogram_points={len(hist)} logs={len(logs)}"
    )
    assert len(spans) >= 1, "FAIL: zero spans received"
    assert len(points) >= 1, "FAIL: zero metric data points received"
    assert len(hist) >= 1, "FAIL: zero histogram data points received"
    assert len(logs) >= 1, "FAIL: zero log records received"

    server_spans = {s.name: s for s in spans if s.kind == 2}
    item_span = server_spans[f"GET {ITEM_ROUTE}"]
    item_attrs = _attrs(item_span.attributes)
    assert item_attrs["http.route"] == ITEM_ROUTE
    assert "12345" not in item_span.name and item_attrs["http.target"] == ITEM_ROUTE
    assert not any(s.name.endswith("/health") for s in spans), "probe route must not be traced"

    community_attrs = _attrs(server_spans["GET /api/v1/_otel/c/<int:community_id>"].attributes)
    assert (
        community_attrs["tenant_id"] == TENANT_ID
        and community_attrs["community_id"] == COMMUNITY_ID
    )

    db_spans = [s for s in spans if _attrs(s.attributes).get("db.system") == "sqlite"]
    assert db_spans, "SQLAlchemy instrumentation produced no db span"

    duration = [
        (_attrs(dp.attributes), dp)
        for name, kind, dp in points
        if name == "http.server.request.duration" and kind == "histogram"
    ]
    routes = {a["http.route"] for a, _ in duration}
    assert ITEM_ROUTE in routes and all("12345" not in r for r in routes), routes
    assert {a["http.response.status_code"] for a, _ in duration} >= {200}
    assert any(name == "http.server.active_requests" for name, _, _ in points)

    item_trace = item_span.trace_id
    matching = [
        lr
        for lr in logs
        if lr.trace_id == item_trace and "otel-test item fetched" in lr.body.string_value
    ]
    assert matching, "log record with the request's trace_id not received"
    assert matching[0].span_id != b"\x00" * 8
    aaa = [
        lr
        for lr in logs
        if lr.trace_id == item_trace and "otel-test aaa line" in lr.body.string_value
    ]
    assert aaa, "AAA (non-propagating) logger records must be exported with trace ids too"

    res = receiver.resources()[0]
    assert res["service.name"] == "hub-api" and res["deployment.environment"] == "alpha"
    assert res["service.namespace"] == "waddlebot" and res["service.version"] == "0.0.0-test"
    assert res["service.instance.id"]


# ---------------------------------------------------------------------------
# PII
# ---------------------------------------------------------------------------


async def test_pii_and_secrets_never_exported(
    monkeypatch: pytest.MonkeyPatch, receiver: _Receiver, client_for: Any
) -> None:
    _enable(monkeypatch, receiver.endpoint)
    app = _build_app()
    async with app.test_app():
        runtime = app.config["telemetry_runtime"]
        client = client_for(app)
        resp = await client.get(
            f"/api/v1/_otel/pii/{PII_USERNAME}",
            params={"email": PII_EMAIL, "token": PII_TOKEN, "code": "oauthcodeSENTINEL"},
            headers={
                "Authorization": f"{PII_BEARER}",
                "X-Forwarded-For": "203.0.113.77",
                "Cookie": f"s={PII_JWT}",
            },
        )
        assert resp.status_code == 200
        await asyncio.to_thread(runtime.force_flush, 10.0)
        assert await _wait_for(lambda: bool(receiver.spans()) and bool(receiver.log_records()))

    wire = receiver.wire_bytes()
    n_spans, n_logs = len(receiver.spans()), len(receiver.log_records())
    print(f"PII-CHECK bytes={len(wire)} spans={n_spans} logs={n_logs}")
    assert len(receiver.spans()) >= 1 and len(receiver.log_records()) >= 1
    for secret in (
        PII_EMAIL,
        PII_USERNAME,
        PII_TOKEN,
        "oauthcodeSENTINEL",
        "sentinelbearer",
        PII_JWT,
        "203.0.113.77",
    ):
        idx = wire.find(secret.encode())
        context = wire[max(0, idx - 80) : idx + 60]
        message = f"{secret!r} leaked into exported telemetry: ...{context!r}"
        assert idx < 0, message
    assert CLIENT_IP.encode() not in wire, "client address must not be exported"
    # the request was real and the scrub marker is what replaced the data
    assert b"[redacted]" in wire
    span = next(s for s in receiver.spans() if s.kind == 2)
    assert _attrs(span.attributes)["http.route"] == "/api/v1/_otel/pii/<name>"


def test_scrub_units() -> None:
    assert telemetry.scrub_text(f"hi {PII_EMAIL}") == "hi [redacted]"
    assert "SENTINEL" not in telemetry.scrub_text(
        f"GET /cb?code=oauthcodeSENTINEL&state=x {PII_BEARER}"
    )
    attrs = telemetry.scrub_attributes(
        {
            "user": "a",
            "http.request.header.authorization": "x",
            "tenant_id": 7,
            "client.address": "1.2.3.4",
            "db.system": "pg",
        }
    )
    assert attrs == {
        "user": "[redacted]",
        "http.request.header.authorization": "[redacted]",
        "tenant_id": 7,
        "db.system": "pg",
    }


# ---------------------------------------------------------------------------
# dead exporter
# ---------------------------------------------------------------------------


async def test_dead_exporter_never_breaks_startup_latency_or_memory(
    monkeypatch: pytest.MonkeyPatch, client_for: Any, caplog: pytest.LogCaptureFixture
) -> None:
    queue_cap = 64
    _enable(
        monkeypatch,
        f"http://127.0.0.1:{_free_port()}",
        OTEL_EXPORTER_OTLP_TIMEOUT="500",
        OTEL_BSP_MAX_QUEUE_SIZE=str(queue_cap),
        OTEL_BLRP_MAX_QUEUE_SIZE=str(queue_cap),
    )
    app = _build_app()
    t0 = time.monotonic()
    async with app.test_app():
        startup_seconds = time.monotonic() - t0
        runtime = app.config["telemetry_runtime"]
        assert runtime.enabled, runtime.reason
        client = client_for(app)
        latencies: list[float] = []
        for i in range(100):  # 1 span + 2 logs per request >> queue_cap, so drop-oldest must engage
            s = time.perf_counter()
            resp = await client.get(f"/api/v1/_otel/items/{i}")
            latencies.append(time.perf_counter() - s)
            assert resp.status_code == 200
        span_q = runtime.span_processor._batch_processor._queue
        log_q = runtime.log_processor._batch_processor._queue
        print(
            f"DEAD-EXPORTER startup={startup_seconds:.2f}s requests={len(latencies)} "
            f"max={max(latencies) * 1000:.0f}ms "
            f"mean={sum(latencies) / len(latencies) * 1000:.1f}ms "
            f"span_q={len(span_q)}/{span_q.maxlen} log_q={len(log_q)}/{log_q.maxlen}"
        )
        assert span_q.maxlen == queue_cap and len(span_q) <= queue_cap
        assert log_q.maxlen == queue_cap and len(log_q) <= queue_cap
        assert max(latencies) < 2.0, "a dead collector slowed a request"
        assert sum(latencies) / len(latencies) < 0.25
        s = time.monotonic()
        finished = await runtime.shutdown(3.0)
        shutdown_seconds = time.monotonic() - s
        print(f"DEAD-EXPORTER shutdown finished={finished} in {shutdown_seconds:.2f}s")
        assert shutdown_seconds < 4.5, "shutdown flush was not bounded"
    assert startup_seconds < 15.0
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR and r.name == "telemetry"]


def test_timeout_env_is_converted_from_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "5000")
    assert telemetry._otlp_timeout_seconds("TRACES") == 5.0
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "250")
    assert telemetry._otlp_timeout_seconds("TRACES") == 0.25
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "99999999")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT")
    assert telemetry._otlp_timeout_seconds("TRACES") == 30.0


# ---------------------------------------------------------------------------
# kill switch / disabled
# ---------------------------------------------------------------------------


async def test_kill_switch_on_disables_telemetry(
    monkeypatch: pytest.MonkeyPatch, receiver: _Receiver, client_for: Any
) -> None:
    _enable(monkeypatch, receiver.endpoint)
    monkeypatch.setenv("FLAG_WADDLES_DISABLE_HUB_API_OTEL", "true")
    app = _build_app()
    async with app.test_app():
        runtime = app.config["telemetry_runtime"]
        assert not runtime.enabled and "kill switch" in runtime.reason
        client = client_for(app)
        assert (await client.get("/api/v1/_otel/items/1")).status_code == 200
        await asyncio.sleep(0.5)
    got = (len(receiver.spans()), len(receiver.metric_points()), len(receiver.log_records()))
    print(f"KILL-SWITCH received spans={got[0]} metrics={got[1]} logs={got[2]}")
    assert (
        not receiver.trace_requests and not receiver.metric_requests and not receiver.log_requests
    )
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)


async def test_kill_switch_off_or_unseen_keeps_telemetry_on(
    monkeypatch: pytest.MonkeyPatch, receiver: _Receiver
) -> None:
    _enable(monkeypatch, receiver.endpoint)
    monkeypatch.setenv("FLAG_WADDLES_DISABLE_HUB_API_OTEL", "false")
    app = _build_app()
    async with app.test_app():
        assert app.config["telemetry_runtime"].enabled


async def test_endpoint_unset_is_a_single_info_line_noop(
    client_for: Any, caplog: pytest.LogCaptureFixture
) -> None:
    app = _build_app()
    with caplog.at_level(logging.INFO, logger="telemetry"):
        async with app.test_app():
            runtime = app.config["telemetry_runtime"]
            assert not runtime.enabled
            client = client_for(app)
            assert (await client.get("/api/v1/_otel/items/1")).status_code == 200
    lines = [r for r in caplog.records if "telemetry export is disabled" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert "OTEL_EXPORTER_OTLP_ENDPOINT is not set" in lines[0].getMessage()
    assert not [r for r in caplog.records if r.name == "telemetry" and r.levelno >= logging.WARNING]


async def test_unsupported_protocol_is_loud_but_never_raises(
    monkeypatch: pytest.MonkeyPatch, receiver: _Receiver
) -> None:
    _enable(monkeypatch, receiver.endpoint, OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf")
    app = _build_app()
    async with app.test_app():
        runtime = app.config["telemetry_runtime"]
        assert not runtime.enabled and "http/protobuf" in runtime.reason


def test_sampler_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "ParentBased" in telemetry.build_sampler().get_description()
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "parentbased_traceidratio")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.25")
    assert "0.25" in telemetry.build_sampler().get_description()
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    assert telemetry.build_sampler().get_description() == "AlwaysOffSampler"


# ---------------------------------------------------------------------------
# gRPC: W3C propagation + handled counter / histogram
# ---------------------------------------------------------------------------


async def test_grpc_traceparent_propagates_client_to_server_with_handled_metrics() -> None:
    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    propagate.set_global_textmap(CompositePropagator([TraceContextTextMapPropagator()]))
    instrumentor = GrpcAioInstrumentorClient()
    instrumentor.instrument(tracer_provider=tracer_provider)
    try:

        async def ok(request: bytes, context: Any) -> bytes:
            return b"pong"

        async def missing(request: bytes, context: Any) -> bytes:
            await context.abort(grpc.StatusCode.NOT_FOUND, "nope")
            return b""

        handlers = grpc.method_handlers_generic_handler(
            "test.v1.Echo",
            {
                "Ok": grpc.unary_unary_rpc_method_handler(ok),
                "Missing": grpc.unary_unary_rpc_method_handler(missing),
            },
        )
        server = grpc.aio.server(interceptors=[OTelInterceptor()])
        server.add_generic_rpc_handlers((handlers,))
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                assert await channel.unary_unary("/test.v1.Echo/Ok")(b"ping", timeout=5) == b"pong"
                with pytest.raises(grpc.aio.AioRpcError) as err:
                    await channel.unary_unary("/test.v1.Echo/Missing")(b"ping", timeout=5)
                assert err.value.code() == grpc.StatusCode.NOT_FOUND
        finally:
            await server.stop(grace=0)

        finished = spans.get_finished_spans()
        client = next(s for s in finished if s.kind == SpanKind.CLIENT and s.name.endswith("/Ok"))
        server_span = next(
            s for s in finished if s.kind == SpanKind.SERVER and s.name.endswith("/Ok")
        )
        assert server_span.parent is not None
        print(
            f"GRPC-PROPAGATION client_span={client.context.span_id:016x} "
            f"server_parent={server_span.parent.span_id:016x}"
        )
        assert server_span.context.trace_id == client.context.trace_id
        assert (
            server_span.parent is not None and server_span.parent.span_id == client.context.span_id
        )
        assert (
            server_span.attributes["rpc.service"] == "test.v1.Echo"
            and server_span.attributes["rpc.method"] == "Ok"
        )

        data = reader.get_metrics_data()
        assert data is not None
        by_name: dict[str, list[Any]] = {}
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    by_name.setdefault(m.name, []).extend(m.data.data_points)
        handled = {
            (dp.attributes["rpc.method"], dp.attributes["rpc.grpc.status_code"]): dp.value
            for dp in by_name["grpc_server_handled"]
        }
        duration_points = by_name["grpc_server_request_duration_seconds"]
        print(f"GRPC-METRICS handled={handled} histogram_points={len(duration_points)}")
        assert handled == {("/test.v1.Echo/Ok", "OK"): 1, ("/test.v1.Echo/Missing", "NOT_FOUND"): 1}
        assert len(by_name["grpc_server_request_duration_seconds"]) == 2
    finally:
        instrumentor.uninstrument()


async def test_grpc_without_traceparent_starts_a_root_server_span() -> None:
    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    trace.set_tracer_provider(tracer_provider)
    propagate.set_global_textmap(CompositePropagator([TraceContextTextMapPropagator()]))

    async def ok(request: bytes, context: Any) -> bytes:
        return b"pong"

    server = grpc.aio.server(interceptors=[OTelInterceptor()])
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "test.v1.Echo", {"Ok": grpc.unary_unary_rpc_method_handler(ok)}
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            await channel.unary_unary("/test.v1.Echo/Ok")(b"ping", timeout=5)
    finally:
        await server.stop(grace=0)
    (span,) = spans.get_finished_spans()
    assert span.kind == SpanKind.SERVER and span.parent is None
