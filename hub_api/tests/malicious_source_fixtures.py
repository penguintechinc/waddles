"""Builders for hostile/clean git source fixtures (never real exploit binaries)."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from services.marketplace_source_service import BuildMode, SourceRecord, SourceStatus

GIT_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "HOME": "/nonexistent",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str) -> str:
    """Run git in `cwd` with a clean env; returns stdout."""
    git_bin = shutil.which("git") or "git"
    return subprocess.run(  # noqa: S603
        [git_bin, *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_source(pinned: str | None, *, mode: BuildMode = BuildMode.PREBUILT) -> SourceRecord:
    """A SourceRecord for an external https source pinned at `pinned`."""
    now = datetime.now(UTC)
    return SourceRecord(
        public_id=uuid.uuid4(), name="s", is_builtin=False,
        git_url="https://git.example.org/acme/bundles.git", ref="main",
        pinned_commit_sha=pinned, pending_commit_sha=None, build_mode=mode,
        publisher_key_fingerprint=None, publisher_public_key=None, sandbox_ack_by=None,
        sandbox_ack_at=None, sandbox_ack_text_hash=None, sandbox_ack_commit_sha=None,
        status=SourceStatus.PENDING_SCAN, last_scanned_at=None, last_scan_error=None,
        created_by=None, created_at=now, updated_at=now,
    )  # fmt: skip


def manifest(
    app_id: str = "acme.hello", version: str = "1.0.0", **over: object
) -> dict[str, object]:
    """A minimal valid thirdparty hub-manifest dict."""
    base: dict[str, object] = {
        "schema_version": 2, "app_id": app_id, "name": "Hello", "version": version,
        "feature": app_id.rsplit(".", 1)[0], "module": "integrations", "provider": "thirdparty",
        "author": "Acme", "license": "Apache-2.0", "language": "python",
        "artifact": "prebuilt", "execution_model": "native", "is_default": False,
        "stages": {"process": {"entry": "e#t", "consumes": [
            {"platform": "twitch", "event_types": ["chat.message"],
             "filters": {"command_prefix": ["!hi"]}}], "produces": [], "config": {}}},
        "egress": [], "data": {"tables": []},
        "limits": {"timeout_ms": 2000, "memory_mb": 64, "egress_rps": 10},
        "permissions": [],
    }  # fmt: skip
    base.update(over)
    return base


def add_bundle(
    repo: Path,
    lang: str,
    name: str,
    man: dict[str, object] | None = None,
    *,
    raw_manifest: str | None = None,
    files: dict[str, str] | None = None,
) -> Path:
    """Write `bundles/<lang>/<name>/` with bundle.yaml + hub-manifest.yaml (+ extra files)."""
    d = repo / "bundles" / lang / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "bundle.yaml").write_text("name: x\n")
    (d / "hub-manifest.yaml").write_text(
        raw_manifest if raw_manifest is not None else yaml.safe_dump(man or manifest())
    )
    for rel, content in (files or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(content)
    return d


def commit_bare(work: Path, bare: Path, *, extra_index: str | None = None) -> str:
    """Init+commit `work`, push to a bare repo that allows fetch-by-SHA; returns the commit SHA."""
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-A")
    if extra_index:
        git(work, "update-index", "--add", "--cacheinfo", extra_index)
    git(work, "commit", "-q", "-m", "init")
    sha = git(work, "rev-parse", "HEAD")
    subprocess.run(  # noqa: S603
        [shutil.which("git") or "git", "init", "-q", "--bare", str(bare)], env=GIT_ENV, check=True
    )
    git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
    git(work, "push", "-q", str(bare), "main")
    return sha


def make_zip_bomb(path: Path, mb: int = 60) -> None:
    """A tiny zip that expands to `mb` MiB of zeros (ratio ~1000+)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.bin", b"\0" * (mb * 1024 * 1024))
