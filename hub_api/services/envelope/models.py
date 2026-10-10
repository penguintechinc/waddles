"""Value objects for per-tenant envelope encryption.

Every type is a frozen, slotted dataclass (immutable, typo-proof, small).
Key-bearing fields (`DekRecord.wrapped_dek`, `EncryptedField.ciphertext`)
are excluded from ``repr`` so an accidental ``log.debug(record)`` can never
write key material -- or even ciphertext -- into a log line.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime

from services.envelope.errors import EnvelopeInputError

#: Self-describing prefix of the single-column wire form, so envelope
#: values coexist with the legacy `base64(iv||ct||tag)` columns (std
#: base64 never contains ".") and a reader can pick the right path.
WIRE_PREFIX = "wenv1"

#: AES-GCM nonce length and tag length, bytes.
IV_LENGTH = 12
TAG_LENGTH = 16

#: `keystore.tenant_encryption_keys.kek_kind` values (design Sec4).
KEK_KIND_PLATFORM = "platform"
KEK_KIND_CUSTOMER = "customer_kms"

#: `keystore.tenant_encryption_keys.status` values.
KEY_ACTIVE = "active"
KEY_RETIRED = "retired"
KEY_DESTROYED = "destroyed"

#: `tenant_kms_configs.status` values.
CONFIG_PENDING = "pending"
CONFIG_ACTIVE = "active"
CONFIG_REVOKED = "revoked"


@dataclass(slots=True, frozen=True)
class EncryptedField:
    """One encrypted value: ciphertext+tag, its nonce, and the DEK version that sealed it."""

    dek_version: int
    iv: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)

    def to_wire(self) -> str:
        """Serialize to the single-TEXT-column form ``wenv1.<version>.<urlsafe-b64(iv||ct)>``."""
        blob = base64.urlsafe_b64encode(self.iv + self.ciphertext).decode("ascii")
        return f"{WIRE_PREFIX}.{self.dek_version}.{blob}"

    @classmethod
    def from_wire(cls, value: str) -> EncryptedField:
        """Parse :meth:`to_wire` output, rejecting anything malformed.

        Raises:
            EnvelopeInputError: wrong prefix, bad version, bad base64, or
                a body too short to hold a nonce plus a GCM tag.
        """
        parts = value.split(".", 2)
        if len(parts) != 3 or parts[0] != WIRE_PREFIX:
            raise EnvelopeInputError("not an envelope-encrypted value")
        try:
            version = int(parts[1])
            raw = base64.urlsafe_b64decode(parts[2].encode("ascii"))
        except (ValueError, binascii.Error, UnicodeEncodeError) as exc:
            raise EnvelopeInputError("malformed envelope-encrypted value") from exc
        if version < 1 or len(raw) < IV_LENGTH + TAG_LENGTH:
            raise EnvelopeInputError("malformed envelope-encrypted value")
        return cls(dek_version=version, iv=raw[:IV_LENGTH], ciphertext=raw[IV_LENGTH:])


def is_envelope_wire(value: str) -> bool:
    """Return whether `value` carries the envelope prefix (vs a legacy single-key blob)."""
    return value.startswith(f"{WIRE_PREFIX}.")


@dataclass(slots=True, frozen=True)
class DekRecord:
    """One `keystore.tenant_encryption_keys` row. Never holds an unwrapped DEK."""

    id: int
    tenant_id: int
    dek_version: int
    wrapped_dek: bytes | None = field(repr=False)
    kek_kind: str
    kek_ref: str
    status: str
    usage_count: int
    activated_at: datetime | None
    retired_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class TenantKmsConfig:
    """One `tenant_kms_configs` row: where a tenant's customer-managed KEK lives.

    Holds no secret: `external_id` is the STS confused-deputy guard the
    customer pastes into their role's trust policy (not a credential), and
    the ARNs identify -- but never authenticate to -- the customer's KMS.
    """

    tenant_id: int
    provider: str
    key_ref: str
    region: str | None
    principal: str | None
    external_id: str
    status: str
    last_verified_at: datetime | None = None
    last_error_code: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class KmsKeyInfo:
    """Result of a successful provider preflight (`KmsAdapter.verify`)."""

    provider: str
    key_ref: str
    key_state: str


@dataclass(slots=True, frozen=True)
class KekTarget:
    """The KEK a (re-)wrap should land on: its kind and canonical reference."""

    kind: str
    key_ref: str


@dataclass(slots=True, frozen=True)
class RewrapReport:
    """Outcome of re-wrapping every DEK version of one tenant onto a target KEK.

    A row is counted exactly once: already on the target, re-wrapped, or
    failed. `failed_versions` rows are untouched and remain readable under
    their previous KEK, so a partial run is always safe to retry.
    """

    tenant_id: int
    target_kind: str
    target_ref: str
    total: int
    rewrapped: int
    already_current: int
    failed_versions: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        """True when every DEK version now sits on the target KEK."""
        return not self.failed_versions and self.rewrapped + self.already_current == self.total
