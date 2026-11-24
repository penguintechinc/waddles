"""Twitch EventSub desired-state ledger: derive, reconcile-work, and result reporting.

Control-plane half of EventSub subscription management. The control plane owns *which*
subscriptions should exist (``twitch_eventsub_subscriptions``, migration 0082); the Twitch
platform bundle's reconcile hook executes create/delete against Twitch with an app token and
reports the outcome back through :func:`report_results`. svc-ingest only *receives* deliveries.

Desired state per community = (approved, enabled, active Twitch channel connection) x (EventSub
types that any activated bundle's ``consumes`` rules ask for on ``twitch``). The broadcaster is
referenced only by the connection row (never a raw platform id). All enum-ish columns are int
codes (``EventSubType`` / ``SubStatus`` / ``FailureReason``). Each subscription carries its own
webhook secret, sealed AES-256-GCM with row-bound AAD via ``platform_integrations_crypto``
(the connection-secret mechanism); plaintext exists only in memory and in the one response to
the machine-authenticated caller -- it is never logged or put on a span.

Logging/metrics/spans carry ids, counts and bounded enums only.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import Any, Final

from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode
from penguin_dal import AsyncDB
from sqlalchemy import text

from services.provider_connections.vault import SEAL_AAD, open_secret, seal_secret

logger = logging.getLogger(__name__)

_NAME: Final = "waddles.hub_api.eventsub"
_meter = metrics.get_meter(_NAME)
_tracer = trace.get_tracer(_NAME)
_ops = _meter.create_counter(
    "waddles_eventsub_ops_total", description="EventSub desired-state ops by op and outcome."
)
_subs = _meter.create_counter(
    "waddles_eventsub_transitions_total",
    description="EventSub subscription transitions by bounded kind.",
)
_duration = _meter.create_histogram(
    "waddles_eventsub_op_duration_seconds", unit="s", description="EventSub op latency by op."
)
_work_size = _meter.create_histogram(
    "waddles_eventsub_work_items", description="Work items handed to the reconcile hook."
)

CALLBACK_PATH: Final = "/eventsub/twitch/webhook"
MAX_WORK: Final = 200
MAX_COMMUNITIES_PER_SWEEP: Final = 500
MAX_ENABLED_IDS: Final = 5000
_BACKOFF_BASE_S: Final = 30
_BACKOFF_CAP_S: Final = 3600
_TABLE: Final = "twitch_eventsub_subscriptions"
_COUNT_KEYS: Final = ("created", "marked_delete", "removed", "marked_stale", "requeued")


class EventSubType(IntEnum):
    """The seven EventSub types the Rust Twitch intake handles (bundles/rust/twitch eventsub.rs)."""

    CHANNEL_FOLLOW = 1
    CHANNEL_SUBSCRIBE = 2
    CHANNEL_SUBSCRIPTION_GIFT = 3
    CHANNEL_CHEER = 4
    CHANNEL_RAID = 5
    STREAM_ONLINE = 6
    STREAM_OFFLINE = 7


#: code -> (Twitch type string, Twitch subscription version). ``channel.follow`` is v2 (v1 is
#: retired); the rest are v1.
EVENTSUB_TYPES: Final[dict[EventSubType, tuple[str, int]]] = {
    EventSubType.CHANNEL_FOLLOW: ("channel.follow", 2),
    EventSubType.CHANNEL_SUBSCRIBE: ("channel.subscribe", 1),
    EventSubType.CHANNEL_SUBSCRIPTION_GIFT: ("channel.subscription.gift", 1),
    EventSubType.CHANNEL_CHEER: ("channel.cheer", 1),
    EventSubType.CHANNEL_RAID: ("channel.raid", 1),
    EventSubType.STREAM_ONLINE: ("stream.online", 1),
    EventSubType.STREAM_OFFLINE: ("stream.offline", 1),
}


class SubStatus(IntEnum):
    """Lifecycle of one desired subscription."""

    PENDING = 1  # wants creating
    ENABLED = 2  # exists on Twitch, verified
    FAILED = 3  # last create failed (retry per next_attempt_at, or awaiting connection change)
    REVOKED = 4  # Twitch revoked it; recreated on the next sweep while still desired
    DELETE_PENDING = 5  # no longer desired; must be deleted on Twitch
    STALE = 6  # exists but callback changed; delete then recreate


class FailureReason(IntEnum):
    """Why a create/delete failed (or Twitch revoked)."""

    AUTH_REJECTED = 1
    MISSING_SCOPE = 2
    RATE_LIMITED = 3
    TWITCH_REJECTED = 4
    TRANSPORT_ERROR = 5
    TWITCH_REVOKED = 6
    CONNECTION_UNAVAILABLE = 7


_RETRYABLE: Final = frozenset(
    {
        FailureReason.RATE_LIMITED,
        FailureReason.TRANSPORT_ERROR,
        FailureReason.CONNECTION_UNAVAILABLE,
    }
)


class Action(IntEnum):
    """What the reconcile hook must do for a work item."""

    CREATE = 1
    DELETE = 2


class Outcome(IntEnum):
    """What the reconcile hook reports for a work item."""

    CREATED = 1
    DELETED = 2
    FAILED = 3
    REVOKED = 4


class EventSubError(Exception):
    """Base error; subclasses map to one HTTP status in the blueprint."""


class NotConfiguredError(EventSubError):
    """No callback base URL is configured (503 -- never register a bogus callback)."""


class InvalidReportError(EventSubError):
    """A result report is malformed or inconsistent (400)."""


class UnknownSubscriptionError(EventSubError):
    """A reported/looked-up subscription does not exist (404)."""


def _aad(public_id: uuid.UUID) -> bytes:
    """Row-bound AAD: a sealed secret copied to another subscription row stops authenticating."""
    return f"eventsub:{public_id}".encode()


def _new_secret() -> str:
    """256-bit CSPRNG webhook secret (Twitch allows 10-100 chars; urlsafe-43 fits)."""
    return secrets.token_urlsafe(32)


def callback_url(base_url: str, callback_key: str) -> str:
    """Compose the transport callback URL from configured base + opaque key."""
    if not base_url:
        raise NotConfiguredError("eventsub callback base url is not configured")
    return f"{base_url.rstrip('/')}{CALLBACK_PATH}/{callback_key}"


@dataclass(slots=True, frozen=True)
class SyncSummary:
    """Denominators of one sweep (zero examined is reported, never hidden)."""

    communities: int
    created: int = 0
    marked_delete: int = 0
    removed: int = 0
    marked_stale: int = 0
    requeued: int = 0
    next_community_id: int | None = None


@dataclass(slots=True, frozen=True)
class WorkItem:
    """One create/delete the reconcile hook must perform. Never carries the webhook secret."""

    public_id: str
    action: Action
    tenant_id: int
    community_id: int
    connection_id: int
    sub_type: EventSubType
    twitch_type: str
    version: int
    callback_url: str
    twitch_subscription_id: str | None
    callback_key: str
    nonce: str


@dataclass(slots=True, frozen=True)
class EnabledSet:
    """Twitch ids of ENABLED subscriptions in the swept communities.

    ``complete`` is true ONLY when this list covers every community in the ledger (sweep started
    at 0, no further page, not truncated). A hook MUST NOT garbage-collect Twitch-side
    subscriptions against an incomplete list.
    """

    ids: list[str]
    complete: bool


@dataclass(slots=True, frozen=True)
class Report:
    """One reconcile result for one subscription."""

    public_id: str
    outcome: Outcome
    twitch_subscription_id: str | None = None
    failure_reason: FailureReason | None = None
    nonce: str | None = None


def _event_pattern_matches(pattern: str, event_type: str) -> bool:
    """Dotted wildcard match: ``*`` = one segment, ``**`` (or a ``**`` segment) = the rest."""
    if pattern == "**":
        return True
    p_parts, e_parts = pattern.split("."), event_type.split(".")
    for i, part in enumerate(p_parts):
        if part == "**":
            return True
        if i >= len(e_parts) or (part != "*" and part != e_parts[i]):
            return False
    return len(p_parts) == len(e_parts)


def types_for_event_patterns(patterns: Iterable[str]) -> frozenset[EventSubType]:
    """EventSub types matched by any bundle ``consumes`` event-type pattern."""
    pats = list(patterns)
    return frozenset(
        t
        for t, (name, _v) in EVENTSUB_TYPES.items()
        if any(_event_pattern_matches(p, name) for p in pats)
    )


async def _desired_types(conn: Any, tenant_id: int, community_id: int) -> frozenset[EventSubType]:
    """Types asked for by activated bundles (community-scoped or tenant-wide) on twitch."""
    rows = (
        await conn.execute(
            text(
                """
                SELECT av.app_id, v.version,
                       (SELECT u.manifest_json FROM app_version_uploads u
                         WHERE u.app_id = av.app_id AND u.version = v.version
                         ORDER BY u.id DESC LIMIT 1) AS manifest_json
                  FROM app_active_versions av
                  JOIN app_versions v ON v.id = av.version_id
                 WHERE av.tenant_id = :t AND av.community_id IN (0, :c)
                """
            ),
            {"t": tenant_id, "c": community_id},
        )
    ).all()
    patterns: list[str] = []
    for row in rows:
        if row.manifest_json is None:
            logger.warning("eventsub.manifest_missing", extra={"app_id": row.app_id})
            continue
        stages = row.manifest_json.get("stages") or {}
        for rule in (stages.get("process") or {}).get("consumes") or []:
            if rule.get("platform") in ("twitch", "*"):
                patterns.extend(str(e) for e in rule.get("event_types") or [])
    return types_for_event_patterns(patterns)


async def _eligible_connections(conn: Any, tenant_id: int, community_id: int) -> list[int]:
    """Active Twitch channel connections this community may use (approved + enabled grant)."""
    rows = (
        await conn.execute(
            text(
                """
                SELECT pc.id FROM platform_connections pc
                  JOIN community_connection_access cca ON cca.connection_id = pc.id
                 WHERE cca.community_id = :c AND cca.status = 'approved' AND cca.enabled
                   AND pc.tenant_id = :t AND pc.platform = 'twitch'
                   AND pc.resource_type = 'twitch_channel' AND pc.status = 'active'
                 ORDER BY pc.id
                """
            ),
            {"t": tenant_id, "c": community_id},
        )
    ).all()
    return [int(r.id) for r in rows]


async def _set_status(conn: Any, row_id: int, status: SubStatus) -> None:
    """Set a row's status and touch ``updated_at``."""
    await conn.execute(
        text(f"UPDATE {_TABLE} SET status = :s, updated_at = NOW() WHERE id = :i"),  # noqa: S608  # nosec B608 -- _TABLE is a module constant
        {"s": int(status), "i": row_id},
    )


async def _set_url(conn: Any, row_id: int, url: str) -> None:
    """Refresh a row's stored callback URL (config drift)."""
    await conn.execute(
        text(f"UPDATE {_TABLE} SET callback_url = :u, updated_at = NOW() WHERE id = :i"),  # noqa: S608  # nosec B608 -- _TABLE is a module constant
        {"u": url, "i": row_id},
    )


async def _requeue(conn: Any, row: Any, want_url: str, *, rotate: bool) -> None:
    """Back to PENDING with failure state cleared; optionally rotate the sealed secret."""
    await conn.execute(
        text(
            f"""
            UPDATE {_TABLE}
               SET status = 1, callback_url = :u, twitch_subscription_id = NULL,
                   failure_reason = NULL, failure_count = 0, next_attempt_at = NULL,
                   secret_enc = CASE WHEN :rot THEN :sec ELSE secret_enc END,
                   updated_at = NOW()
             WHERE id = :i
            """  # noqa: S608  # nosec B608 -- _TABLE is a module constant
        ),
        {
            "u": want_url,
            "rot": rotate,
            "sec": seal_secret(_new_secret(), aad=_aad(row.public_id)) if rotate else "",
            "i": row.id,
        },
    )


async def _insert(
    conn: Any,
    tenant_id: int,
    community_id: int,
    connection_id: int,
    sub_type: EventSubType,
    base_url: str,
) -> None:
    """Insert a PENDING row with a fresh opaque callback key and sealed secret."""
    public_id = uuid.uuid4()
    key = secrets.token_urlsafe(32)
    await conn.execute(
        text(
            f"""
            INSERT INTO {_TABLE}
                (public_id, tenant_id, community_id, connection_id, sub_type, sub_version,
                 status, callback_key, callback_url, secret_enc)
            VALUES (:p, :t, :c, :n, :ty, :v, 1, :k, :u, :s)
            """  # noqa: S608  # nosec B608 -- _TABLE is a module constant
        ),
        {
            "p": public_id,
            "t": tenant_id,
            "c": community_id,
            "n": connection_id,
            "ty": int(sub_type),
            "v": EVENTSUB_TYPES[sub_type][1],
            "k": key,
            "u": callback_url(base_url, key),
            "s": seal_secret(_new_secret(), aad=_aad(public_id)),
        },
    )


async def _sync_existing(
    conn: Any, row: Any, wanted: bool, base_url: str, counts: dict[str, int]
) -> None:
    """Move one existing row toward the desired set (see :func:`sync_community`)."""
    status = SubStatus(row.status)
    if not wanted:
        if row.twitch_subscription_id and status != SubStatus.DELETE_PENDING:
            await _set_status(conn, row.id, SubStatus.DELETE_PENDING)
            counts["marked_delete"] += 1
        elif not row.twitch_subscription_id:
            await conn.execute(text(f"DELETE FROM {_TABLE} WHERE id = :i"), {"i": row.id})  # noqa: S608  # nosec B608 -- _TABLE is a module constant
            counts["removed"] += 1
        return
    want_url = callback_url(base_url, row.callback_key)
    if status == SubStatus.DELETE_PENDING:
        # Desired again before the delete ran: Twitch state is unknown, so delete then recreate.
        await _set_status(conn, row.id, SubStatus.STALE)
        counts["marked_stale"] += 1
    elif status == SubStatus.ENABLED and row.callback_url != want_url:
        await _set_url(conn, row.id, want_url)
        await _set_status(conn, row.id, SubStatus.STALE)
        counts["marked_stale"] += 1
    elif status == SubStatus.REVOKED:
        await _requeue(conn, row, want_url, rotate=True)
        counts["requeued"] += 1
    elif status == SubStatus.PENDING and row.callback_url != want_url:
        await _set_url(conn, row.id, want_url)
    elif (
        status == SubStatus.FAILED
        and row.next_attempt_at is None
        and row.conn_updated is not None
        and (row.last_reconciled_at is None or row.conn_updated > row.last_reconciled_at)
    ):
        # A permanent failure (auth/scope/rejected) clears only once the connection changed.
        await _requeue(conn, row, want_url, rotate=False)
        counts["requeued"] += 1


async def sync_community(
    conn: Any, *, tenant_id: int, community_id: int, base_url: str
) -> dict[str, int]:
    """Reconcile the ledger for one community to the derived desired set.

    Runs in the caller's transaction; returns per-kind transition counts.
    """
    counts = dict.fromkeys(_COUNT_KEYS, 0)
    types = await _desired_types(conn, tenant_id, community_id)
    conns = await _eligible_connections(conn, tenant_id, community_id)
    desired = {(c, int(t)) for c in conns for t in types}
    existing = (
        await conn.execute(
            text(
                f"""
                SELECT s.id, s.public_id, s.connection_id, s.sub_type, s.status,
                       s.twitch_subscription_id, s.callback_key, s.callback_url,
                       s.next_attempt_at, s.last_reconciled_at, pc.updated_at AS conn_updated
                  FROM {_TABLE} s
                  LEFT JOIN platform_connections pc ON pc.id = s.connection_id
                 WHERE s.community_id = :c
                """  # noqa: S608  # nosec B608 -- _TABLE is a module constant
            ),
            {"c": community_id},
        )
    ).all()
    seen: set[tuple[int, int]] = set()
    for row in existing:
        key = (int(row.connection_id), int(row.sub_type))
        seen.add(key)
        await _sync_existing(conn, row, key in desired, base_url, counts)
    for conn_id, sub_type in sorted(desired - seen):
        await _insert(conn, tenant_id, community_id, conn_id, EventSubType(sub_type), base_url)
        counts["created"] += 1
    for kind, n in counts.items():
        if n:
            _subs.add(n, {"kind": kind})
    return counts


async def _community_page(conn: Any, after_community_id: int) -> tuple[list[int], int | None]:
    """One bounded page of communities that have (or had) a Twitch grant or ledger rows."""
    ids = (
        await conn.execute(
            text(
                f"""
                SELECT community_id FROM (
                    SELECT cca.community_id FROM community_connection_access cca
                      JOIN platform_connections pc ON pc.id = cca.connection_id
                     WHERE pc.platform = 'twitch'
                    UNION SELECT community_id FROM {_TABLE}
                ) x WHERE community_id > :a ORDER BY community_id LIMIT :n
                """  # noqa: S608  # nosec B608 -- _TABLE is a module constant
            ),
            {"a": after_community_id, "n": MAX_COMMUNITIES_PER_SWEEP + 1},
        )
    ).all()
    cids = [int(r.community_id) for r in ids]
    if len(cids) > MAX_COMMUNITIES_PER_SWEEP:
        cids = cids[:MAX_COMMUNITIES_PER_SWEEP]
        return cids, cids[-1]
    return cids, None


async def reconcile(
    install_dal: AsyncDB,
    *,
    base_url: str,
    after_community_id: int = 0,
    limit: int = MAX_WORK,
    now: datetime | None = None,
) -> tuple[SyncSummary, list[WorkItem], EnabledSet]:
    """Sweep desired state (one bounded page of communities) and return the pending work.

    Returns ``(summary, work, enabled)``; ``enabled`` is scoped to the swept communities and only
    safe for orphan GC when ``enabled.complete``. Empty ``base_url`` raises
    :class:`NotConfiguredError` before any DB work.
    """
    callback_url(base_url, "x")  # fail loud before touching the DB
    started = time.perf_counter()
    now = now or datetime.now(UTC)
    limit = max(1, min(limit, MAX_WORK))
    with _tracer.start_as_current_span(
        "eventsub.reconcile", record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            async with install_dal.engine.begin() as conn:
                cids, nxt = await _community_page(conn, after_community_id)
                totals = dict.fromkeys(_COUNT_KEYS, 0)
                for cid in cids:
                    tenant = (
                        await conn.execute(
                            text("SELECT tenant_id FROM communities WHERE id = :c"), {"c": cid}
                        )
                    ).scalar_one_or_none()
                    if tenant is None:
                        continue
                    done = await sync_community(
                        conn, tenant_id=int(tenant), community_id=cid, base_url=base_url
                    )
                    for k, v in done.items():
                        totals[k] += v
                work, enabled = await _collect_work(
                    conn, now, limit, cids, complete_scope=after_community_id == 0 and nxt is None
                )
        except BaseException as exc:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            _ops.add(1, {"op": "reconcile", "outcome": "error"})
            raise
        finally:
            _duration.record(time.perf_counter() - started, {"op": "reconcile"})
    summary = SyncSummary(communities=len(cids), next_community_id=nxt, **totals)
    _ops.add(1, {"op": "reconcile", "outcome": "ok"})
    _work_size.record(len(work))
    logger.info(
        "eventsub.reconciled",
        extra={
            "communities": summary.communities,
            "work": len(work),
            "created_subs": summary.created,
        },
    )
    return summary, work, enabled


async def _collect_work(
    conn: Any, now: datetime, limit: int, cids: list[int], *, complete_scope: bool
) -> tuple[list[WorkItem], EnabledSet]:
    """Actionable rows (create/delete, each stamped with a fresh single-use nonce) + enabled ids.

    The nonce/issued action recorded here is what :func:`report_results` requires back, so only
    the most recently issued, still-outstanding action can be reported.
    """
    rows = (
        await conn.execute(
            text(
                f"""
                SELECT public_id, tenant_id, community_id, connection_id, sub_type, sub_version,
                       status, twitch_subscription_id, callback_url, callback_key
                  FROM {_TABLE}
                 WHERE status IN (1, 5, 6)
                    OR (status = 3 AND next_attempt_at IS NOT NULL AND next_attempt_at <= :now)
                 ORDER BY id LIMIT :n
                """  # noqa: S608  # nosec B608 -- _TABLE is a module constant
            ),
            {"now": now, "n": limit},
        )
    ).all()
    work: list[WorkItem] = []
    for r in rows:
        is_delete = SubStatus(r.status) in (SubStatus.DELETE_PENDING, SubStatus.STALE)
        if is_delete and not r.twitch_subscription_id:
            continue  # nothing exists on Twitch to delete
        t = EventSubType(r.sub_type)
        nonce = uuid.uuid4()
        action = Action.DELETE if is_delete else Action.CREATE
        await conn.execute(
            text(
                f"UPDATE {_TABLE} SET issued_action = :a, issued_nonce = :n WHERE public_id = :p"  # noqa: S608  # nosec B608 -- _TABLE is a module constant
            ),
            {"a": int(action), "n": nonce, "p": r.public_id},
        )
        work.append(
            WorkItem(
                public_id=str(r.public_id),
                action=action,
                tenant_id=int(r.tenant_id),
                community_id=int(r.community_id),
                connection_id=int(r.connection_id),
                sub_type=t,
                twitch_type=EVENTSUB_TYPES[t][0],
                version=int(r.sub_version),
                callback_url=r.callback_url,
                twitch_subscription_id=r.twitch_subscription_id,
                callback_key=r.callback_key,
                nonce=str(nonce),
            )
        )
    enabled_rows = (
        await conn.execute(
            text(
                f"SELECT twitch_subscription_id FROM {_TABLE} "  # noqa: S608  # nosec B608 -- _TABLE is a module constant
                "WHERE status = 2 AND twitch_subscription_id IS NOT NULL "
                "AND community_id = ANY(:cids) ORDER BY id LIMIT :n"
            ),
            {"cids": cids, "n": MAX_ENABLED_IDS + 1},
        )
    ).all()
    truncated = len(enabled_rows) > MAX_ENABLED_IDS
    ids = [str(r.twitch_subscription_id) for r in enabled_rows[:MAX_ENABLED_IDS]]
    return work, EnabledSet(ids=ids, complete=complete_scope and not truncated)


_TWITCH_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CALLBACK_KEY_RE: Final = re.compile(r"[A-Za-z0-9_-]{16,64}")


def _backoff(failure_count: int, now: datetime) -> datetime:
    """Exponential retry time, capped."""
    delay = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2 ** max(0, failure_count - 1))
    return now + timedelta(seconds=delay)


async def report_results(
    install_dal: AsyncDB, reports: list[Report], *, now: datetime | None = None
) -> int:
    """Apply the hook's results atomically; returns the number applied.

    CREATED -> ENABLED (needs a Twitch id); DELETED -> row removed, or (STALE) back to PENDING
    with a rotated secret; FAILED -> FAILED with backoff for retryable reasons (permanent ones
    wait for a connection change); REVOKED -> REVOKED (recreated while still desired). Any
    inconsistency raises before anything commits.
    """
    now = now or datetime.now(UTC)
    started = time.perf_counter()
    with _tracer.start_as_current_span(
        "eventsub.report", record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            async with install_dal.engine.begin() as conn:
                for rep in reports:
                    await _apply(conn, rep, now)
        except BaseException as exc:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            _ops.add(1, {"op": "report", "outcome": "error"})
            raise
        finally:
            _duration.record(time.perf_counter() - started, {"op": "report"})
    _ops.add(1, {"op": "report", "outcome": "ok"})
    for rep in reports:
        _subs.add(1, {"kind": f"reported_{rep.outcome.name.lower()}"})
    logger.info("eventsub.reported", extra={"count": len(reports)})
    return len(reports)


_CREATE_STATES: Final = frozenset({SubStatus.PENDING, SubStatus.FAILED})
_DELETE_STATES: Final = frozenset({SubStatus.DELETE_PENDING, SubStatus.STALE})


def _reject(code: str) -> InvalidReportError:
    """Log the bounded rejection code only (never ids/payload) and build the error."""
    _ops.add(1, {"op": "report", "outcome": f"rejected_{code}"})
    logger.warning("eventsub.report_rejected", extra={"reason": code})
    return InvalidReportError(code)


def _guard_transition(row: Any, rep: Report, status: SubStatus) -> None:
    """Accept a report only for the outstanding issued action, in the expected row state.

    CREATED/FAILED/DELETED must echo the nonce issued with the work item (single-use) and match
    the issued action and the row's current state. REVOKED (Twitch-initiated, no work item) is
    accepted only from ENABLED for the exact Twitch subscription id the ledger holds.
    """
    if rep.outcome is Outcome.REVOKED:
        if status != SubStatus.ENABLED:
            raise _reject("revoke_not_enabled")
        if not rep.twitch_subscription_id or rep.twitch_subscription_id != (
            row.twitch_subscription_id
        ):
            raise _reject("revoke_id_mismatch")
        return
    if row.issued_action is None or row.issued_nonce is None:
        raise _reject("no_outstanding_action")
    try:
        supplied = uuid.UUID(rep.nonce or "")
    except ValueError:
        raise _reject("bad_nonce") from None
    if supplied != row.issued_nonce:
        raise _reject("nonce_mismatch")
    issued = Action(row.issued_action)
    if rep.outcome is Outcome.CREATED:
        ok = issued is Action.CREATE and status in _CREATE_STATES
    elif rep.outcome is Outcome.DELETED:
        ok = issued is Action.DELETE and status in _DELETE_STATES
    else:
        ok = (issued is Action.CREATE and status in _CREATE_STATES) or (
            issued is Action.DELETE and status in _DELETE_STATES
        )
    if not ok:
        raise _reject("illegal_transition")


async def _apply(conn: Any, rep: Report, now: datetime) -> None:
    """Apply one report to its row (locked), enforcing the allowed transitions."""
    try:
        pid = uuid.UUID(rep.public_id)
    except ValueError:
        raise InvalidReportError("subscription id is not a uuid") from None
    row = (
        await conn.execute(
            text(
                "SELECT id, public_id, status, failure_count, callback_url, "  # noqa: S608  # nosec B608 -- _TABLE is a module constant
                "twitch_subscription_id, issued_action, issued_nonce "
                f"FROM {_TABLE} "
                "WHERE public_id = :p FOR UPDATE"
            ),
            {"p": pid},
        )
    ).first()
    if row is None:
        raise UnknownSubscriptionError("unknown subscription")
    status = SubStatus(row.status)
    _guard_transition(row, rep, status)
    if rep.outcome is Outcome.CREATED:
        tid = rep.twitch_subscription_id
        if not tid or not _TWITCH_ID_RE.fullmatch(tid):
            raise InvalidReportError("created report needs a valid twitch subscription id")
        await conn.execute(
            text(
                f"""UPDATE {_TABLE} SET status = 2, twitch_subscription_id = :t,
                    failure_reason = NULL, failure_count = 0, next_attempt_at = NULL,
                    last_reconciled_at = :now, updated_at = NOW() WHERE id = :i"""  # noqa: S608  # nosec B608 -- _TABLE is a module constant
            ),
            {"t": tid, "now": now, "i": row.id},
        )
    elif rep.outcome is Outcome.DELETED:
        if status == SubStatus.STALE:
            await _requeue(conn, row, row.callback_url, rotate=True)
            await conn.execute(
                text(f"UPDATE {_TABLE} SET last_reconciled_at = :now WHERE id = :i"),  # noqa: S608  # nosec B608 -- _TABLE is a module constant
                {"now": now, "i": row.id},
            )
        elif status == SubStatus.DELETE_PENDING:
            await conn.execute(text(f"DELETE FROM {_TABLE} WHERE id = :i"), {"i": row.id})  # noqa: S608  # nosec B608 -- _TABLE is a module constant
        else:
            raise InvalidReportError("deleted reported for a subscription not awaiting delete")
    elif rep.outcome is Outcome.FAILED:
        if rep.failure_reason is None:
            raise InvalidReportError("failed report needs a failure reason")
        count = int(row.failure_count) + 1
        retry_at = _backoff(count, now) if rep.failure_reason in _RETRYABLE else None
        await conn.execute(
            text(
                f"""UPDATE {_TABLE} SET status = CASE WHEN status IN (5, 6) THEN status ELSE 3 END,
                    failure_reason = :fr, failure_count = :fc, next_attempt_at = :na,
                    last_reconciled_at = :now, updated_at = NOW() WHERE id = :i"""  # noqa: S608  # nosec B608 -- _TABLE is a module constant
            ),
            {"fr": int(rep.failure_reason), "fc": count, "na": retry_at, "now": now, "i": row.id},
        )
    else:
        await conn.execute(
            text(
                f"""UPDATE {_TABLE} SET status = 4, failure_reason = :fr,
                    last_reconciled_at = :now, updated_at = NOW() WHERE id = :i"""  # noqa: S608  # nosec B608 -- _TABLE is a module constant
            ),
            {"fr": int(FailureReason.TWITCH_REVOKED), "now": now, "i": row.id},
        )
    # Single-use: the issued action/nonce is consumed, so a replayed report is rejected.
    await conn.execute(
        text(f"UPDATE {_TABLE} SET issued_action = NULL, issued_nonce = NULL WHERE id = :i"),  # noqa: S608  # nosec B608 -- _TABLE is a module constant
        {"i": row.id},
    )


async def secret_for_callback_key(install_dal: AsyncDB, callback_key: str) -> str:
    """Webhook secret for the ingest verifier, by opaque callback key (decrypted, never logged).

    DELETE_PENDING rows still resolve: a late notification for a not-yet-deleted subscription
    must still verify.
    """
    if not _CALLBACK_KEY_RE.fullmatch(callback_key):
        raise UnknownSubscriptionError("unknown callback key") from None
    async with install_dal.engine.connect() as conn:
        row = (
            await conn.execute(
                text(f"SELECT public_id, secret_enc FROM {_TABLE} WHERE callback_key = :k"),  # noqa: S608  # nosec B608 -- _TABLE is a module constant
                {"k": callback_key},
            )
        ).first()
    if row is None:
        _ops.add(1, {"op": "secret", "outcome": "not_found"})
        raise UnknownSubscriptionError("unknown callback key") from None
    _ops.add(1, {"op": "secret", "outcome": "ok"})
    # Every eventsub row is sealed AAD-bound (there is no legacy layout), so pin SEAL_AAD:
    # a no-AAD ciphertext is refused rather than accepted through the migration fallback.
    return open_secret(row.secret_enc, aad=_aad(row.public_id), seal_version=SEAL_AAD)
