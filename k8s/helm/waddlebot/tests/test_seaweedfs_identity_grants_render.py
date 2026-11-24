"""Render assertions for the SeaweedFS S3 identity action grants.

SeaweedFS's Identity.CanDo matches a wildcard-free action only against "Action:bucket" exactly;
prefix scoping needs a trailing "*" matched against "Action:bucket/objectKey". A grant like
"Write:bucket/prefix" therefore never authorises an object write (regression: precompiler sign
PUTs to precompiled/<fp>/<digest>.cwasm returned 403). Zero identities parsed is a hard failure.
"""

from __future__ import annotations

import json
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
_IDENTITIES_RE = re.compile(r"s3-identities\.json <<EOF\n(.*?)\n\s*EOF\n", re.DOTALL)


def _identities(values: str | None, *sets: str) -> list[dict[str, Any]]:
    """Render the chart and return the parsed identities list from the seaweedfs Deployment."""
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
    deps = [
        d
        for d in yaml.safe_load_all(out.stdout)
        if d and d.get("kind") == "Deployment" and d["metadata"]["name"] == "seaweedfs"
    ]
    assert len(deps) == 1, f"expected exactly one seaweedfs Deployment, got {len(deps)}"
    script = deps[0]["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    m = _IDENTITIES_RE.search(script)
    assert m, "s3-identities.json heredoc not found in seaweedfs command"
    identities = json.loads(m.group(1))["identities"]
    assert identities, "zero identities parsed"
    return identities


_CASES = [
    (None, ("infrastructure.seaweedfs.encryption.secretName=seaweedfs-sse-kek",)),
    ("values-alpha.yaml", ()),
]


@pytest.mark.parametrize(("values", "sets"), _CASES)
def test_precompiler_write_is_prefix_wildcard(
    values: str | None, sets: tuple[str, ...]
) -> None:
    """Precompiler Write grant is scoped to precompiled/* -- matches object keys, not the bucket."""
    ids = {i["name"]: i for i in _identities(values, *sets)}
    pre = ids["svc-bundles-precompiler"]["actions"]
    writes = [a for a in pre if a.startswith("Write:")]
    assert len(writes) == 1, writes
    assert writes[0].endswith("/precompiled/*"), writes
    # Least privilege: never the whole bucket.
    assert "/" in writes[0].split(":", 1)[1].rstrip("*").rstrip("/")


@pytest.mark.parametrize(("values", "sets"), _CASES)
def test_no_prefix_grant_without_trailing_wildcard(
    values: str | None, sets: tuple[str, ...]
) -> None:
    """No identity carries an 'Action:bucket/prefix' grant lacking a trailing '*'."""
    identities = _identities(values, *sets)
    examined = 0
    bad: list[str] = []
    for ident in identities:
        for action in ident["actions"]:
            examined += 1
            if ":" not in action:
                continue
            resource = action.split(":", 1)[1]
            if "/" in resource and not resource.endswith("*"):
                bad.append(f"{ident['name']}: {action}")
    assert examined >= len(identities) >= 1
    assert not bad, f"prefix grants without trailing '*' never match object keys: {bad}"
