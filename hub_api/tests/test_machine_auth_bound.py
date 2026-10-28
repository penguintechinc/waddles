"""``machine_authenticated(bind_community=True)`` + SVID path against a live Quart app.

Real Ed25519 issuer/verifier; no mocking of the guard. Covers community/connection binding,
the wrong-tenant behaviour, the default-off SPIFFE peer-identity path and fail-closed 503.
"""

from __future__ import annotations

from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.service_jwt import (
    ServiceIdentity,
    ServiceJwtIssuer,
    SigningKey,
    spiffe_id,
)
from quart import Quart

from services.machine_auth import machine_authenticated

SCOPE = "connections:resolve"
INGEST = spiffe_id("alpha", "svc-ingest")
TENANTED = spiffe_id("alpha", "svc-tenanted")
LISTER = spiffe_id("alpha", "svc-lister")
HEADER = "X-Peer-Spiffe-Id"


def _issuer() -> ServiceJwtIssuer:
    priv = Ed25519PrivateKey.generate()
    ids = {
        INGEST: ServiceIdentity(INGEST, "waddlebot", "svc-ingest", frozenset({SCOPE}), "system"),
        TENANTED: ServiceIdentity(
            TENANTED, "waddlebot", "svc-tenanted", frozenset({SCOPE}), "acme"
        ),
        LISTER: ServiceIdentity(
            LISTER, "waddlebot", "svc-lister", frozenset({"connections:list"}), "system"
        ),
    }
    return ServiceJwtIssuer(
        keys={"k1": SigningKey("k1", priv, priv.public_key())}, active_kid="k1", identities=ids
    )


def _guard(**kw: Any) -> Any:
    return machine_authenticated(
        SCOPE,
        unconfigured_event="t.unconfigured",
        rejected_event="t.rejected",
        wrong_tenant_event="t.wrong_tenant",
        **kw,
    )


def _build(*, trust: bool = False, configured: bool = True) -> tuple[Quart, ServiceJwtIssuer]:
    app = Quart(__name__)
    issuer = _issuer()
    if configured:
        app.config["SERVICE_JWT_ISSUER"] = issuer
        app.config["SERVICE_JWT_VERIFIER"] = issuer.as_verifier()
    app.config["SPIFFE_PEER_TRUST"] = trust
    app.config["SPIFFE_PEER_ID_HEADER"] = HEADER

    @app.route("/c/<int:community_id>/conn/<int:connection_id>")
    @_guard(bind_community=True)
    async def bound(community_id: int, connection_id: int) -> Any:
        return {"ok": True, "community": community_id}

    @app.route("/c/<int:community_id>")
    @_guard(bind_community=True)
    async def bound_no_conn(community_id: int) -> Any:
        return {"ok": True}

    @app.route("/open")
    @_guard()
    async def unbound() -> Any:
        return {"ok": True}

    @app.route("/nobind")
    @_guard(bind_community=True)
    async def nobind() -> Any:
        return {"ok": True}

    return app, issuer


def _token(issuer: ServiceJwtIssuer, service: str = INGEST, **extra: int) -> dict[str, str]:
    tok, _ = issuer.issue_with_claims(service, SCOPE, ttl_seconds=300, extra_claims=extra or None)
    return {"Authorization": f"Bearer {tok}"}


async def test_bound_token_matching_community_passes() -> None:
    app, issuer = _build()
    resp = await app.test_client().get("/c/7/conn/3", headers=_token(issuer, community=7, conn=3))
    assert resp.status_code == 200


async def test_token_bound_to_a_cannot_resolve_b() -> None:
    app, issuer = _build()
    resp = await app.test_client().get("/c/8/conn/3", headers=_token(issuer, community=7))
    assert resp.status_code == 403
    assert await resp.get_json() == {"error": "forbidden"}


async def test_conn_claim_mismatch_is_403() -> None:
    app, issuer = _build()
    resp = await app.test_client().get("/c/7/conn/4", headers=_token(issuer, community=7, conn=3))
    assert resp.status_code == 403


async def test_missing_community_claim_on_bound_route_is_401() -> None:
    app, issuer = _build()
    resp = await app.test_client().get("/c/7/conn/3", headers=_token(issuer))
    assert resp.status_code == 401
    assert await resp.get_json() == {"error": "unauthorized"}


async def test_non_integer_community_claim_is_401() -> None:
    app, issuer = _build()
    key = issuer.keys["k1"].private_key
    claims = {
        "iss": "hub-api",
        "aud": issuer.audience,
        "sub": INGEST,
        "scope": SCOPE,
        "iat": 2**31,
        "nbf": 0,
        "exp": 2**32,
        "jti": "x",
        "tenant": "system",
        "community": "abc",
    }
    tok = jwt.encode(claims, key, algorithm="EdDSA", headers={"kid": "k1"})
    resp = await app.test_client().get("/c/7/conn/3", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 401


async def test_wrong_tenant_still_403() -> None:
    app, issuer = _build()
    resp = await app.test_client().get("/c/7/conn/3", headers=_token(issuer, TENANTED, community=7))
    assert resp.status_code == 403


async def test_unbound_route_ignores_community_claim() -> None:
    app, issuer = _build()
    resp = await app.test_client().get("/open", headers=_token(issuer))
    assert resp.status_code == 200


async def test_bound_route_without_community_param_fails_closed() -> None:
    app, issuer = _build()
    resp = await app.test_client().get("/nobind", headers=_token(issuer, community=7))
    assert resp.status_code == 403


async def test_community_only_route_with_conn_claim_present_is_403() -> None:
    """A conn-bound token on a route that has no connection_id cannot be matched -> 403."""
    app, issuer = _build()
    resp = await app.test_client().get("/c/7", headers=_token(issuer, community=7, conn=3))
    assert resp.status_code == 403


async def test_unconfigured_verifier_is_503() -> None:
    app, _ = _build(configured=False)
    resp = await app.test_client().get("/c/7/conn/3", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 503


async def test_spiffe_header_ignored_when_peer_trust_off() -> None:
    app, _ = _build(trust=False)
    resp = await app.test_client().get(
        "/c/7/conn/3", headers={HEADER: INGEST, "X-Connection-Community": "7"}
    )
    assert resp.status_code == 401


async def test_spiffe_identity_accepted_when_peer_trust_on() -> None:
    app, _ = _build(trust=True)
    client = app.test_client()
    ok = await client.get("/c/7/conn/3", headers={HEADER: INGEST, "X-Connection-Community": "7"})
    assert ok.status_code == 200
    mismatch = await client.get(
        "/c/8/conn/3", headers={HEADER: INGEST, "X-Connection-Community": "7"}
    )
    assert mismatch.status_code == 403
    no_param = await client.get("/c/7/conn/3", headers={HEADER: INGEST})
    assert no_param.status_code == 401
    junk_param = await client.get(
        "/c/7/conn/3", headers={HEADER: INGEST, "X-Connection-Community": "x"}
    )
    assert junk_param.status_code == 401
    unbound = await client.get("/open", headers={HEADER: INGEST})
    assert unbound.status_code == 200


@pytest.mark.parametrize(
    "peer",
    [
        "spiffe://penguintech.io/alpha/svc-unknown",
        "spiffe://evil.example/alpha/svc-ingest",
        "not-a-spiffe-id",
        "",
        "spiffe://penguintech.io/alpha/svc-ingest/../x",
        LISTER,  # allow-listed identity but not for this scope
    ],
)
async def test_unknown_or_malformed_svid_is_401(peer: str) -> None:
    app, _ = _build(trust=True)
    resp = await app.test_client().get(
        "/c/7/conn/3", headers={HEADER: peer, "X-Connection-Community": "7"}
    )
    assert resp.status_code == 401


async def test_svid_from_non_system_tenant_is_403() -> None:
    app, _ = _build(trust=True)
    resp = await app.test_client().get(
        "/c/7/conn/3", headers={HEADER: TENANTED, "X-Connection-Community": "7"}
    )
    assert resp.status_code == 403


async def test_svid_with_unconfigured_issuer_is_503() -> None:
    app, _ = _build(trust=True, configured=False)
    resp = await app.test_client().get(
        "/c/7/conn/3", headers={HEADER: INGEST, "X-Connection-Community": "7"}
    )
    assert resp.status_code == 503


async def test_svid_header_name_unset_is_ignored() -> None:
    app, _ = _build(trust=True)
    app.config["SPIFFE_PEER_ID_HEADER"] = ""
    resp = await app.test_client().get("/open", headers={HEADER: INGEST})
    assert resp.status_code == 401


async def test_bearer_takes_precedence_over_peer_header() -> None:
    app, issuer = _build(trust=True)
    resp = await app.test_client().get(
        "/c/8/conn/3",
        headers={**_token(issuer, community=7), HEADER: INGEST, "X-Connection-Community": "8"},
    )
    assert resp.status_code == 403
