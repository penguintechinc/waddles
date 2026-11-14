"""Render test: the reputation DB role password reaches both the migration and svc-process-rust.

`alembic/versions/0050_bundle_reputation_store.py` provisions `waddles_bundle_reputation` from
`DB_REPUTATION_PASSWORD` (db-migrate hook Secret); svc-process-rust logs in as that role using
`BUNDLE_REPUTATION_PASSWORD` (`core/svc_process/src/config.rs`). Both must come from the SAME
generated Secret value via secretKeyRef (never a literal `value:`), non-empty, never a
placeholder.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"


@pytest.fixture(scope="module")
def docs() -> list[dict[str, Any]]:
    """Render the chart with values-alpha.yaml (skips only when the helm CLI is absent)."""
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        [
            "helm", "template", "waddlebot", str(_CHART_DIR),
            "--kube-version", "1.30.0", "--values", str(_ALPHA_VALUES),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    parsed = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert parsed, "helm template produced zero documents -- cannot be a pass"
    return parsed


def _secret(docs: list[dict[str, Any]], name_suffix: str) -> dict[str, Any]:
    matches = [
        d
        for d in docs
        if d.get("kind") == "Secret" and d.get("metadata", {}).get("name", "").endswith(name_suffix)
    ]
    assert matches, f"no Secret ending {name_suffix!r} rendered"
    return matches[0]


def test_secrets_carry_the_same_non_placeholder_value(docs: list[dict[str, Any]]) -> None:
    main = (_secret(docs, "waddlebot-secrets").get("stringData") or {}).get("DB_REPUTATION_PASSWORD")
    hook = (_secret(docs, "-db-migrate-secret").get("stringData") or {}).get(
        "DB_REPUTATION_PASSWORD"
    )
    assert main and hook, "DB_REPUTATION_PASSWORD missing/empty on a rendered Secret"
    assert main == hook, "the role password and the service password must be one value"
    assert "REPLACE_ME" not in main and len(main) >= 16


def test_svc_process_reads_it_via_secret_key_ref(docs: list[dict[str, Any]]) -> None:
    examined = 0
    for doc in docs:
        if doc.get("kind") != "Deployment" or not doc["metadata"]["name"].endswith(
            "svc-process-rust"
        ):
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            if container["name"] != "svc-process-rust":
                continue
            examined += 1
            entries = [e for e in container.get("env", []) if e["name"] == "BUNDLE_REPUTATION_PASSWORD"]
            assert len(entries) == 1, "BUNDLE_REPUTATION_PASSWORD must be set exactly once"
            assert "value" not in entries[0], "must be secretKeyRef only, never a literal"
            ref = entries[0]["valueFrom"]["secretKeyRef"]
            assert ref["key"] == "DB_REPUTATION_PASSWORD"
    assert examined == 1, f"expected exactly one svc-process-rust container, saw {examined}"


def test_db_migrate_job_receives_the_hook_secret(docs: list[dict[str, Any]]) -> None:
    hook = _secret(docs, "-db-migrate-secret")["metadata"]["name"]
    jobs = [
        d
        for d in docs
        if d.get("kind") == "Job" and d.get("metadata", {}).get("name", "").endswith("-db-migrate")
    ]
    assert jobs, "no *-db-migrate Job rendered"
    refs = {
        r["secretRef"]["name"]
        for r in jobs[0]["spec"]["template"]["spec"]["containers"][0].get("envFrom", []) or []
        if "secretRef" in r
    }
    assert hook in refs
