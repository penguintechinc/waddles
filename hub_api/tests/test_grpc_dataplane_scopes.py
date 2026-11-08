"""Scope table completeness for the data-plane gRPC services.

`AuthInterceptor` fails closed on a method with no registered scope, so a new RPC added to a
proto without a `REQUIRED_SCOPES` entry would be unreachable (safe) -- but one registered with the
wrong/loose scope would not be. These tests pin every RPC of the three services to its exact scope
set, and prove the denominator (an empty service list is a failure, never a pass).
"""

from __future__ import annotations

import grpc
import pytest
from waddles.hub.internal.v1 import distribution_pb2, media_pb2, provider_connection_pb2

from grpc_internal.servicers import REQUIRED_SCOPES

_EXPECTED: dict[str, str | tuple[str, ...]] = {
    "/waddles.hub.internal.v1.ProviderConnectionService/ListBindings": "connections:list",
    "/waddles.hub.internal.v1.ProviderConnectionService/ResolveConnection": (
        "connections:resolve",
        "connections:resolve:static",
    ),
    "/waddles.hub.internal.v1.DistributionService/ListBundles": "distribution:read",
    "/waddles.hub.internal.v1.MediaService/ResolveByok": "media:byok:resolve",
    "/waddles.hub.internal.v1.MediaService/GetEffectiveTier": "media:tier:resolve",
}


def _methods_of(module: object) -> list[str]:
    """Every full RPC method name the generated module's services declare."""
    names: list[str] = []
    for service in module.DESCRIPTOR.services_by_name.values():  # type: ignore[attr-defined]
        names.extend(f"/{service.full_name}/{m.name}" for m in service.methods)
    return names


def test_every_dataplane_rpc_has_its_exact_scope() -> None:
    declared = [
        m
        for module in (provider_connection_pb2, distribution_pb2, media_pb2)
        for m in _methods_of(module)
    ]
    assert len(declared) == len(_EXPECTED) == 5, declared  # denominator: 5 RPCs examined
    for method in declared:
        assert REQUIRED_SCOPES[method] == _EXPECTED[method], method


def test_no_dataplane_rpc_is_registered_without_a_scope() -> None:
    for method in _EXPECTED:
        assert REQUIRED_SCOPES.get(method), method


@pytest.mark.parametrize("method", sorted(_EXPECTED))
async def test_unlisted_scope_alternative_is_refused(method: str) -> None:
    """A token holding a scope that is NOT one of the method's alternatives never passes."""
    from flask_core.service_jwt import SYSTEM_TENANT

    from tests._dataplane_grpc import DataplaneClient, serving_dataplane
    from tests._identity_grpc import make_issuer

    issuer = make_issuer(SYSTEM_TENANT, scopes=frozenset({"other:scope"}))
    token, _ = issuer.issue_with_claims("spiffe://penguintech.io/alpha/svc-process", "other:scope")
    async with serving_dataplane({}, issuer) as address:
        request_cls = {
            "ListBindings": provider_connection_pb2.ListBindingsRequest,
            "ResolveConnection": provider_connection_pb2.ResolveConnectionRequest,
            "ListBundles": distribution_pb2.ListBundlesRequest,
            "ResolveByok": media_pb2.ResolveByokRequest,
            "GetEffectiveTier": media_pb2.GetEffectiveTierRequest,
        }[method.rsplit("/", 1)[1]]
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            await DataplaneClient(address).call(method, request_cls(), request_cls, token=token)
    assert caught.value.code() == grpc.StatusCode.UNAUTHENTICATED
