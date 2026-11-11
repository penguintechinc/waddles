"""Helm-template assertions for fix/dal2alpha-gateway-ns-label.

# regression: HTTPRoute `waddlebot` was Accepted=False NotAllowedByListeners on dal2alpha because the
# chart-rendered Namespace lacked the shared Gateway's `gateway-access: shared-dal2a` label.

Asserts the label renders under values-dal2alpha.yaml, PSA labels are preserved, and the default
(no namespaceLabels) renders no gateway-access label.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]


def _namespace_labels(values_file: str | None) -> dict[str, str]:
    """Render the chart and return the labels of the single rendered Namespace."""
    args = ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0"]
    if values_file:
        args += ["--values", str(_CHART_DIR / values_file)]
    result = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, test-only
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    docs = [d for d in yaml.safe_load_all(result.stdout) if d and d.get("kind") == "Namespace"]
    assert len(docs) == 1, f"expected exactly 1 Namespace, examined {len(docs)}"
    return docs[0]["metadata"]["labels"]


def test_dal2alpha_namespace_has_gateway_access_label() -> None:
    """dal2alpha Namespace carries the shared-Gateway label and keeps PSA labels."""
    labels = _namespace_labels("values-dal2alpha.yaml")
    assert labels["gateway-access"] == "shared-dal2a"
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"


def test_default_namespace_has_no_gateway_access_label() -> None:
    """Alpha (no namespaceLabels) adds no extra labels and still renders PSA labels."""
    labels = _namespace_labels("values-alpha.yaml")
    assert "gateway-access" not in labels
    assert "pod-security.kubernetes.io/enforce" in labels
