"""AuditService against a real (sqlite) database: append, gate, fail-loud, list, verify, tamper."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select, update

from services.audit_chain import GENESIS_HASH, ChainBreakReason, ChainStatus
from services.audit_events import ActorKind, AuditAction, AuditCategory, AuditOutcome
from services.audit_service import (
    AuditError,
    AuditService,
    AuditWriteError,
    ListFilters,
    get_audit_service,
    report_write_failure,
    reset_audit_services,
)
from tests.audit_support import (
    OTHER_USER_UUID,
    USER_UUID,
    FakeGate,
    make_event,
)

TENANT_1_CHAIN = "tenant:1"


async def _rows(dal: Any, chain_id: str = TENANT_1_CHAIN) -> list[Any]:
    table = dal.metadata.tables["audit_events"]
    async with dal.engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    select(table).where(table.c.chain_id == chain_id).order_by(table.c.seq)
                )
            ).all()
        )


async def _mutate(dal: Any, statement: Any) -> None:
    async with dal.engine.begin() as conn:
        await conn.execute(statement)


class TestAppend:
    async def test_first_record_starts_at_genesis_and_links_forward(
        self, audit_service: AuditService
    ) -> None:
        first = await audit_service.record(make_event())
        second = await audit_service.record(make_event(action="admin.other"))
        assert first is not None and second is not None
        assert (first.seq, first.prev_hash) == (1, GENESIS_HASH)
        assert (second.seq, second.prev_hash) == (2, first.record_hash)
        assert first.chain_id == second.chain_id == TENANT_1_CHAIN

    async def test_actor_is_stored_as_the_hub_users_uuid_never_the_int(
        self, audit_service: AuditService, audit_dal: Any
    ) -> None:
        record = await audit_service.record(make_event(user_id=7))
        assert record is not None
        assert record.actor_uuid == str(USER_UUID)
        assert record.actor_kind == ActorKind.USER.value
        (row,) = await _rows(audit_dal)
        assert row.actor_uuid == str(USER_UUID)
        assert "7" not in {str(v) for v in (row.actor_uuid, row.actor_kind)}

    async def test_explicit_actor_uuid_is_used_verbatim(self, audit_service: AuditService) -> None:
        event = make_event(user_id=None)
        event = type(event)(
            category=event.category,
            action=event.action,
            actor_uuid=OTHER_USER_UUID,
            tenant_id=1,
        )
        record = await audit_service.record(event)
        assert record is not None
        assert record.actor_uuid == str(OTHER_USER_UUID)

    async def test_system_actor_has_no_uuid(self, audit_service: AuditService) -> None:
        record = await audit_service.record(make_event(user_id=None))
        assert record is not None
        assert (record.actor_uuid, record.actor_kind) == (None, "system")

    async def test_unresolvable_actor_is_recorded_as_unresolved_with_one_warning(
        self, audit_service: AuditService, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="services.audit_service")
        a = await audit_service.record(make_event(user_id=9999))
        b = await audit_service.record(make_event(user_id=9998))
        assert a is not None and b is not None
        assert (a.actor_uuid, a.actor_kind) == (None, "unresolved")
        warnings = [r for r in caplog.records if "could not be resolved" in r.getMessage()]
        assert len(warnings) == 1  # not one per event

    async def test_actor_uuid_lookup_is_cached(
        self, audit_service: AuditService, audit_dal: Any
    ) -> None:
        await audit_service.record(make_event(user_id=7))
        users = audit_dal.metadata.tables["hub_users"]
        await _mutate(audit_dal, delete(users).where(users.c.id == 7))
        again = await audit_service.record(make_event(user_id=7))
        assert again is not None and again.actor_uuid == str(USER_UUID)

    async def test_tenants_have_independent_chains(self, audit_service: AuditService) -> None:
        a = await audit_service.record(make_event(tenant_id=1))
        b = await audit_service.record(make_event(tenant_id=2))
        a2 = await audit_service.record(make_event(tenant_id=1))
        assert a is not None and b is not None and a2 is not None
        assert (a.chain_id, b.chain_id) == ("tenant:1", "tenant:2")
        assert (a.seq, b.seq, a2.seq) == (1, 1, 2)
        assert b.prev_hash == GENESIS_HASH  # a tenant chain never links into another

    async def test_slug_alone_resolves_the_same_chain_as_the_id(
        self, audit_service: AuditService
    ) -> None:
        by_slug = await audit_service.record(make_event(tenant_id=None, tenant_slug="acme-corp"))
        by_id = await audit_service.record(make_event(tenant_id=1))
        assert by_slug is not None and by_id is not None
        assert by_slug.chain_id == by_id.chain_id == TENANT_1_CHAIN

    async def test_no_tenant_goes_to_the_platform_chain_gated_on_the_default_tenant(
        self, audit_service: AuditService, gate: FakeGate
    ) -> None:
        record = await audit_service.record(make_event(tenant_id=None))
        assert record is not None and record.chain_id == "platform"
        assert gate.calls[-1] == "global"

    async def test_timeline_is_non_decreasing_even_if_the_clock_runs_backwards(
        self, audit_service: AuditService, audit_dal: Any
    ) -> None:
        table = audit_dal.metadata.tables["audit_events"]
        first = await audit_service.record(make_event())
        assert first is not None
        # Simulate a replica whose clock is ~1 day behind the head: the new record is
        # clamped to the head's timestamp instead of going backwards in time.
        future = datetime.now(UTC) + timedelta(days=1)
        async with audit_dal.engine.begin() as conn:
            await conn.execute(update(table).values(occurred_at=future.replace(tzinfo=None)))
        second = await audit_service.record(make_event())
        assert second is not None
        assert second.occurred_at >= first.occurred_at

    async def test_details_round_trip_exactly_so_the_chain_re_verifies(
        self, audit_service: AuditService
    ) -> None:
        await audit_service.record(
            make_event(details={"required_scopes": ["tenant:admin"], "status": 200, "ok": True})
        )
        report = await audit_service.verify(TENANT_1_CHAIN)
        assert report.verification.ok

    async def test_concurrent_appends_produce_one_gapless_linear_chain(
        self, audit_service: AuditService
    ) -> None:
        """Writers racing on the same head must serialise, never fork or skip a seq."""
        results = await asyncio.gather(
            *(audit_service.record(make_event(action=f"admin.race_{i % 5}")) for i in range(25))
        )
        seqs = sorted(r.seq for r in results if r is not None)
        assert seqs == list(range(1, 26))
        report = await audit_service.verify(TENANT_1_CHAIN)
        assert report.verification.ok
        assert report.verification.examined == 25

    async def test_exhausted_retries_fail_loudly(
        self,
        audit_service: AuditService,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import services.audit_service as module

        monkeypatch.setattr(module, "MAX_APPEND_ATTEMPTS", 3)
        monkeypatch.setattr(module, "insert", _FailingInsert)

        async def head_keeps_advancing(_table: Any, _chain: str) -> int:
            return 10**6  # every failure looks like "someone else took my seq"

        monkeypatch.setattr(audit_service, "_head_seq", head_keeps_advancing)
        caplog.set_level(logging.ERROR, logger="services.audit_service")
        with pytest.raises(AuditWriteError, match="sustained contention"):
            await audit_service.record(make_event())
        assert any("attempts=3" in r.getMessage() for r in caplog.records)

    async def test_a_genuine_constraint_violation_is_not_retried_and_is_loud(
        self,
        audit_service: AuditService,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import services.audit_service as module

        monkeypatch.setattr(module, "insert", _FailingInsert)
        attempts: list[int] = []

        async def head_not_advanced(_table: Any, _chain: str) -> int | None:
            attempts.append(1)
            return None  # nobody else wrote: the violation is ours, not a race

        monkeypatch.setattr(audit_service, "_head_seq", head_not_advanced)
        caplog.set_level(logging.ERROR, logger="services.audit_service")
        with pytest.raises(AuditWriteError, match="database constraint"):
            await audit_service.record(make_event())
        assert len(attempts) == 1  # fail-fast, no retry loop masking a real bug
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "audit write FAILED" in text and "Traceback (most recent call last)" in text


class _FailingInsert:
    """Stand-in for `sqlalchemy.insert` whose statement violates NOT NULL after the head read."""

    def __init__(self, _table: Any) -> None:
        pass

    def values(self, **_kwargs: Any) -> Any:
        from sqlalchemy import text

        return text("INSERT INTO audit_events (chain_id) VALUES ('tenant:1')")


class TestEntitlementGate:
    async def test_unentitled_tenant_is_skipped_by_policy_and_writes_nothing(
        self, audit_service: AuditService, audit_dal: Any, gate: FakeGate
    ) -> None:
        gate.entitled = False
        assert await audit_service.record(make_event()) is None
        assert await _rows(audit_dal) == []

    async def test_gate_is_per_tenant(self, audit_service: AuditService, gate: FakeGate) -> None:
        gate.entitled = {"acme-corp"}
        assert await audit_service.record(make_event(tenant_id=1)) is not None
        assert await audit_service.record(make_event(tenant_id=2)) is None
        assert gate.calls == ["acme-corp", "other-co"]

    async def test_unentitled_skip_does_not_touch_the_audit_table_at_all(
        self, audit_dal: Any, gate: FakeGate
    ) -> None:
        """A tenant without the feature must not fail just because the migration lagged."""
        gate.entitled = False
        service = AuditService(audit_dal, gate=gate)
        audit_dal.metadata.remove(audit_dal.metadata.tables["audit_events"])
        assert await service.record(make_event()) is None

    async def test_unknown_external_tenant_slug_never_reaches_the_gate(
        self, audit_service: AuditService, gate: FakeGate
    ) -> None:
        assert await audit_service.find_tenant(tenant_id=None, tenant_slug="no-such") is None
        assert gate.calls == []


class TestFailLoud:
    """GRC #3: the old `except: pass` is gone -- a write failure is loud and propagates."""

    async def test_missing_table_is_a_loud_actionable_error(
        self, audit_dal: Any, gate: FakeGate, caplog: pytest.LogCaptureFixture
    ) -> None:
        service = AuditService(audit_dal, gate=gate)
        audit_dal.metadata.remove(audit_dal.metadata.tables["audit_events"])
        caplog.set_level(logging.ERROR, logger="services.audit_service")
        with pytest.raises(AuditWriteError, match="0053_audit_events_hash_chain"):
            await service.record(make_event())
        assert any("audit write FAILED" in r.getMessage() for r in caplog.records)

    async def test_write_failure_is_counted_logged_with_a_traceback_and_leaks_no_values(
        self,
        audit_service: AuditService,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "bob-the-secret-value"

        async def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError(f"driver says bound value {secret} failed")

        monkeypatch.setattr(audit_service, "_append", boom)
        caplog.set_level(logging.ERROR, logger="services.audit_service")
        with pytest.raises(AuditWriteError) as raised:
            await audit_service.record(make_event(action="admin.something"))
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "audit write FAILED" in text
        assert "event was NOT recorded" in text
        assert "action=admin.something" in text
        assert "type=RuntimeError" in text  # exception TYPE is logged
        assert "Traceback (most recent call last)" in text  # and a traceback
        assert secret not in text  # but never the driver message (may embed bound values)
        assert secret not in str(raised.value)
        assert isinstance(raised.value.__cause__, RuntimeError)  # chained, not discarded

    async def test_report_write_failure_is_the_single_loud_path(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR, logger="services.audit_service")
        bound = "-".join(["secret", "bound", "value"])  # runtime value, absent from source lines
        try:
            raise ValueError(bound)
        except ValueError as exc:
            error = report_write_failure(
                category="bundle", action="x.y", chain_id=None, attempts=1, exc=exc
            )
        assert isinstance(error, AuditWriteError)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "chain=unresolved" in text and bound not in text

    async def test_authored_audit_errors_keep_their_message_in_the_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR, logger="services.audit_service")
        try:
            raise AuditError("application-authored detail")
        except AuditError as exc:
            report_write_failure(
                category="a", action="b.c", chain_id="platform", attempts=0, exc=exc
            )
        assert "application-authored detail" in "\n".join(r.getMessage() for r in caplog.records)

    async def test_unknown_tenant_in_an_event_is_a_loud_error(
        self, audit_service: AuditService
    ) -> None:
        with pytest.raises(AuditWriteError, match="does not exist"):
            await audit_service.record(make_event(tenant_id=424242))


class TestListAndHead:
    async def _seed(self, service: AuditService) -> None:
        await service.record(make_event(action="admin.action", user_id=7))
        await service.record(
            make_event(
                action=AuditAction.AUTHZ_DENIED,
                category=AuditCategory.AUTHZ,
                outcome=AuditOutcome.DENIED,
                user_id=8,
            )
        )
        await service.record(
            make_event(
                action=AuditAction.TENANT_ADMIN_ADDED, category=AuditCategory.ROLE, user_id=7
            )
        )
        await service.record(make_event(tenant_id=2, action="admin.other_tenant"))

    async def test_list_is_newest_first_and_tenant_scoped(
        self, audit_service: AuditService
    ) -> None:
        await self._seed(audit_service)
        records, total = await audit_service.list_events(
            TENANT_1_CHAIN, filters=ListFilters(), page=1, limit=50
        )
        assert total == 3
        assert [r.seq for r in records] == [3, 2, 1]
        assert all(r.chain_id == TENANT_1_CHAIN for r in records)

    @pytest.mark.parametrize(
        ("filters", "expected_seqs"),
        [
            (ListFilters(category="authz"), [2]),
            (ListFilters(action="role.x"), []),
            (ListFilters(action=str(AuditAction.TENANT_ADMIN_ADDED)), [3]),
            (ListFilters(outcome="denied"), [2]),
            (ListFilters(outcome="failure"), []),
            (ListFilters(actor_uuid=USER_UUID), [3, 1]),
            (ListFilters(actor_uuid=OTHER_USER_UUID), [2]),
            (ListFilters(actor_uuid=uuid.uuid4()), []),
            (ListFilters(since=datetime.now(UTC) - timedelta(minutes=5)), [3, 2, 1]),
            (ListFilters(since=datetime.now(UTC) + timedelta(minutes=5)), []),
            (ListFilters(until=datetime.now(UTC) + timedelta(minutes=5)), [3, 2, 1]),
            (ListFilters(until=datetime.now(UTC) - timedelta(minutes=5)), []),
            (ListFilters(category="authz", outcome="success"), []),
        ],
    )
    async def test_filters(
        self, audit_service: AuditService, filters: ListFilters, expected_seqs: list[int]
    ) -> None:
        await self._seed(audit_service)
        records, total = await audit_service.list_events(
            TENANT_1_CHAIN, filters=filters, page=1, limit=50
        )
        assert [r.seq for r in records] == expected_seqs
        assert total == len(expected_seqs)

    async def test_pagination_and_limit_clamping(self, audit_service: AuditService) -> None:
        for _ in range(7):
            await audit_service.record(make_event())
        page1, total = await audit_service.list_events(
            TENANT_1_CHAIN, filters=ListFilters(), page=1, limit=3
        )
        page3, _ = await audit_service.list_events(
            TENANT_1_CHAIN, filters=ListFilters(), page=3, limit=3
        )
        assert total == 7
        assert [r.seq for r in page1] == [7, 6, 5]
        assert [r.seq for r in page3] == [1]
        clamped, _ = await audit_service.list_events(
            TENANT_1_CHAIN, filters=ListFilters(), page=-4, limit=10_000
        )
        assert len(clamped) == 7  # page floored to 1, limit capped (not an error)

    async def test_head_and_empty_head(self, audit_service: AuditService) -> None:
        assert await audit_service.head(TENANT_1_CHAIN) is None
        last = None
        for _ in range(3):
            last = await audit_service.record(make_event())
        head = await audit_service.head(TENANT_1_CHAIN)
        assert last is not None and head is not None
        assert (head.seq, head.record_hash) == (3, last.record_hash)

    async def test_read_range_is_ascending_keyset(self, audit_service: AuditService) -> None:
        for _ in range(5):
            await audit_service.record(make_event())
        page = await audit_service.read_range(TENANT_1_CHAIN, after_seq=2, limit=2)
        assert [r.seq for r in page] == [3, 4]

    async def test_chain_ids(self, audit_service: AuditService) -> None:
        await self._seed(audit_service)
        assert await audit_service.chain_ids() == ["tenant:1", "tenant:2"]


class TestVerifyAndTamper:
    """The database-level counterpart of test_audit_chain.TestTamperDetection."""

    async def _chain(self, service: AuditService, n: int = 8) -> None:
        for i in range(n):
            await service.record(make_event(action=f"admin.step_{i}"))

    async def test_clean_chain_is_intact(self, audit_service: AuditService) -> None:
        await self._chain(audit_service)
        report = await audit_service.verify(TENANT_1_CHAIN)
        assert report.verification.status is ChainStatus.INTACT
        assert report.complete
        assert report.next_seq is None and report.anchor_hash is None

    async def test_empty_chain_is_reported_empty_not_ok(self, audit_service: AuditService) -> None:
        report = await audit_service.verify(TENANT_1_CHAIN)
        assert report.verification.status is ChainStatus.EMPTY
        assert not report.verification.ok

    async def test_editing_a_stored_row_is_detected(
        self, audit_service: AuditService, audit_dal: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._chain(audit_service)
        table = audit_dal.metadata.tables["audit_events"]
        await _mutate(
            audit_dal,
            update(table).where(table.c.seq == 4).values(action="admin.nothing_to_see_here"),
        )
        caplog.set_level(logging.ERROR, logger="services.audit_service")
        report = await audit_service.verify(TENANT_1_CHAIN)
        assert report.verification.status is ChainStatus.BROKEN
        assert report.verification.break_ is not None
        assert report.verification.break_.seq == 4
        assert report.verification.break_.reason is ChainBreakReason.HASH_MISMATCH
        # tamper detection must page someone: ERROR log, chain id + seq + reason, no PII
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "AUDIT CHAIN TAMPER DETECTED" in text and "first_bad_seq=4" in text

    async def test_editing_the_actor_is_detected(
        self, audit_service: AuditService, audit_dal: Any
    ) -> None:
        await self._chain(audit_service)
        table = audit_dal.metadata.tables["audit_events"]
        await _mutate(
            audit_dal,
            update(table).where(table.c.seq == 2).values(actor_uuid=str(OTHER_USER_UUID)),
        )
        report = await audit_service.verify(TENANT_1_CHAIN)
        assert report.verification.status is ChainStatus.BROKEN
        assert report.verification.break_ is not None and report.verification.break_.seq == 2

    async def test_editing_details_json_is_detected(
        self, audit_service: AuditService, audit_dal: Any
    ) -> None:
        await self._chain(audit_service)
        table = audit_dal.metadata.tables["audit_events"]
        await _mutate(
            audit_dal, update(table).where(table.c.seq == 6).values(details={"method": "GET"})
        )
        assert (
            await audit_service.verify(TENANT_1_CHAIN)
        ).verification.status is ChainStatus.BROKEN

    async def test_deleting_a_row_is_detected(
        self, audit_service: AuditService, audit_dal: Any
    ) -> None:
        await self._chain(audit_service)
        table = audit_dal.metadata.tables["audit_events"]
        await _mutate(audit_dal, delete(table).where(table.c.seq == 4))
        report = await audit_service.verify(TENANT_1_CHAIN)
        assert report.verification.status is ChainStatus.BROKEN
        assert report.verification.break_ is not None
        assert report.verification.break_.reason is ChainBreakReason.SEQ_GAP

    async def test_truncating_the_tail_is_only_caught_with_a_pinned_head(
        self, audit_service: AuditService, audit_dal: Any
    ) -> None:
        await self._chain(audit_service)
        head = await audit_service.head(TENANT_1_CHAIN)
        assert head is not None
        table = audit_dal.metadata.tables["audit_events"]
        await _mutate(audit_dal, delete(table).where(table.c.seq > 5))
        assert (await audit_service.verify(TENANT_1_CHAIN)).verification.ok  # honest limit
        pinned = await audit_service.verify(
            TENANT_1_CHAIN, expected_head_seq=head.seq, expected_head_hash=head.record_hash
        )
        assert pinned.verification.status is ChainStatus.BROKEN
        assert pinned.verification.break_ is not None
        assert pinned.verification.break_.reason is ChainBreakReason.HEAD_MISMATCH

    async def test_tampering_one_tenant_does_not_affect_another_chain(
        self, audit_service: AuditService, audit_dal: Any
    ) -> None:
        await self._chain(audit_service, 4)
        await audit_service.record(make_event(tenant_id=2))
        table = audit_dal.metadata.tables["audit_events"]
        await _mutate(
            audit_dal,
            update(table).where(table.c.chain_id == "tenant:1").values(action="admin.x"),
        )
        assert (await audit_service.verify("tenant:2")).verification.ok
        assert (await audit_service.verify("tenant:1")).verification.status is ChainStatus.BROKEN

    async def test_long_chains_verify_in_resumable_slices(
        self, audit_service: AuditService
    ) -> None:
        await self._chain(audit_service, 12)
        first = await audit_service.verify(TENANT_1_CHAIN, max_records=5)
        assert first.verification.ok and not first.complete
        assert (first.verification.examined, first.next_seq) == (5, 6)
        assert first.anchor_hash is not None
        second = await audit_service.verify(
            TENANT_1_CHAIN, from_seq=6, anchor_hash=first.anchor_hash, max_records=5
        )
        assert second.verification.ok and not second.complete and second.next_seq == 11
        assert second.anchor_hash is not None
        third = await audit_service.verify(
            TENANT_1_CHAIN, from_seq=11, anchor_hash=second.anchor_hash, max_records=5
        )
        assert third.verification.ok and third.complete and third.next_seq is None

    async def test_a_forged_anchor_is_rejected(self, audit_service: AuditService) -> None:
        await self._chain(audit_service, 6)
        report = await audit_service.verify(TENANT_1_CHAIN, from_seq=3, anchor_hash="a" * 64)
        assert report.verification.status is ChainStatus.BROKEN
        assert report.verification.break_ is not None
        assert report.verification.break_.reason is ChainBreakReason.PREV_HASH_MISMATCH

    async def test_resuming_requires_an_anchor_and_a_positive_seq(
        self, audit_service: AuditService
    ) -> None:
        with pytest.raises(AuditError, match="anchor_hash"):
            await audit_service.verify(TENANT_1_CHAIN, from_seq=5)
        with pytest.raises(AuditError, match="from_seq"):
            await audit_service.verify(TENANT_1_CHAIN, from_seq=0)

    async def test_pin_checks_are_skipped_on_an_incomplete_slice(
        self, audit_service: AuditService
    ) -> None:
        await self._chain(audit_service, 6)
        report = await audit_service.verify(TENANT_1_CHAIN, max_records=3, expected_head_seq=6)
        assert report.verification.ok and not report.complete  # head not reached yet


class TestServiceRegistry:
    async def test_one_service_per_dal_and_resettable(self, audit_dal: Any) -> None:
        reset_audit_services()
        assert get_audit_service(audit_dal) is get_audit_service(audit_dal)
        first = get_audit_service(audit_dal)
        reset_audit_services()
        assert get_audit_service(audit_dal) is not first
        reset_audit_services()
