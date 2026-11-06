"""Helm render assertions for the bundle platform-table DB role wiring.

The password is operator-supplied via `existingSecret` only: unset renders NO
`BUNDLE_PLATFORM_TABLE_*`/`DB_PLATFORM_TABLE_PASSWORD` env (capability fail-closed);
set renders a `secretKeyRef` (never a literal) on svc-process-rust and the
migrations Job. Zero rendered documents / zero matching containers is a hard fail.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[1]
_SECRET = "platform-table-db"


def _render(*sets: str) -> list[dict[str, Any]]:
    argv = [
        "helm", "template", "waddlebot", str(_CHART), "--kube-version", "1.30.0",
        "--values", str(_CHART / "values-alpha.yaml"),
    ]  # fmt: skip
    for s in sets:
        argv += ["--set", s]
    out = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    if out.returncode != 0:
        pytest.fail(f"helm template failed:\n{out.stderr}")
    docs = [d for d in yaml.safe_load_all(out.stdout) if d]
    assert docs, "helm template produced zero documents"
    return docs


def _env(
    docs: list[dict[str, Any]], kind: str, name_part: str
) -> dict[str, dict[str, Any]]:
    envs: dict[str, dict[str, Any]] = {}
    matched = 0
    for d in docs:
        if d.get("kind") != kind or name_part not in d["metadata"]["name"]:
            continue
        spec = (
            d["spec"]["template"]["spec"]
            if kind == "Deployment"
            else d["spec"]["template"]["spec"]
        )
        for c in spec["containers"]:
            matched += 1
            for e in c.get("env", []):
                envs[e["name"]] = e
    assert matched, f"no {kind} container matching {name_part!r} rendered"
    return envs


@pytest.fixture(scope="module", autouse=True)
def _helm() -> None:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available")


def test_unset_renders_no_platform_table_env() -> None:
    docs = _render()
    sp = _env(docs, "Deployment", "svc-process")
    assert not [k for k in sp if k.startswith("BUNDLE_PLATFORM_TABLE")]
    assert "DB_PLATFORM_TABLE_PASSWORD" not in _env(docs, "Job", "db-migrate")


def test_existing_secret_is_wired_as_secretkeyref_never_a_literal() -> None:
    docs = _render(
        f"pipeline.rustDataPlane.svcProcess.platformTable.existingSecret={_SECRET}",
        f"infrastructure.postgresql.platformTable.existingSecret={_SECRET}",
    )
    sp = _env(docs, "Deployment", "svc-process")
    pw = sp["BUNDLE_PLATFORM_TABLE_PASSWORD"]
    assert "value" not in pw
    assert pw["valueFrom"]["secretKeyRef"] == {"name": _SECRET, "key": "password"}
    assert sp["BUNDLE_PLATFORM_TABLE_USER"]["value"] == "waddles_platform_table"
    mig = _env(docs, "Job", "db-migrate")["DB_PLATFORM_TABLE_PASSWORD"]
    assert mig["valueFrom"]["secretKeyRef"] == {"name": _SECRET, "key": "password"}
