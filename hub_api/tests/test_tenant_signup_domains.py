"""Per-tenant signup email-domain restriction: resolver, tenant-admin endpoint, every signup path.

- `email_verification.combine_allowlists` / `resolve_signup_policy`: the ONE resolver (platform
  allowlist ∩ tenant allowlist; an empty side imposes nothing; disjoint lists deny all).
- `PUT /api/v1/tenant/<slug>/signup-domains`: `tenant:admin` AND the per-action
  `tenant.signup_domains:write`, URL slug must match the token's tenant; the generic settings
  routes never list or write the reserved key.
- OAuth: a restricted tenant always mails a verification link and withholds the session
  (tenant SSO: tests/sso/test_sso_signup_domain_restriction.py).

Mail goes to the real aiosmtpd STARTTLS sink (tests/test_dsar_email_sender.py).
"""

# ruff: noqa: F811 - reused fixtures re-declared as args

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from flask_core.auth import DEFAULT_TENANT_SLUG, SCOPE_BUNDLES, SCOPE_TENANT_SIGNUP_DOMAINS_WRITE
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.tenant import tenant_bp
from services import email_verification as ev
from services import oauth_service
from services import tenant_service as tenant_svc
from services.errors import ApiError
from tests.conftest import TENANT_SLUG, make_user_token
from tests.test_dsar_email_sender import make_sink, pki  # noqa: F401 - fixtures
from tests.test_email_verification import (  # noqa: F401 - fixtures + helpers
    _test_config,
    _token_from,
    kill_switch_off,
    no_smtp_env,
    smtp,
)

ADMIN_WRITE = f"tenant:admin {SCOPE_TENANT_SIGNUP_DOMAINS_WRITE}"


class TestResolver:
    @pytest.mark.parametrize(
        ("platform", "tenant", "restricted", "domains"),
        [
            ([], [], False, ()),
            ([], ["acme.test"], True, ("acme.test",)),
            (["acme.test", "corp.test"], [], True, ("acme.test", "corp.test")),
            (["acme.test", "corp.test"], ["corp.test", "x.test"], True, ("corp.test",)),
            (["acme.test"], ["other.test"], True, ()),  # disjoint: deny all, never "open"
        ],
    )
    def test_platform_intersect_tenant(
        self, platform: list[str], tenant: list[str], restricted: bool, domains: tuple[str, ...]
    ) -> None:
        policy = ev.combine_allowlists(platform, tenant)
        assert policy.restricted is restricted and policy.domains == domains

    def test_policy_matching_is_exact_and_denies_missing_email(self) -> None:
        policy = ev.combine_allowlists([], ["acme.test"])
        assert policy.allows("Bob@ACME.test")
        assert not policy.allows("bob@sub.acme.test")
        assert not policy.allows("bob@acme.test.evil.example")
        assert not policy.allows(None)
        assert ev.combine_allowlists([], []).allows(None)
        assert not ev.combine_allowlists(["a.test"], ["b.test"]).allows("x@a.test")

    @pytest.mark.parametrize(
        "bad", ["not a domain", "localhost", "10.0.0.1", "a..b.test", "x" * 300]
    )
    def test_tenant_list_validation(self, bad: str) -> None:
        with pytest.raises(ApiError) as err:
            ev.normalize_tenant_domains([bad])
        assert err.value.status_code == 400

    def test_tenant_list_is_normalised_and_capped(self) -> None:
        assert ev.normalize_tenant_domains([" @Acme.Test ", "acme.test", "corp.test"]) == [
            "acme.test",
            "corp.test",
        ]
        with pytest.raises(ApiError):
            ev.normalize_tenant_domains([f"d{i}.test" for i in range(51)])

    async def test_resolver_reads_platform_and_tenant_rows(self, auth_db: Any) -> None:
        dal = auth_db.dal
        dal.hub_settings.insert(setting_key="signup_allowed_domains", setting_value="a.test,b.test")
        tenant_id = dal(dal.tenants.slug == TENANT_SLUG).select().first().id
        dal.commit()
        await tenant_svc.set_tenant_signup_domains(
            auth_db, dal, tenant_id=tenant_id, domains=["b.test", "c.test"]
        )
        policy = await ev.resolve_signup_policy(auth_db, dal, tenant_slug=TENANT_SLUG)
        assert policy == ev.SignupPolicy(True, ("b.test",))
        # a tenant with no row of its own falls back to the platform list
        default = await ev.resolve_signup_policy(auth_db, dal, tenant_slug="no-such-tenant")
        assert default == ev.SignupPolicy(True, ("a.test", "b.test"))


def test_tenant_admin_bundle_carries_the_per_action_scope() -> None:
    assert SCOPE_TENANT_SIGNUP_DOMAINS_WRITE in SCOPE_BUNDLES["tenant"]["admin"]
    assert SCOPE_TENANT_SIGNUP_DOMAINS_WRITE not in SCOPE_BUNDLES["tenant"]["maintainer"]


@pytest.fixture
def tenant_client(tenant_admin_db: Any) -> Any:
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(tenant_bp)
    app.config["dal"] = tenant_admin_db.dal
    app.config["async_dal"] = tenant_admin_db
    return app.test_client()


def _hdr(scope: str, tenant: str = TENANT_SLUG) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_user_token(user_id=1, scope=scope, tenant=tenant)}"}


URL = f"/api/v1/tenant/{TENANT_SLUG}/signup-domains"


class TestTenantEndpoint:
    async def test_admin_with_write_scope_sets_and_reads_the_list(self, tenant_client: Any) -> None:
        put = await tenant_client.put(
            URL, json={"domains": ["Acme.test", "corp.test"]}, headers=_hdr(ADMIN_WRITE)
        )
        assert put.status_code == 200
        body = await put.get_json()
        assert body["domains"] == ["acme.test", "corp.test"] and body["restricted"] is True
        got = await (await tenant_client.get(URL, headers=_hdr("tenant:admin"))).get_json()
        assert got["effectiveDomains"] == ["acme.test", "corp.test"]

        cleared = await tenant_client.put(URL, json={"domains": []}, headers=_hdr(ADMIN_WRITE))
        assert (await cleared.get_json())["restricted"] is False

    @pytest.mark.parametrize("scope", ["tenant:admin", "tenant:read", "tenant:write"])
    async def test_without_the_per_action_scope_is_403(
        self, tenant_client: Any, scope: str
    ) -> None:
        response = await tenant_client.put(URL, json={"domains": ["a.test"]}, headers=_hdr(scope))
        assert response.status_code == 403

    async def test_non_admin_cannot_read(self, tenant_client: Any) -> None:
        assert (await tenant_client.get(URL, headers=_hdr("tenant:read"))).status_code == 403

    async def test_tenant_mismatch_is_403(self, tenant_client: Any, tenant_admin_db: Any) -> None:
        tenant_admin_db.dal.tenants.insert(slug="other-co", display_name="Other", is_active=True)
        tenant_admin_db.dal.commit()
        response = await tenant_client.put(
            "/api/v1/tenant/other-co/signup-domains",
            json={"domains": ["evil.test"]},
            headers=_hdr(ADMIN_WRITE, tenant=TENANT_SLUG),
        )
        assert response.status_code == 403

    async def test_invalid_domain_is_400(self, tenant_client: Any) -> None:
        response = await tenant_client.put(
            URL, json={"domains": ["not a domain"]}, headers=_hdr(ADMIN_WRITE)
        )
        assert response.status_code == 400

    async def test_generic_settings_routes_cannot_touch_the_reserved_key(
        self, tenant_client: Any
    ) -> None:
        await tenant_client.put(URL, json={"domains": ["acme.test"]}, headers=_hdr(ADMIN_WRITE))
        bypass = await tenant_client.put(
            f"/api/v1/tenant/{TENANT_SLUG}/settings",
            json={"settings": [{"key": "signup_allowed_domains", "value": ""}]},
            headers=_hdr("tenant:admin"),
        )
        assert bypass.status_code == 400
        listed = await tenant_client.get(
            f"/api/v1/tenant/{TENANT_SLUG}/settings", headers=_hdr("tenant:admin")
        )
        keys = [s["key"] for s in (await listed.get_json())["settings"]]
        assert "signup_allowed_domains" not in keys


async def restrict(db: Any, slug: str, domains: list[str]) -> None:
    dal = db.dal
    row = dal(dal.tenants.slug == slug).select().first()
    if row is None:
        tenant_id = dal.tenants.insert(slug=slug, display_name=slug, is_active=True)
        dal.commit()
    else:
        tenant_id = row.id
    await tenant_svc.set_tenant_signup_domains(db, dal, tenant_id=int(tenant_id), domains=domains)


def _discord(email: str | None, ident: str = "2001") -> dict[str, Any]:
    return {"id": ident, "username": f"user{ident}", "email": email, "avatar_url": None}


class TestOAuthTenantRestriction:
    async def _login(self, auth_db: Any, data: dict[str, Any]) -> Any:
        return await oauth_service._find_or_create_user_from_oauth(  # noqa: SLF001
            auth_db, auth_db.dal, platform="discord", user_data=data, cfg=_test_config()
        )

    async def test_default_tenant_restriction_gates_oauth_signup(
        self, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        await restrict(auth_db, DEFAULT_TENANT_SLUG, ["corp.test"])
        with pytest.raises(ApiError) as wrong:
            await self._login(auth_db, _discord("gamer@example.com"))
        assert wrong.value.code == ev.ERR_DOMAIN
        with pytest.raises(ApiError) as pending:
            await self._login(auth_db, _discord("worker@corp.test", "2002"))
        assert pending.value.code == ev.ERR_REQUIRED and len(sink.handler.messages) == 1

    async def test_legacy_unverified_user_passes_until_tenant_restricts(
        self, auth_db: Any, smtp: Any
    ) -> None:
        sink = smtp()
        dal = auth_db.dal
        user_id = dal.hub_users.insert(
            email="legacy@corp.test",
            username="legacy",
            is_active=True,
            email_verified=False,  # pre-feature OAuth row: never verified, no token
            created_at=datetime.now(UTC),
        )
        dal.hub_user_identities.insert(
            hub_user_id=user_id,
            platform="discord",
            platform_user_id="3003",
            platform_username="legacy",
            linked_at=datetime.now(UTC),
        )
        dal.commit()
        assert (await self._login(auth_db, _discord("legacy@corp.test", "3003"))).id == user_id

        await restrict(auth_db, DEFAULT_TENANT_SLUG, ["corp.test"])
        with pytest.raises(ApiError) as held:
            await self._login(auth_db, _discord("legacy@corp.test", "3003"))
        assert held.value.code == ev.ERR_REQUIRED and len(sink.handler.messages) == 1
