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
``TENANT_KEK_HEX_PREVIOUS`` during rotation) held by the per-tenant envelope
service (``services/envelope/platform_kek.py``, PR #789): this module uses that
adapter -- its env contract, its validation, its key-id scheme and its rotation
window -- and asks it to derive the per-tenant key
(:meth:`~services.envelope.platform_kek.PlatformKekAdapter.derive_key`), so the
KEK material never leaves the adapter and there is no second copy of the
Secret or of its parser. :class:`SuppressionKeySource` stays the seam callers
and tests depend on. The key never leaves hub-api (the PII boundary) and never
appears in a log line, repr or error text.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol

from services.envelope.crypto import kek_id as key_version
from services.envelope.errors import EnvelopeError
from services.envelope.platform_kek import (
    CURRENT_ENV,
    PREVIOUS_ENV,
    LazyPlatformKek,
    PlatformKekAdapter,
)

#: Platform KEK env vars -- re-exported from the envelope seam (#789), which owns them.
ENV_CURRENT: Final = CURRENT_ENV
ENV_PREVIOUS: Final = PREVIOUS_ENV

_INFO_PREFIX: Final = b"waddles-suppression/v1/tenant:"
_MESSAGE_PREFIX: Final = b"waddles-suppression/v1\x00"
_PLATFORM_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")
_MAX_PLATFORM_USER_ID: Final = 255

__all__ = [
    "ENV_CURRENT",
    "ENV_PREVIOUS",
    "PlatformKekKeySource",
    "SuppressionCode",
    "SuppressionInputError",
    "SuppressionKeyError",
    "SuppressionKeySource",
    "TenantKey",
    "compute_code",
    "current_code",
    "key_version",
    "lookup_codes",
    "normalize_identity",
    "validate_tenant_id",
]


class SuppressionKeyError(RuntimeError):
    """The master key is missing or malformed. Never carries key material in its text."""


class SuppressionInputError(ValueError):
    """A tenant id / platform / platform user id failed validation (never echoes the value)."""


@dataclass(slots=True, frozen=True)
class TenantKey:
    """One tenant's HMAC key and the non-reversible id (`key_ver`) of the master key behind it."""

    key_ver: str
    key: bytes = field(repr=False)


@dataclass(slots=True, frozen=True)
class SuppressionCode:
    """A stored code: the key version that produced it and 64 lowercase hex chars of HMAC."""

    key_ver: str
    hmac: str


class SuppressionKeySource(Protocol):
    """Where per-tenant HMAC keys come from -- the seam over the envelope service's platform KEK."""

    def tenant_keys(self, tenant_id: int) -> tuple[TenantKey, ...]:
        """Return the tenant's keys, CURRENT first (writes) then previous ones (lookups only).

        Raises:
            SuppressionKeyError: the master key is unavailable.
            SuppressionInputError: invalid tenant id.
        """
        ...


class PlatformKekKeySource:
    """:class:`SuppressionKeySource` over the envelope service's platform KEK (#789).

    Builds the adapter through :class:`~services.envelope.platform_kek.LazyPlatformKek` -- the
    same lazy, fail-loud, failure-not-cached loader the envelope runtime uses -- and derives each
    tenant key from it; the KEK never leaves the adapter. A fresh source reads the environment on
    first use, so a test's ``monkeypatch.setenv`` is seen and fixing the Secret needs no restart
    of anything but the caller's own source object.
    """

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        platform_kek: LazyPlatformKek | None = None,
    ) -> None:
        """Use `platform_kek` (e.g. the envelope runtime's loader) or build one from `environ`.

        `environ` is injectable for tests and defaults to ``os.environ`` (read on first use).
        """
        if platform_kek is not None:
            self._loader = platform_kek
            return

        def from_environment() -> PlatformKekAdapter:
            return PlatformKekAdapter.from_env(environ)

        self._loader = LazyPlatformKek(from_environment)

    def tenant_keys(self, tenant_id: int) -> tuple[TenantKey, ...]:
        """Derive the tenant's key under every loaded platform KEK, current first."""
        info = _INFO_PREFIX + str(validate_tenant_id(tenant_id)).encode("ascii")
        try:
            adapter = self._loader.get()
            return tuple(
                TenantKey(key_ver=kid, key=adapter.derive_key(info, key_id=kid))
                for kid in adapter.kek_ids
            )
        except EnvelopeError as exc:
            # `PlatformKekError` text names the env var and the rule, never a value.
            raise SuppressionKeyError(str(exc)) from exc


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


def compute_code(tenant_key: TenantKey, platform: str, platform_user_id: str) -> SuppressionCode:
    """Compute the keyed one-way code for one identity under one tenant key.

    Deterministic (same inputs -> same code, which is what makes a return matchable) and
    one-way without the key.

    Raises:
        SuppressionInputError: invalid platform / platform user id.
    """
    plat, puid = normalize_identity(platform, platform_user_id)
    message = _MESSAGE_PREFIX + plat.encode("utf-8") + b"\x00" + puid.encode("utf-8")
    return SuppressionCode(
        key_ver=tenant_key.key_ver,
        hmac=hmac.new(tenant_key.key, message, hashlib.sha256).hexdigest(),
    )


def current_code(
    source: SuppressionKeySource, tenant_id: int, platform: str, platform_user_id: str
) -> SuppressionCode:
    """The code to WRITE: always under the current master key."""
    return compute_code(source.tenant_keys(tenant_id)[0], platform, platform_user_id)


def lookup_codes(
    source: SuppressionKeySource, tenant_id: int, platform: str, platform_user_id: str
) -> list[SuppressionCode]:
    """Every code a stored row for this identity could carry (current, then previous keys)."""
    return [
        compute_code(tenant_key, platform, platform_user_id)
        for tenant_key in source.tenant_keys(tenant_id)
    ]
