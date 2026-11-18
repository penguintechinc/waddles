"""DSAR request inbox -- storage, SLA clock, verification, expiry and audit trail.

Level 1 of the two-level DSAR system (statutory: every tier, never tier-gated). Requests are
received (logged-in user, public form, admin-entered), stored in `dsar_requests`, and tenant
admins act on them manually, re-using the existing single-subject export / erase / do-not-sell
logic (`admin_data_privacy_service.run_single_dsar`). Every state change appends a row to the
append-only `dsar_request_events`.

Enum codes (INT everywhere; documented in `openapi/v1.yaml`):

| Enum    | Codes                                                                                   |
|---------|-----------------------------------------------------------------------------------------|
| type    | 1 export, 2 erase, 3 do_not_sell, 4 consent_withdrawal, 5 correction, 6 other            |
| status  | 1 received, 2 verification_pending, 3 verified, 4 in_progress, 5 completed, 6 rejected,  |
|         | 7 withdrawn, 8 expired                                                                   |
| regime  | 1 gdpr, 2 ccpa (CCPA/CPRA), 3 other                                                      |
| source  | 1 logged_in_user, 2 public_form, 3 admin_entered                                         |

SLA (statutory clock). `due_at` = clock start + ONE CALENDAR MONTH (GDPR Art. 12(3): the same day
of the next month, clamped to that month's last day, UTC; Jan 31 -> Feb 28/29) / 45 days (CCPA/CPRA) /
30 days (regime `other`, the pre-existing default). Clock start is `requested_at` for logged-in and admin-entered requests and
`verified_at` for public-form requests (the clock cannot start until the requester proved control
of the email; `requested_at` is still recorded). ONE extension per request, by an admin with a
written reason: GDPR +2 further calendar months (Art. 12(3), 3 months in total), CCPA/CPRA +45 days
(90 in total), other +30 days, added to `due_at` (`extended_until`). `due_state` is computed server-side: `overdue` when past the effective due date,
`due_soon` within 7 days, else `ok`; closed requests and unverified ones (no clock) have none.

Public-form trust model. A public submission is stored `verification_pending` and is NEVER
actionable until the emailed single-use token is consumed. Unverified requests auto-expire to the
terminal `expired` status after `DSAR_UNVERIFIED_EXPIRY_HOURS` (default 72) and their contact data
is purged. Requester contact data (email, message) is AES-GCM encrypted (`dsar_crypto`, via the repo's sealing vault), is shown
to admins only once verified, and is blanked `DSAR_CONTACT_RETENTION_DAYS` (default 30) after the
request closes. No PII in logs, spans, metrics or events.

Tenant isolation: every query is built from `_scoped(tenant_id)`; `tenant_id` always comes from the
validated JWT (admin/user routes) or the URL slug resolved to a tenant (public routes) -- never a
request body.

Automation seam (Level 2, Enterprise): `run_maintenance()` is the single periodic entry point;
rule-based auto-routing/auto-responding would hook `create_request()`'s post-insert and is
intentionally not built here (bulk stays Enterprise-gated by `waddles.compliance.bulk_dsar`).
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import Any

from services import admin_data_privacy_service as admin_svc
from services import dsar_crypto as crypto
from services import dsar_telemetry as telemetry
from services.dsar_notifier import NotifierUnavailable
from services.errors import ApiError, bad_request, conflict, not_found
from services.safe_parse import env_int
from services.schema import bind_dsar_tables

logger = logging.getLogger(__name__)

FEATURE_DSAR_INBOX = "waddles.dsar-inbox"


class RequestType(IntEnum):
    """What the data subject is asking for."""

    EXPORT = 1
    ERASE = 2
    DO_NOT_SELL = 3
    CONSENT_WITHDRAWAL = 4
    CORRECTION = 5
    OTHER = 6


class RequestStatus(IntEnum):
    """Lifecycle state. 5-8 are terminal."""

    RECEIVED = 1
    VERIFICATION_PENDING = 2
    VERIFIED = 3
    IN_PROGRESS = 4
    COMPLETED = 5
    REJECTED = 6
    WITHDRAWN = 7
    EXPIRED = 8


class Regime(IntEnum):
    """Legal regime that sets the response deadline."""

    GDPR = 1
    CCPA = 2
    OTHER = 3


class Source(IntEnum):
    """How the request reached the inbox."""

    LOGGED_IN_USER = 1
    PUBLIC_FORM = 2
    ADMIN_ENTERED = 3


class EventType(IntEnum):
    """Audit event kinds (append-only `dsar_request_events`)."""

    CREATED = 1
    VERIFICATION_SENT = 2
    VERIFICATION_SEND_FAILED = 3
    VERIFIED = 4
    STATUS_CHANGED = 5
    ASSIGNED = 6
    NOTES_UPDATED = 7
    EXTENDED = 8
    ACTION_EXECUTED = 9
    EXPIRED = 10
    CONTACT_PURGED = 11
    VERIFICATION_RESENT = 12
    SUBJECT_LINKED = 13
    CONTENT_REPLACED = 14


class ActorKind(IntEnum):
    """Who caused an event."""

    SYSTEM = 1
    ADMIN = 2
    SUBJECT = 3
    PUBLIC_REQUESTER = 4


TERMINAL_STATUSES = frozenset(
    {
        RequestStatus.COMPLETED,
        RequestStatus.REJECTED,
        RequestStatus.WITHDRAWN,
        RequestStatus.EXPIRED,
    }
)
#: Open AND verified: the only states in which an admin may act or progress a request.
ACTIONABLE_STATUSES = frozenset(
    {RequestStatus.RECEIVED, RequestStatus.VERIFIED, RequestStatus.IN_PROGRESS}
)
#: Statuses an admin may set via update (verification/expiry are system-driven).
ADMIN_SETTABLE_STATUSES = frozenset(
    {
        RequestStatus.IN_PROGRESS,
        RequestStatus.COMPLETED,
        RequestStatus.REJECTED,
        RequestStatus.WITHDRAWN,
    }
)
#: Closing an UNVERIFIED request is allowed (reject/withdraw) -- progressing it is not.
UNVERIFIED_CLOSABLE = frozenset({RequestStatus.REJECTED, RequestStatus.WITHDRAWN})

#: Day-counted regimes. GDPR is NOT here: it runs in calendar months (`add_months`).
BASE_DAYS = {Regime.CCPA: 45, Regime.OTHER: 30}
EXTENSION_DAYS = {Regime.CCPA: 45, Regime.OTHER: 30}
#: GDPR Art. 12(3): one calendar month, extendable by two further months.
GDPR_BASE_MONTHS = 1
GDPR_EXTENSION_MONTHS = 2
DUE_SOON_DAYS = 7

DUE_OK = "ok"
DUE_SOON = "due_soon"
DUE_OVERDUE = "overdue"
DUE_STATES = (DUE_OK, DUE_SOON, DUE_OVERDUE)

NOTES_MAX = 2000
MESSAGE_MAX = 2000
REASON_MAX = 1000
EMAIL_MAX = 254
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})+$"
)
_MAX_BACKDATE_DAYS = 365


def now_utc() -> datetime:
    """Current time, timezone-aware UTC (a single seam tests can monkeypatch)."""
    return datetime.now(UTC)


def aware(value: datetime | None) -> datetime | None:
    """Treat a naive datetime (sqlite) as UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, env_int(name, default)))


def unverified_expiry_hours() -> int:
    """`DSAR_UNVERIFIED_EXPIRY_HOURS` (default 72): unverified requests expire after this."""
    return _int_env("DSAR_UNVERIFIED_EXPIRY_HOURS", 72, 1, 24 * 30)


def contact_retention_days() -> int:
    """`DSAR_CONTACT_RETENTION_DAYS` (default 30): contact PII is blanked this long after close."""
    return _int_env("DSAR_CONTACT_RETENTION_DAYS", 30, 0, 3650)


def verify_token_ttl_minutes() -> int:
    """`DSAR_VERIFY_TOKEN_TTL_MINUTES` (default 60): lifetime of one emailed token."""
    return _int_env("DSAR_VERIFY_TOKEN_TTL_MINUTES", 60, 5, 24 * 60)


# ---------------------------------------------------------------------------------------------
# SLA math (pure)
# ---------------------------------------------------------------------------------------------


def add_months(moment: datetime, months: int) -> datetime:
    """`moment` + `months` calendar months, same day clamped to the target month's last day (UTC).

    Time of day is kept; a naive `moment` is treated as UTC. Jan 31 + 1 -> Feb 28 (29 in a leap
    year); Dec + 1 rolls the year.
    """
    moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    index = moment.year * 12 + (moment.month - 1) + months
    year, month0 = divmod(index, 12)
    last_day = calendar.monthrange(year, month0 + 1)[1]
    return moment.replace(year=year, month=month0 + 1, day=min(moment.day, last_day))


def extension_detail(regime: int) -> dict[str, int]:
    """Event detail naming how much the one extension adds (`extra_months` GDPR, else `extra_days`)."""
    if Regime(regime) is Regime.GDPR:
        return {"extra_months": GDPR_EXTENSION_MONTHS}
    return {"extra_days": EXTENSION_DAYS[Regime(regime)]}


def compute_due_at(regime: int, clock_start: datetime) -> datetime:
    """Statutory deadline: GDPR = one calendar month (clamped), CCPA 45 days, other 30 days."""
    if Regime(regime) is Regime.GDPR:
        return add_months(clock_start, GDPR_BASE_MONTHS)
    return clock_start + timedelta(days=BASE_DAYS[Regime(regime)])


def compute_extended_until(regime: int, due_at: datetime) -> datetime:
    """The single allowed extension: GDPR +2 calendar months, CCPA +45 days, other +30 days."""
    if Regime(regime) is Regime.GDPR:
        return add_months(due_at, GDPR_EXTENSION_MONTHS)
    return due_at + timedelta(days=EXTENSION_DAYS[Regime(regime)])


def effective_due(due_at: datetime | None, extended_until: datetime | None) -> datetime | None:
    """The date the clock currently runs to (`extended_until` once extended)."""
    return aware(extended_until) or aware(due_at)


def compute_due_state(
    due_at: datetime | None, extended_until: datetime | None, status: int, now: datetime
) -> str | None:
    """`ok` / `due_soon` (<= 7 days left, inclusive) / `overdue`; `None` if closed/no clock."""
    if RequestStatus(status) not in ACTIONABLE_STATUSES:
        return None
    due = effective_due(due_at, extended_until)
    if due is None:
        return None
    if now > due:
        return DUE_OVERDUE
    if due - now <= timedelta(days=DUE_SOON_DAYS):
        return DUE_SOON
    return DUE_OK


# ---------------------------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------------------------


def parse_enum(enum_cls: type[IntEnum], value: Any, label: str) -> Any:
    """Strict int -> enum (rejects bool, strings, unknown codes) or a 400."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise bad_request(f"{label} must be an integer code")
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = ", ".join(str(m.value) for m in enum_cls)
        raise bad_request(f"{label} must be one of: {allowed}") from exc


def parse_uuid(value: str, label: str = "id") -> str:
    """Canonical lowercase UUID string or a 400."""
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise bad_request(f"{label} must be a UUID") from exc


def validate_email(email: Any) -> str:
    """Return the stripped email or raise 400 (strict shape, length-capped)."""
    if not isinstance(email, str):
        raise bad_request("email is required")
    value = email.strip()
    if not value or len(value) > EMAIL_MAX or not _EMAIL_RE.match(value):
        raise bad_request("email is invalid")
    return value


def clean_text(value: str | None, label: str, maximum: int) -> str | None:
    """Trim + length-cap free text; control characters are rejected."""
    if value is None:
        return None
    text = value.strip()
    if len(text) > maximum:
        raise bad_request(f"{label} must be at most {maximum} characters")
    if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in text):
        raise bad_request(f"{label} contains invalid characters")
    return text or None


# ---------------------------------------------------------------------------------------------
# Data access (every query tenant-scoped)
# ---------------------------------------------------------------------------------------------


def _scoped(dal: Any, tenant_id: int) -> Any:
    """Base query for one tenant -- the ONLY way service code reaches `dsar_requests`."""
    return dal.dsar_requests.tenant_id == tenant_id


async def get_request(async_dal: Any, dal: Any, *, tenant_id: int, public_id: str) -> Any | None:
    """One request by `public_id` inside `tenant_id`, or `None` (cross-tenant ids look absent)."""
    try:
        canonical = str(uuid.UUID(public_id))
    except (ValueError, AttributeError, TypeError):
        return None
    rows = await async_dal.select_async(
        dal(_scoped(dal, tenant_id) & (dal.dsar_requests.public_id == canonical))
    )
    return rows[0] if rows else None


async def user_uuids(async_dal: Any, dal: Any, user_ids: set[int]) -> dict[int, str]:
    """`hub_users.id -> hub_users.uuid` for the ids (the only user reference allowed in the API)."""
    if not user_ids:
        return {}
    rows = await async_dal.select_async(
        dal(dal.hub_users.id.belongs(sorted(user_ids))), dal.hub_users.id, dal.hub_users.uuid
    )
    return {int(r.id): str(r.uuid) for r in rows if r.uuid}


#: ONE client-visible refusal for every "cannot link/resolve this user" cause (unknown, malformed,
#: other tenant): identical status, code and body so a caller cannot probe platform-wide UUIDs.
SUBJECT_NOT_FOUND_MESSAGE = "User not found"
ASSIGNEE_INVALID_MESSAGE = "assigneeId must be an admin of this tenant"
NIL_UUID = "00000000-0000-0000-0000-000000000000"


def subject_not_found() -> ApiError:
    """The single uniform 404 for an unresolvable / out-of-tenant subject."""
    return not_found(SUBJECT_NOT_FOUND_MESSAGE)


def assignee_invalid() -> ApiError:
    """The single uniform 400 for an unresolvable / non-admin / foreign assignee."""
    return bad_request(ASSIGNEE_INVALID_MESSAGE)


async def _resolve_uuid(async_dal: Any, dal: Any, user_uuid: Any) -> int | None:
    """`hub_users.id` for a UUID string; always runs the same single lookup (nil UUID on junk)."""
    canonical = admin_svc.canonical_uuid(user_uuid)
    found = await admin_svc.resolve_user_uuids(async_dal, dal, [canonical or NIL_UUID])
    return found.get(canonical) if canonical else None


async def user_id_for_uuid_in_tenant(
    async_dal: Any, dal: Any, tenant_id: int, user_uuid: Any
) -> int | None:
    """Internal id of `user_uuid` ONLY if the user is in `tenant_id`, else `None`.

    "In tenant" is the SAME definition the act path enforces (`admin_svc.resolve_scope`:
    community membership in a tenant community, or tenant admin), so link time and act time
    agree. Unknown, malformed and foreign-tenant users all return `None`; the same queries run
    in every case so cost does not reveal which one it was.
    """
    user_id = await _resolve_uuid(async_dal, dal, user_uuid)
    probe = user_id if user_id is not None else 0
    _, scopes = await admin_svc.resolve_scope(async_dal, dal, tenant_id=tenant_id, user_ids=[probe])
    return user_id if user_id is not None and scopes[probe].in_tenant else None


async def subject_in_tenant(async_dal: Any, dal: Any, tenant_id: int, user_id: int) -> bool:
    """Act-time re-check that an already-linked subject is (still) in `tenant_id`."""
    _, scopes = await admin_svc.resolve_scope(
        async_dal, dal, tenant_id=tenant_id, user_ids=[user_id]
    )
    return scopes[user_id].in_tenant


async def tenant_admin_id_for_uuid(
    async_dal: Any, dal: Any, tenant_id: int, user_uuid: Any
) -> int | None:
    """Internal id of `user_uuid` only if it is an admin of THIS tenant (else `None`, uniformly)."""
    user_id = await _resolve_uuid(async_dal, dal, user_uuid)
    probe = user_id if user_id is not None else 0
    is_admin = await _is_tenant_admin(async_dal, dal, tenant_id, probe)
    return user_id if user_id is not None and is_admin else None


async def _is_tenant_admin(async_dal: Any, dal: Any, tenant_id: int, user_id: int) -> bool:
    return (
        int(
            await async_dal.count_async(
                (dal.tenant_admins.tenant_id == tenant_id) & (dal.tenant_admins.user_id == user_id)
            )
        )
        > 0
    )


async def record_event(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    request_id: int,
    event_type: EventType,
    actor_kind: ActorKind,
    actor_user_id: int | None = None,
    from_status: int | None = None,
    to_status: int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one audit event. `detail` must be PII-free (enum names, counts, ids)."""
    await async_dal.insert_async(
        dal.dsar_request_events,
        public_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        request_id=request_id,
        event_type=int(event_type),
        actor_kind=int(actor_kind),
        actor_user_id=actor_user_id,
        from_status=from_status,
        to_status=to_status,
        detail=detail,
        created_at=now_utc(),
    )
    logger.debug(
        "dsar.event",
        extra={"event": event_type.name, "request_id": request_id, "tenant_id": tenant_id},
    )


async def load_events(async_dal: Any, dal: Any, *, tenant_id: int, request_id: int) -> list[Any]:
    """Audit trail for one request, oldest first (tenant-scoped)."""
    return list(
        await async_dal.select_async(
            dal(
                (dal.dsar_request_events.tenant_id == tenant_id)
                & (dal.dsar_request_events.request_id == request_id)
            ),
            orderby=dal.dsar_request_events.id,
        )
    )


# ---------------------------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------------------------


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _build_request_fields(
    *,
    tenant_id: int,
    request_type: RequestType,
    regime: Regime,
    source: Source,
    subject_user_id: int | None,
    requested_at: datetime | None,
    email: str | None,
    message: str | None,
    notes: str | None,
) -> tuple[dict[str, Any], str | None]:
    """Row values for a new request + the raw verification token (public form only)."""
    now = now_utc()
    started = requested_at or now
    public_id = str(uuid.uuid4())
    fields: dict[str, Any] = {
        "public_id": public_id,
        "tenant_id": tenant_id,
        "request_type": int(request_type),
        "regime": int(regime),
        "source": int(source),
        "requested_at": started,
        "subject_user_id": subject_user_id,
        "response_notes": notes,
        "created_at": now,
        "updated_at": now,
    }
    token: str | None = None
    if source is Source.PUBLIC_FORM:
        token = _new_token()
        fields.update(
            status=int(RequestStatus.VERIFICATION_PENDING),
            expires_at=started + timedelta(hours=unverified_expiry_hours()),
            verification_token_hash=crypto.hash_token(token).hex(),
            verification_expires_at=now + timedelta(minutes=verify_token_ttl_minutes()),
            verification_sent_at=None,
        )
    else:
        fields.update(status=int(RequestStatus.RECEIVED), due_at=compute_due_at(regime, started))
    if email is not None:
        fields.update(
            contact_email_enc=crypto.encrypt(email, row_id=public_id),
            contact_email_hash=crypto.email_fingerprint(email).hex(),
        )
    if message:
        fields.update(message_enc=crypto.encrypt(message, row_id=public_id))
    return fields, token


def _created_event_fields(
    *,
    tenant_id: int,
    request_id: int,
    actor_kind: ActorKind,
    actor_user_id: int | None,
    fields: dict[str, Any],
    source: Source,
    request_type: RequestType,
    regime: Regime,
) -> dict[str, Any]:
    """Event-row values for the CREATED event (PII-free detail)."""
    return {
        "public_id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "request_id": request_id,
        "event_type": int(EventType.CREATED),
        "actor_kind": int(actor_kind),
        "actor_user_id": actor_user_id,
        "from_status": None,
        "to_status": fields["status"],
        "detail": {
            "source": source.name.lower(),
            "type": request_type.name.lower(),
            "regime": regime.name.lower(),
        },
        "created_at": now_utc(),
    }


def note_request_created(source: Source, request_type: RequestType, tenant_id: int) -> None:
    """Post-commit telemetry for a created request (counter + debug log)."""
    telemetry.requests_created.add(
        1, {"source": source.name.lower(), "type": request_type.name.lower()}
    )
    logger.debug(
        "dsar.request_created",
        extra={
            "source": source.name.lower(),
            "type": request_type.name.lower(),
            "tenant_id": tenant_id,
        },
    )


async def create_request(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    request_type: RequestType,
    regime: Regime,
    source: Source,
    actor_kind: ActorKind,
    actor_user_id: int | None = None,
    subject_user_id: int | None = None,
    requested_at: datetime | None = None,
    email: str | None = None,
    message: str | None = None,
    notes: str | None = None,
) -> tuple[Any, str | None]:
    """Insert a request (+ CREATED event). Returns `(row, verification_token | None)`.

    Public-form requests start `verification_pending` with NO clock (`due_at` NULL), a hashed
    single-use token, and an `expires_at`; everything else starts `received` with the clock running
    from `requested_at`. The raw token is returned once for emailing and never stored. The public
    route uses `create_public_request_sync` (one transaction with the outbox row) instead.
    """
    fields, token = _build_request_fields(
        tenant_id=tenant_id,
        request_type=request_type,
        regime=regime,
        source=source,
        subject_user_id=subject_user_id,
        requested_at=requested_at,
        email=email,
        message=message,
        notes=notes,
    )
    with telemetry.span(
        "dsar.create_request", source=source.name.lower(), type=request_type.name.lower()
    ):
        row_id = await async_dal.insert_async(dal.dsar_requests, **fields)
        await async_dal.insert_async(
            dal.dsar_request_events,
            **_created_event_fields(
                tenant_id=tenant_id,
                request_id=int(row_id),
                actor_kind=actor_kind,
                actor_user_id=actor_user_id,
                fields=fields,
                source=source,
                request_type=request_type,
                regime=regime,
            ),
        )
    note_request_created(source, request_type, tenant_id)
    rows = await async_dal.select_async(dal(dal.dsar_requests.id == int(row_id)))
    return rows[0], token


def create_public_request_sync(
    dal: Any,
    *,
    tenant_id: int,
    request_type: RequestType,
    regime: Regime,
    email: str,
    message: str | None,
) -> tuple[Any, str]:
    """Insert a public-form request + CREATED event WITHOUT committing (transaction `work` body).

    Raises the driver's integrity error when the partial unique index (alembic 0124) already holds
    an open request for this tenant+email+type: the caller retries via `rotate_pending_public_sync`.
    """
    fields, token = _build_request_fields(
        tenant_id=tenant_id,
        request_type=request_type,
        regime=regime,
        source=Source.PUBLIC_FORM,
        subject_user_id=None,
        requested_at=None,
        email=email,
        message=message,
        notes=None,
    )
    assert token is not None  # nosec B101 - public requests always carry a token
    row_id = dal.dsar_requests.insert(**fields)
    dal.dsar_request_events.insert(
        **_created_event_fields(
            tenant_id=tenant_id,
            request_id=int(row_id),
            actor_kind=ActorKind.PUBLIC_REQUESTER,
            actor_user_id=None,
            fields=fields,
            source=Source.PUBLIC_FORM,
            request_type=request_type,
            regime=regime,
        )
    )
    row = dal(dal.dsar_requests.id == int(row_id)).select().first()
    return row, token


def is_integrity_violation(exc: BaseException | None) -> bool:
    """True when `exc` (or anything it chains from) is a DB unique/integrity violation."""
    seen = 0
    while exc is not None and seen < 6:
        if any(c.__name__ in ("IntegrityError", "UniqueViolation") for c in type(exc).__mro__):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def check_backdate(received_at: datetime | None) -> datetime | None:
    """Admin-entered `receivedAt` must not be in the future nor older than a year."""
    if received_at is None:
        return None
    received = aware(received_at)
    assert received is not None  # nosec B101 - narrowing only
    now = now_utc()
    if received > now + timedelta(minutes=5):
        raise bad_request("receivedAt cannot be in the future")
    if received < now - timedelta(days=_MAX_BACKDATE_DAYS):
        raise bad_request("receivedAt is more than one year in the past")
    return received


async def find_open_duplicate(
    async_dal: Any, dal: Any, *, tenant_id: int, subject_user_id: int, request_type: RequestType
) -> bool:
    """True if the same user already has an OPEN request of this type (stops inbox flooding)."""
    open_codes = [int(s) for s in (*ACTIONABLE_STATUSES, RequestStatus.VERIFICATION_PENDING)]
    return (
        int(
            await async_dal.count_async(
                _scoped(dal, tenant_id)
                & (dal.dsar_requests.subject_user_id == subject_user_id)
                & (dal.dsar_requests.request_type == int(request_type))
                & dal.dsar_requests.status.belongs(open_codes)
            )
        )
        > 0
    )


# ---------------------------------------------------------------------------------------------
# Verification (public form)
# ---------------------------------------------------------------------------------------------


_URL_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")


def build_verify_url(token: str, tenant_slug: str | None = None) -> str | None:
    """Link emailed to the requester, or `None` if `DSAR_VERIFY_URL_BASE` is unset.

    Shape: `<base>?tenant=<slug>#token=<token>`. The token rides in the fragment (never sent to
    servers/proxies or in a Referer; the verify page reads it from there) and `tenant` lets the
    page offer a resend. A slug that is not a plain slug is dropped rather than put in the URL.
    """
    base = os.environ.get("DSAR_VERIFY_URL_BASE", "").strip()
    if not base.startswith(("https://", "http://")) or "#" in base:
        return None
    query = ""
    if tenant_slug and _URL_SLUG_RE.match(tenant_slug):
        query = f"{'&' if '?' in base else '?'}tenant={tenant_slug}"
    return f"{base}{query}#token={token}"


def _failure_reason(exc: BaseException) -> str:
    """Bounded, PII-free reason for the send-failed event (a sender's own enum, else the type)."""
    reason = getattr(exc, "reason", None) if isinstance(exc, NotifierUnavailable) else None
    return reason if isinstance(reason, str) and len(reason) <= 40 else type(exc).__name__


async def _pending_by_token(async_dal: Any, dal: Any, token: str, now: datetime) -> Any | None:
    """The `verification_pending` row whose unexpired token hashes to `token`, else `None`."""
    if not token or len(token) > 256 or not token.isascii():
        return None
    rows = await async_dal.select_async(
        dal(
            (dal.dsar_requests.verification_token_hash == crypto.hash_token(token).hex())
            & (dal.dsar_requests.status == int(RequestStatus.VERIFICATION_PENDING))
        )
    )
    row = rows[0] if rows else None
    if (
        row is None
        or aware(row.verification_expires_at) is None
        or aware(row.verification_expires_at) < now  # type: ignore[operator]
        or (aware(row.expires_at) is not None and aware(row.expires_at) < now)  # type: ignore[operator]
    ):
        return None
    return row


@dataclass(slots=True, frozen=True)
class VerifyOutcome:
    """What redeeming a token WOULD commit: the single source for the preview and the consume.

    `message` is the plaintext the request will hold afterwards (`None` when absent). `commit` is
    the column set the claiming UPDATE adds: empty when no draft exists, the draft's content plus
    the draft-clearing columns when the draft is bound to the token, and only the draft-clearing
    columns (a discard, never a commit) for a draft bound to some other token.
    """

    regime: int
    message: str | None
    content_changed: bool
    commit: dict[str, Any]


def verify_outcome(row: Any, token_hash: str) -> VerifyOutcome:
    """Compute what the request's content becomes if `token_hash` is redeemed (pure, no writes).

    A draft counts only when `pending_token_hash` equals the redeemed hash (constant-time); any
    other draft is discarded. Preview and consume both call this so they cannot drift.
    """
    committed = crypto.decrypt(row.message_enc, row_id=row.public_id) if row.message_enc else None
    if row.pending_regime is None:
        return VerifyOutcome(int(row.regime), committed, False, {})
    clear: dict[str, Any] = {
        "pending_message_enc": None,
        "pending_regime": None,
        "pending_token_hash": None,
    }
    bound = row.pending_token_hash is not None and hmac_equal(
        str(row.pending_token_hash), token_hash
    )
    if not bound:
        return VerifyOutcome(int(row.regime), committed, False, clear)
    draft = (
        crypto.decrypt(row.pending_message_enc, row_id=row.public_id)
        if row.pending_message_enc
        else None
    )
    regime = int(row.pending_regime)
    return VerifyOutcome(
        regime,
        draft,
        draft != committed or regime != int(row.regime),
        {"message_enc": row.pending_message_enc, "regime": regime, **clear},
    )


async def preview_verification_token(async_dal: Any, dal: Any, token: str) -> Any | None:
    """NON-consuming look at a token: `(row, VerifyOutcome)` it would verify, or `None`.

    Backs the confirm page (`GET .../verify`): mail scanners and link prefetchers that follow the
    emailed link must not burn the single-use token, so only `consume_verification_token` (POST)
    changes state. The outcome is EXACTLY what the POST will commit (regime + message); callers
    expose the request type, that regime/message and the token expiry.
    """
    row = await _pending_by_token(async_dal, dal, token, now_utc())
    telemetry.verification_outcomes.add(1, {"outcome": "preview_ok" if row else "preview_invalid"})
    if row is None:
        return None
    return row, verify_outcome(row, crypto.hash_token(token).hex())


async def consume_verification_token(async_dal: Any, dal: Any, token: str) -> bool:
    """Verify a public request from its emailed token. Single use, short-lived, race-safe.

    Returns `True` exactly once per token. The row is matched by the token's hash, must still be
    `verification_pending`, neither the token nor the request may be expired, and the claiming
    UPDATE re-asserts the hash so two concurrent redemptions cannot both win. On success the
    status becomes `verified`, the token hash is cleared and the SLA clock starts now.
    """
    now = now_utc()
    row = await _pending_by_token(async_dal, dal, token, now)
    if row is None:
        telemetry.verification_outcomes.add(1, {"outcome": "invalid"})
        logger.debug("dsar.verification_invalid")
        return False
    token_hash = crypto.hash_token(token).hex()
    outcome = verify_outcome(row, token_hash)
    committed_regime = outcome.regime
    claimed = await async_dal.update_async(
        (dal.dsar_requests.id == row.id)
        & (dal.dsar_requests.verification_token_hash == token_hash)
        & (dal.dsar_requests.status == int(RequestStatus.VERIFICATION_PENDING)),
        status=int(RequestStatus.VERIFIED),
        verified_at=now,
        due_at=compute_due_at(committed_regime, now),
        expires_at=None,
        verification_token_hash=None,
        verification_expires_at=None,
        updated_at=now,
        **outcome.commit,
    )
    if not claimed:
        telemetry.verification_outcomes.add(1, {"outcome": "invalid"})
        return False
    if outcome.content_changed:
        await record_event(
            async_dal,
            dal,
            tenant_id=int(row.tenant_id),
            request_id=int(row.id),
            event_type=EventType.CONTENT_REPLACED,
            actor_kind=ActorKind.PUBLIC_REQUESTER,
            detail={
                "fields": ["message", "regime"],
                "message_present": outcome.message is not None,
            },
        )
    await record_event(
        async_dal,
        dal,
        tenant_id=int(row.tenant_id),
        request_id=int(row.id),
        event_type=EventType.VERIFIED,
        actor_kind=ActorKind.PUBLIC_REQUESTER,
        from_status=int(RequestStatus.VERIFICATION_PENDING),
        to_status=int(RequestStatus.VERIFIED),
    )
    telemetry.verification_outcomes.add(1, {"outcome": "verified"})
    logger.debug("dsar.verified", extra={"request_id": int(row.id)})
    return True


def rotate_pending_public_sync(
    dal: Any,
    *,
    tenant_id: int,
    email: str,
    request_type: RequestType,
    replace_content: tuple[str | None, Regime] | None = None,
) -> tuple[Any, str] | None:
    """New token for the newest still-pending request of `email` AND `request_type` (no commit).

    Matches by the keyed email fingerprint (no plaintext lookup) and ONLY the given type, so a
    requester can never be sent a link that verifies a different request (e.g. an attacker's
    pending ERASE for the same address). `None` when nothing matching is pending. The old token
    stops working (its hash is overwritten); the mail only ever goes to the email's owner.

    `replace_content=(message, regime)` (a fresh submit reusing the row) NEVER touches the committed
    `message_enc`/`regime`: the new content is stored as a DRAFT (`pending_message_enc` encrypted under
    the row's public_id, NULL when absent; `pending_regime`) tied to the token minted here, and
    `consume_verification_token` commits it only when that latest token is redeemed, i.e. by the
    mailbox owner. The draft is bound to this token by `pending_token_hash`. A bare resend passes
    `None` and CLEARS any draft (an earlier submitter's content must not ride a link it did not
    create).
    """
    now = now_utc()
    rows = dal(
        _scoped(dal, tenant_id)
        & (dal.dsar_requests.contact_email_hash == crypto.email_fingerprint(email).hex())
        & (dal.dsar_requests.request_type == int(request_type))
        & (dal.dsar_requests.status == int(RequestStatus.VERIFICATION_PENDING))
        & (dal.dsar_requests.expires_at > now)
    ).select(orderby=~dal.dsar_requests.id, limitby=(0, 1))
    if not rows:
        return None
    row = rows[0]
    token = _new_token()
    new_hash = crypto.hash_token(token).hex()
    content: dict[str, Any] = {
        "pending_message_enc": None,
        "pending_regime": None,
        "pending_token_hash": None,
    }
    if replace_content is not None:
        message, regime = replace_content
        content = {
            "pending_message_enc": (
                crypto.encrypt(message, row_id=row.public_id) if message else None
            ),
            "pending_regime": int(regime),
            "pending_token_hash": new_hash,
        }
    updated = dal(
        (dal.dsar_requests.id == row.id)
        & (dal.dsar_requests.status == int(RequestStatus.VERIFICATION_PENDING))
    ).update(
        verification_token_hash=new_hash,
        verification_expires_at=now + timedelta(minutes=verify_token_ttl_minutes()),
        updated_at=now,
        **content,
    )
    if not updated:
        return None
    return row, token


# ---------------------------------------------------------------------------------------------
# Admin queries / updates
# ---------------------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ListFilters:
    """Validated inbox filters. `status=None` means "default view" (see `list_requests`)."""

    status: int | None = None
    request_type: int | None = None
    regime: int | None = None
    due_state: str | None = None
    assignee_user_id: int | None = None
    sort: str = "requestedAt"
    descending: bool = True
    limit: int = 25
    offset: int = 0


#: Hidden from the default view: unverified requests (no verified contact, no clock) and expired.
HIDDEN_BY_DEFAULT = (int(RequestStatus.VERIFICATION_PENDING), int(RequestStatus.EXPIRED))


async def list_requests(
    async_dal: Any, dal: Any, *, tenant_id: int, filters: ListFilters, now: datetime
) -> tuple[list[Any], int]:
    """Filtered, paginated, stably-sorted page + total. Always tenant-scoped.

    Unverified (`verification_pending`) and `expired` requests are only listed when `status`
    explicitly asks for them. Sort ties break on `id` so pages never overlap or skip rows.
    """
    table = dal.dsar_requests
    query = _scoped(dal, tenant_id)
    if filters.status is not None:
        query &= table.status == filters.status
    else:
        query &= ~table.status.belongs(list(HIDDEN_BY_DEFAULT))
    if filters.request_type is not None:
        query &= table.request_type == filters.request_type
    if filters.regime is not None:
        query &= table.regime == filters.regime
    if filters.assignee_user_id is not None:
        query &= table.assignee_user_id == filters.assignee_user_id
    if filters.due_state is not None:
        eff = table.extended_until.coalesce(table.due_at)
        open_codes = [int(s) for s in ACTIONABLE_STATUSES]
        query &= table.status.belongs(open_codes) & (eff != None)  # noqa: E711 - pydal NOT NULL
        soon = now + timedelta(days=DUE_SOON_DAYS)
        if filters.due_state == DUE_OVERDUE:
            query &= eff < now
        elif filters.due_state == DUE_SOON:
            query &= (eff >= now) & (eff <= soon)
        else:
            query &= eff > soon
    sort_field = table.due_at if filters.sort == "dueAt" else table.requested_at
    primary = ~sort_field if filters.descending else sort_field
    tiebreak = ~table.id if filters.descending else table.id
    total = int(await async_dal.count_async(query))
    rows = await async_dal.select_async(
        dal(query),
        orderby=primary | tiebreak,
        limitby=(filters.offset, filters.offset + filters.limit),
    )
    return list(rows), total


def reveal_contact(row: Any) -> bool:
    """Admins may see requester contact data only for verified-or-better, not-yet-purged rows."""
    if row.contact_email_enc is None or row.contact_purged_at is not None:
        return False
    return RequestStatus(row.status) not in (
        RequestStatus.VERIFICATION_PENDING,
        RequestStatus.EXPIRED,
    )


def decrypt_contact(row: Any) -> tuple[str | None, str | None]:
    """`(email, message)` for a row the admin may see; `(None, None)` otherwise."""
    if not reveal_contact(row):
        return None, None
    email = crypto.decrypt(row.contact_email_enc, row_id=row.public_id)
    message = None
    if row.message_enc:
        message = crypto.decrypt(row.message_enc, row_id=row.public_id)
    return email, message


def require_actionable(row: Any) -> None:
    """409 unless the request is verified/open: unverified and closed requests refuse action."""
    status = RequestStatus(row.status)
    if status is RequestStatus.VERIFICATION_PENDING:
        raise ApiError("The requester has not verified their email yet", 409, "DSAR_UNVERIFIED")
    if status not in ACTIONABLE_STATUSES:
        raise ApiError("The request is closed", 409, "DSAR_REQUEST_CLOSED")


async def _purge_contact(async_dal: Any, dal: Any, row_id: int) -> None:
    """Blank every stored requester contact field and the admin notes (idempotent)."""
    await async_dal.update_async(
        dal.dsar_requests.id == row_id,
        contact_email_enc=None,
        contact_email_hash=None,
        message_enc=None,
        pending_message_enc=None,
        pending_regime=None,
        pending_token_hash=None,
        response_notes=None,  # admin free text may quote the requester: purged with the contact data
        verification_token_hash=None,
        verification_expires_at=None,
        contact_purged_at=now_utc(),
    )


def next_version(row: Any) -> datetime:
    """A strictly newer `updated_at` than `row`'s: the row's optimistic-concurrency version.

    The DAL stores datetimes at SECOND precision, so plain `now` can equal the value a stale
    reader holds and a lost race would slip through. Bumping at least one second past the stored
    value guarantees every successful write changes the version.
    """
    now = now_utc().replace(microsecond=0)
    previous = aware(row.updated_at)
    if previous is not None:
        previous = previous.replace(microsecond=0)
    return now if previous is None or now > previous else previous + timedelta(seconds=1)


def conflict_error() -> ApiError:
    """The ONE uniform 409 for a lost compare-and-set (stale read / concurrent admin)."""
    return ApiError(
        "The request was changed by someone else; reload and try again", 409, "DSAR_CONFLICT"
    )


async def _guarded_update(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    row: Any,
    changes: dict[str, Any],
    expected_status: int | None = None,
) -> None:
    """Compare-and-set UPDATE: applies only if status AND `updated_at` still match what was read.

    Zero rows changed means another writer won the race: 409 `DSAR_CONFLICT`, nothing written.
    """
    cond = (
        _scoped(dal, tenant_id)
        & (dal.dsar_requests.id == row.id)
        & (
            dal.dsar_requests.status
            == int(row.status if expected_status is None else expected_status)
        )
        & (dal.dsar_requests.updated_at == row.updated_at)
    )
    if not await async_dal.update_async(cond, **changes):
        telemetry.actions.add(1, {"action": "write", "outcome": "conflict"})
        raise conflict_error()


async def update_request(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    row: Any,
    admin_user_id: int,
    status: int | None = None,
    assignee_user_id: int | None = None,
    clear_assignee: bool = False,
    notes: str | None = None,
    notes_provided: bool = False,
    subject_user_id: int | None = None,
    subject_override_reason: str | None = None,
) -> Any:
    """Apply an admin update. Unverified requests may only be rejected/withdrawn; closed are frozen.

    Raises 409 `DSAR_UNVERIFIED` / `DSAR_REQUEST_CLOSED` / `DSAR_INVALID_TRANSITION`, 400 for an
    out-of-set status or a non-admin assignee. Every change appends an event. The write is a
    compare-and-set on the status and `updated_at` the caller read: a concurrent change loses with
    409 `DSAR_CONFLICT` instead of being silently overwritten.

    Subject link rules: a LOGGED_IN_USER request's subject is the caller and is never
    retargeted; an already-linked subject changes only with a written `subject_override_reason`
    (recorded in the event trail).
    """
    current = RequestStatus(row.status)
    if current in TERMINAL_STATUSES:
        raise ApiError("The request is closed", 409, "DSAR_REQUEST_CLOSED")
    now = now_utc()
    changes: dict[str, Any] = {"updated_at": next_version(row)}
    pending_events: list[dict[str, Any]] = []
    if status is not None:
        target = parse_enum(RequestStatus, status, "status")
        if target not in ADMIN_SETTABLE_STATUSES:
            raise bad_request("status must be one of: 4 (in_progress), 5, 6, 7")
        if current is RequestStatus.VERIFICATION_PENDING and target not in UNVERIFIED_CLOSABLE:
            raise ApiError("The requester has not verified their email yet", 409, "DSAR_UNVERIFIED")
        if target is current:
            raise ApiError("The request is already in that status", 409, "DSAR_INVALID_TRANSITION")
        changes["status"] = int(target)
        if target in TERMINAL_STATUSES:
            changes["completed_at"] = now
        pending_events.append(
            {
                "event_type": EventType.STATUS_CHANGED,
                "from_status": int(current),
                "to_status": int(target),
            }
        )
    if clear_assignee and assignee_user_id is not None:
        raise bad_request("clearAssignee and assigneeId are mutually exclusive")
    if assignee_user_id is not None:
        if not await _is_tenant_admin(async_dal, dal, tenant_id, assignee_user_id):
            raise assignee_invalid()
        changes["assignee_user_id"] = assignee_user_id
        pending_events.append({"event_type": EventType.ASSIGNED, "detail": {"cleared": False}})
    elif clear_assignee:
        changes["assignee_user_id"] = None
        pending_events.append({"event_type": EventType.ASSIGNED, "detail": {"cleared": True}})
    if notes_provided:
        changes["response_notes"] = notes
        pending_events.append({"event_type": EventType.NOTES_UPDATED})
    if subject_user_id is not None and subject_user_id != row.subject_user_id:
        link_event: dict[str, Any] = {"event_type": EventType.SUBJECT_LINKED}
        if Source(row.source) is Source.LOGGED_IN_USER:
            raise ApiError(
                "The subject of a self-service request cannot be changed",
                409,
                "DSAR_SUBJECT_LOCKED",
            )
        if row.subject_user_id is not None:
            reason = clean_text(subject_override_reason, "subjectOverrideReason", REASON_MAX)
            if not reason:
                raise ApiError(
                    "Changing a linked subject requires subjectOverrideReason",
                    409,
                    "DSAR_SUBJECT_OVERRIDE_REQUIRED",
                )
            link_event["detail"] = {"override": True, "reason": reason}
        changes["subject_user_id"] = subject_user_id
        pending_events.append(link_event)
    with telemetry.span("dsar.update_request"):
        await _guarded_update(async_dal, dal, tenant_id=tenant_id, row=row, changes=changes)
        for event in pending_events:
            await record_event(
                async_dal,
                dal,
                tenant_id=tenant_id,
                request_id=int(row.id),
                actor_kind=ActorKind.ADMIN,
                actor_user_id=admin_user_id,
                **event,
            )
        new_status = changes.get("status")
        if (
            new_status in (int(RequestStatus.REJECTED), int(RequestStatus.WITHDRAWN))
            and current is RequestStatus.VERIFICATION_PENDING
        ):
            await _purge_contact(async_dal, dal, int(row.id))
            await record_event(
                async_dal,
                dal,
                tenant_id=tenant_id,
                request_id=int(row.id),
                event_type=EventType.CONTACT_PURGED,
                actor_kind=ActorKind.SYSTEM,
                detail={"reason": "closed_unverified"},
            )
            telemetry.contacts_purged.add(1, {"reason": "closed_unverified"})
    updated = await get_request(async_dal, dal, tenant_id=tenant_id, public_id=row.public_id)
    assert updated is not None  # nosec B101 - row was just read in this tenant
    return updated


async def extend_request(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    row: Any,
    admin_user_id: int,
    reason: str,
) -> Any:
    """Use the request's one extension (GDPR +2 months / CCPA +45d / other +30d) with a reason."""
    require_actionable(row)
    if row.due_at is None:
        raise ApiError("The SLA clock has not started", 409, "DSAR_UNVERIFIED")
    if row.extended_until is not None:
        raise ApiError("The request was already extended once", 409, "DSAR_ALREADY_EXTENDED")
    cleaned = clean_text(reason, "reason", REASON_MAX)
    if not cleaned:
        raise bad_request("reason is required")
    due = aware(row.due_at)
    assert due is not None  # nosec B101 - checked above
    until = compute_extended_until(row.regime, due)
    claimed = await async_dal.update_async(
        _scoped(dal, tenant_id)
        & (dal.dsar_requests.id == row.id)
        & (dal.dsar_requests.status == int(row.status))
        & (dal.dsar_requests.updated_at == row.updated_at)
        & (dal.dsar_requests.extended_until == None),  # noqa: E711 - pydal IS NULL
        extended_until=until,
        extension_reason=cleaned,
        updated_at=next_version(row),
    )
    if not claimed:
        # Either another admin extended it first or the row changed under us: one uniform 409.
        raise conflict_error()
    await record_event(
        async_dal,
        dal,
        tenant_id=tenant_id,
        request_id=int(row.id),
        event_type=EventType.EXTENDED,
        actor_kind=ActorKind.ADMIN,
        actor_user_id=admin_user_id,
        detail=extension_detail(row.regime),
    )
    updated = await get_request(async_dal, dal, tenant_id=tenant_id, public_id=row.public_id)
    assert updated is not None  # nosec B101
    return updated


#: Request type -> the single-subject action `act` may execute (others are handled manually).
ACT_ACTION_FOR_TYPE = {
    RequestType.EXPORT: "export",
    RequestType.ERASE: "erase",
    RequestType.DO_NOT_SELL: "do_not_sell",
}


async def claim_for_action(async_dal: Any, dal: Any, *, tenant_id: int, row: Any) -> Any:
    """Take the single-flight claim on `row` BEFORE the act runs (compare-and-set on `updated_at`).

    Two admins clicking act on the same request both read the same `updated_at`; only one UPDATE
    matches, the other gets 409 `DSAR_CONFLICT` before anything irreversible runs. Returns the
    re-read row carrying the claim's `updated_at` (pass it to `mark_acted`).
    """
    await _guarded_update(
        async_dal, dal, tenant_id=tenant_id, row=row, changes={"updated_at": next_version(row)}
    )
    claimed = await get_request(async_dal, dal, tenant_id=tenant_id, public_id=row.public_id)
    assert claimed is not None  # nosec B101 - row was just read in this tenant
    return claimed


async def mark_acted(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    row: Any,
    admin_user_id: int,
    action: str,
    outcome: str,
    succeeded: bool,
    partial: bool = False,
    reason: str | None = None,
    detail: dict[str, Any] | None = None,
) -> Any:
    """Record an `act` execution (`reason` = true internal refusal cause, event detail only).

    `row` is the claimed row from `claim_for_action`. ONLY a fully successful erase/do-not-sell
    completes the request; an export moves it to in_progress (the admin completes it after
    delivering the data). A `partial` result (data returned but a source failed) also leaves the
    request in_progress so it is never closed as fulfilled; any other failure changes nothing but
    the audit trail. The status write is a compare-and-set: if the row changed during the action
    the status is left alone and the event records `status_change_skipped`.
    """
    now = now_utc()
    current = RequestStatus(row.status)
    to_status = current
    if succeeded:
        to_status = RequestStatus.IN_PROGRESS if action == "export" else RequestStatus.COMPLETED
    elif partial:
        to_status = RequestStatus.IN_PROGRESS
    changes: dict[str, Any] = {"updated_at": next_version(row)}
    if to_status is not current:
        changes["status"] = int(to_status)
        if to_status in TERMINAL_STATUSES:
            changes["completed_at"] = now
    applied = await async_dal.update_async(
        _scoped(dal, tenant_id)
        & (dal.dsar_requests.id == row.id)
        & (dal.dsar_requests.status == int(current))
        & (dal.dsar_requests.updated_at == row.updated_at),
        **changes,
    )
    event_detail: dict[str, Any] = {
        "action": action,
        "outcome": outcome,
        **({"reason": reason} if reason else {}),
        **(detail or {}),
    }
    if not applied:
        to_status = current
        event_detail["status_change_skipped"] = True
    await record_event(
        async_dal,
        dal,
        tenant_id=tenant_id,
        request_id=int(row.id),
        event_type=EventType.ACTION_EXECUTED,
        actor_kind=ActorKind.ADMIN,
        actor_user_id=admin_user_id,
        from_status=int(current),
        to_status=int(to_status),
        detail=event_detail,
    )
    telemetry.actions.add(1, {"action": action, "outcome": outcome})
    updated = await get_request(async_dal, dal, tenant_id=tenant_id, public_id=row.public_id)
    assert updated is not None  # nosec B101
    return updated


async def subject_email_matches(
    async_dal: Any, dal: Any, *, subject_user_id: int, row: Any
) -> bool:
    """True when the linked subject's ACCOUNT email equals the requester's verified email.

    Compared server-side by keyed fingerprint (`dsar_crypto.email_fingerprint` normalisation);
    neither address is returned or logged. False when either side is missing (purged contact,
    account without email): that case needs an explicit admin override.
    """
    if not row.contact_email_hash:
        return False
    rows = await async_dal.select_async(
        dal(dal.hub_users.id == subject_user_id), dal.hub_users.email
    )
    email = rows[0].email if rows else None
    if not isinstance(email, str) or not email.strip():
        return False
    return hmac_equal(crypto.email_fingerprint(email).hex(), str(row.contact_email_hash))


def hmac_equal(a: str, b: str) -> bool:
    """Constant-time string equality (fingerprints are hex)."""
    return secrets.compare_digest(a.encode(), b.encode())


# ---------------------------------------------------------------------------------------------
# Maintenance (expiry, contact purge, overdue gauge)
# ---------------------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class MaintenanceResult:
    """Counts from one maintenance pass (all >= 0)."""

    expired: int
    contacts_purged: int
    overdue: int


async def run_maintenance(
    async_dal: Any, dal: Any, *, now: datetime | None = None
) -> MaintenanceResult:
    """Expire stale unverified requests, blank retained contact PII, refresh the overdue gauge.

    Idempotent and safe to run from every replica. Called by the background loop and lazily by the
    admin list endpoint, so expiry never depends on a scheduler being alive.
    """
    bind_dsar_tables(dal)  # idempotent: the loop can run before any request binds the tables
    moment = now or now_utc()
    table = dal.dsar_requests
    stale = await async_dal.select_async(
        dal(
            (table.status == int(RequestStatus.VERIFICATION_PENDING))
            & (table.expires_at != None)  # noqa: E711 - pydal NOT NULL
            & (table.expires_at <= moment)
        )
    )
    expired_count = 0
    for row in stale:
        claimed = await async_dal.update_async(
            (table.id == row.id) & (table.status == int(RequestStatus.VERIFICATION_PENDING)),
            status=int(RequestStatus.EXPIRED),
            completed_at=moment,
            updated_at=moment,
        )
        if not claimed:
            continue
        await _purge_contact(async_dal, dal, int(row.id))
        await record_event(
            async_dal,
            dal,
            tenant_id=int(row.tenant_id),
            request_id=int(row.id),
            event_type=EventType.EXPIRED,
            actor_kind=ActorKind.SYSTEM,
            from_status=int(RequestStatus.VERIFICATION_PENDING),
            to_status=int(RequestStatus.EXPIRED),
            detail={"purged": True},
        )
        expired_count += 1
        telemetry.expired.add(1)
        telemetry.contacts_purged.add(1, {"reason": "expired"})
    cutoff = moment - timedelta(days=contact_retention_days())
    retained = await async_dal.select_async(
        dal(
            table.status.belongs([int(s) for s in TERMINAL_STATUSES])
            & (table.contact_purged_at == None)  # noqa: E711 - pydal IS NULL
            & (table.contact_email_enc != None)  # noqa: E711 - pydal NOT NULL
            & (table.completed_at != None)  # noqa: E711 - pydal NOT NULL
            & (table.completed_at <= cutoff)
        )
    )
    purged = 0
    for row in retained:
        await _purge_contact(async_dal, dal, int(row.id))
        await record_event(
            async_dal,
            dal,
            tenant_id=int(row.tenant_id),
            request_id=int(row.id),
            event_type=EventType.CONTACT_PURGED,
            actor_kind=ActorKind.SYSTEM,
            detail={"reason": "retention"},
        )
        purged += 1
        telemetry.contacts_purged.add(1, {"reason": "retention"})
    eff = table.extended_until.coalesce(table.due_at)
    overdue = int(
        await async_dal.count_async(
            table.status.belongs([int(s) for s in ACTIONABLE_STATUSES])
            & (eff != None)  # noqa: E711 - pydal NOT NULL
            & (eff < moment)
        )
    )
    telemetry.set_overdue(overdue)
    if expired_count or purged:
        logger.info(
            "dsar.maintenance",
            extra={"expired": expired_count, "purged": purged, "overdue": overdue},
        )
    return MaintenanceResult(expired=expired_count, contacts_purged=purged, overdue=overdue)


async def run_maintenance_loop(
    async_dal: Any, dal: Any, *, is_ready: Any, interval_seconds: float = 300.0
) -> None:
    """Loop around `run_maintenance`; a failed pass is logged and retried, never fatal."""
    while True:
        try:
            if is_ready():
                await run_maintenance(async_dal, dal)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - maintenance must never take the process down
            logger.exception("dsar.maintenance_failed")
        await asyncio.sleep(interval_seconds)


__all__ = [
    "ACT_ACTION_FOR_TYPE",
    "ADMIN_SETTABLE_STATUSES",
    "ActorKind",
    "EventType",
    "ListFilters",
    "Regime",
    "RequestStatus",
    "RequestType",
    "Source",
    "bad_request",
    "conflict",
    "not_found",
]
