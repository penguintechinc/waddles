"""Publisher-key registration + prebuilt attestation: crypto-focused, no DB."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services.errors import ApiError
from services.marketplace_publisher_attestation import (
    AttestationError,
    build_attestation_payload,
    fingerprint_publisher_key,
    validate_publisher_key,
    verify_prebuilt_attestation,
)
from services.marketplace_source_scanner import BundleCandidate
from services.marketplace_source_service import (
    BuildMode,
    SourceRecord,
    SourceStatus,
    _validate_create,
)

WASM = b"\x00asm\x01\x00\x00\x00fake-component"
APP = "src-abcd-hello"


def _b64pub(k: Ed25519PrivateKey) -> str:
    raw = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def _source(key: str | None, mode: BuildMode = BuildMode.PREBUILT) -> SourceRecord:
    now = datetime.now(UTC)
    return SourceRecord(
        public_id=uuid.uuid4(),
        name="s",
        is_builtin=False,
        git_url="https://x/y",
        ref="main",
        pinned_commit_sha=None,
        pending_commit_sha=None,
        build_mode=mode,
        publisher_key_fingerprint=None,
        publisher_public_key=key,
        sandbox_ack_by=None,
        sandbox_ack_at=None,
        sandbox_ack_text_hash=None,
        sandbox_ack_commit_sha=None,
        status=SourceStatus.ACTIVE,
        last_scanned_at=None,
        last_scan_error=None,
        created_by=None,
        created_at=now,
        updated_at=now,
    )


def _sign(k: Ed25519PrivateKey, *, app: str = APP, ver: str = "1.0.0", wasm: bytes = WASM) -> str:
    d = hashlib.sha256(wasm).hexdigest()
    return base64.b64encode(
        k.sign(build_attestation_payload(app_id=app, version=ver, digest=d))
    ).decode()


def _cand(sig: str | None, digest: str | None, app: str = APP, ver: str = "1.0.0") -> Any:
    return BundleCandidate(
        language="python",
        dir_name="hello",
        assigned_app_id=app,
        version=ver,
        manifest=None,
        wasm_path=Path("x.wasm"),
        sidecar_digest=digest,
        publisher_sig=sig,
        permission_summary={},
    )


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _good(key: Ed25519PrivateKey) -> tuple[Any, SourceRecord]:
    return _cand(_sign(key), hashlib.sha256(WASM).hexdigest()), _source(_b64pub(key))


def test_valid_attestation_accepted(key: Ed25519PrivateKey) -> None:
    cand, src = _good(key)
    v = verify_prebuilt_attestation(cand, src, WASM)
    assert v.app_id == APP and v.digest == hashlib.sha256(WASM).hexdigest()
    assert v.key_fingerprint == fingerprint_publisher_key(_b64pub(key))


def test_sha256_prefixed_sidecar_accepted(key: Ed25519PrivateKey) -> None:
    _, src = _good(key)
    cand = _cand(_sign(key), "sha256:" + hashlib.sha256(WASM).hexdigest().upper())
    verify_prebuilt_attestation(cand, src, WASM)


def _reason(cand: Any, src: SourceRecord, wasm: bytes = WASM) -> str:
    with pytest.raises(AttestationError) as ei:
        verify_prebuilt_attestation(cand, src, wasm)
    return ei.value.reason


def test_wrong_publisher_key_rejected(key: Ed25519PrivateKey) -> None:
    cand, _ = _good(key)
    assert _reason(cand, _source(_b64pub(Ed25519PrivateKey.generate()))) == "bad_signature"


def test_missing_signature_rejected(key: Ed25519PrivateKey) -> None:
    _, src = _good(key)
    assert _reason(_cand(None, hashlib.sha256(WASM).hexdigest()), src) == "missing_signature"


def test_missing_digest_rejected(key: Ed25519PrivateKey) -> None:
    _, src = _good(key)
    assert _reason(_cand(_sign(key), None), src) == "missing_digest"


def test_tampered_artifact_rejected(key: Ed25519PrivateKey) -> None:
    cand, src = _good(key)
    assert _reason(cand, src, WASM + b"x") == "digest_mismatch"


def test_tampered_digest_with_matching_artifact_rejected(key: Ed25519PrivateKey) -> None:
    # Attacker swaps wasm AND sidecar consistently: signature no longer matches.
    evil = WASM + b"evil"
    src = _source(_b64pub(key))
    cand = _cand(_sign(key), hashlib.sha256(evil).hexdigest())
    assert _reason(cand, src, evil) == "bad_signature"


def test_malformed_digest_rejected(key: Ed25519PrivateKey) -> None:
    _, src = _good(key)
    assert _reason(_cand(_sign(key), "nothex"), src) == "digest_malformed"


@pytest.mark.parametrize(("app", "ver"), [("src-ffff-hello", "1.0.0"), (APP, "1.0.1")])
def test_signature_bound_to_app_id_and_version(key: Ed25519PrivateKey, app: str, ver: str) -> None:
    _, src = _good(key)
    cand = _cand(_sign(key), hashlib.sha256(WASM).hexdigest(), app=app, ver=ver)
    assert _reason(cand, src) == "bad_signature"


def test_replay_across_sources_rejected(key: Ed25519PrivateKey) -> None:
    # Same publisher key signed for source A's assigned id; source B assigns a different id.
    sig = _sign(key, app="src-aaaa-hello")
    cand = _cand(sig, hashlib.sha256(WASM).hexdigest(), app="src-bbbb-hello")
    assert _reason(cand, _source(_b64pub(key))) == "bad_signature"


def test_keyless_source_rejected(key: Ed25519PrivateKey) -> None:
    cand, _ = _good(key)
    assert _reason(cand, _source(None)) == "no_publisher_key"


def test_non_prebuilt_source_rejected(key: Ed25519PrivateKey) -> None:
    cand, _ = _good(key)
    assert _reason(cand, _source(_b64pub(key), BuildMode.SANDBOX)) == "source_not_prebuilt"


def test_garbage_signature_rejected(key: Ed25519PrivateKey) -> None:
    _, src = _good(key)
    d = hashlib.sha256(WASM).hexdigest()
    assert _reason(_cand("!!notb64", d), src) == "malformed_input"
    assert _reason(_cand(base64.b64encode(b"short").decode(), d), src) == "malformed_input"


@pytest.mark.parametrize("bad", ["", "not base64!!", base64.b64encode(b"x" * 31).decode()])
def test_invalid_key_registration_rejected(bad: str) -> None:
    with pytest.raises(ApiError) as ei:
        validate_publisher_key(bad)
    assert ei.value.status_code == 400


def test_valid_key_registration_returns_fingerprint(key: Ed25519PrivateKey) -> None:
    pub = _b64pub(key)
    assert validate_publisher_key(pub) == fingerprint_publisher_key(pub)
    assert len(fingerprint_publisher_key(pub)) == 64


def _create(mode: BuildMode, pk: str | None, fp: str | None) -> None:
    _validate_create(
        name="n",
        git_url="https://github.com/a/b",
        ref="main",
        build_mode=mode,
        publisher_key_fingerprint=fp,
        publisher_public_key=pk,
        sandbox_ack=None,
    )


def test_prebuilt_create_requires_matching_valid_key(key: Ed25519PrivateKey) -> None:
    pub = _b64pub(key)
    _create(BuildMode.PREBUILT, pub, fingerprint_publisher_key(pub))
    for pk, fp in [(None, None), ("junk", "fp"), (pub, "0" * 64)]:
        with pytest.raises(ApiError):
            _create(BuildMode.PREBUILT, pk, fp)


def test_sandbox_source_needs_no_publisher_key(key: Ed25519PrivateKey) -> None:
    # Reaches the sandbox-ack check (not a key error): key absent is fine, key present is not.
    with pytest.raises(ApiError, match="trust acknowledgement"):
        _create(BuildMode.SANDBOX, None, None)
    with pytest.raises(ApiError, match="only prebuilt"):
        _create(BuildMode.SANDBOX, _b64pub(key), "x")
