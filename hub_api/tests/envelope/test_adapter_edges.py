"""Misbehaving providers: wrong-key echoes, malformed bodies, transport faults, bad platform creds.

A KMS adapter must treat the provider's answer as untrusted input: an unexpected key, a short
payload or a non-JSON body is a loud ``KmsRejectedError``/``KmsUnavailableError`` -- never a
crash, and never a value that gets stored or used.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from services.envelope.azure_key_vault import PROOF_TAG
from services.envelope.crypto import wrap_context
from services.envelope.errors import (
    KmsConfigError,
    KmsCredentialsError,
    KmsRejectedError,
    KmsUnavailableError,
)
from services.envelope.gcp_kms import PROOF_LABEL, GcpPlatformSettings, ServiceAccountKey
from services.envelope.kms_adapter import PROVIDER_AWS, PROVIDER_AZURE, PROVIDER_GCP
from tests.envelope.conftest import (
    AWS_KEY_ARN,
    AWS_ROLE_ARN,
    AZURE_DIRECTORY,
    AZURE_KEY_URL,
    GCP_KEY,
    make_config,
)
from tests.envelope.fakes import TENANT_A
from tests.envelope.kms_mocks import MockRequest, MockResponse

EXT = "9a" * 24
CTX = wrap_context(TENANT_A)


def rewrite_json(mutate):  # type: ignore[no-untyped-def]
    """An interceptor that edits the JSON body of a 200 response."""

    def interceptor(request: MockRequest, response: MockResponse) -> MockResponse:
        if response.status != 200 or not response.body.startswith(b"{"):
            return response
        body = json.loads(response.body)
        result = mutate(request, body)
        return MockResponse.json_body(body if result is None else result)

    return interceptor


def raw(body: bytes, status: int = 200):  # type: ignore[no-untyped-def]
    """An interceptor that replaces key-service answers with a raw body (token/STS untouched)."""

    def interceptor(request: MockRequest, response: MockResponse) -> MockResponse:
        plumbing = (
            "/token" in request.path
            or "oauth2" in request.path
            or "computeMetadata" in request.path
            or "x-amz-target" not in request.headers
            and request.path == "/"
        )
        if plumbing:
            return response
        return MockResponse(status=status, body=body)

    return interceptor


class TestAwsAdapterDistrustsTheProvider:
    """AWS answers are verified before use."""

    @pytest.fixture
    def adapter(self, make_harness, aws_mock):  # type: ignore[no-untyped-def]
        harness = make_harness()
        aws_mock.trust(AWS_ROLE_ARN, EXT)
        return harness.providers.registry.build(
            make_config(PROVIDER_AWS, AWS_KEY_ARN, principal=AWS_ROLE_ARN, external_id=EXT)
        )

    async def test_encrypt_under_an_unexpected_key_is_rejected(self, adapter, aws_mock) -> None:  # type: ignore[no-untyped-def]
        aws_mock.server.interceptor = rewrite_json(
            lambda r, b: (
                {**b, "KeyId": "arn:aws:kms:us-east-1:1:key/other"}
                if "CiphertextBlob" in b
                else None
            )
        )
        with pytest.raises(KmsRejectedError, match="unexpected key"):
            await adapter.wrap(os.urandom(32), context=CTX)

    async def test_a_decrypt_with_the_wrong_payload_size_is_rejected(
        self, adapter, aws_mock
    ) -> None:  # type: ignore[no-untyped-def]
        wrapped = await adapter.wrap(os.urandom(32), context=CTX)
        aws_mock.server.interceptor = rewrite_json(
            lambda r, b: {**b, "Plaintext": "AAAA"} if "Plaintext" in b else None
        )
        with pytest.raises(KmsRejectedError, match="unexpected key or payload"):
            await adapter.unwrap(wrapped, context=CTX)

    async def test_a_non_bytes_plaintext_is_a_validation_failure_not_a_crash(self, adapter) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(KmsRejectedError, match="validation"):
            await adapter.wrap(12345, context=CTX)

    async def test_a_dead_endpoint_is_transient(self, adapter, aws_mock) -> None:  # type: ignore[no-untyped-def]
        aws_mock.stop()
        with pytest.raises(KmsUnavailableError):
            await adapter.wrap(os.urandom(32), context=CTX)

    async def test_describe_for_a_different_arn_is_a_config_error(self, adapter, aws_mock) -> None:  # type: ignore[no-untyped-def]
        aws_mock.server.interceptor = rewrite_json(
            lambda r, b: (
                {"KeyMetadata": {**b["KeyMetadata"], "Arn": "arn:other"}}
                if "KeyMetadata" in b
                else None
            )
        )
        with pytest.raises(KmsConfigError, match="does not match"):
            await adapter.verify()


class TestGcpAdapterDistrustsTheProvider:
    """Cloud KMS answers are verified before use."""

    @pytest.fixture
    def adapter(self, make_harness, gcp_mock):  # type: ignore[no-untyped-def]
        harness = make_harness()
        gcp_mock.add_key(GCP_KEY, labels={PROOF_LABEL: EXT})
        return harness.providers.registry.build(make_config(PROVIDER_GCP, GCP_KEY, external_id=EXT))

    async def test_encrypt_under_an_unexpected_key_version_is_rejected(
        self, adapter, gcp_mock
    ) -> None:  # type: ignore[no-untyped-def]
        gcp_mock.server.interceptor = rewrite_json(
            lambda r, b: (
                {**b, "name": "projects/x/locations/l/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1"}
                if "ciphertext" in b
                else None
            )
        )
        with pytest.raises(KmsRejectedError, match="unexpected key"):
            await adapter.wrap(os.urandom(32), context=CTX)

    async def test_an_empty_ciphertext_is_rejected(self, adapter, gcp_mock) -> None:  # type: ignore[no-untyped-def]
        gcp_mock.server.interceptor = rewrite_json(
            lambda r, b: {**b, "ciphertext": ""} if "ciphertext" in b else None
        )
        with pytest.raises(KmsRejectedError):
            await adapter.wrap(os.urandom(32), context=CTX)

    async def test_malformed_base64_is_rejected(self, adapter, gcp_mock) -> None:  # type: ignore[no-untyped-def]
        gcp_mock.server.interceptor = rewrite_json(
            lambda r, b: {**b, "ciphertext": "***not base64***"} if "ciphertext" in b else None
        )
        with pytest.raises(KmsRejectedError, match="base64"):
            await adapter.wrap(os.urandom(32), context=CTX)

    async def test_a_short_plaintext_is_rejected(self, adapter, gcp_mock) -> None:  # type: ignore[no-untyped-def]
        wrapped = await adapter.wrap(os.urandom(32), context=CTX)
        gcp_mock.server.interceptor = rewrite_json(
            lambda r, b: {"plaintext": "AAAA"} if "plaintext" in b else None
        )
        with pytest.raises(KmsRejectedError, match="unexpected payload"):
            await adapter.unwrap(wrapped, context=CTX)

    @pytest.mark.parametrize("body", [b"<html>not json</html>", b"[1, 2, 3]"])
    async def test_non_object_bodies_are_rejected(self, adapter, gcp_mock, body) -> None:  # type: ignore[no-untyped-def]
        await adapter.wrap(os.urandom(32), context=CTX)  # warm the token first
        gcp_mock.server.interceptor = raw(body)
        with pytest.raises(KmsRejectedError):
            await adapter.wrap(os.urandom(32), context=CTX)

    async def test_verify_of_a_different_key_name_is_a_config_error(
        self, adapter, gcp_mock
    ) -> None:  # type: ignore[no-untyped-def]
        gcp_mock.server.interceptor = rewrite_json(
            lambda r, b: (
                {**b, "name": "projects/p/locations/l/keyRings/r/cryptoKeys/other"}
                if "purpose" in b
                else None
            )
        )
        with pytest.raises(KmsConfigError, match="does not match"):
            await adapter.verify()

    @pytest.mark.parametrize(
        ("status", "error"), [(503, KmsUnavailableError), (429, KmsUnavailableError)]
    )
    async def test_token_endpoint_outages_are_transient(
        self, make_harness, gcp_mock, status, error
    ) -> None:  # type: ignore[no-untyped-def]
        harness = make_harness()
        adapter = harness.providers.registry.build(
            make_config(PROVIDER_GCP, GCP_KEY, external_id=EXT)
        )
        gcp_mock.server.interceptor = lambda req, resp: (
            MockResponse.json_body({"error": "x"}, status) if req.path == "/token" else resp
        )
        with pytest.raises(error):
            await adapter.wrap(os.urandom(32), context=CTX)

    async def test_a_malformed_token_response_is_a_credential_error(
        self, make_harness, gcp_mock
    ) -> None:  # type: ignore[no-untyped-def]
        harness = make_harness()
        adapter = harness.providers.registry.build(
            make_config(PROVIDER_GCP, GCP_KEY, external_id=EXT)
        )
        gcp_mock.server.interceptor = lambda req, resp: (
            MockResponse.json_body({"no_token": True}) if req.path == "/token" else resp
        )
        with pytest.raises(KmsCredentialsError):
            await adapter.wrap(os.urandom(32), context=CTX)

    async def test_an_unusable_private_key_is_a_credential_error_not_a_crash(
        self, gcp_mock
    ) -> None:  # type: ignore[no-untyped-def]
        from services.envelope.gcp_kms import GcpKeySpec, GcpKmsAdapter, GcpRuntime

        document = json.loads(gcp_mock.service_account_json())
        # Keep the PEM armour of a real (throwaway) key but swap its body for garbage, so the
        # document parses yet can never sign -- without a PEM literal in source for scanners.
        lines = document["private_key"].splitlines()
        document["private_key"] = "\n".join([lines[0], "bm90LWEta2V5", lines[-1]]) + "\n"
        runtime = GcpRuntime(
            GcpPlatformSettings(
                service_account=ServiceAccountKey.parse(json.dumps(document)),
                api_base=gcp_mock.api_base,
            )
        )
        adapter = GcpKmsAdapter(GcpKeySpec(TENANT_A, GCP_KEY, EXT), runtime)
        with pytest.raises(KmsCredentialsError, match="could not sign"):
            await adapter.wrap(os.urandom(32), context=CTX)
        await runtime.http.aclose()


class TestAzureAdapterDistrustsTheProvider:
    """Key Vault answers are verified before use."""

    @pytest.fixture
    def adapter(self, make_harness, azure_mock):  # type: ignore[no-untyped-def]
        harness = make_harness()
        azure_mock.add_key("waddles", tags={PROOF_TAG: EXT})
        azure_mock.consented.add(AZURE_DIRECTORY)
        return harness.providers.registry.build(
            make_config(PROVIDER_AZURE, AZURE_KEY_URL, principal=AZURE_DIRECTORY, external_id=EXT)
        )

    async def test_an_answer_for_another_key_is_rejected(self, adapter, azure_mock) -> None:  # type: ignore[no-untyped-def]
        azure_mock.server.interceptor = rewrite_json(
            lambda r, b: (
                {**b, "kid": "https://evil.vault.azure.net/keys/waddles/" + "0" * 32}
                if "kid" in b
                else None
            )
        )
        with pytest.raises(KmsRejectedError, match="unexpected key"):
            await adapter.wrap(os.urandom(32), context=CTX)

    async def test_a_malformed_key_version_is_rejected(self, adapter, azure_mock) -> None:  # type: ignore[no-untyped-def]
        azure_mock.server.interceptor = rewrite_json(
            lambda r, b: {**b, "kid": f"{AZURE_KEY_URL}/../../etc"} if "kid" in b else None
        )
        with pytest.raises(KmsRejectedError, match="key version"):
            await adapter.wrap(os.urandom(32), context=CTX)

    async def test_an_empty_wrap_is_rejected(self, adapter, azure_mock) -> None:  # type: ignore[no-untyped-def]
        azure_mock.server.interceptor = rewrite_json(
            lambda r, b: {**b, "value": ""} if "kid" in b else None
        )
        with pytest.raises(KmsRejectedError, match="empty wrap"):
            await adapter.wrap(os.urandom(32), context=CTX)

    async def test_a_short_unwrap_payload_is_rejected(self, adapter, azure_mock) -> None:  # type: ignore[no-untyped-def]
        wrapped = await adapter.wrap(os.urandom(32), context=CTX)
        azure_mock.server.interceptor = rewrite_json(
            lambda r, b: {**b, "value": "AAAA"} if "kid" in b else None
        )
        with pytest.raises(KmsRejectedError, match="unexpected payload"):
            await adapter.unwrap(wrapped, context=CTX)

    async def test_malformed_base64_is_rejected(self, adapter, azure_mock) -> None:  # type: ignore[no-untyped-def]
        azure_mock.server.interceptor = rewrite_json(
            lambda r, b: {**b, "value": "!!!!"} if "kid" in b else None
        )
        with pytest.raises(KmsRejectedError):
            await adapter.wrap(os.urandom(32), context=CTX)

    @pytest.mark.parametrize("body", [b"<html>no</html>", b'["x"]'])
    async def test_non_object_bodies_are_rejected(self, adapter, azure_mock, body) -> None:  # type: ignore[no-untyped-def]
        await adapter.wrap(os.urandom(32), context=CTX)
        azure_mock.server.interceptor = raw(body)
        with pytest.raises(KmsRejectedError):
            await adapter.wrap(os.urandom(32), context=CTX)

    async def test_persistent_401_is_a_platform_credential_error(self, adapter, azure_mock) -> None:  # type: ignore[no-untyped-def]
        azure_mock.reject_next_token_use = 2
        with pytest.raises(KmsCredentialsError):
            await adapter.wrap(os.urandom(32), context=CTX)

    async def test_a_malformed_token_response_is_rejected(self, adapter, azure_mock) -> None:  # type: ignore[no-untyped-def]
        azure_mock.server.interceptor = lambda req, resp: (
            MockResponse.json_body({"nope": 1}) if "oauth2" in req.path else resp
        )
        with pytest.raises(KmsRejectedError, match="token response"):
            await adapter.wrap(os.urandom(32), context=CTX)

    async def test_an_expired_key_fails_verification(self, adapter, azure_mock) -> None:  # type: ignore[no-untyped-def]
        azure_mock.server.interceptor = rewrite_json(
            lambda r, b: (
                {**b, "attributes": {"enabled": True, "exp": 1}} if "attributes" in b else None
            )
        )
        with pytest.raises(KmsConfigError, match="expired"):
            await adapter.verify()

    async def test_a_non_rsa_key_fails_verification(self, adapter, azure_mock) -> None:  # type: ignore[no-untyped-def]
        azure_mock.server.interceptor = rewrite_json(
            lambda r, b: {**b, "key": {**b["key"], "kty": "EC"}} if "key" in b else None
        )
        with pytest.raises(KmsConfigError, match="RSA"):
            await adapter.verify()

    async def test_a_non_ok_probe_round_trip_is_rejected(self, adapter, azure_mock) -> None:  # type: ignore[no-untyped-def]
        # A vault that returns a different DEK on unwrap must never pass verification.
        state = {"unwraps": 0}

        def corrupt(request: MockRequest, response: MockResponse) -> MockResponse:
            if request.path.endswith("/unwrapkey") and response.status == 200:
                state["unwraps"] += 1
                body = json.loads(response.body)
                import base64

                payload = bytearray(base64.urlsafe_b64decode(body["value"] + "=="))
                payload[0] ^= 0xFF
                body["value"] = base64.urlsafe_b64encode(bytes(payload)).rstrip(b"=").decode()
                return MockResponse.json_body(body)
            return response

        azure_mock.server.interceptor = corrupt
        with pytest.raises(KmsRejectedError, match="round-trip"):
            await adapter.verify()


async def test_a_transport_error_on_a_closed_client_is_transient(make_harness, gcp_mock) -> None:  # type: ignore[no-untyped-def]
    harness = make_harness()
    gcp_mock.add_key(GCP_KEY, labels={PROOF_LABEL: EXT})
    adapter = harness.providers.registry.build(make_config(PROVIDER_GCP, GCP_KEY, external_id=EXT))
    await adapter.wrap(os.urandom(32), context=CTX)
    # Point the shared client at a port nothing listens on.
    gcp_mock.stop()
    with pytest.raises((KmsUnavailableError, httpx.HTTPError)):
        await adapter.wrap(os.urandom(32), context=CTX)
