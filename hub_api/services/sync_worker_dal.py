"""Raw-pydal DAL lifecycle for the standalone sync CronJob entrypoints.

The role-sync / event-sync / Odoo-sync reconcile engines query the RAW pydal
`DAL` (`dal(query).select()`, `dal.tables`, `dal.define_table(...)`) -- the
same object hub-api itself gets from `flask_core.init_database(...).dal`
(`app.py::startup`). A standalone CronJob `main()` has no `app.config`, so it
opens the identical wrapper here.

This replaces a bug where each `main()` built a penguin-dal 0.4.0 `AsyncDB`
(`bundle_install_dal.build_install_dal`) and read `.dal` off it. `AsyncDB` has
no `.dal`: its `__getattr__` treats the name as a TABLE and raises
`TableNotFoundError: Table 'dal' not found`, so every CronJob run crashed
before reconciling a single row. The wrapper yielded here is the flask_core
`AsyncDAL`, which has BOTH the raw `.dal` the engines need and the
`select_async` surface `community_connections.get_decrypted_tokens` needs.

The standalone process has no startup hook that pre-binds tables, so each
engine binds the tables it reads itself (idempotently); `migrate=False`
always -- schema is owned by Alembic, never this process.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from flask_core import init_database, log_db_error
from flask_core.database import AsyncDAL
from pydal import Field

logger = logging.getLogger(__name__)


@contextmanager
def open_worker_dal(database_url: str) -> Iterator[AsyncDAL]:
    """Open this CronJob process's own pydal-backed `AsyncDAL`; always release it on exit.

    Same constructor `hub_api/app.py::startup` uses (`init_database`), with a
    single-connection pool and `migrate=False` (this process never runs DDL).
    A failure inside the block rolls the engines' raw connection back before
    it is closed -- pydal's `close()` COMMITS by default, which would otherwise
    persist a half-applied reconcile pass. The close runs on the constructing
    (main) thread, the one the sync engines queried on; `AsyncDAL.close_async()`
    is deliberately not used because it closes from the executor thread, which
    does not own that connection.
    """
    async_dal = init_database(database_url, pool_size=1, migrate=False)
    dal = async_dal.dal
    try:
        yield async_dal
    except Exception:
        try:
            dal.rollback()
        except Exception as rollback_exc:  # noqa: BLE001 -- must not mask the original error
            log_db_error(logger, "sync worker DAL rollback failed after error", rollback_exc)
        raise
    finally:
        try:
            dal.close()
        except Exception as close_exc:  # noqa: BLE001 -- teardown must not mask the result
            log_db_error(logger, "sync worker DAL close failed", close_exc)
        finally:
            async_dal.executor.shutdown(wait=True)


def bind_reference_tenants(dal: Any) -> None:
    """Idempotently ensure `dal.tenants` exists, same field set as `app.py::_bind_reference_tables`.

    `services.schema.bind_auth_tables()` binds `communities`/`community_members`
    but not `tenants`; a standalone worker DAL has no startup hook to bind it.
    `migrate=False`: the table's schema is owned by Alembic.
    """
    if "tenants" in dal.tables:
        return
    dal.define_table(
        "tenants",
        Field("slug", "string", length=100),
        Field("display_name", "string", length=255),
        Field("logo_url", "text"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
        migrate=False,
    )
