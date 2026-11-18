"""`analytics_exclusions_service`: planning, same-transaction writes, audit/metrics (sqlite).

Real pydal + sqlite. The Postgres-only pieces (reconcile, advisory lock) are in
`test_analytics_exclusions_pg_integration.py`.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from services import analytics_exclusions_service as ex
from services import audit_http
from services import cookie_consent_service as consent
from services.analytics_exclusions_service import ExclusionError
from services.analytics_exclusions_service import ExclusionReason as R
from services.audit_events import AuditAction


def _user(db: Any, n: int) -> tuple[int, str]:
    dal = db.dal
    uid = dal.hub_users.insert(id=n, email=f"x{n}@e.com", username=f"x{n}", is_active=True)
    dal.commit()
    row = dal(dal.hub_users.id == uid).select(dal.hub_users.uuid).first()
    return int(uid), str(row.uuid)


def _reasons(db: Any, token: str) -> set[int]:
    return set(ex.exclusion_reasons(db.dal, token))


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    service = MagicMock()
    service.record = AsyncMock(return_value=None)
    monkeypatch.setattr(audit_http, "get_service", lambda: service)
    return service


class TestPlan:
    @pytest.mark.parametrize(
        ("previous", "merged", "gpc", "add", "remove"),
        [
            (None, {"doNotSell": True}, False, {R.DO_NOT_SELL}, set()),
            ({}, {"doNotSell": True}, True, {R.DO_NOT_SELL, R.GPC}, set()),
            ({"marketing": True}, {"marketing": False}, False, {R.DO_NOT_SELL}, set()),
            ({"doNotSell": True}, {"doNotSell": False}, False, set(), {R.DO_NOT_SELL, R.GPC}),
            ({"marketing": False}, {"marketing": True}, False, set(), {R.DO_NOT_SELL}),
            (
                {"marketing": False},
                {"marketing": True, "doNotSell": True},
                False,
                {R.DO_NOT_SELL},
                set(),
            ),
            (None, {"marketing": False}, False, set(), set()),
            ({"functional": False}, {"functional": True}, False, set(), set()),
        ],
    )
    def test_plan(self, previous: Any, merged: Any, gpc: bool, add: set, remove: set) -> None:
        plan = ex.plan_consent_change(previous, merged, gpc_applied=gpc)
        assert (set(plan.add), set(plan.remove)) == (add, remove)

    def test_plan_never_touches_erased_age_or_objection(self) -> None:
        for prev in ({}, {"doNotSell": True, "marketing": True}):
            for merged in ({"doNotSell": False, "marketing": True}, {"doNotSell": True}):
                plan = ex.plan_consent_change(prev, merged)
                assert not ({R.ERASED, R.AGE_NOT_ADULT, R.OBJECTION} & (plan.add | plan.remove))


class TestRows:
    def test_add_is_idempotent_and_pii_free(self, privacy_db: Any) -> None:
        _, token = _user(privacy_db, 1)
        dal = privacy_db.dal
        assert ex.add_exclusion(dal, token, R.DO_NOT_SELL) is True
        assert ex.add_exclusion(dal, token, R.DO_NOT_SELL) is False
        dal.commit()
        cols = [r[1] for r in dal.executesql("PRAGMA table_info(data_use_exclusions)")]
        assert cols == ["user_uuid", "reason_code", "since"]

    def test_erased_and_age_are_never_removable(self, privacy_db: Any) -> None:
        _, token = _user(privacy_db, 1)
        dal = privacy_db.dal
        ex.add_exclusion(dal, token, R.ERASED)
        ex.add_exclusion(dal, token, R.AGE_NOT_ADULT)
        for reason in (R.ERASED, R.AGE_NOT_ADULT):
            with pytest.raises(ExclusionError):
                ex.remove_exclusion(dal, token, reason)
        assert _reasons(privacy_db, token) == {4, 5}

    def test_malformed_token_is_refused(self, privacy_db: Any) -> None:
        with pytest.raises(ExclusionError):
            ex.add_exclusion(privacy_db.dal, "'; DROP TABLE data_use_exclusions; --", R.GPC)

    def test_reconcile_refuses_non_postgres(self, privacy_db: Any) -> None:
        with pytest.raises(ExclusionError):
            ex.reconcile_exclusions(privacy_db.dal)


class TestConsentFlows:
    async def _save(
        self, db: Any, uid: int | None, prefs: dict, *, gpc: bool = False, cid: str = "c1"
    ) -> None:
        await consent.save_consent(
            db,
            db.dal,
            user_id=uid,
            consent_id=cid,
            preferences=prefs,
            consent_method="banner",
            ip_address=None,
            user_agent=None,
            version="1.0",
            gpc_applied=gpc,
        )

    async def test_do_not_sell_and_gpc_insert_exclusions_with_audit(
        self, privacy_db: Any, audit: MagicMock
    ) -> None:
        uid, token = _user(privacy_db, 5)
        await self._save(privacy_db, uid, {"doNotSell": True}, gpc=True)
        assert _reasons(privacy_db, token) == {1, 2}
        actions = {c.args[0].action for c in audit.record.await_args_list}
        assert actions == {AuditAction.PRIVACY_EXCLUSION_ADDED.value}
        assert audit.record.await_count == 2
        details = [c.args[0].details for c in audit.record.await_args_list]
        assert all(set(d) == {"reason_code"} for d in details)

    async def test_opt_back_in_removes_only_consent_reasons_never_erased(
        self, privacy_db: Any, audit: MagicMock
    ) -> None:
        uid, token = _user(privacy_db, 6)
        await self._save(privacy_db, uid, {"doNotSell": True}, gpc=True)
        ex.add_exclusion(privacy_db.dal, token, R.ERASED)
        ex.add_exclusion(privacy_db.dal, token, R.OBJECTION)
        privacy_db.dal.commit()
        await self._save(privacy_db, uid, {"doNotSell": False})
        assert _reasons(privacy_db, token) == {3, 4}
        removed = [
            c.args[0].details["reason_code"]
            for c in audit.record.await_args_list
            if c.args[0].action == AuditAction.PRIVACY_EXCLUSION_REMOVED.value
        ]
        assert sorted(removed) == [1, 2]

    async def test_marketing_withdrawal_adds_and_regrant_removes(
        self, privacy_db: Any, audit: MagicMock
    ) -> None:
        uid, token = _user(privacy_db, 7)
        await self._save(privacy_db, uid, {"marketing": True})
        assert _reasons(privacy_db, token) == set()
        await consent.revoke_consent(privacy_db, privacy_db.dal, user_id=uid)
        assert _reasons(privacy_db, token) == {1}
        await consent.update_preferences(
            privacy_db, privacy_db.dal, user_id=uid, preferences={"marketing": True}
        )
        assert _reasons(privacy_db, token) == set()

    async def test_anonymous_visitor_has_no_subject_and_no_exclusion(
        self, privacy_db: Any, audit: MagicMock
    ) -> None:
        await self._save(privacy_db, None, {"doNotSell": True})
        assert privacy_db.dal.executesql("SELECT count(*) FROM data_use_exclusions")[0][0] == 0

    async def test_unknown_subject_rolls_back_consent_too_and_raises(
        self, privacy_db: Any, audit: MagicMock
    ) -> None:
        with pytest.raises(ExclusionError):
            await self._save(privacy_db, 999, {"doNotSell": True}, cid="ghost")
        dal = privacy_db.dal
        assert dal(dal.cookie_consent.consent_id == "ghost").count() == 0
        dal.rollback()

    async def test_write_failure_leaves_no_exclusion(
        self, privacy_db: Any, audit: MagicMock
    ) -> None:
        uid, token = _user(privacy_db, 8)

        def _boom(_d: Any) -> None:
            raise RuntimeError("write failed")

        with pytest.raises(RuntimeError):
            await ex.apply_consent_change(
                privacy_db,
                privacy_db.dal,
                user_id=uid,
                write=_boom,
                previous=None,
                merged={"doNotSell": True},
            )
        assert _reasons(privacy_db, token) == set()

    async def test_audit_failure_is_loud_after_commit(
        self, privacy_db: Any, audit: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        uid, token = _user(privacy_db, 9)
        audit.record = AsyncMock(side_effect=RuntimeError("chain down"))
        caplog.set_level(logging.ERROR)
        with pytest.raises(Exception, match="(?i)audit"):
            await self._save(privacy_db, uid, {"doNotSell": True})
        assert _reasons(privacy_db, token) == {1}  # committed; failure reported, not hidden

    async def test_unwired_audit_is_logged_not_silent(
        self,
        privacy_db: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(audit_http, "get_service", lambda: None)
        uid, _ = _user(privacy_db, 10)
        caplog.set_level(logging.WARNING)
        await self._save(privacy_db, uid, {"doNotSell": True})
        assert any("audit_unwired" in r.getMessage() for r in caplog.records)
