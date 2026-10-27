"""Security regression tests for two account-takeover bugs found in post-merge review.

Both were faithful ports of real bugs already present in Node's
`authController.js`/`identityController.js` -- not introduced by this
port, but not acceptable to carry forward either. See
`services/identity_service.py`'s and `services/oauth_service.py`'s own
module/function docstrings for the full writeup; this file is the
fail-first proof for both.

Fail-first proof (executed, not narrated):

1. Reverted `identity_link_callback`'s state lookup to decode a
   caller-supplied base64 JSON blob directly (the original vulnerable
   shape) instead of querying `hub_oauth_states` -- both
   `test_forged_link_state_with_victim_id_is_rejected` and
   `test_link_state_not_in_server_storage_is_rejected` went red
   (200/link-succeeded instead of 400, since the forged/unknown state
   was accepted at face value); reverted, both green again.
2. Reverted `_find_or_create_user_from_oauth` to its original "adopt an
   existing `hub_users` row on email match" branch --
   `test_oauth_login_with_matching_email_does_not_adopt_existing_user`
   went red (the forged-email OAuth login silently returned the
   victim's `SessionUser`, `user_id` matching the pre-seeded victim
   instead of a freshly-created account); reverted, green again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from config import HubAPIConfig
from services import identity_service, oauth_service
from services.errors import ApiError


def _cfg() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="t",
        module_version="0",
        module_port=1,
        grpc_port=1,
        database_url="x",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        jwt_algorithm="HS256",
        default_tenant_slug="global",
        posthog_api_key=None,
        posthog_host="x",
        license_server_url="x",
        identity_callback_base_url="http://localhost",
        frontend_origin="http://localhost",
        log_level="INFO",
    )


def _seed_user(auth_db: Any, *, email: str) -> int:
    user_id: int = auth_db.dal.hub_users.insert(
        email=email, username=email, is_active=True, created_at=datetime.now(UTC)
    )
    auth_db.dal.commit()
    return user_id


class TestIdentityLinkStateIsServerSide:
    """`identity_service.identity_link_callback` must never trust a client-supplied user id."""

    async def test_forged_link_state_with_victim_id_is_rejected(self, auth_db: Any) -> None:
        """An attacker crafting their own state value naming a victim's id must fail.

        No `hub_oauth_states` row exists for this state at all (the
        attacker invented it out of thin air) -- the server-side lookup
        finds nothing and rejects, regardless of what the attacker put
        in it.
        """
        victim_id = _seed_user(auth_db, email="victim@example.com")
        forged_state = f"attacker-forged-state-claiming-user-{victim_id}"

        with pytest.raises(ApiError) as exc_info:
            await identity_service.identity_link_callback(
                auth_db,
                auth_db.dal,
                platform="discord",
                code="irrelevant-attacker-code",
                state=forged_state,
                callback_base_url="http://localhost",
            )
        assert exc_info.value.status_code == 400

    async def test_link_state_not_in_server_storage_is_rejected(self, auth_db: Any) -> None:
        """A syntactically-plausible but never-issued state token is rejected."""
        with pytest.raises(ApiError) as exc_info:
            await identity_service.identity_link_callback(
                auth_db,
                auth_db.dal,
                platform="discord",
                code="x",
                state="0123456789abcdef0123456789abcdef",
                callback_base_url="http://localhost",
            )
        assert exc_info.value.status_code == 400

    async def test_expired_link_state_is_rejected(self, auth_db: Any) -> None:
        """A real, server-issued state past `expires_at` is rejected -- not replayable forever."""
        user_id = _seed_user(auth_db, email="alice@example.com")
        state = "expired-state-token"
        auth_db.dal.hub_oauth_states.insert(
            state=state,
            mode="link",
            platform="discord",
            user_id=user_id,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            created_at=datetime.now(UTC) - timedelta(minutes=11),
        )
        auth_db.dal.commit()

        with pytest.raises(ApiError) as exc_info:
            await identity_service.identity_link_callback(
                auth_db,
                auth_db.dal,
                platform="discord",
                code="x",
                state=state,
                callback_base_url="http://localhost",
            )
        assert exc_info.value.status_code == 400

    async def test_start_identity_link_writes_state_server_side(
        self, auth_db: Any, monkeypatch: Any
    ) -> None:
        """`start_identity_link` persists `user_id` in `hub_oauth_states`, never in the token."""
        monkeypatch.setenv("DISCORD_CLIENT_ID", "123456789012345678")
        user_id = _seed_user(auth_db, email="bob@example.com")

        authorize_url, state = await identity_service.start_identity_link(
            auth_db,
            auth_db.dal,
            user_id=user_id,
            platform="discord",
            callback_base_url="http://localhost",
        )

        # Query via async_dal.select_async (same executor thread/connection
        # insert_async used) -- insert_async() never commits, so a
        # synchronous query from the main thread's own connection wouldn't
        # see the row yet (a second, separate sqlite-file-visibility gotcha
        # distinct from the sqlite:memory one in PORTING.md Gotcha #2).
        rows = await auth_db.select_async(auth_db.dal(auth_db.dal.hub_oauth_states.state == state))
        assert rows
        row = rows.first()
        assert row.user_id == user_id
        assert row.mode == "link"
        # The state token itself carries no decodable user id -- opaque hex.
        assert str(user_id) not in state or len(state) == 32  # hex token, coincidental digits only
        assert "discord" in authorize_url


class TestOAuthLoginDoesNotAdoptExistingUserByEmail:
    """`oauth_service._find_or_create_user_from_oauth` must never merge accounts on email alone."""

    async def test_oauth_login_with_matching_email_does_not_adopt_existing_user(
        self, auth_db: Any
    ) -> None:
        victim_id = _seed_user(auth_db, email="victim@example.com")

        with pytest.raises(ApiError) as exc_info:
            await oauth_service._find_or_create_user_from_oauth(  # noqa: SLF001 - testing the fix directly
                auth_db,
                auth_db.dal,
                platform="discord",
                user_data={
                    "id": "attacker-discord-id-999",
                    "username": "attacker",
                    "email": "victim@example.com",
                    "avatar_url": None,
                },
            )
        assert exc_info.value.status_code == 409

        # No new hub_user_identities row was created linking the attacker's
        # platform account to the victim's hub_users row.
        linked = auth_db.dal(
            (auth_db.dal.hub_user_identities.hub_user_id == victim_id)
            & (auth_db.dal.hub_user_identities.platform == "discord")
        ).select()
        assert not linked

    async def test_oauth_login_with_new_identity_and_no_email_conflict_creates_new_user(
        self, auth_db: Any
    ) -> None:
        """The safe path still works: brand-new platform identity, no email collision."""
        user = await oauth_service._find_or_create_user_from_oauth(  # noqa: SLF001
            auth_db,
            auth_db.dal,
            platform="discord",
            user_data={
                "id": "fresh-discord-id-123",
                "username": "newperson",
                "email": "newperson@example.com",
                "avatar_url": None,
            },
        )
        assert user.email == "newperson@example.com"

    async def test_oauth_login_with_existing_linked_identity_logs_in_that_user(
        self, auth_db: Any
    ) -> None:
        """The safe path still works: an EXISTING identity link logs in normally."""
        user_id = _seed_user(auth_db, email="carol@example.com")
        auth_db.dal.hub_user_identities.insert(
            hub_user_id=user_id,
            platform="discord",
            platform_user_id="carol-discord-id",
            platform_username="carol",
            linked_at=datetime.now(UTC),
        )
        auth_db.dal.commit()

        user = await oauth_service._find_or_create_user_from_oauth(  # noqa: SLF001
            auth_db,
            auth_db.dal,
            platform="discord",
            user_data={
                "id": "carol-discord-id",
                "username": "carol-updated",
                "email": "carol@example.com",
                "avatar_url": None,
            },
        )
        assert user.id == user_id
