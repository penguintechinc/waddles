"""Helm render assertions for the bundle platform-table DB role password.

Follows the chart convention (templates/secrets.yaml, like DB_READER_PASSWORD): the value
comes from `waddlebot.autoSecretValue` -- generated + persisted on alpha/local, explicit
`infrastructure.postgresql.platformTablePassword` wins, and OUTSIDE alpha/local an unset
value fails the render (that helper's fail-closed branch; a full prod render trips earlier
unrelated required-material checks, so the policy hook is asserted from the template).
The same value must reach the shared Secret (svc-process-rust, via secretKeyRef) and the
db-migrate hook Secret (role provisioning). Zero documents / matches is a hard failure.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[1]
_KEY = "BUNDLE_PLATFORM_TABLE_PASSWORD"


def _render(*sets: str) -> list[dict[str, Any]]:
    argv = [
        "helm", "template", "waddlebot", str(_CHART), "--kube-version", "1.30.0",
        "--values", str(_CHART / "values-alpha.yaml"),
    ]  # fmt: skip
    for s in sets:
        argv += ["--set", s]
    out = subprocess.run(argv, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        pytest.fail(f"helm template failed:\n{out.stderr}")
    docs = [d for d in yaml.safe_load_all(out.stdout) if d]
    assert docs, "helm template produced zero documents"
    return docs


def _secret(docs: list[dict[str, Any]], suffix: str) -> dict[str, str]:
    matches = [
        d
        for d in docs
        if d.get("kind") == "Secret" and d["metadata"]["name"].endswith(suffix)
    ]
    assert matches, f"no Secret ending {suffix!r} rendered"
    out: dict[str, str] = {}
    for d in matches:
        out.update(d.get("stringData") or {})
    return out


def _process_env(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    envs: dict[str, dict[str, Any]] = {}
    matched = 0
    for d in docs:
        if d.get("kind") == "Deployment" and "svc-process" in d["metadata"]["name"]:
            for c in d["spec"]["template"]["spec"]["containers"]:
                matched += 1
                envs.update({e["name"]: e for e in c.get("env", [])})
    assert matched, "no svc-process container rendered"
    return envs


@pytest.fixture(scope="module")
def helm_available() -> None:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available")


def test_alpha_generates_one_value_for_shared_and_migrate_secrets(
    helm_available: None,
) -> None:
    docs = _render()
    shared = _secret(docs, "-secrets")[_KEY]
    migrate = _secret(docs, "-db-migrate-secret")["DB_PLATFORM_TABLE_PASSWORD"]
    assert len(shared) >= 16
    assert shared == migrate  # role provisioning and the service can never drift apart


def test_svc_process_reads_it_by_secretkeyref_never_a_literal(
    helm_available: None,
) -> None:
    env = _process_env(_render())
    pw = env[_KEY]
    assert "value" not in pw
    ref = pw["valueFrom"]["secretKeyRef"]
    assert ref["key"] == _KEY
    assert ref["name"].endswith("-secrets")
    assert env["BUNDLE_PLATFORM_TABLE_USER"]["value"] == "waddles_platform_table"


def test_explicit_value_wins(helm_available: None) -> None:
    docs = _render(
        "infrastructure.postgresql.platformTablePassword=explicit-pt-secret-value-01"
    )
    assert _secret(docs, "-secrets")[_KEY] == "explicit-pt-secret-value-01"
    assert (
        _secret(docs, "-db-migrate-secret")["DB_PLATFORM_TABLE_PASSWORD"]
        == "explicit-pt-secret-value-01"
    )


def test_outside_alpha_local_the_value_is_required_via_the_shared_policy() -> None:
    src = (_CHART / "templates" / "secrets.yaml").read_text(encoding="utf-8")
    assert f'"key" "{_KEY}"' in src
    helper = (_CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    assert "outside alpha/local -- auto-generation is alpha/local only" in helper
