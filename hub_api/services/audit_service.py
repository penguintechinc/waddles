"""Tamper-evident audit service: append, read, verify (GRC audit finding #3).

Persistence layer over :mod:`services.audit_chain`'s pure hash chain, on the same
penguin-dal ``AsyncDB`` (``app.config["install_dal"]``) the bundle control plane uses.

Behavioural contract
--------------------
* **Append-only, linear, race-safe.** One chain per tenant (``tenant:<id>``) plus one
  ``platform`` chain. ``(chain_id, seq)`` is the primary key, so two writers that read the
  same head can never both commit ``seq = head + 1``; the loser re-reads and retries. On
  Postgres a transaction-scoped advisory lock serialises writers per chain so contention
  costs a wait, not a retry storm.
* **Entitlement-gated.** Recording is an Enterprise capability (feature contract
  ``compliance.audit_logs``). An un-entitled tenant's events are *skipped by policy*
  (counted, logged at DEBUG) -- that is the gate working, not a silent failure. The legacy
  ``audit_log`` basic trail is unaffected and stays on in every tier.
* **Fail loud, never swallow.** Any write failure logs at ERROR with the exception type, the
  value-free driver summary and the sanitised traceback, increments
  ``waddles.audit.write_failures``, and raises :class:`AuditWriteError` to the caller. There is
  deliberately no ``except: pass`` anywhere in this module; callers decide the HTTP outcome and
  must not swallow it.
* **PII-free.** Only validated :class:`~services.audit_events.AuditEvent` objects are accepted
  (UUID actor, identifier-shaped details); log lines carry chain ids, categories and counts,
  never user input. Driver messages (which can embed bound values) are never logged -- only
  :func:`flask_core.db_errors.describe_db_error`'s value-free form.
* **Telemetry.** Spans around append/verify; counters ``waddles.audit.events`` and
  ``waddles.audit.write_failures``; histograms ``waddles.audit.append_duration_ms`` and
  ``waddles.audit.append_attempts``. Destination is the standard OTLP env configuration; with no
  provider the API no-ops. A dead exporter never fails a request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from flask_core.db_errors import describe_db_error, format_sanitized_traceback
from flask_core.feature_flags import feature_enabled
from opentelemetry import metrics, trace
from penguin_dal import AsyncDB
from sqlalchemy import Table, func, insert, select, text
from sqlalchemy import types as sqltypes
from sqlalchemy.exc import IntegrityError

from services.audit_chain import (
    GENESIS_HASH,
    HASH_VERSION,
    ChainRecord,
    ChainStatus,
    ChainVerification,
    ChainVerifier,
    canonical_timestamp,
    seal,
)
from services.audit_events import (
    PLATFORM_CHAIN_ID,
    ActorKind,
    AuditEvent,
)

logger = logging.getLogger(__name__)

#: Feature contract flags (libs/core_platform_module/features.py) -- both Enterprise.
FEATURE_AUDIT_LOGS: Final[str] = "waddles.compliance.audit_logs"
FEATURE_AUDIT_EXPORT: Final[str] = "waddles.compliance.audit_export"

#: Tenant slug the platform chain is entitlement-checked against (the default tenant: on-prem
#: it carries the deployment baseline tier, on SaaS it is Free so platform events are skipped).
DEFAULT_GATE_TENANT: Final[str] = "global"

TABLE_NAME: Final[str] = "audit_events"
MAX_APPEND_ATTEMPTS: Final[int] = 10
VERIFY_PAGE_SIZE: Final[int] = 500
#: Upper bound on records verified per call; a longer chain is verified in resumable slices.
DEFAULT_VERIFY_MAX_RECORDS: Final[int] = 50_000
MAX_LIST_LIMIT: Final[int] = 200
MAX_EXPORT_LIMIT: Final[int] = 1000
_ACTOR_CACHE_MAX: Final[int] = 10_000
_SLUG_CACHE_MAX: Final[int] = 4_096

EntitlementGate = Callable[[str], Awaitable[bool]]

_meter = metrics.get_meter("waddles.hub_api.audit")
_tracer = trace.get_tracer("waddles.hub_api.audit")
_EVENTS = _meter.create_counter(
    "waddles.audit.events", unit="1", description="Audit events by result (recorded/skipped)."
)
_WRITE_FAILURES = _meter.create_counter(
    "waddles.audit.write_failures", unit="1", description="Audit writes that failed (loud)."
)
_CHAIN_BREAKS = _meter.create_counter(
    "waddles.audit.chain_breaks", unit="1", description="Verifications that found tampering."
)
_VERIFICATIONS = _meter.create_counter(
    "waddles.audit.verifications", unit="1", description="Chain verifications by status."
)
_APPEND_MS = _meter.create_histogram(
    "waddles.audit.append_duration_ms", unit="ms", description="End-to-end audit append latency."
)
_APPEND_ATTEMPTS = _meter.create_histogram(
    "waddles.audit.append_attempts", unit="1", description="Optimistic-append attempts needed."
)
_VERIFY_RECORDS = _meter.create_histogram(
    "waddles.audit.verify_records", unit="1", description="Records examined per verification."
)


class AuditError(Exception):
    """Base class for audit-subsystem failures (messages are application-authored, PII-free)."""


class AuditWriteError(AuditError):
    """An audit event could not be persisted. Never swallow this -- the event is lost if you do."""


class _AppendExhaustedError(AuditError):
    """The optimistic append lost the race ``MAX_APPEND_ATTEMPTS`` times in a row."""

    def __init__(self, attempts: int) -> None:
        """Remember how many attempts were spent."""
        super().__init__(f"could not append after {attempts} attempts (sustained contention)")
        self.attempts = attempts


@dataclass(slots=True, frozen=True)
class AuditHead:
    """The newest record of a chain -- the anchor an operator pins externally."""

    chain_id: str
    seq: int
    record_hash: str
    occurred_at: str


@dataclass(slots=True, frozen=True)
class VerificationReport:
    """A verification result plus how far it got (long chains verify in resumable slices)."""

    chain_id: str
    verification: ChainVerification
    complete: bool
    next_seq: int | None
    anchor_hash: str | None
    verified_at: str


@dataclass(slots=True, frozen=True)
class ListFilters:
    """Filters for :meth:`AuditService.list_events`; every field optional, ANDed together."""

    category: str | None = None
    action: str | None = None
    outcome: str | None = None
    actor_uuid: uuid.UUID | None = None
    since: datetime | None = None
    until: datetime | None = None


async def default_gate(tenant_slug: str) -> bool:
    """Two-gate entitlement check for the audit-log feature (PostHog flag AND Enterprise tier)."""
    return bool(await feature_enabled(FEATURE_AUDIT_LOGS, tenant=tenant_slug))


def _db_uuid(column: Any, value: str | None) -> Any:
    """Adapt a canonical UUID string to the column's bind type (native UUID vs text)."""
    if value is None:
        return None
    if isinstance(column.type, sqltypes.Uuid):
        return uuid.UUID(value)
    return value


def _db_datetime(column: Any, value: datetime) -> datetime:
    """Adapt an aware UTC datetime to the column (``timestamptz`` aware, else naive UTC)."""
    if getattr(column.type, "timezone", False):
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _as_aware_utc(value: datetime) -> datetime:
    """Normalise a driver-returned datetime (naive -> assume UTC) to aware UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _describe(exc: BaseException) -> str:
    """Loggable cause: authored text for our own errors, the value-free summary for the rest."""
    if isinstance(exc, AuditError):
        return f"{type(exc).__name__}: {exc}"
    return str(describe_db_error(exc))


def report_write_failure(
    *, category: str, action: str, chain_id: str | None, attempts: int, exc: BaseException
) -> AuditWriteError:
    """Make an audit-write failure loud and return the :class:`AuditWriteError` to raise.

    Logs at ERROR with the exception type, a value-free cause (``describe_db_error`` -- driver
    messages can embed bound values, so they are never logged) and the sanitised traceback, and
    increments ``waddles.audit.write_failures``. This is the single place both the chain and the
    legacy ``audit_log`` writer go through, so no audit failure can be swallowed in either.
    """
    cause = _describe(exc)
    frames = format_sanitized_traceback(exc)
    logger.error(
        "audit write FAILED -- event was NOT recorded: category=%s action=%s chain=%s "
        "attempts=%d cause=%s\n%s",
        category,
        action,
        chain_id or "unresolved",
        attempts,
        cause,
        frames,
    )
    _WRITE_FAILURES.add(1, {"category": category})
    return AuditWriteError(f"audit write failed ({cause})")


class AuditService:
    """Append/read/verify the tamper-evident audit log on one ``AsyncDB``."""

    def __init__(self, dal: AsyncDB, *, gate: EntitlementGate | None = None) -> None:
        """Bind to ``dal``; ``gate`` decides per tenant slug whether recording is entitled."""
        self._dal = dal
        self._gate: EntitlementGate = gate or default_gate
        self._actor_cache: dict[int, uuid.UUID] = {}
        self._slug_by_id: dict[int, str] = {}
        self._id_by_slug: dict[str, int] = {}
        self._warned_unresolved_actor = False

    # ------------------------------------------------------------------ table / lookups

    def _table(self) -> Table:
        """The reflected ``audit_events`` table; a missing table is a loud, actionable error."""
        table = self._dal.metadata.tables.get(TABLE_NAME)
        if table is None:
            raise AuditWriteError(
                f"table {TABLE_NAME!r} is not present -- run the alembic migration "
                "0048_audit_events_hash_chain before enabling the audit feature"
            )
        return table

    async def resolve_actor_uuid(self, user_id: int) -> uuid.UUID | None:
        """Map the legacy ``hub_users.id`` surrogate to its ``hub_users.uuid`` (cached)."""
        cached = self._actor_cache.get(user_id)
        if cached is not None:
            return cached
        users = self._dal.metadata.tables.get("hub_users")
        if users is None or "uuid" not in users.c:
            return None
        async with self._dal.engine.connect() as conn:
            row = (await conn.execute(select(users.c.uuid).where(users.c.id == user_id))).first()
        if row is None or row[0] is None:
            return None
        resolved = row[0] if isinstance(row[0], uuid.UUID) else uuid.UUID(str(row[0]))
        if len(self._actor_cache) >= _ACTOR_CACHE_MAX:
            self._actor_cache.clear()
        self._actor_cache[user_id] = resolved
        return resolved

    async def find_tenant(
        self, *, tenant_id: int | None, tenant_slug: str | None
    ) -> tuple[int, str] | None:
        """Resolve a tenant to ``(id, slug)`` from either half (cached); ``None`` if unknown.

        Use this for identifiers that came from outside (a login form's tenant slug): the
        entitlement gate and chain choice must only ever see a tenant that really exists.
        """
        if tenant_id is not None and tenant_id in self._slug_by_id:
            return tenant_id, self._slug_by_id[tenant_id]
        if tenant_slug is not None and tenant_slug in self._id_by_slug:
            return self._id_by_slug[tenant_slug], tenant_slug
        tenants = self._dal.metadata.tables.get("tenants")
        if tenants is None:
            raise AuditWriteError("table 'tenants' is not present; cannot resolve the audit chain")
        stmt = select(tenants.c.id, tenants.c.slug)
        stmt = (
            stmt.where(tenants.c.id == tenant_id)
            if tenant_id is not None
            else stmt.where(tenants.c.slug == tenant_slug)
        )
        async with self._dal.engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        if row is None:
            return None
        resolved_id, resolved_slug = int(row[0]), str(row[1])
        if len(self._slug_by_id) >= _SLUG_CACHE_MAX:
            self._slug_by_id.clear()
            self._id_by_slug.clear()
        self._slug_by_id[resolved_id] = resolved_slug
        self._id_by_slug[resolved_slug] = resolved_id
        return resolved_id, resolved_slug

    async def _tenant_lookup(
        self, *, tenant_id: int | None, tenant_slug: str | None
    ) -> tuple[int, str]:
        """Like :meth:`find_tenant` but an unknown tenant is a loud write error."""
        found = await self.find_tenant(tenant_id=tenant_id, tenant_slug=tenant_slug)
        if found is None:
            raise AuditWriteError("audit event names a tenant that does not exist")
        return found

    async def _gate_slug(self, event: AuditEvent) -> str:
        """The tenant slug the entitlement gate is evaluated against (no chain lookup needed)."""
        if event.tenant_slug is not None:
            return event.tenant_slug
        if event.tenant_id is not None:
            _, slug = await self._tenant_lookup(tenant_id=event.tenant_id, tenant_slug=None)
            return slug
        return DEFAULT_GATE_TENANT

    async def _chain_id(self, event: AuditEvent, slug: str) -> str:
        """Chain ``event`` belongs to (``tenant:<id>`` or ``platform``); resolved after the gate."""
        if event.tenant_id is None and event.tenant_slug is None:
            return PLATFORM_CHAIN_ID
        tenant_id, _ = await self._tenant_lookup(
            tenant_id=event.tenant_id, tenant_slug=slug if event.tenant_id is None else None
        )
        return f"tenant:{tenant_id}"

    async def _resolve_actor(self, event: AuditEvent) -> tuple[str | None, ActorKind]:
        """Return the canonical actor UUID string and kind for ``event``."""
        if event.actor_uuid is not None:
            return str(event.actor_uuid), ActorKind.USER
        if event.actor_user_id is None:
            return None, event.actor_kind
        resolved = await self.resolve_actor_uuid(event.actor_user_id)
        if resolved is None:
            if not self._warned_unresolved_actor:
                self._warned_unresolved_actor = True
                logger.warning(
                    "audit actor could not be resolved to a hub_users.uuid; recording the "
                    "event with actor_kind=unresolved (hub_users.uuid column or row missing)"
                )
            return None, ActorKind.UNRESOLVED
        return str(resolved), ActorKind.USER

    # ------------------------------------------------------------------ append

    def _fail(
        self, event: AuditEvent, chain_id: str | None, attempts: int, exc: BaseException
    ) -> AuditWriteError:
        """Log + count the failure loudly and return the error to raise (see module function)."""
        return report_write_failure(
            category=event.category.value,
            action=event.action,
            chain_id=chain_id,
            attempts=attempts,
            exc=exc,
        )

    async def record(self, event: AuditEvent) -> ChainRecord | None:
        """Append ``event`` to its chain; ``None`` means the tenant is not entitled (skipped).

        Raises:
            AuditWriteError: the event could not be persisted (already logged at ERROR and
                counted). Callers must let it propagate -- swallowing it silently drops a
                security event, which is exactly the defect this subsystem exists to remove.
        """
        started = time.perf_counter()
        with _tracer.start_as_current_span("audit.append") as span:
            span.set_attribute("audit.category", event.category.value)
            span.set_attribute("audit.action", event.action)
            chain_id: str | None = None
            try:
                gate_slug = await self._gate_slug(event)
                if not await self._gate(gate_slug):
                    _EVENTS.add(1, {"result": "skipped", "category": event.category.value})
                    logger.debug(
                        "audit event skipped (not entitled): category=%s action=%s",
                        event.category.value,
                        event.action,
                    )
                    return None
                chain_id = await self._chain_id(event, gate_slug)
                actor_uuid, actor_kind = await self._resolve_actor(event)
                record, attempts = await self._append(chain_id, event, actor_uuid, actor_kind)
            except AuditWriteError as exc:
                span.record_exception(exc)
                raise self._fail(event, chain_id, 0, exc) from exc
            except _AppendExhaustedError as exc:
                span.record_exception(exc)
                raise self._fail(event, chain_id, exc.attempts, exc) from exc
            except Exception as exc:
                span.record_exception(exc)
                raise self._fail(event, chain_id, 0, exc) from exc
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            _EVENTS.add(1, {"result": "recorded", "category": event.category.value})
            _APPEND_MS.record(elapsed_ms, {"category": event.category.value})
            _APPEND_ATTEMPTS.record(attempts)
            span.set_attribute("audit.seq", record.seq)
            logger.debug(
                "audit event recorded: category=%s action=%s chain=%s seq=%d attempts=%d",
                event.category.value,
                event.action,
                record.chain_id,
                record.seq,
                attempts,
            )
            return record

    async def _append(
        self, chain_id: str, event: AuditEvent, actor_uuid: str | None, actor_kind: ActorKind
    ) -> tuple[ChainRecord, int]:
        """Optimistic append with bounded retries; returns the sealed record and attempts used."""
        table = self._table()
        for attempt in range(1, MAX_APPEND_ATTEMPTS + 1):
            attempted_seq = 0
            try:
                async with self._dal.engine.begin() as conn:
                    if conn.dialect.name == "postgresql":
                        await conn.execute(
                            text("SELECT pg_advisory_xact_lock(hashtextextended(:chain, 0))"),
                            {"chain": chain_id},
                        )
                    head = (
                        await conn.execute(
                            select(table.c.seq, table.c.record_hash, table.c.occurred_at)
                            .where(table.c.chain_id == chain_id)
                            .order_by(table.c.seq.desc())
                            .limit(1)
                        )
                    ).first()
                    now = datetime.now(UTC)
                    if head is None:
                        seq, prev_hash = 1, GENESIS_HASH
                    else:
                        seq, prev_hash = int(head[0]) + 1, str(head[1])
                        # Keep the timeline non-decreasing even if replica clocks skew.
                        now = max(now, _as_aware_utc(head[2]))
                    attempted_seq = seq
                    sealed = seal(
                        ChainRecord(
                            chain_id=chain_id,
                            seq=seq,
                            event_id=str(uuid.uuid4()),
                            occurred_at=canonical_timestamp(now),
                            actor_uuid=actor_uuid,
                            actor_kind=actor_kind.value,
                            category=event.category.value,
                            action=str(event.action),
                            outcome=event.outcome.value,
                            target_type=event.target_type,
                            target_id=event.target_id,
                            details=dict(event.details),
                            prev_hash=prev_hash,
                            record_hash="",
                            hash_version=HASH_VERSION,
                        )
                    )
                    await conn.execute(
                        insert(table).values(
                            chain_id=sealed.chain_id,
                            seq=sealed.seq,
                            event_id=_db_uuid(table.c.event_id, sealed.event_id),
                            occurred_at=_db_datetime(table.c.occurred_at, now),
                            actor_uuid=_db_uuid(table.c.actor_uuid, sealed.actor_uuid),
                            actor_kind=sealed.actor_kind,
                            category=sealed.category,
                            action=sealed.action,
                            outcome=sealed.outcome,
                            target_type=sealed.target_type,
                            target_id=sealed.target_id,
                            details=dict(sealed.details),
                            prev_hash=sealed.prev_hash,
                            record_hash=sealed.record_hash,
                            hash_version=sealed.hash_version,
                        )
                    )
                return sealed, attempt
            except IntegrityError as exc:
                # Distinguish "another writer took this seq" (retry) from a genuine
                # constraint violation (fail loudly): re-read the head on a fresh connection.
                current = await self._head_seq(table, chain_id)
                if current is not None and current >= attempted_seq > 0:
                    await asyncio.sleep(random.uniform(0.002, 0.02) * attempt)  # noqa: S311 - retry jitter, not security  # nosec B311
                    continue
                raise AuditWriteError("audit insert violated a database constraint") from exc
        raise _AppendExhaustedError(MAX_APPEND_ATTEMPTS)

    async def _head_seq(self, table: Table, chain_id: str) -> int | None:
        """Current head ``seq`` of ``chain_id`` on a fresh connection (``None`` if empty)."""
        async with self._dal.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(func.max(table.c.seq)).where(table.c.chain_id == chain_id)
                )
            ).first()
        return None if row is None or row[0] is None else int(row[0])

    # ------------------------------------------------------------------ read

    def _to_record(self, row: Any) -> ChainRecord:
        """Rebuild the exact hashed form of a stored row."""
        details = row.details
        if isinstance(details, str | bytes):
            details = json.loads(details)
        return ChainRecord(
            chain_id=row.chain_id,
            seq=int(row.seq),
            event_id=str(row.event_id),
            occurred_at=canonical_timestamp(row.occurred_at),
            actor_uuid=None if row.actor_uuid is None else str(row.actor_uuid),
            actor_kind=row.actor_kind,
            category=row.category,
            action=row.action,
            outcome=row.outcome,
            target_type=row.target_type,
            target_id=row.target_id,
            details=dict(details or {}),
            prev_hash=row.prev_hash,
            record_hash=row.record_hash,
            hash_version=row.hash_version,
        )

    async def head(self, chain_id: str) -> AuditHead | None:
        """Newest record of ``chain_id`` (the externally pinnable anchor), or ``None`` if empty."""
        table = self._table()
        async with self._dal.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(table)
                    .where(table.c.chain_id == chain_id)
                    .order_by(table.c.seq.desc())
                    .limit(1)
                )
            ).first()
        if row is None:
            return None
        record = self._to_record(row)
        return AuditHead(
            chain_id=chain_id,
            seq=record.seq,
            record_hash=record.record_hash,
            occurred_at=record.occurred_at,
        )

    async def read_range(
        self, chain_id: str, *, after_seq: int = 0, limit: int = VERIFY_PAGE_SIZE
    ) -> list[ChainRecord]:
        """Records with ``seq > after_seq`` in ascending order (keyset page; export + verify)."""
        table = self._table()
        async with self._dal.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(table)
                    .where(table.c.chain_id == chain_id, table.c.seq > after_seq)
                    .order_by(table.c.seq.asc())
                    .limit(limit)
                )
            ).all()
        return [self._to_record(row) for row in rows]

    async def list_events(
        self, chain_id: str, *, filters: ListFilters, page: int, limit: int
    ) -> tuple[list[ChainRecord], int]:
        """Newest-first page of ``chain_id`` matching ``filters`` plus the unpaged total."""
        table = self._table()
        limit = min(MAX_LIST_LIMIT, max(1, limit))
        page = max(1, page)
        conditions: list[Any] = [table.c.chain_id == chain_id]
        if filters.category:
            conditions.append(table.c.category == filters.category)
        if filters.action:
            conditions.append(table.c.action == filters.action)
        if filters.outcome:
            conditions.append(table.c.outcome == filters.outcome)
        if filters.actor_uuid is not None:
            conditions.append(
                table.c.actor_uuid == _db_uuid(table.c.actor_uuid, str(filters.actor_uuid))
            )
        if filters.since is not None:
            conditions.append(
                table.c.occurred_at >= _db_datetime(table.c.occurred_at, filters.since)
            )
        if filters.until is not None:
            conditions.append(
                table.c.occurred_at <= _db_datetime(table.c.occurred_at, filters.until)
            )
        async with self._dal.engine.connect() as conn:
            total = int(
                (
                    await conn.execute(select(func.count()).select_from(table).where(*conditions))
                ).scalar_one()
            )
            rows = (
                await conn.execute(
                    select(table)
                    .where(*conditions)
                    .order_by(table.c.seq.desc())
                    .limit(limit)
                    .offset((page - 1) * limit)
                )
            ).all()
        return [self._to_record(row) for row in rows], total

    async def chain_ids(self) -> list[str]:
        """Every chain that has at least one record (for the offline/cron verifier)."""
        table = self._table()
        async with self._dal.engine.connect() as conn:
            rows = (await conn.execute(select(table.c.chain_id).distinct())).all()
        return sorted(str(row[0]) for row in rows)

    # ------------------------------------------------------------------ verify

    async def verify(
        self,
        chain_id: str,
        *,
        from_seq: int = 1,
        anchor_hash: str | None = None,
        expected_head_seq: int | None = None,
        expected_head_hash: str | None = None,
        max_records: int = DEFAULT_VERIFY_MAX_RECORDS,
    ) -> VerificationReport:
        """Recompute and check the chain; pinned-head checks apply only once the end is reached.

        ``from_seq > 1`` resumes a previous slice and requires that slice's ``anchor_hash`` (the
        report's ``anchor_hash``) -- the database is never trusted to supply its own anchor.
        A break is logged at ERROR and counted: tamper detection must page someone.
        """
        if from_seq < 1:
            raise AuditError("from_seq must be >= 1")
        if from_seq > 1 and not anchor_hash:
            raise AuditError("anchor_hash is required to resume verification at from_seq > 1")
        started = time.perf_counter()
        with _tracer.start_as_current_span("audit.verify") as span:
            verifier = ChainVerifier(
                chain_id,
                start_seq=from_seq,
                start_prev_hash=anchor_hash if from_seq > 1 and anchor_hash else GENESIS_HASH,
            )
            after = from_seq - 1
            complete = False
            while verifier.broken is None and verifier.examined < max_records:
                page = await self.read_range(
                    chain_id,
                    after_seq=after,
                    limit=min(VERIFY_PAGE_SIZE, max_records - verifier.examined),
                )
                if not page:
                    complete = True
                    break
                for record in page:
                    if not verifier.feed(record):
                        break
                after = page[-1].seq
            if verifier.broken is None and not complete:
                # Hit max_records: the end may or may not have been reached.
                complete = not await self.read_range(chain_id, after_seq=after, limit=1)
            result = verifier.finish(
                expected_head_seq=expected_head_seq if complete else None,
                expected_head_hash=expected_head_hash if complete else None,
            )
            span.set_attribute("audit.verify.status", result.status.value)
            span.set_attribute("audit.verify.examined", result.examined)
        _VERIFICATIONS.add(1, {"status": result.status.value})
        _VERIFY_RECORDS.record(result.examined)
        if result.status is ChainStatus.BROKEN and result.break_ is not None:
            _CHAIN_BREAKS.add(1, {"reason": result.break_.reason.value})
            logger.error(
                "AUDIT CHAIN TAMPER DETECTED: chain=%s first_bad_seq=%s reason=%s examined=%d",
                chain_id,
                result.break_.seq,
                result.break_.reason.value,
                result.examined,
            )
        logger.debug(
            "audit verification finished: chain=%s status=%s examined=%d complete=%s ms=%.1f",
            chain_id,
            result.status.value,
            result.examined,
            complete,
            (time.perf_counter() - started) * 1000.0,
        )
        incomplete_ok = result.status is ChainStatus.INTACT and not complete
        return VerificationReport(
            chain_id=chain_id,
            verification=result,
            complete=complete,
            next_seq=(result.head_seq + 1) if incomplete_ok and result.head_seq else None,
            anchor_hash=result.head_hash if incomplete_ok else None,
            verified_at=canonical_timestamp(datetime.now(UTC)),
        )


_SERVICES: weakref.WeakKeyDictionary[AsyncDB, AuditService] = weakref.WeakKeyDictionary()


def get_audit_service(dal: AsyncDB, *, gate: EntitlementGate | None = None) -> AuditService:
    """Per-``AsyncDB`` service instance so the actor/tenant caches survive across call sites.

    ``gate`` is honoured only on first creation (tests construct their own ``AuditService``).
    """
    service = _SERVICES.get(dal)
    if service is None:
        service = AuditService(dal, gate=gate)
        _SERVICES[dal] = service
    return service


def reset_audit_services() -> None:
    """Drop every cached service (test isolation; never needed in production)."""
    _SERVICES.clear()


__all__ = [
    "DEFAULT_GATE_TENANT",
    "FEATURE_AUDIT_EXPORT",
    "FEATURE_AUDIT_LOGS",
    "MAX_EXPORT_LIMIT",
    "AuditError",
    "AuditHead",
    "AuditService",
    "AuditWriteError",
    "ListFilters",
    "VerificationReport",
    "default_gate",
    "report_write_failure",
    "get_audit_service",
    "reset_audit_services",
]
