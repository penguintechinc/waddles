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

import os
from typing import Final, Protocol, runtime_checkable

from services import platform_integrations_crypto as crypto
from services.platform_integrations_crypto import (
    PlatformCredentialCryptoError,
    PlatformCredentialKeyError,
)
from services.provider_connections.errors import VaultCorrupt, VaultUnavailable

_KEY_ENV_MARKER = "CREDENTIAL_ENCRYPTION_KEY"
_REFUSE_LEGACY_ENV = "CONNECTION_SEAL_REFUSE_LEGACY"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: ``seal_version`` hint of a pre-M4 row: sealed with no AAD under the raw key. Only a hint -- see
#: :func:`open_secret`; the column never decides whether the AAD control applies.
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


def legacy_fallback_allowed() -> bool:
    """Whether a no-AAD ciphertext may still be accepted for a row-bound secret (migration window).

    Default ``True``. Once ``make reseal-connections`` reports no legacy rows the operator sets
    ``CONNECTION_SEAL_REFUSE_LEGACY=1`` and every no-AAD connection secret fails closed.
    """
    return os.getenv(_REFUSE_LEGACY_ENV, "").strip().lower() not in _TRUTHY


def open_classified(ciphertext: str, *, aad: bytes) -> tuple[str, bool]:
    """Open a row-bound secret, reporting whether it was genuinely legacy: ``(plaintext, legacy)``.

    The AAD open is always tried FIRST; only when it fails (and the migration flag still permits)
    is the no-AAD layout accepted, so a ciphertext that authenticates under the row's AAD is never
    treated as legacy whatever the row's ``seal_version`` column says.
    """
    try:
        return crypto.decrypt_value(ciphertext, aad=aad), False
    except PlatformCredentialKeyError:
        raise
    except PlatformCredentialCryptoError:
        if not legacy_fallback_allowed():
            raise
    return crypto.decrypt_value(ciphertext), True


def reseal_if_legacy(ciphertext: str, *, aad: bytes) -> str | None:
    """Re-seal a genuinely legacy ciphertext under ``aad``; ``None`` if it is already AAD-bound.

    ``None`` on a row whose column says legacy is a suspected downgrade (the column was flipped on
    an AAD-bound row): the caller must log it and must NOT re-bless the row. Raises
    ``PlatformCredentialCryptoError`` when the value opens under neither layout.
    """
    plaintext, legacy = open_classified(ciphertext, aad=aad)
    return seal_secret(plaintext, aad=aad) if legacy else None


def open_secret(
    ciphertext: str, *, aad: bytes | None = None, seal_version: int = SEAL_LEGACY
) -> str:
    """Open ``ciphertext``; raises ``PlatformCredentialCryptoError``.

    ``seal_version`` is only a hint stored in an attacker-writable row: it can never switch the AAD
    control off. With an ``aad`` the AAD open is tried first whatever the column says; the no-AAD
    fallback is considered only for a row marked legacy (0) while the migration flag still permits
    it, so a v1 row flipped to 0 still opens ONLY via AAD and a substituted ciphertext fails
    closed. ``aad=None`` is the deliberately unbound path (tenant app credentials, install state)
    and uses the raw key -- a different key from every row-bound secret.
    """
    if seal_version not in (SEAL_LEGACY, SEAL_AAD):
        raise PlatformCredentialCryptoError("Failed to decrypt credential value")
    if aad is None:
        if seal_version == SEAL_AAD:
            raise PlatformCredentialCryptoError("Failed to decrypt credential value")
        return crypto.decrypt_value(ciphertext)
    if seal_version == SEAL_AAD:
        return crypto.decrypt_value(ciphertext, aad=aad)
    return open_classified(ciphertext, aad=aad)[0]


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
