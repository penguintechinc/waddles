"""Tests for flask_core.correlation using a real Quart app + test client."""

import logging
import uuid

import pytest
from quart import Quart, g

from flask_core import correlation as corr
from flask_core.correlation import (
    CorrelationIDFilter,
    CorrelationIDFormatter,
    CorrelationIDManager,
    create_correlation_manager,
    init_correlation,
    setup_correlation_logging,
)


@pytest.fixture(autouse=True)
def _clean_context():
    corr._correlation_id.set(None)
    corr._request_id.set(None)
    yield
    corr._correlation_id.set(None)
    corr._request_id.set(None)


def make_app(manager: CorrelationIDManager) -> Quart:
    app = Quart("corr-test")
    manager.create_middleware(app)

    @app.route("/ids")
    async def ids():
        return {
            "ctx_cid": manager.get_correlation_id(),
            "ctx_rid": manager.get_request_id(),
            "g_cid": g.correlation_id,
            "g_rid": g.request_id,
            "outgoing": manager.inject_into_headers({}),
        }

    return app


class TestIdPrimitives:
    def test_generate_id_is_uuid4(self):
        m = CorrelationIDManager()
        assert uuid.UUID(m.generate_id()).version == 4

    def test_validate_id_disabled_accepts_anything(self):
        assert CorrelationIDManager(validate_uuid=False).validate_id("not-a-uuid") is True

    def test_validate_id_enabled(self):
        m = CorrelationIDManager(validate_uuid=True)
        assert m.validate_id(str(uuid.uuid4())) is True
        assert m.validate_id("nope") is False
        assert m.validate_id(None) is False  # AttributeError path

    def test_context_roundtrip(self):
        m = CorrelationIDManager()
        assert m.get_correlation_id() is None and m.get_request_id() is None
        m.set_correlation_id("c1")
        m.set_request_id("r1")
        assert (m.get_correlation_id(), m.get_request_id()) == ("c1", "r1")
        assert (corr.get_correlation_id(), corr.get_request_id()) == ("c1", "r1")

    def test_inject_into_headers(self):
        m = CorrelationIDManager(correlation_header="X-C", request_header="X-R")
        assert m.inject_into_headers({"a": "b"}) == {"a": "b"}  # nothing in context
        m.set_correlation_id("c")
        assert m.inject_into_headers({}) == {"X-C": "c"}
        m.set_request_id("r")
        assert m.inject_into_headers({}) == {"X-C": "c", "X-R": "r"}


class TestMiddleware:
    async def test_propagates_incoming_correlation_and_generates_request_id(self):
        m = CorrelationIDManager()
        client = make_app(m).test_client()
        resp = await client.get("/ids", headers={"X-Correlation-ID": "abc", "X-Request-ID": "incoming"})
        body = await resp.get_json()
        assert body["ctx_cid"] == body["g_cid"] == "abc"
        assert body["g_rid"] != "incoming"  # generate_request_id=True always mints a fresh one
        assert resp.headers["X-Correlation-ID"] == "abc"
        assert resp.headers["X-Request-ID"] == body["g_rid"]
        assert body["outgoing"] == {"X-Correlation-ID": "abc", "X-Request-ID": body["g_rid"]}

    async def test_generates_both_when_absent(self):
        m = CorrelationIDManager()
        resp = await make_app(m).test_client().get("/ids")
        body = await resp.get_json()
        uuid.UUID(body["g_cid"])
        uuid.UUID(body["g_rid"])

    async def test_request_id_kept_when_generation_disabled(self):
        m = CorrelationIDManager(generate_request_id=False)
        resp = await make_app(m).test_client().get("/ids", headers={"X-Request-ID": "keep-me"})
        assert (await resp.get_json())["g_rid"] == "keep-me"

    async def test_request_id_generated_when_disabled_but_missing(self):
        m = CorrelationIDManager(generate_request_id=False)
        resp = await make_app(m).test_client().get("/ids")
        uuid.UUID((await resp.get_json())["g_rid"])

    async def test_invalid_uuid_replaced_when_validation_on(self):
        m = CorrelationIDManager(generate_request_id=False, validate_uuid=True)
        resp = await make_app(m).test_client().get(
            "/ids", headers={"X-Correlation-ID": "bad", "X-Request-ID": "also-bad"}
        )
        body = await resp.get_json()
        uuid.UUID(body["g_cid"])
        uuid.UUID(body["g_rid"])

    async def test_valid_uuid_request_id_kept_when_validation_on(self):
        m = CorrelationIDManager(generate_request_id=False, validate_uuid=True)
        rid = str(uuid.uuid4())
        resp = await make_app(m).test_client().get("/ids", headers={"X-Request-ID": rid})
        assert (await resp.get_json())["g_rid"] == rid

    async def test_custom_header_names(self):
        m = CorrelationIDManager(correlation_header="X-Trace", request_header="X-Req")
        resp = await make_app(m).test_client().get("/ids", headers={"X-Trace": "t-1"})
        assert resp.headers["X-Trace"] == "t-1"
        assert "X-Req" in resp.headers

    async def test_without_quart_generates_fresh_ids(self, monkeypatch):
        monkeypatch.setattr(corr, "QUART_AVAILABLE", False)
        ids = await CorrelationIDManager().extract_ids_from_request()
        uuid.UUID(ids["correlation_id"])
        uuid.UUID(ids["request_id"])
        assert ids["correlation_id"] != ids["request_id"]


class TestTrackRequest:
    async def test_async_generates_ids_when_missing(self):
        m = CorrelationIDManager()

        @m.track_request
        async def work(x):
            return x, m.get_correlation_id(), m.get_request_id()

        x, cid, rid = await work(5)
        assert x == 5 and cid and rid
        assert work.__name__ == "work"

    def test_sync_generates_ids_and_keeps_existing(self):
        m = CorrelationIDManager()
        m.set_correlation_id("existing")

        @m.track_request
        def work():
            return m.get_correlation_id(), m.get_request_id()

        cid, rid = work()
        assert cid == "existing" and rid


class TestLogging:
    def _record(self, msg="hello %s", args=("w",)):
        return logging.LogRecord("n", logging.INFO, __file__, 1, msg, args, None)

    def test_filter_adds_na_without_context(self):
        rec = self._record()
        assert CorrelationIDFilter(CorrelationIDManager()).filter(rec) is True
        assert (rec.correlation_id, rec.request_id) == ("N/A", "N/A")
        assert rec.getMessage() == "hello w"

    def test_filter_include_in_message(self):
        m = CorrelationIDManager()
        m.set_correlation_id("c9")
        m.set_request_id("r9")
        rec = self._record()
        CorrelationIDFilter(m, include_in_message=True).filter(rec)
        assert rec.getMessage() == "[cid=c9] [rid=r9] hello w"

    def test_formatter_defaults_and_missing_attrs(self):
        out = CorrelationIDFormatter().format(self._record())
        assert "[cid=N/A] [rid=N/A]" in out and "hello w" in out

    def test_formatter_uses_record_attrs_and_custom_fmt(self):
        rec = self._record()
        rec.correlation_id = "c"
        rec.request_id = "r"
        out = CorrelationIDFormatter(fmt="%(correlation_id)s|%(request_id)s|%(message)s").format(rec)
        assert out == "c|r|hello w"

    def test_setup_correlation_logging_attaches_filter(self):
        m = CorrelationIDManager()
        lg = setup_correlation_logging(m, logger_name="corr.test.logger", include_in_message=True)
        try:
            assert any(isinstance(f, CorrelationIDFilter) and f.include_in_message for f in lg.filters)
        finally:
            lg.filters.clear()


class TestFactoryAndGlobal:
    def test_factory_env_defaults(self, monkeypatch):
        monkeypatch.setenv("CORRELATION_HEADER", "X-E-C")
        monkeypatch.setenv("REQUEST_HEADER", "X-E-R")
        monkeypatch.setenv("GENERATE_REQUEST_ID", "false")
        monkeypatch.setenv("VALIDATE_UUID", "true")
        m = create_correlation_manager()
        assert (m.correlation_header, m.request_header) == ("X-E-C", "X-E-R")
        assert m.generate_request_id is False and m.validate_uuid is True

    def test_factory_plain_defaults(self, monkeypatch):
        for k in ("CORRELATION_HEADER", "REQUEST_HEADER", "GENERATE_REQUEST_ID", "VALIDATE_UUID"):
            monkeypatch.delenv(k, raising=False)
        m = create_correlation_manager()
        assert m.correlation_header == "X-Correlation-ID"
        assert m.generate_request_id is True and m.validate_uuid is False

    def test_factory_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("GENERATE_REQUEST_ID", "false")
        m = create_correlation_manager(correlation_header="H", generate_request_id=True, validate_uuid=True)
        assert m.correlation_header == "H"
        assert m.generate_request_id is True and m.validate_uuid is True

    async def test_init_correlation_registers_middleware(self, monkeypatch):
        monkeypatch.setattr(corr, "_global_correlation_manager", None)
        assert corr.get_correlation_manager() is None
        app = Quart("init-corr")

        @app.route("/ping")
        async def ping():
            return "pong"

        m = init_correlation(app=app, correlation_header="X-G")
        assert corr.get_correlation_manager() is m
        resp = await app.test_client().get("/ping", headers={"X-G": "from-test"})
        assert resp.headers["X-G"] == "from-test"

    def test_init_correlation_without_app(self, monkeypatch):
        monkeypatch.setattr(corr, "_global_correlation_manager", None)
        assert isinstance(init_correlation(), CorrelationIDManager)
