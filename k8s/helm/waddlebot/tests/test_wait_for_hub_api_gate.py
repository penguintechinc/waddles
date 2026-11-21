"""Gate tests for scripts/check_wait_for_hub_api.py (every non-hub-api workload waits for hub-api).

Proves the gate can fail: a Job planted into a copy of the chart without the shared
wait-for-hub-api init container is reported, and the pre-install-hook exemption does not
cover a same-component Job that is no longer a pre-install hook.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_CHART_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _CHART_DIR.parents[2]
_CHECKER = _REPO_ROOT / "scripts" / "check_wait_for_hub_api.py"


def _load_checker():
    """Import the checker module by path (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("check_wait_for_hub_api", _CHECKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()

_PLANTED_JOB = """\
apiVersion: batch/v1
kind: Job
metadata:
  name: planted-{name}
  namespace: {{{{ .Values.namespace }}}}
  labels:
    app.kubernetes.io/component: {component}
{annotations}spec:
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: c
        image: busybox
"""


def _job(component: str, hook: str | None = None, init: bool = False) -> dict:
    meta: dict = {
        "name": f"j-{component}",
        "labels": {"app.kubernetes.io/component": component},
    }
    if hook is not None:
        meta["annotations"] = {"helm.sh/hook": hook}
    spec: dict = {"containers": [{"name": "c"}]}
    if init:
        spec["initContainers"] = [{"name": "wait-for-hub-api"}]
    return {"kind": "Job", "metadata": meta, "spec": {"template": {"spec": spec}}}


_SRC = "waddlebot/templates/x.yaml"


@pytest.mark.parametrize("component", ["provision-secrets", "postgres-handover"])
def test_pre_install_hook_is_exempt_with_reason(component: str) -> None:
    reason = checker.exemption_reason(
        _SRC, _job(component, hook="pre-install,pre-upgrade")
    )
    assert reason and "pre-install hook" in reason


@pytest.mark.parametrize("hook", [None, "post-install,post-upgrade", "pre-upgrade"])
def test_pre_hub_api_component_without_pre_install_hook_must_wait(
    hook: str | None,
) -> None:
    doc = _job("provision-secrets", hook=hook)
    assert checker.exemption_reason(_SRC, doc) is None
    examined, _, violations = checker.find_violations([(_SRC, doc)])
    assert (examined, violations) == (1, ["Job/j-provision-secrets"])


def test_ordinary_job_without_init_is_a_violation_and_with_init_passes() -> None:
    examined, exempted, violations = checker.find_violations(
        [
            (_SRC, _job("executor-precompile-enqueue")),
            (_SRC, _job("hub-admin-seed", init=True)),
        ]
    )
    assert (examined, exempted, violations) == (
        2,
        0,
        ["Job/j-executor-precompile-enqueue"],
    )


def test_infrastructure_and_unconditional_exemptions() -> None:
    _, exempted, violations = checker.find_violations(
        [
            ("waddlebot/templates/infrastructure/postgres.yaml", _job("database")),
            (_SRC, _job("hub-api")),
            (_SRC, _job("db-migrate")),
        ]
    )
    assert (exempted, violations) == (3, [])


@pytest.fixture(scope="module")
def _helm() -> str:
    path = shutil.which("helm")
    if not path:
        pytest.fail("helm CLI not on PATH -- this gate test must never silently skip")
    return path


def _run(chart: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # fixed argv, test-only
        [sys.executable, str(_CHECKER), "--chart-dir", str(chart)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_real_chart_passes(_helm: str) -> None:
    res = _run(_CHART_DIR)
    assert res.returncode == 0, res.stderr
    assert "Examined " in res.stdout and "PASS" in res.stdout


@pytest.mark.parametrize(
    ("name", "component", "annotations"),
    [
        ("plain", "planted-consumer", ""),
        # Same component as an exempt hook, but no longer a pre-install hook: must still fail.
        ("not-a-hook", "provision-secrets", ""),
    ],
)
def test_planted_job_without_init_container_fails(
    _helm: str, tmp_path: Path, name: str, component: str, annotations: str
) -> None:
    chart = tmp_path / "waddlebot"
    shutil.copytree(
        _CHART_DIR, chart, ignore=shutil.ignore_patterns("tests", "__pycache__")
    )
    (chart / "templates" / "zz-planted-job.yaml").write_text(
        _PLANTED_JOB.format(name=name, component=component, annotations=annotations)
    )
    res = _run(chart)
    assert res.returncode == 1, res.stdout + res.stderr
    assert f"Job/planted-{name}" in res.stderr
