"""Render assertions for the seaweedfs-bucket-init hook Job hardening.

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
    for s in sets:
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
    assert 'timeout "${CALL_TIMEOUT}" aws' in script
    # No bare aws invocation outside the timeout wrapper / bounded wait loop.
    bare = [
        ln.strip()
        for ln in script.splitlines()
        if re.match(r"\s*aws\s", ln) or re.search(r"\|\|\s*aws\s", ln)
    ]
    assert not bare, f"unwrapped aws calls: {bare}"
    assert "timeout 10 aws" in script and "WAIT_MAX" in script
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
    assert job["spec"]["activeDeadlineSeconds"] == 300
    assert job["spec"]["backoffLimit"] == 3


def test_image_pull_secrets_omitted_when_empty() -> None:
    pod = _job("values-alpha.yaml")["spec"]["template"]["spec"]
    assert "imagePullSecrets" not in pod


def test_image_pull_secrets_honoured_when_set() -> None:
    job = _job("values-alpha.yaml", "global.imagePullSecrets[0].name=dockerhub-pull")
    assert job["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "dockerhub-pull"}]
