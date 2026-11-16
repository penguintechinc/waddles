"""Tenant key provisioning against a REAL migrated Postgres (create, startup, backfill, failure).

Real: Postgres 17 at Alembic head, the production key-store DDL, the real envelope service and
platform-KEK AES wrap. The only injected fault is a KEK whose KMS raises ``KmsUnavailableError``
(what a real outage does).
"""

from __future__ import annotations

import pytest
from penguin_dal import AsyncDB

from bootstrap import BootstrapState, BootstrapStatus
from services.bundle_install_dal import raw_sql_rows
from services.tenant_key_provisioning import (
    PROVISION_LOCK_KEY,
    SYSTEM_TENANT_SLUG,
    TenantKeyProvisioningError,
    backfill_tenant_keys,
    provision_new_tenant,
    provision_startup_keys,
    run_startup_provisioning,
)
from tests.tenant_keys_pg import (
    DownKek,
    key_rows,
    make_service,
    new_tenant,
    requires_docker,
    scalar,
)

pytestmark = requires_docker


async def _tenant_exists(tk_dal: AsyncDB, tenant_id: int) -> bool:
    rows = await raw_sql_rows(tk_dal, "SELECT id FROM tenants WHERE id = :i", {"i": tenant_id})
    return bool(rows.as_list())


class TestCreate:
    async def test_create_provisions_version_one(self, tk_dal, tk_sql) -> None:  # type: ignore[no-untyped-def]
        tenant_id = new_tenant(tk_sql)
        await provision_new_tenant(tk_dal, make_service(tk_dal), tenant_id=tenant_id, slug="x")
        assert key_rows(tk_sql, tenant_id) == 1
        assert (
            scalar(
                tk_sql,
                "SELECT dek_version FROM keystore.tenant_encryption_keys "
                "WHERE tenant_id = %s AND status = 'active'",
                (tenant_id,),
            )
            == 1
        )

    async def test_failed_create_removes_the_tenant(self, tk_dal, tk_sql) -> None:  # type: ignore[no-untyped-def]
        tenant_id = new_tenant(tk_sql)
        with pytest.raises(TenantKeyProvisioningError):
            await provision_new_tenant(
                tk_dal, make_service(tk_dal, DownKek(b"\x07" * 32)), tenant_id=tenant_id, slug="x"
            )
        assert not await _tenant_exists(tk_dal, tenant_id)  # no tenant without a key
        assert key_rows(tk_sql, tenant_id) == 0


class TestStartup:
    async def test_startup_provisions_global_and_system_and_is_idempotent(  # type: ignore[no-untyped-def]
        self, tk_dal, tk_sql
    ) -> None:
        other = new_tenant(tk_sql)
        service = make_service(tk_dal)
        first = await provision_startup_keys(tk_dal, service)
        system_id = int(
            scalar(tk_sql, "SELECT id FROM tenants WHERE slug = %s", (SYSTEM_TENANT_SLUG,))  # type: ignore[arg-type]
        )
        global_ids = [
            int(r[0]) for r in _all(tk_sql, "SELECT id FROM tenants WHERE is_global = TRUE")
        ]
        assert global_ids, "the migrated schema must seed the global tenant"
        for tenant_id in (system_id, *global_ids, other):
            assert key_rows(tk_sql, tenant_id) == 1
            assert tenant_id in first.created
        assert first.examined >= 3
        second = await provision_startup_keys(tk_dal, service)  # re-run: nothing new, no dupes
        assert second.created == () and second.examined == first.examined
        for tenant_id in (system_id, *global_ids, other):
            assert key_rows(tk_sql, tenant_id) == 1
        assert (
            int(
                scalar(
                    tk_sql, "SELECT count(*) FROM tenants WHERE slug = %s", (SYSTEM_TENANT_SLUG,)
                )
            )  # type: ignore[arg-type]
            == 1
        )

    async def test_startup_fails_loud_when_the_kms_is_down(self, tk_dal, tk_sql) -> None:  # type: ignore[no-untyped-def]
        new_tenant(tk_sql)
        with pytest.raises(TenantKeyProvisioningError, match="startup provisioning"):
            await provision_startup_keys(tk_dal, make_service(tk_dal, DownKek(b"\x07" * 32)))

    async def test_runner_marks_not_ready_after_retries(self, tk_dal) -> None:  # type: ignore[no-untyped-def]
        status = BootstrapStatus(state=BootstrapState.READY)
        with pytest.raises(TenantKeyProvisioningError):
            await run_startup_provisioning(
                status,
                tk_dal,
                make_service(tk_dal, DownKek(b"\x07" * 32)),
                attempts=2,
                initial_backoff_seconds=0.01,
            )
        assert status.state is BootstrapState.FAILED
        assert not status.serving_ready and not status.tenant_keys_ready

    async def test_runner_flips_readiness_on_success(self, tk_dal) -> None:  # type: ignore[no-untyped-def]
        status = BootstrapStatus(state=BootstrapState.READY)
        assert not status.serving_ready  # schema ready alone is not enough
        report = await run_startup_provisioning(status, tk_dal, make_service(tk_dal))
        assert status.serving_ready and report.examined >= 2

    async def test_provisioning_lock_is_released(self, tk_dal, tk_sql) -> None:  # type: ignore[no-untyped-def]
        await provision_startup_keys(tk_dal, make_service(tk_dal))
        held = scalar(
            tk_sql,
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = %s AND granted",
            (PROVISION_LOCK_KEY & 0xFFFFFFFF,),
        )
        assert held == 0


class TestBackfill:
    async def test_backfill_creates_missing_keys_once(self, tk_dal, tk_sql) -> None:  # type: ignore[no-untyped-def]
        ids = [new_tenant(tk_sql) for _ in range(3)]
        service = make_service(tk_dal)
        first = await backfill_tenant_keys(tk_dal, service)
        assert set(ids) <= set(first.created)
        second = await backfill_tenant_keys(tk_dal, service)
        assert second.created == () and set(ids) <= set(second.already_present)
        for tenant_id in ids:
            assert key_rows(tk_sql, tenant_id) == 1  # idempotent: never a second DEK

    async def test_backfill_reports_every_failure_and_raises(self, tk_dal, tk_sql) -> None:  # type: ignore[no-untyped-def]
        new_tenant(tk_sql)
        with pytest.raises(TenantKeyProvisioningError, match="failed for tenant ids"):
            await backfill_tenant_keys(tk_dal, make_service(tk_dal, DownKek(b"\x07" * 32)))


def _all(conn, query: str) -> list[tuple]:  # type: ignore[no-untyped-def,type-arg]
    with conn.cursor() as cur:
        cur.execute(query)
        return list(cur.fetchall())
