"""gRPC servicers for the v3 data-plane reads that used to be internal REST hops.

Three services, one per proto file under `proto/waddles/hub/internal/v1/`:

* `ProviderConnectionService` (`provider_connection.proto`) -- binding list + credential resolve
  for `core/connection_resolver`;
* `DistributionService` (`distribution.proto`) -- the active bundle set per stage;
* `MediaService` (`media.proto`) -- BYOK key resolve + effective tier for `bundle_host_media`.

Each servicer enforces exactly what the retired REST route enforced, from the claims
`AuthInterceptor` verified (scope per method comes from `REQUIRED_SCOPES`): the operator-plane
`system` tenant for the cross-tenant provider/media RPCs, the community binding on `Resolve`, the
caller allow-list + rate limit + audit-before-return on the BYOK resolve, and the token's own
tenant for distribution. Servicers read their dependencies from the Quart app `config` mapping
(same objects the HTTP side uses) and fail loud when one is missing -- never a default answer.
Secrets (BYOK key, access tokens, secret documents) are never logged.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final, NoReturn

import grpc
from flask_core.service_jwt import SYSTEM_TENANT
from waddles.hub.internal.v1 import (
    distribution_pb2,
    distribution_pb2_grpc,
    media_pb2,
    media_pb2_grpc,
    provider_connection_pb2,
    provider_connection_pb2_grpc,
)

from grpc_internal.interceptors import service_claims
from services import distribution_service as distribution
from services import media_byok_service as media
from services.provider_connections import errors as pc_errors
from services.provider_connections.service import MAX_PAGE_SIZE
from services.schema import bind_app_bundle_tables

logger = logging.getLogger(__name__)

SCOPE_CONNECTIONS_RESOLVE: Final = "connections:resolve"
SCOPE_CONNECTIONS_RESOLVE_STATIC: Final = "connections:resolve:static"
SCOPE_CONNECTIONS_LIST: Final = "connections:list"
#: Least-privilege variants for the `gameserver-net` host capability: honoured ONLY for provider
#: `gameserver` (list: any other provider is `PERMISSION_DENIED`; resolve: any other provider is
#: `NOT_FOUND`, checked before any refresh).
SCOPE_CONNECTIONS_RESOLVE_GAMESERVER: Final = "connections:resolve:gameserver"
SCOPE_CONNECTIONS_LIST_GAMESERVER: Final = "connections:list:gameserver"
GAMESERVER_PROVIDER: Final = "gameserver"
SCOPE_DISTRIBUTION_READ: Final = "distribution:read"
SCOPE_MEDIA_BYOK_RESOLVE: Final = "media:byok:resolve"
SCOPE_MEDIA_TIER_RESOLVE: Final = "media:tier:resolve"

_PC = "/waddles.hub.internal.v1.ProviderConnectionService/"
_DIST = "/waddles.hub.internal.v1.DistributionService/"
_MEDIA = "/waddles.hub.internal.v1.MediaService/"

#: Method -> required scope(s); merged into `servicers.REQUIRED_SCOPES` (an unlisted method fails
#: closed in `AuthInterceptor`). A tuple means a token holding ANY one scope is accepted.
DATAPLANE_REQUIRED_SCOPES: Final[dict[str, str | tuple[str, ...]]] = {
    _PC + "ListBindings": (SCOPE_CONNECTIONS_LIST, SCOPE_CONNECTIONS_LIST_GAMESERVER),
    _PC + "ResolveConnection": (
        SCOPE_CONNECTIONS_RESOLVE,
        SCOPE_CONNECTIONS_RESOLVE_STATIC,
        SCOPE_CONNECTIONS_RESOLVE_GAMESERVER,
    ),
    _DIST + "ListBundles": SCOPE_DISTRIBUTION_READ,
    _MEDIA + "ResolveByok": SCOPE_MEDIA_BYOK_RESOLVE,
    _MEDIA + "GetEffectiveTier": SCOPE_MEDIA_TIER_RESOLVE,
}

#: Provider listing page size when the caller sends `limit == 0` (matches the retired REST default).
DEFAULT_DATAPLANE_PAGE: Final = 500

#: Fixed `reason` codes a not-usable refusal may carry; anything else is emitted as `other`.
NOT_USABLE_REASONS: Final = frozenset(
    {
        "pending_approval",
        "revoked_grant",
        "disabled",
        "connection_expired",
        "connection_revoked",
        "metadata_incomplete",
        "token_expired",
        "refresh_rejected",
        "refresh_not_configured",
        "app_replaced",
    }
)
#: Prefix of the `FAILED_PRECONDITION` detail; the fixed reason code follows it.
NOT_USABLE_PREFIX: Final = "not_usable:"

#: Media byok resolver budget per (caller, community) -- same as the retired REST route.
RESOLVE_LIMIT: Final = 30
RESOLVE_WINDOW_SECONDS: Final = 60
DEFAULT_MEDIA_ALLOWED_SERVICES: Final = ("svc-process",)

_TIERS: Final = {
    "free": media_pb2.MEDIA_TIER_FREE,
    "professional": media_pb2.MEDIA_TIER_PROFESSIONAL,
    "enterprise": media_pb2.MEDIA_TIER_ENTERPRISE,
}
_BYOK_STATUS: Final = {
    media.STATUS_OK: media_pb2.BYOK_STATUS_OK,
    media.STATUS_NOT_CONFIGURED: media_pb2.BYOK_STATUS_NOT_CONFIGURED,
    media.STATUS_TIER_REQUIRED: media_pb2.BYOK_STATUS_TIER_REQUIRED,
}
_STAGES: Final = {
    distribution_pb2.DISTRIBUTION_STAGE_INGEST: "ingest",
    distribution_pb2.DISTRIBUTION_STAGE_PROCESS: "process",
    distribution_pb2.DISTRIBUTION_STAGE_ACTION: "action",
}

_Ctx = grpc.aio.ServicerContext[Any, Any]


async def _fail(context: _Ctx, code: grpc.StatusCode, detail: str) -> NoReturn:
    """Abort with a fixed, secret-free detail (the abort raises; the assert is for mypy)."""
    await context.abort(code, detail)
    raise AssertionError("unreachable")


async def _system_claims(context: _Ctx) -> dict[str, Any]:
    """Verified claims of a caller from the operator-plane tenant, else abort.

    `AuthInterceptor` already verified signature/scope/tenant-present; these RPCs deliberately
    span tenants, so only the `system` tenant (stamped by the issuer, never the caller) may call.
    Absent claims mean the servicer was reached around the interceptor chain -- fail closed.
    """
    claims = service_claims.get()
    if not claims:
        await _fail(context, grpc.StatusCode.UNAUTHENTICATED, "unauthorized")
    if claims.get("tenant") != SYSTEM_TENANT:
        logger.warning("grpc.dataplane.wrong_tenant")
        await _fail(context, grpc.StatusCode.PERMISSION_DENIED, "forbidden")
    return claims


def _config_item(config: Mapping[str, Any], key: str) -> Any:
    """A required app-config dependency; its absence is a deployment fault, never a fallback."""
    value = config.get(key)
    if value is None:
        raise LookupError(key)
    return value


def _iso(value: datetime | None) -> str | None:
    """RFC 3339 text of a datetime, or None."""
    return value.isoformat() if value is not None else None


def _as_int(value: Any) -> int | None:
    """`value` as an int only if it already is one (bool refused), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def _abort_for_connection_error(
    exc: pc_errors.ProviderConnectionError, context: _Ctx
) -> NoReturn:
    """Map the provider-connection taxonomy to a gRPC status with a fixed, safe detail.

    A refusal to hand out a credential becomes `FAILED_PRECONDITION` carrying the fixed
    `not_usable:<reason>` code so the data plane can tell "stop" from "retry later" without
    parsing free text. A missing static scope is the same generic `PERMISSION_DENIED` as any
    other guard refusal (never says which check failed). An unmapped error is `INTERNAL`.
    """
    if isinstance(exc, pc_errors.StaticScopeRequired):
        await _fail(context, grpc.StatusCode.PERMISSION_DENIED, "forbidden")
    if isinstance(exc, pc_errors.ConnectionNotFound):
        await _fail(context, grpc.StatusCode.NOT_FOUND, "connection not found")
    if isinstance(
        exc,
        pc_errors.ConnectionNotUsable | pc_errors.RefreshRejected | pc_errors.RefreshNotConfigured,
    ):
        reason = getattr(exc, "reason", None) or exc.code
        if reason not in NOT_USABLE_REASONS:
            reason = "other"
        await _fail(context, grpc.StatusCode.FAILED_PRECONDITION, NOT_USABLE_PREFIX + reason)
    if isinstance(exc, pc_errors.InvalidConnectionInput | pc_errors.UnknownProvider):
        await _fail(context, grpc.StatusCode.INVALID_ARGUMENT, exc.message)
    if isinstance(exc, pc_errors.ProviderQuotaExceeded):
        await _fail(context, grpc.StatusCode.RESOURCE_EXHAUSTED, "provider quota exceeded")
    if isinstance(
        exc,
        pc_errors.VaultUnavailable | pc_errors.SchemaMissing | pc_errors.ProviderUnavailable,
    ):
        await _fail(context, grpc.StatusCode.UNAVAILABLE, "connections temporarily unavailable")
    await _fail(context, grpc.StatusCode.INTERNAL, "connections unavailable")


class ProviderConnectionServicer(provider_connection_pb2_grpc.ProviderConnectionServiceServicer):  # type: ignore[misc]
    """`ProviderConnectionService`: data-plane binding list + per-connection resolve."""

    def __init__(self, config: Mapping[str, Any], service_factory: Any) -> None:
        """Bind the app config and a `config -> ProviderConnectionService` factory."""
        self._config = config
        self._service_factory = service_factory

    def _service(self) -> Any:
        """The process-wide `ProviderConnectionService` (built once, shared with the HTTP side)."""
        return self._service_factory(self._config)

    async def ListBindings(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: provider_connection_pb2.ListBindingsRequest,
        context: _Ctx,
    ) -> provider_connection_pb2.ListBindingsResponse:
        """Usable bindings for one provider across all tenants (operator plane, no tokens)."""
        claims = await _system_claims(context)
        if not request.provider:
            await _fail(context, grpc.StatusCode.INVALID_ARGUMENT, "provider is required")
        if (
            claims.get("scope") != SCOPE_CONNECTIONS_LIST
            and request.provider != GAMESERVER_PROVIDER
        ):
            # A caller holding only the gameserver-scoped list token may enumerate nothing else.
            await _fail(context, grpc.StatusCode.PERMISSION_DENIED, "forbidden")
        limit = request.limit or DEFAULT_DATAPLANE_PAGE
        if not 1 <= limit <= MAX_PAGE_SIZE or not 0 <= request.after <= 2**62:
            await _fail(context, grpc.StatusCode.INVALID_ARGUMENT, "after/limit out of range")
        service = self._service()
        try:
            page = await service.list_for_dataplane(
                provider=request.provider,
                capability=request.capability or None,
                after=request.after,
                limit=limit,
            )
        except pc_errors.ProviderConnectionError as exc:
            await _abort_for_connection_error(exc, context)
        items = []
        for item in page.items:
            metadata = service.binding_metadata(
                item.provider, item.metadata, connection_id=item.connection_id
            )
            if metadata is None:
                continue  # incomplete binding: skipped (and logged) exactly as before
            binding = provider_connection_pb2.ConnectionBinding(
                grant_id=item.grant_id,
                connection_id=item.connection_id,
                community_id=item.community_id,
                tenant_id=item.tenant_id,
                provider=item.provider,
                resource_type=item.resource_type,
                resource_id=item.resource_id,
                capabilities=item.capabilities,
                expires_at=_iso(item.expires_at),
                updated_at=_iso(item.updated_at),
                metadata=metadata,
                kind=item.kind,
                secret_version=item.secret_version,
            )
            items.append(binding)
        return provider_connection_pb2.ListBindingsResponse(items=items, next_after=page.next_after)

    async def ResolveConnection(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: provider_connection_pb2.ResolveConnectionRequest,
        context: _Ctx,
    ) -> provider_connection_pb2.ResolveConnectionResponse:
        """Endpoint + credential of one binding of the token's BOUND community.

        The token must carry an integer `community` claim equal to the request's community (and a
        `conn` claim equal to the connection when present): a token minted for community A can
        never act on community B. Not found / not granted is `NOT_FOUND` (no existence oracle).
        """
        claims = await _system_claims(context)
        claim_community = _as_int(claims.get("community"))
        has_conn = "conn" in claims
        claim_conn = _as_int(claims.get("conn")) if has_conn else None
        if claim_community is None or (has_conn and claim_conn is None):
            logger.warning("grpc.provider_connections.binding_claims_missing")
            await _fail(context, grpc.StatusCode.UNAUTHENTICATED, "unauthorized")
        if request.community_id != claim_community or (
            claim_conn is not None and request.connection_id != claim_conn
        ):
            # Ids only -- never the token.
            logger.warning(
                "grpc.provider_connections.binding_mismatch",
                extra={"jti": claims.get("jti"), "community": claim_community},
            )
            await _fail(context, grpc.StatusCode.PERMISSION_DENIED, "forbidden")
        service = self._service()
        try:
            resolved = await service.resolve(
                community_id=request.community_id,
                connection_id=request.connection_id,
                static_allowed=claims.get("scope") == SCOPE_CONNECTIONS_RESOLVE_STATIC,
                # Gameserver-only credential: any other provider is a NOT_FOUND, checked BEFORE
                # any refresh.
                only_provider=(
                    None
                    if claims.get("scope")
                    in (SCOPE_CONNECTIONS_RESOLVE, SCOPE_CONNECTIONS_RESOLVE_STATIC)
                    else GAMESERVER_PROVIDER
                ),
            )
            metadata = service.binding_metadata(
                resolved.provider, resolved.metadata, connection_id=resolved.connection_id
            )
            if metadata is None:
                raise pc_errors.ConnectionNotUsable(
                    "the connection's binding is incomplete", reason="metadata_incomplete"
                )
        except pc_errors.ProviderConnectionError as exc:
            logger.debug("grpc.provider_connections.resolve_refused")
            await _abort_for_connection_error(exc, context)
        endpoint = resolved.endpoint
        secret = None
        if resolved.secret_scheme is not None and resolved.secret_fields is not None:
            secret = provider_connection_pb2.ResolveSecret(
                scheme=resolved.secret_scheme,
                fields={k: v for k, v in resolved.secret_fields.items() if v is not None},
            )
        return provider_connection_pb2.ResolveConnectionResponse(
            community_id=resolved.community_id,
            connection_id=resolved.connection_id,
            provider=resolved.provider,
            kind=resolved.kind,
            access_token=resolved.access_token,
            expires_at=_iso(resolved.expires_at),
            endpoint=provider_connection_pb2.ResolveEndpoint(
                url=endpoint.get("url"),
                region=endpoint.get("region"),
                function=endpoint.get("function"),
                project=endpoint.get("project"),
            ),
            secret=secret,
            metadata=metadata,
        )


class DistributionServicer(distribution_pb2_grpc.DistributionServiceServicer):  # type: ignore[misc]
    """`DistributionService`: the active bundle set of the caller's own tenant."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        """Bind the app config (`async_dal`, optional read replica)."""
        self._config = config

    async def _tenant_id(self, async_dal: Any, dal: Any, slug: str) -> int | None:
        """Primary key of the tenant named by the token's own `tenant` claim, or None."""
        rows = await async_dal.select_async(dal(dal.tenants.slug == slug))
        return int(rows.first().id) if rows else None

    async def ListBundles(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: distribution_pb2.ListBundlesRequest,
        context: _Ctx,
    ) -> distribution_pb2.ListBundlesResponse:
        """Enabled, activated bundles implementing `stage` for the token's tenant.

        The tenant comes ONLY from the verified `tenant` claim (never the request); an unknown
        tenant claim is `PERMISSION_DENIED`. `community_id` is accepted (per-community activations
        are the point of the 3-tier model); omitting it resolves the tenant-wide fallback only.
        """
        claims = service_claims.get()
        tenant = claims.get("tenant") if claims else None
        if not claims or not isinstance(tenant, str) or not tenant.strip():
            await _fail(context, grpc.StatusCode.UNAUTHENTICATED, "unauthorized")
        stage = _STAGES.get(request.stage)
        if stage is None:
            await _fail(
                context,
                grpc.StatusCode.INVALID_ARGUMENT,
                f"stage must be one of {distribution.BUNDLE_STAGES}",
            )
        community_id = request.community_id if request.HasField("community_id") else None
        try:
            async_dal = _config_item(self._config, "async_dal")
        except LookupError:
            await _fail(context, grpc.StatusCode.UNAVAILABLE, "distribution store not configured")
        read_dal = async_dal.read_dal if async_dal.read_dal is not None else async_dal.dal
        bind_app_bundle_tables(async_dal.dal)
        if async_dal.read_dal is not None:
            bind_app_bundle_tables(async_dal.read_dal)
        tenant_id = await self._tenant_id(async_dal, async_dal.dal, tenant.strip())
        if tenant_id is None:
            await _fail(context, grpc.StatusCode.PERMISSION_DENIED, "forbidden")
        rows = await distribution.list_bundles_for_stage(
            async_dal, read_dal, tenant_id=tenant_id, community_id=community_id, stage=stage
        )
        return distribution_pb2.ListBundlesResponse(
            bundles=[
                distribution_pb2.DistributionBundle(
                    app_id=row.app_id,
                    community_id=row.community_id,
                    entrypoint=row.entrypoint,
                    spec_json=json.dumps(row.spec).encode(),
                    config_json=json.dumps(row.config).encode(),
                )
                for row in rows
            ]
        )


def _allowed_media_services() -> frozenset[str]:
    """Service names (last SPIFFE segment) allowed to call `MediaService`; env overrides."""
    raw = os.getenv("MEDIA_BYOK_ALLOWED_SERVICES")
    if raw is None:
        return frozenset(DEFAULT_MEDIA_ALLOWED_SERVICES)
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def _bad_ids(tenant_id: int, community_id: int) -> bool:
    """True for a negative id (proto3 ints are never bool/None, but 0/negatives are refused)."""
    return tenant_id < 0 or community_id < 0


class MediaServicer(media_pb2_grpc.MediaServiceServicer):  # type: ignore[misc]
    """`MediaService`: BYOK resolve (the only plaintext-key exit) + effective tier."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        """Bind the app config (`async_dal`, `dal`, `rate_limiter`)."""
        self._config = config

    async def _caller(self, context: _Ctx) -> str:
        """Allow-listed service name of the verified caller (defence in depth), else abort."""
        claims = await _system_claims(context)
        subject = str(claims.get("sub") or "")
        name = subject.rsplit("/", 1)[-1] if subject.startswith("spiffe://") else ""
        if name not in _allowed_media_services():
            logger.warning("media_byok.caller_not_allowlisted")
            await _fail(context, grpc.StatusCode.PERMISSION_DENIED, "forbidden")
        return name

    def _dals(self) -> tuple[Any, Any]:
        """`(async_dal, dal)` from the app config; a missing one is a deployment fault."""
        return _config_item(self._config, "async_dal"), _config_item(self._config, "dal")

    async def _audit(
        self, caller: str, tenant_id: int, community_id: int, outcome: str, **extra: Any
    ) -> None:
        """Write one `audit_log` row (who/when/outcome); never the key; raises on failure."""
        async_dal, dal = self._dals()
        details: dict[str, object] = {
            "caller": caller,
            "tenant_id": tenant_id,
            "community_id": community_id,
            "outcome": outcome,
            **{k: v for k, v in extra.items() if v is not None},
        }
        await async_dal.insert_async(
            dal.audit_log,
            user_id=None,
            action="media.byok.resolve",
            target_type="media_byok",
            target_id=str(community_id),
            details=details,
            created_at=datetime.now(UTC),
        )

    async def ResolveByok(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: media_pb2.ResolveByokRequest,
        context: _Ctx,
    ) -> media_pb2.ResolveByokResponse:
        """Resolve the community's BYOK key -- fail-closed ladder, audit BEFORE the key returns.

        Order: caller allow-list (PERMISSION_DENIED) -> rate limit (RESOURCE_EXHAUSTED, or
        UNAVAILABLE with no limiter wired) -> input shape (INVALID_ARGUMENT) -> tenant/community
        scope (NOT_FOUND) -> tier -> key. If the audit row cannot be written the key is withheld
        (INTERNAL): a resolve that cannot be audited does not happen.
        """
        caller = await self._caller(context)
        limiter = self._config.get("rate_limiter")
        if limiter is None:
            logger.error("media_byok.rate_limiter_unconfigured")
            await _fail(context, grpc.StatusCode.UNAVAILABLE, "rate limiter is not configured")
        if not await limiter.check_rate_limit(
            f"media_byok_resolve:{caller}:{request.community_id}",
            RESOLVE_LIMIT,
            RESOLVE_WINDOW_SECONDS,
        ):
            await _fail(context, grpc.StatusCode.RESOURCE_EXHAUSTED, "rate limit exceeded")
        if (
            _bad_ids(request.tenant_id, request.community_id)
            or request.provider not in media.SUPPORTED_MEDIA_PROVIDERS
        ):
            await _fail(context, grpc.StatusCode.INVALID_ARGUMENT, "invalid request")
        try:
            async_dal, dal = self._dals()
        except LookupError:
            await _fail(context, grpc.StatusCode.UNAVAILABLE, "media store not configured")
        try:
            result = await media.resolve_media_key(
                async_dal,
                dal,
                tenant_id=request.tenant_id,
                community_id=request.community_id,
                provider=request.provider,
            )
        except media.CommunityScopeError:
            await self._audit(
                caller,
                request.tenant_id,
                request.community_id,
                "scope_mismatch",
                provider=request.provider,
            )
            await _fail(context, grpc.StatusCode.NOT_FOUND, "community not found")
        try:
            await self._audit(
                caller,
                request.tenant_id,
                request.community_id,
                result.status,
                tier=result.tier,
                provider=request.provider,
            )
        except Exception:
            logger.error("media_byok.audit_write_failed", exc_info=True)
            await _fail(context, grpc.StatusCode.INTERNAL, "audit unavailable")
        tier = _TIERS.get(result.tier)
        status = _BYOK_STATUS.get(result.status)
        if tier is None or status is None:
            logger.error("media_byok.unmapped_resolution")
            await _fail(context, grpc.StatusCode.INTERNAL, "unmapped resolution")
        return media_pb2.ResolveByokResponse(status=status, tier=tier, api_key=result.api_key)

    async def GetEffectiveTier(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: media_pb2.GetEffectiveTierRequest,
        context: _Ctx,
    ) -> media_pb2.GetEffectiveTierResponse:
        """`max(tenant, community)` for a community INSIDE the stated tenant (fail-closed)."""
        await self._caller(context)
        if _bad_ids(request.tenant_id, request.community_id):
            await _fail(context, grpc.StatusCode.INVALID_ARGUMENT, "invalid request")
        try:
            async_dal, dal = self._dals()
        except LookupError:
            await _fail(context, grpc.StatusCode.UNAVAILABLE, "media store not configured")
        try:
            tier = await media.effective_tier(
                async_dal, dal, tenant_id=request.tenant_id, community_id=request.community_id
            )
        except media.CommunityScopeError as exc:
            logger.warning("media_tier.scope_mismatch: %s", type(exc).__name__)
            await _fail(context, grpc.StatusCode.NOT_FOUND, "community not found")
        mapped = _TIERS.get(tier)
        if mapped is None:
            logger.error("media_tier.unmapped_tier")
            await _fail(context, grpc.StatusCode.INTERNAL, "unmapped tier")
        return media_pb2.GetEffectiveTierResponse(tier=mapped)
