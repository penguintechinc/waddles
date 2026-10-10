"""Google Cloud KMS adapter: config validation, credential flows, wrap/unwrap, failure mapping."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from services.envelope.crypto import wrap_context
from services.envelope.errors import (
    KmsAccessDeniedError,
    KmsConfigError,
    KmsCredentialsError,
    KmsRejectedError,
    KmsUnavailableError,
)
from services.envelope.gcp_kms import (
    PROOF_LABEL,
    GcpKeySpec,
    GcpKmsAdapter,
    GcpPlatformSettings,
    GcpRuntime,
    ServiceAccountKey,
    map_http_error,
    validate_gcp_config,
)
from services.envelope.kms_adapter import PROVIDER_GCP
from tests.envelope.conftest import GCP_KEY, make_config
from tests.envelope.fakes import TENANT_A, TENANT_B

EXTERNAL_ID = "ef" * 24


class TestValidateGcpConfig:
    """Only a plain CryptoKey resource name is accepted."""

    def test_accepts_a_crypto_key_and_derives_region(self) -> None:
        canonical = validate_gcp_config(GCP_KEY, None, None)
        assert canonical.key_ref == GCP_KEY
        assert canonical.region == "us-east1"
        assert canonical.principal is None

    @pytest.mark.parametrize(
        ("key_ref", "region", "principal", "fragment"),
        [
            (GCP_KEY + "/cryptoKeyVersions/1", None, None, "CryptoKey resource name"),
            ("projects/p/locations/l", None, None, "CryptoKey resource name"),
            (GCP_KEY.replace("projects/", "folders/"), None, None, "CryptoKey resource name"),
            (GCP_KEY + "/../x", None, None, "CryptoKey resource name"),
            (GCP_KEY, "europe-west1", None, "region must match"),
            (GCP_KEY, None, "sa@x.iam.gserviceaccount.com", "not used"),
            ("", None, None, "CryptoKey resource name"),
        ],
    )
    def test_rejects_bad_config(
        self, key_ref: str, region: str | None, principal: str | None, fragment: str
    ) -> None:
        with pytest.raises(KmsConfigError, match=fragment):
            validate_gcp_config(key_ref, region, principal)


class TestPlatformSettings:
    """Platform credentials are validated at load time, never first use."""

    def test_service_account_key_parses(self, gcp_mock) -> None:
        key = ServiceAccountKey.parse(gcp_mock.service_account_json())
        assert key.client_email == gcp_mock.client_email
        assert "PRIVATE KEY" not in repr(key)  # key material never reaches a repr/log

    @pytest.mark.parametrize(
        "raw",
        ["not json", "[]", json.dumps({"type": "user"}), json.dumps({"type": "service_account"})],
    )
    def test_bad_service_account_documents_fail_loudly(self, raw: str) -> None:
        with pytest.raises(KmsConfigError):
            ServiceAccountKey.parse(raw)

    def test_from_env_requires_https_endpoint(self) -> None:
        with pytest.raises(KmsConfigError, match="https"):
            GcpPlatformSettings.from_env({"ENVELOPE_GCP_KMS_ENDPOINT": "http://evil.example"})

    def test_from_env_defaults_to_metadata_identity(self) -> None:
        settings = GcpPlatformSettings.from_env({})
        assert settings.service_account is None
        assert settings.api_base == "https://cloudkms.googleapis.com/v1"


class TestErrorMapping:
    """HTTP status + Google status -> denied / transient / rejected."""

    @pytest.mark.parametrize(
        ("status", "google", "expected"),
        [
            (403, "PERMISSION_DENIED", KmsAccessDeniedError),
            (404, "NOT_FOUND", KmsAccessDeniedError),
            (400, "FAILED_PRECONDITION", KmsAccessDeniedError),  # disabled / destroyed version
            (400, "INVALID_ARGUMENT", KmsRejectedError),  # AAD mismatch / corrupt ciphertext
            (429, "RESOURCE_EXHAUSTED", KmsUnavailableError),
            (503, "UNAVAILABLE", KmsUnavailableError),
            (409, "ABORTED", KmsRejectedError),
        ],
    )
    def test_classification(self, status: int, google: str, expected: type[Exception]) -> None:
        response = httpx.Response(status, json={"error": {"status": google, "message": "secret"}})
        mapped = map_http_error(response)
        assert type(mapped) is expected
        assert "secret" not in str(mapped)  # provider message is never kept

    def test_non_json_body_still_classifies(self) -> None:
        assert isinstance(map_http_error(httpx.Response(502, text="<html>")), KmsUnavailableError)


class TestAdapterAgainstMockSocket:
    """Real httpx -> loopback Cloud KMS mock, including real RS256 JWT-bearer verification."""

    @pytest.fixture
    def adapter(self, make_harness, gcp_mock):
        harness = make_harness()
        gcp_mock.add_key(GCP_KEY, labels={PROOF_LABEL: EXTERNAL_ID})
        return harness.providers.registry.build(
            make_config(PROVIDER_GCP, GCP_KEY, external_id=EXTERNAL_ID)
        )

    async def test_wrap_unwrap_round_trips_with_context_as_aad(self, adapter, gcp_mock) -> None:
        dek = os.urandom(32)
        context = wrap_context(TENANT_A)
        wrapped = await adapter.wrap(dek, context=context)
        assert await adapter.unwrap(wrapped, context=context) == dek
        encrypt = next(r for r in gcp_mock.server.calls() if r.path.endswith(":encrypt"))
        assert encrypt.json()["additionalAuthenticatedData"]  # context bound on the wire
        assert encrypt.headers["authorization"].startswith("Bearer ya29.")

    async def test_wrapped_dek_does_not_unwrap_under_another_tenants_context(self, adapter) -> None:
        wrapped = await adapter.wrap(os.urandom(32), context=wrap_context(TENANT_A))
        with pytest.raises(KmsRejectedError):
            await adapter.unwrap(wrapped, context=wrap_context(TENANT_B))

    async def test_service_account_assertion_is_a_verified_rs256_jwt(
        self, adapter, gcp_mock
    ) -> None:
        await adapter.wrap(os.urandom(32), context=wrap_context(TENANT_A))
        assert gcp_mock.token_requests == 1  # the mock verified signature, iss, aud and scope

    async def test_token_is_cached_across_calls(self, adapter, gcp_mock) -> None:
        context = wrap_context(TENANT_A)
        for _ in range(3):
            await adapter.wrap(os.urandom(32), context=context)
        assert gcp_mock.token_requests == 1

    async def test_rejected_token_is_refreshed_once_then_succeeds(self, adapter, gcp_mock) -> None:
        context = wrap_context(TENANT_A)
        await adapter.wrap(os.urandom(32), context=context)
        gcp_mock.reject_next_token_use = 1
        await adapter.wrap(os.urandom(32), context=context)
        assert gcp_mock.token_requests == 2

    async def test_persistently_rejected_token_is_a_platform_credential_error(
        self, adapter, gcp_mock
    ) -> None:
        gcp_mock.reject_next_token_use = 2
        with pytest.raises(KmsCredentialsError) as raised:
            await adapter.wrap(os.urandom(32), context=wrap_context(TENANT_A))
        # Our credential failing must NOT read as the customer revoking access.
        assert not isinstance(raised.value, KmsAccessDeniedError)
        assert isinstance(raised.value, KmsUnavailableError)

    async def test_revoked_grant_is_access_denied(self, adapter, gcp_mock) -> None:
        gcp_mock.behavior.deny = True
        with pytest.raises(KmsAccessDeniedError):
            await adapter.wrap(os.urandom(32), context=wrap_context(TENANT_A))

    async def test_disabled_primary_version_is_access_denied(self, adapter, gcp_mock) -> None:
        gcp_mock.keys[GCP_KEY].state = "DISABLED"
        with pytest.raises(KmsAccessDeniedError):
            await adapter.wrap(os.urandom(32), context=wrap_context(TENANT_A))

    async def test_outage_is_transient(self, adapter, gcp_mock) -> None:
        gcp_mock.behavior.fail_status = 503
        with pytest.raises(KmsUnavailableError):
            await adapter.wrap(os.urandom(32), context=wrap_context(TENANT_A))

    async def test_hung_endpoint_hits_the_hard_timeout(self, make_harness, gcp_mock) -> None:
        harness = make_harness(timeout_s=0.3)
        gcp_mock.add_key(GCP_KEY, labels={PROOF_LABEL: EXTERNAL_ID})
        adapter = harness.providers.registry.build(
            make_config(PROVIDER_GCP, GCP_KEY, external_id=EXTERNAL_ID)
        )
        gcp_mock.behavior.delay_s = 1.5
        with pytest.raises(KmsUnavailableError) as raised:
            await adapter.wrap(os.urandom(32), context=wrap_context(TENANT_A))
        assert raised.value.code == "Timeout"

    async def test_connection_refused_is_transient(self, make_harness, gcp_mock) -> None:
        harness = make_harness()
        adapter = harness.providers.registry.build(
            make_config(PROVIDER_GCP, GCP_KEY, external_id=EXTERNAL_ID)
        )
        gcp_mock.stop()
        with pytest.raises(KmsUnavailableError):
            await adapter.wrap(os.urandom(32), context=wrap_context(TENANT_A))

    async def test_verify_requires_the_proof_of_control_label(self, make_harness, gcp_mock) -> None:
        harness = make_harness()
        gcp_mock.add_key(GCP_KEY)  # no label
        adapter = harness.providers.registry.build(
            make_config(PROVIDER_GCP, GCP_KEY, external_id=EXTERNAL_ID)
        )
        with pytest.raises(KmsConfigError, match=PROOF_LABEL):
            await adapter.verify()

    async def test_verify_rejects_someone_elses_external_id(self, make_harness, gcp_mock) -> None:
        harness = make_harness()
        gcp_mock.add_key(GCP_KEY, labels={PROOF_LABEL: "0" * 48})
        adapter = harness.providers.registry.build(
            make_config(PROVIDER_GCP, GCP_KEY, external_id=EXTERNAL_ID)
        )
        with pytest.raises(KmsConfigError, match=PROOF_LABEL):
            await adapter.verify()

    @pytest.mark.parametrize(
        ("attr", "value", "fragment"),
        [
            ("purpose", "ASYMMETRIC_SIGN", "ENCRYPT_DECRYPT"),
            ("algorithm", "RSA_DECRYPT_OAEP_2048_SHA256", "GOOGLE_SYMMETRIC_ENCRYPTION"),
            ("state", "DISABLED", "ENABLED"),
        ],
    )
    async def test_verify_rejects_wrong_key_shapes(
        self, make_harness, gcp_mock, attr, value, fragment
    ) -> None:
        harness = make_harness()
        key = gcp_mock.add_key(GCP_KEY, labels={PROOF_LABEL: EXTERNAL_ID})
        setattr(key, attr, value)
        adapter = harness.providers.registry.build(
            make_config(PROVIDER_GCP, GCP_KEY, external_id=EXTERNAL_ID)
        )
        with pytest.raises(KmsConfigError, match=fragment):
            await adapter.verify()

    async def test_verify_succeeds_for_a_healthy_labelled_key(self, adapter) -> None:
        info = await adapter.verify()
        assert (info.provider, info.key_ref, info.key_state) == (PROVIDER_GCP, GCP_KEY, "ENABLED")


class TestMetadataServerIdentity:
    """With no service-account key, the GKE/GCE metadata identity is used."""

    async def test_metadata_token_is_used_when_no_key_is_configured(self, gcp_mock) -> None:
        gcp_mock.add_key(GCP_KEY, labels={PROOF_LABEL: EXTERNAL_ID})
        runtime = GcpRuntime(
            GcpPlatformSettings(
                service_account=None, api_base=gcp_mock.api_base, metadata_url=gcp_mock.metadata_url
            )
        )
        adapter = GcpKmsAdapter(
            GcpKeySpec(tenant_id=TENANT_A, key_name=GCP_KEY, external_id=EXTERNAL_ID), runtime
        )
        context = wrap_context(TENANT_A)
        dek = os.urandom(32)
        assert (
            await adapter.unwrap(await adapter.wrap(dek, context=context), context=context) == dek
        )
        metadata_calls = [r for r in gcp_mock.server.calls() if "computeMetadata" in r.path]
        assert metadata_calls[0].headers["metadata-flavor"] == "Google"
        await runtime.http.aclose()

    async def test_token_endpoint_rejection_is_a_platform_credential_error(self, gcp_mock) -> None:
        other = GcpPlatformSettings(
            service_account=ServiceAccountKey.parse(
                gcp_mock.service_account_json().replace(gcp_mock.client_email, "evil@x.com")
            ),
            api_base=gcp_mock.api_base,
        )
        runtime = GcpRuntime(other)
        adapter = GcpKmsAdapter(
            GcpKeySpec(tenant_id=TENANT_A, key_name=GCP_KEY, external_id=EXTERNAL_ID), runtime
        )
        with pytest.raises(KmsCredentialsError):
            await adapter.wrap(os.urandom(32), context=wrap_context(TENANT_A))
        await runtime.http.aclose()
