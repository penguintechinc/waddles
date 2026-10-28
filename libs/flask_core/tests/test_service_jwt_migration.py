"""EdDSA service-auth migration: verify-only trust, HS256 rejection, gRPC interceptor, token client.

Covers the HS256 shared-secret removal (security audit HIGH): the legacy fleet-wide
HS256 token is REJECTED by every EdDSA verifier, a verify-only service can never hold a
signing key, cross-module/forged tokens fail, kid rotation works, and the shared gRPC
interceptor is exercised against a real in-process grpc.aio server.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import grpc
import jwt as pyjwt
import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.grpc_service_auth import ServiceJwtAuthInterceptor
from flask_core.service_jwt import (
    BootstrapRejected,
    InvalidServiceToken,
    ServiceIdentity,
    ServiceJwtError,
    ServiceJwtIssuer,
    ServiceTokenClient,
    SigningKey,
    StaticTrustBundle,
    UnknownKeyId,
    load_verifier_from_env,
    spiffe_id,
)

SCOPE_A = "action:discord:invoke"
SCOPE_B = "action:slack:invoke"
CALLER = spiffe_id("alpha", "router")
LEGACY_FLEET_SECRET = "x" * 48  # stands in for the shared MODULE_SECRET_KEY / SECRET_KEY


def _pem_pub(priv: Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def _pem_priv(priv: Ed25519PrivateKey) -> str:
    return priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _issuer(kid: str = "k1", priv: Ed25519PrivateKey | None = None) -> tuple[ServiceJwtIssuer, Ed25519PrivateKey]:
    priv = priv or Ed25519PrivateKey.generate()
    ident = ServiceIdentity(
        service_id=CALLER, k8s_namespace="waddlebot", k8s_service_account="router",
        allowed_scopes=frozenset({SCOPE_A, SCOPE_B}), tenant="system",
    )
    key = SigningKey(kid=kid, private_key=priv, public_key=priv.public_key())
    return ServiceJwtIssuer(keys={kid: key}, active_kid=kid, identities={CALLER: ident}), priv


@pytest.fixture
def verifier_env(monkeypatch: pytest.MonkeyPatch):
    """Env for a verify-only service trusting one issuer key; returns (issuer, priv)."""
    for name in [n for n in __import__("os").environ if n.startswith("SERVICE_JWT_")]:
        monkeypatch.delenv(name)
    issuer, priv = _issuer()
    monkeypatch.setenv("SERVICE_JWT_PUBLIC_KEY_k1", _pem_pub(priv))
    return issuer, priv


def test_verify_only_service_accepts_issuer_token(verifier_env) -> None:
    issuer, _ = verifier_env
    claims = load_verifier_from_env().verify(issuer.issue(CALLER, SCOPE_A), required_scope=SCOPE_A)
    assert claims["sub"] == CALLER and claims["tenant"] == "system"


def test_legacy_hs256_fleet_secret_token_is_rejected_no_downgrade(verifier_env) -> None:
    """The exact legacy token shape (service claim, HS256, shared secret) must NOT verify."""
    legacy = pyjwt.encode(
        {"service": "router_module", "iat": int(time.time()), "exp": int(time.time()) + 3600},
        LEGACY_FLEET_SECRET, algorithm="HS256",
    )
    with pytest.raises(InvalidServiceToken):
        load_verifier_from_env().verify(legacy, required_scope=SCOPE_A)
    # ...even when it is dressed up with a known kid and every required claim.
    now = int(time.time())
    full = pyjwt.encode(
        {"iss": "hub-api", "aud": "waddlebot-internal", "sub": CALLER, "scope": SCOPE_A,
         "iat": now, "nbf": now, "exp": now + 60, "jti": "j", "tenant": "system"},
        LEGACY_FLEET_SECRET, algorithm="HS256", headers={"kid": "k1"},
    )
    with pytest.raises(InvalidServiceToken):
        load_verifier_from_env().verify(full, required_scope=SCOPE_A)


@pytest.mark.parametrize("alg", ["none", "NONE"])
def test_alg_none_rejected(verifier_env, alg: str) -> None:
    import base64

    def b64(d: bytes) -> str:
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()

    now = int(time.time())
    tok = (
        b64(json.dumps({"alg": alg, "kid": "k1"}).encode()) + "."
        + b64(json.dumps({"sub": CALLER, "scope": SCOPE_A, "exp": now + 60}).encode()) + "."
    )
    with pytest.raises(InvalidServiceToken):
        load_verifier_from_env().verify(tok, required_scope=SCOPE_A)


def test_cross_module_forgery_fails(verifier_env) -> None:
    """A compromised module cannot mint: its own key is unknown, and others' scopes don't transfer."""
    issuer, _ = verifier_env
    verifier = load_verifier_from_env()
    # (1) attacker signs with its own Ed25519 key but claims the trusted kid
    attacker = Ed25519PrivateKey.generate()
    forged, _ = _issuer(kid="k1", priv=attacker)
    with pytest.raises(InvalidServiceToken):
        verifier.verify(forged.issue(CALLER, SCOPE_A), required_scope=SCOPE_A)
    # (2) attacker uses a kid not in the bundle
    other, _ = _issuer(kid="evil")
    with pytest.raises(UnknownKeyId):
        verifier.verify(other.issue(CALLER, SCOPE_A), required_scope=SCOPE_A)
    # (3) legitimately-issued token for module A replayed against module B's scope
    with pytest.raises(InvalidServiceToken):
        verifier.verify(issuer.issue(CALLER, SCOPE_A), required_scope=SCOPE_B)


def test_kid_rotation_verify_only_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    old_issuer, old_priv = _issuer("k1")
    new_issuer, new_priv = _issuer("k2")
    old_token = old_issuer.issue(CALLER, SCOPE_A)
    for name in [n for n in __import__("os").environ if n.startswith("SERVICE_JWT_")]:
        monkeypatch.delenv(name)
    # overlap window: both public keys shipped
    monkeypatch.setenv("SERVICE_JWT_PUBLIC_KEY_k1", _pem_pub(old_priv))
    monkeypatch.setenv("SERVICE_JWT_PUBLIC_KEY_k2", _pem_pub(new_priv))
    v = load_verifier_from_env()
    assert v.verify(old_token, required_scope=SCOPE_A)["sub"] == CALLER
    assert v.verify(new_issuer.issue(CALLER, SCOPE_A), required_scope=SCOPE_A)["sub"] == CALLER
    # old key retired: old tokens now refused
    monkeypatch.delenv("SERVICE_JWT_PUBLIC_KEY_k1")
    with pytest.raises(UnknownKeyId):
        load_verifier_from_env().verify(old_token, required_scope=SCOPE_A)


def test_verifier_refuses_to_hold_a_private_key(verifier_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _, priv = verifier_env
    monkeypatch.setenv("SERVICE_JWT_PRIVATE_KEY_k1", _pem_priv(priv))
    with pytest.raises(ServiceJwtError, match="must not hold"):
        load_verifier_from_env()


def test_verifier_fails_closed_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [n for n in __import__("os").environ if n.startswith("SERVICE_JWT_")]:
        monkeypatch.delenv(name)
    with pytest.raises(ServiceJwtError, match="refusing to run"):
        load_verifier_from_env()


def test_verifier_rejects_bad_key_material(verifier_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_JWT_PUBLIC_KEY_bad", "not a pem")
    with pytest.raises(ServiceJwtError):
        load_verifier_from_env()
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_pem = rsa.generate_private_key(65537, 2048).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setenv("SERVICE_JWT_PUBLIC_KEY_bad", rsa_pem)
    with pytest.raises(ServiceJwtError, match="not an Ed25519"):
        load_verifier_from_env()
    monkeypatch.delenv("SERVICE_JWT_PUBLIC_KEY_bad")
    monkeypatch.setenv("SERVICE_JWT_PUBLIC_KEY_../x", _pem_pub(Ed25519PrivateKey.generate()))
    with pytest.raises(ServiceJwtError, match="malformed kid"):
        load_verifier_from_env()


def test_static_trust_bundle_unknown_kid() -> None:
    assert StaticTrustBundle(keys={}).get_public_key("k1") is None


# ---- real-path gRPC interceptor ------------------------------------------------------

async def _serve(interceptor: ServiceJwtAuthInterceptor) -> tuple[grpc.aio.Server, int]:
    async def echo(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
        return b"ok"

    server = grpc.aio.server(interceptors=[interceptor])
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler("t.Svc", {"Do": grpc.unary_unary_rpc_method_handler(echo)}),)
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, port


async def _call(port: int, token: str | None) -> bytes:
    md = (("authorization", f"Bearer {token}"),) if token else ()
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as ch:
        return await ch.unary_unary("/t.Svc/Do")(b"x", metadata=md, timeout=5)


async def test_grpc_interceptor_real_server(verifier_env) -> None:
    issuer, _ = verifier_env
    verifier = load_verifier_from_env()
    server, port = await _serve(
        ServiceJwtAuthInterceptor(verifier, required_scope=SCOPE_A, allowed_callers=frozenset({CALLER}))
    )
    try:
        assert await _call(port, issuer.issue(CALLER, SCOPE_A)) == b"ok"  # happy path
        legacy = pyjwt.encode({"service": "router_module", "exp": int(time.time()) + 60},
                              LEGACY_FLEET_SECRET, algorithm="HS256")
        for bad in (None, "garbage", legacy, issuer.issue(CALLER, SCOPE_B)):
            with pytest.raises(grpc.aio.AioRpcError) as err:
                await _call(port, bad)
            assert err.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await server.stop(None)


async def test_grpc_interceptor_caller_allowlist(verifier_env) -> None:
    issuer, _ = verifier_env
    server, port = await _serve(ServiceJwtAuthInterceptor(
        load_verifier_from_env(), required_scope=SCOPE_A,
        allowed_callers=frozenset({spiffe_id("alpha", "someone-else")})))
    try:
        with pytest.raises(grpc.aio.AioRpcError) as err:
            await _call(port, issuer.issue(CALLER, SCOPE_A))
        assert err.value.code() == grpc.StatusCode.PERMISSION_DENIED
    finally:
        await server.stop(None)


def test_interceptor_fails_closed_on_empty_config(verifier_env) -> None:
    v = load_verifier_from_env()
    with pytest.raises(ServiceJwtError):
        ServiceJwtAuthInterceptor(v, required_scope=SCOPE_A, allowed_callers=frozenset())
    with pytest.raises(ServiceJwtError):
        ServiceJwtAuthInterceptor(v, required_scope="", allowed_callers=frozenset({CALLER}))


# ---- caller-side token client --------------------------------------------------------

class _Resp:
    def __init__(self, body: object, status: int = 200) -> None:
        self._body, self._status = body, status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise requests.HTTPError("boom")

    def json(self) -> object:
        return self._body


def _client(tmp_path: Path, content: str = "sa-token") -> ServiceTokenClient:
    p = tmp_path / "sa"
    p.write_text(content)
    return ServiceTokenClient("http://hub/internal/service-token", SCOPE_A, str(p))


def test_token_client_caches_and_refreshes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def post(url, json, headers, timeout):  # noqa: A002
        calls.append({"url": url, "json": json, "auth": headers["Authorization"]})
        return _Resp({"token": f"t{len(calls)}", "expires_in": 900})

    monkeypatch.setattr(requests, "post", post)
    c = _client(tmp_path)
    assert c.get_token() == "t1" and c.get_token() == "t1" and len(calls) == 1
    assert calls[0]["json"] == {"scope": SCOPE_A} and calls[0]["auth"] == "Bearer sa-token"
    c._expires_at = time.time() + 10  # inside refresh margin
    assert c.get_token() == "t2"


@pytest.mark.parametrize("resp", [_Resp({}, 401), _Resp({"token": "", "expires_in": 5}),
                                  _Resp({"token": "t", "expires_in": 999999}), _Resp([1])])
def test_token_client_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resp: _Resp) -> None:
    monkeypatch.setattr(requests, "post", lambda *a, **k: resp)
    with pytest.raises(BootstrapRejected):
        _client(tmp_path).get_token()


def test_token_client_unreadable_or_empty_sa_token(tmp_path: Path) -> None:
    with pytest.raises(BootstrapRejected):
        ServiceTokenClient("http://x", SCOPE_A, str(tmp_path / "missing")).get_token()
    with pytest.raises(BootstrapRejected):
        _client(tmp_path, content="  ").get_token()


def test_token_client_transport_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def post(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(BootstrapRejected):
        _client(tmp_path).get_token()


async def test_token_client_async(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp({"token": "ta", "expires_in": 900}))
    assert await _client(tmp_path).get_token_async() == "ta"
