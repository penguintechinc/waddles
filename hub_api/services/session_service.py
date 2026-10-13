"""Login-session lifecycle: rotating refresh tokens, revocation triggers, admin session control.

The model, end to end (OWASP session management / SOC 2 CC6.1; ``docs/TOKEN_REVOCATION.md``):

* **Access token** -- a 15-minute HS256 JWT (``flask_core.auth``) carrying ``jti`` and ``sid``.
  Verified statelessly plus one revocation-store lookup (``flask_core.token_revocation``).
* **Login session** -- one ``hub_sessions`` row per login. ``session_token`` holds the *digest* of
  the current refresh token (never a bearer value -- a database leak yields no usable credential);
  ``expires_at`` is the sliding idle deadline, ``created_at`` anchors the absolute cap;
  ``is_active`` / ``revoked_at`` are the revocation state. No schema change.
* **Refresh token** -- ``rt1.<sid>.<nonce>.<tenant>.<tag>``: an opaque 256-bit nonce, the session
  id, the tenant the session was opened for and an HMAC tag over all of it. The tag lets the
  server tell a *genuine, previously issued* token (a replay: compromise) from garbage (a guess:
  just a 401) without storing history, so a stranger cannot revoke sessions by forging ids.
  Rotated on every use; the HttpOnly cookie is scoped to ``/api/v1/auth``.
* **Reuse detection** -- presenting a genuine token that is no longer current revokes the whole
  session (security.md: a reused refresh token is compromise), EXCEPT within a short grace window
  after the rotation, where the stale token simply gets a fresh access token. That grace covers the
  very common case of one SPA page firing several requests, all 401ing at the same expiry, all
  refreshing with the same cookie. The "just rotated" marker lives in the revocation store; if the
  store is unreachable the stale token is refused (401, not revoked) -- never guessed.
* **Revocation** -- session end writes BOTH halves: the database row (stops refresh) and the
  revocation store (``sid`` / per-user cutoff -- stops the already-issued access tokens
  immediately). A store outage leaves the database half in force, so the worst case is an access
  token living out its remaining <= 15 minutes, never a refresh.

Nothing here logs or returns a token, a nonce or a digest.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from flask_core.token_revocation import RevocationStoreError, get_token_revocation

from config import HubAPIConfig
from services.audit_http import record_session_revoked
from services.bundle_telemetry import bundle_span, get_meter
from services.errors import ApiError, unauthorized

logger = logging.getLogger(__name__)

REFRESH_TOKEN_VERSION = "rt1"  # noqa: S105 - a format tag, not a credential  # nosec B105
_DIGEST_PREFIX = "rt1:"
_KEY_LABEL = b"waddles.refresh-token.v1"
_MAX_REFRESH_TOKEN_CHARS = 512
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{32,64}")
_SESSION_ID_RE = re.compile(r"[0-9]{1,12}")
_B64_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")

_meter = get_meter()
_session_events = _meter.create_counter(
    "waddles_hub_session_events_total",
    unit="{event}",
    description=(
        "Login-session lifecycle events: issued, refreshed, refresh_grace_replay, "
        "refresh_reuse_revoked, refresh_rejected, revoked_session, revoked_user, "
        "revocation_store_failed."
    ),
)


@dataclass(slots=True, frozen=True)
class OpenedSession:
    """A freshly opened login session: its id and the refresh token to hand the client."""

    session_id: int
    refresh_token: str
    expires_at: datetime


@dataclass(slots=True, frozen=True)
class ParsedRefreshToken:
    """The authenticated fields of a refresh token (the HMAC tag already verified)."""

    session_id: int
    nonce: str
    tenant_slug: str


@dataclass(slots=True, frozen=True)
class RefreshContext:
    """A refresh token that authenticated against a live, unexpired session (nothing mutated)."""

    session_id: int
    user_id: int
    tenant_slug: str
    presented_digest: str
    is_current: bool
    created_at: datetime


@dataclass(slots=True, frozen=True)
class RotatedSession:
    """Outcome of completing a refresh: ``refresh_token`` is ``None`` on a grace-window replay."""

    session_id: int
    user_id: int
    tenant_slug: str
    refresh_token: str | None
    expires_at: datetime | None


@dataclass(slots=True, frozen=True)
class RevocationOutcome:
    """What a revocation did: DB sessions ended, and whether access tokens died immediately.

    ``access_tokens_recorded`` is ``False`` when the shared revocation store could not be
    written: refresh is still cut off (database), but already-issued access tokens survive until
    their own ``exp`` (<= 15 minutes) on replicas other than this one.
    """

    sessions_revoked: int
    access_tokens_recorded: bool


@dataclass(slots=True, frozen=True)
class SessionInfo:
    """One login session as an admin sees it -- identifiers and timestamps only, never a token."""

    session_id: int
    created_at: datetime | None
    expires_at: datetime | None
    is_active: bool
    revoked_at: datetime | None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _utc(value: datetime | None) -> datetime | None:
    """Normalise a DB timestamp (Postgres returns naive UTC) to an aware UTC datetime."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _digest(nonce: str) -> str:
    """The at-rest form of a refresh nonce: a version-prefixed SHA-256 (the nonce is 256-bit)."""
    return _DIGEST_PREFIX + hashlib.sha256(nonce.encode("ascii")).hexdigest()


def _tag(secret_key: str, session_id: int, nonce: str, tenant_b64: str) -> str:
    """HMAC tag over every authenticated field, keyed by a key derived from the platform secret.

    The derivation label separates this key from the JWT-signing use of the same secret, so a
    refresh tag can never double as (or be confused with) a signature over anything else.
    """
    if not secret_key:
        raise ValueError("secret_key is empty -- refusing to mint a forgeable refresh token")
    key = hmac.new(secret_key.encode("utf-8"), _KEY_LABEL, hashlib.sha256).digest()
    message = f"{session_id}.{nonce}.{tenant_b64}".encode("ascii")
    return _b64(hmac.new(key, message, hashlib.sha256).digest())


def build_refresh_token(secret_key: str, *, session_id: int, nonce: str, tenant_slug: str) -> str:
    """Assemble ``rt1.<sid>.<nonce>.<tenant>.<tag>``."""
    tenant_b64 = _b64(tenant_slug.encode("utf-8"))
    tag = _tag(secret_key, session_id, nonce, tenant_b64)
    return f"{REFRESH_TOKEN_VERSION}.{session_id}.{nonce}.{tenant_b64}.{tag}"


def parse_refresh_token(secret_key: str, raw: object) -> ParsedRefreshToken | None:
    """Authenticate ``raw``; ``None`` for anything malformed, tampered or forged (no detail)."""
    if not isinstance(raw, str) or not 0 < len(raw) <= _MAX_REFRESH_TOKEN_CHARS or not secret_key:
        return None
    parts = raw.split(".")
    if len(parts) != 5 or parts[0] != REFRESH_TOKEN_VERSION:
        return None
    _, sid_text, nonce, tenant_b64, tag = parts
    if not (
        _SESSION_ID_RE.fullmatch(sid_text)
        and _NONCE_RE.fullmatch(nonce)
        and _B64_RE.fullmatch(tenant_b64)
        and _B64_RE.fullmatch(tag)
    ):
        return None
    session_id = int(sid_text)
    expected = _tag(secret_key, session_id, nonce, tenant_b64)
    if not hmac.compare_digest(expected, tag):
        return None
    try:
        tenant_slug = _unb64(tenant_b64).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if not tenant_slug or len(tenant_slug) > 128:
        return None
    return ParsedRefreshToken(session_id=session_id, nonce=nonce, tenant_slug=tenant_slug)


def _marker_name(session_id: int, digest: str) -> str:
    """Store-marker name for "this refresh digest was just rotated away" (identifier-safe)."""
    return f"rot-{session_id}-{hashlib.sha256(digest.encode('ascii')).hexdigest()[:24]}"


def _count(event: str) -> None:
    """Count one session-lifecycle event; a dead meter never changes behaviour."""
    try:
        _session_events.add(1, {"event": event})
    except Exception as exc:  # noqa: BLE001 - telemetry must never affect a login/refresh
        logger.warning("session metric emit failed (%s)", type(exc).__name__)


def _reject(event: str, message: str, *, session_id: int | None = None) -> ApiError:
    """Count + log (PII-free) a refused refresh and return the 401 to raise."""
    _count(event)
    logger.warning(
        "refresh token refused: reason=%s",
        event,
        extra={
            "event_type": "AUTH",
            "action": "refresh",
            "result": "FAILURE",
            "session_id": session_id,
        },
    )
    return unauthorized(message)


async def open_session(
    async_dal: Any,
    dal: Any,
    cfg: HubAPIConfig,
    *,
    user_id: int,
    tenant_slug: str,
    username: str | None,
    avatar_url: str | None,
) -> OpenedSession:
    """Insert the ``hub_sessions`` row for a new login and mint its first refresh token."""
    nonce = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=cfg.refresh_token_ttl_seconds)
    session_id = int(
        await async_dal.insert_async(
            dal.hub_sessions,
            session_token=_digest(nonce),
            user_id=user_id,
            platform_username=username,
            avatar_url=avatar_url,
            is_active=True,
            expires_at=expires_at,
            created_at=now,
        )
    )
    token = build_refresh_token(
        cfg.secret_key, session_id=session_id, nonce=nonce, tenant_slug=tenant_slug
    )
    _count("issued")
    logger.info(
        "login session opened",
        extra={
            "event_type": "AUTH",
            "action": "session_open",
            "result": "SUCCESS",
            "session_id": session_id,
        },
    )
    return OpenedSession(session_id=session_id, refresh_token=token, expires_at=expires_at)


async def authenticate_refresh_token(
    async_dal: Any, dal: Any, cfg: HubAPIConfig, refresh_token: str
) -> RefreshContext:
    """Prove ``refresh_token`` is genuine and its session is live; mutate nothing.

    Raises a 401 ``ApiError`` for every failure, with one generic message per class so the
    response never says whether a session id exists.
    """
    parsed = parse_refresh_token(cfg.secret_key, refresh_token)
    if parsed is None:
        raise _reject("refresh_rejected", "Invalid refresh token")

    rows = await async_dal.select_async(dal(dal.hub_sessions.id == parsed.session_id))
    if not rows:
        raise _reject("refresh_rejected", "Invalid refresh token", session_id=parsed.session_id)
    row = rows.first()
    if not row.is_active or row.revoked_at is not None or row.user_id is None:
        raise _reject(
            "refresh_rejected", "Session is no longer active", session_id=parsed.session_id
        )

    now = datetime.now(UTC)
    created_at = _utc(row.created_at)
    expires_at = _utc(row.expires_at)
    if (
        created_at is None
        or expires_at is None
        or expires_at <= now
        or created_at + timedelta(seconds=cfg.session_absolute_ttl_seconds) <= now
    ):
        raise _reject("refresh_rejected", "Session expired", session_id=parsed.session_id)

    presented = _digest(parsed.nonce)
    return RefreshContext(
        session_id=parsed.session_id,
        user_id=int(row.user_id),
        tenant_slug=parsed.tenant_slug,
        presented_digest=presented,
        is_current=hmac.compare_digest(presented, str(row.session_token)),
        created_at=created_at,
    )


async def complete_refresh(
    async_dal: Any, dal: Any, cfg: HubAPIConfig, ctx: RefreshContext
) -> RotatedSession:
    """Rotate the refresh token (or resolve a stale one) for an authenticated ``ctx``.

    Current token -> compare-and-swap rotation (exactly one concurrent caller wins). A genuine but
    stale token -> grace-window replay, or session revocation as compromise (see module docs).
    """
    revocation = get_token_revocation()
    async with bundle_span("hub.session.refresh", session_id=ctx.session_id):
        if ctx.is_current:
            now = datetime.now(UTC)
            nonce = secrets.token_urlsafe(32)
            new_expiry = min(
                now + timedelta(seconds=cfg.refresh_token_ttl_seconds),
                ctx.created_at + timedelta(seconds=cfg.session_absolute_ttl_seconds),
            )
            # Leave the "just rotated" marker BEFORE the compare-and-swap, not after. A caller
            # that loses the CAS reads the marker immediately, and it can only lose once the
            # winner's CAS has committed -- so a marker written first is always visible by then.
            # Written after, a loser racing the winner's marker write found none, read a plain
            # parallel refresh as theft, and revoked the whole session (flaky 401 under load).
            # A marker left by a caller that then loses the CAS is harmless: its digest was
            # rotated away by the winner anyway.
            await _mark_rotated(revocation, ctx, cfg.refresh_reuse_grace_seconds)
            claimed = await async_dal.update_async(
                (dal.hub_sessions.id == ctx.session_id)
                & (dal.hub_sessions.session_token == ctx.presented_digest)
                & (dal.hub_sessions.is_active == True),  # noqa: E712 - pydal Field comparison
                session_token=_digest(nonce),
                expires_at=new_expiry,
            )
            if claimed:
                _count("refreshed")
                return RotatedSession(
                    session_id=ctx.session_id,
                    user_id=ctx.user_id,
                    tenant_slug=ctx.tenant_slug,
                    refresh_token=build_refresh_token(
                        cfg.secret_key,
                        session_id=ctx.session_id,
                        nonce=nonce,
                        tenant_slug=ctx.tenant_slug,
                    ),
                    expires_at=new_expiry,
                )
            # Lost the compare-and-swap to a concurrent rotation of this same token: the marker
            # was written before that rotation committed, so fall through to the replay decision.

        try:
            just_rotated = await revocation.ahas_marker(
                _marker_name(ctx.session_id, ctx.presented_digest)
            )
        except RevocationStoreError:
            # Cannot tell a racing page from a thief: refuse without revoking the session.
            logger.error(
                "refresh replay undecidable: revocation store unreachable",
                extra={
                    "event_type": "AUTH",
                    "action": "refresh",
                    "result": "FAILURE",
                    "session_id": ctx.session_id,
                },
            )
            raise _reject(
                "refresh_rejected", "Session could not be refreshed", session_id=ctx.session_id
            ) from None
        if just_rotated:
            _count("refresh_grace_replay")
            return RotatedSession(
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                tenant_slug=ctx.tenant_slug,
                refresh_token=None,
                expires_at=None,
            )

        # A genuine token, rotated away longer ago than the grace window: it was copied.
        outcome = await revoke_session(
            async_dal, dal, session_id=ctx.session_id, reason="refresh_reuse"
        )
        await record_session_revoked(
            user_id=None,
            tenant_slug=ctx.tenant_slug,
            reason="refresh_reuse",
            kind="session",
            sessions=outcome.sessions_revoked,
            session_id=ctx.session_id,
        )
        _count("refresh_reuse_revoked")
        logger.error(
            "refresh token reuse detected: session revoked",
            extra={
                "event_type": "AUTH",
                "action": "refresh_reuse",
                "result": "REVOKED",
                "session_id": ctx.session_id,
            },
        )
        raise unauthorized("Session is no longer active")


async def _mark_rotated(revocation: Any, ctx: RefreshContext, grace_seconds: int) -> None:
    """Leave the "just rotated" marker for this digest; best-effort (the rotation stands)."""
    if grace_seconds <= 0:
        return
    try:
        await revocation.aput_marker(
            _marker_name(ctx.session_id, ctx.presented_digest), grace_seconds
        )
    except RevocationStoreError:
        logger.warning(
            "rotation marker not recorded: a parallel refresh in the grace window is refused",
            extra={
                "event_type": "AUTH",
                "action": "refresh",
                "result": "DEGRADED",
                "session_id": ctx.session_id,
            },
        )


async def revoke_session(
    async_dal: Any, dal: Any, *, session_id: int, reason: str
) -> RevocationOutcome:
    """End one login session: stop its refresh (DB) and kill its live access tokens (store)."""
    ended = await async_dal.update_async(
        (dal.hub_sessions.id == session_id) & (dal.hub_sessions.is_active == True),  # noqa: E712
        is_active=False,
        revoked_at=datetime.now(UTC),
    )
    recorded = True
    try:
        await get_token_revocation().arevoke_session(str(session_id))
    except RevocationStoreError:
        recorded = False
        _count("revocation_store_failed")
    _count("revoked_session")
    logger.info(
        "login session revoked: reason=%s",
        reason,
        extra={
            "event_type": "AUTH",
            "action": "session_revoke",
            "result": "SUCCESS",
            "session_id": session_id,
        },
    )
    return RevocationOutcome(sessions_revoked=int(ended or 0), access_tokens_recorded=recorded)


async def revoke_user_sessions(
    async_dal: Any, dal: Any, *, user_id: int, reason: str
) -> RevocationOutcome:
    """End EVERY login session of ``user_id`` and cut off all their access tokens issued so far.

    The store half is a per-user ``iat`` cutoff, so it also covers tokens that never saw a
    session row (service-style tokens) and tokens minted but not yet used.
    """
    ended = await async_dal.update_async(
        (dal.hub_sessions.user_id == user_id) & (dal.hub_sessions.is_active == True),  # noqa: E712
        is_active=False,
        revoked_at=datetime.now(UTC),
    )
    recorded = True
    try:
        await get_token_revocation().arevoke_user(str(user_id))
    except RevocationStoreError:
        recorded = False
        _count("revocation_store_failed")
    _count("revoked_user")
    logger.info(
        "all login sessions of a user revoked: reason=%s sessions=%s",
        reason,
        int(ended or 0),
        extra={"event_type": "AUTH", "action": "user_sessions_revoke", "result": "SUCCESS"},
    )
    return RevocationOutcome(sessions_revoked=int(ended or 0), access_tokens_recorded=recorded)


async def revoke_other_sessions(
    async_dal: Any, dal: Any, *, user_id: int, keep_session_id: int | None, reason: str
) -> RevocationOutcome:
    """End every login session of ``user_id`` EXCEPT ``keep_session_id`` ("sign out other devices").

    Used when the caller changes an authenticator (passkey removed, linked identity unlinked) and
    should stay signed in on the device they are using. Revokes each other session by its ``sid``
    (database + store), so it needs no ``iat`` cutoff and never touches the kept session's tokens.
    ``keep_session_id=None`` (a token with no session) ends them all.
    """
    query = (dal.hub_sessions.user_id == user_id) & (dal.hub_sessions.is_active == True)  # noqa: E712
    if keep_session_id is not None:
        query &= dal.hub_sessions.id != keep_session_id
    rows = await async_dal.select_async(dal(query))
    session_ids = [int(row.id) for row in rows]
    if not session_ids:
        return RevocationOutcome(sessions_revoked=0, access_tokens_recorded=True)

    await async_dal.update_async(
        (dal.hub_sessions.user_id == user_id) & dal.hub_sessions.id.belongs(session_ids),
        is_active=False,
        revoked_at=datetime.now(UTC),
    )
    recorded = True
    revocation = get_token_revocation()
    for session_id in session_ids:
        try:
            await revocation.arevoke_session(str(session_id))
        except RevocationStoreError:
            recorded = False
            _count("revocation_store_failed")
    _count("revoked_session")
    logger.info(
        "other login sessions of a user revoked: reason=%s sessions=%s",
        reason,
        len(session_ids),
        extra={"event_type": "AUTH", "action": "other_sessions_revoke", "result": "SUCCESS"},
    )
    return RevocationOutcome(sessions_revoked=len(session_ids), access_tokens_recorded=recorded)


async def revoke_user_access_tokens(*, user_id: int, reason: str) -> bool:
    """Store half of a revoke-all only: cut off every access token ``user_id`` holds right now.

    For callers whose session rows are already gone or whose database work is its own atomic unit
    (DSAR erasure deletes the ``hub_sessions`` rows inside its all-or-nothing transaction and must
    not gain a second commit). Returns ``False`` when the shared store could not record it.
    """
    try:
        await get_token_revocation().arevoke_user(str(user_id))
    except RevocationStoreError:
        _count("revocation_store_failed")
        return False
    _count("revoked_user")
    logger.info(
        "access tokens of a user revoked: reason=%s",
        reason,
        extra={"event_type": "AUTH", "action": "user_tokens_revoke", "result": "SUCCESS"},
    )
    return True


async def revoke_access_token(jti: str, exp: int | None) -> bool:
    """Reject one specific access token; ``False`` when the shared store could not record it."""
    try:
        await get_token_revocation().arevoke_token(jti, exp=exp)
    except RevocationStoreError:
        _count("revocation_store_failed")
        return False
    return True


async def get_session_owner(async_dal: Any, dal: Any, *, session_id: int) -> int | None:
    """The ``hub_users.id`` that owns ``session_id``, or ``None`` if there is no such session."""
    rows = await async_dal.select_async(dal(dal.hub_sessions.id == session_id))
    if not rows:
        return None
    owner = rows.first().user_id
    return int(owner) if owner is not None else None


async def list_sessions(async_dal: Any, dal: Any, *, user_id: int) -> list[SessionInfo]:
    """Every login session of ``user_id`` (newest first): ids and timestamps, never a token."""
    rows = await async_dal.select_async(
        dal(dal.hub_sessions.user_id == user_id), orderby=~dal.hub_sessions.id
    )
    return [
        SessionInfo(
            session_id=int(row.id),
            created_at=_utc(row.created_at),
            expires_at=_utc(row.expires_at),
            is_active=bool(row.is_active),
            revoked_at=_utc(row.revoked_at),
        )
        for row in rows
    ]
