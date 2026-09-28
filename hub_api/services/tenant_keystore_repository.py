"""Postgres-backed `KeystoreRepository` -- talks to the dedicated `keystore` schema.

Uses a dedicated `asyncpg` pool (see `config.py::keystore_database_url`,
`app.py` startup) authenticated as hub-api's own key-store role, never
the shared `dal`/`async_dal` connection used for application tables --
that separation is the point of migration 0027 (own schema, own backup
policy, own grants).
"""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg

from services.tenant_keystore import TenantDekRecord


def _to_record(row: asyncpg.Record) -> TenantDekRecord:
    return TenantDekRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        dek_version=row["dek_version"],
        wrapped_dek=row["wrapped_dek"],
        kek_ref=row["kek_ref"],
        kek_kind=row["kek_kind"],
        status=row["status"],
        usage_count=row["usage_count"],
        activated_at=row["activated_at"],
        retired_at=row["retired_at"],
        destroyed_at=row["destroyed_at"],
    )


class AsyncpgKeystoreRepository:
    """`KeystoreRepository` implementation over `keystore.tenant_encryption_keys`."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap an `asyncpg` pool already authenticated as the key-store role."""
        self._pool = pool

    async def get_active(self, tenant_id: int) -> TenantDekRecord | None:
        """See `KeystoreRepository.get_active`."""
        row = await self._pool.fetchrow(
            "SELECT * FROM keystore.tenant_encryption_keys "
            "WHERE tenant_id = $1 AND status = 'active' "
            "ORDER BY dek_version DESC LIMIT 1",
            tenant_id,
        )
        return _to_record(row) if row is not None else None

    async def get_version(self, tenant_id: int, dek_version: int) -> TenantDekRecord | None:
        """See `KeystoreRepository.get_version`."""
        row = await self._pool.fetchrow(
            "SELECT * FROM keystore.tenant_encryption_keys "
            "WHERE tenant_id = $1 AND dek_version = $2",
            tenant_id,
            dek_version,
        )
        return _to_record(row) if row is not None else None

    async def insert_active(
        self, tenant_id: int, wrapped_dek: bytes, kek_ref: str, kek_kind: str
    ) -> TenantDekRecord:
        """See `KeystoreRepository.insert_active`."""
        row = await self._pool.fetchrow(
            """
            INSERT INTO keystore.tenant_encryption_keys
                (tenant_id, dek_version, wrapped_dek, kek_ref, kek_kind, status)
            VALUES (
                $1,
                COALESCE(
                    (SELECT MAX(dek_version) + 1 FROM keystore.tenant_encryption_keys
                     WHERE tenant_id = $1),
                    1
                ),
                $2, $3, $4, 'active'
            )
            RETURNING *
            """,
            tenant_id,
            wrapped_dek,
            kek_ref,
            kek_kind,
        )
        if row is None:
            raise RuntimeError("INSERT ... RETURNING * yielded no row -- driver/schema mismatch")
        return _to_record(row)

    async def retire(self, tenant_id: int, dek_version: int) -> None:
        """See `KeystoreRepository.retire`."""
        await self._pool.execute(
            "UPDATE keystore.tenant_encryption_keys "
            "SET status = 'retired', retired_at = $3 "
            "WHERE tenant_id = $1 AND dek_version = $2 AND status = 'active'",
            tenant_id,
            dek_version,
            datetime.now(UTC),
        )

    async def increment_usage(self, tenant_id: int, dek_version: int) -> int:
        """See `KeystoreRepository.increment_usage`."""
        new_count: int = await self._pool.fetchval(
            "UPDATE keystore.tenant_encryption_keys "
            "SET usage_count = usage_count + 1 "
            "WHERE tenant_id = $1 AND dek_version = $2 "
            "RETURNING usage_count",
            tenant_id,
            dek_version,
        )
        return new_count

    async def destroy_all(self, tenant_id: int) -> None:
        """See `KeystoreRepository.destroy_all`."""
        await self._pool.execute(
            "UPDATE keystore.tenant_encryption_keys "
            "SET status = 'destroyed', destroyed_at = $2, wrapped_dek = NULL "
            "WHERE tenant_id = $1 AND status != 'destroyed'",
            tenant_id,
            datetime.now(UTC),
        )

    async def add_tombstone(self, tenant_id: int, dek_version: int, reason: str) -> None:
        """See `KeystoreRepository.add_tombstone`."""
        await self._pool.execute(
            "INSERT INTO keystore.key_tombstones (tenant_id, dek_version, reason) "
            "VALUES ($1, $2, $3)",
            tenant_id,
            dek_version,
            reason,
        )

    async def is_shredded(self, tenant_id: int) -> bool:
        """See `KeystoreRepository.is_shredded`."""
        row = await self._pool.fetchval(
            "SELECT 1 FROM keystore.key_tombstones WHERE tenant_id = $1 LIMIT 1",
            tenant_id,
        )
        return row is not None
