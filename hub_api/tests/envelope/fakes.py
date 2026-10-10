"""In-memory collaborators for envelope tests: repositories, entitlement gate, counting KEK.

The repositories honour the *same* contract as ``PenguinDalEnvelopeRepository``
(one active row per tenant, compare-and-swap re-wrap, conflict on a lost race)
so the service's concurrency-sensitive paths run against faithful semantics; the
real SQL is separately proven against Postgres in ``test_repository_pg.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from services.envelope.models import (
    CONFIG_PENDING,
    KEY_ACTIVE,
    KEY_RETIRED,
    DekRecord,
    KmsKeyInfo,
    TenantKmsConfig,
)
from services.envelope.platform_kek import PlatformKekAdapter
from services.envelope.repository import KeyConflictError

TENANT_A = 101
TENANT_B = 202
SLUG_A = "acme-corp"
SLUG_B = "other-corp"


class InMemoryEnvelopeRepo:
    """Key rows + KMS config rows for any number of tenants (implements both protocols)."""

    def __init__(self) -> None:
        """Start empty."""
        self.rows: dict[int, list[DekRecord]] = {}
        self.configs: dict[int, TenantKmsConfig] = {}
        self._next_id = 1
        self.usage_flushes: list[tuple[int, int, int]] = []
        self.fail_set_status = False

    # -- keys
    async def get_active(self, tenant_id: int) -> DekRecord | None:
        """Return the single active row."""
        return next((r for r in self.rows.get(tenant_id, []) if r.status == KEY_ACTIVE), None)

    async def get_version(self, tenant_id: int, dek_version: int) -> DekRecord | None:
        """Return one version."""
        return next((r for r in self.rows.get(tenant_id, []) if r.dek_version == dek_version), None)

    async def list_keys(self, tenant_id: int) -> list[DekRecord]:
        """Return every non-destroyed row, oldest first."""
        return sorted(
            (r for r in self.rows.get(tenant_id, []) if r.status != "destroyed"),
            key=lambda r: r.dek_version,
        )

    def _new(self, tenant_id: int, wrapped: bytes, kind: str, ref: str) -> DekRecord:
        rows = self.rows.setdefault(tenant_id, [])
        record = DekRecord(
            id=self._next_id,
            tenant_id=tenant_id,
            dek_version=max((r.dek_version for r in rows), default=0) + 1,
            wrapped_dek=wrapped,
            kek_kind=kind,
            kek_ref=ref,
            status=KEY_ACTIVE,
            usage_count=0,
            activated_at=datetime.now(UTC),
        )
        self._next_id += 1
        rows.append(record)
        return record

    async def insert_active(
        self, tenant_id: int, wrapped_dek: bytes, kek_kind: str, kek_ref: str
    ) -> DekRecord:
        """Insert version N+1; conflict if one is already active."""
        if await self.get_active(tenant_id) is not None:
            raise KeyConflictError("tenant key was created concurrently")
        return self._new(tenant_id, wrapped_dek, kek_kind, kek_ref)

    async def rotate_active(
        self,
        tenant_id: int,
        *,
        expected_version: int | None,
        wrapped_dek: bytes,
        kek_kind: str,
        kek_ref: str,
    ) -> DekRecord:
        """Retire the active row and insert the next, conflicting on a stale expectation."""
        active = await self.get_active(tenant_id)
        current = active.dek_version if active is not None else None
        if current != expected_version:
            raise KeyConflictError("the active key changed concurrently")
        if active is not None:
            rows = self.rows[tenant_id]
            rows[rows.index(active)] = replace(active, status=KEY_RETIRED)
        return self._new(tenant_id, wrapped_dek, kek_kind, kek_ref)

    async def replace_wrapped(
        self, record: DekRecord, *, new_wrapped: bytes, kek_kind: str, kek_ref: str
    ) -> bool:
        """Compare-and-swap on the old wrapped bytes."""
        rows = self.rows.get(record.tenant_id, [])
        for index, row in enumerate(rows):
            if row.id == record.id:
                if row.wrapped_dek != record.wrapped_dek:
                    return False
                rows[index] = replace(
                    row, wrapped_dek=new_wrapped, kek_kind=kek_kind, kek_ref=kek_ref
                )
                return True
        return False

    async def add_usage(self, tenant_id: int, dek_version: int, count: int) -> int:
        """Add to the usage counter."""
        rows = self.rows.get(tenant_id, [])
        for index, row in enumerate(rows):
            if row.dek_version == dek_version:
                rows[index] = replace(row, usage_count=row.usage_count + count)
                self.usage_flushes.append((tenant_id, dek_version, count))
                return rows[index].usage_count
        return 0

    # -- config
    async def get(self, tenant_id: int) -> TenantKmsConfig | None:
        """Return the config row."""
        return self.configs.get(tenant_id)

    async def upsert(
        self,
        tenant_id: int,
        *,
        provider: str,
        key_ref: str,
        region: str | None,
        principal: str | None,
        new_external_id: str,
    ) -> TenantKmsConfig:
        """Create/update as pending; the ExternalId is preserved on update."""
        existing = self.configs.get(tenant_id)
        config = TenantKmsConfig(
            tenant_id=tenant_id,
            provider=provider,
            key_ref=key_ref,
            region=region,
            principal=principal,
            external_id=existing.external_id if existing else new_external_id,
            status=CONFIG_PENDING,
            created_at=existing.created_at if existing else datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.configs[tenant_id] = config
        return config

    async def set_status(
        self,
        tenant_id: int,
        status: str,
        *,
        error_code: str | None = None,
        verified: bool = False,
    ) -> None:
        """Set status (optionally failing, to prove best-effort flagging is not load-bearing)."""
        if self.fail_set_status:
            raise RuntimeError("simulated config-store outage")
        config = self.configs[tenant_id]
        self.configs[tenant_id] = replace(
            config,
            status=status,
            last_error_code=error_code,
            last_verified_at=datetime.now(UTC) if verified else config.last_verified_at,
        )

    async def delete(self, tenant_id: int) -> None:
        """Drop the config row."""
        self.configs.pop(tenant_id, None)


@dataclass(slots=True)
class FakeGate:
    """Entitlement gate keyed on slug; records every question it was asked."""

    entitled: set[str] = field(default_factory=set)
    asked: list[str] = field(default_factory=list)

    async def is_entitled(self, tenant_slug: str, *, request_host: str | None = None) -> bool:
        """Answer from the ``entitled`` set."""
        self.asked.append(tenant_slug)
        return tenant_slug in self.entitled


class CountingPlatformKek(PlatformKekAdapter):
    """The real platform KEK, with call counters (to prove nothing silently falls back to it)."""

    def __init__(self, current: bytes, previous: bytes | None = None) -> None:
        """Wrap a real key; counters start at zero."""
        super().__init__(current, previous)
        self.wraps = 0
        self.unwraps = 0

    async def wrap(self, plaintext_dek: bytes, *, context: Mapping[str, str]) -> bytes:
        """Count, then really wrap."""
        self.wraps += 1
        return await super().wrap(plaintext_dek, context=context)

    async def unwrap(self, wrapped_dek: bytes, *, context: Mapping[str, str]) -> bytes:
        """Count, then really unwrap."""
        self.unwraps += 1
        return await super().unwrap(wrapped_dek, context=context)

    async def verify(self) -> KmsKeyInfo:
        """Delegate."""
        return await super().verify()
