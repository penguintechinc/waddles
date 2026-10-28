"""G1: sandbox scan -> build -> ingest orchestration (Job/`build_and_ingest` mocked, scan real)."""

from __future__ import annotations

import dataclasses
import shutil
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from services import marketplace_source_build as mb
from services import marketplace_source_scanner as sc
from services.marketplace_sandbox_builder import (
    SandboxBuilderConfig,
    SandboxBuildError,
    pack_bundle_dir,
)
from services.marketplace_sandbox_warning import WARNING_HASH
from services.marketplace_source_ingest import (
    OUTCOME_NEEDS_APPROVAL,
    OUTCOME_REJECTED,
    IngestedBundle,
)
from services.marketplace_source_service import BuildMode, SourceRecord, SourceStatus
from tests.malicious_source_fixtures import add_bundle, commit_bare, make_source, manifest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")
CFG = SandboxBuilderConfig(image="img@sha256:" + "0" * 64, namespace="ns", instance_label="i")


@pytest.fixture(autouse=True)
def _transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(sc.TEST_TRANSPORT_ENV, "1")

    async def _ok(url: str, *, allowed_schemes: tuple[str, ...]) -> str:
        return url

    monkeypatch.setattr(sc, "validate_outbound_url", _ok)
    yield
    sc._test_remote = None


class Calls:
    """Records the stubbed persistence/builder calls."""

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.statuses: list[tuple[SourceStatus, str | None]] = []
        self.built: list[str] = []
        self.tarballs: dict[str, list[str]] = {}
        self.existing: set[str] = set()
        self.outcome: Any = None  # callable(candidate) -> IngestedBundle | raises


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    c = Calls()

    async def update_status(
        dal: Any, pid: Any, status: SourceStatus, *, last_scan_error: str | None = None, **kw: Any
    ) -> None:
        c.statuses.append((status, last_scan_error))

    async def existing(dal: Any, app_id: str, version: str) -> str | None:
        return "d" if app_id in c.existing else None

    async def audit(*a: Any, **k: Any) -> None:
        return None

    async def build_and_ingest(
        dal: Any, source: SourceRecord, cand: Any, tarball: Path, **kw: Any
    ) -> IngestedBundle:
        assert kw["config"] is CFG and kw["tier"] == "enterprise"
        with tarfile.open(tarball) as t:
            c.tarballs[cand.dir_name] = t.getnames()
        c.built.append(cand.dir_name)
        if c.outcome is not None:
            return c.outcome(cand)  # type: ignore[no-any-return]
        return IngestedBundle(cand.assigned_app_id, cand.version, OUTCOME_NEEDS_APPROVAL)

    monkeypatch.setattr(mb, "update_status", update_status)
    monkeypatch.setattr(sc, "update_status", update_status)
    monkeypatch.setattr(sc, "audit_source_event", audit)
    monkeypatch.setattr(mb, "_existing_digest", existing)
    monkeypatch.setattr(mb, "build_and_ingest", build_and_ingest)
    return c


def _sandbox_source(tmp_path: Path, names: tuple[str, ...] = ("alpha", "beta")) -> SourceRecord:
    work = tmp_path / "work"
    work.mkdir()
    for n in names:
        add_bundle(
            work,
            "python",
            n,
            manifest(f"acme.{n}", artifact="source"),
            files={"src/app.py": "x = 1\n"},
        )
    bare = tmp_path / "remote.git"
    sha = commit_bare(work, bare)
    sc.enable_test_transport(f"file://{bare}")
    src = make_source(sha, mode=BuildMode.SANDBOX)
    return dataclasses.replace(
        src,
        sandbox_ack_by=1,
        sandbox_ack_at=src.created_at,
        sandbox_ack_text_hash=WARNING_HASH,
        sandbox_ack_commit_sha=sha,
    )


async def _run(src: SourceRecord, tier: str = "enterprise", **kw: Any) -> mb.SandboxRunSummary:
    return await mb.scan_build_and_ingest(
        object(),  # type: ignore[arg-type]
        src,
        tier=tier,
        tenant_id=1,
        tenant_slug="t",
        config=CFG,
        client_factory=lambda: object(),  # type: ignore[arg-type,return-value]
        **kw,
    )


async def test_builds_every_candidate_from_archived_source_tree(
    tmp_path: Path, calls: Calls
) -> None:
    summary = await _run(_sandbox_source(tmp_path))
    assert (summary.examined, summary.accepted, summary.built, summary.failed) == (2, 2, 2, 0)
    assert sorted(calls.built) == ["alpha", "beta"]
    assert "bundles/python/alpha/src/app.py" in calls.tarballs["alpha"]
    assert calls.statuses[-1][0] is SourceStatus.ACTIVE  # no failure recorded


async def test_existing_version_is_not_rebuilt(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def existing(dal: Any, app_id: str, version: str) -> str | None:
        return "d"

    monkeypatch.setattr(mb, "_existing_digest", existing)
    summary = await _run(_sandbox_source(tmp_path))
    assert summary.skipped_existing == 2 and summary.built == 0 and calls.built == []


async def test_build_failure_marks_error_and_counts_failed(tmp_path: Path, calls: Calls) -> None:
    def outcome(cand: Any) -> IngestedBundle:
        return IngestedBundle(cand.assigned_app_id, cand.version, OUTCOME_REJECTED, "build_failed")

    calls.outcome = outcome
    summary = await _run(_sandbox_source(tmp_path, ("alpha",)))
    assert summary.failed == 1 and summary.built == 0


async def test_unexpected_build_exception_is_status_error_not_silent(
    tmp_path: Path, calls: Calls
) -> None:
    def boom(cand: Any) -> IngestedBundle:
        raise RuntimeError("k8s api down")

    calls.outcome = boom
    summary = await _run(_sandbox_source(tmp_path, ("alpha", "beta")))
    assert summary.failed == 2 and summary.built == 0  # siblings still attempted
    errs = [e for s, e in calls.statuses if s is SourceStatus.ERROR]
    assert errs and errs[-1] == "sandbox_build:RuntimeError"


async def test_ingest_refusal_after_build_is_status_error(tmp_path: Path, calls: Calls) -> None:
    def outcome(cand: Any) -> IngestedBundle:
        return IngestedBundle(cand.assigned_app_id, cand.version, OUTCOME_REJECTED, "component_x")

    calls.outcome = outcome
    summary = await _run(_sandbox_source(tmp_path, ("alpha",)))
    assert summary.failed == 1
    assert (SourceStatus.ERROR, "sandbox_ingest:component_x") in calls.statuses


@pytest.mark.parametrize(
    ("tier", "mutate", "reason"),
    [
        ("professional", {}, "tier_not_enterprise"),
        ("free", {}, "tier_not_enterprise"),
        ("enterprise", {"sandbox_ack_text_hash": "stale"}, "sandbox_ack_missing_or_stale"),
        ("enterprise", {"sandbox_ack_by": None}, "sandbox_ack_missing_or_stale"),
        ("enterprise", {"sandbox_ack_commit_sha": "c" * 40}, "sandbox_ack_commit_mismatch"),
        ("enterprise", {"status": SourceStatus.DISABLED}, "source_disabled"),
        ("enterprise", {"build_mode": BuildMode.PREBUILT}, "not_a_sandbox_source"),
    ],
)
async def test_gates_block_before_any_clone_or_job(
    tmp_path: Path, calls: Calls, tier: str, mutate: dict[str, Any], reason: str
) -> None:
    src = dataclasses.replace(_sandbox_source(tmp_path), **mutate)
    summary = await _run(src, tier=tier)
    assert summary.failed == 1 and summary.built == 0 and calls.built == []
    assert calls.statuses == [(SourceStatus.ERROR, f"sandbox_build:{reason}")]


async def test_builder_not_configured_fails_loud(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SANDBOX_BUILDER_IMAGE", raising=False)
    summary = await mb.scan_build_and_ingest(
        object(),  # type: ignore[arg-type]
        _sandbox_source(tmp_path),
        tier="enterprise",
        tenant_id=1,
        tenant_slug="t",
    )
    assert summary.failed == 1 and calls.built == []
    assert calls.statuses == [(SourceStatus.ERROR, "sandbox_build:builder_not_configured")]


async def test_scan_rejection_is_failed_and_never_builds(tmp_path: Path, calls: Calls) -> None:
    src = dataclasses.replace(
        _sandbox_source(tmp_path), pinned_commit_sha="e" * 40, sandbox_ack_commit_sha="e" * 40
    )
    summary = await _run(src)
    assert summary.failed == 1 and calls.built == []
    assert any(s is SourceStatus.ERROR for s, _ in calls.statuses)  # set by scan_source


async def test_pack_failure_rejects_scan(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bad(*a: Any) -> Path:
        raise SandboxBuildError("source_contains_special_file")

    monkeypatch.setattr(mb, "pack_bundle_dir", bad)
    summary = await _run(_sandbox_source(tmp_path))
    assert summary.failed == 1 and calls.built == []
    assert any(e and "pack_failed" in e for _, e in calls.statuses)


async def test_launch_runs_in_background_and_crash_becomes_error(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _sandbox_source(tmp_path)

    async def crash(*a: Any, **k: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(mb, "scan_build_and_ingest", crash)
    task = mb.launch_sandbox_scan(object(), src, tier="enterprise")  # type: ignore[arg-type]
    assert not task.done()  # returned immediately -- not awaited on the request path
    assert await task is None
    assert (SourceStatus.ERROR, "sandbox_run:unexpected_error") in calls.statuses
    assert task not in mb._TASKS


async def test_launch_success_returns_summary(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _sandbox_source(tmp_path)
    monkeypatch.setattr(
        mb, "SandboxBuilderConfig", type("C", (), {"from_env": staticmethod(lambda: CFG)})
    )
    monkeypatch.setattr(mb, "KubernetesJobClient", lambda: object())
    task = mb.launch_sandbox_scan(
        object(),
        src,
        tier="enterprise",
        tenant_id=1,
        tenant_slug="t",  # type: ignore[arg-type]
    )
    summary = await task
    assert summary is not None and summary.built == 2


async def test_mark_pending_scan_sets_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[SourceStatus] = []
    src = _sandbox_source(tmp_path)

    async def us(dal: Any, pid: Any, status: SourceStatus, **kw: Any) -> SourceRecord:
        seen.append(status)
        return src

    monkeypatch.setattr(mb, "update_status", us)
    await mb.mark_pending_scan(object(), src)  # type: ignore[arg-type]
    assert seen == [SourceStatus.PENDING_SCAN]


def test_pack_bundle_dir_is_the_packer_used() -> None:
    assert mb.pack_bundle_dir is pack_bundle_dir
