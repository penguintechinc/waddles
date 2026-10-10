"""Tenant-admin API (`/api/v1/tenant/sso/connections`): CRUD, validation, entitlement, IDOR.

Drives the real Quart blueprints through the real tenant -> scope -> entitlement chain.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from blueprints.v1.sso import SsoConnectionDTO
from services import sso_crypto
from services.sso_settings import SsoSettings
from tests.sso.conftest import Entitlements
from tests.sso.idp_fakes import FakeSamlIdp, make_cert, pem_cert
from tests.sso.kit import Kit

DTO_FIELDS = set(SsoConnectionDTO.__dataclass_fields__)  # noqa: SLF001 - exact-shape contract


def _json(response: Any) -> Any:
    return response.get_json()


class TestCreateAndRead:
    async def test_create_complete_oidc_connection(self, kit: Kit) -> None:
        response = await kit.post_connection(kit.oidc_body())
        assert response.status_code == 201
        body = await response.get_json()
        conn = body["connection"]
        assert body["success"] is True
        assert conn["protocol"] == "oidc"
        assert conn["enabled"] is True
        assert conn["entitled"] is True
        assert conn["issuer"] == kit.oidc.issuer
        assert conn["discoveryUrl"] == kit.oidc.issuer + "/.well-known/openid-configuration"
        assert conn["clientId"] == kit.oidc.client_id
        assert conn["hasClientSecret"] is True
        assert conn["allowedDomains"] == ["acme.test"]
        assert conn["scopes"] == ["openid", "email", "profile"]
        assert conn["callbackUrl"] == (
            f"https://hub.example.com/api/v1/auth/sso/{conn['id']}/callback"
        )
        assert conn["loginUrl"].endswith(f"/{conn['id']}/login")
        assert conn["acsUrl"] is None
        assert conn["metadataUrl"] is None

    async def test_response_exposes_exactly_the_dto_fields_and_no_secret(self, kit: Kit) -> None:
        response = await kit.post_connection(kit.oidc_body())
        text = await response.get_data(as_text=True)
        conn = json.loads(text)["connection"]
        assert set(conn) == DTO_FIELDS
        assert str(kit.oidc.client_secret) not in text
        assert "v1:" not in text  # no ciphertext either
        assert "secret_ciphertext" not in text

    async def test_secret_is_encrypted_at_rest_with_the_row_bound_aad(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        row = await kit.connection_row(public_id)
        assert row.secret_ciphertext.startswith("v1:")
        assert str(kit.oidc.client_secret) not in row.secret_ciphertext
        assert (
            sso_crypto.decrypt_secret(row.secret_ciphertext, aad=public_id)
            == kit.oidc.client_secret
        )
        other = await kit.create(kit.oidc_body(displayName="Second"))
        with pytest.raises(Exception):  # noqa: B017, PT011 - SsoConfigError: ciphertext is row-bound
            sso_crypto.decrypt_secret(row.secret_ciphertext, aad=other)

    async def test_config_column_never_contains_the_secret(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        row = await kit.connection_row(public_id)
        assert str(kit.oidc.client_secret) not in json.dumps(row.config)

    async def test_create_is_disabled_by_default(self, kit: Kit) -> None:
        body = kit.oidc_body()
        del body["enabled"]
        conn = (await (await kit.post_connection(body)).get_json())["connection"]
        assert conn["enabled"] is False

    async def test_draft_saml_connection_exposes_acs_and_metadata_urls(self, kit: Kit) -> None:
        response = await kit.post_connection(
            {"protocol": "saml", "displayName": "Draft", "allowedDomains": ["acme.test"]}
        )
        conn = (await response.get_json())["connection"]
        assert conn["enabled"] is False
        assert conn["idpEntityId"] is None
        assert conn["idpCertificateCount"] == 0
        assert conn["acsUrl"].endswith(f"/{conn['id']}/acs")
        assert conn["metadataUrl"].endswith(f"/{conn['id']}/metadata")
        assert conn["spEntityId"] == conn["metadataUrl"]

    async def test_draft_oidc_connection_without_idp_details(self, kit: Kit) -> None:
        response = await kit.post_connection(
            {"protocol": "oidc", "displayName": "Draft", "allowedDomains": ["acme.test"]}
        )
        assert response.status_code == 201
        conn = (await response.get_json())["connection"]
        assert conn["issuer"] is None
        assert conn["callbackUrl"] is not None

    async def test_list_returns_only_the_callers_tenant(self, kit: Kit) -> None:
        mine = await kit.create(kit.oidc_body())
        response = await kit.client.get("/api/v1/tenant/sso/connections", headers=kit.headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert [c["id"] for c in body["connections"]] == [mine]
        other = await kit.client.get(
            "/api/v1/tenant/sso/connections", headers=kit.headers(tenant="other-corp")
        )
        assert (await other.get_json())["connections"] == []

    async def test_get_one(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        response = await kit.get_connection(public_id)
        assert response.status_code == 200
        assert (await response.get_json())["connection"]["id"] == public_id

    async def test_google_defaults_hosted_domain_from_the_single_allowed_domain(
        self, kit: Kit
    ) -> None:
        conn = (await (await kit.post_connection(kit.google_body())).get_json())["connection"]
        assert conn["protocol"] == "google"
        assert conn["hostedDomain"] == "acme.test"
        assert conn["issuer"] == "https://accounts.google.com"
        assert conn["usePlatformClient"] is False
        assert conn["scopes"] == ["openid", "email", "profile"]

    async def test_saml_connection_from_pasted_metadata(self, kit: Kit) -> None:
        conn = (await (await kit.post_connection(kit.saml_body())).get_json())["connection"]
        assert conn["idpEntityId"] == kit.saml.entity_id
        assert conn["idpSsoUrl"] == kit.saml.sso_url
        assert conn["idpCertificateCount"] == 1
        assert conn["nameIdFormat"] == "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"

    async def test_saml_connection_from_explicit_fields(self, kit: Kit) -> None:
        body = {
            "protocol": "saml",
            "displayName": "Explicit",
            "enabled": True,
            "allowedDomains": ["acme.test"],
            "idpEntityId": kit.saml.entity_id,
            "idpSsoUrl": kit.saml.sso_url,
            "idpCertificates": [kit.saml.cert_pem],
            "nameIdFormat": "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
            "emailAttribute": "workEmail",
            "nameAttribute": "fullName",
            "forceAuthn": True,
        }
        conn = (await (await kit.post_connection(body)).get_json())["connection"]
        assert conn["emailAttribute"] == "workEmail"
        assert conn["nameAttribute"] == "fullName"
        assert conn["forceAuthn"] is True


class TestCreateValidation:
    @pytest.mark.parametrize(
        ("mutate", "status"),
        [
            (lambda b: b.pop("displayName"), 400),
            (lambda b: b.pop("allowedDomains"), 400),
            (lambda b: b.pop("protocol"), 400),
            (lambda b: b.update(unexpected="field"), 400),
            (lambda b: b.update(protocol="ldap"), 400),
            (lambda b: b.update(displayName="   "), 400),
            (lambda b: b.update(displayName="x" * 101), 400),
            (lambda b: b.update(displayName="bad\x07name"), 400),
            (lambda b: b.update(allowedDomains=[]), 400),
            (lambda b: b.update(allowedDomains=["not a domain"]), 400),
            (lambda b: b.update(allowedDomains=["localhost"]), 400),
            (lambda b: b.update(allowedDomains=["gmail.com"]), 400),
            (lambda b: b.update(allowedDomains=[f"d{i}.test" for i in range(21)]), 400),
            (lambda b: b.update(issuer="http://idp.example.com"), 400),
            (lambda b: b.update(issuer="https://u:p@idp.example.com"), 400),
            (lambda b: b.update(issuer="https://10.0.0.9"), 422),
            (lambda b: b.update(issuer="https://169.254.169.254"), 422),
            (lambda b: b.update(discoveryUrl="http://idp.example.com/x"), 400),
            (lambda b: b.pop("clientId"), 400),
            (lambda b: b.pop("issuer"), 400),
            (lambda b: b.update(scopes=["email"]), 400),
            (lambda b: b.update(scopes=["openid", "bad scope"]), 400),
            (lambda b: b.update(clientSecret="s" * 1025), 400),
        ],
    )
    async def test_invalid_oidc_bodies(self, kit: Kit, mutate: Any, status: int) -> None:
        body = kit.oidc_body()
        mutate(body)
        response = await kit.post_connection(body)
        assert response.status_code == status, await response.get_data(as_text=True)

    async def test_duplicate_display_name_is_a_409(self, kit: Kit) -> None:
        await kit.create(kit.oidc_body())
        response = await kit.post_connection(kit.oidc_body())
        assert response.status_code == 409

    async def test_same_display_name_in_another_tenant_is_fine(self, kit: Kit) -> None:
        await kit.create(kit.oidc_body())
        response = await kit.post_connection(kit.oidc_body(), tenant="other-corp")
        assert response.status_code == 201

    async def test_private_idp_host_allowed_only_via_operator_allowlist(
        self, kit: Kit, sso_app: Any
    ) -> None:
        body = kit.oidc_body(issuer="https://private-idp.corp.test")
        assert (await kit.post_connection(body)).status_code == 422
        sso_app.config["sso_settings"] = SsoSettings(
            allowed_private_hosts=frozenset({"private-idp.corp.test"})
        )
        assert (await kit.post_connection(body)).status_code == 201

    @pytest.mark.parametrize(
        ("mutate", "status"),
        [
            (lambda b: b.pop("idpMetadataXml"), 400),
            (lambda b: b.update(idpMetadataXml="<not-xml"), 422),
            (lambda b: b.update(idpMetadataXml="x" * 512_001), 400),
            (lambda b: b.update(idpMetadataXml="<!DOCTYPE a [<!ENTITY x 'y'>]><a/>"), 422),
            (lambda b: b.update(nameIdFormat="urn:bogus"), 400),
        ],
    )
    async def test_invalid_saml_bodies(self, kit: Kit, mutate: Any, status: int) -> None:
        body = kit.saml_body()
        mutate(body)
        response = await kit.post_connection(body)
        assert response.status_code == status, await response.get_data(as_text=True)

    async def test_saml_explicit_fields_need_all_three_to_enable(self, kit: Kit) -> None:
        base = {
            "protocol": "saml",
            "displayName": "E",
            "enabled": True,
            "allowedDomains": ["acme.test"],
        }
        for missing in ("idpEntityId", "idpSsoUrl", "idpCertificates"):
            body = {
                **base,
                "idpEntityId": kit.saml.entity_id,
                "idpSsoUrl": kit.saml.sso_url,
                "idpCertificates": [kit.saml.cert_pem],
            }
            del body[missing]
            assert (await kit.post_connection(body)).status_code == 400, missing

    async def test_expired_and_junk_certificates_are_rejected_at_save_time(self, kit: Kit) -> None:
        expired = pem_cert(make_cert(FakeSamlIdp().key, expired=True))
        for cert in (expired, "not a cert"):
            body = kit.saml_body(displayName=f"c-{abs(hash(cert))}")
            del body["idpMetadataXml"]
            body.update(
                idpEntityId=kit.saml.entity_id, idpSsoUrl=kit.saml.sso_url, idpCertificates=[cert]
            )
            assert (await kit.post_connection(body)).status_code == 422

    async def test_too_many_certificates(self, kit: Kit) -> None:
        body = kit.saml_body()
        del body["idpMetadataXml"]
        body.update(
            idpEntityId=kit.saml.entity_id,
            idpSsoUrl=kit.saml.sso_url,
            idpCertificates=[kit.saml.cert_pem] * 6,
        )
        assert (await kit.post_connection(body)).status_code == 400

    async def test_non_https_sso_url_is_rejected(self, kit: Kit) -> None:
        body = kit.saml_body()
        del body["idpMetadataXml"]
        body.update(
            idpEntityId=kit.saml.entity_id,
            idpSsoUrl="http://saml-idp.example.com/sso",
            idpCertificates=[kit.saml.cert_pem],
        )
        assert (await kit.post_connection(body)).status_code == 400
        body["idpSsoUrl"] = "javascript:alert(1)"
        assert (await kit.post_connection(body)).status_code == 400


class TestGoogleValidation:
    async def test_multi_domain_requires_hosted_domain(self, kit: Kit) -> None:
        body = kit.google_body(allowedDomains=["acme.test", "acme.example"])
        assert (await kit.post_connection(body)).status_code == 400
        body["hostedDomain"] = "acme.test"
        assert (await kit.post_connection(body)).status_code == 201

    async def test_hosted_domain_must_be_listed_and_owned(self, kit: Kit) -> None:
        assert (
            await kit.post_connection(kit.google_body(hostedDomain="elsewhere.test"))
        ).status_code == 400
        assert (
            await kit.post_connection(
                kit.google_body(
                    displayName="g2", hostedDomain="gmail.com", allowedDomains=["acme.test"]
                )
            )
        ).status_code == 400

    async def test_enabling_needs_a_client_secret_with_the_client_id(self, kit: Kit) -> None:
        body = kit.google_body()
        del body["clientSecret"]
        assert (await kit.post_connection(body)).status_code == 400

    async def test_platform_client_requires_operator_configuration(
        self, kit: Kit, sso_app: Any
    ) -> None:
        body = {
            "protocol": "google",
            "displayName": "Shared",
            "enabled": True,
            "allowedDomains": ["acme.test"],
        }
        assert (await kit.post_connection(body)).status_code == 422
        sso_app.config["sso_settings"] = SsoSettings(
            google_client_id="platform-gid",
            google_client_secret="platform-gsecret",  # noqa: S106
        )
        response = await kit.post_connection(body)
        assert response.status_code == 201
        conn = (await response.get_json())["connection"]
        assert conn["usePlatformClient"] is True
        assert conn["clientId"] is None
        assert conn["hasClientSecret"] is False

    async def test_platform_client_cannot_be_combined_with_own_credentials(
        self, kit: Kit, sso_app: Any
    ) -> None:
        sso_app.config["sso_settings"] = SsoSettings(
            google_client_id="g",
            google_client_secret="s",  # noqa: S106
        )
        body = kit.google_body(usePlatformClient=True)
        assert (await kit.post_connection(body)).status_code == 400


class TestEntitlement:
    async def test_free_tier_cannot_create_any_connection(
        self, kit: Kit, entitlements: Entitlements
    ) -> None:
        entitlements.set_tier("free")
        for body in (kit.oidc_body(), kit.saml_body(), kit.google_body()):
            assert (await kit.post_connection(body)).status_code == 402

    async def test_professional_unlocks_google_but_not_saml_or_oidc(
        self, kit: Kit, entitlements: Entitlements
    ) -> None:
        entitlements.set_tier("professional")
        assert (await kit.post_connection(kit.google_body())).status_code == 201
        assert (await kit.post_connection(kit.oidc_body())).status_code == 402
        assert (await kit.post_connection(kit.saml_body())).status_code == 402

    async def test_enterprise_unlocks_everything(
        self, kit: Kit, entitlements: Entitlements
    ) -> None:
        entitlements.set_tier("enterprise")
        for body in (kit.oidc_body(), kit.saml_body(), kit.google_body()):
            assert (await kit.post_connection(body)).status_code == 201

    async def test_posthog_flag_off_blocks_even_an_entitled_tier(
        self, kit: Kit, entitlements: Entitlements
    ) -> None:
        entitlements.flag_off("waddles.auth.sso_saml")
        assert (await kit.post_connection(kit.oidc_body())).status_code == 402
        assert (await kit.post_connection(kit.google_body())).status_code == 201

    async def test_402_body_is_the_standard_error_envelope(
        self, kit: Kit, entitlements: Entitlements
    ) -> None:
        entitlements.set_tier("free")
        response = await kit.post_connection(kit.oidc_body())
        body = await response.get_json()
        assert body["success"] is False
        assert body["error"]["code"] == "FEATURE_NOT_ENABLED"

    async def test_downgrade_keeps_connections_visible_but_flags_them_unentitled(
        self, kit: Kit, entitlements: Entitlements
    ) -> None:
        public_id = await kit.create(kit.oidc_body())
        entitlements.set_tier("free")
        listed = await (
            await kit.client.get("/api/v1/tenant/sso/connections", headers=kit.headers())
        ).get_json()
        assert listed["connections"][0]["entitled"] is False
        got = await kit.get_connection(public_id)
        assert got.status_code == 200
        assert (await got.get_json())["connection"]["entitled"] is False

    async def test_downgraded_tenant_can_still_disable_and_delete(
        self, kit: Kit, entitlements: Entitlements
    ) -> None:
        public_id = await kit.create(kit.oidc_body())
        entitlements.set_tier("free")
        assert (await kit.patch_connection(public_id, {"enabled": False})).status_code == 200
        assert (await kit.delete_connection(public_id)).status_code == 200

    async def test_downgraded_tenant_cannot_edit_or_enable(
        self, kit: Kit, entitlements: Entitlements
    ) -> None:
        public_id = await kit.create(kit.oidc_body(enabled=False))
        entitlements.set_tier("free")
        assert (await kit.patch_connection(public_id, {"enabled": True})).status_code == 402
        assert (await kit.patch_connection(public_id, {"displayName": "New"})).status_code == 402


class TestAuthorization:
    async def test_no_token_is_401(self, kit: Kit) -> None:
        assert (await kit.client.get("/api/v1/tenant/sso/connections")).status_code == 401
        assert (await kit.client.post("/api/v1/tenant/sso/connections", json={})).status_code == 401

    @pytest.mark.parametrize("scope", ["tenant:read", "tenant:admin", "auth.sso:read", ""])
    async def test_wrong_or_missing_scope_is_403(self, kit: Kit, scope: str) -> None:
        response = await kit.client.get(
            "/api/v1/tenant/sso/connections", headers=kit.headers(scope=scope)
        )
        assert response.status_code == 403

    async def test_global_admin_wildcard_scope_is_accepted(self, kit: Kit) -> None:
        response = await kit.client.get(
            "/api/v1/tenant/sso/connections", headers=kit.headers(scope="*:admin")
        )
        assert response.status_code == 200

    async def test_unknown_tenant_in_token_is_rejected(self, kit: Kit) -> None:
        response = await kit.client.get(
            "/api/v1/tenant/sso/connections", headers=kit.headers(tenant="ghost-corp")
        )
        assert response.status_code in (401, 403, 404)

    async def test_cross_tenant_read_update_delete_are_404_not_403(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        attacker = {"tenant": "other-corp"}
        assert (await kit.get_connection(public_id, **attacker)).status_code == 404
        assert (
            await kit.patch_connection(public_id, {"enabled": False}, **attacker)
        ).status_code == 404
        assert (await kit.delete_connection(public_id, **attacker)).status_code == 404
        # ...and the victim's connection is untouched.
        still = (await (await kit.get_connection(public_id)).get_json())["connection"]
        assert still["enabled"] is True

    async def test_unknown_id_is_404(self, kit: Kit) -> None:
        assert (await kit.get_connection("00000000-0000-0000-0000-000000000000")).status_code == 404


class TestUpdate:
    async def test_rename_and_change_domains(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        response = await kit.patch_connection(
            public_id, {"displayName": "Renamed", "allowedDomains": ["acme.test", "acme.example"]}
        )
        assert response.status_code == 200
        conn = (await response.get_json())["connection"]
        assert conn["displayName"] == "Renamed"
        assert conn["allowedDomains"] == ["acme.test", "acme.example"]
        assert conn["issuer"] == kit.oidc.issuer  # untouched fields preserved

    async def test_secret_rotation_replaces_the_ciphertext(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        before = (await kit.connection_row(public_id)).secret_ciphertext
        await kit.patch_connection(public_id, {"clientSecret": "rotated-secret"})
        after = (await kit.connection_row(public_id)).secret_ciphertext
        assert after != before
        assert sso_crypto.decrypt_secret(after, aad=public_id) == "rotated-secret"

    async def test_omitting_the_secret_keeps_the_stored_one(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        before = (await kit.connection_row(public_id)).secret_ciphertext
        await kit.patch_connection(public_id, {"displayName": "Other"})
        assert (await kit.connection_row(public_id)).secret_ciphertext == before

    async def test_clear_client_secret_makes_it_a_public_client(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        response = await kit.patch_connection(public_id, {"clearClientSecret": True})
        assert (await response.get_json())["connection"]["hasClientSecret"] is False
        assert (await kit.connection_row(public_id)).secret_ciphertext is None

    async def test_draft_becomes_enableable_once_complete(self, kit: Kit) -> None:
        public_id = await kit.create(
            {"protocol": "oidc", "displayName": "Draft", "allowedDomains": ["acme.test"]}
        )
        assert (await kit.patch_connection(public_id, {"enabled": True})).status_code == 400
        response = await kit.patch_connection(
            public_id,
            {
                "issuer": kit.oidc.issuer,
                "clientId": kit.oidc.client_id,
                "clientSecret": "s",
                "enabled": True,
            },
        )
        assert response.status_code == 200
        assert (await response.get_json())["connection"]["enabled"] is True

    async def test_protocol_is_immutable(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        assert (await kit.patch_connection(public_id, {"protocol": "saml"})).status_code == 400

    async def test_saml_certificate_rotation_by_reimporting_metadata(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        rotated = FakeSamlIdp()
        response = await kit.patch_connection(public_id, {"idpMetadataXml": rotated.metadata_xml()})
        assert response.status_code == 200
        row = await kit.connection_row(public_id)
        assert row.config["idp_certs_pem"] == [rotated.cert_pem]

    async def test_disable_works_even_if_the_stored_certificate_has_since_expired(
        self, kit: Kit
    ) -> None:
        public_id = await kit.create(kit.saml_body())
        expired = pem_cert(make_cert(FakeSamlIdp().key, expired=True))
        table = kit.db.install_dal.sso_connections
        row = await kit.connection_row(public_id)
        await kit.db.install_dal(table.public_id == public_id).update(
            config={**row.config, "idp_certs_pem": [expired]}
        )
        # Editing anything else now fails loudly (the cert must be fixed)...
        assert (await kit.patch_connection(public_id, {"displayName": "x"})).status_code == 422
        # ...but switching the connection OFF must always work.
        response = await kit.patch_connection(public_id, {"enabled": False})
        assert response.status_code == 200
        assert (await response.get_json())["connection"]["enabled"] is False

    async def test_google_switch_from_platform_client_to_own_client(
        self, kit: Kit, sso_app: Any
    ) -> None:
        sso_app.config["sso_settings"] = SsoSettings(
            google_client_id="g",
            google_client_secret="s",  # noqa: S106
        )
        public_id = await kit.create(
            {
                "protocol": "google",
                "displayName": "G",
                "enabled": True,
                "allowedDomains": ["acme.test"],
            }
        )
        response = await kit.patch_connection(
            public_id, {"clientId": "own-id", "clientSecret": "own-secret"}
        )
        assert response.status_code == 200
        conn = (await response.get_json())["connection"]
        assert conn["usePlatformClient"] is False
        assert conn["clientId"] == "own-id"

    async def test_google_switch_back_to_the_platform_client_drops_the_stored_secret(
        self, kit: Kit, sso_app: Any
    ) -> None:
        sso_app.config["sso_settings"] = SsoSettings(
            google_client_id="g",
            google_client_secret="s",  # noqa: S106
        )
        public_id = await kit.create(kit.google_body())
        response = await kit.patch_connection(public_id, {"usePlatformClient": True})
        assert response.status_code == 200
        assert (await kit.connection_row(public_id)).secret_ciphertext is None


class TestDelete:
    async def test_delete_removes_connection_and_its_identity_links(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        assert (await kit.redeem(await kit.oidc_login(public_id)))["sub"]
        assert await kit.count_identities() == 1
        response = await kit.delete_connection(public_id)
        assert response.status_code == 200
        assert (await response.get_json()) == {"success": True}
        assert await kit.count_identities() == 0
        assert (await kit.get_connection(public_id)).status_code == 404

    async def test_logins_stop_immediately_after_delete(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        await kit.delete_connection(public_id)
        started = await kit.start(public_id)
        assert started.status_code == 404


class TestAuditTrail:
    async def test_admin_changes_are_audited_without_pii(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        await kit.patch_connection(public_id, {"displayName": "Renamed"})
        await kit.patch_connection(public_id, {"enabled": False})
        await kit.delete_connection(public_id)
        audit = kit.db.install_dal.audit_log
        rows = list(await kit.db.install_dal(audit.target_id == public_id).select(orderby=audit.id))
        assert [r.action for r in rows] == [
            "sso.connection.create",
            "sso.connection.update",
            "sso.connection.disable",
            "sso.connection.delete",
        ]
        assert all(r.user_id == 7 and r.target_type == "sso_connection" for r in rows)
        blob = json.dumps([r.details for r in rows])
        assert "alice" not in blob
        assert str(kit.oidc.client_secret) not in blob
        assert "acme.test" not in blob  # not even domains: ids and booleans only
