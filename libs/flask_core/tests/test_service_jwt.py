"""Tests for per-service ES256 machine JWTs (service_jwt.py).

Covers the security-critical rejection paths called out in the feature
spec: forged, expired, wrong-audience, wrong-scope, and unknown-`kid`
tokens are all rejected; key rotation keeps verifying old tokens with the
old key while issuing with the new one; and the k8s bootstrap rejects a
ServiceAccount identity that doesn't match any allow-listed service.
"""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask_core.service_jwt import (
    BootstrapRejected,
    InvalidServiceToken,
    ServiceIdentity,
    ServiceJwtError,
    ServiceJwtIssuer,
    SigningKey,
    UnknownKeyId,
    load_identities_from_env,
    load_issuer_from_env,
    spiffe_id,
    verify_service_account_token,
)

AUDIENCE = "waddlebot-internal"
SCOPE = "identity:ephemeral:mint"


@pytest.fixture
def keypair() -> tuple[ec.EllipticCurvePrivateKey, SigningKey]:
    priv = ec.generate_private_key(ec.SECP256R1())
    return priv, SigningKey(kid="k1", private_key=priv, public_key=priv.public_key())


@pytest.fixture
def service_id() -> str:
    return spiffe_id("alpha", "svc-process")


@pytest.fixture
def issuer(keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey], service_id: str) -> ServiceJwtIssuer:
    _, key = keypair
    identity = ServiceIdentity(
        service_id=service_id,
        k8s_namespace="waddlebot",
        k8s_service_account="svc-process",
        allowed_scopes=frozenset({SCOPE}),
    )
    return ServiceJwtIssuer(keys={"k1": key}, active_kid="k1", identities={service_id: identity}, audience=AUDIENCE)


def test_spiffe_id_shape() -> None:
    assert spiffe_id("beta", "svc-action") == "spiffe://penguintech.io/beta/svc-action"
    with pytest.raises(Exception):
        spiffe_id("Bad Env!", "svc-action")


def test_issue_and_verify_round_trip(issuer: ServiceJwtIssuer, service_id: str) -> None:
    token = issuer.issue(service_id, SCOPE)
    claims = issuer.as_verifier().verify(token, required_scope=SCOPE)
    assert claims["sub"] == service_id
    assert claims["scope"] == SCOPE
    assert claims["aud"] == AUDIENCE


def test_issued_token_carries_tenant_claim_from_identity_only(
    keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey], service_id: str
) -> None:
    """The `tenant` claim is stamped from the allow-listed identity -- never the caller."""
    _, key = keypair

    def issuer_for(tenant: str) -> ServiceJwtIssuer:
        identity = ServiceIdentity(
            service_id=service_id,
            k8s_namespace="waddlebot",
            k8s_service_account="svc-process",
            allowed_scopes=frozenset({SCOPE}),
            tenant=tenant,
        )
        return ServiceJwtIssuer(
            keys={"k1": key}, active_kid="k1", identities={service_id: identity}, audience=AUDIENCE
        )

    bound = issuer_for("acme")
    claims = bound.as_verifier().verify(bound.issue(service_id, SCOPE), required_scope=SCOPE)
    assert claims["tenant"] == "acme"
    system = issuer_for("system")
    claims = system.as_verifier().verify(system.issue(service_id, SCOPE), required_scope=SCOPE)
    assert claims["tenant"] == "system"
    # an identity with no tenant binding issues a token with NO tenant claim (consumers that
    # require one -- the internal gRPC server -- reject it; nothing defaults to a tenant)
    untenanted = issuer_for("")
    claims = untenanted.as_verifier().verify(untenanted.issue(service_id, SCOPE), required_scope=SCOPE)
    assert "tenant" not in claims


def test_issue_sets_nbf_to_iat(issuer: ServiceJwtIssuer, service_id: str) -> None:
    """Security review MEDIUM finding: `nbf` must be set at issuance."""
    token = issuer.issue(service_id, SCOPE)
    claims = issuer.as_verifier().verify(token, required_scope=SCOPE)
    assert claims["nbf"] == claims["iat"]


def test_missing_nbf_rejected(issuer: ServiceJwtIssuer, keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey], service_id: str) -> None:
    """A token minted without `nbf` (older issuer, or forged) must be rejected."""
    priv, _ = keypair
    now = int(time.time())
    no_nbf = pyjwt.encode(
        {"iss": "hub-api", "aud": AUDIENCE, "sub": service_id, "scope": SCOPE, "iat": now, "exp": now + 900, "jti": "x"},
        priv,
        algorithm="ES256",
        headers={"kid": "k1"},
    )
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(no_nbf, required_scope=SCOPE)


def test_not_yet_valid_token_rejected(issuer: ServiceJwtIssuer, keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey], service_id: str) -> None:
    """`nbf` in the future, beyond clock-skew leeway, must be rejected."""
    priv, _ = keypair
    now = int(time.time())
    from flask_core.service_jwt import CLOCK_SKEW_SECONDS

    not_yet_valid = pyjwt.encode(
        {
            "iss": "hub-api",
            "aud": AUDIENCE,
            "sub": service_id,
            "scope": SCOPE,
            "iat": now,
            "nbf": now + CLOCK_SKEW_SECONDS + 300,
            "exp": now + 900,
            "jti": "x",
        },
        priv,
        algorithm="ES256",
        headers={"kid": "k1"},
    )
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(not_yet_valid, required_scope=SCOPE)


def test_nbf_within_clock_skew_accepted(issuer: ServiceJwtIssuer, keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey], service_id: str) -> None:
    """`nbf` slightly in the future, within clock-skew leeway, still verifies."""
    priv, _ = keypair
    now = int(time.time())
    from flask_core.service_jwt import CLOCK_SKEW_SECONDS

    token = pyjwt.encode(
        {
            "iss": "hub-api",
            "aud": AUDIENCE,
            "sub": service_id,
            "scope": SCOPE,
            "iat": now,
            "nbf": now + CLOCK_SKEW_SECONDS - 5,
            "exp": now + 900,
            "jti": "x",
        },
        priv,
        algorithm="ES256",
        headers={"kid": "k1"},
    )
    claims = issuer.as_verifier().verify(token, required_scope=SCOPE)
    assert claims["sub"] == service_id


def test_ttl_ceiling_enforced(issuer: ServiceJwtIssuer, service_id: str) -> None:
    with pytest.raises(Exception):
        issuer.issue(service_id, SCOPE, ttl_seconds=3601)


def test_bootstrap_rejected_for_unlisted_service(issuer: ServiceJwtIssuer) -> None:
    with pytest.raises(BootstrapRejected):
        issuer.issue(spiffe_id("alpha", "svc-unknown"), SCOPE)


def test_bootstrap_rejected_for_disallowed_scope(issuer: ServiceJwtIssuer, service_id: str) -> None:
    with pytest.raises(BootstrapRejected):
        issuer.issue(service_id, "not:allowed")


def test_forged_token_unknown_kid_rejected(issuer: ServiceJwtIssuer, service_id: str) -> None:
    forged_key = ec.generate_private_key(ec.SECP256R1())
    now = int(time.time())
    forged = pyjwt.encode(
        {"iss": "hub-api", "aud": AUDIENCE, "sub": service_id, "scope": SCOPE, "iat": now, "exp": now + 900, "jti": "x"},
        forged_key,
        algorithm="ES256",
        headers={"kid": "nonexistent-kid"},
    )
    with pytest.raises(UnknownKeyId):
        issuer.as_verifier().verify(forged, required_scope=SCOPE)


def test_expired_token_rejected(issuer: ServiceJwtIssuer, keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey], service_id: str) -> None:
    priv, _ = keypair
    now = int(time.time())
    expired = pyjwt.encode(
        {"iss": "hub-api", "aud": AUDIENCE, "sub": service_id, "scope": SCOPE, "iat": now - 3600, "exp": now - 1800, "jti": "x"},
        priv,
        algorithm="ES256",
        headers={"kid": "k1"},
    )
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(expired, required_scope=SCOPE)


def test_wrong_audience_rejected(issuer: ServiceJwtIssuer, keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey], service_id: str) -> None:
    priv, _ = keypair
    now = int(time.time())
    wrong_aud = pyjwt.encode(
        {"iss": "hub-api", "aud": "some-other-audience", "sub": service_id, "scope": SCOPE, "iat": now, "exp": now + 900, "jti": "x"},
        priv,
        algorithm="ES256",
        headers={"kid": "k1"},
    )
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(wrong_aud, required_scope=SCOPE)


def test_wrong_scope_rejected(issuer: ServiceJwtIssuer, service_id: str) -> None:
    token = issuer.issue(service_id, SCOPE)
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(token, required_scope="some:other:scope")


def test_untrusted_issuer_rejected(issuer: ServiceJwtIssuer, keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey], service_id: str) -> None:
    priv, _ = keypair
    now = int(time.time())
    bad_iss = pyjwt.encode(
        {"iss": "not-hub-api", "aud": AUDIENCE, "sub": service_id, "scope": SCOPE, "iat": now, "exp": now + 900, "jti": "x"},
        priv,
        algorithm="ES256",
        headers={"kid": "k1"},
    )
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(bad_iss, required_scope=SCOPE)


def test_rotation_old_token_still_verifies_new_key_used_for_issuance(service_id: str) -> None:
    old_priv = ec.generate_private_key(ec.SECP256R1())
    old_key = SigningKey(kid="k1", private_key=old_priv, public_key=old_priv.public_key())
    new_priv = ec.generate_private_key(ec.SECP256R1())
    new_key = SigningKey(kid="k2", private_key=new_priv, public_key=new_priv.public_key())
    identity = ServiceIdentity(
        service_id=service_id, k8s_namespace="waddlebot", k8s_service_account="svc-process", allowed_scopes=frozenset({SCOPE})
    )

    issuer_before_rotation = ServiceJwtIssuer(
        keys={"k1": old_key}, active_kid="k1", identities={service_id: identity}, audience=AUDIENCE
    )
    old_token = issuer_before_rotation.issue(service_id, SCOPE)

    # Rotate: both keys present, k2 now active for new issuance.
    rotated = ServiceJwtIssuer(
        keys={"k1": old_key, "k2": new_key}, active_kid="k2", identities={service_id: identity}, audience=AUDIENCE
    )
    new_token = rotated.issue(service_id, SCOPE)
    assert pyjwt.get_unverified_header(new_token)["kid"] == "k2"

    verifier = rotated.as_verifier()
    assert verifier.verify(old_token, required_scope=SCOPE)["sub"] == service_id
    assert verifier.verify(new_token, required_scope=SCOPE)["sub"] == service_id

    # Dropping k1 (post-rotation cleanup) makes the old token unverifiable.
    fully_rotated = ServiceJwtIssuer(keys={"k2": new_key}, active_kid="k2", identities={service_id: identity}, audience=AUDIENCE)
    with pytest.raises(UnknownKeyId):
        fully_rotated.as_verifier().verify(old_token, required_scope=SCOPE)


def test_bootstrap_rejects_non_matching_service_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """TokenReview succeeds but for a ServiceAccount identity that isn't
    the one the bootstrap flow expects to see -- caller-side validation
    (matching against `ServiceIdentity.matches_service_account`) must
    reject it even though Kubernetes itself authenticated the token."""
    identity = ServiceIdentity(
        service_id=spiffe_id("alpha", "svc-process"),
        k8s_namespace="waddlebot",
        k8s_service_account="svc-process",
        allowed_scopes=frozenset({SCOPE}),
    )
    assert identity.matches_service_account("waddlebot", "svc-process") is True
    assert identity.matches_service_account("waddlebot", "svc-action") is False
    assert identity.matches_service_account("other-namespace", "svc-process") is False


def test_verify_service_account_token_rejects_unauthenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"status": {"authenticated": False}}

    monkeypatch.setattr("flask_core.service_jwt.requests.post", lambda *a, **k: _FakeResponse())
    monkeypatch.setattr("flask_core.service_jwt._read_local_sa_token", lambda: "hub-api-own-token")
    with pytest.raises(BootstrapRejected):
        verify_service_account_token(
            sa_token="bad-token",
            audience="waddlebot-internal-bootstrap",
            k8s_api_server="https://kubernetes.default.svc",
            ca_cert_path="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
        )


def test_verify_service_account_token_rejects_wrong_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "status": {
                    "authenticated": True,
                    "audiences": ["some-other-audience"],
                    "user": {"username": "system:serviceaccount:waddlebot:svc-process"},
                }
            }

    monkeypatch.setattr("flask_core.service_jwt.requests.post", lambda *a, **k: _FakeResponse())
    monkeypatch.setattr("flask_core.service_jwt._read_local_sa_token", lambda: "hub-api-own-token")
    with pytest.raises(BootstrapRejected):
        verify_service_account_token(
            sa_token="sa-token",
            audience="waddlebot-internal-bootstrap",
            k8s_api_server="https://kubernetes.default.svc",
            ca_cert_path="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
        )


def test_verify_service_account_token_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "status": {
                    "authenticated": True,
                    "audiences": ["waddlebot-internal-bootstrap"],
                    "user": {"username": "system:serviceaccount:waddlebot:svc-process"},
                }
            }

    monkeypatch.setattr("flask_core.service_jwt.requests.post", lambda *a, **k: _FakeResponse())
    monkeypatch.setattr("flask_core.service_jwt._read_local_sa_token", lambda: "hub-api-own-token")
    namespace, service_account = verify_service_account_token(
        sa_token="sa-token",
        audience="waddlebot-internal-bootstrap",
        k8s_api_server="https://kubernetes.default.svc",
        ca_cert_path="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
    )
    assert (namespace, service_account) == ("waddlebot", "svc-process")


class TestEnvWiring:
    """hub_api/app.py's real startup path (security review HIGH finding:

    SERVICE_JWT_ISSUER/VERIFIER were never populated from env/Helm
    values). These exercise `load_identities_from_env` +
    `load_issuer_from_env` together exactly as `app.py::startup` calls
    them, proving the Secret-mounted private key + the `serviceJwt.
    identities` JSON the Helm chart renders actually produce a working
    issuer end-to-end.
    """

    def test_load_identities_from_env_parses_helm_shaped_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "SERVICE_JWT_IDENTITIES",
            '[{"service_id": "svc-process", "k8s_namespace": "waddlebot", '
            '"k8s_service_account": "svc-process", "allowed_scopes": ["identity:ephemeral:mint"]}]',
        )
        identities = load_identities_from_env(env="alpha")
        assert len(identities) == 1
        assert identities[0].service_id == "spiffe://penguintech.io/alpha/svc-process"
        assert identities[0].allowed_scopes == frozenset({"identity:ephemeral:mint"})

    def test_load_identities_from_env_parses_tenant_binding(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv(
            "SERVICE_JWT_IDENTITIES",
            '[{"service_id": "svc-process", "k8s_namespace": "waddlebot", '
            '"k8s_service_account": "svc-process", "allowed_scopes": ["identity:ephemeral:mint"], '
            '"tenant": "system"},'
            ' {"service_id": "svc-action", "k8s_namespace": "waddlebot", '
            '"k8s_service_account": "svc-action", "allowed_scopes": ["egress:connect"]}]',
        )
        with caplog.at_level("WARNING"):
            identities = load_identities_from_env(env="alpha")
        assert [i.tenant for i in identities] == ["system", ""]
        # the unbound identity is called out at startup, loudly, by id (not silently accepted)
        warned = [r for r in caplog.records if "no tenant binding" in r.getMessage()]
        assert len(warned) == 1
        assert warned[0].service_id == "spiffe://penguintech.io/alpha/svc-action"  # type: ignore[attr-defined]

    def test_load_identities_from_env_accepts_full_spiffe_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "SERVICE_JWT_IDENTITIES",
            '[{"service_id": "spiffe://penguintech.io/beta/svc-action", "k8s_namespace": "waddlebot", '
            '"k8s_service_account": "svc-action", "allowed_scopes": ["users:display-name:resolve"]}]',
        )
        identities = load_identities_from_env()
        assert identities[0].service_id == "spiffe://penguintech.io/beta/svc-action"

    def test_load_identities_from_env_empty_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SERVICE_JWT_IDENTITIES", raising=False)
        assert load_identities_from_env() == []

    def test_load_identities_from_env_rejects_malformed_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SERVICE_JWT_IDENTITIES", "not-json")
        with pytest.raises(ServiceJwtError):
            load_identities_from_env()

    def test_load_issuer_from_env_end_to_end(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The full Secret+values.yaml -> working issuer path `app.py` runs at startup."""
        priv = ec.generate_private_key(ec.SECP256R1())
        pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        monkeypatch.setenv("SERVICE_JWT_ACTIVE_KID", "k1")
        monkeypatch.setenv("SERVICE_JWT_PRIVATE_KEY_k1", pem)
        monkeypatch.setenv("SERVICE_JWT_AUDIENCE", "waddlebot-internal")
        monkeypatch.setenv(
            "SERVICE_JWT_IDENTITIES",
            '[{"service_id": "svc-process", "k8s_namespace": "waddlebot", '
            '"k8s_service_account": "svc-process", "allowed_scopes": ["identity:ephemeral:mint"]}]',
        )
        identities = load_identities_from_env(env="alpha")
        issuer = load_issuer_from_env(identities)
        token = issuer.issue("spiffe://penguintech.io/alpha/svc-process", "identity:ephemeral:mint")
        claims = issuer.as_verifier().verify(token, required_scope="identity:ephemeral:mint")
        assert claims["sub"] == "spiffe://penguintech.io/alpha/svc-process"


# ---------------------------------------------------------------------------
# H-2 Phase 0 hardening (RFC 8725): one-alg-per-verifier, no key-material headers,
# kid hygiene, per-algorithm verification metric. Every token below is forged the
# way an attacker would; `test_hardening_control_*` proves the same shape verifies
# when it is NOT hostile, so a rejection can't be passing for the wrong reason.
# ---------------------------------------------------------------------------


def _claims_for(service_id: str, **over: object) -> dict[str, object]:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": "hub-api",
        "aud": AUDIENCE,
        "sub": service_id,
        "scope": SCOPE,
        "iat": now,
        "nbf": now,
        "exp": now + 900,
        "jti": "j-1",
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


def _signed(priv: ec.EllipticCurvePrivateKey, service_id: str, **headers: object) -> str:
    return pyjwt.encode(
        _claims_for(service_id), priv, algorithm="ES256", headers={"kid": "k1", **headers}
    )


def _b64u(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_hardening_control_hand_signed_eddsa_token_verifies(
    issuer: ServiceJwtIssuer, keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey], service_id: str
) -> None:
    priv, _ = keypair
    claims = issuer.as_verifier().verify(_signed(priv, service_id), required_scope=SCOPE)
    assert claims["sub"] == service_id


@pytest.mark.parametrize("alg", ["none", "None", "NONE"])
def test_alg_none_rejected_even_with_a_known_kid(
    issuer: ServiceJwtIssuer, service_id: str, alg: str, metrics_capture
) -> None:
    import json

    head = _b64u(json.dumps({"alg": alg, "kid": "k1"}).encode())
    body = _b64u(json.dumps(_claims_for(service_id)).encode())
    with pytest.raises(InvalidServiceToken):  # NOT UnknownKeyId: the alg is named first
        issuer.as_verifier().verify(f"{head}.{body}.", required_scope=SCOPE)
    assert ("service_eddsa", "none", "alg_none") in metrics_capture.verifications()


def test_hmac_token_keyed_with_the_p256_public_key_is_rejected(
    issuer: ServiceJwtIssuer, keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey], service_id: str, metrics_capture
) -> None:
    """The classic alg-confusion: attacker HMACs with the (public) verification key bytes."""
    import hashlib
    import hmac
    import json

    _, key = keypair
    public_raw = key.public_key.public_bytes(
        encoding=serialization.Encoding.X962, format=serialization.PublicFormat.UncompressedPoint
    )
    head = _b64u(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    body = _b64u(json.dumps(_claims_for(service_id)).encode())
    sig = hmac.new(public_raw, f"{head}.{body}".encode(), hashlib.sha256).digest()
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(f"{head}.{body}.{_b64u(sig)}", required_scope=SCOPE)
    assert ("service_eddsa", "hs256", "alg_mismatch") in metrics_capture.verifications()


def test_rs256_token_rejected(issuer: ServiceJwtIssuer, service_id: str, metrics_capture) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = pyjwt.encode(_claims_for(service_id), rsa_key, algorithm="RS256", headers={"kid": "k1"})
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(token, required_scope=SCOPE)
    assert ("service_eddsa", "rs256", "alg_mismatch") in metrics_capture.verifications()


@pytest.mark.parametrize("param", ["jku", "jwk", "x5u", "x5c", "crit"])
def test_key_material_headers_rejected_despite_a_valid_signature(
    issuer: ServiceJwtIssuer,
    keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey],
    service_id: str,
    param: str,
    metrics_capture,
) -> None:
    priv, _ = keypair
    token = _signed(priv, service_id, **{param: "https://evil.example/jwks.json"})
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(token, required_scope=SCOPE)
    assert ("service_eddsa", "es256", "forbidden_header") in metrics_capture.verifications()


@pytest.mark.parametrize("bad_kid", ["../../etc/passwd", "k1; DROP", "x" * 65, "k1\nINJECTED"])
def test_hostile_kid_never_reaches_the_trust_bundle(
    keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey], service_id: str, bad_kid: str, metrics_capture
) -> None:
    priv, key = keypair
    looked_up: list[str] = []

    class _Spy:
        def get_public_key(self, kid: str):  # type: ignore[no-untyped-def]
            looked_up.append(kid)
            return key.public_key

    from flask_core.service_jwt import ServiceJwtVerifier

    verifier = ServiceJwtVerifier(trust_bundle=_Spy(), audience=AUDIENCE, trusted_issuers=frozenset({"hub-api"}))
    token = _signed(priv, service_id, kid=bad_kid)
    with pytest.raises(InvalidServiceToken):
        verifier.verify(token, required_scope=SCOPE)
    assert looked_up == []
    assert ("service_eddsa", "es256", "bad_kid") in metrics_capture.verifications()


def test_missing_kid_is_still_unknown_key_id(
    issuer: ServiceJwtIssuer, keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey], service_id: str
) -> None:
    priv, _ = keypair
    token = pyjwt.encode(_claims_for(service_id), priv, algorithm="ES256")
    with pytest.raises(UnknownKeyId):
        issuer.as_verifier().verify(token, required_scope=SCOPE)


def test_unknown_kid_error_does_not_echo_the_attacker_kid(
    issuer: ServiceJwtIssuer, keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey], service_id: str
) -> None:
    priv, _ = keypair
    token = _signed(priv, service_id, kid="attacker-chosen-kid-7731")
    with pytest.raises(UnknownKeyId) as exc:
        issuer.as_verifier().verify(token, required_scope=SCOPE)
    assert "attacker-chosen-kid-7731" not in str(exc.value)


def test_metrics_cover_success_and_every_failure_class(
    issuer: ServiceJwtIssuer,
    keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey],
    service_id: str,
    metrics_capture,
) -> None:
    priv, _ = keypair
    verifier = issuer.as_verifier()
    now = int(time.time())

    def attempt(token: str, scope: str = SCOPE) -> None:
        try:
            verifier.verify(token, required_scope=scope)
        except (InvalidServiceToken, UnknownKeyId):
            pass

    attempt(issuer.issue(service_id, SCOPE))
    attempt(issuer.issue(service_id, SCOPE), scope="some:other")
    attempt(pyjwt.encode(_claims_for(service_id, exp=now - 3600, iat=now - 7200, nbf=now - 7200), priv, algorithm="ES256", headers={"kid": "k1"}))
    attempt(pyjwt.encode(_claims_for(service_id, iss="evil"), priv, algorithm="ES256", headers={"kid": "k1"}))
    attempt(pyjwt.encode(_claims_for(service_id, aud="other"), priv, algorithm="ES256", headers={"kid": "k1"}))
    attempt(pyjwt.encode(_claims_for(service_id), priv, algorithm="ES256", headers={"kid": "nope"}))
    attempt(_signed(ec.generate_private_key(ec.SECP256R1()), service_id))  # right kid, wrong key
    attempt("not-a-jwt")

    points = metrics_capture.verifications()
    assert points[("service_eddsa", "es256", "ok")] == 1
    assert points[("service_eddsa", "es256", "scope_denied")] == 1
    assert points[("service_eddsa", "es256", "expired")] == 1
    assert points[("service_eddsa", "es256", "bad_issuer")] == 1
    assert points[("service_eddsa", "es256", "bad_audience")] == 1
    assert points[("service_eddsa", "es256", "unknown_kid")] == 1
    assert points[("service_eddsa", "es256", "bad_signature")] == 1
    assert points[("service_eddsa", "absent", "malformed")] == 1
    assert sum(points.values()) == 8
    assert metrics_capture.duration_counts()[("service_eddsa", "es256")] == 7


def test_rejection_log_has_no_token_or_claims(
    issuer: ServiceJwtIssuer,
    keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey],
    service_id: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    priv, _ = keypair
    token = _signed(priv, service_id, jku="https://evil.example/SECRET-PATH-1234")
    with caplog.at_level(logging.DEBUG, logger="flask_core"), pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(token, required_scope=SCOPE)
    assert "JWT rejected: verifier=service_eddsa reason=forbidden_header alg=es256" in caplog.text
    assert token not in caplog.text
    assert "SECRET-PATH-1234" not in caplog.text
    assert service_id not in caplog.text


# ---------------------------------------------------------------------------
# JWKS / env loader / Quart decorator -- the surfaces the hardened verifier is served through.
# ---------------------------------------------------------------------------


def test_jwks_publishes_every_key_with_its_kid(issuer: ServiceJwtIssuer, keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey]) -> None:
    jwks = issuer.jwks()
    assert [k["kid"] for k in jwks["keys"]] == ["k1"]
    entry = jwks["keys"][0]
    assert (entry["kty"], entry["crv"], entry["alg"], entry["use"]) == ("EC", "P-256", "ES256", "sig")
    assert {"x", "y"} <= set(entry)
    assert "d" not in entry  # never the private scalar


def _pem_private(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _pem_public(key: ec.EllipticCurvePrivateKey) -> str:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


class TestLoadIssuerFromEnv:
    def test_private_and_verify_only_keys_loaded(self, monkeypatch: pytest.MonkeyPatch, service_id: str) -> None:
        active, retired = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
        monkeypatch.setenv("SERVICE_JWT_ACTIVE_KID", "newkid")
        monkeypatch.setenv("SERVICE_JWT_PRIVATE_KEY_newkid", _pem_private(active))
        monkeypatch.setenv("SERVICE_JWT_PUBLIC_KEY_oldkid", _pem_public(retired))
        monkeypatch.setenv("SERVICE_JWT_PUBLIC_KEY_newkid", _pem_public(active))  # redundant half is ignored
        monkeypatch.setenv("SERVICE_JWT_AUDIENCE", "aud-x")
        identity = ServiceIdentity(
            service_id=service_id, k8s_namespace="waddlebot", k8s_service_account="svc-process", allowed_scopes=frozenset({SCOPE})
        )
        loaded = load_issuer_from_env([identity])
        assert set(loaded.keys) == {"newkid", "oldkid"}
        assert loaded.keys["oldkid"].private_key is None
        assert loaded.audience == "aud-x"
        # round trip through the real issuer/verifier; the minted header carries the active kid
        token = loaded.issue(service_id, SCOPE)
        assert pyjwt.get_unverified_header(token)["kid"] == "newkid"
        assert loaded.as_verifier().verify(token, required_scope=SCOPE)["sub"] == service_id

    def test_non_p256_private_key_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cryptography.hazmat.primitives.asymmetric import ed25519

        pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        monkeypatch.setenv("SERVICE_JWT_ACTIVE_KID", "ed")
        monkeypatch.setenv("SERVICE_JWT_PRIVATE_KEY_ed", pem)
        with pytest.raises(ServiceJwtError, match="not an ECDSA P-256"):
            load_issuer_from_env([])

    def test_wrong_curve_private_key_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        p384 = _pem_private(ec.generate_private_key(ec.SECP384R1()))
        monkeypatch.setenv("SERVICE_JWT_ACTIVE_KID", "p384")
        monkeypatch.setenv("SERVICE_JWT_PRIVATE_KEY_p384", p384)
        with pytest.raises(ServiceJwtError, match="not an ECDSA P-256"):
            load_issuer_from_env([])

    def test_ed25519_public_key_refused_unless_legacy_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cryptography.hazmat.primitives.asymmetric import ed25519

        ed_pub = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        monkeypatch.setenv("SERVICE_JWT_ACTIVE_KID", "a")
        monkeypatch.setenv("SERVICE_JWT_PRIVATE_KEY_a", _pem_private(ec.generate_private_key(ec.SECP256R1())))
        monkeypatch.setenv("SERVICE_JWT_PUBLIC_KEY_b", ed_pub)
        with pytest.raises(ServiceJwtError, match="SERVICE_JWT_ACCEPT_LEGACY_EDDSA"):
            load_issuer_from_env([])
        monkeypatch.setenv("SERVICE_JWT_ACCEPT_LEGACY_EDDSA", "true")
        loaded = load_issuer_from_env([])
        assert loaded.accept_legacy_eddsa is True
        assert {k["kty"] for k in loaded.jwks()["keys"]} == {"EC", "OKP"}

    def test_active_kid_without_private_key_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SERVICE_JWT_ACTIVE_KID", "ghost")
        monkeypatch.setenv("SERVICE_JWT_PUBLIC_KEY_ghost", _pem_public(ec.generate_private_key(ec.SECP256R1())))
        with pytest.raises(ServiceJwtError, match="no private key"):
            load_issuer_from_env([])


class TestRequireServiceScopeDecorator:
    """The Quart route decorator in front of the hardened verifier, end to end."""

    @pytest.fixture
    def client(self, issuer: ServiceJwtIssuer):
        from quart import Quart, g, jsonify

        from flask_core.service_jwt import require_service_scope

        app = Quart(__name__)
        app.config["SERVICE_JWT_VERIFIER"] = issuer.as_verifier()

        @app.route("/mint")
        @require_service_scope(SCOPE)
        async def mint():  # type: ignore[no-untyped-def]
            return jsonify({"sub": g.service_claims["sub"]})

        return app.test_client()

    async def test_valid_token_reaches_the_handler(self, client, issuer: ServiceJwtIssuer, service_id: str) -> None:
        response = await client.get("/mint", headers={"Authorization": f"Bearer {issuer.issue(service_id, SCOPE)}"})
        assert response.status_code == 200
        assert (await response.get_json())["sub"] == service_id

    @pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer ", "Bearer not-a-jwt"])
    async def test_missing_or_garbage_credentials_are_401(self, client, header: str | None) -> None:
        response = await client.get("/mint", headers={"Authorization": header} if header is not None else {})
        assert response.status_code == 401

    async def test_hostile_header_token_is_401(
        self, client, keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey], service_id: str
    ) -> None:
        priv, _ = keypair
        token = _signed(priv, service_id, jku="https://evil.example/jwks.json")
        response = await client.get("/mint", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
        assert (await response.get_json()) == {"error": "unauthorized"}  # never says which check failed

    async def test_unknown_kid_is_401(self, client, keypair: tuple[ec.EllipticCurvePrivateKey, SigningKey], service_id: str) -> None:
        priv, _ = keypair
        token = _signed(priv, service_id, kid="not-in-bundle")
        response = await client.get("/mint", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401


def test_issue_with_claims_returns_exactly_what_was_signed(
    issuer: ServiceJwtIssuer, service_id: str
) -> None:
    token, claims = issuer.issue_with_claims(service_id, SCOPE)
    signed = pyjwt.decode(token, options={"verify_signature": False})
    assert claims == signed
    assert {"iss", "aud", "sub", "scope", "iat", "nbf", "exp", "jti"} <= claims.keys()
    assert issuer.as_verifier().verify(token, required_scope=SCOPE) == claims


def test_issue_still_returns_a_bare_token_string(issuer: ServiceJwtIssuer, service_id: str) -> None:
    token = issuer.issue(service_id, SCOPE)
    assert isinstance(token, str) and token.count(".") == 2


def test_issue_with_claims_enforces_the_same_guards(
    issuer: ServiceJwtIssuer, service_id: str
) -> None:
    with pytest.raises(BootstrapRejected):
        issuer.issue_with_claims(service_id, "not:allow-listed")
    with pytest.raises(ServiceJwtError):
        issuer.issue_with_claims(service_id, SCOPE, ttl_seconds=3601)


# ---------------------------------------------------------------------------
# ES256 cut-over: legacy EdDSA window (SERVICE_JWT_ACCEPT_LEGACY_EDDSA), key/alg confusion.
# ---------------------------------------------------------------------------


def _legacy_issuer(service_id: str, *, accept_legacy: bool) -> tuple[ServiceJwtIssuer, object]:
    """Issuer holding an ES256 active key `k1` plus a verify-only legacy Ed25519 key `old`."""
    from cryptography.hazmat.primitives.asymmetric import ed25519

    legacy_priv = ed25519.Ed25519PrivateKey.generate()
    es = ec.generate_private_key(ec.SECP256R1())
    identity = ServiceIdentity(
        service_id=service_id, k8s_namespace="waddlebot", k8s_service_account="svc-process", allowed_scopes=frozenset({SCOPE})
    )
    issuer = ServiceJwtIssuer(
        keys={
            "k1": SigningKey(kid="k1", private_key=es, public_key=es.public_key()),
            "old": SigningKey(kid="old", private_key=None, public_key=legacy_priv.public_key()),
        },
        active_kid="k1",
        identities={service_id: identity},
        accept_legacy_eddsa=accept_legacy,
    )
    return issuer, legacy_priv


def test_issuer_signs_es256_only(issuer: ServiceJwtIssuer, service_id: str) -> None:
    token = issuer.issue(service_id, SCOPE)
    assert pyjwt.get_unverified_header(token)["alg"] == "ES256"
    assert issuer.as_verifier().accept_legacy_eddsa is False


def test_legacy_eddsa_token_rejected_by_default(service_id: str, metrics_capture) -> None:
    issuer, legacy_priv = _legacy_issuer(service_id, accept_legacy=False)
    token = pyjwt.encode(_claims_for(service_id), legacy_priv, algorithm="EdDSA", headers={"kid": "old"})
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(token, required_scope=SCOPE)
    assert ("service_eddsa", "eddsa", "alg_mismatch") in metrics_capture.verifications()


def test_legacy_eddsa_token_accepted_only_with_flag(service_id: str) -> None:
    issuer, legacy_priv = _legacy_issuer(service_id, accept_legacy=True)
    token = pyjwt.encode(_claims_for(service_id), legacy_priv, algorithm="EdDSA", headers={"kid": "old"})
    assert issuer.as_verifier().verify(token, required_scope=SCOPE)["sub"] == service_id
    # ES256 still works in the same window, and new tokens are never EdDSA.
    assert issuer.as_verifier().verify(issuer.issue(service_id, SCOPE), required_scope=SCOPE)


def test_key_alg_confusion_refused_even_in_legacy_window(service_id: str, metrics_capture) -> None:
    """An EdDSA-signed token naming the ES256 kid (or vice versa) never reaches signature check."""
    from cryptography.hazmat.primitives.asymmetric import ed25519

    issuer, _ = _legacy_issuer(service_id, accept_legacy=True)
    attacker = ed25519.Ed25519PrivateKey.generate()
    token = pyjwt.encode(_claims_for(service_id), attacker, algorithm="EdDSA", headers={"kid": "k1"})
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(token, required_scope=SCOPE)
    assert ("service_eddsa", "eddsa", "alg_mismatch") in metrics_capture.verifications()


@pytest.mark.parametrize("alg", ["HS256", "HS512", "RS256", "ES384", "PS256"])
def test_every_other_alg_rejected_even_in_legacy_window(service_id: str, alg: str) -> None:
    import base64
    import json

    issuer, _ = _legacy_issuer(service_id, accept_legacy=True)
    head = base64.urlsafe_b64encode(json.dumps({"alg": alg, "kid": "k1"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(_claims_for(service_id)).encode()).rstrip(b"=").decode()
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(f"{head}.{body}.AAAA", required_scope=SCOPE)


def test_es256_kid_rotation_old_key_still_verifies_then_dropped(service_id: str) -> None:
    old = ec.generate_private_key(ec.SECP256R1())
    new = ec.generate_private_key(ec.SECP256R1())
    identity = ServiceIdentity(
        service_id=service_id, k8s_namespace="waddlebot", k8s_service_account="svc-process", allowed_scopes=frozenset({SCOPE})
    )
    keys = {
        "old": SigningKey(kid="old", private_key=old, public_key=old.public_key()),
        "new": SigningKey(kid="new", private_key=new, public_key=new.public_key()),
    }
    rotating = ServiceJwtIssuer(keys=keys, active_kid="old", identities={service_id: identity})
    old_token = rotating.issue(service_id, SCOPE)
    rotating.active_kid = "new"
    new_token = rotating.issue(service_id, SCOPE)
    assert pyjwt.get_unverified_header(new_token)["kid"] == "new"
    verifier = rotating.as_verifier()
    assert verifier.verify(old_token, required_scope=SCOPE)
    assert verifier.verify(new_token, required_scope=SCOPE)
    assert sorted(k["kid"] for k in rotating.jwks()["keys"]) == ["new", "old"]
    del rotating.keys["old"]  # old key retired: its tokens now fail closed
    with pytest.raises(UnknownKeyId):
        rotating.as_verifier().verify(old_token, required_scope=SCOPE)


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, False), ("false", False), ("true", True), ("TRUE", True), ("0", False)]
)
def test_legacy_flag_env_parse(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool) -> None:
    from flask_core.service_jwt import accept_legacy_eddsa_from_env

    monkeypatch.delenv("SERVICE_JWT_ACCEPT_LEGACY_EDDSA", raising=False)
    if raw is not None:
        monkeypatch.setenv("SERVICE_JWT_ACCEPT_LEGACY_EDDSA", raw)
    assert accept_legacy_eddsa_from_env() is expected


def test_legacy_flag_garbage_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    from flask_core.service_jwt import accept_legacy_eddsa_from_env

    monkeypatch.setenv("SERVICE_JWT_ACCEPT_LEGACY_EDDSA", "yes-please")
    with pytest.raises(ServiceJwtError, match="must be 'true' or 'false'"):
        accept_legacy_eddsa_from_env()
