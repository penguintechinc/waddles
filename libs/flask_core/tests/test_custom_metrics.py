"""Tests for flask_core.custom_metrics against a real prometheus_client registry."""

import importlib.util
import sys
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry

from flask_core import custom_metrics as cm
from flask_core.custom_metrics import MetricsManager, create_metrics_manager


@pytest.fixture
def reg():
    return CollectorRegistry()


@pytest.fixture
def mm(reg):
    return MetricsManager("svc", "1.0", namespace="t", registry=reg)


def val(reg, name, **labels):
    return reg.get_sample_value(name, labels)


class TestDefaultMetrics:
    def test_service_info_published(self, mm, reg):
        assert val(reg, "t_service_info_info", service="svc", version="1.0") == 1.0

    def test_track_command_and_duration(self, mm, reg):
        mm.track_command("help", platform="twitch")
        mm.track_command("help", platform="twitch", status="error")
        mm.track_command_duration("help", 0.2, platform="twitch")
        assert val(reg, "t_commands_total", service="svc", command="help", platform="twitch", status="success") == 1
        assert val(reg, "t_commands_total", service="svc", command="help", platform="twitch", status="error") == 1
        assert val(reg, "t_command_duration_seconds_count", service="svc", command="help", platform="twitch") == 1
        assert val(reg, "t_command_duration_seconds_sum", service="svc", command="help", platform="twitch") == pytest.approx(0.2)

    def test_track_message_error(self, mm, reg):
        mm.track_message("discord")
        mm.track_message("discord", "event")
        mm.track_error("db", "critical")
        assert val(reg, "t_messages_total", service="svc", platform="discord", message_type="chat") == 1
        assert val(reg, "t_messages_total", service="svc", platform="discord", message_type="event") == 1
        assert val(reg, "t_errors_total", service="svc", error_type="db", severity="critical") == 1

    def test_track_db_query(self, mm, reg):
        mm.track_db_query("select", "users", 0.01)
        mm.track_db_query("select", "users", 0.02, status="error")
        assert val(reg, "t_db_queries_total", service="svc", operation="select", table="users", status="success") == 1
        assert val(reg, "t_db_queries_total", service="svc", operation="select", table="users", status="error") == 1
        assert val(reg, "t_db_query_duration_seconds_count", service="svc", operation="select", table="users") == 2

    def test_track_http_request_stringifies_status(self, mm, reg):
        mm.track_http_request("GET", "/x", 404, 0.5)
        assert val(reg, "t_http_requests_total", service="svc", method="GET", endpoint="/x", status="404") == 1
        assert val(reg, "t_http_request_duration_seconds_sum", service="svc", method="GET", endpoint="/x") == pytest.approx(0.5)

    def test_disabled_defaults_has_no_default_metrics(self, reg):
        m = MetricsManager("svc", "1", namespace="n", registry=reg, enable_default_metrics=False)
        assert not hasattr(m, "commands_total")
        assert val(reg, "n_service_info_info", service="svc", version="1") is None


class TestCustomMetrics:
    def test_counter_created_once_and_cached(self, mm, reg):
        c1 = mm.create_counter("hits", "hit counter", ["route"])
        c2 = mm.create_counter("hits", "ignored second description", ["other"])
        assert c1 is c2
        c1.labels(route="/a").inc(3)
        assert val(reg, "t_hits_total", route="/a") == 3

    def test_gauge(self, mm, reg):
        g = mm.create_gauge("depth", "queue depth")
        assert mm.create_gauge("depth", "x") is g
        g.set(7)
        assert val(reg, "t_depth") == 7

    def test_histogram_default_and_custom_buckets(self, mm, reg):
        h = mm.create_histogram("lat", "latency", ["op"])
        assert mm.create_histogram("lat", "x") is h
        h.labels(op="a").observe(0.3)
        assert val(reg, "t_lat_count", op="a") == 1
        h2 = mm.create_histogram("lat2", "latency2", buckets=(1.0, 2.0))
        h2.observe(1.5)
        assert val(reg, "t_lat2_bucket", le="1.0") == 0
        assert val(reg, "t_lat2_bucket", le="2.0") == 1

    def test_summary(self, mm, reg):
        s = mm.create_summary("sz", "size", ["k"])
        assert mm.create_summary("sz", "x") is s
        s.labels(k="a").observe(10)
        assert val(reg, "t_sz_sum", k="a") == 10


class TestMeasureTime:
    def test_observes_custom_histogram_with_labels(self, mm, reg):
        mm.create_histogram("job_seconds", "job", ["job"])
        with mm.measure_time("job_seconds", job="sync"):
            pass
        assert val(reg, "t_job_seconds_count", job="sync") == 1

    def test_observes_custom_histogram_without_labels(self, mm, reg):
        mm.create_histogram("plain_seconds", "plain")
        with mm.measure_time("plain_seconds"):
            pass
        assert val(reg, "t_plain_seconds_count") == 1

    def test_unknown_metric_is_silently_ignored(self, mm):
        with mm.measure_time("nope"):
            pass  # no exception, nothing recorded


class TestTrackDecorator:
    def test_sync_histogram_success_and_error(self, mm, reg):
        @mm.track_decorator(metric_type="histogram", metric_name="work_seconds", labels={"kind": "a"})
        def work(ok):
            if not ok:
                raise ValueError("bad")
            return "fine"

        assert work(True) == "fine"
        with pytest.raises(ValueError):
            work(False)
        assert val(reg, "t_work_seconds_count", kind="a", status="success") == 1
        assert val(reg, "t_work_seconds_count", kind="a", status="error") == 1
        assert work.__name__ == "work"

    async def test_async_counter(self, mm, reg):
        @mm.track_decorator(metric_type="counter", metric_name="calls")
        async def op(ok):
            if not ok:
                raise RuntimeError("x")
            return 1

        assert await op(True) == 1
        with pytest.raises(RuntimeError):
            await op(False)
        assert val(reg, "t_calls_total", status="success") == 1
        assert val(reg, "t_calls_total", status="error") == 1

    async def test_async_histogram_default_name(self, mm, reg):
        @mm.track_decorator()
        async def named():
            return None

        await named()
        assert val(reg, "t_named_duration_seconds_count", status="success") == 1

    def test_sync_counter_default_name(self, mm, reg):
        @mm.track_decorator(metric_type="counter")
        def named():
            return None

        named()
        assert val(reg, "t_named_duration_seconds_total", status="success") == 1

    def test_unknown_metric_type_records_nothing(self, mm, reg):
        @mm.track_decorator(metric_type="bogus", metric_name="zz")
        def f():
            return 1

        assert f() == 1
        assert val(reg, "t_zz_count", status="success") is None

    async def test_async_unknown_metric_type(self, mm):
        @mm.track_decorator(metric_type="bogus")
        async def f():
            return 2

        assert await f() == 2


class TestExport:
    def test_export_contains_metrics_and_content_type(self, mm):
        mm.track_command("ping")
        body = mm.export_metrics().decode()
        assert "t_commands_total" in body
        assert 'command="ping"' in body
        assert mm.get_content_type().startswith("text/plain")

    def test_unavailable_prometheus_branches(self, mm, monkeypatch):
        monkeypatch.setattr(cm, "PROMETHEUS_AVAILABLE", False)
        assert mm.export_metrics() == b"# Prometheus client not available\n"
        assert mm.get_content_type() == "text/plain"


class TestFactoryAndGlobal:
    def test_factory_env(self, monkeypatch):
        monkeypatch.setenv("METRICS_NAMESPACE", "envns")
        monkeypatch.setenv("METRICS_ENABLED", "false")
        m = create_metrics_manager("svc", "1", )
        assert m.namespace == "envns"
        assert m.enable_default_metrics is False

    def test_factory_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("METRICS_ENABLED", "false")
        monkeypatch.setenv("METRICS_NAMESPACE", "envns")
        m = create_metrics_manager("svc", "1", namespace="x", enable_default_metrics=False)
        assert m.namespace == "x"

    def test_init_and_get_global(self, monkeypatch):
        monkeypatch.setattr(cm, "_global_metrics_manager", None)
        assert cm.get_metrics_manager() is None
        m = cm.init_metrics("svc", "1", enable_default_metrics=False)
        assert cm.get_metrics_manager() is m


class TestFallbackWithoutPrometheusClient:
    """Re-import the module with prometheus_client unimportable to run the Dummy* shims."""

    @pytest.fixture
    def fallback_module(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "prometheus_client", None)  # import -> ImportError
        path = Path(cm.__file__)
        spec = importlib.util.spec_from_file_location("custom_metrics_noprom", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_flags_and_dummy_metric_surface(self, fallback_module):
        assert fallback_module.PROMETHEUS_AVAILABLE is False
        d = fallback_module.Counter("n", "d")
        assert d.labels(a=1) is d
        d.inc()
        d.dec()
        d.set(1)
        d.observe(1)
        d.info({})
        with d.time() as t:
            assert t is not None

    def test_manager_runs_on_dummies(self, fallback_module):
        m = fallback_module.MetricsManager("svc", "1")
        m.track_command("x")
        m.track_http_request("GET", "/", 200, 0.1)
        m.create_counter("c", "d").labels().inc()
        assert m.export_metrics() == b"# Prometheus client not available\n"
        assert m.get_content_type() == "text/plain"
