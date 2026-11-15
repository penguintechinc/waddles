"""Tests for audit_chart_deep.py: unread-env detection and the delete-and-render check."""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "audit_chart_deep", os.path.join(os.path.dirname(__file__), "audit_chart_deep.py")
)
assert _SPEC and _SPEC.loader
deep = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(deep)

_MANIFEST = """
apiVersion: v1
kind: ConfigMap
metadata: {name: cfg}
data:
  READ_BY_CODE: "1"
  NOBODY_READS_ME: "x"
  INITDB_ARGS: "--x"
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: app}
spec:
  template:
    spec:
      initContainers:
      - name: wait
        image: python:3.13-slim@sha256:abc
        command: ["python3", "-c", "import os; os.environ['READY_URL']"]
        env:
        - {name: READY_URL, value: "http://x"}
      containers:
      - name: hub-api
        image: ghcr.io/org/repo/hub-api:alpha-1
        envFrom:
        - configMapRef: {name: cfg}
        env:
        - {name: DEAD_ENV, value: "1"}
        - {name: NODE_IP, valueFrom: {fieldRef: {fieldPath: status.hostIP}}}
        - {name: PUBLIC_URL, value: "http://$(NODE_IP):80"}
        - {name: OTEL_SERVICE_NAME, value: "hub-api"}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: postgres}
spec:
  template:
    spec:
      containers:
      - name: postgres
        image: postgres:16@sha256:abc
        env:
        - {name: POSTGRES_INITDB_ARGS, valueFrom: {configMapKeyRef: {name: cfg, key: INITDB_ARGS}}}
        - {name: THIRD_PARTY_ONLY, value: "1"}
"""


def _check(tmp_path: Path, code: str, allow: str = "") -> list[str]:
    """Run the env-reader check over the fixture manifest; return the unread names."""
    src = tmp_path / "svc.py"
    src.write_text(code)
    allow_file = tmp_path / "allow.txt"
    allow_file.write_text(allow)
    n, unread = deep.env_reader_check(
        {"alpha": _MANIFEST}, [str(src)], deep.load_allowlist(str(allow_file))
    )
    assert n >= 1
    return [u.split()[0] for u in unread]


def test_unread_env_is_flagged_and_readers_are_honoured(tmp_path: Path) -> None:
    """Code, inline command, $(VAR) expansion and third-party configMapKeyRef all count as readers."""
    unread = _check(tmp_path, 'os.environ["READ_BY_CODE"]; os.environ["PUBLIC_URL"]\n')
    assert unread == ["DEAD_ENV", "NOBODY_READS_ME", "OTEL_SERVICE_NAME"]
    assert "THIRD_PARTY_ONLY" not in unread  # third-party images read their own env


def test_allowlist_globs_need_a_reason(tmp_path: Path) -> None:
    """A `PREFIX*  # reason` entry suppresses matching names; an entry without a reason is rejected."""
    unread = _check(
        tmp_path,
        "READ_BY_CODE PUBLIC_URL\n",
        "OTEL_*  # SDK\nDEAD_ENV  # kept for a test\n",
    )
    assert unread == ["NOBODY_READS_ME"]
    with pytest.raises(SystemExit):
        _check(tmp_path, "READ_BY_CODE\n", "OTEL_*\n")


def _helm() -> str:
    """Helm binary from $HELM or PATH; a missing binary fails (never skips)."""
    helm = os.environ.get("HELM") or shutil.which("helm")
    if not helm:
        pytest.fail(
            "helm not found (set HELM) -- the delete-and-render check cannot run"
        )
    return helm


def test_delete_and_render_separates_used_from_dead(tmp_path: Path) -> None:
    """Deleting a used key changes the render; deleting a dead one does not."""
    chart = tmp_path / "c"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: c\nversion: 0.1.0\n")
    (chart / "values.yaml").write_text(
        "used: a\ndead: b\nglobal: {deploymentTier: alpha}\n"
    )
    (chart / "values-alpha.yaml").write_text("used: c\n")
    (chart / "templates" / "cm.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\ndata: {k: {{ .Values.used | quote }}}\n"
    )
    res = deep.delete_and_render(_helm(), str(chart), [("used",), ("dead",)], jobs=2)
    assert res["used"] == ["alpha"]
    assert res["dead"] == []
