"""Helm-template assertions for the enterprise SSO key wiring (templates/sso.yaml, _sso.tpl).

Two layers:

1. `TestFullChart` renders the REAL chart (alpha values) and checks the Secret is generated
   in alpha, is a 64-lowercase-hex key, survives `helm.sh/resource-policy: keep`, and is
   wired into hub-api's Deployment through the one-line include.
2. `TestTierPolicyIsolated` renders only the SSO templates (the real `_helpers.tpl`,
   `sso.yaml` and `_sso.tpl`, copied into a scratch chart) under every tier. The full chart
   cannot be rendered in beta/gamma/production without dozens of unrelated required values,
   but the SSO policy -- "generate in alpha/local, NEVER fail the release elsewhere" -- is
   entirely inside these three files, so isolating them tests exactly the code under test.
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
        return _docs(result.stdout)

    def test_alpha_generates_a_64_hex_char_key_secret(self, alpha_docs: list[dict[str, Any]]) -> None:
        found = _secrets(alpha_docs)
        assert len(found) == 1
        value = base64.b64decode(found[0]["data"]["SSO_ENCRYPTION_KEY"]).decode()
        assert re.fullmatch(r"[0-9a-f]{64}", value), "must be lowercase hex (bytes.fromhex)"

    def test_secret_survives_uninstall_and_is_labelled_as_an_auto_provisioned_key(
        self, alpha_docs: list[dict[str, Any]]
    ) -> None:
        meta = _secrets(alpha_docs)[0]["metadata"]
        assert meta["annotations"]["helm.sh/resource-policy"] == "keep"
        assert meta["labels"]["app.kubernetes.io/component"] == "auto-provisioned-key"

    def test_two_renders_mint_independent_keys(self) -> None:
        keys = set()
        for _ in range(2):
            out = _helm(["template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
                         "--values", str(_ALPHA_VALUES), "--show-only", "templates/sso.yaml"])  # fmt: skip
            assert out.returncode == 0, out.stderr
            keys.add(_secrets(_docs(out.stdout))[0]["data"]["SSO_ENCRYPTION_KEY"])
        assert len(keys) == 2  # randBytes, not a constant

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
        secret_value = base64.b64decode(_secrets(alpha_docs)[0]["data"]["SSO_ENCRYPTION_KEY"]).decode()
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
        deployment = _hub_api(docs)
        names = {e["name"] for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert not any(n.startswith("SSO_") for n in names)


@pytest.fixture(scope="module")
def sso_only_chart(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Scratch chart = real `_helpers.tpl` + `sso.yaml` + `_sso.tpl` + a probe exposing the env block."""
    chart = tmp_path_factory.mktemp("sso-chart")
    (chart / "templates").mkdir()
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: waddlebot\nversion: 0.0.1\n")
    shutil.copy(_CHART_DIR / "values.yaml", chart / "values.yaml")
    for name in ("_helpers.tpl", "sso.yaml", "_sso.tpl"):
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


def _probe_env(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    probe = next(d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "sso-env-probe")
    return list(yaml.safe_load(probe["data"]["env"]) or [])


class TestTierPolicyIsolated:
    @pytest.mark.parametrize("tier", ["alpha", "local"])
    def test_generated_in_alpha_and_local(self, sso_only_chart: Path, tier: str) -> None:
        docs = _render(sso_only_chart, f"global.deploymentTier={tier}")
        assert len(_secrets(docs)) == 1

    @pytest.mark.parametrize("tier", ["beta", "gamma", "production"])
    def test_never_fails_the_release_and_generates_nothing_outside_alpha_local(
        self, sso_only_chart: Path, tier: str
    ) -> None:
        # The deliberate difference from autoProvisionedKeys.*: SSO is entitlement-gated,
        # so a missing key must not stop beta/gamma/production installing.
        docs = _render(sso_only_chart, f"global.deploymentTier={tier}")
        assert _secrets(docs) == []
        env = {e["name"]: e for e in _probe_env(docs)}
        assert env["SSO_ENCRYPTION_KEY"]["valueFrom"]["secretKeyRef"]["optional"] is True

    def test_external_secret_flag_skips_generation_even_in_alpha(self, sso_only_chart: Path) -> None:
        docs = _render(
            sso_only_chart, "global.deploymentTier=alpha", "sso.encryptionKey.externalSecret=true"
        )
        assert _secrets(docs) == []

    def test_custom_secret_name_and_key_are_honoured(self, sso_only_chart: Path) -> None:
        docs = _render(
            sso_only_chart,
            "global.deploymentTier=alpha",
            "sso.encryptionKey.secretName=my-sso",
            "sso.encryptionKey.secretKey=KEY",
        )
        secret = next(d for d in docs if d.get("kind") == "Secret")
        assert secret["metadata"]["name"] == "my-sso"
        assert "KEY" in secret["data"]
        ref = {e["name"]: e for e in _probe_env(docs)}["SSO_ENCRYPTION_KEY"]["valueFrom"]["secretKeyRef"]
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
