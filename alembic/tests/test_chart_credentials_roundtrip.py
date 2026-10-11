"""Chart <-> database credential round trip on a real Postgres (H-1 / H-3).

Renders the alpha chart with `helm template`, takes the db-migrate HOOK Secret exactly as the
Job would receive it (`DATABASE_URL`, `WADDLES_DB_SERVICE_ROLE_PASSWORDS`, ...), provisions a
fresh-replayed database from it (alembic + the same `service_roles.py reconcile --strict`
`migrations/run-alembic.sh` runs), then authenticates as EVERY workload container the chart
renders using precisely the env Kubernetes would hand it (`DB_USER`, the role's
`PW_<ROLE>` secretKeyRef, `DATABASE_URL` with `$(DB_PASSWORD)` expanded). Catches the
class of bug no unit test sees: a role name or password that the chart renders but the
database was never given, or vice versa.

Skipped (never failed) where `docker` or `helm` is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import Any

import psycopg2
import pytest
import yaml
from pg_docker import DOCKER_AVAILABLE, REPO_ROOT, alembic_cli, empty_postgres

CHART = REPO_ROOT / "k8s" / "helm" / "waddlebot"

pytestmark = pytest.mark.skipif(
    not (DOCKER_AVAILABLE and shutil.which("helm")), reason="docker and helm are required"
)


def _render_alpha() -> list[dict[str, Any]]:
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        [
            "helm",
            "template",
            "waddlebot",
            str(CHART),
            "--kube-version",
            "1.30.0",
            "--values",
            str(CHART / "values-alpha.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs
    return docs


def test_every_rendered_workload_identity_authenticates_against_the_provisioned_database() -> None:
    docs = _render_alpha()
    secrets = {
        d["metadata"]["name"]: dict(d.get("stringData") or {})
        for d in docs
        if d["kind"] == "Secret"
    }
    hook = secrets["waddlebot-db-migrate-secret"]
    credentials = secrets["waddlebot-db-credentials"]
    owner_password = secrets["waddlebot-db-admin"]["POSTGRES_PASSWORD"]

    with empty_postgres("chart-roundtrip") as db:
        owner_dsn = f"postgresql://waddlebot:{owner_password}@{db.host}:{db.port}/{db.dbname}"
        # `empty_postgres` boots with a fixed owner password; align it with the chart's.
        with psycopg2.connect(db.dsn) as admin, admin.cursor() as cur:
            cur.execute("ALTER ROLE waddlebot PASSWORD %s", (owner_password,))
        job_env = {
            "WADDLES_DB_SERVICE_ROLE_PASSWORDS": hook["WADDLES_DB_SERVICE_ROLE_PASSWORDS"],
            "WADDLES_DEPLOYMENT_TIER": hook["WADDLES_DEPLOYMENT_TIER"],
            "DB_READER_PASSWORD": hook["DB_READER_PASSWORD"],
        }
        alembic_cli("upgrade", "head", dsn=owner_dsn, env_overrides=job_env)
        reconcile = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "db" / "service_roles.py"),
                "reconcile",
                "--strict",
            ],
            env={"DATABASE_URL": owner_dsn, "PATH": "/usr/bin:/bin:/usr/local/bin", **job_env},
            capture_output=True,
            text=True,
            check=False,
        )
        assert reconcile.returncode == 0, reconcile.stderr
        assert "Reconciled 33 service roles" in reconcile.stdout

        connected: dict[str, str] = {}
        for doc in docs:
            if doc["kind"] not in ("Deployment", "Job", "CronJob"):
                continue
            spec = doc["spec"]["jobTemplate"]["spec"] if doc["kind"] == "CronJob" else doc["spec"]
            for container in spec["template"]["spec"].get("containers", []):
                env = {e["name"]: e for e in container.get("env") or []}
                if "DB_USER" not in env:
                    continue
                key = env["DB_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"]
                url = (
                    env["DATABASE_URL"]["value"]
                    .replace("$(DB_PASSWORD)", credentials[key])
                    .replace("infra-postgres:5432", f"{db.host}:{db.port}")
                )
                with psycopg2.connect(url, connect_timeout=10) as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT current_user, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
                    )
                    user, is_super = cur.fetchone()
                assert user == env["DB_USER"]["value"] and is_super is False
                connected[doc["metadata"]["name"]] = user
        assert len(connected) >= 8, f"only {len(connected)} workload identities examined"
        assert "waddlebot" not in connected.values()
        # the owner credential is for Postgres + the migrate Job only: no workload env holds it
        blob = json.dumps(
            [d for d in docs if d["kind"] in ("Deployment", "Job", "CronJob")], sort_keys=True
        )
        assert owner_password not in blob


def test_chart_values_role_names_equal_the_database_roles() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    catalog = yaml.safe_load(
        (REPO_ROOT / "config" / "postgres" / "service-roles.yaml").read_text(encoding="utf-8")
    )
    assert values["infrastructure"]["postgresql"]["serviceRoles"]["roles"] == list(catalog["roles"])
