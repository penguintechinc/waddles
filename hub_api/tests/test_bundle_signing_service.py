"""Tests for `services/bundle_signing_service.py` (spec SS5.6, Gemini review condition 9).

Covers: a valid signature verifies against its own public key; a
tampered digest/app_id/version/approval_id no longer verifies (the
"prevent swapping" property); no signing key configured fails closed;
a malformed configured key fails closed; key rotation changes which
key id/keypair signs, and an old signature does not verify under a
newly-rotated-to key; `sign_and_record_version()` writes the expected
columns inside its transaction and refuses a version with no digest yet;
`upload_signed_sidecar()` writes the exact signed document contract
`core/bundle_executor/src/signing.rs::SignedSidecar` deserializes.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services import bundle_signing_service
from services.bundle_signing_service import (
    PlatformSigner,
    build_signing_payload,
    sign_and_record_version,
    upload_signed_sidecar,
)
from services.errors import ApiError

_APP_ID = "waddles.test.signing-app"
_VERSION = "1.0.0"
_DIGEST = "a" * 64
_APPROVAL_ID = 7


def test_build_signing_payload_distinguishes_every_field() -> None:
    base = build_signing_payload(app_id=_APP_ID, version=_VERSION, digest=_DIGEST, approval_id=1)
    assert base != build_signing_payload(
        app_id=_APP_ID, version=_VERSION, digest=_DIGEST, approval_id=2
    )
    assert base != build_signing_payload(
        app_id=_APP_ID, version="2.0.0", digest=_DIGEST, approval_id=1
    )
    assert base != build_signing_payload(
        app_id="waddles.test.a-different-app", version=_VERSION, digest=_DIGEST, approval_id=1
    )
    assert base != build_signing_payload(
        app_id=_APP_ID, version=_VERSION, digest="b" * 64, approval_id=1
    )
    assert base == build_signing_payload(
        app_id=_APP_ID, version=_VERSION, digest=_DIGEST, approval_id=1
    )


def test_build_signing_payload_matches_the_cross_language_golden_vector() -> None:
    """Byte-for-byte cross-check against the Rust side's own golden vector.

    MUST match `signing_payload_matches_the_cross_language_golden_vector` in
    `core/bundle_executor/src/signing.rs` -- every other test in both files
    only checks self-consistency within its own language; this is the one
    that catches a drift between the two encodings.
    """
    payload = build_signing_payload(
        app_id="waddles.core.example.ping",
        version="1.2.3",
        digest="sha256:deadbeef",
        approval_id=42,
    )
    expected = bytes.fromhex(
        "776164646c65732d62756e646c652d7369672d763100000019776164646c65"
        "732e636f72652e6578616d706c652e70696e6700000005312e322e330000000f7368613235363a6465616462"
        "656566000000000000002a"
    )
    assert payload == expected


@pytest.mark.parametrize("app_id", ["app id with spaces", "app/id", "app\x00id", ""])
def test_build_signing_payload_rejects_an_app_id_outside_the_allowed_charset(app_id: str) -> None:
    with pytest.raises(ApiError):
        build_signing_payload(app_id=app_id, version=_VERSION, digest=_DIGEST, approval_id=1)


@pytest.mark.parametrize("version", ["1.0/../etc", "1 0", ""])
def test_build_signing_payload_rejects_a_version_outside_the_allowed_charset(version: str) -> None:
    with pytest.raises(ApiError):
        build_signing_payload(app_id=_APP_ID, version=version, digest=_DIGEST, approval_id=1)


def test_platform_signer_from_env_fails_closed_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUNDLE_SIGNING_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("BUNDLE_SIGNING_KEY_ID", raising=False)
    with pytest.raises(ApiError) as exc:
        PlatformSigner.from_env()
    assert exc.value.code == "artifact_signing_key_unavailable"


def test_platform_signer_from_env_fails_closed_when_only_key_id_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BUNDLE_SIGNING_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("BUNDLE_SIGNING_KEY_ID", "k1")
    with pytest.raises(ApiError) as exc:
        PlatformSigner.from_env()
    assert exc.value.code == "artifact_signing_key_unavailable"


def test_platform_signer_from_env_fails_closed_on_non_base64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUNDLE_SIGNING_PRIVATE_KEY", "not-valid-base64!!")
    monkeypatch.setenv("BUNDLE_SIGNING_KEY_ID", "k1")
    with pytest.raises(ApiError) as exc:
        PlatformSigner.from_env()
    assert exc.value.code == "artifact_signing_key_invalid"


def test_platform_signer_from_env_fails_closed_on_wrong_length_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUNDLE_SIGNING_PRIVATE_KEY", base64.b64encode(b"short").decode("ascii"))
    monkeypatch.setenv("BUNDLE_SIGNING_KEY_ID", "k1")
    with pytest.raises(ApiError) as exc:
        PlatformSigner.from_env()
    assert exc.value.code == "artifact_signing_key_invalid"


def test_platform_signer_sign_produces_a_signature_verifiable_against_its_own_public_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = b"\x01" * 32
    monkeypatch.setenv("BUNDLE_SIGNING_PRIVATE_KEY", base64.b64encode(seed).decode("ascii"))
    monkeypatch.setenv("BUNDLE_SIGNING_KEY_ID", "platform-2026-09")

    signer = PlatformSigner.from_env()
    signature_b64, key_id = signer.sign(
        app_id=_APP_ID, version=_VERSION, digest=_DIGEST, approval_id=_APPROVAL_ID
    )
    assert key_id == "platform-2026-09"

    public_key = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    signature = base64.b64decode(signature_b64)
    payload = build_signing_payload(
        app_id=_APP_ID, version=_VERSION, digest=_DIGEST, approval_id=_APPROVAL_ID
    )
    public_key.verify(signature, payload)  # raises InvalidSignature on any mismatch


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(
            lambda p: build_signing_payload(
                app_id=p["app_id"],
                version=p["version"],
                digest="b" * 64,
                approval_id=p["approval_id"],
            ),
            id="tampered_digest",
        ),
        pytest.param(
            lambda p: build_signing_payload(
                app_id="waddles.test.a-different-app",
                version=p["version"],
                digest=p["digest"],
                approval_id=p["approval_id"],
            ),
            id="wrong_app_id",
        ),
        pytest.param(
            lambda p: build_signing_payload(
                app_id=p["app_id"],
                version="9.9.9",
                digest=p["digest"],
                approval_id=p["approval_id"],
            ),
            id="wrong_version",
        ),
        pytest.param(
            lambda p: build_signing_payload(
                app_id=p["app_id"], version=p["version"], digest=p["digest"], approval_id=999
            ),
            id="wrong_approval_id",
        ),
    ],
)
def test_a_tampered_payload_field_fails_verification(
    monkeypatch: pytest.MonkeyPatch, tamper: Any
) -> None:
    """Signing rejects any post-hoc field swap -- the "prevent swapping" property."""
    seed = b"\x02" * 32
    monkeypatch.setenv("BUNDLE_SIGNING_PRIVATE_KEY", base64.b64encode(seed).decode("ascii"))
    monkeypatch.setenv("BUNDLE_SIGNING_KEY_ID", "k1")
    signer = PlatformSigner.from_env()
    signature_b64, _ = signer.sign(
        app_id=_APP_ID, version=_VERSION, digest=_DIGEST, approval_id=_APPROVAL_ID
    )
    signature = base64.b64decode(signature_b64)
    public_key = Ed25519PrivateKey.from_private_bytes(seed).public_key()

    tampered_payload = tamper(
        {"app_id": _APP_ID, "version": _VERSION, "digest": _DIGEST, "approval_id": _APPROVAL_ID}
    )
    with pytest.raises(InvalidSignature):
        public_key.verify(signature, tampered_payload)


def test_key_rotation_old_signature_does_not_verify_under_the_new_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An "unknown key id" verifier failure mode.

    A signature made under the OLD key must not verify against a
    DIFFERENT (newly rotated-in) key's public half.
    """
    old_seed = b"\x03" * 32
    monkeypatch.setenv("BUNDLE_SIGNING_PRIVATE_KEY", base64.b64encode(old_seed).decode("ascii"))
    monkeypatch.setenv("BUNDLE_SIGNING_KEY_ID", "platform-2026-01")
    old_signer = PlatformSigner.from_env()
    old_signature_b64, old_key_id = old_signer.sign(
        app_id=_APP_ID, version=_VERSION, digest=_DIGEST, approval_id=_APPROVAL_ID
    )
    assert old_key_id == "platform-2026-01"

    new_seed = b"\x04" * 32
    monkeypatch.setenv("BUNDLE_SIGNING_PRIVATE_KEY", base64.b64encode(new_seed).decode("ascii"))
    monkeypatch.setenv("BUNDLE_SIGNING_KEY_ID", "platform-2026-09")
    new_signer = PlatformSigner.from_env()
    new_signature_b64, new_key_id = new_signer.sign(
        app_id=_APP_ID, version=_VERSION, digest=_DIGEST, approval_id=_APPROVAL_ID
    )
    assert new_key_id == "platform-2026-09"
    assert new_signature_b64 != old_signature_b64

    payload = build_signing_payload(
        app_id=_APP_ID, version=_VERSION, digest=_DIGEST, approval_id=_APPROVAL_ID
    )
    new_public_key = Ed25519PrivateKey.from_private_bytes(new_seed).public_key()
    # The OLD signature must not verify under the NEW key -- exactly the
    # rejection an executor sees if a sidecar's claimed `key_id` doesn't
    # match the key that actually produced the bytes.
    with pytest.raises(InvalidSignature):
        new_public_key.verify(base64.b64decode(old_signature_b64), payload)

    # But it verifies fine under its OWN (still-retained) old public key --
    # rotation adds a key, it does not retroactively invalidate the old one.
    old_public_key = Ed25519PrivateKey.from_private_bytes(old_seed).public_key()
    old_public_key.verify(base64.b64decode(old_signature_b64), payload)


async def test_sign_and_record_version_updates_the_row(install_dal: Any) -> None:
    version_id = await install_dal.app_versions.async_insert(
        app_id=_APP_ID,
        version=_VERSION,
        artifact_digest=_DIGEST,
        language="python",
        artifact_kind="prebuilt",
        scan_status="not_scanned",
    )
    versions_table = install_dal.metadata.tables["app_versions"]

    async with install_dal.engine.begin() as conn:
        result = await sign_and_record_version(
            conn,
            app_versions_table=versions_table,
            version_id=version_id,
            app_id=_APP_ID,
            version=_VERSION,
            approval_id=_APPROVAL_ID,
        )

    assert result["digest"] == _DIGEST
    assert result["approval_id"] == _APPROVAL_ID
    assert result["key_id"] == os.environ["BUNDLE_SIGNING_KEY_ID"]
    assert result["signature"]

    row = (await install_dal(install_dal.app_versions.id == version_id).select()).first()
    assert row.artifact_signature == result["signature"]
    assert row.artifact_signature_key_id == result["key_id"]
    assert row.artifact_signed_approval_id == _APPROVAL_ID
    assert row.artifact_signed_at is not None


async def test_sign_and_record_version_raises_when_digest_is_missing(install_dal: Any) -> None:
    version_id = await install_dal.app_versions.async_insert(
        app_id="waddles.test.no-digest-yet",
        version=_VERSION,
        artifact_digest=None,
        language="python",
        artifact_kind="source",
        scan_status="not_scanned",
    )
    versions_table = install_dal.metadata.tables["app_versions"]

    async with install_dal.engine.begin() as conn:
        with pytest.raises(ApiError) as exc:
            await sign_and_record_version(
                conn,
                app_versions_table=versions_table,
                version_id=version_id,
                app_id="waddles.test.no-digest-yet",
                version=_VERSION,
                approval_id=1,
            )
    assert exc.value.code == "missing_digest_for_signing"


async def test_sign_and_record_version_fails_closed_without_a_configured_key(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BUNDLE_SIGNING_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("BUNDLE_SIGNING_KEY_ID", raising=False)
    version_id = await install_dal.app_versions.async_insert(
        app_id=_APP_ID,
        version=_VERSION,
        artifact_digest=_DIGEST,
        language="python",
        artifact_kind="prebuilt",
        scan_status="not_scanned",
    )
    versions_table = install_dal.metadata.tables["app_versions"]

    async with install_dal.engine.begin() as conn:
        with pytest.raises(ApiError) as exc:
            await sign_and_record_version(
                conn,
                app_versions_table=versions_table,
                version_id=version_id,
                app_id=_APP_ID,
                version=_VERSION,
                approval_id=1,
            )
    assert exc.value.code == "artifact_signing_key_unavailable"


async def test_upload_signed_sidecar_writes_the_expected_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str, dict[str, Any]]] = []

    async def fake_write(
        app_id: str, version: str, sha256_hex: str, document: dict[str, Any]
    ) -> str:
        calls.append((app_id, version, sha256_hex, document))
        return "bundles/waddles.test.app/1/abc.json"

    monkeypatch.setattr(bundle_signing_service.storage_service, "write_bundle_sidecar", fake_write)

    key = await upload_signed_sidecar(
        app_id="waddles.test.app",
        version="1",
        digest="sha256:" + "c" * 64,
        approval_id=5,
        key_id="k1",
        signature="c2ln==",
    )
    assert key == "bundles/waddles.test.app/1/abc.json"
    assert calls == [
        (
            "waddles.test.app",
            "1",
            "c" * 64,
            {
                "app_id": "waddles.test.app",
                "version": "1",
                "digest": "sha256:" + "c" * 64,
                "approval_id": 5,
                "key_id": "k1",
                "algorithm": "ed25519",
                "signature": "c2ln==",
            },
        )
    ]
