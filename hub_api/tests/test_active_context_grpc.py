"""`ActiveContextService` over real gRPC against a REAL, fully-migrated Postgres.

Real: a `grpc.aio` server built with the production interceptor chain
(`server.build_interceptor_chain`: deadline -> EdDSA machine-JWT auth -> rate limit -> OTel), real
ES256 service JWTs, the migrated schema (`user_active_context`, `public_id` columns from
migration 0082), and `services.app_command_registry` doing the real SQL. Only the transport is new:
this replaces the REST hop `GET|PUT /api/v1/internal/dataplane/active-context`. One test dials the
production TLS listener (`start_internal_grpc_server`). Contract consumer:
`core/svc_ingest/src/active_context.rs`. A SKIP means docker is absent -- treat that as a gate
FAILURE, never a pass.
"""

# ruff: noqa: F811  -- imported pytest fixtures are re-bound as test params

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey
from sqlalchemy import text
from waddles.hub.internal.v1 import active_context_pb2, active_context_pb2_grpc

from grpc_internal import active_context_servicer as ctx_servicer
from grpc_internal.server import (
    ALL_REQUIRED_SCOPES,
    build_interceptor_chain,
    start_internal_grpc_server,
    stop_internal_grpc_server,
)
from tests.test_app_commands_registry_pg_integration import _world, dal  # noqa: F401  # fixtures
from tests.test_grpc_internal_tls import _free_port, _generate_self_signed_cert
from tests.test_seed_core_bundles_pg_integration import (
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
    pg_dal,  # noqa: F401  # fixture
)

INGEST = "spiffe://penguintech.io/alpha/svc-ingest"
OTHER = "spiffe://penguintech.io/alpha/svc-action"
TENANTED = "spiffe://penguintech.io/alpha/svc-tenant"
SCOPE = ctx_servicer.SCOPE_ACTIVE_CONTEXT
GET = "/waddles.hub.internal.v1.ActiveContextService/GetActiveContext"
SET = "/waddles.hub.internal.v1.ActiveContextService/SetActiveContext"
USER = "7d9f1c52-3b8e-4f6a-9a4d-0c2b6e1f8a11"
CONTEXT_FIELDS = {
    "community_public_id",
    "community_id",
    "community_name",
    "explicit",
    "communities",
}
COMMUNITY_FIELDS = {"public_id", "display_name", "position"}


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """Own uniquely-named container, migrated to alembic `head`."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hub-api-active-context-grpc") as db:
        yield db


def _identity(service_id: str, scopes: set[str], tenant: str) -> ServiceIdentity:
    return ServiceIdentity(
        service_id=service_id,
        k8s_namespace="waddlebot",
        k8s_service_account=service_id.rsplit("/", 1)[-1],
        allowed_scopes=frozenset(scopes),
        tenant=tenant,
    )


@pytest.fixture
def issuer() -> ServiceJwtIssuer:
    """svc-ingest (holds the scope), svc-action (does not), a non-system tenant."""
    private = ec.generate_private_key(ec.SECP256R1())
    key = SigningKey(kid="k1", private_key=private, public_key=private.public_key())
    return ServiceJwtIssuer(
        keys={"k1": key},
        active_kid="k1",
        identities={
            INGEST: _identity(INGEST, {SCOPE, "dataplane:resolve-scope"}, "system"),
            OTHER: _identity(OTHER, {"connections:resolve"}, "system"),
            TENANTED: _identity(TENANTED, {SCOPE}, "acme"),
        },
    )


@dataclass(slots=True, frozen=True)
class _Hub:
    """What a test needs to start the production-chain gRPC server over `install_dal`."""

    install_dal: Any
    issuer: ServiceJwtIssuer


@asynccontextmanager
async def _serve(hub: _Hub) -> AsyncIterator[str]:
    """A real `grpc.aio` server on loopback with the production interceptor chain."""
    server = grpc.aio.server(interceptors=build_interceptor_chain(hub.issuer))
    active_context_pb2_grpc.add_ActiveContextServiceServicer_to_server(
        ctx_servicer.ActiveContextServicer(hub.install_dal), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(grace=None)


def _metadata(
    issuer: ServiceJwtIssuer, service: str = INGEST, scope: str = SCOPE
) -> tuple[tuple[str, str], ...]:
    return (("authorization", f"Bearer {issuer.issue(service, scope)}"),)


async def _rpc(
    hub: _Hub,
    method: str,
    request: Any,
    *,
    service: str = INGEST,
    scope: str = SCOPE,
    metadata: tuple[tuple[str, str], ...] | None = None,
) -> Any:
    """One real call; returns the response `context`, raises `AioRpcError` on refusal."""
    async with _serve(hub) as addr, grpc.aio.insecure_channel(addr) as channel:
        stub = active_context_pb2_grpc.ActiveContextServiceStub(channel)
        md = _metadata(hub.issuer, service, scope) if metadata is None else metadata
        call = getattr(stub, method)
        return (await call(request, metadata=md, timeout=3.0)).context


def _get_req(
    user: str = USER, guild: str = "", platform: str = "discord"
) -> active_context_pb2.GetActiveContextRequest:
    return active_context_pb2.GetActiveContextRequest(platform=platform, source_id=guild, user=user)


def _set_req(guild: str, community: str, user: str = USER) -> Any:
    return active_context_pb2.SetActiveContextRequest(
        platform="discord", source_id=guild, user=user, community=community
    )


async def _code(awaitable: Any) -> grpc.StatusCode:
    """The status a refused call fails with (the call must fail)."""
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await awaitable
    return exc_info.value.code()


async def _count(dal: Any) -> int:
    async with dal.engine.connect() as conn:
        return int(
            (await conn.execute(text("SELECT count(*) FROM user_active_context"))).scalar_one()
        )


async def _foreign(dal: Any, guild: str, suffix: str) -> tuple[int, str]:
    """A second tenant's community also paired to `guild` -> the source spans tenants."""
    async with dal.engine.begin() as conn:
        tenant = (
            await conn.execute(
                text("INSERT INTO tenants (slug) VALUES (:s) RETURNING id"),
                {"s": f"ctx-foreign-{suffix}"},
            )
        ).scalar_one()
        row = (
            await conn.execute(
                text(
                    "INSERT INTO communities (tenant_id, name) VALUES (:t, :n) "
                    "RETURNING id, public_id"
                ),
                {"t": tenant, "n": f"Foreign-{suffix}"},
            )
        ).one()
        await conn.execute(
            text(
                "INSERT INTO community_servers (community_id, platform, platform_server_id, "
                "status) VALUES (:c, 'discord', :g, 'approved')"
            ),
            {"c": row.id, "g": guild},
        )
    return int(row.id), str(row.public_id)


async def test_default_is_first_registered_with_numeric_community_id(
    dal: Any, issuer: ServiceJwtIssuer
) -> None:
    w = await _world(dal)
    got = await _rpc(_Hub(dal, issuer), "GetActiveContext", _get_req(guild=w.guild))
    assert got.community_id == w.first
    assert got.community_public_id == w.first_pub
    assert got.community_name.startswith("Alpha")
    assert got.explicit is False
    assert [c.public_id for c in got.communities] == [w.first_pub, w.second_pub]
    assert [c.position for c in got.communities] == [0, 1]


async def test_exact_field_set_regression(dal: Any, issuer: ServiceJwtIssuer) -> None:
    """The answer carries exactly the svc-ingest `ContextView` fields -- nothing leaks."""
    w = await _world(dal)
    hub = _Hub(dal, issuer)
    got = await _rpc(hub, "GetActiveContext", _get_req(guild=w.guild))
    assert {f.name for f in got.DESCRIPTOR.fields} == CONTEXT_FIELDS
    assert got.communities, "denominator: at least one community listed"
    for entry in got.communities:
        assert {f.name for f in entry.DESCRIPTOR.fields} == COMMUNITY_FIELDS
    put = await _rpc(hub, "SetActiveContext", _set_req(w.guild, w.first_pub))
    assert {f.name for f in put.DESCRIPTOR.fields} == CONTEXT_FIELDS


async def test_set_switches_and_get_honours_the_choice(dal: Any, issuer: ServiceJwtIssuer) -> None:
    w = await _world(dal)
    hub = _Hub(dal, issuer)
    before = await _count(dal)
    put = await _rpc(hub, "SetActiveContext", _set_req(w.guild, w.second_pub))
    assert (put.community_id, put.explicit) == (w.second, True)
    got = await _rpc(hub, "GetActiveContext", _get_req(guild=w.guild))
    assert (got.community_id, got.explicit) == (w.second, True)
    other = await _rpc(hub, "GetActiveContext", _get_req(str(uuid.uuid4()), w.guild))
    assert (other.community_id, other.explicit) == (w.first, False)
    await _rpc(hub, "SetActiveContext", _set_req(w.guild, w.first_pub))
    assert await _count(dal) == before + 1  # upsert, not a second row


async def test_stored_choice_that_left_the_set_yields_to_default(
    dal: Any, issuer: ServiceJwtIssuer
) -> None:
    w = await _world(dal)
    hub = _Hub(dal, issuer)
    await _rpc(hub, "SetActiveContext", _set_req(w.guild, w.second_pub))
    async with dal.engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM community_servers WHERE community_id = :c"), {"c": w.second}
        )
    got = await _rpc(hub, "GetActiveContext", _get_req(guild=w.guild))
    assert (got.community_id, got.explicit) == (w.first, False)


@pytest.mark.parametrize("which", ["foreign", "unknown", "malformed"])
async def test_set_refuses_a_community_outside_the_set(
    dal: Any, issuer: ServiceJwtIssuer, which: str
) -> None:
    w = await _world(dal)
    _, foreign_pub = await _foreign(dal, f"{w.guild[:-1]}9", w.guild)
    target = {"foreign": foreign_pub, "unknown": str(uuid.uuid4()), "malformed": "Alpha"}[which]
    before = await _count(dal)
    code = await _code(_rpc(_Hub(dal, issuer), "SetActiveContext", _set_req(w.guild, target)))
    assert code == (
        grpc.StatusCode.INVALID_ARGUMENT if which == "malformed" else grpc.StatusCode.NOT_FOUND
    )
    assert await _count(dal) == before


async def test_unlinked_source_is_not_found(dal: Any, issuer: ServiceJwtIssuer) -> None:
    code = await _code(
        _rpc(_Hub(dal, issuer), "GetActiveContext", _get_req(guild="999000111222333444"))
    )
    assert code == grpc.StatusCode.NOT_FOUND


async def test_source_spanning_tenants_is_refused_never_pinned(
    dal: Any, issuer: ServiceJwtIssuer
) -> None:
    w = await _world(dal)
    await _foreign(dal, w.guild, w.guild)  # a second tenant also claims the guild
    hub = _Hub(dal, issuer)
    before = await _count(dal)
    got = await _code(_rpc(hub, "GetActiveContext", _get_req(guild=w.guild)))
    put = await _code(_rpc(hub, "SetActiveContext", _set_req(w.guild, w.first_pub)))
    assert (got, put) == (grpc.StatusCode.FAILED_PRECONDITION,) * 2
    assert await _count(dal) == before


@pytest.mark.parametrize(
    ("user", "guild", "platform"),
    [
        ("not-a-uuid", "474965105759748096", "discord"),
        (USER, "", "discord"),
        (USER, "474965105759748096", "Bad Platform"),
    ],
)
async def test_invalid_parameters_are_invalid_argument(
    dal: Any, issuer: ServiceJwtIssuer, user: str, guild: str, platform: str
) -> None:
    code = await _code(_rpc(_Hub(dal, issuer), "GetActiveContext", _get_req(user, guild, platform)))
    assert code == grpc.StatusCode.INVALID_ARGUMENT


async def test_no_credential_is_unauthenticated(dal: Any, issuer: ServiceJwtIssuer) -> None:
    hub = _Hub(dal, issuer)
    assert await _code(_rpc(hub, "GetActiveContext", _get_req(), metadata=())) == (
        grpc.StatusCode.UNAUTHENTICATED
    )
    assert await _code(_rpc(hub, "SetActiveContext", _set_req("1", USER), metadata=())) == (
        grpc.StatusCode.UNAUTHENTICATED
    )


async def test_wrong_scope_is_refused_on_both_rpcs(dal: Any, issuer: ServiceJwtIssuer) -> None:
    hub = _Hub(dal, issuer)
    for method, req in (
        ("GetActiveContext", _get_req()),
        ("SetActiveContext", _set_req("1", USER)),
    ):
        wrong = await _code(_rpc(hub, method, req, service=OTHER, scope="connections:resolve"))
        assert wrong == grpc.StatusCode.UNAUTHENTICATED
        resolve = await _code(_rpc(hub, method, req, scope="dataplane:resolve-scope"))
        assert resolve == grpc.StatusCode.UNAUTHENTICATED


def test_method_scopes_are_registered() -> None:
    """The table the chain enforces names both RPCs (an unlisted method fails closed)."""
    assert ALL_REQUIRED_SCOPES[GET] == SCOPE
    assert ALL_REQUIRED_SCOPES[SET] == SCOPE


async def test_non_system_tenant_is_permission_denied(dal: Any, issuer: ServiceJwtIssuer) -> None:
    code = await _code(_rpc(_Hub(dal, issuer), "GetActiveContext", _get_req(), service=TENANTED))
    assert code == grpc.StatusCode.PERMISSION_DENIED


async def test_unconfigured_store_is_unavailable_never_a_default(issuer: ServiceJwtIssuer) -> None:
    code = await _code(_rpc(_Hub(None, issuer), "GetActiveContext", _get_req()))
    assert code == grpc.StatusCode.UNAVAILABLE


async def test_tls_listener_serves_a_real_context_end_to_end(
    dal: Any, issuer: ServiceJwtIssuer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production TLS listener (`start_internal_grpc_server`) answers a real lookup."""
    w = await _world(dal)
    cert_path, key_path, cert_pem = _generate_self_signed_cert(tmp_path)
    port = _free_port()
    monkeypatch.setenv("GRPC_TLS_CERT_PATH", str(cert_path))
    monkeypatch.setenv("GRPC_TLS_KEY_PATH", str(key_path))
    monkeypatch.setenv("GRPC_PORT", str(port))
    monkeypatch.delenv("GRPC_TLS_CLIENT_CA_PATH", raising=False)
    server = await start_internal_grpc_server(issuer=issuer, app_config={"install_dal": dal})
    try:
        creds = grpc.ssl_channel_credentials(root_certificates=cert_pem)
        async with grpc.aio.secure_channel(f"127.0.0.1:{port}", creds) as channel:
            stub = active_context_pb2_grpc.ActiveContextServiceStub(channel)
            resp = await stub.GetActiveContext(
                _get_req(guild=w.guild), metadata=_metadata(issuer), timeout=3.0
            )
    finally:
        await stop_internal_grpc_server(server)
    assert resp.context.community_id == w.first


async def test_deleting_a_community_cascades_its_contexts(
    dal: Any, issuer: ServiceJwtIssuer
) -> None:
    w = await _world(dal)
    await _rpc(_Hub(dal, issuer), "SetActiveContext", _set_req(w.guild, w.second_pub))
    before = await _count(dal)
    async with dal.engine.begin() as conn:
        await conn.execute(text("DELETE FROM communities WHERE id = :c"), {"c": w.second})
    assert await _count(dal) == before - 1
