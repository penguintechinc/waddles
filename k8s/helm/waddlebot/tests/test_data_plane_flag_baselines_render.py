"""Helm-template assertions for `config.dataPlaneFlagBaselines` (license-bypass scope fix, 2026-11-12).

The bypass domain no longer forces data-plane feature flags ON, so alpha carries
explicit FLAG_* ENV baselines. Base values render none; kill switches are never
baselined; a key without the FLAG_ prefix fails the render.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_REQUIRED = {
    "FLAG_WADDLES_CORE_RUST_DATA_PLANE",
    "FLAG_WADDLES_CORE_BUNDLE_EGRESS",
    "FLAG_WADDLES_BUNDLE_DB_CAPABILITY",
    "FLAG_WADDLES_CORE_OVERLAY_IMAGE_UPLOAD",
}


def _render(*extra: str) -> subprocess.CompletedProcess[str]:
    """Run `helm template` against the chart with extra args."""
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0", *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _configmap(*extra: str) -> dict[str, Any]:
    """Return the shared `-config` ConfigMap data; zero matches is a failure."""
    out = _render(*extra)
    assert out.returncode == 0, out.stderr
    docs = [d for d in yaml.safe_load_all(out.stdout) if d]
    assert docs, "helm rendered zero documents"
    maps = [
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-config")
    ]
    assert len(maps) == 1, f"expected 1 shared ConfigMap, got {len(maps)}"
    data: dict[str, Any] = maps[0]["data"]
    return data


@pytest.mark.parametrize("values_file", ["values-alpha.yaml", "values-dal2alpha.yaml"])
# regression: bypass domains are license-only (2026-11-12 decision)
def test_alpha_values_baseline_previously_bypassed_flags_on(values_file: str) -> None:
    """Alpha keeps every flag the bypass used to force ON, and baselines no kill switch."""
    data = _configmap("-f", str(_CHART_DIR / values_file))
    for key in _REQUIRED:
        assert data.get(key) == "true", key
    assert not [k for k in data if k.startswith("FLAG_") and "_DISABLE_" in k]


def test_base_values_render_no_data_plane_baselines() -> None:
    """Beta/prod inherit nothing: base values render none of the alpha baselines."""
    data = _configmap("--set", "global.deploymentTier=alpha")
    assert not _REQUIRED & set(data)


def test_non_flag_key_fails_render() -> None:
    """A key without the FLAG_ prefix must fail `helm template`, never render."""
    out = _render(
        "--set", "global.deploymentTier=alpha",
        "--set", "config.dataPlaneFlagBaselines.LICENSE_KEY=x",
    )
    assert out.returncode != 0
    assert "must start with FLAG_" in out.stderr
