"""`blueprints/v1/token_billing.py` -- metered token billing HTTP surface.

Registers `token_products_bp` + `tokens_bp` against `token_billing_db`
(real JWTs, real `authorize_community()` DB-backed authz -- no mocking of
the auth chain).

Fail-first proof (executed, not narrated) for the two REQUIRED regression
classes this group's task brief calls out:

1. `TestBalanceAuthz.test_community_in_different_tenant_is_403` (cross-
   community/cross-tenant balance IDOR): temporarily changed
   `get_balances()`'s route body to skip the `authorize_community(...)`
   call entirely (unconditionally treat the caller as authorized) --
   went red (200 instead of 403, letting a caller read a DIFFERENT
   tenant's community balance by URL substitution); reverted, green
   again.
2. `TestCreditAuthz.test_non_admin_member_cannot_credit` (non-admin credit
   denied): temporarily changed `credit_tokens()`'s route to call
   `authorize_community(..., admin=False)` instead of `admin=True` --
   went red (200 instead of 403, letting a plain member grant themselves
   tokens); reverted, green again.
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.token_billing import token_products_bp, tokens_bp
from config import HubAPIConfig
from tests.conftest import TENANT_SLUG, make_user_token


def _test_config() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8210,
        grpc_port=50210,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        jwt_algorithm="HS256",
        default_tenant_slug="default",
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8210",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
    )


@pytest.fixture
def app(token_billing_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(token_products_bp)
    quart_app.register_blueprint(tokens_bp)
    quart_app.config["dal"] = token_billing_db.dal
    quart_app.config["async_dal"] = token_billing_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _tenant_id(db: Any, *, slug: str = TENANT_SLUG) -> int:
    row = db.dal(db.dal.tenants.slug == slug).select().first()
    return int(row.id)


def _seed_community(db: Any, *, tenant_id: int | None = None) -> int:
    tid = tenant_id if tenant_id is not None else _tenant_id(db)
    community_id: int = db.dal.communities.insert(name="acme-community", tenant_id=tid)
    db.dal.commit()
    return community_id


def _seed_member(
    db: Any,
    *,
    community_id: int,
    user_id: int,
    role: str = "member",
    scopes: list[str] | None = None,
) -> None:
    """Mirrors `tests/test_v1_music_blueprint.py`'s own helper exactly.

    `scopes=None` -> plain member, no `community_roles` row, no admin
    scopes (`resolve_community_membership_scoped`'s `is_admin` stays
    `False`). `scopes=[...]` -> a real `community_roles` row is created
    and linked, so `is_admin` is `True` iff it intersects `ADMIN_SCOPES`.
    """
    role_id = None
    if scopes is not None:
        role_id = db.dal.community_roles.insert(
            community_id=community_id, name=role, base_claims={"scopes": scopes}
        )
    db.dal.community_members.insert(
        community_id=community_id,
        user_id=str(user_id),
        role=role,
        community_role_id=role_id,
        is_active=True,
    )
    db.dal.commit()


def _seed_product(
    db: Any, *, key: str = "ai_call", name: str = "AI Routing Call", active: bool = True
) -> int:
    product_id: int = db.dal.token_products.insert(
        key=key, name=name, unit="call", price_cents=100, tokens_granted=10, active=active
    )
    db.dal.commit()
    return product_id


def _seed_balance(db: Any, *, community_id: int, product_id: int, balance: int) -> None:
    db.dal.community_token_balances.insert(
        community_id=community_id, product_id=product_id, balance=balance
    )
    db.dal.commit()


def _admin_headers(db: Any, *, community_id: int, user_id: int = 1) -> dict[str, str]:
    _seed_member(
        db,
        community_id=community_id,
        user_id=user_id,
        role="community-admin",
        scopes=["community:manage_members"],
    )
    return {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}


def _member_headers(db: Any, *, community_id: int, user_id: int = 2) -> dict[str, str]:
    _seed_member(db, community_id=community_id, user_id=user_id, role="member")
    return {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}


class TestAuthBypass:
    async def test_missing_token_catalog_is_401(self, client: Any) -> None:
        response = await client.get("/api/v1/marketplace/token-products")
        assert response.status_code == 401

    async def test_missing_token_balances_is_401(self, client: Any, token_billing_db: Any) -> None:
        community_id = _seed_community(token_billing_db)
        response = await client.get(
            f"/api/v1/marketplace/communities/{community_id}/tokens/balances"
        )
        assert response.status_code == 401


class TestCatalog:
    async def test_lists_only_active_products(self, client: Any, token_billing_db: Any) -> None:
        _seed_product(token_billing_db, key="active_one", active=True)
        _seed_product(token_billing_db, key="inactive_one", active=False)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}

        response = await client.get("/api/v1/marketplace/token-products", headers=headers)

        assert response.status_code == 200
        body = await response.get_json()
        keys = {p["key"] for p in body["products"]}
        assert keys == {"active_one"}


class TestBalanceAuthz:
    """Balance reads -- active membership required, cross-community/cross-tenant IDOR denied."""

    async def test_non_member_is_403(self, client: Any, token_billing_db: Any) -> None:
        community_id = _seed_community(token_billing_db)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=99)}"}

        response = await client.get(
            f"/api/v1/marketplace/communities/{community_id}/tokens/balances", headers=headers
        )

        assert response.status_code == 403

    async def test_member_can_read_own_community_balances(
        self, client: Any, token_billing_db: Any
    ) -> None:
        product_id = _seed_product(token_billing_db)
        community_id = _seed_community(token_billing_db)
        _seed_balance(token_billing_db, community_id=community_id, product_id=product_id, balance=7)
        headers = _member_headers(token_billing_db, community_id=community_id)

        response = await client.get(
            f"/api/v1/marketplace/communities/{community_id}/tokens/balances", headers=headers
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["balances"][0]["balance"] == 7

    async def test_member_of_community_a_cannot_read_community_b(
        self, client: Any, token_billing_db: Any
    ) -> None:
        """Cross-COMMUNITY IDOR: membership in A must not authorize reading B's balance."""
        community_a = _seed_community(token_billing_db)
        community_b = _seed_community(token_billing_db)
        headers = _member_headers(token_billing_db, community_id=community_a, user_id=5)

        response = await client.get(
            f"/api/v1/marketplace/communities/{community_b}/tokens/balances", headers=headers
        )

        assert response.status_code == 403

    async def test_community_in_different_tenant_is_403(
        self, client: Any, token_billing_db: Any
    ) -> None:
        """Cross-TENANT IDOR: admin-scoped membership for another tenant's community.

        Must not authorize access -- `_community_belongs_to_tenant` runs
        before the membership lookup (see `services/community_authz.py`).
        """
        other_tenant_id = token_billing_db.dal.tenants.insert(slug="other-tenant", is_active=True)
        token_billing_db.dal.commit()
        community_id = _seed_community(token_billing_db, tenant_id=other_tenant_id)
        _seed_member(
            token_billing_db,
            community_id=community_id,
            user_id=1,
            role="community-admin",
            scopes=["community:manage_members"],
        )
        # Caller's JWT carries the DEFAULT tenant, not `other-tenant`.
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1, tenant=TENANT_SLUG)}"}

        response = await client.get(
            f"/api/v1/marketplace/communities/{community_id}/tokens/balances", headers=headers
        )

        assert response.status_code == 403


class TestCreditAuthz:
    """Credit is a community-admin action -- financial/grant operation."""

    async def test_non_admin_member_cannot_credit(self, client: Any, token_billing_db: Any) -> None:
        _seed_product(token_billing_db)
        community_id = _seed_community(token_billing_db)
        headers = _member_headers(token_billing_db, community_id=community_id)

        response = await client.post(
            f"/api/v1/marketplace/communities/{community_id}/tokens/credit",
            headers=headers,
            json={"product_key": "ai_call", "amount": 10, "reason": "self_grant_attempt"},
        )

        assert response.status_code == 403

    async def test_non_member_cannot_credit(self, client: Any, token_billing_db: Any) -> None:
        _seed_product(token_billing_db)
        community_id = _seed_community(token_billing_db)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=99)}"}

        response = await client.post(
            f"/api/v1/marketplace/communities/{community_id}/tokens/credit",
            headers=headers,
            json={"product_key": "ai_call", "amount": 10, "reason": "x"},
        )

        assert response.status_code == 403

    async def test_admin_can_credit(self, client: Any, token_billing_db: Any) -> None:
        _seed_product(token_billing_db)
        community_id = _seed_community(token_billing_db)
        headers = _admin_headers(token_billing_db, community_id=community_id)

        response = await client.post(
            f"/api/v1/marketplace/communities/{community_id}/tokens/credit",
            headers=headers,
            json={
                "product_key": "ai_call",
                "amount": 10,
                "reason": "purchase:pack_a",
                "ref": "order-1",
            },
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["balance_after"] == 10
        assert body["transaction_id"] is not None

    async def test_tenant_admin_bypasses(self, client: Any, token_billing_db: Any) -> None:
        _seed_product(token_billing_db)
        community_id = _seed_community(token_billing_db)
        token_billing_db.dal.tenant_admins.insert(
            tenant_id=_tenant_id(token_billing_db), user_id=42
        )
        token_billing_db.dal.commit()
        headers = {"Authorization": f"Bearer {make_user_token(user_id=42)}"}

        response = await client.post(
            f"/api/v1/marketplace/communities/{community_id}/tokens/credit",
            headers=headers,
            json={"product_key": "ai_call", "amount": 5, "reason": "x"},
        )

        assert response.status_code == 200

    async def test_unknown_product_is_404(self, client: Any, token_billing_db: Any) -> None:
        community_id = _seed_community(token_billing_db)
        headers = _admin_headers(token_billing_db, community_id=community_id)

        response = await client.post(
            f"/api/v1/marketplace/communities/{community_id}/tokens/credit",
            headers=headers,
            json={"product_key": "does_not_exist", "amount": 5, "reason": "x"},
        )

        assert response.status_code == 404


class TestDebitAuthz:
    """Debit is a community-member action -- own-community consumption."""

    async def test_non_member_cannot_debit(self, client: Any, token_billing_db: Any) -> None:
        product_id = _seed_product(token_billing_db)
        community_id = _seed_community(token_billing_db)
        _seed_balance(
            token_billing_db, community_id=community_id, product_id=product_id, balance=10
        )
        headers = {"Authorization": f"Bearer {make_user_token(user_id=99)}"}

        response = await client.post(
            f"/api/v1/marketplace/communities/{community_id}/tokens/debit",
            headers=headers,
            json={"product_key": "ai_call", "amount": 1, "reason": "x"},
        )

        assert response.status_code == 403

    async def test_member_can_debit_own_community(self, client: Any, token_billing_db: Any) -> None:
        product_id = _seed_product(token_billing_db)
        community_id = _seed_community(token_billing_db)
        _seed_balance(
            token_billing_db, community_id=community_id, product_id=product_id, balance=10
        )
        headers = _member_headers(token_billing_db, community_id=community_id)

        response = await client.post(
            f"/api/v1/marketplace/communities/{community_id}/tokens/debit",
            headers=headers,
            json={
                "product_key": "ai_call",
                "amount": 3,
                "reason": "ai_route:gpt-4o",
                "ref": "req-1",
            },
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["balance_after"] == 7

    async def test_insufficient_balance_is_402_with_upgrade_path(
        self, client: Any, token_billing_db: Any
    ) -> None:
        product_id = _seed_product(token_billing_db)
        community_id = _seed_community(token_billing_db)
        _seed_balance(token_billing_db, community_id=community_id, product_id=product_id, balance=1)
        headers = _member_headers(token_billing_db, community_id=community_id)

        response = await client.post(
            f"/api/v1/marketplace/communities/{community_id}/tokens/debit",
            headers=headers,
            json={"product_key": "ai_call", "amount": 5, "reason": "x"},
        )

        assert response.status_code == 402
        body = await response.get_json()
        assert body["blocked_reason"] == "insufficient_balance"
        assert body["upgrade_path"] == "/api/v1/marketplace/token-products"

    async def test_never_credited_is_402(self, client: Any, token_billing_db: Any) -> None:
        _seed_product(token_billing_db)
        community_id = _seed_community(token_billing_db)
        headers = _member_headers(token_billing_db, community_id=community_id)

        response = await client.post(
            f"/api/v1/marketplace/communities/{community_id}/tokens/debit",
            headers=headers,
            json={"product_key": "ai_call", "amount": 1, "reason": "x"},
        )

        assert response.status_code == 402
        body = await response.get_json()
        assert body["blocked_reason"] == "no_balance"

    async def test_unknown_product_is_404(self, client: Any, token_billing_db: Any) -> None:
        community_id = _seed_community(token_billing_db)
        headers = _member_headers(token_billing_db, community_id=community_id)

        response = await client.post(
            f"/api/v1/marketplace/communities/{community_id}/tokens/debit",
            headers=headers,
            json={"product_key": "does_not_exist", "amount": 1, "reason": "x"},
        )

        assert response.status_code == 404


class TestTransactionsAuthz:
    async def test_non_admin_member_cannot_read_history(
        self, client: Any, token_billing_db: Any
    ) -> None:
        community_id = _seed_community(token_billing_db)
        headers = _member_headers(token_billing_db, community_id=community_id)

        response = await client.get(
            f"/api/v1/marketplace/communities/{community_id}/tokens/transactions", headers=headers
        )

        assert response.status_code == 403

    async def test_admin_can_read_history(self, client: Any, token_billing_db: Any) -> None:
        product_id = _seed_product(token_billing_db)
        community_id = _seed_community(token_billing_db)
        _seed_balance(
            token_billing_db, community_id=community_id, product_id=product_id, balance=10
        )
        headers = _admin_headers(token_billing_db, community_id=community_id)

        # Generate one ledger row via a real debit through the API itself.
        member_headers = _member_headers(token_billing_db, community_id=community_id, user_id=7)
        await client.post(
            f"/api/v1/marketplace/communities/{community_id}/tokens/debit",
            headers=member_headers,
            json={"product_key": "ai_call", "amount": 1, "reason": "x"},
        )

        response = await client.get(
            f"/api/v1/marketplace/communities/{community_id}/tokens/transactions", headers=headers
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["total"] == 1
        assert body["transactions"][0]["delta"] == -1

    async def test_invalid_limit_is_400(self, client: Any, token_billing_db: Any) -> None:
        community_id = _seed_community(token_billing_db)
        headers = _admin_headers(token_billing_db, community_id=community_id)

        response = await client.get(
            f"/api/v1/marketplace/communities/{community_id}/tokens/transactions?limit=0",
            headers=headers,
        )

        assert response.status_code == 400

    async def test_non_integer_limit_is_400(self, client: Any, token_billing_db: Any) -> None:
        community_id = _seed_community(token_billing_db)
        headers = _admin_headers(token_billing_db, community_id=community_id)

        response = await client.get(
            f"/api/v1/marketplace/communities/{community_id}/tokens/transactions?limit=abc",
            headers=headers,
        )

        assert response.status_code == 400

    async def test_negative_offset_is_400(self, client: Any, token_billing_db: Any) -> None:
        community_id = _seed_community(token_billing_db)
        headers = _admin_headers(token_billing_db, community_id=community_id)

        response = await client.get(
            f"/api/v1/marketplace/communities/{community_id}/tokens/transactions?offset=-1",
            headers=headers,
        )

        assert response.status_code == 400

    async def test_invalid_start_date_is_400(self, client: Any, token_billing_db: Any) -> None:
        community_id = _seed_community(token_billing_db)
        headers = _admin_headers(token_billing_db, community_id=community_id)

        response = await client.get(
            f"/api/v1/marketplace/communities/{community_id}/tokens/transactions?start=not-a-date",
            headers=headers,
        )

        assert response.status_code == 400

    async def test_date_range_filter_narrows_results(
        self, client: Any, token_billing_db: Any
    ) -> None:
        product_id = _seed_product(token_billing_db)
        community_id = _seed_community(token_billing_db)
        _seed_balance(
            token_billing_db, community_id=community_id, product_id=product_id, balance=10
        )
        member_headers = _member_headers(token_billing_db, community_id=community_id, user_id=7)
        await client.post(
            f"/api/v1/marketplace/communities/{community_id}/tokens/debit",
            headers=member_headers,
            json={"product_key": "ai_call", "amount": 1, "reason": "x"},
        )
        headers = _admin_headers(token_billing_db, community_id=community_id)

        # A `start` far in the future excludes the transaction just written.
        response = await client.get(
            f"/api/v1/marketplace/communities/{community_id}/tokens/transactions"
            "?start=2099-01-01T00:00:00",
            headers=headers,
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["total"] == 0
