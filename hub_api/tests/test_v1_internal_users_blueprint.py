"""`blueprints/v1/internal_users.py` -- `POST /api/v1/internal/users/display-names`.

Standalone Quart app registering only `internal_users_bp`, matching
`test_v1_distribution_blueprint.py`'s pattern. Exercises the
`ephemeral_identities` pseudonym-resolution path end to end (PR #429's
table, bound locally in this module's own fixture since it hasn't landed
on this branch yet -- see `services/internal_identity_service.py`'s module
docstring); the "real linked `hub_users` UUID" path has no queryable UUID
column yet and is exercised only at the unit level
(`test_internal_identity_service.py`, asserting the tolerant no-op).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from flask_core.database import AsyncDAL
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.internal_users import internal_users_bp
from services.schema import bind_auth_tables
from tests.conftest import OTHER_TENANT_SLUG, TENANT_SLUG, make_token

TENANT_A_UUID = "11111111-1111-4111-8111-111111111111"
TENANT_B_UUID = "22222222-2222-4222-8222-222222222222"
ERASED_UUID = "33333333-3333-4333-8333-333333333333"
UNKNOWN_UUID = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def internal_users_db(tmp_path: Any) -> Any:
    """File-backed `AsyncDAL` with `tenants`/`hub_users` (M1) + `ephemeral_identities`.

    Seeds two tenants (`TENANT_SLUG`, `OTHER_TENANT_SLUG`) and three
    `ephemeral_identities` rows: one live pseudonym under each tenant, plus
    an EXPIRED pseudonym under `TENANT_SLUG` standing in for "erased" (the
    coordination note's `expires_at`-based exclusion is this table's own
    erasure mechanism -- an expired row must never resolve, same as a
    genuinely deleted one).
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'internal_users_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("is_active", "boolean", default=True),
    )
    bind_auth_tables(dal, migrate=True)
    dal.define_table(
        "ephemeral_identities",
        Field("tenant_id", "integer", notnull=True),
        Field("pseudonym", "string", length=64, notnull=True),
        Field("platform", "string", length=50),
        Field("platform_user_id", "string", length=255),
        Field("handle", "string", length=255),
        Field("last_seen", "datetime"),
        Field("expires_at", "datetime"),
        migrate=True,
    )

    tenant_a = dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    tenant_b = dal.tenants.insert(slug=OTHER_TENANT_SLUG, display_name="Other Corp", is_active=True)

    now = datetime.now(UTC)
    dal.ephemeral_identities.insert(
        tenant_id=tenant_a,
        pseudonym=TENANT_A_UUID,
        platform="twitch",
        platform_user_id="999",
        handle="pixelpenguin",
        last_seen=now,
        expires_at=now + timedelta(days=1),
    )
    dal.ephemeral_identities.insert(
        tenant_id=tenant_b,
        pseudonym=TENANT_B_UUID,
        platform="twitch",
        platform_user_id="888",
        handle="otherTenantViewer",
        last_seen=now,
        expires_at=now + timedelta(days=1),
    )
    dal.ephemeral_identities.insert(
        tenant_id=tenant_a,
        pseudonym=ERASED_UUID,
        platform="discord",
        platform_user_id="777",
        handle="longGoneViewer",
        last_seen=now - timedelta(days=10),
        expires_at=now - timedelta(hours=1),  # expired -- treated as erased
    )
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


@pytest.fixture
def app(internal_users_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(internal_users_bp)
    quart_app.config["dal"] = internal_users_db.dal
    quart_app.config["async_dal"] = internal_users_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _headers(
    *, scope: str = "users:display-name:resolve", tenant: str = TENANT_SLUG
) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(scope=scope, tenant=tenant)}"}


async def _post(client: Any, uuids: list[str], **kwargs: Any) -> Any:
    return await client.post(
        "/api/v1/internal/users/display-names",
        json={"user_uuids": uuids},
        headers=_headers(**kwargs),
    )


class TestAuth:
    async def test_missing_token_is_401(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/internal/users/display-names", json={"user_uuids": [TENANT_A_UUID]}
        )
        assert response.status_code == 401

    async def test_missing_scope_is_403(self, client: Any) -> None:
        response = await _post(client, [TENANT_A_UUID], scope="")
        assert response.status_code == 403

    async def test_wrong_scope_is_403(self, client: Any) -> None:
        response = await _post(client, [TENANT_A_UUID], scope="distribution:read")
        assert response.status_code == 403


class TestResolution:
    async def test_resolves_a_live_pseudonym_for_the_caller_tenant(self, client: Any) -> None:
        response = await _post(client, [TENANT_A_UUID])
        assert response.status_code == 200
        body = await response.get_json()
        assert body["display_names"] == {TENANT_A_UUID: "pixelpenguin"}

    async def test_cross_tenant_uuid_is_absent_from_the_response(self, client: Any) -> None:
        """Tenant A's request for tenant B's UUID must return nothing for it."""
        response = await _post(client, [TENANT_B_UUID], tenant=TENANT_SLUG)
        assert response.status_code == 200
        body = await response.get_json()
        assert TENANT_B_UUID not in body["display_names"]
        assert body["display_names"] == {}

    async def test_erased_expired_identity_is_absent_from_the_response(self, client: Any) -> None:
        response = await _post(client, [ERASED_UUID])
        assert response.status_code == 200
        body = await response.get_json()
        assert body["display_names"] == {}

    async def test_unknown_uuid_is_absent_from_the_response(self, client: Any) -> None:
        response = await _post(client, [UNKNOWN_UUID])
        assert response.status_code == 200
        body = await response.get_json()
        assert body["display_names"] == {}

    async def test_mixed_batch_resolves_only_the_valid_in_tenant_entries(self, client: Any) -> None:
        response = await _post(client, [TENANT_A_UUID, TENANT_B_UUID, UNKNOWN_UUID])
        assert response.status_code == 200
        body = await response.get_json()
        assert body["display_names"] == {TENANT_A_UUID: "pixelpenguin"}


class TestInputValidation:
    async def test_empty_list_is_400(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/internal/users/display-names",
            json={"user_uuids": []},
            headers=_headers(),
        )
        assert response.status_code == 400

    async def test_over_the_cap_is_400(self, client: Any) -> None:
        too_many = [TENANT_A_UUID] * 101
        response = await client.post(
            "/api/v1/internal/users/display-names",
            json={"user_uuids": too_many},
            headers=_headers(),
        )
        assert response.status_code == 400
