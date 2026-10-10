"""Platform-baseline KEK adapter -- the default every tenant starts on.

The baseline is the existing platform-managed-key posture (AES-256-GCM
under a key held in the platform Secret), now used one level up the
hierarchy: it wraps each tenant's DEK instead of encrypting data directly.
External KMS / BYOK (:mod:`services.envelope.aws_kms`) is the Enterprise
upgrade layered *on top*, never a replacement -- a tenant with no
entitlement, or no BYOK config, is always on this adapter.

Key source: ``TENANT_KEK_HEX`` (64 hex chars = 256 bits), already provisioned
by the Helm chart's ``autoProvisionedKeys.tenantKek`` Secret, and optionally
``TENANT_KEK_HEX_PREVIOUS`` during a platform-KEK rotation. Each wrapped DEK
carries its KEK's id (:func:`~services.envelope.crypto.kek_id`), so the
adapter unwraps with whichever of {current, previous} wrapped it, and
``rewrap`` migrates rows onto the current one -- rotation is "deploy the new
key as current, move the old one to PREVIOUS, re-wrap, drop PREVIOUS".

Misconfiguration (missing/short/non-hex key) raises
:class:`~services.envelope.errors.PlatformKekError` at *use*, loudly --
never a fallback to a derived, default, or empty key.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Callable, Mapping

from services.envelope.crypto import (
    KEY_LENGTH,
    kek_id,
    local_unwrap,
    local_wrap,
    local_wrap_key_id,
)
from services.envelope.errors import EnvelopeInputError, KmsRejectedError, PlatformKekError
from services.envelope.models import KEK_KIND_PLATFORM, KmsKeyInfo

CURRENT_ENV = "TENANT_KEK_HEX"
PREVIOUS_ENV = "TENANT_KEK_HEX_PREVIOUS"
_HEX_LENGTH = KEY_LENGTH * 2


def _parse_kek(name: str, raw: str) -> bytes:
    """Decode a 64-hex-char KEK from env var `name`; never echo the value in an error."""
    if len(raw) != _HEX_LENGTH:
        raise PlatformKekError(f"{name} must be {_HEX_LENGTH} hex characters (256-bit key)")
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise PlatformKekError(f"{name} must be hex-encoded") from exc


class PlatformKekAdapter:
    """:class:`~services.envelope.kms_adapter.KmsAdapter` over the platform baseline KEK."""

    def __init__(self, current: bytes, previous: bytes | None = None) -> None:
        """Hold the current KEK (wraps) and an optional previous one (unwrap-only, rotation)."""
        if len(current) != KEY_LENGTH or (previous is not None and len(previous) != KEY_LENGTH):
            raise PlatformKekError("platform KEKs must be exactly 32 bytes")
        self._current = current
        self._current_id = kek_id(current)
        self._by_id: dict[str, bytes] = {self._current_id: current}
        if previous is not None:
            self._by_id[kek_id(previous)] = previous

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> PlatformKekAdapter:
        """Build from ``TENANT_KEK_HEX`` (+ optional ``TENANT_KEK_HEX_PREVIOUS``).

        Raises:
            PlatformKekError: the current key is missing or malformed.
        """
        env = os.environ if environ is None else environ
        raw = env.get(CURRENT_ENV, "").strip()
        if not raw:
            raise PlatformKekError(f"{CURRENT_ENV} is not set -- the platform KEK is required")
        previous_raw = env.get(PREVIOUS_ENV, "").strip()
        previous = _parse_kek(PREVIOUS_ENV, previous_raw) if previous_raw else None
        return cls(_parse_kek(CURRENT_ENV, raw), previous)

    @property
    def kek_kind(self) -> str:
        """Always ``platform``."""
        return KEK_KIND_PLATFORM

    @property
    def key_ref(self) -> str:
        """``platform:<kek-id>`` -- identifies the current KEK without revealing it."""
        return f"platform:{self._current_id}"

    async def wrap(self, plaintext_dek: bytes, *, context: Mapping[str, str]) -> bytes:
        """Wrap under the current KEK (CPU-only, microseconds -- no thread hop)."""
        return local_wrap(self._current, plaintext_dek, context)

    async def unwrap(self, wrapped_dek: bytes, *, context: Mapping[str, str]) -> bytes:
        """Unwrap with whichever configured KEK wrapped it; unknown KEK id fails closed."""
        try:
            wrapped_with = local_wrap_key_id(wrapped_dek)
        except EnvelopeInputError as exc:
            raise KmsRejectedError("wrapped DEK is not a platform-KEK wrap") from exc
        kek = self._by_id.get(wrapped_with)
        if kek is None:
            raise KmsRejectedError("wrapped DEK was sealed by a platform KEK that is not loaded")
        return local_unwrap(kek, wrapped_dek, context)

    async def verify(self) -> KmsKeyInfo:
        """Self-test: a wrap/unwrap probe under the current KEK."""
        probe = os.urandom(KEY_LENGTH)
        context = {"waddles_purpose": "verify"}
        out = await self.unwrap(await self.wrap(probe, context=context), context=context)
        if not hmac.compare_digest(out, probe):
            raise PlatformKekError("platform KEK probe did not round-trip")
        return KmsKeyInfo(provider="platform", key_ref=self.key_ref, key_state="Enabled")


class LazyPlatformKek:
    """Builds :class:`PlatformKekAdapter` on first use and re-raises config errors every time.

    hub-api must boot even where the platform Secret is not mounted yet
    (nothing consumes the envelope until a feature is enabled), but a
    consumer that *does* reach for the baseline must fail loud rather than
    silently skip encryption. A failure is deliberately not cached, so
    fixing the environment and restarting needs no extra step.
    """

    def __init__(self, factory: Callable[[], PlatformKekAdapter] = PlatformKekAdapter.from_env):
        """`factory` is injectable for tests; defaults to :meth:`PlatformKekAdapter.from_env`."""
        self._factory = factory
        self._adapter: PlatformKekAdapter | None = None

    def get(self) -> PlatformKekAdapter:
        """Return the adapter, building it (and raising :class:`PlatformKekError`) if needed."""
        if self._adapter is None:
            self._adapter = self._factory()
        return self._adapter
