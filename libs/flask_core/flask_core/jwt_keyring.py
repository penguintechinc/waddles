"""Process-wide ES256 key ring for platform JWTs (Secrets T10).

The platform JWT used to be an HMAC token signed *and* verified with one shared secret, which
every verifier could therefore also mint with. It is now ES256: only hub-api holds the private
key (sealed in ``platform_vault``); every verifier holds public keys only. This module is the
seam between ``flask_core.auth`` and whatever supplies the keys:

* ``flask_core`` never reads a key from an env var or config -- a missing ring FAILS CLOSED
  (:class:`NoSigningKeyError` / :class:`UnknownKidError`), there is no shared-secret fallback;
* the verifying key is selected by the token's ``kid`` and nothing else, and the algorithm is a
  property of the key ring (always ES256), never read from the token header;
* the ring (not this module) enforces the verify-only window of a rotated-out key.

The concrete ring is installed once at process start by the service
(``hub_api`` installs a vault-backed one; tests install :func:`generate_test_keyring`).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

#: The one algorithm every platform JWT key uses. Pinned in code, never taken from a header.
PLATFORM_JWT_ALGORITHM: Final = "ES256"


class JwtKeyringError(LookupError):
    """Base for key-ring failures; messages are fixed strings (never key material)."""


class NoSigningKeyError(JwtKeyringError):
    """No active signing key is available -- the caller must refuse to mint."""


class UnknownKidError(JwtKeyringError):
    """No servable verification key carries that `kid` (unknown, retired or past its window)."""


@dataclass(slots=True, frozen=True)
class JwtSigningKey:
    """The active signing key: its `kid` and a ``cryptography`` P-256 private key object."""

    kid: str
    private_key: Any = field(repr=False)


@dataclass(slots=True, frozen=True)
class JwtVerifyKey:
    """A servable verification key: its `kid` and a ``cryptography`` P-256 public key object."""

    kid: str
    public_key: Any = field(repr=False)


@runtime_checkable
class JwtKeyring(Protocol):
    """Source of the platform's JWT keys. Implementations must fail closed, never default."""

    def signing_key(self) -> JwtSigningKey:
        """The active signing key, or raise :class:`NoSigningKeyError`."""
        ...

    def verify_key(self, kid: str) -> JwtVerifyKey:
        """The servable verification key for `kid`, or raise :class:`UnknownKidError`."""
        ...


_lock = threading.Lock()
_keyring: JwtKeyring | None = None


def set_jwt_keyring(keyring: JwtKeyring | None) -> None:
    """Install (or clear, with None) the process-wide key ring."""
    global _keyring  # noqa: PLW0603 -- the ring is process-wide by design
    with _lock:
        _keyring = keyring


def get_jwt_keyring() -> JwtKeyring | None:
    """The installed key ring, or None when the service never installed one."""
    return _keyring


def require_signing_key() -> JwtSigningKey:
    """The active signing key from the installed ring; fails closed when there is none."""
    ring = _keyring
    if ring is None:
        raise NoSigningKeyError("no JWT key ring is installed")
    return ring.signing_key()


def require_verify_key(kid: str) -> JwtVerifyKey:
    """The verification key for `kid` from the installed ring; fails closed when there is none."""
    ring = _keyring
    if ring is None:
        raise UnknownKidError("no JWT key ring is installed")
    return ring.verify_key(kid)


@dataclass(slots=True)
class InMemoryJwtKeyring:
    """A key ring over explicit keys: for tests and for tools that mint with a throwaway key.

    ``verify_only`` models a rotated-out key (verifies, never signs); ``expired`` kids are
    refused exactly like a version past its ``verify_until``.
    """

    active: JwtSigningKey
    verify_only: dict[str, JwtVerifyKey] = field(default_factory=dict)
    expired: set[str] = field(default_factory=set)

    def signing_key(self) -> JwtSigningKey:
        """The active signing key."""
        return self.active

    def verify_key(self, kid: str) -> JwtVerifyKey:
        """Active public key or a still-honoured verify-only key; anything else fails closed."""
        if kid in self.expired:
            raise UnknownKidError("no servable JWT key has that key id")
        if kid == self.active.kid:
            return JwtVerifyKey(kid, self.active.private_key.public_key())
        found = self.verify_only.get(kid)
        if found is None:
            raise UnknownKidError("no servable JWT key has that key id")
        return found


def generate_test_keyring(kid: str = "ktest0000000001") -> InMemoryJwtKeyring:
    """A fresh in-memory ES256 ring with one random P-256 key (tests only; never persisted)."""
    from cryptography.hazmat.primitives.asymmetric import ec

    return InMemoryJwtKeyring(JwtSigningKey(kid, ec.generate_private_key(ec.SECP256R1())))
