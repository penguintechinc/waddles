"""Error taxonomy for per-tenant envelope encryption and external KMS (BYOK).

The split between :class:`KmsAccessDeniedError` (an explicit revocation
signal) and :class:`KmsUnavailableError` (a transient outage) is
load-bearing: `docs/superpowers/specs/2026-09-28-tenant-envelope-
encryption-design.md` Sec6 requires the two to be handled differently --
a permission-denied response evicts the cached key immediately and fails
closed, while a timeout/5xx rides the grace window. Providers map their
native errors onto these types; the service never inspects provider
exception classes.

No exception in this module ever carries key material (plaintext DEK,
wrapped DEK, KEK bytes, or credentials) -- messages are fixed strings or
identifiers only, so a traceback can never leak a key.
"""

from __future__ import annotations

from typing import Literal

#: Machine-readable reason attached to :class:`TenantKeyUnavailableError`.
UnavailableReason = Literal[
    "kms_access_denied",
    "kms_unavailable",
    "kms_platform_credentials",
    "kms_rejected",
    "kms_pending",
    "kms_config_missing",
    "kms_blocked",
]


class EnvelopeError(Exception):
    """Base class for every envelope-encryption failure."""


class EnvelopeInputError(EnvelopeError):
    """A caller supplied a malformed tenant id / table / column / row id / wire value."""


class EnvelopeIntegrityError(EnvelopeError):
    """AEAD authentication failed -- tampered ciphertext or a cross-tenant/row/column swap.

    Deliberately carries no detail about *which* component mismatched
    (key, AAD, nonce, tag): that would be an oracle for an attacker
    probing swapped ciphertexts.
    """


class PlatformKekError(EnvelopeError):
    """The platform baseline KEK (``TENANT_KEK_HEX``) is missing or malformed -- fail loud."""


class TenantKeyNotFoundError(EnvelopeError):
    """No (non-destroyed) data key exists for the requested tenant/version."""


class ExternalKmsNotEntitledError(EnvelopeError):
    """The tenant lacks the Enterprise ``compliance.external_kms`` entitlement."""


class KmsError(EnvelopeError):
    """Base class for failures talking to a KMS provider.

    Attributes:
        code: The provider's own error code (e.g. ``AccessDeniedException``)
            when there is one -- a short identifier, never a raw provider
            message (those can echo ARNs and principals).
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        """Record the optional provider error `code` alongside `message`."""
        super().__init__(message)
        self.code = code


class KmsConfigError(KmsError):
    """The supplied KMS configuration is invalid (bad ARN, wrong key spec, etc.)."""


class UnsupportedKmsProviderError(KmsConfigError):
    """The requested provider is unknown or not yet implemented."""


class KmsAccessDeniedError(KmsError):
    """The KMS refused the request: revoked grant, disabled/deleted key, or denied assume-role.

    Treated as an explicit revocation signal -- cached keys are evicted
    immediately and the tenant fails closed (design Sec6). Never retried
    against a fallback KEK.
    """


class KmsUnavailableError(KmsError):
    """The KMS could not be reached or answered with a transient error (timeout/5xx/throttle)."""


class KmsCredentialsError(KmsUnavailableError):
    """The *platform's own* credentials for a provider are missing, expired or wrong.

    An outage on Waddles' side, not a customer revocation: it must never read
    as :class:`KmsAccessDeniedError` (that would flag every tenant of the
    provider "revoked" over our misconfiguration). Subclasses
    :class:`KmsUnavailableError` so it is handled as a transient/unavailable
    failure, but is surfaced under its own alertable reason.
    """


class KmsRejectedError(KmsError):
    """The KMS rejected the request permanently, without it being a revocation.

    Examples: wrong key for this ciphertext, corrupted ciphertext, an
    encryption-context mismatch, an unexpected key in the response.
    """


class TenantKeyUnavailableError(EnvelopeError):
    """The tenant's data key cannot be used right now -- map to HTTP 503.

    Attributes:
        reason: Stable machine-readable cause (see :data:`UnavailableReason`).
    """

    def __init__(self, message: str, *, reason: UnavailableReason) -> None:
        """Record `reason` alongside the human-readable `message`."""
        super().__init__(message)
        self.reason: UnavailableReason = reason
