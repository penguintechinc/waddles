"""v1 `internal.keys` group -- data-plane tenant-DEK broker endpoint.

`POST /api/v1/internal/keys/tenant-dek` is the distribution point for
`services.tenant_keystore.TenantKeystore`: `svc_ingest`/`svc_action`/
`svc_process` (and any future data-plane consumer) call this instead of
ever touching the `keystore` schema or a KEK directly -- "hub-api
decrypts/unwraps, everyone else asks" (spec Sec5).

**Auth dependency (blocking, see PR description):** authenticated with
the EdDSA machine-JWT `ServiceJwtVerifier` from `flask_core.service_jwt`,
introduced on `feature/eddsa-machine-jwt` (PR #438), which is NOT merged
as of this PR. `flask_core.service_jwt` doesn't exist on this branch, so
the import below is wrapped and this blueprint is skipped (with a startup
warning) in `app.py` until #438 lands. Required scope is
`keys:tenant-dek:read:<purpose>`, one-per-purpose -- this route verifies
manually (`verifier.verify(token, required_scope=...)`) rather than via
the fixed-string `require_service_scope` decorator, because the required
scope depends on the request body's `purpose` field, known only after
parsing it; a service holding only the `message_content` scope cannot
fetch the `identity` purpose's key.

Never logs, audits, or returns key material beyond the one
service-scoped, wrapped-for-transport response body of the call that
asked for it -- the audit row records `tenant_id`/`purpose`/`version`/
`service_id` only.
"""

from __future__ import annotations

from typing import Any, cast

from flask_core.api_utils import error_response
from quart import Blueprint, current_app, request

from services.errors import ApiError, bad_request
from services.tenant_keystore import (
    TenantKeyNotFound,
    TenantKeyShredded,
    TenantKeystore,
)

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


async def _authenticate(purpose: str) -> tuple[dict[str, Any], int] | None:
    """Verify the caller's machine JWT against `keys:tenant-dek:read:<purpose>`.

    Returns an error `(body, status)` tuple to short-circuit the route, or
    `None` if the caller is authorized. Fails closed (503) if PR #438
    isn't merged yet -- never silently allows an unauthenticated call.
    """
    if not SERVICE_JWT_AVAILABLE:
        return {"error": "service_jwt_unavailable", "detail": "PR #438 not yet merged"}, 503

    verifier: ServiceJwtVerifier = current_app.config["SERVICE_JWT_VERIFIER"]
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return {"error": "missing bearer token"}, 401
    token = auth_header[len("Bearer ") :]
    try:
        verifier.verify(token, required_scope=f"keys:tenant-dek:read:{purpose}")
    except (UnknownKeyId, InvalidServiceToken):
        return {"error": "unauthorized"}, 401
    return None


internal_keys_bp = Blueprint("v1_internal_keys", __name__, url_prefix="/api/v1/internal")

_VALID_PURPOSES = frozenset({"message_content", "connection_credentials", "identity"})


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
    """`POST /api/v1/internal/keys/tenant-dek {tenant_id, purpose, version?}`.

    Returns the tenant's DEK wrapped for the calling service (never
    plaintext over the wire), scoped to `purpose` via the caller's
    `keys:tenant-dek:read:<purpose>` machine-JWT scope -- a service
    holding the `message_content` scope cannot request the `identity`
    purpose's key, matching the spec's per-purpose distribution model.

    Rate-limited by `services/rate_limiting.py`'s existing global
    `before_request` hook (installed once in `app.py`, in front of every
    route) -- not a per-route decorator, matching that module's own
    "one call site, not 449 individual ones" rationale.
    """
    body = await request.get_json(silent=True) or {}
    tenant_id = body.get("tenant_id")
    purpose = body.get("purpose")
    version = body.get("version")

    if not isinstance(tenant_id, int) or tenant_id <= 0:
        return _err(bad_request("tenant_id must be a positive integer"))
    if purpose not in _VALID_PURPOSES:
        return _err(bad_request(f"purpose must be one of {sorted(_VALID_PURPOSES)}"))
    if version is not None and (not isinstance(version, int) or version <= 0):
        return _err(bad_request("version must be a positive integer when given"))

    auth_error = await _authenticate(purpose)
    if auth_error is not None:
        return auth_error

    keystore: TenantKeystore = current_app.config["tenant_keystore"]
    service_id = getattr(request, "service_identity", "unknown")

    try:
        dek, record = await keystore.get_dek(tenant_id, version=version)
    except TenantKeyShredded:
        await _audit(tenant_id, purpose, version, service_id)
        return {"error": "tenant_key_shredded"}, 410
    except TenantKeyNotFound:
        return _err(bad_request(f"no encryption key for tenant {tenant_id}"))

    await _audit(tenant_id, purpose, version, service_id)

    wrapped_for_transport = await keystore.kek_provider.wrap(tenant_id, dek)
    return {
        "tenant_id": tenant_id,
        "dek_version": record.dek_version,
        "wrapped_dek": wrapped_for_transport.hex(),
        "kek_kind": record.kek_kind,
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
    "SERVICE_JWT_AVAILABLE",
    "UnknownKeyId",
    "internal_keys_bp",
]
