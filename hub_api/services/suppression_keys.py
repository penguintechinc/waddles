"""Per-tenant keyed one-way codes for ban-evasion suppression (GDPR Art. 6(1)(f)).

When a data subject with an *active* ban erases their account, the only thing
Waddles keeps is a ``suppression_records`` row (``services/ban_suppression.py``)
whose identifying column is the code computed here -- never the platform id::

    tenant_key = HKDF-SHA256(ikm = K_master, salt = none,
                             info = "waddles-suppression/v1/tenant:<tenant_id>")
    code       = HMAC-SHA256(tenant_key, "waddles-suppression/v1" || 0x00
                                         || platform || 0x00 || platform_user_id)

Why this shape (each point has a regression test in
``tests/test_suppression_keys.py``):

* **Keyed, not salted.** A static salt (or none) over a 17-19 digit snowflake
  is a lookup table -- an attacker with the table enumerates every plausible
  id in seconds. HMAC under a secret key is not invertible or testable
  without that key, so a leaked database alone yields no platform ids.
* **Per tenant.** The tenant key is HKDF-derived per tenant id, so the same
  platform id yields *different* codes in two tenants and a code can only ever
  match inside the tenant that created it. Waddles is a processor for each
  tenant; a cross-tenant match would make it a controller of a global ban
  list. The platform is bound into the message (not only the ``platform``
  column) so equal ids on two platforms are not linkable either.
* **Versioned.** ``key_ver`` records which master key produced a code, so the
  platform KEK can rotate: lookups try the current key and the previous one.
* **Fail loud.** A missing/short/non-hex master key raises
  :class:`SuppressionKeyError` at use. There is no default, derived or empty
  key, and no "skip the suppression" fallback.

``K_master`` is the platform tenant KEK (``TENANT_KEK_HEX``, optionally
``TENANT_KEK_HEX_PREVIOUS`` during rotation), the same Secret the per-tenant
envelope / external-KMS work (PRs #442 / #789, ``services/envelope/
platform_kek.py``) wraps tenant DEKs with. Those PRs are not on this branch's
base, so :class:`SuppressionKeySource` is the seam: this module reads the same
env contract and uses the same key-id scheme (``sha256(key)[:8]`` hex); once
#789 lands, an adapter over ``services.envelope.platform_kek`` can replace
:class:`PlatformKekKeySource` without touching any caller. The key never leaves
hub-api (the PII boundary) and never appears in a log line, repr or error text.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

#: Platform KEK env vars -- same names as ``services/envelope/platform_kek.py`` (#789).
ENV_CURRENT: Final = "TENANT_KEK_HEX"
ENV_PREVIOUS: Final = "TENANT_KEK_HEX_PREVIOUS"

KEY_LENGTH: Final = 32
_HEX_LENGTH: Final = KEY_LENGTH * 2
_KEY_VER_BYTES: Final = 8
_INFO_PREFIX: Final = b"waddles-suppression/v1/tenant:"
_MESSAGE_PREFIX: Final = b"waddles-suppression/v1\x00"
_PLATFORM_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")
_MAX_PLATFORM_USER_ID: Final = 255


class SuppressionKeyError(RuntimeError):
    """The master key is missing or malformed. Never carries key material in its text."""


class SuppressionInputError(ValueError):
    """A tenant id / platform / platform user id failed validation (never echoes the value)."""


@dataclass(slots=True, frozen=True)
class MasterKey:
    """One master key and its non-reversible version id (safe to store and log)."""

    key_ver: str
    material: bytes = field(repr=False)


@dataclass(slots=True, frozen=True)
class MasterKeys:
    """The current master key (writes) plus previous ones (lookups only, rotation window)."""

    current: MasterKey
    previous: tuple[MasterKey, ...] = ()

    def all(self) -> tuple[MasterKey, ...]:
        """Current first, then previous -- the order lookups try them in."""
        return (self.current, *self.previous)


@dataclass(slots=True, frozen=True)
class SuppressionCode:
    """A stored code: the key version that produced it and 64 lowercase hex chars of HMAC."""

    key_ver: str
    hmac: str


class SuppressionKeySource(Protocol):
    """Where ``K_master`` comes from -- the seam #789's envelope adapter will plug into."""

    def master_keys(self) -> MasterKeys:
        """Return the loaded master keys, or raise :class:`SuppressionKeyError`."""
        ...


def key_version(material: bytes) -> str:
    """Stable 16-hex-char, non-reversible id for a master key (SHA-256 prefix)."""
    return hashlib.sha256(material).digest()[:_KEY_VER_BYTES].hex()


def _parse_key(name: str, raw: str) -> MasterKey:
    """Decode a 64-hex-char key from env var `name`; never echo the value in an error."""
    if len(raw) != _HEX_LENGTH:
        raise SuppressionKeyError(f"{name} must be {_HEX_LENGTH} hex characters (256-bit key)")
    try:
        material = bytes.fromhex(raw)
    except ValueError as exc:
        raise SuppressionKeyError(f"{name} must be hex-encoded") from exc
    return MasterKey(key_ver=key_version(material), material=material)


class PlatformKekKeySource:
    """:class:`SuppressionKeySource` over ``TENANT_KEK_HEX`` / ``TENANT_KEK_HEX_PREVIOUS``.

    Reads the environment on every call and never caches a failure, so fixing
    the Secret needs no extra step and a test's ``monkeypatch.setenv`` is seen
    at once. The decode + SHA-256 is microseconds -- not worth a stale cache.
    """

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        """`environ` is injectable for tests; defaults to ``os.environ`` (read lazily)."""
        self._environ = environ

    def master_keys(self) -> MasterKeys:
        """Load the current (+ optional previous) key.

        Raises:
            SuppressionKeyError: the current key is missing/malformed, or the previous key is
                malformed (a broken rotation must not silently lose lookups).
        """
        env = os.environ if self._environ is None else self._environ
        raw = env.get(ENV_CURRENT, "").strip()
        if not raw:
            raise SuppressionKeyError(
                f"{ENV_CURRENT} is not set -- ban-suppression needs the platform KEK"
            )
        current = _parse_key(ENV_CURRENT, raw)
        previous_raw = env.get(ENV_PREVIOUS, "").strip()
        previous = (_parse_key(ENV_PREVIOUS, previous_raw),) if previous_raw else ()
        return MasterKeys(current=current, previous=previous)


def validate_tenant_id(tenant_id: int) -> int:
    """Return `tenant_id` if it is a positive int, else raise :class:`SuppressionInputError`."""
    if isinstance(tenant_id, bool) or not isinstance(tenant_id, int) or tenant_id < 1:
        raise SuppressionInputError("tenant_id must be a positive integer")
    return tenant_id


def normalize_identity(platform: str, platform_user_id: str) -> tuple[str, str]:
    """Canonicalise a platform identity: platform lower-cased, id stripped but case-preserved.

    Raises:
        SuppressionInputError: platform is not a plain lowercase token, or the id is empty,
            too long, or carries a control character (NUL is the MAC field separator).
    """
    if not isinstance(platform, str) or not isinstance(platform_user_id, str):
        raise SuppressionInputError("platform and platform_user_id must be strings")
    plat = platform.strip().lower()
    puid = platform_user_id.strip()
    if not _PLATFORM_RE.match(plat):
        raise SuppressionInputError("platform is not a valid platform token")
    if not puid or len(puid) > _MAX_PLATFORM_USER_ID or _CONTROL_RE.search(puid):
        raise SuppressionInputError("platform_user_id is empty, too long or has control chars")
    return plat, puid


def derive_tenant_key(master: MasterKey, tenant_id: int) -> bytes:
    """HKDF-SHA256 the per-tenant MAC key from `master` (domain-separated by tenant id)."""
    info = _INFO_PREFIX + str(validate_tenant_id(tenant_id)).encode("ascii")
    return HKDF(algorithm=hashes.SHA256(), length=KEY_LENGTH, salt=None, info=info).derive(
        master.material
    )


def compute_code(
    master: MasterKey, tenant_id: int, platform: str, platform_user_id: str
) -> SuppressionCode:
    """Compute the keyed one-way code for one identity under one master key.

    Deterministic (same inputs -> same code, which is what makes a return
    matchable) and one-way without `master`.

    Raises:
        SuppressionInputError: invalid tenant id / platform / platform user id.
    """
    plat, puid = normalize_identity(platform, platform_user_id)
    key = derive_tenant_key(master, tenant_id)
    message = _MESSAGE_PREFIX + plat.encode("utf-8") + b"\x00" + puid.encode("utf-8")
    return SuppressionCode(
        key_ver=master.key_ver, hmac=hmac.new(key, message, hashlib.sha256).hexdigest()
    )


def current_code(
    source: SuppressionKeySource, tenant_id: int, platform: str, platform_user_id: str
) -> SuppressionCode:
    """The code to WRITE: always under the current master key."""
    return compute_code(source.master_keys().current, tenant_id, platform, platform_user_id)


def lookup_codes(
    source: SuppressionKeySource, tenant_id: int, platform: str, platform_user_id: str
) -> list[SuppressionCode]:
    """Every code a stored row for this identity could carry (current, then previous keys)."""
    return [
        compute_code(master, tenant_id, platform, platform_user_id)
        for master in source.master_keys().all()
    ]
