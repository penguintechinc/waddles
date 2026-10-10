"""Process wiring for the envelope service: build once at startup, close on shutdown.

hub-api constructs exactly one :class:`EnvelopeRuntime` in ``app.py``'s
``before_serving`` hook and stores it at ``app.config["envelope_runtime"]``.
Nothing here is "on" by default:

- **Baseline.** The platform KEK (``TENANT_KEK_HEX``) is resolved *lazily* by
  :class:`~services.envelope.platform_kek.LazyPlatformKek`, so hub-api boots
  with zero KMS configuration and the platform-managed baseline keeps working
  exactly as before. A consumer that does reach for the baseline without the
  KEK mounted fails loud with ``PlatformKekError`` -- never a derived or
  empty key.
- **External KMS.** The provider registry is empty until an operator sets
  ``ENVELOPE_KMS_PROVIDERS``; an enabled provider with bad platform
  credentials fails the *startup* (loud), not a tenant's first request. Using
  a provider additionally needs the Enterprise ``compliance.external_kms``
  entitlement per tenant at call time (see :mod:`services.envelope.gate`).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from penguin_dal import AsyncDB

from services.bundle_install_dal import raw_sql_rows
from services.envelope.gate import ExternalKmsEntitlement, ExternalKmsGate
from services.envelope.platform_kek import LazyPlatformKek
from services.envelope.providers import KmsProviders, ProviderSettings, build_providers
from services.envelope.repository import PenguinDalEnvelopeRepository
from services.envelope.service import EnvelopeSettings, SlugResolver, TenantEnvelopeService

logger = logging.getLogger(__name__)

_SQL_TENANT_SLUG = "SELECT slug FROM tenants WHERE id = :t"


@dataclass(slots=True)
class EnvelopeRuntime:
    """The process-wide envelope service and the network resources behind it."""

    service: TenantEnvelopeService
    providers: KmsProviders
    enabled_providers: tuple[str, ...]
    #: Provider id -> the public identity a customer must trust/grant (shown by ``GET /kms``).
    platform_principals: Mapping[str, str] = field(default_factory=dict)

    async def aclose(self) -> None:
        """Release pooled HTTP clients (idempotent); called from the shutdown hook."""
        await self.providers.aclose()


def tenant_slug_resolver(dal: AsyncDB) -> SlugResolver:
    """Return a resolver mapping a tenant id to its slug (the entitlement system keys on slugs)."""

    async def resolve(tenant_id: int) -> str | None:
        rows = await raw_sql_rows(dal, _SQL_TENANT_SLUG, {"t": tenant_id})
        return str(rows[0]["slug"]) if rows else None

    return resolve


def build_envelope_runtime(
    dal: AsyncDB,
    *,
    environ: Mapping[str, str] | None = None,
    gate: ExternalKmsEntitlement | None = None,
    providers: KmsProviders | None = None,
    platform_kek: LazyPlatformKek | None = None,
) -> EnvelopeRuntime:
    """Assemble the envelope service over hub-api's penguin-dal connection.

    Raises:
        KmsConfigError: ``ENVELOPE_KMS_PROVIDERS`` names an unknown provider, or an enabled
            provider's platform credentials are missing/malformed -- at startup, by design.
        ValueError: an ``ENVELOPE_*`` tunable is out of range.
    """
    settings = ProviderSettings.from_env(environ)
    built = providers or build_providers(settings)
    repository = PenguinDalEnvelopeRepository(dal)
    lazy_kek = platform_kek or LazyPlatformKek()
    service = TenantEnvelopeService(
        keys=repository,
        configs=repository,
        registry=built.registry,
        platform_kek=lazy_kek.get,
        gate=gate or ExternalKmsGate(),
        settings=EnvelopeSettings.from_env(environ),
        slug_resolver=tenant_slug_resolver(dal),
    )
    enabled = tuple(sorted(settings.enabled))
    logger.info("envelope.runtime.ready", extra={"providers": list(enabled)})
    return EnvelopeRuntime(
        service=service,
        providers=built,
        enabled_providers=enabled,
        platform_principals=dict(settings.platform_principals),
    )
