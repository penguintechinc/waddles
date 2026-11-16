"""Regression: Helm must not eat the kubectl "no value" sentinel strip in provision-secrets.sh.

# regression: alpha deploy blocker 2026-11-14 -- the chart file held the literal sentinel, Helm's
# renderer blanked it, the deployed script's strip became a no-op and F_MARK stayed non-empty
# ("root marker mismatch" on secret/waddlebot-tenant-kek).

The bug exists only POST-render, so these tests exercise the RENDERED ConfigMap script.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_SENTINEL = "<no" + " value>"
_HELM = shutil.which("helm3") or shutil.which("helm")


def _rendered_script() -> str:
    """Render the chart and return the provision-secrets.sh text from the ConfigMap."""
    if not _HELM:
        pytest.skip("helm CLI not available in this environment")
    res = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        [_HELM, "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
         "--values", str(_CHART_DIR / "values-alpha.yaml")],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, f"helm template failed:\n{res.stderr}"
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs, "helm template produced zero documents"
    cms = [d for d in docs if d.get("kind") == "ConfigMap"
           and d.get("metadata", {}).get("name", "").endswith("-provision-secrets")]
    assert len(cms) == 1, f"expected one provision-secrets ConfigMap, got {len(cms)}"
    return str(cms[0]["data"]["provision-secrets.sh"])


def test_rendered_script_still_strips_sentinel() -> None:
    script = _rendered_script()
    assert "out=\"${out//''/}\"" not in script, "Helm blanked the sentinel strip (no-op)"
    assert 'out="${out//"${NOVAL}"/}"' in script
    assert "NOVAL=" in script


def test_chart_sources_have_no_literal_sentinel() -> None:
    hits = [str(p) for d in ("files", "templates") for p in (_CHART_DIR / d).rglob("*")
            if p.is_file() and _SENTINEL in p.read_text(errors="ignore")]
    assert not hits, f"literal sentinel Helm would blank: {hits}"


def test_rendered_fetch_secret_strips_sentinel_from_mark() -> None:
    """Run the rendered fetch_secret against a fake kubectl printing the sentinel for MARK.

    The rendered script text is fed to bash on stdin (never written to disk) and the harness
    evals only the constants and the fetch_secret function body out of it.
    """
    script = _rendered_script()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        kubectl = tmp / "kubectl"
        kubectl.write_text(
            "#!/bin/sh\n"
            f"printf 'RV 1\\nANN keep\\nMARK {_SENTINEL}\\nCID \\nCSAN \\nK a Zm9v\\n'\n"
        )
        kubectl.chmod(0o755)
        harness = (
            "set -euo pipefail\n"
            "SRC=$(cat)\n"
            "eval \"$(printf '%s\\n' \"$SRC\" | grep -E '^(KEEP_ANN|MARKER_ANN|CERT_ID_ANN|CERT_SANS_ANN)=')\"\n"
            "eval \"$(printf '%s\\n' \"$SRC\" | grep -E '^NOVAL=')\"\n"
            "eval \"$(printf '%s\\n' \"$SRC\" | awk '/^fetch_secret\\(\\)/{p=1} p{print} p&&/^}/{exit}')\"\n"
            f"KUBECTL={kubectl}\n"
            "kc() { \"$KUBECTL\" \"$@\"; }\n"
            "die() { echo \"DIE: $*\" >&2; exit 9; }\n"
            "errhead() { :; }\n"
            f"WORK={tmp}\n"
            f"fetch_secret x {tmp / 'dest'}\n"
            "printf 'F_MARK=[%s] F_KEEP=[%s]\\n' \"${F_MARK}\" \"${F_KEEP}\"\n"
        )
        res = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
            ["bash", "-c", harness], input=script, capture_output=True, text=True,
            env={**os.environ}, check=False,
        )
        assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
        assert "F_MARK=[] F_KEEP=[keep]" in res.stdout, res.stdout
