"""`DELETE /api/v1/auth/oauth/{platform}` -- 400 / 404 instead of a silent 200 no-op.

Standalone Quart app registering only `auth_bp` against the real `auth_db` fixture. An unknown
platform is 400, a platform the caller has no link for is 404, and a real unlink still succeeds
(and removes exactly that one identity row).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.auth import auth_bp
from services import oauth_service
from tests.conftest import make_user_token


@pytest.fixture
def app(auth_db: Any) -> Quart:
    """App with only the auth blueprint."""
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(auth_bp)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    """Test client."""
    return app.test_client()


def _seed_user(auth_db: Any, *, with_password: bool = True) -> int:
    user_id: int = auth_db.dal.hub_users.insert(
        email="u@example.com",
        username="u",
        is_active=True,
        password_hash="x" if with_password else None,
        created_at=datetime.now(UTC),
    )
    auth_db.dal.commit()
    return user_id


def _link(auth_db: Any, user_id: int, platform: str) -> None:
    auth_db.dal.hub_user_identities.insert(
        hub_user_id=user_id,
        platform=platform,
        platform_user_id=f"{platform}-id",
        platform_username=platform,
        linked_at=datetime.now(UTC),
    )
    auth_db.dal.commit()


def _headers(user_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}


async def _identity_count(auth_db: Any, user_id: int) -> int:
    return int(await auth_db.count_async(auth_db.dal.hub_user_identities.hub_user_id == user_id))


class TestOauthUnlink:
    async def test_unknown_platform_is_400(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user(auth_db)
        _link(auth_db, user_id, "discord")
        response = await client.delete(
            "/api/v1/auth/oauth/not-a-platform", headers=_headers(user_id)
        )
        assert response.status_code == 400
        assert await _identity_count(auth_db, user_id) == 1

    async def test_platform_outside_allowlist_even_if_row_exists_is_400(
        self, client: Any, auth_db: Any
    ) -> None:
        """The allowlist (`VALID_PLATFORMS`) is checked before any lookup."""
        assert "myspace" not in oauth_service.VALID_PLATFORMS
        user_id = _seed_user(auth_db)
        _link(auth_db, user_id, "myspace")
        response = await client.delete("/api/v1/auth/oauth/myspace", headers=_headers(user_id))
        assert response.status_code == 400
        assert await _identity_count(auth_db, user_id) == 1

    async def test_valid_platform_not_linked_is_404(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user(auth_db)
        _link(auth_db, user_id, "discord")
        response = await client.delete("/api/v1/auth/oauth/twitch", headers=_headers(user_id))
        assert response.status_code == 404
        assert await _identity_count(auth_db, user_id) == 1

    async def test_another_users_link_is_404(self, client: Any, auth_db: Any) -> None:
        """A link owned by someone else is invisible to the caller (404, row untouched)."""
        owner = _seed_user(auth_db)
        _link(auth_db, owner, "twitch")
        auth_db.dal.hub_users.insert(email="b@example.com", username="b", is_active=True)
        auth_db.dal.commit()
        other = owner + 1
        response = await client.delete("/api/v1/auth/oauth/twitch", headers=_headers(other))
        assert response.status_code == 404
        assert await _identity_count(auth_db, owner) == 1

    async def test_unlink_linked_platform_succeeds(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user(auth_db)
        _link(auth_db, user_id, "discord")
        _link(auth_db, user_id, "twitch")
        response = await client.delete("/api/v1/auth/oauth/discord", headers=_headers(user_id))
        assert response.status_code == 200
        assert (await response.get_json())["success"] is True
        assert await _identity_count(auth_db, user_id) == 1

    async def test_second_unlink_of_same_platform_is_404(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user(auth_db)
        _link(auth_db, user_id, "discord")
        _link(auth_db, user_id, "twitch")
        first = await client.delete("/api/v1/auth/oauth/discord", headers=_headers(user_id))
        second = await client.delete("/api/v1/auth/oauth/discord", headers=_headers(user_id))
        assert (first.status_code, second.status_code) == (200, 404)

    async def test_last_platform_without_password_is_still_400(
        self, client: Any, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db, with_password=False)
        _link(auth_db, user_id, "discord")
        response = await client.delete("/api/v1/auth/oauth/discord", headers=_headers(user_id))
        assert response.status_code == 400
        assert await _identity_count(auth_db, user_id) == 1

    async def test_unauthenticated_is_401(self, client: Any) -> None:
        response = await client.delete("/api/v1/auth/oauth/discord")
        assert response.status_code == 401
