"""Render assertions for the generated INITIAL_ADMIN_PASSWORD + hub-admin-seed hook Job."""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA = _CHART_DIR / "values-alpha.yaml"


@pytest.fixture(scope="module", autouse=True)
def _require_helm() -> None:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")


def _render(extra_set: list[str] | None = None) -> list[dict[str, Any]]:
    args = ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0", "--values", str(_ALPHA)]
    for kv in extra_set or []:
        args += ["--set", kv]
    res = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, test-only
    assert res.returncode == 0, res.stderr
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def _by_name(docs: list[dict[str, Any]], kind: str, suffix: str) -> dict[str, Any]:
    found = [d for d in docs if d.get("kind") == kind and d["metadata"]["name"].endswith(suffix)]
    assert len(found) == 1, f"expected exactly one {kind} *{suffix}, got {len(found)}"
    return found[0]


@pytest.fixture(scope="module")
def docs() -> list[dict[str, Any]]:
    return _render()


def _secret_value(d: list[dict[str, Any]]) -> str:
    sec = _by_name(d, "Secret", "waddlebot-secrets")
    return (sec.get("stringData") or {}).get("INITIAL_ADMIN_PASSWORD") or base64.b64decode(
        (sec.get("data") or {}).get("INITIAL_ADMIN_PASSWORD", "")
    ).decode()


def test_secret_has_generated_nonempty_password(docs: list[dict[str, Any]]) -> None:
    pw = _secret_value(docs)
    assert len(pw) >= 24 and pw.isalnum()


def test_explicit_password_wins_over_generation() -> None:
    d = _render(["global.initialAdmin.password=ExplicitAdminPw123456789x"])
    assert _secret_value(d) == "ExplicitAdminPw123456789x"


def test_generate_if_absent_structure() -> None:
    helpers = (_CHART_DIR / "templates" / "_helpers.tpl").read_text()
    secrets = (_CHART_DIR / "templates" / "secrets.yaml").read_text()
    # lookup-KEEP of non-empty existing value precedes generation in the shared helper
    assert helpers.index("secretKeyNonEmpty\" (dict \"existing\" $existing") < helpers.index("randAlphaNum (int $length)")
    assert '"key" "INITIAL_ADMIN_PASSWORD"' in secrets and '"alwaysGenerate" true' in secrets


def test_seed_job_hook_annotations_image_envfrom(docs: list[dict[str, Any]]) -> None:
    job = _by_name(docs, "Job", "hub-admin-seed")
    ann = job["metadata"]["annotations"]
    assert ann["helm.sh/hook"] == "post-install,post-upgrade"
    assert int(ann["helm.sh/hook-weight"]) > -5  # after db-migrate
    assert ann["helm.sh/hook-delete-policy"] == "before-hook-creation,hook-succeeded"
    spec = job["spec"]["template"]["spec"]
    c = spec["containers"][0]
    migrate = _by_name(docs, "Job", "db-migrate")["spec"]["template"]["spec"]["containers"][0]
    assert c["image"] == migrate["image"]
    assert spec["restartPolicy"] == "Never" and job["spec"]["backoffLimit"] <= 3
    assert not c.get("envFrom")  # credential-separation: single-key refs only
    env = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in c["env"]}
    assert env["DATABASE_URL"]["name"].endswith("-db-migrate-secret")
    assert env["INITIAL_ADMIN_PASSWORD"]["name"] == "waddlebot-secrets"
    assert env["INITIAL_ADMIN_EMAIL"]["name"] == "waddlebot-secrets"
    assert "081_seed_default_hub_admin.sql" in c["args"][0]


def test_seed_job_rootless_with_resources(docs: list[dict[str, Any]]) -> None:
    c = _by_name(docs, "Job", "hub-admin-seed")["spec"]["template"]["spec"]["containers"][0]
    assert c["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["resources"]["requests"] and c["resources"]["limits"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
