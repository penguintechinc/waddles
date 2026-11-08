"""Routes the data-plane reads of the provider-connection tests over REAL gRPC.

The internal REST routes (`/api/v1/internal/provider-connections` and `.../resolve`) were
replaced by `ProviderConnectionService` (see `grpc_internal.dataplane_servicers`). The existing
provider-connection suites assert data-plane behaviour (refresh, static secrets, binding
metadata, tenant/community isolation) through those URLs; `GrpcRoutingClient` wraps the Quart
test client and serves any request under `/api/v1/internal/` through a loopback gRPC server
behind the production interceptor chain, so every such assertion now exercises the gRPC
servicer end to end. Everything else (the admin API) still goes to the wrapped Quart client.

`InternalResponse` mirrors the old REST envelope (status code + JSON body in the same shape) so
the assertions read unchanged: the mapping gRPC status -> HTTP status is fixed and documented in
`_HTTP_BY_CODE`; a `not_usable:<reason>` refusal becomes the old flat 409 body.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import grpc
from waddles.hub.internal.v1 import provider_connection_pb2 as pb

from tests._dataplane_grpc import DataplaneClient, serving_dataplane

INTERNAL_PREFIX = "/api/v1/internal/"
_LIST = "/waddles.hub.internal.v1.ProviderConnectionService/ListBindings"
_RESOLVE = "/waddles.hub.internal.v1.ProviderConnectionService/ResolveConnection"
_RESOLVE_URL = re.compile(
    r"^/api/v1/internal/communities/(?P<community>\d+)/provider-connections/(?P<conn>\d+)/resolve$"
)
_HTTP_BY_CODE = {
    grpc.StatusCode.UNAUTHENTICATED: 401,
    grpc.StatusCode.PERMISSION_DENIED: 403,
    grpc.StatusCode.NOT_FOUND: 404,
    grpc.StatusCode.FAILED_PRECONDITION: 409,
    grpc.StatusCode.INVALID_ARGUMENT: 400,
    grpc.StatusCode.RESOURCE_EXHAUSTED: 429,
    grpc.StatusCode.UNAVAILABLE: 503,
    grpc.StatusCode.INTERNAL: 500,
}


class InternalResponse:
    """REST-shaped view of one gRPC outcome: `status_code`, `get_json()`, `get_data()`."""

    def __init__(self, status_code: int, body: dict[str, Any], detail: str = "") -> None:
        """Store the mapped status, JSON body, and the raw gRPC status detail."""
        self.status_code = status_code
        self._body = body
        self.detail = detail
        self.headers: dict[str, str] = {}

    async def get_json(self) -> dict[str, Any]:
        """The JSON body (same shape the retired REST route returned)."""
        return self._body

    async def get_data(self, as_text: bool = False) -> str | bytes:
        """The serialised body + status detail (what a leak check scans for secrets)."""
        text = json.dumps(self._body) + self.detail
        return text if as_text else text.encode()


def _binding_json(b: pb.ConnectionBinding) -> dict[str, Any]:
    return {
        "grantId": b.grant_id,
        "connectionId": b.connection_id,
        "communityId": b.community_id,
        "tenantId": b.tenant_id,
        "provider": b.provider,
        "resourceType": b.resource_type,
        "resourceId": b.resource_id,
        "capabilities": list(b.capabilities),
        "expiresAt": b.expires_at if b.HasField("expires_at") else None,
        "updatedAt": b.updated_at if b.HasField("updated_at") else None,
        "metadata": dict(b.metadata),
        "kind": b.kind,
        "secretVersion": b.secret_version,
    }


def _opt(msg: Any, field: str) -> Any:
    return getattr(msg, field) if msg.HasField(field) else None


def _resolved_json(r: pb.ResolveConnectionResponse) -> dict[str, Any]:
    return {
        "success": True,
        "connectionId": r.connection_id,
        "communityId": r.community_id,
        "provider": r.provider,
        "kind": r.kind,
        "accessToken": _opt(r, "access_token"),
        "expiresAt": _opt(r, "expires_at"),
        "endpoint": {f: _opt(r.endpoint, f) for f in ("url", "region", "function", "project")},
        "secret": (
            {"scheme": r.secret.scheme, "fields": dict(r.secret.fields)}
            if r.HasField("secret")
            else None
        ),
        "metadata": dict(r.metadata),
    }


def _error(err: grpc.aio.AioRpcError) -> InternalResponse:
    detail = err.details() or ""
    status = _HTTP_BY_CODE.get(err.code(), 500)
    if err.code() == grpc.StatusCode.FAILED_PRECONDITION and detail.startswith("not_usable:"):
        body: dict[str, Any] = {
            "success": False,
            "error": "not_usable",
            "reason": detail.removeprefix("not_usable:"),
        }
    else:
        body = {"error": detail}
    return InternalResponse(status, body, detail)


class _VerifierIssuer:
    """Adapts a bare verifier to the `.as_verifier()` shape `build_interceptor_chain` wants."""

    def __init__(self, verifier: Any) -> None:
        self._verifier = verifier

    def as_verifier(self) -> Any:
        """The wrapped verifier."""
        return self._verifier


class GrpcRoutingClient:
    """Quart test client whose `/api/v1/internal/*` requests are served over real gRPC."""

    def __init__(self, inner: Any) -> None:
        """Wrap `inner` (the Quart test client); its app config feeds the servicers."""
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        """Everything not data-plane (admin routes, `.app`) goes to the wrapped client."""
        return getattr(self._inner, name)

    async def get(self, url: str, **kwargs: Any) -> Any:
        """GET: internal list over gRPC, anything else to the wrapped client."""
        if url.startswith(INTERNAL_PREFIX):
            return await self._internal("GET", url, kwargs.get("headers") or {})
        return await self._inner.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        """POST: internal resolve over gRPC, anything else to the wrapped client."""
        if url.startswith(INTERNAL_PREFIX):
            return await self._internal("POST", url, kwargs.get("headers") or {})
        return await self._inner.post(url, **kwargs)

    async def _internal(self, method: str, url: str, headers: dict[str, str]) -> InternalResponse:
        """Serve one internal request: parse it, call the matching RPC, return a REST-shaped view."""
        app = self._inner.app
        auth = headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else None
        parts = urlsplit(url)
        verifier = app.config["SERVICE_JWT_VERIFIER"]
        async with serving_dataplane(
            app.config, _VerifierIssuer(verifier), self._service_factory
        ) as address:
            client = DataplaneClient(address)
            try:
                if method == "GET" and parts.path == INTERNAL_PREFIX + "provider-connections":
                    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                    request = pb.ListBindingsRequest(
                        provider=query.get("provider", ""),
                        capability=query.get("capability", ""),
                        after=int(query.get("after", 0)),
                        limit=int(query.get("limit", 0)),
                    )
                    page = await client.call(_LIST, request, pb.ListBindingsResponse, token=token)
                    return InternalResponse(
                        200,
                        {
                            "success": True,
                            "items": [_binding_json(b) for b in page.items],
                            "nextAfter": _opt(page, "next_after"),
                        },
                    )
                match = _RESOLVE_URL.match(parts.path)
                if method == "POST" and match:
                    request = pb.ResolveConnectionRequest(
                        community_id=int(match["community"]), connection_id=int(match["conn"])
                    )
                    resolved = await client.call(
                        _RESOLVE, request, pb.ResolveConnectionResponse, token=token
                    )
                    return InternalResponse(200, _resolved_json(resolved))
            except grpc.aio.AioRpcError as err:
                return _error(err)
        raise AssertionError(f"no gRPC route for {method} {url}")

    @staticmethod
    def _service_factory(config: Any) -> Any:
        """The test's pre-seeded `ProviderConnectionService` (same object the admin API uses)."""
        from blueprints.v1.provider_connections import service_from_config

        return service_from_config(config)
