"""Service-to-service token debit (`POST /api/v1/internal/communities/<id>/tokens/debit`).

svc-streaming authenticates as ITSELF (community-bound machine JWT, scope `token.billing:debit`)
and names the acting member; hub-api verifies that member's active membership. Real ES256
issuer/verifier and the real `authorize_community` / membership DB -- no mocked guards.
"""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from flask_core.service_jwt import (
    SCOPE_TOKEN_DEBIT,
    ServiceIdentity,
    ServiceJwtIssuer,
    SigningKey,
    spiffe_id,
)
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.token_billing import tokens_bp, tokens_internal_bp
from tests.community_ids import UNKNOWN_PUBLIC_ID, cpath, install_community_ids, public_id
from tests.conftest import make_user_token
from tests.test_v1_token_billing_blueprint import (
    _admin_headers,
    _member_headers,
    _seed_balance,
    _seed_community,
    _seed_member,
    _seed_product,
    _test_config,
)

STREAMING = spiffe_id("alpha", "svc-streaming")
OTHER_SVC = spiffe_id("alpha", "svc-other")
DEBIT_PATH = "/api/v1/internal/communities/{cid}/tokens/debit"


@pytest.fixture
def issuer() -> ServiceJwtIssuer:
    priv = ec.generate_private_key(ec.SECP256R1())
    scopes = frozenset({SCOPE_TOKEN_DEBIT})
    ids = {
        STREAMING: ServiceIdentity(STREAMING, "waddlebot", "svc-streaming", scopes, "system"),
        OTHER_SVC: ServiceIdentity(OTHER_SVC, "waddlebot", "svc-other", scopes, "system"),
    }
    return ServiceJwtIssuer(
        keys={"k1": SigningKey("k1", priv, priv.public_key())}, active_kid="k1", identities=ids
    )


@pytest.fixture
def app(token_billing_db: Any, issuer: ServiceJwtIssuer) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    install_community_ids(quart_app, token_billing_db.dal)
    quart_app.register_blueprint(tokens_bp)
    quart_app.register_blueprint(tokens_internal_bp)
    quart_app.config["dal"] = token_billing_db.dal
    quart_app.config["async_dal"] = token_billing_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    quart_app.config["SERVICE_JWT_ISSUER"] = issuer
    quart_app.config["SERVICE_JWT_VERIFIER"] = issuer.as_verifier()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _debit_url(db: Any, community_id: int) -> str:
    """Internal debit URL addressed by the community's public UUID (never the numeric id)."""
    return DEBIT_PATH.format(cid=public_id(db, community_id))


def _svc(issuer: ServiceJwtIssuer, community_id: int, service: str = STREAMING) -> dict[str, str]:
    tok, _ = issuer.issue_with_claims(
        service, SCOPE_TOKEN_DEBIT, ttl_seconds=300, extra_claims={"community": community_id}
    )
    return {"Authorization": f"Bearer {tok}"}


def _body(actor: int, amount: int = 3) -> dict[str, Any]:
    return {
        "product_key": "ai_call",
        "amount": amount,
        "reason": "transcode",
        "actor_user_id": actor,
    }


def _balance(db: Any, community_id: int) -> int:
    row = db.dal(db.dal.community_token_balances.community_id == community_id).select().first()
    return int(row.balance)


def _seeded(db: Any) -> int:
    product_id = _seed_product(db)
    community_id = _seed_community(db)
    _seed_balance(db, community_id=community_id, product_id=product_id, balance=10)
    return community_id


async def test_service_debit_for_active_member_succeeds(
    client: Any, token_billing_db: Any, issuer: ServiceJwtIssuer
) -> None:
    cid = _seeded(token_billing_db)
    _seed_member(token_billing_db, community_id=cid, user_id=2)
    resp = await client.post(
        _debit_url(token_billing_db, cid), headers=_svc(issuer, cid), json=_body(2)
    )
    assert resp.status_code == 200
    assert (await resp.get_json())["balance_after"] == 7
    assert _balance(token_billing_db, cid) == 7


async def test_service_debit_for_non_member_is_403_and_does_not_mutate(
    client: Any, token_billing_db: Any, issuer: ServiceJwtIssuer
) -> None:
    cid = _seeded(token_billing_db)
    resp = await client.post(
        _debit_url(token_billing_db, cid), headers=_svc(issuer, cid), json=_body(99)
    )
    assert resp.status_code == 403
    assert _balance(token_billing_db, cid) == 10


async def test_service_debit_for_other_tenants_member_is_refused(
    client: Any, token_billing_db: Any, issuer: ServiceJwtIssuer
) -> None:
    """A member of ANOTHER tenant's community is not a member of this one."""
    cid = _seeded(token_billing_db)
    other_tenant = token_billing_db.dal.tenants.insert(slug="other-tenant", is_active=True)
    token_billing_db.dal.commit()
    foreign = _seed_community(token_billing_db, tenant_id=other_tenant)
    _seed_member(token_billing_db, community_id=foreign, user_id=5)
    resp = await client.post(
        _debit_url(token_billing_db, cid), headers=_svc(issuer, cid), json=_body(5)
    )
    assert resp.status_code == 403
    assert _balance(token_billing_db, cid) == 10


async def test_service_debit_token_bound_to_other_community_is_403(
    client: Any, token_billing_db: Any, issuer: ServiceJwtIssuer
) -> None:
    cid = _seeded(token_billing_db)
    _seed_member(token_billing_db, community_id=cid, user_id=2)
    resp = await client.post(
        _debit_url(token_billing_db, cid), headers=_svc(issuer, cid + 1), json=_body(2)
    )
    assert resp.status_code == 403
    assert _balance(token_billing_db, cid) == 10


async def test_service_debit_other_allow_listed_service_is_403(
    client: Any, token_billing_db: Any, issuer: ServiceJwtIssuer
) -> None:
    """Holding the scope is not enough: only svc-streaming may debit on a member's behalf."""
    cid = _seeded(token_billing_db)
    _seed_member(token_billing_db, community_id=cid, user_id=2)
    resp = await client.post(
        _debit_url(token_billing_db, cid), headers=_svc(issuer, cid, OTHER_SVC), json=_body(2)
    )
    assert resp.status_code == 403
    assert _balance(token_billing_db, cid) == 10


async def test_service_debit_unauthenticated_is_401(client: Any, token_billing_db: Any) -> None:
    cid = _seeded(token_billing_db)
    resp = await client.post(_debit_url(token_billing_db, cid), json=_body(2))
    assert resp.status_code == 401


async def test_service_debit_rejects_a_member_user_jwt(client: Any, token_billing_db: Any) -> None:
    """A user JWT (even a member's own) is not a service credential."""
    cid = _seeded(token_billing_db)
    headers = _member_headers(token_billing_db, community_id=cid, user_id=2)
    resp = await client.post(_debit_url(token_billing_db, cid), headers=headers, json=_body(2))
    assert resp.status_code == 401
    assert _balance(token_billing_db, cid) == 10


async def test_service_debit_insufficient_balance_is_402(
    client: Any, token_billing_db: Any, issuer: ServiceJwtIssuer
) -> None:
    cid = _seeded(token_billing_db)
    _seed_member(token_billing_db, community_id=cid, user_id=2)
    resp = await client.post(
        _debit_url(token_billing_db, cid), headers=_svc(issuer, cid), json=_body(2, amount=50)
    )
    assert resp.status_code == 402


# ---- the user-facing route keeps its stricter contract -------------------------------------


async def test_member_direct_debit_is_403(client: Any, token_billing_db: Any) -> None:
    cid = _seeded(token_billing_db)
    headers = _member_headers(token_billing_db, community_id=cid, user_id=2)
    resp = await client.post(
        cpath(token_billing_db, "/api/v1/marketplace/communities/{community_id}/tokens/debit", cid),
        headers=headers,
        json={"product_key": "ai_call", "amount": 1, "reason": "x"},
    )
    assert resp.status_code == 403
    assert _balance(token_billing_db, cid) == 10


async def test_admin_without_token_billing_scope_cannot_debit(
    client: Any, token_billing_db: Any
) -> None:
    """A `community:manage_channels` role without `token.billing:manage` is refused."""
    cid = _seeded(token_billing_db)
    _seed_member(
        token_billing_db,
        community_id=cid,
        user_id=3,
        role="moderator",
        scopes=["community:manage_channels"],
    )
    headers = {"Authorization": f"Bearer {make_user_token(user_id=3)}"}
    resp = await client.post(
        cpath(token_billing_db, "/api/v1/marketplace/communities/{community_id}/tokens/debit", cid),
        headers=headers,
        json={"product_key": "ai_call", "amount": 1, "reason": "x"},
    )
    assert resp.status_code == 403
    assert _balance(token_billing_db, cid) == 10


async def test_admin_with_scope_can_still_debit(client: Any, token_billing_db: Any) -> None:
    cid = _seeded(token_billing_db)
    headers = _admin_headers(token_billing_db, community_id=cid)
    resp = await client.post(
        cpath(token_billing_db, "/api/v1/marketplace/communities/{community_id}/tokens/debit", cid),
        headers=headers,
        json={"product_key": "ai_call", "amount": 1, "reason": "x"},
    )
    assert resp.status_code == 200


async def test_service_debit_numeric_or_malformed_path_is_404(
    client: Any, token_billing_db: Any, issuer: ServiceJwtIssuer
) -> None:
    cid = _seeded(token_billing_db)
    _seed_member(token_billing_db, community_id=cid, user_id=2)
    for segment in (str(cid), "not-a-uuid", f"{public_id(token_billing_db, cid)}x"):
        resp = await client.post(
            DEBIT_PATH.format(cid=segment), headers=_svc(issuer, cid), json=_body(2)
        )
        assert resp.status_code == 404, segment
    assert _balance(token_billing_db, cid) == 10


async def test_service_debit_unknown_uuid_never_debits(
    client: Any, token_billing_db: Any, issuer: ServiceJwtIssuer
) -> None:
    """A UUID no community owns resolves to the sentinel id: the community binding refuses it."""
    cid = _seeded(token_billing_db)
    _seed_member(token_billing_db, community_id=cid, user_id=2)
    resp = await client.post(
        DEBIT_PATH.format(cid=UNKNOWN_PUBLIC_ID), headers=_svc(issuer, cid), json=_body(2)
    )
    assert resp.status_code == 403
    assert _balance(token_billing_db, cid) == 10


async def test_service_debit_uuid_resolves_end_to_end(
    client: Any, token_billing_db: Any, issuer: ServiceJwtIssuer
) -> None:
    """The UUID in the path debits exactly the community whose numeric id the token is bound to."""
    cid = _seeded(token_billing_db)
    _seed_member(token_billing_db, community_id=cid, user_id=2)
    resp = await client.post(
        _debit_url(token_billing_db, cid), headers=_svc(issuer, cid), json=_body(2, amount=4)
    )
    assert resp.status_code == 200
    assert (await resp.get_json())["balance_after"] == 6
    assert _balance(token_billing_db, cid) == 6
