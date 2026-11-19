"""Authoritative, always-current `data_use_exclusions` (analytics W5).

`public.data_use_exclusions(user_uuid, reason_code, since)` is the suppression list the
`svc_analytics` writer reads before EVERY layer A upsert and the layer B builder reads before
every cell: a subject present here (any reason) never enters an analytics dataset. hub-api is the
ONLY writer. This module keeps it current:

* **Same transaction as the triggering event.** Every writer here is a synchronous pydal function
  that does NOT commit -- the caller's consent / erasure write and the exclusion write share one
  transaction (`apply_consent_change` runs both in ONE executor job and commits once), so there is
  no eventual-consistency window in which a subject has opted out but is still analysed, and a
  rolled-back event leaves no exclusion behind.
* **Reason codes are the documented enum** (`docs/analytics-foundation.md`, alembic 0129): 1
  do_not_sell, 2 gpc, 3 objection, 4 erased, 5 age_not_adult. Nothing here invents a code.
* **ERASED (4) and AGE_NOT_ADULT (5) are never removed by a consent change** -- `remove_exclusion`
  refuses them (`ExclusionError`); erasure is permanent and age needs its own evidence source.
* **PII-free.** Only the uuid token + reason + timestamp are stored, logged, audited and counted
  (metric labels are the reason name only).
* **Self-healing.** `reconcile_exclusions()` rebuilds MISSING rows from the authoritative sources
  (`cookie_consent` do-not-sell state, the `data_deletion_requests` erasure ledger) idempotently,
  in bounded batches under a Postgres advisory lock so only one replica runs a pass. It only ever
  ADDS -- removal happens solely at act time -- so it can never undo a consent re-grant.

Failure policy: nothing here swallows an error. A write that cannot be made raises
`ExclusionError` (or the driver error) after rolling back, so a consent change is never reported
as saved while its exclusion was lost.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Final

from flask_core.feature_flags import feature_enabled
from opentelemetry import metrics

from services import audit_http
from services.audit_events import (
    ActorKind,
    AuditAction,
    AuditCategory,
    AuditEvent,
    AuditOutcome,
)
from services.audit_service import report_write_failure

logger = logging.getLogger(__name__)
_meter = metrics.get_meter("hub_api.analytics_exclusions")
_added = _meter.create_counter(
    "hub_api.analytics_exclusions.added",
    description="Exclusion rows inserted at act time, by reason (no subject labels)",
)
_removed = _meter.create_counter(
    "hub_api.analytics_exclusions.removed",
    description="Exclusion rows removed at act time (consent re-grant), by reason",
)
_reconciled = _meter.create_counter(
    "hub_api.analytics_exclusions.reconciled",
    description="Missing exclusion rows rebuilt by the reconciler, by reason",
)
_examined = _meter.create_counter(
    "hub_api.analytics_exclusions.reconcile_examined",
    description="Authoritative source rows examined by the reconciler, by source",
)
_passes = _meter.create_counter(
    "hub_api.analytics_exclusions.reconcile_passes",
    description="Reconcile passes by result (ok / skipped_locked / error)",
)

#: Feature flag gating the periodic reconciler. Default OFF (flag plane down = OFF).
RECONCILE_FLAG: Final = "waddles.analytics-exclusions-reconcile"
#: Flags for a platform job have no tenant; the reserved operator tenant is the evaluation scope.
_FLAG_TENANT: Final = "system"
#: Fixed forever (how replicas agree on "who reconciles"): ASCII "wadb" high, tag 0x08 low.
_ADVISORY_LOCK_KEY: Final[int] = 0x7761646200000008

DEFAULT_BATCH_SIZE: Final = 500
DEFAULT_INTERVAL_SECONDS: Final = 3600.0
_TABLE: Final = "data_use_exclusions"
_SOURCE_CONSENT: Final = "cookie_consent"
_SOURCE_ERASURE: Final = "data_deletion_requests"


class ExclusionReason(IntEnum):
    """`data_use_exclusions.reason_code` -- the alembic 0129 enum (see analytics-foundation.md)."""

    DO_NOT_SELL = 1
    GPC = 2
    OBJECTION = 3
    ERASED = 4
    AGE_NOT_ADULT = 5


#: Reasons a data subject's own act can lift. ERASED / AGE_NOT_ADULT can never be lifted here.
REMOVABLE_REASONS: Final[frozenset[ExclusionReason]] = frozenset(
    {ExclusionReason.DO_NOT_SELL, ExclusionReason.GPC, ExclusionReason.OBJECTION}
)


class ExclusionError(RuntimeError):
    """An exclusion could not be recorded/cleared safely (bad input, unknown subject, no table)."""


@dataclass(slots=True, frozen=True)
class ConsentPlan:
    """What a consent write requires of the exclusion list (pure; see `plan_consent_change`)."""

    add: frozenset[ExclusionReason] = frozenset()
    remove: frozenset[ExclusionReason] = frozenset()

    @property
    def empty(self) -> bool:
        """True when the consent write touches no exclusion."""
        return not self.add and not self.remove


@dataclass(slots=True)
class ExclusionChange:
    """Rows actually inserted / deleted by one act (an already-present row is not 'added')."""

    user_uuid: str | None = None
    added: list[ExclusionReason] = field(default_factory=list)
    removed: list[ExclusionReason] = field(default_factory=list)


@dataclass(slots=True)
class ReconcileResult:
    """One reconcile pass: denominators (rows examined) and what was rebuilt."""

    consent_examined: int = 0
    ledger_examined: int = 0
    added: dict[ExclusionReason, int] = field(default_factory=dict)
    batches: int = 0
    skipped_locked: bool = False

    @property
    def total_added(self) -> int:
        """Total rows rebuilt across every reason."""
        return sum(self.added.values())


def _token(user_uuid: str | uuid.UUID) -> str:
    """Canonical lower-case uuid string; anything else is refused (it is inlined into SQL)."""
    try:
        return str(uuid.UUID(str(user_uuid)))
    except ValueError as exc:
        raise ExclusionError("user_uuid is not a canonical uuid token") from exc


def _is_sqlite(dal: Any) -> bool:
    return getattr(dal, "_dbname", "") == "sqlite"


def add_exclusion(dal: Any, user_uuid: str | uuid.UUID, reason: ExclusionReason) -> bool:
    """Insert `(user_uuid, reason)` if absent; NO commit (the caller owns the transaction).

    Returns True when a row was inserted, False when it already existed (idempotent, so a replay
    or a concurrent writer is harmless). Stores the uuid token and the reason only.
    """
    token = _token(user_uuid)
    code = int(ExclusionReason(reason))
    if _row_exists(dal, token, code):
        return False
    cast = "" if _is_sqlite(dal) else "::uuid"
    on_conflict = "" if _is_sqlite(dal) else " ON CONFLICT DO NOTHING"
    # Token is a validated canonical uuid and `code` an int from the enum: safe to inline.
    dal.executesql(
        f"INSERT INTO {_TABLE} (user_uuid, reason_code) "  # noqa: S608  # nosec B608
        f"VALUES ('{token}'{cast}, {code}){on_conflict}"
    )
    return True


def _row_exists(dal: Any, token: str, code: int) -> bool:
    """Whether the row for `(token, code)` exists."""
    cast = "" if _is_sqlite(dal) else "::uuid"
    rows = dal.executesql(
        f"SELECT 1 FROM {_TABLE} WHERE user_uuid = '{token}'{cast} AND reason_code = {code}"  # noqa: S608  # nosec B608
    )
    return bool(rows)


def exclusion_reasons(dal: Any, user_uuid: str | uuid.UUID) -> frozenset[int]:
    """Reason codes currently excluding `user_uuid` (read-only)."""
    token = _token(user_uuid)
    cast = "" if _is_sqlite(dal) else "::uuid"
    rows = dal.executesql(
        f"SELECT reason_code FROM {_TABLE} WHERE user_uuid = '{token}'{cast}"  # noqa: S608  # nosec B608
    )
    return frozenset(int(r[0]) for r in rows or [])


def remove_exclusion(dal: Any, user_uuid: str | uuid.UUID, reason: ExclusionReason) -> bool:
    """Delete exactly `(user_uuid, reason)`; NO commit. Returns True if a row existed.

    Refuses ERASED and AGE_NOT_ADULT: a consent re-grant must never lift a suppression that
    rests on erasure or on missing age evidence.
    """
    reason = ExclusionReason(reason)
    if reason not in REMOVABLE_REASONS:
        raise ExclusionError(
            f"exclusion reason {reason.name} cannot be removed by a consent change"
        )
    token = _token(user_uuid)
    code = int(reason)
    cast = "" if _is_sqlite(dal) else "::uuid"
    existed = _row_exists(dal, token, code)
    if existed:
        dal.executesql(
            f"DELETE FROM {_TABLE} WHERE user_uuid = '{token}'{cast} AND reason_code = {code}"  # noqa: S608  # nosec B608
        )
    return existed


def plan_consent_change(
    previous: dict[str, Any] | None, merged: dict[str, Any], *, gpc_applied: bool = False
) -> ConsentPlan:
    """Decide which exclusions a consent write adds / removes (pure, fail-closed).

    * `doNotSell` true (explicit, or forced by GPC / the admin DSAR) -> add DO_NOT_SELL; every such
      write re-asserts it, so a lost row heals on the user's next consent write.
    * a `Sec-GPC` signal applied on this write -> also add GPC.
    * marketing consent WITHDRAWN (true -> false) -> add DO_NOT_SELL (CPRA: marketing/sharing
      consent withdrawal is an opt-out of sharing; the doc has no separate reason for it).
    * explicit opt-back-in (`doNotSell` true -> not true, no GPC) -> remove DO_NOT_SELL and GPC.
    * marketing re-granted (false -> true) while `doNotSell` is not true -> remove DO_NOT_SELL.
    ERASED / AGE_NOT_ADULT / OBJECTION are never planned here.
    """
    before = previous or {}
    do_not_sell = merged.get("doNotSell") is True
    add: set[ExclusionReason] = set()
    remove: set[ExclusionReason] = set()
    if do_not_sell:
        add.add(ExclusionReason.DO_NOT_SELL)
    if gpc_applied:
        add.update({ExclusionReason.GPC, ExclusionReason.DO_NOT_SELL})
    if before.get("marketing") is True and merged.get("marketing") is False:
        add.add(ExclusionReason.DO_NOT_SELL)
    if not do_not_sell and not gpc_applied:
        if before.get("doNotSell") is True:
            remove.update({ExclusionReason.DO_NOT_SELL, ExclusionReason.GPC})
        elif before.get("marketing") is False and merged.get("marketing") is True:
            remove.add(ExclusionReason.DO_NOT_SELL)
    # A reason can be both re-asserted and lifted by the same write only through the marketing
    # rules above; additions win (fail-closed).
    remove -= add
    return ConsentPlan(add=frozenset(add), remove=frozenset(remove))


def _resolve_user_uuid(dal: Any, user_id: int) -> str:
    """`hub_users.uuid` for the numeric id; raises (never defaults) for an unknown subject."""
    rows = dal.executesql(f"SELECT uuid FROM hub_users WHERE id = {int(user_id)}")  # noqa: S608  # nosec B608
    if not rows or not rows[0][0]:
        raise ExclusionError("cannot record an exclusion: hub user has no uuid token")
    return _token(rows[0][0])


def apply_plan(dal: Any, user_id: int, plan: ConsentPlan) -> ExclusionChange:
    """Apply `plan` for `user_id` on the caller's transaction (no commit)."""
    change = ExclusionChange()
    if plan.empty:
        return change
    token = _resolve_user_uuid(dal, user_id)
    change.user_uuid = token
    for reason in sorted(plan.add):
        if add_exclusion(dal, token, reason):
            change.added.append(reason)
    for reason in sorted(plan.remove):
        if remove_exclusion(dal, token, reason):
            change.removed.append(reason)
    return change


async def apply_consent_change(
    async_dal: Any,
    dal: Any,
    *,
    user_id: int | None,
    write: Callable[[Any], None],
    previous: dict[str, Any] | None,
    merged: dict[str, Any],
    gpc_applied: bool = False,
) -> ExclusionChange:
    """Run the consent `write` and its exclusion changes in ONE transaction, then audit + count.

    `write(dal)` performs the consent row write with plain pydal calls and MUST NOT commit. A
    failure anywhere rolls back BOTH the consent write and the exclusion write. `user_id=None`
    (an anonymous visitor) has no subject token, so only the write runs.
    """
    plan = (
        ConsentPlan()
        if user_id is None
        else plan_consent_change(previous, merged, gpc_applied=gpc_applied)
    )

    def _job() -> ExclusionChange:
        try:
            write(dal)
            change = apply_plan(dal, user_id, plan) if user_id is not None else ExclusionChange()
            dal.commit()
            return change
        except Exception:
            dal.rollback()
            raise

    loop = asyncio.get_running_loop()
    change = await loop.run_in_executor(
        async_dal.executor, functools.partial(contextvars.copy_context().run, _job)
    )
    await _after_commit(change, actor_user_id=user_id)
    return change


async def _after_commit(change: ExclusionChange, *, actor_user_id: int | None) -> None:
    """Count + audit what the (already committed) act changed; audit failure is loud, not hidden."""
    for reason in change.added:
        _added.add(1, {"reason": reason.name.lower()})
    for reason in change.removed:
        _removed.add(1, {"reason": reason.name.lower()})
    if change.user_uuid is None:
        return
    for action, reasons in (
        (AuditAction.PRIVACY_EXCLUSION_ADDED, change.added),
        (AuditAction.PRIVACY_EXCLUSION_REMOVED, change.removed),
    ):
        for reason in reasons:
            await _audit(
                AuditEvent(
                    category=AuditCategory.PRIVACY,
                    action=action.value,
                    outcome=AuditOutcome.SUCCESS,
                    actor_kind=ActorKind.USER if actor_user_id is not None else ActorKind.SYSTEM,
                    actor_user_id=actor_user_id,
                    target_type="analytics_subject",
                    target_id=change.user_uuid,
                    details={"reason_code": int(reason)},
                )
            )


async def _audit(event: AuditEvent) -> None:
    """Record `event` on the chain (one retry, then a loud `AuditWriteError`); skip if unwired."""
    service = audit_http.get_service()
    if service is None:
        logger.warning(
            "analytics_exclusions.audit_unwired",
            extra={"action": event.action, "result": "skipped"},
        )
        return
    try:
        await service.record(event)
        return
    except Exception as first:  # noqa: BLE001 - retried once below, then re-raised loudly
        logger.warning("analytics_exclusions.audit_retry", extra={"type": type(first).__name__})
    try:
        await service.record(event)
    except Exception as exc:
        raise report_write_failure(
            category=event.category.value,
            action=event.action,
            chain_id=None,
            attempts=2,
            exc=exc,
        ) from exc


# --------------------------------------------------------------------------- reconciliation

_CONSENT_BATCH_SQL = (
    "WITH batch AS (SELECT id, user_id, preferences FROM cookie_consent "
    "WHERE id > %s AND user_id IS NOT NULL ORDER BY id LIMIT %s), "
    "ins AS (INSERT INTO data_use_exclusions (user_uuid, reason_code) "
    "SELECT DISTINCT hu.uuid, %s FROM batch b JOIN hub_users hu ON hu.id = b.user_id "
    "WHERE (b.preferences::jsonb ->> 'doNotSell') = 'true' "
    "ON CONFLICT DO NOTHING RETURNING 1) "
    "SELECT (SELECT count(*) FROM batch), (SELECT count(*) FROM ins), "
    "(SELECT max(id) FROM batch)"
)
_LEDGER_BATCH_SQL = (
    "WITH batch AS (SELECT id, hub_user_id FROM data_deletion_requests "
    "WHERE id > %s AND status = 'completed' AND hub_user_id IS NOT NULL ORDER BY id LIMIT %s), "
    "ins AS (INSERT INTO data_use_exclusions (user_uuid, reason_code) "
    "SELECT DISTINCT hu.uuid, %s FROM batch b JOIN hub_users hu ON hu.id = b.hub_user_id "
    "ON CONFLICT DO NOTHING RETURNING 1) "
    "SELECT (SELECT count(*) FROM batch), (SELECT count(*) FROM ins), "
    "(SELECT max(id) FROM batch)"
)


def _reconcile_batch(dal: Any, sql: str, *, after: int, limit: int, reason: ExclusionReason) -> Any:
    """One bounded batch in its OWN transaction, under the advisory lock (None = lock not won)."""
    try:
        got = dal.executesql(
            "SELECT pg_try_advisory_xact_lock(%s)", placeholders=[_ADVISORY_LOCK_KEY]
        )
        if not got or not got[0][0]:
            dal.rollback()
            return None
        rows = dal.executesql(sql, placeholders=[after, limit, int(reason)])
        dal.commit()
        examined, inserted, last = rows[0]
        return int(examined), int(inserted), (int(last) if last is not None else None)
    except Exception:
        dal.rollback()
        raise


def reconcile_exclusions(dal: Any, *, batch_size: int = DEFAULT_BATCH_SIZE) -> ReconcileResult:
    """Rebuild MISSING exclusions from the authoritative sources; idempotent, bounded, ADD-only.

    Sources: `cookie_consent` rows whose `doNotSell` is true -> DO_NOT_SELL; the erasure ledger
    (`data_deletion_requests.status = 'completed'`) -> ERASED for the retained `hub_users.uuid`.
    (Marketing-withdrawal and GPC rows are act-time only: neither is derivable from stored state
    without the risk of undoing a later re-grant; a GPC subject is covered by DO_NOT_SELL.) Each
    batch is its own transaction guarded by a Postgres advisory transaction lock so exactly one
    replica works at a time -- a lost lock ends the pass (`skipped_locked`). Postgres only.
    """
    if _is_sqlite(dal) or getattr(dal, "_dbname", "") not in ("postgres", "postgresql"):
        raise ExclusionError("reconcile_exclusions requires Postgres (advisory lock + CTE insert)")
    if batch_size < 1:
        raise ExclusionError("batch_size must be >= 1")
    result = ReconcileResult()
    for sql, reason, source in (
        (_CONSENT_BATCH_SQL, ExclusionReason.DO_NOT_SELL, _SOURCE_CONSENT),
        (_LEDGER_BATCH_SQL, ExclusionReason.ERASED, _SOURCE_ERASURE),
    ):
        after = 0
        while True:
            batch = _reconcile_batch(dal, sql, after=after, limit=batch_size, reason=reason)
            if batch is None:
                result.skipped_locked = True
                return result
            examined, inserted, last = batch
            result.batches += 1
            if source == _SOURCE_CONSENT:
                result.consent_examined += examined
            else:
                result.ledger_examined += examined
            if inserted:
                result.added[reason] = result.added.get(reason, 0) + inserted
            _examined.add(examined, {"source": source})
            if last is None or examined < batch_size:
                break
            after = last
    for reason, count in result.added.items():
        _reconciled.add(count, {"reason": reason.name.lower()})
    return result


async def run_reconcile_once(
    async_dal: Any, *, batch_size: int = DEFAULT_BATCH_SIZE
) -> ReconcileResult | None:
    """One flag-gated pass in an executor job; returns None when the flag is OFF."""
    if not await feature_enabled(RECONCILE_FLAG, tenant=_FLAG_TENANT, default=False):
        logger.debug("analytics_exclusions.reconcile_flag_off")
        return None
    loop = asyncio.get_running_loop()
    result: ReconcileResult = await loop.run_in_executor(
        async_dal.executor,
        functools.partial(
            contextvars.copy_context().run,
            functools.partial(reconcile_exclusions, async_dal.dal, batch_size=batch_size),
        ),
    )
    _passes.add(1, {"result": "skipped_locked" if result.skipped_locked else "ok"})
    logger.info(
        "analytics_exclusions.reconciled",
        extra={
            "action": "analytics_exclusions_reconcile",
            "consent_examined": result.consent_examined,
            "ledger_examined": result.ledger_examined,
            "added": result.total_added,
            "batches": result.batches,
            "skipped_locked": result.skipped_locked,
        },
    )
    if result.total_added:
        await _audit(
            AuditEvent(
                category=AuditCategory.PRIVACY,
                action=AuditAction.PRIVACY_EXCLUSIONS_RECONCILED.value,
                actor_kind=ActorKind.SYSTEM,
                target_type="analytics_exclusions",
                details={
                    "added": result.total_added,
                    "examined": result.consent_examined + result.ledger_examined,
                },
            )
        )
    return result


async def run_reconcile_loop(
    async_dal: Any,
    *,
    is_ready: Callable[[], bool],
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    """Reconcile forever on `interval_seconds`; a failed pass is logged at ERROR and retried.

    Started once per replica from hub-api startup; the advisory lock makes concurrent replicas
    take turns. `is_ready` (schema bootstrap finished) gates each pass.
    """
    while True:
        try:
            if is_ready():
                await run_reconcile_once(async_dal, batch_size=batch_size)
        except asyncio.CancelledError:
            raise
        except Exception:
            _passes.add(1, {"result": "error"})
            logger.exception(
                "analytics_exclusions.reconcile_failed",
                extra={"action": "analytics_exclusions_reconcile", "result": "error"},
            )
        await asyncio.sleep(interval_seconds)


__all__: Sequence[str] = (
    "DEFAULT_BATCH_SIZE",
    "RECONCILE_FLAG",
    "REMOVABLE_REASONS",
    "ConsentPlan",
    "ExclusionChange",
    "ExclusionError",
    "ExclusionReason",
    "ReconcileResult",
    "add_exclusion",
    "apply_consent_change",
    "apply_plan",
    "exclusion_reasons",
    "plan_consent_change",
    "reconcile_exclusions",
    "remove_exclusion",
    "run_reconcile_loop",
    "run_reconcile_once",
)
