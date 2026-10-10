"""Ban-evasion re-catch at the three places a platform identity gets linked to a hub account.

A banned person can erase their account, sign up again with a fresh hub account, join a community
first and link the banned platform account LAST. `services/ban_suppression.reapply_for_hub_user`
closes that: once the platform id is known it is re-checked against the tenant lists of every
tenant the account already belongs to. Wired into `oauth_service._find_or_create_user_from_oauth`
(new account), `oauth_service.link_callback` and `identity_service.identity_link_callback`.

Real pydal + real HKDF/HMAC against the shared `auth_db` fixture. Only the two things that talk to
the network / to Postgres-only SQL are replaced: the OAuth code exchange, and
`add_user_to_global_community` (raw `ON CONFLICT` SQL sqlite cannot run) -- the latter by a stub
that creates exactly the membership row it would have.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services import identity_service, oauth_service
from services import suppression_keys as keys
from tests.conftest import TENANT_SLUG

MASTER_HEX = "ab" * 32
PLATFORM_ID = "d-evader-1"


@pytest.fixture(autouse=True)
def master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(keys.ENV_CURRENT, MASTER_HEX)
    monkeypatch.delenv(keys.ENV_PREVIOUS, raising=False)


def _now() -> datetime:
    return datetime.now(UTC)


def _tenant(auth_db: Any, slug: str = TENANT_SLUG) -> int:
    row = auth_db.dal(auth_db.dal.tenants.slug == slug).select().first()
    auth_db.dal.commit()
    if row is not None:
        return int(row.id)
    new_id = auth_db.dal.tenants.insert(slug=slug, display_name=slug, is_active=True)
    auth_db.dal.commit()
    return int(new_id)


def _community(auth_db: Any, tenant_id: int) -> int:
    community_id = auth_db.dal.communities.insert(
        name=f"c-{tenant_id}-{_now().timestamp()}", tenant_id=tenant_id, is_active=True
    )
    auth_db.dal.commit()
    return int(community_id)


def _user(auth_db: Any, name: str) -> int:
    user_id = auth_db.dal.hub_users.insert(
        email=f"{name}@example.com", username=name, is_active=True, created_at=_now()
    )
    auth_db.dal.commit()
    return int(user_id)


def _member(auth_db: Any, community_id: int, user_id: int) -> int:
    member_id = auth_db.dal.community_members.insert(
        community_id=community_id,
        user_id=str(user_id),
        role="member",
        is_active=True,
        joined_at=_now(),
    )
    auth_db.dal.commit()
    return int(member_id)


def _suppress(auth_db: Any, tenant_id: int, platform_user_id: str = PLATFORM_ID) -> None:
    code = keys.current_code(keys.PlatformKekKeySource(), tenant_id, "discord", platform_user_id)
    auth_db.dal.suppression_records.insert(
        tenant_id=tenant_id,
        platform="discord",
        key_ver=code.key_ver,
        hmac=code.hmac,
        reason_enum="ban",
        expires_at=None,
        created_at=_now(),
    )
    auth_db.dal.commit()


def _role(auth_db: Any, member_id: int) -> tuple[str, bool]:
    row = auth_db.dal(auth_db.dal.community_members.id == member_id).select().first()
    auth_db.dal.commit()
    return row.role, bool(row.is_active)


class TestNewAccountViaOAuth:
    async def test_a_suppressed_platform_id_gets_its_membership_banned_on_signup(
        self, auth_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = _tenant(auth_db)
        community = _community(auth_db, tenant)
        _suppress(auth_db, tenant)
        created: dict[str, int] = {}

        async def fake_global_join(async_dal: Any, dal: Any, *, user_id: int) -> None:
            created["member"] = _member(auth_db, community, user_id)

        monkeypatch.setattr("services.auth_service.add_user_to_global_community", fake_global_join)

        user = await oauth_service._find_or_create_user_from_oauth(  # noqa: SLF001
            auth_db,
            auth_db.dal,
            platform="discord",
            user_data={"id": PLATFORM_ID, "username": "evader", "email": "evader@example.com"},
        )

        assert user.id
        assert _role(auth_db, created["member"]) == ("banned", False)

    async def test_an_unsuppressed_platform_id_keeps_its_membership(
        self, auth_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = _tenant(auth_db)
        community = _community(auth_db, tenant)
        _suppress(auth_db, tenant, "someone-else")
        created: dict[str, int] = {}

        async def fake_global_join(async_dal: Any, dal: Any, *, user_id: int) -> None:
            created["member"] = _member(auth_db, community, user_id)

        monkeypatch.setattr("services.auth_service.add_user_to_global_community", fake_global_join)

        await oauth_service._find_or_create_user_from_oauth(  # noqa: SLF001
            auth_db,
            auth_db.dal,
            platform="discord",
            user_data={"id": PLATFORM_ID, "username": "fine", "email": "fine@example.com"},
        )

        assert _role(auth_db, created["member"]) == ("member", True)

    async def test_a_key_failure_with_live_rows_is_loud(
        self, auth_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = _tenant(auth_db)
        community = _community(auth_db, tenant)
        _suppress(auth_db, tenant)

        async def fake_global_join(async_dal: Any, dal: Any, *, user_id: int) -> None:
            _member(auth_db, community, user_id)

        monkeypatch.setattr("services.auth_service.add_user_to_global_community", fake_global_join)
        monkeypatch.delenv(keys.ENV_CURRENT)

        with pytest.raises(keys.SuppressionKeyError):
            await oauth_service._find_or_create_user_from_oauth(  # noqa: SLF001
                auth_db,
                auth_db.dal,
                platform="discord",
                user_data={"id": PLATFORM_ID, "username": "x", "email": "x@example.com"},
            )


class TestLinkCallbacks:
    @staticmethod
    def _state(auth_db: Any, user_id: int, state: str) -> None:
        auth_db.dal.hub_oauth_states.insert(
            state=state,
            mode="link",
            platform="discord",
            user_id=user_id,
            expires_at=_now() + timedelta(minutes=10),
            created_at=_now(),
        )
        auth_db.dal.commit()

    async def test_oauth_link_callback_bans_matching_memberships_only_in_the_banned_tenant(
        self, auth_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        acme, beta = _tenant(auth_db), _tenant(auth_db, "beta")
        user = _user(auth_db, "joiner")
        acme_member = _member(auth_db, _community(auth_db, acme), user)
        beta_member = _member(auth_db, _community(auth_db, beta), user)
        _suppress(auth_db, acme)
        self._state(auth_db, user, "state-oauth-link")

        async def creds(*_a: Any, **_k: Any) -> dict[str, str]:
            return {"client_id": "x", "client_secret": "y"}

        async def exchange(*_a: Any, **_k: Any) -> dict[str, Any]:
            return {"id": PLATFORM_ID, "username": "evader", "avatar_url": None}

        monkeypatch.setattr(oauth_service, "_get_platform_credentials", creds)
        monkeypatch.setattr(oauth_service, "_exchange_code", exchange)

        await oauth_service.link_callback(
            auth_db,
            auth_db.dal,
            platform="discord",
            code="c",
            state="state-oauth-link",
            callback_base_url="http://localhost",
        )

        assert _role(auth_db, acme_member) == ("banned", False)
        assert _role(auth_db, beta_member) == ("member", True)  # per tenant only

    async def test_oauth_link_retry_after_a_failed_recheck_still_applies_the_ban(
        self, auth_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = _tenant(auth_db)
        user = _user(auth_db, "retrier")
        member = _member(auth_db, _community(auth_db, tenant), user)
        _suppress(auth_db, tenant)

        async def creds(*_a: Any, **_k: Any) -> dict[str, str]:
            return {"client_id": "x", "client_secret": "y"}

        async def exchange(*_a: Any, **_k: Any) -> dict[str, Any]:
            return {"id": PLATFORM_ID, "username": "evader", "avatar_url": None}

        monkeypatch.setattr(oauth_service, "_get_platform_credentials", creds)
        monkeypatch.setattr(oauth_service, "_exchange_code", exchange)
        monkeypatch.delenv(keys.ENV_CURRENT)
        self._state(auth_db, user, "state-first")
        with pytest.raises(keys.SuppressionKeyError):
            await oauth_service.link_callback(
                auth_db,
                auth_db.dal,
                platform="discord",
                code="c",
                state="state-first",
                callback_base_url="http://localhost",
            )
        # The identity WAS linked before the re-check failed; the retry must still re-check.
        assert _role(auth_db, member) == ("member", True)
        monkeypatch.setenv(keys.ENV_CURRENT, MASTER_HEX)
        self._state(auth_db, user, "state-second")

        await oauth_service.link_callback(
            auth_db,
            auth_db.dal,
            platform="discord",
            code="c",
            state="state-second",
            callback_base_url="http://localhost",
        )

        assert _role(auth_db, member) == ("banned", False)

    async def test_identity_link_callback_bans_a_matching_membership(
        self, auth_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = _tenant(auth_db)
        user = _user(auth_db, "linker")
        member = _member(auth_db, _community(auth_db, tenant), user)
        _suppress(auth_db, tenant)
        self._state(auth_db, user, "state-identity-link")

        async def exchange(*_a: Any, **_k: Any) -> dict[str, Any]:
            return {"id": PLATFORM_ID, "username": "evader", "avatar_url": None}

        monkeypatch.setattr(oauth_service, "exchange_code", exchange)

        await identity_service.identity_link_callback(
            auth_db,
            auth_db.dal,
            platform="discord",
            code="c",
            state="state-identity-link",
            callback_base_url="http://localhost",
        )

        assert _role(auth_db, member) == ("banned", False)

    async def test_identity_link_retry_when_already_linked_still_applies_the_ban(
        self, auth_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = _tenant(auth_db)
        user = _user(auth_db, "relinker")
        member = _member(auth_db, _community(auth_db, tenant), user)
        auth_db.dal.hub_user_identities.insert(
            hub_user_id=user, platform="discord", platform_user_id=PLATFORM_ID, linked_at=_now()
        )
        auth_db.dal.commit()
        _suppress(auth_db, tenant)
        self._state(auth_db, user, "state-relink")

        async def exchange(*_a: Any, **_k: Any) -> dict[str, Any]:
            return {"id": PLATFORM_ID, "username": "evader", "avatar_url": None}

        monkeypatch.setattr(oauth_service, "exchange_code", exchange)

        await identity_service.identity_link_callback(
            auth_db,
            auth_db.dal,
            platform="discord",
            code="c",
            state="state-relink",
            callback_base_url="http://localhost",
        )

        assert _role(auth_db, member) == ("banned", False)

    async def test_linking_with_no_suppression_changes_nothing(
        self, auth_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = _tenant(auth_db)
        user = _user(auth_db, "innocent")
        member = _member(auth_db, _community(auth_db, tenant), user)
        self._state(auth_db, user, "state-clean")

        async def exchange(*_a: Any, **_k: Any) -> dict[str, Any]:
            return {"id": PLATFORM_ID, "username": "fine", "avatar_url": None}

        monkeypatch.setattr(oauth_service, "exchange_code", exchange)
        monkeypatch.delenv(keys.ENV_CURRENT)  # no live rows -> the key is never read

        await identity_service.identity_link_callback(
            auth_db,
            auth_db.dal,
            platform="discord",
            code="c",
            state="state-clean",
            callback_base_url="http://localhost",
        )

        assert _role(auth_db, member) == ("member", True)
