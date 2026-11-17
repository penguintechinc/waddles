"""Platform-vault backed ES256 key ring for platform JWTs (Secrets T10).

Adapts :class:`PlatformSecretCache` to ``flask_core.jwt_keyring.JwtKeyring``: the ACTIVE
``JWT_SIGNING`` version signs; verification looks the `kid` up among servable versions only
(active, or verify-only before its ``verify_until`` -- the cache enforces that window at lookup
time). Everything fails closed: an empty cache, an unknown kid or a corrupt row raises, and there
is no shared-secret fallback anywhere.
"""

from __future__ import annotations

import logging
import re

from flask_core.jwt_keyring import (
    JwtSigningKey,
    JwtVerifyKey,
    NoSigningKeyError,
    UnknownKidError,
)

from services.platform_secrets.cache import (
    NoActiveSecretError,
    PlatformSecretCache,
    UnknownKeyIdError,
)
from services.platform_secrets.registry import SecretType
from services.provider_connections.vault import (
    PlatformSecretInputError,
    es256_private_key,
    es256_public_key,
)

logger = logging.getLogger(__name__)

#: The only kid shape the vault mints for JWT keys (`platform_kid`: "k" + 16 hex). Anything else
#: is refused BEFORE the cache, so hostile kids never reach a lookup, a reload or a negative entry.
_KID_RE = re.compile(r"k[0-9a-f]{16}")


class PlatformVaultJwtKeyring:
    """Signing + verification keys for platform JWTs, read from the process secret cache."""

    def __init__(self, cache: PlatformSecretCache) -> None:
        """Bind to the (process-wide) platform secret cache."""
        self._cache = cache

    def signing_key(self) -> JwtSigningKey:
        """The active JWT signing key; raises :class:`NoSigningKeyError` when there is none."""
        try:
            entry = self._cache.active(int(SecretType.JWT_SIGNING))
        except NoActiveSecretError:
            raise NoSigningKeyError("no active JWT signing key in the platform vault") from None
        if entry.kid is None:
            raise NoSigningKeyError("the active JWT signing key has no key id")
        try:
            return JwtSigningKey(entry.kid, es256_private_key(entry.plaintext))
        except PlatformSecretInputError:
            logger.error("active JWT signing key is not a valid P-256 scalar")
            raise NoSigningKeyError("the active JWT signing key is unusable") from None

    def verify_key(self, kid: str) -> JwtVerifyKey:
        """The servable public key for `kid`; unknown / retired / expired fails closed."""
        if not isinstance(kid, str) or _KID_RE.fullmatch(kid) is None:
            raise UnknownKidError("no servable JWT key has that key id")
        try:
            entry = self._cache.by_kid(int(SecretType.JWT_SIGNING), kid)
        except UnknownKeyIdError:
            raise UnknownKidError("no servable JWT key has that key id") from None
        if entry.public_material is None:
            raise UnknownKidError("no servable JWT key has that key id")
        try:
            return JwtVerifyKey(kid, es256_public_key(entry.public_material))
        except PlatformSecretInputError:
            logger.error("a published JWT public key is not a valid P-256 point")
            raise UnknownKidError("no servable JWT key has that key id") from None
