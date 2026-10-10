"""Provider settings parsing, registry assembly, and the process runtime wiring."""

from __future__ import annotations

import os

import pytest

from services.envelope import KmsConfigError, UnsupportedKmsProviderError
from services.envelope.kms_adapter import PROVIDER_AWS, PROVIDER_AZURE, PROVIDER_GCP
from services.envelope.platform_kek import LazyPlatformKek, PlatformKekAdapter
from services.envelope.providers import ProviderSettings, build_providers
from services.envelope.runtime import build_envelope_runtime, tenant_slug_resolver
from tests.envelope.conftest import AWS_KEY_ARN, AWS_ROLE_ARN
from tests.envelope.fakes import FakeGate, InMemoryEnvelopeRepo

GCP_ENV_KEY = "ENVELOPE_GCP_CREDENTIALS_JSON"


class TestProviderSettings:
    """``ENVELOPE_KMS_PROVIDERS`` defaults to nothing -- the flag-OFF posture."""

    def test_nothing_is_enabled_by_default(self) -> None:
        settings = ProviderSettings.from_env({})
        assert settings.enabled == frozenset()
        assert settings.gcp is None and settings.azure is None

    def test_enables_exactly_the_listed_providers(self) -> None:
        env = {
            "ENVELOPE_KMS_PROVIDERS": " aws_kms , gcp_kms ",
            "ENVELOPE_AWS_KMS_ENDPOINT_URL": "https://kms-fips.us-east-1.amazonaws.com",
            "ENVELOPE_KMS_TIMEOUT_S": "4",
        }
        settings = ProviderSettings.from_env(env)
        assert settings.enabled == {PROVIDER_AWS, PROVIDER_GCP}
        assert settings.aws_kms_endpoint_url == "https://kms-fips.us-east-1.amazonaws.com"
        assert settings.timeout_s == 4.0
        assert settings.gcp is not None and settings.azure is None

    def test_unknown_providers_fail_loudly(self) -> None:
        with pytest.raises(KmsConfigError, match="unknown provider"):
            ProviderSettings.from_env({"ENVELOPE_KMS_PROVIDERS": "aws_kms,hashicorp_vault"})

    @pytest.mark.parametrize("name", ["KMS", "STS"])
    def test_aws_endpoint_overrides_must_be_https(self, name: str) -> None:
        with pytest.raises(KmsConfigError, match="https"):
            ProviderSettings.from_env(
                {
                    "ENVELOPE_KMS_PROVIDERS": "aws_kms",
                    f"ENVELOPE_AWS_{name}_ENDPOINT_URL": "http://169.254.169.254/",
                }
            )

    def test_enabled_azure_without_credentials_fails_at_startup(self) -> None:
        with pytest.raises(KmsConfigError, match="ENVELOPE_AZURE_CLIENT_ID"):
            ProviderSettings.from_env({"ENVELOPE_KMS_PROVIDERS": "azure_key_vault"})

    def test_enabled_gcp_with_a_malformed_credentials_document_fails_at_startup(self) -> None:
        with pytest.raises(KmsConfigError, match="JSON"):
            ProviderSettings.from_env(
                {"ENVELOPE_KMS_PROVIDERS": "gcp_kms", GCP_ENV_KEY: "{not json"}
            )

    def test_secrets_never_appear_in_the_settings_repr(self, gcp_mock) -> None:
        settings = ProviderSettings.from_env(
            {
                "ENVELOPE_KMS_PROVIDERS": "gcp_kms,azure_key_vault",
                GCP_ENV_KEY: gcp_mock.service_account_json(),
                "ENVELOPE_AZURE_CLIENT_ID": "cid",
                "ENVELOPE_AZURE_CLIENT_SECRET": "super-secret-azure",
            }
        )
        rendered = repr(settings)
        assert "super-secret-azure" not in rendered and "PRIVATE KEY" not in rendered


class TestPlatformPrincipals:
    """The public identities customers must trust/grant -- identifiers, never credentials."""

    def test_principals_come_from_env_and_the_loaded_credentials(self, gcp_mock) -> None:
        settings = ProviderSettings.from_env(
            {
                "ENVELOPE_KMS_PROVIDERS": "aws_kms,gcp_kms,azure_key_vault",
                "ENVELOPE_AWS_PLATFORM_PRINCIPAL": "arn:aws:iam::999988887777:role/waddles-hub-api",
                GCP_ENV_KEY: gcp_mock.service_account_json(),
                "ENVELOPE_AZURE_CLIENT_ID": "11111111-2222-3333-4444-555555555555",
                "ENVELOPE_AZURE_CLIENT_SECRET": "secret",
            }
        )
        assert settings.platform_principals == {
            PROVIDER_AWS: "arn:aws:iam::999988887777:role/waddles-hub-api",
            PROVIDER_GCP: gcp_mock.client_email,
            PROVIDER_AZURE: "11111111-2222-3333-4444-555555555555",
        }

    def test_gcp_metadata_identity_needs_an_explicit_principal_to_be_shown(self) -> None:
        env = {"ENVELOPE_KMS_PROVIDERS": "gcp_kms"}
        assert ProviderSettings.from_env(env).platform_principals == {}
        env["ENVELOPE_GCP_PLATFORM_PRINCIPAL"] = "waddles@proj.iam.gserviceaccount.com"
        assert ProviderSettings.from_env(env).platform_principals == {
            PROVIDER_GCP: "waddles@proj.iam.gserviceaccount.com"
        }

    def test_only_enabled_providers_are_listed(self) -> None:
        env = {
            "ENVELOPE_KMS_PROVIDERS": "azure_key_vault",
            "ENVELOPE_AWS_PLATFORM_PRINCIPAL": "arn:aws:iam::1:role/x",
            "ENVELOPE_AZURE_CLIENT_ID": "cid",
            "ENVELOPE_AZURE_CLIENT_SECRET": "s",
        }
        assert set(ProviderSettings.from_env(env).platform_principals) == {PROVIDER_AZURE}

    @pytest.mark.parametrize("bad", ["has space", "x" * 600, "new\nline", "<script>"])
    def test_a_malformed_principal_fails_at_startup(self, bad: str) -> None:
        with pytest.raises(KmsConfigError, match="ENVELOPE_AWS_PLATFORM_PRINCIPAL"):
            ProviderSettings.from_env(
                {"ENVELOPE_KMS_PROVIDERS": "aws_kms", "ENVELOPE_AWS_PLATFORM_PRINCIPAL": bad}
            )


class TestBuildProviders:
    """The registry holds exactly the enabled providers; others are refused, not stubbed."""

    async def test_registers_only_enabled_providers(self) -> None:
        providers = build_providers(ProviderSettings(enabled=frozenset({PROVIDER_AWS})))
        assert providers.registry.supported() == (PROVIDER_AWS,)
        with pytest.raises(UnsupportedKmsProviderError, match="not enabled"):
            providers.registry.validate(PROVIDER_GCP, key_ref="x", region=None, principal=None)
        await providers.aclose()

    async def test_builds_runtimes_from_settings_when_none_are_injected(self, gcp_mock) -> None:
        settings = ProviderSettings.from_env(
            {
                "ENVELOPE_KMS_PROVIDERS": "aws_kms,gcp_kms,azure_key_vault",
                GCP_ENV_KEY: gcp_mock.service_account_json(),
                "ENVELOPE_AZURE_CLIENT_ID": "cid",
                "ENVELOPE_AZURE_CLIENT_SECRET": "secret",
            }
        )
        providers = build_providers(settings)
        assert providers.registry.supported() == (PROVIDER_AWS, PROVIDER_AZURE, PROVIDER_GCP)
        await providers.aclose()
        await providers.aclose()  # idempotent

    def test_an_enabled_provider_with_no_platform_settings_is_a_config_error(self) -> None:
        with pytest.raises(KmsConfigError, match="gcp_kms"):
            build_providers(ProviderSettings(enabled=frozenset({PROVIDER_GCP})))
        with pytest.raises(KmsConfigError, match="azure_key_vault"):
            build_providers(ProviderSettings(enabled=frozenset({PROVIDER_AZURE})))


class TestRuntime:
    """hub-api's process wiring is inert by default and loud when misconfigured."""

    async def test_zero_config_runtime_has_no_providers_and_still_serves_the_baseline(
        self,
    ) -> None:
        repo = InMemoryEnvelopeRepo()
        runtime = build_envelope_runtime(
            dal=None,  # type: ignore[arg-type]  # the in-memory repo replaces it below
            environ={},
            platform_kek=LazyPlatformKek(lambda: PlatformKekAdapter(os.urandom(32))),
        )
        assert runtime.enabled_providers == ()
        assert runtime.service._registry.supported() == ()
        runtime.service._keys = repo
        runtime.service._configs = repo
        field = await runtime.service.encrypt(
            1, b"x", table="t", column="c", row_uuid="11111111-2222-3333-4444-555555555555"
        )
        assert field.dek_version == 1
        await runtime.aclose()

    async def test_zero_config_runtime_does_not_need_the_platform_kek_until_used(self) -> None:
        runtime = build_envelope_runtime(dal=None, environ={})  # type: ignore[arg-type]
        assert runtime.enabled_providers == ()  # constructing it never read TENANT_KEK_HEX
        await runtime.aclose()

    def test_a_bad_provider_list_fails_the_build(self) -> None:
        with pytest.raises(KmsConfigError):
            build_envelope_runtime(  # type: ignore[arg-type]
                dal=None, environ={"ENVELOPE_KMS_PROVIDERS": "nope"}
            )

    def test_a_bad_tunable_fails_the_build(self) -> None:
        with pytest.raises(ValueError):
            build_envelope_runtime(  # type: ignore[arg-type]
                dal=None, environ={"ENVELOPE_DEK_CACHE_TTL_S": "0"}
            )

    async def test_enabled_providers_are_reported_sorted(self) -> None:
        runtime = build_envelope_runtime(
            dal=None,  # type: ignore[arg-type]
            environ={"ENVELOPE_KMS_PROVIDERS": "aws_kms"},
            gate=FakeGate(),
        )
        assert runtime.enabled_providers == (PROVIDER_AWS,)
        assert AWS_KEY_ARN and AWS_ROLE_ARN
        await runtime.aclose()

    async def test_slug_resolver_reads_the_tenants_table(self) -> None:
        import sqlalchemy as sa
        from penguin_dal import AsyncDB

        dal = AsyncDB("sqlite+aiosqlite:///:memory:", pool_size=1)
        async with dal.engine.begin() as conn:
            await conn.execute(sa.text("CREATE TABLE tenants (id INTEGER PRIMARY KEY, slug TEXT)"))
            await conn.execute(sa.text("INSERT INTO tenants (id, slug) VALUES (5, 'acme-corp')"))
        resolve = tenant_slug_resolver(dal)
        assert await resolve(5) == "acme-corp"
        assert await resolve(6) is None
        await dal.close()
