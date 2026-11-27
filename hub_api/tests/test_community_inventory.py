"""`blueprints/v1/community_inventory.py` -- admin item CRUD + member checkout/checkin.

Also proves the checkout/checkin bug fix documented in
`services/community_inventory.py`'s module docstring: a real
`inventory_checkouts` row is created on checkout (Node's equivalent
never wrote one -- see that docstring for the parameter-order bug this
port fixes instead of reproducing).
"""

from __future__ import annotations

import json as json_module
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.community_inventory import inventory_bp
from services import community_inventory as inventory_svc
from tests.community_ids import UNKNOWN_PUBLIC_ID, install_community_ids, public_id

MEMBER, ADMIN, BANNED, SOFT_BANNED, STRANGER = 1, 2, 3, 4, 5
OTHER_TENANT = "other-corp"


@pytest.fixture
def community_db(tenant_db: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Overrides conftest's: real membership/role/ban tables + seeded users for the authz gates.

    Users: 1 plain member, 2 community owner (admin kind), 3 banned, 4 soft-banned, 5 not a
    member. Also a second tenant with its own community (cross-tenant 404s).
    """
    from services.community_common import ensure_community_tables
    from services.community_reputation_service import _ensure_reputation_tables
    from services.schema import bind_auth_tables

    dal = tenant_db
    bind_auth_tables(dal, migrate=True)
    ensure_community_tables(dal, migrate=True)
    _ensure_reputation_tables(dal, migrate=True)

    tenant_id = dal(dal.tenants.slug == "acme-corp").select().first().id
    dal.tenants.insert(slug=OTHER_TENANT, is_active=True)
    community_id = dal.communities.insert(name="test-community", tenant_id=tenant_id)
    for n in range(1, 6):
        dal.hub_users.insert(username=f"user{n}")
    owner_role = dal.community_roles.insert(
        community_id=community_id,
        name="community-owner",
        is_system=True,
        role_kind=1,
        base_claims={"scopes": []},
    )
    # The banned member stays is_active=True so ONLY the BANNED membership_state can deny them.
    for uid, role, state, uuid_, role_id in [
        (MEMBER, "member", 1, "uuid-member", None),
        (ADMIN, "community-owner", 1, "uuid-admin", owner_role),
        (BANNED, "member", 2, "uuid-banned", None),
        (SOFT_BANNED, "member", 1, "uuid-soft", None),
    ]:
        dal.community_members.insert(
            community_id=community_id,
            user_id=str(uid),
            role=role,
            is_active=True,
            membership_state=state,
            user_uuid=uuid_,
            community_role_id=role_id,
        )
    dal.bundle_reputation_soft_bans.insert(
        tenant_id=tenant_id,
        community_id=community_id,
        user_uuid="uuid-soft",
        reason_code=1,
        set_by="admin",
    )
    dal.commit()
    return dal, community_id


@pytest.fixture
def app(community_db: Any) -> Quart:
    dal, _ = community_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    install_community_ids(quart_app, dal)
    quart_app.register_blueprint(inventory_bp)
    quart_app.config["dal"] = dal
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Default the `community.inventory` two-gate Feature flag ON for every test in this file."""
    import blueprints.v1.community_inventory as inventory_module

    monkeypatch.setattr(inventory_module, "feature_enabled", AsyncMock(return_value=True))


def _join_other(dal: Any, other: int, *, admin: bool) -> None:
    """Make users 1 (member) and 2 (admin) active in `other` so only SCOPE can reject them."""
    role = dal.community_roles.insert(
        community_id=other,
        name="community-owner",
        is_system=True,
        role_kind=1,
        base_claims={"scopes": []},
    )
    for uid, kind, role_id in (("1", "member", None), ("2", "community-owner", role)):
        if kind == "community-owner" and not admin:
            continue
        dal.community_members.insert(
            community_id=other,
            user_id=uid,
            role=kind,
            is_active=True,
            user_uuid=f"uuid-other-{uid}",
            community_role_id=role_id,
        )
    dal.commit()


def _nid(dal: Any, public_uuid: str) -> int:
    """Numeric `inventory_items.id` behind a response's public UUID (for direct DB asserts)."""
    return int(dal(dal.inventory_items.public_id == public_uuid).select().first().id)


async def _post_json(
    client: Any, path: str, headers: dict[str, str], payload: dict[str, Any]
) -> Any:
    return await client.post(
        path,
        headers={**headers, "Content-Type": "application/json"},
        data=json_module.dumps(payload),
    )


class TestScopeAndTenant:
    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items",
            headers=auth_headers(scope="community.inventory:use", user_id="1"),
        )
        assert response.status_code == 403

    async def test_unknown_community_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            f"/api/v1/admin/{UNKNOWN_PUBLIC_ID}/inventory/items",
            headers=auth_headers(scope="community.inventory:read", user_id="2"),
        )
        assert response.status_code == 404


class TestUnknownCommunity404Sweep:
    """One 404 case per remaining route -- coverage.py tracks the `_tenant_ok`.

    check per-function, so each route needs its own hit (see the matching
    sweep in `test_community_interaction.py`).
    """

    @pytest.mark.parametrize(
        "method,path_suffix,scope",
        [
            ("POST", "inventory/items", "community.inventory:write"),
            (
                "PUT",
                "inventory/items/00000000-0000-4000-8000-00000000dead",
                "community.inventory:write",
            ),
            (
                "DELETE",
                "inventory/items/00000000-0000-4000-8000-00000000dead",
                "community.inventory:write",
            ),
            (
                "POST",
                "inventory/items/00000000-0000-4000-8000-00000000dead/stock/add",
                "community.inventory:write",
            ),
            (
                "POST",
                "inventory/items/00000000-0000-4000-8000-00000000dead/stock/remove",
                "community.inventory:write",
            ),
            ("GET", "inventory/checkouts", "community.inventory:read"),
            ("GET", "inventory/summary", "community.inventory:read"),
            ("GET", "inventory/log", "community.inventory:read"),
            ("GET", "inventory/available", "community.inventory:use"),
            ("POST", "inventory/checkout", "community.inventory:use"),
            ("POST", "inventory/checkin", "community.inventory:use"),
            ("GET", "inventory/my-items", "community.inventory:use"),
        ],
    )
    async def test_route_404s_on_unknown_community(
        self, client: Any, auth_headers: Any, method: str, path_suffix: str, scope: str
    ) -> None:
        response = await client.open(
            f"/api/v1/admin/{UNKNOWN_PUBLIC_ID}/{path_suffix}",
            method=method,
            headers=auth_headers(scope=scope, user_id="1"),
        )
        assert response.status_code == 404


class TestItemCrud:
    async def test_create_item_requires_name(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items",
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"name": "", "quantity": 5},
        )
        assert response.status_code == 400

    async def test_create_list_update_delete_item(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        write_headers = auth_headers(scope="community.inventory:write", user_id="2")

        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items",
            write_headers,
            {"name": "Green Screen", "category": "gear", "quantity": 3},
        )
        assert create_resp.status_code == 201
        item = (await create_resp.get_json())["item"]
        assert item["quantity"] == 3
        assert item["available_quantity"] == 3
        item_id = item["id"]

        list_resp = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items",
            headers=auth_headers(scope="community.inventory:read", user_id="2"),
        )
        assert len((await list_resp.get_json())["items"]) == 1

        update_resp = await client.put(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items/{item_id}",
            headers={**write_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"name": "Green Screen Mk2"}),
        )
        assert update_resp.status_code == 200
        assert (await update_resp.get_json())["item"]["name"] == "Green Screen Mk2"

        delete_resp = await client.delete(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items/{item_id}",
            headers=write_headers,
        )
        assert delete_resp.status_code == 200

        after_delete = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items",
            headers=auth_headers(scope="community.inventory:read", user_id="2"),
        )
        assert len((await after_delete.get_json())["items"]) == 0


def _fake_stock_fn(
    dal: Any,
    fn_name: str,
    item_id: int,
    quantity: int,
    user_id: int,
    community_id: int,
    note: str | None,
) -> tuple[bool, str]:
    """Stand-in for `_run_stock_fn`'s call to the real Postgres `*_inventory_stock` functions.

    See `config/postgres/migrations/014_add_quartermaster_tables.sql` --
    sqlite:memory has no stored-function support. Applies the same
    available_quantity delta the real function would, so checkout/checkin's
    surrounding logic (item lookup, checkout row lifecycle -- the actual
    behavior this port fixes, per the module docstring) is still exercised
    against a real row.
    """
    delta = -quantity if fn_name == "remove_inventory_stock" else quantity
    item = dal.inventory_items[item_id]
    dal(dal.inventory_items.id == item_id).update(
        available_quantity=item.available_quantity + delta
    )
    dal.commit()
    return True, "ok"


class TestCheckoutCheckin:
    async def test_checkout_creates_a_real_checkout_row(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        """Regression coverage for the Node bug fixed during this port.

        See `services/community_inventory.py`'s module docstring.
        """
        monkeypatch.setattr(inventory_svc, "_run_stock_fn", _fake_stock_fn)
        dal, community_id = community_db
        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items",
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"name": "Boom Mic", "quantity": 2},
        )
        item_id = (await create_resp.get_json())["item"]["id"]

        checkout_resp = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/checkout",
            auth_headers(scope="community.inventory:use", user_id="1"),
            {"item_id": item_id, "quantity": 1},
        )
        assert checkout_resp.status_code == 201
        checkout = (await checkout_resp.get_json())["checkout"]
        assert checkout["status"] == "active"
        assert dal(dal.inventory_checkouts.id == checkout["id"]).count() == 1
        assert dal.inventory_items[_nid(dal, item_id)].available_quantity == 1

        my_items_resp = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/my-items",
            headers=auth_headers(scope="community.inventory:use", user_id="1"),
        )
        assert len((await my_items_resp.get_json())["checkouts"]) == 1

        checkin_resp = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/checkin",
            auth_headers(scope="community.inventory:use", user_id="1"),
            {"checkout_id": checkout["id"]},
        )
        assert checkin_resp.status_code == 200
        assert (await checkin_resp.get_json())["checkout"]["status"] == "returned"
        assert dal.inventory_items[_nid(dal, item_id)].available_quantity == 2

    async def test_checkout_insufficient_quantity_is_409(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items",
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"name": "Rare Lens", "quantity": 1},
        )
        item_id = (await create_resp.get_json())["item"]["id"]

        response = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/checkout",
            auth_headers(scope="community.inventory:use", user_id="1"),
            {"item_id": item_id, "quantity": 5},
        )
        assert response.status_code == 409

    async def test_checkout_missing_item_id_is_400(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/checkout",
            auth_headers(scope="community.inventory:use", user_id="1"),
            {"quantity": 1},
        )
        assert response.status_code == 400

    async def test_checkout_invalid_quantity_is_400(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/checkout",
            auth_headers(scope="community.inventory:use", user_id="1"),
            {"item_id": "00000000-0000-4000-8000-00000000dead", "quantity": "not-a-number"},
        )
        assert response.status_code == 400

    async def test_checkout_unknown_item_is_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/checkout",
            auth_headers(scope="community.inventory:use", user_id="1"),
            {"item_id": "00000000-0000-4000-8000-00000000dead", "quantity": 1},
        )
        assert response.status_code == 404

    async def test_checkin_missing_checkout_id_is_400(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/checkin",
            auth_headers(scope="community.inventory:use", user_id="1"),
            {},
        )
        assert response.status_code == 400

    async def test_checkin_unknown_checkout_is_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/checkin",
            auth_headers(scope="community.inventory:use", user_id="1"),
            {"checkout_id": 9999},
        )
        assert response.status_code == 404

    async def test_checkin_with_partial_quantity_returned(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        """Covers `checkin_item`'s `quantity_returned` branch (vs. the default.

        full-quantity-return path already exercised above).
        """
        monkeypatch.setattr(inventory_svc, "_run_stock_fn", _fake_stock_fn)
        dal, community_id = community_db
        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items",
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"name": "Tripod", "quantity": 5},
        )
        item_id = (await create_resp.get_json())["item"]["id"]
        checkout_resp = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/checkout",
            auth_headers(scope="community.inventory:use", user_id="1"),
            {"item_id": item_id, "quantity": 3},
        )
        checkout_id = (await checkout_resp.get_json())["checkout"]["id"]

        response = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/checkin",
            auth_headers(scope="community.inventory:use", user_id="1"),
            {"checkout_id": checkout_id, "quantity_returned": 3},
        )
        assert response.status_code == 200


class TestUpdateDeleteNotFound:
    async def test_update_unknown_item_is_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.put(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items/9999",
            headers={
                **auth_headers(scope="community.inventory:write", user_id="2"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"name": "x"}),
        )
        assert response.status_code == 404

    async def test_delete_unknown_item_is_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.delete(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items/9999",
            headers=auth_headers(scope="community.inventory:write", user_id="2"),
        )
        assert response.status_code == 404


class TestStockAdjustments:
    async def test_add_stock_success(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(inventory_svc, "_run_stock_fn", _fake_stock_fn)
        _, community_id = community_db
        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items",
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"name": "Batteries", "quantity": 10},
        )
        item_id = (await create_resp.get_json())["item"]["id"]

        response = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}"
            f"/inventory/items/{item_id}/stock/add",
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"quantity": 5, "notes": "restock"},
        )
        assert response.status_code == 200
        assert (await response.get_json())["item"]["available_quantity"] == 15

    async def test_add_stock_invalid_quantity_is_400(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}"
            f"/inventory/items/{UNKNOWN_PUBLIC_ID}/stock/add",
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"quantity": -1},
        )
        assert response.status_code == 400

    async def test_remove_stock_success(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(inventory_svc, "_run_stock_fn", _fake_stock_fn)
        _, community_id = community_db
        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items",
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"name": "Cables", "quantity": 10},
        )
        item_id = (await create_resp.get_json())["item"]["id"]

        response = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}"
            f"/inventory/items/{item_id}/stock/remove",
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"quantity": 4, "notes": "damaged"},
        )
        assert response.status_code == 200
        assert (await response.get_json())["item"]["available_quantity"] == 6

    async def test_remove_stock_invalid_quantity_is_400(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}"
            f"/inventory/items/00000000-0000-4000-8000-00000000dead/stock/remove",
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"quantity": 0},
        )
        assert response.status_code == 400


class TestListCheckoutsAuditAndSummary:
    async def test_list_checkouts_with_and_without_status_filter(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(inventory_svc, "_run_stock_fn", _fake_stock_fn)
        dal, community_id = community_db
        dal.hub_users.insert(username="alice", display_name="Alice")
        dal.commit()
        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items",
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"name": "Camera", "quantity": 3},
        )
        item_id = (await create_resp.get_json())["item"]["id"]
        await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/checkout",
            auth_headers(scope="community.inventory:use", user_id="1"),
            {"item_id": item_id, "quantity": 1},
        )

        all_resp = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/checkouts",
            headers=auth_headers(scope="community.inventory:read", user_id="2"),
        )
        assert all_resp.status_code == 200
        assert len((await all_resp.get_json())["checkouts"]) == 1

        filtered_resp = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}"
            f"/inventory/checkouts?status=active",
            headers=auth_headers(scope="community.inventory:read", user_id="2"),
        )
        assert len((await filtered_resp.get_json())["checkouts"]) == 1

        none_resp = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}"
            f"/inventory/checkouts?status=returned",
            headers=auth_headers(scope="community.inventory:read", user_id="2"),
        )
        assert len((await none_resp.get_json())["checkouts"]) == 0

    async def test_summary_uses_stored_function(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        """`get_inventory_summary()` is a Postgres stored function -- stub.

        `dal.executesql` for just that call (sqlite:memory has no stored
        functions), same technique as `test_community_relay.py`.
        """
        dal, community_id = community_db
        real_executesql = dal.executesql

        def fake_executesql(sql: str, placeholders: Any = None, **kwargs: Any) -> list[Any]:
            if "get_inventory_summary" in sql:
                return [(3, 30, 20, 2, 1, 4)]
            return real_executesql(sql, placeholders=placeholders, **kwargs)

        monkeypatch.setattr(dal, "executesql", fake_executesql)

        response = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/summary",
            headers=auth_headers(scope="community.inventory:read", user_id="2"),
        )
        assert response.status_code == 200
        assert (await response.get_json())["summary"] == {
            "total_items": 3,
            "total_quantity": 30,
            "total_available": 20,
            "active_checkouts": 2,
            "overdue_checkouts": 1,
            "low_stock_items": 4,
        }

    async def test_audit_log_with_filters(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(inventory_svc, "_run_stock_fn", _fake_stock_fn)
        dal, community_id = community_db
        create_resp = await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items",
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"name": "Mixer", "quantity": 2},
        )
        item_id = (await create_resp.get_json())["item"]["id"]
        await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}"
            f"/inventory/items/{item_id}/stock/add",
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"quantity": 1},
        )
        # add_stock via the stubbed _run_stock_fn doesn't write inventory_log
        # (that's the real stored function's job) -- insert a row directly so
        # the read path (get_audit_log) has something to filter/return.
        dal.inventory_log.insert(
            item_id=_nid(dal, item_id),
            community_id=community_id,
            action="add_stock",
            quantity_change=1,
        )
        dal.commit()

        all_resp = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/log",
            headers=auth_headers(scope="community.inventory:read", user_id="2"),
        )
        assert all_resp.status_code == 200
        assert len((await all_resp.get_json())["log"]) == 1

        item_filtered = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}"
            f"/inventory/log?item_id={item_id}&action=add_stock",
            headers=auth_headers(scope="community.inventory:read", user_id="2"),
        )
        assert len((await item_filtered.get_json())["log"]) == 1

        no_match = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}"
            f"/inventory/log?action=remove_stock",
            headers=auth_headers(scope="community.inventory:read", user_id="2"),
        )
        assert len((await no_match.get_json())["log"]) == 0

    async def test_audit_log_limit_query_param_is_capped_before_reaching_raw_sql(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        """Security-review HIGH: `get_audit_log()`'s raw SQL has no `LIMIT` clamp of its own.

        Every other list endpoint's service function re-clamps `limit`
        server-side even if the route somehow passed an unbounded value
        (`min(100, max(1, limit))`, see e.g. `user_management_service.py`).
        `get_audit_log()` does not -- it interpolates `limit` straight into
        a parameterized `LIMIT $n` (`services/community_inventory.py`), so
        `blueprints/v1/community_inventory.py::audit_log_route`'s own
        `parse_limit()` call is the ONLY bound in this endpoint's entire
        call chain. Asserting the raw SQL boundary itself, not just the
        route, since that's where an unbounded value would have actually
        landed.

        Fail-first proof (executed, not narrated): reverted
        `audit_log_route`'s `limit = parse_limit(...)` back to the pre-fix
        `limit = request.args.get("limit", default=50, type=int)` --
        this test went red (captured `limit=999999`, unbounded, reaching
        `get_audit_log`); reverted to `parse_limit`, green again.
        """
        import blueprints.v1.community_inventory as inventory_bp_module

        dal, community_id = community_db
        captured: dict[str, Any] = {}
        real_get_audit_log = inventory_bp_module.get_audit_log

        def _spy_get_audit_log(*args: Any, **kwargs: Any) -> Any:
            captured["limit"] = kwargs.get("limit")
            return real_get_audit_log(*args, **kwargs)

        monkeypatch.setattr(inventory_bp_module, "get_audit_log", _spy_get_audit_log)

        response = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/log?limit=999999",
            headers=auth_headers(scope="community.inventory:read", user_id="2"),
        )
        assert response.status_code == 200
        assert captured["limit"] == 100


class TestAvailableItems:
    async def test_available_without_search(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        await _post_json(
            client,
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/items",
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"name": "Drone", "quantity": 1},
        )
        response = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/inventory/available",
            headers=auth_headers(scope="community.inventory:use", user_id="1"),
        )
        assert response.status_code == 200
        assert len((await response.get_json())["items"]) == 1

    async def test_available_with_search_uses_stored_function(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        """`search_inventory_items()` is a Postgres full-text-search stored.

        function -- stub `dal.executesql` for just that call.
        """
        dal, community_id = community_db
        real_executesql = dal.executesql
        seeded = dal.inventory_items.insert(community_id=community_id, name="Drone", quantity=1)
        dal.commit()

        def fake_executesql(sql: str, placeholders: Any = None, **kwargs: Any) -> list[Any]:
            if "search_inventory_items" in sql:
                return [(seeded, "Drone", "desc", "gear", "electronics", 1, 1, 0)]
            return real_executesql(sql, placeholders=placeholders, **kwargs)

        monkeypatch.setattr(dal, "executesql", fake_executesql)

        response = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}"
            f"/inventory/available?search=drone",
            headers=auth_headers(scope="community.inventory:use", user_id="1"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["items"][0]["name"] == "Drone"


# ── Per-community authorization (membership / admin / ban / flag / quantity) ──────────────────


def _url(db: Any, community_id: int, suffix: str) -> str:
    return f"/api/v1/admin/{public_id(db, community_id)}/inventory/{suffix}"


def _hdr(auth_headers: Any, user_id: int, tenant: str = "acme-corp") -> dict[str, str]:
    """A token that carries NO inventory scope beyond the universal `*:read` every session has."""
    return auth_headers(scope="*:read", user_id=str(user_id), tenant=tenant)


MEMBER_ROUTES = [
    ("GET", "available", None),
    ("POST", "checkout", {"item_id": "00000000-0000-4000-8000-00000000dead", "quantity": 1}),
    ("GET", "my-items", None),
]
ADMIN_READ_ROUTES = [("GET", "items"), ("GET", "checkouts"), ("GET", "summary"), ("GET", "log")]


async def _call(client: Any, method: str, url: str, headers: dict[str, str], body: Any) -> Any:
    if body is None:
        return await client.open(url, method=method, headers=headers)
    return await _post_json(client, url, headers, body)


class TestMemberRoutesAuthorizeThroughTheCommunity:
    @pytest.mark.parametrize("method,suffix,body", MEMBER_ROUTES)
    async def test_non_member_is_403(
        self, client: Any, auth_headers: Any, community_db: Any, method: str, suffix: str, body: Any
    ) -> None:
        dal, cid = community_db
        resp = await _call(
            client, method, _url(dal, cid, suffix), _hdr(auth_headers, STRANGER), body
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize("who", [BANNED, SOFT_BANNED])
    @pytest.mark.parametrize("method,suffix,body", MEMBER_ROUTES)
    async def test_banned_and_soft_banned_are_403(
        self,
        client: Any,
        auth_headers: Any,
        community_db: Any,
        who: int,
        method: str,
        suffix: str,
        body: Any,
    ) -> None:
        dal, cid = community_db
        resp = await _call(client, method, _url(dal, cid, suffix), _hdr(auth_headers, who), body)
        assert resp.status_code == 403

    async def test_lifted_ban_restores_member_access(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        """Banned (state) is denied; `lift_membership_ban` writes ACTIVE back and access returns."""
        from datetime import UTC, datetime

        from services.ban_suppression import lift_membership_ban
        from services.membership_state import BanSource

        dal, cid = community_db
        denied = await client.get(_url(dal, cid, "available"), headers=_hdr(auth_headers, BANNED))
        assert denied.status_code == 403
        assert (
            lift_membership_ban(
                dal,
                community_id=cid,
                hub_user_id=BANNED,
                source=BanSource.MANUAL,
                now=datetime.now(UTC),
            )
            == 1
        )
        dal.commit()
        allowed = await client.get(_url(dal, cid, "available"), headers=_hdr(auth_headers, BANNED))
        assert allowed.status_code == 200

    async def test_expired_soft_ban_no_longer_blocks(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        from datetime import datetime, timedelta

        dal, cid = community_db
        dal(dal.bundle_reputation_soft_bans.user_uuid == "uuid-soft").update(
            expires_at=datetime.utcnow() - timedelta(days=1)
        )
        dal.commit()
        resp = await client.get(
            _url(dal, cid, "available"), headers=_hdr(auth_headers, SOFT_BANNED)
        )
        assert resp.status_code == 200

    async def test_soft_banned_member_row_without_user_uuid_is_403(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        """regression: a membership row lacking user_uuid must not skip the soft-ban check."""
        dal, cid = community_db
        tenant_id = dal(dal.communities.id == cid).select().first().tenant_id
        hub_uuid = dal(dal.hub_users.id == STRANGER).select().first().uuid
        dal.community_members.insert(
            community_id=cid, user_id=str(STRANGER), role="member", is_active=True
        )
        dal.bundle_reputation_soft_bans.insert(
            tenant_id=tenant_id,
            community_id=cid,
            user_uuid=hub_uuid,
            reason_code=1,
            set_by="admin",
        )
        dal.commit()
        resp = await client.get(_url(dal, cid, "available"), headers=_hdr(auth_headers, STRANGER))
        assert resp.status_code == 403

    async def test_unresolvable_identity_is_denied_and_logged(
        self, community_db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No hub_users uuid and no member user_uuid: fail closed, never skip the check."""
        from types import SimpleNamespace

        from services.community_inventory_authz import require_inventory_member
        from services.errors import ApiError

        dal, cid = community_db
        dal.community_members.insert(community_id=cid, user_id="777", role="member", is_active=True)
        dal.commit()
        ctx = SimpleNamespace(tenant_id=1)
        with caplog.at_level("ERROR"), pytest.raises(ApiError):
            require_inventory_member(dal, ctx, community_id=cid, user_id=777)
        assert "unresolvable identity" in caplog.text

    @pytest.mark.parametrize("keyed_on", ["pseudonym", "platform_keyed_member_row"])
    async def test_soft_ban_keyed_on_other_identity_forms_is_403(
        self, client: Any, auth_headers: Any, community_db: Any, keyed_on: str
    ) -> None:
        """regression: a bundle-written ban keyed on a pseudonym / linked-identity row denies."""
        from services.schema import bind_erasure_reputation_tables

        dal, cid = community_db
        bind_erasure_reputation_tables(dal, migrate=True)
        tenant_id = dal(dal.communities.id == cid).select().first().tenant_id
        dal.hub_user_identities.insert(
            hub_user_id=STRANGER, platform="discord", platform_user_id="p-123"
        )
        dal.community_members.insert(
            community_id=cid, user_id=str(STRANGER), role="member", is_active=True
        )
        if keyed_on == "pseudonym":
            token = "11111111-1111-4111-8111-111111111111"
            dal.ephemeral_pseudonyms.insert(
                pseudonym=token, tenant_id=tenant_id, platform="discord", platform_user_id="p-123"
            )
        else:
            token = "22222222-2222-4222-8222-222222222222"
            dal.community_members.insert(
                community_id=cid,
                user_id="",
                platform="discord",
                platform_user_id="p-123",
                role="member",
                is_active=True,
                user_uuid=token,
            )
        dal.bundle_reputation_soft_bans.insert(
            tenant_id=tenant_id, community_id=cid, user_uuid=token, reason_code=1, set_by="bundle"
        )
        dal.commit()
        resp = await client.get(_url(dal, cid, "available"), headers=_hdr(auth_headers, STRANGER))
        assert resp.status_code == 403

    @pytest.mark.parametrize("status,expected", [(2, 403), (1, 403), (6, 200)])
    async def test_tenant_floor_ban_denies_even_when_member_row_is_not_banned(
        self, client: Any, auth_headers: Any, community_db: Any, status: int, expected: int
    ) -> None:
        """regression: a tenant-level floor ban applies in a community whose row isn't banned."""
        from services import ban_suppression

        dal, cid = community_db
        ban_suppression._bind(dal)
        tenant_id = dal(dal.communities.id == cid).select().first().tenant_id
        hub_uuid = dal(dal.hub_users.id == MEMBER).select().first().uuid
        dal.bundle_reputation_floor_bans.insert(
            tenant_id=tenant_id,
            community_id=cid + 100,  # banned elsewhere in the tenant
            user_uuid=hub_uuid,
            platform="discord",
            status=status,
        )
        dal.commit()
        resp = await client.get(_url(dal, cid, "available"), headers=_hdr(auth_headers, MEMBER))
        assert resp.status_code == expected

    async def test_unbanned_member_without_member_uuid_is_allowed(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, cid = community_db
        dal.community_members.insert(
            community_id=cid, user_id=str(STRANGER), role="member", is_active=True
        )
        dal.commit()
        resp = await client.get(_url(dal, cid, "available"), headers=_hdr(auth_headers, STRANGER))
        assert resp.status_code == 200

    async def test_normal_unbanned_member_is_allowed(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, cid = community_db
        resp = await client.get(_url(dal, cid, "available"), headers=_hdr(auth_headers, MEMBER))
        assert resp.status_code == 200

    @pytest.mark.parametrize("method,suffix,body", MEMBER_ROUTES)
    async def test_cross_tenant_is_404(
        self, client: Any, auth_headers: Any, community_db: Any, method: str, suffix: str, body: Any
    ) -> None:
        dal, cid = community_db
        headers = _hdr(auth_headers, MEMBER, tenant=OTHER_TENANT)
        resp = await _call(client, method, _url(dal, cid, suffix), headers, body)
        assert resp.status_code == 404

    async def test_plain_member_without_any_inventory_scope_can_browse(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        dal, cid = community_db
        for suffix in ("available", "my-items"):
            resp = await client.get(_url(dal, cid, suffix), headers=_hdr(auth_headers, MEMBER))
            assert resp.status_code == 200

    @pytest.mark.parametrize("method,suffix,body", MEMBER_ROUTES + [("POST", "checkin", {})])
    async def test_flag_off_is_402_with_standard_body(
        self,
        client: Any,
        auth_headers: Any,
        community_db: Any,
        monkeypatch: pytest.MonkeyPatch,
        method: str,
        suffix: str,
        body: Any,
    ) -> None:
        import blueprints.v1.community_inventory as inventory_module

        monkeypatch.setattr(inventory_module, "feature_enabled", AsyncMock(return_value=False))
        dal, cid = community_db
        resp = await _call(client, method, _url(dal, cid, suffix), _hdr(auth_headers, MEMBER), body)
        assert resp.status_code == 402
        payload = await resp.get_json()
        assert payload["error"]["message"] == (
            "Community inventory requires a Professional plan or higher"
        )


class TestAdminReadsRequireCommunityAdmin:
    @pytest.mark.parametrize("method,suffix", ADMIN_READ_ROUTES)
    async def test_plain_member_is_403_despite_universal_read_scope(
        self, client: Any, auth_headers: Any, community_db: Any, method: str, suffix: str
    ) -> None:
        dal, cid = community_db
        resp = await client.open(
            _url(dal, cid, suffix),
            method=method,
            headers=auth_headers(scope="community.inventory:read", user_id=str(MEMBER)),
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,suffix", ADMIN_READ_ROUTES)
    async def test_non_member_is_403(
        self, client: Any, auth_headers: Any, community_db: Any, method: str, suffix: str
    ) -> None:
        dal, cid = community_db
        resp = await client.open(
            _url(dal, cid, suffix),
            method=method,
            headers=auth_headers(scope="community.inventory:read", user_id=str(STRANGER)),
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,suffix", ADMIN_READ_ROUTES)
    async def test_community_admin_gets_200(
        self,
        client: Any,
        auth_headers: Any,
        community_db: Any,
        monkeypatch: pytest.MonkeyPatch,
        method: str,
        suffix: str,
    ) -> None:
        import blueprints.v1.community_inventory as inventory_module

        # `summary`/`log` run Postgres-only SQL (stored fn / column shape) that sqlite lacks; the
        # authz gate in front of them is what this test exercises.
        monkeypatch.setattr(
            inventory_module,
            "get_summary",
            lambda *_a, **_k: inventory_svc.InventorySummary(1, 2, 2, 0, 0, 0),
        )
        monkeypatch.setattr(inventory_module, "get_audit_log", lambda *_a, **_k: [])
        dal, cid = community_db
        resp = await client.open(
            _url(dal, cid, suffix),
            method=method,
            headers=auth_headers(scope="community.inventory:read", user_id=str(ADMIN)),
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize("method,suffix", ADMIN_READ_ROUTES)
    async def test_flag_off_is_402(
        self,
        client: Any,
        auth_headers: Any,
        community_db: Any,
        monkeypatch: pytest.MonkeyPatch,
        method: str,
        suffix: str,
    ) -> None:
        import blueprints.v1.community_inventory as inventory_module

        monkeypatch.setattr(inventory_module, "feature_enabled", AsyncMock(return_value=False))
        dal, cid = community_db
        resp = await client.open(
            _url(dal, cid, suffix),
            method=method,
            headers=auth_headers(scope="community.inventory:read", user_id=str(ADMIN)),
        )
        assert resp.status_code == 402


class TestCheckinQuantityValidation:
    async def _checked_out(self, client: Any, auth_headers: Any, db: Any, cid: int) -> int:
        item = await _post_json(
            client,
            _url(db, cid, "items"),
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"name": "Lens", "quantity": 5},
        )
        item_id = (await item.get_json())["item"]["id"]
        out = await _post_json(
            client,
            _url(db, cid, "checkout"),
            _hdr(auth_headers, MEMBER),
            {"item_id": item_id, "quantity": 2},
        )
        return int((await out.get_json())["checkout"]["id"])

    @pytest.mark.parametrize("bad", [0, -1, 3, "abc", True])
    async def test_bad_quantity_returned_is_400_and_stock_unchanged(
        self,
        client: Any,
        auth_headers: Any,
        community_db: Any,
        monkeypatch: pytest.MonkeyPatch,
        bad: Any,
    ) -> None:
        monkeypatch.setattr(inventory_svc, "_run_stock_fn", _fake_stock_fn)
        dal, cid = community_db
        checkout_id = await self._checked_out(client, auth_headers, dal, cid)
        before = dal(dal.inventory_items).select().first().available_quantity
        resp = await _post_json(
            client,
            _url(dal, cid, "checkin"),
            _hdr(auth_headers, MEMBER),
            {"checkout_id": checkout_id, "quantity_returned": bad},
        )
        assert resp.status_code == 400
        assert dal(dal.inventory_items).select().first().available_quantity == before

    async def test_service_rejects_oversized_return_directly(
        self, community_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defence in depth: the service refuses even if a caller skips the route's check."""
        monkeypatch.setattr(inventory_svc, "_run_stock_fn", _fake_stock_fn)
        dal, cid = community_db
        item_id = dal.inventory_items.insert(
            community_id=cid, name="Cable", quantity=4, available_quantity=4
        )
        dal.commit()
        dto, _ = inventory_svc.checkout_item(dal, cid, MEMBER, item_id, 2, None, None)
        assert dto is not None
        for bad in (0, -5, 3):
            result, err = inventory_svc.checkin_item(dal, cid, MEMBER, dto.id, bad, None)
            assert result is None and "between 1 and 2" in (err or "")


WRITE_ROUTES = [
    ("POST", "items", {"name": "X", "quantity": 1}),
    ("PUT", "items/00000000-0000-4000-8000-00000000dead", {"name": "Y"}),
    ("DELETE", "items/00000000-0000-4000-8000-00000000dead", None),
    ("POST", "items/00000000-0000-4000-8000-00000000dead/stock/add", {"quantity": 1}),
    ("POST", "items/00000000-0000-4000-8000-00000000dead/stock/remove", {"quantity": 1}),
]


async def _write_call(
    client: Any, method: str, url: str, headers: dict[str, str], body: Any
) -> Any:
    if body is None:
        return await client.open(url, method=method, headers=headers)
    return await client.open(
        url,
        method=method,
        headers={**headers, "Content-Type": "application/json"},
        data=json_module.dumps(body),
    )


class TestWriteRoutesRequireCommunityAdmin:
    @pytest.mark.parametrize("who", [MEMBER, STRANGER])
    @pytest.mark.parametrize("method,suffix,body", WRITE_ROUTES)
    async def test_non_admin_is_403_even_with_the_write_scope(
        self,
        client: Any,
        auth_headers: Any,
        community_db: Any,
        who: int,
        method: str,
        suffix: str,
        body: Any,
    ) -> None:
        dal, cid = community_db
        headers = auth_headers(scope="community.inventory:write", user_id=str(who))
        resp = await _write_call(client, method, _url(dal, cid, suffix), headers, body)
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,suffix,body", WRITE_ROUTES)
    async def test_flag_off_is_402(
        self,
        client: Any,
        auth_headers: Any,
        community_db: Any,
        monkeypatch: pytest.MonkeyPatch,
        method: str,
        suffix: str,
        body: Any,
    ) -> None:
        import blueprints.v1.community_inventory as inventory_module

        monkeypatch.setattr(inventory_module, "feature_enabled", AsyncMock(return_value=False))
        dal, cid = community_db
        headers = auth_headers(scope="community.inventory:write", user_id=str(ADMIN))
        resp = await _write_call(client, method, _url(dal, cid, suffix), headers, body)
        assert resp.status_code == 402


class TestSoftBanExpiryBoundary:
    """`expires_at` is TIMESTAMPTZ: the check compares an aware UTC `now`."""

    @pytest.mark.parametrize("delta_seconds,status", [(1, 403), (-1, 200)])
    async def test_expiry_one_second_either_side(
        self,
        client: Any,
        auth_headers: Any,
        community_db: Any,
        delta_seconds: int,
        status: int,
    ) -> None:
        from datetime import UTC, datetime, timedelta

        dal, cid = community_db
        dal(dal.bundle_reputation_soft_bans.user_uuid == "uuid-soft").update(
            expires_at=datetime.now(UTC) + timedelta(seconds=delta_seconds)
        )
        dal.commit()
        resp = await client.get(
            _url(dal, cid, "available"), headers=_hdr(auth_headers, SOFT_BANNED)
        )
        assert resp.status_code == status


class TestInventoryItemPublicUuid:
    """`<puuid:item_id>`: UUID resolves; foreign-community UUID and a numeric id are 404."""

    async def test_uuid_resolves_foreign_and_numeric_404(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        from tests.entity_uuid_support import assert_uuid, second_community

        dal, community_id = community_db
        other = second_community(dal, community_id)
        _join_other(dal, other, admin=True)
        base = f"/api/v1/admin/{public_id(dal, community_id)}/inventory/items"
        write = auth_headers(scope="community.inventory:write", user_id="2")
        created = await _post_json(client, base, write, {"name": "Cam", "quantity": 2})
        item_uuid = assert_uuid((await created.get_json())["item"]["id"])

        ok = await client.open(
            f"{base}/{item_uuid}",
            method="PUT",
            headers={**write, "Content-Type": "application/json"},
            data=json_module.dumps({"name": "Cam2"}),
        )
        assert ok.status_code == 200
        assert (await ok.get_json())["item"]["id"] == item_uuid

        foreign_base = f"/api/v1/admin/{public_id(dal, other)}/inventory/items"
        numeric_id = _nid(dal, item_uuid)
        codes = []
        for path in (f"{foreign_base}/{item_uuid}", f"{base}/{numeric_id}"):
            resp = await client.open(path, method="DELETE", headers=write)
            codes.append(resp.status_code)
        assert codes == [404, 404]
        assert dal(dal.inventory_items.id == numeric_id).count() == 1

    async def test_checkout_body_takes_uuid_and_foreign_or_numeric_is_404(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        from tests.entity_uuid_support import assert_uuid, second_community

        def _fake_stock_fn(dal: Any, fn: str, item_id: int, qty: int, *rest: Any) -> Any:
            return True, None

        monkeypatch.setattr(inventory_svc, "_run_stock_fn", _fake_stock_fn)
        dal, community_id = community_db
        other = second_community(dal, community_id)
        _join_other(dal, other, admin=True)
        pid = public_id(dal, community_id)
        created = await _post_json(
            client,
            f"/api/v1/admin/{pid}/inventory/items",
            auth_headers(scope="community.inventory:write", user_id="2"),
            {"name": "Mic", "quantity": 3},
        )
        item_uuid = (await created.get_json())["item"]["id"]
        use = auth_headers(scope="community.inventory:use", user_id="1")
        checkout_url = f"/api/v1/admin/{pid}/inventory/checkout"

        ok = await _post_json(client, checkout_url, use, {"item_id": item_uuid, "quantity": 1})
        assert ok.status_code == 201
        assert assert_uuid((await ok.get_json())["checkout"]["item_id"]) == item_uuid

        foreign = await _post_json(
            client,
            f"/api/v1/admin/{public_id(dal, other)}/inventory/checkout",
            use,
            {"item_id": item_uuid, "quantity": 1},
        )
        numeric = await _post_json(
            client, checkout_url, use, {"item_id": _nid(dal, item_uuid), "quantity": 1}
        )
        assert (foreign.status_code, numeric.status_code) == (404, 404)
