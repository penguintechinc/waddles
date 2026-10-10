"""Per-tenant envelope encryption for identity fields on the ingest->process Valkey stream.

Implements the wire format from `docs/superpowers/specs/
2026-09-28-tenant-envelope-encryption-design.md` (AES-256-GCM, versioned
envelope header, length-prefixed AAD) -- the same shape as `penguin_security.
crypto.envelope` (`~/code/penguin-libs/.worktrees/security-crypto-module`,
unmerged/unpublished as of this writing, see `DekProvider` docstring below)
-- so a raw platform username/login/display-name/handle never sits in
plaintext in the `:process` Valkey key, which lives outside the PII
boundary (`critical-rules.md` PII Tokenization: only hub-api holds raw PII).

AAD = tenant_id | stream | field | event_id | dek_version, length-prefixed
per field (never delimiter-joined, so no combination of values collides).

**Fail closed, always.** If a tenant DEK cannot be resolved, the caller
(`runner.py`) must never fall back to writing plaintext -- see
`DekUnavailableError` and `runner.IngestRunner`'s dead-letter path.
"""

from __future__ import annotations

import base64
import os
import struct
import time
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_FORMAT_VERSION = 1
_NONCE_LEN = 12  # 96-bit GCM nonce, CSPRNG per NIST SP 800-38D
_HEADER_STRUCT = struct.Struct(">BI")  # format version (1B) + dek_version (4B)
_LEN_STRUCT = struct.Struct(">I")
_DEK_LEN = 32  # AES-256
DEFAULT_DEK_CACHE_TTL_S = 600  # matches the tenant-envelope-encryption design's 10-minute TTL

# Identity fields recognized on a normalized platform event -- everything
# else (payload.text, timestamps, ids) stays plaintext per the task's
# scope; message content and other non-identity fields are out of scope
# here (they are the hub-api/Postgres design's concern, not this stream).
IDENTITY_FIELDS: tuple[str, ...] = ("actor",)


class CiphertextFormatError(ValueError):
    """Raised when a ciphertext envelope is malformed, truncated, or an unknown version."""


class DekUnavailableError(RuntimeError):
    """Raised when a tenant's DEK cannot be resolved -- the caller MUST fail closed.

    Never caught-and-ignored to fall back to plaintext; `runner.py` catches
    this specifically to dead-letter the event and increment a metric.
    """


def _encode_field(value: str | int) -> bytes:
    """Length-prefix-encode one AAD field so no combination of values can collide."""
    raw = str(value).encode("utf-8")
    return _LEN_STRUCT.pack(len(raw)) + raw


def build_aad(*, tenant_id: str, stream: str, field: str, event_id: str, dek_version: int) -> bytes:
    """Build the canonical AAD binding one encrypted field to its exact context.

    `AAD = tenant_id|stream|field|event_id|dek_version`, length-prefixed --
    GCM's auth tag covers this, so copying raw ciphertext bytes into
    another tenant/stream/field/event fails to decrypt rather than
    silently decrypting under the wrong context.
    """
    return (
        _encode_field(tenant_id)
        + _encode_field(stream)
        + _encode_field(field)
        + _encode_field(event_id)
        + _encode_field(dek_version)
    )


def envelope_encrypt(plaintext: str, dek: bytes, *, dek_version: int, aad: bytes) -> bytes:
    """Encrypt one identity field value into a versioned envelope ciphertext.

    Returns: `version(1B) || dek_version(4B) || nonce(12B) || ciphertext||tag`.
    """
    if len(dek) != _DEK_LEN:
        raise ValueError(f"DEK must be {_DEK_LEN} bytes, got {len(dek)}")
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), aad)
    return _HEADER_STRUCT.pack(_FORMAT_VERSION, dek_version) + nonce + ct


def envelope_decrypt(envelope: bytes, dek: bytes, *, aad: bytes) -> str:
    """Decrypt a versioned envelope ciphertext produced by `envelope_encrypt`.

    Raises `InvalidTag` (from `cryptography`) on any AAD/key/ciphertext
    mismatch -- wrong tenant, wrong event, wrong field, or a stale
    dek_version passed the wrong key.
    """
    if len(dek) != _DEK_LEN:
        raise ValueError(f"DEK must be {_DEK_LEN} bytes, got {len(dek)}")
    header_len = _HEADER_STRUCT.size
    min_len = header_len + _NONCE_LEN + 16
    if len(envelope) < min_len:
        raise CiphertextFormatError(f"envelope too short: {len(envelope)} < {min_len} bytes")
    version, _dek_version = _HEADER_STRUCT.unpack_from(envelope, 0)
    if version != _FORMAT_VERSION:
        raise CiphertextFormatError(f"unsupported envelope format version: {version}")
    offset = header_len
    nonce = envelope[offset : offset + _NONCE_LEN]
    ct = envelope[offset + _NONCE_LEN :]
    try:
        return AESGCM(dek).decrypt(nonce, ct, aad).decode("utf-8")
    except InvalidTag:
        raise


def envelope_dek_version(envelope: bytes) -> int:
    """Read the dek_version out of an envelope header without decrypting it."""
    header_len = _HEADER_STRUCT.size
    if len(envelope) < header_len:
        raise CiphertextFormatError(f"envelope too short: {len(envelope)} < {header_len} bytes")
    version, dek_version = _HEADER_STRUCT.unpack_from(envelope, 0)
    if version != _FORMAT_VERSION:
        raise CiphertextFormatError(f"unsupported envelope format version: {version}")
    return int(dek_version)


def to_json_envelope(envelope: bytes) -> dict[str, Any]:
    """Split a raw envelope into the JSON-safe `{v, dek_version, nonce, ct}` shape."""
    header_len = _HEADER_STRUCT.size
    version, dek_version = _HEADER_STRUCT.unpack_from(envelope, 0)
    nonce = envelope[header_len : header_len + _NONCE_LEN]
    ct = envelope[header_len + _NONCE_LEN :]
    return {
        "v": version,
        "dek_version": dek_version,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
    }


def from_json_envelope(obj: dict[str, Any]) -> bytes:
    """Reassemble the raw envelope bytes from the JSON `{v, dek_version, nonce, ct}` shape."""
    try:
        version = int(obj["v"])
        dek_version = int(obj["dek_version"])
        nonce = base64.b64decode(obj["nonce"])
        ct = base64.b64decode(obj["ct"])
    except (KeyError, ValueError, TypeError) as exc:
        raise CiphertextFormatError(f"malformed ciphertext envelope: {exc}") from exc
    return _HEADER_STRUCT.pack(version, dek_version) + nonce + ct


def encrypt_identity_value(
    plaintext: str,
    dek: bytes,
    *,
    dek_version: int,
    tenant_id: str,
    stream: str,
    field: str,
    event_id: str,
) -> dict[str, Any]:
    """Encrypt one identity field value to the JSON envelope shape, AAD-bound to its context."""
    aad = build_aad(
        tenant_id=tenant_id, stream=stream, field=field, event_id=event_id, dek_version=dek_version
    )
    envelope = envelope_encrypt(plaintext, dek, dek_version=dek_version, aad=aad)
    return to_json_envelope(envelope)


def decrypt_identity_value(
    obj: dict[str, Any], dek: bytes, *, tenant_id: str, stream: str, field: str, event_id: str
) -> str:
    """Decrypt one identity field's JSON envelope, re-deriving the exact same AAD."""
    envelope = from_json_envelope(obj)
    dek_version = envelope_dek_version(envelope)
    aad = build_aad(
        tenant_id=tenant_id, stream=stream, field=field, event_id=event_id, dek_version=dek_version
    )
    return envelope_decrypt(envelope, dek, aad=aad)


# --------------------------------------------------------------------------
# DEK provider: hub-api-issued, TTL-cached, never hardcoded.
# --------------------------------------------------------------------------


class DekProvider(Protocol):
    """Resolves a tenant's active (or a specific, for rotation-window reads) DEK.

    **No keystore/DEK-distribution service exists in this codebase yet**
    (checked at implementation time: no `tenant_encryption_keys` table, no
    hub-api broker endpoint, no published `penguin_security.crypto`
    package -- that module exists only in an unmerged/unpublished
    penguin-libs branch, `packages/python-security/src/penguin_security/
    crypto/envelope/`, and this module's wire format matches it exactly
    so the two interoperate once it ships). `HubApiDekProvider` below is
    the minimal spec-conformant client for the broker endpoint the
    tenant-envelope-encryption design describes (S5: "hub-api decrypts,
    everyone else asks") -- **the server side of that endpoint does not
    exist yet either**; this is called out explicitly in the PR.
    """

    async def get_dek(self, tenant_id: str, *, dek_version: int | None = None) -> tuple[bytes, int]:
        """Return `(dek_bytes, dek_version)`. Raises `DekUnavailableError` on any failure."""
        ...


@dataclass
class _CacheEntry:
    dek: bytes
    dek_version: int
    expires_at: float


class TtlCachedDekProvider:
    """Wraps another `DekProvider` with a per-tenant, per-dek_version TTL cache.

    Multi-process-safe in the sense that matters here: each process/replica
    caches independently (matches the design's "hub-api caches unwrapped
    DEKs in-process, TTL 10 minutes" -- this is the same posture, just
    fronting the ingest-side client instead of hub-api itself). A cache
    miss (expired or never-seen) always re-resolves via `inner`, so a
    rotated DEK is picked up within one TTL window without a restart.
    """

    def __init__(self, inner: DekProvider, *, ttl_s: float = DEFAULT_DEK_CACHE_TTL_S) -> None:
        """Wrap `inner`, caching each `(tenant_id, dek_version)` resolution for `ttl_s` seconds."""
        self._inner = inner
        self._ttl_s = ttl_s
        self._cache: dict[tuple[str, int | None], _CacheEntry] = {}

    async def get_dek(self, tenant_id: str, *, dek_version: int | None = None) -> tuple[bytes, int]:
        """Return the cached `(dek, dek_version)` if fresh, else re-resolve via `inner`."""
        key = (tenant_id, dek_version)
        entry = self._cache.get(key)
        now = time.monotonic()
        if entry is not None and entry.expires_at > now:
            return entry.dek, entry.dek_version
        dek, resolved_version = await self._inner.get_dek(tenant_id, dek_version=dek_version)
        self._cache[key] = _CacheEntry(
            dek=dek, dek_version=resolved_version, expires_at=now + self._ttl_s
        )
        return dek, resolved_version

    def invalidate(self, tenant_id: str) -> None:
        """Evict every cached entry for `tenant_id` -- called on a `dek-rotated:{tenant}` signal."""
        for key in [k for k in self._cache if k[0] == tenant_id]:
            del self._cache[key]


class HubApiDekProvider:
    """Calls hub-api's tenant-DEK broker endpoint -- server side is a documented gap, not built yet.

    Fails closed unconditionally: any non-2xx response, network error, or
    malformed body raises `DekUnavailableError` -- never a silent
    plaintext fallback. Expects hub-api to have already unwrapped the DEK
    server-side (spec S5: "hub-api decrypts, everyone else asks" --
    RO/edge processes are never handed a `wrapped_dek`/`kek_ref` to unwrap
    themselves) and return it over an authenticated internal channel.
    """

    def __init__(self, http_client: Any, hub_api_url: str, jwt_provider: Any) -> None:
        """Bind to one hub-api base URL; `jwt_provider()` mints the internal-call auth token."""
        self._http = http_client
        self._url = hub_api_url.rstrip("/") + "/internal/v1/tenant-keys"
        self._jwt_provider = jwt_provider

    async def get_dek(self, tenant_id: str, *, dek_version: int | None = None) -> tuple[bytes, int]:
        """Fetch `(dek, dek_version)` from hub-api; any failure raises `DekUnavailableError`."""
        params = {"dek_version": dek_version} if dek_version is not None else {}
        try:
            resp = await self._http.get(
                f"{self._url}/{tenant_id}/dek",
                params=params,
                headers={"Authorization": f"Bearer {self._jwt_provider()}"},
                timeout=5.0,
            )
            resp.raise_for_status()
            body = resp.json()
            dek = base64.b64decode(body["dek"])
            resolved_version = int(body["dek_version"])
        except Exception as exc:  # noqa: BLE001 - any failure here must fail closed, never leak plaintext
            raise DekUnavailableError(
                f"tenant DEK unavailable for tenant={tenant_id!r} "
                f"dek_version={dek_version!r}: {exc}"
            ) from exc
        if len(dek) != _DEK_LEN:
            raise DekUnavailableError(f"hub-api returned a malformed DEK for tenant={tenant_id!r}")
        return dek, resolved_version


class LocalDevDekProvider:
    """Dev/alpha-only fallback DEK provider -- HKDF-derives a per-tenant DEK from a local KEK.

    **Not the production mechanism** -- the tenant-envelope-encryption
    design's production posture is hub-api-brokered, KMS-backed DEKs
    (`HubApiDekProvider`). This exists only so local/alpha environments
    without a live hub-api broker endpoint can still exercise real
    AES-256-GCM encryption end-to-end, never a no-op/plaintext stand-in.
    KEK is read from `env_var` (never hardcoded) -- construction fails if
    unset or too short.
    """

    def __init__(self, *, env_var: str = "INGEST_DEV_KEK") -> None:
        """Resolve the dev KEK from `env_var`; raises if unset or too short."""
        raw = os.environ.get(env_var)
        if not raw:
            raise ValueError(
                f"{env_var} is not set -- LocalDevDekProvider requires an explicit dev KEK, "
                "never a hardcoded default"
            )
        key_bytes = raw.encode("utf-8")
        if len(key_bytes) < 32:
            raise ValueError(f"{env_var} must resolve to >= 32 bytes of KEK material")
        self._kek = key_bytes[:32]

    async def get_dek(self, tenant_id: str, *, dek_version: int | None = None) -> tuple[bytes, int]:
        """HKDF-derive `(dek, dek_version)` for `tenant_id` from the local dev KEK."""
        version = dek_version if dek_version is not None else 1
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=_DEK_LEN,
            salt=None,
            info=f"waddles-ingest-dev-dek:{tenant_id}:{version}".encode(),
        )
        dek = hkdf.derive(self._kek)
        return dek, version


__all__ = [
    "IDENTITY_FIELDS",
    "CiphertextFormatError",
    "DekUnavailableError",
    "DekProvider",
    "TtlCachedDekProvider",
    "HubApiDekProvider",
    "LocalDevDekProvider",
    "DEFAULT_DEK_CACHE_TTL_S",
    "build_aad",
    "envelope_encrypt",
    "envelope_decrypt",
    "envelope_dek_version",
    "to_json_envelope",
    "from_json_envelope",
    "encrypt_identity_value",
    "decrypt_identity_value",
]
