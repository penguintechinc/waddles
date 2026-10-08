"""Regression coverage for PR #570 review blocker 1.

hub-api's internal gRPC
listener (`grpc_internal/server.py`) never started its TLS listener because
the chart never set `GRPC_TLS_CERT_PATH`/`GRPC_TLS_KEY_PATH`.
`tests/test_grpc_internal.py` explicitly runs over plaintext and documents
that `server.py`'s credential loading isn't exercised there -- this file is
that missing coverage: a real `grpc.aio.server()` started via
`start_internal_grpc_server` with real (self-signed, generated fresh per
test) cert/key files, proving the listener is TLS, not plaintext, and is
actually live end to end (reaches the auth interceptor).
"""

from __future__ import annotations

import datetime
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
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey
from waddles.hub.internal.v1 import identity_pb2, identity_pb2_grpc

from grpc_internal.server import start_internal_grpc_server, stop_internal_grpc_server

SPIFFE_ID = "spiffe://penguintech.io/alpha/svc-process"


@pytest.fixture
def issuer() -> ServiceJwtIssuer:
    """A throwaway single-key issuer.

    Unused by this file's tests (every call here is deliberately
    unauthenticated), but `build_internal_grpc_server` requires one to wire
    `AuthInterceptor`.
    """
    private_key = Ed25519PrivateKey.generate()
    identity = ServiceIdentity(
        service_id=SPIFFE_ID,
        k8s_namespace="waddlebot",
        k8s_service_account="svc-process",
        allowed_scopes=frozenset({"identity:ephemeral:mint"}),
    )
    signing_key = SigningKey(
        kid="test-kid", private_key=private_key, public_key=private_key.public_key()
    )
    return ServiceJwtIssuer(
        keys={"test-kid": signing_key},
        active_kid="test-kid",
        identities={identity.service_id: identity},
    )


def _free_port() -> int:
    """A real OS-assigned free TCP port.

    Needed up front (not `:0`) so the test knows which port to dial without
    `build_internal_grpc_server` exposing the ephemeral port
    `add_secure_port` resolves internally.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _generate_self_signed_cert(tmp_path: Path) -> tuple[Path, Path, bytes]:
    """A throwaway self-signed cert/key for 127.0.0.1.

    Generated fresh per test run -- no key material, test-only or
    otherwise, committed to git.
    """
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
            x509.SubjectAlternativeName(
                [x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_path = tmp_path / "server.crt"
    key_path = tmp_path / "server.key"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)
    return cert_path, key_path, cert_pem


@pytest.fixture
def tls_server_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, bytes]]:
    """Points GRPC_TLS_CERT_PATH/GRPC_TLS_KEY_PATH/GRPC_PORT at real material.

    A generated cert/key pair and a known free port -- exactly the three
    env vars `k8s/helm/waddlebot/templates/hub-api.yaml` now wires.
    """
    cert_path, key_path, cert_pem = _generate_self_signed_cert(tmp_path)
    port = _free_port()
    monkeypatch.setenv("GRPC_TLS_CERT_PATH", str(cert_path))
    monkeypatch.setenv("GRPC_TLS_KEY_PATH", str(key_path))
    monkeypatch.setenv("GRPC_PORT", str(port))
    monkeypatch.delenv("GRPC_TLS_CLIENT_CA_PATH", raising=False)
    yield port, cert_pem


async def test_server_fails_closed_without_cert_env_vars(issuer: ServiceJwtIssuer) -> None:
    """The regression itself.

    With no GRPC_TLS_CERT_PATH/GRPC_TLS_KEY_PATH set (the chart's prior
    state), `_load_server_credentials` raises `KeyError` and the listener
    never starts -- never silently falls back to plaintext.
    """
    with pytest.raises(KeyError):
        await start_internal_grpc_server(issuer=issuer)


async def test_server_starts_a_real_tls_listener_not_plaintext(
    issuer: ServiceJwtIssuer, tls_server_env: tuple[int, bytes]
) -> None:
    port, _cert_pem = tls_server_env
    server = await start_internal_grpc_server(issuer=issuer)
    try:
        # A plaintext client against this port must fail the connection --
        # proves the listener is TLS, not accidentally insecure despite the
        # TLS env vars being present.
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = identity_pb2_grpc.IdentityServiceStub(channel)
            with pytest.raises(grpc.aio.AioRpcError):
                await stub.MintEphemeralPseudonyms(
                    identity_pb2.MintEphemeralPseudonymsRequest(items=[]), timeout=3.0
                )
    finally:
        await stop_internal_grpc_server(server)


async def test_server_accepts_a_real_tls_handshake_and_runs_the_auth_chain(
    issuer: ServiceJwtIssuer, tls_server_env: tuple[int, bytes]
) -> None:
    port, cert_pem = tls_server_env
    server = await start_internal_grpc_server(issuer=issuer)
    try:
        creds = grpc.ssl_channel_credentials(root_certificates=cert_pem)
        async with grpc.aio.secure_channel(f"127.0.0.1:{port}", creds) as channel:
            stub = identity_pb2_grpc.IdentityServiceStub(channel)
            # No bearer token -- the TLS handshake itself must succeed (this
            # is the regression under test), and the call is then rejected
            # by AuthInterceptor at the application layer (UNAUTHENTICATED),
            # never a transport-level failure.
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.MintEphemeralPseudonyms(
                    identity_pb2.MintEphemeralPseudonymsRequest(items=[]), timeout=3.0
                )
            assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await stop_internal_grpc_server(server)
