"""Helm-template assertions for the enterprise SSO key wiring (provision-secrets hook, _sso.tpl).

The SSO key Secret is created at RUNTIME, create-if-absent, in every tier by the provision-secrets
hook (templates/provision-secrets.yaml) -- never by template-time `lookup`. Two layers:

1. `TestFullChart` renders the REAL chart (alpha values), runs the real hook against the rendered
   spec (tests/provision_harness.py) and checks the Secret is a 64-lowercase-hex key, carries
   `helm.sh/resource-policy: keep`, and is wired into hub-api's Deployment (optional ref).
2. `TestTierPolicy` renders the full chart in every tier (now possible: no manual secrets) and
   checks the key is provisioned everywhere SSO is enabled, opt-outs are skips, and the env block
   (`_sso.tpl`, via a scratch chart probe) honours operator settings.
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from provision_harness import materialize, spec_of, with_provisioned  # type: ignore[import-not-found]

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"
_SECRET_NAME = "waddlebot-sso-encryption-key"


def _helm(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, test-only
        ["helm", *args], capture_output=True, text=True, check=False
    )


@pytest.fixture(scope="module", autouse=True)
def _require_helm() -> None:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")


def _hub_api(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        d
        for d in docs
        if d.get("kind") == "Deployment" and "-hub-api" in d["metadata"]["name"]
    )


def _docs(stdout: str) -> list[dict[str, Any]]:
    docs = [d for d in yaml.safe_load_all(stdout) if d]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def _secrets(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in docs if d.get("kind") == "Secret" and d["metadata"]["name"] == _SECRET_NAME]


class TestFullChart:
    @pytest.fixture(scope="class")
    def alpha_docs(self) -> list[dict[str, Any]]:
        result = _helm(
            ["template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
             "--values", str(_ALPHA_VALUES)]
        )  # fmt: skip
        assert result.returncode == 0, result.stderr
        return with_provisioned(_docs(result.stdout))

    def test_alpha_generates_a_64_hex_char_key_secret(self, alpha_docs: list[dict[str, Any]]) -> None:
        found = _secrets(alpha_docs)
        assert len(found) == 1
        assert found[0].get("x-provisioned"), "must come from the runtime hook, never a Helm-rendered Secret"
        value = found[0]["stringData"]["SSO_ENCRYPTION_KEY"]
        assert re.fullmatch(r"[0-9a-f]{64}", value), "must be lowercase hex (bytes.fromhex)"

    def test_secret_survives_uninstall_and_pruning(self, alpha_docs: list[dict[str, Any]]) -> None:
        ann = _secrets(alpha_docs)[0]["metadata"]["annotations"]
        assert ann["helm.sh/resource-policy"] == "keep"
        assert ann["argocd.argoproj.io/sync-options"] == "Prune=false"

    def test_two_fresh_installs_mint_independent_keys_and_upgrades_keep_them(
        self, alpha_docs: list[dict[str, Any]]
    ) -> None:
        rendered = [d for d in alpha_docs if not d.get("x-provisioned")]
        a = materialize(rendered)[_SECRET_NAME]["data"]["SSO_ENCRYPTION_KEY"]
        b = materialize(rendered)[_SECRET_NAME]["data"]["SSO_ENCRYPTION_KEY"]
        assert a != b  # CSPRNG, not a constant
        kept = materialize(rendered, {_SECRET_NAME: {"SSO_ENCRYPTION_KEY": a}})
        assert kept[_SECRET_NAME]["data"]["SSO_ENCRYPTION_KEY"] == a  # never rotated by an upgrade

    def test_hub_api_receives_the_key_optionally_and_the_tuning_env(self, alpha_docs: list[dict[str, Any]]) -> None:
        deployment = _hub_api(alpha_docs)
        env = {e["name"]: e for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        key_ref = env["SSO_ENCRYPTION_KEY"]["valueFrom"]["secretKeyRef"]
        assert key_ref["name"] == _SECRET_NAME
        assert key_ref["key"] == "SSO_ENCRYPTION_KEY"
        assert key_ref["optional"] is True  # entitlement-gated: absence must not block the pod
        assert env["SSO_STATE_TTL_SECONDS"]["value"] == "600"
        assert env["SSO_CLOCK_SKEW_SECONDS"]["value"] == "120"
        assert "SSO_ALLOWED_PRIVATE_HOSTS" not in env  # empty allowlist -> nothing injected
        assert "SSO_GOOGLE_CLIENT_ID" not in env

    def test_key_is_never_inlined_as_a_literal_anywhere(self, alpha_docs: list[dict[str, Any]]) -> None:
        secret_value = _secrets(alpha_docs)[0]["stringData"]["SSO_ENCRYPTION_KEY"]
        deployment_text = yaml.safe_dump(
            [d for d in alpha_docs if d.get("kind") in {"Deployment", "ConfigMap", "Job"}]
        )
        assert secret_value not in deployment_text

    def test_existing_alpha_render_still_succeeds_with_sso_disabled(self) -> None:
        out = _helm(["template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
                     "--values", str(_ALPHA_VALUES), "--set", "sso.enabled=false"])  # fmt: skip
        assert out.returncode == 0, out.stderr
        docs = _docs(out.stdout)
        assert _secrets(docs) == []
        assert _SECRET_NAME not in spec_of(docs)
        deployment = _hub_api(docs)
        names = {e["name"] for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert not any(n.startswith("SSO_") for n in names)


@pytest.fixture(scope="module")
def sso_only_chart(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Scratch chart = real `_helpers.tpl` + `_sso.tpl` + a probe exposing the hub-api env block."""
    chart = tmp_path_factory.mktemp("sso-chart")
    (chart / "templates").mkdir()
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: waddlebot\nversion: 0.0.1\n")
    shutil.copy(_CHART_DIR / "values.yaml", chart / "values.yaml")
    for name in ("_helpers.tpl", "_sso.tpl"):
        shutil.copy(_CHART_DIR / "templates" / name, chart / "templates" / name)
    (chart / "templates" / "probe.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: sso-env-probe\ndata:\n"
        '  env: |\n{{ include "waddlebot.sso.hubApiEnv" . | indent 4 }}\n'
    )
    return chart


def _render(chart: Path, *sets: str) -> list[dict[str, Any]]:
    args = ["template", "waddlebot", str(chart), "--kube-version", "1.30.0"]
    for s in sets:
        args += ["--set", s]
    result = _helm(args)
    assert result.returncode == 0, f"render must not fail: {result.stderr}"
    return _docs(result.stdout)


def _full(values: str, *sets: str) -> list[dict[str, Any]]:
    args = ["template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0", "--values", str(_CHART_DIR / values)]
    for s in sets:
        args += ["--set", s]
    result = _helm(args)
    assert result.returncode == 0, f"render must not fail: {result.stderr}"
    return _docs(result.stdout)


def _probe_env(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    probe = next(d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "sso-env-probe")
    return list(yaml.safe_load(probe["data"]["env"]) or [])


class TestTierPolicy:
    @pytest.mark.parametrize("values", ["values-alpha.yaml", "values-local.yaml", "values-beta.yaml",
                                        "values-gamma.yaml", "values-production.yaml"])  # fmt: skip
    def test_provisioned_in_every_tier_and_never_fails_the_release(self, values: str) -> None:
        # Owner policy (2026-11-14): generated on first run in EVERY tier (was alpha/local only).
        # hub-api's ref stays optional: SSO is entitlement-gated and an externally owned key may lag.
        docs = _full(values, "sso.enabled=true")
        assert f"secret|{_SECRET_NAME}|\nkey|SSO_ENCRYPTION_KEY|gen|hex:32|hex64" in spec_of(docs)
        assert _secrets(docs) == [], "never rendered by Helm"
        env = {e["name"]: e for e in _hub_api(docs)["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["SSO_ENCRYPTION_KEY"]["valueFrom"]["secretKeyRef"]["optional"] is True

    def test_external_secret_flag_skips_generation(self) -> None:
        docs = _full("values-alpha.yaml", "sso.enabled=true", "sso.encryptionKey.externalSecret=true")
        spec = spec_of(docs)
        assert f"secret|{_SECRET_NAME}|skip=sso.encryptionKey.externalSecret" in spec
        assert "key|SSO_ENCRYPTION_KEY" not in spec
        assert _SECRET_NAME not in materialize(docs)

    def test_custom_secret_name_and_key_are_honoured(self, sso_only_chart: Path) -> None:
        docs = _full("values-alpha.yaml", "sso.enabled=true", "sso.encryptionKey.secretName=my-sso",
                     "sso.encryptionKey.secretKey=KEY")  # fmt: skip
        out = materialize(docs)
        assert re.fullmatch(r"[0-9a-f]{64}", out["my-sso"]["data"]["KEY"])
        probe = _render(sso_only_chart, "sso.encryptionKey.secretName=my-sso", "sso.encryptionKey.secretKey=KEY")
        ref = {e["name"]: e for e in _probe_env(probe)}["SSO_ENCRYPTION_KEY"]["valueFrom"]["secretKeyRef"]
        assert (ref["name"], ref["key"]) == ("my-sso", "KEY")

    def test_disabled_renders_nothing(self, sso_only_chart: Path) -> None:
        docs = _render(sso_only_chart, "global.deploymentTier=alpha", "sso.enabled=false")
        assert _secrets(docs) == []
        assert _probe_env(docs) == []

    def test_operator_settings_flow_into_env(self, sso_only_chart: Path) -> None:
        docs = _render(
            sso_only_chart,
            "global.deploymentTier=alpha",
            "sso.allowedPrivateHosts={keycloak.corp.test,adfs.corp.test}",
            "sso.stateTtlSeconds=300",
            "sso.clockSkewSeconds=30",
        )
        env = {e["name"]: e for e in _probe_env(docs)}
        assert env["SSO_ALLOWED_PRIVATE_HOSTS"]["value"] == "keycloak.corp.test,adfs.corp.test"
        assert env["SSO_STATE_TTL_SECONDS"]["value"] == "300"
        assert env["SSO_CLOCK_SKEW_SECONDS"]["value"] == "30"

    def test_shared_google_client_comes_from_an_existing_secret_never_a_literal(
        self, sso_only_chart: Path
    ) -> None:
        docs = _render(
            sso_only_chart,
            "global.deploymentTier=alpha",
            "sso.google.existingSecret=google-oauth",
        )
        env = {e["name"]: e for e in _probe_env(docs)}
        for name, key in (("SSO_GOOGLE_CLIENT_ID", "SSO_GOOGLE_CLIENT_ID"),
                          ("SSO_GOOGLE_CLIENT_SECRET", "SSO_GOOGLE_CLIENT_SECRET")):  # fmt: skip
            assert "value" not in env[name]
            ref = env[name]["valueFrom"]["secretKeyRef"]
            assert ref["name"] == "google-oauth"
            assert ref["key"] == key
            assert "optional" not in ref  # naming a Secret is a promise it exists

    def test_the_default_values_never_name_a_google_secret(self) -> None:
        values = yaml.safe_load((_CHART_DIR / "values.yaml").read_text())
        assert values["sso"]["google"]["existingSecret"] == ""
        assert values["sso"]["encryptionKey"]["externalSecret"] is False
