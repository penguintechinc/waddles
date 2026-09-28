"""Mints ephemeral pseudonyms for unknown/unlinked platform identities.

`POST /api/v1/internal/identities/ephemeral` -- inside the PII boundary,
never locally computable by `core/svc_process`.

Security review fix to PR #429 (`docs/superpowers/specs/2026-09-28-bundle-
permissions-and-capability-gate.md` S10.1/S10.3): the original pseudonym
was `UUIDv5(FIXED_PUBLIC_NAMESPACE, "platform:handle")`, computed locally
in `core/svc_process` -- reversible by dictionary attack (anyone can
recompute the same fixed, public derivation for a known handle, breaking
the whole point of pseudonymization). The pseudonym is now
`HMAC-SHA256(<per-tenant secret>, "platform:platform_user_id")`, minted
here, formatted as a UUID -- the secret (`EPHEMERAL_IDENTITY_HMAC_MASTER_
KEY`, hub-api's own env/secret store) never leaves this process, so
`core/svc_process` cannot recompute a pseudonym it hasn't been handed, and
neither can anyone else.

Service-to-service only, same `X-Service-Key` pattern as
`community_music_queue.py`'s `music_internal_bp` -- but with a DEDICATED
credential (`IDENTITY_SERVICE_API_KEY`, distinct from the general
`SERVICE_API_KEY` every other internal blueprint in this port shares):
least-privilege, a compromised caller of any other internal endpoint
gains no ability to mint pseudonyms.

Upserts `ephemeral_identities` (migration `097_ephemeral_identities.sql`)
-- `UNIQUE (tenant_id, platform, platform_user_id)`, `last_seen`/
`expires_at` bumped on every call, `pseudonym` never overwritten (the
HMAC derivation is already deterministic per `(tenant_id, platform,
platform_user_id)`, so a conflicting insert would compute the identical
value anyway). Part of the PII store: covered by the existing DSAR/
erasure job, plus its own independent `expires_at` TTL (unlinked
identities nobody claims eventually age out).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from quart import Blueprint, current_app, request

from services.errors import ApiError, bad_request

identity_internal_bp = Blueprint("v1_identity_internal", __name__, url_prefix="/api/v1/internal")

#: 30 days -- an unlinked/unknown platform identity nobody ever claims
#: (OAuth-links) ages out on its own, independent of any erasure request.
_TTL = timedelta(days=30)

_MASTER_KEY_ENV = "EPHEMERAL_IDENTITY_HMAC_MASTER_KEY"
_MASTER_KEY_HEX_LENGTH = 64


class EphemeralIdentityKeyError(ValueError):
    """`EPHEMERAL_IDENTITY_HMAC_MASTER_KEY` is missing or malformed."""


def _master_key() -> bytes:
    """Returns the 32-byte master key.

    Fails closed, mirrors `services.bundle_secret_crypto._get_key`'s
    exact contract (never a silently-insecure default).
    """
    hex_key = os.environ.get(_MASTER_KEY_ENV, "")
    if len(hex_key) != _MASTER_KEY_HEX_LENGTH:
        raise EphemeralIdentityKeyError(
            f"{_MASTER_KEY_ENV} must be a {_MASTER_KEY_HEX_LENGTH}-character hex string"
        )
    return bytes.fromhex(hex_key)


def _per_tenant_secret(tenant_id: int) -> bytes:
    """Derives a per-tenant secret from the single master key.

    Avoids a separate `tenant_secrets` table while still giving every
    tenant a cryptographically distinct key (test requirement: the same
    handle in two different tenants must mint two different pseudonyms).
    """
    return hmac.new(_master_key(), str(tenant_id).encode("utf-8"), hashlib.sha256).digest()


def derive_pseudonym(tenant_id: int, platform: str, platform_user_id: str) -> uuid.UUID:
    """Derives the ephemeral pseudonym for one platform identity.

    `HMAC-SHA256(per-tenant secret, "platform:platform_user_id")`,
    formatted as a UUID (the first 16 bytes of the digest, RFC 4122
    version/variant bits set so it round-trips through the same
    `{user:<uuid-v4-or-v5-shape>}` grammar every other token already
    uses -- this is not a "real" v4/v5 UUID, just UUID-*shaped*, which is
    all the wire grammar requires).
    """
    secret = _per_tenant_secret(tenant_id)
    digest = hmac.new(secret, f"{platform}:{platform_user_id}".encode(), hashlib.sha256).digest()
    raw = bytearray(digest[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40  # version 4
    raw[8] = (raw[8] & 0x3F) | 0x80  # variant 10
    return uuid.UUID(bytes=bytes(raw))


def ensure_ephemeral_identities_table(dal: Any, *, migrate: bool = False) -> None:
    """Idempotently binds the `ephemeral_identities` pydal table.

    `app.py` is frozen (see `services.community_common.
    ensure_community_tables`'s identical rationale), so every handler
    binds its own table on first use. `migrate=False` in production
    (schema owned by migration `097_ephemeral_identities.sql`); tests
    pass `migrate=True` against a throwaway `sqlite:memory`/file DAL,
    same convention as `ensure_community_tables`.
    """
    if "ephemeral_identities" not in dal.tables:
        dal.define_table(
            "ephemeral_identities",
            dal.Field("tenant_id", "integer", notnull=True),
            dal.Field("pseudonym", "string", length=36, notnull=True),
            dal.Field("platform", "string", length=50, notnull=True),
            dal.Field("platform_user_id", "string", length=255, notnull=True),
            dal.Field("handle", "string", length=255),
            dal.Field("last_seen", "datetime", notnull=True),
            dal.Field("expires_at", "datetime", notnull=True),
            migrate=migrate,
        )
        if migrate:
            # pydal's own `define_table` has no composite-unique-index
            # primitive -- production's real UNIQUE constraint comes from
            # migration `097_ephemeral_identities.sql` itself; this is the
            # test-only equivalent so `ON CONFLICT (tenant_id, platform,
            # platform_user_id)` below has a matching index to target
            # against a throwaway `migrate=True` sqlite test DB.
            dal.executesql(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_ephemeral_identities_scope "
                "ON ephemeral_identities (tenant_id, platform, platform_user_id)"
            )


def is_valid_identity_service_key(req: Any) -> bool:
    """Checks the dedicated-scope `X-Service-Key` header.

    Checked against `IDENTITY_SERVICE_API_KEY` -- deliberately NOT
    `services.community_common.is_valid_service_key`'s shared
    `SERVICE_API_KEY` (least privilege: a compromised caller of any other
    internal endpoint must not be able to mint pseudonyms). Fails closed:
    no key configured means every request is rejected, never silently
    allowed.
    """
    from flask_core.auth import verify_service_key

    provided = req.headers.get("X-Service-Key", "")
    expected = os.environ.get("IDENTITY_SERVICE_API_KEY")
    return bool(verify_service_key(provided, expected))


def mint_or_touch_ephemeral_identity(
    dal: Any,
    *,
    tenant_id: int,
    platform: str,
    platform_user_id: str,
    handle: str | None,
    migrate: bool = False,
) -> uuid.UUID:
    """Upserts `ephemeral_identities` and returns the pseudonym.

    `ON CONFLICT ... DO UPDATE` (real upsert, not the select-then-branch
    idiom `services/cookie_consent_service.py` uses elsewhere in this
    port) -- concurrent mints for the same identity from multiple
    `svc_process` instances must not race into two rows.
    """
    ensure_ephemeral_identities_table(dal, migrate=migrate)
    pseudonym = derive_pseudonym(tenant_id, platform, platform_user_id)
    now = datetime.now(UTC)
    expires_at = now + _TTL
    dal.executesql(
        """
        INSERT INTO ephemeral_identities
          (tenant_id, pseudonym, platform, platform_user_id, handle, last_seen, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (tenant_id, platform, platform_user_id)
        DO UPDATE SET
          handle = COALESCE($5, ephemeral_identities.handle),
          last_seen = $6,
          expires_at = $7
        """,
        placeholders=[
            tenant_id,
            str(pseudonym),
            platform,
            platform_user_id,
            handle,
            now,
            expires_at,
        ],
    )
    dal.commit()
    return pseudonym


@identity_internal_bp.route("/identities/ephemeral", methods=["POST"])
async def mint_ephemeral_identity() -> tuple[dict[str, Any], int]:
    """`POST /api/v1/internal/identities/ephemeral`.

    Body `{tenant_id, platform, platform_user_id, handle}`. `handle` is
    optional (an actor with no id at all falls back to an
    `actor:<name>`-shaped key on the caller's side, still passed through
    here as `platform_user_id`).
    """
    if not is_valid_identity_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    body = await request.get_json(force=True, silent=True) or {}
    tenant_id = body.get("tenant_id")
    platform = body.get("platform")
    platform_user_id = body.get("platform_user_id")
    handle = body.get("handle")

    if not isinstance(tenant_id, int) or tenant_id < 0:
        return _err(bad_request("tenant_id must be a non-negative integer"))
    if not platform or not isinstance(platform, str):
        return _err(bad_request("platform is required"))
    if not platform_user_id or not isinstance(platform_user_id, str):
        return _err(bad_request("platform_user_id is required"))
    if handle is not None and not isinstance(handle, str):
        return _err(bad_request("handle must be a string when present"))

    dal = current_app.config["dal"]
    try:
        pseudonym = mint_or_touch_ephemeral_identity(
            dal,
            tenant_id=tenant_id,
            platform=platform,
            platform_user_id=platform_user_id,
            handle=handle,
        )
    except EphemeralIdentityKeyError:
        current_app.logger.error(
            "EPHEMERAL_IDENTITY_HMAC_MASTER_KEY missing/malformed; cannot mint pseudonyms"
        )
        return {"success": False, "error": "identity service misconfigured"}, 500

    return {"success": True, "data": {"pseudonym": str(pseudonym)}, "meta": {"version": 1}}, 200


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return {
        "success": False,
        "error": {"message": exc.message, "code": exc.code},
    }, exc.status_code


BLUEPRINTS: list[Blueprint] = [identity_internal_bp]
