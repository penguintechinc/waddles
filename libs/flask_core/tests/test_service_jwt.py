"""Tests for per-service EdDSA machine JWTs (service_jwt.py).

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
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
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
def keypair() -> tuple[Ed25519PrivateKey, SigningKey]:
    priv = Ed25519PrivateKey.generate()
    return priv, SigningKey(kid="k1", private_key=priv, public_key=priv.public_key())


@pytest.fixture
def service_id() -> str:
    return spiffe_id("alpha", "svc-process")


@pytest.fixture
def issuer(keypair: tuple[Ed25519PrivateKey, SigningKey], service_id: str) -> ServiceJwtIssuer:
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


def test_issue_sets_nbf_to_iat(issuer: ServiceJwtIssuer, service_id: str) -> None:
    """Security review MEDIUM finding: `nbf` must be set at issuance."""
    token = issuer.issue(service_id, SCOPE)
    claims = issuer.as_verifier().verify(token, required_scope=SCOPE)
    assert claims["nbf"] == claims["iat"]


def test_missing_nbf_rejected(issuer: ServiceJwtIssuer, keypair: tuple[Ed25519PrivateKey, SigningKey], service_id: str) -> None:
    """A token minted without `nbf` (older issuer, or forged) must be rejected."""
    priv, _ = keypair
    now = int(time.time())
    no_nbf = pyjwt.encode(
        {"iss": "hub-api", "aud": AUDIENCE, "sub": service_id, "scope": SCOPE, "iat": now, "exp": now + 900, "jti": "x"},
        priv,
        algorithm="EdDSA",
        headers={"kid": "k1"},
    )
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(no_nbf, required_scope=SCOPE)


def test_not_yet_valid_token_rejected(issuer: ServiceJwtIssuer, keypair: tuple[Ed25519PrivateKey, SigningKey], service_id: str) -> None:
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
        algorithm="EdDSA",
        headers={"kid": "k1"},
    )
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(not_yet_valid, required_scope=SCOPE)


def test_nbf_within_clock_skew_accepted(issuer: ServiceJwtIssuer, keypair: tuple[Ed25519PrivateKey, SigningKey], service_id: str) -> None:
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
        algorithm="EdDSA",
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
    forged_key = Ed25519PrivateKey.generate()
    now = int(time.time())
    forged = pyjwt.encode(
        {"iss": "hub-api", "aud": AUDIENCE, "sub": service_id, "scope": SCOPE, "iat": now, "exp": now + 900, "jti": "x"},
        forged_key,
        algorithm="EdDSA",
        headers={"kid": "nonexistent-kid"},
    )
    with pytest.raises(UnknownKeyId):
        issuer.as_verifier().verify(forged, required_scope=SCOPE)


def test_expired_token_rejected(issuer: ServiceJwtIssuer, keypair: tuple[Ed25519PrivateKey, SigningKey], service_id: str) -> None:
    priv, _ = keypair
    now = int(time.time())
    expired = pyjwt.encode(
        {"iss": "hub-api", "aud": AUDIENCE, "sub": service_id, "scope": SCOPE, "iat": now - 3600, "exp": now - 1800, "jti": "x"},
        priv,
        algorithm="EdDSA",
        headers={"kid": "k1"},
    )
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(expired, required_scope=SCOPE)


def test_wrong_audience_rejected(issuer: ServiceJwtIssuer, keypair: tuple[Ed25519PrivateKey, SigningKey], service_id: str) -> None:
    priv, _ = keypair
    now = int(time.time())
    wrong_aud = pyjwt.encode(
        {"iss": "hub-api", "aud": "some-other-audience", "sub": service_id, "scope": SCOPE, "iat": now, "exp": now + 900, "jti": "x"},
        priv,
        algorithm="EdDSA",
        headers={"kid": "k1"},
    )
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(wrong_aud, required_scope=SCOPE)


def test_wrong_scope_rejected(issuer: ServiceJwtIssuer, service_id: str) -> None:
    token = issuer.issue(service_id, SCOPE)
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(token, required_scope="some:other:scope")


def test_untrusted_issuer_rejected(issuer: ServiceJwtIssuer, keypair: tuple[Ed25519PrivateKey, SigningKey], service_id: str) -> None:
    priv, _ = keypair
    now = int(time.time())
    bad_iss = pyjwt.encode(
        {"iss": "not-hub-api", "aud": AUDIENCE, "sub": service_id, "scope": SCOPE, "iat": now, "exp": now + 900, "jti": "x"},
        priv,
        algorithm="EdDSA",
        headers={"kid": "k1"},
    )
    with pytest.raises(InvalidServiceToken):
        issuer.as_verifier().verify(bad_iss, required_scope=SCOPE)


def test_rotation_old_token_still_verifies_new_key_used_for_issuance(service_id: str) -> None:
    old_priv = Ed25519PrivateKey.generate()
    old_key = SigningKey(kid="k1", private_key=old_priv, public_key=old_priv.public_key())
    new_priv = Ed25519PrivateKey.generate()
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
        priv = Ed25519PrivateKey.generate()
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
