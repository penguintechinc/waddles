"""Durable outbox + worker for DSAR verification mail (delivery is OFF the request path).

Why: the public routes used to await SMTP in-band, so `resend-verification` latency revealed
whether a pending request existed (non-enumeration needs uniform *timing*, not just uniform
bodies) and a dead relay could pin a worker for the whole SMTP timeout (a cheap DoS lever on a
public endpoint). Now a route does its DB work, `enqueue()`s a row and returns the uniform 202;
`run_worker_loop` (one per hub-api replica) sends and records the outcome.

Properties:
- Durable: the queue is the `dsar_mail_outbox` table (alembic 0123), so a pod restart loses nothing.
- Multi-replica safe: `claim()` is a conditional UPDATE lease (status pending, lease expired,
  attempts unchanged) that stamps a per-claim `lease_token`; identical on Postgres and sqlite.
  EVERY later write (lease renewal, retry, terminal) is a conditional UPDATE guarded by that token
  with a rowcount check, so a stale worker whose lease expired (and whose row another worker
  re-claimed) can neither send nor overwrite state: a lost lease returns `lost`. One row is
  claimed per iteration and the lease is renewed to cover the full send timeout just before the
  send. Delivery is AT-LEAST-ONCE: if SMTP accepted the mail but the terminal DB write fails, the
  `verification_sent` event was already written first, an ERROR `dsar.outbox.sent_unconfirmed` is
  logged, and the row is re-sent after the lease expires (the older link stays valid, so a
  duplicate mail is harmless).
- No PII: the raw token is sealed in the row (nulled at the terminal state); the requester email
  is read from the request's sealed contact blob at send time and never copied.
- Failure policy: permanent errors (and an unconfigured/misconfigured sender) fail the row at
  once; transient ones retry with exponential backoff + jitter up to `DSAR_OUTBOX_MAX_ATTEMPTS`.
  Timeouts and connection errors count as transient. A terminal failure writes a
  `verification_send_failed` event and an ERROR log (exception class + bounded reason only, no
  traceback: exception text may carry the requester email or token). Delivery is never
  faked. A row whose request closed, whose token expired or was rotated by a resend, is discarded.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from enum import IntEnum
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

from services import dsar_crypto as crypto
from services import dsar_service as svc
from services import dsar_telemetry as telemetry
from services.dsar_notifier import DsarNotifier, NotifierUnavailable, get_notifier
from services.safe_parse import env_float
from services.schema import bind_dsar_tables

logger = logging.getLogger(__name__)

_meter = metrics.get_meter(telemetry._SCOPE)  # noqa: SLF001 - same OTel scope as the DSAR metrics
queue_latency = _meter.create_histogram(
    "waddles_dsar_mail_queue_latency_seconds",
    unit="s",
    description="Enqueue-to-terminal latency of a DSAR verification mail, by outcome.",
)
queue_results = _meter.create_counter(
    "waddles_dsar_mail_queue_total",
    description="Outbox rows reaching a terminal state (sent/failed/discarded).",
)
_depth_value = 0


def _observe_depth(_options: CallbackOptions) -> Iterator[Observation]:
    yield Observation(_depth_value)


_meter.create_observable_gauge(
    "waddles_dsar_mail_queue_depth",
    callbacks=[_observe_depth],
    description="Pending DSAR verification mails in the outbox (last worker poll).",
)


class OutboxStatus(IntEnum):
    """`dsar_mail_outbox.status` codes."""

    PENDING = 1
    SENT = 2
    FAILED = 3
    DISCARDED = 4


class MailKind(IntEnum):
    """`dsar_mail_outbox.kind` codes."""

    SEND = 1
    RESEND = 2


_TERMINAL = (OutboxStatus.SENT, OutboxStatus.FAILED, OutboxStatus.DISCARDED)
_BACKOFF_CAP = 3600.0
_RNG = secrets.SystemRandom()
_wakers: set[asyncio.Event] = set()


def _num(name: str, default: float, minimum: float, maximum: float) -> float:
    """Bounded numeric env var: malformed raises (config error); out-of-range uses the default."""
    value = env_float(name, default)
    return value if minimum <= value <= maximum else default


def _validate_env() -> None:
    """Fail loudly at worker start if any outbox tunable is set but malformed."""
    try:
        for tunable in (max_attempts, backoff_base, lease_seconds, poll_seconds):
            tunable()
        retention_days()
    except ValueError as exc:
        logger.error("dsar.outbox.invalid_config error=%s", exc)  # names the variable only
        raise


def max_attempts() -> int:
    """`DSAR_OUTBOX_MAX_ATTEMPTS` (default 5): total worker attempts before a terminal failure."""
    return int(_num("DSAR_OUTBOX_MAX_ATTEMPTS", 5, 1, 50))


def backoff_base() -> float:
    """`DSAR_OUTBOX_BACKOFF_SECONDS` (default 30): first retry delay; doubles, capped at 1 h."""
    return _num("DSAR_OUTBOX_BACKOFF_SECONDS", 30.0, 0.0, 3600.0)


def lease_seconds() -> float:
    """`DSAR_OUTBOX_LEASE_SECONDS` (default 180): how long a claimed row is held by one worker."""
    return _num("DSAR_OUTBOX_LEASE_SECONDS", 180.0, 5.0, 3600.0)


def poll_seconds() -> float:
    """`DSAR_OUTBOX_POLL_SECONDS` (default 5): worker poll interval (an enqueue also wakes it)."""
    return _num("DSAR_OUTBOX_POLL_SECONDS", 5.0, 0.05, 300.0)


def retention_days() -> float:
    """`DSAR_OUTBOX_RETENTION_DAYS` (default 7): terminal rows are pruned after this."""
    return _num("DSAR_OUTBOX_RETENTION_DAYS", 7.0, 0.0, 365.0)


def notify() -> None:
    """Wake this process's worker(s) now; other replicas pick the row up on their next poll."""
    for waker in tuple(_wakers):
        waker.set()


def _enqueue_fields(
    *, request_row: Any, token: str, resend: bool, tenant_slug: str
) -> dict[str, Any]:
    """Build the outbox row values (token sealed with the row's own public_id as AAD)."""
    public_id = str(uuid.uuid4())
    now = svc.now_utc()
    return {
        "public_id": public_id,
        "tenant_id": int(request_row.tenant_id),
        "request_id": int(request_row.id),
        "tenant_slug": tenant_slug[:63],
        "kind": int(MailKind.RESEND if resend else MailKind.SEND),
        "token_enc": crypto.encrypt(token, row_id=public_id),
        "status": int(OutboxStatus.PENDING),
        "attempts": 0,
        "next_attempt_at": now,
        "created_at": now,
    }


async def enqueue(
    async_dal: Any,
    dal: Any,
    *,
    request_row: Any,
    token: str,
    resend: bool,
    tenant_slug: str,
) -> None:
    """Queue one verification mail. One INSERT, no network I/O to SMTP; never raises SMTP errors.

    This commits on its own (AsyncDAL per-call commit). To make the request write and the enqueue
    ONE atomic unit use `enqueue_in_transaction` inside `ban_suppression.run_in_transaction`.
    """
    await async_dal.insert_async(
        dal.dsar_mail_outbox,
        **_enqueue_fields(
            request_row=request_row, token=token, resend=resend, tenant_slug=tenant_slug
        ),
    )
    telemetry.verification_outcomes.add(1, {"outcome": "queued"})
    notify()


def enqueue_in_transaction(
    dal: Any, *, request_row: Any, token: str, resend: bool, tenant_slug: str
) -> str:
    """Transaction-participating enqueue: a SYNC insert that does NOT commit.

    Call it from the `work` callable of `ban_suppression.run_in_transaction(async_dal, dal, work)`
    together with the request INSERT/UPDATE, so both commit or roll back together (no request
    without its mail, no mail for a rolled-back request). Returns the outbox `public_id`. The
    caller must call `after_commit_enqueued()` once the transaction has committed (wakes the
    worker, records the `queued` metric); skipping it only delays delivery to the next poll.
    """
    fields = _enqueue_fields(
        request_row=request_row, token=token, resend=resend, tenant_slug=tenant_slug
    )
    dal.dsar_mail_outbox.insert(**fields)
    return str(fields["public_id"])


def after_commit_enqueued() -> None:
    """Post-commit hook for `enqueue_in_transaction`: queued metric + wake the local worker."""
    telemetry.verification_outcomes.add(1, {"outcome": "queued"})
    notify()


async def decoy(async_dal: Any, dal: Any, *, writes: int = 2) -> None:
    """Spend the same DB work as a real enqueue path when there is nothing to send.

    Used by the public routes' "nothing pending / unknown tenant / duplicate" branches so latency
    does not reveal which branch ran. A zero-row UPDATE is measurably cheaper than a committed
    write, so this performs REAL writes: a terminal (discarded) outbox row for tenant 0 / request 0
    (the table has no foreign keys for this reason; pruned after the retention window), then
    `writes - 1` UPDATEs of it, all in one transaction like the real path. The token is
    generated, hashed and sealed like the real path.
    """
    public_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)
    crypto.hash_token(token)
    sealed = crypto.encrypt(token, row_id=public_id)
    now = svc.now_utc()

    def _job() -> None:
        # ONE executor job and ONE commit, exactly like the real paths (`run_in_transaction`):
        # per-call AsyncDAL helpers commit separately, which made the decoy measurably slower than
        # the real enqueue (2 commits + 2 thread hops vs 1) and so a timing oracle.
        try:
            row_id = dal.dsar_mail_outbox.insert(
                public_id=public_id,
                tenant_id=0,
                request_id=0,
                tenant_slug="-",
                kind=int(MailKind.SEND),
                token_enc=sealed,
                status=int(OutboxStatus.DISCARDED),
                attempts=0,
                next_attempt_at=now,
                last_reason="decoy",
                created_at=now,
                completed_at=now,
            )
            for _ in range(max(0, writes - 1)):
                dal(dal.dsar_mail_outbox.id == int(row_id)).update(
                    token_enc=None, completed_at=svc.now_utc()
                )
            dal.commit()
        except Exception:
            dal.rollback()
            raise

    await asyncio.get_running_loop().run_in_executor(async_dal.executor, _job)


async def claim(async_dal: Any, dal: Any, *, limit: int = 1) -> list[Any]:
    """Atomically lease up to `limit` due rows; returns only the rows THIS caller won.

    The lease UPDATE re-asserts pending status, an expired/absent lease and the attempt counter
    we read, so two workers racing for one row cannot both win it. The winner stamps a fresh
    `lease_token` (also set on the returned row); every later write is guarded by it. The worker
    claims ONE row per iteration so no row waits out its lease behind a serial batch.
    """
    table = dal.dsar_mail_outbox
    now = svc.now_utc()
    candidates = await async_dal.select_async(
        dal(
            (table.status == int(OutboxStatus.PENDING))
            & (table.next_attempt_at <= now)
            & ((table.claimed_until == None) | (table.claimed_until < now))  # noqa: E711
        ),
        orderby=table.id,
        limitby=(0, limit),
    )
    won: list[Any] = []
    for row in candidates:
        token = uuid.uuid4().hex
        updated = await async_dal.update_async(
            (table.id == row.id)
            & (table.status == int(OutboxStatus.PENDING))
            & (table.attempts == row.attempts)
            & ((table.claimed_until == None) | (table.claimed_until < now)),  # noqa: E711
            claimed_until=now + timedelta(seconds=lease_seconds()),
            attempts=int(row.attempts) + 1,
            lease_token=token,
        )
        if updated:
            row.attempts = int(row.attempts) + 1
            row.lease_token = token
            won.append(row)
    return won


def _leased(dal: Any, item: Any) -> Any:
    """Query matching `item` only while THIS worker still holds its lease (token + pending)."""
    table = dal.dsar_mail_outbox
    return (
        (table.id == item.id)
        & (table.status == int(OutboxStatus.PENDING))
        & (table.lease_token == item.lease_token)
    )


async def _renew(async_dal: Any, dal: Any, item: Any, seconds: float) -> bool:
    """Extend the lease (to cover the whole send); False = lease lost, the caller must stop."""
    updated = await async_dal.update_async(
        _leased(dal, item), claimed_until=svc.now_utc() + timedelta(seconds=seconds)
    )
    return bool(updated)


async def _finish(
    async_dal: Any, dal: Any, item: Any, status: OutboxStatus, *, reason: str | None = None
) -> bool:
    """Move a leased row to a terminal state (drops the sealed token); False if lease lost."""
    now = svc.now_utc()
    updated = await async_dal.update_async(
        _leased(dal, item),
        status=int(status),
        token_enc=None,
        claimed_until=None,
        lease_token=None,
        last_reason=reason,
        completed_at=now,
    )
    if updated:
        created = svc.aware(item.created_at)
        elapsed = max(0.0, (now - created).total_seconds()) if created else 0.0
        label = {"outcome": status.name.lower()}
        queue_latency.record(elapsed, label)
        queue_results.add(1, label)
    return bool(updated)


def _is_transient(exc: BaseException) -> bool:
    """Transient: sender-classified `transient`, or a raw timeout / connection-level failure.

    Config, permanent and unknown errors fail the row at once. `asyncio.wait_for` raises a bare
    `TimeoutError` (no `kind`), which must retry rather than permanently fail the mail.
    """
    if getattr(exc, "kind", None) == "transient":
        return True
    return isinstance(exc, TimeoutError | ConnectionError)


def _send_timeout(notifier: DsarNotifier) -> float:
    """Hard cap for one attempt: the sender's own total timeout (+ margin), bounded."""
    settings = getattr(notifier, "settings", None)
    own = getattr(settings, "total_timeout", None) or getattr(notifier, "total_timeout", None)
    return min(float(own) + 5.0, 900.0) if own else 60.0


async def _discard_reason(async_dal: Any, dal: Any, item: Any, row: Any | None) -> str | None:
    """Why this mail must not be sent (closed request, purged contact), else None."""
    if row is None or row.status != int(svc.RequestStatus.VERIFICATION_PENDING):
        return "request_closed"
    if not row.contact_email_enc:
        return "contact_purged"
    expires = svc.aware(row.verification_expires_at)
    if expires is None or expires <= svc.now_utc():
        return "token_expired"
    return None


async def process(async_dal: Any, dal: Any, item: Any, notifier: DsarNotifier) -> str:
    """Send one leased row, record the outcome.

    Returns sent|sent_unconfirmed|retry|failed|discarded|lost.
    """
    requests = dal.dsar_requests
    rows = await async_dal.select_async(dal(requests.id == int(item.request_id)))
    row = rows[0] if rows else None
    skip = await _discard_reason(async_dal, dal, item, row)
    token: str | None = None
    if skip is None and item.token_enc:
        try:
            token = crypto.decrypt(item.token_enc, row_id=item.public_id)
        except crypto.CryptoError:
            skip = "token_unreadable"
    if (
        skip is None
        and row is not None
        and (token is None or row.verification_token_hash != crypto.hash_token(token).hex())
    ):
        skip = "superseded"
    if skip is not None or token is None or row is None:
        logger.debug("dsar.outbox.discard", extra={"reason": skip or "no_token"})
        await _finish(async_dal, dal, item, OutboxStatus.DISCARDED, reason=skip or "no_token")
        return "discarded"
    resend = int(item.kind) == int(MailKind.RESEND)
    timeout = _send_timeout(notifier)
    if not await _renew(async_dal, dal, item, max(lease_seconds(), timeout + 30.0)):
        return "lost"  # another worker owns the row now: no send, no state change
    try:
        url = svc.build_verify_url(token, item.tenant_slug)
        if url is None:
            raise NotifierUnavailable("DSAR_VERIFY_URL_BASE is not configured")
        email = crypto.decrypt(row.contact_email_enc, row_id=row.public_id)
        await asyncio.wait_for(notifier.send_verification(to_email=email, verify_url=url), timeout)
    except Exception as exc:  # noqa: BLE001 - classified below; never fails the worker loop
        reason = svc._failure_reason(exc)  # noqa: SLF001 - bounded, PII-free enum
        attempts = int(item.attempts)
        if _is_transient(exc) and attempts < max_attempts():
            delay = _RNG.uniform(0, min(_BACKOFF_CAP, backoff_base() * 2 ** (attempts - 1)))
            released = await async_dal.update_async(
                _leased(dal, item),
                claimed_until=None,
                last_reason=reason,
                next_attempt_at=svc.now_utc() + timedelta(seconds=delay),
            )
            if not released:
                return "lost"
            logger.warning(
                "dsar.outbox.retry",
                extra={"request_id": int(item.request_id), "attempt": attempts, "reason": reason},
            )
            return "retry"
        if not await _finish(async_dal, dal, item, OutboxStatus.FAILED, reason=reason):
            return "lost"
        # Class + bounded reason only: no exc_info/str(exc) (could carry the email or token).
        logger.error(
            "dsar.verification_send_failed",
            extra={
                "request_id": int(item.request_id),
                "error_type": type(exc).__name__,
                "reason": reason,
                "attempts": attempts,
            },
        )
        telemetry.verification_outcomes.add(1, {"outcome": "send_failed"})
        await svc.record_event(
            async_dal,
            dal,
            tenant_id=int(item.tenant_id),
            request_id=int(item.request_id),
            event_type=svc.EventType.VERIFICATION_SEND_FAILED,
            actor_kind=svc.ActorKind.SYSTEM,
            detail={"reason": reason, "attempts": attempts},
        )
        return "failed"
    return await _record_sent(async_dal, dal, item, row, resend)


async def _record_sent(async_dal: Any, dal: Any, item: Any, row: Any, resend: bool) -> str:
    """Record a send SMTP accepted. The audit event goes FIRST so it survives a failed finish.

    A DB failure here must not look like "nothing happened": the event is written before the
    terminal UPDATE, and an unrecoverable finish is logged as `sent_unconfirmed` (the row stays
    leased and is re-sent after expiry: at-least-once; a duplicate event/mail is harmless).
    """
    try:
        await svc.record_event(
            async_dal,
            dal,
            tenant_id=int(item.tenant_id),
            request_id=int(item.request_id),
            event_type=(
                svc.EventType.VERIFICATION_RESENT if resend else svc.EventType.VERIFICATION_SENT
            ),
            actor_kind=svc.ActorKind.SYSTEM,
        )
        finished = False
        for attempt in range(3):
            try:
                finished = await _finish(async_dal, dal, item, OutboxStatus.SENT)
                break
            except Exception:  # noqa: BLE001 - bounded retry of the terminal write
                if attempt == 2:
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))
        if not finished:
            return "lost"
        await async_dal.update_async(
            dal.dsar_requests.id == row.id, verification_sent_at=svc.now_utc()
        )
    except Exception as exc:  # noqa: BLE001 - mail already left: surface, never swallow silently
        logger.error(
            "dsar.outbox.sent_unconfirmed",
            extra={"request_id": int(item.request_id), "error_type": type(exc).__name__},
        )
        telemetry.verification_outcomes.add(1, {"outcome": "sent_unconfirmed"})
        return "sent_unconfirmed"
    telemetry.verification_outcomes.add(1, {"outcome": "resent" if resend else "sent"})
    return "sent"


async def depth(async_dal: Any, dal: Any) -> int:
    """Pending rows (also published to the queue-depth gauge)."""
    global _depth_value  # noqa: PLW0603 - gauge state, single writer
    count = int(
        await async_dal.count_async(dal.dsar_mail_outbox.status == int(OutboxStatus.PENDING))
    )
    _depth_value = count
    return count


async def prune(async_dal: Any, dal: Any) -> int:
    """Delete terminal rows older than the retention window (tokens are already nulled)."""
    table = dal.dsar_mail_outbox
    cutoff = svc.now_utc() - timedelta(days=retention_days())
    deleted = await async_dal.delete_async(
        table.status.belongs([int(s) for s in _TERMINAL])
        & (table.completed_at != None)  # noqa: E711
        & (table.completed_at <= cutoff)
    )
    return int(deleted or 0)


async def run_once(
    async_dal: Any, dal: Any, notifier: DsarNotifier | Callable[[], DsarNotifier]
) -> int:
    """One worker pass: lease due rows, send each, refresh the gauge. Returns rows handled."""
    bind_dsar_tables(dal)
    handled = 0
    while True:
        batch = await claim(async_dal, dal, limit=1)
        if not batch:
            break
        sender = notifier() if callable(notifier) else notifier
        for item in batch:
            outcome = await process(async_dal, dal, item, sender)
            handled += 1
            logger.debug("dsar.outbox.processed", extra={"outcome": outcome})
    await depth(async_dal, dal)
    return handled


async def run_worker_loop(
    async_dal: Any,
    dal: Any,
    *,
    app: Any,
    is_ready: Callable[[], bool],
) -> None:
    """Replica-local worker: wakes on enqueue or every poll interval; a failed pass is retried.

    On cancellation (shutdown) the in-flight send is allowed to finish only via the lease: an
    interrupted row is re-claimed by any replica once its lease expires, so nothing is lost.
    """
    _validate_env()
    waker = asyncio.Event()
    _wakers.add(waker)
    last_prune = 0.0
    try:
        while True:
            try:
                if is_ready():
                    await run_once(async_dal, dal, lambda: get_notifier(app))
                    if time.monotonic() - last_prune > 3600:
                        last_prune = time.monotonic()
                        await prune(async_dal, dal)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the worker must never take the process down
                logger.exception("dsar.outbox.pass_failed")
            # Timeout is the NORMAL path (poll tick): wait on the waker without raising.
            waiter = asyncio.ensure_future(waker.wait())
            try:
                await asyncio.wait({waiter}, timeout=poll_seconds())
            finally:
                waiter.cancel()
            waker.clear()
    finally:
        _wakers.discard(waker)
