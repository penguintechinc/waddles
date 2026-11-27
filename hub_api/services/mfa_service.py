"""Account multi-factor authentication: TOTP (preferred), email one-time code, recovery codes.

Flow at login (`auth_service.login` / `admin_login`):

  password OK + an ACTIVE method  ->  :func:`start_login_challenge` -> opaque MFA-pending token
                                      (no JWT, no session, no scope: it can only be presented to
                                      the two `/auth/mfa/login/*` routes)
  `/auth/mfa/login/verify`        ->  :func:`complete_login` (TOTP / emailed code / recovery
                                      code) -> `auth_service` mints the real session

Security properties (each pinned by `tests/test_mfa_*.py`):

* **TOTP secret at rest** -- envelope-encrypted (`services/envelope`, platform-KEK baseline, BYOK
  when the operator configures it) under the ``system`` tenant's DEK -- users are global
  identities, not tenant-owned -- with AAD bound to (table, column, the user's UUID), so a
  ciphertext copied to another user's row fails to decrypt. No plaintext fallback.
* **Enrollment** stays PENDING until the user proves the authenticator with a first valid code.
* **Drift / replay** -- +/-1 step; the accepted step is persisted with a conditional UPDATE
  (``last_step < step``), so the same code cannot be accepted twice, even concurrently.
* **Recovery codes** -- 10, single use (conditional UPDATE on ``used_at IS NULL``), stored as
  SHA-256, returned exactly once.
* **Email code** -- 6 digits, salted hash, 10-minute TTL, at most 5 wrong attempts per
  challenge (the challenge then burns), sends capped per challenge, spaced, and per user.
* **Rate limits** -- per-user verification budget on top of the per-challenge attempt cap; the
  global auth-tier limiter (`services/rate_limiting.py`) already covers every `/api/v1/auth/*`.
* **Disable / regenerate** -- re-auth (current password, or a fresh sign-in for password-less
  accounts) AND a valid second-factor code.
* **Kill switches** (core mechanism, opt-out): ``waddles.disable-mfa`` ON = legacy single-factor
  login and no new enrollment; ``waddles.disable-mfa-email`` ON = email method off.

Every code comparison is constant time. Audit events and telemetry carry ids and closed enums
only -- never a code, secret, recovery code or email address.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Final

from flask_core.auth import DEFAULT_TENANT_SLUG
from flask_core.feature_flags import feature_enabled

from services import dsar_rate_limit, mfa_email, totp
from services.audit_events import AuditAction, AuditCategory, AuditOutcome
from services.audit_http import record_mfa_event
from services.envelope import EnvelopeError, EnvelopeIntegrityError
from services.errors import ApiError, bad_request, conflict, not_found
from services.global_identity import SYSTEM_TENANT_SLUG
from services.mfa_telemetry import (
    challenge_counter,
    email_send_counter,
    enrollment_counter,
    mfa_span,
    verify_counter,
    verify_duration,
)

logger = logging.getLogger(__name__)

FLAG_DISABLE_MFA: Final = "waddles.disable-mfa"
FLAG_DISABLE_EMAIL_MFA: Final = "waddles.disable-mfa-email"

METHOD_TOTP: Final = "totp"
METHOD_EMAIL: Final = "email"
METHOD_RECOVERY: Final = "recovery"
METHODS: Final = (METHOD_TOTP, METHOD_EMAIL, METHOD_RECOVERY)

#: `hub_user_mfa.{totp,email}_status` codes (alembic 0145).
STATUS_NONE: Final = 0
STATUS_PENDING: Final = 1
STATUS_ACTIVE: Final = 2

#: `hub_mfa_challenges.purpose` codes.
PURPOSE_LOGIN: Final = 1
PURPOSE_ACCOUNT: Final = 2

CHALLENGE_TTL_SECONDS: Final = 600
EMAIL_CODE_TTL_SECONDS: Final = 600
MAX_ATTEMPTS: Final = 5
MAX_EMAIL_SENDS_PER_CHALLENGE: Final = 3
EMAIL_RESEND_INTERVAL_SECONDS: Final = 30
USER_EMAIL_SENDS_PER_HOUR: Final = 5
USER_VERIFY_ATTEMPTS: Final = 20
USER_VERIFY_WINDOW_SECONDS: Final = 900
#: A password-less account proves "it's really you" with a sign-in at most this old.
REAUTH_MAX_AGE_SECONDS: Final = 300

SECRET_TABLE: Final = "hub_user_mfa"  # noqa: S105 -- a table name, not a credential
SECRET_COLUMN: Final = "totp_secret_enc"  # noqa: S105 -- a column name
_LIMIT_PREFIX: Final = "mfa:"

#: amr (RFC 8176) minted for a session completed with each method (password was the 1st factor).
_AMR: Final[dict[str, list[str]]] = {
    METHOD_TOTP: ["pwd", "otp", "mfa"],
    METHOD_EMAIL: ["pwd", "otp", "mfa"],
    METHOD_RECOVERY: ["pwd", "mfa"],
}


def _unavailable() -> ApiError:
    """503 when a backing store (limiter, key service) cannot answer -- fail closed."""
    return ApiError(
        "Multi-factor authentication is temporarily unavailable", 503, "MFA_UNAVAILABLE"
    )


def _disabled() -> ApiError:
    """Enrollment refused while the `waddles.disable-mfa` kill switch is ON."""
    return ApiError("Multi-factor authentication is disabled on this server", 503, "MFA_DISABLED")


def _rate_limited() -> ApiError:
    """Too many MFA attempts or sends for this account right now."""
    return ApiError("Too many attempts. Please wait and try again.", 429, "MFA_RATE_LIMITED")


def _invalid_code() -> ApiError:
    """Uniform wrong-code answer (never says WHICH factor or why)."""
    return ApiError("Invalid verification code", 401, "MFA_INVALID_CODE")


def _challenge_invalid() -> ApiError:
    """The MFA-pending token is unknown, used, expired or locked: start the login again."""
    return ApiError(
        "Verification session expired. Please sign in again.", 401, "MFA_CHALLENGE_INVALID"
    )


def _now() -> datetime:
    """Timezone-aware now (one place, so tests can reason about TTLs)."""
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    """Treat naive datetimes read back from sqlite as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _limit(key: str, limit: int, window_seconds: int) -> None:
    """Spend one unit of a per-account budget; 429 when exhausted, 503 when the store is down."""
    allowed = await dsar_rate_limit.allow(
        key, limit, window_seconds, prefix=_LIMIT_PREFIX, on_unavailable=_unavailable
    )
    if not allowed:
        raise _rate_limited()


async def mechanism_enabled(tenant_slug: str) -> bool:
    """False only when the `waddles.disable-mfa` kill switch is ON (unseen/OFF = MFA on)."""
    return not await feature_enabled(FLAG_DISABLE_MFA, tenant=tenant_slug)


async def email_method_enabled(tenant_slug: str) -> bool:
    """Email MFA is usable: mechanism on, email kill switch off, and an SMTP transport ready."""
    if not await mechanism_enabled(tenant_slug):
        return False
    if await feature_enabled(FLAG_DISABLE_EMAIL_MFA, tenant=tenant_slug):
        return False
    return mfa_email.email_available()


async def _mfa_row(async_dal: Any, dal: Any, user_id: int) -> Any | None:
    """The user's `hub_user_mfa` row, or None."""
    rows = await async_dal.select_async(dal(dal.hub_user_mfa.user_id == user_id))
    return rows.first() if rows else None


async def _user_row(async_dal: Any, dal: Any, user_id: int) -> Any:
    """The `hub_users` row (404 when the account vanished mid-flow)."""
    rows = await async_dal.select_async(dal(dal.hub_users.id == user_id))
    if not rows:
        raise not_found("User not found")
    return rows.first()


async def _upsert_mfa_row(async_dal: Any, dal: Any, user_id: int, **fields: Any) -> None:
    """Insert or update the user's single `hub_user_mfa` row."""
    now = _now()
    if await _mfa_row(async_dal, dal, user_id) is None:
        await async_dal.insert_async(
            dal.hub_user_mfa, user_id=user_id, created_at=now, updated_at=now, **fields
        )
    else:
        await async_dal.update_async(dal.hub_user_mfa.user_id == user_id, updated_at=now, **fields)


def active_methods(row: Any | None) -> list[str]:
    """Second factors the user can use at login, preferred (TOTP) first."""
    if row is None:
        return []
    methods = []
    if row.totp_status == STATUS_ACTIVE:
        methods.append(METHOD_TOTP)
    if row.email_status == STATUS_ACTIVE:
        methods.append(METHOD_EMAIL)
    return methods


async def _recovery_remaining(async_dal: Any, dal: Any, user_id: int) -> int:
    """Unused recovery codes left for the user."""
    t = dal.hub_user_mfa_recovery_codes
    rows = await async_dal.select_async(dal((t.user_id == user_id) & (t.used_at == None)))  # noqa: E711
    return len(rows)


# -- secret sealing ------------------------------------------------------------------------------


async def _system_tenant_id(async_dal: Any, dal: Any) -> int:
    """`tenants.id` of the operator-plane tenant whose DEK protects identity-level secrets."""
    rows = await async_dal.select_async(dal(dal.tenants.slug == SYSTEM_TENANT_SLUG))
    if not rows:
        logger.error("mfa.system_tenant_missing (cannot seal identity secrets)")
        raise _unavailable()
    return int(rows.first().id)


def _envelope_service(envelope_runtime: Any | None) -> Any:
    """The at-rest envelope service; 503 (never plaintext) when hub-api has none wired."""
    service = getattr(envelope_runtime, "service", None)
    if service is None:
        logger.error("mfa.envelope_unavailable (no envelope runtime; refusing plaintext secret)")
        raise _unavailable()
    return service


async def _seal_secret(
    async_dal: Any, dal: Any, envelope_runtime: Any | None, *, user_uuid: str, secret: str
) -> str:
    """Encrypt a TOTP secret to the `wenv1.` wire form bound to this user's row."""
    service = _envelope_service(envelope_runtime)
    tenant_id = await _system_tenant_id(async_dal, dal)
    wire: str = await service.encrypt_text(
        tenant_id, secret, table=SECRET_TABLE, column=SECRET_COLUMN, row_uuid=user_uuid
    )
    return wire


async def _open_secret(
    async_dal: Any, dal: Any, envelope_runtime: Any | None, *, user_uuid: str, wire: str
) -> str:
    """Decrypt a sealed TOTP secret (raises loudly on tampering / a missing key)."""
    service = _envelope_service(envelope_runtime)
    tenant_id = await _system_tenant_id(async_dal, dal)
    try:
        secret: str = await service.decrypt_text(
            tenant_id, wire, table=SECRET_TABLE, column=SECRET_COLUMN, row_uuid=user_uuid
        )
    except EnvelopeIntegrityError as exc:
        # Tampered, or copied from another user's row (AAD mismatch): never usable.
        logger.error("mfa.totp_secret_integrity_failure (row tampered or moved)")
        raise ApiError(
            "Your authenticator enrollment cannot be read; use a recovery code",
            500,
            "MFA_SECRET_UNREADABLE",
        ) from exc
    except EnvelopeError as exc:
        logger.error("mfa.totp_secret_key_unavailable err_type=%s", type(exc).__name__)
        raise _unavailable() from exc
    return secret


# -- status --------------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class MfaStatus:
    """What the security-settings page needs to render the MFA section."""

    mechanism_enabled: bool
    totp_status: int
    email_status: int
    email_available: bool
    email_verified: bool
    recovery_codes_remaining: int
    has_password: bool

    @property
    def preferred_method(self) -> str | None:
        """TOTP whenever it is active, else email, else None."""
        if self.totp_status == STATUS_ACTIVE:
            return METHOD_TOTP
        if self.email_status == STATUS_ACTIVE:
            return METHOD_EMAIL
        return None


async def get_status(async_dal: Any, dal: Any, *, user_id: int, tenant_slug: str) -> MfaStatus:
    """The caller's MFA state (no secret material)."""
    user = await _user_row(async_dal, dal, user_id)
    row = await _mfa_row(async_dal, dal, user_id)
    return MfaStatus(
        mechanism_enabled=await mechanism_enabled(tenant_slug),
        totp_status=int(row.totp_status) if row is not None else STATUS_NONE,
        email_status=int(row.email_status) if row is not None else STATUS_NONE,
        email_available=await email_method_enabled(tenant_slug),
        email_verified=bool(user.email_verified) and bool(user.email),
        recovery_codes_remaining=await _recovery_remaining(async_dal, dal, user_id),
        has_password=bool(user.password_hash),
    )


# -- recovery codes ------------------------------------------------------------------------------


async def _replace_recovery_codes(async_dal: Any, dal: Any, user_id: int) -> list[str]:
    """Delete the user's old codes and store 10 fresh hashed ones; return the plaintext once."""
    t = dal.hub_user_mfa_recovery_codes
    await async_dal.delete_async(t.user_id == user_id)
    codes = totp.new_recovery_codes()
    now = _now()
    for code in codes:
        await async_dal.insert_async(
            t, user_id=user_id, code_hash=totp.hash_recovery_code(code), created_at=now
        )
    return codes


async def _consume_recovery_code(async_dal: Any, dal: Any, user_id: int, code: str) -> bool:
    """Spend one unused recovery code; False when none matches (or a racer spent it first)."""
    t = dal.hub_user_mfa_recovery_codes
    rows = await async_dal.select_async(dal((t.user_id == user_id) & (t.used_at == None)))  # noqa: E711
    wanted = totp.hash_recovery_code(code)
    match_id: int | None = None
    for row in rows:  # compare against every row: no early exit
        if totp.digests_equal(str(row.code_hash), wanted) and match_id is None:
            match_id = int(row.id)
    if match_id is None:
        return False
    spent = await async_dal.update_async(
        (t.id == match_id) & (t.used_at == None),  # noqa: E711
        used_at=_now(),
    )
    return bool(spent == 1)


# -- TOTP ----------------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class TotpEnrollment:
    """Returned once to the enrolling user's browser (the QR code is rendered client-side)."""

    otpauth_uri: str
    secret: str


async def start_totp_enrollment(
    async_dal: Any, dal: Any, envelope_runtime: Any | None, *, user_id: int, tenant_slug: str
) -> TotpEnrollment:
    """Create (or replace a still-PENDING) TOTP secret; 409 when TOTP is already active."""
    if not await mechanism_enabled(tenant_slug):
        raise _disabled()
    await _limit(f"enroll:{user_id}", 10, 3600)
    row = await _mfa_row(async_dal, dal, user_id)
    if row is not None and row.totp_status == STATUS_ACTIVE:
        raise conflict("An authenticator app is already enrolled")
    user = await _user_row(async_dal, dal, user_id)
    with mfa_span("mfa.totp.enroll", method=METHOD_TOTP):
        secret = totp.new_secret()
        sealed = await _seal_secret(
            async_dal, dal, envelope_runtime, user_uuid=str(user.uuid), secret=secret
        )
        await _upsert_mfa_row(
            async_dal,
            dal,
            user_id,
            totp_secret_enc=sealed,
            totp_status=STATUS_PENDING,
            totp_last_step=None,
            totp_confirmed_at=None,
        )
    enrollment_counter.add(1, {"method": METHOD_TOTP, "operation": "enroll"})
    logger.info("mfa.totp.enroll_started user_id=%d", user_id)
    label = str(user.email or user.username or user.uuid)
    return TotpEnrollment(
        otpauth_uri=totp.provisioning_uri(secret, account_label=label), secret=secret
    )


async def confirm_totp_enrollment(
    async_dal: Any,
    dal: Any,
    envelope_runtime: Any | None,
    *,
    user_id: int,
    code: str,
    tenant_slug: str,
) -> list[str]:
    """Activate a PENDING TOTP enrollment with its first valid code; returns new recovery codes."""
    if not await mechanism_enabled(tenant_slug):
        raise _disabled()
    await _limit(f"verify:{user_id}", USER_VERIFY_ATTEMPTS, USER_VERIFY_WINDOW_SECONDS)
    row = await _mfa_row(async_dal, dal, user_id)
    if row is None or row.totp_status != STATUS_PENDING or not row.totp_secret_enc:
        raise bad_request("No authenticator enrollment is pending")
    user = await _user_row(async_dal, dal, user_id)
    with mfa_span("mfa.totp.confirm", method=METHOD_TOTP):
        secret = await _open_secret(
            async_dal, dal, envelope_runtime, user_uuid=str(user.uuid), wire=row.totp_secret_enc
        )
        step = totp.matching_step(secret, code, now=time.time(), after_step=None)
    if step is None:
        verify_counter.add(1, {"method": METHOD_TOTP, "purpose": "enroll", "outcome": "invalid"})
        raise _invalid_code()
    updated = await async_dal.update_async(
        (dal.hub_user_mfa.user_id == user_id) & (dal.hub_user_mfa.totp_status == STATUS_PENDING),
        totp_status=STATUS_ACTIVE,
        totp_last_step=step,
        totp_confirmed_at=_now(),
        updated_at=_now(),
    )
    if updated != 1:
        raise conflict("Enrollment changed concurrently; start again")
    codes = await _replace_recovery_codes(async_dal, dal, user_id)
    verify_counter.add(1, {"method": METHOD_TOTP, "purpose": "enroll", "outcome": "ok"})
    enrollment_counter.add(1, {"method": METHOD_TOTP, "operation": "confirm"})
    await record_mfa_event(
        user_id=user_id,
        category=AuditCategory.CREDENTIAL,
        action=AuditAction.CREDENTIAL_MFA_ENROLLED,
        outcome=AuditOutcome.SUCCESS,
        method=METHOD_TOTP,
        tenant_slug=tenant_slug,
    )
    logger.info("mfa.totp.enrolled user_id=%d", user_id)
    return codes


async def _verify_totp(
    async_dal: Any, dal: Any, envelope_runtime: Any | None, *, user_id: int, code: str
) -> bool:
    """Check an ACTIVE TOTP code and atomically record its step (single use, replay-safe)."""
    row = await _mfa_row(async_dal, dal, user_id)
    if row is None or row.totp_status != STATUS_ACTIVE or not row.totp_secret_enc:
        return False
    user = await _user_row(async_dal, dal, user_id)
    secret = await _open_secret(
        async_dal, dal, envelope_runtime, user_uuid=str(user.uuid), wire=row.totp_secret_enc
    )
    last = int(row.totp_last_step) if row.totp_last_step is not None else None
    step = totp.matching_step(secret, code, now=time.time(), after_step=last)
    if step is None:
        return False
    t = dal.hub_user_mfa
    claimed = await async_dal.update_async(
        (t.user_id == user_id) & ((t.totp_last_step == None) | (t.totp_last_step < step)),  # noqa: E711
        totp_last_step=step,
        updated_at=_now(),
    )
    return bool(claimed == 1)


# -- email one-time codes ------------------------------------------------------------------------


async def _new_challenge(
    async_dal: Any,
    dal: Any,
    *,
    user_id: int,
    purpose: int,
    tenant_id: int | None = None,
    tenant_slug: str | None = None,
    auth_method: str | None = None,
) -> tuple[str, Any]:
    """Insert a challenge row; return (opaque token, row). Only the token's hash is stored."""
    token = secrets.token_urlsafe(32)
    now = _now()
    fields: dict[str, Any] = {
        "user_id": user_id,
        "purpose": purpose,
        "token_hash": totp.hash_token(token),
        "tenant_id": tenant_id,
        "tenant_slug": tenant_slug,
        "auth_method": auth_method,
        "attempts": 0,
        "email_sends": 0,
        "expires_at": now + timedelta(seconds=CHALLENGE_TTL_SECONDS),
        "created_at": now,
    }
    row_id = await async_dal.insert_async(dal.hub_mfa_challenges, **fields)
    # Built from what was just written (not re-read: a read replica may lag the primary).
    row = SimpleNamespace(
        id=int(row_id),
        email_code_hash=None,
        email_code_salt=None,
        email_code_expires_at=None,
        email_last_sent_at=None,
        consumed_at=None,
        **fields,
    )
    return token, row


def _challenge_live(row: Any) -> bool:
    """Not consumed, not expired, attempts left."""
    expires = _aware(row.expires_at)
    return (
        row.consumed_at is None
        and expires is not None
        and expires > _now()
        and int(row.attempts) < MAX_ATTEMPTS
    )


async def _account_challenge(async_dal: Any, dal: Any, user_id: int) -> Any | None:
    """The user's newest live PURPOSE_ACCOUNT challenge (email codes for settings actions)."""
    t = dal.hub_mfa_challenges
    rows = await async_dal.select_async(
        dal((t.user_id == user_id) & (t.purpose == PURPOSE_ACCOUNT)), orderby=~t.id
    )
    for row in rows:
        return row if _challenge_live(row) else None
    return None


async def _send_code(async_dal: Any, dal: Any, *, challenge: Any, email: str) -> None:
    """Issue a fresh email code on `challenge` and deliver it; enforce every send limit first."""
    if not mfa_email.email_available():
        email_send_counter.add(1, {"outcome": "unavailable"})
        raise mfa_email.email_unavailable()
    if int(challenge.email_sends) >= MAX_EMAIL_SENDS_PER_CHALLENGE:
        email_send_counter.add(1, {"outcome": "rate_limited"})
        raise _rate_limited()
    last = _aware(challenge.email_last_sent_at)
    if last is not None and (_now() - last).total_seconds() < EMAIL_RESEND_INTERVAL_SECONDS:
        email_send_counter.add(1, {"outcome": "rate_limited"})
        raise _rate_limited()
    try:
        await _limit(f"send:{challenge.user_id}", USER_EMAIL_SENDS_PER_HOUR, 3600)
    except ApiError:
        email_send_counter.add(1, {"outcome": "rate_limited"})
        raise
    code = totp.new_email_code()
    salt = secrets.token_hex(16)
    now = _now()
    await async_dal.update_async(
        dal.hub_mfa_challenges.id == challenge.id,
        email_code_hash=totp.hash_email_code(code, salt),
        email_code_salt=salt,
        email_code_expires_at=now + timedelta(seconds=EMAIL_CODE_TTL_SECONDS),
        email_sends=int(challenge.email_sends) + 1,
        email_last_sent_at=now,
    )
    with mfa_span("mfa.email.send", method=METHOD_EMAIL):
        try:
            await mfa_email.send_email(
                to=email,
                subject="Your Waddles verification code",
                text=(
                    f"Your Waddles verification code is {code}.\n\n"
                    f"It expires in {EMAIL_CODE_TTL_SECONDS // 60} minutes. If you did not try "
                    "to sign in, change your password."
                ),
            )
        except Exception:
            email_send_counter.add(1, {"outcome": "error"})
            raise
    email_send_counter.add(1, {"outcome": "sent"})
    logger.info("mfa.email.code_sent user_id=%d purpose=%d", challenge.user_id, challenge.purpose)


async def _check_email_code(async_dal: Any, dal: Any, *, challenge: Any, code: str) -> bool:
    """Constant-time check of the challenge's email code; clears it on success (single use)."""
    expires = _aware(challenge.email_code_expires_at)
    digits = totp.normalize_digits(code)
    if (
        not challenge.email_code_hash
        or not challenge.email_code_salt
        or expires is None
        or expires <= _now()
        or digits is None
    ):
        return False
    if not totp.digests_equal(
        totp.hash_email_code(digits, str(challenge.email_code_salt)), str(challenge.email_code_hash)
    ):
        return False
    t = dal.hub_mfa_challenges
    cleared = await async_dal.update_async(
        (t.id == challenge.id) & (t.email_code_hash == challenge.email_code_hash),
        email_code_hash=None,
        email_code_expires_at=None,
    )
    return bool(cleared == 1)


async def _record_failure(async_dal: Any, dal: Any, challenge: Any) -> None:
    """Count one wrong code on the challenge; burn it at the attempt cap."""
    attempts = int(challenge.attempts) + 1
    fields: dict[str, Any] = {"attempts": attempts}
    if attempts >= MAX_ATTEMPTS:
        fields["consumed_at"] = _now()
    await async_dal.update_async(dal.hub_mfa_challenges.id == challenge.id, **fields)


async def start_email_enrollment(
    async_dal: Any, dal: Any, *, user_id: int, tenant_slug: str
) -> None:
    """Begin enrolling the email method: requires a verified address, sends a code to it."""
    if not await mechanism_enabled(tenant_slug):
        raise _disabled()
    if not await email_method_enabled(tenant_slug):
        raise mfa_email.email_unavailable()
    user = await _user_row(async_dal, dal, user_id)
    if not user.email or not user.email_verified:
        raise ApiError(
            "Verify your email address before using it for sign-in codes",
            400,
            "EMAIL_NOT_VERIFIED",
        )
    row = await _mfa_row(async_dal, dal, user_id)
    if row is not None and row.email_status == STATUS_ACTIVE:
        raise conflict("Email sign-in codes are already enabled")
    await _upsert_mfa_row(async_dal, dal, user_id, email_status=STATUS_PENDING)
    await send_account_code(async_dal, dal, user_id=user_id, tenant_slug=tenant_slug)
    enrollment_counter.add(1, {"method": METHOD_EMAIL, "operation": "enroll"})


async def send_account_code(async_dal: Any, dal: Any, *, user_id: int, tenant_slug: str) -> None:
    """Email a code for an account action (confirm email enrollment, disable, regenerate)."""
    if not await email_method_enabled(tenant_slug):
        raise mfa_email.email_unavailable()
    user = await _user_row(async_dal, dal, user_id)
    if not user.email or not user.email_verified:
        raise ApiError("No verified email address on this account", 400, "EMAIL_NOT_VERIFIED")
    challenge = await _account_challenge(async_dal, dal, user_id)
    if challenge is None:
        _token, challenge = await _new_challenge(
            async_dal, dal, user_id=user_id, purpose=PURPOSE_ACCOUNT, tenant_slug=tenant_slug
        )
    await _send_code(async_dal, dal, challenge=challenge, email=str(user.email))


async def _verify_account_email_code(async_dal: Any, dal: Any, *, user_id: int, code: str) -> bool:
    """Check `code` against the user's live account-action email challenge."""
    challenge = await _account_challenge(async_dal, dal, user_id)
    if challenge is None:
        return False
    if await _check_email_code(async_dal, dal, challenge=challenge, code=code):
        return True
    await _record_failure(async_dal, dal, challenge)
    return False


async def confirm_email_enrollment(
    async_dal: Any, dal: Any, *, user_id: int, code: str, tenant_slug: str
) -> list[str]:
    """Activate a PENDING email method; returns recovery codes when the user had none."""
    await _limit(f"verify:{user_id}", USER_VERIFY_ATTEMPTS, USER_VERIFY_WINDOW_SECONDS)
    row = await _mfa_row(async_dal, dal, user_id)
    if row is None or row.email_status != STATUS_PENDING:
        raise bad_request("No email enrollment is pending")
    with mfa_span("mfa.email.confirm", method=METHOD_EMAIL):
        ok = await _verify_account_email_code(async_dal, dal, user_id=user_id, code=code)
    if not ok:
        verify_counter.add(1, {"method": METHOD_EMAIL, "purpose": "enroll", "outcome": "invalid"})
        raise _invalid_code()
    await _upsert_mfa_row(
        async_dal, dal, user_id, email_status=STATUS_ACTIVE, email_confirmed_at=_now()
    )
    codes: list[str] = []
    if await _recovery_remaining(async_dal, dal, user_id) == 0:
        codes = await _replace_recovery_codes(async_dal, dal, user_id)
    verify_counter.add(1, {"method": METHOD_EMAIL, "purpose": "enroll", "outcome": "ok"})
    enrollment_counter.add(1, {"method": METHOD_EMAIL, "operation": "confirm"})
    await record_mfa_event(
        user_id=user_id,
        category=AuditCategory.CREDENTIAL,
        action=AuditAction.CREDENTIAL_MFA_ENROLLED,
        outcome=AuditOutcome.SUCCESS,
        method=METHOD_EMAIL,
        tenant_slug=tenant_slug,
    )
    logger.info("mfa.email.enrolled user_id=%d", user_id)
    return codes


# -- login ---------------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class LoginChallenge:
    """MFA-pending state handed to the browser instead of a session."""

    token: str
    methods: list[str]
    expires_in: int


async def login_methods(async_dal: Any, dal: Any, *, user_id: int, tenant_slug: str) -> list[str]:
    """Methods the user must complete at login; empty = no MFA step (none enrolled / kill switch).

    Policy hook: no tenant or platform setting models "require MFA" today, so enforcement is
    per-user enrollment. A future tenant policy plugs in here (and enrollment then becomes
    mandatory at next login) without touching the login routes.
    """
    methods = active_methods(await _mfa_row(async_dal, dal, user_id))
    if not methods:
        return []
    if not await mechanism_enabled(tenant_slug):
        logger.warning("mfa.kill_switch_on: second factor skipped user_id=%d", user_id)
        return []
    if METHOD_EMAIL in methods and not await email_method_enabled(tenant_slug):
        methods = [m for m in methods if m != METHOD_EMAIL]
    if not methods:
        # Email-only user while email delivery is down: recovery codes are the remaining factor.
        return [METHOD_RECOVERY]
    return [*methods, METHOD_RECOVERY]


async def start_login_challenge(
    async_dal: Any,
    dal: Any,
    *,
    user_id: int,
    methods: list[str],
    tenant_id: int | None,
    tenant_slug: str,
    auth_method: str,
) -> LoginChallenge:
    """Record MFA-pending state after a successful first factor (no session, no scope)."""
    token, _row = await _new_challenge(
        async_dal,
        dal,
        user_id=user_id,
        purpose=PURPOSE_LOGIN,
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        auth_method=auth_method,
    )
    challenge_counter.add(1, {"auth_method": auth_method})
    await record_mfa_event(
        user_id=user_id,
        category=AuditCategory.AUTHN,
        action=AuditAction.MFA_CHALLENGE_ISSUED,
        outcome=AuditOutcome.SUCCESS,
        method=methods[0],
        tenant_slug=tenant_slug,
    )
    logger.info("mfa.challenge_issued user_id=%d auth_method=%s", user_id, auth_method)
    return LoginChallenge(token=token, methods=methods, expires_in=CHALLENGE_TTL_SECONDS)


async def _login_challenge(async_dal: Any, dal: Any, token: str) -> Any:
    """The live login challenge for `token`, else 401 MFA_CHALLENGE_INVALID."""
    if not token or len(token) > 128:
        raise _challenge_invalid()
    t = dal.hub_mfa_challenges
    rows = await async_dal.select_async(
        dal((t.token_hash == totp.hash_token(token)) & (t.purpose == PURPOSE_LOGIN))
    )
    if not rows or not _challenge_live(rows.first()):
        raise _challenge_invalid()
    return rows.first()


async def send_login_email_code(async_dal: Any, dal: Any, *, token: str) -> None:
    """Email a sign-in code for a pending login (the user chose the email method)."""
    challenge = await _login_challenge(async_dal, dal, token)
    slug = str(challenge.tenant_slug or DEFAULT_TENANT_SLUG)
    methods = await login_methods(async_dal, dal, user_id=int(challenge.user_id), tenant_slug=slug)
    if METHOD_EMAIL not in methods:
        raise mfa_email.email_unavailable()
    user = await _user_row(async_dal, dal, int(challenge.user_id))
    await _send_code(async_dal, dal, challenge=challenge, email=str(user.email))


@dataclass(slots=True, frozen=True)
class CompletedLogin:
    """Everything `auth_service` needs to mint the session the challenge was standing in for."""

    user_id: int
    tenant_id: int | None
    tenant_slug: str
    auth_method: str
    amr: list[str]


async def complete_login(
    async_dal: Any,
    dal: Any,
    envelope_runtime: Any | None,
    *,
    token: str,
    method: str,
    code: str,
) -> CompletedLogin:
    """Verify the second factor for a pending login and consume the challenge (single use)."""
    if method not in METHODS:
        raise bad_request("Unknown verification method")
    challenge = await _login_challenge(async_dal, dal, token)
    user_id = int(challenge.user_id)
    slug = str(challenge.tenant_slug or DEFAULT_TENANT_SLUG)
    await _limit(f"verify:{user_id}", USER_VERIFY_ATTEMPTS, USER_VERIFY_WINDOW_SECONDS)
    allowed = await login_methods(async_dal, dal, user_id=user_id, tenant_slug=slug)
    started = time.perf_counter()
    with mfa_span("mfa.login.verify", method=method):
        ok = False
        if method in allowed:
            if method == METHOD_TOTP:
                ok = await _verify_totp(
                    async_dal, dal, envelope_runtime, user_id=user_id, code=code
                )
            elif method == METHOD_EMAIL:
                ok = await _check_email_code(async_dal, dal, challenge=challenge, code=code)
            else:
                ok = await _consume_recovery_code(async_dal, dal, user_id, code)
    verify_duration.record(time.perf_counter() - started, {"method": method})
    if not ok:
        await _record_failure(async_dal, dal, challenge)
        locked = int(challenge.attempts) + 1 >= MAX_ATTEMPTS
        reason = "locked" if locked else "invalid_code"
        verify_counter.add(1, {"method": method, "purpose": "login", "outcome": reason})
        await record_mfa_event(
            user_id=user_id,
            category=AuditCategory.AUTHN,
            action=AuditAction.MFA_FAILED,
            outcome=AuditOutcome.DENIED,
            method=method,
            reason=reason,
            tenant_slug=slug,
        )
        logger.warning("mfa.login.failed user_id=%d method=%s reason=%s", user_id, method, reason)
        raise _challenge_invalid() if locked else _invalid_code()
    t = dal.hub_mfa_challenges
    consumed = await async_dal.update_async(
        (t.id == challenge.id) & (t.consumed_at == None),  # noqa: E711
        consumed_at=_now(),
    )
    if consumed != 1:
        raise _challenge_invalid()
    verify_counter.add(1, {"method": method, "purpose": "login", "outcome": "ok"})
    await record_mfa_event(
        user_id=user_id,
        category=AuditCategory.AUTHN,
        action=AuditAction.MFA_VERIFIED,
        outcome=AuditOutcome.SUCCESS,
        method=method,
        tenant_slug=slug,
    )
    logger.info("mfa.login.verified user_id=%d method=%s", user_id, method)
    return CompletedLogin(
        user_id=user_id,
        tenant_id=int(challenge.tenant_id) if challenge.tenant_id is not None else None,
        tenant_slug=slug,
        auth_method=str(challenge.auth_method or "password"),
        amr=list(_AMR[method]),
    )


# -- disable / regenerate (re-auth + a valid code) -----------------------------------------------


async def _reauthenticate(
    user: Any, *, password: str | None, claims: dict[str, Any] | None
) -> None:
    """Prove "it's really you": the current password, or (password-less) a fresh sign-in."""
    if user.password_hash:
        # Imported here: auth_service imports this module for the login MFA step.
        from services.auth_service import _verify_password  # noqa: PLC0415

        if not password or not await _verify_password(password, str(user.password_hash)):
            raise ApiError("Current password is incorrect", 401, "REAUTH_FAILED")
        return
    auth_time = (claims or {}).get("auth_time")
    if (
        isinstance(auth_time, bool)
        or not isinstance(auth_time, int | float)
        or time.time() - float(auth_time) > REAUTH_MAX_AGE_SECONDS
    ):
        raise ApiError("Please sign in again to continue", 403, "REAUTH_REQUIRED")


async def _verify_any_factor(
    async_dal: Any, dal: Any, envelope_runtime: Any | None, *, user_id: int, code: str
) -> str | None:
    """Accept a code from any active factor (TOTP, emailed account code, recovery code)."""
    row = await _mfa_row(async_dal, dal, user_id)
    methods = active_methods(row)
    if METHOD_TOTP in methods and totp.normalize_digits(code) is not None:
        if await _verify_totp(async_dal, dal, envelope_runtime, user_id=user_id, code=code):
            return METHOD_TOTP
    if METHOD_EMAIL in methods and totp.normalize_digits(code) is not None:
        if await _verify_account_email_code(async_dal, dal, user_id=user_id, code=code):
            return METHOD_EMAIL
    if totp.normalize_digits(code) is None and await _consume_recovery_code(
        async_dal, dal, user_id, code
    ):
        return METHOD_RECOVERY
    return None


async def _reauth_and_factor(
    async_dal: Any,
    dal: Any,
    envelope_runtime: Any | None,
    *,
    user_id: int,
    password: str | None,
    code: str,
    claims: dict[str, Any] | None,
) -> str:
    """Shared gate for sensitive MFA changes; returns the factor that satisfied it."""
    await _limit(f"verify:{user_id}", USER_VERIFY_ATTEMPTS, USER_VERIFY_WINDOW_SECONDS)
    user = await _user_row(async_dal, dal, user_id)
    await _reauthenticate(user, password=password, claims=claims)
    factor = await _verify_any_factor(async_dal, dal, envelope_runtime, user_id=user_id, code=code)
    if factor is None:
        verify_counter.add(1, {"method": "any", "purpose": "account", "outcome": "invalid"})
        raise _invalid_code()
    verify_counter.add(1, {"method": factor, "purpose": "account", "outcome": "ok"})
    return factor


async def disable_method(
    async_dal: Any,
    dal: Any,
    envelope_runtime: Any | None,
    *,
    user_id: int,
    method: str,
    password: str | None,
    code: str,
    claims: dict[str, Any] | None,
    tenant_slug: str,
) -> None:
    """Turn off one method after re-auth + a valid code; drop recovery codes with the last one."""
    if method not in (METHOD_TOTP, METHOD_EMAIL):
        raise bad_request("Unknown MFA method")
    row = await _mfa_row(async_dal, dal, user_id)
    status = (row.totp_status if method == METHOD_TOTP else row.email_status) if row else 0
    if status == STATUS_NONE:
        raise bad_request("That method is not enabled")
    if status == STATUS_ACTIVE:
        await _reauth_and_factor(
            async_dal,
            dal,
            envelope_runtime,
            user_id=user_id,
            password=password,
            code=code,
            claims=claims,
        )
    # A PENDING (never confirmed) method protects nothing yet: cancelling it needs no factor.
    if method == METHOD_TOTP:
        fields: dict[str, Any] = {
            "totp_status": STATUS_NONE,
            "totp_secret_enc": None,
            "totp_last_step": None,
            "totp_confirmed_at": None,
        }
    else:
        fields = {"email_status": STATUS_NONE, "email_confirmed_at": None}
    await _upsert_mfa_row(async_dal, dal, user_id, **fields)
    if not active_methods(await _mfa_row(async_dal, dal, user_id)):
        await async_dal.delete_async(dal.hub_user_mfa_recovery_codes.user_id == user_id)
    enrollment_counter.add(1, {"method": method, "operation": "disable"})
    if status == STATUS_ACTIVE:
        await record_mfa_event(
            user_id=user_id,
            category=AuditCategory.CREDENTIAL,
            action=AuditAction.CREDENTIAL_MFA_DISABLED,
            outcome=AuditOutcome.SUCCESS,
            method=method,
            tenant_slug=tenant_slug,
        )
    logger.info("mfa.disabled user_id=%d method=%s", user_id, method)


async def regenerate_recovery_codes(
    async_dal: Any,
    dal: Any,
    envelope_runtime: Any | None,
    *,
    user_id: int,
    password: str | None,
    code: str,
    claims: dict[str, Any] | None,
    tenant_slug: str,
) -> list[str]:
    """Replace every recovery code (re-auth + a valid code); returns the new ones once."""
    if not active_methods(await _mfa_row(async_dal, dal, user_id)):
        raise bad_request("Enable a verification method first")
    await _reauth_and_factor(
        async_dal,
        dal,
        envelope_runtime,
        user_id=user_id,
        password=password,
        code=code,
        claims=claims,
    )
    codes = await _replace_recovery_codes(async_dal, dal, user_id)
    enrollment_counter.add(1, {"method": METHOD_RECOVERY, "operation": "regenerate"})
    await record_mfa_event(
        user_id=user_id,
        category=AuditCategory.CREDENTIAL,
        action=AuditAction.CREDENTIAL_MFA_RECOVERY_REGENERATED,
        outcome=AuditOutcome.SUCCESS,
        method=METHOD_RECOVERY,
        tenant_slug=tenant_slug,
    )
    return codes
