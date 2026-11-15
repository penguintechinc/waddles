"""`blueprints/v1/community_profile.py` -- the M2 Core Tenancy-Misc `communityProfile` group.

Standalone Quart app registering only `community_profile_bp` (mirrors
`test_v1_auth_blueprint.py`'s own pattern) against the `auth_db` fixture
(`tests/conftest.py`) -- real JWTs, real pydal queries, no mocking of the
authz chain itself.

Fail-first proof (executed, not narrated): temporarily replaced
`services.community_authz.require_community_scope`'s body with an
unconditional `return CommunityRole(name="community-admin", ...)` --
simulating what a naive `@require_scope("community:admin")`-only port
(checking a flat JWT scope claim, never cross-checked against the
`community_id` in the URL) would let through for ANY caller holding that
scope, for ANY community. `test_update_profile_by_non_member_of_this_
community_is_403` went red (200, the outsider's PUT updated a community
they have no membership row in at all); reverted, green again. This is the
IDOR class `services/community_authz.py`'s own docstring documents.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from flask_core.tenancy import DEFAULT_TENANT_SLUG
from quart import Quart
from quart_schema import QuartSchema
from werkzeug.datastructures import FileStorage

from blueprints.v1.community_profile import community_profile_bp
from config import HubAPIConfig
from services import community_profile_service as svc
from services.errors import ApiError
from tests.conftest import TENANT_SLUG, make_user_token


def _test_config() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8205,
        grpc_port=50205,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        jwt_algorithm="HS256",
        default_tenant_slug=DEFAULT_TENANT_SLUG,
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8205",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
    )


@pytest.fixture
def app(auth_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(community_profile_bp)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _seed_user(auth_db: Any, *, email: str, username: str, email_verified: bool = True) -> int:
    user_id: int = auth_db.dal.hub_users.insert(
        email=email,
        username=username,
        password_hash="x",
        is_active=True,
        email_verified=email_verified,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    auth_db.dal.commit()
    return user_id


def _seed_community(
    auth_db: Any, *, visibility: str = "public", owner_id: str | None = None
) -> int:
    community_id: int = auth_db.dal.communities.insert(
        name=f"acme-{visibility}-{uuid.uuid4()}",
        display_name="Acme",
        description="An acme community",
        is_active=True,
        is_public=True,
        join_mode="open",
        visibility=visibility,
        owner_id=owner_id,
        config={},
    )
    auth_db.dal.commit()
    return community_id


def _seed_role(auth_db: Any, *, community_id: int, name: str, scopes: list[str]) -> int:
    role_id: int = auth_db.dal.community_roles.insert(
        community_id=community_id, name=name, priority=0, base_claims={"scopes": scopes}
    )
    auth_db.dal.commit()
    return role_id


def _seed_member(auth_db: Any, *, community_id: int, user_id: int, role_id: int) -> None:
    auth_db.dal.community_members.insert(
        community_id=community_id,
        user_id=str(user_id),
        role="member",
        is_active=True,
        joined_at=datetime.now(UTC),
        community_role_id=role_id,
    )
    auth_db.dal.commit()


def _auth_header(*, user_id: int) -> dict[str, str]:
    token = make_user_token(user_id=user_id, tenant=TENANT_SLUG)
    return {"Authorization": f"Bearer {token}"}


class TestGetCommunityProfile:
    async def test_public_community_visible_to_anonymous(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db, visibility="public")
        response = await client.get(f"/api/v1/public/communities/{community_id}/profile")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        assert body["community"]["restricted"] is False
        assert body["community"]["visibility"] == "public"

    async def test_registered_only_restricted_for_anonymous(
        self, client: Any, auth_db: Any
    ) -> None:
        community_id = _seed_community(auth_db, visibility="registered")
        response = await client.get(f"/api/v1/public/communities/{community_id}/profile")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["community"]["restricted"] is True

    async def test_registered_only_visible_to_verified_user(
        self, client: Any, auth_db: Any
    ) -> None:
        community_id = _seed_community(auth_db, visibility="registered")
        viewer_id = _seed_user(auth_db, email="viewer@example.com", username="viewer")
        response = await client.get(
            f"/api/v1/public/communities/{community_id}/profile",
            headers=_auth_header(user_id=viewer_id),
        )
        body = await response.get_json()
        assert body["community"]["restricted"] is False

    async def test_missing_community_is_404(self, client: Any) -> None:
        response = await client.get("/api/v1/public/communities/999999/profile")
        assert response.status_code == 404


class TestUpdateCommunityProfile:
    async def test_missing_token_is_401(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        response = await client.put(
            f"/api/v1/admin/{community_id}/profile", json={"displayName": "New Name"}
        )
        assert response.status_code == 401

    async def test_update_profile_by_non_member_of_this_community_is_403(
        self, client: Any, auth_db: Any
    ) -> None:
        """SECURITY (IDOR): a caller with NO membership in `community_id` is rejected.

        `outsider_id` is a `community-admin` of a *different* community
        (`other_community_id`) -- proves `community_id` from the URL is
        cross-checked against a real per-community DB row, not just "does
        this caller hold some community-admin-shaped scope anywhere".
        """
        community_id = _seed_community(auth_db)
        other_community_id = _seed_community(auth_db, visibility="public")
        outsider_id = _seed_user(auth_db, email="outsider@example.com", username="outsider")
        other_admin_role = _seed_role(
            auth_db,
            community_id=other_community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(
            auth_db, community_id=other_community_id, user_id=outsider_id, role_id=other_admin_role
        )

        response = await client.put(
            f"/api/v1/admin/{community_id}/profile",
            json={"displayName": "Hijacked"},
            headers=_auth_header(user_id=outsider_id),
        )
        assert response.status_code == 403

    async def test_update_profile_by_moderator_is_403(self, client: Any, auth_db: Any) -> None:
        """Moderator has `community:manage_channels` (passes the route-level check).

        But is not owner/admin -- `update_community_profile`'s own extra
        role check still rejects, matching `communityProfileController.js`'s
        in-controller `['community-owner','community-admin']` gate.
        """
        community_id = _seed_community(auth_db)
        mod_id = _seed_user(auth_db, email="mod@example.com", username="mod")
        mod_role = _seed_role(
            auth_db,
            community_id=community_id,
            name="moderator",
            scopes=["community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=mod_id, role_id=mod_role)

        response = await client.put(
            f"/api/v1/admin/{community_id}/profile",
            json={"displayName": "Nope"},
            headers=_auth_header(user_id=mod_id),
        )
        assert response.status_code == 403

    async def test_update_profile_by_admin_succeeds(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_user(auth_db, email="admin@example.com", username="admin")
        admin_role = _seed_role(
            auth_db,
            community_id=community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=admin_role)

        response = await client.put(
            f"/api/v1/admin/{community_id}/profile",
            json={"displayName": "New Name", "visibility": "members_only"},
            headers=_auth_header(user_id=admin_id),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["community"]["displayName"] == "New Name"
        assert body["community"]["visibility"] == "members_only"

    async def test_update_profile_invalid_visibility_is_400(
        self, client: Any, auth_db: Any
    ) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_user(auth_db, email="admin2@example.com", username="admin2")
        admin_role = _seed_role(
            auth_db,
            community_id=community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=admin_role)

        response = await client.put(
            f"/api/v1/admin/{community_id}/profile",
            json={"visibility": "not-a-real-visibility"},
            headers=_auth_header(user_id=admin_id),
        )
        assert response.status_code == 400


class TestCommunityLogo:
    async def test_upload_missing_token_is_401(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        response = await client.post(f"/api/v1/admin/{community_id}/logo")
        assert response.status_code == 401

    async def test_delete_by_non_admin_is_403(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        random_id = _seed_user(auth_db, email="rand@example.com", username="rand")
        response = await client.delete(
            f"/api/v1/admin/{community_id}/logo", headers=_auth_header(user_id=random_id)
        )
        assert response.status_code == 403

    async def test_delete_no_logo_set_by_moderator_succeeds(
        self, client: Any, auth_db: Any
    ) -> None:
        """Moderator (`community:manage_channels` only) CAN manage logo/banner.

        Looser than the text-profile-field gate (matches Node's
        route-level-only `requireCommunityAdmin` check for logo/banner, no
        extra controller check).
        """
        community_id = _seed_community(auth_db)
        mod_id = _seed_user(auth_db, email="mod2@example.com", username="mod2")
        mod_role = _seed_role(
            auth_db,
            community_id=community_id,
            name="moderator",
            scopes=["community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=mod_id, role_id=mod_role)

        response = await client.delete(
            f"/api/v1/admin/{community_id}/logo", headers=_auth_header(user_id=mod_id)
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True

    async def test_upload_no_file_is_400(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_user(auth_db, email="admin3@example.com", username="admin3")
        admin_role = _seed_role(
            auth_db,
            community_id=community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=admin_role)

        response = await client.post(
            f"/api/v1/admin/{community_id}/logo", headers=_auth_header(user_id=admin_id)
        )
        assert response.status_code == 400

    async def test_upload_success_replaces_existing_logo(
        self, client: Any, auth_db: Any, monkeypatch: Any
    ) -> None:
        deleted_urls: list[str] = []

        async def _fake_upload(
            data: bytes, filename: str, content_type: str, *, folder: str
        ) -> str:
            assert folder == "community-logos"
            return "https://cdn.example.com/community-logos/new-logo.png"

        async def _fake_delete(url: str) -> None:
            deleted_urls.append(url)

        monkeypatch.setattr(
            "services.community_profile_service.upload_community_asset", _fake_upload
        )
        monkeypatch.setattr("services.community_profile_service.delete_object", _fake_delete)

        community_id = _seed_community(auth_db)
        auth_db.dal(auth_db.dal.communities.id == community_id).update(
            config={"logo_url": "https://cdn.example.com/community-logos/old-logo.png"}
        )
        auth_db.dal.commit()

        admin_id = _seed_user(auth_db, email="admin4@example.com", username="admin4")
        admin_role = _seed_role(
            auth_db,
            community_id=community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=admin_role)

        files = {
            "logo": FileStorage(
                stream=io.BytesIO(b"fake-png-bytes"), filename="logo.png", content_type="image/png"
            )
        }
        response = await client.post(
            f"/api/v1/admin/{community_id}/logo",
            files=files,
            headers=_auth_header(user_id=admin_id),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["logoUrl"] == "https://cdn.example.com/community-logos/new-logo.png"
        assert deleted_urls == ["https://cdn.example.com/community-logos/old-logo.png"]


class TestCommunityBanner:
    async def test_upload_missing_token_is_401(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        response = await client.post(f"/api/v1/admin/{community_id}/banner")
        assert response.status_code == 401

    async def test_upload_success(self, client: Any, auth_db: Any, monkeypatch: Any) -> None:
        async def _fake_upload(
            data: bytes, filename: str, content_type: str, *, folder: str
        ) -> str:
            assert folder == "community-banners"
            return "https://cdn.example.com/community-banners/new-banner.png"

        monkeypatch.setattr(
            "services.community_profile_service.upload_community_asset", _fake_upload
        )

        community_id = _seed_community(auth_db)
        admin_id = _seed_user(auth_db, email="admin5@example.com", username="admin5")
        admin_role = _seed_role(
            auth_db,
            community_id=community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=admin_role)

        files = {
            "banner": FileStorage(
                stream=io.BytesIO(b"fake-png-bytes"),
                filename="banner.png",
                content_type="image/png",
            )
        }
        response = await client.post(
            f"/api/v1/admin/{community_id}/banner",
            files=files,
            headers=_auth_header(user_id=admin_id),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["bannerUrl"] == "https://cdn.example.com/community-banners/new-banner.png"

    async def test_delete_by_non_admin_is_403(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        random_id = _seed_user(auth_db, email="rand2@example.com", username="rand2")
        response = await client.delete(
            f"/api/v1/admin/{community_id}/banner", headers=_auth_header(user_id=random_id)
        )
        assert response.status_code == 403

    async def test_delete_by_admin_succeeds(self, client: Any, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_user(auth_db, email="admin6@example.com", username="admin6")
        admin_role = _seed_role(
            auth_db,
            community_id=community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=admin_role)

        response = await client.delete(
            f"/api/v1/admin/{community_id}/banner", headers=_auth_header(user_id=admin_id)
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True


class TestServiceLayerValidation:
    """Direct `services.community_profile_service` calls.

    Validation branches HTTP round-tripping doesn't reach cheaply (invalid
    URL formats, oversized/invalid file uploads without needing real
    multi-MB payloads, owner_username resolution).
    """

    async def test_get_profile_resolves_owner_username(self, auth_db: Any) -> None:
        owner_id = _seed_user(auth_db, email="owner@example.com", username="theowner")
        community_id = _seed_community(auth_db, visibility="public", owner_id=str(owner_id))
        profile = await svc.get_community_profile(
            auth_db, auth_db.dal, community_id=community_id, viewer_id=None
        )
        assert profile is not None
        assert profile.owner_username == "theowner"

    async def test_update_invalid_website_url_is_bad_request(self, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_user(auth_db, email="urladmin@example.com", username="urladmin")
        role_id = _seed_role(
            auth_db,
            community_id=community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=role_id)

        with pytest.raises(ApiError) as exc_info:
            await svc.update_community_profile(
                auth_db,
                auth_db.dal,
                community_id=community_id,
                user_id=admin_id,
                fields={"websiteUrl": "not-a-url"},
            )
        assert exc_info.value.status_code == 400

    async def test_update_invalid_discord_invite_is_bad_request(self, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_user(auth_db, email="discordadmin@example.com", username="discordadmin")
        role_id = _seed_role(
            auth_db,
            community_id=community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=role_id)

        with pytest.raises(ApiError) as exc_info:
            await svc.update_community_profile(
                auth_db,
                auth_db.dal,
                community_id=community_id,
                user_id=admin_id,
                fields={"discordInviteUrl": "https://evil.example.com/not-discord"},
            )
        assert exc_info.value.status_code == 400

    async def test_update_missing_community_is_not_found(self, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_user(auth_db, email="ghostadmin@example.com", username="ghostadmin")
        role_id = _seed_role(
            auth_db,
            community_id=community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=role_id)
        # Deactivate the community AFTER the admin membership exists, so
        # `require_community_scope` still passes but `_get_active_community`
        # no longer finds it -- exercises `update_community_profile`'s own
        # not_found branch (distinct from the membership-check 403 above).
        auth_db.dal(auth_db.dal.communities.id == community_id).update(is_active=False)
        auth_db.dal.commit()

        with pytest.raises(ApiError) as exc_info:
            await svc.update_community_profile(
                auth_db,
                auth_db.dal,
                community_id=community_id,
                user_id=admin_id,
                fields={"displayName": "New"},
            )
        assert exc_info.value.status_code == 404

    async def test_upload_logo_invalid_content_type_is_bad_request(self, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_user(auth_db, email="badtype@example.com", username="badtype")
        role_id = _seed_role(
            auth_db,
            community_id=community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=role_id)

        with pytest.raises(ApiError) as exc_info:
            await svc.upload_community_logo(
                auth_db,
                auth_db.dal,
                community_id=community_id,
                user_id=admin_id,
                data=b"not-an-image",
                filename="file.txt",
                content_type="text/plain",
                size=13,
            )
        assert exc_info.value.status_code == 400

    async def test_upload_logo_too_large_is_bad_request(self, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_user(auth_db, email="toobig@example.com", username="toobig")
        role_id = _seed_role(
            auth_db,
            community_id=community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=role_id)

        with pytest.raises(ApiError) as exc_info:
            await svc.upload_community_logo(
                auth_db,
                auth_db.dal,
                community_id=community_id,
                user_id=admin_id,
                data=b"x",
                filename="logo.png",
                content_type="image/png",
                size=999_999_999,
            )
        assert exc_info.value.status_code == 400

    async def test_upload_logo_missing_community_is_not_found(
        self, auth_db: Any, monkeypatch: Any
    ) -> None:
        async def _is_super_admin_true(async_dal: Any, dal: Any, *, user_id: int) -> bool:
            return True

        monkeypatch.setattr("services.community_authz.is_super_admin", _is_super_admin_true)

        with pytest.raises(ApiError) as exc_info:
            await svc.upload_community_logo(
                auth_db,
                auth_db.dal,
                community_id=999999,
                user_id=1,
                data=b"x",
                filename="logo.png",
                content_type="image/png",
                size=1,
            )
        assert exc_info.value.status_code == 404

    async def test_upload_banner_invalid_content_type_is_bad_request(self, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_user(auth_db, email="badbanner@example.com", username="badbanner")
        role_id = _seed_role(
            auth_db,
            community_id=community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=role_id)

        with pytest.raises(ApiError) as exc_info:
            await svc.upload_community_banner(
                auth_db,
                auth_db.dal,
                community_id=community_id,
                user_id=admin_id,
                data=b"not-an-image",
                filename="file.txt",
                content_type="text/plain",
                size=13,
            )
        assert exc_info.value.status_code == 400

    async def test_upload_banner_too_large_is_bad_request(self, auth_db: Any) -> None:
        community_id = _seed_community(auth_db)
        admin_id = _seed_user(auth_db, email="toobigbanner@example.com", username="toobigbanner")
        role_id = _seed_role(
            auth_db,
            community_id=community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=role_id)

        with pytest.raises(ApiError) as exc_info:
            await svc.upload_community_banner(
                auth_db,
                auth_db.dal,
                community_id=community_id,
                user_id=admin_id,
                data=b"x",
                filename="banner.png",
                content_type="image/png",
                size=999_999_999,
            )
        assert exc_info.value.status_code == 400

    async def test_delete_logo_replaces_existing(self, auth_db: Any, monkeypatch: Any) -> None:
        deleted_urls: list[str] = []

        async def _fake_delete(url: str) -> None:
            deleted_urls.append(url)

        monkeypatch.setattr("services.community_profile_service.delete_object", _fake_delete)

        community_id = _seed_community(auth_db)
        auth_db.dal(auth_db.dal.communities.id == community_id).update(
            config={"logo_url": "https://cdn.example.com/community-logos/existing.png"}
        )
        auth_db.dal.commit()
        admin_id = _seed_user(auth_db, email="delexisting@example.com", username="delexisting")
        role_id = _seed_role(
            auth_db,
            community_id=community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=role_id)

        await svc.delete_community_logo(
            auth_db, auth_db.dal, community_id=community_id, user_id=admin_id
        )
        assert deleted_urls == ["https://cdn.example.com/community-logos/existing.png"]

    async def test_delete_banner_missing_community_is_not_found(
        self, auth_db: Any, monkeypatch: Any
    ) -> None:
        async def _is_super_admin_true(async_dal: Any, dal: Any, *, user_id: int) -> bool:
            return True

        monkeypatch.setattr("services.community_authz.is_super_admin", _is_super_admin_true)

        with pytest.raises(ApiError) as exc_info:
            await svc.delete_community_banner(auth_db, auth_db.dal, community_id=999999, user_id=1)
        assert exc_info.value.status_code == 404

    async def test_delete_banner_replaces_existing(self, auth_db: Any, monkeypatch: Any) -> None:
        deleted_urls: list[str] = []

        async def _fake_delete(url: str) -> None:
            deleted_urls.append(url)

        monkeypatch.setattr("services.community_profile_service.delete_object", _fake_delete)

        community_id = _seed_community(auth_db)
        auth_db.dal(auth_db.dal.communities.id == community_id).update(
            config={"banner_url": "https://cdn.example.com/community-banners/existing.png"}
        )
        auth_db.dal.commit()
        admin_id = _seed_user(auth_db, email="delbanner@example.com", username="delbanner")
        role_id = _seed_role(
            auth_db,
            community_id=community_id,
            name="community-admin",
            scopes=["community:manage_members", "community:manage_channels"],
        )
        _seed_member(auth_db, community_id=community_id, user_id=admin_id, role_id=role_id)

        await svc.delete_community_banner(
            auth_db, auth_db.dal, community_id=community_id, user_id=admin_id
        )
        assert deleted_urls == ["https://cdn.example.com/community-banners/existing.png"]
