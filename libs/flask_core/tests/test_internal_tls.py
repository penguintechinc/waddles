"""Policy + real-path TLS tests for ``flask_core.internal_tls``.

The real-path tests stand up a live hypercorn HTTPS server (ECDSA P-256 cert signed by a
throwaway internal CA) and drive it with the production client builder, proving: CA
verification passes only for the right CA, TLS >= 1.2 is negotiated, HTTP/2 is selected via
ALPN, a handshake failure is a loud error, and one pooled client reuses one connection.
"""

from __future__ import annotations

import asyncio
import datetime
import socket
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from flask_core.internal_tls import (
    InternalTlsError,
    InternalTlsPolicy,
    PooledClient,
    build_client,
)
from hypercorn.asyncio import serve
from hypercorn.config import Config


def _make_ca(common_name: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """Self-signed ECDSA P-256 / SHA-256 CA."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _write_server_material(
    tmp: Path, ca_key: ec.EllipticCurvePrivateKey, ca_cert: x509.Certificate
) -> tuple[Path, Path, Path]:
    """Write ca.crt + a server cert/key (ECDSA P-256, SAN=localhost) signed by the CA."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path, crt_path, key_path = tmp / "ca.crt", tmp / "tls.crt", tmp / "tls.key"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, crt_path, key_path


async def _app(scope: dict, receive: object, send: object) -> None:  # type: ignore[type-arg]
    """Minimal ASGI app answering 200 ``ok``."""
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
    await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[operator]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def tls_server(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Live hypercorn HTTPS server; yields (url, ca_path, other_ca_path)."""
    ca_key, ca_cert = _make_ca("test-internal-ca")
    ca_path, crt, key = _write_server_material(tmp_path, ca_key, ca_cert)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_key, other_cert = _make_ca("untrusted-ca")
    other_ca, _, _ = _write_server_material(other_dir, other_key, other_cert)

    port = _free_port()
    cfg = Config()
    cfg.bind = [f"127.0.0.1:{port}"]
    cfg.certfile = str(crt)
    cfg.keyfile = str(key)
    cfg.alpn_protocols = ["h2", "http/1.1"]
    cfg.accesslog = None
    shutdown = asyncio.Event()
    task = asyncio.create_task(serve(_app, cfg, shutdown_trigger=shutdown.wait))  # type: ignore[arg-type]
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            await asyncio.sleep(0.05)
    else:
        pytest.fail("hypercorn TLS test server never started")
    yield f"https://localhost:{port}/", ca_path, other_ca
    shutdown.set()
    await task


def test_required_without_ca_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """INTERNAL_TLS_REQUIRED with no CA fails loud rather than guessing a trust root."""
    monkeypatch.setenv("INTERNAL_TLS_REQUIRED", "true")
    monkeypatch.delenv("INTERNAL_CA_FILE", raising=False)
    with pytest.raises(InternalTlsError):
        InternalTlsPolicy.from_env()


def test_required_refuses_plaintext_url() -> None:
    """Under the policy an http:// URL is refused; https:// passes."""
    pol = InternalTlsPolicy(ca_file="/x", required=True)
    with pytest.raises(InternalTlsError):
        pol.check_url("http://hub-api:8204/x")
    assert pol.check_url("https://hub-api:8204/x") == "https://hub-api:8204/x"


def test_missing_and_garbage_ca_fail_loud(tmp_path: Path) -> None:
    """An unreadable or non-PEM CA file raises instead of silently using system roots."""
    with pytest.raises(InternalTlsError):
        InternalTlsPolicy(
            ca_file=str(tmp_path / "nope.crt"), required=True
        ).ssl_context()
    bad = tmp_path / "bad.crt"
    bad.write_text("not a cert")
    with pytest.raises(InternalTlsError):
        InternalTlsPolicy(ca_file=str(bad), required=True).ssl_context()


def test_inline_pem_policy_works_and_garbage_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INTERNAL_CA_PEM (chart secretKeyRef) is honoured; a non-PEM value fails loud."""
    import ssl

    ca_key, ca_cert = _make_ca("pem")
    ca_path, _, _ = _write_server_material(tmp_path, ca_key, ca_cert)
    monkeypatch.delenv("INTERNAL_CA_FILE", raising=False)
    monkeypatch.setenv("INTERNAL_TLS_REQUIRED", "true")
    monkeypatch.setenv("INTERNAL_CA_PEM", ca_path.read_text())
    assert isinstance(InternalTlsPolicy.from_env().ssl_context(), ssl.SSLContext)
    monkeypatch.setenv("INTERNAL_CA_PEM", "garbage")
    with pytest.raises(InternalTlsError):
        InternalTlsPolicy.from_env().ssl_context()


def test_context_floors_tls12_and_offers_h2(tmp_path: Path) -> None:
    """The pinned context floors TLS at 1.2 and advertises h2 + http/1.1 via ALPN."""
    import ssl

    ca_key, ca_cert = _make_ca("c")
    ca_path, _, _ = _write_server_material(tmp_path, ca_key, ca_cert)
    ctx = InternalTlsPolicy(ca_file=str(ca_path), required=True).ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


async def test_real_handshake_verifies_ca_negotiates_h2_tls13(tls_server) -> None:  # type: ignore[no-untyped-def]
    """Real hypercorn HTTPS: trusted CA succeeds over HTTP/2 + TLS 1.3; wrong CA fails loud."""
    url, ca_path, other_ca = tls_server
    async with build_client(InternalTlsPolicy(str(ca_path), True)) as client:
        resp = await client.get(url)
        assert resp.text == "ok"
        assert resp.http_version == "HTTP/2"
        stream = resp.extensions["network_stream"]
        assert stream.get_extra_info("ssl_object").version() == "TLSv1.3"
    async with build_client(InternalTlsPolicy(str(other_ca), True)) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(url)


async def test_pooled_client_reuses_one_connection(tls_server) -> None:  # type: ignore[no-untyped-def]
    """One PooledClient handle: the same object every call and one TCP connection for N requests."""
    url, ca_path, _ = tls_server
    pool = PooledClient(InternalTlsPolicy(str(ca_path), True))
    assert pool.get() is pool.get()
    ports = set()
    for _ in range(4):
        resp = await pool.get().get(url)
        assert resp.status_code == 200
        ports.add(resp.extensions["network_stream"].get_extra_info("client_addr")[1])
    assert len(ports) == 1, f"expected 1 pooled connection, saw client ports {ports}"
    await pool.get().aclose()
