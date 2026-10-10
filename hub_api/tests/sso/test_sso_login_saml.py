"""SAML 2.0 login end to end: SP-initiated redirect -> IdP -> POST to the ACS.

The fake IdP signs real SAML Responses; hub-api's blueprints, state store, binder cookie,
signature verification, replay cache, JIT provisioning and session minting all run for real.
"""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import parse_qs, urlparse
from xml.etree.ElementTree import Element  # noqa: S405 -- type annotation only

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from defusedxml.ElementTree import fromstring as safe_fromstring

from services import sso_saml
from tests.conftest import TENANT_SLUG
from tests.sso.conftest import Entitlements
from tests.sso.idp_fakes import NS_SAML, FakeSamlIdp, make_cert, remove_signatures, tamper
from tests.sso.kit import Kit

PERSISTENT = "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent"


class TestHappyPath:
    async def test_sp_initiated_login_provisions_user_and_mints_tenant_session(
        self, kit: Kit
    ) -> None:
        public_id = await kit.create(kit.saml_body())
        response = await kit.saml_login(public_id)
        assert response.status_code == 303
        payload = await kit.redeem(response)
        assert payload["tenant"] == TENANT_SLUG
        users = await kit.users_by_email("alice@acme.test")
        assert len(users) == 1
        assert users[0].display_name == "Alice Example"
        assert users[0].email_verified is True
        assert payload["sub"] == str(users[0].id)
        assert await kit.count_identities() == 1

    @pytest.mark.parametrize("sign", ["response", "assertion", "both"])
    async def test_all_valid_signing_modes(self, kit: Kit, sign: str) -> None:
        public_id = await kit.create(kit.saml_body())
        response = await kit.saml_login(public_id, sign=sign)
        assert response.status_code == 303, sign

    async def test_returning_user_is_matched_by_nameid(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        first = await kit.redeem(await kit.saml_login(public_id))
        second = await kit.redeem(await kit.saml_login(public_id))
        assert first["sub"] == second["sub"]
        assert await kit.count_identities() == 1

    async def test_persistent_nameid_with_email_attribute(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        response = await kit.saml_login(
            public_id,
            name_id="persistent-opaque-1",
            name_id_format=PERSISTENT,
            attributes={"mail": ["carol@acme.test"], "displayName": ["Carol"]},
        )
        payload = await kit.redeem(response)
        assert (await kit.users_by_email("carol@acme.test"))[0].id == int(payload["sub"])

    async def test_start_sets_a_cross_site_capable_binder_cookie(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        response = await kit.start(public_id)
        cookie = [
            c for c in response.headers.getlist("Set-Cookie") if c.startswith("wb_sso_bind=")
        ][0]
        # The ACS receives a cross-site POST, which a SameSite=Lax cookie would not accompany.
        assert "SameSite=None" in cookie
        assert "Secure" in cookie
        assert "HttpOnly" in cookie

    async def test_authn_request_targets_the_configured_sso_url_and_this_connections_acs(
        self, kit: Kit
    ) -> None:
        public_id = await kit.create(kit.saml_body())
        authn, relay = await kit.saml_start(public_id)
        assert authn.destination == kit.saml.sso_url
        assert authn.acs_url == f"https://hub.example.com/api/v1/auth/sso/{public_id}/acs"
        assert authn.issuer == f"https://hub.example.com/api/v1/auth/sso/{public_id}/metadata"
        assert relay == authn.relay_state

    async def test_certificate_rotation_via_metadata_reimport(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        rotated = FakeSamlIdp(key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
        await kit.patch_connection(public_id, {"idpMetadataXml": rotated.metadata_xml()})
        # Old key no longer trusted...
        old = await kit.saml_login(public_id)
        assert kit.error_reason(old) == "sso_invalid_response"
        # ...new key is.
        kit.saml = rotated
        assert (await kit.saml_login(public_id)).status_code == 303


class TestSpMetadataEndpoint:
    async def test_public_metadata_for_a_saml_connection(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        response = await kit.client.get(f"/api/v1/auth/sso/{public_id}/metadata")  # no auth header
        assert response.status_code == 200
        assert response.mimetype == "application/samlmetadata+xml"
        root = safe_fromstring(await response.get_data())
        assert (
            root.get("entityID") == f"https://hub.example.com/api/v1/auth/sso/{public_id}/metadata"
        )
        acs = root.find(".//{urn:oasis:names:tc:SAML:2.0:metadata}AssertionConsumerService")
        assert acs.get("Location").endswith(f"/{public_id}/acs")

    async def test_metadata_is_available_for_a_draft_so_the_idp_can_be_configured_first(
        self, kit: Kit
    ) -> None:
        public_id = await kit.create(
            {"protocol": "saml", "displayName": "D", "allowedDomains": ["acme.test"]}
        )
        assert (await kit.client.get(f"/api/v1/auth/sso/{public_id}/metadata")).status_code == 200

    async def test_metadata_does_not_leak_idp_configuration(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        text = await (await kit.client.get(f"/api/v1/auth/sso/{public_id}/metadata")).get_data(
            as_text=True
        )
        assert kit.saml.entity_id not in text
        assert "BEGIN CERTIFICATE" not in text
        assert "acme.test" not in text

    async def test_non_saml_and_unknown_connections_are_404(self, kit: Kit) -> None:
        oidc_id = await kit.create(kit.oidc_body())
        assert (await kit.client.get(f"/api/v1/auth/sso/{oidc_id}/metadata")).status_code == 404
        assert (await kit.client.get("/api/v1/auth/sso/nope/metadata")).status_code == 404


class TestAcsIntegrity:
    async def test_acs_rejects_get(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        assert (await kit.client.get(f"/api/v1/auth/sso/{public_id}/acs")).status_code == 405

    @pytest.mark.parametrize("fields", [{}, {"SAMLResponse": "x"}, {"RelayState": "y"}])
    async def test_missing_form_fields(self, kit: Kit, fields: dict[str, str]) -> None:
        public_id = await kit.create(kit.saml_body())
        response = await kit.client.post(f"/api/v1/auth/sso/{public_id}/acs", form=fields)
        assert kit.error_reason(response) == "sso_denied"

    async def test_unsolicited_idp_initiated_post_without_a_known_state_is_refused(
        self, kit: Kit
    ) -> None:
        public_id = await kit.create(kit.saml_body())
        authn, _ = await kit.saml_start(public_id)
        raw = kit.saml.build_response(authn)
        response = await kit.post_acs(public_id, raw, "relay-state-we-never-issued")
        assert kit.error_reason(response) == "sso_session_mismatch"
        assert await kit.users_by_email("alice@acme.test") == []

    async def test_login_csrf_response_posted_by_a_browser_that_did_not_start_the_flow(
        self, kit: Kit
    ) -> None:
        public_id = await kit.create(kit.saml_body())
        attacker = kit.app.test_client()
        started = await attacker.get(f"/api/v1/auth/sso/{public_id}/start")
        authn = kit.saml.parse_authn_request((await started.get_json())["redirectUrl"])
        raw = kit.saml.build_response(authn, name_id="mallory@acme.test")
        victim = kit.app.test_client()  # no binder cookie
        response = await victim.post(
            f"/api/v1/auth/sso/{public_id}/acs",
            form={"SAMLResponse": kit.saml.encode(raw), "RelayState": authn.relay_state},
        )
        assert kit.error_reason(response) == "sso_session_mismatch"
        assert await kit.users_by_email("mallory@acme.test") == []

    async def test_state_is_single_use_so_a_posted_response_cannot_be_replayed(
        self, kit: Kit
    ) -> None:
        public_id = await kit.create(kit.saml_body())
        authn, relay = await kit.saml_start(public_id)
        raw = kit.saml.build_response(authn)
        assert (await kit.post_acs(public_id, raw, relay)).status_code == 303
        replay = await kit.post_acs(public_id, raw, relay)
        assert kit.error_reason(replay) == "sso_session_mismatch"
        assert await kit.count_identities() == 1

    async def test_same_assertion_id_cannot_be_reused_across_flows(
        self, kit: Kit, caplog: pytest.LogCaptureFixture
    ) -> None:
        public_id = await kit.create(kit.saml_body())
        authn1, relay1 = await kit.saml_start(public_id)
        assert (
            await kit.post_acs(
                public_id, kit.saml.build_response(authn1, assertion_id="_fixed"), relay1
            )
        ).status_code == 303
        authn2, relay2 = await kit.saml_start(public_id)
        with caplog.at_level("INFO"):
            response = await kit.post_acs(
                public_id, kit.saml.build_response(authn2, assertion_id="_fixed"), relay2
            )
        assert kit.error_reason(response) == "sso_invalid_response"
        assert "err_code=saml_replay" in caplog.text

    async def test_response_for_one_flow_posted_with_another_flows_state(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        authn1, _ = await kit.saml_start(public_id)
        _, relay2 = await kit.saml_start(public_id)
        raw = kit.saml.build_response(authn1)  # answers request 1
        response = await kit.post_acs(public_id, raw, relay2)  # but presented under state 2
        assert kit.error_reason(response) == "sso_invalid_response"

    async def test_a_response_cannot_be_used_on_a_different_connection(self, kit: Kit) -> None:
        one = await kit.create(kit.saml_body(displayName="One"))
        two = await kit.create(kit.saml_body(displayName="Two"))
        authn, relay = await kit.saml_start(one)
        raw = kit.saml.build_response(authn)
        response = await kit.post_acs(two, raw, relay)
        assert kit.error_reason(response) == "sso_session_mismatch"


class TestForgeries:
    async def _attack(self, kit: Kit, build: Any) -> Any:
        public_id = await kit.create(kit.saml_body())
        authn, relay = await kit.saml_start(public_id)
        response = await kit.post_acs(public_id, build(authn), relay)
        assert kit.error_reason(response) == "sso_invalid_response", kit.location(response)
        assert await kit.users_by_email("alice@acme.test") == []
        assert await kit.users_by_email("admin@acme.test") == []
        assert await kit.count_identities() == 0
        return response

    async def test_unsigned(self, kit: Kit) -> None:
        await self._attack(kit, lambda a: kit.saml.build_response(a, sign="none"))

    async def test_signed_by_an_attacker_key(self, kit: Kit) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = make_cert(key, cn="attacker")
        await self._attack(
            kit, lambda a: kit.saml.build_response(a, signing_key=key, signing_cert=cert)
        )

    async def test_nameid_tampered_after_signing(self, kit: Kit) -> None:
        def build(authn: Any) -> bytes:
            raw = kit.saml.build_response(authn, name_id="alice@acme.test")
            return tamper(
                raw, lambda r: setattr(r.find(f".//{{{NS_SAML}}}NameID"), "text", "admin@acme.test")
            )

        await self._attack(kit, build)

    async def test_signature_wrapping_with_an_extra_forged_assertion(self, kit: Kit) -> None:
        def build(authn: Any) -> bytes:
            raw = kit.saml.build_response(authn, sign="assertion", name_id="alice@acme.test")

            def mutate(root: Element) -> None:
                forged = copy.deepcopy(root.find(f"{{{NS_SAML}}}Assertion"))
                remove_signatures(forged)
                forged.set("ID", "_forged")
                forged.find(f".//{{{NS_SAML}}}NameID").text = "admin@acme.test"
                root.insert(0, forged)

            return tamper(raw, mutate)

        await self._attack(kit, build)

    async def test_wrong_audience(self, kit: Kit) -> None:
        await self._attack(
            kit, lambda a: kit.saml.build_response(a, audience="https://other-sp.example.com")
        )

    async def test_expired(self, kit: Kit) -> None:
        await self._attack(
            kit,
            lambda a: kit.saml.build_response(
                a, not_on_or_after_offset=-3600, conf_not_on_or_after_offset=-3600
            ),
        )

    async def test_transient_nameid(self, kit: Kit) -> None:
        await self._attack(
            kit,
            lambda a: kit.saml.build_response(
                a, name_id="x", name_id_format="urn:oasis:names:tc:SAML:2.0:nameid-format:transient"
            ),
        )

    async def test_xxe_payload(self, kit: Kit) -> None:
        await self._attack(
            kit,
            lambda a: (
                b'<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">&x;</samlp:Response>'
            ),
        )

    async def test_failed_authentication_status(self, kit: Kit) -> None:
        await self._attack(
            kit,
            lambda a: kit.saml.build_response(
                a, status="urn:oasis:names:tc:SAML:2.0:status:AuthnFailed"
            ),
        )

    async def test_garbage(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        _, relay = await kit.saml_start(public_id)
        for payload in ("!!!notbase64!!!", "AAAA", ""):
            authn, relay = await kit.saml_start(public_id)
            response = await kit.post_acs(public_id, payload, relay)
            assert kit.error_reason(response) in {"sso_invalid_response", "sso_denied"}


class TestProvisioningPolicy:
    async def test_email_outside_allowed_domains(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        response = await kit.saml_login(public_id, name_id="eve@evil.test")
        assert kit.error_reason(response) == "sso_domain_not_allowed"
        assert await kit.users_by_email("eve@evil.test") == []

    async def test_no_email_for_a_new_account(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        response = await kit.saml_login(
            public_id,
            name_id="opaque-1",
            name_id_format=PERSISTENT,
            attributes={"displayName": ["No Mail"]},
        )
        assert kit.error_reason(response) == "sso_email_required"

    async def test_existing_account_with_same_email_is_not_adopted(self, kit: Kit) -> None:
        await kit.db.insert_async(
            kit.db.dal.hub_users,
            email="alice@acme.test",
            username="alice",
            password_hash="$2b$04$x",
            is_active=True,
            email_verified=True,
        )
        public_id = await kit.create(kit.saml_body())
        response = await kit.saml_login(public_id)
        assert kit.error_reason(response) == "sso_account_conflict"
        assert await kit.count_identities() == 0

    async def test_domain_removal_cuts_off_returning_users(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        await kit.redeem(await kit.saml_login(public_id))
        await kit.patch_connection(public_id, {"allowedDomains": ["other.example"]})
        assert kit.error_reason(await kit.saml_login(public_id)) == "sso_domain_not_allowed"


class TestEntitlement:
    async def test_downgrade_between_start_and_acs_blocks_the_login(
        self, kit: Kit, entitlements: Entitlements
    ) -> None:
        public_id = await kit.create(kit.saml_body())
        authn, relay = await kit.saml_start(public_id)
        entitlements.set_tier("professional")  # Google-only tier: SAML is Enterprise
        response = await kit.post_acs(public_id, kit.saml.build_response(authn), relay)
        assert kit.error_reason(response) == "sso_not_entitled"
        assert await kit.users_by_email("alice@acme.test") == []

    async def test_professional_tier_cannot_start_saml(
        self, kit: Kit, entitlements: Entitlements
    ) -> None:
        public_id = await kit.create(kit.saml_body())
        entitlements.set_tier("professional")
        assert (await kit.start(public_id)).status_code == 402
        options = await (
            await kit.client.get(f"/api/v1/auth/sso/options?tenant={TENANT_SLUG}")
        ).get_json()
        assert options["options"] == []

    async def test_options_lists_saml_for_enterprise(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        options = await (
            await kit.client.get(f"/api/v1/auth/sso/options?tenant={TENANT_SLUG}")
        ).get_json()
        assert options["options"] == [
            {"id": public_id, "displayName": "Acme SAML", "protocol": "saml"}
        ]


def test_sp_entity_ids_are_unique_per_connection() -> None:
    from config import HubAPIConfig  # noqa: F401  (import check only)
    from services.sso_service import sp_urls
    from tests.sso.conftest import hub_config

    a, b = sp_urls(hub_config(), "id-a"), sp_urls(hub_config(), "id-b")
    assert a.entity_id != b.entity_id
    assert a.acs_url != b.acs_url
    assert sso_saml.BINDING_POST.endswith("HTTP-POST")
    assert parse_qs(urlparse(a.login_url).query) == {}
