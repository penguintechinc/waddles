"""Passkey step-up: re-prove who you are, right before a sensitive action (e.g. a key rotation).

An already-signed-in operator calls `start` (WebAuthn options restricted to THEIR registered
credentials, user verification REQUIRED) and then `finish` with the authenticator's assertion. The
server verifies the assertion -- challenge bound to this user, credential owned by this user,
signature, sign counter, and the authenticator's user-verified flag -- and only then re-issues the
caller's own access token (same session id, tenant, scopes and roles: nothing is escalated) with
fresh `amr` (`[hwk|swk, user, mfa]`) and `auth_time` (now). That token satisfies
`services/mfa_stepup.py` for the next five minutes; a refresh re-mints without the claims.

Challenge storage is in-process and keyed by user (single outstanding challenge per user), the same
caveat `passkey_service` documents: start and finish must reach the same hub-api replica
(session affinity), otherwise finish answers "challenge expired" and the operator simply retries.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any, cast

import webauthn
from flask_core.auth import create_jwt_token
from webauthn.helpers import base64url_to_bytes, options_to_json
from webauthn.helpers.structs import (
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
)

from config import HubAPIConfig
from services.auth_amr import passkey_amr
from services.errors import bad_request, unauthorized
from services.passkey_service import ORIGIN, RP_ID, _is_single_device

logger = logging.getLogger(__name__)

CHALLENGE_TTL_SECONDS = 5 * 60

_lock = threading.Lock()
#: user_id -> (challenge bytes, expires_at epoch).
_challenges: dict[int, tuple[bytes, float]] = {}


def _purge(now: float) -> None:
    """Drop expired challenges (caller holds the lock)."""
    for user_id in [u for u, (_c, exp) in _challenges.items() if exp < now]:
        del _challenges[user_id]


async def start(async_dal: Any, dal: Any, *, user_id: int) -> dict[str, Any]:
    """WebAuthn assertion options for `user_id`'s own credentials, user verification required."""
    rows = await async_dal.select_async(dal(dal.user_passkeys.user_id == user_id))
    allow = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(r.credential_id)) for r in rows]
    if not allow:
        raise bad_request("No passkey is registered for this account")
    options = webauthn.generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    now = time.time()
    with _lock:
        _purge(now)
        _challenges[user_id] = (options.challenge, now + CHALLENGE_TTL_SECONDS)
    return cast(dict[str, Any], json.loads(options_to_json(options)))


async def finish(
    async_dal: Any,
    dal: Any,
    cfg: HubAPIConfig,
    *,
    user_id: int,
    credential: dict[str, Any],
    claims: dict[str, Any],
) -> str:
    """Verify the assertion and return the caller's token re-issued with step-up claims.

    `claims` are the verified claims of the access token presented with the request; they are
    copied (session id, tenant, scope, roles, teams), never widened. Raises 400 for a missing or
    expired challenge, 401 for a credential that is not this user's or fails verification.
    """
    credential_id = credential.get("id")
    if not credential_id:
        raise bad_request("Missing credential")
    with _lock:
        _purge(time.time())
        entry = _challenges.pop(user_id, None)  # single use, even when verification fails
    if entry is None:
        raise bad_request("Step-up challenge expired or not started")
    rows = await async_dal.select_async(
        dal(
            (dal.user_passkeys.user_id == user_id)
            & (dal.user_passkeys.credential_id == credential_id)
        )
    )
    if not rows:
        raise unauthorized("Passkey not found")
    stored = rows.first()
    try:
        verification = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=entry[0],
            expected_origin=ORIGIN,
            expected_rp_id=RP_ID,
            credential_public_key=base64url_to_bytes(stored.public_key),
            credential_current_sign_count=stored.sign_count or 0,
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001 -- any verification failure is a 401
        raise unauthorized("Passkey verification failed") from exc
    await async_dal.update_async(
        dal.user_passkeys.id == stored.id,
        sign_count=verification.new_sign_count,
        last_used_at=datetime.now(UTC),
    )
    now = int(time.time())
    logger.info("step-up verified user_id=%d", user_id)
    return cast(
        str,
        create_jwt_token(
            user_id=str(user_id),
            username=str(claims.get("username") or ""),
            email=str(claims.get("email") or ""),
            roles=list(claims.get("roles") or []),
            secret_key=cfg.secret_key,
            tenant=str(claims["tenant"]),
            scope=str(claims.get("scope") or ""),
            teams=list(claims.get("teams") or []),
            session_id=None if claims.get("sid") is None else str(claims["sid"]),
            issued_at=now,
            amr=passkey_amr(
                user_verified=bool(verification.user_verified),
                single_device=_is_single_device(verification),
            ),
            auth_time=now,
        ),
    )
