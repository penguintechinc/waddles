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
from flask_core.tenancy import DEFAULT_TENANT_SLUG
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.music as music_module
from blueprints.v1.music import music_bp
from config import HubAPIConfig
from tests.community_ids import cpath, install_community_ids
from tests.conftest import (
    TENANT_SLUG,
    make_user_token,
    make_user_token_with_roles,
    role_kind_fields,
)


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
        default_tenant_slug=DEFAULT_TENANT_SLUG,
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
    install_community_ids(quart_app, streaming_db.dal)
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
            community_id=community_id,
            name=role,
            base_claims={"scopes": scopes},
            **role_kind_fields(role),
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
        response = await client.get(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/settings", community_id)
        )
        assert response.status_code == 401


class TestCommunityAdminGate:
    async def test_non_member_is_403(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1, tenant=TENANT_SLUG)}"}
        response = await client.get(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/settings", community_id),
            headers=headers,
        )
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
        response = await client.get(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/settings", community_id),
            headers=headers,
        )
        assert response.status_code == 403

    async def test_moderator_scope_is_sufficient(self, client: Any, streaming_db: Any) -> None:
        """The moderator-kind role (seeded with `music:station.manage`) satisfies the gate."""
        community_id = _seed_community(streaming_db)
        _seed_member(
            streaming_db,
            community_id=community_id,
            user_id=1,
            role="moderator",
            scopes=["community:manage_channels", "music:station.manage"],
        )
        _seed_settings(streaming_db, community_id=community_id)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.get(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/settings", community_id),
            headers=headers,
        )
        assert response.status_code == 200

    async def test_broad_manage_channels_scope_is_not_music_station(
        self, client: Any, streaming_db: Any
    ) -> None:
        """`community:manage_channels` alone no longer satisfies the dedicated music gate."""
        community_id = _seed_community(streaming_db)
        _seed_member(
            streaming_db,
            community_id=community_id,
            user_id=1,
            role="moderator",
            scopes=["community:manage_channels", "community:manage_members"],
        )
        _seed_settings(streaming_db, community_id=community_id)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.get(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/settings", community_id),
            headers=headers,
        )
        assert response.status_code == 403

    async def test_super_admin_role_claim_bypasses(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        _seed_settings(streaming_db, community_id=community_id)
        headers = {
            "Authorization": (
                f"Bearer {make_user_token_with_roles(user_id=99, roles=['super_admin'])}"
            )
        }
        response = await client.get(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/settings", community_id),
            headers=headers,
        )
        assert response.status_code == 200

    async def test_tenant_admin_bypasses(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        _seed_settings(streaming_db, community_id=community_id)
        streaming_db.dal.tenant_admins.insert(tenant_id=_tenant_id(streaming_db), user_id=42)
        streaming_db.dal.commit()
        headers = {"Authorization": f"Bearer {make_user_token(user_id=42)}"}
        response = await client.get(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/settings", community_id),
            headers=headers,
        )
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
        response = await client.get(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/settings", community_id),
            headers=headers,
        )
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
        response = await client.get(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/settings", community_id),
            headers=headers,
        )
        assert response.status_code == 404

    async def test_get_settings_success(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        _seed_settings(streaming_db, community_id=community_id)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.get(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/settings", community_id),
            headers=headers,
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["settings"]["defaultProvider"] == "spotify"
        assert body["settings"]["volumeLimit"] == 80

    async def test_update_settings_no_fields_is_400(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        _seed_settings(streaming_db, community_id=community_id)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.put(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/settings", community_id),
            headers=headers,
            json={},
        )
        assert response.status_code == 400

    async def test_update_settings_clamps_volume_and_persists(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        _seed_settings(streaming_db, community_id=community_id)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.put(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/settings", community_id),
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
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/settings", community_id),
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
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/settings", community_id),
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
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/providers", community_id),
            headers=headers,
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["providers"] == []

    async def test_get_providers_non_admin_is_403(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.get(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/providers", community_id),
            headers=headers,
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
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/providers", community_id),
            headers=headers,
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
            cpath(
                streaming_db,
                "/api/v1/admin/{community_id}/music/providers/spotify/oauth",
                community_id,
            ),
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
            cpath(
                streaming_db,
                "/api/v1/admin/{community_id}/music/providers/spotify/oauth",
                community_id,
            ),
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
            cpath(
                streaming_db,
                "/api/v1/admin/{community_id}/music/providers/napster/oauth",
                community_id,
            ),
            headers=headers,
            json={"redirectUri": "https://8.8.8.8/callback"},
        )
        assert response.status_code == 400

    async def test_start_oauth_success(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.post(
            cpath(
                streaming_db,
                "/api/v1/admin/{community_id}/music/providers/spotify/oauth",
                community_id,
            ),
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
            cpath(
                streaming_db,
                "/api/v1/admin/{community_id}/music/providers/{provider}/oauth",
                community_id,
                provider=provider,
            ),
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
            cpath(
                streaming_db, "/api/v1/admin/{community_id}/music/providers/spotify", community_id
            ),
            headers=headers,
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
            cpath(
                streaming_db, "/api/v1/admin/{community_id}/music/providers/spotify", community_id
            ),
            headers=headers,
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
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/radio-stations", community_id),
            headers=headers,
        )
        assert response.status_code == 403

    async def test_get_radio_stations_invalid_page_is_400(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.get(
            cpath(
                streaming_db,
                "/api/v1/admin/{community_id}/music/radio-stations?page=not-a-number",
                community_id,
            ),
            headers=headers,
        )
        assert response.status_code == 400

    async def test_add_station_missing_fields_is_400(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.post(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/radio-stations", community_id),
            headers=headers,
            json={"name": "", "url": ""},
        )
        assert response.status_code == 400

    async def test_add_station_ssrf_url_is_400(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)
        response = await client.post(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/radio-stations", community_id),
            headers=headers,
            json={"name": "Internal", "url": "http://127.0.0.1:8080/admin"},
        )
        assert response.status_code == 400

    async def test_add_get_remove_station_roundtrip(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        headers = self._admin_headers(streaming_db, community_id=community_id)

        add_response = await client.post(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/radio-stations", community_id),
            headers=headers,
            json={"name": "Lofi Beats", "url": "https://8.8.8.8/lofi", "genre": "lofi"},
        )
        assert add_response.status_code == 201
        added = await add_response.get_json()
        station_id = added["station"]["id"]
        assert added["station"]["name"] == "Lofi Beats"

        list_response = await client.get(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/radio-stations", community_id),
            headers=headers,
        )
        assert list_response.status_code == 200
        listed = await list_response.get_json()
        assert listed["pagination"]["total"] == 1
        assert len(listed["stations"]) == 1

        remove_response = await client.delete(
            cpath(
                streaming_db,
                "/api/v1/admin/{community_id}/music/radio-stations/{station_id}",
                community_id,
                station_id=station_id,
            ),
            headers=headers,
        )
        assert remove_response.status_code == 200

        remove_again = await client.delete(
            cpath(
                streaming_db,
                "/api/v1/admin/{community_id}/music/radio-stations/{station_id}",
                community_id,
                station_id=station_id,
            ),
            headers=headers,
        )
        assert remove_again.status_code == 404


class TestRadioStationPublicUuid:
    """`<puuid:station_id>`: UUID resolves; a foreign-community UUID and a numeric id are 404."""

    async def test_uuid_resolves_foreign_and_numeric_404(
        self, client: Any, streaming_db: Any
    ) -> None:
        from tests.entity_uuid_support import assert_uuid

        dal = streaming_db.dal
        community_id = _seed_community(streaming_db)
        other = _seed_community(streaming_db)
        admin = TestMusicSettings()._admin_headers
        headers = admin(streaming_db, community_id=community_id)
        other_headers = admin(streaming_db, community_id=other)

        added = await client.post(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/radio-stations", community_id),
            headers=headers,
            json={"name": "Lofi", "url": "https://8.8.8.8/lofi"},
        )
        station_uuid = assert_uuid((await added.get_json())["station"]["id"])
        numeric_id = int(
            dal(dal.community_radio_stations.public_id == station_uuid).select().first().id
        )
        tmpl = "/api/v1/admin/{community_id}/music/radio-stations/{station_id}"

        foreign = await client.delete(
            cpath(streaming_db, tmpl, other, station_id=station_uuid), headers=other_headers
        )
        numeric = await client.delete(
            cpath(streaming_db, tmpl, community_id, station_id=numeric_id), headers=headers
        )
        assert (foreign.status_code, numeric.status_code) == (404, 404)
        assert dal(dal.community_radio_stations.id == numeric_id).count() == 1

        ok = await client.delete(
            cpath(streaming_db, tmpl, community_id, station_id=station_uuid), headers=headers
        )
        assert ok.status_code == 200


_PROVIDER_FIELDS = {
    "communityId",
    "providerName",
    "isConnected",
    "isActive",
    "oauthExpiresAt",
    "lastSync",
    "config",
    "createdAt",
    "updatedAt",
}
_STATION_FIELDS = {
    "id",
    "communityId",
    "name",
    "url",
    "description",
    "genre",
    "isActive",
    "isDefault",
    "lastTestedAt",
    "createdAt",
    "updatedAt",
}
_CONFIG_TMPL = "/api/v1/admin/{community_id}/music/providers/{provider}/config"
_TEST_TMPL = "/api/v1/admin/{community_id}/music/radio-stations/{station_id}/test"
_DEFAULT_TMPL = "/api/v1/admin/{community_id}/music/radio-stations/{station_id}/default"
_LIST_TMPL = "/api/v1/admin/{community_id}/music/radio-stations"


def _admin(db: Any, community_id: int) -> dict[str, str]:
    """Community-admin member + bearer header for `community_id`."""
    _seed_member(
        db,
        community_id=community_id,
        user_id=1,
        role="community-admin",
        scopes=["community:manage_members"],
    )
    return {"Authorization": f"Bearer {make_user_token(user_id=1)}"}


def _foreign_tenant_admin(db: Any) -> tuple[int, dict[str, str]]:
    """A community in ANOTHER tenant whose admin's JWT carries the default tenant."""
    other_tenant_id = db.dal.tenants.insert(slug="other-tenant", is_active=True)
    db.dal.commit()
    community_id = _seed_community(db, tenant_id=other_tenant_id)
    _seed_member(
        db,
        community_id=community_id,
        user_id=1,
        role="community-admin",
        scopes=["community:manage_members"],
    )
    token = make_user_token(user_id=1, tenant=TENANT_SLUG)
    return community_id, {"Authorization": f"Bearer {token}"}


def _seed_provider(db: Any, community_id: int, *, connected: bool = True) -> None:
    db.dal.community_music_providers.insert(
        community_id=community_id,
        provider_name="spotify",
        is_connected=connected,
        is_active=False,
        config='{"playlistId": "abc", "refreshToken": "never-out", "apiKey": "never-out"}',
    )
    db.dal.commit()


def _seed_station(
    db: Any,
    community_id: int,
    *,
    name: str = "Lofi",
    url: str = "https://8.8.8.8/lofi",
    active: bool = True,
) -> str:
    """Insert a station directly; returns its public UUID."""
    table = db.dal.community_radio_stations
    row_id = table.insert(community_id=community_id, name=name, url=url, is_active=active)
    db.dal.commit()
    return str(db.dal(table.id == row_id).select().first().public_id)


def _provider_row(db: Any, community_id: int) -> Any:
    return db.dal(db.dal.community_music_providers.community_id == community_id).select().first()


def _station_row(db: Any, station_uuid: str) -> Any:
    return db.dal(db.dal.community_radio_stations.public_id == station_uuid).select().first()


class _FakeProbeResponse:
    def __init__(self, status: int, content_type: str | None) -> None:
        self.status_code = status
        self.headers = {"content-type": content_type} if content_type else {}


class TestProviderConfig:
    """`PUT .../music/providers/<provider>/config` -- enable/disable a connected provider."""

    async def test_missing_token_is_401(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        response = await client.put(
            cpath(streaming_db, _CONFIG_TMPL, community_id, provider="spotify"),
            json={"enabled": True},
        )
        assert response.status_code == 401

    async def test_member_without_scope_is_403(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        _seed_member(streaming_db, community_id=community_id, user_id=1, scopes=["community:read"])
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.put(
            cpath(streaming_db, _CONFIG_TMPL, community_id, provider="spotify"),
            headers=headers,
            json={"enabled": True},
        )
        assert response.status_code == 403

    async def test_tenant_mismatch_is_403(self, client: Any, streaming_db: Any) -> None:
        community_id, headers = _foreign_tenant_admin(streaming_db)
        _seed_provider(streaming_db, community_id)
        response = await client.put(
            cpath(streaming_db, _CONFIG_TMPL, community_id, provider="spotify"),
            headers=headers,
            json={"enabled": True},
        )
        assert response.status_code == 403
        assert _provider_row(streaming_db, community_id).is_active is False

    async def test_flag_off_is_402(
        self, client: Any, streaming_db: Any, _feature_enabled_default_on: AsyncMock
    ) -> None:
        _feature_enabled_default_on.return_value = False
        community_id = _seed_community(streaming_db)
        _seed_provider(streaming_db, community_id)
        response = await client.put(
            cpath(streaming_db, _CONFIG_TMPL, community_id, provider="spotify"),
            headers=_admin(streaming_db, community_id),
            json={"enabled": True},
        )
        assert response.status_code == 402
        assert _provider_row(streaming_db, community_id).is_active is False

    @pytest.mark.parametrize("body", [{}, {"enabled": "not-a-bool"}, {"enabled": None}])
    async def test_invalid_body_is_400(self, client: Any, streaming_db: Any, body: Any) -> None:
        community_id = _seed_community(streaming_db)
        response = await client.put(
            cpath(streaming_db, _CONFIG_TMPL, community_id, provider="spotify"),
            headers=_admin(streaming_db, community_id),
            json=body,
        )
        assert response.status_code == 400

    async def test_unsupported_provider_is_400(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        response = await client.put(
            cpath(streaming_db, _CONFIG_TMPL, community_id, provider="napster"),
            headers=_admin(streaming_db, community_id),
            json={"enabled": True},
        )
        assert response.status_code == 400

    async def test_unknown_provider_row_is_404(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        response = await client.put(
            cpath(streaming_db, _CONFIG_TMPL, community_id, provider="spotify"),
            headers=_admin(streaming_db, community_id),
            json={"enabled": True},
        )
        assert response.status_code == 404

    async def test_enable_disconnected_is_409(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        _seed_provider(streaming_db, community_id, connected=False)
        response = await client.put(
            cpath(streaming_db, _CONFIG_TMPL, community_id, provider="spotify"),
            headers=_admin(streaming_db, community_id),
            json={"enabled": True},
        )
        assert response.status_code == 409

    async def test_enable_then_disable_exact_fields_no_secrets(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        _seed_provider(streaming_db, community_id)
        headers = _admin(streaming_db, community_id)
        url = cpath(streaming_db, _CONFIG_TMPL, community_id, provider="spotify")

        on = await client.put(url, headers=headers, json={"enabled": True})
        assert on.status_code == 200
        body = await on.get_json()
        assert set(body) == {"success", "provider"}
        assert set(body["provider"]) == _PROVIDER_FIELDS
        assert body["provider"]["isActive"] is True
        assert body["provider"]["config"] == {"playlistId": "abc"}
        assert "never-out" not in str(body)

        off = await client.put(url, headers=headers, json={"enabled": False})
        assert (await off.get_json())["provider"]["isActive"] is False
        row = _provider_row(streaming_db, community_id)
        assert (row.is_active, row.is_connected) == (False, True)

    async def test_list_providers_exact_fields(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        _seed_provider(streaming_db, community_id)
        response = await client.get(
            cpath(streaming_db, "/api/v1/admin/{community_id}/music/providers", community_id),
            headers=_admin(streaming_db, community_id),
        )
        body = await response.get_json()
        assert set(body["providers"][0]) == _PROVIDER_FIELDS
        assert "never-out" not in str(body)


class TestRadioStationTest:
    """`POST .../radio-stations/<uuid>/test` -- header-only SSRF-guarded reachability probe."""

    async def test_member_without_scope_is_403(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        station = _seed_station(streaming_db, community_id)
        _seed_member(streaming_db, community_id=community_id, user_id=1, scopes=["community:read"])
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.post(
            cpath(streaming_db, _TEST_TMPL, community_id, station_id=station), headers=headers
        )
        assert response.status_code == 403

    async def test_tenant_mismatch_is_403(self, client: Any, streaming_db: Any) -> None:
        community_id, headers = _foreign_tenant_admin(streaming_db)
        station = _seed_station(streaming_db, community_id)
        response = await client.post(
            cpath(streaming_db, _TEST_TMPL, community_id, station_id=station), headers=headers
        )
        assert response.status_code == 403

    async def test_flag_off_is_402(
        self, client: Any, streaming_db: Any, _feature_enabled_default_on: AsyncMock
    ) -> None:
        _feature_enabled_default_on.return_value = False
        community_id = _seed_community(streaming_db)
        station = _seed_station(streaming_db, community_id)
        response = await client.post(
            cpath(streaming_db, _TEST_TMPL, community_id, station_id=station),
            headers=_admin(streaming_db, community_id),
        )
        assert response.status_code == 402

    async def test_foreign_community_station_is_404(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        other = _seed_community(streaming_db)
        station = _seed_station(streaming_db, other)
        response = await client.post(
            cpath(streaming_db, _TEST_TMPL, community_id, station_id=station),
            headers=_admin(streaming_db, community_id),
        )
        assert response.status_code == 404

    async def test_reachable_exact_fields_and_stamps_last_tested(
        self, client: Any, streaming_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import services.music_service as svc_module

        probe = AsyncMock(return_value=_FakeProbeResponse(200, "audio/mpeg"))
        monkeypatch.setattr(svc_module, "guarded_probe", probe)
        community_id = _seed_community(streaming_db)
        station = _seed_station(streaming_db, community_id)
        response = await client.post(
            cpath(streaming_db, _TEST_TMPL, community_id, station_id=station),
            headers=_admin(streaming_db, community_id),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert set(body) == {"success", "result"}
        assert set(body["result"]) == {"reachable", "httpStatus", "contentType", "testedAt"}
        assert body["result"]["reachable"] is True
        assert body["result"]["httpStatus"] == 200
        assert body["result"]["contentType"] == "audio/mpeg"
        assert probe.await_args is not None
        assert probe.await_args.args[1] == "https://8.8.8.8/lofi"
        assert _station_row(streaming_db, station).last_tested_at is not None

    async def test_non_2xx_is_unreachable(
        self, client: Any, streaming_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import services.music_service as svc_module

        monkeypatch.setattr(
            svc_module, "guarded_probe", AsyncMock(return_value=_FakeProbeResponse(404, None))
        )
        community_id = _seed_community(streaming_db)
        station = _seed_station(streaming_db, community_id)
        response = await client.post(
            cpath(streaming_db, _TEST_TMPL, community_id, station_id=station),
            headers=_admin(streaming_db, community_id),
        )
        result = (await response.get_json())["result"]
        assert (result["reachable"], result["httpStatus"]) == (False, 404)

    async def test_transport_error_is_unreachable_not_500(
        self, client: Any, streaming_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        import services.music_service as svc_module

        monkeypatch.setattr(
            svc_module, "guarded_probe", AsyncMock(side_effect=httpx.ConnectError("refused"))
        )
        community_id = _seed_community(streaming_db)
        station = _seed_station(streaming_db, community_id)
        response = await client.post(
            cpath(streaming_db, _TEST_TMPL, community_id, station_id=station),
            headers=_admin(streaming_db, community_id),
        )
        assert response.status_code == 200
        result = (await response.get_json())["result"]
        assert (result["reachable"], result["httpStatus"]) == (False, None)

    async def test_private_url_is_blocked_400_real_guard(
        self, client: Any, streaming_db: Any
    ) -> None:
        """Real `guarded_probe`: a stored URL pointing at loopback is refused, never fetched."""
        community_id = _seed_community(streaming_db)
        station = _seed_station(streaming_db, community_id, url="http://127.0.0.1:8080/admin")
        response = await client.post(
            cpath(streaming_db, _TEST_TMPL, community_id, station_id=station),
            headers=_admin(streaming_db, community_id),
        )
        assert response.status_code == 400
        assert _station_row(streaming_db, station).last_tested_at is None


class TestRadioStationDefault:
    """`PUT .../radio-stations/<uuid>/default` -- one active default per community."""

    async def test_member_without_scope_is_403(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        station = _seed_station(streaming_db, community_id)
        _seed_member(streaming_db, community_id=community_id, user_id=1, scopes=["community:read"])
        headers = {"Authorization": f"Bearer {make_user_token(user_id=1)}"}
        response = await client.put(
            cpath(streaming_db, _DEFAULT_TMPL, community_id, station_id=station), headers=headers
        )
        assert response.status_code == 403
        assert _station_row(streaming_db, station).is_default is False

    async def test_tenant_mismatch_is_403(self, client: Any, streaming_db: Any) -> None:
        community_id, headers = _foreign_tenant_admin(streaming_db)
        station = _seed_station(streaming_db, community_id)
        response = await client.put(
            cpath(streaming_db, _DEFAULT_TMPL, community_id, station_id=station), headers=headers
        )
        assert response.status_code == 403

    async def test_flag_off_is_402(
        self, client: Any, streaming_db: Any, _feature_enabled_default_on: AsyncMock
    ) -> None:
        _feature_enabled_default_on.return_value = False
        community_id = _seed_community(streaming_db)
        station = _seed_station(streaming_db, community_id)
        response = await client.put(
            cpath(streaming_db, _DEFAULT_TMPL, community_id, station_id=station),
            headers=_admin(streaming_db, community_id),
        )
        assert response.status_code == 402

    async def test_numeric_or_foreign_station_is_404(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        other = _seed_community(streaming_db)
        foreign = _seed_station(streaming_db, other)
        headers = _admin(streaming_db, community_id)
        own = _seed_station(streaming_db, community_id)
        numeric = _station_row(streaming_db, own).id
        for sid in (foreign, numeric):
            response = await client.put(
                cpath(streaming_db, _DEFAULT_TMPL, community_id, station_id=sid), headers=headers
            )
            assert response.status_code == 404
        assert _station_row(streaming_db, foreign).is_default is False

    async def test_inactive_station_is_409(self, client: Any, streaming_db: Any) -> None:
        community_id = _seed_community(streaming_db)
        station = _seed_station(streaming_db, community_id, active=False)
        response = await client.put(
            cpath(streaming_db, _DEFAULT_TMPL, community_id, station_id=station),
            headers=_admin(streaming_db, community_id),
        )
        assert response.status_code == 409

    async def test_switching_default_clears_previous_exact_fields(
        self, client: Any, streaming_db: Any
    ) -> None:
        community_id = _seed_community(streaming_db)
        other = _seed_community(streaming_db)
        other_default = _seed_station(streaming_db, other, name="Other")
        other_resp = await client.put(
            cpath(streaming_db, _DEFAULT_TMPL, other, station_id=other_default),
            headers=_admin(streaming_db, other),
        )
        assert other_resp.status_code == 200
        headers = _admin(streaming_db, community_id)
        first = _seed_station(streaming_db, community_id, name="A")
        second = _seed_station(streaming_db, community_id, name="B")

        r1 = await client.put(
            cpath(streaming_db, _DEFAULT_TMPL, community_id, station_id=first), headers=headers
        )
        assert r1.status_code == 200
        body = await r1.get_json()
        assert set(body) == {"success", "station"}
        assert set(body["station"]) == _STATION_FIELDS
        assert (body["station"]["id"], body["station"]["isDefault"]) == (first, True)

        r2 = await client.put(
            cpath(streaming_db, _DEFAULT_TMPL, community_id, station_id=second), headers=headers
        )
        assert r2.status_code == 200
        listed = await client.get(cpath(streaming_db, _LIST_TMPL, community_id), headers=headers)
        stations = (await listed.get_json())["stations"]
        assert all(set(s) == _STATION_FIELDS for s in stations)
        assert {s["id"]: s["isDefault"] for s in stations} == {first: False, second: True}
        # Another community's default is untouched (community-scoped clear).
        assert _station_row(streaming_db, other_default).is_default is True


class TestGuardedProbe:
    """`services.url_guard.guarded_probe` -- headers only, every redirect hop re-validated."""

    async def test_redirect_to_private_is_blocked(self) -> None:
        import httpx

        from services.errors import ApiError
        from services.url_guard import guarded_probe

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            with pytest.raises(ApiError) as exc:
                await guarded_probe(client, "https://8.8.8.8/stream")
        assert exc.value.status_code == 400

    async def test_redirect_loop_is_capped(self) -> None:
        import httpx

        from services.errors import ApiError
        from services.url_guard import guarded_probe

        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(302, headers={"location": "https://8.8.4.4/again"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            with pytest.raises(ApiError):
                await guarded_probe(client, "https://8.8.8.8/stream")
        assert calls == 6

    async def test_follows_public_redirect_and_returns_headers(self) -> None:
        import httpx

        from services.url_guard import guarded_probe

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "8.8.8.8":
                return httpx.Response(301, headers={"location": "https://8.8.4.4/live"})
            return httpx.Response(200, headers={"content-type": "audio/aac"}, content=b"x" * 10)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            response = await guarded_probe(client, "https://8.8.8.8/stream")
        assert (response.status_code, response.headers["content-type"]) == (200, "audio/aac")


#: Every JSON response read here must carry community ids as UUID strings (conftest guard).
UUID_COMMUNITY_RESPONSES = True
