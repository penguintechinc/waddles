"""Helm render: the seeder Job publishes connector objects only where `publishAtSeed` is on.

alpha + dal2alpha ON; beta/gamma/production inherit OFF. Zero seeder Jobs rendered is a failure.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[1]
_HELM = os.environ.get("HELM_BIN", "helm")


def _seeder_jobs(values: str) -> tuple[int, list[dict[str, Any]]]:
    res = subprocess.run(  # noqa: S603 -- fixed argv, test-only
        [_HELM, "template", "waddlebot", str(_CHART), "--kube-version", "1.30.0",
         "--values", str(_CHART / values)],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stderr[-600:]
    docs: list[dict[str, Any]] = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs, "helm rendered zero documents"
    jobs = [
        d for d in docs
        if d["kind"] == "Job" and "core-bundle-seeder" in d["metadata"]["name"]
    ]
    return len(docs), jobs


def _seeder_env(values: str) -> dict[str, str]:
    _, jobs = _seeder_jobs(values)
    assert len(jobs) == 1, f"expected exactly one core-bundle-seeder Job, got {len(jobs)}"
    env = jobs[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    return {e["name"]: e.get("value", "") for e in env}


@pytest.mark.parametrize("values", ["values-alpha.yaml", "values-dal2alpha.yaml"])
def test_alpha_seeder_publishes_connectors(values: str) -> None:
    assert _seeder_env(values).get("CORE_BUNDLES_PUBLISH_CONNECTORS") == "true"


def test_default_and_upper_tiers_do_not_publish_at_seed() -> None:
    """Upper tiers do not render without real TLS material, so assert the values directly."""
    for name in ("values.yaml", "values-beta.yaml", "values-gamma.yaml", "values-production.yaml"):
        data = yaml.safe_load((_CHART / name).read_text()) or {}
        connectors = (data.get("pipeline") or {}).get("connectors") or {}
        assert not connectors.get("publishAtSeed", False), f"{name} must not publish at seed"
    base = yaml.safe_load((_CHART / "values.yaml").read_text())
    assert base["pipeline"]["connectors"]["publishAtSeed"] is False, "key must exist (denominator)"
