"""`BundleConfigService` end to end over a real gRPC server + the real gateway save path.

Real: `grpc.aio` server with the production interceptor chain (ES256 machine JWTs, scope table),
`bundle_admin_gateway_service.save_config` (the same function the REST admin route calls) writing
`bundle_community_config`, the active-manifest join, and the schema-default merge. Proves the chain
"admin saves -> stage RPC returns the new value" and the invalidation publish.
"""

# ruff: noqa: F811  -- imported fixtures are re-bound as params

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import grpc
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey, spiffe_id
from waddles.hub.internal.v1 import bundle_config_pb2, bundle_config_pb2_grpc

from grpc_internal import bundle_config_servicer as servicer
from grpc_internal.server import ALL_REQUIRED_SCOPES, build_interceptor_chain
from services import bundle_admin_gateway_service as svc
from services import bundle_community_config_runtime as runtime
from services.community_authz import CommunityMembership
from tests.conftest import TENANT_SLUG
from tests.test_bundle_admin_gateway import _APP_MANIFEST, _publish, env  # noqa: F401  # fixtures

SCOPE = servicer.SCOPE_BUNDLE_CONFIG_READ
PROC = spiffe_id("alpha", "svc-process")
ACTION = spiffe_id("alpha", "svc-action")
ROGUE = spiffe_id("alpha", "svc-rogue")
APP = "waddles.socials.cfgdefaults.default"
ADMIN = CommunityMembership("admin", frozenset({"community:manage_members"}), True, False)
_SCHEMA = {
    "type": "object",
    "properties": {
        "default_duration_minutes": {"type": "integer", "minimum": 1, "default": 60},
        "default_tz": {"type": "string", "default": "UTC"},
        "approval_required": {"type": "boolean", "default": False},
        "nodefault": {"type": "string"},
    },
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUNDLE_CONFIG_ALLOWED_SERVICES", raising=False)


@pytest.fixture
def issuer() -> ServiceJwtIssuer:
    priv = ec.generate_private_key(ec.SECP256R1())
    ids = {
        PROC: ServiceIdentity(PROC, "waddlebot", "svc-process", frozenset({SCOPE}), "system"),
        ACTION: ServiceIdentity(ACTION, "waddlebot", "svc-action", frozenset({SCOPE}), "system"),
        ROGUE: ServiceIdentity(ROGUE, "waddlebot", "svc-rogue", frozenset({SCOPE}), "system"),
    }
    return ServiceJwtIssuer(
        keys={"k1": SigningKey("k1", priv, priv.public_key())}, active_kid="k1", identities=ids
    )


@pytest.fixture
async def hub(env: dict[str, Any]) -> dict[str, Any]:
    """The gateway `env` plus an app activated for the community with schema defaults."""
    manifest = {
        **_APP_MANIFEST,
        "app_id": APP,
        "feature": "waddles.socials.cfgdefaults",
        "community_config_schema": _SCHEMA,
        "admin_tools": [],
        "requested_tools": [],
    }
    await _publish(env["dal"], manifest, env["cid"])
    return env


async def _save(
    hub: dict[str, Any], config: dict[str, Any], expected: int
) -> list[tuple[Any, ...]]:
    published: list[tuple[Any, ...]] = []

    async def on_saved(*args: Any) -> None:
        published.append(args)

    bundle = await svc.load_active_bundle(
        hub["dal"], tenant_id=1, community_id=hub["cid"], app_id=APP
    )
    await svc.save_config(
        hub["dal"],
        bundle,
        ADMIN,
        tenant_id=1,
        community_id=hub["cid"],
        actor_id=11,
        config=config,
        expected_version=expected,
        on_saved=on_saved,
    )
    return published


@asynccontextmanager
async def _stub(hub: dict[str, Any], issuer: ServiceJwtIssuer) -> AsyncIterator[Any]:
    server = grpc.aio.server(interceptors=build_interceptor_chain(issuer))
    bundle_config_pb2_grpc.add_BundleConfigServiceServicer_to_server(
        servicer.BundleConfigServicer(hub["dal"]), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            yield bundle_config_pb2_grpc.BundleConfigServiceStub(channel)
    finally:
        await server.stop(grace=None)


def _md(issuer: ServiceJwtIssuer, sub: str = PROC, scope: str = SCOPE) -> tuple[Any, ...]:
    token, _ = issuer.issue_with_claims(sub, scope, ttl_seconds=300, extra_claims=None)
    return (("authorization", f"Bearer {token}"),)


def _req(
    hub: dict[str, Any], *, tenant_id: int = 1, community_id: int | None = None, app_id: str = APP
) -> bundle_config_pb2.GetCommunityConfigRequest:
    return bundle_config_pb2.GetCommunityConfigRequest(
        api_version="v1",
        tenant_id=tenant_id,
        community_id=hub["cid"] if community_id is None else community_id,
        app_id=app_id,
    )


def test_scope_table_registers_both_rpcs() -> None:
    for rpc in ("GetCommunityConfig", "BatchGetCommunityConfig"):
        method = f"/waddles.hub.internal.v1.BundleConfigService/{rpc}"
        assert ALL_REQUIRED_SCOPES[method] == "bundle-config:read"


def test_merge_saved_wins_over_defaults() -> None:
    merged = runtime.merge_config(_SCHEMA, {"default_tz": "Europe/Paris"})
    assert merged == {
        "default_duration_minutes": 60,
        "default_tz": "Europe/Paris",
        "approval_required": False,
    }
    assert runtime.merge_config(None, None) == {}


async def test_never_saved_returns_schema_defaults(hub: dict[str, Any], issuer: Any) -> None:
    async with _stub(hub, issuer) as stub:
        got = await stub.GetCommunityConfig(_req(hub), metadata=_md(issuer), timeout=3.0)
    assert got.version == 0
    assert json.loads(got.config_json) == {
        "default_duration_minutes": 60,
        "default_tz": "UTC",
        "approval_required": False,
    }


async def test_save_then_rpc_returns_new_value_and_publishes(
    hub: dict[str, Any], issuer: Any
) -> None:
    published = await _save(hub, {"default_duration_minutes": 45}, 0)
    assert published == [(1, hub["cid"], APP, 1)]
    async with _stub(hub, issuer) as stub:
        got = await stub.GetCommunityConfig(_req(hub), metadata=_md(issuer, ACTION), timeout=3.0)
        assert got.version == 1
        assert json.loads(got.config_json)["default_duration_minutes"] == 45
        assert json.loads(got.config_json)["default_tz"] == "UTC"  # default still merged
        await _save(hub, {"default_duration_minutes": 90, "default_tz": "Asia/Tokyo"}, 1)
        got2 = await stub.GetCommunityConfig(_req(hub), metadata=_md(issuer), timeout=3.0)
    assert got2.version == 2
    assert json.loads(got2.config_json)["default_duration_minutes"] == 90


async def test_batch_omits_refused_and_inactive_scopes(hub: dict[str, Any], issuer: Any) -> None:
    await _save(hub, {"default_duration_minutes": 30}, 0)
    batch = bundle_config_pb2.BatchGetCommunityConfigRequest(
        api_version="v1",
        scopes=[_req(hub), _req(hub, app_id="waddles.socials.nothere.default")],
    )
    async with _stub(hub, issuer) as stub:
        out = await stub.BatchGetCommunityConfig(batch, metadata=_md(issuer), timeout=3.0)
    assert [c.app_id for c in out.configs] == [APP]
    assert json.loads(out.configs[0].config_json)["default_duration_minutes"] == 30


async def _status(coro: Any) -> grpc.StatusCode:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await coro
    return exc.value.code()


async def test_refusals(hub: dict[str, Any], issuer: Any) -> None:
    other = int(await hub["dal"].communities.async_insert(tenant_id=2, name="other"))
    async with _stub(hub, issuer) as stub:
        # community of another tenant -> 403
        denied = await _status(
            stub.GetCommunityConfig(
                _req(hub, community_id=other), metadata=_md(issuer), timeout=3.0
            )
        )
        assert denied == grpc.StatusCode.PERMISSION_DENIED
        # not-allow-listed caller
        assert (
            await _status(
                stub.GetCommunityConfig(_req(hub), metadata=_md(issuer, ROGUE), timeout=3.0)
            )
            == grpc.StatusCode.PERMISSION_DENIED
        )
        # api_version missing -> UNIMPLEMENTED
        bad = _req(hub)
        bad.api_version = ""
        assert (
            await _status(stub.GetCommunityConfig(bad, metadata=_md(issuer), timeout=3.0))
            == grpc.StatusCode.UNIMPLEMENTED
        )
        # system token must name a tenant
        assert (
            await _status(
                stub.GetCommunityConfig(_req(hub, tenant_id=0), metadata=_md(issuer), timeout=3.0)
            )
            == grpc.StatusCode.INVALID_ARGUMENT
        )
        # app not active for the community
        assert (
            await _status(
                stub.GetCommunityConfig(
                    _req(hub, app_id="waddles.socials.nothere.default"),
                    metadata=_md(issuer),
                    timeout=3.0,
                )
            )
            == grpc.StatusCode.NOT_FOUND
        )


async def test_tenant_pinned_token_cannot_read_another_tenant(hub: dict[str, Any]) -> None:
    priv = ec.generate_private_key(ec.SECP256R1())
    sid = spiffe_id("alpha", "svc-process")
    pinned = ServiceJwtIssuer(
        keys={"k1": SigningKey("k1", priv, priv.public_key())},
        active_kid="k1",
        identities={
            sid: ServiceIdentity(sid, "waddlebot", "svc-process", frozenset({SCOPE}), TENANT_SLUG)
        },
    )
    async with _stub(hub, pinned) as stub:
        md = _md(pinned, sid)
        got = await stub.GetCommunityConfig(_req(hub, tenant_id=0), metadata=md, timeout=3.0)
        assert got.tenant_id == 1
        assert (
            await _status(stub.GetCommunityConfig(_req(hub, tenant_id=2), metadata=md, timeout=3.0))
            == grpc.StatusCode.PERMISSION_DENIED
        )


async def test_publish_version_is_best_effort_and_keyed_by_ids() -> None:
    class Valkey:
        def __init__(self, fail: bool) -> None:
            self.fail, self.sets = fail, []

        async def set(self, key: str, value: str, ex: int) -> None:
            if self.fail:
                raise ConnectionError("down")
            self.sets.append((key, value, ex))

    ok = Valkey(False)
    assert await runtime.publish_version(ok, tenant_id=1, community_id=5, app_id="a.b", version=3)
    assert ok.sets == [("waddles:bundle_config:v:1:5:a.b", "3", runtime.VERSION_KEY_TTL_S)]
    assert not await runtime.publish_version(
        Valkey(True), tenant_id=1, community_id=5, app_id="a.b", version=3
    )


def _calendar_files() -> tuple[dict[str, Any], str]:
    """The calendar manifest and the shared served-config fixture (sync file reads)."""
    repo = Path(__file__).resolve().parents[2]
    cal = yaml.safe_load((repo / "bundles/rust/calendar/bundle.yaml").read_text())
    fixture = (repo / "bundles/rust/calendar/tests/fixtures/live_config_served.json").read_text()
    return cal, fixture


async def test_calendar_saved_default_duration_is_served(env: dict[str, Any], issuer: Any) -> None:
    """Real calendar schema + a gateway save -> exactly the calendar bundle test's fixture.

    `bundles/rust/calendar/tests/flow.rs` feeds the same bytes into `effective_config`.
    """
    cal, fixture = _calendar_files()
    app_id = "waddles.core.app.calendar"
    manifest = {
        **_APP_MANIFEST,
        "app_id": app_id,
        "feature": "waddles.core.app",
        "community_config_schema": cal["community_config_schema"],
        "admin_tools": [],
        "requested_tools": [],
    }
    await _publish(env["dal"], manifest, env["cid"])
    bundle = await svc.load_active_bundle(
        env["dal"], tenant_id=1, community_id=env["cid"], app_id=app_id
    )
    await svc.save_config(
        env["dal"],
        bundle,
        ADMIN,
        tenant_id=1,
        community_id=env["cid"],
        actor_id=11,
        config={"default_duration_minutes": 45},
        expected_version=0,
    )
    async with _stub(env, issuer) as stub:
        got = await stub.GetCommunityConfig(
            _req(env, app_id=app_id), metadata=_md(issuer), timeout=3.0
        )
    assert got.config_json.decode() == fixture.strip()
