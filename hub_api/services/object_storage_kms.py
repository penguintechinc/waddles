"""Server-side-encryption parameters for hub-api's object-storage writes (baseline vs SSE-KMS).

Two postures, one decision point -- every ``put_object`` in :mod:`services.storage_service`
asks :func:`get_object_storage_sse` for its SSE parameters:

- **Baseline (default, zero configuration).** ``ServerSideEncryption="AES256"`` -- SeaweedFS's
  SSE-S3 under the platform KEK (``WEED_S3_SSE_KEK``). Exactly what hub-api sent before this
  module existed; nothing about it depends on a licence, a KMS or any new setting.
- **Customer-managed KMS (Enterprise, on top).** When the chart runs ``kms.objectStorage.mode:
  kms`` it sets ``OBJECT_STORAGE_KMS_ENABLED=true`` and ``OBJECT_STORAGE_KMS_KEY_ID``; uploads then
  carry ``ServerSideEncryption="aws:kms"`` + ``SSEKMSKeyId`` and SeaweedFS envelope-encrypts each
  object under a data key wrapped by the operator's own AWS KMS / Google Cloud KMS key.

**Fail-loud, no silent fallback.** Once KMS mode is on, a write that cannot be KMS-encrypted must
NOT quietly become an AES256 write -- that would downgrade the isolation the operator asked for
while looking like success. So when the Enterprise ``compliance.external_kms`` entitlement is not
held by the configured subject tenant, :meth:`ObjectStorageSse.put_params` raises
:class:`~services.envelope.errors.ExternalKmsNotEntitledError`; a KMS outage surfaces as the S3
error SeaweedFS returns for the PUT. The licence gates *new* KMS writes only: reading objects that
were already encrypted needs no entitlement (SeaweedFS does that), and the exit ramp
(``mode: drain``, which makes ``OBJECT_STORAGE_KMS_ENABLED`` false) is never gated.

**Where the entitlement boundary really is.** Helm cannot ask the licence server, and the
bucket default (``put-bucket-encryption``) is applied by the chart's Admin-identity hook, so
non-hub-api writers (svc-presentation, svc-streaming, the bundle seeder) inherit the KMS bucket
default without a per-write check. hub-api therefore checks the entitlement at startup
(:meth:`ObjectStorageSse.startup_check` logs a loud ERROR when unentitled) and on its own writes;
see ``docs/guides/external-kms-byok.md`` for the operator-facing statement of this limit.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from services.envelope.errors import ExternalKmsNotEntitledError, KmsConfigError
from services.envelope.gate import ExternalKmsEntitlement, ExternalKmsGate, current_request_host

logger = logging.getLogger(__name__)

SSE_BASELINE = "AES256"
SSE_KMS = "aws:kms"

#: AWS key ARN / alias, or a GCP CryptoKey resource name -- also what the chart accepts.
_KEY_ID = re.compile(r"^[A-Za-z0-9:/_.+=,@-]{1,512}$")
_TRUE = frozenset({"1", "true", "yes", "on"})


@dataclass(slots=True, frozen=True)
class ObjectStorageKmsSettings:
    """Object-storage SSE settings, from the chart's ``-kms`` ConfigMap (env vars)."""

    enabled: bool = False
    key_id: str = ""
    tenant_slug: str = "system"

    def __post_init__(self) -> None:
        """Reject an enabled-but-unusable configuration at construction (startup), not per write."""
        if self.enabled and not _KEY_ID.match(self.key_id):
            raise KmsConfigError(
                "OBJECT_STORAGE_KMS_KEY_ID is required (and may only contain "
                "[A-Za-z0-9:/_.+=,@-]) when OBJECT_STORAGE_KMS_ENABLED is true"
            )
        if self.enabled and not self.tenant_slug:
            raise KmsConfigError("OBJECT_STORAGE_KMS_TENANT must not be empty in KMS mode")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ObjectStorageKmsSettings:
        """Read ``OBJECT_STORAGE_KMS_*``; enabled-but-invalid raises ``KmsConfigError``."""
        env = os.environ if environ is None else environ
        return cls(
            enabled=env.get("OBJECT_STORAGE_KMS_ENABLED", "").strip().lower() in _TRUE,
            key_id=env.get("OBJECT_STORAGE_KMS_KEY_ID", "").strip(),
            tenant_slug=env.get("OBJECT_STORAGE_KMS_TENANT", "system").strip(),
        )


class ObjectStorageSse:
    """Decides the SSE parameters for each hub-api object write (see module docstring)."""

    #: How long a positive entitlement answer is reused. Bounds licence-server load on upload
    #: bursts while a lapse still takes effect within a minute. Negative answers are never cached.
    ENTITLEMENT_TTL_S = 60.0

    def __init__(
        self,
        settings: ObjectStorageKmsSettings,
        gate: ExternalKmsEntitlement,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the settings and the entitlement gate; `clock` is injectable for tests."""
        self._settings = settings
        self._gate = gate
        self._clock = clock
        self._entitled_until = 0.0

    @property
    def kms_mode(self) -> bool:
        """True when new writes must be SSE-KMS."""
        return self._settings.enabled

    async def _require_entitlement(self) -> None:
        """Raise unless the subject tenant holds ``compliance.external_kms`` (cached 60s)."""
        now = self._clock()
        if now < self._entitled_until:
            return
        if not await self._gate.is_entitled(
            self._settings.tenant_slug, request_host=current_request_host()
        ):
            self._entitled_until = 0.0
            logger.error(
                "object_storage.kms.not_entitled",
                extra={"feature": "compliance.external_kms", "alert": "object-storage-kms"},
            )
            raise ExternalKmsNotEntitledError(
                "object-storage SSE-KMS requires the Enterprise compliance.external_kms "
                "entitlement; refusing to write under the platform key instead"
            )
        self._entitled_until = now + self.ENTITLEMENT_TTL_S

    async def put_params(self) -> dict[str, str]:
        """SSE keyword arguments for one ``put_object`` (baseline, or entitled SSE-KMS).

        Raises:
            ExternalKmsNotEntitledError: KMS mode is on but the entitlement is not held.
        """
        if not self._settings.enabled:
            return {"ServerSideEncryption": SSE_BASELINE}
        await self._require_entitlement()
        return {"ServerSideEncryption": SSE_KMS, "SSEKMSKeyId": self._settings.key_id}

    async def startup_check(self) -> None:
        """Log the effective posture once at boot; a KMS-mode/entitlement mismatch is an ERROR.

        Never raises and never blocks startup: an unentitled KMS mode must not take the control
        plane down, but it must be impossible to miss -- hub-api's own writes will be refused.
        """
        if not self._settings.enabled:
            logger.info("object_storage.sse.baseline", extra={"sse": SSE_BASELINE})
            return
        try:
            await self._require_entitlement()
        except ExternalKmsNotEntitledError:
            return  # already logged at ERROR with the alert marker
        logger.info("object_storage.sse.kms_enabled", extra={"sse": SSE_KMS})


_instance: ObjectStorageSse | None = None


def get_object_storage_sse() -> ObjectStorageSse:
    """Return the process-wide :class:`ObjectStorageSse`, built from the environment on first use.

    A misconfigured KMS mode raises ``KmsConfigError`` here -- loudly, on the first write (or at
    startup via :meth:`ObjectStorageSse.startup_check`'s caller) -- never a quiet baseline write.
    """
    global _instance
    if _instance is None:
        _instance = ObjectStorageSse(ObjectStorageKmsSettings.from_env(), ExternalKmsGate())
    return _instance


def reset_object_storage_sse(instance: ObjectStorageSse | None = None) -> None:
    """Replace (or clear) the process-wide instance -- for tests and settings reloads."""
    global _instance
    _instance = instance
