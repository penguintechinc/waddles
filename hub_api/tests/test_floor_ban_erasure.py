"""Art. 17 erasure of the automated hard-ban outbox (`bundle_reputation_floor_bans`).

The SQL function `reputation_floor_ban_erase_subject` needs Postgres, so its semantics are proven
against a real database in `alembic/tests/test_0114_reputation_floor_ban.py` (including that
hub-api cannot DELETE the table). Here the production entry point `anonymize_user_data()` runs on
the sqlite harness with the function call replaced by a faithful emulation of its documented rules
(refused deleted, done pseudonymised from the suppression code + row id, pending untouched), so the
Python side is proven end to end: which rows count as active restrictions, that the suppression
code is written BEFORE anything is erased, that the erased user cannot re-join, that a pending ban
survives and that a confirmed ban without a suppression code fails the whole erasure.
"""

# ruff: noqa: F811, S608

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from services import ban_suppression, floor_ban_erasure
from services import data_privacy_service as privacy
from services import suppression_keys as keys
from services.ban_suppression import FLOOR_BAN_ACTIVE_STATUSES, RestrictionReason
from tests.erasure_support import FakeExternal, create_missing_registry_tables
from tests.test_ban_suppression import _community, _tenant
from tests.test_data_privacy_erasure import (  # noqa: F401 - fixtures re-exported for this module
    _ledger,
    _link_identity,
    _now,
    _user,
    block_anonymization,
    db,
    module_db,
)

MASTER_HEX = "ab" * 32
HUB_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_UUID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PUID = "123456789012345678"
_SQL_FILE = (
    Path(__file__).resolve().parents[2] / "scripts" / "db" / "bundle_reputation_floor_ban.sql"
)

QUEUED, DONE, RETRY, DEAD, RATE_LIMITED, REFUSED, DISPATCHED = 1, 2, 3, 4, 5, 6, 7
PENDING = (QUEUED, RETRY, DEAD, RATE_LIMITED, DISPATCHED)


@pytest.fixture(scope="module", autouse=True)
def registry_schema(module_db: Any) -> Iterator[None]:
    """Every registry table on the shared erasure DB (the floor-ban table is bound by the DAL)."""
    create_missing_registry_tables(module_db.dal)
    ban_suppression._bind(module_db.dal)
    yield


@pytest.fixture(autouse=True)
def master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform KEK a banned erasure needs."""
    monkeypatch.setenv(keys.ENV_CURRENT, MASTER_HEX)
    monkeypatch.delenv(keys.ENV_PREVIOUS, raising=False)


@pytest.fixture(autouse=True)
def emulated_sql_function(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the Postgres-only function call with an emulation of its documented rules."""

    def factory(dal: Any) -> floor_ban_erasure.EraseCall:
        def call(
            token: str, tenant_id: int, platform: str, code: bytes | None
        ) -> tuple[int, int, int]:
            where = "user_uuid = ? AND tenant_id = ? AND platform = ?"
            args = [token, tenant_id, platform]
            [[deleted_before]] = dal.executesql(
                f"SELECT COUNT(*) FROM bundle_reputation_floor_bans WHERE {where} AND status = 6",
                args,
            )
            dal.executesql(
                f"DELETE FROM bundle_reputation_floor_bans WHERE {where} AND status = 6", args
            )
            done = dal.executesql(
                f"SELECT id FROM bundle_reputation_floor_bans WHERE {where} AND status = 2", args
            )
            if done and (code is None or len(code) < 16):
                raise floor_ban_erasure.FloorBanErasureError("suppression_code_required")
            for (row_id,) in done:
                digest = hashlib.sha256(code + int(row_id).to_bytes(8, "big")).hexdigest()[:32]
                dal.executesql(
                    "UPDATE bundle_reputation_floor_bans SET user_uuid = ? WHERE id = ?",
                    [str(uuid.UUID(digest)), row_id],
                )
            [[pending]] = dal.executesql(
                f"SELECT COUNT(*) FROM bundle_reputation_floor_bans WHERE {where} "
                "AND status IN (1, 3, 4, 5, 7)",
                args,
            )
            return int(deleted_before), len(done), int(pending)

        return call

    monkeypatch.setattr(floor_ban_erasure, "_postgres_call", factory)


def _ban_row(db: Any, *, tenant: int, community: int, user_uuid: str, status: int) -> int:
    row_id = db.dal.bundle_reputation_floor_bans.insert(
        tenant_id=tenant,
        community_id=community,
        user_uuid=user_uuid,
        platform="discord",
        status=status,
    )
    db.dal.commit()
    return int(row_id)


def _rows(db: Any) -> dict[int, Any]:
    table = db.dal.bundle_reputation_floor_bans
    rows = {int(r.id): r for r in db.dal(table.id > 0).select()}
    db.dal.commit()
    return rows


def _subject(db: Any, name: str, *, with_identity: bool = True) -> tuple[int, int, int]:
    """(user id, tenant id, community id); the hub user's uuid is HUB_UUID."""
    tenant = _tenant(db, f"floor-{name}")
    community = _community(db, tenant_id=tenant, name=name)
    user = _user(db, name=name)
    if with_identity:
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
    db.dal(db.dal.hub_users.id == user).update(uuid=HUB_UUID)
    db.dal.commit()
    return user, tenant, community


async def _erase(db: Any, user_id: int) -> None:
    await privacy.anonymize_user_data(
        db, db.dal, user_id=user_id, email="subject@example.com", external_stores=FakeExternal()
    )


class TestActiveRestrictions:
    """Which outbox rows make the subject's erasure write a suppression code."""

    def test_every_status_except_refused_is_an_active_auto_ban(self, db: Any) -> None:
        assert set(FLOOR_BAN_ACTIVE_STATUSES) == {DONE, *PENDING}
        assert REFUSED not in FLOOR_BAN_ACTIVE_STATUSES
        user, tenant, community = _subject(db, "active")
        for status in (*PENDING, DONE, REFUSED):
            _ban_row(db, tenant=tenant, community=community, user_uuid=HUB_UUID, status=status)
        identities = ban_suppression.identities_of(db.dal, user)

        found = ban_suppression.collect_active_restrictions(
            db.dal, hub_user_id=user, identities=identities, members=[], user_uuids=[HUB_UUID]
        )

        assert [(r.tenant_id, r.platform, r.reason) for r in found] == [
            (tenant, "discord", RestrictionReason.AUTO_BAN)
        ]

    def test_a_refused_row_alone_is_not_a_restriction(self, db: Any) -> None:
        user, tenant, community = _subject(db, "refusedonly")
        _ban_row(db, tenant=tenant, community=community, user_uuid=HUB_UUID, status=REFUSED)

        found = ban_suppression.collect_active_restrictions(
            db.dal,
            hub_user_id=user,
            identities=ban_suppression.identities_of(db.dal, user),
            members=[],
            user_uuids=[HUB_UUID],
        )

        assert found == []

    def test_another_subjects_rows_are_ignored(self, db: Any) -> None:
        user, tenant, community = _subject(db, "decoy")
        _ban_row(db, tenant=tenant, community=community, user_uuid=OTHER_UUID, status=DONE)

        found = ban_suppression.collect_active_restrictions(
            db.dal,
            hub_user_id=user,
            identities=ban_suppression.identities_of(db.dal, user),
            members=[],
            user_uuids=[HUB_UUID],
        )

        assert found == []


class TestBannedUserErasure:
    """A confirmed automated ban: suppression written, the row pseudonymised, never deleted."""

    async def test_suppression_hash_written_row_pseudonymised_and_user_cannot_rejoin(
        self, db: Any
    ) -> None:
        user, tenant, community = _subject(db, "banned")
        row_id = _ban_row(db, tenant=tenant, community=community, user_uuid=HUB_UUID, status=DONE)

        await _erase(db, user)

        # the keyed suppression code (not the raw id) is the one thing kept about the identity
        [record] = db.dal(db.dal.suppression_records.id > 0).select()
        db.dal.commit()
        code = keys.current_code(keys.PlatformKekKeySource(), tenant, "discord", PUID)
        assert record.hmac == code.hmac
        assert record.reason_enum == RestrictionReason.AUTO_BAN.value
        assert PUID not in repr(record.as_dict())
        # the audit row survives, no longer naming the person
        row = _rows(db)[row_id]
        expected = hashlib.sha256(bytes.fromhex(code.hmac) + row_id.to_bytes(8, "big")).hexdigest()
        assert row.user_uuid == str(uuid.UUID(expected[:32]))
        assert row.user_uuid != HUB_UUID
        assert row.status == DONE
        # ... and the erased user is still caught on re-join
        match = ban_suppression.check_identities(
            db.dal,
            tenant_id=tenant,
            identities=[("discord", PUID)],
            keys=keys.PlatformKekKeySource(),
            now=_now(),
        )
        assert match is not None and match.reason is RestrictionReason.AUTO_BAN
        [ledger] = _ledger(db, user)
        scope = dict(ledger.deletion_scope)
        assert scope["suppression_records_written"] == 1
        assert scope[floor_ban_erasure.COUNT_PSEUDONYMISED] == 1
        assert floor_ban_erasure.COUNT_DELETED not in scope
        assert not privacy._is_delete_count_key(floor_ban_erasure.COUNT_PSEUDONYMISED)

    async def test_a_confirmed_ban_without_a_suppression_code_fails_the_whole_erasure(
        self, db: Any
    ) -> None:
        user, tenant, community = _subject(db, "nocode", with_identity=False)
        row_id = _ban_row(db, tenant=tenant, community=community, user_uuid=HUB_UUID, status=DONE)

        with pytest.raises(floor_ban_erasure.FloorBanErasureError):
            await _erase(db, user)

        assert _rows(db)[row_id].user_uuid == HUB_UUID  # nothing rewritten, nothing deleted
        assert db.dal(db.dal.suppression_records.id > 0).count() == 0
        db.dal.commit()

    async def test_without_suppression_keys_a_confirmed_ban_is_not_erased(self, db: Any) -> None:
        user, tenant, community = _subject(db, "nokeys")
        _ban_row(db, tenant=tenant, community=community, user_uuid=HUB_UUID, status=DONE)
        with pytest.raises(floor_ban_erasure.FloorBanErasureError):
            floor_ban_erasure.erase_floor_ban_rows(
                db.dal, user_uuids=[HUB_UUID], restrictions=[], keys=None
            )


class TestPendingAndRefusedRows:
    """An unsent ban survives erasure; a ban that was never enforced is deleted."""

    @pytest.mark.parametrize("status", PENDING)
    async def test_a_pending_ban_survives_erasure_and_still_writes_the_suppression_code(
        self, db: Any, status: int
    ) -> None:
        user, tenant, community = _subject(db, f"pending{status}")
        row_id = _ban_row(db, tenant=tenant, community=community, user_uuid=HUB_UUID, status=status)

        await _erase(db, user)

        row = _rows(db)[row_id]
        assert (row.user_uuid, row.status) == (HUB_UUID, status)  # the dispatcher can still act
        [record] = db.dal(db.dal.suppression_records.id > 0).select()
        db.dal.commit()
        assert record.reason_enum == RestrictionReason.AUTO_BAN.value
        [ledger] = _ledger(db, user)
        assert ledger.deletion_scope[floor_ban_erasure.COUNT_PENDING] == 1
        assert not privacy._is_delete_count_key(floor_ban_erasure.COUNT_PENDING)

    async def test_a_refused_row_is_deleted_and_writes_no_suppression(self, db: Any) -> None:
        user, tenant, community = _subject(db, "refused")
        row_id = _ban_row(
            db, tenant=tenant, community=community, user_uuid=HUB_UUID, status=REFUSED
        )
        kept = _ban_row(
            db, tenant=tenant, community=community, user_uuid=OTHER_UUID, status=REFUSED
        )

        await _erase(db, user)

        remaining = _rows(db)
        assert row_id not in remaining
        assert kept in remaining  # another subject's refused row is untouched
        assert db.dal(db.dal.suppression_records.id > 0).count() == 0
        db.dal.commit()
        [ledger] = _ledger(db, user)
        assert ledger.deletion_scope[floor_ban_erasure.COUNT_DELETED] == 1


class TestGrantsAreSelectOnly:
    """hub-api must have no table-level DELETE/UPDATE; the SQL function is its only write path."""

    def test_the_sql_grants_hub_api_select_and_execute_only(self) -> None:
        sql = _SQL_FILE.read_text(encoding="utf-8")
        assert "GRANT SELECT ON bundle_reputation_floor_bans TO hub_api;" in sql
        for forbidden in (
            "DELETE ON bundle_reputation_floor_bans",
            "UPDATE ON bundle_reputation_floor_bans",
        ):
            assert forbidden not in sql
        assert "reputation_floor_ban_erase_subject(uuid, bigint, text, bytea)" in sql
        assert "SECURITY DEFINER" in sql.split("reputation_floor_ban_erase_subject", 1)[1][:600]
