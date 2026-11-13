"""Account-linking hardening against a REAL Postgres (v3.0 identity-link review).

1. Unlinking through `/user/identities` signs the user out of every OTHER session, exactly like
   the OAuth unlink already did (a compromised login method must not leave its sessions alive).
2. Link and unlink (both endpoint families) write an `audit_log` row naming the user by UUID,
   with platform + actor only -- never an email, username, handle or platform user id.
3. A renamed Twitch login does not keep routing to its old owner: `@oldname` resolves only via a
   handle FRESHLY asserted by ingest for a numeric id, and a new claimant takes the name over.

Real migrations on Postgres 17 and the real pydal `AsyncDAL`. Skipped where docker is absent.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from flask_core.database import AsyncDAL
from pydal import Field

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services import ban_suppression, identity_service, login_providers, oauth_service  # noqa: E402
from services.identity_resolution_service import (  # noqa: E402
    HandleNotFoundError,
    IdentityRequest,
    resolve_identities,
    resolve_target,
)
from services.install_state_binding import bound_state  # noqa: E402
from services.schema import bind_auth_tables  # noqa: E402

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="docker CLI not available")

EMAIL = "victim-email@example.test"
USERNAME = "victim_username_pii"
HANDLE = "victim_handle_pii"
PUID = "9876543210"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head."""
    with migrated_postgres("hub-api-identity-link-hardening") as db:
        yield db


@pytest.fixture
async def adal(pg_db: PgTestDatabase) -> AsyncIterator[AsyncDAL]:
    """A real `AsyncDAL` with the auth tables bound (`migrate=False`: schema is the real one)."""
    async_dal = AsyncDAL(pg_db.dsn.replace("postgresql://", "postgres://", 1), pool_size=1)
    dal = async_dal.dal
    if "tenants" not in dal.tables:
        dal.define_table("tenants", Field("slug"), migrate=False)
    bind_auth_tables(dal, migrate=False)
    # Legacy-baseline `audit_log` (000_create_base_schema.sql) is not in pg_docker's alembic-only
    # bootstrap; production has it (same DDL as test_audit_pg_integration).
    await async_dal.executesql_async(
        "CREATE TABLE IF NOT EXISTS audit_log (id SERIAL PRIMARY KEY, "
        "user_id INTEGER REFERENCES hub_users(id) ON DELETE SET NULL, "
        "action VARCHAR(100) NOT NULL, target_type VARCHAR(50), target_id VARCHAR(255), "
        "details JSONB DEFAULT '{}', ip_address VARCHAR(45), user_agent TEXT, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    # pg_docker's alembic-only bootstrap has minimal hub_users / hub_user_identities: add every
    # column the auth binding uses (production's 000_create_base_schema.sql already has them).
    sql_type = {
        "string": "VARCHAR(255)",
        "text": "TEXT",
        "boolean": "BOOLEAN",
        "datetime": "TIMESTAMP",
        "integer": "INTEGER",
    }
    for table in ("hub_users", "hub_user_identities"):
        for f in dal[table]:
            if f.name != "id" and f.type in sql_type:
                await async_dal.executesql_async(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {f.name} {sql_type[f.type]}"
                )
    # Same story for the two legacy-baseline auth tables (DDL from 000_create_base_schema.sql).
    await async_dal.executesql_async(
        "CREATE TABLE IF NOT EXISTS hub_sessions (id SERIAL PRIMARY KEY, "
        "session_token TEXT NOT NULL, user_id INTEGER, platform VARCHAR(50), "
        "platform_user_id VARCHAR(255), platform_username VARCHAR(255), avatar_url TEXT, "
        "is_active BOOLEAN DEFAULT true, expires_at TIMESTAMP, revoked_at TIMESTAMP, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    await async_dal.executesql_async(
        "CREATE TABLE IF NOT EXISTS hub_oauth_states (id SERIAL PRIMARY KEY, "
        "state VARCHAR(255) UNIQUE NOT NULL, mode VARCHAR(50) NOT NULL DEFAULT 'login', "
        "platform VARCHAR(50) NOT NULL, "
        "user_id INTEGER REFERENCES hub_users(id) ON DELETE CASCADE, redirect_uri TEXT, "
        "expires_at TIMESTAMP NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    await async_dal.executesql_async(
        "TRUNCATE audit_log, hub_sessions, hub_oauth_states, community_members, "
        "ephemeral_pseudonyms, identity_resolution_events, hub_user_identities, hub_users, "
        "communities, tenants RESTART IDENTITY CASCADE"
    )
    yield async_dal
    dal.close()


@pytest.fixture
def no_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the provider gate and ban re-check (covered by their own suites)."""

    async def _ok(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(login_providers, "require_enabled", _ok)
    monkeypatch.setattr(ban_suppression, "reapply_for_hub_user_async", _ok)


async def _q(adal: AsyncDAL, sql: str, params: list[Any] | None = None) -> list[Any]:
    return list(await adal.executesql_async(sql, params) or [])


async def _hub_user(adal: AsyncDAL, *, password: bool = True) -> tuple[int, str]:
    rows = await _q(
        adal,
        "INSERT INTO hub_users (username, email, password_hash) VALUES (%s, %s, %s) "
        "RETURNING id, uuid::text",
        [USERNAME, EMAIL, "x" if password else None],
    )
    return int(rows[0][0]), str(rows[0][1])


async def _identity(adal: AsyncDAL, hub_id: int, platform: str, puid: str, name: str) -> None:
    await _q(
        adal,
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id, "
        "platform_username, is_primary, linked_at) VALUES (%s, %s, %s, %s, false, now())",
        [hub_id, platform, puid, name],
    )


async def _sessions(adal: AsyncDAL, hub_id: int, n: int) -> list[int]:
    ids = []
    for i in range(n):
        rows = await _q(
            adal,
            "INSERT INTO hub_sessions (session_token, user_id, is_active, created_at) "
            "VALUES (%s, %s, true, now()) RETURNING id",
            [f"tok-{i}", hub_id],
        )
        ids.append(int(rows[0][0]))
    return ids


async def _active_ids(adal: AsyncDAL, hub_id: int) -> set[int]:
    rows = await _q(adal, "SELECT id FROM hub_sessions WHERE user_id = %s AND is_active", [hub_id])
    return {int(r[0]) for r in rows}


async def _audit_rows(adal: AsyncDAL) -> list[tuple[str, str, str, dict[str, Any]]]:
    rows = await _q(
        adal, "SELECT action, target_type, target_id, details::text FROM audit_log ORDER BY id"
    )
    return [(r[0], r[1], r[2], json.loads(r[3])) for r in rows]


def _assert_pii_free(adal_rows: list[tuple[str, str, str, dict[str, Any]]]) -> None:
    blob = json.dumps(adal_rows)
    for pii in (EMAIL, USERNAME, HANDLE, PUID):
        assert pii not in blob


# ------------------------------------------------------------------ 1 + 2: unlink


async def test_identities_unlink_revokes_other_sessions_and_audits(adal: AsyncDAL) -> None:
    hub_id, user_uuid = await _hub_user(adal)
    await _identity(adal, hub_id, "discord", "d-1", HANDLE)
    await _identity(adal, hub_id, "twitch", PUID, HANDLE)
    keep, *others = await _sessions(adal, hub_id, 3)

    await identity_service.unlink_identity(
        adal, adal.dal, user_id=hub_id, platform="twitch", keep_session_id=keep
    )

    assert await _active_ids(adal, hub_id) == {keep}
    assert others  # 2 other sessions existed and were revoked
    rows = await _audit_rows(adal)
    assert rows == [
        (
            "identity.unlinked",
            "hub_user",
            user_uuid,
            {
                "user_uuid": user_uuid,
                "platform": "twitch",
                "actor": "self",
                "via": "user_identities",
            },
        )
    ]
    _assert_pii_free(rows)


async def test_identities_unlink_refused_revokes_and_audits_nothing(adal: AsyncDAL) -> None:
    hub_id, _ = await _hub_user(adal)
    await _identity(adal, hub_id, "discord", "d-1", HANDLE)
    await _sessions(adal, hub_id, 2)
    with pytest.raises(Exception):  # noqa: B017 - ApiError: last identity
        await identity_service.unlink_identity(adal, adal.dal, user_id=hub_id, platform="discord")
    assert len(await _active_ids(adal, hub_id)) == 2
    assert await _audit_rows(adal) == []


async def test_oauth_unlink_audits_without_pii(adal: AsyncDAL) -> None:
    hub_id, user_uuid = await _hub_user(adal)
    await _identity(adal, hub_id, "twitch", PUID, HANDLE)
    keep, _other = await _sessions(adal, hub_id, 2)
    await oauth_service.unlink_account(
        adal, adal.dal, user_id=hub_id, platform="twitch", keep_session_id=keep
    )
    assert await _active_ids(adal, hub_id) == {keep}
    rows = await _audit_rows(adal)
    assert [(r[0], r[2], r[3]["via"], r[3]["platform"]) for r in rows] == [
        ("identity.unlinked", user_uuid, "oauth", "twitch")
    ]
    _assert_pii_free(rows)


# ------------------------------------------------------------------ 2: link


async def _state(adal: AsyncDAL, hub_id: int, platform: str, nonce: str) -> str:
    state = bound_state(uuid.uuid4().hex, nonce)
    await _q(
        adal,
        "INSERT INTO hub_oauth_states (state, mode, platform, user_id, expires_at, created_at) "
        "VALUES (%s, 'link', %s, %s, now() + interval '10 minutes', now())",
        [state, platform, hub_id],
    )
    return state


async def test_identities_link_audits_without_pii(
    adal: AsyncDAL, monkeypatch: pytest.MonkeyPatch, no_side_effects: None
) -> None:
    hub_id, user_uuid = await _hub_user(adal)

    async def _exchange(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"id": PUID, "username": HANDLE, "email": EMAIL}

    monkeypatch.setattr(oauth_service, "exchange_code", _exchange)
    nonce = "n" * 32
    state = await _state(adal, hub_id, "twitch", nonce)
    await identity_service.identity_link_callback(
        adal,
        adal.dal,
        platform="twitch",
        code="c",
        state=state,
        callback_base_url="https://h",
        binding_nonce=nonce,
    )
    rows = await _audit_rows(adal)
    assert rows == [
        (
            "identity.linked",
            "hub_user",
            user_uuid,
            {
                "user_uuid": user_uuid,
                "platform": "twitch",
                "actor": "self",
                "via": "user_identities",
            },
        )
    ]
    _assert_pii_free(rows)


async def test_oauth_link_audits_without_pii(
    adal: AsyncDAL, monkeypatch: pytest.MonkeyPatch, no_side_effects: None
) -> None:
    hub_id, user_uuid = await _hub_user(adal)

    async def _creds(*_a: Any, **_k: Any) -> dict[str, str]:
        return {"client_id": "id", "client_secret": "s"}

    async def _exchange(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"id": PUID, "username": HANDLE, "email": EMAIL}

    monkeypatch.setattr(oauth_service, "_get_platform_credentials", _creds)
    monkeypatch.setattr(oauth_service, "_exchange_code", _exchange)
    nonce = "m" * 32
    state = await _state(adal, hub_id, "discord", nonce)
    await oauth_service.link_callback(
        adal,
        adal.dal,
        platform="discord",
        code="c",
        state=state,
        callback_base_url="https://h",
        binding_nonce=nonce,
    )
    rows = await _audit_rows(adal)
    assert [(r[0], r[2], r[3]["via"], r[3]["platform"]) for r in rows] == [
        ("identity.linked", user_uuid, "oauth", "discord")
    ]
    _assert_pii_free(rows)


# ------------------------------------------------------------------ 3: Twitch rename


async def _tenant_community(adal: AsyncDAL) -> int:
    t = int((await _q(adal, "INSERT INTO tenants (slug) VALUES ('acme') RETURNING id"))[0][0])
    return int(
        (
            await _q(
                adal,
                "INSERT INTO communities (name, tenant_id) VALUES ('c', %s) RETURNING id",
                [t],
            )
        )[0][0]
    )


async def _twitch_member(adal: AsyncDAL, community: int, puid: str) -> None:
    await _q(
        adal,
        "INSERT INTO community_members (community_id, platform, platform_user_id) "
        "VALUES (%s, 'twitch', %s)",
        [community, puid],
    )


async def _ingest(adal: AsyncDAL, puid: str, handle: str) -> None:
    """What svc-process sends for a chat message: numeric Twitch id + the login it carried."""
    await resolve_identities(adal, [IdentityRequest("acme", "twitch", puid, handle)])


async def test_renamed_twitch_handle_routes_to_new_claimant_not_old_owner(adal: AsyncDAL) -> None:
    c = await _tenant_community(adal)
    bob_id, bob_uuid = await _hub_user(adal)
    await _identity(adal, bob_id, "twitch", "111", "oldname")
    await _twitch_member(adal, c, "111")
    await _twitch_member(adal, c, "222")

    await _ingest(adal, "111", "oldname")
    assert (await resolve_target(adal, "acme", "twitch", "@oldname")).uuid == uuid.UUID(bob_uuid)

    # Bob renames; the OAuth-time platform_username still says "oldname".
    await _ingest(adal, "111", "newname")
    assert (await resolve_target(adal, "acme", "twitch", "@newname")).uuid == uuid.UUID(bob_uuid)
    with pytest.raises(HandleNotFoundError):
        await resolve_target(adal, "acme", "twitch", "@oldname")

    # Someone else claims "oldname": it is theirs, never Bob's.
    await _ingest(adal, "222", "OldName")
    got = await resolve_target(adal, "acme", "twitch", "@oldname")
    assert got.uuid != uuid.UUID(bob_uuid)


async def test_new_claimant_strips_name_from_previous_holder(adal: AsyncDAL) -> None:
    """Claimant posts before the old owner is seen again: the stale holder loses the name."""
    c = await _tenant_community(adal)
    bob_id, bob_uuid = await _hub_user(adal)
    await _identity(adal, bob_id, "twitch", "111", "oldname")
    await _twitch_member(adal, c, "111")
    await _twitch_member(adal, c, "222")
    await _ingest(adal, "111", "oldname")
    await _ingest(adal, "222", "oldname")
    got = await resolve_target(adal, "acme", "twitch", "@oldname")
    assert got.uuid != uuid.UUID(bob_uuid)
    held = await _q(adal, "SELECT handle FROM ephemeral_pseudonyms WHERE platform_user_id = '111'")
    assert held == [[None]] or held == [(None,)]


async def test_linked_oauth_username_alone_never_resolves_on_twitch(adal: AsyncDAL) -> None:
    c = await _tenant_community(adal)
    bob_id, _ = await _hub_user(adal)
    await _identity(adal, bob_id, "twitch", "111", "oldname")
    await _twitch_member(adal, c, "111")
    with pytest.raises(HandleNotFoundError):
        await resolve_target(adal, "acme", "twitch", "@oldname")


async def test_stale_twitch_handle_does_not_resolve(adal: AsyncDAL) -> None:
    c = await _tenant_community(adal)
    await _twitch_member(adal, c, "111")
    await _ingest(adal, "111", "quietname")
    assert await resolve_target(adal, "acme", "twitch", "@quietname")
    await _q(
        adal,
        "UPDATE ephemeral_pseudonyms SET handle_seen_at = now() - interval '31 days' "
        "WHERE platform_user_id = '111'",
    )
    with pytest.raises(HandleNotFoundError):
        await resolve_target(adal, "acme", "twitch", "@quietname")


async def test_handle_fallback_keys_never_record_a_login(adal: AsyncDAL) -> None:
    """svc-process `handle:<name>` keys (no numeric id) never claim or strip a login."""
    c = await _tenant_community(adal)
    await _twitch_member(adal, c, "111")
    await _ingest(adal, "111", "realname")
    await _ingest(adal, "handle:realname", "realname")
    rows = await _q(adal, "SELECT handle FROM ephemeral_pseudonyms WHERE platform_user_id = '111'")
    assert [r[0] for r in rows] == ["realname"]
