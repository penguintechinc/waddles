"""Sandbox builder orchestrator: gated Job launch -> digest -> needs-approval (Task 7B)."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services import bundle_approval_service as approvals
from services import bundle_signing_service as signing
from services import bundle_version_service as versions
from services import marketplace_sandbox_builder as sb
from services import marketplace_source_ingest as ing
from services.bundle_component_validator import ComponentValidationResult
from services.bundle_manifest_v2 import parse_bundle_manifest_v2
from services.marketplace_sandbox_warning import WARNING_HASH
from services.marketplace_source_scanner import BundleCandidate
from services.marketplace_source_service import BuildMode, SourceStatus
from services.vendor_bundle_authz import source_namespace_prefix, source_short
from tests.malicious_source_fixtures import make_source, manifest

WASM = b"\x00asm\x0d\x00\x01\x00fake-component"
DIGEST = hashlib.sha256(WASM).hexdigest()
SHA = "a" * 40
CFG = sb.SandboxBuilderConfig(
    image="reg/builder@sha256:" + "0" * 64, namespace="wb", instance_label="rel"
)


def _log(wasm: bytes = WASM) -> str:
    return f"noise\n{sb.BEGIN_MARKER}\n{base64.b64encode(wasm).decode()}\n{sb.END_MARKER}\n"


class FakeClient:
    """In-memory `BuildJobClient` recording what the orchestrator asked Kubernetes to do."""

    def __init__(self, *, outcome: sb.JobOutcome | None = None, log: str | None = None) -> None:
        """Preset the Job outcome + pod log."""
        self.outcome = outcome or sb.JobOutcome(True)
        self.log = _log() if log is None else log
        self.jobs: list[dict[str, Any]] = []
        self.configmaps: list[tuple[str, bytes]] = []
        self.cleaned: list[str] = []

    async def create_source_configmap(self, namespace: str, name: str, data: bytes) -> None:
        self.configmaps.append((name, data))

    async def create_job(self, namespace: str, manifest: dict[str, Any]) -> None:
        self.jobs.append(manifest)

    async def wait_for_job(self, namespace: str, name: str, timeout_s: float) -> sb.JobOutcome:
        return self.outcome

    async def read_logs(self, namespace: str, job_name: str) -> str:
        return self.log

    async def cleanup(self, namespace: str, job_name: str, configmap_name: str) -> None:
        self.cleaned.append(job_name)


def _source(**over: Any) -> Any:
    base = make_source(SHA, mode=BuildMode.SANDBOX)
    ack = dict(
        sandbox_ack_by=1,
        sandbox_ack_at=datetime.now(UTC),
        sandbox_ack_text_hash=WARNING_HASH,
        sandbox_ack_commit_sha=SHA,
        status=SourceStatus.ACTIVE,
    )
    return dataclasses.replace(base, **{**ack, **over})


def _candidate(
    src: Any, *, sidecar: str | None = None, language: str = "python"
) -> BundleCandidate:
    app_id = f"{source_namespace_prefix(source_short(src))}hello"
    raw = manifest(app_id, "1.0.0", permissions=["messages:send"])
    raw["feature"] = app_id.rsplit(".", 1)[0]
    parsed = parse_bundle_manifest_v2(
        raw, known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=True
    )
    return BundleCandidate(
        language=language, dir_name="hello", assigned_app_id=app_id, version="1.0.0",
        manifest=parsed, wasm_path=None, sidecar_digest=sidecar, publisher_sig=None,
        permission_summary={}, raw_manifest=raw,
    )  # fmt: skip


def _read(path: Path) -> bytes:
    """Read a file (sync helper so async tests stay non-blocking-clean)."""
    return path.read_bytes()


@pytest.fixture
def tarball(tmp_path: Path) -> Path:
    bundle = tmp_path / "b"
    (bundle / "src").mkdir(parents=True)
    (bundle / "src" / "app.py").write_text("x = 1\n")
    return sb.pack_bundle_dir(bundle, "python", "hello", tmp_path / "s.tgz")


# --- gate ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "tier", "reason"),
    [
        ({}, "professional", "tier_not_enterprise"),
        ({}, "free", "tier_not_enterprise"),
        ({"sandbox_ack_at": None}, "enterprise", "sandbox_ack_missing_or_stale"),
        ({"sandbox_ack_by": None}, "enterprise", "sandbox_ack_missing_or_stale"),
        ({"sandbox_ack_text_hash": "old"}, "enterprise", "sandbox_ack_missing_or_stale"),
        ({"sandbox_ack_commit_sha": "b" * 40}, "enterprise", "sandbox_ack_commit_mismatch"),
        ({"build_mode": BuildMode.PREBUILT}, "enterprise", "not_a_sandbox_source"),
        ({"is_builtin": True}, "enterprise", "not_a_sandbox_source"),
        ({"status": SourceStatus.DISABLED}, "enterprise", "source_disabled"),
    ],
)
async def test_refused_launches_no_job(over: Any, tier: str, reason: str, tarball: Path) -> None:
    src, client = _source(**over), FakeClient()
    with pytest.raises(sb.SandboxBuildError) as ei:
        await sb.run_sandbox_build(
            src, _candidate(src), tarball, config=CFG, tier=tier, client=client
        )
    assert ei.value.reason == reason
    assert client.jobs == [] and client.configmaps == []


# --- Job spec ------------------------------------------------------------------------------


async def test_job_spec_is_locked_down(tarball: Path) -> None:
    src, client = _source(), FakeClient()
    src_bytes = _read(tarball)
    art = await sb.run_sandbox_build(
        src, _candidate(src), tarball, config=CFG, tier="enterprise", client=client
    )
    assert art.sha256 == DIGEST and art.wasm_bytes == WASM and art.reproducible is None
    (job,) = client.jobs
    pod = job["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == CFG.deadline_seconds
    assert job["spec"]["ttlSecondsAfterFinished"] == CFG.ttl_seconds_after_finished
    assert pod["automountServiceAccountToken"] is False and pod["enableServiceLinks"] is False
    assert not (pod["hostNetwork"] or pod["hostPID"] or pod["hostIPC"])
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    sc = c["securityContext"]
    assert sc["readOnlyRootFilesystem"] is True and sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"] == {"drop": ["ALL"]} and sc["runAsNonRoot"] is True
    assert set(c["resources"]["limits"]) == {"cpu", "memory", "ephemeral-storage"}
    assert [e["name"] for e in c["env"]] == ["SANDBOX_MAX_WASM_BYTES"]  # no creds/secrets
    assert "envFrom" not in c and "secret" not in str(pod["volumes"]).lower()
    assert {v["name"] for v in pod["volumes"]} == {"source", "work"}
    assert next(m for m in c["volumeMounts"] if m["name"] == "source")["readOnly"] is True
    assert pod["volumes"][1]["emptyDir"]["sizeLimit"]
    labels = job["metadata"]["labels"]
    assert labels["app.kubernetes.io/component"] == sb.COMPONENT_LABEL  # selected by deny-all CNP
    assert client.cleaned == [job["metadata"]["name"]]
    assert client.configmaps[0][1] == src_bytes


async def test_reproducible_flag_from_committed_artifact(tarball: Path) -> None:
    src = _source()
    ok = await sb.run_sandbox_build(
        src,
        _candidate(src, sidecar=DIGEST),
        tarball,
        config=CFG,
        tier="enterprise",
        client=FakeClient(),
    )
    bad = await sb.run_sandbox_build(
        src,
        _candidate(src, sidecar="0" * 64),
        tarball,
        config=CFG,
        tier="enterprise",
        client=FakeClient(),
    )
    assert (ok.reproducible, bad.reproducible) == (True, False)


# --- build failure / guards ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("client", "reason"),
    [
        (FakeClient(outcome=sb.JobOutcome(False, "timeout")), "build_failed:timeout"),
        (
            FakeClient(outcome=sb.JobOutcome(False, "DeadlineExceeded")),
            "build_failed:deadlineexceeded",
        ),
        (FakeClient(log="no markers"), "no_artifact_in_output"),
        (FakeClient(log=_log(b"notwasm-bytes")), "output_not_wasm"),
        (FakeClient(log=f"{sb.BEGIN_MARKER}\n!!!\n{sb.END_MARKER}"), "artifact_not_base64"),
    ],
)
async def test_failures_raise_and_still_clean_up(
    client: FakeClient, reason: str, tarball: Path
) -> None:
    src = _source()
    with pytest.raises(sb.SandboxBuildError) as ei:
        await sb.run_sandbox_build(
            src, _candidate(src), tarball, config=CFG, tier="enterprise", client=client
        )
    assert ei.value.reason == reason
    assert len(client.cleaned) == 1


async def test_oversize_output_and_source_rejected(tarball: Path, tmp_path: Path) -> None:
    src = _source()
    small = dataclasses.replace(CFG, max_wasm_bytes=8)
    with pytest.raises(sb.SandboxBuildError, match="output_too_large"):
        await sb.run_sandbox_build(
            src, _candidate(src), tarball, config=small, tier="enterprise", client=FakeClient()
        )
    tiny = dataclasses.replace(CFG, max_source_bytes=4)
    client = FakeClient()
    with pytest.raises(sb.SandboxBuildError, match="source_size_invalid"):
        await sb.run_sandbox_build(
            src, _candidate(src), tarball, config=tiny, tier="enterprise", client=client
        )
    assert client.jobs == []


async def test_unsupported_language_refused_before_job(tarball: Path) -> None:
    src, client = _source(), FakeClient()
    with pytest.raises(sb.SandboxBuildError, match="unsupported_language"):
        await sb.run_sandbox_build(
            src,
            _candidate(src, language="csharp"),
            tarball,
            config=CFG,
            tier="enterprise",
            client=client,
        )
    assert client.jobs == []


def test_builder_reported_digest_is_never_trusted() -> None:
    log = f"digest: {'f' * 64}\n" + _log()
    assert hashlib.sha256(sb.parse_build_log(log, max_wasm_bytes=1024)).hexdigest() == DIGEST


def test_pack_refuses_symlinks(tmp_path: Path) -> None:
    b = tmp_path / "b"
    b.mkdir()
    (b / "link").symlink_to("/etc/passwd")
    with pytest.raises(sb.SandboxBuildError, match="source_contains_special_file"):
        sb.pack_bundle_dir(b, "python", "hello", tmp_path / "o.tgz")


def test_pack_layout(tarball: Path) -> None:
    with tarfile.open(tarball) as t:
        assert {m.name for m in t.getmembers()} >= {"bundles/python/hello/src/app.py"}


# --- ingest hand-off (real DAL) ----------------------------------------------------------------


@pytest.fixture
async def env(install_dal: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        versions, "validate_component", AsyncMock(return_value=ComponentValidationResult(ok=True))
    )
    monkeypatch.setattr(
        versions.storage_service,
        "upload_bundle_component",
        AsyncMock(side_effect=lambda a, v, d, b: f"bundles/{a}/{v}/{d}.wasm"),
    )
    monkeypatch.setattr(
        signing.storage_service, "write_bundle_sidecar", AsyncMock(return_value="k")
    )
    src = _source()
    await install_dal.marketplace_sources.async_insert(public_id=str(src.public_id))
    statuses: list[tuple[SourceStatus, str | None]] = []

    async def _pk(dal: Any, public_id: uuid.UUID) -> int | None:
        rows = await dal(dal.marketplace_sources.public_id == str(public_id)).select()
        row = rows.first()
        return None if row is None else int(row.id)

    async def _get(dal: Any, public_id: uuid.UUID) -> Any:
        return src

    async def _status(dal: Any, public_id: uuid.UUID, status: SourceStatus, **kw: Any) -> Any:
        statuses.append((status, kw.get("last_scan_error")))
        return src

    monkeypatch.setattr(sb, "get_source_pk", _pk)
    monkeypatch.setattr(ing, "get_source_pk", _pk)
    monkeypatch.setattr(ing, "get_source", _get)
    monkeypatch.setattr(sb, "update_status", _status)
    return src, statuses


async def _run(
    install_dal: Any, src: Any, client: FakeClient, tarball: Path, tier: str = "enterprise"
) -> ing.IngestedBundle:
    return await sb.build_and_ingest(
        install_dal, src, _candidate(src), tarball, config=CFG, tier=tier, client=client,
        tenant_id=1, tenant_slug="acme", valkey_client=AsyncMock(),
    )  # fmt: skip


async def test_success_creates_needs_approval_not_installed(
    install_dal: Any, env: Any, tarball: Path
) -> None:
    src, statuses = env
    out = await _run(install_dal, src, FakeClient(), tarball)
    assert out.outcome == ing.OUTCOME_NEEDS_APPROVAL and out.permission_hash
    row = (await install_dal(install_dal.app_versions.app_id == out.app_id).select()).first()
    assert row.source_id is not None and row.artifact_digest == DIGEST
    assert row.artifact_signature is None  # platform signs only on human approval
    assert (await approvals.list_global_installs(install_dal)) == []  # NOT auto-installed
    audit = (
        await install_dal(install_dal.audit_log.action == "marketplace_sandbox_built").select()
    ).first()
    assert audit.details["wasm_sha256"] == DIGEST and audit.details["builder_image"] == CFG.image
    assert statuses == []


@pytest.mark.parametrize("case", ["timeout", "no_artifact", "not_enterprise", "no_ack"])
async def test_failure_or_refusal_sets_error_and_no_version(
    install_dal: Any, env: Any, tarball: Path, case: str
) -> None:
    src, statuses = env
    client = FakeClient()
    tier = "enterprise"
    if case == "timeout":
        client = FakeClient(outcome=sb.JobOutcome(False, "timeout"))
    elif case == "no_artifact":
        client = FakeClient(log="nothing")
    elif case == "not_enterprise":
        tier = "professional"
    else:
        src = dataclasses.replace(src, sandbox_ack_at=None)
    out = await _run(install_dal, src, client, tarball, tier)
    assert out.outcome == ing.OUTCOME_REJECTED
    assert (
        await install_dal(install_dal.app_versions.app_id == out.app_id).select()
    ).first() is None
    assert [s for s, _ in statuses] == [SourceStatus.ERROR]
    assert statuses[0][1] and statuses[0][1].startswith("sandbox_build:")
    if case in ("not_enterprise", "no_ack"):
        assert client.jobs == []
