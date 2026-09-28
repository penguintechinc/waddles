"""v1 `internal users` group -- service-only display-name resolution for chat egress.

Mounted at `/api/v1/internal/users*`. The sole consumer is `core/svc_action`'s
`HubUsersResolver` (`core/svc_action/src/detokenize.rs`), resolving
`{user:<uuid>}` egress-detokenizer tokens (spec `docs/superpowers/specs/
2026-09-28-bundle-permissions-and-capability-gate.md` S10.4) to display
names without ever handing a raw `hub_users` row -- or PII beyond a display
name -- outside the PII boundary (`rules/critical-rules.md` PII
Tokenization: hub-api is the boundary; every caller outside it, including
every stage-runner, gets UUID/display-name only, never raw identity rows).

Auth: `@tenant_middleware` (tenant strictly from the caller's own service
JWT `tenant` claim -- security.md Tenant Isolation; the request body never
carries a tenant, unlike this endpoint's earlier sketch, precisely to avoid
the "trust tenant from body" anti-pattern `distribution.py`'s own docstring
calls out) + `@require_scope("users:display-name:resolve")`, a scope no
user-facing token is ever minted with -- only `core/svc_action`'s own
service JWT (`crate::detokenize::mint_service_jwt`) requests it, so this
route is unreachable by user tokens by construction. It is also not
externally routable: `k8s/helm/waddlebot/templates/ingress.yaml`'s
`.Values.ingress.hosts[].paths` (`values.yaml`) exposes only `/` and
`/api/router` (the webui/gateway paths) -- no ingress path names hub-api's
own service directly, so `/api/v1/internal/*` is reachable only from
inside the cluster's network. This endpoint does not add its own
NetworkPolicy beyond that -- a follow-up hardening item, same posture the
rest of hub-api's `/api/v1/internal/*`-shaped internal surface already
has.

Rate-limited (`flask_core.api_utils.rate_limit`) and audit-logged with
counts only, never the UUIDs or resolved names themselves (`services.
internal_identity_service.resolve_display_names`'s own logging).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from flask_core.api_utils import error_response, rate_limit
from flask_core.authz import require_scope
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_response

from services import internal_identity_service as svc
from services.errors import ApiError, bad_request

internal_users_bp = Blueprint("v1_internal_users", __name__, url_prefix="/api/v1/internal/users")


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the flask_core `error_response()` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


@dataclass(slots=True, frozen=True)
class DisplayNamesMetaDTO:
    """Response metadata -- backend.md API response format's `meta` block."""

    version: int
    timestamp: str


@dataclass(slots=True, frozen=True)
class DisplayNamesResponse:
    """Response DTO for `POST /api/v1/internal/users/display-names`.

    `display_names` maps a requested UUID string to its display name --
    entries for a cross-tenant, erased, unknown, or malformed UUID are
    simply absent (never a per-entry error, never the UUID echoed back
    unresolved).
    """

    success: bool
    display_names: dict[str, str] = field(default_factory=dict)
    meta: DisplayNamesMetaDTO | None = None


def _parse_user_uuids(body: Any) -> list[str]:
    """Validates the request body's `user_uuids` field; raises `ApiError` on a bad shape."""
    if not isinstance(body, dict):
        raise bad_request("request body must be a JSON object")
    user_uuids = body.get("user_uuids")
    if not isinstance(user_uuids, list) or not user_uuids:
        raise bad_request("user_uuids must be a non-empty list of UUID strings")
    if len(user_uuids) > svc.MAX_USER_UUIDS:
        raise bad_request(f"user_uuids exceeds the maximum of {svc.MAX_USER_UUIDS} per request")
    if not all(isinstance(u, str) for u in user_uuids):
        raise bad_request("user_uuids must contain only strings")
    return cast(list[str], user_uuids)


@internal_users_bp.route("/display-names", methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope("users:display-name:resolve")  # type: ignore[untyped-decorator]
@rate_limit(requests_per_minute=120)  # type: ignore[untyped-decorator]
@validate_response(DisplayNamesResponse)
async def resolve_display_names() -> DisplayNamesResponse | tuple[dict[str, object], int]:
    """Batched, tenant-scoped `{uuid: display_name}` resolution for chat-egress detokenization."""
    body = await request.get_json(silent=True)
    try:
        user_uuids = _parse_user_uuids(body)
    except ApiError as exc:
        return _err(exc)

    ctx = get_tenant_context(request)
    assert ctx is not None  # tenant_middleware always publishes this on the success path

    dal = current_app.config["dal"]
    names = await svc.resolve_display_names(dal, tenant_id=ctx.tenant_id, user_uuids=user_uuids)

    return DisplayNamesResponse(
        success=True,
        display_names=names,
        meta=DisplayNamesMetaDTO(version=1, timestamp=datetime.now(UTC).isoformat()),
    )


BLUEPRINTS: list[Blueprint] = [internal_users_bp]
