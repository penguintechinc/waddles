"""v1 provider connections -- the standard, provider-parameterised connection API.

Two blueprints, split the way ``community_connections.py`` and ``sso.py`` split theirs:

**Admin (``/api/v1/...``) -- webui and CLI use the same routes.** Per-community CRUD for the
credentials a community connects an external platform with (a Twitch bot + channel today).

* ``GET    /connection-providers``                                   -- install descriptors
* ``GET    /communities/<id>/provider-connections``                  -- list
* ``POST   /communities/<id>/provider-connections``                  -- create (verifies upstream)
* ``GET    /communities/<id>/provider-connections/<conn_id>``        -- read
* ``PATCH  /communities/<id>/provider-connections/<conn_id>``        -- toggle, re-target, rotate
* ``DELETE /communities/<id>/provider-connections/<conn_id>``        -- disconnect (purges if last)

Every route runs ``tenant_middleware`` -> ``require_scope("connections:admin")`` (tenant before
scope, security.md) -> ``authorize_community`` (a DB-backed check that the caller administers THIS
community, in THEIR tenant -- the JWT scope alone cannot say *which* community) -> the two-gate
feature check (flag default OFF + Professional tier) -> the service. The tenant always comes from
the validated token, never from the path or body. Responses are explicit DTOs
(security.md Output Validation): a token, its ciphertext and the OAuth app secret have no field to
travel in, and adapter metadata is filtered through the adapter's own public-key allow-list.

**Internal (``/api/v1/internal/...``) -- the data plane (svc-ingest) only.** Authenticated with a
short-lived machine JWT (scope ``connections:resolve``, operator-plane ``system`` tenant), never a
shared key:

* ``GET  /provider-connections?provider=&capability=&after=&limit=`` -- what to run (no tokens)
* ``POST /communities/<id>/provider-connections/<conn_id>/resolve``  -- a fresh access token

The resolve response is the only place a decrypted secret leaves hub-api, it carries the access
token only (never the refresh token or OAuth app secret), and is ``Cache-Control: no-store``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from typing import Any, ParamSpec, TypeVar, cast

from flask_core.api_utils import error_response
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import get_tenant_context, tenant_middleware
from pydantic import ConfigDict
from quart import Blueprint, Response, current_app, request
from quart_schema import validate_request, validate_response

from services.community_authz import authorize_community
from services.current_user import get_current_user_id
from services.errors import ApiError, bad_request, forbidden, payment_required
from services.machine_auth import machine_authenticated
from services.provider_connections import (
    AdapterSpec,
    PlatformKeyVault,
    ProviderConnectionService,
)
from services.provider_connections import errors as pc_errors
from services.provider_connections.registry import build_default_registry
from services.provider_connections.service import (
    MAX_PAGE_SIZE,
    ConnectionView,
    DataPlaneBinding,
    ResolvedCredentials,
)
from services.provider_connections.telemetry import log_failure

logger = logging.getLogger(__name__)

provider_connections_bp = Blueprint("v1_provider_connections", __name__, url_prefix="/api/v1")
provider_connections_internal_bp = Blueprint(
    "v1_provider_connections_internal", __name__, url_prefix="/api/v1/internal"
)

#: OIDC scope for community connection administration. Carried by the tenant-owner bundle
#: (``flask_core.auth.SCOPE_BUNDLES["tenant"]["admin"]``) and by global admins via ``*:admin``;
#: deliberately NOT ``*:read``-satisfiable (every session has that wildcard).
SCOPE_CONNECTIONS_ADMIN = "connections:admin"
#: Machine-JWT scope the data plane's service identity must hold.
SCOPE_CONNECTIONS_RESOLVE = "connections:resolve"
#: Two-gate feature (PostHog flag default OFF AND Professional tier) -- see
#: ``libs/core_platform_module/features.py`` ``integrations.provider_connections``.
FEATURE_PROVIDER_CONNECTIONS = "waddles.integrations.provider_connections"

#: ``app.config`` key a test (or a future composition root) may pre-populate; otherwise the
#: service is built once, lazily, from ``install_dal`` + the default registry + the platform vault.
SERVICE_CONFIG_KEY = "provider_connection_service"

_DEFAULT_DATAPLANE_PAGE = 500


# --------------------------------------------------------------------------
# DTOs (security.md Output Validation: explicit shapes, never raw rows)
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class CredentialFieldDTO:
    """One credential input a provider takes (a descriptor, never a value)."""

    name: str
    required: bool
    description: str


@dataclass(slots=True, frozen=True)
class MetadataFieldDTO:
    """One metadata input a provider takes."""

    name: str
    required: bool
    description: str
    pattern: str | None
    maxLength: int


@dataclass(slots=True, frozen=True)
class ProviderDTO:
    """Install descriptor: everything a UI/CLI needs to render the connect form, generically."""

    provider: str
    displayName: str
    installKind: str
    resourceType: str
    capabilities: list[str]
    requiredScopes: list[str]
    refreshable: bool
    credentialFields: list[CredentialFieldDTO]
    metadataFields: list[MetadataFieldDTO]


@dataclass(slots=True, frozen=True)
class ProviderListResponse:
    """Response for ``GET /connection-providers``."""

    success: bool
    providers: list[ProviderDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class ProviderConnectionDTO:
    """Admin view of one connection. Has no token field of any kind."""

    id: int
    provider: str
    resourceType: str
    status: str
    statusReason: str | None
    enabled: bool
    access: str
    usable: bool
    shared: bool
    hasRefreshToken: bool
    expiresAt: str | None
    capabilities: list[str]
    metadata: dict[str, Any]
    createdAt: str | None
    updatedAt: str | None


@dataclass(slots=True, frozen=True)
class ProviderConnectionResponse:
    """Response wrapping one connection."""

    success: bool
    connection: ProviderConnectionDTO


@dataclass(slots=True, frozen=True)
class ProviderConnectionListResponse:
    """Response wrapping a community's connections."""

    success: bool
    connections: list[ProviderConnectionDTO] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class DeleteResponse:
    """Response for DELETE; ``purged`` is true when the shared link and its tokens were removed."""

    success: bool
    purged: bool


@dataclass(slots=True, frozen=True)
class CredentialsBody:
    """Token pair as supplied by an admin. Unknown fields are a 400; secrets never reach a repr."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)


@dataclass(slots=True, frozen=True)
class CreateConnectionRequest:
    """Body of ``POST``. ``metadata`` keys are the provider's ``metadataFields``."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    provider: str
    credentials: CredentialsBody
    metadata: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass(slots=True, frozen=True)
class UpdateConnectionRequest:
    """Body of ``PATCH``. Omitted fields are unchanged; the provider is immutable."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    enabled: bool | None = None
    metadata: dict[str, str] | None = None
    credentials: CredentialsBody | None = None


@dataclass(slots=True, frozen=True)
class DataPlaneBindingDTO:
    """One (community, connection) pair the data plane should run. Carries no token."""

    grantId: int
    connectionId: int
    communityId: int
    tenantId: int
    provider: str
    resourceType: str
    resourceId: str
    capabilities: list[str]
    expiresAt: str | None
    updatedAt: str | None
    metadata: dict[str, Any]


@dataclass(slots=True, frozen=True)
class DataPlaneListResponse:
    """Response for the internal list; ``nextAfter`` is the cursor for the next page, if any."""

    success: bool
    items: list[DataPlaneBindingDTO] = field(default_factory=list)
    nextAfter: int | None = None


@dataclass(slots=True, frozen=True)
class ResolvedCredentialsDTO:
    """A fresh access token for the data plane (the only secret-bearing response in this API)."""

    success: bool
    connectionId: int
    communityId: int
    provider: str
    accessToken: str = field(repr=False)
    expiresAt: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _provider_dto(spec: AdapterSpec) -> ProviderDTO:
    return ProviderDTO(
        provider=spec.provider,
        displayName=spec.display_name,
        installKind=spec.install_kind,
        resourceType=spec.resource_type,
        capabilities=sorted(spec.capabilities),
        requiredScopes=list(spec.required_scopes),
        refreshable=spec.refreshable,
        credentialFields=[
            CredentialFieldDTO(name=item.name, required=item.required, description=item.description)
            for item in spec.credential_fields
        ],
        metadataFields=[
            MetadataFieldDTO(
                name=item.name,
                required=item.required,
                description=item.description,
                pattern=item.pattern,
                maxLength=item.max_length,
            )
            for item in spec.metadata_fields
        ],
    )


def _connection_dto(view: ConnectionView) -> ProviderConnectionDTO:
    binding = view.binding
    return ProviderConnectionDTO(
        id=binding.connection.id,
        provider=binding.connection.platform,
        resourceType=binding.connection.resource_type,
        status=binding.connection.status,
        statusReason=binding.connection.status_reason,
        enabled=binding.grant.enabled,
        access=binding.grant.status,
        usable=view.usable,
        shared=binding.shared_with > 0,
        hasRefreshToken=binding.connection.has_refresh_token,
        expiresAt=_iso(binding.connection.expires_at),
        capabilities=view.capabilities,
        metadata=view.public_metadata,
        createdAt=_iso(binding.connection.created_at),
        updatedAt=_iso(binding.connection.updated_at),
    )


def _dataplane_dto(item: DataPlaneBinding) -> DataPlaneBindingDTO:
    return DataPlaneBindingDTO(
        grantId=item.grant_id,
        connectionId=item.connection_id,
        communityId=item.community_id,
        tenantId=item.tenant_id,
        provider=item.provider,
        resourceType=item.resource_type,
        resourceId=item.resource_id,
        capabilities=item.capabilities,
        expiresAt=_iso(item.expires_at),
        updatedAt=_iso(item.updated_at),
        metadata=item.metadata,
    )


def _resolved_dto(item: ResolvedCredentials) -> ResolvedCredentialsDTO:
    return ResolvedCredentialsDTO(
        success=True,
        connectionId=item.connection_id,
        communityId=item.community_id,
        provider=item.provider,
        accessToken=item.access_token,
        expiresAt=_iso(item.expires_at),
        metadata=item.metadata,
    )


# --------------------------------------------------------------------------
# Wiring + error mapping
# --------------------------------------------------------------------------


def _service() -> ProviderConnectionService:
    """The process-wide service, built once from ``install_dal`` unless a test pre-seeded one."""
    existing = current_app.config.get(SERVICE_CONFIG_KEY)
    if existing is None:
        existing = ProviderConnectionService(
            current_app.config["install_dal"], build_default_registry(), PlatformKeyVault()
        )
        current_app.config[SERVICE_CONFIG_KEY] = existing
    return cast(ProviderConnectionService, existing)


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


#: ``ProviderConnectionError`` subclass -> (HTTP status, API error code). Anything not listed is a
#: 500 with the generic code -- an unmapped error is a bug to be seen, never an implicit 4xx.
_STATUS_BY_ERROR: dict[type[pc_errors.ProviderConnectionError], tuple[int, str]] = {
    pc_errors.UnknownProvider: (400, "UNKNOWN_PROVIDER"),
    pc_errors.InvalidConnectionInput: (400, "BAD_REQUEST"),
    pc_errors.CredentialsRejected: (422, "CREDENTIALS_REJECTED"),
    pc_errors.InsufficientScopes: (422, "INSUFFICIENT_SCOPES"),
    pc_errors.ResourceUnresolvable: (422, "RESOURCE_NOT_FOUND"),
    pc_errors.ProviderUnavailable: (502, "PROVIDER_UNAVAILABLE"),
    pc_errors.ConnectionNotFound: (404, "NOT_FOUND"),
    pc_errors.ConnectionConflict: (409, "CONFLICT"),
    pc_errors.ConnectionNotUsable: (409, "CONNECTION_NOT_USABLE"),
    pc_errors.RefreshRejected: (409, "REFRESH_REJECTED"),
    pc_errors.RefreshNotConfigured: (409, "REFRESH_NOT_CONFIGURED"),
    pc_errors.VaultUnavailable: (503, "CONNECTIONS_UNAVAILABLE"),
    pc_errors.VaultCorrupt: (500, "CONNECTIONS_UNAVAILABLE"),
    pc_errors.SchemaMissing: (503, "CONNECTIONS_UNAVAILABLE"),
}


def _connection_err(
    exc: pc_errors.ProviderConnectionError, *, event: str
) -> tuple[dict[str, object], int]:
    """Map a framework error to the JSON error envelope (fixed, application-authored text).

    A refusal to hand out a connection carries a machine-readable ``reason`` in ``error.details``
    so the data plane can tell "stop" (disabled, expired) from "retry later" without parsing text.
    """
    status, code = _STATUS_BY_ERROR.get(type(exc), (500, "INTERNAL_ERROR"))
    level = logging.ERROR if status >= 500 else logging.INFO
    # The event name is a static literal (LiteralString); the route name is a bounded label.
    log_failure(logger, "provider_connections.request_failed", exc, level=level, route=event)
    message = exc.message if status < 500 else "Connections are temporarily unavailable"
    details = {"reason": exc.reason} if isinstance(exc, pc_errors.ConnectionNotUsable) else None
    return cast(tuple[dict[str, object], int], error_response(message, status, code, details))


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _guarded(
    route: str,
) -> Callable[
    [Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R | tuple[dict[str, object], int]]]
]:
    """Turn the two error families a handler can raise into the JSON error envelope.

    Innermost decorator on every route, so ``tenant_middleware`` / ``require_scope`` /
    ``validate_*`` still run outside it. ``ApiError`` (authz, feature gate, bad query args) and
    ``ProviderConnectionError`` (the framework's own taxonomy) are mapped here and nowhere else; any
    other exception is a bug and propagates to the app's 500 handler untouched.
    """

    def decorator(
        handler: Callable[_P, Awaitable[_R]],
    ) -> Callable[_P, Awaitable[_R | tuple[dict[str, object], int]]]:
        @wraps(handler)
        async def wrapper(
            *args: _P.args, **kwargs: _P.kwargs
        ) -> _R | tuple[dict[str, object], int]:
            try:
                return await handler(*args, **kwargs)
            except ApiError as exc:
                logger.debug(
                    "provider_connections.request_rejected route=%s status=%d code=%s",
                    route,
                    exc.status_code,
                    exc.code,
                )
                return _err(exc)
            except pc_errors.ProviderConnectionError as exc:
                logger.debug("provider_connections.request_refused route=%s", route)
                return _connection_err(exc, event=route)

        return wrapper

    return decorator


async def _admin_guard(community_id: int) -> tuple[int, int | None]:
    """Authorise one admin request; returns ``(tenant_id, actor_user_id)`` or raises ``ApiError``.

    Order is security-relevant: the caller must administer *this* community of *their* tenant
    (DB-backed) before the feature gate is consulted, so an outsider learns nothing about whether
    the feature is on, and the tenant never comes from anything the client controls.
    """
    ctx = get_tenant_context(request)
    if ctx is None:
        # tenant_middleware always publishes this before the handler runs.
        raise forbidden("Tenant context not resolved")
    await authorize_community(
        request,
        current_app.config["async_dal"],
        current_app.config["dal"],
        community_id=community_id,
        admin=True,
    )
    if not await feature_enabled(FEATURE_PROVIDER_CONNECTIONS, tenant=ctx.tenant_slug):
        raise payment_required("Provider connections are not enabled for this plan")
    return ctx.tenant_id, get_current_user_id(request)


# --------------------------------------------------------------------------
# Admin routes
# --------------------------------------------------------------------------

_BASE = "/communities/<int:community_id>/provider-connections"
_ONE = _BASE + "/<int:connection_id>"


@provider_connections_bp.route("/connection-providers", methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(SCOPE_CONNECTIONS_ADMIN)  # type: ignore[untyped-decorator]
@validate_response(ProviderListResponse)
@_guarded("providers")
async def list_connection_providers() -> ProviderListResponse:
    """``GET /connection-providers`` -- the install descriptor of every registered provider."""
    ctx = get_tenant_context(request)
    assert ctx is not None  # nosec B101 - tenant_middleware guarantees this
    if not await feature_enabled(FEATURE_PROVIDER_CONNECTIONS, tenant=ctx.tenant_slug):
        raise payment_required("Provider connections are not enabled for this plan")
    return ProviderListResponse(
        success=True, providers=[_provider_dto(spec) for spec in _service().providers()]
    )


@provider_connections_bp.route(_BASE, methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(SCOPE_CONNECTIONS_ADMIN)  # type: ignore[untyped-decorator]
@validate_response(ProviderConnectionListResponse)
@_guarded("list")
async def list_provider_connections(community_id: int) -> ProviderConnectionListResponse:
    """``GET`` -- this community's connections. No secret is loaded to build the response."""
    tenant_id, _actor = await _admin_guard(community_id)
    views = await _service().list(tenant_id=tenant_id, community_id=community_id)
    return ProviderConnectionListResponse(
        success=True, connections=[_connection_dto(view) for view in views]
    )


@provider_connections_bp.route(_BASE, methods=["POST"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(SCOPE_CONNECTIONS_ADMIN)  # type: ignore[untyped-decorator]
@validate_request(CreateConnectionRequest)
@validate_response(ProviderConnectionResponse, 201)
@_guarded("create")
async def create_provider_connection(
    community_id: int, data: CreateConnectionRequest
) -> tuple[ProviderConnectionResponse, int]:
    """``POST`` -- verify the credential upstream, resolve the resource, store it sealed."""
    tenant_id, actor = await _admin_guard(community_id)
    view = await _service().create(
        tenant_id=tenant_id,
        community_id=community_id,
        actor_user_id=actor,
        provider=data.provider,
        access_token=data.credentials.access_token,
        refresh_token=data.credentials.refresh_token,
        metadata=data.metadata,
        enabled=data.enabled,
    )
    return ProviderConnectionResponse(success=True, connection=_connection_dto(view)), 201


@provider_connections_bp.route(_ONE, methods=["GET"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(SCOPE_CONNECTIONS_ADMIN)  # type: ignore[untyped-decorator]
@validate_response(ProviderConnectionResponse)
@_guarded("get")
async def get_provider_connection(
    community_id: int, connection_id: int
) -> ProviderConnectionResponse:
    """``GET`` -- one connection of this community."""
    tenant_id, _actor = await _admin_guard(community_id)
    view = await _service().get(
        tenant_id=tenant_id, community_id=community_id, connection_id=connection_id
    )
    return ProviderConnectionResponse(success=True, connection=_connection_dto(view))


@provider_connections_bp.route(_ONE, methods=["PATCH"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(SCOPE_CONNECTIONS_ADMIN)  # type: ignore[untyped-decorator]
@validate_request(UpdateConnectionRequest)
@validate_response(ProviderConnectionResponse)
@_guarded("update")
async def update_provider_connection(
    community_id: int, connection_id: int, data: UpdateConnectionRequest
) -> ProviderConnectionResponse:
    """``PATCH`` -- enable/disable for this community, re-target the resource, rotate the token."""
    tenant_id, actor = await _admin_guard(community_id)
    view = await _service().update(
        tenant_id=tenant_id,
        community_id=community_id,
        actor_user_id=actor,
        connection_id=connection_id,
        enabled=data.enabled,
        metadata=data.metadata,
        access_token=data.credentials.access_token if data.credentials else None,
        refresh_token=data.credentials.refresh_token if data.credentials else None,
    )
    return ProviderConnectionResponse(success=True, connection=_connection_dto(view))


@provider_connections_bp.route(_ONE, methods=["DELETE"])
@tenant_middleware  # type: ignore[untyped-decorator]
@require_scope(SCOPE_CONNECTIONS_ADMIN)  # type: ignore[untyped-decorator]
@validate_response(DeleteResponse)
@_guarded("delete")
async def delete_provider_connection(community_id: int, connection_id: int) -> DeleteResponse:
    """``DELETE`` -- disconnect this community; tokens are purged when no one else uses the link."""
    tenant_id, _actor = await _admin_guard(community_id)
    purged = await _service().delete(
        tenant_id=tenant_id, community_id=community_id, connection_id=connection_id
    )
    return DeleteResponse(success=True, purged=purged)


# --------------------------------------------------------------------------
# Internal (data plane) routes -- machine JWT, operator-plane tenant
# --------------------------------------------------------------------------


#: Requires a valid machine JWT with ``connections:resolve`` from the operator-plane tenant; the
#: fail-closed ladder lives in :func:`services.machine_auth.machine_authenticated`.
_service_authenticated = machine_authenticated(
    SCOPE_CONNECTIONS_RESOLVE,
    unconfigured_event="provider_connections.service_auth_unconfigured",
    rejected_event="provider_connections.service_auth_rejected",
    wrong_tenant_event="provider_connections.service_auth_wrong_tenant",
)


@provider_connections_internal_bp.after_request
async def _no_store(response: Response) -> Response:
    """Nothing under ``/internal`` may be cached by any intermediary (some bodies hold a token)."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _int_arg(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise bad_request(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise bad_request(f"{name} must be between {minimum} and {maximum}")
    return value


@provider_connections_internal_bp.route("/provider-connections", methods=["GET"])
@_service_authenticated
@validate_response(DataPlaneListResponse)
@_guarded("internal_list")
async def internal_list() -> DataPlaneListResponse:
    """``GET`` -- usable bindings for one provider across all tenants (no tokens)."""
    provider = request.args.get("provider", "")
    if not provider:
        raise bad_request("provider is required")
    capability = request.args.get("capability") or None
    after = _int_arg("after", default=0, minimum=0, maximum=2**62)
    limit = _int_arg("limit", default=_DEFAULT_DATAPLANE_PAGE, minimum=1, maximum=MAX_PAGE_SIZE)
    page = await _service().list_for_dataplane(
        provider=provider, capability=capability, after=after, limit=limit
    )
    return DataPlaneListResponse(
        success=True, items=[_dataplane_dto(item) for item in page.items], nextAfter=page.next_after
    )


@provider_connections_internal_bp.route(_ONE + "/resolve", methods=["POST"])
@_service_authenticated
@validate_response(ResolvedCredentialsDTO)
@_guarded("internal_resolve")
async def internal_resolve(community_id: int, connection_id: int) -> ResolvedCredentialsDTO:
    """``POST .../resolve`` -- a fresh access token for one usable binding (refresh under lock)."""
    resolved = await _service().resolve(community_id=community_id, connection_id=connection_id)
    return _resolved_dto(resolved)


BLUEPRINTS = [provider_connections_bp, provider_connections_internal_bp]
