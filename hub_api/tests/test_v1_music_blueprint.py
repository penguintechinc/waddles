"""`blueprints/v1/music.py` -- characterization tests for the M7 music-station port.

Standalone Quart app registering only `music_bp` against the
`streaming_db` fixture (`tests/conftest.py`) -- real JWTs via
`flask_core.auth.create_jwt_token`, real pydal queries, no mocking of the
authz chain itself (mirrors `tests/test_v1_auth_blueprint.py`'s own
pattern).

Fail-first proof (executed, not narrated): temporarily changed
`services.community_authz.require_community_admin_scoped` to always return
the resolved membership without the `is_admin`/`bypass` check (i.e. any
active member, not just admin/moderator, treated as authorized) --
`test_member_without_admin_scope_is_403` went red (200 instead of 403,
an authz bypass letting a plain member update another community's music
settings); reverted, green again. Separately, temporarily removed the
tenant-ownership check in `services.community_authz.
resolve_community_membership_scoped` (the `community_rows` lookup) --
`test_community_in_different_tenant_is_403` went red (200 instead of 403,
a cross-tenant IDOR); reverted, green again.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.music as music_module
from blueprints.v1.music import music_bp
from config import HubAPIConfig
from tests.conftest import TENANT_SLUG, make_user_token, make_user_token_with_roles


def _test_config() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8204,
        grpc_port=50204,
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
        identity_callback_base_url="http://localhost:8204",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
    )


@pytest.fixture
def app(streaming_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(music_bp)
    quart_app.config["dal"] = streaming_db.dal
    quart_app.config["async_dal"] = streaming_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Default the `streaming.music_station` two-gate Feature flag ON for this file's tests."""
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(music_module, "feature_enabled", stub)
    return stub


def _tenant_id(db: Any) -> int:
    row = db.dal(db.dal.tenants.slug == TENANT_SLUG).select().first()
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


def _seed_settings(db: Any, *, community_id: int) -> None:
    db.dal.community_music_settings.insert(
        community_id=community_id,
        default_provider="spotify",
        autoplay_enabled=True,
        volume_limit=80,
        allowed_genres=["pop"],
        blocked_artists=[],
        require_dj_approval=False,
        is_active=True,
    )
    db.dal.commit()


class TestAuthBypass:
    async def test_missing_token_is_401(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        response = await client.get(f"/api/v1/admin/{community_id}/music/settings")
        assert response.status_code == 401


class TestCommunityAdminGate:
    async def test_non_member_is_403(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1, tenant=TENANT_SLUG)}"}
        response = await client.get(f"/api/v1/admin/{community_id}/music/settings", headers=headers)
        assert response.status_code == 403

    async def test_member_without_admin_scope_is_403(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        _seed_member(
            streaming_db,
            community_id=community_id,
            user_id=1,
            role="member",
            scopes=["community:read"],
        )
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.get(f"/api/v1/admin/{community_id}/music/settings", headers=headers)
        assert response.status_code == 403

    async def test_moderator_scope_is_sufficient(self, client: Any, streaming_db: Any) -> None:
        """`community:manage_channels` (the seeded `moderator` role bundle) satisfies the gate."""
        community_id = _seed_community(streaming_db)
        _seed_member(
            streaming_db,
            community_id=community_id,
            user_id=1,
            role="moderator",
            scopes=["community:manage_channels"],
        )
        _seed_settings(streaming_db, community_id=community_id)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.get(f"/api/v1/admin/{community_id}/music/settings", headers=headers)
        assert response.status_code == 200

    async def test_super_admin_role_claim_bypasses(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        _seed_settings(streaming_db, community_id=community_id)
        headers = {
            "Authorization": (
                f"Bearer {make_user_token_with_roles(user_id=99, roles=['super_admin'])}"
            )
        }
        response = await client.get(f"/api/v1/admin/{community_id}/music/settings", headers=headers)
        assert response.status_code == 200

    async def test_tenant_admin_bypasses(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        _seed_settings(streaming_db, community_id=community_id)
        streaming_db.dal.tenant_admins.insert(tenant_id=_tenant_id(streaming_db), user_id=42)
        streaming_db.dal.commit()
        headers = {"Authorization": f"Bearer {make_user_token(user_id=42)}"}
        response = await client.get(f"/api/v1/admin/{community_id}/music/settings", headers=headers)
        assert response.status_code == 200

    async def test_community_in_different_tenant_is_403(
        self, client: Any, streaming_db: Any
    ) -> None:
        """IDOR guard: an admin of a DIFFERENT tenant's community must not reach this one."""
        other_tenant_id = streaming_db.dal.tenants.insert(slug="other-tenant", is_active=True)
        streaming_db.dal.commit()
        community_id = _seed_community(streaming_db, tenant_id=other_tenant_id)
        _seed_member(
            streaming_db,
            community_id=community_id,
            user_id=1,
            role="community-admin",
            scopes=["community:manage_members"],
        )
        # Caller's JWT carries the DEFAULT tenant, not `other-tenant`.
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1, tenant=TENANT_SLUG)}"}
        response = await client.get(f"/api/v1/admin/{community_id}/music/settings", headers=headers)
        assert response.status_code == 403


class TestMusicSettings:
    def _admin_headers(self, db: Any, *, community_id: int, user_id: int = 1) -> dict[str, str]:
        _seed_member(
            db,
            community_id=community_id,
            user_id=user_id,
            role="community-admin",
            scopes=["community:manage_members"],
        )
        return {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}

    async def test_get_settings_not_found_is_404(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.get(f"/api/v1/admin/{community_id}/music/settings", headers=headers)
        assert response.status_code == 404

    async def test_get_settings_success(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        _seed_settings(streaming_db, community_id=community_id)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.get(f"/api/v1/admin/{community_id}/music/settings", headers=headers)
        assert response.status_code == 200
        body = await response.get_json()
        assert body["settings"]["defaultProvider"] == "spotify"
        assert body["settings"]["volumeLimit"] == 80

    async def test_update_settings_no_fields_is_400(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        _seed_settings(streaming_db, community_id=community_id)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.put(
            f"/api/v1/admin/{community_id}/music/settings", headers=headers, json={}
        )
        assert response.status_code == 400

    async def test_update_settings_clamps_volume_and_persists(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        _seed_settings(streaming_db, community_id=community_id)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.put(
            f"/api/v1/admin/{community_id}/music/settings",
            headers=headers,
            json={"volumeLimit": 250, "isActive": False},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["settings"]["volumeLimit"] == 100
        assert body["settings"]["isActive"] is False

    async def test_update_settings_remaining_fields(self, client: Any, streaming_db: Any) -> None:
        """Covers the `defaultProvider`/`autoplayEnabled`/genre-list/`requireDjApproval` fields."""
        community_id = _seed_community(streaming_db)
        _seed_settings(streaming_db, community_id=community_id)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.put(
            f"/api/v1/admin/{community_id}/music/settings",
            headers=headers,
            json={
                "defaultProvider": "youtube",
                "autoplayEnabled": False,
                "allowedGenres": ["rock", "jazz"],
                "blockedArtists": ["nobody"],
                "requireDjApproval": True,
            },
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["settings"]["defaultProvider"] == "youtube"
        assert body["settings"]["autoplayEnabled"] is False
        assert body["settings"]["allowedGenres"] == ["rock", "jazz"]
        assert body["settings"]["blockedArtists"] == ["nobody"]
        assert body["settings"]["requireDjApproval"] is True

    async def test_update_settings_missing_community_is_404(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.put(
            f"/api/v1/admin/{community_id}/music/settings",
            headers=headers,
            json={"isActive": True},
        )
        assert response.status_code == 404


class TestMusicProviders:
    def _admin_headers(self, db: Any, *, community_id: int, user_id: int = 1) -> dict[str, str]:
        _seed_member(
            db,
            community_id=community_id,
            user_id=user_id,
            role="community-admin",
            scopes=["community:manage_members"],
        )
        return {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}

    async def test_get_providers_empty(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.get(
            f"/api/v1/admin/{community_id}/music/providers", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["providers"] == []

    async def test_get_providers_non_admin_is_403(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.get(
            f"/api/v1/admin/{community_id}/music/providers", headers=headers
        )
        assert response.status_code == 403

    async def test_get_providers_lists_connected_provider(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        streaming_db.dal.community_music_providers.insert(
            community_id=community_id,
            provider_name="spotify",
            is_connected=True,
            is_active=True,
            config='{"playlistId": "abc"}',
        )
        streaming_db.dal.commit()
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.get(
            f"/api/v1/admin/{community_id}/music/providers", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["providers"][0]["providerName"] == "spotify"
        assert body["providers"][0]["isConnected"] is True

    async def test_start_oauth_missing_redirect_uri_is_400(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.post(
            f"/api/v1/admin/{community_id}/music/providers/spotify/oauth",
            headers=headers,
            json={"redirectUri": ""},
        )
        assert response.status_code == 400

    async def test_start_oauth_ssrf_redirect_uri_is_400(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.post(
            f"/api/v1/admin/{community_id}/music/providers/spotify/oauth",
            headers=headers,
            json={"redirectUri": "http://169.254.169.254/latest/meta-data/"},
        )
        assert response.status_code == 400

    async def test_start_oauth_invalid_provider_is_400(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.post(
            f"/api/v1/admin/{community_id}/music/providers/napster/oauth",
            headers=headers,
            json={"redirectUri": "https://8.8.8.8/callback"},
        )
        assert response.status_code == 400

    async def test_start_oauth_success(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.post(
            f"/api/v1/admin/{community_id}/music/providers/spotify/oauth",
            headers=headers,
            json={"redirectUri": "https://8.8.8.8/callback"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["authUrl"].startswith("https://accounts.spotify.com/authorize?")
        assert body["stateToken"]

    @pytest.mark.parametrize(
        ("provider", "expected_host"),
        [
            ("soundcloud", "https://soundcloud.com/oauth/authorize?"),
            ("youtube", "https://accounts.google.com/o/oauth2/v2/auth?"),
        ],
    )
    async def test_start_oauth_success_other_providers(
        self, client: Any, streaming_db: Any, provider: str, expected_host: str
    ) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.post(
            f"/api/v1/admin/{community_id}/music/providers/{provider}/oauth",
            headers=headers,
            json={"redirectUri": "https://8.8.8.8/callback"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["authUrl"].startswith(expected_host)

    async def test_disconnect_provider_not_found_is_404(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.delete(
            f"/api/v1/admin/{community_id}/music/providers/spotify", headers=headers
        )
        assert response.status_code == 404

    async def test_disconnect_provider_success(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        streaming_db.dal.community_music_providers.insert(
            community_id=community_id, provider_name="spotify", is_connected=True, is_active=True
        )
        streaming_db.dal.commit()
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.delete(
            f"/api/v1/admin/{community_id}/music/providers/spotify", headers=headers
        )
        assert response.status_code == 200
        # `update_async` never commits (hub_api/PORTING.md Gotcha #2) --
        # assert via `select_async` on the SAME executor connection the
        # write used, not a bare synchronous `dal(...)` call on the main
        # thread's separate connection (which would see the pre-write
        # value and look like the update silently failed).
        rows = await streaming_db.select_async(
            streaming_db.dal(
                streaming_db.dal.community_music_providers.community_id == community_id
            )
        )
        assert rows.first().is_connected is False


class TestRadioStations:
    def _admin_headers(self, db: Any, *, community_id: int, user_id: int = 1) -> dict[str, str]:
        _seed_member(
            db,
            community_id=community_id,
            user_id=user_id,
            role="community-admin",
            scopes=["community:manage_members"],
        )
        return {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}

    async def test_get_radio_stations_non_admin_is_403(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.get(
            f"/api/v1/admin/{community_id}/music/radio-stations", headers=headers
        )
        assert response.status_code == 403

    async def test_get_radio_stations_invalid_page_is_400(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.get(
            f"/api/v1/admin/{community_id}/music/radio-stations?page=not-a-number",
            headers=headers,
        )
        assert response.status_code == 400

    async def test_add_station_missing_fields_is_400(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.post(
            f"/api/v1/admin/{community_id}/music/radio-stations",
            headers=headers,
            json={"name": "", "url": ""},
        )
        assert response.status_code == 400

    async def test_add_station_ssrf_url_is_400(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.post(
            f"/api/v1/admin/{community_id}/music/radio-stations",
            headers=headers,
            json={"name": "Internal", "url": "http://127.0.0.1:8080/admin"},
        )
        assert response.status_code == 400

    async def test_add_get_remove_station_roundtrip(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)

        add_response = await client.post(
            f"/api/v1/admin/{community_id}/music/radio-stations",
            headers=headers,
            json={"name": "Lofi Beats", "url": "https://8.8.8.8/lofi", "genre": "lofi"},
        )
        assert add_response.status_code == 201
        added = await add_response.get_json()
        station_id = added["station"]["id"]
        assert added["station"]["name"] == "Lofi Beats"

        list_response = await client.get(
            f"/api/v1/admin/{community_id}/music/radio-stations", headers=headers
        )
        assert list_response.status_code == 200
        listed = await list_response.get_json()
        assert listed["pagination"]["total"] == 1
        assert len(listed["stations"]) == 1

        remove_response = await client.delete(
            f"/api/v1/admin/{community_id}/music/radio-stations/{station_id}", headers=headers
        )
        assert remove_response.status_code == 200

        remove_again = await client.delete(
            f"/api/v1/admin/{community_id}/music/radio-stations/{station_id}", headers=headers
        )
        assert remove_again.status_code == 404
