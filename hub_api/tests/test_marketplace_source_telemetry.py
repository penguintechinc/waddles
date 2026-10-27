"""Marketplace-source observability + audit: the OTel triad, PII-free labels, audit row set.

Real Postgres + a real local git remote drive the scanner; the ingest/approve audit rows reuse
the ingest test env. Counts are asserted AND printed -- a zero-item run is a failure.
A SKIP means docker/git is absent -- treat it as a gate FAILURE, never a pass.
"""

# ruff: noqa: F811  -- imported pytest fixtures are re-bound as test params
# flake8: noqa: F811

from __future__ import annotations

import dataclasses
import logging
import shutil
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import text

from services import bundle_telemetry
from services import marketplace_source_ingest as ing
from services import marketplace_source_scanner as sc
from services import marketplace_source_service as svc
from services import marketplace_source_telemetry as tel
from services.marketplace_publisher_attestation import fingerprint_publisher_key
from services.marketplace_source_service import (
    BuildMode,
    SandboxAck,
    SourceStatus,
    create_source,
    delete_source,
    disable_source,
    update_status,
)
from tests.malicious_source_fixtures import add_bundle, commit_bare, git
from tests.test_marketplace_source_ingest import (  # noqa: F401  # fixtures + helpers
    _candidate,
    _ingest,
    env,
)
from tests.test_seed_core_bundles_pg_integration import (  # noqa: F401  # fixtures
    pg_dal,
    pg_db,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")

SECRET = "s3cretpw-do-not-leak"
USERNAME = "alice-the-admin"
CRED_URL = f"https://{USERNAME}:{SECRET}@github.com/acme/leaky-repo"
FREE_TEXT = "free-text-manifest-note-xyz"
_PK = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


class Sink:
    """In-process OTel sink: spans + metric points (the local OTLP test sink)."""

    def __init__(self) -> None:
        """Wire a real SDK tracer + meter provider to in-memory collectors."""
        self.spans = InMemorySpanExporter()
        tp = TracerProvider()
        tp.add_span_processor(SimpleSpanProcessor(self.spans))
        self.reader = InMemoryMetricReader()
        self.tracer = tp.get_tracer("test")
        self.meter = MeterProvider(metric_readers=[self.reader]).get_meter("test")

    def points(self, name: str) -> list[Any]:
        """All data points received for metric `name`."""
        data = self.reader.get_metrics_data()
        out: list[Any] = []
        for res in data.resource_metrics if data else []:
            for scope in res.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == name:
                        out.extend(metric.data.data_points)
        return out

    def all_label_values(self) -> list[str]:
        """Every metric label value + span attribute value emitted (stringified)."""
        vals: list[str] = []
        data = self.reader.get_metrics_data()
        for res in data.resource_metrics if data else []:
            for scope in res.scope_metrics:
                for metric in scope.metrics:
                    for pt in metric.data.data_points:
                        vals += [str(v) for v in dict(pt.attributes).values()]
        for span in self.spans.get_finished_spans():
            vals += [str(v) for v in dict(span.attributes or {}).values()]
        return vals


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> Sink:
    s = Sink()
    monkeypatch.setattr(bundle_telemetry, "_TRACER", s.tracer)
    monkeypatch.setattr(bundle_telemetry, "_METER", s.meter)
    return s


@pytest.fixture(autouse=True)
def _transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(sc.TEST_TRANSPORT_ENV, "1")
    yield
    sc._test_remote = None


def _remote(tmp: Path, *, with_bundle: bool) -> str:
    work = tmp / "work"
    work.mkdir()
    if with_bundle:
        add_bundle(work, "python", "alpha")
    else:
        (work / "README.md").write_text("no bundles here\n")
        git(work, "init", "-q", "-b", "main")
    bare = tmp / "remote.git"
    sha = commit_bare(work, bare)
    sc.enable_test_transport(f"file://{bare}")
    return sha


async def _user(dal: Any, user_id: int) -> int:
    async with dal.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO hub_users (id, username, display_name) OVERRIDING SYSTEM VALUE "
                "VALUES (:i, :n, :n) ON CONFLICT (id) DO NOTHING"
            ),
            {"i": user_id, "n": f"user{user_id}"},
        )
    return user_id


async def _source(dal: Any, sha: str, *, mode: BuildMode = BuildMode.PREBUILT, **kw: Any) -> Any:
    extra: dict[str, Any] = {}
    if mode == BuildMode.PREBUILT:
        extra = {
            "publisher_key_fingerprint": fingerprint_publisher_key(_PK),
            "publisher_public_key": _PK,
        }
    rec = await create_source(
        dal,
        name=USERNAME,
        git_url="https://github.com/acme/leaky-repo",
        ref="main",
        build_mode=mode,
        pinned_commit_sha=sha,
        **extra,
        **kw,
    )
    return await update_status(dal, rec.public_id, SourceStatus.ACTIVE)


async def _audit(dal: Any, sid: Any, *, like: str = "marketplace_%") -> list[tuple[Any, ...]]:
    async with dal.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT action, user_id, details FROM audit_log "
                "WHERE target_id=:t AND action LIKE :a ORDER BY id"
            ),
            {"t": str(sid), "a": like},
        )
        return [tuple(r) for r in rows]


def _log_blob(caplog: pytest.LogCaptureFixture) -> str:
    parts: list[str] = []
    for rec in caplog.records:
        parts.append(rec.getMessage())
        parts += [str(v) for k, v in rec.__dict__.items() if not k.startswith("_")]
    return "\n".join(parts)


async def test_scan_emits_triad_and_carries_public_id_only(
    pg_dal: Any, tmp_path: Path, sink: Sink, caplog: pytest.LogCaptureFixture
) -> None:
    sha = _remote(tmp_path, with_bundle=True)
    src = dataclasses.replace(await _source(pg_dal, sha), git_url=CRED_URL)
    scratch = tmp_path / "s"
    scratch.mkdir()
    with caplog.at_level(logging.DEBUG):
        result = await sc.scan_source(src, workdir=scratch, dal=pg_dal)
    spans = sink.spans.get_finished_spans()
    logs = [r for r in caplog.records if r.name.startswith("services.marketplace")]
    totals, examined = (
        sink.points("marketplace.scan.total"),
        sink.points("marketplace.scan.bundles_examined"),
    )
    hist = sink.points("marketplace.scan.duration_s")
    print(
        f"telemetry check: logs={len(logs)} metric_points={len(totals) + len(examined)} "
        f"histogram_points={len(hist)} spans={len(spans)}"
    )
    assert len(logs) >= 1 and len(totals) >= 1 and len(examined) >= 1
    assert len(hist) >= 1 and len(spans) >= 1
    assert result.examined == 1 and examined[0].value == 1
    scan_span = next(s for s in spans if s.name == "marketplace.source.scan")
    assert scan_span.attributes["source_public_id"] == str(src.public_id)
    assert set(scan_span.attributes) - {"duration_ms"} == {"source_public_id"}
    # no creds / usernames / free text anywhere in logs, labels or span attributes
    blob = _log_blob(caplog) + "\n".join(sink.all_label_values())
    for leaked in (SECRET, USERNAME, "leaky-repo", CRED_URL, FREE_TEXT):
        assert leaked not in blob


async def test_zero_examined_emits_failure_metric(
    pg_dal: Any, tmp_path: Path, sink: Sink, caplog: pytest.LogCaptureFixture
) -> None:
    sha = _remote(tmp_path, with_bundle=False)
    src = dataclasses.replace(await _source(pg_dal, sha), git_url=CRED_URL)
    scratch = tmp_path / "s"
    scratch.mkdir()
    with caplog.at_level(logging.DEBUG), pytest.raises(sc.ScanRejectedError):
        await sc.scan_source(src, workdir=scratch, dal=pg_dal)
    failures = sink.points("marketplace.scan.failures")
    print(f"telemetry check: failure_points={len(failures)}")
    assert len(failures) == 1 and failures[0].value == 1
    assert dict(failures[0].attributes) == {"category": "zero_examined"}
    assert sink.points("marketplace.scan.bundles_examined")[0].value == 0
    assert SECRET not in _log_blob(caplog) + "\n".join(sink.all_label_values())


async def test_audit_row_set_for_source_lifecycle(pg_dal: Any, tmp_path: Path, sink: Sink) -> None:
    admin = await _user(pg_dal, 41)
    ack = SandboxAck(by=admin, at=datetime.now(UTC), text_hash="a" * 64, commit_sha="b" * 40)
    sb = await create_source(
        pg_dal, name="sb", git_url="https://github.com/acme/sb", ref="main",
        build_mode=BuildMode.SANDBOX, created_by=admin, sandbox_ack=ack,
        pinned_commit_sha="b" * 40,
    )  # fmt: skip
    rows = await _audit(pg_dal, sb.public_id)
    assert [r[0] for r in rows] == [svc.ACTION_ADDED, svc.ACTION_ACK_RECORDED]
    assert rows[1][2]["ack_text_hash"] == "a" * 64 and rows[0][1] == admin

    # rescan (success + rejected) through the real scanner
    sha = _remote(tmp_path, with_bundle=True)
    src = await _source(pg_dal, sha)
    scratch = tmp_path / "s"
    scratch.mkdir()
    await sc.scan_source(src, workdir=scratch, dal=pg_dal, actor_id=admin)
    with pytest.raises(sc.ScanRejectedError):
        await sc.scan_source(
            await _source(pg_dal, "0" * 40), workdir=scratch, dal=pg_dal, actor_id=admin
        )
    scan_rows = await _audit(pg_dal, src.public_id, like="marketplace_source_rescanned")
    assert len(scan_rows) == 1 and scan_rows[0][1] == admin
    assert scan_rows[0][2] == {"examined": 1, "accepted": 1, "rejected": 0}

    # pending update -> approve-update, then disable and delete (two distinct sources)
    await update_status(
        pg_dal, sb.public_id, SourceStatus.NEEDS_APPROVAL, pending_commit_sha="c" * 40
    )
    await svc.approve_update(
        pg_dal, sb.public_id, expected_pending_sha="c" * 40, sandbox_ack=ack, actor_id=admin
    )
    approved = await _audit(pg_dal, sb.public_id, like=svc.ACTION_UPDATE_APPROVED)
    assert len(approved) == 1 and approved[0][2]["pinned_commit_sha"] == "c" * 40
    assert len(sink.points("marketplace.source.pending_updates")) >= 1
    await disable_source(pg_dal, sb.public_id, actor_id=admin)
    await delete_source(pg_dal, src.public_id, actor_id=admin)
    assert (await _audit(pg_dal, sb.public_id, like=svc.ACTION_REMOVED))[0][2] == {
        "disposition": "disabled"
    }
    assert (await _audit(pg_dal, src.public_id, like=svc.ACTION_REMOVED))[0][2] == {
        "disposition": "deleted"
    }
    # audit rows never hold the git URL, name or credentials
    async with pg_dal.engine.connect() as conn:
        leaked = (
            await conn.execute(
                text("SELECT count(*) FROM audit_log WHERE details::text ILIKE ANY(:p)"),
                {"p": [f"%{SECRET}%", f"%{USERNAME}%", "%github.com%"]},
            )
        ).scalar_one()
    assert leaked == 0


async def test_approve_and_signature_audit_rows(install_dal: Any, env: Any, sink: Sink) -> None:
    src, _platform, _sidecar = env
    ok = _candidate(src, name="okay")
    summary = await _ingest(install_dal, src, ok)
    by_app = {b.app_id: b for b in summary.bundles}
    await ing.approve_source_version(
        install_dal, src, app_id=ok.assigned_app_id, version="1.0.0", approved_by=7,
        expected_permission_hash=by_app[ok.assigned_app_id].permission_hash,
    )  # fmt: skip
    rows = list(await install_dal(install_dal.audit_log.target_id == str(src.public_id)).select())
    actions = sorted(r.action for r in rows)
    print(f"audit check: rows={len(actions)} {actions}")
    assert actions == sorted([svc.ACTION_VERSION_APPROVED, svc.ACTION_SIGNATURE_ISSUED])
    outcomes = {dict(p.attributes)["outcome"] for p in sink.points("marketplace.approve.total")}
    assert outcomes == {"signed"}
    spans = {s.name for s in sink.spans.get_finished_spans()}
    assert "marketplace.source.approve" in spans


class _RaisingExporter(SpanExporter):
    def export(self, spans: Any) -> SpanExportResult:
        raise RuntimeError("exporter down")


class _BoomInstrument:
    def add(self, *a: Any, **k: Any) -> None:
        raise RuntimeError("meter down")

    record = add


class _BoomMeter:
    def create_counter(self, *a: Any, **k: Any) -> _BoomInstrument:
        return _BoomInstrument()

    create_histogram = create_up_down_counter = create_counter


class _BoomTracer:
    def start_as_current_span(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("tracer down")


async def test_dead_exporter_never_fails_the_operation(
    pg_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sha = _remote(tmp_path, with_bundle=True)
    src = await _source(pg_dal, sha)
    scratch = tmp_path / "s"
    scratch.mkdir()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(_RaisingExporter()))
    # (1) dead span exporter, (2) dead meter, (3) dead tracer: the scan still succeeds
    monkeypatch.setattr(bundle_telemetry, "_TRACER", tp.get_tracer("t"))
    monkeypatch.setattr(bundle_telemetry, "_METER", _BoomMeter())
    assert (await sc.scan_source(src, workdir=scratch, dal=pg_dal)).examined == 1
    monkeypatch.setattr(bundle_telemetry, "_TRACER", _BoomTracer())
    with caplog.at_level(logging.WARNING):
        assert (await sc.scan_source(src, workdir=scratch, dal=pg_dal)).examined == 1
    assert any("telemetry emit failed" in r.getMessage() for r in caplog.records)
    assert any("span start failed" in r.getMessage() for r in caplog.records)


async def test_span_body_exception_still_propagates_with_dead_tracer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink_exc = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(sink_exc))
    monkeypatch.setattr(bundle_telemetry, "_TRACER", tp.get_tracer("t"))
    with pytest.raises(ValueError, match="real failure"):
        async with tel.source_span("marketplace.source.scan", uuid.uuid4()):
            raise ValueError("real failure")
    (span,) = sink_exc.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.ERROR


def test_otlp_destination_is_env_only() -> None:
    """The module never names an endpoint, exporter or provider: destination is `OTEL_*` env."""
    src = Path(tel.__file__).read_text()
    for banned in (
        "http://",
        "https://",
        "4317",
        "4318",
        "OTLPSpanExporter",
        "set_tracer_provider",
    ):
        assert banned not in src


@pytest.mark.parametrize(
    ("reason", "category"),
    [
        ("no_bundles", "zero_examined"),
        ("archive_bomb", "guard"),
        ("build_failed:oom", "build"),
        ("manifest_invalid", "validation"),
        (f"weird {SECRET}", "other"),
    ],
)
def test_reason_category_is_bounded(reason: str, category: str) -> None:
    assert tel.reason_category(reason) == category
