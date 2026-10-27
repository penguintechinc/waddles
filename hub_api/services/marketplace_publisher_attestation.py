"""Publisher-key registration + prebuilt-bundle attestation (marketplace sources, Task 4).

Defense-in-depth only: a PREBUILT source registers an Ed25519 publisher public key, and each
prebuilt bundle must carry a detached publisher signature over (assigned app_id, version, digest).
This is an ATTESTATION that gates approval eligibility; the executor still trusts only platform
keys, and the platform re-signs at approval (bundle_signing_service). Fail-closed throughout.

Signed bytes (Task 5 / publishers MUST match exactly)::

    b"waddles-publisher-attest-v1" + u32be(len(app_id)) + app_id
        + u32be(len(version)) + version + u32be(len(digest)) + digest

with `app_id` the platform-ASSIGNED `src-` id (anti-replay across sources/names), `digest` the
lowercase hex sha256 of the .wasm (no prefix). `component.sig` holds base64 of the 64-byte sig.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from services.errors import ApiError, bad_request

if TYPE_CHECKING:
    from services.marketplace_source_scanner import BundleCandidate
    from services.marketplace_source_service import SourceRecord

logger = logging.getLogger(__name__)

_DOMAIN = b"waddles-publisher-attest-v1"
_ED25519_KEY_LEN = 32
_ED25519_SIG_LEN = 64
_PREBUILT_MODE = 1  # BuildMode.PREBUILT (int literal avoids a circular import)


class AttestationError(Exception):
    """A prebuilt bundle failed attestation; `reason` is a stable machine-checkable code."""

    def __init__(self, reason: str, detail: str) -> None:
        """Store the machine `reason` and a human-readable `detail`."""
        self.reason = reason
        super().__init__(f"{reason}: {detail}")


@dataclass(slots=True, frozen=True)
class VerifiedPrebuilt:
    """A prebuilt bundle whose publisher attestation verified (eligible for approval)."""

    app_id: str
    version: str
    digest: str
    key_fingerprint: str


def _load_public_key(pubkey_b64: str) -> tuple[Ed25519PublicKey, bytes]:
    """Decode a base64 raw-32-byte Ed25519 public key; ValueError if malformed."""
    try:
        raw = base64.b64decode(pubkey_b64.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("publisher key is not valid base64") from exc
    if len(raw) != _ED25519_KEY_LEN:
        raise ValueError("publisher key must be a raw 32-byte Ed25519 public key")
    try:
        return Ed25519PublicKey.from_public_bytes(raw), raw
    except ValueError as exc:  # pragma: no cover - any 32 bytes load; defensive
        raise ValueError("publisher key is not a valid Ed25519 public key") from exc


def fingerprint_publisher_key(pubkey_b64: str) -> str:
    """Return the lowercase-hex sha256 of the raw key bytes; ValueError if the key is invalid."""
    _, raw = _load_public_key(pubkey_b64)
    return hashlib.sha256(raw).hexdigest()


def validate_publisher_key(pubkey_b64: str) -> str:
    """Validate a key for registration and return its fingerprint; 400 `ApiError` if invalid."""
    try:
        return fingerprint_publisher_key(pubkey_b64)
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


def _lp(field: bytes) -> bytes:
    return struct.pack(">I", len(field)) + field


def build_attestation_payload(*, app_id: str, version: str, digest: str) -> bytes:
    """Exact bytes a publisher signs (see module docstring); fields are length-prefixed."""
    return _DOMAIN + _lp(app_id.encode()) + _lp(version.encode()) + _lp(digest.encode())


def _normalize_digest(raw: str) -> str:
    d = raw.strip().lower().removeprefix("sha256:")
    if len(d) != 64 or any(c not in "0123456789abcdef" for c in d):
        raise AttestationError("digest_malformed", "sidecar digest is not a sha256 hex string")
    return d


def verify_prebuilt_attestation(
    candidate: BundleCandidate, source: SourceRecord, wasm_bytes: bytes
) -> VerifiedPrebuilt:
    """Verify the publisher signature of a prebuilt bundle; raise `AttestationError` otherwise."""
    if int(source.build_mode) != _PREBUILT_MODE:
        raise AttestationError("source_not_prebuilt", "publisher signatures only apply to prebuilt")
    if not source.publisher_public_key:
        raise AttestationError("no_publisher_key", "source has no registered publisher key")
    if not candidate.publisher_sig:
        raise AttestationError("missing_signature", "bundle carries no publisher signature")
    if not candidate.sidecar_digest:
        raise AttestationError("missing_digest", "bundle carries no digest sidecar")
    claimed = _normalize_digest(candidate.sidecar_digest)
    actual = hashlib.sha256(wasm_bytes).hexdigest()
    if claimed != actual:
        raise AttestationError("digest_mismatch", "artifact does not match the sidecar digest")
    try:
        key, raw = _load_public_key(source.publisher_public_key)
        sig = base64.b64decode(candidate.publisher_sig.strip(), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AttestationError(
            "malformed_input", "registered key or signature is malformed"
        ) from exc
    if len(sig) != _ED25519_SIG_LEN:
        raise AttestationError("malformed_input", "signature must be 64 bytes")
    payload = build_attestation_payload(
        app_id=candidate.assigned_app_id, version=candidate.version, digest=actual
    )
    try:
        key.verify(sig, payload)
    except InvalidSignature as exc:
        logger.warning(
            "publisher attestation rejected app_id=%s version=%s",
            candidate.assigned_app_id,
            candidate.version,
        )
        raise AttestationError("bad_signature", "signature does not verify") from exc
    logger.info("publisher attestation verified app_id=%s", candidate.assigned_app_id)
    return VerifiedPrebuilt(
        app_id=candidate.assigned_app_id,
        version=candidate.version,
        digest=actual,
        key_fingerprint=hashlib.sha256(raw).hexdigest(),
    )


__all__ = [
    "ApiError",
    "AttestationError",
    "VerifiedPrebuilt",
    "build_attestation_payload",
    "fingerprint_publisher_key",
    "validate_publisher_key",
    "verify_prebuilt_attestation",
]
