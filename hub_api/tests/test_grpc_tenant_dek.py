"""``KeyService.GetTenantDek`` end to end: real gRPC server + chain, real Postgres key store.

Real: an in-process ``grpc.aio`` server built from ``build_interceptor_chain`` (deadline -> JWT auth
-> rate limit -> OTel), the real ``KeyServicer``, real Ed25519 machine JWTs, the real envelope
service/KEK wrap over a migrated Postgres 17, a real X25519/HKDF/AES-GCM client written
independently of the server code (the Rust ``hpke_seal_open`` algorithm). Faults injected: a KEK
whose KMS raises, and an audit sink that raises. TLS credential loading (``server.py``) is
deploy-time-verified, as for every other internal RPC test.
"""

from __future__ import annotations

import base64
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import grpc
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey, spiffe_id
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from waddles.hub.internal.v1 import key_pb2, key_pb2_grpc

from grpc_internal import servicers
from grpc_internal.server import build_interceptor_chain
from grpc_internal.servicers import REQUIRED_SCOPES, KeyServicer
from services.envelope.crypto import wrap_context
from services.schema import _define_audit_log
from tests.envelope.fakes import CountingPlatformKek
from tests.tenant_keys_pg import (
    KEK,
    DownKek,
    make_service,
    new_tenant,
    requires_docker,
    scalar,
)

pytestmark = requires_docker

SCOPE = "keys:tenant-dek:read"
PROCESS = spiffe_id("alpha", "svc-process")
ROGUE = spiffe_id("alpha", "svc-rogue")
PRESENTATION = spiffe_id("alpha", "svc-presentation")
TENANTED = spiffe_id("alpha", "svc-action")
AT_REST = key_pb2.TENANT_DEK_PURPOSE_AT_REST


def _issuer() -> ServiceJwtIssuer:
    priv = Ed25519PrivateKey.generate()
    ids = {
        PROCESS: ServiceIdentity(PROCESS, "waddlebot", "svc-process", frozenset({SCOPE}), "system"),
        ROGUE: ServiceIdentity(ROGUE, "waddlebot", "svc-rogue", frozenset({SCOPE}), "system"),
        PRESENTATION: ServiceIdentity(
            PRESENTATION,
            "waddlebot",
            "svc-presentation",
            frozenset({"identity:displayname:read"}),
            "system",
        ),
        TENANTED: ServiceIdentity(TENANTED, "waddlebot", "svc-action", frozenset({SCOPE}), "acme"),
    }
    return ServiceJwtIssuer(
        keys={"k1": SigningKey("k1", priv, priv.public_key())}, active_kid="k1", identities=ids
    )


@dataclass(slots=True)
class Harness:
    """A running server plus what tests need to call and inspect it."""

    address: str
    issuer: ServiceJwtIssuer
    db: Any
    tk_sql: Any

    def token(self, sid: str = PROCESS, scope: str = SCOPE) -> str:
        tok, _ = self.issuer.issue_with_claims(sid, scope)
        return str(tok)

    def audit(self) -> list[Any]:
        return list(self.db.dal(self.db.dal.audit_log.action == "keys.tenant_dek.release").select())


@pytest.fixture
def issuer() -> ServiceJwtIssuer:
    return _issuer()


async def _serve(
    issuer: ServiceJwtIssuer, db: Any, service: Any, sql_conn: Any
) -> AsyncIterator[Harness]:
    server = grpc.aio.server(interceptors=build_interceptor_chain(issuer))
    key_pb2_grpc.add_KeyServiceServicer_to_server(KeyServicer(db, service), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield Harness(f"127.0.0.1:{port}", issuer, db, sql_conn)
    finally:
        await server.stop(grace=None)


@pytest.fixture
async def harness(
    issuer: ServiceJwtIssuer, ai_routing_db: Any, tk_dal: Any, tk_sql: Any
) -> AsyncIterator[Harness]:
    _define_audit_log(ai_routing_db.dal, migrate=True)
    ai_routing_db.dal.commit()
    async for h in _serve(issuer, ai_routing_db, make_service(tk_dal), tk_sql):
        yield h


def _request(
    tenant_id: int,
    *,
    purpose: int = AT_REST,
    dek_version: int = 0,
    ephemeral: X25519PrivateKey | None = None,
    pub: bytes | None = None,
) -> tuple[key_pb2.GetTenantDekRequest, X25519PrivateKey]:
    secret = ephemeral or X25519PrivateKey.generate()
    public = (
        pub if pub is not None else secret.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    return (
        key_pb2.GetTenantDekRequest(
            tenant_id=tenant_id,
            purpose=purpose,
            dek_version=dek_version,
            caller_ephemeral_public_key=public,
        ),
        secret,
    )


async def _call(
    h: Harness, request: key_pb2.GetTenantDekRequest, *, token: str | None = None
) -> key_pb2.GetTenantDekResponse:
    async with grpc.aio.insecure_channel(h.address) as channel:
        stub = key_pb2_grpc.KeyServiceStub(channel)
        metadata = (("authorization", f"Bearer {token or h.token()}"),)
        return await stub.GetTenantDek(request, metadata=metadata, timeout=3.0)  # type: ignore[no-any-return]


def open_sealed(
    secret: X25519PrivateKey, response: key_pb2.GetTenantDekResponse, context: str
) -> bytes:
    """Independent client: the Rust ``hpke_seal_open`` algorithm."""
    shared = secret.exchange(X25519PublicKey.from_public_bytes(response.enc))
    material = HKDF(hashes.SHA256(), 44, None, context.encode()).derive(shared)
    return AESGCM(material[:32]).decrypt(material[32:], response.ciphertext, context.encode())


async def true_dek(sql_conn: Any, tenant_id: int, version: int | None = None) -> bytes:
    """The ground-truth DEK: unwrap the stored row with the platform KEK directly."""
    select = "SELECT wrapped_dek FROM keystore.tenant_encryption_keys WHERE tenant_id = %s"
    if version:
        wrapped = scalar(sql_conn, select + " AND dek_version = %s", (tenant_id, version))
    else:
        wrapped = scalar(sql_conn, select + " AND status = 'active'", (tenant_id,))
    return await CountingPlatformKek(KEK).unwrap(
        bytes(wrapped),  # type: ignore[arg-type]
        context=wrap_context(tenant_id),
    )


async def _provision(tk_dal: Any, sql_conn: Any) -> int:
    tenant_id = new_tenant(sql_conn)
    await make_service(tk_dal).ensure_tenant_key(tenant_id)
    return tenant_id


class TestRelease:
    async def test_releases_the_exact_dek_sealed_to_the_caller(
        self, harness, tk_dal, tk_sql
    ) -> None:  # type: ignore[no-untyped-def]
        tenant_id = await _provision(tk_dal, tk_sql)
        request, secret = _request(tenant_id)
        response = await _call(harness, request)
        dek = open_sealed(secret, response, f"svc-process|{tenant_id}|at-rest|1")
        assert dek == await true_dek(tk_sql, tenant_id)
        assert response.dek_version == 1 and 30 <= response.ttl_seconds <= 600

    async def test_response_field_set_is_exact(self, harness, tk_dal, tk_sql) -> None:  # type: ignore[no-untyped-def]
        tenant_id = await _provision(tk_dal, tk_sql)
        request, _ = _request(tenant_id)
        response = await _call(harness, request)
        assert {f.name for f in response.DESCRIPTOR.fields} == {
            "enc",
            "ciphertext",
            "dek_version",
            "ttl_seconds",
        }
        assert {f.name for f, _ in response.ListFields()} == {
            "enc",
            "ciphertext",
            "dek_version",
            "ttl_seconds",
        }
        assert len(response.enc) == 32 and len(response.ciphertext) == 48  # DEK 32 + GCM tag 16

    async def test_seal_is_bound_to_service_tenant_and_purpose(
        self, harness, tk_dal, tk_sql
    ) -> None:  # type: ignore[no-untyped-def]
        tenant_id = await _provision(tk_dal, tk_sql)
        request, secret = _request(tenant_id, purpose=key_pb2.TENANT_DEK_PURPOSE_INGEST_STREAM)
        response = await _call(harness, request)
        open_sealed(secret, response, f"svc-process|{tenant_id}|ingest-stream|1")
        for wrong in (
            f"svc-action|{tenant_id}|ingest-stream|1",
            f"svc-process|{tenant_id + 1}|ingest-stream|1",
            f"svc-process|{tenant_id}|at-rest|1",
        ):
            with pytest.raises(Exception):  # noqa: B017 - cryptography InvalidTag
                open_sealed(secret, response, wrong)

    async def test_every_response_uses_a_fresh_server_key(self, harness, tk_dal, tk_sql) -> None:  # type: ignore[no-untyped-def]
        tenant_id = await _provision(tk_dal, tk_sql)
        request, _ = _request(tenant_id)
        first = await _call(harness, request)
        second = await _call(harness, request)
        assert first.enc != second.enc and first.ciphertext != second.ciphertext

    async def test_retired_version_stays_readable_and_active_moves(  # type: ignore[no-untyped-def]
        self, harness, tk_dal, tk_sql
    ) -> None:
        tenant_id = await _provision(tk_dal, tk_sql)
        old = await true_dek(tk_sql, tenant_id, 1)
        await make_service(tk_dal).rotate_dek(tenant_id)
        request, secret = _request(tenant_id)
        active = await _call(harness, request)
        assert active.dek_version == 2
        assert open_sealed(secret, active, f"svc-process|{tenant_id}|at-rest|1") == await true_dek(
            tk_sql, tenant_id
        )
        request, secret = _request(tenant_id, dek_version=1)
        retired = await _call(harness, request)
        assert retired.dek_version == 1
        assert open_sealed(secret, retired, f"svc-process|{tenant_id}|at-rest|1") == old


class TestRefusals:
    async def _code(self, h: Harness, request: Any, token: str | None = None) -> grpc.StatusCode:
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await _call(h, request, token=token)
        return exc.value.code()

    async def test_wrong_scope_is_unauthenticated(self, harness, tk_dal, tk_sql) -> None:  # type: ignore[no-untyped-def]
        tenant_id = await _provision(tk_dal, tk_sql)
        request, _ = _request(tenant_id)
        token = harness.token(PRESENTATION, "identity:displayname:read")
        assert await self._code(harness, request, token) is grpc.StatusCode.UNAUTHENTICATED

    async def test_missing_or_garbage_token_is_unauthenticated(
        self, harness, tk_dal, tk_sql
    ) -> None:  # type: ignore[no-untyped-def]
        tenant_id = await _provision(tk_dal, tk_sql)
        request, _ = _request(tenant_id)
        assert await self._code(harness, request, "not-a-jwt") is grpc.StatusCode.UNAUTHENTICATED

    async def test_valid_scope_but_not_allowlisted_service_is_denied(  # type: ignore[no-untyped-def]
        self, harness, tk_dal, tk_sql
    ) -> None:
        tenant_id = await _provision(tk_dal, tk_sql)
        request, _ = _request(tenant_id)
        assert (
            await self._code(harness, request, harness.token(ROGUE))
            is grpc.StatusCode.PERMISSION_DENIED
        )
        assert harness.audit() == []  # a refused caller never reaches the key path

    async def test_non_system_tenant_token_is_denied(self, harness, tk_dal, tk_sql) -> None:  # type: ignore[no-untyped-def]
        tenant_id = await _provision(tk_dal, tk_sql)
        request, _ = _request(tenant_id)
        assert (
            await self._code(harness, request, harness.token(TENANTED))
            is grpc.StatusCode.PERMISSION_DENIED
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"purpose": 0},
            {"pub": b"short"},
            {"pub": bytes(32)},  # low-order point -> all-zero shared secret
        ],
    )
    async def test_malformed_requests_are_invalid_argument(  # type: ignore[no-untyped-def]
        self, harness, tk_dal, tk_sql, kwargs
    ) -> None:
        tenant_id = await _provision(tk_dal, tk_sql)
        request, _ = _request(tenant_id, **kwargs)
        assert await self._code(harness, request) is grpc.StatusCode.INVALID_ARGUMENT

    async def test_tenant_id_zero_is_invalid_argument(self, harness) -> None:  # type: ignore[no-untyped-def]
        request, _ = _request(0)
        assert await self._code(harness, request) is grpc.StatusCode.INVALID_ARGUMENT

    async def test_tenant_without_a_key_is_not_found_and_never_provisioned(  # type: ignore[no-untyped-def]
        self, harness, tk_sql
    ) -> None:
        tenant_id = new_tenant(tk_sql)  # tenant row, no key
        request, _ = _request(tenant_id)
        assert await self._code(harness, request) is grpc.StatusCode.NOT_FOUND
        assert (
            scalar(
                tk_sql,
                "SELECT count(*) FROM keystore.tenant_encryption_keys WHERE tenant_id = %s",
                (tenant_id,),
            )
            == 0
        )

    async def test_unknown_retired_version_is_not_found(self, harness, tk_dal, tk_sql) -> None:  # type: ignore[no-untyped-def]
        tenant_id = await _provision(tk_dal, tk_sql)
        request, _ = _request(tenant_id, dek_version=9)
        assert await self._code(harness, request) is grpc.StatusCode.NOT_FOUND

    async def test_kms_outage_is_unavailable_with_no_fallback_key(  # type: ignore[no-untyped-def]
        self, issuer, ai_routing_db, tk_dal, tk_sql
    ) -> None:
        _define_audit_log(ai_routing_db.dal, migrate=True)
        ai_routing_db.dal.commit()
        tenant_id = await _provision(tk_dal, tk_sql)
        down = make_service(tk_dal, DownKek(KEK))
        async for h in _serve(issuer, ai_routing_db, down, tk_sql):
            request, _ = _request(tenant_id)
            assert await self._code(h, request) is grpc.StatusCode.UNAVAILABLE
            outcomes = [row.details["outcome"] for row in h.audit()]
            assert outcomes == ["unavailable:kms_unavailable"]

    async def test_audit_failure_withholds_the_key(  # type: ignore[no-untyped-def]
        self, issuer, ai_routing_db, tk_dal, tk_sql
    ) -> None:
        class BrokenAudit:
            dal = ai_routing_db.dal

            async def insert_async(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("audit sink down")

        tenant_id = await _provision(tk_dal, tk_sql)
        async for h in _serve(issuer, BrokenAudit(), make_service(tk_dal), tk_sql):
            request, _ = _request(tenant_id)
            assert await self._code(h, request) is grpc.StatusCode.INTERNAL

    async def test_rate_limit_is_enforced_per_caller(  # type: ignore[no-untyped-def]
        self, monkeypatch, issuer, ai_routing_db, tk_dal, tk_sql
    ) -> None:
        monkeypatch.setenv("GRPC_TENANT_DEK_BURST", "2")
        monkeypatch.setenv("GRPC_TENANT_DEK_RPS", "0.0001")
        _define_audit_log(ai_routing_db.dal, migrate=True)
        ai_routing_db.dal.commit()
        tenant_id = await _provision(tk_dal, tk_sql)
        async for h in _serve(issuer, ai_routing_db, make_service(tk_dal), tk_sql):
            request, _ = _request(tenant_id)
            await _call(h, request)
            await _call(h, request)
            assert await self._code(h, request) is grpc.StatusCode.RESOURCE_EXHAUSTED


class TestSvidBinding:
    class _Ctx:
        def __init__(self, auth: dict[str, list[bytes]]) -> None:
            self._auth = auth
            self.aborted: grpc.StatusCode | None = None

        def auth_context(self) -> dict[str, list[bytes]]:
            return self._auth

        async def abort(self, code: grpc.StatusCode, detail: str) -> None:
            self.aborted = code
            raise grpc.aio.AbortError()

    async def test_client_cert_must_match_the_jwt_subject(self) -> None:
        servicer = KeyServicer()
        claims = {"sub": PROCESS, "tenant": "system"}
        mismatch = self._Ctx(
            {"x509_pem_cert": [b"pem"], "x509_subject_alternative_name": [ROGUE.encode()]}
        )
        with pytest.raises(grpc.aio.AbortError):
            await servicer._authorize_caller(claims, mismatch, 0.0)  # type: ignore[arg-type]
        assert mismatch.aborted is grpc.StatusCode.PERMISSION_DENIED
        match = self._Ctx(
            {"x509_pem_cert": [b"pem"], "x509_subject_alternative_name": [PROCESS.encode()]}
        )
        assert await servicer._authorize_caller(claims, match, 0.0) == "svc-process"  # type: ignore[arg-type]

    async def test_no_client_cert_falls_back_to_jwt_alone(self) -> None:
        servicer = KeyServicer()
        ctx = self._Ctx({})  # SPIRE not deployed: TLS without client auth
        claims = {"sub": PROCESS, "tenant": "system"}
        assert await servicer._authorize_caller(claims, ctx, 0.0) == "svc-process"  # type: ignore[arg-type]


class TestSecretsAndTelemetry:
    async def test_no_key_material_in_logs_or_audit(  # type: ignore[no-untyped-def]
        self, harness, tk_dal, tk_sql, caplog
    ) -> None:
        tenant_id = await _provision(tk_dal, tk_sql)
        caplog.set_level(logging.DEBUG)
        request, secret = _request(tenant_id)
        response = await _call(harness, request)
        dek = await true_dek(tk_sql, tenant_id)
        needles = {
            dek.hex(),
            base64.b64encode(dek).decode(),
            base64.urlsafe_b64encode(dek).decode(),
            base64.b64encode(response.enc).decode(),
            base64.b64encode(response.ciphertext).decode(),
            response.ciphertext.hex(),
            base64.b64encode(request.caller_ephemeral_public_key).decode(),
            repr(dek),
            repr(response.ciphertext),
        }
        assert caplog.records, "the broker must log (zero records proves nothing)"
        haystack = "\n".join(f"{r.getMessage()} {r.__dict__!r}" for r in caplog.records)
        haystack += "\n" + repr([row.details for row in harness.audit()])
        for needle in needles:
            assert needle not in haystack
        assert [row.details["outcome"] for row in harness.audit()] == ["released"]

    async def test_emits_metric_data_points_and_histogram(  # type: ignore[no-untyped-def]
        self, monkeypatch, harness, tk_dal, tk_sql
    ) -> None:
        reader = InMemoryMetricReader()
        meter = MeterProvider(metric_readers=[reader]).get_meter("test")
        monkeypatch.setattr(
            servicers,
            "_dek_requests",
            meter.create_counter("waddles_tenant_dek_broker_requests_total"),
        )
        monkeypatch.setattr(
            servicers,
            "_dek_duration",
            meter.create_histogram("waddles_tenant_dek_broker_duration_seconds", unit="s"),
        )
        tenant_id = await _provision(tk_dal, tk_sql)
        request, _ = _request(tenant_id)
        await _call(harness, request)
        data = reader.get_metrics_data()
        assert data is not None
        points = {
            m.name: list(m.data.data_points)
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
        }
        counter = points["waddles_tenant_dek_broker_requests_total"]
        assert sum(p.value for p in counter) >= 1
        assert {p.attributes["result"] for p in counter} == {"ok"}
        hist = points["waddles_tenant_dek_broker_duration_seconds"]
        assert sum(p.count for p in hist) >= 1


def test_required_scope_registered_for_the_rpc() -> None:
    assert REQUIRED_SCOPES["/waddles.hub.internal.v1.KeyService/GetTenantDek"] == SCOPE
    assert os.environ.get("KEYS_BROKER_ALLOWED_SERVICES") is None  # defaults under test
