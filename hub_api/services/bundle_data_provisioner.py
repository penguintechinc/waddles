"""Bundle `db` v2 table lifecycle: provision on approval, migrate on upgrade, drop on uninstall.

Glue between the global install tier (:mod:`bundle_approval_service`) and the DDL
library (:mod:`bundle_data_v2`). The DDL library takes a psycopg2 cursor and is meant
to run as ``waddles_bundle_migrator`` (DDL owner of ``app_core``/``app_community`` and
writer of ``bundle_data.table_registry``), a different role from hub-api's own
``DATABASE_URL`` account, so this module owns a dedicated synchronous migrator
connection and runs the DDL in a worker thread.

Failure policy is **fail closed**: a bundle that declares ``data.schema`` is only
approved when its tables were provisioned. Every DDL error (ownership refusal,
destructive migration, bad declaration, connection or SQL failure) surfaces as an
:class:`ApiError` and aborts the install/uninstall; nothing is skipped silently.

Atomicity: the DDL runs in one migrator transaction that is committed only after the
hub-api install-state write has committed, and rolled back if either step fails. The
one residual window (the migrator commit failing *after* the state write committed) is
reported loudly as ``bundle_data_commit_failed`` rather than ignored.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import psycopg2
from sqlalchemy.engine import make_url

from services.bundle_data_ddl import MigrationError
from services.bundle_data_schema import TableDeclarationError
from services.bundle_data_v2 import (
    SchemaDeclV2,
    TableOwnershipError,
    migrate_schema,
    provision_schema,
    teardown_schema,
    validate_schema_v2,
)
from services.errors import ApiError
from services.vendor_bundle_authz import CORE_NAMESPACE_PREFIX

logger = logging.getLogger(__name__)

MIGRATOR_USER_ENV = "BUNDLE_MIGRATOR_USER"
MIGRATOR_PASSWORD_ENV = "BUNDLE_MIGRATOR_PASSWORD"  # noqa: S105 -- env var NAME, not a secret
DEFAULT_MIGRATOR_USER = "waddles_bundle_migrator"

#: Per-statement ceiling for bundle DDL (ms), so a lock wait cannot wedge approval.
_STATEMENT_TIMEOUT_MS = 30_000

ConnectionFactory = Callable[[], Any]


def provider_for(app_id: str) -> str:
    """Resolve the table provider (``builtin``/``thirdparty``) from the platform namespace.

    Derived from the app id's reserved namespace, never from the manifest's own claim
    (a vendor cannot name itself ``builtin``).
    """
    return "builtin" if app_id.startswith(CORE_NAMESPACE_PREFIX) else "thirdparty"


def schema_from_manifest(manifest_json: Mapping[str, Any]) -> SchemaDeclV2 | None:
    """Parse and validate a stored manifest's ``data.schema``; ``None`` when it declares none.

    A manifest without ``data.schema`` has nothing to provision (a ``data.tables``-only
    declaration carries no column detail). A declared but invalid schema raises
    :class:`ApiError` 422 so approval of a malformed declaration fails closed.
    """
    data = manifest_json.get("data") or {}
    raw_schema = data.get("schema")
    if raw_schema is None:
        return None
    app_id = str(manifest_json.get("app_id", ""))
    try:
        return validate_schema_v2(
            raw_schema,
            provider=provider_for(app_id),
            declared_names=data.get("tables") or [],
        )
    except TableDeclarationError as exc:
        raise ApiError(f"{app_id}: invalid data.schema: {exc}", 422, "invalid_data_schema") from exc


def connection_factory_from_env() -> ConnectionFactory:
    """Build a migrator connection factory from ``DATABASE_URL`` + ``BUNDLE_MIGRATOR_PASSWORD``.

    Host, port, database and query options (e.g. ``sslmode``) come from hub-api's own
    ``DATABASE_URL``; only the role and password differ. Raises :class:`ApiError` 503 when
    the password is unset, so an unprovisioned deployment fails the approval loudly
    instead of approving a bundle whose tables can never exist.
    """
    password = os.environ.get(MIGRATOR_PASSWORD_ENV, "")
    if not password:
        raise ApiError(
            f"{MIGRATOR_PASSWORD_ENV} is not set; cannot provision bundle data tables",
            503,
            "bundle_data_provisioning_unavailable",
        )
    url_text = os.environ.get("DATABASE_URL", "")
    if not url_text:
        raise ApiError(
            "DATABASE_URL is not set; cannot provision bundle data tables",
            503,
            "bundle_data_provisioning_unavailable",
        )
    url = make_url(url_text)
    user = os.environ.get(MIGRATOR_USER_ENV) or DEFAULT_MIGRATOR_USER

    def _connect() -> Any:
        return psycopg2.connect(
            host=url.host,
            port=url.port or 5432,
            dbname=url.database,
            user=user,
            password=password,
            options=f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}",
            **{k: v for k, v in url.query.items() if isinstance(v, str) and k == "sslmode"},
        )

    return _connect


class MigratorTransaction:
    """One uncommitted migrator transaction; DDL calls run in a worker thread."""

    def __init__(self, conn: Any) -> None:
        """Wrap an open (non-autocommit) migrator connection."""
        self._conn = conn

    async def _run(self, label: str, app_id: str, fn: Callable[[Any], None]) -> None:
        def _work() -> None:
            with self._conn.cursor() as cur:
                fn(cur)

        try:
            await asyncio.to_thread(_work)
        except TableOwnershipError as exc:
            raise ApiError(
                f"{app_id}: {label} refused: {exc}", 409, "bundle_data_ownership_conflict"
            ) from exc
        except MigrationError as exc:
            raise ApiError(
                f"{app_id}: {label} refused (non-additive change): {exc}",
                409,
                "bundle_data_migration_refused",
            ) from exc
        except psycopg2.Error as exc:
            logger.error(
                "bundle data %s failed",
                label,
                extra={"app_id": app_id, "pg_code": getattr(exc, "pgcode", None)},
            )
            raise ApiError(
                f"{app_id}: bundle data {label} failed", 500, "bundle_data_provisioning_failed"
            ) from exc

    async def provision(self, app_id: str, schema: SchemaDeclV2) -> None:
        """Create every declared table and its registry rows (first install)."""
        provider = provider_for(app_id)
        await self._run(
            "provisioning",
            app_id,
            lambda cur: provision_schema(cur, app_id, provider, schema),
        )

    async def migrate(self, app_id: str, old: SchemaDeclV2, new: SchemaDeclV2) -> None:
        """Apply an additive upgrade; destructive changes raise (409)."""
        provider = provider_for(app_id)
        await self._run(
            "migration",
            app_id,
            lambda cur: migrate_schema(cur, app_id, provider, old, new),
        )

    async def teardown(self, app_id: str, schema: SchemaDeclV2) -> None:
        """Drop this app's tables, registry and usage rows (uninstall)."""
        provider = provider_for(app_id)
        await self._run(
            "teardown",
            app_id,
            lambda cur: teardown_schema(cur, app_id, provider, schema),
        )

    async def commit(self, app_id: str) -> None:
        """Commit the migrator transaction; a failure is loud (see module doc)."""
        try:
            await asyncio.to_thread(self._conn.commit)
        except psycopg2.Error as exc:
            logger.error("bundle data migrator commit failed", extra={"app_id": app_id})
            raise ApiError(
                f"{app_id}: bundle data commit failed after the install state was written",
                500,
                "bundle_data_commit_failed",
            ) from exc

    async def rollback(self) -> None:
        """Roll back and close; never raises (the original error is what matters)."""
        try:
            await asyncio.to_thread(self._conn.rollback)
        except psycopg2.Error:
            logger.exception("bundle data migrator rollback failed")

    async def close(self) -> None:
        """Close the migrator connection."""
        try:
            await asyncio.to_thread(self._conn.close)
        except psycopg2.Error:
            logger.exception("bundle data migrator close failed")


class BundleDataProvisioner:
    """Opens migrator transactions; inject a custom factory in tests."""

    def __init__(self, connect: ConnectionFactory | None = None) -> None:
        """`connect` overrides the env-derived migrator connection factory (tests)."""
        self._connect = connect

    @asynccontextmanager
    async def transaction(self, app_id: str) -> AsyncIterator[MigratorTransaction]:
        """Yield an open migrator transaction: commit on clean exit, roll back on any error."""
        connect = self._connect or connection_factory_from_env()
        try:
            conn = await asyncio.to_thread(connect)
        except psycopg2.Error as exc:
            logger.error("bundle data migrator connection failed", extra={"app_id": app_id})
            raise ApiError(
                f"{app_id}: cannot connect as the bundle migrator role",
                503,
                "bundle_data_provisioning_unavailable",
            ) from exc
        tx = MigratorTransaction(conn)
        try:
            yield tx
            await tx.commit(app_id)
        except BaseException:
            await tx.rollback()
            raise
        finally:
            await tx.close()


__all__ = [
    "BundleDataProvisioner",
    "MigratorTransaction",
    "connection_factory_from_env",
    "provider_for",
    "schema_from_manifest",
]
