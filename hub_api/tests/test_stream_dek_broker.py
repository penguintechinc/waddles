"""KeyService.GetStreamDek broker (#442): authz, sealing contract, versioning, rotation, audit.

Drives the REAL `TenantEnvelopeService` (ingest-stream lineage) over the in-memory key repo and
the real platform KEK, the real `StreamDekBroker`, and -- for the gRPC tests -- a real
`grpc.aio` server with the production `AuthInterceptor`. Only the two SQL helpers (tenant
lookup + audit insert) are faked here; the real-Postgres path is
`tests/envelope/test_stream_dek_pg.py`.

The unseal helper mirrors svc-ingest/svc-process `hpke_seal_open` byte for byte (X25519 ECDH,
HKDF-SHA256 salt=None info=context -> key||nonce, AES-256-GCM with AAD=context), so these tests
pin the wire contract the Rust side consumes.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import grpc
import pytest
from cryptography.exceptions import InvalidTag
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
from services.envelope.crypto import derive_data_subkey
from services.envelope.kms_adapter import KmsProviderRegistry
from services.envelope.repository import PURPOSE_INGEST_STREAM
from services.envelope.service import TenantEnvelopeService
from services.stream_dek_broker import (
    OUTCOME_DENIED_SERVICE,
    OUTCOME_DENIED_TENANT,
    OUTCOME_INVALID,
    OUTCOME_ISSUED,
    OUTCOME_NOT_FOUND,
    OUTCOME_UNAVAILABLE,
    STREAM_DEK_SCOPE,
    StreamDekBroker,
    StreamDekDeniedError,
    StreamDekInvalidError,
    StreamDekNotFoundError,
    StreamDekRequest,
    StreamDekServiceDeniedError,
    StreamDekUnavailableError,
    seal_context,
    seal_dek,
    service_name,
    ttl_seconds_from_env,
)
from tests.envelope.fakes import CountingPlatformKek, FakeGate, InMemoryEnvelopeRepo

INGEST = "spiffe://penguintech.io/alpha/svc-ingest"
PROCESS = "spiffe://penguintech.io/alpha/svc-process"
ACTION = "spiffe://penguintech.io/alpha/svc-action"
TENANT = 7
OTHER_TENANT = 8
SLUG = "acme"
PURPOSE = PURPOSE_INGEST_STREAM


def unseal(sealed: bytes, client: X25519PrivateKey, context: bytes) -> bytes:
    """Test-side mirror of the Rust `hpke_seal_open` (the contract under test)."""
    enc, ciphertext = sealed[:32], sealed[32:]
    shared = client.exchange(X25519PublicKey.from_public_bytes(enc))
    okm = HKDF(algorithm=hashes.SHA256(), length=44, salt=None, info=context).derive(shared)
    return AESGCM(okm[:32]).decrypt(okm[32:], ciphertext, context)


def pub(client: X25519PrivateKey) -> bytes:
    """Raw 32-byte X25519 public key."""
    return client.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


@dataclass(slots=True)
class FakeDb:
    """The tenants table + audit table the broker touches, in memory."""

    tenants: dict[int, str]
    audit: list[dict[str, Any]]
    fail_audit: bool = False


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDb:
    """Patch the broker's SQL helpers onto an in-memory tenants/audit store."""
    store = FakeDb(tenants={TENANT: SLUG, OTHER_TENANT: "other"}, audit=[])

    async def rows(_dal: Any, sql: str, params: Mapping[str, Any]) -> list[dict[str, int]]:
        if "WHERE id = :t" in sql:
            return [{"id": params["t"]}] if params["t"] in store.tenants else []
        ref = params["r"]
        for tid, slug in store.tenants.items():
            if ref in (str(tid), slug):
                return [{"id": tid}]
        return []

    async def write(_dal: Any, sql: str, params: Mapping[str, Any]) -> list[dict[str, int]]:
        assert "keystore.stream_dek_issuances" in sql
        if store.fail_audit:
            raise RuntimeError("audit store down")
        store.audit.append(dict(params))
        return [{"id": len(store.audit)}]

    monkeypatch.setattr(broker_mod, "raw_sql_rows", rows)
    monkeypatch.setattr(broker_mod, "raw_sql_write", write)
    return store


@pytest.fixture
def keys() -> TenantEnvelopeService:
    """The real stream-lineage envelope service over in-memory rows + the real platform KEK."""
    repo = InMemoryEnvelopeRepo()
    kek = CountingPlatformKek(secrets.token_bytes(32))
    return TenantEnvelopeService(
        keys=repo,
        configs=repo,
        registry=KmsProviderRegistry(),
        platform_kek=lambda: kek,
        gate=FakeGate(),
        lineage=PURPOSE_INGEST_STREAM,
    )


@pytest.fixture
def broker(db: FakeDb, keys: TenantEnvelopeService) -> StreamDekBroker:
    """Broker under test (TTL 600 s)."""
    return StreamDekBroker(object(), keys, ttl_seconds=600)  # type: ignore[arg-type]


def req(
    client: X25519PrivateKey,
    *,
    tenant: str = str(TENANT),
    claim: str = "system",
    sub: str = INGEST,
    purpose: str = PURPOSE,
    version: int = 0,
    public_key: bytes | None = None,
) -> StreamDekRequest:
    """A request as the servicer would build it from proto + verified claims."""
    return StreamDekRequest(
        tenant_id=tenant,
        purpose=purpose,
        version=version,
        caller_public_key=pub(client) if public_key is None else public_key,
        claim_tenant=claim,
        caller_sub=sub,
    )


class TestSealingContract:
    """Byte-level contract with the Rust consumers."""

    def test_seal_round_trips_with_the_rust_construction(self) -> None:
        client = X25519PrivateKey.generate()
        dek = os.urandom(32)
        ctx = seal_context("svc-process", 7, PURPOSE, 3)
        assert ctx == b"svc-process|7|ingest-stream|3"
        sealed = seal_dek(dek, pub(client), ctx)
        assert len(sealed) == 32 + 32 + 16
        assert unseal(sealed, client, ctx) == dek

    def test_wrong_context_or_key_fails_authentication(self) -> None:
        client = X25519PrivateKey.generate()
        sealed = seal_dek(os.urandom(32), pub(client), b"svc-ingest|7|ingest-stream|1")
        with pytest.raises(InvalidTag):
            unseal(sealed, client, b"svc-ingest|8|ingest-stream|1")
        with pytest.raises(InvalidTag):
            unseal(sealed, X25519PrivateKey.generate(), b"svc-ingest|7|ingest-stream|1")

    def test_fresh_server_key_per_seal(self) -> None:
        client = X25519PrivateKey.generate()
        dek, ctx = os.urandom(32), b"svc-ingest|7|ingest-stream|1"
        assert seal_dek(dek, pub(client), ctx)[:32] != seal_dek(dek, pub(client), ctx)[:32]

    @pytest.mark.parametrize("bad", [b"", b"\x01" * 31, b"\x01" * 33, b"\x00" * 32])
    def test_unusable_public_keys_rejected(self, bad: bytes) -> None:
        with pytest.raises(StreamDekInvalidError):
            seal_dek(os.urandom(32), bad, b"ctx")

    def test_wrong_length_dek_never_sealed(self) -> None:
        with pytest.raises(StreamDekUnavailableError):
            seal_dek(b"\x00" * 16, pub(X25519PrivateKey.generate()), b"ctx")


class TestServiceAllowList:
    """Only svc-ingest / svc-process SPIFFE ids may hold stream DEKs."""

    @pytest.mark.parametrize(("sub", "name"), [(INGEST, "svc-ingest"), (PROCESS, "svc-process")])
    def test_allowed(self, sub: str, name: str) -> None:
        assert service_name(sub) == name

    @pytest.mark.parametrize(
        "sub",
        [
            ACTION,
            "",
            "svc-ingest",
            "spiffe://evil.example/alpha/svc-ingest",
            "spiffe://penguintech.io/alpha/svc-ingest/extra",
            "spiffe://penguintech.io/svc-ingest",
        ],
    )
    def test_denied(self, sub: str) -> None:
        with pytest.raises(StreamDekServiceDeniedError):
            service_name(sub)


class TestTtl:
    """Caller cache TTL config is bounded and fails loudly."""

    def test_default_and_override(self) -> None:
        assert ttl_seconds_from_env({}) == 600
        assert ttl_seconds_from_env({"STREAM_DEK_TTL_SECONDS": "120"}) == 120

    @pytest.mark.parametrize("raw", ["0", "59", "3601", "ten"])
    def test_out_of_range_rejected(self, raw: str) -> None:
        with pytest.raises(ValueError):
            ttl_seconds_from_env({"STREAM_DEK_TTL_SECONDS": raw})


class TestIssue:
    """End-to-end broker decisions over the real envelope service."""

    async def test_issue_is_stable_per_tenant_and_version_and_audited(
        self, broker: StreamDekBroker, db: FakeDb
    ) -> None:
        c1, c2 = X25519PrivateKey.generate(), X25519PrivateKey.generate()
        first = await broker.issue(req(c1))
        second = await broker.issue(req(c2, sub=PROCESS))
        assert first.version == second.version == 1
        assert first.ttl_seconds == 600
        dek_ingest = unseal(first.sealed_dek, c1, seal_context("svc-ingest", TENANT, PURPOSE, 1))
        dek_process = unseal(second.sealed_dek, c2, seal_context("svc-process", TENANT, PURPOSE, 1))
        assert dek_ingest == dek_process and len(dek_ingest) == 32
        assert [row["outcome"] for row in db.audit] == [OUTCOME_ISSUED, OUTCOME_ISSUED]
        assert db.audit[0] == {
            "tenant_id": TENANT,
            "purpose": PURPOSE,
            "dek_version": 1,
            "caller_service": "svc-ingest",
            "outcome": OUTCOME_ISSUED,
        }
        assert all(dek_ingest.hex() not in repr(row) for row in db.audit)

    async def test_tenants_get_different_keys(self, broker: StreamDekBroker) -> None:
        c1, c2 = X25519PrivateKey.generate(), X25519PrivateKey.generate()
        a = await broker.issue(req(c1))
        b = await broker.issue(req(c2, tenant=str(OTHER_TENANT)))
        ka = unseal(a.sealed_dek, c1, seal_context("svc-ingest", TENANT, PURPOSE, 1))
        kb = unseal(b.sealed_dek, c2, seal_context("svc-ingest", OTHER_TENANT, PURPOSE, 1))
        assert ka != kb

    async def test_rotation_issues_new_version_and_old_stays_readable(
        self, broker: StreamDekBroker, keys: TenantEnvelopeService
    ) -> None:
        c = X25519PrivateKey.generate()
        v1 = await broker.issue(req(c))
        k1 = unseal(v1.sealed_dek, c, seal_context("svc-ingest", TENANT, PURPOSE, 1))
        await keys.rotate_dek(TENANT, reason="test")
        c2 = X25519PrivateKey.generate()
        v2 = await broker.issue(req(c2))
        assert v2.version == 2
        k2 = unseal(v2.sealed_dek, c2, seal_context("svc-ingest", TENANT, PURPOSE, 2))
        assert k2 != k1
        c3 = X25519PrivateKey.generate()
        old = await broker.issue(req(c3, sub=PROCESS, version=1))
        assert old.version == 1
        assert unseal(old.sealed_dek, c3, seal_context("svc-process", TENANT, PURPOSE, 1)) == k1

    async def test_unknown_version_not_found_and_audited(
        self, broker: StreamDekBroker, db: FakeDb
    ) -> None:
        await broker.issue(req(X25519PrivateKey.generate()))
        with pytest.raises(StreamDekNotFoundError):
            await broker.issue(req(X25519PrivateKey.generate(), version=9))
        assert db.audit[-1]["outcome"] == OUTCOME_NOT_FOUND

    async def test_stream_key_is_lineage_separated_from_at_rest_subkey(
        self, broker: StreamDekBroker, keys: TenantEnvelopeService
    ) -> None:
        c = X25519PrivateKey.generate()
        issued = await broker.issue(req(c))
        got = unseal(issued.sealed_dek, c, seal_context("svc-ingest", TENANT, PURPOSE, 1))
        record = (await keys._keys.list_keys(TENANT))[0]  # noqa: SLF001 - white-box proof
        assert record.wrapped_dek is not None and got not in record.wrapped_dek
        raw = await keys._platform().unwrap(  # noqa: SLF001
            record.wrapped_dek, context={"waddles_purpose": "tenant-dek", "waddles_tenant_id": "7"}
        )
        assert got == derive_data_subkey(raw, TENANT, lineage=PURPOSE)
        assert got != derive_data_subkey(raw, TENANT)  # never the at-rest subkey
        assert got != raw  # never the raw DEK


class TestDenials:
    """Every refusal fails closed and leaves an audit row."""

    @pytest.mark.parametrize(
        ("kwargs", "error", "outcome"),
        [
            ({"sub": ACTION}, StreamDekServiceDeniedError, OUTCOME_DENIED_SERVICE),
            ({"tenant": SLUG}, StreamDekInvalidError, OUTCOME_INVALID),
            ({"tenant": "0"}, StreamDekInvalidError, OUTCOME_INVALID),
            ({"tenant": "-1"}, StreamDekInvalidError, OUTCOME_INVALID),
            ({"tenant": "99999999999"}, StreamDekInvalidError, OUTCOME_INVALID),
            ({"purpose": "at-rest"}, StreamDekInvalidError, OUTCOME_INVALID),
            ({"public_key": b"\x01" * 31}, StreamDekInvalidError, OUTCOME_INVALID),
            (
                {"claim": SLUG, "tenant": str(OTHER_TENANT)},
                StreamDekDeniedError,
                OUTCOME_DENIED_TENANT,
            ),
            ({"claim": "8", "tenant": str(TENANT)}, StreamDekDeniedError, OUTCOME_DENIED_TENANT),
            ({"claim": "ghost-tenant"}, StreamDekDeniedError, OUTCOME_DENIED_TENANT),
            ({"claim": "  "}, StreamDekDeniedError, OUTCOME_DENIED_TENANT),
            ({"tenant": "4242"}, StreamDekDeniedError, OUTCOME_DENIED_TENANT),
        ],
    )
    async def test_refused(
        self,
        broker: StreamDekBroker,
        db: FakeDb,
        keys: TenantEnvelopeService,
        kwargs: dict[str, Any],
        error: type[Exception],
        outcome: int,
    ) -> None:
        with pytest.raises(error):
            await broker.issue(req(X25519PrivateKey.generate(), **kwargs))
        assert db.audit[-1]["outcome"] == outcome
        assert await keys._keys.list_keys(TENANT) == []  # noqa: SLF001 - nothing provisioned

    async def test_tenant_bound_token_gets_its_own_tenant(
        self, broker: StreamDekBroker, db: FakeDb
    ) -> None:
        for claim in (SLUG, str(TENANT)):
            issued = await broker.issue(req(X25519PrivateKey.generate(), claim=claim))
            assert issued.version == 1
        assert db.audit[-1]["outcome"] == OUTCOME_ISSUED

    async def test_audit_failure_never_returns_a_key(
        self, broker: StreamDekBroker, db: FakeDb
    ) -> None:
        db.fail_audit = True
        with pytest.raises(StreamDekUnavailableError):
            await broker.issue(req(X25519PrivateKey.generate()))
        assert db.audit == []

    async def test_denial_audit_failure_still_denies(
        self, broker: StreamDekBroker, db: FakeDb
    ) -> None:
        db.fail_audit = True
        with pytest.raises(StreamDekServiceDeniedError):
            await broker.issue(req(X25519PrivateKey.generate(), sub=ACTION))

    async def test_kek_unavailable_fails_closed(self, db: FakeDb) -> None:
        repo = InMemoryEnvelopeRepo()

        def no_kek() -> CountingPlatformKek:
            from services.envelope.errors import KmsUnavailableError

            raise KmsUnavailableError("platform KEK missing")

        svc = TenantEnvelopeService(
            keys=repo,
            configs=repo,
            registry=KmsProviderRegistry(),
            platform_kek=no_kek,
            gate=FakeGate(),
            lineage=PURPOSE,
        )
        broker = StreamDekBroker(object(), svc)  # type: ignore[arg-type]
        with pytest.raises(StreamDekUnavailableError):
            await broker.issue(req(X25519PrivateKey.generate()))
        assert db.audit[-1]["outcome"] == OUTCOME_UNAVAILABLE


# -- gRPC: the production interceptor chain in front of the real servicer --------------------


@dataclass(slots=True)
class _Runtime:
    stream_service: TenantEnvelopeService


def _issuer(scopes: frozenset[str], sub: str = INGEST, tenant: str = "system") -> ServiceJwtIssuer:
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


@pytest.fixture
def issuer() -> ServiceJwtIssuer:
    """Issuer trusting svc-ingest (system tenant) with the stream-DEK scope + one other."""
    return _issuer(frozenset({STREAM_DEK_SCOPE, "identity:ephemeral:mint"}))


@pytest.fixture
async def address(
    issuer: ServiceJwtIssuer, db: FakeDb, keys: TenantEnvelopeService
) -> AsyncIterator[str]:
    """Real grpc.aio server: Deadline + Auth interceptors, real KeyServicer."""
    server = grpc.aio.server(
        interceptors=[
            DeadlineInterceptor(),
            AuthInterceptor(verifier=issuer.as_verifier(), required_scopes=REQUIRED_SCOPES),
        ]
    )
    config = {"envelope_runtime": _Runtime(keys), "install_dal": object()}
    key_pb2_grpc.add_KeyServiceServicer_to_server(KeyServicer(config), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(grace=None)


async def _call(address: str, token: str | None, client: X25519PrivateKey, tenant: str = "7"):  # type: ignore[no-untyped-def]
    async with grpc.aio.insecure_channel(address) as channel:
        stub = key_pb2_grpc.KeyServiceStub(channel)
        metadata = (("authorization", f"Bearer {token}"),) if token else ()
        return await stub.GetStreamDek(
            key_pb2.GetStreamDekRequest(
                tenant_id=tenant,
                purpose=PURPOSE,
                version=0,
                caller_ephemeral_public_key=pub(client),
            ),
            metadata=metadata,
            timeout=2.0,
        )


class TestGrpc:
    """Authn/authz at the wire."""

    async def test_scope_constant_is_the_contract(self) -> None:
        assert REQUIRED_SCOPES["/waddles.hub.internal.v1.KeyService/GetStreamDek"] == (
            "key:stream-dek:read"
        )

    async def test_valid_token_gets_a_sealed_key(
        self, address: str, issuer: ServiceJwtIssuer
    ) -> None:
        client = X25519PrivateKey.generate()
        resp = await _call(address, issuer.issue(INGEST, STREAM_DEK_SCOPE), client)
        assert resp.version == 1 and resp.ttl_seconds == 600
        key = unseal(resp.sealed_dek, client, seal_context("svc-ingest", 7, PURPOSE, 1))
        assert len(key) == 32

    async def test_missing_token_unauthenticated(self, address: str, db: FakeDb) -> None:
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await _call(address, None, X25519PrivateKey.generate())
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
        assert db.audit == []

    async def test_forged_token_unauthenticated(self, address: str) -> None:
        forged = _issuer(frozenset({STREAM_DEK_SCOPE})).issue(INGEST, STREAM_DEK_SCOPE)
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await _call(address, forged, X25519PrivateKey.generate())
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED

    async def test_wrong_scope_rejected(self, address: str, issuer: ServiceJwtIssuer) -> None:
        token = issuer.issue(INGEST, "identity:ephemeral:mint")
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await _call(address, token, X25519PrivateKey.generate())
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED

    async def test_wrong_tenant_denied(
        self, address: str, issuer: ServiceJwtIssuer, db: FakeDb
    ) -> None:
        issuer.identities[INGEST] = ServiceIdentity(
            service_id=INGEST,
            k8s_namespace="waddlebot",
            k8s_service_account="svc-ingest",
            allowed_scopes=frozenset({STREAM_DEK_SCOPE}),
            tenant=SLUG,
        )
        token = issuer.issue(INGEST, STREAM_DEK_SCOPE)
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await _call(address, token, X25519PrivateKey.generate(), tenant=str(OTHER_TENANT))
        assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
        assert db.audit[-1]["outcome"] == OUTCOME_DENIED_TENANT

    async def test_slug_tenant_invalid_argument(
        self, address: str, issuer: ServiceJwtIssuer
    ) -> None:
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await _call(
                address, issuer.issue(INGEST, STREAM_DEK_SCOPE), X25519PrivateKey.generate(), SLUG
            )
        assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    async def test_unwired_key_store_unavailable(self, issuer: ServiceJwtIssuer) -> None:
        server = grpc.aio.server(
            interceptors=[AuthInterceptor(issuer.as_verifier(), REQUIRED_SCOPES)]
        )
        key_pb2_grpc.add_KeyServiceServicer_to_server(KeyServicer({}), server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await _call(
                    f"127.0.0.1:{port}",
                    issuer.issue(INGEST, STREAM_DEK_SCOPE),
                    X25519PrivateKey.generate(),
                )
            assert exc.value.code() == grpc.StatusCode.UNAVAILABLE
        finally:
            await server.stop(grace=None)

    async def test_no_claims_unauthenticated(self) -> None:
        server = grpc.aio.server()
        key_pb2_grpc.add_KeyServiceServicer_to_server(KeyServicer({}), server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await _call(f"127.0.0.1:{port}", None, X25519PrivateKey.generate())
            assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
        finally:
            await server.stop(grace=None)
