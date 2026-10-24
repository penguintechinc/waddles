"""Token vault: the single seam through which a connection secret is encrypted or decrypted.

Secrets are sealed *before* they reach the store and opened only at the point of use (a provider
call, or the data-plane resolve response). Nothing else in the package ever sees a ciphertext as
anything but an opaque string, and nothing outside this module touches a key.

The default :class:`PlatformKeyVault` is the platform at-rest baseline: AES-256-GCM under
``CREDENTIAL_ENCRYPTION_KEY`` via ``services.platform_integrations_crypto`` -- the same primitive,
key and wire format the existing layer-2 readers (``credential_resolver``) already use, so rows
written here stay readable by them. A missing or malformed key fails loudly
(:class:`VaultUnavailable`); there is no plaintext mode and no fallback key.

The per-tenant envelope / customer-managed KMS (PR #789) is the Enterprise upgrade *on top of* this
baseline: implement :class:`TokenVault` over the envelope and pass it to the service -- no store,
adapter or API change is needed.
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

from services import platform_integrations_crypto as crypto
from services.platform_integrations_crypto import (
    PlatformCredentialCryptoError,
    PlatformCredentialKeyError,
)
from services.provider_connections.errors import VaultCorrupt, VaultUnavailable

_KEY_ENV_MARKER = "CREDENTIAL_ENCRYPTION_KEY"

#: ``seal_version`` of a pre-M4 row: sealed with no AAD (``open`` ignores any ``aad`` given).
SEAL_LEGACY: Final = 0
#: ``seal_version`` of an AAD-bound row (every new write): the ciphertext authenticates only for
#: the row identity it was sealed for, so copying it to another row fails closed (finding M4).
SEAL_AAD: Final = 1


def seal_secret(plaintext: str, *, aad: bytes | None = None) -> str:
    """Seal ``plaintext`` (AAD-bound when ``aad`` is given); raise a crypto error on failure.

    The single write seam: nothing outside this module and the crypto module calls the raw
    primitive. ``aad=None`` is for secrets with no durable row identity (short-lived install state,
    layer-1 tenant app credentials) -- connection rows always pass their AAD.
    """
    return crypto.encrypt_token(plaintext, aad=aad)


def open_secret(
    ciphertext: str, *, aad: bytes | None = None, seal_version: int = SEAL_LEGACY
) -> str:
    """Open ``ciphertext`` per its ``seal_version``; raises ``PlatformCredentialCryptoError``.

    The single versioned read seam. Version 0 ignores ``aad``; version 1 requires it and fails
    closed when it is missing or wrong -- there is never a silent fallback to the no-AAD path, and
    an unknown version is refused outright.
    """
    if seal_version == SEAL_LEGACY:
        return crypto.decrypt_value(ciphertext)
    if seal_version == SEAL_AAD and aad is not None:
        return crypto.decrypt_value(ciphertext, aad=aad)
    raise PlatformCredentialCryptoError("Failed to decrypt credential value")


@runtime_checkable
class TokenVault(Protocol):
    """Encrypts a secret for storage and decrypts it for use. Implementations must not log."""

    def seal(self, plaintext: str, *, aad: bytes) -> str:
        """Return an opaque ciphertext for ``plaintext``, bound to ``aad`` (version 1)."""
        ...

    def open(self, ciphertext: str, *, aad: bytes | None, seal_version: int) -> str:
        """Return the plaintext for ``ciphertext`` or raise :class:`VaultCorrupt`."""
        ...


class PlatformKeyVault:
    """AES-256-GCM under the platform credential key (the at-rest baseline)."""

    def seal(self, plaintext: str, *, aad: bytes) -> str:
        """Encrypt ``plaintext`` bound to ``aad``; raise :class:`VaultUnavailable` without a key."""
        try:
            return seal_secret(plaintext, aad=aad)
        except PlatformCredentialCryptoError as exc:
            raise VaultUnavailable(
                f"the connection encryption key ({_KEY_ENV_MARKER}) is missing or malformed"
            ) from exc

    def open(self, ciphertext: str, *, aad: bytes | None, seal_version: int) -> str:
        """Decrypt ``ciphertext``; map key problems and tamper detection onto distinct errors."""
        try:
            return open_secret(ciphertext, aad=aad, seal_version=seal_version)
        except PlatformCredentialKeyError as exc:
            raise VaultUnavailable(
                f"the connection encryption key ({_KEY_ENV_MARKER}) is missing or malformed"
            ) from exc
        except PlatformCredentialCryptoError as exc:
            raise VaultCorrupt("a stored connection secret failed authentication") from exc
