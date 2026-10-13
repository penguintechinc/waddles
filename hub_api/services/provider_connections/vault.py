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

from typing import Protocol, runtime_checkable

from services.platform_integrations_crypto import (
    PlatformCredentialCryptoError,
    PlatformCredentialKeyError,
    decrypt_value,
    encrypt_token,
)
from services.provider_connections.errors import VaultCorrupt, VaultUnavailable

_KEY_ENV_MARKER = "CREDENTIAL_ENCRYPTION_KEY"


@runtime_checkable
class TokenVault(Protocol):
    """Encrypts a secret for storage and decrypts it for use. Implementations must not log."""

    def seal(self, plaintext: str) -> str:
        """Return an opaque ciphertext for ``plaintext``."""
        ...

    def open(self, ciphertext: str) -> str:
        """Return the plaintext for ``ciphertext`` or raise :class:`VaultCorrupt`."""
        ...


class PlatformKeyVault:
    """AES-256-GCM under the platform credential key (the at-rest baseline)."""

    def seal(self, plaintext: str) -> str:
        """Encrypt ``plaintext``; raise :class:`VaultUnavailable` if the key is not usable."""
        try:
            return encrypt_token(plaintext)
        except PlatformCredentialCryptoError as exc:
            raise VaultUnavailable(
                f"the connection encryption key ({_KEY_ENV_MARKER}) is missing or malformed"
            ) from exc

    def open(self, ciphertext: str) -> str:
        """Decrypt ``ciphertext``; map key problems and tamper detection onto distinct errors."""
        try:
            return decrypt_value(ciphertext)
        except PlatformCredentialKeyError as exc:
            raise VaultUnavailable(
                f"the connection encryption key ({_KEY_ENV_MARKER}) is missing or malformed"
            ) from exc
        except PlatformCredentialCryptoError as exc:
            raise VaultCorrupt("a stored connection secret failed authentication") from exc
