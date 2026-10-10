"""Fixtures for the envelope / external-KMS suite: mock providers, real adapters, real service."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import boto3
import pytest

from services.envelope import TenantEnvelopeService
from services.envelope.aws_kms import AwsClientProvider
from services.envelope.azure_key_vault import PROOF_TAG, AzurePlatformSettings, AzureRuntime
from services.envelope.gcp_kms import (
    PROOF_LABEL,
    GcpPlatformSettings,
    GcpRuntime,
    ServiceAccountKey,
)
from services.envelope.kms_adapter import PROVIDER_AWS, PROVIDER_AZURE, PROVIDER_GCP
from services.envelope.models import TenantKmsConfig
from services.envelope.providers import KmsProviders, ProviderSettings, build_providers
from services.envelope.service import EnvelopeSettings
from tests.envelope.fakes import (
    SLUG_A,
    TENANT_A,
    CountingPlatformKek,
    FakeGate,
    InMemoryEnvelopeRepo,
)
from tests.envelope.kms_mocks import MockAwsKms, MockAzureKeyVault, MockGcpKms

AWS_KEY_ARN = "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"
AWS_KEY_ARN_2 = "arn:aws:kms:us-east-1:111122223333:key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
AWS_ROLE_ARN = "arn:aws:iam::111122223333:role/waddles-byok"
GCP_KEY = "projects/cust-proj-1/locations/us-east1/keyRings/ring/cryptoKeys/waddles"
AZURE_KEY_URL = "https://contoso-vault.vault.azure.net/keys/waddles"
AZURE_DIRECTORY = "99999999-8888-7777-6666-555555555555"
ALL_PROVIDERS = frozenset({PROVIDER_AWS, PROVIDER_GCP, PROVIDER_AZURE})


class FakeClock:
    """A manually advanced monotonic clock (drives cache TTL / stale-grace tests)."""

    def __init__(self) -> None:
        """Start at a non-zero instant so 'never fetched' (0.0) is distinguishable."""
        self.now = 1_000.0

    def __call__(self) -> float:
        """Current fake time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move time forward."""
        self.now += seconds


@pytest.fixture
def aws_mock() -> Iterator[MockAwsKms]:
    """A running mock AWS KMS+STS with the standard test key created."""
    mock = MockAwsKms()
    mock.add_key(AWS_KEY_ARN)
    yield mock
    mock.stop()


@pytest.fixture
def gcp_mock() -> Iterator[MockGcpKms]:
    """A running mock Cloud KMS (+ token + metadata endpoints)."""
    mock = MockGcpKms()
    yield mock
    mock.stop()


@pytest.fixture
def azure_mock() -> Iterator[MockAzureKeyVault]:
    """A running mock Entra + Key Vault."""
    mock = MockAzureKeyVault()
    yield mock
    mock.stop()


@dataclass(slots=True)
class Harness:
    """A fully wired service: real adapters -> mock provider sockets, in-memory persistence."""

    service: TenantEnvelopeService
    repo: InMemoryEnvelopeRepo
    gate: FakeGate
    kek: CountingPlatformKek
    providers: KmsProviders
    clock: FakeClock
    aws: MockAwsKms
    gcp: MockGcpKms
    azure: MockAzureKeyVault
    slug_by_tenant: dict[int, str] = field(default_factory=dict)

    def provider_side(self, config: TenantKmsConfig) -> None:
        """Do what the customer does at their provider (trust/label/tag with the ExternalId)."""
        if config.provider == PROVIDER_AWS:
            self.aws.trust(AWS_ROLE_ARN, config.external_id)
        elif config.provider == PROVIDER_GCP:
            self.gcp.keys[GCP_KEY].labels[PROOF_LABEL] = config.external_id
        else:
            self.azure.keys["waddles"].tags[PROOF_TAG] = config.external_id
            self.azure.consented.add(AZURE_DIRECTORY)

    async def onboard(
        self, provider: str, *, tenant_id: int = TENANT_A, slug: str = SLUG_A
    ) -> tuple[TenantKmsConfig, object]:
        """Configure -> customer-side setup -> activate; returns ``(config, rewrap_report)``."""
        self.gate.entitled.add(slug)
        key_ref, principal = {
            PROVIDER_AWS: (AWS_KEY_ARN, AWS_ROLE_ARN),
            PROVIDER_GCP: (GCP_KEY, None),
            PROVIDER_AZURE: (AZURE_KEY_URL, AZURE_DIRECTORY),
        }[provider]
        if provider == PROVIDER_GCP and GCP_KEY not in self.gcp.keys:
            self.gcp.add_key(GCP_KEY)
        if provider == PROVIDER_AZURE and "waddles" not in self.azure.keys:
            self.azure.add_key("waddles")
        config = await self.service.configure_external_kms(
            tenant_id,
            tenant_slug=slug,
            provider=provider,
            key_ref=key_ref,
            region=None,
            principal=principal,
        )
        self.provider_side(config)
        return await self.service.activate_external_kms(tenant_id, tenant_slug=slug)


@pytest.fixture
def make_harness(
    aws_mock: MockAwsKms, gcp_mock: MockGcpKms, azure_mock: MockAzureKeyVault
) -> Callable[..., Harness]:
    """Factory building a :class:`Harness`; kwargs tune the service settings / timeouts."""

    def build(
        *,
        settings: EnvelopeSettings | None = None,
        timeout_s: float = 10.0,
        entitled: set[str] | None = None,
    ) -> Harness:
        clock = FakeClock()
        repo = InMemoryEnvelopeRepo()
        gate = FakeGate(entitled=set(entitled or ()))
        kek = CountingPlatformKek(os.urandom(32))
        aws_provider = AwsClientProvider(
            session_factory=lambda: boto3.session.Session(
                aws_access_key_id="AKIAPLATFORM",
                aws_secret_access_key="platform-secret",  # noqa: S106 - test fixture
                region_name="us-east-1",
            ),
            kms_endpoint_url=aws_mock.url,
            sts_endpoint_url=aws_mock.url,
        )
        gcp_runtime = GcpRuntime(
            GcpPlatformSettings(
                service_account=ServiceAccountKey.parse(gcp_mock.service_account_json()),
                api_base=gcp_mock.api_base,
            )
        )
        azure_runtime = AzureRuntime(
            AzurePlatformSettings(
                client_id=MockAzureKeyVault.PLATFORM_CLIENT_ID,
                client_secret=MockAzureKeyVault.PLATFORM_SECRET,
                authority=azure_mock.url,
                vault_url_rewrite=azure_mock.rewrite,
            )
        )
        providers = build_providers(
            ProviderSettings(enabled=ALL_PROVIDERS, timeout_s=timeout_s),
            aws_provider=aws_provider,
            gcp_runtime=gcp_runtime,
            azure_runtime=azure_runtime,
        )
        service = TenantEnvelopeService(
            keys=repo,
            configs=repo,
            registry=providers.registry,
            platform_kek=lambda: kek,
            gate=gate,
            settings=settings or EnvelopeSettings(),
            clock=clock,
        )
        return Harness(
            service=service,
            repo=repo,
            gate=gate,
            kek=kek,
            providers=providers,
            clock=clock,
            aws=aws_mock,
            gcp=gcp_mock,
            azure=azure_mock,
        )

    return build


def make_config(
    provider: str,
    key_ref: str,
    *,
    principal: str | None = None,
    external_id: str = "ab" * 24,
    tenant_id: int = TENANT_A,
    region: str | None = None,
    status: str = "pending",
) -> TenantKmsConfig:
    """Build a stored-shaped config row for adapter-level tests."""
    return TenantKmsConfig(
        tenant_id=tenant_id,
        provider=provider,
        key_ref=key_ref,
        region=region,
        principal=principal,
        external_id=external_id,
        status=status,
    )
