"""`blueprints/v1/flags.py` -- resolved feature flags proxy (gh #hub-webui-s0).

Standalone-app / real-JWT / real-pydal pattern (`test_v1_platform_config_
blueprint.py`'s own precedent). `get_entitlement_client` is monkeypatched
per-test to a real `EntitlementClient` wired with a fake `FlagGate`/
`LicenseGate` -- this exercises the REAL two-gate evaluation/degradation
logic in `flask_core.entitlement` (same seam that module's own unit tests
use), never a hand-rolled stand-in for `evaluate()` itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from flask_core.entitlement import EntitlementClient
from pydal import DAL, Field
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.flags as flags_module
from blueprints.v1.flags import CLIENT_FLAG_KEYS, flags_bp
from tests.conftest import OTHER_TENANT_SLUG, TENANT_SLUG, make_token


class _FakeFlagGate:
    """Controllable `FlagGate` -- per-`distinct_id` canned answer, or an error signal."""

    def __init__(
        self, *, answers: Mapping[str, bool] | None = None, unreachable: bool = False
    ) -> None:
        self._answers = dict(answers or {})
        self._unreachable = unreachable

    def is_enabled(
        self,
        flag_key: str,
        distinct_id: str,
        *,
        groups: Mapping[str, str] | None = None,
    ) -> bool | None:
        if self._unreachable:
            return None  # PostHog unreachable/unresolvable
        return self._answers.get(distinct_id, False)


class _FakeLicenseGate:
    """Controllable `LicenseGate` -- fixed tier, or `None` via `unreachable`."""

    def __init__(self, *, tier: str = "enterprise", unreachable: bool = False) -> None:
        self._tier = tier
        self._unreachable = unreachable

    def resolve_tier(self) -> str:
        if self._unreachable:
            raise RuntimeError("license server unreachable")
        return self._tier


@pytest.fixture
def dal() -> Any:
    """In-memory pydal DB, `tenants` only -- all `tenant_middleware` needs."""
    db = DAL("sqlite:memory")
    db.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("is_active", "boolean", default=True),
    )
    db.tenants.insert(slug=TENANT_SLUG, is_active=True)
    db.tenants.insert(slug=OTHER_TENANT_SLUG, is_active=True)
    db.commit()
    yield db
    db.close()


@pytest.fixture
def app(dal: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(flags_bp)
    quart_app.config["dal"] = dal
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _headers(*, scope: str = "flags:read", tenant: str = TENANT_SLUG) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(scope=scope, tenant=tenant)}"}


def _install_client(monkeypatch: pytest.MonkeyPatch, flag_gate: Any, license_gate: Any) -> None:
    fake_client = EntitlementClient(flag_gate=flag_gate, license_gate=license_gate)
    monkeypatch.setattr(flags_module, "get_entitlement_client", lambda: fake_client)


class TestAuth:
    async def test_no_token_is_401(self, client: Any) -> None:
        response = await client.get("/api/v1/flags")
        assert response.status_code == 401

    async def test_wrong_scope_is_403(self, client: Any) -> None:
        response = await client.get("/api/v1/flags", headers=_headers(scope="other:write"))
        assert response.status_code == 403

    async def test_wildcard_read_scope_is_200(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_client(monkeypatch, _FakeFlagGate(unreachable=True), _FakeLicenseGate())
        response = await client.get("/api/v1/flags", headers=_headers(scope="*:read"))
        assert response.status_code == 200


class TestResolution:
    async def test_authed_returns_resolved_map(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answers = {TENANT_SLUG: True}
        _install_client(monkeypatch, _FakeFlagGate(answers=answers), _FakeLicenseGate())
        response = await client.get("/api/v1/flags", headers=_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert set(body.keys()) == {"flags"}  # exact DTO shape, no extra fields
        assert set(body["flags"].keys()) == set(CLIENT_FLAG_KEYS)
        assert all(value is True for value in body["flags"].values())

    async def test_tenant_isolation(self, client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        # Only TENANT_SLUG's distinct_id resolves true; OTHER_TENANT_SLUG's doesn't.
        answers = {TENANT_SLUG: True}
        _install_client(monkeypatch, _FakeFlagGate(answers=answers), _FakeLicenseGate())

        resp_a = await client.get("/api/v1/flags", headers=_headers(tenant=TENANT_SLUG))
        resp_b = await client.get("/api/v1/flags", headers=_headers(tenant=OTHER_TENANT_SLUG))
        body_a = await resp_a.get_json()
        body_b = await resp_b.get_json()

        assert all(v is True for v in body_a["flags"].values())
        assert all(v is False for v in body_b["flags"].values())

    async def test_posthog_unreachable_degrades_all_false_never_500(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_client(monkeypatch, _FakeFlagGate(unreachable=True), _FakeLicenseGate())
        response = await client.get("/api/v1/flags", headers=_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert set(body["flags"].keys()) == set(CLIENT_FLAG_KEYS)
        assert all(value is False for value in body["flags"].values())

    async def test_license_server_unreachable_degrades_all_false_never_500(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answers = {TENANT_SLUG: True}
        _install_client(
            monkeypatch, _FakeFlagGate(answers=answers), _FakeLicenseGate(unreachable=True)
        )
        response = await client.get("/api/v1/flags", headers=_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert all(value is False for value in body["flags"].values())

    async def test_no_extra_response_fields(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: response must go through the DTO, never a raw dict dump."""
        _install_client(monkeypatch, _FakeFlagGate(unreachable=True), _FakeLicenseGate())
        response = await client.get("/api/v1/flags", headers=_headers())
        body = await response.get_json()
        assert list(body.keys()) == ["flags"]
