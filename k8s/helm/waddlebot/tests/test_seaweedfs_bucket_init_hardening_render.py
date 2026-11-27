"""Render assertions for the seaweedfs-bucket-init script + hook Job hardening.

The container is defined once (templates/_seaweedfs_bucket_init.tpl). By default it is hub-api's
initContainer (test_escrow_bucket_init_ordering_render.py); the hook Job form asserted here renders
only with hub-api disabled, so `_job` sets pipeline.hubApi.enabled=false. The script is identical.

A stall must fail FAST and DIAGNOSABLY: every aws call is wrapped in `timeout`, the S3 wait is
bounded and names the endpoint, the script ends with an N/N verified line equal to the configured
bucket count, and failed Jobs/pods are kept (hook-delete-policy has no hook-failed + a TTL).
Zero Jobs / zero buckets examined is a hard failure.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[1]
_HELM = os.environ.get("HELM") or shutil.which("helm") or shutil.which("helm3")


def _job(values: str | None, *sets: str) -> dict[str, Any]:
    if not _HELM:
        pytest.skip("helm CLI not available")
    argv = [_HELM, "template", "waddlebot", str(_CHART), "--kube-version", "1.30.0"]
    if values:
        argv += ["--values", str(_CHART / values)]
    for s in ("pipeline.hubApi.enabled=false", *sets):
        argv += ["--set", s]
    out = subprocess.run(argv, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        pytest.fail(f"helm template failed:\n{out.stderr}")
    assert "<no value>" not in out.stdout, "literal '<no value>' in rendered output"
    jobs = [
        d
        for d in yaml.safe_load_all(out.stdout)
        if d and d.get("kind") == "Job" and d["metadata"]["name"] == "seaweedfs-bucket-init"
    ]
    assert len(jobs) == 1, f"expected exactly one seaweedfs-bucket-init Job, got {len(jobs)}"
    return jobs[0]


def _script(job: dict[str, Any]) -> str:
    return job["spec"]["template"]["spec"]["containers"][0]["command"][-1]


@pytest.mark.parametrize("values", [None, "values-alpha.yaml"])
def test_script_is_bounded_and_counts_buckets(values: str | None) -> None:
    extra = () if values else ("infrastructure.seaweedfs.encryption.secretName=seaweedfs-sse-kek",)
    script = _script(_job(values, *extra))
    assert "set -eu" in script and "pipefail" in script
    assert 'timeout "$(capped_timeout "${CALL_TIMEOUT}")" "${AWS_BIN}"' in script
    assert "AWS_BIN=$(command -v aws)" in script
    # No bare aws invocation outside the timeout wrapper / bounded wait loop.
    bare = [
        ln.strip()
        for ln in script.splitlines()
        if re.match(r"\s*aws\s", ln) or re.search(r"\|\|\s*aws\s", ln)
    ]
    assert not bare, f"unwrapped aws calls: {bare}"
    # Readiness wait is capped by CALL_TIMEOUT (not a hardcoded 10s) and logs rc + per-attempt time.
    assert 'timeout "${call_to}" "${AWS_BIN}"' in script and "WAIT_MAX" in script
    assert "capped_timeout 10" not in script
    assert "rc=${rc}" in script and "took=${took}s" in script and "124)" in script
    assert "not ready after" in script and "${ENDPOINT}" in script
    assert "buckets verified" in script and "${VERIFIED}" in script
    # Configured bucket count must equal the number of names in the for-loop.
    expected = int(re.search(r"^\s*EXPECTED=(\d+)", script, re.M).group(1))  # type: ignore[union-attr]
    loop = re.search(r"for bucket in (.*?); do", script).group(1)  # type: ignore[union-attr]
    names = re.findall(r'"([^"]+)"', loop)
    assert expected >= 1 and len(names) == expected, (expected, names)


def test_failed_pods_are_kept_for_inspection() -> None:
    job = _job("values-alpha.yaml")
    policy = job["metadata"]["annotations"]["helm.sh/hook-delete-policy"]
    assert "before-hook-creation" in policy and "hook-succeeded" in policy
    assert "hook-failed" not in policy
    assert job["spec"]["ttlSecondsAfterFinished"] >= 3600
    assert job["spec"]["activeDeadlineSeconds"] == 600
    assert job["spec"]["backoffLimit"] == 3


@pytest.mark.parametrize("values", [None, "values-alpha.yaml"])
def test_total_budget_is_strictly_below_job_deadline(values: str | None) -> None:
    """The script must self-terminate (with a log line) before the Job controller kills it."""
    extra = () if values else ("infrastructure.seaweedfs.encryption.secretName=seaweedfs-sse-kek",)
    job = _job(values, *extra)
    script = _script(job)
    deadline = job["spec"]["activeDeadlineSeconds"]
    m = re.search(r"^\s*TOTAL_BUDGET=(\d+)", script, re.M)
    assert m, "TOTAL_BUDGET not rendered"
    budget = int(m.group(1))
    assert 0 < budget <= deadline - 60, (budget, deadline)
    call_timeout = int(re.search(r"^\s*CALL_TIMEOUT=(\d+)", script, re.M).group(1))  # type: ignore[union-attr]
    wait_max = int(re.search(r"^\s*WAIT_MAX=(\d+)", script, re.M).group(1))  # type: ignore[union-attr]
    assert call_timeout >= 30, "per-call timeout must be >= 30s (aws-cli cold start)"
    assert call_timeout < budget and wait_max < budget
    assert "budget_check()" in script and "capped_timeout()" in script
    assert 'budget_check "bucket ${bucket}"' in script
    assert "total budget exhausted elapsed=" in script and "/budget=${TOTAL_BUDGET}s" in script
    assert "trap " in script and " EXIT" in script


def test_image_pull_secrets_omitted_when_empty() -> None:
    pod = _job("values-alpha.yaml")["spec"]["template"]["spec"]
    assert "imagePullSecrets" not in pod


def test_image_pull_secrets_honoured_when_set() -> None:
    job = _job("values-alpha.yaml", "global.imagePullSecrets[0].name=dockerhub-pull")
    assert job["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "dockerhub-pull"}]


def test_cpu_resources_raised_for_aws_cli_cold_start() -> None:
    res = _job("values-alpha.yaml")["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert res["requests"]["cpu"] == "100m" and res["limits"]["cpu"] == "500m"


def test_budget_overrides_render_and_invariant_is_enforced() -> None:
    script = _script(_job("values-alpha.yaml", "infrastructure.seaweedfs.bucketInitJob.callTimeoutSeconds=45"))
    assert re.search(r"^\s*CALL_TIMEOUT=45$", script, re.M)
    if not _HELM:
        pytest.skip("helm CLI not available")
    bad = subprocess.run(
        [_HELM, "template", "waddlebot", str(_CHART), "--kube-version", "1.30.0", "--values",
         str(_CHART / "values-alpha.yaml"), "--set", "infrastructure.seaweedfs.bucketInitJob.totalBudgetSeconds=590"],
        capture_output=True, text=True, check=False,
    )
    assert bad.returncode != 0 and "totalBudgetSeconds" in bad.stderr
