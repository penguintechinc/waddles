"""`DistributionService.ListBundles` over real gRPC -- real interceptor chain, real pydal queries.

Mirrors `test_v1_distribution_blueprint.py`'s coverage for the gRPC surface that replaces the
stage-runners' REST poll: the tenant comes ONLY from the verified token's `tenant` claim, the
stage is a typed enum, community config wins over the tenant-wide default, and nothing leaks
across tenants or stages.
"""

from __future__ import annotations

import json
from typing import Any

import grpc
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey, spiffe_id
from waddles.hub.internal.v1 import distribution_pb2 as pb

from grpc_internal.dataplane_servicers import SCOPE_DISTRIBUTION_READ
from tests._dataplane_grpc import DataplaneClient, serving_dataplane
from tests.conftest import TENANT_SLUG
from tests.test_v1_distribution_blueprint import _seed_bundle, _seed_community

_LIST = "/waddles.hub.internal.v1.DistributionService/ListBundles"
RUNNER = spiffe_id("alpha", "svc-process")


def _issuer(*, tenant: str = TENANT_SLUG, scopes: frozenset[str] | None = None) -> ServiceJwtIssuer:
    """One stage-runner identity bound to `tenant` (the claim the servicer trusts)."""
    priv = Ed25519PrivateKey.generate()
    identity = ServiceIdentity(
        RUNNER,
        "waddlebot",
        "svc-process",
        scopes if scopes is not None else frozenset({SCOPE_DISTRIBUTION_READ}),
        tenant,
    )
    return ServiceJwtIssuer(
        keys={"k1": SigningKey("k1", priv, priv.public_key())},
        active_kid="k1",
        identities={RUNNER: identity},
    )


@pytest.fixture
def config(distribution_db: Any) -> dict[str, Any]:
    """The Quart-app-config-shaped mapping the servicer reads."""
    return {"dal": distribution_db.dal, "async_dal": distribution_db}


async def _list(
    config: dict[str, Any],
    stage: int,
    *,
    community_id: int | None = None,
    issuer: ServiceJwtIssuer | None = None,
    token: str | None = "",
) -> pb.ListBundlesResponse:
    """Call `ListBundles`; `token=""` issues a valid one from `issuer`, `None` sends none."""
    issuer = issuer or _issuer()
    if token == "":
        token, _ = issuer.issue_with_claims(RUNNER, SCOPE_DISTRIBUTION_READ)
    request = pb.ListBundlesRequest(stage=stage)
    if community_id is not None:
        request.community_id = community_id
    async with serving_dataplane(config, issuer) as address:
        return await DataplaneClient(address).call(
            _LIST, request, pb.ListBundlesResponse, token=token
        )


async def _code(awaitable: Any) -> grpc.StatusCode:
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await awaitable
    return caught.value.code()


class TestAuthAndInput:
    async def test_missing_token_is_unauthenticated(self, config: dict[str, Any]) -> None:
        assert await _code(_list(config, pb.DISTRIBUTION_STAGE_INGEST, token=None)) == (
            grpc.StatusCode.UNAUTHENTICATED
        )

    async def test_wrong_scope_is_unauthenticated(self, config: dict[str, Any]) -> None:
        issuer = _issuer(scopes=frozenset({"other:scope"}))
        token, _ = issuer.issue_with_claims(RUNNER, "other:scope")
        code = await _code(_list(config, pb.DISTRIBUTION_STAGE_INGEST, issuer=issuer, token=token))
        assert code == grpc.StatusCode.UNAUTHENTICATED

    async def test_unknown_tenant_claim_is_permission_denied(self, config: dict[str, Any]) -> None:
        """A valid token whose tenant is not in this DB resolves to nobody's data."""
        issuer = _issuer(tenant="someone-elses-tenant")
        code = await _code(_list(config, pb.DISTRIBUTION_STAGE_INGEST, issuer=issuer))
        assert code == grpc.StatusCode.PERMISSION_DENIED

    async def test_unspecified_stage_is_invalid_argument(self, config: dict[str, Any]) -> None:
        assert await _code(_list(config, pb.DISTRIBUTION_STAGE_UNSPECIFIED)) == (
            grpc.StatusCode.INVALID_ARGUMENT
        )

    async def test_no_async_dal_fails_loud_unavailable(self) -> None:
        assert await _code(_list({}, pb.DISTRIBUTION_STAGE_INGEST)) == (grpc.StatusCode.UNAVAILABLE)


class TestListBundles:
    async def test_no_activations_is_empty(self, config: dict[str, Any]) -> None:
        assert list((await _list(config, pb.DISTRIBUTION_STAGE_INGEST)).bundles) == []

    async def test_tenant_wide_availability_surfaces_bundle(
        self, config: dict[str, Any], distribution_db: Any
    ) -> None:
        _seed_bundle(distribution_db)
        tenant_id, _ = _seed_community(distribution_db)
        distribution_db.dal.app_tenant_availability.insert(
            tenant_id=tenant_id,
            app_id="waddles.core.demo.echo",
            available=True,
            config_defaults={"greeting": "hi"},
        )
        distribution_db.dal.commit()
        (bundle,) = (await _list(config, pb.DISTRIBUTION_STAGE_INGEST)).bundles
        assert bundle.app_id == "waddles.core.demo.echo"
        assert not bundle.HasField("community_id")
        assert bundle.entrypoint == "builtin_handlers.echo_ingest:normalize"
        assert json.loads(bundle.config_json) == {"greeting": "hi"}
        assert json.loads(bundle.spec_json) == {}

    async def test_community_activation_overrides_tenant_wide_config(
        self, config: dict[str, Any], distribution_db: Any
    ) -> None:
        _seed_bundle(distribution_db)
        tenant_id, community_id = _seed_community(distribution_db)
        dal = distribution_db.dal
        dal.app_tenant_availability.insert(
            tenant_id=tenant_id,
            app_id="waddles.core.demo.echo",
            available=True,
            config_defaults={"greeting": "hi", "volume": 1},
        )
        dal.app_activations.insert(
            community_id=community_id,
            tenant_id=tenant_id,
            app_id="waddles.core.demo.echo",
            enabled=True,
            config={"greeting": "yo"},
        )
        dal.commit()
        response = await _list(config, pb.DISTRIBUTION_STAGE_INGEST, community_id=community_id)
        (bundle,) = response.bundles  # deduped by app_id: the community row wins
        assert bundle.community_id == community_id
        assert json.loads(bundle.config_json) == {"greeting": "yo"}

    async def test_disabled_activation_is_excluded(
        self, config: dict[str, Any], distribution_db: Any
    ) -> None:
        _seed_bundle(distribution_db)
        tenant_id, community_id = _seed_community(distribution_db)
        distribution_db.dal.app_activations.insert(
            community_id=community_id,
            tenant_id=tenant_id,
            app_id="waddles.core.demo.echo",
            enabled=False,
            config={},
        )
        distribution_db.dal.commit()
        response = await _list(config, pb.DISTRIBUTION_STAGE_INGEST, community_id=community_id)
        assert list(response.bundles) == []

    async def test_stage_filter(self, config: dict[str, Any], distribution_db: Any) -> None:
        """An action-only bundle never appears in an ingest list, and does in an action list."""
        _seed_bundle(
            distribution_db,
            app_id="waddles.core.demo.actiononly",
            stages={"action": {"entrypoint": "builtin_handlers.echo_action:run"}},
        )
        tenant_id, community_id = _seed_community(distribution_db)
        distribution_db.dal.app_activations.insert(
            community_id=community_id,
            tenant_id=tenant_id,
            app_id="waddles.core.demo.actiononly",
            enabled=True,
            config={},
        )
        distribution_db.dal.commit()
        ingest = await _list(config, pb.DISTRIBUTION_STAGE_INGEST, community_id=community_id)
        action = await _list(config, pb.DISTRIBUTION_STAGE_ACTION, community_id=community_id)
        assert list(ingest.bundles) == []
        assert [b.app_id for b in action.bundles] == ["waddles.core.demo.actiononly"]

    async def test_tenant_comes_only_from_the_token(
        self, config: dict[str, Any], distribution_db: Any
    ) -> None:
        """Tenant B's availability is invisible to a tenant-A token (and vice versa)."""
        dal = distribution_db.dal
        other_tenant = dal.tenants.insert(slug="other-corp", display_name="Other", is_active=True)
        dal.commit()
        _seed_bundle(distribution_db)
        tenant_a, _ = _seed_community(distribution_db)
        dal.app_tenant_availability.insert(
            tenant_id=tenant_a, app_id="waddles.core.demo.echo", available=True
        )
        dal.commit()
        mine = await _list(config, pb.DISTRIBUTION_STAGE_INGEST)
        theirs = await _list(
            config, pb.DISTRIBUTION_STAGE_INGEST, issuer=_issuer(tenant="other-corp")
        )
        assert [b.app_id for b in mine.bundles] == ["waddles.core.demo.echo"]
        assert list(theirs.bundles) == []
        assert other_tenant

    async def test_yanked_bundle_and_null_stages_are_excluded(
        self, config: dict[str, Any], distribution_db: Any
    ) -> None:
        dal = distribution_db.dal
        _seed_bundle(distribution_db)
        dal(dal.app_catalog.app_id == "waddles.core.demo.echo").update(status="yanked")
        dal.app_catalog.insert(
            app_id="waddles.core.demo.prelegacy",
            manifest_version="1.0.0",
            module="core",
            feature="waddles.core.demo",
            provider="builtin",
            execution_model="native",
            platform_compatibility={},
            status="active",
            stages=None,
        )
        tenant_id, _ = _seed_community(distribution_db)
        for app_id in ("waddles.core.demo.echo", "waddles.core.demo.prelegacy"):
            dal.app_tenant_availability.insert(tenant_id=tenant_id, app_id=app_id, available=True)
        dal.commit()
        assert list((await _list(config, pb.DISTRIBUTION_STAGE_INGEST)).bundles) == []
