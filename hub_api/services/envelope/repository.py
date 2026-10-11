"""Storage boundary for wrapped DEKs and per-tenant KMS config.

:class:`EnvelopeKeyRepository` / :class:`KmsConfigRepository` are the only
things :class:`~services.envelope.service.TenantEnvelopeService` knows about
persistence -- unit tests drive it with in-memory fakes, production uses
:class:`PenguinDalEnvelopeRepository` over hub-api's penguin-dal ``AsyncDB``.

Every statement below is a complete, literal, parameterized SQL string: no
interpolation of any kind, so there is no string-built query for an
injection to land in (values only ever travel as bind parameters).

**Tenant isolation at the storage layer.** Every statement is scoped by
``tenant_id`` -- there is no query that can address another tenant's key
or config by id alone, and no method that lists across tenants.

**Key store placement (design Sec4).** Wrapped DEKs live in the dedicated
``keystore`` schema, apart from the application tables whose data they
protect. The dedicated *role/connection and short-retention backup
policy* for that schema are infrastructure follow-ups: hand this class an
``AsyncDB`` authenticated as that role and nothing else changes.

The DDL these queries target is ``alembic/versions/0054_tenant_external_
kms.py``; its key-store table is a superset-compatible ``IF NOT EXISTS`` of
the one in the open DEK-broker PR (#442), so the two migrations are
order-independent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from penguin_dal import AsyncDB
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from services.bundle_install_dal import raw_sql_rows, raw_sql_write
from services.envelope.errors import EnvelopeError
from services.envelope.models import (
    CONFIG_PENDING,
    DekRecord,
    TenantKmsConfig,
)

#: The only key lineage this repository reads/writes (``ingest-stream`` is
#: reserved for the DEK-broker PR's purpose-limited stream key).
PURPOSE_AT_REST = "at-rest"

_SQL_GET_ACTIVE = """
SELECT id, tenant_id, dek_version, wrapped_dek, kek_kind, kek_ref, status, usage_count,
       activated_at, retired_at
  FROM keystore.tenant_encryption_keys
 WHERE tenant_id = :t AND purpose = :p AND status = 'active'
 ORDER BY dek_version DESC LIMIT 1
"""

_SQL_GET_VERSION = """
SELECT id, tenant_id, dek_version, wrapped_dek, kek_kind, kek_ref, status, usage_count,
       activated_at, retired_at
  FROM keystore.tenant_encryption_keys
 WHERE tenant_id = :t AND purpose = :p AND dek_version = :v
"""

_SQL_LIST_KEYS = """
SELECT id, tenant_id, dek_version, wrapped_dek, kek_kind, kek_ref, status, usage_count,
       activated_at, retired_at
  FROM keystore.tenant_encryption_keys
 WHERE tenant_id = :t AND purpose = :p AND status <> 'destroyed'
 ORDER BY dek_version
"""

# The next version is computed in the INSERT itself; two concurrent writers
# collide on UNIQUE (tenant_id, purpose, dek_version) / the one-active partial
# unique index and the loser surfaces as an IntegrityError -> KeyConflictError.
# `:tenant_id` / `:purpose` appear twice (VALUES list and the MAX() subquery), so asyncpg
# needs an explicit type to deduce one consistent parameter type (found by
# tests/envelope/test_repository_pg.py against the real schema).
_SQL_INSERT_NEXT_VERSION = """
INSERT INTO keystore.tenant_encryption_keys
    (tenant_id, purpose, dek_version, wrapped_dek, kek_ref, kek_kind, status)
VALUES (
    CAST(:tenant_id AS INTEGER), CAST(:purpose AS VARCHAR(30)),
    COALESCE((SELECT MAX(dek_version) FROM keystore.tenant_encryption_keys
               WHERE tenant_id = CAST(:tenant_id AS INTEGER)
                 AND purpose = CAST(:purpose AS VARCHAR(30))), 0) + 1,
    :wrapped, :kek_ref, :kek_kind, 'active')
RETURNING id, tenant_id, dek_version, wrapped_dek, kek_kind, kek_ref, status, usage_count,
          activated_at, retired_at
"""

_SQL_RETIRE_ACTIVE = """
UPDATE keystore.tenant_encryption_keys
   SET status = 'retired', retired_at = NOW()
 WHERE tenant_id = :t AND purpose = :p AND status = 'active'
RETURNING dek_version
"""

_SQL_REPLACE_WRAPPED = """
UPDATE keystore.tenant_encryption_keys
   SET wrapped_dek = :new, kek_kind = :kind, kek_ref = :ref, rewrapped_at = NOW()
 WHERE id = :id AND tenant_id = :t AND wrapped_dek = :old AND status <> 'destroyed'
RETURNING id
"""

_SQL_ADD_USAGE = """
UPDATE keystore.tenant_encryption_keys
   SET usage_count = usage_count + :n
 WHERE tenant_id = :t AND purpose = :p AND dek_version = :v
RETURNING usage_count
"""

_SQL_CONFIG_GET = """
SELECT tenant_id, provider, key_ref, region, principal, external_id, status,
       last_verified_at, last_error_code, created_at, updated_at
  FROM tenant_kms_configs
 WHERE tenant_id = :t
"""

# external_id is deliberately absent from the DO UPDATE list: the customer's
# trust policy pins it, so editing the key/role must never rotate it.
_SQL_CONFIG_UPSERT = """
INSERT INTO tenant_kms_configs
    (tenant_id, provider, key_ref, region, principal, external_id, status)
VALUES (:t, :provider, :key_ref, :region, :principal, :ext, :status)
ON CONFLICT (tenant_id) DO UPDATE SET
    provider = EXCLUDED.provider, key_ref = EXCLUDED.key_ref,
    region = EXCLUDED.region, principal = EXCLUDED.principal,
    status = EXCLUDED.status, last_verified_at = NULL, last_error_code = NULL,
    updated_at = NOW()
RETURNING tenant_id, provider, key_ref, region, principal, external_id, status,
          last_verified_at, last_error_code, created_at, updated_at
"""

_SQL_CONFIG_SET_STATUS = """
UPDATE tenant_kms_configs
   SET status = :status, last_error_code = :err,
       last_verified_at = CASE WHEN :verified THEN NOW() ELSE last_verified_at END,
       updated_at = NOW()
 WHERE tenant_id = :t
"""

_SQL_CONFIG_DELETE = "DELETE FROM tenant_kms_configs WHERE tenant_id = :t"


class KeyConflictError(EnvelopeError):
    """A concurrent writer created/rotated the tenant's active key first -- re-read and retry."""


class EnvelopeKeyRepository(Protocol):
    """Persistence for a tenant's wrapped DEK versions."""

    async def get_active(self, tenant_id: int) -> DekRecord | None:
        """Return the tenant's single ``active`` key row, or ``None`` if none exists."""
        ...

    async def get_version(self, tenant_id: int, dek_version: int) -> DekRecord | None:
        """Return one specific key version (any status), or ``None``."""
        ...

    async def list_keys(self, tenant_id: int) -> list[DekRecord]:
        """Return every non-destroyed key row for the tenant, oldest first."""
        ...

    async def insert_active(
        self, tenant_id: int, wrapped_dek: bytes, kek_kind: str, kek_ref: str
    ) -> DekRecord:
        """Insert the next version as ``active``; raise :class:`KeyConflictError` on a race."""
        ...

    async def rotate_active(
        self,
        tenant_id: int,
        *,
        expected_version: int | None,
        wrapped_dek: bytes,
        kek_kind: str,
        kek_ref: str,
    ) -> DekRecord:
        """Atomically retire the active version and insert the next one.

        Raises :class:`KeyConflictError` if the active version is no longer
        `expected_version` (someone else rotated first).
        """
        ...

    async def replace_wrapped(
        self,
        record: DekRecord,
        *,
        new_wrapped: bytes,
        kek_kind: str,
        kek_ref: str,
    ) -> bool:
        """Compare-and-swap a row's wrapped DEK; return False if it changed underneath us."""
        ...

    async def add_usage(self, tenant_id: int, dek_version: int, count: int) -> int:
        """Add `count` to the version's usage counter; return the new total."""
        ...


class KmsConfigRepository(Protocol):
    """Persistence for the per-tenant external KMS configuration."""

    async def get(self, tenant_id: int) -> TenantKmsConfig | None:
        """Return the tenant's config row, or ``None``."""
        ...

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
        """Create or update the config as ``pending``; an existing ExternalId is preserved."""
        ...

    async def set_status(
        self,
        tenant_id: int,
        status: str,
        *,
        error_code: str | None = None,
        verified: bool = False,
    ) -> None:
        """Set the config status; ``verified`` stamps ``last_verified_at``."""
        ...

    async def delete(self, tenant_id: int) -> None:
        """Remove the tenant's config row (the exit back to the platform baseline)."""
        ...


def _key_record(row: Mapping[str, Any]) -> DekRecord:
    """Convert a result mapping to a :class:`DekRecord` (the only place key bytes are unpacked)."""
    wrapped = row["wrapped_dek"]
    return DekRecord(
        id=int(row["id"]),
        tenant_id=int(row["tenant_id"]),
        dek_version=int(row["dek_version"]),
        wrapped_dek=bytes(wrapped) if wrapped is not None else None,
        kek_kind=str(row["kek_kind"]),
        kek_ref=str(row["kek_ref"]),
        status=str(row["status"]),
        usage_count=int(row["usage_count"]),
        activated_at=row["activated_at"],
        retired_at=row["retired_at"],
    )


def _config(row: Mapping[str, Any]) -> TenantKmsConfig:
    """Convert a result mapping to a :class:`TenantKmsConfig`."""
    return TenantKmsConfig(
        tenant_id=int(row["tenant_id"]),
        provider=str(row["provider"]),
        key_ref=str(row["key_ref"]),
        region=row["region"],
        principal=row["principal"],
        external_id=str(row["external_id"]),
        status=str(row["status"]),
        last_verified_at=row["last_verified_at"],
        last_error_code=row["last_error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _insert_params(
    tenant_id: int, wrapped_dek: bytes, kek_kind: str, kek_ref: str
) -> dict[str, Any]:
    """Bind parameters for :data:`_SQL_INSERT_NEXT_VERSION`."""
    return {
        "tenant_id": tenant_id,
        "purpose": PURPOSE_AT_REST,
        "wrapped": wrapped_dek,
        "kek_ref": kek_ref,
        "kek_kind": kek_kind,
    }


class PenguinDalEnvelopeRepository:
    """Postgres implementation of both repository protocols over a penguin-dal ``AsyncDB``."""

    def __init__(self, dal: AsyncDB) -> None:
        """Wrap `dal` (the hub-api ``install_dal``, or one on the key-store role)."""
        self._dal = dal

    async def get_active(self, tenant_id: int) -> DekRecord | None:
        """See :meth:`EnvelopeKeyRepository.get_active`."""
        params = {"t": tenant_id, "p": PURPOSE_AT_REST}
        rows = await raw_sql_rows(self._dal, _SQL_GET_ACTIVE, params)
        row = rows.first()
        return _key_record(row.as_dict()) if row is not None else None

    async def get_version(self, tenant_id: int, dek_version: int) -> DekRecord | None:
        """See :meth:`EnvelopeKeyRepository.get_version`."""
        rows = await raw_sql_rows(
            self._dal, _SQL_GET_VERSION, {"t": tenant_id, "p": PURPOSE_AT_REST, "v": dek_version}
        )
        row = rows.first()
        return _key_record(row.as_dict()) if row is not None else None

    async def list_keys(self, tenant_id: int) -> list[DekRecord]:
        """See :meth:`EnvelopeKeyRepository.list_keys`."""
        params = {"t": tenant_id, "p": PURPOSE_AT_REST}
        rows = await raw_sql_rows(self._dal, _SQL_LIST_KEYS, params)
        return [_key_record(row) for row in rows.as_list()]

    async def insert_active(
        self, tenant_id: int, wrapped_dek: bytes, kek_kind: str, kek_ref: str
    ) -> DekRecord:
        """See :meth:`EnvelopeKeyRepository.insert_active`."""
        try:
            rows = await raw_sql_write(
                self._dal,
                _SQL_INSERT_NEXT_VERSION,
                _insert_params(tenant_id, wrapped_dek, kek_kind, kek_ref),
            )
        except IntegrityError as exc:
            raise KeyConflictError("tenant key was created concurrently") from exc
        row = rows.first()
        if row is None:
            raise EnvelopeError("key insert returned no row")
        return _key_record(row.as_dict())

    async def rotate_active(
        self,
        tenant_id: int,
        *,
        expected_version: int | None,
        wrapped_dek: bytes,
        kek_kind: str,
        kek_ref: str,
    ) -> DekRecord:
        """See :meth:`EnvelopeKeyRepository.rotate_active` (one transaction: retire then insert)."""
        try:
            async with self._dal.engine.begin() as conn:
                retired = await conn.execute(
                    text(_SQL_RETIRE_ACTIVE), {"t": tenant_id, "p": PURPOSE_AT_REST}
                )
                retired_versions = [int(r[0]) for r in retired.all()]
                if expected_version is None:
                    if retired_versions:
                        raise KeyConflictError("an active key already exists")
                elif retired_versions != [expected_version]:
                    raise KeyConflictError("the active key changed concurrently")
                inserted = await conn.execute(
                    text(_SQL_INSERT_NEXT_VERSION),
                    _insert_params(tenant_id, wrapped_dek, kek_kind, kek_ref),
                )
                mapping = inserted.mappings().first()
        except IntegrityError as exc:
            raise KeyConflictError("tenant key was rotated concurrently") from exc
        if mapping is None:
            raise EnvelopeError("key rotation returned no row")
        return _key_record(dict(mapping))

    async def replace_wrapped(
        self, record: DekRecord, *, new_wrapped: bytes, kek_kind: str, kek_ref: str
    ) -> bool:
        """See :meth:`EnvelopeKeyRepository.replace_wrapped` (CAS on the old wrapped bytes)."""
        rows = await raw_sql_write(
            self._dal,
            _SQL_REPLACE_WRAPPED,
            {
                "new": new_wrapped,
                "kind": kek_kind,
                "ref": kek_ref,
                "id": record.id,
                "t": record.tenant_id,
                "old": record.wrapped_dek,
            },
        )
        return rows.first() is not None

    async def add_usage(self, tenant_id: int, dek_version: int, count: int) -> int:
        """See :meth:`EnvelopeKeyRepository.add_usage`."""
        rows = await raw_sql_write(
            self._dal,
            _SQL_ADD_USAGE,
            {"n": count, "t": tenant_id, "p": PURPOSE_AT_REST, "v": dek_version},
        )
        row = rows.first()
        return int(row["usage_count"]) if row is not None else 0

    async def get(self, tenant_id: int) -> TenantKmsConfig | None:
        """See :meth:`KmsConfigRepository.get`."""
        rows = await raw_sql_rows(self._dal, _SQL_CONFIG_GET, {"t": tenant_id})
        row = rows.first()
        return _config(row.as_dict()) if row is not None else None

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
        """See :meth:`KmsConfigRepository.upsert` (ExternalId is never overwritten on update)."""
        rows = await raw_sql_write(
            self._dal,
            _SQL_CONFIG_UPSERT,
            {
                "t": tenant_id,
                "provider": provider,
                "key_ref": key_ref,
                "region": region,
                "principal": principal,
                "ext": new_external_id,
                "status": CONFIG_PENDING,
            },
        )
        row = rows.first()
        if row is None:
            raise EnvelopeError("config upsert returned no row")
        return _config(row.as_dict())

    async def set_status(
        self,
        tenant_id: int,
        status: str,
        *,
        error_code: str | None = None,
        verified: bool = False,
    ) -> None:
        """See :meth:`KmsConfigRepository.set_status`."""
        await raw_sql_write(
            self._dal,
            _SQL_CONFIG_SET_STATUS,
            {"status": status, "err": error_code, "verified": verified, "t": tenant_id},
        )

    async def delete(self, tenant_id: int) -> None:
        """See :meth:`KmsConfigRepository.delete`."""
        await raw_sql_write(self._dal, _SQL_CONFIG_DELETE, {"t": tenant_id})
