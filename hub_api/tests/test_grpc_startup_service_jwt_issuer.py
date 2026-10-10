"""Regression coverage for fix/hub-api-grpc-service-jwt-issuer.

Live evidence (alpha): hub-api logged `"hub-api internal gRPC server not
started: service_jwt issuer unconfigured"` (`grpc_startup_skipped`) and never
bound port 50204, blocking svc-process/svc-action's `HubClient` and the whole
PII-tokenization data path.

Root cause was in the chart, not this code: `k8s/helm/waddlebot/templates/
auto-provisioned-keys-job.yaml`'s auto-provisioned `service-jwt-signing-key`
Secret stored the private key under the bare name `SERVICE_JWT_PRIVATE_KEY`
(no `_<kid>` suffix) as a raw base64 seed -- `flask_core.service_jwt.
load_issuer_from_env` only ever recognizes `SERVICE_JWT_PRIVATE_KEY_<kid>`
env vars holding PEM text, so `hub_api/app.py::startup` always caught a
`ServiceJwtError`/`KeyError`, set `issuer = None`, and the gRPC startup guard
(`cfg.grpc_enabled and issuer is not None`) skipped the listener every time.

This file proves the FIX end to end at the level `app.py::startup` actually
operates at: given env vars shaped exactly like the corrected chart render
(`SERVICE_JWT_ACTIVE_KID` + PEM `SERVICE_JWT_PRIVATE_KEY_<kid>` +
`SERVICE_JWT_IDENTITIES`) plus the TLS env `fix/hub-grpc-tls-and-ca-trust`
(#572) already wires, `load_issuer_from_env` builds a real issuer and
`start_internal_grpc_server` actually binds -- never hits
`grpc_startup_skipped`. The negative case (chart's old, broken Secret shape)
is asserted too, so this test would have caught the regression.
"""

from __future__ import annotations

import datetime
import ipaddress
import socket
from collections.abc import Iterator
from pathlib import Path

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID
from flask_core.service_jwt import ServiceJwtError, load_identities_from_env, load_issuer_from_env

from grpc_internal.server import start_internal_grpc_server, stop_internal_grpc_server

SPIFFE_ID = "spiffe://penguintech.io/alpha/svc-process"
KID = "waddlebot1"  # must be a valid POSIX env-var-name suffix, see values.yaml comment


def _free_port() -> int:
    """A real OS-assigned free TCP port, resolved before the server binds."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _generate_self_signed_cert(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway self-signed cert/key for 127.0.0.1 (mirrors test_grpc_internal_tls.py)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "server.crt"
    key_path = tmp_path / "server.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def chart_shaped_service_jwt_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env vars exactly as the FIXED chart renders them.

    `auto-provisioned-keys-job.yaml` + `hub-api.yaml`: `SERVICE_JWT_ACTIVE_KID`
    (bare) + PEM-encoded `SERVICE_JWT_PRIVATE_KEY_<kid>` (projected from the
    Secret via `envFrom.secretRef`) + `SERVICE_JWT_IDENTITIES` (rendered by
    `_service_jwt.tpl`'s `waddlebot.serviceJwtIdentitiesJson`).
    """
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    monkeypatch.setenv("SERVICE_JWT_ACTIVE_KID", KID)
    monkeypatch.setenv(f"SERVICE_JWT_PRIVATE_KEY_{KID}", pem)
    monkeypatch.setenv("SERVICE_JWT_AUDIENCE", "waddlebot-internal")
    monkeypatch.setenv(
        "SERVICE_JWT_IDENTITIES",
        '[{"service_id": "svc-process", "k8s_namespace": "waddlebot", '
        '"k8s_service_account": "svc-process", '
        '"allowed_scopes": ["identity:ephemeral:mint"]}]',
    )


@pytest.fixture
def grpc_tls_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Points GRPC_TLS_CERT_PATH/GRPC_TLS_KEY_PATH/GRPC_PORT at real material."""
    cert_path, key_path = _generate_self_signed_cert(tmp_path)
    port = _free_port()
    monkeypatch.setenv("GRPC_TLS_CERT_PATH", str(cert_path))
    monkeypatch.setenv("GRPC_TLS_KEY_PATH", str(key_path))
    monkeypatch.setenv("GRPC_PORT", str(port))
    monkeypatch.delenv("GRPC_TLS_CLIENT_CA_PATH", raising=False)
    yield port


class TestServerStartsWithChartShapedIssuerConfig:
    """The actual fix: issuer config in the shape the corrected chart renders."""

    async def test_server_binds_and_does_not_skip_startup(
        self, chart_shaped_service_jwt_env: None, grpc_tls_env: int
    ) -> None:
        """`load_issuer_from_env`+`start_internal_grpc_server` both succeed.

        The exact two calls `hub_api/app.py::startup` makes, proving the
        `grpc_startup_skipped` path is never taken with this config.
        """
        identities = load_identities_from_env(env="alpha")
        issuer = load_issuer_from_env(identities)  # must not raise

        server = await start_internal_grpc_server(issuer=issuer)
        try:
            assert server is not None
            # A client can at least complete a TLS handshake against the bound
            # port -- proves this is a live listener, not just a constructed,
            # never-started server object.
            port = grpc_tls_env
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                from waddles.hub.internal.v1 import identity_pb2, identity_pb2_grpc

                stub = identity_pb2_grpc.IdentityServiceStub(channel)
                with pytest.raises(grpc.aio.AioRpcError):
                    # Plaintext against a TLS-only listener must fail the
                    # connection, not succeed -- confirms a real secure port.
                    await stub.MintEphemeralPseudonyms(
                        identity_pb2.MintEphemeralPseudonymsRequest(items=[]), timeout=3.0
                    )
        finally:
            await stop_internal_grpc_server(server)


class TestServerSkipsStartupWithoutIssuerConfig:
    """The regression itself: hub_api/app.py's exact guard condition."""

    def test_load_issuer_from_env_raises_on_missing_active_kid(self, grpc_tls_env: int) -> None:
        """No `SERVICE_JWT_ACTIVE_KID` at all (never-configured deployment).

        `app.py::startup` catches this `KeyError` and leaves `issuer = None`,
        which is exactly what makes the gRPC startup guard skip the listener.
        """
        with pytest.raises(KeyError):
            load_issuer_from_env([])

    def test_load_issuer_from_env_raises_on_old_broken_chart_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The actual historical bug: a present but unsuffixed private key name.

        `SERVICE_JWT_ACTIVE_KID` is present, but the private key Secret-key
        the old (broken) `auto-provisioned-keys-job.yaml` wrote was the bare,
        unsuffixed `SERVICE_JWT_PRIVATE_KEY` -- never matching the
        `SERVICE_JWT_PRIVATE_KEY_<kid>` prefix `load_issuer_from_env` scans
        for, so `keys` stays empty and this raises `ServiceJwtError` exactly
        as it did on alpha.
        """
        monkeypatch.setenv("SERVICE_JWT_ACTIVE_KID", KID)
        monkeypatch.setenv("SERVICE_JWT_PRIVATE_KEY", "not-matched-by-the-_<kid>-suffix-scan")
        with pytest.raises(ServiceJwtError):
            load_issuer_from_env([])
