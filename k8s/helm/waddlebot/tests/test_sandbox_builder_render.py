"""Render + Dockerfile assertions for the marketplace sandbox builder (Task 7B).

Proves the standing lockdown objects render (deny-all-egress CNP, Tetragon allowlist, no-token SA,
restricted-compliant selftest Job) and that the builder image/entrypoint are offline by
construction. A missing helm CLI or zero rendered documents FAILS (never a silent pass).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART = Path(__file__).resolve().parent.parent
_REPO = _CHART.parent.parent.parent
_TPL = "templates/marketplace-sandbox-builder.yaml"


def _render(*extra: str, template: str = _TPL) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        pytest.fail("helm CLI not found -- cannot verify the sandbox builder render")
    cmd = [
        "helm", "template", "waddlebot", str(_CHART), "--kube-version", "1.30.0",
        "--values", str(_CHART / "values-beta.yaml"), "--set", "global.deploymentTier=alpha",
        "-s", template, *extra,
    ]  # fmt: skip
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        return []
    return [d for d in yaml.safe_load_all(res.stdout) if d]


def _by_kind(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {d["kind"]: d for d in docs}


def test_disabled_by_default_renders_nothing() -> None:
    assert _render() == []  # `-s` of an empty template errors -> no documents


def test_enabled_renders_full_lockdown() -> None:
    docs = _render("--set", "sandboxBuilder.enabled=true")
    kinds = _by_kind(docs)
    assert len(docs) == 8, [d["kind"] for d in docs]
    assert kinds["ServiceAccount"]["automountServiceAccountToken"] is False
    cnp = kinds["CiliumNetworkPolicy"]["spec"]
    assert cnp["egress"] == [{}] and cnp["ingress"] == [
        {}
    ]  # default-deny, no allow rule
    assert (
        cnp["endpointSelector"]["matchLabels"]["app.kubernetes.io/component"]
        == "sandbox-builder"
    )
    trace = kinds["TracingPolicyNamespaced"]["spec"]
    sel = trace["kprobes"][0]["selectors"][0]
    assert sel["matchActions"] == [{"action": "Sigkill"}]
    paths = next(a for a in sel["matchArgs"] if a["operator"] == "NotEqual")["values"]
    assert "/usr/local/bin/sandbox-build" in paths
    assert not {"/bin/sh", "/usr/bin/curl", "/usr/bin/wget", "/bin/bash"} & set(paths)
    role = kinds["Role"]["rules"]
    assert all("secrets" not in r["resources"] for r in role)  # no secret access
    job = kinds["Job"]
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "test"
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    c = pod["containers"][0]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert job["spec"]["activeDeadlineSeconds"] == 600
    assert "ephemeral-storage" in c["resources"]["limits"]
    assert "env" not in c and "envFrom" not in c  # no credentials


def test_hub_api_gets_builder_env_only_when_enabled() -> None:
    on = _render(
        "--set", "sandboxBuilder.enabled=true", template="templates/hub-api.yaml"
    )
    dep = next(d for d in on if d["kind"] == "Deployment")
    env = {e["name"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert {
        "SANDBOX_BUILDER_IMAGE",
        "SANDBOX_BUILDER_NAMESPACE",
        "SANDBOX_BUILDER_INSTANCE",
    } <= env
    off = _render(template="templates/hub-api.yaml")
    dep = next(d for d in off if d["kind"] == "Deployment")
    env = {e["name"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert not any(n.startswith("SANDBOX_BUILDER_") for n in env)


def test_dockerfile_is_offline_rootless_and_pinned() -> None:
    text = (_REPO / "bundles" / "Dockerfile.sandbox-builder").read_text()
    assert "CARGO_NET_OFFLINE=true" in text
    assert "USER builder" in text and "USER root" not in text
    assert text.count("@sha256:") >= 2  # digest-pinned bases
    final = text.split("# Stage 2")[1]
    assert "cargo install" not in final and "rustup toolchain install" not in final


def test_entrypoint_forces_cargo_offline_and_injects_vendor_source() -> None:
    src = (_REPO / "bundles" / "sandbox_build.py").read_text()
    for needle in (
        '"--offline"',
        '"--locked"',
        "rust_deps_not_vendored",
        "replace-with",
        'CARGO_NET_OFFLINE": "true"',
    ):
        assert needle in src, needle


def test_launcher_bound_only_to_dedicated_hub_api_sa_and_admission_policy() -> None:
    """Launcher Role must not be bound to the shared SA; VAP confines hub-api's Job-create."""
    docs = _render("--set", "sandboxBuilder.enabled=true")
    kinds = _by_kind(docs)
    subj = kinds["RoleBinding"]["subjects"]
    assert [s["name"] for s in subj] == ["waddlebot-hub-api"], subj
    shared = _render(template="templates/serviceaccount.yaml")
    shared_name = shared[0]["metadata"]["name"]
    assert shared_name != "waddlebot-hub-api" and shared_name not in [
        s["name"] for s in subj
    ]
    vap = kinds["ValidatingAdmissionPolicy"]["spec"]
    exprs = " ".join(v["expression"] for v in vap["validations"])
    for needle in (
        "sandbox-builder",
        "automountServiceAccountToken",
        "configMap",
        "emptyDir",
        "c.image ==",
    ):
        assert needle in exprs, needle
    assert "system:serviceaccount:" in vap["matchConditions"][0]["expression"]
    assert kinds["ValidatingAdmissionPolicyBinding"]["spec"]["validationActions"] == [
        "Deny"
    ]


def test_hub_api_uses_dedicated_sa_with_tokenreview() -> None:
    dep = next(
        d
        for d in _render(template="templates/hub-api.yaml")
        if d["kind"] == "Deployment"
    )
    assert dep["spec"]["template"]["spec"]["serviceAccountName"] == "waddlebot-hub-api"
    rbac = _render(template="templates/service-jwt-rbac.yaml")
    assert any(
        d["kind"] == "ServiceAccount" and d["metadata"]["name"] == "waddlebot-hub-api"
        for d in rbac
    )
    crb = next(d for d in rbac if d["kind"] == "ClusterRoleBinding")
    assert [s["name"] for s in crb["subjects"]] == ["waddlebot-hub-api"]
