"""The shipped KMS provider set -- the only place concrete adapters are registered.

Implemented: ``aws_kms``, ``gcp_kms``, ``azure_key_vault``. A deployment
enables a subset with ``ENVELOPE_KMS_PROVIDERS`` (comma-separated); the
default is **none**, which is the "flag defaulted OFF" posture -- the
platform-managed baseline works with zero configuration and BYOK configuration
fails loudly with :class:`~services.envelope.errors.UnsupportedKmsProviderError`
until an operator turns a provider on. A provider that is enabled but whose
platform credentials are missing or malformed fails at *startup* (loud), never
on a tenant's first request.

Platform credentials (never tenant-supplied, always from an existing Secret in
the chart -- see ``k8s/helm/waddlebot/templates/kms.yaml``):

========== ==============================================================
AWS        ambient boto3 chain (IRSA / Pod Identity / env); optional
           ``ENVELOPE_AWS_KMS_ENDPOINT_URL`` / ``ENVELOPE_AWS_STS_ENDPOINT_URL``
GCP        ``ENVELOPE_GCP_CREDENTIALS_JSON`` (service-account key) or the
           GKE/GCE metadata identity; optional ``ENVELOPE_GCP_KMS_ENDPOINT``
Azure      ``ENVELOPE_AZURE_CLIENT_ID`` + ``ENVELOPE_AZURE_CLIENT_SECRET``;
           optional ``ENVELOPE_AZURE_AUTHORITY``
========== ==============================================================

Adding a provider is a new adapter module plus one registration here.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from concurrent.futures import Executor
from dataclasses import dataclass, field

from services.envelope._http import SharedHttp
from services.envelope.aws_kms import AwsClientProvider, aws_adapter_factory, validate_aws_config
from services.envelope.azure_key_vault import (
    AzurePlatformSettings,
    AzureRuntime,
    azure_adapter_factory,
    validate_azure_config,
)
from services.envelope.errors import KmsConfigError
from services.envelope.gcp_kms import (
    GcpPlatformSettings,
    GcpRuntime,
    gcp_adapter_factory,
    validate_gcp_config,
)
from services.envelope.kms_adapter import (
    IMPLEMENTED_PROVIDERS,
    PROVIDER_AWS,
    PROVIDER_AZURE,
    PROVIDER_GCP,
    KmsProviderRegistry,
)

#: A loose identity check for the operator-supplied principal shown to customers (an AWS role/user
#: ARN or a GCP service-account email). It is display-only -- never used to authenticate.
_PRINCIPAL = re.compile(r"^[A-Za-z0-9:/_.+=,@-]{1,512}$")


def _principal(name: str, value: str) -> str:
    """Validate one operator-supplied platform principal (a public identifier, not a secret)."""
    if not _PRINCIPAL.match(value):
        raise KmsConfigError(f"{name} must be an ARN or service-account email (no spaces)")
    return value


@dataclass(slots=True, frozen=True)
class ProviderSettings:
    """Which providers this deployment enables and the platform wiring each needs."""

    enabled: frozenset[str] = frozenset()
    aws_kms_endpoint_url: str | None = None
    aws_sts_endpoint_url: str | None = None
    gcp: GcpPlatformSettings | None = field(default=None, repr=False)
    azure: AzurePlatformSettings | None = field(default=None, repr=False)
    timeout_s: float = 10.0
    #: Provider id -> the public identity customers must trust/grant (AWS principal ARN, GCP
    #: service-account email, Azure application id). Shown by ``GET /kms``; never a credential.
    platform_principals: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ProviderSettings:
        """Parse ``ENVELOPE_KMS_PROVIDERS`` and each enabled provider's platform settings.

        Raises:
            KmsConfigError: an unknown provider id, a malformed endpoint, or an
                enabled provider whose platform credentials are missing.
        """
        env = os.environ if environ is None else environ
        requested = {
            p.strip() for p in env.get("ENVELOPE_KMS_PROVIDERS", "").split(",") if p.strip()
        }
        unknown = requested - IMPLEMENTED_PROVIDERS
        if unknown:
            raise KmsConfigError(
                f"ENVELOPE_KMS_PROVIDERS names unknown provider(s): {sorted(unknown)}; "
                f"valid: {sorted(IMPLEMENTED_PROVIDERS)}"
            )
        aws_kms = env.get("ENVELOPE_AWS_KMS_ENDPOINT_URL", "").strip() or None
        aws_sts = env.get("ENVELOPE_AWS_STS_ENDPOINT_URL", "").strip() or None
        for name, url in (("KMS", aws_kms), ("STS", aws_sts)):
            if url is not None and not url.startswith("https://"):
                raise KmsConfigError(f"ENVELOPE_AWS_{name}_ENDPOINT_URL must be an https:// URL")
        gcp = GcpPlatformSettings.from_env(env) if PROVIDER_GCP in requested else None
        azure = AzurePlatformSettings.from_env(env) if PROVIDER_AZURE in requested else None
        principals: dict[str, str] = {}
        if aws_principal := env.get("ENVELOPE_AWS_PLATFORM_PRINCIPAL", "").strip():
            principals[PROVIDER_AWS] = _principal("ENVELOPE_AWS_PLATFORM_PRINCIPAL", aws_principal)
        if gcp is not None:
            configured = env.get("ENVELOPE_GCP_PLATFORM_PRINCIPAL", "").strip()
            if gcp.service_account is not None:
                principals[PROVIDER_GCP] = gcp.service_account.client_email
            elif configured:
                principals[PROVIDER_GCP] = _principal("ENVELOPE_GCP_PLATFORM_PRINCIPAL", configured)
        if azure is not None:
            principals[PROVIDER_AZURE] = azure.client_id
        return cls(
            enabled=frozenset(requested),
            aws_kms_endpoint_url=aws_kms,
            aws_sts_endpoint_url=aws_sts,
            gcp=gcp,
            azure=azure,
            timeout_s=float(env.get("ENVELOPE_KMS_TIMEOUT_S", "10")),
            platform_principals={k: v for k, v in principals.items() if k in requested},
        )


class KmsProviders:
    """The built registry plus the shared network resources that must be closed on shutdown."""

    def __init__(self, registry: KmsProviderRegistry, http_clients: list[SharedHttp]) -> None:
        """Hold the registry and the HTTP holders behind its REST-based adapters."""
        self.registry = registry
        self._http_clients = http_clients

    async def aclose(self) -> None:
        """Close every pooled HTTP client (idempotent)."""
        for http in self._http_clients:
            await http.aclose()


def build_providers(
    settings: ProviderSettings,
    *,
    aws_provider: AwsClientProvider | None = None,
    executor: Executor | None = None,
    gcp_runtime: GcpRuntime | None = None,
    azure_runtime: AzureRuntime | None = None,
) -> KmsProviders:
    """Register every provider `settings` enables.

    The optional runtimes exist so tests can inject mock-backed collaborators
    while still exercising the real registration path.
    """
    registry = KmsProviderRegistry()
    http_clients: list[SharedHttp] = []
    if PROVIDER_AWS in settings.enabled:
        provider = aws_provider or AwsClientProvider(
            kms_endpoint_url=settings.aws_kms_endpoint_url,
            sts_endpoint_url=settings.aws_sts_endpoint_url,
        )
        registry.register(
            PROVIDER_AWS,
            factory=aws_adapter_factory(
                provider=provider, executor=executor, timeout_s=settings.timeout_s
            ),
            validator=validate_aws_config,
        )
    if PROVIDER_GCP in settings.enabled:
        if gcp_runtime is None:
            if settings.gcp is None:
                raise KmsConfigError("gcp_kms is enabled but has no platform settings")
            gcp_runtime = GcpRuntime(settings.gcp)
        http_clients.append(gcp_runtime.http)
        registry.register(
            PROVIDER_GCP,
            factory=gcp_adapter_factory(gcp_runtime, timeout_s=settings.timeout_s),
            validator=validate_gcp_config,
        )
    if PROVIDER_AZURE in settings.enabled:
        if azure_runtime is None:
            if settings.azure is None:
                raise KmsConfigError("azure_key_vault is enabled but has no platform settings")
            azure_runtime = AzureRuntime(settings.azure)
        http_clients.append(azure_runtime.http)
        registry.register(
            PROVIDER_AZURE,
            factory=azure_adapter_factory(azure_runtime, timeout_s=settings.timeout_s),
            validator=validate_azure_config,
        )
    return KmsProviders(registry, http_clients)
