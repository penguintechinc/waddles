"""Sandboxed in-cluster builder orchestrator for SANDBOX marketplace sources (Phase 1, Task 7B).

For a scanned bundle of a SANDBOX source this launches ONE locked-down K8s Job that compiles the
bundle's vendored source to WASM, reads the artifact back, recomputes its sha256 (a builder-
reported digest is never trusted) and feeds the bytes into the SAME ingest tail as prebuilt
bundles (`create_unapproved_version`) -- minus publisher attestation, because the PLATFORM is the
builder and (on human approval) the signer::

    gate(Enterprise + valid ack) -> ConfigMap(source.tgz) + Job -> wait -> pod log -> WASM
        -> sha256 -> create_unapproved_version (PUBLISHED, NOT installed) -> NEEDS_APPROVAL
    human platform:admin -> approve_source_version -> platform Ed25519 re-sign -> install

Hand-off choice (documented for the PR): the source tarball travels in a per-build ConfigMap
(mounted read-only) and the artifact returns via the pod log (base64 between fixed markers), so
the builder holds NO database/S3/hub credential, mounts NO service-account token and needs NO
network path at all (the Job's CiliumNetworkPolicy denies all egress, DNS included). Cost: the
gzip source is capped at the ConfigMap object limit (~1 MiB).

Job lockdown (defense in depth with the chart's CNP/Tetragon/ResourceQuota): non-root, read-only
root FS, all caps dropped, seccomp RuntimeDefault, `automountServiceAccountToken: false`, no
secrets/env credentials, `enableServiceLinks: false`, emptyDir scratch with a size limit,
CPU/mem/ephemeral-storage limits, `activeDeadlineSeconds`, `backoffLimit: 0`, TTL cleanup.

Any build failure, timeout or guard trip rejects: source -> ERROR with a reason, NO version row.
Logs/audit carry app_id + counts only (no source content, no PII).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import importlib
import logging
import os
import re
import tarfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from flask_core.tier_catalog import TIER_ENTERPRISE, TIER_LEVELS
from penguin_dal import AsyncDB

from services.bundle_telemetry import bundle_span, get_meter
from services.errors import ApiError, conflict
from services.marketplace_sandbox_warning import WARNING_HASH
from services.marketplace_source_ingest import (
    OUTCOME_REJECTED,
    IngestedBundle,
    create_unapproved_version,
)
from services.marketplace_source_scanner import BundleCandidate
from services.marketplace_source_service import (
    BuildMode,
    SourceRecord,
    SourceStatus,
    get_source_pk,
    update_status,
)

logger = logging.getLogger(__name__)

BEGIN_MARKER = "WADDLE_WASM_B64_BEGIN"
END_MARKER = "WADDLE_WASM_B64_END"
COMPONENT_LABEL = "sandbox-builder"
SUPPORTED_LANGUAGES = frozenset({"python", "rust"})
_REASON_RE = re.compile(r"[^a-z0-9_:.\-]")
_WASM_MAGIC = b"\x00asm"
#: ConfigMap objects are capped at 1 MiB by the API server; leave headroom for base64/metadata.
CONFIGMAP_SOURCE_LIMIT = 700 * 1024


class SandboxBuildError(Exception):
    """A build was refused/failed; `reason` is a short machine-checkable code."""

    def __init__(self, reason: str) -> None:
        """Store the sanitized reason."""
        self.reason = _REASON_RE.sub("_", reason.lower())[:120]
        super().__init__(self.reason)


@dataclass(slots=True, frozen=True)
class SandboxBuilderConfig:
    """Build-Job settings; values come from the chart `sandboxBuilder:` block."""

    image: str
    namespace: str
    instance_label: str
    service_account: str = "waddlebot-sandbox-builder"
    cpu_limit: str = "2"
    memory_limit: str = "2Gi"
    ephemeral_storage_limit: str = "2Gi"
    scratch_size_limit: str = "1536Mi"
    deadline_seconds: int = 600
    wait_timeout_seconds: int = 660
    ttl_seconds_after_finished: int = 300
    max_source_bytes: int = CONFIGMAP_SOURCE_LIMIT
    max_wasm_bytes: int = 8 * 1024 * 1024
    run_as_user: int = 10001

    @classmethod
    def from_env(cls) -> SandboxBuilderConfig:
        """Load from `SANDBOX_BUILDER_*` env (set by the chart); fails loud when not configured."""
        env = os.environ
        image = env.get("SANDBOX_BUILDER_IMAGE", "")
        if not image:
            raise SandboxBuildError("builder_not_configured")
        defaults = cls(image=image, namespace="", instance_label="")
        return cls(
            image=image,
            namespace=env["SANDBOX_BUILDER_NAMESPACE"],
            instance_label=env["SANDBOX_BUILDER_INSTANCE"],
            service_account=env.get("SANDBOX_BUILDER_SERVICE_ACCOUNT", defaults.service_account),
            cpu_limit=env.get("SANDBOX_BUILDER_CPU_LIMIT", defaults.cpu_limit),
            memory_limit=env.get("SANDBOX_BUILDER_MEMORY_LIMIT", defaults.memory_limit),
            ephemeral_storage_limit=env.get(
                "SANDBOX_BUILDER_EPHEMERAL_LIMIT", defaults.ephemeral_storage_limit
            ),
            deadline_seconds=int(env.get("SANDBOX_BUILDER_DEADLINE_SECONDS", "600")),
            wait_timeout_seconds=int(env.get("SANDBOX_BUILDER_DEADLINE_SECONDS", "600")) + 60,
            max_wasm_bytes=int(env.get("SANDBOX_BUILDER_MAX_WASM_BYTES", str(8 * 1024 * 1024))),
        )


@dataclass(slots=True, frozen=True)
class BuildArtifact:
    """A successfully built, size-checked, hub-digested artifact."""

    wasm_bytes: bytes
    sha256: str
    reproducible: bool | None
    duration_s: float


@dataclass(slots=True, frozen=True)
class JobOutcome:
    """Terminal state of a build Job."""

    succeeded: bool
    reason: str = ""


class BuildJobClient(Protocol):
    """The Kubernetes operations the orchestrator needs (real impl: `KubernetesJobClient`)."""

    async def create_source_configmap(self, namespace: str, name: str, data: bytes) -> None:
        """Create the per-build ConfigMap holding `source.tgz` (binaryData)."""

    async def create_job(self, namespace: str, manifest: dict[str, Any]) -> None:
        """Create the build Job."""

    async def wait_for_job(self, namespace: str, name: str, timeout_s: float) -> JobOutcome:
        """Block until the Job succeeds/fails or `timeout_s` elapses (then failed/timeout)."""

    async def read_logs(self, namespace: str, job_name: str) -> str:
        """Return the build pod's stdout log."""

    async def cleanup(self, namespace: str, job_name: str, configmap_name: str) -> None:
        """Best-effort delete of the Job (+ pods) and its ConfigMap."""


def pack_bundle_dir(bundle_dir: Path, language: str, dir_name: str, dest: Path) -> Path:
    """Tar+gzip one scanned bundle dir as `bundles/<language>/<dir_name>/...` (no symlinks)."""
    prefix = f"bundles/{language}/{dir_name}"
    with tarfile.open(dest, "w:gz") as tar:
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise SandboxBuildError("source_contains_special_file")
            tar.add(
                path, arcname=f"{prefix}/{path.relative_to(bundle_dir).as_posix()}", recursive=False
            )
    return dest


def assert_build_allowed(source: SourceRecord, tier: str) -> None:
    """Fail closed unless this is an external SANDBOX source, Enterprise tier, with a valid ack.

    Defense in depth behind the API gates: the ack must exist, carry the CURRENT warning hash and
    be bound to the commit being built (a stale/voided ack, or a pin that moved, is refused).
    """
    if source.is_builtin or source.build_mode != BuildMode.SANDBOX:
        raise SandboxBuildError("not_a_sandbox_source")
    if source.status == SourceStatus.DISABLED:
        raise SandboxBuildError("source_disabled")
    if TIER_LEVELS.get(tier, 0) < TIER_LEVELS[TIER_ENTERPRISE]:
        raise SandboxBuildError("tier_not_enterprise")
    if (
        source.sandbox_ack_by is None
        or source.sandbox_ack_at is None
        or source.sandbox_ack_text_hash != WARNING_HASH
    ):
        raise SandboxBuildError("sandbox_ack_missing_or_stale")
    if (
        not source.sandbox_ack_commit_sha
        or source.sandbox_ack_commit_sha != source.pinned_commit_sha
    ):
        raise SandboxBuildError("sandbox_ack_commit_mismatch")


def build_job_manifest(
    cfg: SandboxBuilderConfig, *, job_name: str, configmap_name: str, language: str, dir_name: str
) -> dict[str, Any]:
    """Render the locked-down batch/v1 Job (the same posture the chart's CNP/Tetragon police)."""
    labels = {
        "app.kubernetes.io/name": "waddlebot",
        "app.kubernetes.io/instance": cfg.instance_label,
        "app.kubernetes.io/component": COMPONENT_LABEL,
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name, "namespace": cfg.namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": cfg.deadline_seconds,
            "ttlSecondsAfterFinished": cfg.ttl_seconds_after_finished,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": cfg.service_account,
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "hostNetwork": False,
                    "hostPID": False,
                    "hostIPC": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": cfg.run_as_user,
                        "runAsGroup": cfg.run_as_user,
                        "fsGroup": cfg.run_as_user,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "builder",
                            "image": cfg.image,
                            "imagePullPolicy": "IfNotPresent",
                            "args": [language, dir_name],
                            "env": [
                                {
                                    "name": "SANDBOX_MAX_WASM_BYTES",
                                    "value": str(cfg.max_wasm_bytes),
                                },
                            ],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "runAsNonRoot": True,
                                "runAsUser": cfg.run_as_user,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "250m", "memory": "256Mi"},
                                "limits": {
                                    "cpu": cfg.cpu_limit,
                                    "memory": cfg.memory_limit,
                                    "ephemeral-storage": cfg.ephemeral_storage_limit,
                                },
                            },
                            "volumeMounts": [
                                {"name": "source", "mountPath": "/input", "readOnly": True},
                                {"name": "work", "mountPath": "/work"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "source", "configMap": {"name": configmap_name}},
                        {"name": "work", "emptyDir": {"sizeLimit": cfg.scratch_size_limit}},
                    ],
                },
            },
        },
    }


def parse_build_log(log: str, *, max_wasm_bytes: int) -> bytes:
    """Extract + validate the base64 WASM between the markers (size cap, magic, non-empty)."""
    start = log.find(BEGIN_MARKER)
    end = log.find(END_MARKER, start + 1)
    if start < 0 or end < 0:
        raise SandboxBuildError("no_artifact_in_output")
    b64 = "".join(log[start + len(BEGIN_MARKER) : end].split())
    if len(b64) > (max_wasm_bytes * 4) // 3 + 8:
        raise SandboxBuildError("output_too_large")
    try:
        data = base64.b64decode(b64, validate=True)
    except binascii.Error as exc:
        raise SandboxBuildError("artifact_not_base64") from exc
    if len(data) > max_wasm_bytes:
        raise SandboxBuildError("output_too_large")
    if not data.startswith(_WASM_MAGIC):
        raise SandboxBuildError("output_not_wasm")
    return data


async def run_sandbox_build(
    source: SourceRecord,
    candidate: BundleCandidate,
    source_tarball: Path,
    *,
    config: SandboxBuilderConfig,
    tier: str,
    client: BuildJobClient,
) -> BuildArtifact:
    """Run the gated build Job and return the hub-digested artifact (raises `SandboxBuildError`)."""
    assert_build_allowed(source, tier)
    if candidate.language not in SUPPORTED_LANGUAGES:
        raise SandboxBuildError(f"unsupported_language:{candidate.language}")
    tarball = await asyncio.to_thread(source_tarball.read_bytes)
    if not tarball or len(tarball) > config.max_source_bytes:
        raise SandboxBuildError("source_size_invalid")
    suffix = uuid.uuid4().hex[:12]
    job_name, cm_name = f"sbx-{suffix}", f"sbx-src-{suffix}"
    manifest = build_job_manifest(
        config,
        job_name=job_name,
        configmap_name=cm_name,
        language=candidate.language,
        dir_name=candidate.dir_name,
    )
    started = time.monotonic()
    meter = get_meter()
    async with bundle_span("marketplace.sandbox.build", app_id=candidate.assigned_app_id):
        try:
            await client.create_source_configmap(config.namespace, cm_name, tarball)
            await client.create_job(config.namespace, manifest)
            outcome = await client.wait_for_job(
                config.namespace, job_name, float(config.wait_timeout_seconds)
            )
            if not outcome.succeeded:
                raise SandboxBuildError(f"build_failed:{outcome.reason or 'unknown'}")
            logs = await client.read_logs(config.namespace, job_name)
            wasm = parse_build_log(logs, max_wasm_bytes=config.max_wasm_bytes)
        except SandboxBuildError as exc:
            meter.create_counter("marketplace.sandbox.build.total").add(1, {"outcome": exc.reason})
            raise
        finally:
            try:
                await client.cleanup(config.namespace, job_name, cm_name)
            except Exception:  # noqa: BLE001 - cleanup is best-effort; TTL reaps the Job anyway
                logger.warning("sandbox build cleanup failed", extra={"job": job_name})
    digest = hashlib.sha256(wasm).hexdigest()  # recomputed here, never the builder's word
    reproducible = None if not candidate.sidecar_digest else candidate.sidecar_digest == digest
    meter.create_counter("marketplace.sandbox.build.total").add(1, {"outcome": "ok"})
    return BuildArtifact(wasm, digest, reproducible, time.monotonic() - started)


async def build_and_ingest(
    install_dal: AsyncDB,
    source: SourceRecord,
    candidate: BundleCandidate,
    source_tarball: Path,
    *,
    config: SandboxBuilderConfig,
    tier: str,
    client: BuildJobClient,
    tenant_id: int,
    tenant_slug: str,
    requested_by: int | None = None,
    valkey_client: Any | None = None,
) -> IngestedBundle:
    """Build one sandbox bundle and create its NEEDS_APPROVAL version (never installs/signs).

    A refusal, build failure, timeout or guard trip leaves NO version and marks the source ERROR
    with the reason; the caller gets a rejected `IngestedBundle`.
    """
    source_pk = await get_source_pk(install_dal, source.public_id)
    if source_pk is None:
        raise ApiError("marketplace source not found", 404, "source_not_found")
    try:
        assert_build_allowed(source, tier)
        artifact = await run_sandbox_build(
            source, candidate, source_tarball, config=config, tier=tier, client=client
        )
    except SandboxBuildError as exc:
        logger.warning(
            "marketplace sandbox build rejected",
            extra={"app_id": candidate.assigned_app_id, "reason": exc.reason},
        )
        await update_status(
            install_dal,
            source.public_id,
            SourceStatus.ERROR,
            last_scan_error=f"sandbox_build:{exc.reason}",
        )
        return IngestedBundle(
            candidate.assigned_app_id, candidate.version, OUTCOME_REJECTED, exc.reason
        )
    if source.status == SourceStatus.DISABLED:
        raise conflict("marketplace source is disabled")
    return await create_unapproved_version(
        install_dal,
        source,
        source_pk,
        candidate,
        artifact.wasm_bytes,
        audit_action="marketplace_sandbox_built",
        audit_details={
            "builder_image": config.image,
            "build_duration_s": round(artifact.duration_s, 1),
            "reproducible": artifact.reproducible,
        },
        key_fingerprint=None,
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        requested_by=requested_by,
        valkey_client=valkey_client,
    )


class KubernetesJobClient:
    """`BuildJobClient` over `kubernetes_asyncio` (in-cluster config; lazy import, fail-loud)."""

    def __init__(self) -> None:
        """Import the client library and load in-cluster config; raises if unavailable."""
        try:
            k8s_client = importlib.import_module("kubernetes_asyncio.client")
            k8s_config = importlib.import_module("kubernetes_asyncio.config")
        except ImportError as exc:  # pragma: no cover - dependency not installed
            raise SandboxBuildError("k8s_client_unavailable") from exc
        self._k = k8s_client
        self._cfg = k8s_config
        self._apis: tuple[Any, Any] | None = None

    async def _api(self) -> tuple[Any, Any]:
        """Return (BatchV1Api, CoreV1Api), loading in-cluster config + one ApiClient once."""
        if self._apis is None:
            self._cfg.load_incluster_config()
            api = self._k.ApiClient()
            self._apis = (self._k.BatchV1Api(api), self._k.CoreV1Api(api))
        return self._apis

    async def create_source_configmap(self, namespace: str, name: str, data: bytes) -> None:
        """Create the source ConfigMap (binaryData is base64 on the wire)."""
        _, core = await self._api()
        body = self._k.V1ConfigMap(
            metadata=self._k.V1ObjectMeta(
                name=name, labels={"app.kubernetes.io/component": COMPONENT_LABEL}
            ),
            binary_data={"source.tgz": base64.b64encode(data).decode("ascii")},
        )
        await core.create_namespaced_config_map(namespace, body)

    async def create_job(self, namespace: str, manifest: dict[str, Any]) -> None:
        """Create the Job from the rendered manifest."""
        batch, _ = await self._api()
        await batch.create_namespaced_job(namespace, manifest)

    async def wait_for_job(self, namespace: str, name: str, timeout_s: float) -> JobOutcome:
        """Poll Job status until a terminal condition or `timeout_s`."""
        batch, _ = await self._api()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            job = await batch.read_namespaced_job_status(name, namespace)
            if job.status.succeeded:
                return JobOutcome(True)
            for cond in job.status.conditions or []:
                if cond.type == "Failed" and cond.status == "True":
                    return JobOutcome(False, str(cond.reason or "failed"))
            await asyncio.sleep(2.0)
        return JobOutcome(False, "timeout")

    async def read_logs(self, namespace: str, job_name: str) -> str:
        """Return the (single) build pod's log."""
        _, core = await self._api()
        pods = await core.list_namespaced_pod(namespace, label_selector=f"job-name={job_name}")
        if not pods.items:
            raise SandboxBuildError("build_pod_missing")
        return str(await core.read_namespaced_pod_log(pods.items[0].metadata.name, namespace))

    async def cleanup(self, namespace: str, job_name: str, configmap_name: str) -> None:
        """Delete the Job (background-propagating to pods) and its ConfigMap."""
        batch, core = await self._api()
        await batch.delete_namespaced_job(job_name, namespace, propagation_policy="Background")
        await core.delete_namespaced_config_map(configmap_name, namespace)


__all__ = [
    "BuildArtifact",
    "BuildJobClient",
    "JobOutcome",
    "KubernetesJobClient",
    "SandboxBuildError",
    "SandboxBuilderConfig",
    "assert_build_allowed",
    "build_and_ingest",
    "build_job_manifest",
    "pack_bundle_dir",
    "parse_build_log",
    "run_sandbox_build",
]
