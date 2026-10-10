"""Azure Key Vault adapter: validation, Entra tokens, RSA-OAEP wrap, failure mapping."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from services.envelope.azure_key_vault import (
    PROOF_TAG,
    AzurePlatformSettings,
    map_token_error,
    map_vault_error,
    validate_azure_config,
)
from services.envelope.crypto import wrap_context
from services.envelope.errors import (
    KmsAccessDeniedError,
    KmsConfigError,
    KmsCredentialsError,
    KmsRejectedError,
    KmsUnavailableError,
)
from services.envelope.kms_adapter import PROVIDER_AZURE
from tests.envelope.conftest import AZURE_DIRECTORY, AZURE_KEY_URL, make_config
from tests.envelope.fakes import TENANT_A, TENANT_B
from tests.envelope.kms_mocks import MockAzureKeyVault

EXTERNAL_ID = "12" * 24


class TestValidateAzureConfig:
    """Strict key-URL and directory-id validation (this URL is later fetched -> SSRF surface)."""

    def test_accepts_vault_and_managed_hsm_and_normalises_host_case(self) -> None:
        canonical = validate_azure_config(
            "https://Contoso-Vault.VAULT.azure.net/keys/waddles", None, AZURE_DIRECTORY.upper()
        )
        assert canonical.key_ref == "https://contoso-vault.vault.azure.net/keys/waddles"
        assert canonical.principal == AZURE_DIRECTORY
        hsm = validate_azure_config(
            "https://my-hsm.managedhsm.azure.net/keys/k1", None, AZURE_DIRECTORY
        )
        assert hsm.key_ref.endswith("managedhsm.azure.net/keys/k1")

    @pytest.mark.parametrize(
        "key_ref",
        [
            "http://contoso.vault.azure.net/keys/k",  # not https
            "https://contoso.vault.azure.net/keys/k/0123456789abcdef0123456789abcdef",  # version
            "https://evil.example.com/keys/k",
            "https://contoso.vault.azure.net.evil.com/keys/k",
            "https://contoso.vault.azure.cn/keys/k",  # sovereign cloud: unsupported
            "https://169.254.169.254/keys/k",
            "https://contoso.vault.azure.net/secrets/k",
            "https://contoso.vault.azure.net/keys/k?x=y",
            "https://user@contoso.vault.azure.net/keys/k",
            "contoso.vault.azure.net/keys/k",
            "",
        ],
    )
    def test_rejects_urls_that_could_steer_a_credentialed_request(self, key_ref: str) -> None:
        with pytest.raises(KmsConfigError, match="key URL"):
            validate_azure_config(key_ref, None, AZURE_DIRECTORY)

    @pytest.mark.parametrize(
        "principal", [None, "", "contoso.onmicrosoft.com", "not-a-guid", "../x"]
    )
    def test_requires_a_directory_guid(self, principal: str | None) -> None:
        with pytest.raises(KmsConfigError, match="directory"):
            validate_azure_config(AZURE_KEY_URL, None, principal)

    def test_region_is_not_used(self) -> None:
        with pytest.raises(KmsConfigError, match="region"):
            validate_azure_config(AZURE_KEY_URL, "eastus", AZURE_DIRECTORY)


class TestPlatformSettings:
    """The application credential is required and validated at startup."""

    def test_from_env_requires_client_id_and_secret(self) -> None:
        with pytest.raises(KmsConfigError, match="ENVELOPE_AZURE_CLIENT_ID"):
            AzurePlatformSettings.from_env({})

    def test_from_env_requires_https_authority(self) -> None:
        env = {
            "ENVELOPE_AZURE_CLIENT_ID": "id",
            "ENVELOPE_AZURE_CLIENT_SECRET": "s",
            "ENVELOPE_AZURE_AUTHORITY": "http://login.evil.example",
        }
        with pytest.raises(KmsConfigError, match="https"):
            AzurePlatformSettings.from_env(env)

    def test_secret_is_never_in_repr(self) -> None:
        settings = AzurePlatformSettings(client_id="id", client_secret="super-secret-value")
        assert "super-secret-value" not in repr(settings)


class TestErrorMapping:
    """Vault and Entra errors classify into customer-side vs platform-side vs transient."""

    @pytest.mark.parametrize(
        ("status", "code", "expected"),
        [
            (403, "Forbidden", KmsAccessDeniedError),
            (403, "ForbiddenByRbac", KmsAccessDeniedError),
            (404, "KeyNotFound", KmsAccessDeniedError),
            (429, "Throttled", KmsUnavailableError),
            (500, "InternalError", KmsUnavailableError),
            (400, "BadParameter", KmsRejectedError),
        ],
    )
    def test_vault_classification(self, status: int, code: str, expected: type[Exception]) -> None:
        mapped = map_vault_error(
            httpx.Response(status, json={"error": {"code": code, "message": "x"}})
        )
        assert type(mapped) is expected

    @pytest.mark.parametrize(
        ("status", "codes", "expected"),
        [
            (400, [700016], KmsAccessDeniedError),  # app not in the customer's directory
            (400, [65001], KmsAccessDeniedError),  # consent not granted
            (401, [7000215], KmsCredentialsError),  # OUR secret is wrong
            (401, [7000222], KmsCredentialsError),  # OUR secret expired
            (503, [], KmsUnavailableError),
            (400, [12345], KmsRejectedError),
        ],
    )
    def test_token_classification(
        self, status: int, codes: list[int], expected: type[Exception]
    ) -> None:
        response = httpx.Response(status, json={"error": "x", "error_codes": codes})
        assert type(map_token_error(response)) is expected

    def test_platform_credential_failure_is_not_a_customer_revocation(self) -> None:
        mapped = map_token_error(httpx.Response(401, json={"error_codes": [7000215]}))
        assert isinstance(mapped, KmsUnavailableError)
        assert not isinstance(mapped, KmsAccessDeniedError)


class TestAdapterAgainstMockSocket:
    """Real httpx -> loopback Entra + Key Vault mock with genuine RSA-OAEP-256."""

    @pytest.fixture
    def azure(self, make_harness, azure_mock):
        harness = make_harness()
        azure_mock.add_key("waddles", tags={PROOF_TAG: EXTERNAL_ID})
        azure_mock.consented.add(AZURE_DIRECTORY)
        adapter = harness.providers.registry.build(
            make_config(
                PROVIDER_AZURE, AZURE_KEY_URL, principal=AZURE_DIRECTORY, external_id=EXTERNAL_ID
            )
        )
        return adapter

    async def test_wrap_unwrap_round_trips_and_records_the_key_version(
        self, azure, azure_mock
    ) -> None:
        dek = os.urandom(32)
        context = wrap_context(TENANT_A)
        wrapped = await azure.wrap(dek, context=context)
        document = json.loads(wrapped)
        assert document["v"] == 1 and len(document["kv"]) == 32
        assert dek not in wrapped
        assert await azure.unwrap(wrapped, context=context) == dek

    async def test_wrapped_dek_does_not_unwrap_under_another_tenants_context(self, azure) -> None:
        """Key Vault RSA wrap has no AAD, so the context digest travels inside the payload."""
        wrapped = await azure.wrap(os.urandom(32), context=wrap_context(TENANT_A))
        with pytest.raises(KmsRejectedError, match="different context"):
            await azure.unwrap(wrapped, context=wrap_context(TENANT_B))

    async def test_old_wraps_stay_readable_after_the_customer_rotates_the_key(
        self, azure, azure_mock
    ) -> None:
        context = wrap_context(TENANT_A)
        dek = os.urandom(32)
        old = await azure.wrap(dek, context=context)
        azure_mock.rotate_key("waddles")
        new = await azure.wrap(dek, context=context)
        assert json.loads(old)["kv"] != json.loads(new)["kv"]
        assert await azure.unwrap(old, context=context) == dek
        assert await azure.unwrap(new, context=context) == dek

    async def test_token_is_requested_from_the_customers_directory(self, azure, azure_mock) -> None:
        await azure.wrap(os.urandom(32), context=wrap_context(TENANT_A))
        token_call = azure_mock.server.calls(lambda r: r.path.endswith("/oauth2/v2.0/token"))[0]
        assert token_call.path == f"/{AZURE_DIRECTORY}/oauth2/v2.0/token"
        assert token_call.form()["scope"] == "https://vault.azure.net/.default"

    async def test_unconsented_directory_is_access_denied(self, azure, azure_mock) -> None:
        azure_mock.consented.clear()
        with pytest.raises(KmsAccessDeniedError):
            await azure.wrap(os.urandom(32), context=wrap_context(TENANT_A))

    async def test_wrong_platform_secret_is_a_credential_error_not_a_revocation(
        self, azure, azure_mock, monkeypatch
    ) -> None:
        monkeypatch.setattr(MockAzureKeyVault, "PLATFORM_SECRET", "rotated-away")
        with pytest.raises(KmsCredentialsError):
            await azure.wrap(os.urandom(32), context=wrap_context(TENANT_A))

    async def test_rejected_token_is_refreshed_once(self, azure, azure_mock) -> None:
        context = wrap_context(TENANT_A)
        await azure.wrap(os.urandom(32), context=context)
        azure_mock.reject_next_token_use = 1
        await azure.wrap(os.urandom(32), context=context)
        assert azure_mock.token_requests == 2

    async def test_revoked_role_assignment_is_access_denied(self, azure, azure_mock) -> None:
        azure_mock.behavior.deny = True
        with pytest.raises(KmsAccessDeniedError):
            await azure.wrap(os.urandom(32), context=wrap_context(TENANT_A))

    async def test_disabled_key_is_access_denied(self, azure, azure_mock) -> None:
        azure_mock.keys["waddles"].enabled = False
        with pytest.raises(KmsAccessDeniedError):
            await azure.wrap(os.urandom(32), context=wrap_context(TENANT_A))

    async def test_outage_is_transient(self, azure, azure_mock) -> None:
        azure_mock.behavior.fail_status = 503
        with pytest.raises(KmsUnavailableError):
            await azure.wrap(os.urandom(32), context=wrap_context(TENANT_A))

    async def test_garbled_wrapped_blobs_are_rejected_not_crashed_on(self, azure) -> None:
        context = wrap_context(TENANT_A)
        for blob in (
            b"",
            b"not json",
            b'{"v":2,"kv":"x","ct":"AA"}',
            b'{"v":1,"kv":"../etc","ct":"AA"}',
        ):
            with pytest.raises(KmsRejectedError):
                await azure.unwrap(blob, context=context)

    async def test_verify_succeeds_for_a_healthy_tagged_key(self, azure) -> None:
        info = await azure.verify()
        assert (info.provider, info.key_ref) == (PROVIDER_AZURE, AZURE_KEY_URL)

    async def test_verify_requires_the_proof_of_control_tag(self, make_harness, azure_mock) -> None:
        harness = make_harness()
        azure_mock.add_key("waddles")
        azure_mock.consented.add(AZURE_DIRECTORY)
        adapter = harness.providers.registry.build(
            make_config(
                PROVIDER_AZURE, AZURE_KEY_URL, principal=AZURE_DIRECTORY, external_id=EXTERNAL_ID
            )
        )
        with pytest.raises(KmsConfigError, match=PROOF_TAG):
            await adapter.verify()

    @pytest.mark.parametrize(
        ("mutate", "fragment"),
        [
            (lambda k: setattr(k, "enabled", False), "disabled"),
            (lambda k: setattr(k, "key_ops", ["encrypt"]), "wrapKey"),
            (lambda k: setattr(k, "key_size", 1024), "2048"),
        ],
    )
    async def test_verify_rejects_unsuitable_keys(
        self, make_harness, azure_mock, mutate, fragment
    ) -> None:
        harness = make_harness()
        azure_mock.add_key("waddles", tags={PROOF_TAG: EXTERNAL_ID}, key_size=2048)
        key = azure_mock.keys["waddles"]
        if fragment == "2048":
            azure_mock.keys["waddles"].versions.clear()
            key.key_size = 1024
            azure_mock.rotate_key("waddles")
        else:
            mutate(key)
        azure_mock.consented.add(AZURE_DIRECTORY)
        adapter = harness.providers.registry.build(
            make_config(
                PROVIDER_AZURE, AZURE_KEY_URL, principal=AZURE_DIRECTORY, external_id=EXTERNAL_ID
            )
        )
        with pytest.raises(KmsConfigError, match=fragment):
            await adapter.verify()
