"""Platform Ed25519 artifact signing at approval time (spec SS5.6, Gemini review condition 9).

`bundle_approval_service._write_approval_and_activate()` calls
`sign_and_record_version()` in the SAME transaction that inserts the
`app_install_approvals` row it signs -- a signing failure (no key
configured, or a data-integrity gap where the version has no digest yet)
rolls the whole approval back rather than leaving an approved-but-unsigned
version. `approve_version()` then calls `upload_signed_sidecar()` AFTER
that transaction commits, overwriting the bucket's `.json` sidecar object
with the signed document `core/bundle_executor/src/signing.rs` verifies
before instantiating any component -- see that module's own doc for why
the sidecar (not the wire protocol's `LoadBody`, which this repo does not
own) is the transport for these fields.

**Signing key rotation.** hub-api signs with exactly ONE active key at a
time (`BUNDLE_SIGNING_PRIVATE_KEY`/`BUNDLE_SIGNING_KEY_ID`, env only, never
logged/hardcoded) -- the executor verifies against a SET of configured
public keys (`BUNDLE_SIGNING_PUBLIC_KEYS`), so rotation is: add the new
public key to the executor's set (old key still verifies), switch hub-api
to sign with the new key, re-sign any still-relevant older approvals via
`hub_api/cli/sign_approved_bundles.py`, then eventually drop the old
public key from the executor's set once nothing depends on it.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import Table
from sqlalchemy import update as sa_update
from sqlalchemy.future import select

from services import storage_service
from services.errors import ApiError

logger = logging.getLogger(__name__)

_PRIVATE_KEY_ENV = "BUNDLE_SIGNING_PRIVATE_KEY"
_KEY_ID_ENV = "BUNDLE_SIGNING_KEY_ID"

_ALGORITHM = "ed25519"

#: Prefixes every signed payload -- MUST match `core/bundle_executor/src/
#: signing.rs`'s `DOMAIN_SEPARATOR` byte-for-byte. The `-v1` suffix means a
#: future format change is a new, distinguishable domain rather than a
#: silent reinterpretation of old bytes.
_DOMAIN_SEPARATOR = b"waddles-bundle-sig-v1"

#: `app_id`/`version` charset (task instruction) -- matches the Rust side's
#: `validate_id_charset`. Digest is deliberately NOT charset-checked here,
#: same rationale as the Rust side's own doc comment on this point.
_ID_CHARSET_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_id_charset(field_name: str, value: str) -> None:
    if not _ID_CHARSET_RE.match(value):
        raise ApiError(
            f"{field_name} {value!r} contains characters outside [A-Za-z0-9._-]",
            500,
            "artifact_signing_invalid_charset",
        )


def _length_prefixed(field: bytes) -> bytes:
    return struct.pack(">I", len(field)) + field


def build_signing_payload(*, app_id: str, version: str, digest: str, approval_id: int) -> bytes:
    r"""The exact byte payload Ed25519-signs.

    MUST match `core/bundle_executor/src/signing.rs`'s `signing_payload()`
    byte-for-byte, or every signature fails to verify.

    Length-prefixed, domain-separated encoding, not canonical JSON or
    NUL-delimited concatenation (an earlier revision used the latter --
    NUL-delimited fields cannot distinguish `("a\0b", "c")` from `("a",
    "b\0c")`, a canonicalization ambiguity a length-prefixed encoding does
    not have): `_DOMAIN_SEPARATOR + u32be(len(app_id)) + app_id +
    u32be(len(version)) + version + u32be(len(digest)) + digest +
    u64be(approval_id)`. Including `app_id`/`version`/`digest`/
    `approval_id` together (task instruction) is what prevents swapping: a
    signature made for one `(app_id, version, digest, approval_id)` tuple
    never verifies against a different one.
    """
    _validate_id_charset("app_id", app_id)
    _validate_id_charset("version", version)
    return (
        _DOMAIN_SEPARATOR
        + _length_prefixed(app_id.encode("utf-8"))
        + _length_prefixed(version.encode("utf-8"))
        + _length_prefixed(digest.encode("utf-8"))
        + struct.pack(">Q", approval_id)
    )


@dataclass(slots=True, frozen=True)
class PlatformSigner:
    """Holds the one currently-active platform Ed25519 signing key + its id.

    Never logs or otherwise exposes the private key material -- only
    `key_id` (a label, not a secret) ever reaches a log line.
    """

    key_id: str
    _private_key: Ed25519PrivateKey  # type annotation only, no secret value -- gitleaks:allow

    @classmethod
    def from_env(cls) -> PlatformSigner:
        """Loads the active signer from `BUNDLE_SIGNING_PRIVATE_KEY`/`BUNDLE_SIGNING_KEY_ID`.

        Fails closed (`ApiError` 500) if either is unset, non-base64, the
        wrong length, or not a valid Ed25519 seed -- spec SS5.6 has no
        supported "unsigned" mode for a real approval, matching
        `core/bundle_executor/src/signing.rs::PlatformPublicKeys::
        from_cli_required`'s own fail-closed posture on the verification
        side.
        """
        raw_key_b64 = os.environ.get(_PRIVATE_KEY_ENV)
        key_id = os.environ.get(_KEY_ID_ENV)
        if not raw_key_b64 or not key_id:
            raise ApiError(
                f"{_PRIVATE_KEY_ENV} and {_KEY_ID_ENV} must both be set -- artifact signing has "
                "no supported disabled mode (spec SS5.6)",
                500,
                "artifact_signing_key_unavailable",
            )
        try:
            raw_key = base64.b64decode(raw_key_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ApiError(
                f"{_PRIVATE_KEY_ENV} is not valid base64", 500, "artifact_signing_key_invalid"
            ) from exc
        if len(raw_key) != 32:
            raise ApiError(
                f"{_PRIVATE_KEY_ENV} must decode to exactly 32 bytes, got {len(raw_key)}",
                500,
                "artifact_signing_key_invalid",
            )
        try:
            private_key = Ed25519PrivateKey.from_private_bytes(raw_key)
        except (InvalidKey, ValueError) as exc:  # pragma: no cover - defensive
            # Every exactly-32-byte value is a well-formed Ed25519 seed --
            # this branch is unreachable in practice (the length check
            # above is what actually guards this constructor), kept only
            # in case a future `cryptography` version adds its own
            # validation this function should still fail closed on.
            raise ApiError(
                f"{_PRIVATE_KEY_ENV} is not a valid Ed25519 private key seed",
                500,
                "artifact_signing_key_invalid",
            ) from exc
        return cls(key_id=key_id, _private_key=private_key)

    def sign(self, *, app_id: str, version: str, digest: str, approval_id: int) -> tuple[str, str]:
        """Returns `(base64_signature, key_id)` over `build_signing_payload(...)`."""
        payload = build_signing_payload(
            app_id=app_id, version=version, digest=digest, approval_id=approval_id
        )
        signature = self._private_key.sign(payload)
        return base64.b64encode(signature).decode("ascii"), self.key_id


async def sign_and_record_version(
    conn: Any,
    *,
    app_versions_table: Table,
    version_id: int,
    app_id: str,
    version: str,
    approval_id: int,
) -> dict[str, Any]:
    """Signs `(app_id, version, digest, approval_id)` and UPDATEs `app_versions` in the SAME tx.

    `conn` is the SQLAlchemy `AsyncConnection` already inside the caller's
    `engine.begin()` block -- typed `Any` (matching `app_source_binding_
    service.sync_bindings()`'s own precedent) rather than `AsyncConnection`
    because SQLAlchemy's async `Connection.execute()` overloads otherwise
    make mypy see a `Coroutine | CursorResult` union on the `await`.

    Called from `bundle_approval_service._write_approval_and_activate()`,
    inside its own `engine.begin()` block, right after the
    `app_install_approvals` insert that produced `approval_id` -- a
    signing failure here (missing key, or a data-integrity gap where the
    version has no digest yet) raises and the whole transaction rolls
    back, so a committed approval is never left unsigned.

    Returns the exact fields `bundle_approval_service.approve_version()`
    passes to `upload_signed_sidecar()` post-commit: `app_id`, `version`,
    `digest`, `approval_id`, `key_id`, `signature`.
    """
    row = (
        await conn.execute(
            select(app_versions_table.c.artifact_digest).where(
                app_versions_table.c.id == version_id
            )
        )
    ).first()
    if row is None or row.artifact_digest is None:
        # Defensive only: `approve_version()` already refuses (500,
        # missing_app_version) a PUBLISHED upload with no app_version_id
        # before this point is ever reached, and the prebuilt-publish path
        # always sets `artifact_digest` in the same insert that creates
        # this row -- a version with no digest here is a data-integrity
        # bug, never a legitimate caller state.
        raise ApiError(
            f"app_versions.id={version_id} has no artifact_digest to sign",
            500,
            "missing_digest_for_signing",
        )
    digest = row.artifact_digest

    signer = PlatformSigner.from_env()
    signature_b64, key_id = signer.sign(
        app_id=app_id, version=version, digest=digest, approval_id=approval_id
    )

    now = datetime.now(UTC)
    await conn.execute(
        sa_update(app_versions_table)
        .where(app_versions_table.c.id == version_id)
        .values(
            artifact_signature=signature_b64,
            artifact_signature_key_id=key_id,
            artifact_signed_approval_id=approval_id,
            artifact_signed_at=now,
        )
    )
    logger.info(
        "bundle signing: version signed",
        extra={
            "app_id": app_id,
            "version": version,
            "approval_id": approval_id,
            "key_id": key_id,
        },
    )
    return {
        "app_id": app_id,
        "version": version,
        "digest": digest,
        "approval_id": approval_id,
        "key_id": key_id,
        "signature": signature_b64,
    }


async def upload_signed_sidecar(
    *, app_id: str, version: str, digest: str, approval_id: int, key_id: str, signature: str
) -> str:
    """Overwrites the bucket sidecar with the signed document the executor verifies.

    Called AFTER `sign_and_record_version()`'s transaction commits (same
    post-commit convention `bundle_approval_service.approve_version()`'s
    own Valkey provisioning step already uses) -- a failure here is
    surfaced to the caller, never swallowed: an executor cannot load an
    artifact whose sidecar was never successfully overwritten with a real
    signature. The DB-side signature/approval already committed by this
    point; `hub_api/cli/sign_approved_bundles.py` re-runs this exact write
    idempotently for any row whose sidecar upload is suspected stale.
    """
    sha256_hex = digest.removeprefix("sha256:")
    document = {
        "app_id": app_id,
        "version": version,
        "digest": digest,
        "approval_id": approval_id,
        "key_id": key_id,
        "algorithm": _ALGORITHM,
        "signature": signature,
    }
    key = await storage_service.write_bundle_sidecar(app_id, version, sha256_hex, document)
    logger.info(
        "bundle signing: signed sidecar uploaded",
        extra={"app_id": app_id, "version": version, "key": key, "key_id": key_id},
    )
    return key
