"""Cookie consent + CCPA/CPRA opt-out.

Ported from `cookieConsentService.js`/`cookieConsentController.js`.

Public/self-service split matches Node's `routes/cookieConsent.js`
exactly: `get_or_create_consent`/`save_consent`/`get_current_policy`/
`get_policy_history` are called from PRE-AUTH routes (an anonymous
visitor, resolved via the `waddlebot_consent_id` cookie, is a normal
caller here -- GDPR Art. 7(1) requires being able to demonstrate
consent, and most banner interactions are unauthenticated).
`update_preferences`/`revoke_consent`/`get_audit_log` are self-service
(`user_id` from the bearer JWT only, never a request parameter) and
`create_policy_version`/`activate_policy_version` are super-admin-only.
See `blueprints/v1/cookie_consent.py`'s module docstring for the exact
decorator per route.

Global Privacy Control (`utils/globalPrivacyControl.js`): a `Sec-GPC: 1`
request header is itself a valid CPRA opt-out request and overrides
whatever the request body says -- `apply_gpc()` below. One-way on
purpose: absence of the header means "not opted out via this mechanism",
never "opted in" -- a missing header must never be read as consent.

Preference writes MERGE, never replace (`merge_preferences()` below):
`save_consent`/`update_preferences`/`revoke_consent` each carry only the
keys they own, so a stored `doNotSell` opt-out (a statutory CCPA/CPRA
right) is never reverted by an unrelated update.

Uses the pydal query builder throughout (Gotcha #1, `hub_api/
PORTING.md`) -- `save_consent()`'s select-then-branch replaces Node's
`INSERT ... ON CONFLICT (consent_id) DO UPDATE`, the same idiom
`profile_service.py`'s `update_my_profile()`/`upload_my_avatar()`
already use for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from quart import Request

from services.errors import not_found

#: Matches `routes/cookieConsent.js`'s cookie name verbatim -- the
#: frontend (`CookieConsentContext.jsx`) reads/writes this exact name.
CONSENT_COOKIE_NAME = "waddlebot_consent_id"
#: 12 months, matches Node's `res.cookie(..., { maxAge: 365 * 24 * 60 * 60 * 1000 })`.
CONSENT_COOKIE_MAX_AGE = 365 * 24 * 60 * 60

_AUDIT_CATEGORIES = ("functional", "analytics", "marketing")


def has_global_privacy_control(request: Request) -> bool:
    """True when the request carries a Global Privacy Control opt-out signal."""
    header = request.headers.get("Sec-GPC")
    return header in ("1", 1)


def apply_gpc(request: Request, preferences: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Apply a GPC signal to `preferences`, forcing `doNotSell` on and `marketing` off.

    Returns `preferences` unchanged (and `applied=False`) when no signal
    is present -- a user who explicitly opted in is never silently reset
    by a browser default.
    """
    if not has_global_privacy_control(request):
        return preferences, False
    return {**preferences, "doNotSell": True, "marketing": False}, True


def default_preferences() -> dict[str, Any]:
    """Default preferences for a brand-new visitor -- matches Node's `DEFAULT_CONSENT`."""
    return {
        "necessary": True,
        "functional": False,
        "analytics": False,
        "marketing": False,
        "doNotSell": False,
    }


def merge_preferences(stored: dict[str, Any] | None, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge `updates` over `stored` key-by-key; keys absent from `updates` are kept.

    Every write that touches the stored `preferences` object goes through
    here so a payload that does not carry a key (`doNotSell`, any future
    field) can never silently erase it. A CCPA/CPRA opt-out reverted by an
    unrelated update is a statutory-rights violation, not a cosmetic bug.
    Only a key the caller explicitly supplies is changed.
    """
    return {**(stored or {}), **updates}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


@dataclass(slots=True, frozen=True)
class ConsentRecord:
    """One resolved consent record.

    Shared shape across getConsent/saveConsent/updatePreferences/revokeConsent.

    A byte-compatible SUPERSET of each individual Node response (fields a
    specific Node method omits are simply `None`/`False` here) -- safe
    because no live frontend caller reads a field this port adds on top
    of what that specific endpoint returns; see `blueprints/v1/
    cookie_consent.py`'s module docstring for the endpoint-by-endpoint
    audit.
    """

    consent_id: str | None
    user_id: int | None
    preferences: dict[str, Any]
    version: str
    consented_at: str | None = None
    expires_at: str | None = None
    updated_at: str | None = None
    requires_update: bool = False


async def get_or_create_consent(
    async_dal: Any,
    dal: Any,
    *,
    user_id: int | None,
    consent_id: str | None,
    current_version: str,
) -> ConsentRecord | None:
    """Resolve the most recent consent record for `user_id` or `consent_id`.

    Returns `None` (not a default record) when no record exists, or when
    neither identifier is given -- the caller (blueprint) builds the
    anonymous-visitor default inline, matching the controller-level
    branch in Node, not this service.
    """
    if user_id is not None:
        rows = await async_dal.select_async(
            dal(dal.cookie_consent.user_id == user_id),
            orderby=~dal.cookie_consent.updated_at,
            limitby=(0, 1),
        )
    elif consent_id:
        rows = await async_dal.select_async(
            dal(dal.cookie_consent.consent_id == consent_id),
            orderby=~dal.cookie_consent.updated_at,
            limitby=(0, 1),
        )
    else:
        return None

    if not rows:
        return None
    row = rows[0]
    return ConsentRecord(
        consent_id=row.consent_id,
        user_id=row.user_id,
        preferences=dict(row.preferences or {}),
        version=row.consent_version,
        consented_at=_iso(row.consented_at),
        expires_at=_iso(row.expires_at),
        updated_at=_iso(row.updated_at),
        requires_update=row.consent_version != current_version,
    )


async def save_consent(
    async_dal: Any,
    dal: Any,
    *,
    user_id: int | None,
    consent_id: str | None,
    preferences: dict[str, Any],
    consent_method: str,
    ip_address: str | None,
    user_agent: str | None,
    version: str,
) -> ConsentRecord:
    """Insert-or-update the consent record for `consent_id` (a fresh UUID if absent).

    `preferences` carries only the keys the caller actually specified. On
    update it is MERGED over the stored object (`merge_preferences`), so a
    save that omits `doNotSell` keeps a stored CCPA opt-out; an explicit
    `doNotSell=False` still wins (a deliberate opt-back-in). On insert the
    omitted keys fall back to `default_preferences()`.
    """
    final_consent_id = consent_id or str(uuid4())
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=365)

    existing = await async_dal.select_async(dal(dal.cookie_consent.consent_id == final_consent_id))
    if existing:
        await async_dal.update_async(
            dal.cookie_consent.consent_id == final_consent_id,
            user_id=user_id,
            preferences=merge_preferences(dict(existing[0].preferences or {}), preferences),
            updated_at=now,
        )
    else:
        await async_dal.insert_async(
            dal.cookie_consent,
            user_id=user_id,
            consent_id=final_consent_id,
            preferences=merge_preferences(default_preferences(), preferences),
            consent_version=version,
            consent_method=consent_method,
            ip_address=ip_address,
            user_agent=user_agent,
            consented_at=now,
            updated_at=now,
            expires_at=expires_at,
        )

    await log_audit_event(
        async_dal,
        dal,
        consent_id=final_consent_id,
        user_id=user_id,
        action="ACCEPT",
        version=version,
        ip_address=ip_address,
        user_agent=user_agent,
    )

    rows = await async_dal.select_async(dal(dal.cookie_consent.consent_id == final_consent_id))
    row = rows[0]
    return ConsentRecord(
        consent_id=row.consent_id,
        user_id=row.user_id,
        preferences=dict(row.preferences or {}),
        version=row.consent_version,
        consented_at=_iso(row.consented_at),
        expires_at=_iso(row.expires_at),
        updated_at=_iso(row.updated_at),
    )


async def update_preferences(
    async_dal: Any, dal: Any, *, user_id: int, preferences: dict[str, Any]
) -> ConsentRecord:
    """Update specific consent categories for the authenticated user (own record only).

    `preferences` is MERGED over the stored object key-by-key
    (`merge_preferences`), never written as a whole-object replacement:
    keys the payload does not carry -- notably the CCPA/CPRA `doNotSell`
    opt-out (statutory right), and any other stored key -- are preserved.
    The Node original's full-object `SET preferences = $1` silently
    dropped `doNotSell` the next time this endpoint was used; this port
    deliberately does NOT reproduce that defect. A `Sec-GPC` opt-out is
    applied by the caller (`apply_gpc`) before this runs, so a GPC-forced
    `doNotSell=True` is carried in `preferences` and persisted here.
    """
    existing = await async_dal.select_async(
        dal(dal.cookie_consent.user_id == user_id),
        orderby=~dal.cookie_consent.updated_at,
        limitby=(0, 1),
    )
    if not existing:
        raise not_found("No consent record found for user")
    row = existing[0]
    previous = dict(row.preferences or {})
    merged = merge_preferences(previous, preferences)
    consent_id = row.consent_id
    version = row.consent_version
    now = datetime.now(UTC)

    await async_dal.update_async(
        dal.cookie_consent.user_id == user_id, preferences=merged, updated_at=now
    )

    for category in _AUDIT_CATEGORIES:
        if previous.get(category) != merged.get(category):
            await log_audit_event(
                async_dal,
                dal,
                consent_id=consent_id,
                user_id=user_id,
                action="UPDATE",
                category=category,
                previous_value=previous.get(category),
                new_value=merged.get(category),
                version=version,
            )

    rows = await async_dal.select_async(dal(dal.cookie_consent.user_id == user_id))
    updated = rows[0]
    return ConsentRecord(
        consent_id=updated.consent_id,
        user_id=updated.user_id,
        preferences=dict(updated.preferences or {}),
        version=updated.consent_version,
        consented_at=_iso(updated.consented_at),
        expires_at=_iso(updated.expires_at),
        updated_at=_iso(updated.updated_at),
    )


async def revoke_consent(async_dal: Any, dal: Any, *, user_id: int) -> ConsentRecord:
    """Revoke all non-essential cookies for the authenticated user (own record only).

    Only the cookie categories are reset -- the revoked categories are
    MERGED over the stored object (`merge_preferences`), so a CCPA/CPRA
    `doNotSell` opt-out (and any other stored key) survives a revoke.
    Revoking cookies must never silently re-enable the sale/sharing of a
    user's data.
    """
    existing = await async_dal.select_async(
        dal(dal.cookie_consent.user_id == user_id),
        orderby=~dal.cookie_consent.updated_at,
        limitby=(0, 1),
    )
    if not existing:
        raise not_found("No consent record found for user")
    consent_id = existing[0].consent_id
    version = existing[0].consent_version
    now = datetime.now(UTC)
    revoked_preferences = merge_preferences(
        dict(existing[0].preferences or {}),
        {"necessary": True, "functional": False, "analytics": False, "marketing": False},
    )

    await async_dal.update_async(
        dal.cookie_consent.user_id == user_id, preferences=revoked_preferences, updated_at=now
    )
    await log_audit_event(
        async_dal, dal, consent_id=consent_id, user_id=user_id, action="REVOKE", version=version
    )

    rows = await async_dal.select_async(dal(dal.cookie_consent.user_id == user_id))
    row = rows[0]
    return ConsentRecord(
        consent_id=row.consent_id,
        user_id=row.user_id,
        preferences=dict(row.preferences or {}),
        version=row.consent_version,
        updated_at=_iso(row.updated_at),
    )


async def log_audit_event(
    async_dal: Any,
    dal: Any,
    *,
    consent_id: str | None,
    user_id: int | None,
    action: str,
    version: str | None,
    category: str | None = None,
    previous_value: bool | None = None,
    new_value: bool | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Best-effort audit log write -- a logging failure must never break the main flow.

    Matches Node's own `.catch(() => {})` on `logAuditEvent()`.
    """
    try:
        await async_dal.insert_async(
            dal.cookie_audit_log,
            consent_id=consent_id,
            user_id=user_id,
            action=action,
            category=category,
            previous_value=previous_value,
            new_value=new_value,
            consent_version=version,
            ip_address=ip_address,
            user_agent=user_agent,
            created_at=datetime.now(UTC),
        )
    except Exception:  # noqa: BLE001, S110 - audit logging failure must not break the main flow
        pass


@dataclass(slots=True, frozen=True)
class PolicyRecord:
    """A `cookie_policy_versions` row.

    Snake_case -- these endpoints return raw DB rows in Node too.
    """

    id: int
    version: str
    content: str | None
    changes_summary: str | None
    is_active: bool
    effective_date: str | None
    created_at: str | None


@dataclass(slots=True, frozen=True)
class CreatedPolicy:
    """Fields Node's `INSERT ... RETURNING` actually returns -- no `content`/`changes_summary`."""

    id: int
    version: str
    is_active: bool
    effective_date: str | None
    created_at: str | None


async def get_current_policy(async_dal: Any, dal: Any) -> PolicyRecord | None:
    """Get the current active cookie policy, or `None` if none is active."""
    rows = await async_dal.select_async(
        dal(dal.cookie_policy_versions.is_active == True),  # noqa: E712
        limitby=(0, 1),
    )
    if not rows:
        return None
    row = rows[0]
    return PolicyRecord(
        id=row.id,
        version=row.version,
        content=row.content,
        changes_summary=row.changes_summary,
        is_active=bool(row.is_active),
        effective_date=_iso(row.effective_date),
        created_at=_iso(row.created_at),
    )


async def get_policy_history(
    async_dal: Any, dal: Any, *, limit: int, offset: int
) -> tuple[list[PolicyRecord], int]:
    """List policy versions, newest first, paginated."""
    rows = await async_dal.select_async(
        dal(dal.cookie_policy_versions),
        orderby=~dal.cookie_policy_versions.created_at,
        limitby=(offset, offset + limit),
    )
    # Bare Query, NOT `dal(dal.cookie_policy_versions)` -- `count_async`
    # self-wraps internally (`self.dal(query).count()`); a pre-wrapped Set
    # double-wraps and 500s ("near WHERE: syntax error" against sqlite).
    # Gotcha #1, hub_api/PORTING.md; `user_management_service.py::list_users`'s
    # `query = dal.hub_users.id > 0` is the same "count everything" idiom.
    total = await async_dal.count_async(dal.cookie_policy_versions.id > 0)
    versions = [
        PolicyRecord(
            id=r.id,
            version=r.version,
            content=None,
            changes_summary=r.changes_summary,
            is_active=bool(r.is_active),
            effective_date=_iso(r.effective_date),
            created_at=_iso(r.created_at),
        )
        for r in rows
    ]
    return versions, int(total)


@dataclass(slots=True, frozen=True)
class AuditLogEntry:
    """One `cookie_audit_log` row."""

    id: int
    action: str
    category: str | None
    previous_value: bool | None
    new_value: bool | None
    consent_version: str | None
    created_at: str | None


async def get_audit_log(
    async_dal: Any, dal: Any, *, user_id: int, limit: int, offset: int
) -> tuple[list[AuditLogEntry], int]:
    """List the authenticated user's own consent audit trail, newest first, paginated.

    `user_id` is the caller's own id (from the bearer JWT) -- there is no
    code path here that accepts a different user's id.
    """
    rows = await async_dal.select_async(
        dal(dal.cookie_audit_log.user_id == user_id),
        orderby=~dal.cookie_audit_log.created_at,
        limitby=(offset, offset + limit),
    )
    # Bare Query, not `dal(...)`-wrapped -- see get_policy_history()'s comment.
    total = await async_dal.count_async(dal.cookie_audit_log.user_id == user_id)
    entries = [
        AuditLogEntry(
            id=r.id,
            action=r.action,
            category=r.category,
            previous_value=r.previous_value,
            new_value=r.new_value,
            consent_version=r.consent_version,
            created_at=_iso(r.created_at),
        )
        for r in rows
    ]
    return entries, int(total)


async def create_policy_version(
    async_dal: Any,
    dal: Any,
    *,
    version: str,
    content: str,
    changes_summary: str | None,
    created_by: int | None,
) -> CreatedPolicy:
    """Deactivate the current policy and activate a newly-created version (super admin only)."""
    now = datetime.now(UTC)
    await async_dal.update_async(
        dal.cookie_policy_versions.is_active == True,  # noqa: E712
        is_active=False,
    )
    new_id = await async_dal.insert_async(
        dal.cookie_policy_versions,
        version=version,
        content=content,
        changes_summary=changes_summary,
        is_active=True,
        effective_date=now,
        created_by=created_by,
        created_at=now,
    )
    rows = await async_dal.select_async(dal(dal.cookie_policy_versions.id == new_id))
    row = rows[0]
    return CreatedPolicy(
        id=row.id,
        version=row.version,
        is_active=bool(row.is_active),
        effective_date=_iso(row.effective_date),
        created_at=_iso(row.created_at),
    )


async def activate_policy_version(async_dal: Any, dal: Any, *, version: str) -> None:
    """Activate `version`, deactivating every other version. Raises 404 if `version` is unknown."""
    existing = await async_dal.select_async(dal(dal.cookie_policy_versions.version == version))
    if not existing:
        raise not_found("Policy version not found")
    await async_dal.update_async(dal.cookie_policy_versions.version != version, is_active=False)
    await async_dal.update_async(dal.cookie_policy_versions.version == version, is_active=True)
