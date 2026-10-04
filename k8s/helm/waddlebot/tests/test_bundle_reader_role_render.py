"""Helm-template assertions for feature/bundle-reader-role.

# regression: RO reader role never provisioned, multi-app path stayed off (alpha 2026-10-02)

Renders the chart with `values-alpha.yaml` (the environment this change
actually turns the DB-driven multi-app data-plane path on for) and asserts,
against the real rendered manifest text -- not a hand-read of the templates
-- that:

1. The generated `waddlebot-secrets` Secret carries a non-empty
   `DB_READER_PASSWORD` (auto-generated in alpha via
   `waddlebot.autoSecretValue`'s lookup-KEEP-then-generate policy -- see
   `templates/secrets.yaml`).
2. Both `svc-process-rust` and `svc-action-rust` Deployments read
   `DB_READER_USER=waddles_bundle_reader` (the role
   `alembic/versions/0032_bundle_reader_role.py` actually provisions) and
   source `DB_READER_PASSWORD` from that same generated Secret key, never a
   literal `value:`.
3. The core-bundle-seeder Job's `CORE_BUNDLES_PLATFORM_CONNECTIONS` env var
   is present and carries the alpha Discord ingest source
   (`values-alpha.yaml`'s `pipeline.coreBundleSeeder.platformConnections` --
   moved here from rustDataPlane.svcProcess's legacy processIngestPlatform/
   processIngestSourceId fields, which PR #538 removed from alpha).

Zero rendered documents, or zero matching containers, is a hard failure
here, never a silent pass (critical-rules.md Verification Integrity) -- a
chart layout change that moved these templates would otherwise make this
test vacuously green.
"""

from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"


def _helm_template() -> list[dict[str, Any]]:
    """Render the chart with values-alpha.yaml; returns every parsed document."""
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        [
            "helm", "template", "waddlebot", str(_CHART_DIR),
            "--kube-version", "1.30.0",
            "--values", str(_ALPHA_VALUES),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


@pytest.fixture(scope="module")
def rendered_docs() -> list[dict[str, Any]]:
    if not _shutil_which("helm"):
        pytest.skip("helm CLI not available in this environment")
    return _helm_template()


def _shutil_which(cmd: str) -> str | None:
    import shutil

    return shutil.which(cmd)


def _find_container_env(
    docs: list[dict[str, Any]], deployment_name_suffix: str, container_name: str
) -> list[dict[str, Any]]:
    """Returns the named container's `env[]` list from the Deployment whose name ends with the suffix."""
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        name = doc.get("metadata", {}).get("name", "")
        if not name.endswith(deployment_name_suffix):
            continue
        containers = doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for container in containers:
            if container.get("name") == container_name:
                return list(container.get("env", []) or [])
    return []


def _env_value(env_list: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for entry in env_list:
        if entry.get("name") == name:
            return entry
    return None


class TestSecretCarriesNonEmptyReaderPassword:
    def test_db_reader_password_non_empty_in_alpha(
        self, rendered_docs: list[dict[str, Any]]
    ) -> None:
        # regression: feature/alpha-localized-posthog added a second
        # "*-secrets"-suffixed Secret (posthog-secrets) to the alpha render,
        # sorting before waddlebot-secrets and silently picking the wrong
        # Secret via a bare endswith("-secrets") match (it has no
        # DB_READER_PASSWORD key at all) -- match the exact chart Secret
        # name, never a suffix that a future infra Secret could collide with.
        secrets = [
            doc
            for doc in rendered_docs
            if doc.get("kind") == "Secret" and doc.get("metadata", {}).get("name", "") == "waddlebot-secrets"
        ]
        assert secrets, "no waddlebot-secrets Secret found in rendered manifest"
        secret = secrets[0]
        # stringData is rendered inline (never base64) -- but guard both
        # shapes in case a future chart revision switches this Secret to
        # `data:`.
        value = (secret.get("stringData") or {}).get("DB_READER_PASSWORD")
        if value is None:
            raw = (secret.get("data") or {}).get("DB_READER_PASSWORD", "")
            value = base64.b64decode(raw).decode() if raw else ""
        assert value, "DB_READER_PASSWORD rendered empty in alpha -- multi-app data-plane path stays disabled"


class TestServicesReadWaddlesBundleReader:
    @pytest.mark.parametrize(
        ("deployment_suffix", "container_name"),
        [
            ("svc-process-rust", "svc-process-rust"),
            ("svc-action-rust", "svc-action-rust"),
        ],
    )
    def test_db_reader_user_and_password_wired(
        self,
        rendered_docs: list[dict[str, Any]],
        deployment_suffix: str,
        container_name: str,
    ) -> None:
        env = _find_container_env(rendered_docs, deployment_suffix, container_name)
        assert env, f"no {container_name} container found in a Deployment ending {deployment_suffix}"

        reader_user = _env_value(env, "DB_READER_USER")
        assert reader_user is not None, "DB_READER_USER env var missing"
        assert reader_user.get("value") == "waddles_bundle_reader"

        reader_password = _env_value(env, "DB_READER_PASSWORD")
        assert reader_password is not None, "DB_READER_PASSWORD env var missing"
        secret_ref = reader_password.get("valueFrom", {}).get("secretKeyRef", {})
        assert secret_ref.get("key") == "DB_READER_PASSWORD"
        assert "value" not in reader_password, (
            "DB_READER_PASSWORD must come from secretKeyRef only -- a literal `value:` "
            "alongside `valueFrom` is rejected by the API server"
        )


class TestSeederPlatformConnections:
    def test_core_bundles_platform_connections_set(
        self, rendered_docs: list[dict[str, Any]]
    ) -> None:
        jobs = [doc for doc in rendered_docs if doc.get("kind") == "Job"]
        assert jobs, "no Job manifests rendered"
        seeder_jobs = [
            job for job in jobs if "core-bundle-seeder" in job.get("metadata", {}).get("name", "")
        ]
        assert seeder_jobs, "no core-bundle-seeder Job found in rendered manifest"

        containers = (
            seeder_jobs[0]
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        assert containers, "core-bundle-seeder Job has no containers"
        env = list(containers[0].get("env", []) or [])
        connections = _env_value(env, "CORE_BUNDLES_PLATFORM_CONNECTIONS")
        assert connections is not None, (
            "CORE_BUNDLES_PLATFORM_CONNECTIONS missing -- seeder won't register the "
            "alpha Discord ingest source"
        )
        assert "dg-474965105759748096" in connections.get("value", "")
        assert "discord" in connections.get("value", "")


class TestDbMigrateJobReceivesReaderPassword:
    """fix/chart-fresh-install-hooks (#526) replaced the per-pod dbMigrateInitContainer

    with a dedicated post-install/pre-upgrade hook Job (templates/migrations-job.yaml)
    fed by its own minimal hook Secret (templates/secrets.yaml's `-db-migrate-secret`),
    never `waddlebot-secrets` directly. `alembic/versions/0032_bundle_reader_role.py`
    still reads `DB_READER_PASSWORD` via `os.environ.get` inside that same Job, so the
    dedicated hook Secret must carry the key too -- asserting against `waddlebot-secrets`
    alone (as the chart's OTHER *_PASSWORD keys for this role would suggest) would pass
    even if the Job itself never actually received it.
    """

    def test_db_migrate_secret_carries_non_empty_reader_password(
        self, rendered_docs: list[dict[str, Any]]
    ) -> None:
        hook_secrets = [
            doc
            for doc in rendered_docs
            if doc.get("kind") == "Secret"
            and doc.get("metadata", {}).get("name", "").endswith("-db-migrate-secret")
        ]
        assert hook_secrets, "no *-db-migrate-secret Secret found in rendered manifest"
        value = (hook_secrets[0].get("stringData") or {}).get("DB_READER_PASSWORD")
        assert value, (
            "DB_READER_PASSWORD missing/empty on the db-migrate hook Secret -- "
            "0032_bundle_reader_role.py's migration would run with no password"
        )

        jobs = [
            doc
            for doc in rendered_docs
            if doc.get("kind") == "Job"
            and doc.get("metadata", {}).get("name", "").endswith("-db-migrate")
        ]
        assert jobs, "no *-db-migrate Job found in rendered manifest"
        containers = jobs[0].get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        assert containers, "db-migrate Job has no containers"
        env_from_secrets = {
            ref.get("secretRef", {}).get("name")
            for ref in containers[0].get("envFrom", []) or []
            if "secretRef" in ref
        }
        assert hook_secrets[0]["metadata"]["name"] in env_from_secrets, (
            "db-migrate Job's envFrom does not reference the Secret carrying "
            "DB_READER_PASSWORD -- the password would never reach the migration process"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
