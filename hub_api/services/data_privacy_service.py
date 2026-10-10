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
there is deliberately no code path anywhere in this port that accepts a
caller-supplied user id for either operation.

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
from datetime import UTC, datetime
from typing import Any

import bcrypt

from services.audit_service import report_write_failure
from services.errors import bad_request, not_found, unauthorized


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


async def _export_message_activity(async_dal: Any, dal: Any, user_id: int) -> list[dict[str, Any]]:
    rows = await async_dal.select_async(
        dal(dal.activity_message_events.hub_user_id == user_id),
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


async def _export_watch_activity(async_dal: Any, dal: Any, user_id: int) -> list[dict[str, Any]]:
    rows = await async_dal.select_async(
        dal(dal.activity_watch_sessions.hub_user_id == user_id),
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


async def _export_chat_messages(async_dal: Any, dal: Any, user_id: int) -> list[dict[str, Any]]:
    rows = await async_dal.select_async(
        dal(dal.hub_chat_messages.sender_hub_user_id == user_id),
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
    async_dal: Any, dal: Any, *, user_id: int
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
    """Gather every export source for `user_id`; a failing source is reported, not fatal.

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
        ("message_activity", _export_message_activity(async_dal, dal, user_id)),
        ("watch_activity", _export_watch_activity(async_dal, dal, user_id)),
        ("chat_messages", _export_chat_messages(async_dal, dal, user_id)),
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

    Mirrors `requestDataDeletion()` step-for-step: password confirmation
    only if the account has one, sequential deletes across every table
    holding this user's PII, in-place anonymization of `hub_users` (the
    row is KEPT for FK integrity, matching Node), then an always-written
    audit record in `data_deletion_requests` (success or best-effort
    failure), matching Node's "record failure, then re-raise the original
    error" shape. Not wrapped in an explicit pydal transaction -- no
    service in this codebase uses one yet (`AsyncDAL` exposes no
    transaction primitive); this is the same commit model every other
    write in this port already relies on, not a gap introduced here.
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

    now = datetime.now(UTC)
    counts: dict[str, int] = {}
    try:
        counts["profiles"] = await async_dal.delete_async(
            dal.hub_user_profiles.hub_user_id == user_id
        )
        counts["sessions"] = await async_dal.delete_async(dal.hub_sessions.user_id == user_id)
        # Node's own WHERE (`user_identifier = (SELECT email FROM hub_users
        # WHERE id = $1)`) never matches when email IS NULL -- SQL NULL
        # comparison, not an omission. Mirrored directly rather than
        # issuing a query that would silently match nothing anyway.
        counts["temp_passwords"] = (
            await async_dal.delete_async(dal.hub_temp_passwords.user_identifier == email)
            if email
            else 0
        )
        counts["passkeys"] = await async_dal.delete_async(dal.user_passkeys.user_id == user_id)
        counts["message_events"] = await async_dal.delete_async(
            dal.activity_message_events.hub_user_id == user_id
        )
        counts["watch_sessions"] = await async_dal.delete_async(
            dal.activity_watch_sessions.hub_user_id == user_id
        )
        await async_dal.update_async(
            dal.hub_users.id == user_id,
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

        await async_dal.insert_async(
            dal.data_deletion_requests,
            hub_user_id=user_id,
            requested_at=now,
            completed_at=now,
            status="completed",
            deletion_scope=counts,
        )
    except Exception as exc:
        try:
            await async_dal.insert_async(
                dal.data_deletion_requests,
                hub_user_id=user_id,
                requested_at=now,
                status="failed",
                error_detail=str(exc),
            )
        except Exception as record_exc:
            # Recording the *failed* deletion attempt failed too. Make that loud (ERROR + metric +
            # traceback) but never let it mask the original error, which is re-raised below.
            report_write_failure(
                category="privacy",
                action="privacy.deletion_failure_record",
                chain_id=None,
                attempts=1,
                exc=record_exc,
            )
        raise

    return False, True
