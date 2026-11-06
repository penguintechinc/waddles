"""Helm render assertions for the bundle platform-table DB role password.

EXPLICIT OPT-IN (`infrastructure.postgresql.platformTable.enabled`, default false). Disabled:
no password anywhere (role stays NOLOGIN via alembic 0079) and no svc-process env. Enabled:
the password lives ONLY in `<release>-db-credentials` (key PW_WADDLES_PLATFORM_TABLE, generated
on alpha/local via `waddlebot.autoSecretValue`, required elsewhere) and the db-migrate hook
Secret -- never in the envFrom'd `waddlebot-secrets` -- and only svc-process-rust (secretKeyRef)
and the migrate Job reference it. Zero documents / matches is a hard failure.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[1]
_KEY = "PW_WADDLES_PLATFORM_TABLE"
_ENABLE = "infrastructure.postgresql.platformTable.enabled=true"


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


def _workload_refs(docs: list[dict[str, Any]]) -> set[str]:
    """Names of workloads whose pod spec mentions the platform-table credential at all."""
    refs: set[str] = set()
    for d in docs:
        if d.get("kind") not in {
            "Deployment",
            "StatefulSet",
            "Job",
            "CronJob",
            "DaemonSet",
        }:
            continue
        if "PLATFORM_TABLE" in yaml.safe_dump(d) or _KEY in yaml.safe_dump(d):
            refs.add(d["metadata"]["name"])
    return refs


@pytest.fixture(scope="module")
def helm_available() -> None:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available")


def test_disabled_by_default_renders_no_credential_anywhere(
    helm_available: None,
) -> None:
    docs = _render()
    text = yaml.safe_dump_all(docs)
    assert "PLATFORM_TABLE" not in text and _KEY not in text
    assert "DB_PLATFORM_TABLE_PASSWORD" not in _secret(docs, "-db-migrate-secret")
    assert not [k for k in _process_env(docs) if k.startswith("BUNDLE_PLATFORM_TABLE")]


def test_enabled_alpha_autogenerates_into_db_credentials_only(
    helm_available: None,
) -> None:
    docs = _render(_ENABLE)
    creds = _secret(docs, "-db-credentials")[_KEY]
    assert len(creds) >= 16
    assert _KEY not in _secret(
        docs, "-secrets"
    ) and "BUNDLE_PLATFORM_TABLE_PASSWORD" not in _secret(docs, "-secrets")
    assert _secret(docs, "-db-migrate-secret")["DB_PLATFORM_TABLE_PASSWORD"] == creds


def test_only_svc_process_and_migrate_reference_it_never_envfrom(
    helm_available: None,
) -> None:
    docs = _render(_ENABLE)
    refs = _workload_refs(docs)
    assert refs, "no workload references the credential"
    # The migrate Job gets it only via its dedicated hook Secret (envFrom of -db-migrate-secret,
    # asserted in the previous test), so the only workload naming it is svc-process-rust.
    assert all("svc-process" in n for n in refs), refs
    for d in docs:
        pod = (d.get("spec") or {}).get("template", {}).get("spec", {})
        for c in pod.get("containers", []) + pod.get("initContainers", []):
            for ef in c.get("envFrom", []):
                assert "db-credentials" not in (ef.get("secretRef") or {}).get(
                    "name", ""
                )
    env = _process_env(docs)
    pw = env["BUNDLE_PLATFORM_TABLE_PASSWORD"]
    assert "value" not in pw
    ref = pw["valueFrom"]["secretKeyRef"]
    assert ref["key"] == _KEY and ref["name"].endswith("-db-credentials")
    assert env["BUNDLE_PLATFORM_TABLE_USER"]["value"] == "waddles_platform_table"


def test_explicit_value_wins(helm_available: None) -> None:
    docs = _render(
        _ENABLE,
        "infrastructure.postgresql.platformTable.password=explicit-pt-secret-value-01",
    )
    assert _secret(docs, "-db-credentials")[_KEY] == "explicit-pt-secret-value-01"
    assert (
        _secret(docs, "-db-migrate-secret")["DB_PLATFORM_TABLE_PASSWORD"]
        == "explicit-pt-secret-value-01"
    )


def test_outside_alpha_local_the_value_is_required_via_the_shared_policy() -> None:
    src = (_CHART / "templates" / "secrets.yaml").read_text(encoding="utf-8")
    assert f'"key" "{_KEY}"' in src
    helper = (_CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    assert "outside alpha/local -- auto-generation is alpha/local only" in helper
