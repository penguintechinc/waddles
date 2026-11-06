"""`GET/POST /api/v1/admin/:communityId/browser-sources*` -- real DB, real JWTs, no authz mocks."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.overlay as overlay_module
from blueprints.v1.overlay import SCOPE_ADMIN, overlay_bp
from services import overlay_service
from tests.conftest import (
    OTHER_TENANT_SLUG,
    TENANT_SLUG,
    make_user_token,
    seed_community,
    seed_membership,
)
from tests.test_v1_overlay_blueprint import _test_config


@pytest.fixture
def app(overlay_db: Any) -> Quart:
    overlay_db.dal.executesql("ALTER TABLE communities ADD COLUMN overlay_code VARCHAR(16)")
    overlay_db.dal.commit()
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(overlay_bp)
    quart_app.config["dal"] = overlay_db.dal
    quart_app.config["async_dal"] = overlay_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(overlay_module, "feature_enabled", stub)
    return stub


def _admin(overlay_db: Any, *, tenant: str = TENANT_SLUG) -> tuple[dict[str, str], int]:
    community_id = seed_community(overlay_db, tenant_slug=tenant)
    seed_membership(overlay_db, community_id=community_id, user_id=501)
    token = make_user_token(user_id=501, scope=SCOPE_ADMIN, tenant=tenant)
    return {"Authorization": f"Bearer {token}"}, community_id


def _key(url: str) -> str:
    return parse_qs(urlparse(url).query)["key"][0]


class TestGetBrowserSources:
    async def test_401_without_token(self, client: Any) -> None:
        assert (await client.get("/api/v1/admin/1/browser-sources")).status_code == 401

    async def test_missing_scope_is_403(self, client: Any, overlay_db: Any) -> None:
        cid = seed_community(overlay_db)
        seed_membership(overlay_db, community_id=cid, user_id=42)
        token = make_user_token(user_id=42, scope="")
        r = await client.get(
            f"/api/v1/admin/{cid}/browser-sources", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 403

    async def test_foreign_tenant_community_is_404(self, client: Any, overlay_db: Any) -> None:
        cid = seed_community(overlay_db, tenant_slug=TENANT_SLUG)
        seed_membership(overlay_db, community_id=cid, user_id=55)
        token = make_user_token(user_id=55, scope=SCOPE_ADMIN, tenant=OTHER_TENANT_SLUG)
        r = await client.get(
            f"/api/v1/admin/{cid}/browser-sources", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 404

    async def test_non_member_is_404_and_leaks_no_key(self, client: Any, overlay_db: Any) -> None:
        cid = seed_community(overlay_db)
        token = make_user_token(user_id=99, scope=SCOPE_ADMIN)
        r = await client.get(
            f"/api/v1/admin/{cid}/browser-sources", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 404
        assert "key=" not in await r.get_data(as_text=True)

    async def test_plan_without_feature_is_402(
        self, client: Any, overlay_db: Any, _flag_on: AsyncMock
    ) -> None:
        _flag_on.return_value = False
        headers, cid = _admin(overlay_db)
        r = await client.get(f"/api/v1/admin/{cid}/browser-sources", headers=headers)
        assert r.status_code == 402

    async def test_exact_field_set_and_url_scheme(self, client: Any, overlay_db: Any) -> None:
        headers, cid = _admin(overlay_db)
        r = await client.get(f"/api/v1/admin/{cid}/browser-sources", headers=headers)
        assert r.status_code == 200
        body = await r.get_json()
        assert set(body) == {"success", "sources"}
        assert [s["sourceType"] for s in body["sources"]] == ["ticker", "media", "general"]
        code = overlay_db.dal.executesql(
            f"SELECT overlay_code FROM communities WHERE id = {cid}"  # noqa: S608
        )[0][0]
        for s, surface in zip(body["sources"], ["ticker", "media", "full_screen"], strict=True):
            assert set(s) == {"sourceType", "url", "isActive", "createdAt"}
            assert s["isActive"] is True
            assert s["url"].startswith(f"https://overlay.example.test/{code}/{surface}?key=")
        assert len({_key(s["url"]) for s in body["sources"]}) == 1

    async def test_stable_across_calls(self, client: Any, overlay_db: Any) -> None:
        headers, cid = _admin(overlay_db)
        a = await (
            await client.get(f"/api/v1/admin/{cid}/browser-sources", headers=headers)
        ).get_json()
        b = await (
            await client.get(f"/api/v1/admin/{cid}/browser-sources", headers=headers)
        ).get_json()
        assert [x["url"] for x in a["sources"]] == [x["url"] for x in b["sources"]]


class TestRegenerate:
    async def test_foreign_community_is_404_and_key_unchanged(
        self, client: Any, overlay_db: Any
    ) -> None:
        headers, cid = _admin(overlay_db)
        before = await (
            await client.get(f"/api/v1/admin/{cid}/browser-sources", headers=headers)
        ).get_json()
        token = make_user_token(user_id=99, scope=SCOPE_ADMIN)
        r = await client.post(
            f"/api/v1/admin/{cid}/browser-sources/regenerate",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 404
        after = await (
            await client.get(f"/api/v1/admin/{cid}/browser-sources", headers=headers)
        ).get_json()
        assert [x["url"] for x in before["sources"]] == [x["url"] for x in after["sources"]]

    async def test_unknown_source_type_is_400(self, client: Any, overlay_db: Any) -> None:
        headers, cid = _admin(overlay_db)
        r = await client.post(
            f"/api/v1/admin/{cid}/browser-sources/regenerate",
            json={"sourceType": "bogus"},
            headers=headers,
        )
        assert r.status_code == 400

    async def test_plan_without_feature_is_402(
        self, client: Any, overlay_db: Any, _flag_on: AsyncMock
    ) -> None:
        headers, cid = _admin(overlay_db)
        _flag_on.return_value = False
        r = await client.post(
            f"/api/v1/admin/{cid}/browser-sources/regenerate", json={}, headers=headers
        )
        assert r.status_code == 402

    @pytest.mark.parametrize("payload", [{}, {"sourceType": "ticker"}])
    async def test_rotation_invalidates_old_key_and_never_echoes_key(
        self, client: Any, overlay_db: Any, payload: dict[str, str]
    ) -> None:
        headers, cid = _admin(overlay_db)
        old = await (
            await client.get(f"/api/v1/admin/{cid}/browser-sources", headers=headers)
        ).get_json()
        old_key = _key(old["sources"][0]["url"])
        r = await client.post(
            f"/api/v1/admin/{cid}/browser-sources/regenerate", json=payload, headers=headers
        )
        assert r.status_code == 200
        body = await r.get_json()
        assert set(body) == {"success", "message"}
        assert old_key not in str(body)
        new = await (
            await client.get(f"/api/v1/admin/{cid}/browser-sources", headers=headers)
        ).get_json()
        new_key = _key(new["sources"][0]["url"])
        assert new_key != old_key
        # svc-presentation validates SHA-256(key) against the credentials row
        row = overlay_db.dal.executesql(
            "SELECT key_hash, previous_key_hash FROM overlay_view_credentials "  # noqa: S608
            f"WHERE community_id = {cid}"
        )[0]
        assert row[0] == overlay_service._hash_key(new_key)  # noqa: SLF001
        assert row[0] != overlay_service._hash_key(old_key)  # noqa: SLF001
        assert row[1] == overlay_service._hash_key(old_key)  # noqa: SLF001


async def test_missing_overlay_code_fails_loud_503(
    overlay_db: Any,
) -> None:
    """Pre-0057 DB (no overlay_code column): refuse rather than emit a dead URL."""
    from services.errors import ApiError

    cid = seed_community(overlay_db)
    with pytest.raises(ApiError) as exc:
        await overlay_service.get_browser_sources(
            overlay_db, overlay_db.dal, _test_config(), community_id=cid
        )
    assert exc.value.status_code == 503
