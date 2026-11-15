"""Tenant at-rest key provisioning: every tenant owns a DEK before any data is written.

Wires the existing :meth:`TenantEnvelopeService.ensure_tenant_key` (no new crypto) into the three
places a tenant can come into existence, per
``docs/superpowers/specs/2026-11-08-tenant-at-rest-encryption.md`` Sec2:

* **create** -- :func:`provision_new_tenant` is called by the tenant-create path; if the key cannot
  be created the just-inserted tenant row is deleted again and the create fails (no tenant without
  a key). The key store lives on the envelope runtime's own connection, so this is a compensating
  action rather than one SQL transaction; the tenant row is removed before the 503 is returned and a
  failed removal is logged as an error and raised, never swallowed.
* **startup** -- :func:`provision_startup_keys` ensures the global/default tenant and the ``system``
  tenant (creating the reserved ``system`` row if absent) and then backfills every other existing
  tenant. Idempotent, serialised across replicas by a Postgres advisory lock, and it runs only after
  the schema bootstrap (which holds its own advisory lock) has reached READY. Any failure raises:
  hub-api does not report ready with a tenant that has no key.
* **backfill** -- :func:`backfill_tenant_keys` (also ``python3 -m cli.backfill_tenant_keys``).

Key material never passes through this module: it only calls ``ensure_tenant_key`` (which returns
wrapped-key metadata) and logs tenant ids and DEK versions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from opentelemetry import metrics, trace
from penguin_dal import AsyncDB
from sqlalchemy import text

from bootstrap import BootstrapState, BootstrapStatus
from services.bundle_install_dal import raw_sql_rows, raw_sql_write
from services.envelope.models import DekRecord
from services.free_tier_caps import SYSTEM_TENANT_SLUG, ensure_system_tenant

logger = logging.getLogger(__name__)


#: Fixed Postgres advisory-lock key serialising startup/backfill provisioning across replicas.
#: Distinct from ``bootstrap.BOOTSTRAP_LOCK_KEY`` (schema bootstrap) -- provisioning runs only
#: after that lock was released and the schema is at head, so the two never nest.
PROVISION_LOCK_KEY: Final = 0x5741_4442_4F4F_5402

TRIGGER_CREATE: Final = "create"
TRIGGER_STARTUP: Final = "startup"
TRIGGER_BACKFILL: Final = "backfill"

_meter = metrics.get_meter("waddles.hub_api.tenant_key_provisioning")
_tracer = trace.get_tracer("waddles.hub_api.tenant_key_provisioning")
_provisioned = _meter.create_counter(
    "waddles_tenant_key_provisioning_total",
    description="Tenant key provisioning attempts by trigger and result (created/present/failed).",
)
_duration = _meter.create_histogram(
    "waddles_tenant_key_provisioning_duration_seconds",
    unit="s",
    description="Latency of one tenant key provisioning call, by trigger.",
)

_SQL_STARTUP_TENANTS: Final = (
    "SELECT id, slug FROM tenants WHERE is_global = TRUE OR slug = :slug ORDER BY id"
)
_SQL_ALL_TENANTS: Final = "SELECT id, slug FROM tenants ORDER BY id"
_SQL_DELETE_TENANT: Final = "DELETE FROM tenants WHERE id = :id"


class TenantKeyProvisioningError(RuntimeError):
    """A tenant key could not be provisioned; the caller must fail closed."""


class KeyProvisioner(Protocol):
    """The slice of :class:`TenantEnvelopeService` this module needs."""

    async def ensure_tenant_key(
        self, tenant_id: int, *, tenant_slug: str | None = None
    ) -> DekRecord:
        """Return the tenant's active key row, creating version 1 if there is none."""
        ...

    async def get_status(self, tenant_id: int) -> Any:
        """Return the tenant's key summary (``.keys`` lists its DEK versions)."""
        ...


@dataclass(slots=True, frozen=True)
class ProvisionReport:
    """What one provisioning pass did (ids only, never key material)."""

    created: tuple[int, ...] = ()
    already_present: tuple[int, ...] = ()
    failed: tuple[int, ...] = ()

    @property
    def examined(self) -> int:
        """Tenants examined; zero is a failure for callers that expect at least one."""
        return len(self.created) + len(self.already_present) + len(self.failed)


@dataclass(slots=True)
class _Accumulator:
    """Mutable builder for :class:`ProvisionReport`."""

    created: list[int] = field(default_factory=list)
    already_present: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)

    def freeze(self) -> ProvisionReport:
        """Snapshot into the immutable report."""
        return ProvisionReport(tuple(self.created), tuple(self.already_present), tuple(self.failed))


async def ensure_key(
    service: KeyProvisioner, tenant_id: int, slug: str | None, *, trigger: str
) -> bool:
    """Ensure one tenant has an active DEK; return True iff this call created it.

    Raises:
        TenantKeyProvisioningError: the key could not be ensured (KMS down, KEK missing, ...).
    """
    started = time.perf_counter()
    with _tracer.start_as_current_span("tenant_key.provision") as span:
        span.set_attribute("tenant_id", tenant_id)
        span.set_attribute("trigger", trigger)
        try:
            existing = await service.get_status(tenant_id)
            had_key = bool(existing.keys)
            record = await service.ensure_tenant_key(tenant_id, tenant_slug=slug)
        except Exception as exc:
            _provisioned.add(1, {"trigger": trigger, "result": "failed"})
            _duration.record(time.perf_counter() - started, {"trigger": trigger})
            logger.error(
                "tenant_key.provision_failed",
                extra={"tenant_id": tenant_id, "trigger": trigger, "error": type(exc).__name__},
                exc_info=True,
            )
            raise TenantKeyProvisioningError(
                f"could not provision the encryption key for tenant {tenant_id}: "
                f"{type(exc).__name__}"
            ) from exc
        result = "present" if had_key else "created"
        _provisioned.add(1, {"trigger": trigger, "result": result})
        _duration.record(time.perf_counter() - started, {"trigger": trigger})
        logger.info(
            "tenant_key.provisioned",
            extra={
                "tenant_id": tenant_id,
                "trigger": trigger,
                "dek_version": record.dek_version,
                "result": result,
            },
        )
        return not had_key


async def provision_new_tenant(
    dal: AsyncDB, service: KeyProvisioner, *, tenant_id: int, slug: str
) -> None:
    """Create the key for a tenant that was JUST inserted; on failure remove the tenant row.

    Raises:
        TenantKeyProvisioningError: the key could not be created (the tenant row is gone again,
            or -- if even that removal failed -- the error message says so and it is logged).
    """
    try:
        await ensure_key(service, tenant_id, slug, trigger=TRIGGER_CREATE)
    except Exception as exc:
        try:
            await raw_sql_write(dal, _SQL_DELETE_TENANT, {"id": tenant_id})
        except Exception:
            logger.error(
                "tenant_key.create_rollback_failed",
                extra={"tenant_id": tenant_id},
                exc_info=True,
            )
            raise TenantKeyProvisioningError(
                f"tenant {tenant_id} was created without a key and could not be removed"
            ) from exc
        logger.error("tenant_key.create_aborted", extra={"tenant_id": tenant_id})
        raise


async def _provision_rows(
    service: KeyProvisioner, rows: list[tuple[int, str]], *, trigger: str, acc: _Accumulator
) -> None:
    """Ensure keys for ``rows``, recording each outcome; never stops at the first failure."""
    for tenant_id, slug in rows:
        try:
            created = await ensure_key(service, tenant_id, slug, trigger=trigger)
        except TenantKeyProvisioningError:
            acc.failed.append(tenant_id)
            continue
        (acc.created if created else acc.already_present).append(tenant_id)


async def _tenant_rows(
    dal: AsyncDB, sql: str, params: dict[str, Any] | None = None
) -> list[tuple[int, str]]:
    """Read ``(id, slug)`` pairs."""
    rows = await raw_sql_rows(dal, sql, params)
    return [(int(row["id"]), str(row["slug"])) for row in rows.as_list()]


def _raise_on_failures(report: ProvisionReport, *, what: str) -> ProvisionReport:
    """Fail loud when nothing was examined or any tenant failed."""
    if report.examined == 0:
        raise TenantKeyProvisioningError(f"{what}: no tenants were examined")
    if report.failed:
        raise TenantKeyProvisioningError(
            f"{what}: key provisioning failed for tenant ids {list(report.failed)}"
        )
    return report


class _AdvisoryLock:
    """Session-level Postgres advisory lock on a dedicated connection (no-op off Postgres)."""

    def __init__(self, dal: AsyncDB, key: int) -> None:
        """Remember the engine and lock key."""
        self._dal = dal
        self._key = key
        self._conn: Any = None

    async def __aenter__(self) -> None:
        """Block until the lock is held (Postgres only)."""
        engine = self._dal.engine
        if engine.dialect.name != "postgresql":
            return
        self._conn = await engine.connect()
        await self._conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": self._key})

    async def __aexit__(self, *exc_info: object) -> None:
        """Release the lock and return the connection."""
        if self._conn is None:
            return
        try:
            await self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self._key})
        finally:
            await self._conn.close()
            self._conn = None


async def provision_startup_keys(dal: AsyncDB, service: KeyProvisioner) -> ProvisionReport:
    """Startup pass: global + ``system`` tenants first (hard requirement), then the backfill.

    Creates the reserved ``system`` tenant row if absent (the operator-plane tenant has to exist to
    own a key). Raises :class:`TenantKeyProvisioningError` if the global tenant is missing or any
    tenant fails; the caller must then keep hub-api out of service.
    """
    async with _AdvisoryLock(dal, PROVISION_LOCK_KEY):
        await ensure_system_tenant(dal)
        core = await _tenant_rows(dal, _SQL_STARTUP_TENANTS, {"slug": SYSTEM_TENANT_SLUG})
        slugs = {slug for _, slug in core}
        if SYSTEM_TENANT_SLUG not in slugs or len(slugs) < 2:
            raise TenantKeyProvisioningError(
                "startup: the global and system tenants must both exist"
            )
        acc = _Accumulator()
        await _provision_rows(service, core, trigger=TRIGGER_STARTUP, acc=acc)
        done = {tid for tid, _ in core}
        rest = [row for row in await _tenant_rows(dal, _SQL_ALL_TENANTS) if row[0] not in done]
        await _provision_rows(service, rest, trigger=TRIGGER_BACKFILL, acc=acc)
        return _raise_on_failures(acc.freeze(), what="startup provisioning")


async def backfill_tenant_keys(dal: AsyncDB, service: KeyProvisioner) -> ProvisionReport:
    """Idempotent backfill: ensure a key for every tenant (re-running creates nothing new)."""
    async with _AdvisoryLock(dal, PROVISION_LOCK_KEY):
        acc = _Accumulator()
        await _provision_rows(
            service, await _tenant_rows(dal, _SQL_ALL_TENANTS), trigger=TRIGGER_BACKFILL, acc=acc
        )
        return _raise_on_failures(acc.freeze(), what="backfill")


async def run_startup_provisioning(
    status: BootstrapStatus,
    dal: AsyncDB,
    service: KeyProvisioner,
    *,
    attempts: int = 5,
    initial_backoff_seconds: float = 1.0,
    poll_seconds: float = 0.5,
) -> ProvisionReport:
    """Background startup task: wait for the schema, provision keys, then flip readiness.

    Retries transient failures with doubling backoff; after ``attempts`` the status is marked
    FAILED (``/ready`` stays 503 and reports why), an ERROR is logged and the error is re-raised.
    hub-api never reports ready, and the data plane never sees a tenant without a key.
    """
    while not status.is_ready:
        if status.state is BootstrapState.FAILED:
            raise TenantKeyProvisioningError("schema bootstrap failed; tenant keys not provisioned")
        await asyncio.sleep(poll_seconds)
    backoff = initial_backoff_seconds
    for attempt in range(1, attempts + 1):
        try:
            report = await provision_startup_keys(dal, service)
        except Exception as exc:
            if attempt == attempts:
                status.state = BootstrapState.FAILED
                status.detail = f"tenant key provisioning failed: {type(exc).__name__}"
                logger.error(
                    "tenant_key.startup_failed",
                    extra={"attempts": attempts, "action": "tenant_key_startup_failed"},
                    exc_info=True,
                )
                raise
            logger.warning(
                "tenant_key.startup_retry",
                extra={"attempt": attempt, "error": type(exc).__name__},
            )
            await asyncio.sleep(backoff)
            backoff *= 2
            continue
        status.tenant_keys_ready = True
        logger.info(
            "tenant_key.startup_ready",
            extra={
                "created": len(report.created),
                "present": len(report.already_present),
                "examined": report.examined,
            },
        )
        return report
    raise TenantKeyProvisioningError("unreachable")  # pragma: no cover - loop always returns/raises
