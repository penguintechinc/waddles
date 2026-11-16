"""Explicit primary-vs-read-replica routing for hub-api DB reads.

Owner directive (2026-11-15): pure reads go to the Postgres read replica wherever possible.
Everything that needs freshness or carries a security meaning stays on the primary. The full
per-module classification is `docs/hub-api-db-routing.md`; this module is the small mechanism
that classification is applied with.

Routing is **explicit, never ambient.** A pydal query runs on the DAL its table objects were
built from, so "use the replica" means "build the query from the replica DAL". A handler opts in
by calling `get_read_dals()` / `get_read_dal()` instead of reading `current_app.config["dal"]`,
and by carrying `@reads_from_replica` (a marker the guard tests enumerate). Nothing else in the
process ever reaches the replica. Every call is observable: a DEBUG log line names the DAL that
served it and `hub_api_db_reads_total{target,route}` is incremented.

Targets
-------
* `replica`           -- `DATABASE_READ_REPLICA_URL` is configured; the read went to it.
* `primary_fallback`  -- a replica-eligible read ran on the primary because no replica is
  configured. A single WARN is logged per process the first time; never an error.
* `primary`           -- a read deliberately kept on the primary (auth/session/OAuth state/MFA/
  rate-limit/idempotency, read-after-write, watermark). Recorded via `record_primary_read()`.

Replica-lag tolerance
---------------------
A replica read can be behind the primary by the streaming-replication lag (typically
sub-second, unbounded in a failure). Only reads whose callers tolerate seconds of staleness may
use it: public pages, marketplace/catalog browse, dashboards, stats and append-only audit
listings. A flow that writes and then reads the same data in one request or in the immediately
following client request (admin edits then re-fetches the row) is **read-after-write** and
stays on the primary. Replica reads never carry a transaction that must observe the caller's
own writes.

Security invariants
-------------------
Authentication, session, OAuth state, MFA, passkey, rate-limit and idempotency reads, tenant
resolution and scope checks never use this module. The `bundle_active_set` watermark job
requires the primary (`pg_is_in_recovery()` guard) and refuses a DAL tagged by
`mark_replica()`.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import threading
from collections.abc import Awaitable, Callable, Iterable
from enum import StrEnum
from typing import Any, ParamSpec, TypeVar

from opentelemetry import metrics
from quart import current_app

logger = logging.getLogger("waddles.hub_api.db_routing")

#: Attribute stamped on every DAL object that is (a view of) the read replica.
REPLICA_MARK = "_hub_api_is_read_replica"
#: Attribute holding the names of lazy table binders already applied to a replica DAL.
_BOUND_ATTR = "_hub_api_routing_bound"

#: Marker attribute `@reads_from_replica` sets on a handler (enumerated by the guard tests).
HANDLER_MARK = "__reads_from_replica__"

_meter = metrics.get_meter("waddles.hub_api.db_routing")
_reads_counter = _meter.create_counter(
    "hub_api_db_reads_total",
    unit="1",
    description="hub-api DB reads by serving target (replica | primary_fallback | primary).",
)

_fallback_warned = False
_warn_lock = threading.Lock()
_bind_lock = threading.Lock()

_current_route: contextvars.ContextVar[str] = contextvars.ContextVar(
    "hub_api_db_route", default="unlabelled"
)

P = ParamSpec("P")
R = TypeVar("R")


class ReplicaAsyncDAL:
    """Read-only `AsyncDAL` look-alike whose every helper runs on the replica DAL.

    Services take an `async_dal` and call `select_async` / `count_async` / `executesql_async`;
    handing them the primary wrapper while the queries come from the replica would split one
    request across both databases (raw SQL silently on primary). This view makes the whole call
    chain consistent. It deliberately has NO write helpers (`insert_async`, `update_async`,
    `delete_async`, `transaction_async` ...): a write attempted through a replica route raises
    `AttributeError` instead of reaching a standby.
    """

    def __init__(self, parent: Any) -> None:
        """Wrap `parent` (the real `AsyncDAL`); `dal` and `read_dal` both name the replica."""
        self._parent = parent
        self.dal = parent.read_dal
        self.read_dal = parent.read_dal
        self._executor = getattr(parent, "executor", None)

    async def _run(self, fn: Callable[[], R]) -> R:
        """Run `fn` on the parent's executor inside the caller's contextvars context."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, functools.partial(contextvars.copy_context().run, fn)
        )

    async def select_async(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        """Select on the replica; ends its own transaction (commit, rollback on error)."""

        def _select() -> Any:
            try:
                result = query.select(*args, **kwargs)
                self.dal.commit()
                return result
            except Exception:
                self.dal.rollback()
                raise

        return await self._run(_select)

    async def count_async(self, query: Any) -> int:
        """Count on the replica; ends its own transaction."""

        def _count() -> int:
            try:
                result = int(self.dal(query).count())
                self.dal.commit()
                return result
            except Exception:
                self.dal.rollback()
                raise

        return await self._run(_count)

    async def executesql_async(self, sql: str, params: list[Any] | None = None) -> Any:
        """Run raw (read) SQL on the replica; a standby rejects writes server-side too."""

        def _execute() -> Any:
            try:
                result = self.dal.executesql(sql, placeholders=params)
                self.dal.commit()
                return result
            except Exception:
                self.dal.rollback()
                raise

        return await self._run(_execute)


class DbTarget(StrEnum):
    """Which database served (or would have served) a read."""

    REPLICA = "replica"
    PRIMARY_FALLBACK = "primary_fallback"
    PRIMARY = "primary"


def mark_replica(dal: Any) -> None:
    """Tag `dal` as the read-replica connection so primary-only code can refuse it."""
    setattr(dal, REPLICA_MARK, True)


def is_replica_dal(dal: Any) -> bool:
    """True when `dal` (an `AsyncDAL`, raw pydal DAL or `AsyncDB`) is the replica connection."""
    return bool(getattr(dal, REPLICA_MARK, False))


def reset_fallback_warning() -> None:
    """Test hook: re-arm the one-time fallback WARN."""
    global _fallback_warned
    with _warn_lock:
        _fallback_warned = False


def _record(target: DbTarget, route: str) -> None:
    """Count one read and log (DEBUG) which DAL served it."""
    _reads_counter.add(1, {"target": target.value, "route": route})
    logger.debug("db_routing.read", extra={"target": target.value, "route": route})


def _warn_fallback_once(route: str) -> None:
    """Emit the single per-process WARN that replica-eligible reads are running on primary."""
    global _fallback_warned
    with _warn_lock:
        if _fallback_warned:
            return
        _fallback_warned = True
    logger.warning(
        "db_routing.replica_not_configured: replica-eligible reads are served by the primary "
        "(set DATABASE_READ_REPLICA_URL to enable the read replica); first seen on route=%s",
        route,
    )


def _bind_on(dal: Any, binders: Iterable[Callable[[Any], None]]) -> None:
    """Apply each lazy table binder to `dal` exactly once (tracked per DAL object)."""
    for binder in binders:
        name = f"{binder.__module__}.{binder.__qualname__}"
        with _bind_lock:
            bound: set[str] = getattr(dal, _BOUND_ATTR, None) or set()
            if name in bound:
                continue
            binder(dal)
            bound.add(name)
            setattr(dal, _BOUND_ATTR, bound)


def _route_label(route: str | None) -> str:
    """Resolve the metric/log route label (explicit arg, else the `@reads_from_replica` one)."""
    return route or _current_route.get()


def get_read_dal(route: str | None = None, *, binders: Iterable[Callable[[Any], None]] = ()) -> Any:
    """Return the raw pydal DAL a replica-eligible read must be built from.

    The replica DAL when configured, else the primary raw DAL (WARN once, metric
    `primary_fallback`). `binders` are the lazy `bind_*_tables` functions the handler's tables
    need; they are applied to whichever DAL is returned, once per DAL.
    """
    label = _route_label(route)
    async_dal = current_app.config.get("async_dal")
    replica = getattr(async_dal, "read_dal", None)
    if replica is not None:
        mark_replica(replica)
        _bind_on(replica, binders)
        _record(DbTarget.REPLICA, label)
        return replica
    primary = getattr(async_dal, "dal", None) or current_app.config["dal"]
    _bind_on(primary, binders)
    _warn_fallback_once(label)
    _record(DbTarget.PRIMARY_FALLBACK, label)
    return primary


def get_read_dals(
    route: str | None = None, *, binders: Iterable[Callable[[Any], None]] = ()
) -> tuple[Any, Any]:
    """Drop-in for the `async_dal, dal = _dal()` idiom: `(async_dal, read_dal)`.

    With a replica both members are replica-bound (`ReplicaAsyncDAL` is read-only); without
    one they are the primary pair, exactly the pre-routing behaviour.
    """
    dal = get_read_dal(route, binders=binders)
    async_dal = current_app.config.get("async_dal")
    if async_dal is not None and is_replica_dal(dal):
        return ReplicaAsyncDAL(async_dal), dal
    return async_dal, dal


def get_read_async_dal(
    route: str | None = None, *, binders: Iterable[Callable[[Any], None]] = ()
) -> Any:
    """Return only the `async_dal` half of `get_read_dals()` (replica view or primary wrapper)."""
    return get_read_dals(route, binders=binders)[0]


def record_primary_read(route: str) -> None:
    """Record a read deliberately kept on the primary (auth/session/read-after-write)."""
    _record(DbTarget.PRIMARY, route)


def reads_from_replica(
    route: str,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Mark a handler as a replica-eligible, read-only route and label its DB reads.

    Marker + label only: routing still happens at the explicit `get_read_dal(s)()` call. Apply it
    *below* the `@bp.route` decorator. The guard tests refuse a marked handler that accepts
    anything but GET.
    """

    def decorate(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            token = _current_route.set(route)
            try:
                return await fn(*args, **kwargs)
            finally:
                _current_route.reset(token)

        setattr(wrapper, HANDLER_MARK, route)
        return wrapper

    return decorate
