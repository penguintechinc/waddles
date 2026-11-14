"""Materialize the RUNTIME-provisioned Secrets of a rendered chart for render tests.

Internally generated Secrets are no longer rendered by Helm -- the provision-secrets hook creates them
at install time (templates/provision-secrets.yaml, files/provision-secrets.sh). Render tests that assert
"Secret X carries a non-empty key K" therefore run the REAL hook script, with the REAL rendered spec and
inputs Secret, against tests/fake_kubectl.py, and assert on what it produced. That is a stronger check
than reading a rendered value: it exercises generation, explicit overrides, derivation and validation.

`with_provisioned(docs)` returns the rendered docs plus one synthetic
``{"kind": "Secret", "metadata": {"name", "annotations"}, "stringData": {...}, "x-provisioned": True}``
per runtime Secret, so existing lookups by kind/name keep working.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_TESTS = Path(__file__).resolve().parent
_SCRIPT = _TESTS.parent / "files" / "provision-secrets.sh"
_FAKE = _TESTS / "fake_kubectl.py"
PROVISION_JOB = "waddlebot-provision-secrets"


def _find(docs: list[dict[str, Any]], kind: str, suffix: str) -> dict[str, Any]:
    """Exactly one rendered `kind` whose name ends with `suffix` (release-name independent)."""
    found = [
        d
        for d in docs
        if d.get("kind") == kind
        and d.get("metadata", {}).get("name", "").endswith(suffix)
        and not d.get("x-provisioned")
    ]
    assert len(found) == 1, (
        f"expected exactly one {kind}/*{suffix} in the render, got {len(found)}"
    )
    return found[0]


def spec_of(docs: list[dict[str, Any]]) -> str:
    """The rendered provision-secrets spec (structure only, no values)."""
    return _find(docs, "ConfigMap", "-provision-secrets")["data"]["spec.psv"]


def inputs_of(docs: list[dict[str, Any]]) -> dict[str, str]:
    """The rendered hook-only inputs Secret (explicit operator values)."""
    return dict(
        _find(docs, "Secret", "-provision-secrets-inputs").get("stringData") or {}
    )


def spec_keys(docs: list[dict[str, Any]]) -> dict[str, list[str]]:
    """{secret name: [key names]} from the spec, skipped Secrets excluded."""
    out: dict[str, list[str]] = {}
    cur = None
    for ln in spec_of(docs).splitlines():
        parts = ln.split("|")
        if parts[0] == "secret":
            cur = (
                None if (len(parts) > 2 and parts[2].startswith("skip=")) else parts[1]
            )
            if cur:
                out[cur] = []
        elif parts[0] == "key" and cur:
            out[cur].append(parts[1])
    return out


def run_hook(
    docs: list[dict[str, Any]], existing: dict[str, dict[str, str]] | None = None
) -> tuple[subprocess.CompletedProcess[str], dict[str, dict[str, Any]]]:
    """Run the real hook script against a fake cluster; return (process, final cluster state)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state, inputs, work = tmp / "state", tmp / "inputs", tmp / "work"
        for d in (state, inputs, work):
            d.mkdir()
        (state / "_calls.log").write_text("")
        for name, data in (existing or {}).items():
            obj = {
                "rv": 1,
                "annotations": {},
                "data": {
                    k: base64.b64encode(v.encode()).decode() for k, v in data.items()
                },
            }
            (state / f"{name}.json").write_text(json.dumps(obj))
        for k, v in inputs_of(docs).items():
            (inputs / k).write_text(v)
        spec = tmp / "spec.psv"
        spec.write_text(spec_of(docs))
        kubectl = tmp / "kubectl"
        kubectl.write_text(f'#!/bin/sh\nexec python3 "{_FAKE}" "$@"\n')
        kubectl.chmod(0o755)
        env = {
            **os.environ,
            "NAMESPACE": "waddlebot",
            "SPEC_FILE": str(spec),
            "INPUTS_DIR": str(inputs),
            "KUBECTL": str(kubectl),
            "FAKE_KUBE_STATE": str(state),
            "WORK_PARENT": str(work),
        }
        res = subprocess.run(
            ["bash", str(_SCRIPT)], capture_output=True, text=True, env=env, check=False
        )
        out: dict[str, dict[str, Any]] = {}
        for p in state.glob("*.json"):
            if p.name.startswith("_"):
                continue
            obj = json.loads(p.read_text())
            out[p.stem] = {
                "data": {
                    k: base64.b64decode(v).decode()
                    for k, v in obj.get("data", {}).items()
                },
                "annotations": obj.get("annotations", {}),
            }
        return res, out


def materialize(
    docs: list[dict[str, Any]], existing: dict[str, dict[str, str]] | None = None
) -> dict[str, dict[str, Any]]:
    """Run the hook (must succeed); return {name: {"data": {k: plaintext}, "annotations": {...}}}."""
    res, out = run_hook(docs, existing)
    assert res.returncode == 0, (
        f"provision-secrets hook failed:\n{res.stdout}\n{res.stderr}"
    )
    return out


def hook_failure(
    docs: list[dict[str, Any]], existing: dict[str, dict[str, str]] | None = None
) -> str:
    """Run the hook expecting it to FAIL closed; return its stderr."""
    res, _ = run_hook(docs, existing)
    assert res.returncode != 0, (
        f"provision-secrets hook unexpectedly succeeded:\n{res.stdout}"
    )
    return res.stderr


def with_provisioned(
    docs: list[dict[str, Any]], existing: dict[str, dict[str, str]] | None = None
) -> list[dict[str, Any]]:
    """Rendered docs + one synthetic Secret per runtime-provisioned Secret (see module docstring)."""
    extra = [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "annotations": obj["annotations"]},
            "stringData": obj["data"],
            "x-provisioned": True,
        }
        for name, obj in sorted(materialize(docs, existing).items())
    ]
    return docs + extra
