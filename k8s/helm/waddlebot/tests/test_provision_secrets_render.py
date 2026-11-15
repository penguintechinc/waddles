"""Render assertions for the runtime provision-secrets hook (helm3 and helm4 -- whichever `helm` is on PATH).

Proves: the hook Job exists in EVERY tier with no manual secrets; no template-time `lookup`/rand* left
for generated secrets; generated Secrets are not rendered by Helm; RBAC names are exact and minimal;
the image is digest-pinned; the spec ConfigMap carries no explicit value (the hook-only inputs Secret
does); externalSecret opt-outs become skips; the real rendered spec drives the script end to end
against a fake kubectl with second-run-unchanged.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from test_provision_secrets_script import Env  # type: ignore[import-not-found]

_CHART = Path(__file__).resolve().parents[1]
_ENVS = ["alpha", "beta", "gamma", "production", "dal2alpha", "local"]

pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="helm CLI not available")


def _render(env: str, sets: list[str] | None = None) -> list[dict[str, Any]]:
    args = ["helm", "template", "waddlebot", str(_CHART), "--kube-version", "1.30.0", "-f", str(_CHART / f"values-{env}.yaml")]
    for kv in sets or []:
        args += ["--set", kv]
    res = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    assert res.returncode == 0, res.stderr
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs, "zero documents rendered"
    return docs


def _one(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"expected exactly one {kind}/{name}, got {len(found)}"
    return found[0]


def _spec(docs: list[dict[str, Any]]) -> str:
    return _one(docs, "ConfigMap", "waddlebot-provision-secrets")["data"]["spec.psv"]


@pytest.fixture(scope="module", params=_ENVS)
def rendered(request: pytest.FixtureRequest) -> tuple[str, list[dict[str, Any]]]:
    return request.param, _render(request.param)


def test_hook_present_in_every_tier_with_ordering(rendered: tuple[str, list[dict[str, Any]]]) -> None:
    _, docs = rendered
    job = _one(docs, "Job", "waddlebot-provision-secrets")
    ann = job["metadata"]["annotations"]
    assert ann["helm.sh/hook"] == "pre-install,pre-upgrade" and ann["helm.sh/hook-weight"] == "-8"
    for kind in ("ServiceAccount", "Role", "RoleBinding", "ConfigMap", "Secret"):
        name = "waddlebot-provision-secrets-inputs" if kind == "Secret" else "waddlebot-provision-secrets"
        assert _one(docs, kind, name)["metadata"]["annotations"]["helm.sh/hook-weight"] == "-10"
    c = job["spec"]["template"]["spec"]["containers"][0]
    assert re.search(r"@sha256:[0-9a-f]{64}$", c["image"]), c["image"]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True and c["securityContext"]["runAsNonRoot"] is True
    assert job["spec"]["template"]["spec"]["volumes"][2]["emptyDir"]["medium"] == "Memory"
    sa = _one(docs, "ServiceAccount", "waddlebot-provision-secrets")
    assert sa["automountServiceAccountToken"] is False and job["spec"]["template"]["spec"]["automountServiceAccountToken"] is True


def test_no_generated_secret_is_rendered_by_helm(rendered: tuple[str, list[dict[str, Any]]]) -> None:
    _, docs = rendered
    generated = {
        "waddlebot-secrets", "waddlebot-db-admin", "waddlebot-db-credentials", "waddlebot-db-migrate-secret",
        "waddlebot-valkey-durable-admin", "waddlebot-valkey-kv", "waddlebot-hub-admin-bootstrap", "seaweedfs-sse-kek",
        "waddlebot-tenant-kek", "waddlebot-ots-encryption-key", "waddlebot-sso-encryption-key", "waddlebot-outbound-pin",
        "waddlebot-bundle-admin-request", "waddlebot-event-sync-request", "posthog-secrets",
    }
    leaked = [d["metadata"]["name"] for d in docs if d["kind"] == "Secret" and d["metadata"]["name"] in generated]
    assert leaked == [], f"Helm still renders generated Secrets: {leaked}"


def test_rbac_exact_and_minimal(rendered: tuple[str, list[dict[str, Any]]]) -> None:
    env, docs = rendered
    role = _one(docs, "Role", "waddlebot-provision-secrets")
    create, named = role["rules"]
    assert create == {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]}
    assert named["verbs"] == ["get", "patch"] and named["resources"] == ["secrets"]
    assert len(role["rules"]) == 2
    expected = {
        "waddlebot-db-admin", "waddlebot-db-credentials", "waddlebot-secrets", "waddlebot-db-migrate-secret",
        "waddlebot-hub-admin-bootstrap", "seaweedfs-sse-kek", "waddlebot-tenant-kek", "waddlebot-ots-encryption-key",
    }
    assert expected <= set(named["resourceNames"]), expected - set(named["resourceNames"])
    assert "waddlebot-platform-credentials" not in named["resourceNames"]
    assert named["resourceNames"] == sorted(set(named["resourceNames"]))
    job_ann = _one(docs, "Job", "waddlebot-provision-secrets")["metadata"]["annotations"]["waddles.penguintech.io/provisions-secrets"]
    assert set(job_ann.split(",")) <= set(named["resourceNames"])


def test_spec_has_roots_and_marker_and_no_platform_credentials(rendered: tuple[str, list[dict[str, Any]]]) -> None:
    _, docs = rendered
    spec = _spec(docs)
    assert "marker|TENANT_KEK_HEX|root-hmac-v1" in spec
    assert "key|TENANT_KEK_HEX|gen|hex:32|hex64" in spec
    assert "key|POSTGRES_PASSWORD|gen|b64url:32" in spec and "legacy=waddlebot-secrets/POSTGRES_PASSWORD" in spec
    assert "waddlebot-platform-credentials" not in spec
    assert "REPLACE_ME" not in spec


def test_no_template_time_generation_left() -> None:
    allowed_lookup = {  # TLS/CA certificate Secrets: separate follow-up, never symmetric keys/passwords
        "_connector_runtime.tpl", "hub-api-grpc-tls-secret.yaml", "host-api-tls-secret.yaml", "transit-tls-secret.yaml",
        "valkey-tls-secret.yaml",
    }
    for p in (_CHART / "templates").rglob("*"):
        if not p.is_file():
            continue
        text = p.read_text()
        code = re.sub(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", "", text, flags=re.S)  # strip template comments
        code = "\n".join(ln for ln in code.splitlines() if not ln.lstrip().startswith("#"))
        for pat in ("randAlphaNum", "randBytes", "randAscii", "randNumeric"):
            assert pat not in code, f"{p.relative_to(_CHART)} still uses {pat}"
        if "lookup " in code or 'lookup "' in code:
            assert p.name in allowed_lookup, f"{p.relative_to(_CHART)} uses template-time lookup"


def test_explicit_values_go_to_inputs_secret_never_the_spec_cm() -> None:
    marker = "explicit-secret-value-0123456789abcdef"
    docs = _render("production", [f"global.jwtSecret={marker}", "global.moduleSecretKey=REPLACE_ME_x"])
    inputs = _one(docs, "Secret", "waddlebot-provision-secrets-inputs")["stringData"]
    assert inputs["waddlebot-secrets__JWT_SECRET"] == marker
    assert "waddlebot-secrets__MODULE_SECRET_KEY" not in inputs  # placeholder is never an explicit value
    cm = json.dumps(_one(docs, "ConfigMap", "waddlebot-provision-secrets")["data"])
    job = json.dumps(_one(docs, "Job", "waddlebot-provision-secrets"))
    assert marker not in cm and marker not in job


def test_external_secret_opt_out_is_a_skip_not_a_generation() -> None:
    docs = _render("production", ["autoProvisionedKeys.tenantKek.externalSecret=true"])
    spec = _spec(docs)
    assert "secret|waddlebot-tenant-kek|skip=autoProvisionedKeys.tenantKek.externalSecret" in spec
    assert "marker|TENANT_KEK_HEX" not in spec and "key|TENANT_KEK_HEX|gen" not in spec
    docs = _render("production", ["infrastructure.postgresql.admin.existingSecret=my-admin"])
    assert "secret|my-admin|skip=" in _spec(docs)


def test_keys_job_has_no_tier_gate_and_no_argv_secrets() -> None:
    text = (_CHART / "templates" / "auto-provisioned-keys-job.yaml").read_text()
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "can_generate" not in code and "DEPLOYMENT_TIER" not in code
    assert "AlreadyExists" in code and "--from-file=\"${gen_priv_key}" in code
    assert not re.search(r"--from-literal=\"\$\{gen_priv_key", code)
    for env in ("beta", "production"):
        docs = _render(env)
        job = _one(docs, "Job", "waddlebot-auto-provision-keys")
        assert "alpha|local" not in json.dumps(job)


@pytest.mark.parametrize("env", ["alpha", "production"])
def test_rendered_spec_drives_script_end_to_end_second_run_unchanged(env: str, tmp_path: Path) -> None:
    docs = _render(env)
    e = Env(tmp_path)
    spec = _spec(docs)
    # a pre-existing legacy cluster state: owner-supplied credentials Secret must survive untouched
    e.put("waddlebot-platform-credentials", {"DISCORD_BOT_TOKEN": "owner-token"})
    r1 = e.run(spec, {"LABEL_RELEASE": "waddlebot"})
    assert r1.returncode == 0, r1.stderr
    names = [ln.split("|")[1] for ln in spec.splitlines() if ln.startswith("secret|") and "skip=" not in ln]
    for n in names:
        assert e.get(n) is not None, n
    snapshot = {p.name: p.read_text() for p in e.state.glob("*.json")}
    (e.state / "_calls.log").write_text("")
    r2 = e.run(spec, {"LABEL_RELEASE": "waddlebot"})
    assert r2.returncode == 0, r2.stderr
    assert e.mutating() == [], "second run must not create or patch anything"
    assert snapshot == {p.name: p.read_text() for p in e.state.glob("*.json")}
    assert e.val("waddlebot-platform-credentials", "DISCORD_BOT_TOKEN") == "owner-token"
    # db-migrate secret composed from the generated owner password
    pw = e.val("waddlebot-db-admin", "POSTGRES_PASSWORD")
    assert pw in e.val("waddlebot-db-migrate-secret", "DATABASE_URL")
    roles = json.loads(e.val("waddlebot-db-migrate-secret", "WADDLES_DB_SERVICE_ROLE_PASSWORDS"))
    assert roles and all(len(v) == 43 for v in roles.values())
    assert e.val("waddlebot-secrets", "SECRET_KEY") == e.val("waddlebot-secrets", "JWT_SECRET")
    assert f":{e.val('waddlebot-secrets', 'REDIS_PASSWORD')}@" in e.val("waddlebot-secrets", "REDIS_URL")
    base64.b64decode(e.get("waddlebot-tenant-kek")["data"]["TENANT_KEK_HEX"])
    assert "waddles.penguintech.io/root-marker" in e.get("waddlebot-tenant-kek")["annotations"]
