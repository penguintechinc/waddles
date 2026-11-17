"""Every leaf the chart asks cert-manager for must validate against its CA's X.509 nameConstraints.

Regression (alpha 2026-11-16): the host-api-tls leaf gained a bare `<fullname>-svc-streaming` DNS
SAN that the name-constrained internal CA (k8s/dal2alpha/internal-ca.yaml) does not permit.
rustls-webpki checks EVERY SAN against the CA's permitted subtrees (not only the dialled name), so
svc-bundles-executor rejected the svc-process-rust / svc-action-rust host-API certs with
`invalid peer certificate: Other(OtherError(NameConstraintViolation))`, crashlooped, and blocked
`helm --wait` on every alpha deploy.

The check is cryptographic, not a string compare: for each rendered cert-manager Certificate a real
P-256 CA carrying the issuer's nameConstraints is minted with OpenSSL, the leaf is signed with the
rendered CN/dnsNames/uris/ipAddresses, and `openssl verify` (which enforces nameConstraints) must
accept it. The issuer's constraints come from the source of truth: the internal CA manifest for the
`waddlebot-internal-ca` ClusterIssuer, the rendered CA Certificate for chart-owned CA Issuers.
Zero leaves examined, or a leaf whose issuer constraints are unknown, is a FAILURE.
"""

from __future__ import annotations

import ipaddress
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _CHART_DIR.parents[2]
_INTERNAL_CA_MANIFEST = _REPO_ROOT / "k8s" / "dal2alpha" / "internal-ca.yaml"
_INTERNAL_CA_ISSUER = ("ClusterIssuer", "waddlebot-internal-ca")

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None or shutil.which("openssl") is None,
    reason="helm and openssl required",
)

# Every overlay set a deploy actually uses (alpha deploys layer values-dal2alpha over values-alpha).
# values-alpha / values-local alone run without cert-manager (runtime generator, own unconstrained CA).
_NO_CERT_MANAGER = {"alpha", "local"}
_ENVS: dict[str, list[str]] = {
    "alpha": ["values-alpha.yaml"],
    "dal2alpha": ["values-alpha.yaml", "values-dal2alpha.yaml"],
    "beta": ["values-beta.yaml"],
    "gamma": ["values-gamma.yaml"],
    "production": ["values-production.yaml"],
    "local": ["values-local.yaml"],
}


def _render(files: list[str]) -> list[dict[str, Any]]:
    """Render the chart with the given overlays; a failed render is a test failure."""
    args = ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0"]
    for f in files:
        args += ["--values", str(_CHART_DIR / f)]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _internal_ca_constraints() -> dict[str, Any]:
    """nameConstraints of the out-of-chart internal CA (the ClusterIssuer's signing cert)."""
    docs = [d for d in yaml.safe_load_all(_INTERNAL_CA_MANIFEST.read_text()) if d]
    cas = [d for d in docs if d["kind"] == "Certificate" and d["spec"].get("isCA")]
    assert len(cas) == 1, f"expected one CA Certificate in {_INTERNAL_CA_MANIFEST}, got {len(cas)}"
    nc = cas[0]["spec"].get("nameConstraints")
    assert nc, "internal CA must stay name-constrained (fail closed)"
    return nc


def _issuer_constraints(docs: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any] | None]:
    """Map (kind, name) of every issuer the render references to its CA's nameConstraints."""
    out: dict[tuple[str, str], dict[str, Any] | None] = {_INTERNAL_CA_ISSUER: _internal_ca_constraints()}
    ca_by_secret = {
        d["spec"]["secretName"]: d["spec"].get("nameConstraints")
        for d in docs
        if d.get("kind") == "Certificate" and d["spec"].get("isCA")
    }
    for d in docs:
        if d.get("kind") in ("Issuer", "ClusterIssuer") and "ca" in d.get("spec", {}):
            secret = d["spec"]["ca"]["secretName"]
            assert secret in ca_by_secret, f"{d['kind']}/{d['metadata']['name']}: CA secret {secret} not rendered"
            out[(d["kind"], d["metadata"]["name"])] = ca_by_secret[secret]
    return out


def _ip_mask(cidr: str) -> str:
    """cert-manager CIDR -> OpenSSL `addr/netmask` notation."""
    net = ipaddress.ip_network(cidr, strict=False)
    return f"{net.network_address}/{net.netmask}"


def _nc_section(nc: dict[str, Any]) -> list[str]:
    """OpenSSL [nc] lines for a cert-manager nameConstraints block."""
    lines: list[str] = []
    for kind in ("permitted", "excluded"):
        sub = nc.get(kind) or {}
        for i, v in enumerate(sub.get("dnsDomains") or []):
            lines.append(f"{kind};DNS.{i} = {v}")
        for i, v in enumerate(sub.get("uriDomains") or []):
            lines.append(f"{kind};URI.{i} = {v}")
        for i, v in enumerate(sub.get("ipRanges") or []):
            lines.append(f"{kind};IP.{i} = {_ip_mask(v)}")
        for i, v in enumerate(sub.get("emailAddresses") or []):
            lines.append(f"{kind};email.{i} = {v}")
    return lines


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def _verify_leaf(work: Path, nc: dict[str, Any] | None, spec: dict[str, Any]) -> str:
    """Mint CA(nc) + leaf(spec) and return '' when `openssl verify` accepts it, else the error."""
    work.mkdir(parents=True)
    ca_ext = ["basicConstraints = critical,CA:TRUE", "keyUsage = critical,keyCertSign,cRLSign"]
    nc_lines = _nc_section(nc) if nc else []
    if nc_lines:
        ca_ext.append(f"nameConstraints = {'critical,' if nc.get('critical') else ''}@nc")
    (work / "ca.cnf").write_text(
        "[req]\nprompt = no\ndistinguished_name = dn\nx509_extensions = v3_ca\n[dn]\nCN = test-ca\n"
        "[v3_ca]\n" + "\n".join(ca_ext) + "\n" + ("[nc]\n" + "\n".join(nc_lines) + "\n" if nc_lines else "")
    )
    sans = [f"DNS:{n}" for n in spec.get("dnsNames") or []]
    sans += [f"URI:{u}" for u in spec.get("uris") or []]
    sans += [f"IP:{ip}" for ip in spec.get("ipAddresses") or []]
    leaf_ext = ["basicConstraints = critical,CA:FALSE", "keyUsage = critical,digitalSignature",
                "extendedKeyUsage = serverAuth,clientAuth"]
    if sans:
        leaf_ext.append("subjectAltName = " + ",".join(sans))
    (work / "leaf.ext").write_text("\n".join(leaf_ext) + "\n")
    (work / "leaf.cnf").write_text(
        f"[req]\nprompt = no\ndistinguished_name = dn\n[dn]\nCN = {spec.get('commonName') or 'leaf'}\n"
    )
    steps = [
        ["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "ca.key"],
        ["openssl", "req", "-x509", "-new", "-sha256", "-key", "ca.key", "-days", "2",
         "-config", "ca.cnf", "-out", "ca.pem"],
        ["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "leaf.key"],
        ["openssl", "req", "-new", "-sha256", "-key", "leaf.key", "-config", "leaf.cnf", "-out", "leaf.csr"],
        ["openssl", "x509", "-req", "-sha256", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
         "-set_serial", "1", "-days", "1", "-extfile", "leaf.ext", "-out", "leaf.pem"],
    ]
    for step in steps:
        r = _run(step, work)
        assert r.returncode == 0, f"{' '.join(step)} failed: {r.stderr}"
    r = _run(["openssl", "verify", "-CAfile", "ca.pem", "leaf.pem"], work)
    return "" if r.returncode == 0 else (r.stdout + r.stderr).strip()


def test_openssl_verify_enforces_name_constraints(tmp_path: Path) -> None:
    """The oracle itself must be able to fail: an out-of-tree bare SAN is rejected."""
    nc = {"critical": True, "permitted": {"dnsDomains": ["waddlebot.svc.cluster.local", "allowed-bare"]}}
    assert _verify_leaf(tmp_path / "ok", nc, {"dnsNames": ["allowed-bare", "x.waddlebot.svc.cluster.local"]}) == ""
    err = _verify_leaf(tmp_path / "bad", nc, {"dnsNames": ["allowed-bare", "waddlebot-svc-streaming"]})
    assert "subtree" in err.lower() or "name constraint" in err.lower(), err


@pytest.mark.parametrize("env", sorted(_ENVS))
def test_every_leaf_satisfies_its_ca_name_constraints(env: str, tmp_path: Path) -> None:
    """Each rendered non-CA Certificate validates (openssl verify) under its issuer's nameConstraints."""
    docs = _render(_ENVS[env])
    issuers = _issuer_constraints(docs)
    leaves = [d for d in docs if d.get("kind") == "Certificate" and not d["spec"].get("isCA")]
    failures: list[str] = []
    for leaf in leaves:
        ref = leaf["spec"]["issuerRef"]
        key = (ref.get("kind", "Issuer"), ref["name"])
        assert key in issuers, f"{env}: Certificate/{leaf['metadata']['name']} issuer {key} has unknown constraints"
        err = _verify_leaf(tmp_path / leaf["metadata"]["name"], issuers[key], leaf["spec"])
        if err:
            failures.append(f"{leaf['metadata']['name']} ({key[1]}): {err}")
    print(f"{env}: {len(leaves)} leaf Certificates verified against {len(issuers)} issuers")
    if env not in _NO_CERT_MANAGER:
        assert leaves, f"{env}: zero leaf Certificates rendered -- nothing was examined"
    assert not failures, f"{env}: name-constraint violations:\n" + "\n".join(failures)


@pytest.mark.parametrize("env", ["dal2alpha", "beta", "gamma", "production"])
def test_hub_api_streaming_server_name_is_a_host_api_san(env: str) -> None:
    """hub-api's RoomsService TLS server name + target host must be SANs on the host-api leaf."""
    docs = _render(_ENVS[env])
    hub = [d for d in docs if d.get("kind") == "Deployment"
           and d["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "hub-api"]
    assert len(hub) == 1, f"{env}: expected one hub-api Deployment, got {len(hub)}"
    env_vars = {e["name"]: e.get("value", "") for c in hub[0]["spec"]["template"]["spec"]["containers"]
                for e in c.get("env", [])}
    if "STREAMING_RTC_GRPC_TARGET" not in env_vars:
        pytest.skip(f"{env}: svc-streaming RoomsService not wired")
    certs = [d for d in docs if d.get("kind") == "Certificate" and d["metadata"]["name"].endswith("-host-api-tls")]
    assert len(certs) == 1, f"{env}: expected one host-api-tls Certificate, got {len(certs)}"
    sans = set(certs[0]["spec"]["dnsNames"])
    host = env_vars["STREAMING_RTC_GRPC_TARGET"].rsplit(":", 1)[0]
    assert host in sans, f"{env}: target host {host} not in host-api SANs {sorted(sans)}"
    assert env_vars["STREAMING_RTC_GRPC_SERVER_NAME"] in sans
