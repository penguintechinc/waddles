"""OIDC and Google login, end to end through the real blueprints.

Everything runs for real -- tenant resolution, entitlement gate, state store, PKCE,
discovery/JWKS/token calls over the guarded HTTP client, ID-token validation, JIT
provisioning, session minting and the exchange-code hand-off -- with only the IdP
socket and Redis replaced.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from services.sso_settings import SsoSettings
from tests.conftest import TENANT_SLUG
from tests.sso.conftest import Entitlements, FakeRedis
from tests.sso.idp_fakes import FakeOidcIdp
from tests.sso.kit import FRONTEND, Kit


class TestHappyPath:
    async def test_first_login_provisions_a_user_and_mints_a_tenant_session(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        response = await kit.oidc_login(public_id)
        assert response.status_code == 303
        payload = await kit.redeem(response)

        assert payload["tenant"] == TENANT_SLUG
        assert payload["iss"] and payload["aud"] and payload["exp"]
        assert "*:read" in payload["scope"].split()
        assert "tenant:admin" not in payload["scope"].split()  # SSO users are not admins

        users = await kit.users_by_email("alice@acme.test")
        assert len(users) == 1
        user = users[0]
        assert user.display_name == "Alice Example"
        assert user.username is None
        assert user.email_verified is True
        assert user.is_active is True
        assert user.password_hash is None  # SSO-only account: no local password
        assert payload["sub"] == str(user.id)
        assert await kit.count_identities() == 1

    async def test_returning_user_is_matched_by_subject_not_duplicated(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        first = await kit.redeem(await kit.oidc_login(public_id))
        second = await kit.redeem(await kit.oidc_login(public_id))
        assert first["sub"] == second["sub"]
        assert len(await kit.users_by_email("alice@acme.test")) == 1
        assert await kit.count_identities() == 1

    async def test_returning_user_matched_by_subject_even_if_email_changed(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        first = await kit.redeem(await kit.oidc_login(public_id))
        second = await kit.redeem(await kit.oidc_login(public_id, email="alice.renamed@acme.test"))
        assert first["sub"] == second["sub"]
        assert await kit.users_by_email("alice.renamed@acme.test") == []

    async def test_same_subject_on_two_connections_is_two_distinct_users(self, kit: Kit) -> None:
        a = await kit.create(kit.oidc_body(displayName="A"))
        b = await kit.create(kit.oidc_body(displayName="B"))
        first = await kit.redeem(await kit.oidc_login(a))
        second = await kit.redeem(await kit.oidc_login(b, email="bob@acme.test"))
        assert first["sub"] != second["sub"]

    async def test_session_jwt_is_never_placed_in_a_url(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        response = await kit.oidc_login(public_id)
        location = kit.location(response)
        assert location.startswith(f"{FRONTEND}/auth/callback?code=")
        assert "eyJ" not in location  # JWT header prefix
        assert set(parse_qs(urlparse(location).query)) == {"code"}

    async def test_exchange_code_is_single_use(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        response = await kit.oidc_login(public_id)
        code = parse_qs(urlparse(kit.location(response)).query)["code"][0]
        await kit.redeem(response)
        again = await kit.client.post("/api/v1/auth/exchange", json={"code": code})
        assert again.status_code == 400

    async def test_responses_are_not_cacheable_and_binder_cookie_is_cleared(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        response = await kit.oidc_login(public_id)
        assert response.headers["Cache-Control"] == "no-store"
        cleared = [
            c for c in response.headers.getlist("Set-Cookie") if c.startswith("wb_sso_bind=")
        ]
        assert cleared and ("Max-Age=0" in cleared[0] or "expires=" in cleared[0].lower())

    async def test_public_client_with_pkce_only(self, kit: Kit) -> None:
        kit.oidc.client_secret = None
        body = kit.oidc_body()
        del body["clientSecret"]
        public_id = await kit.create(body)
        assert (await kit.redeem(await kit.oidc_login(public_id)))["sub"]

    async def test_idp_that_only_supports_client_secret_post(self, kit: Kit) -> None:
        kit.oidc.advertised_auth_methods = ["client_secret_post"]
        public_id = await kit.create(kit.oidc_body())
        assert (await kit.redeem(await kit.oidc_login(public_id)))["sub"]


class TestStartEndpoints:
    async def test_start_returns_the_idp_url_and_sets_a_scoped_httponly_binder_cookie(
        self, kit: Kit
    ) -> None:
        public_id = await kit.create(kit.oidc_body())
        kit.use_oidc_idp()
        response = await kit.start(public_id)
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        assert body["protocol"] == "oidc"
        parsed = urlparse(body["redirectUrl"])
        q = parse_qs(parsed.query)
        assert parsed.netloc == "idp.example.com"
        assert q["code_challenge_method"] == ["S256"]
        assert q["redirect_uri"] == [
            f"https://hub.example.com/api/v1/auth/sso/{public_id}/callback"
        ]
        cookie = [
            c for c in response.headers.getlist("Set-Cookie") if c.startswith("wb_sso_bind=")
        ][0]
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=Lax" in cookie
        assert f"Path=/api/v1/auth/sso/{public_id}" in cookie
        assert response.headers["Cache-Control"] == "no-store"
        # The state in the URL is NOT the cookie value (cookie = HMAC(state)).
        assert q["state"][0] not in cookie

    async def test_login_endpoint_302s_straight_to_the_idp(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        kit.use_oidc_idp()
        response = await kit.client.get(f"/api/v1/auth/sso/{public_id}/login")
        assert response.status_code == 302
        assert kit.location(response).startswith("https://idp.example.com/authorize?")
        assert any(c.startswith("wb_sso_bind=") for c in response.headers.getlist("Set-Cookie"))

    async def test_login_endpoint_failure_redirects_to_the_spa_with_a_fixed_reason(
        self, kit: Kit
    ) -> None:
        response = await kit.client.get("/api/v1/auth/sso/does-not-exist/login")
        assert response.status_code == 302
        assert kit.error_reason(response) == "sso_denied"

    async def test_every_start_gets_a_fresh_state(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        kit.use_oidc_idp()
        states = set()
        for _ in range(3):
            body = await (await kit.start(public_id)).get_json()
            states.add(parse_qs(urlparse(body["redirectUrl"]).query)["state"][0])
        assert len(states) == 3


class TestOptions:
    async def test_lists_enabled_entitled_connections_of_the_tenant_only(self, kit: Kit) -> None:
        enabled = await kit.create(kit.oidc_body(displayName="On"))
        await kit.create(kit.oidc_body(displayName="Off", enabled=False))
        await kit.post_connection(kit.oidc_body(displayName="Theirs"), tenant="other-corp")
        response = await kit.client.get(f"/api/v1/auth/sso/options?tenant={TENANT_SLUG}")
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {
            "success": True,
            "options": [{"id": enabled, "displayName": "On", "protocol": "oidc"}],
        }

    async def test_unknown_tenant_gets_an_empty_list_not_an_error(self, kit: Kit) -> None:
        response = await kit.client.get("/api/v1/auth/sso/options?tenant=ghost")
        assert response.status_code == 200
        assert (await response.get_json())["options"] == []

    async def test_unentitled_tenant_gets_no_buttons(
        self, kit: Kit, entitlements: Entitlements
    ) -> None:
        await kit.create(kit.oidc_body())
        entitlements.set_tier("free")
        body = await (
            await kit.client.get(f"/api/v1/auth/sso/options?tenant={TENANT_SLUG}")
        ).get_json()
        assert body["options"] == []

    async def test_options_exposes_no_configuration(self, kit: Kit) -> None:
        await kit.create(kit.oidc_body())
        text = await (
            await kit.client.get(f"/api/v1/auth/sso/options?tenant={TENANT_SLUG}")
        ).get_data(as_text=True)
        assert "idp.example.com" not in text
        assert "acme.test" not in text
        assert "clientId" not in text


class TestFlowIntegrity:
    async def test_idp_error_param_is_a_denied_login(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        response = await kit.client.get(
            f"/api/v1/auth/sso/{public_id}/callback", query_string={"error": "access_denied"}
        )
        assert response.status_code == 302
        assert kit.error_reason(response) == "sso_denied"

    @pytest.mark.parametrize("query", [{}, {"code": "x"}, {"state": "y"}])
    async def test_missing_code_or_state(self, kit: Kit, query: dict[str, str]) -> None:
        public_id = await kit.create(kit.oidc_body())
        response = await kit.client.get(
            f"/api/v1/auth/sso/{public_id}/callback", query_string=query
        )
        assert kit.error_reason(response) == "sso_denied"

    async def test_forged_state_is_a_session_mismatch(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        response = await kit.client.get(
            f"/api/v1/auth/sso/{public_id}/callback", query_string={"code": "c", "state": "forged"}
        )
        assert kit.error_reason(response) == "sso_session_mismatch"
        assert await kit.users_by_email("alice@acme.test") == []

    async def test_replaying_a_completed_callback_fails_and_creates_nothing_new(
        self, kit: Kit
    ) -> None:
        public_id = await kit.create(kit.oidc_body())
        kit.use_oidc_idp()
        redirect_url = (await (await kit.start(public_id)).get_json())["redirectUrl"]
        code, state = kit.oidc.authorize(redirect_url)
        first = await kit.client.get(
            f"/api/v1/auth/sso/{public_id}/callback", query_string={"code": code, "state": state}
        )
        assert first.status_code == 303
        replay = await kit.client.get(
            f"/api/v1/auth/sso/{public_id}/callback", query_string={"code": code, "state": state}
        )
        assert kit.error_reason(replay) == "sso_session_mismatch"
        assert await kit.count_identities() == 1

    async def test_login_csrf_flow_started_in_another_browser_is_rejected(self, kit: Kit) -> None:
        """An attacker starts a flow, then lures the victim's browser to the callback."""
        public_id = await kit.create(kit.oidc_body())
        kit.use_oidc_idp()
        attacker = kit.app.test_client()
        started = await attacker.get(f"/api/v1/auth/sso/{public_id}/start")
        redirect_url = (await started.get_json())["redirectUrl"]
        code, state = kit.oidc.authorize(
            redirect_url, sub="attacker-subject", email="mallory@acme.test"
        )
        victim = kit.app.test_client()  # holds no binder cookie for this flow
        response = await victim.get(
            f"/api/v1/auth/sso/{public_id}/callback", query_string={"code": code, "state": state}
        )
        assert kit.error_reason(response) == "sso_session_mismatch"
        assert await kit.users_by_email("mallory@acme.test") == []

    async def test_binder_cookie_of_a_different_flow_is_rejected(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        kit.use_oidc_idp()
        browser_a = kit.app.test_client()
        browser_b = kit.app.test_client()
        url_a = (await (await browser_a.get(f"/api/v1/auth/sso/{public_id}/start")).get_json())[
            "redirectUrl"
        ]
        await browser_b.get(f"/api/v1/auth/sso/{public_id}/start")
        code, state = kit.oidc.authorize(url_a)
        # browser_b holds a valid binder -- for ITS flow, not A's.
        response = await browser_b.get(
            f"/api/v1/auth/sso/{public_id}/callback", query_string={"code": code, "state": state}
        )
        assert kit.error_reason(response) == "sso_session_mismatch"

    async def test_state_from_another_connection_is_rejected(
        self, kit: Kit, caplog: pytest.LogCaptureFixture
    ) -> None:
        one = await kit.create(kit.oidc_body(displayName="One"))
        two = await kit.create(kit.oidc_body(displayName="Two"))
        kit.use_oidc_idp()
        url = (await (await kit.start(one)).get_json())["redirectUrl"]
        await kit.start(two)
        code, state = kit.oidc.authorize(url)
        with caplog.at_level(logging.INFO):
            response = await kit.client.get(
                f"/api/v1/auth/sso/{two}/callback", query_string={"code": code, "state": state}
            )
        assert kit.error_reason(response) == "sso_session_mismatch"
        assert "err_code=state_connection_mismatch" in caplog.text


class TestIdpFailures:
    async def test_unreachable_idp_at_start_is_a_502_json_error(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())

        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        kit.app.config["sso_http_transport"] = httpx.MockTransport(down)
        response = await kit.start(public_id)
        assert response.status_code == 502
        assert (await response.get_json())["error"]["code"] == "IDP_UNAVAILABLE"

    async def test_unreachable_idp_via_login_redirect_shows_the_fixed_reason(
        self, kit: Kit
    ) -> None:
        public_id = await kit.create(kit.oidc_body())
        kit.app.config["sso_http_transport"] = httpx.MockTransport(lambda r: httpx.Response(503))
        response = await kit.client.get(f"/api/v1/auth/sso/{public_id}/login")
        assert kit.error_reason(response) == "sso_idp_unavailable"

    async def test_token_endpoint_failure(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        kit.oidc.token_response_override = (500, {"error": "server_error"})
        response = await kit.oidc_login(public_id)
        assert kit.error_reason(response) == "sso_idp_unavailable"

    @pytest.mark.parametrize(
        "idp_kwargs",
        [
            {"claims": {"aud": "someone-else"}},
            {"claims": {"iss": "https://evil.example.com"}},
            {"id_token_ttl": -3600},
            {"alg": "HS256"},
            {"alg": "none"},
        ],
    )
    async def test_invalid_id_tokens_never_log_anyone_in(
        self, kit: Kit, idp_kwargs: dict[str, Any]
    ) -> None:
        for key, value in idp_kwargs.items():
            setattr(kit.oidc, key, value)
        public_id = await kit.create(kit.oidc_body())
        response = await kit.oidc_login(public_id)
        assert kit.error_reason(response) == "sso_invalid_response"
        assert await kit.users_by_email("alice@acme.test") == []
        assert await kit.count_identities() == 0

    async def test_id_token_from_an_attacker_key_is_rejected(self, kit: Kit) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa

        kit.oidc.signing_key_override = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        public_id = await kit.create(kit.oidc_body())
        assert kit.error_reason(await kit.oidc_login(public_id)) == "sso_invalid_response"


class TestProvisioningPolicy:
    async def test_email_outside_allowed_domains_is_refused(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        response = await kit.oidc_login(public_id, email="eve@evil.test")
        assert kit.error_reason(response) == "sso_domain_not_allowed"
        assert await kit.users_by_email("eve@evil.test") == []

    async def test_lookalike_domain_is_not_a_match(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        for email in ("eve@acme.test.evil.example", "eve@notacme.test", "eve@sub.acme.test"):
            response = await kit.oidc_login(public_id, email=email, sub=f"s-{email}")
            assert kit.error_reason(response) == "sso_domain_not_allowed", email

    async def test_unverified_email_is_refused_for_new_accounts(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        response = await kit.oidc_login(public_id, email_verified=False)
        assert kit.error_reason(response) == "sso_email_unverified"
        assert await kit.users_by_email("alice@acme.test") == []

    async def test_missing_email_is_refused_for_new_accounts(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        response = await kit.oidc_login(public_id, email=None)
        assert kit.error_reason(response) == "sso_email_required"

    async def test_returning_user_needs_no_email_claim(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        first = await kit.redeem(await kit.oidc_login(public_id))
        again = await kit.redeem(await kit.oidc_login(public_id, email=None, email_verified=None))
        assert again["sub"] == first["sub"]

    async def test_removing_a_domain_cuts_off_returning_users_of_that_domain(
        self, kit: Kit
    ) -> None:
        public_id = await kit.create(kit.oidc_body())
        await kit.redeem(await kit.oidc_login(public_id))
        await kit.patch_connection(public_id, {"allowedDomains": ["other.example"]})
        response = await kit.oidc_login(public_id)
        assert kit.error_reason(response) == "sso_domain_not_allowed"

    async def test_inactive_linked_account_cannot_log_in(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        payload = await kit.redeem(await kit.oidc_login(public_id))
        await kit.db.update_async(kit.db.dal.hub_users.id == int(payload["sub"]), is_active=False)
        response = await kit.oidc_login(public_id)
        assert kit.error_reason(response) == "sso_account_inactive"

    async def test_existing_local_account_with_same_email_is_never_adopted(self, kit: Kit) -> None:
        """Cross-tenant takeover guard: hub_users is global, an IdP-asserted email proves nothing."""
        victim_id = await kit.db.insert_async(
            kit.db.dal.hub_users,
            email="alice@acme.test",
            username="alice",
            password_hash="$2b$04$x",
            is_active=True,
            email_verified=True,
        )
        public_id = await kit.create(kit.oidc_body())
        response = await kit.oidc_login(public_id)
        assert kit.error_reason(response) == "sso_account_conflict"
        assert await kit.count_identities() == 0  # no link was created
        users = await kit.users_by_email("alice@acme.test")
        assert [int(u.id) for u in users] == [int(victim_id)]  # untouched, still alone

    async def test_jit_user_is_not_a_tenant_admin_even_if_the_idp_claims_admin(
        self, kit: Kit
    ) -> None:
        public_id = await kit.create(kit.oidc_body())
        response = await kit.oidc_login(
            public_id, roles=["admin"], groups=["tenant-admins"], is_admin=True
        )
        payload = await kit.redeem(response)
        scopes = set(payload["scope"].split())
        assert not scopes & {"tenant:admin", "auth.sso:admin", "users:admin"}
        assert payload["roles"] == []


class TestEntitlementAtLoginTime:
    async def test_downgrade_between_start_and_callback_blocks_the_login(
        self, kit: Kit, entitlements: Entitlements
    ) -> None:
        public_id = await kit.create(kit.oidc_body())
        kit.use_oidc_idp()
        url = (await (await kit.start(public_id)).get_json())["redirectUrl"]
        code, state = kit.oidc.authorize(url)
        entitlements.set_tier("free")
        response = await kit.client.get(
            f"/api/v1/auth/sso/{public_id}/callback", query_string={"code": code, "state": state}
        )
        assert kit.error_reason(response) == "sso_not_entitled"
        assert await kit.users_by_email("alice@acme.test") == []

    async def test_start_is_refused_for_an_unentitled_tenant(
        self, kit: Kit, entitlements: Entitlements
    ) -> None:
        public_id = await kit.create(kit.oidc_body())
        entitlements.set_tier("free")
        response = await kit.start(public_id)
        assert response.status_code == 402
        assert (await response.get_json())["error"]["code"] == "FEATURE_NOT_ENABLED"

    async def test_posthog_flag_off_stops_logins(
        self, kit: Kit, entitlements: Entitlements
    ) -> None:
        public_id = await kit.create(kit.oidc_body())
        entitlements.flag_off("waddles.auth.sso_saml")
        redirect = await kit.client.get(f"/api/v1/auth/sso/{public_id}/login")
        assert kit.error_reason(redirect) == "sso_not_entitled"

    async def test_disabled_connection_cannot_start(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        await kit.patch_connection(public_id, {"enabled": False})
        assert (await kit.start(public_id)).status_code == 404

    async def test_login_for_an_inactive_tenant_is_refused(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        await kit.db.update_async(kit.db.dal.tenants.slug == TENANT_SLUG, is_active=False)
        assert (await kit.start(public_id)).status_code == 404


class TestOperationalFailures:
    async def test_missing_encryption_key_fails_loudly_not_silently(
        self, kit: Kit, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        public_id = await kit.create(kit.oidc_body())
        monkeypatch.delenv("SSO_ENCRYPTION_KEY")
        with caplog.at_level(logging.ERROR):
            response = await kit.start(public_id)
        assert response.status_code == 503
        body = await response.get_json()
        assert body["error"]["code"] == "SSO_UNAVAILABLE"
        assert "SSO_ENCRYPTION_KEY" in body["error"]["message"]
        assert "err_code=sso_key_missing" in caplog.text

    async def test_rotated_key_makes_stored_secrets_undecryptable_and_login_fails_loudly(
        self, kit: Kit, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        public_id = await kit.create(kit.oidc_body())
        kit.use_oidc_idp()
        url = (await (await kit.start(public_id)).get_json())["redirectUrl"]
        code, state = kit.oidc.authorize(url)
        monkeypatch.setenv("SSO_ENCRYPTION_KEY", "cd" * 32)
        # The binder cookie is HMAC(state) under the (now different) key -> session mismatch;
        # the point is that it FAILS CLOSED and LOUD, never falls back.
        with caplog.at_level(logging.ERROR):
            response = await kit.client.get(
                f"/api/v1/auth/sso/{public_id}/callback",
                query_string={"code": code, "state": state},
            )
        assert kit.error_reason(response) in {"sso_session_mismatch", "sso_unavailable"}
        assert await kit.users_by_email("alice@acme.test") == []
        assert "sso.login.failed" in caplog.text

    async def test_state_store_outage_is_a_502_at_start(
        self, kit: Kit, fake_redis: FakeRedis
    ) -> None:
        public_id = await kit.create(kit.oidc_body())
        fake_redis.fail = True
        response = await kit.start(public_id)
        assert response.status_code == 502

    async def test_missing_schema_is_a_loud_503_not_a_500(self, kit: Kit) -> None:
        # Simulate hub-api running ahead of migration 0049: the table is not reflected.
        import types

        kit.app.config["install_dal"] = types.SimpleNamespace()  # no SSO tables reflected
        response = await kit.client.get("/api/v1/tenant/sso/connections", headers=kit.headers())
        assert response.status_code == 503
        assert "0049_sso_connections" in (await response.get_json())["error"]["message"]


class TestConcurrency:
    async def test_two_simultaneous_first_logins_of_one_subject_create_one_user(
        self, kit: Kit
    ) -> None:
        public_id = await kit.create(kit.oidc_body())
        kit.use_oidc_idp()

        async def attempt() -> Any:
            browser = kit.app.test_client()
            url = (await (await browser.get(f"/api/v1/auth/sso/{public_id}/start")).get_json())[
                "redirectUrl"
            ]
            code, state = kit.oidc.authorize(url)
            return await browser.get(
                f"/api/v1/auth/sso/{public_id}/callback",
                query_string={"code": code, "state": state},
            )

        results = await asyncio.gather(attempt(), attempt())
        assert len(await kit.users_by_email("alice@acme.test")) == 1
        assert await kit.count_identities() == 1
        assert sorted(r.status_code for r in results) in ([303, 303], [302, 303])


class TestGoogle:
    @pytest.fixture
    def google(self, kit: Kit) -> FakeOidcIdp:
        idp = FakeOidcIdp(issuer="https://accounts.google.com", claims={"hd": "acme.test"})
        kit.oidc = idp
        return idp

    async def test_workspace_login_end_to_end(self, kit: Kit, google: FakeOidcIdp) -> None:
        public_id = await kit.create(kit.google_body())
        payload = await kit.redeem(await kit.oidc_login(public_id))
        assert payload["tenant"] == TENANT_SLUG
        assert len(await kit.users_by_email("alice@acme.test")) == 1

    async def test_authorize_url_carries_the_hosted_domain_hint(
        self, kit: Kit, google: FakeOidcIdp
    ) -> None:
        public_id = await kit.create(kit.google_body())
        kit.use_oidc_idp()
        body = await (await kit.start(public_id)).get_json()
        assert parse_qs(urlparse(body["redirectUrl"]).query)["hd"] == ["acme.test"]
        assert urlparse(body["redirectUrl"]).netloc == "accounts.google.com"

    async def test_account_from_another_workspace_is_refused(
        self, kit: Kit, google: FakeOidcIdp
    ) -> None:
        public_id = await kit.create(kit.google_body())
        response = await kit.oidc_login(public_id, hd="other-workspace.test", email="x@acme.test")
        assert kit.error_reason(response) == "sso_domain_not_allowed"

    async def test_personal_gmail_account_without_hd_is_refused(
        self, kit: Kit, google: FakeOidcIdp
    ) -> None:
        public_id = await kit.create(kit.google_body())
        response = await kit.oidc_login(public_id, hd=None, email="alice@acme.test")
        assert kit.error_reason(response) == "sso_domain_not_allowed"

    async def test_unverified_google_email_is_refused_even_for_a_returning_user(
        self, kit: Kit, google: FakeOidcIdp
    ) -> None:
        public_id = await kit.create(kit.google_body())
        await kit.redeem(await kit.oidc_login(public_id))
        response = await kit.oidc_login(public_id, email_verified=False)
        assert kit.error_reason(response) == "sso_email_unverified"

    async def test_shared_platform_client_is_used_when_the_tenant_brings_none(
        self, kit: Kit, google: FakeOidcIdp, sso_app: Any
    ) -> None:
        google.client_id, google.client_secret = "platform-gid", "platform-gsecret"  # noqa: S105
        sso_app.config["sso_settings"] = SsoSettings(
            google_client_id="platform-gid",
            google_client_secret="platform-gsecret",  # noqa: S106
        )
        public_id = await kit.create(
            {
                "protocol": "google",
                "displayName": "Shared",
                "enabled": True,
                "allowedDomains": ["acme.test"],
            }
        )
        assert (await kit.redeem(await kit.oidc_login(public_id)))["sub"]

    async def test_professional_tier_is_enough_for_google_but_not_generic_oidc(
        self, kit: Kit, google: FakeOidcIdp, entitlements: Entitlements
    ) -> None:
        entitlements.set_tier("professional")
        public_id = await kit.create(kit.google_body())
        assert (await kit.redeem(await kit.oidc_login(public_id)))["sub"]
        assert (await kit.post_connection(kit.oidc_body())).status_code == 402
