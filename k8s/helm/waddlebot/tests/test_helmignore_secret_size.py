"""Regression tests ensuring `.helmignore` excludes non-runtime chart files.

Without `.helmignore`, Helm packages `tests/`, `docs/`, and markdown docs into every
release Secret (`sh.helm.release.v1.*`), which pushed the Secret payload past the
1 MiB Kubernetes etcd/apiserver limit (`1048576` bytes) during `helm upgrade`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

_CHART_DIR = Path(__file__).resolve().parents[1]
_HELM3 = Path.home() / ".local" / "bin" / "helm3"


def _find_helm() -> str | None:
    """Return a usable helm binary path, preferring helm3."""
    if _HELM3.exists():
        return str(_HELM3)
    return shutil.which("helm")


def test_helmignore_excludes_tests_and_docs(tmp_path: Path) -> None:
    """Packaged chart must omit tests/, docs/, and markdown files to stay under Secret limits."""
    helm_bin = _find_helm()
    if not helm_bin:
        pytest.skip("helm binary not available")

    helmignore = _CHART_DIR / ".helmignore"
    assert helmignore.is_file(), ".helmignore must exist in chart root"

    result = subprocess.run(
        [helm_bin, "package", str(_CHART_DIR), "-d", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "HELM_PLUGINS": "/nonexistent"},
    )
    assert result.returncode == 0, f"helm package failed: {result.stderr}"

    archives = list(tmp_path.glob("waddlebot-*.tgz"))
    assert len(archives) == 1, f"expected 1 packaged chart archive, got {archives}"
    archive = archives[0]

    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getnames()

    assert len(members) > 0, "packaged chart archive must not be empty"
    forbidden = [
        m
        for m in members
        if "/tests/" in m
        or "/docs/" in m
        or m.endswith(("README.md", "PIPELINE_MAPPING.md"))
        or "__pycache__" in m
    ]
    assert not forbidden, (
        f"non-runtime files leaked into packaged chart: {forbidden[:10]}"
    )
    assert archive.stat().st_size < 450 * 1024, (
        f"packaged chart too large ({archive.stat().st_size} bytes); "
        "risk of exceeding 1 MiB Helm release Secret limit"
    )
