"""Helm-template assertions for the per-service database accounts (H-1 / H-3).

# regression: every workload connected as the shared database superuser (alpha/beta, 2026-10)

Renders the chart for alpha, local, and the beta topology (beta values with the alpha secret
tier, exactly as CI does) and asserts, against the real rendered manifests -- not a hand
reading of the templates -- that:

1. the database OWNER credential is confined to the Postgres Deployment and the db-migrate
   hook Secret/Job: it is not a key of `waddlebot-secrets`, and its value appears in no
   other rendered object;
2. every database-using workload connects as ITS OWN catalog role (`DB_USER`), never the
   owner, with `DB_PASSWORD` sourced from that role's own `PW_<ROLE>` key via `secretKeyRef`
   (no workload `envFrom`s the credentials/admin Secrets wholesale) and `DATABASE_URL` carrying
   only a `$(DB_PASSWORD)` reference;
3. per-role passwords are distinct, strong, URL-safe, and identical in the credentials Secret
   and the db-migrate hook Secret's JSON (so Postgres and the pods can never diverge);
4. production-tier renders fail closed without credentials, and weak/placeholder values are
   rejected.

Zero rendered documents / zero matching containers is a hard failure, never a silent pass.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_CATALOG = yaml.safe_load(
    (_CHART_DIR.parents[2] / "config" / "postgres" / "service-roles.yaml").read_text(
        encoding="utf-8"
    )
)
_ROLES = list(_CATALOG["roles"])
_OWNER = "waddlebot"
_OWNER_SECRET_KEYS = {
    "POSTGRES_PASSWORD",
    "DB_PASS",
    "DATABASE_PASSWORD",
    "DATABASE_URL",
    "DB_PASSWORD",
}


def _helm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _render(*args: str) -> list[dict[str, Any]]:
    result = _helm(*args)
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stderr}")
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


@pytest.fixture(scope="module")
def helm_available() -> None:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")


@pytest.fixture(scope="module")
def renders(helm_available: None) -> dict[str, list[dict[str, Any]]]:
    return {
        "alpha": _render("--values", str(_CHART_DIR / "values-alpha.yaml")),
        "local": _render("--values", str(_CHART_DIR / "values-local.yaml")),
        "beta": _render(
            "--values", str(_CHART_DIR / "values-beta.yaml"), "--set", "global.deploymentTier=alpha"
        ),
    }


def _secret(docs: list[dict[str, Any]], name: str) -> dict[str, Any]:
    found = [d for d in docs if d.get("kind") == "Secret" and d["metadata"]["name"] == name]
    assert len(found) == 1, f"expected exactly one Secret {name!r}, found {len(found)}"
    return dict(found[0].get("stringData") or {})


def _pod_spec(doc: dict[str, Any]) -> dict[str, Any] | None:
    spec = doc.get("spec") or {}
    if doc.get("kind") == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    if doc.get("kind") in ("Deployment", "StatefulSet", "Job", "CronJob", "DaemonSet"):
        return spec["template"]["spec"]
    return None


def _containers(docs: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    out = []
    for doc in docs:
        pod = _pod_spec(doc)
        if pod is None:
            continue
        for container in [*(pod.get("containers") or []), *(pod.get("initContainers") or [])]:
            out.append((doc, container))
    return out


def _env(container: dict[str, Any]) -> list[dict[str, Any]]:
    return list(container.get("env") or [])


def _secret_refs(container: dict[str, Any]) -> set[str]:
    names = {ef["secretRef"]["name"] for ef in container.get("envFrom") or [] if "secretRef" in ef}
    names |= {
        e["valueFrom"]["secretKeyRef"]["name"]
        for e in _env(container)
        if "secretKeyRef" in (e.get("valueFrom") or {})
    }
    return names


@pytest.mark.parametrize("env_name", ["alpha", "local", "beta"])
def test_owner_credential_is_not_in_the_shared_secret(
    renders: dict[str, list[dict[str, Any]]], env_name: str
) -> None:
    shared = _secret(renders[env_name], "waddlebot-secrets")
    assert len(shared) > 20, "shared Secret unexpectedly small -- wrong object?"
    leaked = _OWNER_SECRET_KEYS & set(shared)
    assert not leaked, f"waddlebot-secrets still carries owner DB credential keys: {sorted(leaked)}"


@pytest.mark.parametrize("env_name", ["alpha", "local", "beta"])
def test_owner_credential_reaches_only_postgres_and_the_migrate_job(
    renders: dict[str, list[dict[str, Any]]], env_name: str
) -> None:
    docs = renders[env_name]
    admin = _secret(docs, "waddlebot-db-admin")
    owner_password = admin["POSTGRES_PASSWORD"]
    assert len(owner_password) >= 16
    consumers = {
        doc["metadata"]["name"]
        for doc, container in _containers(docs)
        if "waddlebot-db-admin" in _secret_refs(container)
    }
    has_postgres = any(
        d.get("kind") == "Deployment" and d["metadata"]["name"] == "postgres" for d in docs
    )
    # values-local points at an external database, so no in-chart Postgres Deployment exists
    assert consumers == ({"postgres"} if has_postgres else set()), (
        f"owner Secret consumed by {consumers}"
    )
    if env_name in ("alpha", "beta"):
        assert has_postgres, (
            "alpha/beta render the in-chart Postgres -- the denominator must include it"
        )
    migrate = _secret(docs, "waddlebot-db-migrate-secret")
    assert owner_password in migrate["DATABASE_URL"]
    # The owner password must appear in exactly these two Secrets and nowhere else.
    holders = [
        f"{d['kind']}/{d['metadata']['name']}"
        for d in docs
        if owner_password in json.dumps(d, sort_keys=True)
    ]
    assert sorted(holders) == ["Secret/waddlebot-db-admin", "Secret/waddlebot-db-migrate-secret"], (
        holders
    )


_EXPECTED_ALPHA = {
    "waddlebot-hub-api-v3": "waddles_hub_api",
    "waddlebot-core-bundle-seeder": "waddles_hub_api",
    "waddlebot-bundle-signing-reconciler": "waddles_hub_api",
    "waddlebot-svc-action-rust": "waddles_svc_action",
    "waddlebot-svc-process-rust": "waddles_svc_process",
    "waddlebot-svc-ingest-rust": "waddles_svc_ingest",
    "waddlebot-svc-presentation": "waddles_svc_presentation",
    "waddlebot-svc-streaming": "waddles_svc_streaming",
    "waddlebot-reputation": "waddles_reputation",
}


@pytest.mark.parametrize("env_name", ["alpha", "local", "beta"])
def test_every_db_workload_uses_its_own_non_owner_role(
    renders: dict[str, list[dict[str, Any]]], env_name: str
) -> None:
    docs = renders[env_name]
    seen: dict[str, str] = {}
    examined = 0
    for doc, container in _containers(docs):
        env = {e["name"]: e for e in _env(container)}
        if "DB_USER" not in env:
            # a workload without a DB identity must not inherit one from the shared Secret
            continue
        examined += 1
        role = env["DB_USER"]["value"]
        name = doc["metadata"]["name"]
        assert role in _ROLES, f"{name}: DB_USER {role!r} is not a catalog role"
        assert role != _OWNER, f"{name} connects as the database owner"
        for alias in ("DB_PASSWORD", "DB_PASS", "DATABASE_PASSWORD"):
            ref = env[alias]["valueFrom"]["secretKeyRef"]
            assert ref == {"name": "waddlebot-db-credentials", "key": f"PW_{role.upper()}"}, (
                name,
                alias,
                ref,
            )
        url = env["DATABASE_URL"]["value"]
        assert url.startswith(f"postgresql://{role}:$(DB_PASSWORD)@"), (name, url)
        order = [e["name"] for e in _env(container)]
        assert order.index("DB_PASSWORD") < order.index("DATABASE_URL"), (
            "$(DB_PASSWORD) must be defined first"
        )
        assert len(order) == len(set(order)), f"{name}: duplicate env names"
        seen[name] = role
    assert examined >= 8, f"only {examined} DB-identity containers examined in {env_name}"
    if env_name == "alpha":
        for workload, role in _EXPECTED_ALPHA.items():
            assert seen.get(workload) == role, (
                f"{workload}: expected {role}, got {seen.get(workload)}"
            )
    if env_name in ("local", "beta"):
        assert len(set(seen.values())) >= 25, "legacy pods must each have their own role"
    # one role per workload family, never a role shared between unrelated workloads
    by_role: dict[str, set[str]] = {}
    for workload, role in seen.items():
        by_role.setdefault(role, set()).add(workload)
    shared_ok = {"waddles_hub_api"}  # hub-api + its CLI Jobs/CronJobs are one codebase
    for role, workloads in by_role.items():
        if role not in shared_ok:
            assert len(workloads) <= 2, f"{role} shared by {sorted(workloads)}"


@pytest.mark.parametrize("env_name", ["alpha", "local", "beta"])
def test_no_workload_envfroms_the_credential_secrets_wholesale(
    renders: dict[str, list[dict[str, Any]]], env_name: str
) -> None:
    examined = 0
    for doc, container in _containers(renders[env_name]):
        examined += 1
        whole = {
            ef["secretRef"]["name"] for ef in container.get("envFrom") or [] if "secretRef" in ef
        }
        assert not whole & {
            "waddlebot-db-credentials",
            "waddlebot-db-admin",
            "waddlebot-db-migrate-secret",
        } or (doc["metadata"]["name"] == "waddlebot-db-migrate"), (
            f"{doc['metadata']['name']} envFrom's a DB credential Secret wholesale"
        )
    assert examined > 10


@pytest.mark.parametrize("env_name", ["alpha", "local", "beta"])
def test_role_passwords_are_distinct_strong_and_match_the_migrate_hook(
    renders: dict[str, list[dict[str, Any]]], env_name: str
) -> None:
    docs = renders[env_name]
    creds = _secret(docs, "waddlebot-db-credentials")
    assert set(creds) == {f"PW_{r.upper()}" for r in _ROLES}
    values = list(creds.values())
    assert len(set(values)) == len(_ROLES), "role passwords must be pairwise distinct"
    for key, value in creds.items():
        assert re.fullmatch(r"[A-Za-z0-9._~-]{16,}", value), key
        assert not re.search(r"(?i)changeme|change_me|replace_me|example|password|dev_", value), key
    hook = _secret(docs, "waddlebot-db-migrate-secret")
    assert json.loads(hook["WADDLES_DB_SERVICE_ROLE_PASSWORDS"]) == {
        r: creds[f"PW_{r.upper()}"] for r in _ROLES
    }
    assert hook["WADDLES_DEPLOYMENT_TIER"] in {"alpha", "local"}
    # each role password appears only in the credentials Secret and the hook Secret
    for password in values[:5]:
        holders = {d["metadata"]["name"] for d in docs if password in json.dumps(d, sort_keys=True)}
        assert holders == {"waddlebot-db-credentials", "waddlebot-db-migrate-secret"}, holders


def test_production_tier_fails_closed_without_role_credentials(helm_available: None) -> None:
    result = _helm(
        "--values",
        str(_CHART_DIR / "values-beta.yaml"),
        "--set-string",
        f"infrastructure.postgresql.password={secrets.token_hex(12)}",
        "--set-string",
        f"infrastructure.postgresql.readerPassword={secrets.token_hex(12)}",
        "--set-string",
        f"infrastructure.redis.password={secrets.token_hex(12)}",
    )
    assert result.returncode != 0
    assert "has no value" in result.stderr and "outside alpha/local" in result.stderr


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("short", "must be >=16 chars"),
        ("changemechangeme123456", "placeholder"),
        ("has space and bang! 12345678", "must be >=16 chars"),
        ("hub_admin_dev_changeme", "placeholder"),
    ],
)
def test_weak_explicit_role_password_is_rejected(
    helm_available: None, password: str, message: str
) -> None:
    result = _helm(
        "--values",
        str(_CHART_DIR / "values-alpha.yaml"),
        "--set-string",
        f"infrastructure.postgresql.serviceRoles.passwords.waddles_hub_api={password}",
    )
    assert result.returncode != 0
    assert message in result.stderr


def test_explicit_strong_role_password_is_used_verbatim(helm_available: None) -> None:
    strong = secrets.token_hex(16)
    docs = _render(
        "--values",
        str(_CHART_DIR / "values-alpha.yaml"),
        "--set-string",
        f"infrastructure.postgresql.serviceRoles.passwords.waddles_hub_api={strong}",
    )
    assert _secret(docs, "waddlebot-db-credentials")["PW_WADDLES_HUB_API"] == strong


def test_existing_secret_is_referenced_and_not_rendered(helm_available: None) -> None:
    docs = _render(
        "--values",
        str(_CHART_DIR / "values-alpha.yaml"),
        "--set",
        "infrastructure.postgresql.serviceRoles.existingSecret=ext-db-creds",
        "--set",
        "infrastructure.postgresql.admin.existingSecret=ext-db-admin",
    )
    names = {d["metadata"]["name"] for d in docs if d.get("kind") == "Secret"}
    assert "waddlebot-db-credentials" not in names and "waddlebot-db-admin" not in names
    refs = {ref for _, c in _containers(docs) for ref in _secret_refs(c)}
    assert "ext-db-creds" in refs and "ext-db-admin" in refs
    assert "waddlebot-db-credentials" not in refs and "waddlebot-db-admin" not in refs


def test_unknown_role_in_a_template_fails_the_render(helm_available: None) -> None:
    result = _helm(
        "--values",
        str(_CHART_DIR / "values-alpha.yaml"),
        "--set",
        "infrastructure.postgresql.serviceRoles.roles={waddles_hub_api}",
    )
    assert result.returncode != 0
    assert "unknown PostgreSQL service role" in result.stderr
