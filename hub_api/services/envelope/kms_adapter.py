"""Provider-agnostic KMS adapter interface and provider registry.

A :class:`KmsAdapter` wraps/unwraps a 32-byte DEK under a key-encryption
key that never leaves its KMS (customer-managed providers) or the process
environment (the platform baseline). The service depends only on this
Protocol, so adding another provider is "write one adapter + register it"
-- no service change.

:class:`KmsProviderRegistry` is the single place a provider id
(``"aws_kms"``, ``"gcp_kms"``, ``"azure_key_vault"``) turns into an
adapter. A provider the deployment has not enabled (``ENVELOPE_KMS_
PROVIDERS``) or does not know fails loudly with
:class:`UnsupportedKmsProviderError` -- never a silent fallback to another
KEK and never a stub that pretends to work.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from services.envelope.errors import UnsupportedKmsProviderError
from services.envelope.models import KmsKeyInfo, TenantKmsConfig

#: Every provider id this release implements. A deployment enables a subset
#: via ``ENVELOPE_KMS_PROVIDERS`` (see :mod:`services.envelope.providers`).
PROVIDER_AWS = "aws_kms"
PROVIDER_GCP = "gcp_kms"
PROVIDER_AZURE = "azure_key_vault"
IMPLEMENTED_PROVIDERS: frozenset[str] = frozenset({PROVIDER_AWS, PROVIDER_GCP, PROVIDER_AZURE})


@dataclass(slots=True, frozen=True)
class CanonicalConfig:
    """A validated, normalised tenant KMS config (what gets stored and later built from).

    `region` and `principal` are provider-specific and may be ``None``: AWS
    needs both (region is derived from the key ARN), GCP needs neither, Azure
    needs the customer's directory id as `principal`.
    """

    key_ref: str
    region: str | None
    principal: str | None


class KmsAdapter(Protocol):
    """A KEK that can wrap and unwrap tenant DEKs.

    Implementations must raise only the :mod:`services.envelope.errors`
    KMS types (``KmsAccessDeniedError`` for revocation, ``KmsUnavailable
    Error`` for transient failures, ``KmsRejectedError`` for permanent
    rejections) so the service can apply the design's failure policy
    without knowing the provider.
    """

    @property
    def kek_kind(self) -> str:
        """``"platform"`` or ``"customer_kms"`` -- stored on every wrapped-DEK row."""
        ...

    @property
    def key_ref(self) -> str:
        """Canonical identifier of the KEK this adapter is bound to (stored as ``kek_ref``)."""
        ...

    async def wrap(self, plaintext_dek: bytes, *, context: Mapping[str, str]) -> bytes:
        """Wrap `plaintext_dek` under the KEK, binding `context` (tenant + purpose)."""
        ...

    async def unwrap(self, wrapped_dek: bytes, *, context: Mapping[str, str]) -> bytes:
        """Unwrap; must fail if `context` differs from the one used to wrap."""
        ...

    async def verify(self) -> KmsKeyInfo:
        """Preflight: confirm the KEK exists, is usable, and a wrap/unwrap round-trips."""
        ...


#: `(config, key_ref_override)` -> adapter. `key_ref_override` lets the
#: service unwrap rows that still reference a previous key (after the
#: customer moved to a new key but before every DEK was re-wrapped).
AdapterFactory = Callable[[TenantKmsConfig, str | None], KmsAdapter]
#: `(key_ref, region, principal)` -> :class:`CanonicalConfig`; raises
#: `KmsConfigError` on invalid input.
ConfigValidator = Callable[[str, str | None, str | None], CanonicalConfig]


@dataclass(slots=True, frozen=True)
class _Registration:
    """One registered provider: how to validate its config and build its adapter."""

    factory: AdapterFactory
    validator: ConfigValidator


class KmsProviderRegistry:
    """Maps provider ids to adapter factories; the only provider-selection point."""

    def __init__(self) -> None:
        """Create an empty registry (use :func:`default_registry` for the shipped providers)."""
        self._providers: dict[str, _Registration] = {}

    def register(
        self, provider: str, *, factory: AdapterFactory, validator: ConfigValidator
    ) -> None:
        """Register `provider`; refuses to shadow an existing registration."""
        if provider in self._providers:
            raise ValueError(f"KMS provider {provider!r} is already registered")
        self._providers[provider] = _Registration(factory=factory, validator=validator)

    def supported(self) -> tuple[str, ...]:
        """Sorted ids of every implemented provider."""
        return tuple(sorted(self._providers))

    def _get(self, provider: str) -> _Registration:
        registration = self._providers.get(provider)
        if registration is not None:
            return registration
        supported = ", ".join(self.supported()) or "none"
        provider = provider[:64]  # tenant-supplied: bound what an error message can echo
        if provider in IMPLEMENTED_PROVIDERS:
            raise UnsupportedKmsProviderError(
                f"KMS provider {provider!r} is not enabled on this deployment; enabled: {supported}"
            )
        raise UnsupportedKmsProviderError(
            f"unknown KMS provider {provider!r}; enabled: {supported}"
        )

    def validate(
        self, provider: str, *, key_ref: str, region: str | None, principal: str | None
    ) -> CanonicalConfig:
        """Validate a tenant-supplied config and return its canonical form."""
        return self._get(provider).validator(key_ref, region, principal)

    def build(self, config: TenantKmsConfig, *, key_ref: str | None = None) -> KmsAdapter:
        """Build the adapter for `config` (optionally pinned to a different `key_ref`)."""
        return self._get(config.provider).factory(config, key_ref)
