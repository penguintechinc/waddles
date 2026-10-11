"""AWS KMS adapter: config validation, and the real boto3 -> mock-socket wrap/unwrap path."""

from __future__ import annotations

import os

import pytest

from services.envelope.aws_kms import map_client_error, validate_aws_config
from services.envelope.crypto import wrap_context
from services.envelope.errors import (
    KmsAccessDeniedError,
    KmsConfigError,
    KmsRejectedError,
    KmsUnavailableError,
)
from services.envelope.kms_adapter import PROVIDER_AWS
from tests.envelope.conftest import AWS_KEY_ARN, AWS_ROLE_ARN, make_config
from tests.envelope.fakes import TENANT_A, TENANT_B

EXTERNAL_ID = "cd" * 24


class TestValidateAwsConfig:
    """Tenant-supplied AWS config is validated strictly at the boundary."""

    def test_accepts_a_key_arn_and_role(self) -> None:
        canonical = validate_aws_config(AWS_KEY_ARN, None, AWS_ROLE_ARN)
        assert canonical.key_ref == AWS_KEY_ARN
        assert canonical.region == "us-east-1"
        assert canonical.principal == AWS_ROLE_ARN

    @pytest.mark.parametrize(
        ("key_ref", "region", "role", "fragment"),
        [
            ("alias/my-key", None, AWS_ROLE_ARN, "key ARN"),
            (AWS_KEY_ARN.replace("arn:aws:", "arn:aws-cn:"), None, AWS_ROLE_ARN, "key ARN"),
            (AWS_KEY_ARN, None, None, "principal"),
            (AWS_KEY_ARN, None, "arn:aws:iam::111122223333:user/bob", "IAM role ARN"),
            (AWS_KEY_ARN, None, AWS_ROLE_ARN.replace("arn:aws:", "arn:aws-us-gov:"), "partition"),
            (AWS_KEY_ARN, "eu-west-1", AWS_ROLE_ARN, "region must match"),
            (AWS_KEY_ARN, "not a region", AWS_ROLE_ARN, "valid AWS region"),
            ("", None, AWS_ROLE_ARN, "key ARN"),
        ],
    )
    def test_rejects_malformed_or_unsafe_config(
        self, key_ref: str, region: str | None, role: str | None, fragment: str
    ) -> None:
        with pytest.raises(KmsConfigError, match=fragment):
            validate_aws_config(key_ref, region, role)


class TestErrorMapping:
    """Provider error codes map onto the denied / transient / rejected taxonomy."""

    @pytest.mark.parametrize(
        ("code", "status", "expected"),
        [
            ("AccessDeniedException", 400, KmsAccessDeniedError),
            ("DisabledException", 400, KmsAccessDeniedError),
            ("NotFoundException", 400, KmsAccessDeniedError),
            ("KMSInvalidStateException", 400, KmsAccessDeniedError),
            ("ThrottlingException", 400, KmsUnavailableError),
            ("KMSInternalException", 500, KmsUnavailableError),
            ("SomethingNew", 503, KmsUnavailableError),
            # Our own credentials failing is OUR outage, never a customer revocation.
            ("UnrecognizedClientException", 400, KmsUnavailableError),
            ("InvalidCiphertextException", 400, KmsRejectedError),
        ],
    )
    def test_classification(self, code: str, status: int, expected: type[Exception]) -> None:
        from botocore.exceptions import ClientError

        error = ClientError(
            {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "Op"
        )
        mapped = map_client_error(error)
        assert type(mapped) is expected
        assert mapped.code == code
        assert "arn:" not in str(mapped)  # provider text/identifiers never copied through


class TestAdapterAgainstMockSocket:
    """The real boto3 stack (SigV4, STS AssumeRole, KMS JSON-1.1) against the loopback mock."""

    @pytest.fixture
    def adapter(self, make_harness, aws_mock):
        harness = make_harness()
        aws_mock.trust(AWS_ROLE_ARN, EXTERNAL_ID)
        config = make_config(
            PROVIDER_AWS, AWS_KEY_ARN, principal=AWS_ROLE_ARN, external_id=EXTERNAL_ID
        )
        return harness.providers.registry.build(config)

    async def test_wrap_unwrap_round_trips_through_real_kms_math(self, adapter, aws_mock) -> None:
        dek = os.urandom(32)
        context = wrap_context(TENANT_A)
        wrapped = await adapter.wrap(dek, context=context)
        assert wrapped != dek
        assert await adapter.unwrap(wrapped, context=context) == dek
        # The customer-side contract: the context is bound on both calls.
        encrypt = aws_mock.kms_calls("Encrypt")[0].json()
        assert encrypt["EncryptionContext"] == {
            "waddles_purpose": "tenant-dek",
            "waddles_tenant_id": str(TENANT_A),
        }
        assert encrypt["KeyId"] == AWS_KEY_ARN

    async def test_wrapped_dek_does_not_unwrap_under_another_tenants_context(self, adapter) -> None:
        wrapped = await adapter.wrap(os.urandom(32), context=wrap_context(TENANT_A))
        with pytest.raises(KmsRejectedError):
            await adapter.unwrap(wrapped, context=wrap_context(TENANT_B))

    async def test_assume_role_with_the_wrong_external_id_is_access_denied(
        self, make_harness, aws_mock
    ) -> None:
        harness = make_harness()
        aws_mock.trust(AWS_ROLE_ARN, "someone-elses-external-id")
        config = make_config(
            PROVIDER_AWS, AWS_KEY_ARN, principal=AWS_ROLE_ARN, external_id=EXTERNAL_ID
        )
        adapter = harness.providers.registry.build(config)
        with pytest.raises(KmsAccessDeniedError):
            await adapter.wrap(os.urandom(32), context=wrap_context(TENANT_A))
        assert aws_mock.kms_calls("Encrypt") == []  # never reached KMS

    async def test_revoked_grant_is_access_denied(self, adapter, aws_mock) -> None:
        aws_mock.behavior.deny = True
        with pytest.raises(KmsAccessDeniedError):
            await adapter.unwrap(b"x" * 40, context=wrap_context(TENANT_A))

    async def test_server_error_is_transient(self, adapter, aws_mock) -> None:
        aws_mock.behavior.fail_status = 500
        with pytest.raises(KmsUnavailableError):
            await adapter.wrap(os.urandom(32), context=wrap_context(TENANT_A))

    async def test_hung_kms_hits_the_hard_timeout(self, make_harness, aws_mock) -> None:
        harness = make_harness(timeout_s=0.3)
        aws_mock.trust(AWS_ROLE_ARN, EXTERNAL_ID)
        adapter = harness.providers.registry.build(
            make_config(PROVIDER_AWS, AWS_KEY_ARN, principal=AWS_ROLE_ARN, external_id=EXTERNAL_ID)
        )
        aws_mock.behavior.delay_s = 1.5
        with pytest.raises(KmsUnavailableError) as raised:
            await adapter.wrap(os.urandom(32), context=wrap_context(TENANT_A))
        assert raised.value.code == "Timeout"

    async def test_expired_session_credentials_are_refreshed_once(self, adapter, aws_mock) -> None:
        context = wrap_context(TENANT_A)
        wrapped = await adapter.wrap(os.urandom(32), context=context)
        sts_before = len(aws_mock.server.calls(lambda r: "x-amz-target" not in r.headers))
        aws_mock.expire_session_once = True
        assert len(await adapter.unwrap(wrapped, context=context)) == 32
        sts_after = len(aws_mock.server.calls(lambda r: "x-amz-target" not in r.headers))
        assert sts_after == sts_before + 1  # exactly one fresh AssumeRole

    async def test_verify_accepts_a_healthy_key(self, adapter) -> None:
        info = await adapter.verify()
        assert (info.provider, info.key_ref, info.key_state) == (
            PROVIDER_AWS,
            AWS_KEY_ARN,
            "Enabled",
        )

    async def test_verify_rejects_a_disabled_key(self, adapter, aws_mock) -> None:
        aws_mock.behavior.key_state = "Disabled"
        with pytest.raises(KmsConfigError, match="Disabled"):
            await adapter.verify()

    async def test_verify_rejects_a_non_encrypt_key(self, adapter, aws_mock) -> None:
        aws_mock.key_usage[AWS_KEY_ARN] = "SIGN_VERIFY"
        with pytest.raises(KmsConfigError, match="symmetric"):
            await adapter.verify()

    async def test_unknown_key_is_treated_as_revoked(self, make_harness, aws_mock) -> None:
        harness = make_harness()
        aws_mock.trust(AWS_ROLE_ARN, EXTERNAL_ID)
        other = AWS_KEY_ARN.replace("1234abcd", "ffffffff")
        adapter = harness.providers.registry.build(
            make_config(PROVIDER_AWS, other, principal=AWS_ROLE_ARN, external_id=EXTERNAL_ID)
        )
        with pytest.raises(KmsAccessDeniedError):
            await adapter.wrap(os.urandom(32), context=wrap_context(TENANT_A))
