"""Thin gRPC servicer adapters for `waddles.hub.internal.v1`.

`MintEphemeralPseudonyms` (#429), `ResolveDisplayNames` (#427) and `ResolveHandle`
are real and delegate to `services.identity_resolution_service`; `GetStreamDek`
(#442) remains an UNIMPLEMENTED stub. Keep these methods thin adapters over plain
service functions -- no business logic here. Every failure aborts with a gRPC
status; none returns a default UUID or name.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from typing import Any, Final, NoReturn

import grpc
from flask_core.service_jwt import SYSTEM_TENANT
from opentelemetry import metrics, trace
from waddles.hub.internal.v1 import identity_pb2, identity_pb2_grpc, key_pb2, key_pb2_grpc

from grpc_internal.interceptors import service_claims
from services.envelope.errors import TenantKeyNotFoundError, TenantKeyUnavailableError
from services.identity_resolution_service import (
    AmbiguousHandleError,
    HandleNotFoundError,
    IdentityRequest,
    IdentityResolutionError,
    IdentityValidationError,
    PlatformUserNotFoundError,
    TenantAccessDeniedError,
    TenantNotFoundError,
    audit_platform_user_resolution,
    authorize_tenant,
    resolve_display_names,
    resolve_identities,
    resolve_platform_users,
    resolve_target,
)
from services.tenant_dek_seal import parse_client_key, seal_context, seal_dek

#: Method -> required scope, consumed by `interceptors.AuthInterceptor`.
REQUIRED_SCOPES: dict[str, str] = {
    "/waddles.hub.internal.v1.IdentityService/MintEphemeralPseudonyms": "identity:ephemeral:mint",
    "/waddles.hub.internal.v1.IdentityService/ResolveDisplayNames": "identity:displayname:read",
    "/waddles.hub.internal.v1.IdentityService/ResolveHandle": "identity:handle:resolve",
    "/waddles.hub.internal.v1.IdentityService/ResolvePlatformUser": (
        "identity:platform-user:resolve"
    ),
    "/waddles.hub.internal.v1.KeyService/GetStreamDek": "key:stream-dek:read",
    "/waddles.hub.internal.v1.KeyService/GetTenantDek": "keys:tenant-dek:read",
}

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("waddles.hub_api.tenant_dek")
_meter = metrics.get_meter("waddles.hub_api.tenant_dek")
_dek_requests = _meter.create_counter(
    "waddles_tenant_dek_broker_requests_total",
    description="Tenant-DEK broker requests by result (never labelled by tenant or key).",
)
_dek_duration = _meter.create_histogram(
    "waddles_tenant_dek_broker_duration_seconds",
    unit="s",
    description="Latency of one tenant-DEK broker request.",
)

#: Data-plane services allowed to fetch a tenant DEK (`KEYS_BROKER_ALLOWED_SERVICES` overrides).
DEFAULT_DEK_ALLOWED_SERVICES: Final = frozenset({"svc-ingest", "svc-process", "svc-action"})
DEFAULT_DEK_TTL_SECONDS: Final = 600
MIN_DEK_TTL_SECONDS: Final = 30
_MAX_TENANT_ID: Final = 2**31 - 1
_PURPOSE_LABELS: Final = {
    key_pb2.TENANT_DEK_PURPOSE_AT_REST: "at-rest",
    key_pb2.TENANT_DEK_PURPOSE_INGEST_STREAM: "ingest-stream",
}

_MATCH_KINDS: dict[str, int] = {
    "mention": identity_pb2.MATCH_KIND_MENTION,
    "handle": identity_pb2.MATCH_KIND_HANDLE,
}


async def _abort_for(
    exc: IdentityResolutionError, context: grpc.aio.ServicerContext[Any, Any]
) -> NoReturn:
    """Map a resolution failure to its gRPC status (messages are PII-free constants)."""
    if isinstance(exc, IdentityValidationError):
        code, detail = grpc.StatusCode.INVALID_ARGUMENT, str(exc)
    elif isinstance(exc, TenantNotFoundError | HandleNotFoundError | PlatformUserNotFoundError):
        code, detail = grpc.StatusCode.NOT_FOUND, str(exc)
    elif isinstance(exc, AmbiguousHandleError):
        code, detail = grpc.StatusCode.FAILED_PRECONDITION, str(exc)
    elif isinstance(exc, TenantAccessDeniedError):
        code, detail = grpc.StatusCode.PERMISSION_DENIED, str(exc)
    else:
        code, detail = grpc.StatusCode.INTERNAL, "identity resolution failed"
    await context.abort(code, detail)
    raise AssertionError("unreachable") from exc


class IdentityServicer(identity_pb2_grpc.IdentityServiceServicer):  # type: ignore[misc]
    """Adapter for `IdentityService`: pseudonym mint (#429), display names (#427), handles."""

    def __init__(self, async_dal: Any = None) -> None:
        """Bind the AsyncDAL; without one every RPC fails UNAVAILABLE (never a default)."""
        self._async_dal = async_dal

    async def _require_dal(self, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        """Return the bound AsyncDAL or abort UNAVAILABLE."""
        if self._async_dal is None:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "identity store not configured")
            raise AssertionError("unreachable")
        return self._async_dal

    async def _caller(self, context: grpc.aio.ServicerContext[Any, Any]) -> tuple[str, str]:
        """Return `(tenant claim, sub)` of the verified caller, or abort UNAUTHENTICATED.

        Reads the claims `AuthInterceptor` stored after verifying the token. Absent
        claims mean this servicer was reached around the interceptor chain -- fail
        closed rather than fall back to trusting the request body's tenant.
        """
        claims = service_claims.get()
        tenant = claims.get("tenant") if claims else None
        if not claims or not isinstance(tenant, str) or not tenant.strip():
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "unauthorized")
            raise AssertionError("unreachable")
        return tenant.strip(), str(claims.get("sub", ""))

    async def MintEphemeralPseudonyms(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: identity_pb2.MintEphemeralPseudonymsRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> identity_pb2.MintEphemeralPseudonymsResponse:
        """Mint-or-return the stable UUID for each platform identity (batch <=100).

        Every distinct tenant in the batch is authorized against the caller's token
        before anything is minted -- one denied tenant rejects the whole batch.
        """
        claim_tenant, caller = await self._caller(context)
        dal = await self._require_dal(context)
        try:
            effective: dict[str, str] = {}
            for ref in dict.fromkeys(i.tenant_id for i in request.items):
                effective[ref] = await authorize_tenant(
                    dal, claim_tenant, ref, op="mint", caller=caller
                )
            items = [
                IdentityRequest(effective[i.tenant_id], i.platform, i.platform_user_id, i.handle)
                for i in request.items
            ]
            resolved = await resolve_identities(dal, items)
        except IdentityResolutionError as exc:
            await _abort_for(exc, context)
        return identity_pb2.MintEphemeralPseudonymsResponse(
            pseudonyms=[
                identity_pb2.EphemeralPseudonym(
                    platform_user_id=r.platform_user_id, pseudonym=str(r.uuid)
                )
                for r in resolved
            ]
        )

    async def ResolveDisplayNames(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: identity_pb2.ResolveDisplayNamesRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> identity_pb2.ResolveDisplayNamesResponse:
        """Resolve a tenant-scoped batch of UUIDs to display names (egress detokenizer)."""
        claim_tenant, caller = await self._caller(context)
        dal = await self._require_dal(context)
        try:
            tenant = await authorize_tenant(
                dal, claim_tenant, request.tenant_id, op="display_names", caller=caller
            )
            result = await resolve_display_names(dal, tenant, list(request.uuids))
        except IdentityResolutionError as exc:
            await _abort_for(exc, context)
        return identity_pb2.ResolveDisplayNamesResponse(
            names=[
                identity_pb2.ResolvedDisplayName(
                    uuid=n.uuid, display_name=n.display_name, is_hub_user=n.is_hub_user
                )
                for n in result.names
            ],
            unresolved_uuids=list(result.unresolved),
        )

    async def ResolveHandle(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: identity_pb2.ResolveHandleRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> identity_pb2.ResolveHandleResponse:
        """Resolve one raw handle/mention to a UUID; the handle never leaves hub-api.

        Only a verified login (never a display name / nickname) of a CURRENT tenant member
        resolves; a mention never mints for a non-member. Every refusal -- unknown, not a
        member, display-name-only, ambiguous -- is a loud NOT_FOUND / FAILED_PRECONDITION,
        and `HandleUnverifiedError` / `TargetNotMemberError` map to NOT_FOUND like any miss.
        """
        claim_tenant, caller = await self._caller(context)
        dal = await self._require_dal(context)
        try:
            tenant = await authorize_tenant(
                dal, claim_tenant, request.tenant_id, op="resolve_handle", caller=caller
            )
            resolved = await resolve_target(dal, tenant, request.platform, request.target)
        except IdentityResolutionError as exc:
            await _abort_for(exc, context)
        return identity_pb2.ResolveHandleResponse(
            uuid=str(resolved.uuid), match_kind=_MATCH_KINDS[resolved.kind]
        )

    async def ResolvePlatformUser(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: identity_pb2.ResolvePlatformUserRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> identity_pb2.ResolvePlatformUserResponse:
        """Detokenize <=25 member UUIDs of ONE community to raw platform user ids.

        The only RPC that returns raw platform ids. The tenant comes from the verified token
        (`authorize_tenant`), the community is a numeric id that must belong to it, and any miss
        -- wrong community, non-member, unknown -- is the same constant NOT_FOUND. Every call,
        success or refusal, writes one PII-free audit record (caller, community id, count,
        outcome); the ids themselves are never logged and nothing is cached.
        """
        claim_tenant, caller = await self._caller(context)
        dal = await self._require_dal(context)
        count = len(request.uuids)
        outcome = "ok"
        try:
            tenant = await authorize_tenant(
                dal, claim_tenant, request.tenant_id, op="resolve_platform_user", caller=caller
            )
            result = await resolve_platform_users(
                dal, tenant, request.community_id, request.platform, list(request.uuids)
            )
        except IdentityResolutionError as exc:
            outcome = {
                PlatformUserNotFoundError: "not_found",
                TenantNotFoundError: "not_found",
                TenantAccessDeniedError: "denied",
                IdentityValidationError: "invalid",
            }.get(type(exc), "error")
            audit_platform_user_resolution(
                caller=caller,
                community_id=request.community_id,
                platform=request.platform,
                count=count,
                outcome=outcome,
            )
            await _abort_for(exc, context)
        audit_platform_user_resolution(
            caller=caller,
            community_id=request.community_id,
            platform=request.platform,
            count=count,
            outcome=outcome,
        )
        return identity_pb2.ResolvePlatformUserResponse(
            users=[
                identity_pb2.ResolvedPlatformUser(uuid=r.uuid, platform_user_id=r.platform_user_id)
                for r in result
            ]
        )


class KeyServicer(key_pb2_grpc.KeyServiceServicer):  # type: ignore[misc]
    """Adapter for `KeyService`: tenant at-rest DEK broker; `GetStreamDek` is still pending #442."""

    def __init__(self, async_dal: Any = None, envelope_service: Any = None) -> None:
        """Bind the AsyncDAL (audit) and the envelope service; without either, fail UNAVAILABLE."""
        self._async_dal = async_dal
        self._envelope = envelope_service

    async def GetStreamDek(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: key_pb2.GetStreamDekRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> key_pb2.GetStreamDekResponse:
        """Stub -- real DEK sealing ports from PR #442 onto this method."""
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "GetStreamDek: pending PR #442")
        raise AssertionError("unreachable")

    async def GetTenantDek(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: key_pb2.GetTenantDekRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> key_pb2.GetTenantDekResponse:
        """Seal a tenant's DEK to the verified data-plane caller (the only raw-DEK exit).

        Ladder after the interceptor chain (JWT scope + rate limit): operator-plane `system`
        tenant and caller allow-list (PERMISSION_DENIED) -> mTLS SVID must match the JWT `sub`
        when a client cert was presented (PERMISSION_DENIED) -> request shape (INVALID_ARGUMENT)
        -> key lookup (NOT_FOUND / UNAVAILABLE, never a fallback key) -> audit row written BEFORE
        the key is returned (INTERNAL withholds it). Key bytes, the caller's public key and the
        sealed output are never logged or put in telemetry.
        """
        started = time.perf_counter()
        caller = await self._authorize_caller(service_claims.get(), context, started)
        purpose_label = _PURPOSE_LABELS.get(request.purpose)
        client_public = parse_client_key(bytes(request.caller_ephemeral_public_key))
        if (
            purpose_label is None
            or client_public is None
            or not 1 <= request.tenant_id <= _MAX_TENANT_ID
        ):
            await self._deny(
                context, started, "bad_request", grpc.StatusCode.INVALID_ARGUMENT, "invalid request"
            )
        if self._envelope is None or self._async_dal is None:
            await self._deny(
                context,
                started,
                "unconfigured",
                grpc.StatusCode.UNAVAILABLE,
                "key broker not configured",
            )
        tenant_id = int(request.tenant_id)
        with _tracer.start_as_current_span("tenant_dek.release") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("caller", caller)
            try:
                dek_version, dek = await self._envelope.export_dek(
                    tenant_id, dek_version=request.dek_version or None
                )
            except TenantKeyNotFoundError:
                await self._audit(caller, tenant_id, "not_found", None)
                await self._deny(
                    context, started, "not_found", grpc.StatusCode.NOT_FOUND, "tenant key not found"
                )
            except TenantKeyUnavailableError as exc:
                logger.error(
                    "tenant_dek.key_unavailable",
                    extra={"tenant_id": tenant_id, "caller": caller, "reason": exc.reason},
                )
                await self._audit(caller, tenant_id, f"unavailable:{exc.reason}", None)
                await self._deny(
                    context,
                    started,
                    "unavailable",
                    grpc.StatusCode.UNAVAILABLE,
                    "tenant key unavailable",
                )
            span.set_attribute("dek_version", dek_version)
            try:
                enc, ciphertext = seal_dek(
                    bytes(dek), client_public, seal_context(caller, tenant_id, purpose_label)
                )
            except ValueError:
                await self._deny(
                    context,
                    started,
                    "bad_request",
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "invalid request",
                )
            finally:
                dek[:] = bytes(len(dek))  # best-effort zeroisation of the raw DEK buffer
            try:
                await self._audit(caller, tenant_id, "released", dek_version)
            except Exception:
                logger.error("tenant_dek.audit_write_failed", exc_info=True)
                await self._deny(
                    context, started, "audit_failed", grpc.StatusCode.INTERNAL, "audit unavailable"
                )
        logger.info(
            "tenant_dek.released",
            extra={"tenant_id": tenant_id, "caller": caller, "dek_version": dek_version},
        )
        _dek_done("ok", started)
        return key_pb2.GetTenantDekResponse(
            enc=enc, ciphertext=ciphertext, dek_version=dek_version, ttl_seconds=_dek_ttl()
        )

    async def _deny(
        self,
        context: grpc.aio.ServicerContext[Any, Any],
        started: float,
        result: str,
        code: grpc.StatusCode,
        detail: str,
    ) -> NoReturn:
        """Count the outcome then abort with a constant, non-revealing message."""
        _dek_done(result, started)
        await context.abort(code, detail)
        raise AssertionError("unreachable")

    async def _authorize_caller(
        self,
        claims: dict[str, Any] | None,
        context: grpc.aio.ServicerContext[Any, Any],
        started: float,
    ) -> str:
        """Enforce system-tenant + allow-list + SVID/sub binding; return the caller service name."""
        if not claims:
            await self._deny(
                context, started, "unauthenticated", grpc.StatusCode.UNAUTHENTICATED, "unauthorized"
            )
            raise AssertionError("unreachable")
        subject = str(claims.get("sub", ""))
        name = subject.rsplit("/", 1)[-1] if subject.startswith("spiffe://") else ""
        if claims.get("tenant") != SYSTEM_TENANT or name not in _dek_allowed_services():
            logger.warning("tenant_dek.caller_refused", extra={"caller": name[:64]})
            await self._deny(
                context, started, "forbidden", grpc.StatusCode.PERMISSION_DENIED, "forbidden"
            )
        peer_ids = _peer_spiffe_ids(context)
        if peer_ids is not None and subject not in peer_ids:
            logger.warning("tenant_dek.svid_mismatch", extra={"caller": name[:64]})
            await self._deny(
                context, started, "svid_mismatch", grpc.StatusCode.PERMISSION_DENIED, "forbidden"
            )
        return name

    async def _audit(
        self, caller: str, tenant_id: int, outcome: str, dek_version: int | None
    ) -> None:
        """Write one `audit_log` row (who/when/tenant/version/outcome); raises on failure."""
        details: dict[str, object] = {"caller": caller, "tenant_id": tenant_id, "outcome": outcome}
        if dek_version is not None:
            details["dek_version"] = dek_version
        await self._async_dal.insert_async(
            self._async_dal.dal.audit_log,
            user_id=None,
            action="keys.tenant_dek.release",
            target_type="tenant_key",
            target_id=str(tenant_id),
            details=details,
            created_at=datetime.now(UTC),
        )


def _peer_spiffe_ids(context: grpc.aio.ServicerContext[Any, Any]) -> frozenset[str] | None:
    """SPIFFE ids in the mTLS client cert's URI SANs; ``None`` when no client cert was presented.

    SPIFFE-ready: with `GRPC_TLS_CLIENT_CA_PATH` set the server requires a client X.509-SVID and
    this binds it to the JWT `sub`; without a client cert (SPIRE not yet deployed) the machine JWT
    alone authenticates, exactly as for every other internal RPC.
    """
    auth = context.auth_context()
    if not auth.get("x509_pem_cert"):
        return None
    return frozenset(
        value.decode()
        for value in auth.get("x509_subject_alternative_name", [])
        if value.startswith(b"spiffe://")
    )


def _dek_allowed_services() -> frozenset[str]:
    """Caller allow-list (defence in depth on top of the chart's scope grant)."""
    raw = os.getenv("KEYS_BROKER_ALLOWED_SERVICES")
    if raw is None:
        return DEFAULT_DEK_ALLOWED_SERVICES
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _dek_ttl() -> int:
    """The `ttl_seconds` to assert (`KEYS_BROKER_TTL_SECONDS`, clamped to 30..600)."""
    raw = os.getenv("KEYS_BROKER_TTL_SECONDS")
    if raw is None:
        return DEFAULT_DEK_TTL_SECONDS
    return max(MIN_DEK_TTL_SECONDS, min(int(raw), DEFAULT_DEK_TTL_SECONDS))


def _dek_done(result: str, started: float) -> None:
    """Record request count + latency under a bounded result label."""
    _dek_requests.add(1, {"result": result})
    _dek_duration.record(time.perf_counter() - started, {"result": result})
