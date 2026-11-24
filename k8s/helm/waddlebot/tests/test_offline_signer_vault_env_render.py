"""Helm-template assertions: offline signers (seeder, backfill) sign without the platform KEK.

Regression (alpha deploy run 43819105673, 2026-11-16): T4 moved artifact signing to
`PlatformSigner.from_vault()`, which reads the platform-secret cache of the CURRENT process. Only
hub-api's bootstrap seeds that cache, so the core-bundle seeder Job (its own pod) failed 145/164
seeds `platform_vault has no active bundle_signing key`. The fix reads the vault's ACTIVE key by
its public columns and accepts the Job's bundle-signing Secret only when it is that key -- the
platform KEK stays inside hub-api (test_suppression_hub_api_env_render.py pins that).

Also: the seeder approves bundles with a db-v2 schema (e.g. `penguintechinc.waddles.music-requests`),
which provisions tables through `BUNDLE_MIGRATOR_PASSWORD` -- previously wired into hub-api only,
so the seeder failed `bundle_data_provisioning_unavailable` and every consumer of that service
then hit 422 `tool_target_cross_tenant`.
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


def _render(extra_set: list[str]) -> list[dict[str, Any]]:
    """`helm template` with the alpha values; fails the test on a render error or zero docs."""
    args = [
        "helm",
        "template",
        "waddlebot",
        str(_CHART_DIR),
        "--kube-version",
        "1.30.0",
        "--values",
        str(_ALPHA_VALUES),
    ]
    for kv in extra_set:
        args += ["--set", kv]
    result = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, test-only
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


@pytest.fixture(scope="module", autouse=True)
def _require_helm() -> None:
    """Skip without the helm CLI (CI's chart job has it)."""
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")


def _job_env(docs: list[dict[str, Any]], name_suffix: str) -> dict[str, Any]:
    """Env of the single container of the Job whose name ends in `name_suffix`."""
    jobs = [
        d
        for d in docs
        if d.get("kind") == "Job" and d.get("metadata", {}).get("name", "").endswith(name_suffix)
    ]
    assert len(jobs) == 1, f"expected one Job ending in {name_suffix!r}, found {len(jobs)}"
    containers = jobs[0]["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return {item["name"]: item for item in containers[0].get("env", [])}


_ENABLE = [
    "pipeline.coreBundleSeeder.enabled=true",
    "pipeline.hubApi.bundleSigningBackfill.enabled=true",
]


@pytest.mark.parametrize("suffix", ["-core-bundle-seeder", "-bundle-signing-backfill"])
def test_offline_signers_mount_the_signing_secret_but_never_the_kek(suffix: str) -> None:
    """The private half comes from the bundle-signing Secret; the platform KEK is never mounted."""
    env = _job_env(_render(_ENABLE), suffix)
    for var in ("BUNDLE_SIGNING_PRIVATE_KEY", "BUNDLE_SIGNING_KEY_ID"):
        ref = env[var]["valueFrom"]["secretKeyRef"]
        assert ref == {"name": "waddlebot-bundle-signing", "key": var}
    assert "TENANT_KEK_HEX" not in env


def test_seeder_gets_bundle_migrator_password_by_default() -> None:
    """Same BUNDLE_MIGRATOR_* wiring as hub-api, ON by default from the chart-provisioned Secret."""
    env = _job_env(_render(_ENABLE), "-core-bundle-seeder")
    ref = env["BUNDLE_MIGRATOR_PASSWORD"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "waddlebot-bundle-migrator", "key": "BUNDLE_MIGRATOR_PASSWORD"}


def test_seeder_has_no_migrator_env_while_migrator_disabled() -> None:
    """Explicitly disabled renders nothing -- no dangling secretKeyRef to a missing Secret."""
    env = _job_env(
        _render(_ENABLE + ["bundleStorageV2.migrator.enabled=false"]), "-core-bundle-seeder"
    )
    assert "BUNDLE_MIGRATOR_PASSWORD" not in env
