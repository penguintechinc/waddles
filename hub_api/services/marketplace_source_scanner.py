"""Safe clone + guarded walk + validation of an external marketplace source (Phase 1, Task 3).

Pipeline (design 4.x): validate the https URL (url_guard, SSRF) -> `git init` + `git fetch
--depth 1 origin <pinned_sha>` through the egress proxy (no shell, sanitized env, https only,
no redirects, hooks/LFS/submodules disabled, wall-clock timeout, size monitor) -> verify the
fetched commit EQUALS `pinned_commit_sha` -> pre-checkout tree-listing guards -> checkout ->
filesystem guards (symlink escape, archive bombs, size/count/depth) -> walk
`bundles/{python,rust,csharp,javascript}/*/` -> validate each manifest via
`parse_bundle_manifest_v2`, enforce the source's `src-{short}` namespace, scan dependencies
against the supply-chain denylist. A guard violation rejects the whole scan (raises
`ScanRejectedError`); a per-bundle problem rejects only that bundle. The clone lives in an isolated
temp dir that is always removed. Logs/metrics carry `public_id` only (no URLs, no PII).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from penguin_dal import AsyncDB

from services import marketplace_source_telemetry as mkt_telemetry
from services.bundle_manifest_v2 import ManifestV2Error, parse_bundle_manifest_v2
from services.dependency_denylist import DepFinding, scan_bundle_dependencies
from services.errors import ApiError
from services.marketplace_source_guards import (
    GuardLimits,
    GuardViolationError,
    check_listing,
    check_tree,
    dir_size,
)
from services.marketplace_source_service import (
    ACTION_RESCANNED,
    ACTION_SCAN_REJECTED,
    BuildMode,
    SourceRecord,
    SourceStatus,
    audit_source_event,
    update_status,
)
from services.url_guard import validate_outbound_url
from services.vendor_bundle_authz import (
    enforce_source_namespace,
    source_namespace_prefix,
    source_short,
)

logger = logging.getLogger(__name__)

BUNDLE_LANGUAGES = ("python", "rust", "csharp", "javascript")
PROXY_ENV = "MARKETPLACE_GIT_PROXY_URL"
TEST_TRANSPORT_ENV = "MARKETPLACE_SCANNER_ALLOW_TEST_TRANSPORT"
_MAX_MANIFEST_BYTES = 1024 * 1024
_MONITOR_INTERVAL_S = 0.25

#: Test-only local remote (`file://` URL). Settable only when `TEST_TRANSPORT_ENV=1`.
_test_remote: str | None = None


class ScanRejectedError(Exception):
    """The whole source scan was rejected (guard trip, SHA mismatch, clone failure, ...)."""

    def __init__(self, reason: str, detail: str) -> None:
        """Store the machine-checkable `reason` and a human-readable `detail`."""
        self.reason = reason
        super().__init__(f"{reason}: {detail}")


@dataclass(slots=True, frozen=True)
class BundleCandidate:
    """One validated bundle discovered in a source (Task 5 consumes this)."""

    language: str
    dir_name: str
    assigned_app_id: str
    version: str
    manifest: Any
    wasm_path: Path | None
    sidecar_digest: str | None
    publisher_sig: str | None
    permission_summary: dict[str, Any]
    dep_findings: tuple[DepFinding, ...] = ()
    #: Namespace-rewritten manifest mapping (the exact input that was validated); Task 5 re-feeds
    #: it to `create_version`, which re-validates, so nothing unvalidated is ever persisted.
    raw_manifest: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class RejectedBundle:
    """A bundle that failed validation/namespace/denylist, with the reason."""

    language: str
    dir_name: str
    reason: str
    detail: str


@dataclass(slots=True, frozen=True)
class ScanResult:
    """Outcome of scanning one source at one pinned commit."""

    source_public_id: uuid.UUID
    commit_sha: str
    bundles: tuple[BundleCandidate, ...]
    rejected: tuple[RejectedBundle, ...]
    examined: int
    # Detached clone root is deleted after the scan; paths above are informational only.
    notes: tuple[str, ...] = field(default=())


def enable_test_transport(file_url: str | None) -> None:
    """Point the scanner at a local `file://` remote; refuses unless the test flag is set."""
    global _test_remote
    if os.environ.get(TEST_TRANSPORT_ENV) != "1":
        raise RuntimeError("scanner test transport is disabled outside tests")
    _test_remote = file_url


def _git_env(home: Path, *, proxy: str | None) -> dict[str, str]:
    """Minimal, sanitized environment for git (no inherited secrets/config)."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_LFS_SKIP_SMUDGE": "1",
        "LC_ALL": "C",
    }
    if proxy:
        env["HTTPS_PROXY"] = proxy
        env["https_proxy"] = proxy
    return env


def _git_argv(*args: str, proxy: str | None, local: bool) -> list[str]:
    """Build a hardened git argv (no shell): https-only, no hooks, no redirects."""
    hardening = [
        "-c", "protocol.allow=never",
        "-c", "protocol.https.allow=always",
        "-c", "core.hooksPath=" + os.devnull,
        "-c", "core.fsmonitor=false",
        "-c", "http.followRedirects=false",
        "-c", "submodule.recurse=false",
        "-c", "filter.lfs.required=false",
        "-c", "core.symlinks=true",
    ]  # fmt: skip
    if local:
        hardening += ["-c", "protocol.file.allow=always"]
    if proxy:
        hardening += ["-c", f"http.proxy={proxy}"]
    return ["git", *hardening, *args]


async def _run_git(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    limits: GuardLimits,
    deadline: float,
    watch: Path | None = None,
) -> str:
    """Run git (no shell) with a wall-clock deadline and an optional disk-size monitor."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ScanRejectedError("clone_timeout", "scan wall-clock budget exhausted")
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def monitor() -> None:
        """Kill git if the clone dir outgrows the size guard mid-transfer."""
        while True:
            await asyncio.sleep(_MONITOR_INTERVAL_S)
            if watch is not None and await asyncio.to_thread(dir_size, watch) > (
                limits.max_total_bytes
            ):
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                raise ScanRejectedError("clone_too_large", "clone exceeded max total size")

    mon = asyncio.create_task(monitor())
    comm = asyncio.create_task(proc.communicate())
    try:
        done, _ = await asyncio.wait(
            {mon, comm}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            raise ScanRejectedError("clone_timeout", f"git exceeded {limits.clone_timeout_s}s")
        if mon in done:
            mon.result()
        out, err = comm.result()
    finally:
        for task in (mon, comm):
            task.cancel()
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        await asyncio.gather(mon, comm, return_exceptions=True)
    if proc.returncode != 0:
        # https-only, no auth: stderr carries no credentials; keep only its last line.
        raise ScanRejectedError(
            "git_failed",
            err.decode("utf-8", "replace").strip().splitlines()[-1:][0]
            if err.strip()
            else f"exit {proc.returncode}",
        )
    return out.decode("utf-8", "replace")


async def _clone_pinned(source: SourceRecord, root: Path, limits: GuardLimits) -> None:
    """Fetch exactly `pinned_commit_sha` into `root`, verify it, apply tree guards, check out."""
    pinned = source.pinned_commit_sha
    if not pinned:
        raise ScanRejectedError("no_pinned_sha", "source has no pinned commit")
    if source.git_url is None:
        raise ScanRejectedError("no_url", "source has no git_url")
    local = _test_remote is not None
    try:
        await validate_outbound_url(source.git_url, allowed_schemes=("https",))
    except ApiError as exc:
        raise ScanRejectedError("url_rejected", str(exc.message)) from exc
    proxy = os.environ.get(PROXY_ENV)
    if not proxy and not local:
        raise ScanRejectedError(
            "no_egress_proxy", f"{PROXY_ENV} is not configured (direct egress denied)"
        )
    remote = _test_remote if local and _test_remote else source.git_url
    env = _git_env(root.parent, proxy=None if local else proxy)
    deadline = time.monotonic() + limits.clone_timeout_s

    async def git(*args: str, watch: Path | None = None) -> str:
        argv = _git_argv(*args, proxy=None if local else proxy, local=local)
        return await _run_git(
            argv, cwd=root, env=env, limits=limits, deadline=deadline, watch=watch
        )

    await git("init", "--quiet")
    await git("remote", "add", "origin", remote)
    await git("fetch", "--quiet", "--depth", "1", "--no-tags", "--no-recurse-submodules",
              "origin", pinned, watch=root)  # fmt: skip
    resolved = (await git("rev-parse", "FETCH_HEAD^{commit}")).strip()
    if resolved != pinned:
        raise ScanRejectedError("sha_mismatch", "fetched commit does not equal the pinned SHA")
    listing = (await git("ls-tree", "-r", "-l", "-z", resolved)).split("\0")
    entries: list[tuple[str, int, str]] = []
    for row in filter(None, listing):
        meta, _, path = row.partition("\t")
        mode, _typ, _sha, size = meta.split()
        entries.append((mode, 0 if size == "-" else int(size), path))
    check_listing(entries, limits)
    await git("checkout", "--quiet", "--detach", resolved, watch=root)
    head = (await git("rev-parse", "HEAD")).strip()
    if head != pinned:
        raise ScanRejectedError("sha_mismatch", "checked-out HEAD does not equal the pinned SHA")


def _load_yaml(path: Path) -> Any:
    """Safe-load a bounded YAML file (never following a symlink)."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError(f"{path.name} missing, symlinked or oversized")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _permission_preview(manifest: Any) -> dict[str, Any]:
    """Cheap, DB-free preview of what the bundle asks for (Task 5 builds the real summary)."""
    return {
        "permissions": list(manifest.permissions),
        "egress": [getattr(r, "host", str(r)) for r in manifest.egress],
        "data_tables": list(manifest.data_tables),
        "consumes": len(manifest.consumes),
    }


def _validate_bundle(
    source: SourceRecord, language: str, bundle_dir: Path
) -> BundleCandidate | RejectedBundle:
    """Validate one `bundles/{language}/{app}/` directory; reject (never repair) on failure."""
    dir_name = bundle_dir.name

    def reject(reason: str, detail: str) -> RejectedBundle:
        return RejectedBundle(language, dir_name, reason, detail)

    try:
        bundle_yaml = _load_yaml(bundle_dir / "bundle.yaml")
        raw = _load_yaml(bundle_dir / "hub-manifest.yaml")
    except (ValueError, yaml.YAMLError, OSError) as exc:
        logger.warning(
            "marketplace scan: bundle rejected",
            extra={
                "source_public_id": str(source.public_id),
                "reason": "manifest_unreadable",
                "exc_type": type(exc).__name__,
            },
        )
        return reject("manifest_unreadable", str(exc))
    if not isinstance(raw, dict) or not isinstance(bundle_yaml, dict):
        return reject("manifest_malformed", "manifest must be a YAML mapping")

    claimed = raw.get("app_id")
    if not isinstance(claimed, str):
        return reject("manifest_malformed", "app_id missing")
    try:
        if claimed.startswith("waddles."):
            enforce_source_namespace(app_id=claimed, source=source)
        assigned = f"{source_namespace_prefix(source_short(source))}{dir_name}"
        enforce_source_namespace(app_id=assigned, source=source)
    except ApiError as exc:
        logger.warning(
            "marketplace scan: bundle rejected",
            extra={
                "source_public_id": str(source.public_id),
                "reason": "namespace_violation",
                "exc_type": type(exc).__name__,
            },
        )
        return reject("namespace_violation", str(exc.message))
    raw = {
        **raw,
        "app_id": assigned,
        "feature": assigned.rsplit(".", 1)[0],
        "module": "integrations",
    }
    try:
        manifest = parse_bundle_manifest_v2(
            raw,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=source.build_mode == BuildMode.PREBUILT,
        )
    except (ManifestV2Error, KeyError, TypeError, AttributeError) as exc:
        reason = str(getattr(exc, "reason", "manifest_invalid"))
        logger.warning(
            "marketplace scan: bundle rejected",
            extra={
                "source_public_id": str(source.public_id),
                "reason": reason,
                "exc_type": type(exc).__name__,
            },
        )
        return reject(reason, str(exc))
    if manifest.provider != "thirdparty":
        return reject(
            "provider_not_thirdparty", "external bundles must declare provider: thirdparty"
        )
    try:
        findings = scan_bundle_dependencies(bundle_dir)
    except ValueError as exc:
        logger.warning(
            "marketplace scan: bundle rejected",
            extra={
                "source_public_id": str(source.public_id),
                "reason": "dependency_manifest_unreadable",
                "exc_type": type(exc).__name__,
            },
        )
        return reject("dependency_manifest_unreadable", str(exc))
    if findings:
        first = findings[0]
        return reject(
            "denylisted_dependency",
            f"{first.ecosystem}:{first.package} ({first.file}); {len(findings)} finding(s)",
        )
    wasm = next(iter(sorted(bundle_dir.glob("*.wasm"))), None)
    sidecar = bundle_dir / "component.sha256"
    sig = bundle_dir / "component.sig"
    return BundleCandidate(
        language=language,
        dir_name=dir_name,
        assigned_app_id=assigned,
        version=manifest.version,
        manifest=manifest,
        wasm_path=wasm,
        sidecar_digest=sidecar.read_text().strip() if sidecar.is_file() else None,
        publisher_sig=sig.read_text().strip() if sig.is_file() else None,
        permission_summary=_permission_preview(manifest),
        dep_findings=findings,
        raw_manifest=raw,
    )


def _walk_bundles(
    source: SourceRecord, root: Path
) -> tuple[list[BundleCandidate], list[RejectedBundle], int]:
    """Discover and validate every bundle dir under `bundles/<language>/*/`."""
    good: list[BundleCandidate] = []
    bad: list[RejectedBundle] = []
    examined = 0
    for language in BUNDLE_LANGUAGES:
        lang_dir = root / "bundles" / language
        if lang_dir.is_symlink() or not lang_dir.is_dir():
            continue
        for bundle_dir in sorted(
            p for p in lang_dir.iterdir() if not p.is_symlink() and p.is_dir()
        ):
            examined += 1
            result = _validate_bundle(source, language, bundle_dir)
            (good if isinstance(result, BundleCandidate) else bad).append(result)  # type: ignore[arg-type]
    return good, bad, examined


async def scan_source(
    source: SourceRecord,
    *,
    workdir: Path,
    limits: GuardLimits | None = None,
    dal: AsyncDB | None = None,
    actor_id: int | None = None,
) -> ScanResult:
    """Clone `source` at its pinned SHA into an isolated temp dir, guard, walk and validate.

    Raises `ScanRejectedError` on any whole-scan failure (guard trip, SHA mismatch, clone failure,
    zero bundles examined). With `dal`, updates the source's status + `last_scanned_at`.
    """
    lim = limits or GuardLimits()
    started = time.monotonic()
    examined_n = rejected_n = 0
    tmp = Path(tempfile.mkdtemp(prefix="mktsrc-", dir=workdir))
    root = tmp / "repo"
    root.mkdir()
    outcome = "ok"
    try:
        async with mkt_telemetry.source_span("marketplace.source.scan", source.public_id):
            try:
                await _clone_pinned(source, root, lim)
                await asyncio.to_thread(check_tree, root, lim)
                good, bad, examined = await asyncio.to_thread(_walk_bundles, source, root)
                if examined == 0:
                    raise ScanRejectedError(
                        "no_bundles", "no bundles/{language}/{app}/ directories found"
                    )
            except GuardViolationError as exc:
                raise ScanRejectedError(exc.reason, str(exc)) from exc
            pinned = source.pinned_commit_sha or ""
            result = ScanResult(source.public_id, pinned, tuple(good), tuple(bad), examined)
            examined_n, rejected_n = examined, len(bad)
            logger.info(
                "marketplace source scan complete",
                extra={
                    "source_public_id": str(source.public_id),
                    "examined": examined,
                    "accepted": len(good),
                    "rejected": len(bad),
                },
            )
            if dal is not None:
                await update_status(dal, source.public_id, SourceStatus.ACTIVE, scanned=True)
                await audit_source_event(
                    dal,
                    ACTION_RESCANNED,
                    source.public_id,
                    actor_id=actor_id,
                    details={"examined": examined, "accepted": len(good), "rejected": len(bad)},
                )
            return result
    except ScanRejectedError as exc:
        outcome = exc.reason
        logger.warning(
            "marketplace source scan rejected",
            extra={"source_public_id": str(source.public_id), "reason": exc.reason},
        )
        if dal is not None:
            await audit_source_event(
                dal,
                ACTION_SCAN_REJECTED,
                source.public_id,
                actor_id=actor_id,
                details={"category": mkt_telemetry.reason_category(exc.reason)},
            )
            try:
                await update_status(
                    dal,
                    source.public_id,
                    SourceStatus.ERROR,
                    last_scan_error=str(exc)[:500],
                    scanned=True,
                )
            except ApiError as status_exc:
                if status_exc.status_code != 409:  # disabled source: drop the status write
                    raise
        raise
    except Exception:
        outcome = "error"
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        mkt_telemetry.record_scan(
            examined=examined_n,
            rejected=rejected_n,
            outcome=outcome,
            duration_s=time.monotonic() - started,
        )
