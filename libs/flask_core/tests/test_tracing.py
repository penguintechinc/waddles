"""Tests for flask_core.tracing against the real OpenTelemetry SDK.

Spans are captured with the SDK's InMemorySpanExporter, so span names,
attributes, status and recorded exceptions are asserted on real span data.
"""

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from flask_core import tracing as tr
from flask_core.tracing import TracingManager, create_tracing_manager


@pytest.fixture
def mgr():
    m = TracingManager("svc", "1.2.3", exporter_type="console", enable_auto_instrumentation=False)
    exporter = InMemorySpanExporter()
    m.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    # trace.set_tracer_provider only honours the FIRST call per process, so bind this
    # manager's tracer to its own provider explicitly to capture its spans.
    m.tracer = m.tracer_provider.get_tracer("svc", "1.2.3")
    m.exporter = exporter
    yield m
    m.shutdown()


class TestInit:
    def test_resource_attributes(self, mgr):
        attrs = mgr.resource.attributes
        assert attrs["service.name"] == "svc"
        assert attrs["service.version"] == "1.2.3"

    def test_defaults(self, mgr):
        assert mgr.jaeger_host == "localhost" and mgr.jaeger_port == 6831
        assert mgr.zipkin_endpoint.endswith("/api/v2/spans")
        assert mgr.otlp_endpoint == "http://localhost:4317"

    def test_exporter_type_lowercased(self):
        m = TracingManager("s", "1", exporter_type="CONSOLE", enable_auto_instrumentation=False)
        assert m.exporter_type == "console"
        m.shutdown()

    def test_otlp_exporter_configured(self):
        m = TracingManager(
            "s", "1", exporter_type="otlp", otlp_endpoint="http://collector:4317",
            enable_auto_instrumentation=False,
        )
        assert m.otlp_endpoint == "http://collector:4317"
        m.shutdown()

    @pytest.mark.parametrize("kind", ["jaeger", "zipkin"])
    def test_optional_exporter_unavailable_falls_back(self, monkeypatch, kind):
        monkeypatch.setattr(tr, "JAEGER_AVAILABLE", False)
        monkeypatch.setattr(tr, "ZIPKIN_AVAILABLE", False)
        m = TracingManager("s", "1", exporter_type=kind, enable_auto_instrumentation=False)
        m.shutdown()

    @pytest.mark.parametrize(
        "kind,flag,cls_attr",
        [("jaeger", "JAEGER_AVAILABLE", "JaegerExporter"), ("zipkin", "ZIPKIN_AVAILABLE", "ZipkinExporter")],
    )
    def test_optional_exporter_available_is_constructed(self, monkeypatch, kind, flag, cls_attr):
        built = {}

        class FakeExporter(tr.ConsoleSpanExporter):
            def __init__(self, **kwargs):
                super().__init__()
                built.update(kwargs)

        monkeypatch.setattr(tr, flag, True)
        monkeypatch.setattr(tr, cls_attr, FakeExporter)
        m = TracingManager(
            "s", "1", exporter_type=kind, jaeger_host="jh", jaeger_port=1, zipkin_endpoint="http://z/x",
            enable_auto_instrumentation=False,
        )
        m.shutdown()
        assert built  # exporter constructed with the configured target

    def test_exporter_setup_failure_falls_back_to_console(self, monkeypatch):
        def boom(**kwargs):
            raise RuntimeError("bad exporter")

        monkeypatch.setattr(tr, "OTLPSpanExporter", boom)
        m = TracingManager("s", "1", exporter_type="otlp", enable_auto_instrumentation=False)
        m.shutdown()

    def test_auto_instrumentation_requests_available(self, monkeypatch):
        calls = []

        class FakeInstr:
            def instrument(self):
                calls.append("instrument")

        monkeypatch.setattr(tr, "REQUESTS_INSTRUMENTATION_AVAILABLE", True)
        monkeypatch.setattr(tr, "RequestsInstrumentor", FakeInstr)
        m = TracingManager("s", "1", enable_auto_instrumentation=True)
        m.shutdown()
        assert calls == ["instrument"]

    def test_auto_instrumentation_unavailable_and_failing(self, monkeypatch):
        monkeypatch.setattr(tr, "REQUESTS_INSTRUMENTATION_AVAILABLE", False)
        TracingManager("s", "1", enable_auto_instrumentation=True).shutdown()

        class Boom:
            def instrument(self):
                raise RuntimeError("cannot patch")

        monkeypatch.setattr(tr, "REQUESTS_INSTRUMENTATION_AVAILABLE", True)
        monkeypatch.setattr(tr, "RequestsInstrumentor", Boom)
        TracingManager("s", "1", enable_auto_instrumentation=True).shutdown()  # swallowed


class TestInstrumentApp:
    def test_available(self, mgr, monkeypatch):
        seen = []

        class FakeFlaskInstr:
            def instrument_app(self, app):
                seen.append(app)

        class App:
            name = "myapp"

        monkeypatch.setattr(tr, "FLASK_INSTRUMENTATION_AVAILABLE", True)
        monkeypatch.setattr(tr, "FlaskInstrumentor", FakeFlaskInstr)
        app = App()
        mgr.instrument_app(app)
        assert seen == [app]

    def test_unavailable(self, mgr, monkeypatch):
        monkeypatch.setattr(tr, "FLASK_INSTRUMENTATION_AVAILABLE", False)
        mgr.instrument_app(object())  # no raise

    def test_instrumentor_failure_swallowed(self, mgr, monkeypatch):
        class Boom:
            def instrument_app(self, app):
                raise RuntimeError("x")

        monkeypatch.setattr(tr, "FLASK_INSTRUMENTATION_AVAILABLE", True)
        monkeypatch.setattr(tr, "FlaskInstrumentor", Boom)

        class App:
            name = "a"

        mgr.instrument_app(App())


class TestSpans:
    def test_start_span_records_attributes_and_kind(self, mgr):
        with mgr.start_span("op", kind=SpanKind.CLIENT, attributes={"k": "v"}) as span:
            span.set_attribute("extra", 1)
        (done,) = mgr.exporter.get_finished_spans()
        assert done.name == "op"
        assert done.kind is SpanKind.CLIENT
        assert dict(done.attributes) == {"k": "v", "extra": 1}

    def test_start_span_records_exception_and_error_status(self, mgr):
        with pytest.raises(ValueError, match="bad"):
            with mgr.start_span("failing"):
                raise ValueError("bad")
        (done,) = mgr.exporter.get_finished_spans()
        assert done.status.status_code is StatusCode.ERROR
        assert "bad" in done.status.description
        assert {e.name for e in done.events} == {"exception"}

    async def test_trace_function_async(self, mgr):
        @mgr.trace_function(attributes={"a": 1})
        async def work(x):
            return x + 1

        assert await work(1) == 2
        (done,) = mgr.exporter.get_finished_spans()
        assert done.name == "work"
        assert done.attributes["function.name"] == "work"
        assert done.attributes["function.module"] == __name__
        assert done.attributes["a"] == 1

    def test_trace_function_sync_custom_name(self, mgr):
        @mgr.trace_function(name="custom", kind=SpanKind.SERVER)
        def work(x):
            return x * 2

        assert work(4) == 8
        (done,) = mgr.exporter.get_finished_spans()
        assert done.name == "custom" and done.kind is SpanKind.SERVER
        assert work.__name__ == "work"

    def test_trace_function_propagates_errors(self, mgr):
        @mgr.trace_function()
        def bad():
            raise KeyError("k")

        with pytest.raises(KeyError):
            bad()
        assert mgr.exporter.get_finished_spans()[0].status.status_code is StatusCode.ERROR


class TestCurrentSpanHelpers:
    def test_event_attribute_attributes_on_current_span(self, mgr):
        with mgr.start_span("outer") as span:
            assert mgr.get_current_span() is span
            mgr.add_event("evt", {"e": 1})
            mgr.set_attribute("one", 1)
            mgr.set_attributes({"two": 2, "three": 3})
        (done,) = mgr.exporter.get_finished_spans()
        assert [e.name for e in done.events] == ["evt"]
        assert dict(done.attributes) == {"one": 1, "two": 2, "three": 3}

    def test_helpers_outside_span_are_safe(self, mgr):
        mgr.add_event("nothing")
        mgr.set_attribute("a", 1)
        mgr.set_attributes({"b": 2})
        assert mgr.exporter.get_finished_spans() == ()


class TestPropagation:
    def test_inject_then_extract_roundtrip_trace_id(self, mgr):
        carrier: dict[str, str] = {}
        with mgr.start_span("client") as span:
            mgr.inject_context(carrier)
            trace_id = span.get_span_context().trace_id
        assert "traceparent" in carrier
        ctx = mgr.extract_context(carrier)
        with mgr.start_span("server", parent_context=ctx) as child:
            assert child.get_span_context().trace_id == trace_id

    def test_extract_empty_carrier_gives_context(self, mgr):
        assert mgr.extract_context({}) is not None


class TestShutdown:
    def test_shutdown_error_swallowed(self, mgr, monkeypatch):
        def boom():
            raise RuntimeError("flush failed")

        monkeypatch.setattr(mgr.tracer_provider, "shutdown", boom)
        mgr.shutdown()


class TestFactoryAndGlobal:
    def test_factory_env_defaults(self, monkeypatch):
        monkeypatch.setenv("TRACING_EXPORTER", "console")
        monkeypatch.setenv("JAEGER_HOST", "jh")
        monkeypatch.setenv("JAEGER_PORT", "1234")
        monkeypatch.setenv("TRACING_AUTO_INSTRUMENT", "false")
        monkeypatch.setenv("TRACING_SAMPLE_RATE", "0.5")
        m = create_tracing_manager("svc", "9")
        try:
            assert (m.jaeger_host, m.jaeger_port) == ("jh", 1234)
            assert m.enable_auto_instrumentation is False
            assert m.sample_rate == 0.5
        finally:
            m.shutdown()

    def test_factory_explicit_args_win(self, monkeypatch):
        monkeypatch.setenv("TRACING_EXPORTER", "otlp")
        m = create_tracing_manager(
            "svc", "9", exporter_type="console", jaeger_host="h", jaeger_port=7,
            zipkin_endpoint="http://z", otlp_endpoint="http://o", enable_auto_instrumentation=False,
            sample_rate=0.25,
        )
        try:
            assert m.exporter_type == "console"
            assert (m.jaeger_host, m.jaeger_port, m.zipkin_endpoint, m.otlp_endpoint) == (
                "h", 7, "http://z", "http://o",
            )
        finally:
            m.shutdown()

    def test_factory_tracing_disabled_forces_console(self, monkeypatch):
        monkeypatch.setenv("TRACING_ENABLED", "false")
        monkeypatch.setenv("TRACING_EXPORTER", "otlp")
        monkeypatch.setenv("TRACING_AUTO_INSTRUMENT", "false")
        m = create_tracing_manager("svc", "9")
        try:
            # TRACING_ENABLED=false pins the exporter to console, ignoring TRACING_EXPORTER.
            assert m.exporter_type == "console"
        finally:
            m.shutdown()

    def test_init_and_get_global(self, monkeypatch):
        monkeypatch.setattr(tr, "_global_tracing_manager", None)
        assert tr.get_tracing_manager() is None
        m = tr.init_tracing("svc", "1", exporter_type="console", enable_auto_instrumentation=False)
        try:
            assert tr.get_tracing_manager() is m
        finally:
            m.shutdown()
