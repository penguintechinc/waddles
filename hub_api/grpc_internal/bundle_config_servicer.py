"""gRPC servicer for `BundleConfigService` -- live per-community bundle config for the stages.

The interceptor chain (`server.build_interceptor_chain`) has verified the machine JWT and its
`bundle-config:read` scope. This adapter then:

1. checks the caller's SPIFFE service name against an allow-list (default `svc-process`,
   `svc-action`; `BUNDLE_CONFIG_ALLOWED_SERVICES` comma-list overrides);
2. takes the tenant ONLY from the verified `tenant` claim: a token of the operator-plane `system`
   tenant (the shared stages) names the numeric tenant in the request, any other token is pinned
   to its own tenant (a different `tenant_id` is `PERMISSION_DENIED`);
3. requires the community to belong to that tenant (`PERMISSION_DENIED` otherwise).

The answer is the manifest `community_config_schema` defaults overlaid with the latest saved
version. Ids only; config bodies are never logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Final

import grpc
from flask_core.service_jwt import SYSTEM_TENANT
from opentelemetry import metrics, trace
from waddles.hub.internal.v1 import bundle_config_pb2, bundle_config_pb2_grpc

from grpc_internal.interceptors import service_claims
from services import bundle_community_config_runtime as runtime
from services import bundle_install_dal

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("waddles.hub_api.bundle_config")
_meter = metrics.get_meter("waddles.hub_api.bundle_config")
_requests = _meter.create_counter(
    "bundle_config_requests_total", description="BundleConfigService lookups by outcome."
)
_latency = _meter.create_histogram(
    "bundle_config_lookup_seconds", unit="s", description="Config lookup latency."
)

SCOPE_BUNDLE_CONFIG_READ: Final = "bundle-config:read"
_SVC = "/waddles.hub.internal.v1.BundleConfigService/"
BUNDLE_CONFIG_SCOPES: Final[dict[str, str]] = {
    _SVC + "GetCommunityConfig": SCOPE_BUNDLE_CONFIG_READ,
    _SVC + "BatchGetCommunityConfig": SCOPE_BUNDLE_CONFIG_READ,
}
DEFAULT_ALLOWED_SERVICES: Final = ("svc-process", "svc-action")
API_VERSION: Final = "v1"
MAX_BATCH: Final = 200
_MAX_ID: Final = 2**31 - 1
_APP_ID_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def _allowed_services() -> frozenset[str]:
    """Caller allow-list from the environment when set (an empty value allows nobody)."""
    raw = os.getenv("BUNDLE_CONFIG_ALLOWED_SERVICES")
    if raw is None:
        return frozenset(DEFAULT_ALLOWED_SERVICES)
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def _caller_allowed(subject: str) -> bool:
    """True when the verified caller's SPIFFE service segment is allow-listed."""
    name = subject.rsplit("/", 1)[-1] if subject.startswith("spiffe://") else ""
    return name in _allowed_services()


class _RefusedError(Exception):
    """A request refusal carrying its gRPC status (constant message, no request data)."""

    def __init__(self, code: grpc.StatusCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _to_message(cfg: runtime.RuntimeConfig) -> bundle_config_pb2.GetCommunityConfigResponse:
    return bundle_config_pb2.GetCommunityConfigResponse(
        tenant_id=cfg.tenant_id,
        community_id=cfg.community_id,
        app_id=cfg.app_id,
        version=cfg.version,
        config_json=json.dumps(cfg.config, separators=(",", ":"), sort_keys=True).encode(),
    )


class BundleConfigServicer(bundle_config_pb2_grpc.BundleConfigServiceServicer):  # type: ignore[misc]
    """Thin adapter over `bundle_community_config_runtime.get_runtime_config`."""

    def __init__(self, install_dal: Any = None) -> None:
        """Bind the install DAL; without one every RPC fails UNAVAILABLE (never `{}`)."""
        self._install_dal = install_dal

    async def _tenant_id(self, claims: dict[str, Any], requested: int) -> int:
        """Numeric tenant for this call, from the verified token claim (see module doc)."""
        slug = str(claims.get("tenant", "")).strip()
        if not slug:
            raise _RefusedError(grpc.StatusCode.UNAUTHENTICATED, "unauthorized")
        if slug == SYSTEM_TENANT:
            if not 1 <= requested <= _MAX_ID:
                raise _RefusedError(grpc.StatusCode.INVALID_ARGUMENT, "tenant_id required")
            return requested
        found = await bundle_install_dal.raw_sql_rows(
            self._install_dal, "SELECT id FROM tenants WHERE slug = :slug LIMIT 1", {"slug": slug}
        )
        row = found.first() if found else None
        if row is None:
            raise _RefusedError(grpc.StatusCode.PERMISSION_DENIED, "forbidden")
        tenant_id = int(row["id"])
        if requested not in (0, tenant_id):
            raise _RefusedError(grpc.StatusCode.PERMISSION_DENIED, "forbidden")
        return tenant_id

    async def _one(
        self, claims: dict[str, Any], request: bundle_config_pb2.GetCommunityConfigRequest
    ) -> bundle_config_pb2.GetCommunityConfigResponse:
        """Validate one scope and return its merged config, or raise `_RefusedError`."""
        if request.api_version != API_VERSION:
            raise _RefusedError(
                grpc.StatusCode.UNIMPLEMENTED, f"api_version {request.api_version!r} not supported"
            )
        if not 0 <= request.community_id <= _MAX_ID or _APP_ID_RE.fullmatch(request.app_id) is None:
            raise _RefusedError(grpc.StatusCode.INVALID_ARGUMENT, "invalid request")
        tenant_id = await self._tenant_id(claims, request.tenant_id)
        try:
            cfg = await runtime.get_runtime_config(
                self._install_dal,
                tenant_id=tenant_id,
                community_id=request.community_id,
                app_id=request.app_id,
            )
        except runtime.CommunityNotInTenantError as exc:
            raise _RefusedError(grpc.StatusCode.PERMISSION_DENIED, "forbidden") from exc
        except runtime.BundleNotActiveError as exc:
            raise _RefusedError(grpc.StatusCode.NOT_FOUND, "bundle not active") from exc
        return _to_message(cfg)

    async def _preflight(self, context: grpc.aio.ServicerContext[Any, Any]) -> dict[str, Any]:
        """Claims + allow-list + store checks shared by both RPCs; aborts on refusal."""
        claims = service_claims.get()
        if not claims:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "unauthorized")
            raise AssertionError("unreachable")
        if not _caller_allowed(str(claims.get("sub", ""))):
            logger.warning("bundle_config.caller_not_allowlisted")
            _requests.add(1, {"outcome": "forbidden"})
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "forbidden")
            raise AssertionError("unreachable")
        if self._install_dal is None:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "install store not configured")
            raise AssertionError("unreachable")
        return dict(claims)

    async def GetCommunityConfig(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: bundle_config_pb2.GetCommunityConfigRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> bundle_config_pb2.GetCommunityConfigResponse:
        """Merged live config of one scope; refusals are loud gRPC statuses."""
        claims = await self._preflight(context)
        started = time.monotonic()
        with _tracer.start_as_current_span("bundle_config.get") as span:
            span.set_attribute("waddles.community_id", request.community_id)
            span.set_attribute("waddles.app_id", request.app_id)
            try:
                response = await self._one(claims, request)
            except _RefusedError as exc:
                _requests.add(1, {"outcome": exc.code.name.lower()})
                logger.info(
                    "bundle_config.refused",
                    extra={"code": exc.code.name, "community_id": request.community_id},
                )
                await context.abort(exc.code, exc.message)
                raise AssertionError("unreachable") from exc
            _requests.add(1, {"outcome": "ok"})
            _latency.record(time.monotonic() - started)
            return response

    async def BatchGetCommunityConfig(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: bundle_config_pb2.BatchGetCommunityConfigRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> bundle_config_pb2.BatchGetCommunityConfigResponse:
        """Seed lookup: refused/not-active scopes are omitted (logged), never fail the batch."""
        claims = await self._preflight(context)
        if request.api_version != API_VERSION:
            await context.abort(grpc.StatusCode.UNIMPLEMENTED, "api_version not supported")
            raise AssertionError("unreachable")
        if len(request.scopes) > MAX_BATCH:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"at most {MAX_BATCH} scopes")
            raise AssertionError("unreachable")
        out: list[bundle_config_pb2.GetCommunityConfigResponse] = []
        with _tracer.start_as_current_span("bundle_config.batch_get") as span:
            span.set_attribute("waddles.scopes", len(request.scopes))
            for scope in request.scopes:
                scope.api_version = request.api_version
                try:
                    out.append(await self._one(claims, scope))
                    _requests.add(1, {"outcome": "ok"})
                except _RefusedError as exc:
                    _requests.add(1, {"outcome": exc.code.name.lower()})
                    logger.warning(
                        "bundle_config.batch_scope_omitted",
                        extra={"code": exc.code.name, "community_id": scope.community_id},
                    )
        return bundle_config_pb2.BatchGetCommunityConfigResponse(configs=out)
