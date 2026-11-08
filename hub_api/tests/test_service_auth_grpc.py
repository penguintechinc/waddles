"""`ServiceAuthService` end to end: the machine-JWT mint + JWKS over real gRPC.

Replaces the REST `POST /internal/service-token` / `GET /internal/service-jwks.json` tests. Only the
k8s TokenReview call is faked; the server is the real interceptor chain
(`build_interceptor_chain`) + real `ServiceAuthServicer` + real `ServiceJwtIssuer`, and the client
is the real `waddle_transports.service_jwt_client`. The real-path tests mint a token over gRPC and
verify it with the key material fetched over gRPC (JWKS), never with the issuer object.
"""

from __future__ import annotations

import base64
import datetime
import ipaddress
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import grpc
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.x509.oid import NameOID
from flask_core.service_jwt import (
    BootstrapRejected,
    ServiceIdentity,
    ServiceJwtIssuer,
    SigningKey,
    spiffe_id,
)
from waddle_transports import service_jwt_client
from waddle_transports.service_jwt_client import ServiceTokenError, mint_service_token
from waddles.hub.internal.v1 import key_pb2, key_pb2_grpc, service_auth_pb2, service_auth_pb2_grpc

from grpc_internal import service_auth_servicer as servicer_module
from grpc_internal.server import build_interceptor_chain, build_internal_grpc_server
from grpc_internal.service_auth_servicer import ServiceAuthServicer

SCOPE = "identity:ephemeral:mint"
SERVICE_ID = spiffe_id("alpha", "svc-process")
_MINT = "/waddles.hub.internal.v1.ServiceAuthService/MintServiceToken"


class _AuditLog:
    """Captures `logger.audit(...)` calls the way hub-api's AAA logger receives them."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def audit(self, **fields: Any) -> None:
        self.events.append(fields)


def _issuer(tenant: str = "acme", scopes: frozenset[str] | None = None) -> ServiceJwtIssuer:
    priv = Ed25519PrivateKey.generate()
    identity = ServiceIdentity(
        service_id=SERVICE_ID,
        k8s_namespace="waddlebot",
        k8s_service_account="svc-process",
        allowed_scopes=scopes
        or frozenset(
            {
                SCOPE,
                "connections:resolve",
                "connections:resolve:static",
                "connections:list",
                "presentation.overlay:push",
            }
        ),
        tenant=tenant,
    )
    return ServiceJwtIssuer(
        keys={"k1": SigningKey(kid="k1", private_key=priv, public_key=priv.public_key())},
        active_kid="k1",
        identities={SERVICE_ID: identity},
        audience="waddlebot-internal",
    )


@pytest.fixture
def audit() -> _AuditLog:
    return _AuditLog()


@pytest.fixture
def issuer() -> ServiceJwtIssuer:
    return _issuer()


@pytest.fixture
async def hub(issuer: ServiceJwtIssuer, audit: _AuditLog) -> AsyncIterator[str]:
    """Real production interceptor chain + servicers on a loopback port; yields `host:port`."""
    server = grpc.aio.server(interceptors=build_interceptor_chain(issuer))
    service_auth_pb2_grpc.add_ServiceAuthServiceServicer_to_server(
        ServiceAuthServicer(
            issuer,
            audit_logger=audit,
            token_reviewer=lambda **_kw: ("waddlebot", "svc-process"),
        ),
        server,
    )
    key_pb2_grpc.add_KeyServiceServicer_to_server(key_pb2_grpc.KeyServiceServicer(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(grace=None)
        await service_jwt_client.close_channels()


@pytest.fixture
def sa_token(tmp_path: Path) -> str:
    path = tmp_path / "sa-token"
    path.write_text("projected-sa-token\n", encoding="utf-8")
    return str(path)


async def _raw_mint(
    address: str,
    request: service_auth_pb2.MintServiceTokenRequest,
    *,
    bearer: str | None = "sa-token",
) -> service_auth_pb2.MintServiceTokenResponse:
    async with grpc.aio.insecure_channel(address) as channel:
        stub = service_auth_pb2_grpc.ServiceAuthServiceStub(channel)
        metadata = (("authorization", f"Bearer {bearer}"),) if bearer is not None else ()
        return await stub.MintServiceToken(request, metadata=metadata, timeout=3.0)


def _req(scope: str = SCOPE, **kw: Any) -> service_auth_pb2.MintServiceTokenRequest:
    return service_auth_pb2.MintServiceTokenRequest(api_version="v1", scope=scope, **kw)


async def _code(coro: Any) -> grpc.StatusCode:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await coro
    return exc.value.code()


# --- mint: success, audit, response shape -------------------------------------------------


async def test_token_minted_and_verifies_with_the_issuers_own_verifier(
    hub: str, issuer: ServiceJwtIssuer
) -> None:
    response = await _raw_mint(hub, _req())
    claims = issuer.as_verifier().verify(response.token, required_scope=SCOPE)
    assert claims["sub"] == SERVICE_ID and claims["tenant"] == "acme"
    assert response.expires_in == claims["exp"] - claims["iat"] > 0


async def test_audit_event_carries_the_signed_claims_and_never_the_token(
    hub: str, audit: _AuditLog
) -> None:
    response = await _raw_mint(hub, _req())
    signed = jwt.decode(response.token, options={"verify_signature": False})
    (event,) = audit.events
    assert (event["action"], event["user"], event["result"]) == (
        "service_jwt.issue",
        SERVICE_ID,
        "SUCCESS",
    )
    assert (event["jti"], event["exp"], event["tenant"], event["scope"]) == (
        signed["jti"],
        signed["exp"],
        "acme",
        SCOPE,
    )
    assert response.token not in repr(event)


async def test_missing_audit_logger_does_not_break_issuance(
    issuer: ServiceJwtIssuer,
) -> None:
    servicer = ServiceAuthServicer(
        issuer, audit_logger=None, token_reviewer=lambda **_k: ("waddlebot", "svc-process")
    )
    server = grpc.aio.server(interceptors=build_interceptor_chain(issuer))
    service_auth_pb2_grpc.add_ServiceAuthServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        assert (await _raw_mint(f"127.0.0.1:{port}", _req())).token
    finally:
        await server.stop(grace=None)


# --- mint: bootstrap authentication --------------------------------------------------------


async def test_missing_bearer_is_unauthenticated(hub: str) -> None:
    assert await _code(_raw_mint(hub, _req(), bearer=None)) == grpc.StatusCode.UNAUTHENTICATED


async def test_missing_scope_is_invalid_argument(hub: str) -> None:
    code = await _code(_raw_mint(hub, service_auth_pb2.MintServiceTokenRequest(api_version="v1")))
    assert code == grpc.StatusCode.INVALID_ARGUMENT


async def test_unsupported_api_version_is_unimplemented(hub: str) -> None:
    request = service_auth_pb2.MintServiceTokenRequest(api_version="v9", scope=SCOPE)
    assert await _code(_raw_mint(hub, request)) == grpc.StatusCode.UNIMPLEMENTED


async def test_rejected_service_account_token_is_unauthenticated_and_not_audited(
    issuer: ServiceJwtIssuer, audit: _AuditLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(**_kw: Any) -> tuple[str, str]:
        raise BootstrapRejected("nope")

    # No explicit reviewer: the servicer falls through to the module-level TokenReview call.
    monkeypatch.setattr(servicer_module, "verify_service_account_token", reject)
    server = grpc.aio.server(interceptors=build_interceptor_chain(issuer))
    service_auth_pb2_grpc.add_ServiceAuthServiceServicer_to_server(
        ServiceAuthServicer(issuer, audit_logger=audit), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        code = await _code(_raw_mint(f"127.0.0.1:{port}", _req()))
    finally:
        await server.stop(grace=None)
    assert code == grpc.StatusCode.UNAUTHENTICATED
    assert audit.events == []


async def test_unknown_service_account_is_unauthenticated(
    issuer: ServiceJwtIssuer, audit: _AuditLog
) -> None:
    server = grpc.aio.server(interceptors=build_interceptor_chain(issuer))
    service_auth_pb2_grpc.add_ServiceAuthServiceServicer_to_server(
        ServiceAuthServicer(
            issuer, audit_logger=audit, token_reviewer=lambda **_k: ("other", "intruder")
        ),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        code = await _code(_raw_mint(f"127.0.0.1:{port}", _req()))
    finally:
        await server.stop(grace=None)
    assert code == grpc.StatusCode.UNAUTHENTICATED and audit.events == []


async def test_scope_not_allow_listed_is_unauthenticated_and_not_audited(
    hub: str, audit: _AuditLog
) -> None:
    code = await _code(_raw_mint(hub, _req("identity:other:scope")))
    assert code == grpc.StatusCode.UNAUTHENTICATED and audit.events == []


# --- mint: community binding --------------------------------------------------------------


async def test_resolve_scope_requires_community(hub: str) -> None:
    assert await _code(_raw_mint(hub, _req("connections:resolve"))) == (
        grpc.StatusCode.INVALID_ARGUMENT
    )


@pytest.mark.parametrize(
    "kwargs",
    [{"community_id": -1}, {"community_id": 7, "connection_id": -1}],
    ids=["negative-community", "negative-connection"],
)
async def test_negative_binding_ids_are_rejected(hub: str, kwargs: dict[str, int]) -> None:
    code = await _code(_raw_mint(hub, _req("connections:resolve", **kwargs)))
    assert code == grpc.StatusCode.INVALID_ARGUMENT


async def test_resolve_token_is_bound_and_short_lived(
    hub: str, issuer: ServiceJwtIssuer, audit: _AuditLog
) -> None:
    response = await _raw_mint(hub, _req("connections:resolve", community_id=7, connection_id=3))
    assert response.expires_in <= 300
    claims = issuer.as_verifier().verify(response.token, required_scope="connections:resolve")
    assert (claims["community"], claims["conn"]) == (7, 3)
    assert claims["exp"] - claims["iat"] <= 300
    event = audit.events[-1]
    assert event["community_id"] == 7 and response.token not in repr(event)


async def test_static_resolve_scope_is_community_bound(hub: str, issuer: ServiceJwtIssuer) -> None:
    scope = "connections:resolve:static"
    assert await _code(_raw_mint(hub, _req(scope))) == grpc.StatusCode.INVALID_ARGUMENT
    response = await _raw_mint(hub, _req(scope, community_id=7))
    claims = issuer.as_verifier().verify(response.token, required_scope=scope)
    assert claims["community"] == 7 and claims["exp"] - claims["iat"] <= 300


async def test_community_zero_is_a_valid_binding_and_has_no_conn_claim(
    hub: str, issuer: ServiceJwtIssuer
) -> None:
    response = await _raw_mint(hub, _req("connections:resolve", community_id=0))
    claims = issuer.as_verifier().verify(response.token, required_scope="connections:resolve")
    assert claims["community"] == 0 and "conn" not in claims


async def test_unbound_list_scope_never_has_community(hub: str, issuer: ServiceJwtIssuer) -> None:
    response = await _raw_mint(hub, _req("connections:list"))
    claims = issuer.as_verifier().verify(response.token, required_scope="connections:list")
    assert "community" not in claims
    assert await _code(_raw_mint(hub, _req("connections:list", community_id=7))) == (
        grpc.StatusCode.INVALID_ARGUMENT
    )


async def test_other_scopes_reject_binding_fields(hub: str) -> None:
    assert await _code(_raw_mint(hub, _req(SCOPE, community_id=7))) == (
        grpc.StatusCode.INVALID_ARGUMENT
    )


# --- mint: overlay PUSH scope -------------------------------------------------------------


async def test_overlay_push_token_carries_the_exact_per_community_scope(
    hub: str, issuer: ServiceJwtIssuer
) -> None:
    response = await _raw_mint(hub, _req("presentation.overlay:push", community_id=42))
    verifier = issuer.as_verifier()
    claims = verifier.verify(response.token, required_scope="presentation.overlay:push:42")
    assert claims["community"] == 42 and response.expires_in <= 300
    with pytest.raises(Exception):  # noqa: B017 -- InvalidServiceToken: wrong community
        verifier.verify(response.token, required_scope="presentation.overlay:push:91")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scope": "presentation.overlay:push"},
        {"scope": "presentation.overlay:push", "community_id": 7, "connection_id": 1},
        {"scope": "presentation.overlay:push", "community_id": -1},
        {"scope": "presentation.overlay:push:7", "community_id": 7},
        {"scope": "presentation.overlay:push:7"},
    ],
)
async def test_overlay_push_requests_are_validated(hub: str, kwargs: dict[str, Any]) -> None:
    code = await _code(_raw_mint(hub, _req(**kwargs)))
    assert code in (grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.UNAUTHENTICATED)


async def test_overlay_push_requires_the_identity_grant() -> None:
    issuer = _issuer(scopes=frozenset({SCOPE}))
    server = grpc.aio.server(interceptors=build_interceptor_chain(issuer))
    service_auth_pb2_grpc.add_ServiceAuthServiceServicer_to_server(
        ServiceAuthServicer(issuer, token_reviewer=lambda **_k: ("waddlebot", "svc-process")),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        code = await _code(
            _raw_mint(f"127.0.0.1:{port}", _req("presentation.overlay:push", community_id=7))
        )
    finally:
        await server.stop(grace=None)
    assert code == grpc.StatusCode.UNAUTHENTICATED


# --- JWKS ---------------------------------------------------------------------------------


async def test_jwks_needs_no_credential_and_serves_public_keys_only(
    hub: str, issuer: ServiceJwtIssuer
) -> None:
    async with grpc.aio.insecure_channel(hub) as channel:
        stub = service_auth_pb2_grpc.ServiceAuthServiceStub(channel)
        response = await stub.GetServiceJwks(
            service_auth_pb2.GetServiceJwksRequest(api_version="v1"), timeout=3.0
        )
    (key,) = response.keys
    assert (key.kid, key.kty, key.crv, key.alg, key.use) == ("k1", "OKP", "Ed25519", "EdDSA", "sig")
    assert key.x and key.y == ""
    assert "d" not in service_auth_pb2.Jwk.DESCRIPTOR.fields_by_name  # no private component exists


async def test_jwks_unsupported_api_version_is_unimplemented(hub: str) -> None:
    async with grpc.aio.insecure_channel(hub) as channel:
        stub = service_auth_pb2_grpc.ServiceAuthServiceStub(channel)
        code = await _code(
            stub.GetServiceJwks(
                service_auth_pb2.GetServiceJwksRequest(api_version="v9"), timeout=3.0
            )
        )
    assert code == grpc.StatusCode.UNIMPLEMENTED


# --- bootstrap exemption must not widen --------------------------------------------------


async def test_non_bootstrap_rpcs_still_require_a_machine_jwt(hub: str) -> None:
    async with grpc.aio.insecure_channel(hub) as channel:
        stub = key_pb2_grpc.KeyServiceStub(channel)
        code = await _code(stub.GetStreamDek(key_pb2.GetStreamDekRequest(), timeout=3.0))
    assert code == grpc.StatusCode.UNAUTHENTICATED


def test_bootstrap_methods_are_exactly_mint_and_jwks() -> None:
    assert servicer_module.BOOTSTRAP_METHODS == {
        "/waddles.hub.internal.v1.ServiceAuthService/MintServiceToken",
        "/waddles.hub.internal.v1.ServiceAuthService/GetServiceJwks",
    }


async def test_bootstrap_rpcs_are_rate_limited_per_peer(issuer: ServiceJwtIssuer) -> None:
    from grpc_internal.interceptors import RateLimitInterceptor

    limiter = RateLimitInterceptor(requests_per_second=0.0001, burst=2)
    assert [limiter._take("peer:10.0.0.1") for _ in range(3)] == [True, True, False]
    assert limiter._take("peer:10.0.0.2") is True  # another host has its own bucket


@pytest.mark.parametrize(
    ("peer", "host"),
    [
        ("ipv4:10.1.2.3:5555", "10.1.2.3"),
        ("ipv6:[::1]:5555", "::1"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_peer_host_strips_the_port(peer: str | None, host: str) -> None:
    from grpc_internal.interceptors import _peer_host

    assert _peer_host(peer) == host


# --- real path: real client -> real server, mint then verify via gRPC-fetched JWKS --------


def _b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


async def test_real_path_mint_then_verify_with_jwks_fetched_over_grpc(
    hub: str, sa_token: str
) -> None:
    """The whole bootstrap: client mints over gRPC, verifier trusts only the gRPC-served JWKS."""
    endpoint = f"http://{hub}"
    minted = await mint_service_token(
        endpoint=endpoint, scope="connections:resolve", community_id=11, sa_token_path=sa_token
    )
    async with grpc.aio.insecure_channel(hub) as channel:
        jwks = await service_auth_pb2_grpc.ServiceAuthServiceStub(channel).GetServiceJwks(
            service_auth_pb2.GetServiceJwksRequest(api_version="v1"), timeout=3.0
        )
    public = {k.kid: Ed25519PublicKey.from_public_bytes(_b64u_decode(k.x)) for k in jwks.keys}
    header = jwt.get_unverified_header(minted.token)
    assert header["alg"] == "EdDSA" and header["kid"] in public
    claims = jwt.decode(
        minted.token,
        public[header["kid"]],
        algorithms=["EdDSA"],
        audience="waddlebot-internal",
    )
    assert claims["sub"] == SERVICE_ID
    assert claims["scope"] == "connections:resolve" and claims["community"] == 11
    assert minted.expires_in == claims["exp"] - claims["iat"]
    # A token signed by any other key must NOT verify against the served JWKS.
    forged = jwt.encode(
        {"sub": SERVICE_ID, "aud": "waddlebot-internal"},
        Ed25519PrivateKey.generate(),
        algorithm="EdDSA",
        headers={"kid": "k1"},
    )
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(forged, public["k1"], algorithms=["EdDSA"], audience="waddlebot-internal")


async def test_real_client_surfaces_server_rejection_as_service_token_error(
    issuer: ServiceJwtIssuer, sa_token: str
) -> None:
    server = grpc.aio.server(interceptors=build_interceptor_chain(issuer))
    service_auth_pb2_grpc.add_ServiceAuthServiceServicer_to_server(
        ServiceAuthServicer(issuer, token_reviewer=lambda **_k: ("other", "intruder")), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        with pytest.raises(ServiceTokenError, match="UNAUTHENTICATED"):
            await mint_service_token(
                endpoint=f"http://127.0.0.1:{port}", scope=SCOPE, sa_token_path=sa_token
            )
    finally:
        await server.stop(grace=None)
        await service_jwt_client.close_channels()


async def test_channel_is_long_lived_and_reused(hub: str, sa_token: str) -> None:
    for _ in range(3):
        await mint_service_token(endpoint=f"http://{hub}", scope=SCOPE, sa_token_path=sa_token)
    assert len(service_jwt_client._channels) == 1


# --- real path over TLS: ECDSA P-256 server cert -----------------------------------------


def _ec_cert(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Throwaway ECDSA P-256 self-signed cert for 127.0.0.1 (generated per run, never committed)."""
    key = ec.generate_private_key(ec.SECP256R1())
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
    tmp_path.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = tmp_path / "srv.crt", tmp_path / "srv.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path, cert_path


async def test_real_path_mint_over_tls_with_pinned_ca(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sa_token: str
) -> None:
    """Production `build_internal_grpc_server` (TLS creds from env) <- real client with CA file."""
    cert, key, ca = _ec_cert(tmp_path)
    monkeypatch.setenv("GRPC_TLS_CERT_PATH", str(cert))
    monkeypatch.setenv("GRPC_TLS_KEY_PATH", str(key))
    monkeypatch.delenv("GRPC_TLS_CLIENT_CA_PATH", raising=False)
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("GRPC_PORT", str(port))
    issuer = _issuer()
    monkeypatch.setattr(
        servicer_module, "verify_service_account_token", lambda **_kw: ("waddlebot", "svc-process")
    )
    server = await build_internal_grpc_server(issuer=issuer)
    await server.start()
    try:
        minted = await mint_service_token(
            endpoint=f"https://127.0.0.1:{port}",
            scope=SCOPE,
            sa_token_path=sa_token,
            ca_file=str(ca),
        )
        assert issuer.as_verifier().verify(minted.token, required_scope=SCOPE)["sub"] == SERVICE_ID
        # An https endpoint with no CA fails loud -- never a plaintext/unverified fallback.
        monkeypatch.delenv("HUB_API_GRPC_CA_FILE", raising=False)
        with pytest.raises(ServiceTokenError, match="HUB_API_GRPC_CA_FILE is required"):
            await mint_service_token(
                endpoint=f"https://127.0.0.1:{port}", scope=SCOPE, sa_token_path=sa_token
            )
        # A CA that did not sign the server cert is refused by the TLS handshake.
        other_cert, _, other_ca = _ec_cert(tmp_path / "other")
        with pytest.raises(ServiceTokenError, match="UNAVAILABLE"):
            await mint_service_token(
                endpoint=f"https://127.0.0.1:{port}",
                scope=SCOPE,
                sa_token_path=sa_token,
                ca_file=str(other_ca),
                timeout_s=2.0,
            )
    finally:
        await server.stop(grace=None)
        await service_jwt_client.close_channels()


# --- client config failures are loud ------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint", "match"),
    [
        ("http://hub-api.internal:50204", "non-loopback"),
        ("hub-api:50204", "invalid hub-api gRPC endpoint"),
        ("ftp://127.0.0.1:1", "invalid hub-api gRPC endpoint"),
        ("http://127.0.0.1", "invalid hub-api gRPC endpoint"),
        ("https://hub-api.internal:50204", "HUB_API_GRPC_CA_FILE is required"),
    ],
)
async def test_client_refuses_insecure_or_malformed_endpoints(
    endpoint: str, match: str, sa_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HUB_API_GRPC_CA_FILE", raising=False)
    with pytest.raises(ServiceTokenError, match=match):
        await mint_service_token(endpoint=endpoint, scope=SCOPE, sa_token_path=sa_token)


async def test_client_fails_loud_on_unreadable_ca_file(sa_token: str, tmp_path: Path) -> None:
    with pytest.raises(ServiceTokenError, match="cannot read gRPC CA file"):
        await mint_service_token(
            endpoint="https://hub-api.internal:50204",
            scope=SCOPE,
            sa_token_path=sa_token,
            ca_file=str(tmp_path / "absent.pem"),
        )


@pytest.mark.parametrize(
    ("content", "match"),
    [(None, "unreadable"), ("  \n", "empty")],
    ids=["missing", "empty"],
)
async def test_client_fails_loud_on_bad_sa_token(
    tmp_path: Path, content: str | None, match: str
) -> None:
    path = tmp_path / "sa"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(ServiceTokenError, match=match):
        await mint_service_token(
            endpoint="http://127.0.0.1:1", scope=SCOPE, sa_token_path=str(path)
        )


async def test_client_default_sa_path_comes_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SERVICE_JWT_SA_TOKEN_PATH", str(tmp_path / "absent"))
    with pytest.raises(ServiceTokenError, match="unreadable"):
        await mint_service_token(endpoint="http://127.0.0.1:1", scope=SCOPE)


async def test_client_unreachable_hub_is_a_loud_error(sa_token: str) -> None:
    with pytest.raises(ServiceTokenError, match="UNAVAILABLE"):
        await mint_service_token(
            endpoint="http://127.0.0.1:1", scope=SCOPE, sa_token_path=sa_token, timeout_s=1.0
        )
    await service_jwt_client.close_channels()
