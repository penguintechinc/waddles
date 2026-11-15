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

import base64
import hashlib
import hmac
import logging
import os
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Final, Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from services import platform_integrations_crypto as crypto
from services.envelope.errors import EnvelopeError, PlatformKekError
from services.envelope.platform_kek import PlatformKekAdapter
from services.platform_integrations_crypto import (
    PlatformCredentialCryptoError,
    PlatformCredentialKeyError,
)
from services.provider_connections.errors import VaultCorrupt, VaultUnavailable

logger = logging.getLogger(__name__)

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


# Platform secrets (`platform_vault.secrets`, migration 0109): the platform's own generated keys and
# passwords. A NEW pair of functions with deliberately NONE of the legacy machinery above: no
# no-AAD fallback, no `seal_version=0`, no CREDENTIAL_ENCRYPTION_KEY. The only root is the platform
# KEK (`TENANT_KEK_HEX`, or the previous one during a rotation); the key is an HKDF subkey in the
# reserved `waddles/platform-secrets/` namespace (never shared with tenant DEK or ban-evasion keys),
# selected by the explicit `kek_id` stored in the envelope header -- never by trial decryption.
# `seal_platform_secret`, `open_platform_secret` and the keyed-MAC helpers below are the ONLY code
# that derives that key family or runs its primitives; the seal-callers guard test enforces it.

#: ``platform_vault.secrets.seal_version`` of the only envelope layout defined so far.
PLATFORM_SEAL_V1: Final = 1
_PLATFORM_SEAL_VERSIONS: Final = frozenset({PLATFORM_SEAL_V1})

#: Envelope magic. The database CHECK on ``sealed_value`` pins the same bytes.
PLATFORM_ENVELOPE_MAGIC: Final = b"WPS"
_KEK_ID_BYTES: Final = 8
_IV_BYTES: Final = 12
_TAG_BYTES: Final = 16
_HEADER_BYTES: Final = len(PLATFORM_ENVELOPE_MAGIC) + 1 + _KEK_ID_BYTES + _IV_BYTES
#: Shortest valid envelope: header + an AEAD tag over a 1-byte plaintext (the migration's CHECK).
PLATFORM_ENVELOPE_MIN_BYTES: Final = _HEADER_BYTES + _TAG_BYTES + 1

#: HKDF ``info`` labels, all inside the reserved namespace and referenced only in this module
#: (guard-enforced). Distinct labels give independent keys for sealing, fingerprints, the root
#: marker and the event chain, so a MAC can never be replayed as a ciphertext key or vice versa.
PLATFORM_INFO_NAMESPACE: Final = b"waddles/platform-secrets/"
_SEAL_INFO: Final = PLATFORM_INFO_NAMESPACE + b"v1"
_FINGERPRINT_INFO: Final = PLATFORM_INFO_NAMESPACE + b"fp/v1"
_CHAIN_INFO: Final = PLATFORM_INFO_NAMESPACE + b"chain/v1"
_ESCROW_INFO: Final = PLATFORM_INFO_NAMESPACE + b"escrow/v1"
_MANIFEST_INFO: Final = PLATFORM_INFO_NAMESPACE + b"escrow-manifest/v1"

_FINGERPRINT_BYTES: Final = 32
_SMALLINT_MAX: Final = 32767
_INT_MAX: Final = 2**31 - 1
_BIGINT_MAX: Final = 2**63 - 1
_KEK_ID_HEX_LEN: Final = _KEK_ID_BYTES * 2


class PlatformSecretError(Exception):
    """Base of every platform-secret seal/open failure.

    Every message is a fixed application-authored string: never plaintext, key material,
    ciphertext, a fingerprint or a caller-supplied value, so any instance is safe to log.
    """


class PlatformSecretInputError(PlatformSecretError):
    """A type / scope / version / id / plaintext argument is malformed (a programming error)."""


class PlatformSecretKekUnavailableError(PlatformSecretError):
    """No loaded platform KEK can serve the request.

    The KEK is unset or malformed, or sealed this value and is no longer loaded (the root-change
    signal). An operator action, never something a retry fixes.
    """


class PlatformSecretCorruptError(PlatformSecretError):
    """The envelope failed to authenticate or parse.

    Tampered, truncated, copied onto another row identity, a legacy/unknown layout, or sealed
    under another key. One undifferentiated failure by design.
    """


@dataclass(slots=True, frozen=True)
class SealedPlatformSecret:
    """What to store for one sealed secret: the whole envelope plus the two derived columns."""

    sealed_value: bytes
    kek_id: str
    seal_version: int


def _length_prefixed(*fields: bytes) -> bytes:
    """Canonical injective encoding: each field as a 4-byte big-endian length then its bytes."""
    return b"".join(len(field).to_bytes(4, "big") + field for field in fields)


def _checked_kek_id(kek_id: str) -> str:
    """Return `kek_id` if it is 16 lowercase hex characters, else raise a value-free error."""
    if (
        not isinstance(kek_id, str)
        or len(kek_id) != _KEK_ID_HEX_LEN
        or any(c not in "0123456789abcdef" for c in kek_id)
    ):
        raise PlatformSecretInputError("kek_id must be 16 lowercase hex characters")
    return kek_id


def _checked_identity(
    secret_type: int, scope_id: int | None, version: int, public_id: uuid.UUID
) -> None:
    """Validate the row identity (type, scope, version, public_id); raises a value-free error."""
    for value, upper in ((secret_type, _SMALLINT_MAX), (version, _INT_MAX)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
            raise PlatformSecretInputError("secret_type and version must be positive integers")
    if scope_id is not None and (
        isinstance(scope_id, bool)
        or not isinstance(scope_id, int)
        or not 0 <= scope_id <= _BIGINT_MAX
    ):
        raise PlatformSecretInputError("scope_id must be None or a non-negative integer")
    if not isinstance(public_id, uuid.UUID):
        raise PlatformSecretInputError("public_id must be a UUID")


def platform_secret_aad(
    *,
    secret_type: int,
    scope_id: int | None,
    version: int,
    kek_id: str,
    public_id: uuid.UUID,
    seal_version: int = PLATFORM_SEAL_V1,
) -> bytes:
    """Build the canonical AAD binding an envelope to its row and to the KEK that sealed it.

    Length-prefixed ``psv<seal_version> | type | scope or "-" | version | kek_id | public_id``.
    ``state`` is deliberately absent (it is mutable; the grants, triggers and event log protect
    it). ``scope_id=None`` (global) and ``scope_id=0`` (the default tenant) are distinct. A sealed
    value copied onto another row, or re-labelled with another kek_id, fails to open.
    """
    _checked_identity(secret_type, scope_id, version, public_id)
    scope = b"-" if scope_id is None else str(scope_id).encode()
    return _length_prefixed(
        f"psv{seal_version}".encode(),
        str(secret_type).encode(),
        scope,
        str(version).encode(),
        _checked_kek_id(kek_id).encode(),
        str(public_id).encode(),
    )


@contextmanager
def _platform_key(kek: PlatformKekAdapter, info: bytes, kek_id: str | None) -> Iterator[bytearray]:
    """Yield a 32-byte key derived from a loaded KEK for `info`; zero our copy on exit.

    `kek_id=None` selects the CURRENT KEK, otherwise exactly the named one. Best effort:
    ``derive_key`` returns an immutable ``bytes`` that CPython cannot wipe, so only this mutable
    copy (the one the primitive is built from) is zeroed. It bounds how long the derived key lives
    in our code; it is not a hard guarantee.
    """
    try:
        derived = kek.derive_key(info, key_id=kek_id)
    except PlatformKekError as exc:
        raise PlatformSecretKekUnavailableError("the platform KEK is unavailable") from exc
    except EnvelopeError as exc:
        raise PlatformSecretInputError("platform key derivation input is invalid") from exc
    key = bytearray(derived)
    del derived
    try:
        yield key
    finally:
        for i in range(len(key)):
            key[i] = 0


def seal_platform_secret(
    plaintext: bytes | bytearray,
    *,
    secret_type: int,
    scope_id: int | None,
    version: int,
    public_id: uuid.UUID,
    kek: PlatformKekAdapter,
) -> SealedPlatformSecret:
    """Seal ``plaintext`` for one platform_vault.secrets row under the CURRENT KEK.

    Envelope: ``"WPS" || seal_version(1) || kek_id(8) || iv(12) || AES-256-GCM(ct || tag)``; the
    AAD is :func:`platform_secret_aad` over the row identity, the layout version and the sealing
    kek_id. A fresh random 96-bit IV is drawn per call. Raises :class:`PlatformSecretInputError`
    on a malformed argument or empty plaintext and :class:`PlatformSecretKekUnavailableError`
    without a usable KEK.
    """
    if not isinstance(plaintext, bytes | bytearray) or len(plaintext) == 0:
        raise PlatformSecretInputError("plaintext must be non-empty bytes")
    kek_id = kek.kek_ids[0]
    aad = platform_secret_aad(
        secret_type=secret_type,
        scope_id=scope_id,
        version=version,
        kek_id=kek_id,
        public_id=public_id,
    )
    iv = os.urandom(_IV_BYTES)
    with _platform_key(kek, _SEAL_INFO, kek_id) as key:
        ciphertext = AESGCM(key).encrypt(iv, bytes(plaintext), aad)
    envelope = (
        PLATFORM_ENVELOPE_MAGIC
        + bytes([PLATFORM_SEAL_V1])
        + bytes.fromhex(kek_id)
        + iv
        + ciphertext
    )
    logger.debug(
        "platform secret sealed type=%d version=%d kek_id=%s", secret_type, version, kek_id
    )
    return SealedPlatformSecret(sealed_value=envelope, kek_id=kek_id, seal_version=PLATFORM_SEAL_V1)


def open_platform_secret(
    sealed_value: bytes | bytearray | memoryview,
    *,
    secret_type: int,
    scope_id: int | None,
    version: int,
    public_id: uuid.UUID,
    kek: PlatformKekAdapter,
    kek_id: str | None = None,
    seal_version: int | None = None,
) -> bytes:
    """Open an envelope for the row identity; the header's kek_id picks the KEK (no trial opens).

    No legacy path: an envelope that is not the exact platform layout (magic, a known
    ``seal_version``, authenticated header) is :class:`PlatformSecretCorruptError`, including any
    ``seal_version=0`` or no-AAD ciphertext. ``kek_id`` / ``seal_version`` are the stored columns,
    used ONLY as consistency checks against the header: they are attacker-writable and never decide
    anything. Opens with the current KEK or, in a rotation window, the previous one that sealed it.
    A KEK that is not loaded raises :class:`PlatformSecretKekUnavailableError`; every other failure
    raises the same undifferentiated :class:`PlatformSecretCorruptError`.
    """
    _checked_identity(secret_type, scope_id, version, public_id)
    blob = bytes(sealed_value)
    magic_len = len(PLATFORM_ENVELOPE_MAGIC)
    if len(blob) < PLATFORM_ENVELOPE_MIN_BYTES or not hmac.compare_digest(
        blob[:magic_len], PLATFORM_ENVELOPE_MAGIC
    ):
        raise _open_failure(secret_type, version)
    header_version = blob[magic_len]
    kek_start = magic_len + 1
    header_kek_id = blob[kek_start : kek_start + _KEK_ID_BYTES].hex()
    if (
        header_version not in _PLATFORM_SEAL_VERSIONS
        or (seal_version is not None and seal_version != header_version)
        or (kek_id is not None and not hmac.compare_digest(kek_id.encode(), header_kek_id.encode()))
    ):
        raise _open_failure(secret_type, version)
    iv = blob[kek_start + _KEK_ID_BYTES : _HEADER_BYTES]
    ciphertext = blob[_HEADER_BYTES:]
    aad = platform_secret_aad(
        secret_type=secret_type,
        scope_id=scope_id,
        version=version,
        kek_id=header_kek_id,
        public_id=public_id,
        seal_version=header_version,
    )
    try:
        with _platform_key(kek, _SEAL_INFO, header_kek_id) as key:
            plaintext = AESGCM(key).decrypt(iv, ciphertext, aad)
    except PlatformSecretKekUnavailableError:
        _log_open_failure(secret_type, version)
        raise
    except (InvalidTag, ValueError):
        raise _open_failure(secret_type, version) from None
    logger.debug(
        "platform secret opened type=%d version=%d kek_id=%s", secret_type, version, header_kek_id
    )
    return plaintext


def platform_secret_fingerprint(
    plaintext: bytes | bytearray, *, kek: PlatformKekAdapter, kek_id: str | None = None
) -> bytes:
    """Keyed fingerprint (HMAC-SHA256, 32 bytes) of a secret value; never a bare hash.

    Keyed under a KEK-derived key so a guessed value cannot be confirmed offline from a DB dump.
    `kek_id` selects a specific loaded KEK (the row's, during a rotation window); default current.
    """
    if not isinstance(plaintext, bytes | bytearray) or len(plaintext) == 0:
        raise PlatformSecretInputError("plaintext must be non-empty bytes")
    return _platform_mac(kek, _FINGERPRINT_INFO, _length_prefixed(bytes(plaintext)), kek_id)


def platform_root_mac(kek: PlatformKekAdapter, *, kek_id: str | None = None) -> bytes:
    """MAC stored in ``root_marker.check_mac``: proves the loaded KEK is the one that marked it.

    HMAC-SHA256 keyed with the raw KEK over ``waddles-root-check`` -- the exact construction the
    Helm hook writes into the root Secret's annotation, so the two markers are comparable.
    """
    try:
        return kek.root_marker_mac(key_id=kek_id)
    except PlatformKekError as exc:
        raise PlatformSecretKekUnavailableError("the platform KEK is unavailable") from exc


#: Version tag of the root Secret annotation value ``v1.<kek_id>.<hmac hex>``.
ROOT_MARKER_ANNOTATION_VERSION: Final = "v1"


def format_root_marker(kek_id: str, mac: bytes) -> str:
    """Render the root marker as the hook's annotation value ``v1.<kek_id>.<hmac hex>``."""
    if len(mac) != hashlib.sha256().digest_size:
        raise PlatformSecretInputError("root marker MAC must be 32 bytes")
    return f"{ROOT_MARKER_ANNOTATION_VERSION}.{_checked_kek_id(kek_id)}.{mac.hex()}"


def parse_root_marker(value: str) -> tuple[str, bytes]:
    """Parse an annotation value into ``(kek_id, mac)``; any malformed value is an input error."""
    parts = value.split(".") if isinstance(value, str) else []
    if len(parts) != 3 or parts[0] != ROOT_MARKER_ANNOTATION_VERSION:
        raise PlatformSecretInputError("root marker has an unknown format")
    try:
        mac = bytes.fromhex(parts[2])
    except ValueError:
        raise PlatformSecretInputError("root marker has an unknown format") from None
    if len(mac) != hashlib.sha256().digest_size:
        raise PlatformSecretInputError("root marker has an unknown format")
    return _checked_kek_id(parts[1]), mac


def verify_platform_root_mac(
    check_mac: bytes | bytearray | memoryview, *, kek: PlatformKekAdapter, kek_id: str
) -> bool:
    """Constant-time check of the stored root marker MAC against the KEK named `kek_id`.

    Returns False for a wrong MAC (the root changed). Raises
    :class:`PlatformSecretKekUnavailableError` when no loaded KEK has that id.
    """
    expected = platform_root_mac(kek, kek_id=_checked_kek_id(kek_id))
    return hmac.compare_digest(expected, bytes(check_mac))


def platform_event_hash(
    prev_chain_hash: bytes | None,
    canonical_event: bytes,
    *,
    kek: PlatformKekAdapter,
    kek_id: str | None = None,
) -> bytes:
    """Keyed chain hash of one audit event: HMAC over ``prev_chain_hash`` and the canonical event.

    The database enforces a single linear chain (unique ``prev_chain_hash``); only the holder of
    the KEK can extend it with a hash that verifies, so a DB-only writer cannot forge history.
    """
    if prev_chain_hash is not None and len(prev_chain_hash) != hashlib.sha256().digest_size:
        raise PlatformSecretInputError("prev_chain_hash must be 32 bytes or None")
    payload = _length_prefixed(prev_chain_hash or b"", canonical_event)
    return _platform_mac(kek, _CHAIN_INFO, payload, kek_id)


#: S3 escrow envelope (DR copy of a platform secret). Same layout as the row envelope but its own
#: magic, HKDF label and AAD, so an escrow object can never be opened as a row (or vice versa) and a
#: row's ciphertext planted in the escrow prefix fails authentication.
ESCROW_ENVELOPE_MAGIC: Final = b"WPE"
ESCROW_SEAL_V1: Final = 1


def escrow_aad(
    *,
    secret_type: int,
    scope_id: int | None,
    version: int,
    kek_id: str,
    public_id: uuid.UUID,
    fingerprint: bytes,
) -> bytes:
    """Canonical AAD of an escrow object: ``pse1|type|scope|version|kek_id|public_id|fp``.

    Binding the keyed fingerprint (H5) means an object cannot be re-labelled as another version, and
    the manifest's expected fingerprint is authenticated by the object itself, not only by the
    manifest MAC. Length-prefixed, so no two distinct tuples encode alike.
    """
    _checked_identity(secret_type, scope_id, version, public_id)
    if not isinstance(fingerprint, bytes | bytearray) or len(fingerprint) != _FINGERPRINT_BYTES:
        raise PlatformSecretInputError("fingerprint must be 32 bytes")
    scope = b"-" if scope_id is None else str(scope_id).encode()
    return _length_prefixed(
        f"pse{ESCROW_SEAL_V1}".encode(),
        str(secret_type).encode(),
        scope,
        str(version).encode(),
        _checked_kek_id(kek_id).encode(),
        str(public_id).encode(),
        bytes(fingerprint).hex().encode(),
    )


def seal_escrow_object(
    plaintext: bytes | bytearray,
    *,
    secret_type: int,
    scope_id: int | None,
    version: int,
    public_id: uuid.UUID,
    fingerprint: bytes,
    kek: PlatformKekAdapter,
) -> tuple[bytes, str]:
    """Seal one secret for the S3 escrow under the CURRENT KEK; returns ``(object, kek_id)``.

    Object: ``"WPE" || version(1) || kek_id(8) || iv(12) || AES-256-GCM(ct || tag)`` with
    :func:`escrow_aad`. The caller records ``kek_id`` and ``fingerprint`` in the signed manifest.
    """
    if not isinstance(plaintext, bytes | bytearray) or len(plaintext) == 0:
        raise PlatformSecretInputError("plaintext must be non-empty bytes")
    kek_id = kek.kek_ids[0]
    aad = escrow_aad(
        secret_type=secret_type,
        scope_id=scope_id,
        version=version,
        kek_id=kek_id,
        public_id=public_id,
        fingerprint=fingerprint,
    )
    iv = os.urandom(_IV_BYTES)
    with _platform_key(kek, _ESCROW_INFO, kek_id) as key:
        ciphertext = AESGCM(key).encrypt(iv, bytes(plaintext), aad)
    blob = ESCROW_ENVELOPE_MAGIC + bytes([ESCROW_SEAL_V1]) + bytes.fromhex(kek_id) + iv + ciphertext
    return blob, kek_id


def open_escrow_object(
    blob: bytes | bytearray | memoryview,
    *,
    secret_type: int,
    scope_id: int | None,
    version: int,
    public_id: uuid.UUID,
    fingerprint: bytes,
    kek_id: str,
    kek: PlatformKekAdapter,
) -> bytes:
    """Open an escrow object for the identity the (signed) manifest claims.

    The header kek_id must equal the manifest's ``kek_id`` and picks the KEK; the AAD binds type,
    scope, version, kek_id, public_id and fingerprint. A wrong KEK is
    :class:`PlatformSecretKekUnavailableError` (not loaded) or :class:`PlatformSecretCorruptError`
    (loaded but not the sealing one); every other defect is the same undifferentiated corrupt error.
    """
    _checked_identity(secret_type, scope_id, version, public_id)
    data = bytes(blob)
    magic_len = len(ESCROW_ENVELOPE_MAGIC)
    if len(data) < _HEADER_BYTES + _TAG_BYTES + 1 or not hmac.compare_digest(
        data[:magic_len], ESCROW_ENVELOPE_MAGIC
    ):
        raise _open_failure(secret_type, version)
    header_kek_id = data[magic_len + 1 : magic_len + 1 + _KEK_ID_BYTES].hex()
    if data[magic_len] != ESCROW_SEAL_V1 or not hmac.compare_digest(
        _checked_kek_id(kek_id).encode(), header_kek_id.encode()
    ):
        raise _open_failure(secret_type, version)
    aad = escrow_aad(
        secret_type=secret_type,
        scope_id=scope_id,
        version=version,
        kek_id=header_kek_id,
        public_id=public_id,
        fingerprint=fingerprint,
    )
    iv = data[magic_len + 1 + _KEK_ID_BYTES : _HEADER_BYTES]
    try:
        with _platform_key(kek, _ESCROW_INFO, header_kek_id) as key:
            return AESGCM(key).decrypt(iv, data[_HEADER_BYTES:], aad)
    except PlatformSecretKekUnavailableError:
        _log_open_failure(secret_type, version)
        raise
    except (InvalidTag, ValueError):
        raise _open_failure(secret_type, version) from None


def constant_time_equal(left: bytes, right: bytes) -> bool:
    """Constant-time bytes comparison (so callers outside the vault need no `hmac` import)."""
    return hmac.compare_digest(left, right)


def sign_escrow_manifest(
    manifest_body: bytes, *, kek: PlatformKekAdapter, kek_id: str | None = None
) -> bytes:
    """Keyed MAC (HMAC-SHA256 under a KEK-derived key) over the canonical manifest bytes."""
    return _platform_mac(kek, _MANIFEST_INFO, _length_prefixed(manifest_body), kek_id)


def verify_escrow_manifest(
    manifest_body: bytes, mac: bytes, *, kek: PlatformKekAdapter, kek_id: str
) -> bool:
    """Constant-time check of a manifest MAC under the named KEK.

    False for a tampered body or a MAC from another key; raises
    :class:`PlatformSecretKekUnavailableError` when no loaded KEK has `kek_id`.
    """
    expected = sign_escrow_manifest(manifest_body, kek=kek, kek_id=_checked_kek_id(kek_id))
    return hmac.compare_digest(expected, bytes(mac))


def _platform_mac(
    kek: PlatformKekAdapter, info: bytes, message: bytes, kek_id: str | None
) -> bytes:
    """HMAC-SHA256 of `message` under the KEK-derived key for `info`."""
    with _platform_key(kek, info, kek_id) as key:
        return hmac.new(bytes(key), message, hashlib.sha256).digest()


def _log_open_failure(secret_type: int, version: int) -> None:
    """Warn that an open failed -- ints only, never the envelope, a key or a plaintext."""
    logger.warning("platform secret open failed type=%d version=%d", secret_type, version)


def _open_failure(secret_type: int, version: int) -> PlatformSecretCorruptError:
    """Log (ints only) and build the one undifferentiated open failure."""
    _log_open_failure(secret_type, version)
    return PlatformSecretCorruptError("platform secret failed authentication")


# Key material for platform secrets: generation and parsing of the four algorithm families. Lives in
# the vault because it is the only module allowed to import `cryptography` for this key family
# (seal-callers guard); the registry / bootstrap only call these. Plaintext layout (what is sealed):
# Ed25519 = raw 32-byte seed, ES256 = raw 32-byte big-endian P-256 scalar, random 256-bit =
# 32 raw bytes, password = 43-char unpadded base64url text of 32 random bytes.
# Public material is the raw 32-byte Ed25519 key / the 65-byte uncompressed P-256 point.

#: ``platform_vault.secrets.algorithm`` codes (the migration documents the same mapping).
PLATFORM_ALG_ED25519: Final = 1
PLATFORM_ALG_ES256: Final = 2
PLATFORM_ALG_RANDOM_256: Final = 3
PLATFORM_ALG_PASSWORD: Final = 4
_PLATFORM_ALGORITHMS: Final = frozenset(
    {PLATFORM_ALG_ED25519, PLATFORM_ALG_ES256, PLATFORM_ALG_RANDOM_256, PLATFORM_ALG_PASSWORD}
)
_RANDOM_BYTES: Final = 32


@dataclass(slots=True, frozen=True)
class PlatformKeyMaterial:
    """One secret value plus the public columns derived from it (never printed: repr hides it)."""

    plaintext: bytes = field(repr=False)
    public_material: bytes | None
    kid: str | None


def platform_kid(public_material: bytes) -> str:
    """Deterministic key id for a public key: ``k`` + first 16 hex chars of its SHA-256."""
    return "k" + hashlib.sha256(public_material).hexdigest()[:16]


def _public_columns(algorithm: int, plaintext: bytes, kid: str | None) -> PlatformKeyMaterial:
    """Parse an Ed25519 seed / P-256 scalar and return the material with its public key and kid."""
    if algorithm == PLATFORM_ALG_ED25519:
        if len(plaintext) != _RANDOM_BYTES:
            raise PlatformSecretInputError("an Ed25519 seed must be exactly 32 bytes")
        public = (
            Ed25519PrivateKey.from_private_bytes(plaintext)
            .public_key()
            .public_bytes(Encoding.Raw, PublicFormat.Raw)
        )
    else:
        if len(plaintext) != _RANDOM_BYTES:
            raise PlatformSecretInputError("a P-256 private scalar must be exactly 32 bytes")
        try:
            ec_key = ec.derive_private_key(int.from_bytes(plaintext, "big"), ec.SECP256R1())
        except ValueError:
            raise PlatformSecretInputError("not a valid P-256 private scalar") from None
        public = ec_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return PlatformKeyMaterial(plaintext, public, kid or platform_kid(public))


def platform_key_material_from_private(
    algorithm: int, plaintext: bytes, *, kid: str | None = None
) -> PlatformKeyMaterial:
    """Validate imported asymmetric key material and derive its public columns.

    Only Ed25519 (32-byte seed) and ES256 (32-byte P-256 scalar) can be parsed; any other
    algorithm, a wrong length or a degenerate scalar raises :class:`PlatformSecretInputError`
    with a fixed message (never the value).
    """
    if algorithm not in (PLATFORM_ALG_ED25519, PLATFORM_ALG_ES256):
        raise PlatformSecretInputError("only asymmetric algorithms carry parseable key material")
    return _public_columns(algorithm, bytes(plaintext), kid)


def generate_platform_key_material(algorithm: int) -> PlatformKeyMaterial:
    """Generate fresh material for one algorithm code from the OS CSPRNG.

    Ed25519 and ES256 return the private bytes plus public material and kid; random-256 and
    password return only the secret (no public columns, no kid), as the migration's CHECK requires.
    """
    if algorithm == PLATFORM_ALG_ED25519:
        seed = Ed25519PrivateKey.generate().private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption()
        )
        return _public_columns(algorithm, seed, None)
    if algorithm == PLATFORM_ALG_ES256:
        scalar = (
            ec.generate_private_key(ec.SECP256R1())
            .private_numbers()
            .private_value.to_bytes(_RANDOM_BYTES, "big")
        )
        return _public_columns(algorithm, scalar, None)
    if algorithm == PLATFORM_ALG_RANDOM_256:
        return PlatformKeyMaterial(secrets.token_bytes(_RANDOM_BYTES), None, None)
    if algorithm == PLATFORM_ALG_PASSWORD:
        text = base64.urlsafe_b64encode(secrets.token_bytes(_RANDOM_BYTES)).rstrip(b"=")
        return PlatformKeyMaterial(text, None, None)
    raise PlatformSecretInputError("unknown platform secret algorithm")
