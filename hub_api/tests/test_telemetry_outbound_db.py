"""Outbound httpx propagation/scrubbing and pydal DB spans, against the real OTLP receiver.

Proves: `traceparent`/`baggage` reach cluster-internal hosts and NEVER third-party hosts (checked at
the receiving socket, not just in the span), client-span URLs carry no query/token (and no path
for external hosts), pydal queries (the `flask_core` raw `dal` and the thread-pool `AsyncDAL`)
produce parented db spans with operation + table only, and no SQL text or driver error text is
exported. Counts are printed; zero spans is a failure (testing.md Telemetry Validation).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from opentelemetry import baggage, context
from opentelemetry.trace import StatusCode

import telemetry
from tests.telemetry_support import _attrs, _build_app, _enable, _Receiver, _wait_for

pytestmark = pytest.mark.usefixtures("_clean_otel_env")

SECRET_QUERY = "tok_" + "OUTBOUNDSECRET777"
SECRET_PATH_TOKEN = "hook_" + "PATHTOKEN888"
EMAIL = "outbound-user@example.invalid"
DB_LITERAL = "sentinel_slug_" + "x1"
DB_ERROR_LITERAL = "sentinel_val_" + "zz9"


class _Origin:
    """A real loopback HTTP server that records the path and trace headers of every request."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        origin = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                origin.seen.append(
                    {
                        "path": self.path,
                        "traceparent": self.headers.get("traceparent"),
                        "tracestate": self.headers.get("tracestate"),
                        "baggage": self.headers.get("baggage"),
                    }
                )
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def origin() -> Iterator[_Origin]:
    server = _Origin()
    yield server
    server.stop()


@pytest.mark.parametrize(
    ("host", "internal"),
    [
        ("svc-streaming", True),
        ("localhost", True),
        ("hub-api.waddlebot.svc", True),
        ("hub-api.waddlebot.svc.cluster.local", True),
        ("10.1.2.3", True),
        ("192.168.8.41", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("discord.com", False),
        ("api.twitch.tv", False),
        ("8.8.8.8", False),
        ("evil.svc.example.com", False),
        ("", False),
        (None, False),
    ],
)
def test_internal_host_classification(host: str | None, internal: bool) -> None:
    assert telemetry.is_internal_host(host) is internal


def test_extra_internal_suffix_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not telemetry.is_internal_host("billing.corp.example")
    monkeypatch.setenv(telemetry.INTERNAL_SUFFIXES_ENV, ".corp.example, .other.test")
    assert telemetry.is_internal_host("billing.corp.example")


async def test_httpx_propagates_to_internal_only_and_scrubs_urls(
    monkeypatch: pytest.MonkeyPatch, receiver: _Receiver, origin: _Origin
) -> None:
    _enable(monkeypatch, receiver.endpoint)
    app = _build_app()
    base = f"http://127.0.0.1:{origin.port}"
    token = baggage.set_baggage("tenant", "1")
    attached = context.attach(token)
    try:
        async with app.test_app():
            runtime = app.config["telemetry_runtime"]
            assert runtime.enabled, runtime.reason
            async with httpx.AsyncClient() as client:
                # 1) cluster-internal (loopback): propagated, path kept, query dropped
                internal_url = f"{base}/internal/ok?token={SECRET_QUERY}&email={EMAIL}"
                assert (await client.get(internal_url)).status_code == 200
                # 2) third-party: same socket, but the host is classified external
                with monkeypatch.context() as patched:
                    patched.setattr(telemetry, "is_internal_host", lambda _host: False)
                    external_url = f"{base}/api/webhooks/123/{SECRET_PATH_TOKEN}?x={SECRET_QUERY}"
                    assert (await client.get(external_url)).status_code == 200
                    # 3) the sync client goes through the guarded sync transport too
                    with httpx.Client() as sync_client:
                        await asyncio.to_thread(
                            sync_client.get, f"{base}/sync/external?t={SECRET_QUERY}"
                        )
                with httpx.Client() as sync_client:
                    await asyncio.to_thread(sync_client.get, f"{base}/sync/internal")
            await asyncio.to_thread(runtime.force_flush, 10.0)
            assert await _wait_for(lambda: len(_client_spans(receiver)) >= 4)
    finally:
        context.detach(attached)

    seen = {entry["path"].split("?")[0]: entry for entry in origin.seen}
    internal, external = seen["/internal/ok"], seen[f"/api/webhooks/123/{SECRET_PATH_TOKEN}"]
    sync_external, sync_internal = seen["/sync/external"], seen["/sync/internal"]
    print(
        f"HTTPX received_client_spans={len(_client_spans(receiver))} "
        f"internal_traceparent={internal['traceparent']} "
        f"external_traceparent={external['traceparent']}"
    )
    assert internal["traceparent"] and "tenant=1" in (internal["baggage"] or "")
    assert sync_internal["traceparent"]
    for leaked in (external, sync_external):
        assert leaked["traceparent"] is None and leaked["tracestate"] is None
        assert leaked["baggage"] is None, "baggage must never reach a third party"

    spans = _client_spans(receiver)
    by_path = {
        _attrs(s.attributes).get("http.url") or _attrs(s.attributes).get("url.full"): s
        for s in spans
    }
    assert f"{base}/internal/ok" in by_path, list(by_path)
    assert base in by_path, "external client span URL must be scheme://host[:port] only"
    internal_span = by_path[f"{base}/internal/ok"]
    assert internal["traceparent"].split("-")[2] == format(
        int.from_bytes(internal_span.span_id, "big"), "016x"
    )

    wire = receiver.wire_bytes()
    for secret in (SECRET_QUERY, SECRET_PATH_TOKEN, EMAIL):
        assert secret.encode() not in wire, f"{secret!r} leaked into exported telemetry"
    guards_left = [
        hasattr(httpx.AsyncHTTPTransport.handle_async_request, "__wrapped__"),
        hasattr(httpx.HTTPTransport.handle_request, "__wrapped__"),
    ]
    assert not any(guards_left), "propagation guard not removed after shutdown"


def _client_spans(receiver: _Receiver) -> list[Any]:
    """Exported CLIENT spans of the httpx instrumentation (kind 3)."""
    return [
        s
        for s in receiver.spans()
        if s.kind == 3
        and (_attrs(s.attributes).get("http.url") or _attrs(s.attributes).get("url.full"))
    ]


async def test_pydal_queries_produce_parented_db_spans_without_sql_text(
    monkeypatch: pytest.MonkeyPatch, receiver: _Receiver, client_for: Any
) -> None:
    _enable(monkeypatch, receiver.endpoint)
    app = _build_app()

    @app.get("/api/v1/_otel/pydal")
    async def pydal_route() -> dict[str, int]:
        async_dal = app.config["async_dal"]
        dal = async_dal.dal
        dal.executesql("CREATE TABLE IF NOT EXISTS otel_probe (id INTEGER, slug TEXT)")
        # pool worker thread (sqlite:memory is per-thread, so use a table-free statement here)
        await async_dal.executesql_async("SELECT 1 FROM sqlite_master WHERE name = 'x'")
        direct = dal.executesql(
            f"SELECT id FROM otel_probe WHERE slug = '{DB_LITERAL}'"  # noqa: S608
        )  # loop thread
        try:
            bad = f"SELECT 1 FROM otel_probe WHERE nope_col = '{DB_ERROR_LITERAL}'"  # noqa: S608
            dal.executesql(bad)
        except Exception:  # noqa: BLE001 - the failing statement is the point of this route
            failed = 1
        else:
            failed = 0
        return {"direct": len(direct), "failed": failed}

    async with app.test_app():
        runtime = app.config["telemetry_runtime"]
        assert runtime.enabled, runtime.reason
        client = client_for(app)
        resp = await client.get("/api/v1/_otel/pydal")
        assert resp.status_code == 200 and resp.json()["failed"] == 1
        await asyncio.to_thread(runtime.force_flush, 10.0)
        assert await _wait_for(
            lambda: any(_attrs(s.attributes).get("db.system") == "sqlite" for s in receiver.spans())
        )

    spans = receiver.spans()
    request_span = next(s for s in spans if s.name == "GET /api/v1/_otel/pydal")
    # (SQLAlchemy's startup `connect` span belongs to no request, so scope to this request's trace)
    db_spans = [
        s
        for s in spans
        if _attrs(s.attributes).get("db.system") == "sqlite" and s.trace_id == request_span.trace_id
    ]
    selects = [s for s in db_spans if s.status.code != 2]
    failed = [s for s in db_spans if s.status.code == 2]
    print(f"PYDAL received_db_spans={len(db_spans)} selects={len(selects)} failed={len(failed)}")
    tables = {_attrs(s.attributes).get("db.sql.table") for s in selects}
    assert {"otel_probe", "sqlite_master"} <= tables, tables  # event-loop thread AND pool thread
    assert failed, "the failing statement must be an ERROR db span"
    for span in db_spans:
        attrs = _attrs(span.attributes)
        assert span.trace_id == request_span.trace_id, "db span must be in the request's trace"
        # direct child of the request, or nested under another db span (PRAGMA runs inside CREATE)
        assert span.parent_span_id in ({request_span.span_id} | {x.span_id for x in db_spans})
        assert "db.statement" not in attrs and "db.query.text" not in attrs
        assert attrs["db.operation"] == span.name
    operations = {_attrs(s.attributes)["db.operation"] for s in selects}
    assert {"SELECT"} <= operations <= {"SELECT", "CREATE", "PRAGMA"}, operations
    assert failed[0].status.message == "OperationalError"
    exception_events = [e for e in failed[0].events if e.name == "exception"]
    assert not exception_events, "driver error text must not be recorded"
    wire = receiver.wire_bytes()
    for secret in (DB_LITERAL, DB_ERROR_LITERAL, "nope_col"):
        assert secret.encode() not in wire, f"{secret!r} leaked into exported telemetry"


async def test_sqlalchemy_statement_text_is_dropped_but_operation_kept(
    monkeypatch: pytest.MonkeyPatch, receiver: _Receiver, client_for: Any
) -> None:
    _enable(monkeypatch, receiver.endpoint)
    app = _build_app()
    async with app.test_app():
        runtime = app.config["telemetry_runtime"]
        client = client_for(app)
        assert (await client.get("/api/v1/_otel/db")).status_code == 200
        await asyncio.to_thread(runtime.force_flush, 10.0)
        assert await _wait_for(
            lambda: any(_attrs(s.attributes).get("db.system") == "sqlite" for s in receiver.spans())
        )
    db_spans = [s for s in receiver.spans() if _attrs(s.attributes).get("db.system") == "sqlite"]
    print(f"SQLALCHEMY received_db_spans={len(db_spans)}")
    assert db_spans
    for span in db_spans:
        attrs = _attrs(span.attributes)
        assert "db.statement" not in attrs and "db.query.text" not in attrs
    assert any(_attrs(s.attributes).get("db.operation") == "SELECT" for s in db_spans)
    assert all(s.status.code != StatusCode.ERROR.value for s in db_spans)
