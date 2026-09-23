"""Same names, same contract as the real ``flask_core.bundle_runtime``.

Pure stdlib (``contextvars`` + ``dataclasses``), adapted near-verbatim from
``spikes/penguin-dal-wasm/waddle_sdk/flask_core/bundle_runtime.py`` (branch
``spike/penguin-dal-wasm``, commit ``f45e6578``) and cross-checked against
the real, current ``libs/flask_core/flask_core/bundle_runtime.py`` on this
branch (post-M1.5) for exact names and signatures. The component entry
(``_component_entry.py``) calls ``set_bundle_dal()`` once at process start and
wraps each envelope's export call in ``bundle_context()``, exactly as
``core/svc_process/app.py``'s ``before_serving`` hook and ``runner.py``'s
``_transform_and_enqueue`` do today for the real stage runner.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from waddle_sdk.db import AsyncDB, Rows


class BundleRuntimeError(RuntimeError):
    """Raised when ``get_bundle_dal()``/``get_bundle_context()`` is called unbound."""


_dal: AsyncDB | None = None


def set_bundle_dal(dal: AsyncDB) -> None:
    """Bind the process-wide DAL facade every ``get_bundle_dal()`` call returns."""
    global _dal
    _dal = dal


def get_bundle_dal() -> AsyncDB:
    """Return the DAL facade bound by :func:`set_bundle_dal`.

    Returns:
        The ``waddle_sdk.db.AsyncDB`` instance bound at component-entry startup.

    Raises:
        BundleRuntimeError: No ``set_bundle_dal()`` call has ever bound one.
    """
    if _dal is None:
        raise BundleRuntimeError("no DAL bound -- call set_bundle_dal() first")
    return _dal


def reset_bundle_dal_for_tests() -> None:
    """Clear the bound DAL. Test-only."""
    global _dal
    _dal = None


@dataclass(slots=True, frozen=True)
class BundleContext:
    """The tenant/community/app_id scope of the envelope currently being processed."""

    tenant: str
    community: str | None
    app_id: str


_context: contextvars.ContextVar[BundleContext | None] = contextvars.ContextVar(
    "waddles_bundle_context", default=None
)


def get_bundle_context() -> BundleContext:
    """Return the ``BundleContext`` bound for the envelope currently being processed.

    Raises:
        BundleRuntimeError: Called outside any ``bundle_context()`` block.
    """
    ctx = _context.get()
    if ctx is None:
        raise BundleRuntimeError("no bundle context bound -- enter bundle_context() first")
    return ctx


@contextmanager
def bundle_context(*, tenant: str, community: str | None, app_id: str) -> Iterator[BundleContext]:
    """Scope tenant/community/app_id for one bundle entrypoint invocation."""
    ctx = BundleContext(tenant=tenant, community=community, app_id=app_id)
    token = _context.set(ctx)
    try:
        yield ctx
    finally:
        _context.reset(token)


async def raw_sql_rows(dal: AsyncDB, sql: str, params: Mapping[str, Any] | None = None) -> Rows:
    """Not implemented.

    The real ``flask_core.bundle_runtime.raw_sql_rows`` runs read-only raw SQL
    (named ``:param`` placeholders) against ``dal.engine.connect()`` -- a live
    SQLAlchemy async engine connection -- for the joins/``GROUP BY``/``RANDOM()``
    cases the single-table query builder cannot express. There is no live
    engine inside the sandbox (see ``AsyncDB.engine``'s own docstring), so
    this construct cannot be lowered and raises explicitly (D21) instead of
    silently returning empty or wrong results. A bundle needing this today
    (e.g. a permission-check join) needs its own migration to a
    single-table-expressible query, or a manifest-declared ``db`` capability
    the stage implements as a first-class join -- both out of scope for this
    SDK's facade.
    """
    raise NotImplementedError(
        "raw_sql_rows() is not implemented in the waddle-sdk facade -- AsyncDB.engine has "
        "no live SQLAlchemy connection inside the sandbox; the WIT db import only exposes "
        "single-statement execute(), not an ad-hoc connection for named-parameter joins"
    )


async def raw_sql_write(dal: AsyncDB, sql: str, params: Mapping[str, Any] | None = None) -> Rows:
    """Not implemented -- see :func:`raw_sql_rows`'s docstring; same construct, write path."""
    raise NotImplementedError(
        "raw_sql_write() is not implemented in the waddle-sdk facade -- AsyncDB.engine has "
        "no live SQLAlchemy connection inside the sandbox; the WIT db import only exposes "
        "single-statement execute(), not an ad-hoc connection for named-parameter writes"
    )
