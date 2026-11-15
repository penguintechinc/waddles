"""Helm-template assertions for svc-presentation's in-cluster surface gRPC listener
(waddles.presentation.internal.v1.SurfaceService, port `pipeline.svcPresentation.grpcPort`).

Proves, against the rendered chart (alpha values), on every Helm binary available (helm3 AND helm4):

1. svc-presentation serves gRPC on 50051 with a runtime-provisioned ECDSA P-256 server cert whose
   SANs cover the Service DNS names, mounted from `<fullname>-presentation-grpc-tls`.
2. The Service exposes the port, and the generic workload policy does NOT open it.
3. Exactly ONE ingress rule reaches 50051: from svc-action-rust / svc-process-rust, that port only.
4. svc-action-rust and svc-process-rust get PRESENTATION_GRPC_ENDPOINT (https, the Service DNS name)
   plus a CA-only mount (never the server key), and each has a narrow egress allow to that port.
5. The caller scope is wired (`presentation.overlay:push` stays granted to svc-action).

Zero rendered documents / zero matching objects is a hard failure, never a silent pass.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography import x509
from provision_harness import with_provisioned  # type: ignore[import-not-found]

_CHART_DIR = Path(__file__).resolve().parents[1]
_VALUES = {
    "alpha": _CHART_DIR / "values-alpha.yaml",
    "beta": _CHART_DIR / "values-beta.yaml",
    "gamma": _CHART_DIR / "values-gamma.yaml",
    "production": _CHART_DIR / "values-production.yaml",
}
_SECRET_SUFFIX = "-presentation-grpc-tls"
_GRPC_PORT = "50051"
_CALLERS = ("svc-action-rust", "svc-process-rust")
_HELM_BINARIES = [b for b in ("helm3", "helm4") if shutil.which(b)] or (
    ["helm"] if shutil.which("helm") else []
)


def _render_raw(binary: str, tier: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            binary, "template", "waddlebot", str(_CHART_DIR),
            "--kube-version", "1.30.0",
            "--values", str(_VALUES[tier]),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"{binary} template failed:\n{result.stdout}\n{result.stderr}")
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert docs, f"{binary} template produced zero documents -- cannot be a pass"
    return docs


def _render(binary: str, tier: str) -> list[dict[str, Any]]:
    return with_provisioned(_render_raw(binary, tier))


@pytest.fixture(
    scope="module",
    params=[
        (b, t)
        for b in (_HELM_BINARIES or ["none"])
        for t in _VALUES
    ],
    ids=lambda p: f"{p[0]}-{p[1]}",
)
def docs(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    binary, tier = request.param
    if binary == "none":
        pytest.skip("no helm CLI")
    return _render(binary, tier)


def _by_kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [d for d in docs if d.get("kind") == kind]


def _deployment(docs: list[dict[str, Any]], suffix: str) -> dict[str, Any]:
    matches = [d for d in _by_kind(docs, "Deployment") if d["metadata"]["name"].endswith(suffix)]
    assert len(matches) == 1, f"expected exactly one Deployment ending {suffix!r}, got {len(matches)}"
    return matches[0]


def _container(dep: dict[str, Any]) -> dict[str, Any]:
    containers = dep["spec"]["template"]["spec"]["containers"]
    assert containers, "Deployment has no containers"
    return containers[0]


def _env(container: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in container.get("env", []) if "value" in e}


def _cnps(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = _by_kind(docs, "CiliumNetworkPolicy")
    assert out, "no CiliumNetworkPolicy rendered"
    return out


def _component(cnp: dict[str, Any]) -> str:
    sel = cnp["spec"].get("endpointSelector") or {}
    return (sel.get("matchLabels") or {}).get("app.kubernetes.io/component", "")


def _selects(cnp: dict[str, Any], component: str) -> bool:
    """True when the policy's endpointSelector can match `component` (labels or In-expression)."""
    sel = cnp["spec"].get("endpointSelector") or {}
    if _component(cnp) == component:
        return True
    for expr in sel.get("matchExpressions") or []:
        if expr.get("key") == "app.kubernetes.io/component" and component in expr.get("values", []):
            return True
    return False


def _ports_of(rule: dict[str, Any]) -> set[str]:
    return {p["port"] for tp in rule.get("toPorts", []) for p in tp.get("ports", [])}


def _allowed_components(rule: dict[str, Any]) -> set[str]:
    comps: set[str] = set()
    for ep in rule.get("fromEndpoints", []):
        for expr in ep.get("matchExpressions", []):
            if expr.get("key") == "app.kubernetes.io/component":
                comps.update(expr.get("values", []))
        comp = (ep.get("matchLabels") or {}).get("app.kubernetes.io/component")
        if comp:
            comps.add(comp)
    return comps


class TestServer:
    def test_listens_with_tls_files_from_the_provisioned_secret(self, docs: list[dict[str, Any]]) -> None:
        dep = _deployment(docs, "-svc-presentation")
        c = _container(dep)
        env = _env(c)
        assert env["SURFACE_GRPC_PORT"] == _GRPC_PORT
        cert, key = env["SURFACE_GRPC_TLS_CERT_FILE"], env["SURFACE_GRPC_TLS_KEY_FILE"]
        mounts = {m["mountPath"] for m in c["volumeMounts"]}
        assert any(cert.startswith(m) for m in mounts) and any(key.startswith(m) for m in mounts)
        assert {"containerPort": int(_GRPC_PORT), "name": "grpc", "protocol": "TCP"} in c["ports"]
        vols = dep["spec"]["template"]["spec"]["volumes"]
        assert any(
            (v.get("secret") or {}).get("secretName", "").endswith(_SECRET_SUFFIX) for v in vols
        ), "no pod volume sources the presentation gRPC TLS Secret"

    def test_cert_is_ecdsa_p256_with_service_sans(self, docs: list[dict[str, Any]]) -> None:
        secrets = [
            d for d in _by_kind(docs, "Secret") if d["metadata"]["name"].endswith(_SECRET_SUFFIX)
        ]
        assert secrets, "no *-presentation-grpc-tls Secret provisioned"
        data = secrets[0].get("stringData") or {}
        assert {"ca.crt", "tls.crt", "tls.key"} <= set(data), sorted(data)
        cert = x509.load_pem_x509_certificate(data["tls.crt"].encode())
        pub = cert.public_key()
        from cryptography.hazmat.primitives.asymmetric import ec

        assert isinstance(pub, ec.EllipticCurvePublicKey) and isinstance(pub.curve, ec.SECP256R1)
        sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        dns = set(sans.get_values_for_type(x509.DNSName))
        svc = next(
            d["metadata"]["name"]
            for d in _by_kind(docs, "Service")
            if d["metadata"]["name"].endswith("-svc-presentation")
        )
        assert {svc, f"{svc}.waddlebot", f"{svc}.waddlebot.svc"} <= dns, sorted(dns)

    def test_service_exposes_the_grpc_port(self, docs: list[dict[str, Any]]) -> None:
        svc = next(
            d for d in _by_kind(docs, "Service") if d["metadata"]["name"].endswith("-svc-presentation")
        )
        ports = {p["name"]: p for p in svc["spec"]["ports"]}
        assert ports["grpc"]["port"] == int(_GRPC_PORT) and ports["grpc"]["targetPort"] == "grpc"


class TestNetworkPolicy:
    def test_only_the_dedicated_policy_opens_the_grpc_port_and_only_to_the_stages(
        self, docs: list[dict[str, Any]]
    ) -> None:
        openers: list[tuple[str, dict[str, Any]]] = []
        for cnp in _cnps(docs):
            if not _selects(cnp, "svc-presentation"):
                continue
            for rule in cnp["spec"].get("ingress", []) or []:
                if _GRPC_PORT in _ports_of(rule):
                    openers.append((cnp["metadata"]["name"], rule))
        assert len(openers) == 1, f"expected exactly one ingress rule on {_GRPC_PORT}, got {openers}"
        name, rule = openers[0]
        assert name.endswith("-svc-presentation-grpc"), name
        assert _ports_of(rule) == {_GRPC_PORT}, "the dedicated rule must open that one port only"
        assert _allowed_components(rule) == set(_CALLERS), _allowed_components(rule)
        # No wildcard / whole-namespace source on the grpc rule.
        for ep in rule["fromEndpoints"]:
            assert "io.kubernetes.pod.namespace" not in (ep.get("matchLabels") or {})

    @pytest.mark.parametrize("caller", _CALLERS)
    def test_each_caller_has_a_narrow_egress_allow_to_that_port(
        self, docs: list[dict[str, Any]], caller: str
    ) -> None:
        matches = [
            c for c in _cnps(docs) if c["metadata"]["name"].endswith(f"-{caller}-presentation-grpc-egress")
        ]
        assert len(matches) == 1, f"{caller}: expected one egress policy, got {len(matches)}"
        cnp = matches[0]
        assert _component(cnp) == caller
        (rule,) = cnp["spec"]["egress"]
        assert _ports_of(rule) == {_GRPC_PORT}
        (target,) = rule["toEndpoints"]
        assert target["matchLabels"]["app.kubernetes.io/component"] == "svc-presentation"


class TestCallers:
    @pytest.mark.parametrize("caller", _CALLERS)
    def test_endpoint_is_https_service_dns_and_ca_is_mounted_without_the_key(
        self, docs: list[dict[str, Any]], caller: str
    ) -> None:
        dep = _deployment(docs, caller)
        c = _container(dep)
        env = _env(c)
        endpoint = env["PRESENTATION_GRPC_ENDPOINT"]
        assert endpoint.startswith("https://") and endpoint.endswith(f"-svc-presentation:{_GRPC_PORT}"), endpoint
        ca_file = env["PRESENTATION_GRPC_CA_FILE"]
        assert ca_file.endswith("/ca.crt")
        assert any(ca_file.startswith(m["mountPath"]) for m in c["volumeMounts"])
        vols = [
            v
            for v in dep["spec"]["template"]["spec"]["volumes"]
            if (v.get("secret") or {}).get("secretName", "").endswith(_SECRET_SUFFIX)
        ]
        assert len(vols) == 1
        items = vols[0]["secret"]["items"]
        assert [i["key"] for i in items] == ["ca.crt"], "callers must mount the CA only, never tls.key"

    def test_action_keeps_the_push_scope(self, docs: list[dict[str, Any]]) -> None:
        rendered = yaml.safe_dump_all(docs)
        assert "presentation.overlay:push" in rendered


@pytest.mark.parametrize("tier", list(_VALUES))
def test_secret_is_generated_in_every_tier_and_never_overwritten(tier: str) -> None:
    """Owner policy: internal identities are create-if-absent in EVERY tier, never regenerated."""
    if not _HELM_BINARIES:
        pytest.skip("no helm CLI")
    raw = _render_raw(_HELM_BINARIES[0], tier)
    first = with_provisioned(raw)
    secrets = [d for d in first if d.get("x-provisioned") and d["metadata"]["name"].endswith(_SECRET_SUFFIX)]
    assert len(secrets) == 1, f"{tier}: presentation gRPC Secret must be generated, got {len(secrets)}"
    generated = dict(secrets[0]["stringData"])
    assert {"ca.crt", "tls.crt", "tls.key"} <= set(generated)
    # Second run with the Secret already present: every key is kept verbatim.
    name = secrets[0]["metadata"]["name"]
    again = with_provisioned(
        raw,
        existing={name: generated},
        existing_annotations={name: dict(secrets[0]["metadata"]["annotations"])},
    )
    kept = [d for d in again if d.get("x-provisioned") and d["metadata"]["name"].endswith(_SECRET_SUFFIX)]
    assert kept and all(kept[0]["stringData"][k] == generated[k] for k in ("ca.crt", "tls.crt", "tls.key")), (
        f"{tier}: an existing presentation gRPC Secret was overwritten"
    )


@pytest.mark.parametrize("tier", ["beta", "gamma", "production"])
def test_no_tier_forces_an_external_secret(tier: str) -> None:
    text = _VALUES[tier].read_text()
    assert "presentationGrpcTls" not in text, f"values-{tier}.yaml must not override presentationGrpcTls"
