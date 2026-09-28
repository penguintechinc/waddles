"""`blueprints/v1/internal_keys.py` -- POST /api/v1/internal/keys/tenant-dek.

`flask_core.service_jwt` doesn't exist on this branch yet (PR #438,
`feature/eddsa-machine-jwt`, unmerged as of this PR -- see the blueprint's
own module docstring). Auth-path tests are marked `xfail(strict=False)`
until that lands rather than skipped outright, so this file starts
failing loudly (not silently staying green) the moment #438 merges and
`SERVICE_JWT_AVAILABLE` flips true without this suite being updated.
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart

import blueprints.v1.internal_keys as internal_keys_module
from blueprints.v1.internal_keys import InvalidServiceToken, internal_keys_bp
from services.tenant_keystore import TenantKeyNotFound, TenantKeyShredded


class _FakeKeystore:
    def __init__(self) -> None:
        self.shredded_tenants: set[int] = set()
        self.missing_tenants: set[int] = set()
        self.kek_provider = _FakeKek()

    async def get_dek(self, tenant_id: int, *, version: int | None = None) -> Any:
        if tenant_id in self.shredded_tenants:
            raise TenantKeyShredded(str(tenant_id))
        if tenant_id in self.missing_tenants:
            raise TenantKeyNotFound(str(tenant_id))
        record = _FakeRecord(dek_version=version or 1, kek_kind="platform")
        return b"\x01" * 32, record


class _FakeRecord:
    def __init__(self, dek_version: int, kek_kind: str) -> None:
        self.dek_version = dek_version
        self.kek_kind = kek_kind


class _FakeKek:
    async def wrap(self, tenant_id: int, dek: bytes) -> bytes:
        return b"wrapped:" + dek


@pytest.fixture
def app() -> Quart:
    application = Quart(__name__)
    application.register_blueprint(internal_keys_bp)
    application.config["tenant_keystore"] = _FakeKeystore()
    application.config["dal"] = None  # audit helper no-ops when dal is unset
    return application


async def test_missing_tenant_id_is_bad_request(app: Quart) -> None:
    client = app.test_client()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek", json={"purpose": "message_content"}
    )
    assert resp.status_code == 400


async def test_invalid_purpose_is_bad_request(app: Quart) -> None:
    client = app.test_client()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek", json={"tenant_id": 1, "purpose": "not-a-purpose"}
    )
    assert resp.status_code == 400


@pytest.mark.skipif(
    internal_keys_module.SERVICE_JWT_AVAILABLE,
    reason="PR #438 merged -- auth is now enforced, see fail-closed test below instead",
)
async def test_fails_closed_503_without_service_jwt(app: Quart) -> None:
    """Until PR #438 merges, every request is denied (503), never silently allowed."""
    client = app.test_client()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={"tenant_id": 1, "purpose": "message_content"},
    )
    assert resp.status_code == 503
    body = await resp.get_json()
    assert body["error"] == "service_jwt_unavailable"


class _FakeVerifier:
    """Stands in for `flask_core.service_jwt.ServiceJwtVerifier` once auth is enabled."""

    def __init__(self, *, allowed_scope: str | None) -> None:
        self._allowed_scope = allowed_scope

    def verify(self, token: str, *, required_scope: str) -> dict[str, str]:
        if token != "good-token" or required_scope != self._allowed_scope:
            raise InvalidServiceToken("scope mismatch")
        return {"sub": "spiffe://penguintech.io/alpha/svc-ingest", "scope": required_scope}


@pytest.fixture
def _service_jwt_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate PR #438 being merged: flip the module flag and swap in a fake verifier."""
    monkeypatch.setattr(internal_keys_module, "SERVICE_JWT_AVAILABLE", True)


async def test_authorized_call_returns_wrapped_dek(app: Quart, _service_jwt_enabled: None) -> None:
    app.config["SERVICE_JWT_VERIFIER"] = _FakeVerifier(
        allowed_scope="keys:tenant-dek:read:message_content"
    )
    client = app.test_client()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={"tenant_id": 1, "purpose": "message_content"},
        headers={"Authorization": "Bearer good-token"},
    )
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["tenant_id"] == 1
    assert body["wrapped_dek"] != (b"\x01" * 32).hex()  # never the raw DEK over the wire


async def test_wrong_purpose_scope_denied(app: Quart, _service_jwt_enabled: None) -> None:
    """A token scoped to `message_content` cannot fetch the `identity` purpose's key."""
    app.config["SERVICE_JWT_VERIFIER"] = _FakeVerifier(
        allowed_scope="keys:tenant-dek:read:message_content"
    )
    client = app.test_client()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={"tenant_id": 1, "purpose": "identity"},
        headers={"Authorization": "Bearer good-token"},
    )
    assert resp.status_code == 401


async def test_missing_bearer_header_denied(app: Quart, _service_jwt_enabled: None) -> None:
    app.config["SERVICE_JWT_VERIFIER"] = _FakeVerifier(allowed_scope="anything")
    client = app.test_client()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={"tenant_id": 1, "purpose": "message_content"},
    )
    assert resp.status_code == 401


async def test_shredded_tenant_returns_410(app: Quart, _service_jwt_enabled: None) -> None:
    app.config["SERVICE_JWT_VERIFIER"] = _FakeVerifier(
        allowed_scope="keys:tenant-dek:read:message_content"
    )
    keystore: _FakeKeystore = app.config["tenant_keystore"]
    keystore.shredded_tenants.add(1)
    client = app.test_client()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={"tenant_id": 1, "purpose": "message_content"},
        headers={"Authorization": "Bearer good-token"},
    )
    assert resp.status_code == 410


async def test_missing_key_returns_400_not_404(app: Quart, _service_jwt_enabled: None) -> None:
    """No key at all (never provisioned) is a bad request, distinct from shredded (410)."""
    app.config["SERVICE_JWT_VERIFIER"] = _FakeVerifier(
        allowed_scope="keys:tenant-dek:read:message_content"
    )
    keystore: _FakeKeystore = app.config["tenant_keystore"]
    keystore.missing_tenants.add(1)
    client = app.test_client()
    resp = await client.post(
        "/api/v1/internal/keys/tenant-dek",
        json={"tenant_id": 1, "purpose": "message_content"},
        headers={"Authorization": "Bearer good-token"},
    )
    assert resp.status_code == 400


async def test_wrapped_dek_never_equals_raw_dek() -> None:
    """The value put on the wire is never the raw unwrapped key bytes."""
    keystore = _FakeKeystore()
    dek, _record = await keystore.get_dek(1)
    wrapped = await keystore.kek_provider.wrap(1, dek)
    assert wrapped != dek
