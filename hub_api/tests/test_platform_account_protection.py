"""The bootstrap admin (`hub_users.is_bootstrap`) is a non-person PLATFORM account.

It can never be deleted, deactivated, demoted from `system` admin or erased -- via API or service
layer -- and every refusal is audited. Person admins are unaffected by the protection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.user_management import user_management_bp
from services import audit_http, data_privacy_service, platform_service, session_service
from services.audit_events import AuditAction, AuditOutcome
from services.errors import ApiError
from services.global_identity import grant_global_admin_sync, is_global_admin_sync
from services.platform_account import PLATFORM_ACCOUNT_CODE
from tests.conftest import TENANT_SLUG, make_user_token, seed_super_admin

BOOTSTRAP_ID = 1


class _FakeAudit:
    """Records the audit events the service layer emits."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def record(self, event: Any) -> None:
        """Remember the event."""
        self.events.append(event)


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> _FakeAudit:
    """Wire a recording audit service."""
    fake = _FakeAudit()
    monkeypatch.setattr(audit_http, "get_service", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _no_sessions(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Session revocation is not under test here."""
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(session_service, "revoke_user_sessions", mock)
    return mock


@pytest.fixture
def app(auth_db: Any) -> Quart:
    """App with only the superadmin user-management blueprint."""
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(user_management_bp)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    """Test client."""
    return app.test_client()


def _headers(user_id: int) -> dict[str, str]:
    token = make_user_token(
        user_id=user_id,
        scope="*:read *:write *:admin *:delete settings:write users:admin",
        tenant=TENANT_SLUG,
    )
    return {"Authorization": f"Bearer {token}"}


def _world(auth_db: Any) -> int:
    """Bootstrap platform admin (id 1) plus a person global admin; return the person's id."""
    seed_super_admin(auth_db, user_id=BOOTSTRAP_ID, bootstrap=True)
    person = auth_db.dal.hub_users.insert(
        username="person",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    grant_global_admin_sync(auth_db.dal, int(person))
    auth_db.dal.commit()
    return int(person)


def _assert_denied(audit: _FakeAudit, operation: str) -> None:
    (event,) = audit.events
    assert event.outcome == AuditOutcome.DENIED
    assert event.details == {"operation": operation, "reason": "platform_account"}
    assert event.target_id  # the platform account's uuid, never a name/e-mail


async def test_delete_is_refused_and_audited(client: Any, auth_db: Any, audit: _FakeAudit) -> None:
    person = _world(auth_db)
    response = await client.delete(
        f"/api/v1/superadmin/users/{BOOTSTRAP_ID}", headers=_headers(person)
    )
    assert response.status_code == 403
    assert (await response.get_json())["error"]["code"] == PLATFORM_ACCOUNT_CODE
    assert is_global_admin_sync(auth_db.dal, BOOTSTRAP_ID) is True
    _assert_denied(audit, "delete")
    assert audit.events[0].action == AuditAction.USER_DELETED


async def test_deactivate_is_refused_and_audited(
    client: Any, auth_db: Any, audit: _FakeAudit
) -> None:
    person = _world(auth_db)
    response = await client.put(
        f"/api/v1/superadmin/users/{BOOTSTRAP_ID}",
        headers=_headers(person),
        json={"isActive": False},
    )
    assert response.status_code == 403
    assert (await response.get_json())["error"]["code"] == PLATFORM_ACCOUNT_CODE
    assert is_global_admin_sync(auth_db.dal, BOOTSTRAP_ID) is True
    _assert_denied(audit, "deactivate")


async def test_demote_is_refused_even_with_other_admins(
    client: Any, auth_db: Any, audit: _FakeAudit
) -> None:
    person = _world(auth_db)
    response = await client.post(
        f"/api/v1/superadmin/users/{BOOTSTRAP_ID}/super-admin-role",
        headers=_headers(person),
        json={"grant": False},
    )
    assert response.status_code == 403
    assert (await response.get_json())["error"]["code"] == PLATFORM_ACCOUNT_CODE
    assert is_global_admin_sync(auth_db.dal, BOOTSTRAP_ID) is True
    _assert_denied(audit, "demote")
    assert audit.events[0].action == AuditAction.ROLE_SUPER_ADMIN_CHANGED


async def test_platform_deactivate_route_service_is_refused(
    auth_db: Any, audit: _FakeAudit
) -> None:
    person = _world(auth_db)
    with pytest.raises(ApiError) as info:
        await platform_service.deactivate_user(
            auth_db, auth_db.dal, user_id=BOOTSTRAP_ID, reason=None, actor_id=person
        )
    assert info.value.code == PLATFORM_ACCOUNT_CODE
    _assert_denied(audit, "deactivate")


async def test_self_service_erasure_is_refused_and_audited(auth_db: Any, audit: _FakeAudit) -> None:
    _world(auth_db)
    with pytest.raises(ApiError) as info:
        await data_privacy_service.request_data_deletion(
            auth_db, auth_db.dal, user_id=BOOTSTRAP_ID, password=None
        )
    assert info.value.status_code == 403
    assert info.value.code == PLATFORM_ACCOUNT_CODE
    _assert_denied(audit, "erase")
    assert audit.events[0].action == AuditAction.PRIVACY_ERASURE_REQUESTED
    row = auth_db.dal(auth_db.dal.hub_users.id == BOOTSTRAP_ID).select().first()
    assert not str(row.email or "").startswith("deleted_")


async def test_erasure_core_refuses_the_platform_account(auth_db: Any, audit: _FakeAudit) -> None:
    """The shared erasure core (also behind the tenant DSAR console) is a backstop."""
    _world(auth_db)
    with pytest.raises(ApiError) as info:
        await data_privacy_service.anonymize_user_data(
            auth_db, auth_db.dal, user_id=BOOTSTRAP_ID, email=None, actor_user_id=2
        )
    assert info.value.code == PLATFORM_ACCOUNT_CODE
    _assert_denied(audit, "erase")
    assert audit.events[0].actor_user_id == 2


async def test_person_admins_are_unaffected(client: Any, auth_db: Any, audit: _FakeAudit) -> None:
    """Demote, deactivate and delete still work for person accounts (bootstrap is the caller)."""
    person = _world(auth_db)
    demoted = await client.post(
        f"/api/v1/superadmin/users/{person}/super-admin-role",
        headers=_headers(BOOTSTRAP_ID),
        json={"grant": False},
    )
    assert demoted.status_code == 200
    assert is_global_admin_sync(auth_db.dal, person) is False
    deactivated = await client.put(
        f"/api/v1/superadmin/users/{person}",
        headers=_headers(BOOTSTRAP_ID),
        json={"isActive": False},
    )
    assert deactivated.status_code == 200
    deleted = await client.delete(
        f"/api/v1/superadmin/users/{person}", headers=_headers(BOOTSTRAP_ID)
    )
    assert deleted.status_code == 200
    assert all(e.details.get("reason") != "platform_account" for e in audit.events)


async def test_user_dto_flags_only_the_platform_account(client: Any, auth_db: Any) -> None:
    """The API exposes `isPlatformAccount` so the webui can badge it (person admin: false)."""
    person = _world(auth_db)
    response = await client.get("/api/v1/superadmin/users", headers=_headers(person))
    flags = {u["id"]: u["isPlatformAccount"] for u in (await response.get_json())["users"]}
    assert flags == {BOOTSTRAP_ID: True, person: False}


async def test_last_admin_guard_still_protects_person_accounts(client: Any, auth_db: Any) -> None:
    """A person who is the last admin (no bootstrap flag) is still 409-protected."""
    seed_super_admin(auth_db, user_id=1)  # not bootstrap
    response = await client.put(
        "/api/v1/superadmin/users/1", headers=_headers(1), json={"isActive": False}
    )
    assert response.status_code == 409
