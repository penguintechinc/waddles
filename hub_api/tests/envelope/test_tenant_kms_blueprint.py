"""`/api/v1/tenant/<slug>/kms`: scopes, tenant isolation, flows, error mapping, exact DTO shapes."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.tenant_kms import BLUEPRINTS
from services.bundle_install_dal import raw_sql_rows
from services.envelope import (
    EnvelopeError,
    KmsAccessDeniedError,
    KmsRejectedError,
    KmsUnavailableError,
    PlatformKekError,
    TenantKeyUnavailableError,
)
from services.envelope.kms_adapter import PROVIDER_AWS, PROVIDER_AZURE, PROVIDER_GCP
from services.envelope.runtime import EnvelopeRuntime
from tests.conftest import OTHER_TENANT_SLUG, TENANT_SLUG, make_token
from tests.envelope.conftest import (
    AWS_KEY_ARN,
    AWS_ROLE_ARN,
    AZURE_DIRECTORY,
    AZURE_KEY_URL,
    GCP_KEY,
)

ADMIN = "compliance.kms:admin"
BASE = f"/api/v1/tenant/{TENANT_SLUG}/kms"

#: The complete, exact wire shapes. A new field here must be a deliberate change: an
#: over-exposing response (wrapped DEK bytes, KEK refs of other tenants, provider text) fails.
CONFIG_FIELDS = {
    "provider",
    "keyRef",
    "region",
    "principal",
    "externalId",
    "status",
    "lastVerifiedAt",
    "lastErrorCode",
    "updatedAt",
}
DEK_FIELDS = {
    "dekVersion",
    "kekKind",
    "kekRef",
    "status",
    "usageCount",
    "activatedAt",
    "retiredAt",
}
REWRAP_FIELDS = {
    "targetKind",
    "targetRef",
    "total",
    "rewrapped",
    "alreadyCurrent",
    "failedVersions",
    "ok",
}


@pytest.fixture
def harness(make_harness):  # type: ignore[no-untyped-def]
    """The real service over mock provider sockets, entitled for the test tenant."""
    return make_harness(entitled={TENANT_SLUG})


@pytest.fixture
async def app(bundle_install_db: Any, install_dal: Any, harness: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["async_dal"] = bundle_install_db
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = install_dal
    quart_app.config["envelope_runtime"] = EnvelopeRuntime(
        service=harness.service,
        providers=harness.providers,
        enabled_providers=(PROVIDER_AWS, PROVIDER_AZURE, PROVIDER_GCP),
        platform_principals={
            PROVIDER_AWS: "arn:aws:iam::999988887777:role/waddles",
            "hashicorp_vault": "must-not-be-listed",  # not a supported provider -> filtered out
        },
    )
    for blueprint in BLUEPRINTS:
        quart_app.register_blueprint(blueprint)
    return quart_app


def auth(scope: str = ADMIN, *, tenant: str = TENANT_SLUG) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(scope=scope, tenant=tenant, user_id='1')}"}


AWS_BODY = {
    "provider": PROVIDER_AWS,
    "keyRef": AWS_KEY_ARN,
    "principal": AWS_ROLE_ARN,
}


class TestAccessControl:
    """Tenant first, then scope; roles are never consulted."""

    async def test_no_token_is_401(self, app: Quart) -> None:
        response = await app.test_client().get(BASE)
        assert response.status_code == 401

    @pytest.mark.parametrize("scope", ["", "tenant:admin", "compliance.kms:read", "*:read"])
    async def test_other_scopes_are_403_on_every_route(self, app: Quart, scope: str) -> None:
        client = app.test_client()
        headers = auth(scope)
        assert (await client.get(BASE, headers=headers)).status_code == 403
        assert (await client.put(BASE, headers=headers, json=AWS_BODY)).status_code == 403
        assert (await client.post(f"{BASE}/activate", headers=headers)).status_code == 403
        assert (await client.delete(BASE, headers=headers)).status_code == 403

    async def test_wildcard_admin_scope_is_honoured(self, app: Quart) -> None:
        response = await app.test_client().get(BASE, headers=auth("*:admin"))
        assert response.status_code == 200

    async def test_a_tenant_cannot_address_another_tenants_kms(self, app: Quart) -> None:
        """regression: external-kms IDOR -- the URL slug must equal the JWT's tenant."""
        other = f"/api/v1/tenant/{OTHER_TENANT_SLUG}/kms"
        client = app.test_client()
        headers = auth()
        assert (await client.get(other, headers=headers)).status_code == 403
        assert (await client.put(other, headers=headers, json=AWS_BODY)).status_code == 403
        assert (await client.post(f"{other}/activate", headers=headers)).status_code == 403
        assert (await client.delete(other, headers=headers)).status_code == 403

    async def test_503_when_the_envelope_runtime_is_not_initialised(self, app: Quart) -> None:
        del app.config["envelope_runtime"]
        response = await app.test_client().get(BASE, headers=auth())
        assert response.status_code == 503
        assert (await response.get_json())["error"]["code"] == "ENVELOPE_UNAVAILABLE"


class TestStatus:
    """GET is never entitlement-gated and never shows secrets."""

    async def test_default_state_is_the_unconfigured_baseline(self, app: Quart) -> None:
        body = await (await app.test_client().get(BASE, headers=auth())).get_json()
        assert body["success"] is True
        assert body["config"] is None and body["keys"] == []
        assert body["supportedProviders"] == [PROVIDER_AWS, PROVIDER_AZURE, PROVIDER_GCP]
        assert body["platformPrincipals"] == {
            PROVIDER_AWS: "arn:aws:iam::999988887777:role/waddles"
        }

    async def test_status_is_readable_after_the_licence_lapses(self, app: Quart, harness) -> None:  # type: ignore[no-untyped-def]
        client = app.test_client()
        await client.put(BASE, headers=auth(), json=AWS_BODY)
        harness.gate.entitled.clear()
        response = await client.get(BASE, headers=auth())
        assert response.status_code == 200
        assert (await response.get_json())["config"]["provider"] == PROVIDER_AWS


class TestConfigure:
    """PUT records the customer key as pending (Enterprise only) and validates strictly."""

    async def test_put_returns_the_external_id_and_pending_status(self, app: Quart) -> None:
        response = await app.test_client().put(BASE, headers=auth(), json=AWS_BODY)
        assert response.status_code == 200
        config = (await response.get_json())["config"]
        assert set(config) == CONFIG_FIELDS
        assert config["status"] == "pending" and config["region"] == "us-east-1"
        assert len(config["externalId"]) == 48

    async def test_not_entitled_is_403_and_records_nothing(self, app: Quart, harness) -> None:  # type: ignore[no-untyped-def]
        harness.gate.entitled.clear()
        client = app.test_client()
        response = await client.put(BASE, headers=auth(), json=AWS_BODY)
        assert response.status_code == 403
        assert (await response.get_json())["error"]["code"] == "EXTERNAL_KMS_NOT_ENTITLED"
        assert (await (await client.get(BASE, headers=auth())).get_json())["config"] is None

    @pytest.mark.parametrize(
        "body",
        [
            {"provider": PROVIDER_AWS, "keyRef": "alias/mine", "principal": AWS_ROLE_ARN},
            {"provider": PROVIDER_AWS, "keyRef": AWS_KEY_ARN},
            {"provider": PROVIDER_GCP, "keyRef": GCP_KEY + "/cryptoKeyVersions/1"},
            {"provider": PROVIDER_AZURE, "keyRef": AZURE_KEY_URL, "principal": "not-a-guid"},
            {"provider": "hashicorp_vault", "keyRef": "x"},
            {
                "provider": PROVIDER_AWS,
                "keyRef": AWS_KEY_ARN,
                "region": "eu-west-1",
                "principal": AWS_ROLE_ARN,
            },
        ],
    )
    async def test_invalid_config_is_422_with_a_stable_code(self, app: Quart, body) -> None:  # type: ignore[no-untyped-def]
        response = await app.test_client().put(BASE, headers=auth(), json=body)
        assert response.status_code == 422
        assert (await response.get_json())["error"]["code"] == "INVALID_KMS_CONFIG"

    @pytest.mark.parametrize(
        "body",
        [
            {"provider": PROVIDER_AWS, "keyRef": "k" * 2049, "principal": AWS_ROLE_ARN},
            {"provider": "p" * 65, "keyRef": AWS_KEY_ARN},
            {"provider": PROVIDER_AWS, "keyRef": AWS_KEY_ARN, "region": "r" * 65},
            {"provider": PROVIDER_AWS, "keyRef": AWS_KEY_ARN, "principal": "x" * 2049},
            {"provider": "", "keyRef": AWS_KEY_ARN},
            {"provider": PROVIDER_AWS, "keyRef": ""},
        ],
    )
    async def test_oversized_or_empty_fields_are_rejected_at_the_boundary(
        self, app: Quart, body
    ) -> None:
        response = await app.test_client().put(BASE, headers=auth(), json=body)
        assert response.status_code == 400

    @pytest.mark.parametrize("body", [{}, {"provider": PROVIDER_AWS}, {"keyRef": "x"}])
    async def test_missing_required_fields_are_rejected_before_any_work(
        self, app: Quart, body
    ) -> None:  # type: ignore[no-untyped-def]
        response = await app.test_client().put(BASE, headers=auth(), json=body)
        assert response.status_code == 400


class TestActivateAndDisable:
    """The full BYOK lifecycle through HTTP, per provider."""

    async def _configure(self, app: Quart, harness, provider: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        body = {
            PROVIDER_AWS: AWS_BODY,
            PROVIDER_GCP: {"provider": PROVIDER_GCP, "keyRef": GCP_KEY},
            PROVIDER_AZURE: {
                "provider": PROVIDER_AZURE,
                "keyRef": AZURE_KEY_URL,
                "principal": AZURE_DIRECTORY,
            },
        }[provider]
        if provider == PROVIDER_GCP:
            harness.gcp.add_key(GCP_KEY)
        if provider == PROVIDER_AZURE:
            harness.azure.add_key("waddles")
        response = await app.test_client().put(BASE, headers=auth(), json=body)
        assert response.status_code == 200
        return (await response.get_json())["config"]

    @pytest.mark.parametrize("provider", [PROVIDER_AWS, PROVIDER_GCP, PROVIDER_AZURE])
    async def test_configure_activate_use_disable(self, app: Quart, harness, provider) -> None:  # type: ignore[no-untyped-def]
        client = app.test_client()
        config = await self._configure(app, harness, provider)
        # The customer does their half, pinning the ExternalId they were shown.
        from tests.envelope.conftest import Harness

        Harness.provider_side(
            harness,
            type("C", (), {"provider": provider, "external_id": config["externalId"]})(),
        )

        activated = await client.post(f"{BASE}/activate", headers=auth())
        assert activated.status_code == 200
        body = await activated.get_json()
        assert set(body["config"]) == CONFIG_FIELDS and set(body["rewrap"]) == REWRAP_FIELDS
        assert body["config"]["status"] == "active" and body["rewrap"]["ok"] is True

        # Data written now is protected by a key wrapped under the customer's KMS.
        await harness.service.encrypt(
            1, b"x", table="t", column="c", row_uuid="11111111-2222-3333-4444-555555555555"
        )
        status = await (await client.get(BASE, headers=auth())).get_json()
        assert [set(k) for k in status["keys"]] == [DEK_FIELDS]
        assert status["keys"][0]["kekKind"] == "customer_kms"

        harness.gate.entitled.clear()  # the exit ramp needs no entitlement
        disabled = await client.delete(BASE, headers=auth())
        assert disabled.status_code == 200
        dbody = await disabled.get_json()
        assert set(dbody["rewrap"]) == REWRAP_FIELDS and dbody["rewrap"]["targetKind"] == "platform"
        after = await (await client.get(BASE, headers=auth())).get_json()
        assert after["config"] is None and after["keys"][0]["kekKind"] == "platform"

    async def test_activating_without_a_config_is_422(self, app: Quart) -> None:
        response = await app.test_client().post(f"{BASE}/activate", headers=auth())
        assert response.status_code == 422
        assert (await response.get_json())["error"]["code"] == "INVALID_KMS_CONFIG"

    async def test_activating_before_the_customer_pins_the_external_id_is_422(
        self, app: Quart, harness
    ) -> None:  # type: ignore[no-untyped-def]
        await self._configure(app, harness, PROVIDER_GCP)  # label never set
        response = await app.test_client().post(f"{BASE}/activate", headers=auth())
        assert response.status_code == 422
        assert (await response.get_json())["error"]["code"] == "INVALID_KMS_CONFIG"

    async def test_a_denied_grant_is_422_kms_access_denied(self, app: Quart, harness) -> None:  # type: ignore[no-untyped-def]
        config = await self._configure(app, harness, PROVIDER_AWS)
        harness.aws.trust(AWS_ROLE_ARN, "someone-else")  # wrong ExternalId
        response = await app.test_client().post(f"{BASE}/activate", headers=auth())
        assert response.status_code == 422
        assert (await response.get_json())["error"]["code"] == "KMS_ACCESS_DENIED"
        assert config["status"] == "pending"

    async def test_an_unreachable_provider_is_503_kms_unavailable(
        self, app: Quart, harness
    ) -> None:  # type: ignore[no-untyped-def]
        config = await self._configure(app, harness, PROVIDER_AWS)
        harness.aws.trust(AWS_ROLE_ARN, config["externalId"])
        harness.aws.behavior.fail_status = 503
        response = await app.test_client().post(f"{BASE}/activate", headers=auth())
        assert response.status_code == 503
        assert (await response.get_json())["error"]["code"] == "KMS_UNAVAILABLE"

    async def test_activation_without_the_entitlement_is_403(self, app: Quart, harness) -> None:  # type: ignore[no-untyped-def]
        await self._configure(app, harness, PROVIDER_AWS)
        harness.gate.entitled.clear()
        response = await app.test_client().post(f"{BASE}/activate", headers=auth())
        assert response.status_code == 403

    async def test_a_partial_disable_is_409_and_keeps_the_config(self, app: Quart, harness) -> None:  # type: ignore[no-untyped-def]
        client = app.test_client()
        config = await self._configure(app, harness, PROVIDER_AWS)
        harness.aws.trust(AWS_ROLE_ARN, config["externalId"])
        assert (await client.post(f"{BASE}/activate", headers=auth())).status_code == 200
        await harness.service.encrypt(
            1, b"x", table="t", column="c", row_uuid="11111111-2222-3333-4444-555555555555"
        )
        harness.aws.behavior.deny = True  # the customer already revoked
        response = await client.delete(BASE, headers=auth())
        assert response.status_code == 409
        error = (await response.get_json())["error"]
        assert error["code"] == "REWRAP_INCOMPLETE"
        assert (await (await client.get(BASE, headers=auth())).get_json())["config"] is not None


class TestErrorMapping:
    """Every envelope failure maps to a stable status and code, with fixed messages."""

    @pytest.mark.parametrize(
        ("raised", "status", "code"),
        [
            (KmsRejectedError("secret provider text"), 502, "KMS_REJECTED"),
            (KmsUnavailableError("secret provider text"), 503, "KMS_UNAVAILABLE"),
            (KmsAccessDeniedError("secret provider text"), 422, "KMS_ACCESS_DENIED"),
            (
                TenantKeyUnavailableError("secret provider text", reason="kms_pending"),
                503,
                "TENANT_KEY_UNAVAILABLE",
            ),
            (PlatformKekError("secret kek detail"), 503, "PLATFORM_KEK_UNAVAILABLE"),
            (EnvelopeError("secret internal detail"), 500, "ENVELOPE_ERROR"),
        ],
    )
    async def test_each_failure_has_a_stable_code_and_never_echoes_its_message(
        self, app: Quart, harness, raised: Exception, status: int, code: str
    ) -> None:
        async def boom(*args: object, **kwargs: object) -> None:
            raise raised

        harness.service.get_status = boom  # type: ignore[method-assign]
        response = await app.test_client().get(BASE, headers=auth())
        assert response.status_code == status
        body = await response.get_data(as_text=True)
        assert json.loads(body)["error"]["code"] == code
        assert "secret" not in body

    async def test_an_unexpected_exception_is_not_swallowed_into_a_kms_error(
        self, app: Quart, harness
    ) -> None:
        async def bug(*args: object, **kwargs: object) -> None:
            raise ZeroDivisionError("programming error")

        harness.service.get_status = bug  # type: ignore[method-assign]
        response = await app.test_client().get(BASE, headers=auth())
        assert response.status_code == 500  # the framework's own 500, not a disguised KMS code


class TestResponsesNeverLeakKeyMaterial:
    """regression: external-kms -- only identifiers/counters leave the process, never key bytes."""

    async def test_no_response_contains_wrapped_or_plaintext_key_bytes(
        self, app: Quart, harness
    ) -> None:  # type: ignore[no-untyped-def]
        client = app.test_client()
        config = (await (await client.put(BASE, headers=auth(), json=AWS_BODY)).get_json())[
            "config"
        ]
        harness.aws.trust(AWS_ROLE_ARN, config["externalId"])
        texts = [
            await (await client.post(f"{BASE}/activate", headers=auth())).get_data(as_text=True)
        ]
        await harness.service.encrypt(
            1, b"x", table="t", column="c", row_uuid="11111111-2222-3333-4444-555555555555"
        )
        texts.append(await (await client.get(BASE, headers=auth())).get_data(as_text=True))
        wrapped = (await harness.repo.list_keys(1))[0].wrapped_dek
        assert wrapped
        needles = [
            wrapped.hex(),
            base64.b64encode(wrapped).decode(),
            base64.urlsafe_b64encode(wrapped).decode(),
        ]
        for text in texts:
            json.loads(text)
            for needle in needles:
                assert needle not in text

    async def test_error_bodies_do_not_echo_provider_text(self, app: Quart, harness) -> None:  # type: ignore[no-untyped-def]
        config = (
            await (await app.test_client().put(BASE, headers=auth(), json=AWS_BODY)).get_json()
        )["config"]
        harness.aws.trust(AWS_ROLE_ARN, config["externalId"])
        harness.aws.behavior.deny = True
        response = await app.test_client().post(f"{BASE}/activate", headers=auth())
        text = await response.get_data(as_text=True)
        assert AWS_KEY_ARN not in text and AWS_ROLE_ARN not in text and "mock" not in text


class TestAudit:
    """Every change leaves an audit_log row; a failing audit write is loud, not swallowed."""

    async def _actions(self, install_dal: Any) -> list[str]:
        rows = await raw_sql_rows(
            install_dal, "SELECT action FROM audit_log WHERE target_type = 'tenant_kms' ORDER BY id"
        )
        return [r["action"] for r in rows.as_list()]

    async def test_configure_activate_disable_are_audited(
        self, app: Quart, harness, install_dal: Any
    ) -> None:  # type: ignore[no-untyped-def]
        client = app.test_client()
        config = (await (await client.put(BASE, headers=auth(), json=AWS_BODY)).get_json())[
            "config"
        ]
        harness.aws.trust(AWS_ROLE_ARN, config["externalId"])
        await client.post(f"{BASE}/activate", headers=auth())
        await client.delete(BASE, headers=auth())
        assert await self._actions(install_dal) == ["kms.configure", "kms.activate", "kms.disable"]

    async def test_audit_rows_carry_no_key_material_or_key_refs(
        self, app: Quart, harness, install_dal: Any
    ) -> None:  # type: ignore[no-untyped-def]
        client = app.test_client()
        config = (await (await client.put(BASE, headers=auth(), json=AWS_BODY)).get_json())[
            "config"
        ]
        harness.aws.trust(AWS_ROLE_ARN, config["externalId"])
        await client.post(f"{BASE}/activate", headers=auth())
        rows = await raw_sql_rows(install_dal, "SELECT details FROM audit_log")
        blob = json.dumps([str(r["details"]) for r in rows.as_list()])
        assert AWS_KEY_ARN not in blob and config["externalId"] not in blob

    async def test_a_failed_audit_write_is_logged_not_swallowed_and_does_not_fail_the_call(
        self, app: Quart, caplog: Any
    ) -> None:
        import logging

        async def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("audit store down")

        app.config["install_dal"] = type(
            "Broken", (), {"audit_log": type("T", (), {"async_insert": staticmethod(boom)})()}
        )()
        with caplog.at_level(logging.ERROR, logger="blueprints.v1.tenant_kms"):
            response = await app.test_client().put(BASE, headers=auth(), json=AWS_BODY)
        assert response.status_code == 200
        assert any(r.getMessage() == "envelope.api.audit_failed" for r in caplog.records)
