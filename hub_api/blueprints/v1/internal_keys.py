"""v1 `internal.keys` group -- data-plane `ingest-stream` DEK broker endpoint.

`POST /api/v1/internal/keys/tenant-dek` is the distribution point for
`services.tenant_keystore.TenantKeystore`'s **`ingest-stream` purpose
only** -- `svc_ingest` (encrypts) / `svc_process` (decrypts) call this
instead of ever touching the `keystore` schema or a KEK directly.

**Amendment 2026-09-28 (post security-review, PR #442):** the original
version of this endpoint served the `at-rest` DEK (`message_content`/
`connection_credentials`/`identity` purposes) to any caller presenting a
matching scope, and "wrapped for transport" it under the same platform
KEK the caller had no way to unwrap -- both findings from the security
review. Per `docs/superpowers/specs/2026-09-28-tenant-envelope-
encryption-design.md` Sec5a/5b: the `at-rest` DEK NEVER leaves hub-api
(unchanged, spec Sec5 intro); this endpoint now issues only the
purpose-limited `ingest-stream` DEK, sealed to the caller's ephemeral
X25519 public key (Sec5b), never platform-KEK-wrapped.

**Auth dependency (blocking, see PR description):** authenticated with
the EdDSA machine-JWT `ServiceJwtVerifier` from `flask_core.service_jwt`,
introduced on `feature/eddsa-machine-jwt` (PR #438), which is NOT merged
as of this PR. `flask_core.service_jwt` doesn't exist on this branch, so
the import below is wrapped and this blueprint is skipped (with a startup
warning) in `app.py` until #438 lands.

Never logs, audits, or returns key material beyond the one
service-scoped, sealed response body of the call that asked for it -- the
audit row records `tenant_id`/`purpose`/`version`/`service_id` only.
"""

from __future__ import annotations

import base64
from typing import Any, cast

from flask_core.api_utils import error_response
from quart import Blueprint, current_app, request

from services.errors import ApiError, bad_request
from services.stream_key_transport import StreamKeySealError, seal_stream_key
from services.tenant_keystore import (
    PURPOSE_INGEST_STREAM,
    TenantKeyNotFound,
    TenantKeyShredded,
    TenantKeystore,
    is_service_allowed,
)
from services.tenant_keystore_metrics import ServiceTenantFanoutTracker

try:  # pragma: no cover - exercised once PR #438 merges; see module docstring.
    from flask_core.service_jwt import (
        InvalidServiceToken,
        ServiceJwtVerifier,
        UnknownKeyId,
    )

    SERVICE_JWT_AVAILABLE = True
except ImportError:  # pragma: no cover
    SERVICE_JWT_AVAILABLE = False
    ServiceJwtVerifier = Any

    class InvalidServiceToken(Exception):  # type: ignore[no-redef]  # noqa: N818 - mirrors flask_core's real name
        """Placeholder until PR #438 merges."""

    class UnknownKeyId(Exception):  # type: ignore[no-redef]  # noqa: N818 - mirrors flask_core's real name
        """Placeholder until PR #438 merges."""


#: Every `ingest-stream`-scoped response's cached-key lifetime ceiling
#: (spec Sec5b) -- independent of the key's own 24h rotation/48h grace
#: window; the backstop if a consumer misses `keys:tenant-dek:invalidate`.
MAX_CACHE_TTL_SECONDS = 300

_fanout_tracker = ServiceTenantFanoutTracker()


async def _authenticate(purpose: str) -> tuple[tuple[dict[str, Any], int] | None, dict[str, Any]]:
    """Verify the caller's machine JWT against `keys:tenant-dek:read:<purpose>`.

    Returns `(error_or_None, claims)` -- `claims` is `{}` on any failure
    path. Fails closed (503) if PR #438 isn't merged yet -- never silently
    allows an unauthenticated call. Spec Sec5d: the returned `claims` (not
    a request-object attribute nothing ever sets) is the ONLY source of
    `service_id` for both the purpose-allowlist check and the audit row.
    """
    if not SERVICE_JWT_AVAILABLE:
        return ({"error": "service_jwt_unavailable", "detail": "PR #438 not yet merged"}, 503), {}

    verifier: ServiceJwtVerifier = current_app.config["SERVICE_JWT_VERIFIER"]
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return ({"error": "missing bearer token"}, 401), {}
    token = auth_header[len("Bearer ") :]
    try:
        claims = verifier.verify(token, required_scope=f"keys:tenant-dek:read:{purpose}")
    except (UnknownKeyId, InvalidServiceToken):
        return ({"error": "unauthorized"}, 401), {}
    return None, dict(claims)


internal_keys_bp = Blueprint("v1_internal_keys", __name__, url_prefix="/api/v1/internal")

#: The ONLY purpose this broker ever issues (spec Sec5a) -- the at-rest
#: DEK's `message_content`/`connection_credentials`/`identity` purposes
#: from the pre-security-review version are removed entirely, not just
#: deprioritized: they never leave hub-api (spec Sec5 intro).
_VALID_PURPOSES = frozenset({PURPOSE_INGEST_STREAM})


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


async def _audit(tenant_id: int, purpose: str, version: int | None, service_id: str) -> None:
    """Best-effort audit row -- never includes key material, never blocks the response."""
    dal = current_app.config.get("dal")
    if dal is None:
        return
    try:
        dal.audit_log.insert(
            user_id=None,
            action="internal.keys.tenant_dek.read",
            target_type="tenant",
            target_id=str(tenant_id),
            details={"purpose": purpose, "version": version, "service_id": service_id},
        )
        dal.commit()
    except Exception as exc:  # audit-log failure must never fail the key-fetch response
        logger = current_app.config.get("logger")
        if logger is not None:
            logger.system(
                "internal_keys audit-log write failed",
                action="internal.keys.tenant_dek.read",
                result="DEGRADED",
                extra={"error": str(exc)},
            )


@internal_keys_bp.route("/keys/tenant-dek", methods=["POST"])
async def get_tenant_dek() -> tuple[dict[str, Any], int]:
    """`POST /api/v1/internal/keys/tenant-dek`.

    Body: `{tenant_id, purpose, version?, client_ephemeral_pubkey}`.

    Returns the tenant's `ingest-stream` DEK sealed to the caller's
    ephemeral X25519 public key (spec Sec5b) -- never plaintext or
    platform-KEK-wrapped over the wire. `purpose` must be
    `"ingest-stream"` (the only purpose this endpoint ever serves, spec
    Sec5a) AND the verified caller's `service_id` (spec Sec5d) must be in
    `PURPOSE_SERVICE_CAPABILITIES[purpose]` -- checked server-side,
    independent of (in addition to) the JWT scope check.

    Rate-limited by `services/rate_limiting.py`'s existing global
    `before_request` hook (installed once in `app.py`, in front of every
    route) -- not a per-route decorator, matching that module's own
    "one call site, not 449 individual ones" rationale.
    """
    body = await request.get_json(silent=True) or {}
    tenant_id = body.get("tenant_id")
    purpose = body.get("purpose")
    version = body.get("version")
    client_ephemeral_pubkey_b64 = body.get("client_ephemeral_pubkey")

    if not isinstance(tenant_id, int) or tenant_id <= 0:
        return _err(bad_request("tenant_id must be a positive integer"))
    if purpose not in _VALID_PURPOSES:
        return _err(bad_request(f"purpose must be one of {sorted(_VALID_PURPOSES)}"))
    if version is not None and (not isinstance(version, int) or version <= 0):
        return _err(bad_request("version must be a positive integer when given"))
    if not isinstance(client_ephemeral_pubkey_b64, str) or not client_ephemeral_pubkey_b64:
        return _err(bad_request("client_ephemeral_pubkey (base64) is required"))
    try:
        client_ephemeral_pubkey = base64.b64decode(client_ephemeral_pubkey_b64, validate=True)
    except (ValueError, TypeError):
        return _err(bad_request("client_ephemeral_pubkey must be valid base64"))

    auth_error, claims = await _authenticate(purpose)
    if auth_error is not None:
        return auth_error

    # spec Sec5d: service_id comes ONLY from the verified JWT's `sub`
    # claim -- never a request-object attribute, never request-supplied.
    service_id = claims.get("sub", "unknown")

    # spec Sec5a: server-side purpose->service allowlist, checked in
    # ADDITION to (never instead of) the JWT scope already verified above.
    if not is_service_allowed(purpose, service_id):
        return _err(bad_request(f"service {service_id!r} is not permitted purpose {purpose!r}"))

    keystore: TenantKeystore = current_app.config["tenant_keystore"]

    try:
        dek, record = await keystore.get_dek(tenant_id, purpose=purpose, version=version)
    except TenantKeyShredded:
        await _audit(tenant_id, purpose, version, service_id)
        return {"error": "tenant_key_shredded"}, 410
    except TenantKeyNotFound:
        return _err(bad_request(f"no {purpose} encryption key for tenant {tenant_id}"))

    await _audit(tenant_id, purpose, version, service_id)
    if _fanout_tracker.logger is None:
        _fanout_tracker.logger = current_app.config.get("logger")
    _fanout_tracker.record(service_id, tenant_id)

    info = f"{service_id}|{tenant_id}|{purpose}|{record.dek_version}".encode()
    try:
        sealed = seal_stream_key(
            plaintext_dek=dek, client_ephemeral_pubkey=client_ephemeral_pubkey, info=info
        )
    except StreamKeySealError as exc:
        return _err(bad_request(f"invalid client_ephemeral_pubkey: {exc}"))

    return {
        "tenant_id": tenant_id,
        "purpose": purpose,
        "dek_version": record.dek_version,
        "hub_api_ephemeral_pubkey": base64.b64encode(sealed.hub_api_ephemeral_pubkey).decode(),
        "nonce": base64.b64encode(sealed.nonce).decode(),
        "sealed": base64.b64encode(sealed.sealed).decode(),
        "kek_kind": record.kek_kind,
        "max_cache_ttl_s": MAX_CACHE_TTL_SECONDS,
    }, 200


BLUEPRINTS: list[Blueprint] = [internal_keys_bp]

# Explicit re-export list -- `InvalidServiceToken`/`UnknownKeyId`/
# `SERVICE_JWT_AVAILABLE` are conditionally bound (try/except ImportError
# above), which `mypy --strict`'s implicit-reexport check otherwise flags
# for any importer (e.g. tests/test_internal_keys_api.py simulating PR
# #438 merging).
__all__ = [
    "BLUEPRINTS",
    "InvalidServiceToken",
    "MAX_CACHE_TTL_SECONDS",
    "SERVICE_JWT_AVAILABLE",
    "UnknownKeyId",
    "internal_keys_bp",
]
