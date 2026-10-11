"""Per-tenant envelope encryption with optional customer-managed KMS keys (Enterprise BYOK).

Public surface: :class:`TenantEnvelopeService` (encrypt/decrypt, DEK
rotation, re-wrap, BYOK configure/activate/disable), the
:class:`KmsAdapter` provider interface, and the error taxonomy. See
``README.md`` in this package for BYOK setup and the failure policy, and
``docs/superpowers/specs/2026-09-28-tenant-envelope-encryption-design.md``
for the design this implements.
"""

from __future__ import annotations

from services.envelope.errors import (
    EnvelopeError,
    EnvelopeInputError,
    EnvelopeIntegrityError,
    ExternalKmsNotEntitledError,
    KmsAccessDeniedError,
    KmsConfigError,
    KmsCredentialsError,
    KmsError,
    KmsRejectedError,
    KmsUnavailableError,
    PlatformKekError,
    TenantKeyNotFoundError,
    TenantKeyUnavailableError,
    UnsupportedKmsProviderError,
)
from services.envelope.kms_adapter import KmsAdapter, KmsProviderRegistry
from services.envelope.models import (
    DekRecord,
    EncryptedField,
    RewrapReport,
    TenantKmsConfig,
)
from services.envelope.service import EnvelopeSettings, TenantEnvelopeService, TenantKmsStatus

__all__ = [
    "DekRecord",
    "EncryptedField",
    "EnvelopeError",
    "EnvelopeInputError",
    "EnvelopeIntegrityError",
    "EnvelopeSettings",
    "ExternalKmsNotEntitledError",
    "KmsAccessDeniedError",
    "KmsAdapter",
    "KmsConfigError",
    "KmsCredentialsError",
    "KmsError",
    "KmsProviderRegistry",
    "KmsRejectedError",
    "KmsUnavailableError",
    "PlatformKekError",
    "RewrapReport",
    "TenantEnvelopeService",
    "TenantKeyNotFoundError",
    "TenantKeyUnavailableError",
    "TenantKmsConfig",
    "TenantKmsStatus",
    "UnsupportedKmsProviderError",
]
