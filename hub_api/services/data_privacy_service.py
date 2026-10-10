"""Self-service GDPR/CCPA data subject rights -- ported from `dataPrivacyController.js`.

Art. 15 access / Art. 20 portability: `export_user_data()`. Art. 17
erasure: `request_data_deletion()`. Art. 16 rectification is
`profile_service.update_my_profile()` (already ported, M1) -- not
duplicated here.

Every entry point takes `user_id` as a plain argument resolved by the
BLUEPRINT exclusively from `services.current_user.get_current_user_id()`
(the bearer JWT's `sub` claim) -- never from a request parameter/body.
This is the single most important invariant for this group: DSAR export
or deletion for another user is the textbook IDOR/BOLA case
(security.md, `hub_api/PORTING.md` Auth pattern "self-service" row), and
there is deliberately no SELF-SERVICE code path (`export_user_data()` /
`request_data_deletion()`, `blueprints/v1/data_privacy.py`) that accepts a
caller-supplied user id for either operation. The one exception is the
Enterprise tenant-admin DSAR console (`admin_data_privacy_service.py`):
it reuses `collect_user_data()` / `anonymize_user_data()` below with a
caller-supplied id, but only after tenant-membership proof, the
`compliance.bulk_dsar` gate, and a mandatory audit row -- see that
module. Self-service stays ungated in every tier (critical-rules.md:
statutory rights are never tier-gated).

Every export source lists its columns explicitly (never `dal.<table>.
ALL`) -- mirrors `admin/hub_module/backend/src/utils/userDataExport.js`'s
own module docstring: an access request discloses personal data, but a
response containing `password_hash`, a session token, a passkey
`public_key`, or a verification/reset token would hand the requester
credential material, including an attacker who reached an authenticated
session via some other means. Uses the pydal query builder throughout
(never raw SQL with `%s` placeholders) -- Gotcha #1, `hub_api/
PORTING.md`: this repo's tests run against sqlite, and `AsyncDAL`'s raw-
SQL helpers hardcode psycopg2's paramstyle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import bcrypt
from flask_core.db_errors import describe_db_error, log_db_error
from opentelemetry import trace

from services.bundle_telemetry import get_meter, get_tracer
from services.errors import bad_request, not_found, unauthorized

logger = logging.getLogger(__name__)

#: Tables erasure deliberately NEVER deletes from. GDPR Art. 5(2)
#: (accountability) obliges the controller to be able to DEMONSTRATE that
#: consent was obtained and that a deletion request was honoured, so the
#: consent record, its change log, the platform audit trail and the
#: deletion-request ledger outlive the data subject's own data. Pinned by
#: `tests/test_data_privacy_erasure.py::TestRetention`; adding a delete
#: against any of these is a compliance regression, not a cleanup.
ERASURE_RETAINED_TABLES: tuple[str, ...] = (
    "cookie_consent",
    "cookie_audit_log",
    "audit_log",
    "data_deletion_requests",
)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


async def _verify_password(password: str, password_hash: str) -> bool:
    def _verify() -> bool:
        return bcrypt.checkpw(password.encode(), password_hash.encode())

    return await asyncio.to_thread(_verify)


async def _export_account(async_dal: Any, dal: Any, user_id: int) -> list[dict[str, Any]]:
    rows = await async_dal.select_async(
        dal(dal.hub_users.id == user_id),
        dal.hub_users.id,
        dal.hub_users.display_name,
        dal.hub_users.username,
        dal.hub_users.email,
        dal.hub_users.avatar_url,
        dal.hub_users.is_super_admin,
        dal.hub_users.is_vendor,
        dal.hub_users.email_verified,
        dal.hub_users.last_login,
        dal.hub_users.created_at,
        dal.hub_users.updated_at,
        dal.hub_users.is_active,
    )
    return [
        {
            "id": r.id,
            "display_name": r.display_name,
            "username": r.username,
            "email": r.email,
            "avatar_url": r.avatar_url,
            "is_super_admin": bool(r.is_super_admin),
            "is_vendor": bool(r.is_vendor),
            "email_verified": bool(r.email_verified),
            "last_login": _iso(r.last_login),
            "created_at": _iso(r.created_at),
            "updated_at": _iso(r.updated_at),
            "is_active": bool(r.is_active),
        }
        for r in rows
    ]


async def _export_profile(async_dal: Any, dal: Any, user_id: int) -> list[dict[str, Any]]:
    rows = await async_dal.select_async(
        dal(dal.hub_user_profiles.hub_user_id == user_id),
        dal.hub_user_profiles.display_name,
        dal.hub_user_profiles.bio,
        dal.hub_user_profiles.location,
        dal.hub_user_profiles.location_city,
        dal.hub_user_profiles.location_state,
        dal.hub_user_profiles.location_country,
        dal.hub_user_profiles.website_url,
        dal.hub_user_profiles.custom_avatar_url,
        dal.hub_user_profiles.banner_url,
        dal.hub_user_profiles.visibility,
        dal.hub_user_profiles.show_activity,
        dal.hub_user_profiles.show_communities,
        dal.hub_user_profiles.updated_at,
    )
    return [
        {
            "display_name": r.display_name,
            "bio": r.bio,
            "location": r.location,
            "location_city": r.location_city,
            "location_state": r.location_state,
            "location_country": r.location_country,
            "website_url": r.website_url,
            "custom_avatar_url": r.custom_avatar_url,
            "banner_url": r.banner_url,
            "visibility": r.visibility,
            "show_activity": bool(r.show_activity) if r.show_activity is not None else None,
            "show_communities": (
                bool(r.show_communities) if r.show_communities is not None else None
            ),
            "updated_at": _iso(r.updated_at),
        }
        for r in rows
    ]


async def _export_linked_identities(async_dal: Any, dal: Any, user_id: int) -> list[dict[str, Any]]:
    rows = await async_dal.select_async(
        dal(dal.hub_user_identities.hub_user_id == user_id),
        dal.hub_user_identities.platform,
        dal.hub_user_identities.platform_user_id,
        dal.hub_user_identities.platform_username,
        dal.hub_user_identities.avatar_url,
        dal.hub_user_identities.is_primary,
        dal.hub_user_identities.linked_at,
        dal.hub_user_identities.last_used,
    )
    return [
        {
            "platform": r.platform,
            "platform_user_id": r.platform_user_id,
            "platform_username": r.platform_username,
            "avatar_url": r.avatar_url,
            "is_primary": bool(r.is_primary),
            "linked_at": _iso(r.linked_at),
            "last_used": _iso(r.last_used),
        }
        for r in rows
    ]


async def _export_sessions(async_dal: Any, dal: Any, user_id: int) -> list[dict[str, Any]]:
    rows = await async_dal.select_async(
        dal(dal.hub_sessions.user_id == user_id),
        dal.hub_sessions.platform,
        dal.hub_sessions.platform_username,
        dal.hub_sessions.is_active,
        dal.hub_sessions.expires_at,
        dal.hub_sessions.revoked_at,
        dal.hub_sessions.created_at,
    )
    return [
        {
            "platform": r.platform,
            "platform_username": r.platform_username,
            "is_active": bool(r.is_active),
            "expires_at": _iso(r.expires_at),
            "revoked_at": _iso(r.revoked_at),
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]


async def _export_passkeys(async_dal: Any, dal: Any, user_id: int) -> list[dict[str, Any]]:
    rows = await async_dal.select_async(
        dal(dal.user_passkeys.user_id == user_id),
        dal.user_passkeys.device_name,
        dal.user_passkeys.sign_count,
        dal.user_passkeys.created_at,
        dal.user_passkeys.last_used_at,
    )
    return [
        {
            "device_name": r.device_name,
            "sign_count": r.sign_count,
            "created_at": _iso(r.created_at),
            "last_used_at": _iso(r.last_used_at),
        }
        for r in rows
    ]


def _community_scope(community_field: Any, community_ids: Collection[int] | None) -> Any:
    """Return an extra query clause limiting rows to `community_ids`, or `None` for no limit.

    `None` (the self-service default) means "every community" -- the data
    subject is entitled to all of their own data. A tenant-admin export
    passes the admin's own tenant's community ids so a community-keyed row
    belonging to a DIFFERENT tenant is never disclosed to this tenant's
    admin (security.md Tenant Isolation). An empty collection matches no rows.
    """
    if community_ids is None:
        return None
    return community_field.belongs(sorted(community_ids)) if community_ids else community_field < 0


async def _export_message_activity(
    async_dal: Any, dal: Any, user_id: int, community_ids: Collection[int] | None = None
) -> list[dict[str, Any]]:
    query = dal.activity_message_events.hub_user_id == user_id
    scope = _community_scope(dal.activity_message_events.community_id, community_ids)
    if scope is not None:
        query &= scope
    rows = await async_dal.select_async(
        dal(query),
        dal.activity_message_events.community_id,
        dal.activity_message_events.platform,
        dal.activity_message_events.platform_username,
        dal.activity_message_events.channel_id,
        dal.activity_message_events.created_at,
    )
    return [
        {
            "community_id": r.community_id,
            "platform": r.platform,
            "platform_username": r.platform_username,
            "channel_id": r.channel_id,
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]


async def _export_watch_activity(
    async_dal: Any, dal: Any, user_id: int, community_ids: Collection[int] | None = None
) -> list[dict[str, Any]]:
    query = dal.activity_watch_sessions.hub_user_id == user_id
    scope = _community_scope(dal.activity_watch_sessions.community_id, community_ids)
    if scope is not None:
        query &= scope
    rows = await async_dal.select_async(
        dal(query),
        dal.activity_watch_sessions.community_id,
        dal.activity_watch_sessions.platform,
        dal.activity_watch_sessions.platform_username,
        dal.activity_watch_sessions.channel_id,
        dal.activity_watch_sessions.session_start,
        dal.activity_watch_sessions.session_end,
        dal.activity_watch_sessions.duration_seconds,
        dal.activity_watch_sessions.created_at,
    )
    return [
        {
            "community_id": r.community_id,
            "platform": r.platform,
            "platform_username": r.platform_username,
            "channel_id": r.channel_id,
            "session_start": _iso(r.session_start),
            "session_end": _iso(r.session_end),
            "duration_seconds": r.duration_seconds,
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]


async def _export_chat_messages(
    async_dal: Any, dal: Any, user_id: int, community_ids: Collection[int] | None = None
) -> list[dict[str, Any]]:
    query = dal.hub_chat_messages.sender_hub_user_id == user_id
    scope = _community_scope(dal.hub_chat_messages.community_id, community_ids)
    if scope is not None:
        query &= scope
    rows = await async_dal.select_async(
        dal(query),
        dal.hub_chat_messages.community_id,
        dal.hub_chat_messages.channel_name,
        dal.hub_chat_messages.sender_platform,
        dal.hub_chat_messages.sender_username,
        dal.hub_chat_messages.message_content,
        dal.hub_chat_messages.message_type,
        dal.hub_chat_messages.created_at,
    )
    return [
        {
            "community_id": r.community_id,
            "channel_name": r.channel_name,
            "sender_platform": r.sender_platform,
            "sender_username": r.sender_username,
            "message_content": r.message_content,
            "message_type": r.message_type,
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]


async def _export_cookie_consent(async_dal: Any, dal: Any, user_id: int) -> list[dict[str, Any]]:
    rows = await async_dal.select_async(
        dal(dal.cookie_consent.user_id == user_id),
        dal.cookie_consent.consent_id,
        dal.cookie_consent.preferences,
        dal.cookie_consent.consent_version,
        dal.cookie_consent.consent_method,
        dal.cookie_consent.ip_address,
        dal.cookie_consent.user_agent,
        dal.cookie_consent.consented_at,
        dal.cookie_consent.updated_at,
        dal.cookie_consent.expires_at,
    )
    return [
        {
            "consent_id": r.consent_id,
            "preferences": dict(r.preferences or {}),
            "consent_version": r.consent_version,
            "consent_method": r.consent_method,
            "ip_address": r.ip_address,
            "user_agent": r.user_agent,
            "consented_at": _iso(r.consented_at),
            "updated_at": _iso(r.updated_at),
            "expires_at": _iso(r.expires_at),
        }
        for r in rows
    ]


async def _export_deletion_requests(async_dal: Any, dal: Any, user_id: int) -> list[dict[str, Any]]:
    rows = await async_dal.select_async(
        dal(dal.data_deletion_requests.hub_user_id == user_id),
        dal.data_deletion_requests.requested_at,
        dal.data_deletion_requests.completed_at,
        dal.data_deletion_requests.status,
        dal.data_deletion_requests.deletion_scope,
    )
    return [
        {
            "requested_at": _iso(r.requested_at),
            "completed_at": _iso(r.completed_at),
            "status": r.status,
            "deletion_scope": dict(r.deletion_scope or {}),
        }
        for r in rows
    ]


async def collect_user_data(
    async_dal: Any,
    dal: Any,
    *,
    user_id: int,
    community_ids: Collection[int] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
    """Gather every export source for `user_id`; a failing source is reported, not fatal.

    `community_ids=None` (self-service) exports every community's rows;
    the tenant-admin DSAR console (`admin_data_privacy_service.py`) passes
    its own tenant's community ids so the three community-keyed sources
    (message/watch activity, chat messages) never include another
    tenant's rows. Account-level sources (profile, identities, sessions,
    passkeys, consent, deletion requests) are the subject's own identity
    record and are not community-scoped.

    Mirrors `userDataExport.js::collectUserData()` -- a partial export the
    subject can see is more useful than a 500, and silently omitting a
    table would understate what is held. Sequential (not
    `asyncio.gather`) to match Node's own sequential `for` loop.
    """
    sources: list[tuple[str, Any]] = [
        ("account", _export_account(async_dal, dal, user_id)),
        ("profile", _export_profile(async_dal, dal, user_id)),
        ("linked_identities", _export_linked_identities(async_dal, dal, user_id)),
        ("sessions", _export_sessions(async_dal, dal, user_id)),
        ("passkeys", _export_passkeys(async_dal, dal, user_id)),
        ("message_activity", _export_message_activity(async_dal, dal, user_id, community_ids)),
        ("watch_activity", _export_watch_activity(async_dal, dal, user_id, community_ids)),
        ("chat_messages", _export_chat_messages(async_dal, dal, user_id, community_ids)),
        ("cookie_consent", _export_cookie_consent(async_dal, dal, user_id)),
        ("deletion_requests", _export_deletion_requests(async_dal, dal, user_id)),
    ]
    data: dict[str, list[dict[str, Any]]] = {}
    failures: list[dict[str, str]] = []
    for key, coro in sources:
        try:
            data[key] = await coro
        except Exception as exc:  # noqa: BLE001 - a partial export beats a 500, see docstring
            failures.append({"source": key, "error": str(exc)})
    return data, failures


async def export_user_data(
    async_dal: Any, dal: Any, *, user_id: int
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
    """Export `user_id`'s personal data (GDPR Art. 15/20). `user_id` MUST be the caller's own."""
    exists = await async_dal.select_async(dal(dal.hub_users.id == user_id), dal.hub_users.id)
    if not exists:
        raise not_found("User not found")
    return await collect_user_data(async_dal, dal, user_id=user_id)


async def request_data_deletion(
    async_dal: Any, dal: Any, *, user_id: int, password: str | None
) -> tuple[bool, bool]:
    """Anonymize and delete personal data for `user_id`. Returns `(already_deleted, deleted)`.

    `user_id` MUST be the caller's own id -- see module docstring; there
    is no parameter here a caller could point at someone else's account.

    Mirrors `requestDataDeletion()`: password confirmation only if the
    account has one, then `anonymize_user_data()` -- the all-or-nothing
    erasure core (deletes across every table holding this user's PII,
    in-place anonymization of `hub_users`, and the `data_deletion_requests`
    audit record, in ONE database transaction).
    """
    rows = await async_dal.select_async(
        dal(dal.hub_users.id == user_id), dal.hub_users.email, dal.hub_users.password_hash
    )
    if not rows:
        raise not_found("User not found")
    email = rows[0].email
    password_hash = rows[0].password_hash

    if email and email.startswith(f"deleted_{user_id}@"):
        return True, False

    if password_hash:
        if not password:
            raise bad_request("Password confirmation required")
        if not await _verify_password(password, password_hash):
            raise unauthorized("Password confirmation failed")

    await anonymize_user_data(async_dal, dal, user_id=user_id, email=email)
    return False, True


@dataclass(slots=True, frozen=True)
class _ErasureInstruments:
    """The OTel instruments erasure reports through (created lazily, once)."""

    total: Any
    duration_ms: Any
    rows_deleted: Any


_INSTRUMENTS: _ErasureInstruments | None = None


def _instruments() -> _ErasureInstruments:
    """Return the erasure counter/histograms, creating them on first use.

    Lazy (not import-time) so a deployment's / test's MeterProvider is the one
    instruments bind to. Labels are `result` only -- never a user id (PII).
    """
    global _INSTRUMENTS
    if _INSTRUMENTS is None:
        meter = get_meter()
        _INSTRUMENTS = _ErasureInstruments(
            total=meter.create_counter(
                "waddles_hub_dsar_erasure_total", description="Art. 17 erasures, by result"
            ),
            duration_ms=meter.create_histogram(
                "waddles_hub_dsar_erasure_duration_ms",
                unit="ms",
                description="wall time of one all-or-nothing erasure transaction",
            ),
            rows_deleted=meter.create_histogram(
                "waddles_hub_dsar_erasure_rows",
                description="rows deleted by one erasure (excludes the in-place anonymization)",
            ),
        )
    return _INSTRUMENTS


def _rows_deleted(counts: dict[str, int]) -> int:
    """Total rows hard-deleted (the in-place `hub_users` anonymization is not a delete)."""
    return sum(v for k, v in counts.items() if k != "hub_users_anonymized")


def _record_erasure_metrics(result: str, started: float, counts: dict[str, int] | None) -> None:
    """Emit erasure metrics; a telemetry failure is logged, never raised into the request."""
    try:
        instruments = _instruments()
        labels = {"result": result}
        instruments.total.add(1, labels)
        instruments.duration_ms.record((time.perf_counter() - started) * 1000.0, labels)
        if counts is not None:
            instruments.rows_deleted.record(_rows_deleted(counts), labels)
    except Exception:  # noqa: BLE001 - telemetry must never fail an erasure
        logger.exception("dsar.erasure_metrics_failed")


def _sync_erase(dal: Any, *, user_id: int, email: str | None, now: datetime) -> dict[str, int]:
    """Erase `user_id` in ONE transaction: every delete, the anonymization and the audit row.

    Synchronous on purpose -- it MUST run as a single `run_in_executor()`
    job so every statement lands on the same pool thread and therefore the
    same thread-local pydal connection. `AsyncDAL.insert_async()` /
    `update_async()` / `delete_async()` each COMMIT inside their own
    executor job (#280), so composing them (what this function replaced)
    left a mid-way failure with the earlier deletes already durable and
    the account still un-anonymized: a half-erased subject, which is worse
    than neither. Here nothing is committed until the final statement has
    succeeded, and any failure rolls back everything (Art. 17 is
    all-or-nothing). `AsyncDAL.transaction_async()`'s own docstring and
    `services/community_loyalty.py` document this same pattern.

    Does NOT touch `ERASURE_RETAINED_TABLES` (consent / audit retention,
    Art. 5(2)). Returns the per-table row counts written to the audit row.
    """
    try:
        counts: dict[str, int] = {}
        counts["profiles"] = dal(dal.hub_user_profiles.hub_user_id == user_id).delete()
        counts["sessions"] = dal(dal.hub_sessions.user_id == user_id).delete()
        # Node's own WHERE (`user_identifier = (SELECT email FROM hub_users
        # WHERE id = $1)`) never matches when email IS NULL -- SQL NULL
        # comparison, not an omission. Mirrored directly rather than
        # issuing a query that would silently match nothing anyway.
        counts["temp_passwords"] = (
            dal(dal.hub_temp_passwords.user_identifier == email).delete() if email else 0
        )
        counts["passkeys"] = dal(dal.user_passkeys.user_id == user_id).delete()
        counts["message_events"] = dal(dal.activity_message_events.hub_user_id == user_id).delete()
        counts["watch_sessions"] = dal(dal.activity_watch_sessions.hub_user_id == user_id).delete()
        # GRC#1: Art. 17 erasure previously exported these (Art. 15) but never
        # deleted them, leaving the subject's message bodies, platform username
        # and avatar behind after a "completed" erasure.
        counts["chat_messages"] = dal(dal.hub_chat_messages.sender_hub_user_id == user_id).delete()
        dal(dal.hub_users.id == user_id).update(
            email=f"deleted_{user_id}@deleted.waddlebot",
            username=f"deleted_{user_id}",
            display_name=None,
            password_hash=None,
            avatar_url=None,
            email_verification_token=None,
            password_reset_token=None,
            is_active=False,
            updated_at=now,
        )
        counts["hub_users_anonymized"] = 1
        dal.data_deletion_requests.insert(
            hub_user_id=user_id,
            requested_at=now,
            completed_at=now,
            status="completed",
            deletion_scope=counts,
        )
        dal.commit()
        return counts
    except Exception:
        dal.rollback()
        raise


async def anonymize_user_data(async_dal: Any, dal: Any, *, user_id: int, email: str | None) -> None:
    """Delete/anonymize every PII-bearing row for `user_id`, atomically, and record the outcome.

    The shared erasure core: `request_data_deletion()` (self-service, after
    its password confirmation) and the tenant-admin DSAR console
    (`admin_data_privacy_service.py`, after ITS authorization + audit
    checks) both call this, so the two paths can never drift apart on what
    "erased" means. It performs NO authorization of its own -- the caller
    owns that, and MUST have already proven the caller may erase
    `user_id`.

    All-or-nothing (see `_sync_erase()`): on failure the data subject's
    rows are exactly as they were, a separate best-effort `"failed"` row is
    written to `data_deletion_requests`, and the ORIGINAL error is
    re-raised (a failure to record the failure never masks it). Consent
    and audit logs are retained (`ERASURE_RETAINED_TABLES`).
    """
    now = datetime.now(UTC)
    started = time.perf_counter()
    loop = asyncio.get_running_loop()
    logger.debug("dsar.erasure_started", extra={"hub_user_id": user_id, "has_email": bool(email)})
    try:
        # record_exception / set_status_on_exception OFF: the SDK would otherwise
        # stamp the raw exception message (row values -> PII) on the span at exit.
        with get_tracer().start_as_current_span(
            "hub.dsar.erase", record_exception=False, set_status_on_exception=False
        ) as span:
            try:
                counts = await loop.run_in_executor(
                    async_dal.executor,
                    lambda: _sync_erase(dal, user_id=user_id, email=email, now=now),
                )
            except Exception as exc:
                # Value-free status text (exception type / SQLSTATE only).
                span.set_status(trace.Status(trace.StatusCode.ERROR, describe_db_error(exc)))
                raise
            span.set_attribute("rows_deleted", _rows_deleted(counts))
    except Exception as exc:
        # `log_db_error` / `describe_db_error` emit the exception TYPE + SQLSTATE
        # only: a driver message routinely echoes bound row values (PII), and
        # `error_detail` is retained in `data_deletion_requests` indefinitely.
        log_db_error(logger, f"dsar.erasure_failed hub_user_id={user_id}", exc)
        _record_erasure_metrics("failed", started, None)
        try:
            await async_dal.insert_async(
                dal.data_deletion_requests,
                hub_user_id=user_id,
                requested_at=now,
                status="failed",
                error_detail=describe_db_error(exc),
            )
        except Exception as record_exc:  # noqa: BLE001 - best-effort; must not mask the original
            log_db_error(
                logger, f"dsar.erasure_failure_record_failed hub_user_id={user_id}", record_exc
            )
        raise
    _record_erasure_metrics("completed", started, counts)
    logger.info("dsar.erasure_completed", extra={"hub_user_id": user_id, "deleted_counts": counts})
