"""Security-review fixes for the system-tenant global-admin model (PR #1234 follow-up).

Findings covered (each with a regression that fails against the pre-fix code):

1/3. The `system` tenant's admins are managed ONLY via the superadmin route: tenant routes 403
     (see `test_v1_tenant_blueprint.TestSystemTenantAdminsAreSuperadminOnly`); the superadmin
     path itself re-checks the caller in the DB, refuses the last admin (409), writes the
     `tenant_members` row on grant, revokes sessions on revoke and audits both directions.
2.   Deactivating / deleting the last global admin is refused (409); acting on a global admin
     needs a DB-verified global-admin caller (403); deactivated users lose their sessions.
4.   (migration) see `alembic/tests/test_0096_system_tenant_global_admin.py`.
5.   An inactive `system` tenant makes nobody global.
6.   The last-admin count + mutation run under one `pg_advisory_xact_lock` transaction; the
     concurrent-revoke race is exercised against REAL Postgres (skipped without docker).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.user_management import user_management_bp
from services import audit_http, global_identity, session_service, user_management_service
from services.audit_events import AuditAction, AuditOutcome
from services.errors import ApiError
from services.global_identity import (
    GLOBAL_ADMIN_LOCK_KEY,
    global_admin_ids,
    grant_global_admin_sync,
    is_global_admin_sync,
    is_global_user_sync,
)
from tests.conftest import TENANT_SLUG, make_user_token, seed_super_admin

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)


@pytest.fixture
def app(auth_db: Any) -> Quart:
    """Standalone app mounting only the superadmin user-management blueprint."""
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(user_management_bp)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    """Test client for `app`."""
    return app.test_client()


@pytest.fixture
def revoked(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace session revocation with a recorder (the reason string is what we assert)."""
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(session_service, "revoke_user_sessions", mock)
    return mock


class _FakeAudit:
    """Collects `AuditEvent`s the way `AuditService.record` would receive them."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def record(self, event: Any) -> None:
        """Remember the event."""
        self.events.append(event)


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> _FakeAudit:
    """Wire a recording audit service into `audit_http.get_service`."""
    fake = _FakeAudit()
    monkeypatch.setattr(audit_http, "get_service", lambda: fake)
    return fake


def _seed_user(auth_db: Any, email: str) -> int:
    """Insert a plain active `hub_users` row; return its id."""
    user_id = auth_db.dal.hub_users.insert(
        email=email,
        username=email,
        is_active=True,
        email_verified=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    auth_db.dal.commit()
    return int(user_id)


def _headers(user_id: int) -> dict[str, str]:
    """Token carrying the platform admin scope bundle for `user_id`."""
    token = make_user_token(
        user_id=user_id,
        scope="*:read *:write *:admin *:delete settings:write users:admin",
        tenant=TENANT_SLUG,
    )
    return {"Authorization": f"Bearer {token}"}


def _uuid_of(auth_db: Any, user_id: int) -> str:
    """The `hub_users.uuid` of `user_id`."""
    dal = auth_db.dal
    return str(dal(dal.hub_users.id == user_id).select(dal.hub_users.uuid).first().uuid)


class TestRevokeLastAdmin:
    """Finding 1: the superadmin path is the only door and it keeps >= 1 admin."""

    async def test_revoke_last_admin_is_409(
        self, client: Any, auth_db: Any, revoked: AsyncMock
    ) -> None:
        seed_super_admin(auth_db, user_id=1)
        response = await client.post(
            "/api/v1/superadmin/users/1/super-admin-role",
            headers=_headers(1),
            json={"grant": False},
        )
        assert response.status_code == 409
        assert is_global_admin_sync(auth_db.dal, 1) is True
        revoked.assert_not_awaited()

    async def test_revoke_revokes_sessions_with_reason(
        self, client: Any, auth_db: Any, revoked: AsyncMock
    ) -> None:
        seed_super_admin(auth_db, user_id=1)
        second = _seed_user(auth_db, "second@example.com")
        grant_global_admin_sync(auth_db.dal, second)
        auth_db.dal.commit()
        response = await client.post(
            f"/api/v1/superadmin/users/{second}/super-admin-role",
            headers=_headers(1),
            json={"grant": False},
        )
        assert response.status_code == 200
        assert is_global_admin_sync(auth_db.dal, second) is False
        assert revoked.await_args.kwargs["user_id"] == second
        assert revoked.await_args.kwargs["reason"] == "super_admin_revoked"

    async def test_caller_is_rechecked_in_db_not_from_the_jwt_scope(
        self, client: Any, auth_db: Any, revoked: AsyncMock
    ) -> None:
        """A token with the admin scope but no `system` admin row is refused (403)."""
        seed_super_admin(auth_db, user_id=1)
        impostor = _seed_user(auth_db, "impostor@example.com")  # not a global admin
        response = await client.post(
            "/api/v1/superadmin/users/1/super-admin-role",
            headers=_headers(impostor),
            json={"grant": False},
        )
        assert response.status_code == 403
        assert is_global_admin_sync(auth_db.dal, 1) is True

    async def test_grant_writes_tenant_member_and_admin_rows(
        self, client: Any, auth_db: Any, revoked: AsyncMock
    ) -> None:
        seed_super_admin(auth_db, user_id=1, bootstrap=True)
        target = _seed_user(auth_db, "t@example.com")
        response = await client.post(
            f"/api/v1/superadmin/users/{target}/super-admin-role",
            headers=_headers(1),
            json={"grant": True},
        )
        assert response.status_code == 200
        assert is_global_admin_sync(auth_db.dal, target) is True
        assert is_global_user_sync(auth_db.dal, target) is True
        dal = auth_db.dal
        sys_id = dal(dal.tenants.slug == "system").select(dal.tenants.id).first().id
        assert (
            dal(
                (dal.tenant_members.tenant_id == sys_id) & (dal.tenant_members.user_id == target)
            ).count()
            == 1
        )


class TestDeactivateDeleteLastAdmin:
    """Finding 2: lockout via deactivation / deletion."""

    async def test_self_deactivating_the_last_admin_is_409(
        self, client: Any, auth_db: Any, revoked: AsyncMock
    ) -> None:
        seed_super_admin(auth_db, user_id=1)
        response = await client.put(
            "/api/v1/superadmin/users/1", headers=_headers(1), json={"isActive": False}
        )
        assert response.status_code == 409
        assert is_global_admin_sync(auth_db.dal, 1) is True
        revoked.assert_not_awaited()

    async def test_deleting_the_last_admin_is_409_for_any_caller(
        self, client: Any, auth_db: Any, revoked: AsyncMock
    ) -> None:
        """Even an admin-scoped token held by a non-admin cannot delete the last admin."""
        seed_super_admin(auth_db, user_id=1)
        other = _seed_user(auth_db, "o@example.com")
        response = await client.delete("/api/v1/superadmin/users/1", headers=_headers(other))
        assert response.status_code == 409
        assert is_global_admin_sync(auth_db.dal, 1) is True
        revoked.assert_not_awaited()

    async def test_deactivating_the_last_admin_is_409_for_any_caller(
        self, client: Any, auth_db: Any, revoked: AsyncMock
    ) -> None:
        seed_super_admin(auth_db, user_id=1)
        other = _seed_user(auth_db, "o@example.com")
        response = await client.put(
            "/api/v1/superadmin/users/1", headers=_headers(other), json={"isActive": False}
        )
        assert response.status_code == 409
        assert is_global_admin_sync(auth_db.dal, 1) is True

    async def test_deleting_one_of_two_admins_is_allowed_then_the_last_is_protected(
        self, client: Any, auth_db: Any, revoked: AsyncMock
    ) -> None:
        seed_super_admin(auth_db, user_id=1)
        second = _seed_user(auth_db, "second@example.com")
        grant_global_admin_sync(auth_db.dal, second)
        auth_db.dal.commit()
        ok = await client.delete(f"/api/v1/superadmin/users/{second}", headers=_headers(1))
        assert ok.status_code == 200
        assert global_admin_ids(auth_db.dal) == {1}
        assert revoked.await_args.kwargs["reason"] == "user_deleted"
        again = await client.put(
            "/api/v1/superadmin/users/1", headers=_headers(1), json={"isActive": False}
        )
        assert again.status_code == 409

    async def test_non_admin_caller_cannot_act_on_a_global_admin(
        self, auth_db: Any, revoked: AsyncMock
    ) -> None:
        seed_super_admin(auth_db, user_id=1)
        second = _seed_user(auth_db, "second@example.com")
        grant_global_admin_sync(auth_db.dal, second)
        plain = _seed_user(auth_db, "plain@example.com")
        auth_db.dal.commit()
        with pytest.raises(ApiError) as info:
            await user_management_service.update_user(
                auth_db,
                auth_db.dal,
                user_id=second,
                email="hijack@example.com",
                is_active=None,
                caller_id=plain,
            )
        assert info.value.status_code == 403
        with pytest.raises(ApiError) as info2:
            await user_management_service.delete_user(
                auth_db, auth_db.dal, user_id=second, caller_id=plain
            )
        assert info2.value.status_code == 403
        assert is_global_admin_sync(auth_db.dal, second) is True

    async def test_deactivating_a_user_revokes_their_sessions(
        self, client: Any, auth_db: Any, revoked: AsyncMock
    ) -> None:
        seed_super_admin(auth_db, user_id=1)
        target = _seed_user(auth_db, "victim@example.com")
        response = await client.put(
            f"/api/v1/superadmin/users/{target}", headers=_headers(1), json={"isActive": False}
        )
        assert response.status_code == 200
        assert revoked.await_args.kwargs == {"user_id": target, "reason": "user_deactivated"}

    async def test_deactivating_a_non_last_admin_revokes_sessions_and_audits(
        self, auth_db: Any, revoked: AsyncMock, audit: _FakeAudit
    ) -> None:
        seed_super_admin(auth_db, user_id=1)
        second = _seed_user(auth_db, "second@example.com")
        grant_global_admin_sync(auth_db.dal, second)
        auth_db.dal.commit()
        await user_management_service.update_user(
            auth_db, auth_db.dal, user_id=second, email=None, is_active=False, caller_id=1
        )
        assert is_global_admin_sync(auth_db.dal, second) is False  # inactive = not global
        assert revoked.await_args.kwargs["reason"] == "user_deactivated"
        (event,) = audit.events
        assert event.action == AuditAction.USER_UPDATED
        assert event.outcome == AuditOutcome.SUCCESS
        assert event.details["operation"] == "deactivate"
        assert event.target_id == _uuid_of(auth_db, second)
        assert event.actor_user_id == 1


class TestReactivationRestoresPrivilege:
    """A deactivated global admin keeps its `system` row: reactivation is a guarded action."""

    @staticmethod
    async def _deactivated_admin(client: Any, auth_db: Any) -> int:
        seed_super_admin(auth_db, user_id=1)
        second = _seed_user(auth_db, "second@example.com")
        grant_global_admin_sync(auth_db.dal, second)
        auth_db.dal.commit()
        off = await client.put(
            f"/api/v1/superadmin/users/{second}", headers=_headers(1), json={"isActive": False}
        )
        assert off.status_code == 200
        assert is_global_admin_sync(auth_db.dal, second) is False
        return second

    async def test_non_admin_with_users_admin_scope_cannot_reactivate(
        self, client: Any, auth_db: Any, revoked: AsyncMock
    ) -> None:
        second = await self._deactivated_admin(client, auth_db)
        plain = _seed_user(auth_db, "plain@example.com")
        response = await client.put(
            f"/api/v1/superadmin/users/{second}",
            headers=_headers(plain),
            json={"isActive": True},
        )
        assert response.status_code == 403
        assert is_global_admin_sync(auth_db.dal, second) is False

    async def test_global_admin_can_reactivate_and_it_is_audited(
        self, client: Any, auth_db: Any, revoked: AsyncMock, audit: _FakeAudit
    ) -> None:
        second = await self._deactivated_admin(client, auth_db)
        audit.events.clear()
        response = await client.put(
            f"/api/v1/superadmin/users/{second}", headers=_headers(1), json={"isActive": True}
        )
        assert response.status_code == 200
        assert is_global_admin_sync(auth_db.dal, second) is True
        (event,) = audit.events
        assert event.details["operation"] == "reactivate"
        assert event.outcome == AuditOutcome.SUCCESS
        assert event.target_id == _uuid_of(auth_db, second)
        assert event.actor_user_id == 1

    async def test_denied_reactivation_is_audited(
        self, client: Any, auth_db: Any, revoked: AsyncMock, audit: _FakeAudit
    ) -> None:
        second = await self._deactivated_admin(client, auth_db)
        plain = _seed_user(auth_db, "plain@example.com")
        audit.events.clear()
        await client.put(
            f"/api/v1/superadmin/users/{second}",
            headers=_headers(plain),
            json={"isActive": True},
        )
        (event,) = audit.events
        assert event.outcome == AuditOutcome.DENIED
        assert event.details == {"operation": "reactivate", "reason": "caller_not_admin"}


class TestAdminAuditRecords:
    """Audit content: actor, target UUID, action, result -- no PII."""

    async def test_grant_and_revoke_are_audited(
        self, auth_db: Any, revoked: AsyncMock, audit: _FakeAudit
    ) -> None:
        seed_super_admin(auth_db, user_id=1, bootstrap=True)
        target = _seed_user(auth_db, "t@example.com")
        kwargs = {"caller_id": 1, "tenant_slug": TENANT_SLUG}
        await user_management_service.assign_super_admin_role(
            auth_db, auth_db.dal, user_id=target, grant=True, **kwargs
        )
        await user_management_service.assign_super_admin_role(
            auth_db, auth_db.dal, user_id=target, grant=False, **kwargs
        )
        assert [e.details["operation"] for e in audit.events] == ["grant", "revoke"]
        for event in audit.events:
            assert event.action == AuditAction.ROLE_SUPER_ADMIN_CHANGED
            assert event.outcome == AuditOutcome.SUCCESS
            assert event.actor_user_id == 1
            assert event.target_type == "user"
            assert event.target_id == _uuid_of(auth_db, target)
            assert "@" not in repr(event.details)  # no e-mail / PII in the record

    async def test_refused_last_admin_revoke_is_audited_as_denied(
        self, auth_db: Any, revoked: AsyncMock, audit: _FakeAudit
    ) -> None:
        seed_super_admin(auth_db, user_id=1)
        with pytest.raises(ApiError):
            await user_management_service.assign_super_admin_role(
                auth_db, auth_db.dal, user_id=1, grant=False, caller_id=1, tenant_slug=TENANT_SLUG
            )
        (event,) = audit.events
        assert event.outcome == AuditOutcome.DENIED
        assert event.details == {"operation": "revoke", "reason": "last_global_admin"}
        assert event.target_id == _uuid_of(auth_db, 1)

    async def test_delete_of_a_global_admin_is_audited(
        self, auth_db: Any, revoked: AsyncMock, audit: _FakeAudit
    ) -> None:
        seed_super_admin(auth_db, user_id=1)
        second = _seed_user(auth_db, "second@example.com")
        grant_global_admin_sync(auth_db.dal, second)
        auth_db.dal.commit()
        await user_management_service.delete_user(auth_db, auth_db.dal, user_id=second, caller_id=1)
        (event,) = audit.events
        assert event.action == AuditAction.USER_DELETED
        assert event.details["operation"] == "delete"
        assert event.outcome == AuditOutcome.SUCCESS
        assert event.target_id == _uuid_of(auth_db, second)
        assert revoked.await_args.kwargs["reason"] == "user_deleted"


class TestInactiveSystemTenant:
    """Finding 5: an inactive `system` tenant fails closed."""

    def test_inactive_system_tenant_nobody_is_global(self, auth_db: Any) -> None:
        dal = auth_db.dal
        seed_super_admin(auth_db, user_id=1)
        assert is_global_admin_sync(dal, 1) is True
        dal(dal.tenants.slug == "system").update(is_active=False)
        dal.commit()
        assert is_global_admin_sync(dal, 1) is False
        assert is_global_user_sync(dal, 1) is False
        assert global_admin_ids(dal) == set()

    def test_null_is_active_also_fails_closed(self, auth_db: Any) -> None:
        dal = auth_db.dal
        seed_super_admin(auth_db, user_id=1)
        dal(dal.tenants.slug == "system").update(is_active=None)
        dal.commit()
        assert is_global_admin_sync(dal, 1) is False

    def test_grant_refuses_to_reuse_an_inactive_system_tenant(self, auth_db: Any) -> None:
        dal = auth_db.dal
        seed_super_admin(auth_db, user_id=1)
        dal(dal.tenants.slug == "system").update(is_active=False)
        dal.commit()
        other = _seed_user(auth_db, "o@example.com")
        with pytest.raises(RuntimeError, match="inactive"):
            grant_global_admin_sync(dal, other)
        assert dal(dal.tenants.slug == "system").count() == 1  # no duplicate row created


class TestLockKeyIsStable:
    """Finding 6: one stable key serialises every revoke/deactivate/delete."""

    def test_lock_statement_uses_the_global_admin_key(self) -> None:
        calls: list[tuple[str, list[str]]] = []

        class _PgDal:
            _dbname = "postgres"

            def executesql(self, sql: str, placeholders: list[str]) -> None:
                calls.append((sql, placeholders))

        global_identity.take_global_admin_lock(_PgDal())
        assert calls == [("SELECT pg_advisory_xact_lock(hashtext(%s))", [GLOBAL_ADMIN_LOCK_KEY])]
        assert GLOBAL_ADMIN_LOCK_KEY == "global_admin"

    def test_lock_is_a_noop_off_postgres(self, auth_db: Any) -> None:
        global_identity.take_global_admin_lock(auth_db.dal)  # sqlite: must not raise


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real, migrated Postgres container for the concurrency test."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("admin-revoke-race") as db:
        yield db


def _pg_seed_two_admins(db: PgTestDatabase) -> tuple[int, int]:
    """Insert two active users who are both `system` tenant admins; return their ids."""
    import psycopg2

    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tenant_admins")
            cur.execute("DELETE FROM tenant_members")
            cur.execute("DELETE FROM hub_users WHERE username IN ('race-a', 'race-b')")
            cur.execute(
                "INSERT INTO tenants (slug, is_active) VALUES ('system', TRUE) "
                "ON CONFLICT (slug) DO UPDATE SET is_active = TRUE RETURNING id"
            )
            sys_id = cur.fetchone()[0]
            ids = []
            for name in ("race-a", "race-b"):
                cur.execute(
                    "INSERT INTO hub_users (username, is_active) VALUES (%s, TRUE) RETURNING id",
                    (name,),
                )
                uid = cur.fetchone()[0]
                ids.append(uid)
                cur.execute(
                    "INSERT INTO tenant_admins (tenant_id, user_id, role) "
                    "VALUES (%s, %s, 'tenant-owner')",
                    (sys_id, uid),
                )
            return ids[0], ids[1]
    finally:
        conn.close()


def _pg_admin_count(db: PgTestDatabase) -> int:
    """Committed number of active `system` admins, read on a fresh connection."""
    import psycopg2

    conn = psycopg2.connect(db.dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM tenant_admins ta JOIN tenants t ON t.id = ta.tenant_id "
                "JOIN hub_users u ON u.id = ta.user_id WHERE t.slug = 'system' AND u.is_active"
            )
            return int(cur.fetchone()[0])
    finally:
        conn.close()


class _PgSide:
    """One independent 'request': its own single-thread executor and its own pydal connection."""

    def __init__(self, db: PgTestDatabase) -> None:
        from pydal import DAL

        self.executor = ThreadPoolExecutor(max_workers=1)
        dsn = db.dsn.replace("postgresql://", "postgres://", 1)
        # pydal binds a connection to the creating thread: build it on the executor thread.
        self.dal = self.executor.submit(lambda: DAL(dsn, migrate=False, pool_size=0)).result()
        self.executor.submit(global_identity.ensure_tables, self.dal).result()


@pytest.mark.parametrize("attempt", range(3))
async def test_concurrent_revoke_race_keeps_at_least_one_admin(
    pg_db: PgTestDatabase, attempt: int
) -> None:
    """Two admins demote each other concurrently: the advisory lock leaves >= 1 admin.

    Each work item sleeps after its guard passed and BEFORE the commit, so without the
    `pg_advisory_xact_lock` both would read "2 admins", both delete, and commit zero admins.
    """
    import asyncio

    a, b = _pg_seed_two_admins(pg_db)
    side_a, side_b = _PgSide(pg_db), _PgSide(pg_db)

    def _slowed(work: Any) -> Any:
        def _w(d: Any) -> Any:
            result = work(d)
            time.sleep(0.6)  # widen the window between guard and commit
            return result

        return _w

    async def _revoke(side: _PgSide, caller: int, victim: int) -> str:
        work = _slowed(
            user_management_service.revoke_global_admin_guarded(caller_id=caller, user_id=victim)
        )
        try:
            await user_management_service._run_under_admin_lock(side, side.dal, work)
        except ApiError as exc:
            return f"refused:{exc.status_code}"
        return "revoked"

    results = await asyncio.gather(_revoke(side_a, b, a), _revoke(side_b, a, b))
    assert sorted(results)[0] in ("refused:403", "refused:409"), results
    assert "revoked" in results
    assert _pg_admin_count(pg_db) >= 1
    assert _pg_admin_count(pg_db) == 1
