"""Temp-password login honors the row's binding to ONE hub user (PR #1542 review, alembic 0147).

`admin_service.generate_temp_password` binds the row to an active member of that community; the
login (`auth_service.temp_password_login`) must accept only a bound row whose user is still
active and an active, unbanned member there -- and name THAT user, never the typed identifier.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import pytest

from services import auth_service
from services.errors import ApiError
from services.membership_state import MembershipState
from tests.test_v1_join_request_blueprint import _seed_community, _seed_user, _test_config

#: Generated per run: no credential-shaped literal in source.
PASSWORD = "-".join(str(uuid.uuid4()).split("-")[:2])


def _temp_row(auth_db: Any, *, community_id: int, hub_user_id: int | None, ident: str) -> None:
    dal = auth_db.dal
    dal.hub_temp_passwords.insert(
        community_id=community_id,
        hub_user_id=hub_user_id,
        user_identifier=ident,
        password_hash=bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        created_at=datetime.now(UTC),
    )
    dal.commit()


def _member(auth_db: Any, *, community_id: int, user_id: int, **fields: Any) -> None:
    row = {"role": "member", "is_active": True, "joined_at": datetime.now(UTC), **fields}
    auth_db.dal.community_members.insert(community_id=community_id, user_id=str(user_id), **row)
    auth_db.dal.commit()


async def _login(auth_db: Any, ident: str) -> Any:
    return await auth_service.temp_password_login(
        auth_db, auth_db.dal, _test_config(), identifier=ident, password=PASSWORD
    )


class TestTempPasswordBinding:
    async def test_unbound_legacy_row_is_refused(self, auth_db: Any) -> None:
        cid = _seed_community(auth_db)
        uid = _seed_user(auth_db, email="l@example.com", username="legacy")
        _member(auth_db, community_id=cid, user_id=uid)
        _temp_row(auth_db, community_id=cid, hub_user_id=None, ident="legacy")
        with pytest.raises(ApiError) as err:
            await _login(auth_db, "legacy")
        assert err.value.status_code == 401

    @pytest.mark.parametrize("state", ["removed", "banned", "deactivated_user"])
    async def test_bound_user_no_longer_eligible_is_refused(self, auth_db: Any, state: str) -> None:
        cid = _seed_community(auth_db)
        uid = _seed_user(auth_db, email=f"{state}@example.com", username=state)
        if state == "removed":
            _member(auth_db, community_id=cid, user_id=uid, is_active=False)
        elif state == "banned":
            _member(
                auth_db,
                community_id=cid,
                user_id=uid,
                membership_state=int(MembershipState.BANNED),
            )
        else:
            _member(auth_db, community_id=cid, user_id=uid)
            auth_db.dal(auth_db.dal.hub_users.id == uid).update(is_active=False)
            auth_db.dal.commit()
        _temp_row(auth_db, community_id=cid, hub_user_id=uid, ident=state)
        with pytest.raises(ApiError) as err:
            await _login(auth_db, state)
        assert err.value.status_code == 401
        row = auth_db.dal(auth_db.dal.hub_temp_passwords.hub_user_id == uid).select().first()
        assert not row.is_used, "a refused login never burns the one-time password"

    async def test_bound_active_member_resolves_to_that_user(self, auth_db: Any) -> None:
        cid = _seed_community(auth_db)
        uid = _seed_user(auth_db, email="ok@example.com", username="okuser")
        _member(auth_db, community_id=cid, user_id=uid)
        _temp_row(auth_db, community_id=cid, hub_user_id=uid, ident="ok@example.com")
        row = auth_db.dal(auth_db.dal.hub_temp_passwords.hub_user_id == uid).select().first()
        # The session names the BOUND user's username, never the typed identifier.
        assert (
            await auth_service._temp_password_bound_username(  # noqa: SLF001
                auth_db, auth_db.dal, row
            )
            == "okuser"
        )
