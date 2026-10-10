"""`bundle_audit.record` -- regression for GRC #3's `except: pass` (best-effort -> fail-loud).

Before this change a failed `audit_log` insert was swallowed by a bare ``except Exception: pass``,
so a permission grant or global install could happen with no audit trail and no signal. These tests
pin the replacement contract: the failure is logged at ERROR (type + value-free cause + traceback),
counted, and raised as `AuditWriteError`; and, for entitled tenants, the same event lands in the
tamper-evident chain.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from services import bundle_audit
from services.audit_service import AuditService, AuditWriteError, reset_audit_services
from tests.audit_support import (
    USER_UUID,
    FakeGate,
)


@pytest.fixture(autouse=True)
def _isolated_services() -> Any:
    reset_audit_services()
    yield
    reset_audit_services()


@pytest.fixture
def entitled(monkeypatch: pytest.MonkeyPatch, gate: FakeGate) -> FakeGate:
    """Route the bridge's cached service through the controllable gate."""
    import services.audit_service as module

    monkeypatch.setattr(module, "default_gate", gate)
    return gate


async def _legacy_rows(dal: Any) -> list[Any]:
    table = dal.metadata.tables["audit_log"]
    async with dal.engine.connect() as conn:
        return list((await conn.execute(select(table))).all())


async def _chain_rows(dal: Any) -> list[Any]:
    table = dal.metadata.tables["audit_events"]
    async with dal.engine.connect() as conn:
        return list((await conn.execute(select(table).order_by(table.c.seq))).all())


class TestNoSwallow:
    def test_the_bare_except_pass_is_gone_from_bundle_audit_source(self) -> None:
        """Static guard: every handler in bundle_audit.py re-raises or hands the error back."""
        source = Path(bundle_audit.__file__).read_text(encoding="utf-8")
        handlers = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ExceptHandler)]
        assert handlers, "scanner examined no handlers -- it is pointed at the wrong file"

        def loud(handler: ast.ExceptHandler) -> bool:
            raises = any(isinstance(n, ast.Raise) for n in ast.walk(handler))
            returns_the_error = handler.name is not None and any(
                isinstance(n, ast.Return)
                and isinstance(n.value, ast.Name)
                and n.value.id == handler.name
                for n in ast.walk(handler)
            )
            return raises or returns_the_error  # try_record() returns it for DeferredAudit

        for handler in handlers:
            assert not all(isinstance(stmt, ast.Pass) for stmt in handler.body)
            assert loud(handler), f"bundle_audit.py:{handler.lineno} swallows an exception"

    async def test_a_failed_legacy_insert_is_raised_not_swallowed(
        self,
        audit_dal: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "-".join(["bound", "value", "that", "must", "not", "leak"])

        async def boom(_self: Any, **_kw: Any) -> None:
            raise RuntimeError(f"insert failed: {secret}")

        # `audit_dal.audit_log` builds a fresh TableProxy per access, so patch the class.
        monkeypatch.setattr(type(audit_dal.audit_log), "async_insert", boom)
        caplog.set_level(logging.ERROR, logger="services.audit_service")
        with pytest.raises(AuditWriteError) as raised:
            await bundle_audit.record(
                audit_dal,
                actor_id=7,
                action="app_installed_globally",
                target_type="app_global_installs",
                target_id="waddles.core.ping@1.0.0",
            )
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "audit write FAILED" in text
        assert "action=app_installed_globally" in text
        assert "type=RuntimeError" in text
        assert "Traceback (most recent call last)" in text
        assert secret not in text and secret not in str(raised.value)
        assert isinstance(raised.value.__cause__, RuntimeError)

    async def test_a_failed_chain_write_is_raised_after_the_legacy_row_is_kept(
        self,
        audit_dal: Any,
        entitled: FakeGate,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def boom(self: AuditService, *_a: object, **_k: object) -> None:
            raise AuditWriteError("chain unavailable")

        monkeypatch.setattr(AuditService, "record", boom)
        with pytest.raises(AuditWriteError):
            await bundle_audit.record(
                audit_dal,
                actor_id=7,
                action="app_installed_globally",
                target_type="app_global_installs",
                target_id="a.b@1.0.0",
                details={"tenant_id": 1},
            )
        assert len(await _legacy_rows(audit_dal)) == 1  # the basic trail still has it


class TestLegacyTrailUnchanged:
    async def test_legacy_audit_log_row_is_written_in_every_tier(
        self,
        audit_dal: Any,
        entitled: FakeGate,
    ) -> None:
        entitled.entitled = False
        await bundle_audit.record(
            audit_dal,
            actor_id=7,
            action="tenant_availability_enabled",
            target_type="bundle_tenant_availability",
            target_id="1:waddles.core.ping",
            details={"reason": "free text is fine in the legacy row"},
        )
        (row,) = await _legacy_rows(audit_dal)
        assert row.user_id == 7
        assert row.action == "tenant_availability_enabled"
        assert row.details == {"reason": "free text is fine in the legacy row"}
        assert await _chain_rows(audit_dal) == []  # un-entitled: no chain record, by policy

    async def test_system_actor_is_recorded_without_a_user(
        self,
        audit_dal: Any,
        entitled: FakeGate,
    ) -> None:
        await bundle_audit.record(
            audit_dal,
            actor_id=None,
            action="app_installed_globally",
            target_type="app_global_installs",
            target_id="waddles.core.ping@1.0.0",
            details={"tenant_id": 1},
        )
        (legacy,) = await _legacy_rows(audit_dal)
        (chain,) = await _chain_rows(audit_dal)
        assert legacy.user_id is None
        assert (chain.actor_kind, chain.actor_uuid) == ("system", None)


class TestChainBridge:
    async def test_entitled_tenant_gets_a_chain_record_with_a_uuid_actor(
        self,
        audit_dal: Any,
        entitled: FakeGate,
    ) -> None:
        await bundle_audit.record(
            audit_dal,
            actor_id=7,
            action="permissions_approved_globally",
            target_type="app_permission_requests",
            target_id="waddles.core.ping@1.0.0",
            details={"permission_ids": ["storage.kv", "net.http"], "tenant_id": 1},
        )
        (chain,) = await _chain_rows(audit_dal)
        assert chain.chain_id == "tenant:1"  # tenant_id in details picks the chain
        assert chain.category == "bundle"
        assert chain.actor_uuid == str(USER_UUID)
        assert chain.details["permission_ids"] == ["storage.kv", "net.http"]

    async def test_explicit_tenant_id_wins_over_details(
        self,
        audit_dal: Any,
        entitled: FakeGate,
    ) -> None:
        await bundle_audit.record(
            audit_dal,
            actor_id=7,
            action="x_happened",
            target_type="thing",
            target_id="abc",
            details={"tenant_id": 1},
            tenant_id=2,
        )
        (chain,) = await _chain_rows(audit_dal)
        assert chain.chain_id == "tenant:2"

    async def test_no_tenant_means_the_platform_chain(
        self,
        audit_dal: Any,
        entitled: FakeGate,
    ) -> None:
        await bundle_audit.record(
            audit_dal,
            actor_id=7,
            action="instance_permission_policy_set",
            target_type="instance_permission_policies",
            target_id="net.http.private-ip",
        )
        (chain,) = await _chain_rows(audit_dal)
        assert chain.chain_id == "platform"

    async def test_free_text_details_are_dropped_from_the_chain_but_counted(
        self,
        audit_dal: Any,
        entitled: FakeGate,
    ) -> None:
        free_text = "target app 'bob smith' is not installed"
        await bundle_audit.record(
            audit_dal,
            actor_id=7,
            action="routes_to_refused",
            target_type="app_install_approvals",
            target_id="a.b@1.0.0",
            details={"target_app_id": "waddles.other", "reason": free_text},
        )
        (chain,) = await _chain_rows(audit_dal)
        assert chain.details == {"target_app_id": "waddles.other", "dropped_detail_keys": 1}
        assert free_text not in str(chain.details)  # raw text never reaches the tamper-evident log
        (legacy,) = await _legacy_rows(audit_dal)
        assert legacy.details["reason"] == free_text  # the legacy row keeps what it always kept

    async def test_non_identifier_target_is_dropped_and_flagged_not_raised(
        self,
        audit_dal: Any,
        entitled: FakeGate,
    ) -> None:
        await bundle_audit.record(
            audit_dal,
            actor_id=7,
            action="x_happened",
            target_type="Weird Type",
            target_id="has spaces",
        )
        (chain,) = await _chain_rows(audit_dal)
        assert (chain.target_type, chain.target_id) == (None, None)
        assert chain.details["target_id_dropped"] is True

    async def test_bool_tenant_id_in_details_is_not_mistaken_for_a_tenant(
        self,
        audit_dal: Any,
        entitled: FakeGate,
    ) -> None:
        await bundle_audit.record(
            audit_dal,
            actor_id=7,
            action="x_happened",
            target_type="thing",
            target_id="abc",
            details={"tenant_id": True},
        )
        (chain,) = await _chain_rows(audit_dal)
        assert chain.chain_id == "platform"


class TestDeferredAudit:
    """`try_record`/`DeferredAudit`: loud, but never skips a flow's required follow-on work."""

    async def test_try_record_returns_instead_of_raising(
        self,
        audit_dal: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def down(_dal: Any, **_kw: Any) -> None:
            raise AuditWriteError("down")

        monkeypatch.setattr(bundle_audit, "record", down)
        failure = await bundle_audit.try_record(audit_dal, action="x_happened")
        assert isinstance(failure, AuditWriteError)

    async def test_try_record_returns_none_on_success(
        self, audit_dal: Any, entitled: FakeGate
    ) -> None:
        failure = await bundle_audit.try_record(
            audit_dal,
            actor_id=7,
            action="x_happened",
            target_type="thing",
            target_id="abc",
        )
        assert failure is None
        assert len(await _legacy_rows(audit_dal)) == 1

    async def test_deferred_audit_keeps_the_first_failure_and_raises_it_later(
        self,
        audit_dal: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []

        async def flaky(_dal: Any, *, action: str, **_kw: Any) -> None:
            calls.append(action)
            raise AuditWriteError(f"down: {action}")

        monkeypatch.setattr(bundle_audit, "record", flaky)
        deferred = bundle_audit.DeferredAudit()
        await deferred.record(audit_dal, action="first_event")
        await deferred.record(audit_dal, action="second_event")  # still attempted
        assert calls == ["first_event", "second_event"]
        with pytest.raises(AuditWriteError, match="first_event"):
            deferred.raise_if_failed()

    async def test_deferred_audit_with_no_failure_does_not_raise(
        self, audit_dal: Any, entitled: FakeGate
    ) -> None:
        deferred = bundle_audit.DeferredAudit()
        await deferred.record(
            audit_dal, actor_id=7, action="x_happened", target_type="thing", target_id="abc"
        )
        deferred.raise_if_failed()
        assert deferred.error is None


class TestLegacyTimestampForm:
    """Regression: the legacy insert failed on real Postgres (tz-aware value, naive column)."""

    def test_naive_timestamp_column_gets_a_naive_utc_value(self, audit_dal: Any) -> None:
        value = bundle_audit._legacy_created_at(audit_dal)
        assert value.tzinfo is None  # the sqlite mirror and the baseline are plain TIMESTAMP

    def test_timezone_aware_column_gets_an_aware_value(self, audit_dal: Any) -> None:
        from sqlalchemy import Column, DateTime, MetaData, Table

        stub = Table("audit_log", MetaData(), Column("created_at", DateTime(timezone=True)))
        fake = type("Dal", (), {"metadata": type("M", (), {"tables": {"audit_log": stub}})()})()
        assert bundle_audit._legacy_created_at(fake).tzinfo is not None

    def test_missing_table_falls_back_to_naive(self) -> None:
        fake = type("Dal", (), {"metadata": type("M", (), {"tables": {}})()})()
        assert bundle_audit._legacy_created_at(fake).tzinfo is None
