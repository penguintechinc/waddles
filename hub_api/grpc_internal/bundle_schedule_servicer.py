"""gRPC servicer for `BundleScheduleService` -- the listing svc-ingest's scheduler polls.

Replaces the former REST hop `GET /api/v1/internal/bundle-schedules` (intra-cluster traffic is gRPC
only). The interceptor chain (`server.build_interceptor_chain`) has already verified the EdDSA
machine JWT and its `bundle-schedules:read` scope; this adapter adds the same two checks the REST
route applied -- operator-plane `system` tenant, and a caller-name allow-list (the last SPIFFE path
segment of `sub`, default `svc-ingest`, `BUNDLE_SCHEDULES_ALLOWED_SERVICES` comma-list overrides) --
then delegates to `services.bundle_schedule_service`. Every refusal is a loud gRPC status with a
constant message; the response carries ids, display names and cadence only.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Final

import grpc
from flask_core.service_jwt import SYSTEM_TENANT
from waddles.hub.internal.v1 import bundle_schedule_pb2, bundle_schedule_pb2_grpc

from grpc_internal.interceptors import service_claims
from services import bundle_schedule_service as schedules

logger = logging.getLogger(__name__)

#: Machine-JWT scope the data plane's svc-ingest identity must hold (and no other service).
SCOPE_BUNDLE_SCHEDULES_READ: Final = "bundle-schedules:read"

#: Method -> required scope, merged into the interceptor chain's table by `server.py`.
BUNDLE_SCHEDULE_SCOPES: Final[dict[str, str]] = {
    "/waddles.hub.internal.v1.BundleScheduleService/ListBundleSchedules": (
        SCOPE_BUNDLE_SCHEDULES_READ
    ),
}

#: Service names (last SPIFFE path segment) allowed to call.
DEFAULT_ALLOWED_SERVICES: Final = ("svc-ingest",)
_APP_ID_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MAX_ID: Final = 2**31 - 1


def _allowed_services() -> frozenset[str]:
    """The caller allow-list, from the environment when set (an empty value allows nobody)."""
    raw = os.getenv("BUNDLE_SCHEDULES_ALLOWED_SERVICES")
    if raw is None:
        return frozenset(DEFAULT_ALLOWED_SERVICES)
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def _caller_allowed(subject: str) -> bool:
    """True when the verified caller's SPIFFE service segment is allow-listed."""
    name = subject.rsplit("/", 1)[-1] if subject.startswith("spiffe://") else ""
    return name in _allowed_services()


def _filters_valid(request: bundle_schedule_pb2.ListBundleSchedulesRequest) -> bool:
    """Bounds/shape check of the optional filters (numeric ids, slug-shaped app id)."""
    if request.HasField("tenant_id") and not 1 <= request.tenant_id <= _MAX_ID:
        return False
    if request.HasField("community_id") and not 0 <= request.community_id <= _MAX_ID:
        return False
    return not (request.HasField("app_id") and _APP_ID_RE.fullmatch(request.app_id) is None)


class BundleScheduleServicer(bundle_schedule_pb2_grpc.BundleScheduleServiceServicer):  # type: ignore[misc]
    """Thin adapter over `bundle_schedule_service.list_schedule_targets`."""

    def __init__(self, install_dal: Any = None) -> None:
        """Bind the install DAL; without one every RPC fails UNAVAILABLE (never an empty list)."""
        self._install_dal = install_dal

    async def ListBundleSchedules(  # noqa: N802 - grpc-generated servicer method name
        self,
        request: bundle_schedule_pb2.ListBundleSchedulesRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> bundle_schedule_pb2.ListBundleSchedulesResponse:
        """List active scheduled bundle scopes.

        Ladder after interceptor auth: system tenant (PERMISSION_DENIED) -> caller allow-list
        (PERMISSION_DENIED) -> filter shape (INVALID_ARGUMENT) -> store configured (UNAVAILABLE)
        -> listing. A candidate failing the manifest/binding checks is excluded and
        logged/metered by the service, never returned.
        """
        claims = service_claims.get()
        if not claims:
            # Reached around the interceptor chain -- fail closed, never trust the request.
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "unauthorized")
            raise AssertionError("unreachable")
        if str(claims.get("tenant", "")).strip() != SYSTEM_TENANT:
            logger.warning("bundle_schedules.wrong_tenant")
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "forbidden")
            raise AssertionError("unreachable")
        if not _caller_allowed(str(claims.get("sub", ""))):
            logger.warning("bundle_schedules.caller_not_allowlisted")
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "forbidden")
            raise AssertionError("unreachable")
        if not _filters_valid(request):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid request")
            raise AssertionError("unreachable")
        if self._install_dal is None:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "install store not configured")
            raise AssertionError("unreachable")

        listing = await schedules.list_schedule_targets(
            self._install_dal,
            tenant_id=request.tenant_id if request.HasField("tenant_id") else None,
            community_id=request.community_id if request.HasField("community_id") else None,
            app_id=request.app_id if request.HasField("app_id") else None,
        )
        targets = []
        for t in listing.targets:
            target = bundle_schedule_pb2.BundleScheduleTarget(
                tenant_id=t.tenant_id,
                community_id=t.community_id,
                tenant_slug=t.tenant_slug,
                app_id=t.app_id,
                interval_s=t.interval_s,
                jitter=t.jitter,
            )
            if t.community_name is not None:
                target.community_name = t.community_name
            targets.append(target)
        return bundle_schedule_pb2.ListBundleSchedulesResponse(targets=targets)
