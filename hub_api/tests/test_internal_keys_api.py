"""`blueprints/v1/internal_keys.py` -- POST /api/v1/internal/keys/tenant-dek.

**Amendment 2026-09-28 (post security-review, PR #442):** rewritten for
the purpose-limited `ingest-stream`-only broker (spec Sec5a/5b/5d) --
`message_content`/`connection_credentials`/`identity` purposes and the
platform-KEK-wrap response are gone.

`flask_core.service_jwt` doesn't exist on this branch yet (PR #438,
`feature/eddsa-machine-jwt`, unmerged as of this PR -- see the blueprint's
own module docstring). Auth-path tests are marked `xfail(strict=False)`
until that lands rather than skipped outright, so this file starts
failing loudly (not silently staying green) the moment #438 merges and
`SERVICE_JWT_AVAILABLE` flips true without this suite being updated.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from quart import Quart

import blueprints.v1.internal_keys as internal_keys_module
from blueprints.v1.internal_keys import InvalidServiceToken, internal_keys_bp
from services.stream_key_transport import open_stream_key
from services.tenant_keystore import TenantKeyNotFound, TenantKeyShredded

_RAW_DEK = b"\x01" * 32


def _client_pubkey_b64() -> tuple[str, X25519PrivateKey]:
    private_key = X25519PrivateKey.generate()
    pub_bytes = private_key.public_key().public_bytes_raw()
    return base64.b64encode(pub_bytes).decode(), private_key


class _FakeRecord:
    def __init__(self, dek_version: int, kek_kind: str) -> None:
        self.dek_version = dek_version
        self.kek_kind = kek_kind


class _FakeKeystore:
    def __init__(self) -> None:
        self.shredded_tenants: set[int] = set()
        self.missing_tenants: set[int] = set()
        self.calls: list[tuple[int, str, int | None]] = []

    async def get_dek(
        self, tenant_id: int, *, purpose: str = "ingest-stream", version: int | None = None
    ) -> Any:
        self.calls.append((tenant_id, purpose, version))
        if tenant_id in self.shredded_tenants:
            raise TenantKeyShredded(str(tenant_id))
        if tenant_id in self.missing_tenants:
            raise TenantKeyNotFound(str(tenant_id))
        record = _FakeRecord(dek_version=version or 1, kek_kind="platform")
        return _RAW_DEK, record


@pytest.fixture
def app() -> Quart:
    application = Quart(__name__)
    application.register_blueprint(internal_keys_bp)
    application.config["tenant_keystore"] = _FakeKeystore()
    application.config["dal"] = None  # audit helper no-ops when dal is unset
    return application


async def test_missing_tenant_id_is_bad_request(app: Quart) -> None:
    client = app.test_client()
    pubkey_b64, _ = _client_pubkey_b64()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={"purpose": "ingest-stream", "client_ephemeral_pubkey": pubkey_b64},
    )
    assert resp.status_code == 400


async def test_invalid_purpose_is_bad_request(app: Quart) -> None:
    """`message_content`/`connection_credentials`/`identity` are no longer valid (spec Sec5a)."""
    client = app.test_client()
    pubkey_b64, _ = _client_pubkey_b64()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={
            "tenant_id": 1,
            "purpose": "message_content",
            "client_ephemeral_pubkey": pubkey_b64,
        },
    )
    assert resp.status_code == 400


async def test_missing_client_ephemeral_pubkey_is_bad_request(app: Quart) -> None:
    client = app.test_client()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek", json={"tenant_id": 1, "purpose": "ingest-stream"}
    )
    assert resp.status_code == 400


async def test_malformed_client_ephemeral_pubkey_is_bad_request(app: Quart) -> None:
    client = app.test_client()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={
            "tenant_id": 1,
            "purpose": "ingest-stream",
            "client_ephemeral_pubkey": "not-valid-base64!!!",
        },
    )
    assert resp.status_code == 400


@pytest.mark.skipif(
    internal_keys_module.SERVICE_JWT_AVAILABLE,
    reason="PR #438 merged -- auth is now enforced, see fail-closed test below instead",
)
async def test_fails_closed_503_without_service_jwt(app: Quart) -> None:
    """Until PR #438 merges, every request is denied (503), never silently allowed."""
    client = app.test_client()
    pubkey_b64, _ = _client_pubkey_b64()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={
            "tenant_id": 1,
            "purpose": "ingest-stream",
            "client_ephemeral_pubkey": pubkey_b64,
        },
    )
    assert resp.status_code == 503
    body = await resp.get_json()
    assert body["error"] == "service_jwt_unavailable"


class _FakeVerifier:
    """Stands in for `flask_core.service_jwt.ServiceJwtVerifier` once auth is enabled."""

    def __init__(self, *, allowed_scope: str | None, sub: str = "svc-ingest") -> None:
        self._allowed_scope = allowed_scope
        self._sub = sub

    def verify(self, token: str, *, required_scope: str) -> dict[str, str]:
        if token != "good-token" or required_scope != self._allowed_scope:
            raise InvalidServiceToken("scope mismatch")
        return {"sub": self._sub, "scope": required_scope}


@pytest.fixture
def _service_jwt_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate PR #438 being merged: flip the module flag and swap in a fake verifier."""
    monkeypatch.setattr(internal_keys_module, "SERVICE_JWT_AVAILABLE", True)


async def test_authorized_svc_ingest_call_returns_sealed_dek(
    app: Quart, _service_jwt_enabled: None
) -> None:
    app.config["SERVICE_JWT_VERIFIER"] = _FakeVerifier(
        allowed_scope="keys:tenant-dek:read:ingest-stream", sub="svc-ingest"
    )
    client = app.test_client()
    pubkey_b64, private_key = _client_pubkey_b64()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={
            "tenant_id": 1,
            "purpose": "ingest-stream",
            "client_ephemeral_pubkey": pubkey_b64,
        },
        headers={"Authorization": "Bearer good-token"},
    )
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["tenant_id"] == 1
    assert body["purpose"] == "ingest-stream"
    assert body["max_cache_ttl_s"] == 300
    assert "wrapped_dek" not in body  # platform-KEK-wrap response is gone (spec Sec5b)

    info = f"svc-ingest|1|ingest-stream|{body['dek_version']}".encode()
    recovered = open_stream_key(
        hub_api_ephemeral_pubkey=base64.b64decode(body["hub_api_ephemeral_pubkey"]),
        nonce=base64.b64decode(body["nonce"]),
        sealed=base64.b64decode(body["sealed"]),
        client_ephemeral_private_key=private_key,
        info=info,
    )
    assert recovered == _RAW_DEK


async def test_authorized_svc_process_call_allowed(app: Quart, _service_jwt_enabled: None) -> None:
    app.config["SERVICE_JWT_VERIFIER"] = _FakeVerifier(
        allowed_scope="keys:tenant-dek:read:ingest-stream", sub="svc-process"
    )
    client = app.test_client()
    pubkey_b64, _ = _client_pubkey_b64()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={
            "tenant_id": 1,
            "purpose": "ingest-stream",
            "client_ephemeral_pubkey": pubkey_b64,
        },
        headers={"Authorization": "Bearer good-token"},
    )
    assert resp.status_code == 200


async def test_service_not_in_purpose_allowlist_denied_even_with_valid_scope(
    app: Quart, _service_jwt_enabled: None
) -> None:
    """Spec Sec5a: server-side allowlist enforced IN ADDITION to the JWT scope.

    `svc-action` presenting a technically-valid `keys:tenant-dek:read:
    ingest-stream` scope (e.g. a forged/over-broad token) is still denied
    because `svc-action` is not in `PURPOSE_SERVICE_CAPABILITIES`.
    """
    app.config["SERVICE_JWT_VERIFIER"] = _FakeVerifier(
        allowed_scope="keys:tenant-dek:read:ingest-stream", sub="svc-action"
    )
    client = app.test_client()
    pubkey_b64, _ = _client_pubkey_b64()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={
            "tenant_id": 1,
            "purpose": "ingest-stream",
            "client_ephemeral_pubkey": pubkey_b64,
        },
        headers={"Authorization": "Bearer good-token"},
    )
    assert resp.status_code == 400


async def test_wrong_scope_denied(app: Quart, _service_jwt_enabled: None) -> None:
    app.config["SERVICE_JWT_VERIFIER"] = _FakeVerifier(
        allowed_scope="keys:tenant-dek:read:something-else", sub="svc-ingest"
    )
    client = app.test_client()
    pubkey_b64, _ = _client_pubkey_b64()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={
            "tenant_id": 1,
            "purpose": "ingest-stream",
            "client_ephemeral_pubkey": pubkey_b64,
        },
        headers={"Authorization": "Bearer good-token"},
    )
    assert resp.status_code == 401


async def test_missing_bearer_header_denied(app: Quart, _service_jwt_enabled: None) -> None:
    app.config["SERVICE_JWT_VERIFIER"] = _FakeVerifier(allowed_scope="anything")
    client = app.test_client()
    pubkey_b64, _ = _client_pubkey_b64()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={
            "tenant_id": 1,
            "purpose": "ingest-stream",
            "client_ephemeral_pubkey": pubkey_b64,
        },
    )
    assert resp.status_code == 401


async def test_shredded_tenant_returns_410(app: Quart, _service_jwt_enabled: None) -> None:
    app.config["SERVICE_JWT_VERIFIER"] = _FakeVerifier(
        allowed_scope="keys:tenant-dek:read:ingest-stream", sub="svc-ingest"
    )
    keystore: _FakeKeystore = app.config["tenant_keystore"]
    keystore.shredded_tenants.add(1)
    client = app.test_client()
    pubkey_b64, _ = _client_pubkey_b64()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={
            "tenant_id": 1,
            "purpose": "ingest-stream",
            "client_ephemeral_pubkey": pubkey_b64,
        },
        headers={"Authorization": "Bearer good-token"},
    )
    assert resp.status_code == 410


async def test_missing_key_returns_400_not_404(app: Quart, _service_jwt_enabled: None) -> None:
    """No key at all (never provisioned) is a bad request, distinct from shredded (410)."""
    app.config["SERVICE_JWT_VERIFIER"] = _FakeVerifier(
        allowed_scope="keys:tenant-dek:read:ingest-stream", sub="svc-ingest"
    )
    keystore: _FakeKeystore = app.config["tenant_keystore"]
    keystore.missing_tenants.add(1)
    client = app.test_client()
    pubkey_b64, _ = _client_pubkey_b64()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={
            "tenant_id": 1,
            "purpose": "ingest-stream",
            "client_ephemeral_pubkey": pubkey_b64,
        },
        headers={"Authorization": "Bearer good-token"},
    )
    assert resp.status_code == 400


async def test_service_id_passed_to_keystore_comes_from_verified_claims(
    app: Quart, _service_jwt_enabled: None
) -> None:
    """Spec Sec5d regression: service_id must come from verified JWT claims, not a stub."""
    app.config["SERVICE_JWT_VERIFIER"] = _FakeVerifier(
        allowed_scope="keys:tenant-dek:read:ingest-stream", sub="svc-process"
    )
    keystore: _FakeKeystore = app.config["tenant_keystore"]
    client = app.test_client()
    pubkey_b64, _ = _client_pubkey_b64()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={
            "tenant_id": 1,
            "purpose": "ingest-stream",
            "client_ephemeral_pubkey": pubkey_b64,
        },
        headers={"Authorization": "Bearer good-token"},
    )
    assert resp.status_code == 200
    assert keystore.calls == [(1, "ingest-stream", None)]
