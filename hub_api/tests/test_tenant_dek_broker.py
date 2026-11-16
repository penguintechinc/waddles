"""`KeyService.GetTenantDek`: the at-rest lineage served by the shared `StreamDekBroker`.

Real: the `StreamDekBroker`, two real `TenantEnvelopeService` lineages (at-rest + ingest-stream)
over in-memory rows with the real platform KEK, the production `AuthInterceptor` in front of the
real `KeyServicer` on a `grpc.aio` server, and an independent client that mirrors the Rust
`hpke_seal_open`. Only the tenant-lookup and audit SQL helpers are faked (as in
`test_stream_dek_broker.py`). Covers the cross-tenant boundary: a tenant-bound caller can never
obtain another tenant's key, and one tenant's key can never open another tenant's ciphertext.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import grpc
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey
from waddles.hub.internal.v1 import key_pb2, key_pb2_grpc

import services.stream_dek_broker as broker_mod
from grpc_internal.interceptors import AuthInterceptor, DeadlineInterceptor
from grpc_internal.servicers import REQUIRED_SCOPES, KeyServicer
from services.envelope.kms_adapter import KmsProviderRegistry
from services.envelope.repository import PURPOSE_INGEST_STREAM
from services.envelope.service import TenantEnvelopeService
from services.stream_dek_broker import (
    OUTCOME_DENIED_SERVICE,
    OUTCOME_DENIED_TENANT,
    OUTCOME_INVALID,
    OUTCOME_ISSUED,
    PURPOSE_AT_REST,
    STREAM_DEK_SCOPE,
    TENANT_DEK_SCOPE,
    StreamDekBroker,
    StreamDekDeniedError,
    StreamDekInvalidError,
    StreamDekRequest,
    StreamDekServiceDeniedError,
    seal_context,
)
from tests.envelope.fakes import CountingPlatformKek, FakeGate, InMemoryEnvelopeRepo

ACTION = "spiffe://penguintech.io/alpha/svc-action"
PROCESS = "spiffe://penguintech.io/alpha/svc-process"
PRESENTATION = "spiffe://penguintech.io/alpha/svc-presentation"
TENANT_A = 7
TENANT_B = 8
AT_REST = key_pb2.TENANT_DEK_PURPOSE_AT_REST


def pub(client: X25519PrivateKey) -> bytes:
    """Raw 32-byte X25519 public key."""
    return client.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


def unseal(enc: bytes, ciphertext: bytes, client: X25519PrivateKey, context: bytes) -> bytes:
    """Independent client: the Rust `hpke_seal_open` algorithm."""
    shared = client.exchange(X25519PublicKey.from_public_bytes(enc))
    okm = HKDF(algorithm=hashes.SHA256(), length=44, salt=None, info=context).derive(shared)
    return AESGCM(okm[:32]).decrypt(okm[32:], ciphertext, context)


@pytest.fixture(autouse=True)
def spiffe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin this deployment's SPIFFE env segment (production reads SPIFFE_ENV)."""
    monkeypatch.setenv("SPIFFE_ENV", "alpha")


@dataclass(slots=True)
class FakeDb:
    """The tenants table + audit table the broker touches, in memory."""

    tenants: dict[int, str]
    audit: list[dict[str, Any]]


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDb:
    """Patch the broker's SQL helpers onto an in-memory tenants/audit store."""
    store = FakeDb(tenants={TENANT_A: "acme", TENANT_B: "other"}, audit=[])

    async def rows(_dal: Any, sql: str, params: Mapping[str, Any]) -> list[dict[str, int]]:
        if "WHERE id = :t" in sql:
            return [{"id": params["t"]}] if params["t"] in store.tenants else []
        ref = params["r"]
        return [{"id": t} for t, slug in store.tenants.items() if ref in (str(t), slug)][:1]

    async def write(_dal: Any, sql: str, params: Mapping[str, Any]) -> list[dict[str, int]]:
        store.audit.append(dict(params))
        return [{"id": len(store.audit)}]

    monkeypatch.setattr(broker_mod, "raw_sql_rows", rows)
    monkeypatch.setattr(broker_mod, "raw_sql_write", write)
    return store


def _service(lineage: str | None, kek: CountingPlatformKek) -> TenantEnvelopeService:
    repo = InMemoryEnvelopeRepo()
    return TenantEnvelopeService(
        keys=repo,
        configs=repo,
        registry=KmsProviderRegistry(),
        platform_kek=lambda: kek,
        gate=FakeGate(),
        lineage=lineage,
    )


@dataclass(slots=True)
class _Runtime:
    service: TenantEnvelopeService
    stream_service: TenantEnvelopeService


@pytest.fixture
def runtime() -> _Runtime:
    """Both real lineages over one platform KEK (as in production)."""
    kek = CountingPlatformKek(secrets.token_bytes(32))
    return _Runtime(
        service=_service(None, kek), stream_service=_service(PURPOSE_INGEST_STREAM, kek)
    )


@pytest.fixture
def broker(db: FakeDb, runtime: _Runtime) -> StreamDekBroker:
    """Broker serving both lineages (TTL 600 s)."""
    return StreamDekBroker(
        object(),  # type: ignore[arg-type]
        runtime.stream_service,
        at_rest_keys=runtime.service,
        ttl_seconds=600,
    )


def req(
    client: X25519PrivateKey,
    *,
    tenant: int = TENANT_A,
    claim: str = "system",
    sub: str = ACTION,
    purpose: str = PURPOSE_AT_REST,
    version: int = 0,
) -> StreamDekRequest:
    """A request as the servicer builds it from proto + verified claims."""
    return StreamDekRequest(
        tenant_id=str(tenant),
        purpose=purpose,
        version=version,
        caller_public_key=pub(client),
        claim_tenant=claim,
        caller_sub=sub,
    )


async def _key(broker: StreamDekBroker, **kwargs: Any) -> bytes:
    client = X25519PrivateKey.generate()
    request = req(client, **kwargs)
    issued = await broker.issue(request, lineage=request.purpose)
    service = kwargs.get("sub", ACTION).rsplit("/", 1)[-1]
    tenant = kwargs.get("tenant", TENANT_A)
    ctx = seal_context(service, tenant, request.purpose, issued.version)
    return unseal(issued.sealed_dek[:32], issued.sealed_dek[32:], client, ctx)


class TestAtRestLineage:
    """The at-rest key through the shared broker."""

    async def test_issue_is_audited_and_opens_with_the_bound_context(
        self, broker: StreamDekBroker, db: FakeDb
    ) -> None:
        key = await _key(broker)
        assert len(key) == 32
        assert db.audit == [
            {
                "tenant_id": TENANT_A,
                "purpose": PURPOSE_AT_REST,
                "dek_version": 1,
                "caller_service": "svc-action",
                "outcome": OUTCOME_ISSUED,
            }
        ]
        assert key.hex() not in repr(db.audit)

    async def test_stable_per_tenant_and_version_across_callers(
        self, broker: StreamDekBroker
    ) -> None:
        assert await _key(broker, sub=ACTION) == await _key(broker, sub=PROCESS)

    async def test_tenants_get_different_keys(self, broker: StreamDekBroker) -> None:
        """Cross-tenant isolation at the key level: A's key is not B's key."""
        assert await _key(broker, tenant=TENANT_A) != await _key(broker, tenant=TENANT_B)

    async def test_at_rest_key_is_domain_separated_from_the_stream_key(
        self, broker: StreamDekBroker
    ) -> None:
        at_rest = await _key(broker, sub=PROCESS)
        stream = await _key(broker, sub=PROCESS, purpose=PURPOSE_INGEST_STREAM)
        assert at_rest != stream

    async def test_seal_context_binds_service_tenant_purpose_and_version(
        self, broker: StreamDekBroker
    ) -> None:
        client = X25519PrivateKey.generate()
        issued = await broker.issue(req(client), lineage=PURPOSE_AT_REST)
        enc, ct = issued.sealed_dek[:32], issued.sealed_dek[32:]
        unseal(enc, ct, client, b"svc-action|7|at-rest|1")
        for wrong in (
            b"svc-process|7|at-rest|1",
            b"svc-action|8|at-rest|1",
            b"svc-action|7|ingest-stream|1",
            b"svc-action|7|at-rest|2",
        ):
            with pytest.raises(Exception):  # noqa: B017 - cryptography InvalidTag
                unseal(enc, ct, client, wrong)

    async def test_rotation_keeps_retired_version_readable(
        self, broker: StreamDekBroker, runtime: _Runtime
    ) -> None:
        v1 = await _key(broker)
        await runtime.service.rotate_dek(TENANT_A)
        assert await _key(broker, version=1) == v1
        assert await _key(broker) != v1


class TestAuthorization:
    """Who may obtain an at-rest key, and for which tenant."""

    async def test_tenant_bound_caller_cannot_fetch_another_tenants_key(
        self, broker: StreamDekBroker, db: FakeDb
    ) -> None:
        client = X25519PrivateKey.generate()
        with pytest.raises(StreamDekDeniedError):
            await broker.issue(req(client, tenant=TENANT_B, claim="acme"), lineage=PURPOSE_AT_REST)
        assert db.audit[-1]["outcome"] == OUTCOME_DENIED_TENANT

    async def test_tenant_bound_caller_gets_its_own(self, broker: StreamDekBroker) -> None:
        assert len(await _key(broker, claim="acme")) == 32

    async def test_slug_claim_cannot_reach_a_numeric_other_tenant(
        self, broker: StreamDekBroker
    ) -> None:
        with pytest.raises(StreamDekDeniedError):
            await broker.issue(
                req(X25519PrivateKey.generate(), tenant=TENANT_B, claim="acme"),
                lineage=PURPOSE_AT_REST,
            )

    async def test_unknown_tenant_denied(self, broker: StreamDekBroker) -> None:
        with pytest.raises(StreamDekDeniedError):
            await broker.issue(req(X25519PrivateKey.generate(), tenant=99), lineage=PURPOSE_AT_REST)

    async def test_non_data_plane_service_denied_and_audited(
        self, broker: StreamDekBroker, db: FakeDb
    ) -> None:
        with pytest.raises(StreamDekServiceDeniedError):
            await broker.issue(
                req(X25519PrivateKey.generate(), sub=PRESENTATION), lineage=PURPOSE_AT_REST
            )
        assert db.audit[-1]["outcome"] == OUTCOME_DENIED_SERVICE

    async def test_svc_action_holds_at_rest_but_not_stream_keys(
        self, broker: StreamDekBroker
    ) -> None:
        assert len(await _key(broker, sub=ACTION)) == 32
        with pytest.raises(StreamDekServiceDeniedError):
            await broker.issue(
                req(X25519PrivateKey.generate(), sub=ACTION, purpose=PURPOSE_INGEST_STREAM),
                lineage=PURPOSE_INGEST_STREAM,
            )

    async def test_purpose_must_match_the_served_lineage(
        self, broker: StreamDekBroker, db: FakeDb
    ) -> None:
        with pytest.raises(StreamDekInvalidError):
            await broker.issue(
                req(X25519PrivateKey.generate(), purpose=PURPOSE_INGEST_STREAM),
                lineage=PURPOSE_AT_REST,
            )
        assert db.audit[-1]["outcome"] == OUTCOME_INVALID

    async def test_unconfigured_at_rest_lineage_unavailable(
        self, db: FakeDb, runtime: _Runtime
    ) -> None:
        stream_only = StreamDekBroker(object(), runtime.stream_service)  # type: ignore[arg-type]
        with pytest.raises(Exception, match="not configured"):
            await stream_only.issue(req(X25519PrivateKey.generate()), lineage=PURPOSE_AT_REST)


def _issuer(sub: str, scopes: frozenset[str], tenant: str = "system") -> ServiceJwtIssuer:
    private_key = ec.generate_private_key(ec.SECP256R1())
    signing = SigningKey(kid="k", private_key=private_key, public_key=private_key.public_key())
    identity = ServiceIdentity(
        service_id=sub,
        k8s_namespace="waddlebot",
        k8s_service_account=sub.rsplit("/", 1)[-1],
        allowed_scopes=scopes,
        tenant=tenant,
    )
    return ServiceJwtIssuer(keys={"k": signing}, active_kid="k", identities={sub: identity})


@dataclass(slots=True)
class Wire:
    """A running server and the issuer that mints tokens for it."""

    address: str
    issuer: ServiceJwtIssuer


async def _serve(issuer: ServiceJwtIssuer, runtime: _Runtime | None) -> AsyncIterator[str]:
    server = grpc.aio.server(
        interceptors=[
            DeadlineInterceptor(),
            AuthInterceptor(verifier=issuer.as_verifier(), required_scopes=REQUIRED_SCOPES),
        ]
    )
    config: dict[str, Any] = {"install_dal": object()}
    if runtime is not None:
        config["envelope_runtime"] = runtime
    key_pb2_grpc.add_KeyServiceServicer_to_server(KeyServicer(config), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(grace=None)


@pytest.fixture
async def wire(db: FakeDb, runtime: _Runtime) -> AsyncIterator[Wire]:
    """Real gRPC server; svc-action (system) holds both DEK scopes."""
    issuer = _issuer(ACTION, frozenset({TENANT_DEK_SCOPE, STREAM_DEK_SCOPE}))
    async for address in _serve(issuer, runtime):
        yield Wire(address, issuer)


async def _get(
    wire: Wire,
    client: X25519PrivateKey,
    *,
    tenant: int = TENANT_A,
    purpose: int = AT_REST,
    version: int = 0,
    token: str | None = None,
) -> key_pb2.GetTenantDekResponse:
    async with grpc.aio.insecure_channel(wire.address) as channel:
        stub = key_pb2_grpc.KeyServiceStub(channel)
        bearer = token or wire.issuer.issue(ACTION, TENANT_DEK_SCOPE)
        return await stub.GetTenantDek(  # type: ignore[no-any-return]
            key_pb2.GetTenantDekRequest(
                tenant_id=tenant,
                purpose=purpose,
                dek_version=version,
                caller_ephemeral_public_key=pub(client),
            ),
            metadata=(("authorization", f"Bearer {bearer}"),),
            timeout=3.0,
        )


class TestGrpc:
    """GetTenantDek at the wire, behind the production auth interceptor."""

    async def test_scope_constant_is_the_contract(self) -> None:
        assert REQUIRED_SCOPES["/waddles.hub.internal.v1.KeyService/GetTenantDek"] == (
            "keys:tenant-dek:read"
        )

    async def test_round_trip_matches_the_broker_key_and_field_set_is_exact(
        self, wire: Wire, broker: StreamDekBroker
    ) -> None:
        client = X25519PrivateKey.generate()
        response = await _get(wire, client)
        assert {f.name for f, _ in response.ListFields()} == {
            "enc",
            "ciphertext",
            "dek_version",
            "ttl_seconds",
        }
        assert len(response.enc) == 32 and len(response.ciphertext) == 48
        key = unseal(response.enc, response.ciphertext, client, b"svc-action|7|at-rest|1")
        assert key == await _key(broker)
        assert response.dek_version == 1 and response.ttl_seconds >= 60

    async def test_ingest_stream_purpose_is_refused_here(self, wire: Wire) -> None:
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await _get(
                wire, X25519PrivateKey.generate(), purpose=key_pb2.TENANT_DEK_PURPOSE_INGEST_STREAM
            )
        assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    async def test_unspecified_purpose_is_refused(self, wire: Wire) -> None:
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await _get(wire, X25519PrivateKey.generate(), purpose=0)
        assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    async def test_stream_scope_cannot_call_get_tenant_dek(self, wire: Wire) -> None:
        token = wire.issuer.issue(ACTION, STREAM_DEK_SCOPE)
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await _get(wire, X25519PrivateKey.generate(), token=token)
        assert exc.value.code() in (
            grpc.StatusCode.UNAUTHENTICATED,
            grpc.StatusCode.PERMISSION_DENIED,
        )

    async def test_missing_token_unauthenticated(self, wire: Wire) -> None:
        async with grpc.aio.insecure_channel(wire.address) as channel:
            stub = key_pb2_grpc.KeyServiceStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.GetTenantDek(
                    key_pb2.GetTenantDekRequest(
                        tenant_id=TENANT_A,
                        purpose=AT_REST,
                        caller_ephemeral_public_key=pub(X25519PrivateKey.generate()),
                    ),
                    timeout=2.0,
                )
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED

    async def test_bad_public_key_invalid_argument(self, wire: Wire) -> None:
        async with grpc.aio.insecure_channel(wire.address) as channel:
            stub = key_pb2_grpc.KeyServiceStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.GetTenantDek(
                    key_pb2.GetTenantDekRequest(
                        tenant_id=TENANT_A, purpose=AT_REST, caller_ephemeral_public_key=b"\x01" * 5
                    ),
                    metadata=(
                        ("authorization", f"Bearer {wire.issuer.issue(ACTION, TENANT_DEK_SCOPE)}"),
                    ),
                    timeout=2.0,
                )
        assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    async def test_tenant_bound_token_denied_for_other_tenant(
        self, db: FakeDb, runtime: _Runtime
    ) -> None:
        issuer = _issuer(ACTION, frozenset({TENANT_DEK_SCOPE}), tenant="acme")
        async for address in _serve(issuer, runtime):
            wire = Wire(address, issuer)
            assert len((await _get(wire, X25519PrivateKey.generate(), tenant=TENANT_A)).enc) == 32
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await _get(wire, X25519PrivateKey.generate(), tenant=TENANT_B)
            assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED

    async def test_unwired_key_store_unavailable(self) -> None:
        issuer = _issuer(ACTION, frozenset({TENANT_DEK_SCOPE}))
        async for address in _serve(issuer, None):
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await _get(Wire(address, issuer), X25519PrivateKey.generate())
            assert exc.value.code() == grpc.StatusCode.UNAVAILABLE
