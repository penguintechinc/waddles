"""WebAuthn passkey registration/login -- ported from `passkeyController.js`.

Node uses `@simplewebauthn/server`; this port uses the wire-protocol-
compatible Python equivalent, `webauthn` (duo-labs/py_webauthn) -- same
FIDO2/WebAuthn spec, same JSON options shape sent to the browser's
`@simplewebauthn/browser` client, so the frontend is unchanged.

Challenge storage: Node keeps an in-process `Map` with an explicit code
comment flagging it as single-instance-only ("replace with Redis/DB for
multi-instance prod"). Ported faithfully with the same module-level dict
and the same caveat -- introducing Redis-backed storage here would be a
behavior change beyond what "port the Node controller" asks for, and
`HubAPIConfig` has no Redis wiring yet. Tracked in `hub_api/PORTING.md`
as a known follow-up, not a silently new gap.
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import UTC, datetime
from typing import Any, cast

import webauthn
from webauthn.helpers import base64url_to_bytes, options_to_json
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from config import HubAPIConfig
from services.errors import bad_request, not_found, unauthorized

RP_NAME = os.getenv("PASSKEY_RP_NAME", "Waddles")
RP_ID = os.getenv("PASSKEY_RP_ID", "localhost")
ORIGIN = os.getenv("PASSKEY_ORIGIN", "http://localhost:5173")
_CHALLENGE_TTL_SECONDS = 5 * 60

#: user_id -> (challenge_bytes, expires_at_epoch) for registration.
_registration_challenges: dict[int, tuple[bytes, float]] = {}
#: challenge_str -> expires_at_epoch for authentication (keyed by challenge itself,
#: matching Node's "look for any matching auth_ key" fallback -- single
#: outstanding login challenge assumption, ported as-is).
_authentication_challenges: dict[str, float] = {}


def _purge_expired(store: dict[Any, Any], now: float) -> None:
    expired = [k for k, v in store.items() if (v[1] if isinstance(v, tuple) else v) < now]
    for k in expired:
        del store[k]


async def start_registration(async_dal: Any, dal: Any, *, user_id: int) -> dict[str, Any]:
    """Start registration."""
    rows = await async_dal.select_async(dal(dal.hub_users.id == user_id))
    if not rows:
        raise not_found("User not found")
    user = rows.first()

    existing = await async_dal.select_async(dal(dal.user_passkeys.user_id == user_id))
    exclude = [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(r.credential_id)) for r in existing
    ]

    options = webauthn.generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=str(user.id).encode(),
        user_name=user.email or user.username or str(user.id),
        user_display_name=user.username or user.email or str(user.id),
        exclude_credentials=exclude,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )

    now = time.time()
    _purge_expired(_registration_challenges, now)
    _registration_challenges[user_id] = (options.challenge, now + _CHALLENGE_TTL_SECONDS)

    return cast(dict[str, Any], json.loads(options_to_json(options)))


async def finish_registration(
    async_dal: Any, dal: Any, *, user_id: int, credential: dict[str, Any], device_name: str | None
) -> None:
    """Finish registration."""
    entry = _registration_challenges.get(user_id)
    if entry is None or entry[1] < time.time():
        raise bad_request("Registration challenge expired or not started")
    expected_challenge, _ = entry

    try:
        verification = webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=expected_challenge,
            expected_origin=ORIGIN,
            expected_rp_id=RP_ID,
        )
    except Exception as exc:  # noqa: BLE001 - any verification failure is a 400
        raise bad_request("Passkey verification failed") from exc

    await async_dal.insert_async(
        dal.user_passkeys,
        user_id=user_id,
        credential_id=_b64url(verification.credential_id),
        public_key=_b64url(verification.credential_public_key),
        sign_count=verification.sign_count,
        device_name=device_name or "Passkey",
        created_at=datetime.now(UTC),
    )
    _registration_challenges.pop(user_id, None)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


async def start_login() -> dict[str, Any]:
    """Start login."""
    options = webauthn.generate_authentication_options(
        rp_id=RP_ID, user_verification=UserVerificationRequirement.PREFERRED
    )
    now = time.time()
    _purge_expired(_authentication_challenges, now)
    _authentication_challenges[options.challenge.hex()] = now + _CHALLENGE_TTL_SECONDS
    return cast(dict[str, Any], json.loads(options_to_json(options)))


async def finish_login(
    async_dal: Any, dal: Any, cfg: HubAPIConfig, *, credential: dict[str, Any]
) -> tuple[str, Any]:
    """Verify a passkey assertion and mint a session JWT. Returns `(token, user_row)`."""
    from services.auth_service import SessionUser, create_session_token

    credential_id = credential.get("id")
    if not credential_id:
        raise bad_request("Missing credential")

    rows = await async_dal.executesql_async(
        "SELECT pk.id, pk.credential_id, pk.public_key, pk.sign_count, "
        "u.id, u.email, u.username, u.avatar_url, u.is_super_admin, u.is_vendor, "
        "u.is_analytics_consumer "
        "FROM user_passkeys pk JOIN hub_users u ON u.id = pk.user_id "
        "WHERE pk.credential_id = %s",
        [credential_id],
    )
    if not rows:
        raise unauthorized("Passkey not found")
    (
        pk_id,
        _stored_credential_id,
        public_key,
        sign_count,
        uid,
        email,
        username,
        avatar_url,
        is_super_admin,
        is_vendor,
        is_analytics_consumer,
    ) = rows[0]

    now = time.time()
    _purge_expired(_authentication_challenges, now)
    if not _authentication_challenges:
        raise bad_request("Authentication challenge expired")
    # Matches Node's own fallback: pick any outstanding auth challenge
    # rather than keying strictly off the credential response.
    challenge_hex = next(iter(_authentication_challenges))
    expected_challenge = bytes.fromhex(challenge_hex)

    try:
        verification = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=expected_challenge,
            expected_origin=ORIGIN,
            expected_rp_id=RP_ID,
            credential_public_key=base64url_to_bytes(public_key),
            credential_current_sign_count=sign_count,
        )
    except Exception as exc:  # noqa: BLE001
        raise unauthorized("Passkey verification failed") from exc

    await async_dal.update_async(
        dal.user_passkeys.id == pk_id,
        sign_count=verification.new_sign_count,
        last_used_at=datetime.now(UTC),
    )
    _authentication_challenges.clear()

    user = SessionUser(
        id=uid,
        email=email,
        username=username,
        avatar_url=avatar_url,
        is_super_admin=bool(is_super_admin),
        is_vendor=bool(is_vendor),
        is_analytics_consumer=bool(is_analytics_consumer),
    )
    token = await create_session_token(async_dal, dal, cfg, user=user, auth_method="passkey")
    return token, user


async def list_credentials(async_dal: Any, dal: Any, *, user_id: int) -> list[Any]:
    """List credentials."""
    rows = await async_dal.select_async(
        dal(dal.user_passkeys.user_id == user_id), orderby=~dal.user_passkeys.created_at
    )
    return list(rows)


async def remove_credential(
    async_dal: Any, dal: Any, *, user_id: int, credential_pk_id: int
) -> None:
    """Remove credential."""
    await async_dal.delete_async(
        (dal.user_passkeys.id == credential_pk_id) & (dal.user_passkeys.user_id == user_id)
    )
