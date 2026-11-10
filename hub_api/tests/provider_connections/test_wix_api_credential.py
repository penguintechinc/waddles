"""Wix outbound API credential: write-only, sealed at rest, resolver-only exposure, never logged.

Real path: the real blueprints, auth chain, service, Wix adapter, AES-256-GCM vault and store over
sqlite. Only the machine-JWT issuer is the shared test one. No network (the adapter never calls Wix).
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask_core.service_jwt import ServiceJwtIssuer
from quart import Quart

from blueprints.v1.provider_connections import SERVICE_CONFIG_KEY
from services.provider_connections import PlatformKeyVault, ProviderConnectionService
from services.provider_connections.adapter import AdapterRegistry
from services.provider_connections.errors import InvalidConnectionInput, VaultCorrupt
from services.provider_connections.static_secret import ExternalHttpAdapter
from services.provider_connections.wix import WixAdapter
from tests.provider_connections.conftest import FakeClock, admin_headers
from tests.provider_connections.support import open_row, raw_rows, seal_for_row, set_column
from tests.provider_connections.test_resolve_v2 import STATIC_SCOPE, _bound, _url

API_KEY = "IST.fake-wix-api-key-0123456789"
SITE_ID = "0b9e1a52-4c3d-4e6f-8a7b-1234567890ab"
NEW_KEY = "IST.fake-rotated-key-" + "b" * 12
INSTANCE = "5f1c2d3e-aaaa-bbbb-cccc-0123456789ab"
DOC = {"api_key": API_KEY, "site_id": SITE_ID}


@pytest.fixture(scope="module")
def public_key_b64() -> str:
    """A fresh RSA public key per run (never a committed key)."""
    der = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    return base64.b64encode(der).decode()


@pytest.fixture
def wix_service(pc_db: Any, pc_app: Quart, clock: FakeClock) -> ProviderConnectionService:
    """The real service with the Wix + one static adapter registered, wired into the app."""
    service = ProviderConnectionService(
        pc_db.install_dal,
        AdapterRegistry([WixAdapter(), ExternalHttpAdapter()]),
        PlatformKeyVault(),
        clock=clock,
    )
    pc_app.config[SERVICE_CONFIG_KEY] = service
    return service


def _base(db: Any) -> str:
    return f"/api/v1/communities/{db.seed.community_id}/provider-connections"


def _body(public_key: str, **overrides: Any) -> str:
    body: dict[str, Any] = {
        "provider": "wix",
        "credentials": {"access_token": public_key},
        "metadata": {"instance_id": INSTANCE},
        "api_credential": dict(DOC),
    }
    body.update(overrides)
    return json.dumps(body)


async def _create(client: Any, db: Any, public_key: str, **overrides: Any) -> Any:
    return await client.post(
        _base(db), headers=admin_headers(db), data=_body(public_key, **overrides)
    )


class TestWriteOnly:
    async def test_create_never_echoes_the_credential_and_flags_its_presence(
        self, client: Any, pc_db: Any, wix_service: Any, public_key_b64: str
    ) -> None:
        response = await _create(client, pc_db, public_key_b64)
        text = await response.get_data(as_text=True)
        assert response.status_code == 201, text
        for secret in (API_KEY, SITE_ID, public_key_b64):
            assert secret not in text
        connection = json.loads(text)["connection"]
        assert connection["hasApiCredential"] is True
        cid = connection["id"]

        got = await client.get(f"{_base(pc_db)}/{cid}", headers=admin_headers(pc_db))
        listed = await client.get(_base(pc_db), headers=admin_headers(pc_db))
        for reply in (got, listed):
            body = await reply.get_data(as_text=True)
            assert reply.status_code == 200
            assert API_KEY not in body and SITE_ID not in body and "sealed" not in body

    async def test_credential_is_optional(
        self, client: Any, pc_db: Any, wix_service: Any, public_key_b64: str
    ) -> None:
        body = json.loads(_body(public_key_b64))
        del body["api_credential"]
        response = await client.post(_base(pc_db), headers=admin_headers(pc_db), json=body)
        assert response.status_code == 201
        assert (await response.get_json())["connection"]["hasApiCredential"] is False

    @pytest.mark.parametrize(
        "bad",
        [
            {"api_key": API_KEY},
            {"api_key": API_KEY, "site_id": SITE_ID, "extra": "x"},
            {"api_key": "has space", "site_id": SITE_ID},
            {"api_key": API_KEY, "site_id": "bad site!"},
            {"api_key": "", "site_id": SITE_ID},
            {"api_key": "k" * 4097, "site_id": SITE_ID},
        ],
    )
    async def test_invalid_credential_is_a_400_that_echoes_nothing(
        self, client: Any, pc_db: Any, wix_service: Any, public_key_b64: str, bad: dict[str, str]
    ) -> None:
        response = await _create(client, pc_db, public_key_b64, api_credential=bad)
        text = await response.get_data(as_text=True)
        assert response.status_code == 400, text
        assert API_KEY not in text and "has space" not in text and "k" * 100 not in text
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_a_provider_without_an_api_credential_refuses_one(
        self, wix_service: ProviderConnectionService
    ) -> None:
        spec = wix_service._registry.get("external_http").spec
        with pytest.raises(InvalidConnectionInput, match="does not take"):
            wix_service._validated_api_credential(spec, DOC)

    async def test_static_provider_create_with_api_credential_is_refused(
        self, client: Any, pc_db: Any, wix_service: Any, entitlements: Any
    ) -> None:
        response = await client.post(
            _base(pc_db),
            headers=admin_headers(pc_db),
            json={
                "provider": "external_http",
                "secret": {"scheme": 1, "document": {"token": "tok"}},
                "api_credential": dict(DOC),
                "metadata": {"url": "https://example.com/hook"},
            },
        )
        assert response.status_code == 400


class TestSealedAtRest:
    async def test_stored_sealed_row_bound_and_separate_from_the_public_key(
        self, client: Any, pc_db: Any, wix_service: Any, public_key_b64: str
    ) -> None:
        await _create(client, pc_db, public_key_b64)
        (row,) = await raw_rows(pc_db, "platform_connections")
        sealed = row["sealed_api_credential"]
        assert sealed and API_KEY not in sealed and SITE_ID not in sealed
        assert row["sealed_secret"] is None  # kind-1 invariant (0064 CHECK) untouched
        assert row["access_token"] != public_key_b64
        assert json.loads(open_row(row, "sealed_api_credential")) == DOC
        assert API_KEY not in json.dumps(row["resource_metadata"])
        assert SITE_ID not in json.dumps(row["resource_metadata"]).replace(INSTANCE, "")

    async def test_ciphertext_copied_to_another_row_fails_closed(
        self, client: Any, pc_db: Any, wix_service: Any, public_key_b64: str
    ) -> None:
        await _create(client, pc_db, public_key_b64)
        (row,) = await raw_rows(pc_db, "platform_connections")
        forged = dict(row, public_id="00000000-0000-4000-8000-000000000000")
        with pytest.raises(Exception, match=r".+"):
            open_row(forged, "sealed_api_credential")


class TestResolverExposure:
    async def _setup(self, client: Any, pc_db: Any, public_key_b64: str) -> tuple[int, int]:
        response = await _create(client, pc_db, public_key_b64)
        assert response.status_code == 201
        return pc_db.seed.community_id, (await response.get_json())["connection"]["id"]

    async def test_static_scope_gets_the_credential_and_the_public_key(
        self,
        client: Any,
        pc_db: Any,
        wix_service: Any,
        public_key_b64: str,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        community, conn = await self._setup(client, pc_db, public_key_b64)
        response = await client.post(
            _url(community, conn), headers=_bound(service_issuer, community, scope=STATIC_SCOPE)
        )
        body = await response.get_json()
        assert response.status_code == 200, body
        assert response.headers["Cache-Control"] == "no-store"
        assert body["accessToken"] == public_key_b64
        assert body["secret"] == {"scheme": 6, "fields": {"apiKey": API_KEY, "siteId": SITE_ID}}
        assert body["metadata"] == {"instance_id": INSTANCE}

    async def test_base_scope_never_receives_the_credential(
        self,
        client: Any,
        pc_db: Any,
        wix_service: Any,
        public_key_b64: str,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        community, conn = await self._setup(client, pc_db, public_key_b64)
        response = await client.post(
            _url(community, conn),
            headers=_bound(service_issuer, community, scope="connections:resolve"),
        )
        text = await response.get_data(as_text=True)
        assert response.status_code == 200, text
        assert API_KEY not in text and SITE_ID not in text
        body = json.loads(text)
        assert body["secret"] is None and body["accessToken"] == public_key_b64

    async def test_row_without_a_credential_resolves_with_no_secret(
        self,
        client: Any,
        pc_db: Any,
        wix_service: Any,
        public_key_b64: str,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        body = json.loads(_body(public_key_b64))
        del body["api_credential"]
        created = await client.post(_base(pc_db), headers=admin_headers(pc_db), json=body)
        conn = (await created.get_json())["connection"]["id"]
        community = pc_db.seed.community_id
        response = await client.post(
            _url(community, conn), headers=_bound(service_issuer, community, scope=STATIC_SCOPE)
        )
        assert response.status_code == 200
        assert (await response.get_json())["secret"] is None

    @pytest.mark.parametrize(
        "stored",
        ['{"token": "x"}', "not json", '["api_key", "site_id"]'],
    )
    async def test_a_malformed_stored_credential_fails_loud(
        self,
        client: Any,
        pc_db: Any,
        wix_service: ProviderConnectionService,
        public_key_b64: str,
        stored: str,
    ) -> None:
        community, conn = await self._setup(client, pc_db, public_key_b64)
        (row,) = await raw_rows(pc_db, "platform_connections")
        await set_column(
            pc_db,
            "platform_connections",
            row["id"],
            sealed_api_credential=seal_for_row(row, stored),
        )
        with pytest.raises(VaultCorrupt):
            await wix_service.resolve(
                community_id=community, connection_id=conn, static_allowed=True
            )
        # Without the static scope the broken optional credential is never opened.
        resolved = await wix_service.resolve(community_id=community, connection_id=conn)
        assert resolved.secret_scheme is None


class TestRotation:
    async def test_credential_only_patch_rotates_and_bumps_the_counter(
        self,
        client: Any,
        pc_db: Any,
        wix_service: ProviderConnectionService,
        public_key_b64: str,
    ) -> None:
        created = await _create(client, pc_db, public_key_b64)
        conn = (await created.get_json())["connection"]["id"]
        (before,) = await raw_rows(pc_db, "platform_connections")
        response = await client.patch(
            f"{_base(pc_db)}/{conn}",
            headers=admin_headers(pc_db),
            json={"api_credential": {"api_key": NEW_KEY, "site_id": SITE_ID}},
        )
        text = await response.get_data(as_text=True)
        assert response.status_code == 200, text
        assert NEW_KEY not in text and API_KEY not in text
        (after,) = await raw_rows(pc_db, "platform_connections")
        assert after["secret_version"] == before["secret_version"] + 1
        assert after["access_token"] == before["access_token"]
        assert json.loads(open_row(after, "sealed_api_credential"))["api_key"] == NEW_KEY
        resolved = await wix_service.resolve(
            community_id=pc_db.seed.community_id, connection_id=conn, static_allowed=True
        )
        assert resolved.secret_fields == {"apiKey": NEW_KEY, "siteId": SITE_ID}

    async def test_patch_with_token_and_credential_together(
        self,
        client: Any,
        pc_db: Any,
        wix_service: ProviderConnectionService,
        public_key_b64: str,
    ) -> None:
        created = await _create(client, pc_db, public_key_b64)
        conn = (await created.get_json())["connection"]["id"]
        response = await client.patch(
            f"{_base(pc_db)}/{conn}",
            headers=admin_headers(pc_db),
            json={
                "credentials": {"access_token": public_key_b64},
                "api_credential": {"api_key": NEW_KEY, "site_id": SITE_ID},
            },
        )
        assert response.status_code == 200, await response.get_data(as_text=True)
        (row,) = await raw_rows(pc_db, "platform_connections")
        assert json.loads(open_row(row, "sealed_api_credential"))["api_key"] == NEW_KEY

    async def test_adding_a_credential_to_a_row_that_had_none(
        self, client: Any, pc_db: Any, wix_service: Any, public_key_b64: str
    ) -> None:
        body = json.loads(_body(public_key_b64))
        del body["api_credential"]
        created = await client.post(_base(pc_db), headers=admin_headers(pc_db), json=body)
        conn = (await created.get_json())["connection"]["id"]
        response = await client.patch(
            f"{_base(pc_db)}/{conn}", headers=admin_headers(pc_db), json={"api_credential": DOC}
        )
        assert response.status_code == 200
        assert (await response.get_json())["connection"]["hasApiCredential"] is True

    async def test_other_tenant_cannot_rotate_it(
        self, client: Any, pc_db: Any, wix_service: Any, public_key_b64: str
    ) -> None:
        created = await _create(client, pc_db, public_key_b64)
        conn = (await created.get_json())["connection"]["id"]
        response = await client.patch(
            f"/api/v1/communities/{pc_db.seed.other_community_id}/provider-connections/{conn}",
            headers=admin_headers(pc_db),
            json={"api_credential": DOC},
        )
        assert response.status_code in (403, 404)

    async def test_static_connection_cannot_take_one(
        self, wix_service: ProviderConnectionService, pc_db: Any, entitlements: Any
    ) -> None:
        from tests.provider_connections.support import create_static

        view = await create_static(wix_service, pc_db)
        with pytest.raises(InvalidConnectionInput, match="cannot be edited"):
            await wix_service.update(
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                actor_user_id=1,
                connection_id=view.binding.connection.id,
                enabled=None,
                metadata=None,
                access_token=None,
                refresh_token=None,
                api_credential=DOC,
            )


class TestNeverLogged:
    async def test_no_log_record_carries_the_credential(
        self,
        client: Any,
        pc_db: Any,
        wix_service: ProviderConnectionService,
        public_key_b64: str,
        service_issuer: ServiceJwtIssuer,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        created = await _create(client, pc_db, public_key_b64)
        conn = (await created.get_json())["connection"]["id"]
        await client.patch(
            f"{_base(pc_db)}/{conn}",
            headers=admin_headers(pc_db),
            json={"api_credential": {"api_key": NEW_KEY, "site_id": SITE_ID}},
        )
        await client.post(
            _url(pc_db.seed.community_id, conn),
            headers=_bound(service_issuer, pc_db.seed.community_id, scope=STATIC_SCOPE),
        )
        await _create(client, pc_db, public_key_b64, api_credential={"api_key": "bad key"})
        assert caplog.records  # the scan below must examine something
        joined = "\n".join(record.getMessage() for record in caplog.records)
        for secret in (API_KEY, NEW_KEY, SITE_ID, "bad key", public_key_b64):
            assert secret not in joined
