"""Community-bound machine JWT issuance (HIGH-2): allow-listed integer claims, <=300s TTL."""

from __future__ import annotations

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.service_jwt import (
    BOUND_TOKEN_TTL_SECONDS,
    ServiceIdentity,
    ServiceJwtError,
    ServiceJwtIssuer,
    SigningKey,
    spiffe_id,
)

SERVICE_ID = spiffe_id("alpha", "svc-ingest")


def _issuer() -> ServiceJwtIssuer:
    priv = Ed25519PrivateKey.generate()
    identity = ServiceIdentity(
        service_id=SERVICE_ID,
        k8s_namespace="waddlebot",
        k8s_service_account="svc-ingest",
        allowed_scopes=frozenset({"connections:resolve"}),
        tenant="system",
    )
    return ServiceJwtIssuer(
        keys={"k1": SigningKey(kid="k1", private_key=priv, public_key=priv.public_key())},
        active_kid="k1",
        identities={SERVICE_ID: identity},
    )


def test_issue_bound_token_carries_community_claim() -> None:
    issuer = _issuer()
    token, claims = issuer.issue_with_claims(
        SERVICE_ID, "connections:resolve", ttl_seconds=900, extra_claims={"community": 7, "conn": 3}
    )
    verified = issuer.as_verifier().verify(token, required_scope="connections:resolve")
    assert verified["community"] == 7 and verified["conn"] == 3
    assert claims["community"] == 7
    assert verified["exp"] - verified["iat"] <= BOUND_TOKEN_TTL_SECONDS == 300
    assert jwt.get_unverified_header(token)["alg"] == "EdDSA"


def test_community_zero_sentinel_is_allowed() -> None:
    _, claims = _issuer().issue_with_claims(
        SERVICE_ID, "connections:resolve", ttl_seconds=60, extra_claims={"community": 0}
    )
    assert claims["community"] == 0 and "conn" not in claims


def test_issue_rejects_unknown_extra_claim() -> None:
    with pytest.raises(ValueError):
        _issuer().issue_with_claims(
            SERVICE_ID, "connections:resolve", ttl_seconds=60, extra_claims={"tenant": 1}
        )


@pytest.mark.parametrize("bad", ["7", 7.5, True, None, -1])
def test_issue_rejects_non_integer_or_negative_claim_values(bad: object) -> None:
    with pytest.raises(ValueError):
        _issuer().issue_with_claims(
            SERVICE_ID, "connections:resolve", ttl_seconds=60, extra_claims={"community": bad}  # type: ignore[dict-item]
        )


def test_unbound_issue_keeps_requested_ttl_and_no_extra_claims() -> None:
    _, claims = _issuer().issue_with_claims(SERVICE_ID, "connections:resolve", ttl_seconds=900)
    assert claims["exp"] - claims["iat"] == 900
    assert "community" not in claims and "conn" not in claims


def test_ceiling_still_enforced_with_extra_claims() -> None:
    with pytest.raises(ServiceJwtError):
        _issuer().issue_with_claims(
            SERVICE_ID, "connections:resolve", ttl_seconds=99999, extra_claims={"community": 1}
        )
