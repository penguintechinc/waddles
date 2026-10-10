"""Professional-tier whitelabel gate -- `services/branding_service.py` + its two call sites.

Call sites: `GET /api/v1/auth/tenant/<slug>` (APPLIES branding -- the login
page's data source) and `PUT /api/v1/tenant/<slug>` (WRITES it). A tenant
that is not entitled to `tenancy.whitelabel` gets DEFAULT branding on read
and a 402 when trying to SET custom branding; entitlement is the real
two-gate (PostHog flag AND license tier) and fails closed.

The gate is exercised two ways: a mocked `feature_enabled` for call-site
behavior, and the REAL `EntitlementClient` over fake gates
(`entitlement_fakes`) for tier resolution and outage fail-closed.

One `AsyncDAL` per module with every non-`tenants` table emptied per test.

Fail-first proofs (executed, not narrated -- each mutation applied
in-process, the named test re-run, then dropped):
- `test_free_tenant_is_served_default_branding_not_stored_custom` --
  `resolve_login_branding()` serving sanitized custom branding without the
  gate: red (custom logo served to a Free tenant).
- `test_free_tenant_cannot_set_a_logo` -- `sets_branding()` always `False`
  (write gate never fires): red (200 instead of 402).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from flask_core.database import AsyncDAL
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.auth import auth_bp
from blueprints.v1.tenant import tenant_bp
from services import branding_service as branding
from services.branding_service import LoginBranding
from services.schema import bind_auth_tables, bind_tenant_tables
from tests.conftest import TENANT_SLUG, make_user_token
from tests.entitlement_fakes import FakeFlagGate, FakeLicenseGate, install_entitlement_client

FLAG = "waddles.tenancy.whitelabel"
STORED_LOGO = "https://cdn.acme.example/logo.png"
STORED_CONFIG = {"theme": "midnight", "welcomeMessage": "Welcome, Acme folks", "locale": "en"}


def _define_tenants(dal: Any, *, migrate: bool) -> None:
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
        migrate=migrate,
    )


@pytest.fixture(scope="module")
def module_db(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Schema built ONCE per module (defining ~100 tables costs seconds); see `db` for reset."""
    path = tmp_path_factory.mktemp("branding") / "branding.db"
    async_dal = AsyncDAL(f"sqlite://{path}", pool_size=1)
    dal = async_dal.dal
    _define_tenants(dal, migrate=True)
    bind_auth_tables(dal, migrate=True)
    bind_tenant_tables(dal, migrate=True)
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


@pytest.fixture
def db(module_db: Any) -> Any:
    """Every non-`tenants` table emptied and the tenant reset to STORED custom branding."""
    dal = module_db.dal
    for table_name in dal.tables:
        if table_name != "tenants":
            dal(dal[table_name]).delete()
    dal(dal.tenants.slug == TENANT_SLUG).update(
        display_name="Acme Corp", is_active=True, logo_url=STORED_LOGO, config=STORED_CONFIG
    )
    dal.commit()
    return module_db


@pytest.fixture
def client(db: Any) -> Any:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(auth_bp)
    quart_app.register_blueprint(tenant_bp)
    quart_app.config["dal"] = db.dal
    quart_app.config["async_dal"] = db
    return quart_app.test_client()


def _set_stored_branding(db: Any, *, logo_url: str | None, config: dict[str, Any] | None) -> None:
    db.dal(db.dal.tenants.slug == TENANT_SLUG).update(logo_url=logo_url, config=config)
    db.dal.commit()


def _tenant_row(db: Any) -> Any:
    row = db.dal(db.dal.tenants.slug == TENANT_SLUG).select().first()
    db.dal.commit()
    return row


def _admin_headers() -> dict[str, str]:
    token = make_user_token(user_id=1, scope="tenant:admin", tenant=TENANT_SLUG)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def gate_off(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    gate = AsyncMock(return_value=False)
    monkeypatch.setattr(branding, "feature_enabled", gate)
    return gate


@pytest.fixture
def gate_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    gate = AsyncMock(return_value=True)
    monkeypatch.setattr(branding, "feature_enabled", gate)
    return gate


class TestLoginInfoBrandingApplication:
    async def test_free_tenant_is_served_default_branding_not_stored_custom(
        self, client: Any, gate_off: AsyncMock
    ) -> None:
        response = await client.get(f"/api/v1/auth/tenant/{TENANT_SLUG}")

        assert response.status_code == 200
        tenant = (await response.get_json())["tenant"]
        assert tenant["logoUrl"] is None
        assert tenant["config"] == {"theme": None, "welcomeMessage": None}
        assert tenant["whitelabeled"] is False
        # Non-branding, functional fields are untouched by the gate.
        assert tenant["slug"] == TENANT_SLUG
        assert tenant["displayName"] == "Acme Corp"
        assert tenant["enabledPlatforms"] == []

    async def test_entitled_tenant_is_served_its_custom_branding(
        self, client: Any, gate_on: AsyncMock
    ) -> None:
        response = await client.get(f"/api/v1/auth/tenant/{TENANT_SLUG}")

        tenant = (await response.get_json())["tenant"]
        assert tenant["logoUrl"] == STORED_LOGO
        assert tenant["config"] == {"theme": "midnight", "welcomeMessage": "Welcome, Acme folks"}
        assert tenant["whitelabeled"] is True
        assert gate_on.await_args.args[0] == FLAG
        assert gate_on.await_args.kwargs["tenant"] == TENANT_SLUG

    async def test_tenant_with_no_custom_branding_never_evaluates_the_gate(
        self, client: Any, db: Any, gate_on: AsyncMock
    ) -> None:
        """Nothing to whitelabel -> no PostHog/license round trip on every login-page view."""
        _set_stored_branding(db, logo_url=None, config=None)

        response = await client.get(f"/api/v1/auth/tenant/{TENANT_SLUG}")

        tenant = (await response.get_json())["tenant"]
        assert tenant["whitelabeled"] is False
        assert tenant["logoUrl"] is None
        gate_on.assert_not_awaited()

    async def test_entitled_tenant_with_a_hostile_logo_url_gets_the_default_logo(
        self, client: Any, db: Any, gate_on: AsyncMock
    ) -> None:
        _set_stored_branding(db, logo_url="javascript:alert(1)", config={"theme": "midnight"})

        response = await client.get(f"/api/v1/auth/tenant/{TENANT_SLUG}")

        tenant = (await response.get_json())["tenant"]
        assert tenant["logoUrl"] is None
        assert tenant["config"]["theme"] == "midnight"
        assert tenant["whitelabeled"] is True

    async def test_entitled_tenant_with_only_invalid_branding_is_not_whitelabeled(
        self, client: Any, db: Any, gate_on: AsyncMock
    ) -> None:
        _set_stored_branding(db, logo_url="http://insecure.example/x.png", config={"theme": 7})

        response = await client.get(f"/api/v1/auth/tenant/{TENANT_SLUG}")

        tenant = (await response.get_json())["tenant"]
        assert tenant["whitelabeled"] is False
        assert tenant["logoUrl"] is None

    async def test_unknown_tenant_is_still_404(self, client: Any, gate_on: AsyncMock) -> None:
        response = await client.get("/api/v1/auth/tenant/no-such-tenant")
        assert response.status_code == 404

    @pytest.mark.parametrize(
        ("tier", "flag_result", "expected_whitelabeled"),
        [
            ("free", True, False),
            ("community", True, False),  # penguin_licensing's unlicensed floor name
            ("professional", True, True),
            ("enterprise", True, True),
            ("professional", False, False),  # entitled by license, flag OFF
            ("professional", None, False),  # flag unresolvable -> fail closed
        ],
    )
    async def test_tier_resolution_through_the_real_two_gate(
        self,
        client: Any,
        monkeypatch: pytest.MonkeyPatch,
        tier: str,
        flag_result: bool | None,
        expected_whitelabeled: bool,
    ) -> None:
        install_entitlement_client(
            monkeypatch,
            flag_gate=FakeFlagGate(result=flag_result),
            license_gate=FakeLicenseGate(tier=tier),
            tier_requirements={FLAG: "professional"},
        )

        response = await client.get(f"/api/v1/auth/tenant/{TENANT_SLUG}")

        tenant = (await response.get_json())["tenant"]
        assert tenant["whitelabeled"] is expected_whitelabeled
        assert (tenant["logoUrl"] == STORED_LOGO) is expected_whitelabeled

    async def test_unreachable_gates_degrade_to_default_branding_not_an_error(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_entitlement_client(
            monkeypatch,
            flag_gate=FakeFlagGate(raises=ConnectionError("posthog down")),
            license_gate=FakeLicenseGate(raises=ConnectionError("license down")),
            tier_requirements={FLAG: "professional"},
        )

        response = await client.get(f"/api/v1/auth/tenant/{TENANT_SLUG}")

        assert response.status_code == 200
        tenant = (await response.get_json())["tenant"]
        assert tenant["whitelabeled"] is False
        assert tenant["logoUrl"] is None


class TestTenantUpdateBrandingGate:
    async def test_free_tenant_cannot_set_a_logo(
        self, client: Any, db: Any, gate_off: AsyncMock
    ) -> None:
        response = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}",
            headers=_admin_headers(),
            json={"logoUrl": "https://cdn.acme.example/new.png"},
        )

        assert response.status_code == 402
        body = await response.get_json()
        assert body["error"]["code"] == "FEATURE_NOT_ENABLED"
        assert _tenant_row(db).logo_url == STORED_LOGO  # unchanged
        assert gate_off.await_args.args[0] == FLAG

    async def test_free_tenant_cannot_set_a_theme_or_welcome_message(
        self, client: Any, db: Any, gate_off: AsyncMock
    ) -> None:
        for config in (
            {**STORED_CONFIG, "theme": "sunrise"},
            {**STORED_CONFIG, "welcomeMessage": "Hello!"},
        ):
            response = await client.put(
                f"/api/v1/tenant/{TENANT_SLUG}", headers=_admin_headers(), json={"config": config}
            )
            assert response.status_code == 402
        assert _tenant_row(db).config == STORED_CONFIG

    async def test_non_branding_edits_are_never_gated(
        self, client: Any, gate_off: AsyncMock
    ) -> None:
        response = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}",
            headers=_admin_headers(),
            json={"displayName": "Acme Renamed", "description": "d"},
        )

        assert response.status_code == 200
        gate_off.assert_not_awaited()

    async def test_downgraded_tenant_can_resend_unchanged_branding_with_other_edits(
        self, client: Any, db: Any, gate_off: AsyncMock
    ) -> None:
        """Echoing the stored branding back while editing `locale` is not a branding change."""
        response = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}",
            headers=_admin_headers(),
            json={"logoUrl": STORED_LOGO, "config": {**STORED_CONFIG, "locale": "fr"}},
        )

        assert response.status_code == 200
        assert _tenant_row(db).config["locale"] == "fr"
        gate_off.assert_not_awaited()

    async def test_downgraded_tenant_can_clear_its_branding(
        self, client: Any, db: Any, gate_off: AsyncMock
    ) -> None:
        response = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}",
            headers=_admin_headers(),
            json={"config": {"locale": "en"}},
        )

        assert response.status_code == 200
        assert "theme" not in (_tenant_row(db).config or {})

    async def test_entitled_tenant_can_set_branding(
        self, client: Any, db: Any, gate_on: AsyncMock
    ) -> None:
        response = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}",
            headers=_admin_headers(),
            json={"logoUrl": "https://cdn.acme.example/new.png", "config": {"theme": "sunrise"}},
        )

        assert response.status_code == 200
        row = _tenant_row(db)
        assert row.logo_url == "https://cdn.acme.example/new.png"
        assert row.config == {"theme": "sunrise"}

    @pytest.mark.parametrize(
        ("tier", "expected"),
        [("free", 402), ("professional", 200), ("enterprise", 200)],
    )
    async def test_tier_resolution_through_the_real_two_gate(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, tier: str, expected: int
    ) -> None:
        install_entitlement_client(
            monkeypatch,
            flag_gate=FakeFlagGate(result=True),
            license_gate=FakeLicenseGate(tier=tier),
            tier_requirements={FLAG: "professional"},
        )

        response = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}",
            headers=_admin_headers(),
            json={"logoUrl": "https://cdn.acme.example/new.png"},
        )

        assert response.status_code == expected

    async def test_unreachable_gates_fail_closed_on_write(
        self, client: Any, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_entitlement_client(
            monkeypatch,
            flag_gate=FakeFlagGate(raises=ConnectionError("down")),
            license_gate=FakeLicenseGate(raises=ConnectionError("down")),
            tier_requirements={FLAG: "professional"},
        )

        response = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}",
            headers=_admin_headers(),
            json={"logoUrl": "https://cdn.acme.example/new.png"},
        )

        assert response.status_code == 402
        assert _tenant_row(db).logo_url == STORED_LOGO


class TestBrandingServiceUnits:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://cdn.example/logo.png", "https://cdn.example/logo.png"),
            ("/static/logo.png", "/static/logo.png"),
            ("  https://cdn.example/x.png  ", "https://cdn.example/x.png"),
            ("http://cdn.example/logo.png", None),
            ("//evil.example/logo.png", None),
            ("javascript:alert(1)", None),
            ("data:image/png;base64,AAAA", None),
            ("ftp://x/y", None),
            ("", None),
            ("   ", None),
            (None, None),
            (123, None),
            ("https://" + "a" * 3000, None),
        ],
    )
    def test_logo_url_sanitization(self, raw: Any, expected: str | None) -> None:
        cleaned = branding.sanitize_branding(LoginBranding(raw, None, None))
        assert cleaned.logo_url == expected

    def test_text_fields_are_stripped_capped_and_type_checked(self) -> None:
        cleaned = branding.sanitize_branding(LoginBranding(None, "mid\x00night\n", "Hi\x07 there"))
        assert (cleaned.theme, cleaned.welcome_message) == ("midnight", "Hi there")

        too_long = branding.sanitize_branding(LoginBranding(None, "t" * 101, "w" * 501))
        assert (too_long.theme, too_long.welcome_message) == (None, None)

        wrong_type = branding.sanitize_branding(LoginBranding(None, ["x"], {"a": 1}))
        assert (wrong_type.theme, wrong_type.welcome_message) == (None, None)

    def test_has_custom_branding(self) -> None:
        assert not branding.has_custom_branding(branding.DEFAULT_BRANDING)
        assert branding.has_custom_branding(LoginBranding(None, "x", None))
        assert branding.has_custom_branding(LoginBranding("/l.png", None, None))

    @pytest.mark.parametrize(
        ("current_logo", "current_cfg", "new_logo", "new_cfg", "expected"),
        [
            (None, None, None, None, False),
            (None, None, "https://a/b.png", None, True),
            ("https://a/b.png", None, "https://a/b.png", None, False),  # unchanged echo
            ("https://a/b.png", None, "https://a/c.png", None, True),
            ("https://a/b.png", None, "", None, False),  # clearing is allowed
            (None, {"theme": "x"}, None, {"theme": "x"}, False),
            (None, {"theme": "x"}, None, {"theme": "y"}, True),
            (None, {"theme": "x"}, None, {}, False),  # clearing is allowed
            (None, None, None, {"welcomeMessage": "hi"}, True),
            (None, None, None, {"locale": "en"}, False),  # non-branding key
            (None, {"locale": "en"}, None, {"locale": "fr"}, False),
        ],
    )
    def test_sets_branding_truth_table(
        self,
        current_logo: str | None,
        current_cfg: dict[str, Any] | None,
        new_logo: str | None,
        new_cfg: dict[str, Any] | None,
        expected: bool,
    ) -> None:
        assert (
            branding.sets_branding(
                current_logo_url=current_logo,
                current_config=current_cfg,
                new_logo_url=new_logo,
                new_config=new_cfg,
            )
            is expected
        )

    async def test_resolve_login_branding_directly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stored = LoginBranding(STORED_LOGO, "midnight", "Hello")
        monkeypatch.setattr(branding, "feature_enabled", AsyncMock(return_value=True))
        served, whitelabeled = await branding.resolve_login_branding("acme", stored)
        assert (served, whitelabeled) == (stored, True)

        monkeypatch.setattr(branding, "feature_enabled", AsyncMock(return_value=False))
        served, whitelabeled = await branding.resolve_login_branding("acme", stored)
        assert (served, whitelabeled) == (branding.DEFAULT_BRANDING, False)

    async def test_whitelabel_enabled_defaults_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate = AsyncMock(return_value=False)
        monkeypatch.setattr(branding, "feature_enabled", gate)

        assert await branding.whitelabel_enabled("acme") is False
        assert gate.await_args.kwargs == {"tenant": "acme", "default": False}
