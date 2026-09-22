"""Blueprint tests for GET /api/v1/tenant/{slug}/usage (spec Sec5.12, D31)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.workstream_usage import BLUEPRINTS
from tests.conftest import TENANT_SLUG, make_user_token


@pytest.fixture
async def app(bundle_install_db: Any, install_dal: Any) -> Quart:
    await install_dal.workstream_usage_hourly.async_insert(
        tenant_id=1,
        community_id=None,
        workstream_id="ws-1",
        stage="ingest",
        app_id=None,
        hour=datetime(2026, 9, 14, 10, 0, tzinfo=UTC),
        events=4,
        invocations=0,
        host_calls=0,
        actions_delivered=0,
        fuel_ms=0,
        outbound_bytes=200,
        media_minutes=None,
        recorded_at=datetime.now(UTC),
    )
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["async_dal"] = bundle_install_db
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    return quart_app


async def test_usage_requires_tenant_admin(app: Quart) -> None:
    token = make_user_token(user_id=1, scope="", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/usage", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


async def test_usage_returns_the_seeded_row(app: Quart) -> None:
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/usage", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["meta"]["total"] == 1
    assert body["rows"][0]["workstreamId"] == "ws-1"
    assert body["rows"][0]["events"] == 4


async def test_usage_filters_by_workstream_id(app: Quart) -> None:
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/usage?workstreamId=nope",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["meta"]["total"] == 0
    assert body["rows"] == []


async def test_usage_rejects_an_invalid_stage(app: Quart) -> None:
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/usage?stage=bogus",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert (await response.get_json())["error"]["code"] == "invalid_stage"


async def test_usage_rejects_an_out_of_range_limit(app: Quart) -> None:
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/usage?limit=9999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert (await response.get_json())["error"]["code"] == "invalid_limit"


async def test_usage_paginates_via_query_params(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    for i in range(3):
        await install_dal.workstream_usage_hourly.async_insert(
            tenant_id=1,
            community_id=None,
            workstream_id=f"ws-extra-{i}",
            stage="ingest",
            app_id=None,
            hour=datetime(2026, 9, 14, 12 + i, 0, tzinfo=UTC),
            events=1,
            invocations=0,
            host_calls=0,
            actions_delivered=0,
            fuel_ms=0,
            outbound_bytes=0,
            media_minutes=None,
            recorded_at=datetime.now(UTC),
        )
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/usage?limit=2&offset=0",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = await response.get_json()
    assert body["meta"]["total"] == 4
    assert len(body["rows"]) == 2


async def test_usage_never_returns_another_tenants_rows(app: Quart) -> None:
    dal = app.config["dal"]
    install_dal = app.config["install_dal"]
    # int(...): pydal's insert() returns a `Reference` (int subclass whose
    # __getattr__ answers a truthy `__clause_element__`, tripping
    # SQLAlchemy's coercion when passed straight into a penguin-dal
    # async_insert) -- cast to a plain int at the pydal/penguin-dal
    # boundary, exactly as every real caller already does (tenant_id
    # always arrives here as a plain int from the JWT/tenant-context
    # resolution, never a live pydal Reference).
    other_tenant_id = int(
        dal.tenants.insert(slug="other-corp", display_name="Other", is_active=True)
    )
    dal.commit()
    await install_dal.workstream_usage_hourly.async_insert(
        tenant_id=other_tenant_id,
        community_id=None,
        workstream_id="ws-other",
        stage="ingest",
        app_id=None,
        hour=datetime(2026, 9, 14, 10, 0, tzinfo=UTC),
        events=99,
        invocations=0,
        host_calls=0,
        actions_delivered=0,
        fuel_ms=0,
        outbound_bytes=0,
        media_minutes=None,
        recorded_at=datetime.now(UTC),
    )
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    response = await app.test_client().get(
        f"/api/v1/tenant/{TENANT_SLUG}/usage", headers={"Authorization": f"Bearer {token}"}
    )
    body = await response.get_json()
    assert all(r["events"] != 99 for r in body["rows"])
