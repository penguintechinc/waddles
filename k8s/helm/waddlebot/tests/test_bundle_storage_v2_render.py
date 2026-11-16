"""Helm-template + post-render assertions for bundle storage v2 wiring (docs/bundle-storage-v2.md).

Regression (alpha helm upgrade, 2026-11-16): the post-upgrade core-bundle seeder failed
`waddles.core.service.music-requests` with `bundle_data_provisioning_unavailable` because
BUNDLE_MIGRATOR_PASSWORD was empty (feature off in values, no Secret, role with no password);
its consumers then failed 422 `tool_target_cross_tenant`.

Asserts, against the real rendered manifests and the REAL provision-secrets hook (post-render):

1. Default (every tier's values file): hub-api + the seeder get BUNDLE_MIGRATOR_PASSWORD from the
   chart-provisioned `waddlebot-bundle-migrator`; svc-process-rust / svc-action-rust (ONLY) get
   BUNDLE_DB_* from `waddlebot-bundle-db`; no other workload gets either.
2. The hook creates both Secrets (create-if-absent, never overwritten), hands the SAME values to the
   db-migrate hook Secret, scrubs the retired keys from the envFrom'd waddlebot-secrets, and the
   values appear in no other object.
3. An operator existingSecret is referenced and never touched; a missing one fails the hook closed.

Zero matching containers is a hard failure, never a vacuous pass.
"""

from __future__ import annotations

import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_CHART_DIR / "tests"))
from provision_harness import hook_failure, run_hook, spec_of, with_provisioned  # noqa: E402

_VALUES = _CHART_DIR / "values-dal2alpha.yaml"
_TIERS = (
    "values-dal2alpha.yaml",
    "values-alpha.yaml",
    "values-beta.yaml",
    "values-gamma.yaml",
    "values-production.yaml",
)
_DB_WORKLOADS = {"svc-process-rust", "svc-action-rust"}
_MIGRATOR_SECRET = "waddlebot-bundle-migrator"
_DB_SECRET = "waddlebot-bundle-db"
_MIGRATE_SECRET = "waddlebot-db-migrate-secret"


def _throwaway(label: str) -> str:
    """A per-run random test value (never a literal a secret scanner would flag)."""
    return f"{label}-{secrets.token_urlsafe(18)}"


def _render(
    extra: list[str], values: Path = _VALUES
) -> subprocess.CompletedProcess[str]:
    """Run helm template with a values file plus extra args."""
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        [
            "helm",
            "template",
            "waddlebot",
            str(_CHART_DIR),
            "--kube-version",
            "1.30.0",
            "--values",
            str(values),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _docs(extra: list[str], values: Path = _VALUES) -> list[dict[str, Any]]:
    res = _render(extra, values)
    if res.returncode != 0:
        pytest.fail(f"helm template failed:\n{res.stderr}")
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs, "nothing rendered"
    return docs


def _workload_env(docs: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """Map workload (Deployment component / Job name) -> {env name: env entry}, all containers."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for doc in docs:
        if doc.get("kind") not in ("Deployment", "Job"):
            continue
        meta = doc["metadata"]
        name = (
            meta.get("labels", {}).get("app.kubernetes.io/component", meta["name"])
            if doc["kind"] == "Deployment"
            else meta["name"]
        )
        for c in doc["spec"]["template"]["spec"]["containers"]:
            out.setdefault(name, {}).update({e["name"]: e for e in c.get("env") or []})
    assert out, "no workloads rendered"
    return out


def _secrets(docs: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    return {
        d["metadata"]["name"]: dict(d.get("stringData") or {})
        for d in docs
        if d["kind"] == "Secret" and d.get("x-provisioned")
    }


@pytest.mark.parametrize("values", _TIERS)
def test_default_wires_chart_provisioned_secrets_to_owning_pods_only(
    values: str,
) -> None:
    workloads = _workload_env(_docs([], _CHART_DIR / values))
    assert _DB_WORKLOADS <= workloads.keys(), (
        f"missing: {_DB_WORKLOADS - workloads.keys()}"
    )
    migrator_holders = set()
    for name, env in workloads.items():
        if "BUNDLE_MIGRATOR_PASSWORD" in env:
            ref = env["BUNDLE_MIGRATOR_PASSWORD"]["valueFrom"]["secretKeyRef"]
            assert ref == {
                "name": _MIGRATOR_SECRET,
                "key": "BUNDLE_MIGRATOR_PASSWORD",
            }, name
            migrator_holders.add(name)
        if name in _DB_WORKLOADS:
            for var in (
                "BUNDLE_DB_HOST",
                "BUNDLE_DB_PORT",
                "BUNDLE_DB_NAME",
                "BUNDLE_DB_USER",
            ):
                assert var in env, f"{name} missing {var}"
            ref = env["BUNDLE_DB_PASSWORD"]["valueFrom"]["secretKeyRef"]
            assert ref == {"name": _DB_SECRET, "key": "BUNDLE_DB_PASSWORD"}
        else:
            assert not any(n.startswith("BUNDLE_DB_") for n in env), (
                f"{name} got BUNDLE_DB_*"
            )
    assert "hub-api" in migrator_holders, migrator_holders
    assert "waddlebot-core-bundle-seeder" in migrator_holders, migrator_holders
    assert migrator_holders == {"hub-api", "waddlebot-core-bundle-seeder"}, (
        migrator_holders
    )


@pytest.mark.parametrize("values", _TIERS)
def test_hook_generates_both_secrets_and_hands_the_same_values_to_db_migrate(
    values: str,
) -> None:
    docs = with_provisioned(_docs([], _CHART_DIR / values))
    secrets = _secrets(docs)
    migrator = secrets[_MIGRATOR_SECRET]["BUNDLE_MIGRATOR_PASSWORD"]
    runtime = secrets[_DB_SECRET]["BUNDLE_DB_PASSWORD"]
    assert len(migrator) >= 32 and len(runtime) >= 32 and migrator != runtime
    hook = secrets[_MIGRATE_SECRET]
    assert hook["BUNDLE_MIGRATOR_PASSWORD"] == migrator
    assert hook["BUNDLE_RUNTIME_PASSWORD"] == runtime
    # retired keys scrubbed from the Secret ~40 workloads envFrom
    assert secrets["waddlebot-secrets"].get("BUNDLE_MIGRATOR_PASSWORD", "") == ""
    assert secrets["waddlebot-secrets"].get("BUNDLE_RUNTIME_PASSWORD", "") == ""
    owners = {_MIGRATOR_SECRET, _DB_SECRET, _MIGRATE_SECRET}
    examined = 0
    for d in docs:
        if d.get("x-provisioned") and d["metadata"]["name"] in owners:
            continue
        blob = yaml.safe_dump(d)
        assert migrator not in blob and runtime not in blob, d["metadata"].get("name")
        examined += 1
    assert examined > 50, examined


def test_existing_values_are_never_overwritten() -> None:
    docs = _docs([])
    kept = {
        _MIGRATOR_SECRET: {
            "BUNDLE_MIGRATOR_PASSWORD": _throwaway("kept-migrator-value")
        },
        _DB_SECRET: {"BUNDLE_DB_PASSWORD": _throwaway("kept-runtime-value")},
    }
    res, state = run_hook(docs, existing=kept)
    assert res.returncode == 0, res.stderr
    assert state[_MIGRATOR_SECRET]["data"] == kept[_MIGRATOR_SECRET]
    assert state[_DB_SECRET]["data"] == kept[_DB_SECRET]
    hook = state[_MIGRATE_SECRET]["data"]
    assert (
        hook["BUNDLE_MIGRATOR_PASSWORD"]
        == kept[_MIGRATOR_SECRET]["BUNDLE_MIGRATOR_PASSWORD"]
    )
    assert hook["BUNDLE_RUNTIME_PASSWORD"] == kept[_DB_SECRET]["BUNDLE_DB_PASSWORD"]


def test_legacy_value_in_waddlebot_secrets_is_imported_then_scrubbed() -> None:
    legacy = _throwaway("legacy-migrator-value")
    res, state = run_hook(
        _docs([]), existing={"waddlebot-secrets": {"BUNDLE_MIGRATOR_PASSWORD": legacy}}
    )
    assert res.returncode == 0, res.stderr
    assert state[_MIGRATOR_SECRET]["data"]["BUNDLE_MIGRATOR_PASSWORD"] == legacy
    assert state["waddlebot-secrets"]["data"]["BUNDLE_MIGRATOR_PASSWORD"] == ""
    assert legacy not in res.stdout + res.stderr


def test_operator_existing_secret_is_referenced_and_never_provisioned() -> None:
    extra = [
        "--set",
        "bundleStorageV2.migrator.existingSecret=bundle-migrator-ext",
        "--set",
        "bundleStorageV2.db.existingSecret=bundle-db-ext",
    ]
    docs = _docs(extra)
    env = _workload_env(docs)
    assert (
        env["svc-process-rust"]["BUNDLE_DB_PASSWORD"]["valueFrom"]["secretKeyRef"][
            "name"
        ]
        == "bundle-db-ext"
    )
    seeder = env["waddlebot-core-bundle-seeder"]["BUNDLE_MIGRATOR_PASSWORD"][
        "valueFrom"
    ]["secretKeyRef"]
    assert seeder["name"] == "bundle-migrator-ext"
    spec = spec_of(docs)
    assert (
        "secret|bundle-migrator-ext|skip=" in spec
        and "secret|bundle-db-ext|skip=" in spec
    )
    assert (
        f"secret|{_MIGRATOR_SECRET}|" not in spec
        and f"secret|{_DB_SECRET}|" not in spec
    )
    ext = {
        "bundle-migrator-ext": {
            "BUNDLE_MIGRATOR_PASSWORD": _throwaway("operator-migrator")
        },
        "bundle-db-ext": {"BUNDLE_DB_PASSWORD": _throwaway("operator-runtime")},
    }
    res, state = run_hook(docs, existing=ext)
    assert res.returncode == 0, res.stderr
    assert state["bundle-migrator-ext"]["data"] == ext["bundle-migrator-ext"]
    assert (
        state[_MIGRATE_SECRET]["data"]["BUNDLE_MIGRATOR_PASSWORD"]
        == ext["bundle-migrator-ext"]["BUNDLE_MIGRATOR_PASSWORD"]
    )
    # operator Secret absent -> the hook fails closed (never a passwordless role)
    assert "bundle-migrator-ext" in hook_failure(docs)


def test_disabled_renders_no_env_and_no_secret() -> None:
    docs = _docs(
        [
            "--set",
            "bundleStorageV2.db.enabled=false",
            "--set",
            "bundleStorageV2.migrator.enabled=false",
        ]
    )
    for name, env in _workload_env(docs).items():
        leaked = {
            n
            for n in env
            if n.startswith("BUNDLE_DB_") or n == "BUNDLE_MIGRATOR_PASSWORD"
        }
        assert not leaked, f"{name} leaked {leaked} with the feature off"
    spec = spec_of(docs)
    assert _MIGRATOR_SECRET not in spec and _DB_SECRET not in spec
