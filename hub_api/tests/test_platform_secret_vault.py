"""Platform secret vault: seal/open under the platform KEK (platform_vault, migration 0109).

Covers the round trip, AAD binding (type / scope / version / public_id / kek_id / layout), the
no-legacy-path guarantee, previous-KEK rotation, tamper detection, value-free errors and logs,
key-material zeroisation, and the keyed MAC helpers (fingerprint, root marker, event chain). Every
negative test has a positive control in the same file, so a failing open is proven to be the
binding and not a broken fixture.
"""

from __future__ import annotations

import base64
import importlib.util
import logging
import traceback
import uuid
from pathlib import Path
from typing import Any

import pytest

from services.envelope.platform_kek import PlatformKekAdapter
from services.provider_connections import vault
from services.provider_connections.vault import (
    PLATFORM_ENVELOPE_MAGIC,
    PLATFORM_ENVELOPE_MIN_BYTES,
    PLATFORM_SEAL_V1,
    PlatformSecretCorruptError,
    PlatformSecretError,
    PlatformSecretInputError,
    PlatformSecretKekUnavailableError,
    SealedPlatformSecret,
    open_platform_secret,
    platform_event_hash,
    platform_root_mac,
    platform_secret_aad,
    platform_secret_fingerprint,
    seal_platform_secret,
    verify_platform_root_mac,
)

CURRENT = bytes.fromhex("a1" * 32)
PREVIOUS = bytes.fromhex("b2" * 32)
OTHER = bytes.fromhex("c3" * 32)
PUBLIC_ID = uuid.UUID("11111111-2222-4333-8444-555555555555")
OTHER_ID = uuid.UUID("99999999-2222-4333-8444-555555555555")
SECRET = b"sentinel-plaintext-\x00\x01\xfe\xff-do-not-leak-0123456789"
MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0109_platform_vault.py"

_ROW: dict[str, Any] = {"secret_type": 3, "scope_id": None, "version": 1, "public_id": PUBLIC_ID}


def _kek(current: bytes = CURRENT, previous: bytes | None = None) -> PlatformKekAdapter:
    """An adapter over test-only fixed KEK bytes (never real key material)."""
    return PlatformKekAdapter(current, previous)


def _seal(plaintext: bytes = SECRET, kek: PlatformKekAdapter | None = None, **row: Any) -> bytes:
    """Seal `plaintext` for the default row (override fields via `row`); return the envelope."""
    merged = {**_ROW, **row}
    return seal_platform_secret(plaintext, kek=kek or _kek(), **merged).sealed_value


def _open(envelope: bytes, kek: PlatformKekAdapter | None = None, **row: Any) -> bytes:
    """Open `envelope` for the default row (override fields via `row`)."""
    merged = {**_ROW, **row}
    return open_platform_secret(envelope, kek=kek or _kek(), **merged)


class TestRoundTrip:
    def test_seals_and_opens(self) -> None:
        sealed = seal_platform_secret(SECRET, kek=_kek(), **_ROW)
        assert isinstance(sealed, SealedPlatformSecret)
        assert _open(sealed.sealed_value) == SECRET

    @pytest.mark.parametrize("plaintext", [b"x", b"\x00" * 32, bytes(range(256)), b"p" * 4096])
    def test_any_binary_plaintext_round_trips(self, plaintext: bytes) -> None:
        assert _open(_seal(plaintext)) == plaintext

    def test_bytearray_plaintext_and_memoryview_envelope_are_accepted(self) -> None:
        envelope = seal_platform_secret(bytearray(SECRET), kek=_kek(), **_ROW).sealed_value
        assert _open(memoryview(envelope)) == SECRET  # type: ignore[arg-type]

    def test_envelope_layout_matches_the_database_check(self) -> None:
        sealed = seal_platform_secret(SECRET, kek=_kek(), **_ROW)
        blob = sealed.sealed_value
        assert blob[:3] == PLATFORM_ENVELOPE_MAGIC == b"WPS"
        assert sealed.seal_version == PLATFORM_SEAL_V1 == blob[3]
        assert sealed.kek_id == blob[4:12].hex() == _kek().kek_ids[0]
        assert len(blob) == 3 + 1 + 8 + 12 + len(SECRET) + 16
        assert len(blob) >= PLATFORM_ENVELOPE_MIN_BYTES
        assert SECRET not in blob

    def test_each_seal_uses_a_fresh_iv(self) -> None:
        first, second = _seal(), _seal()
        assert first != second
        assert first[12:24] != second[12:24]

    def test_migration_constants_agree_with_the_vault(self) -> None:
        spec = importlib.util.spec_from_file_location("migration_0109", MIGRATION)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.ENVELOPE_MIN_BYTES == PLATFORM_ENVELOPE_MIN_BYTES
        assert PLATFORM_ENVELOPE_MAGIC.hex() == "575053"
        assert "decode('575053', 'hex')" in module._SECRETS


class TestAadBinding:
    @pytest.mark.parametrize(
        "other",
        [
            {"secret_type": 4},
            {"scope_id": 0},  # the default tenant is NOT the global scope
            {"scope_id": 7},
            {"version": 2},
            {"public_id": OTHER_ID},
        ],
    )
    def test_any_other_row_identity_fails_to_open(self, other: dict[str, Any]) -> None:
        envelope = _seal()
        assert _open(envelope) == SECRET  # control: the right identity opens
        with pytest.raises(PlatformSecretCorruptError):
            _open(envelope, **other)

    def test_scope_bound_secret_does_not_open_as_global_or_for_another_scope(self) -> None:
        envelope = _seal(scope_id=5)
        assert _open(envelope, scope_id=5) == SECRET
        for scope in (None, 0, 4, 6, 55):
            with pytest.raises(PlatformSecretCorruptError):
                _open(envelope, scope_id=scope)

    def test_swapping_sealed_values_between_rows_fails_both_ways(self) -> None:
        a = _seal(b"secret-for-row-a", version=1)
        b = _seal(b"secret-for-row-b", version=2)
        assert _open(a, version=1) == b"secret-for-row-a"
        assert _open(b, version=2) == b"secret-for-row-b"
        with pytest.raises(PlatformSecretCorruptError):
            _open(a, version=2)
        with pytest.raises(PlatformSecretCorruptError):
            _open(b, version=1)

    def test_aad_is_canonical_and_unambiguous(self) -> None:
        kwargs = {"kek_id": _kek().kek_ids[0], "public_id": PUBLIC_ID}
        # (type=1, scope=23) must not collide with (type=12, scope=3): length-prefixed fields
        a = platform_secret_aad(secret_type=1, scope_id=23, version=4, **kwargs)
        b = platform_secret_aad(secret_type=12, scope_id=3, version=4, **kwargs)
        c = platform_secret_aad(secret_type=1, scope_id=2, version=34, **kwargs)
        assert len({a, b, c}) == 3
        assert platform_secret_aad(secret_type=1, scope_id=None, version=1, **kwargs) != (
            platform_secret_aad(secret_type=1, scope_id=0, version=1, **kwargs)
        )
        assert a == platform_secret_aad(secret_type=1, scope_id=23, version=4, **kwargs)

    def test_stored_column_cross_checks_must_match_the_authenticated_header(self) -> None:
        sealed = seal_platform_secret(SECRET, kek=_kek(), **_ROW)
        good = open_platform_secret(
            sealed.sealed_value,
            kek=_kek(),
            kek_id=sealed.kek_id,
            seal_version=sealed.seal_version,
            **_ROW,
        )
        assert good == SECRET
        with pytest.raises(PlatformSecretCorruptError):
            open_platform_secret(sealed.sealed_value, kek=_kek(), kek_id="0123456789abcdef", **_ROW)
        for wrong in (0, 2):
            with pytest.raises(PlatformSecretCorruptError):
                open_platform_secret(sealed.sealed_value, kek=_kek(), seal_version=wrong, **_ROW)


class TestNoLegacyPath:
    def test_a_credential_key_ciphertext_never_opens_as_a_platform_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)
        no_aad = base64.b64decode(vault.seal_secret("legacy-value"))
        with_aad = base64.b64decode(vault.seal_secret("legacy-value", aad=b"row"))
        assert vault.open_secret(base64.b64encode(no_aad).decode()) == "legacy-value"  # control
        for blob in (no_aad, with_aad):
            with pytest.raises(PlatformSecretCorruptError):
                _open(blob)
        # re-labelled with the platform magic: still never opened (fail closed either way)
        for blob in (b"WPS\x01" + no_aad, b"WPS\x00" + no_aad, b"WPS\x01" + with_aad):
            with pytest.raises(PlatformSecretError):
                _open(blob)

    @pytest.mark.parametrize("version_byte", [0, 2, 255])
    def test_seal_version_zero_or_unknown_is_rejected_even_when_otherwise_valid(
        self, version_byte: int
    ) -> None:
        envelope = bytearray(_seal())
        envelope[3] = version_byte
        with pytest.raises(PlatformSecretCorruptError):
            _open(bytes(envelope))
        with pytest.raises(PlatformSecretCorruptError):
            _open(bytes(envelope), seal_version=version_byte)

    def test_the_legacy_machinery_is_not_reachable_from_the_platform_functions(self) -> None:
        for fn in (seal_platform_secret, open_platform_secret):
            names = set(fn.__code__.co_names)
            assert not names & {"crypto", "open_secret", "seal_secret", "legacy_fallback_allowed"}


class TestKekRotation:
    def test_previous_kek_opens_old_data_and_new_data_seals_with_current(self) -> None:
        old = _seal(b"sealed-before-rotation", kek=_kek(PREVIOUS))
        rotated = _kek(CURRENT, PREVIOUS)
        assert _open(old, rotated) == b"sealed-before-rotation"  # previous KEK still opens it
        fresh = seal_platform_secret(b"sealed-after-rotation", kek=rotated, **_ROW)
        assert fresh.kek_id == rotated.kek_ids[0] != old[4:12].hex()
        assert _open(fresh.sealed_value, _kek(CURRENT)) == b"sealed-after-rotation"
        # the fresh value is under the CURRENT KEK only: a previous-only adapter cannot open it
        with pytest.raises(PlatformSecretKekUnavailableError):
            _open(fresh.sealed_value, _kek(PREVIOUS))

    def test_old_data_is_unavailable_once_previous_is_dropped(self) -> None:
        old = _seal(kek=_kek(PREVIOUS))
        with pytest.raises(PlatformSecretKekUnavailableError):
            _open(old, _kek(CURRENT))

    def test_a_different_root_is_unavailable_never_a_trial_decryption(self) -> None:
        envelope = _seal()
        assert _open(envelope) == SECRET
        with pytest.raises(PlatformSecretKekUnavailableError):
            _open(envelope, _kek(OTHER))

    def test_rewrap_by_open_then_seal_moves_a_row_onto_the_current_kek(self) -> None:
        old = _seal(kek=_kek(PREVIOUS))
        rotated = _kek(CURRENT, PREVIOUS)
        plaintext = _open(old, rotated)
        moved = seal_platform_secret(plaintext, kek=rotated, **_ROW)
        assert moved.kek_id == rotated.kek_ids[0]
        assert _open(moved.sealed_value, _kek(CURRENT)) == SECRET


class TestTamper:
    @pytest.mark.parametrize("index", [0, 1, 2, 3, 12, 13, 23, 24, 30, -1, -16, -17])
    def test_any_flipped_byte_fails(self, index: int) -> None:
        envelope = bytearray(_seal())
        assert _open(bytes(envelope)) == SECRET  # control
        envelope[index] ^= 0x01
        with pytest.raises(PlatformSecretCorruptError):
            _open(bytes(envelope))

    def test_a_flipped_header_kek_id_is_a_failure_either_way(self) -> None:
        envelope = bytearray(_seal())
        envelope[4] ^= 0x01
        with pytest.raises(PlatformSecretError):
            _open(bytes(envelope))

    @pytest.mark.parametrize("cut", [0, 1, 3, 4, 12, 24, 40, -1, -16])
    def test_truncation_fails(self, cut: int) -> None:
        envelope = _seal()
        with pytest.raises(PlatformSecretCorruptError):
            _open(envelope[:cut] if cut >= 0 else envelope[:cut])

    def test_extension_and_garbage_fail(self) -> None:
        envelope = _seal()
        for blob in (envelope + b"\x00", b"", b"WPS", b"\x00" * 64, b"x" * 200):
            with pytest.raises(PlatformSecretCorruptError):
                _open(blob)


class TestInputValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            {"secret_type": 0},
            {"secret_type": -1},
            {"secret_type": 32768},
            {"secret_type": True},
            {"secret_type": "3"},
            {"version": 0},
            {"version": 2**31},
            {"version": False},
            {"scope_id": -1},
            {"scope_id": True},
            {"scope_id": "1"},
            {"public_id": str(PUBLIC_ID)},
            {"public_id": None},
        ],
    )
    def test_malformed_identity_is_a_value_free_input_error(self, bad: dict[str, Any]) -> None:
        with pytest.raises(PlatformSecretInputError) as seal_exc:
            _seal(**bad)
        with pytest.raises(PlatformSecretInputError):
            _open(_seal(), **bad)
        assert str(bad[next(iter(bad))]) not in str(seal_exc.value) or str(
            bad[next(iter(bad))]
        ) in ("0", "1", "-1", "True", "False", "None")

    @pytest.mark.parametrize("plaintext", [b"", bytearray(), "text", None, 5])
    def test_empty_or_non_bytes_plaintext_is_rejected(self, plaintext: Any) -> None:
        with pytest.raises(PlatformSecretInputError):
            seal_platform_secret(plaintext, kek=_kek(), **_ROW)

    def test_a_missing_or_malformed_kek_surfaces_as_unavailable_not_a_crash(self) -> None:
        class _Broken:
            kek_ids = ("0123456789abcdef",)

            def derive_key(self, info: bytes, *, key_id: str | None = None) -> bytes:
                from services.envelope.errors import PlatformKekError

                raise PlatformKekError("platform KEK is not set")

        with pytest.raises(PlatformSecretKekUnavailableError):
            seal_platform_secret(SECRET, kek=_Broken(), **_ROW)  # type: ignore[arg-type]


def _no_secret_material(text: str, extra: tuple[bytes, ...] = ()) -> None:
    """Assert `text` carries no plaintext, KEK, derived key, ciphertext or id in any encoding."""
    kek = _kek()
    derived = kek.derive_key(vault._SEAL_INFO)
    needles = [SECRET, CURRENT, PREVIOUS, derived, *extra, SECRET[:12]]
    for needle in needles:
        for rendering in (
            needle.decode("latin-1"),
            needle.hex(),
            base64.b64encode(needle).decode(),
            repr(needle),
        ):
            assert rendering not in text, f"leaked a {len(needle)}-byte value as {rendering[:8]}..."
    assert str(PUBLIC_ID) not in text


class TestNoSecretBytesInErrorsOrLogs:
    def _failures(self) -> list[BaseException]:
        envelope = _seal()
        tampered = bytearray(envelope)
        tampered[-1] ^= 1
        trials: list[tuple[Any, ...]] = [
            (_open, bytes(tampered)),
            (_open, envelope, _kek(OTHER)),
            (_open, envelope[:20]),
            (_open, envelope, None, {"version": 9}),
            (_seal, b""),
        ]
        errors: list[BaseException] = []
        for fn, *args in trials:
            extra = args[2] if len(args) > 2 else {}
            try:
                fn(*args[:2], **extra)
            except PlatformSecretError as exc:
                errors.append(exc)
        return errors

    def test_exception_text_repr_and_traceback_are_value_free(self) -> None:
        errors = self._failures()
        assert len(errors) == 5, "denominator: every trial raised"
        envelope = _seal()
        for exc in errors:
            rendered = "\n".join([str(exc), repr(exc), *traceback.format_exception(exc)])
            _no_secret_material(rendered, (envelope, envelope[24:]))
            assert exc.__cause__ is None or isinstance(exc.__cause__, Exception)

    def test_failure_messages_are_fixed_strings(self) -> None:
        messages = {str(e) for e in self._failures()}
        assert messages <= {
            "platform secret failed authentication",
            "the platform KEK is unavailable",
            "plaintext must be non-empty bytes",
        }, messages

    def test_no_secret_value_reaches_the_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="services.provider_connections.vault")
        envelope = _seal()
        _open(envelope)
        self._failures()
        fingerprint = platform_secret_fingerprint(SECRET, kek=_kek())
        mac = platform_root_mac(_kek())
        chain = platform_event_hash(None, b"canonical", kek=_kek())
        assert len(caplog.records) >= 7, "denominator: seal, open and failure records captured"
        text = "\n".join(r.getMessage() for r in caplog.records) + "\n" + caplog.text
        _no_secret_material(text, (envelope, envelope[24:], fingerprint, mac, chain))
        # only ints and the non-secret key id are logged
        assert any("kek_id=" in r.getMessage() for r in caplog.records)
        assert all(r.name == "services.provider_connections.vault" for r in caplog.records)


class TestKeyMaterialZeroisation:
    def _spy(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[bytearray, bytes]]:
        captured: list[tuple[bytearray, bytes]] = []
        real = vault.AESGCM

        def spy(key: bytearray) -> Any:
            captured.append((key, bytes(key)))
            return real(bytes(key))

        monkeypatch.setattr(vault, "AESGCM", spy)
        return captured

    def test_the_aead_key_copy_is_zeroed_after_seal_and_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._spy(monkeypatch)
        envelope = _seal()
        _open(envelope)
        assert len(captured) == 2
        expected = _kek().derive_key(vault._SEAL_INFO)
        for live, snapshot in captured:
            assert snapshot == expected, "the spy saw the real derived key (not a vacuous check)"
            assert len(live) == 32 and not any(live), "key copy was not zeroed"

    def test_the_key_copy_is_zeroed_even_when_authentication_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._spy(monkeypatch)
        envelope = bytearray(_seal())
        envelope[-1] ^= 1
        with pytest.raises(PlatformSecretCorruptError):
            _open(bytes(envelope))
        assert len(captured) == 2  # one seal + the failed open
        assert all(not any(live) for live, _ in captured)


class TestKeyedMacs:
    def test_fingerprint_is_keyed_deterministic_and_not_a_bare_hash(self) -> None:
        import hashlib

        one = platform_secret_fingerprint(SECRET, kek=_kek())
        assert one == platform_secret_fingerprint(SECRET, kek=_kek())
        assert len(one) == 32
        assert one != hashlib.sha256(SECRET).digest()
        assert one != platform_secret_fingerprint(SECRET, kek=_kek(OTHER))
        assert one != platform_secret_fingerprint(SECRET + b"x", kek=_kek())

    def test_fingerprint_can_target_a_specific_loaded_kek(self) -> None:
        rotated = _kek(CURRENT, PREVIOUS)
        previous_id = _kek(PREVIOUS).kek_ids[0]
        assert platform_secret_fingerprint(SECRET, kek=rotated, kek_id=previous_id) == (
            platform_secret_fingerprint(SECRET, kek=_kek(PREVIOUS))
        )
        with pytest.raises(PlatformSecretKekUnavailableError):
            platform_secret_fingerprint(SECRET, kek=_kek(), kek_id=previous_id)
        with pytest.raises(PlatformSecretInputError):
            platform_secret_fingerprint(b"", kek=_kek())

    def test_root_mac_verifies_only_under_the_marking_kek(self) -> None:
        kek = _kek()
        marker_id = kek.kek_ids[0]
        mac = platform_root_mac(kek)
        assert verify_platform_root_mac(mac, kek=kek, kek_id=marker_id) is True
        bad = bytearray(mac)
        bad[0] ^= 1
        assert verify_platform_root_mac(bytes(bad), kek=kek, kek_id=marker_id) is False
        assert verify_platform_root_mac(mac[:-1], kek=kek, kek_id=marker_id) is False
        # a different root cannot even locate the marker's key: unavailable, not "ok"
        with pytest.raises(PlatformSecretKekUnavailableError):
            verify_platform_root_mac(mac, kek=_kek(OTHER), kek_id=marker_id)
        with pytest.raises(PlatformSecretInputError):
            verify_platform_root_mac(mac, kek=kek, kek_id="not-a-kek-id")
        # the marker survives a rotation: the previous KEK still verifies its own marker
        rotated = _kek(CURRENT, PREVIOUS)
        old_mac = platform_root_mac(_kek(PREVIOUS))
        assert verify_platform_root_mac(old_mac, kek=rotated, kek_id=_kek(PREVIOUS).kek_ids[0])

    def test_event_hash_chains_and_is_sensitive_to_every_input(self) -> None:
        kek = _kek()
        genesis = platform_event_hash(None, b"event-1", kek=kek)
        second = platform_event_hash(genesis, b"event-1", kek=kek)
        assert len({genesis, second}) == 2 and len(genesis) == len(second) == 32
        assert genesis != platform_event_hash(None, b"event-2", kek=kek)
        assert second != platform_event_hash(genesis, b"event-2", kek=kek)
        assert genesis != platform_event_hash(None, b"event-1", kek=_kek(OTHER))
        assert genesis != platform_event_hash(b"\x00" * 32, b"event-1", kek=kek)
        with pytest.raises(PlatformSecretInputError):
            platform_event_hash(b"short", b"event", kek=kek)

    def test_key_namespaces_are_separate_and_reserved(self) -> None:
        kek = _kek()
        infos = [
            vault._SEAL_INFO,
            vault._FINGERPRINT_INFO,
            vault._CHAIN_INFO,
        ]
        assert all(i.startswith(b"waddles/platform-secrets/") for i in infos)
        keys = [kek.derive_key(i) for i in infos]
        assert len(set(keys)) == len(infos), "each purpose derives an independent key"
        # a key from another feature's label (tenant DEK / ban-evasion style) never collides
        for other in (
            b"waddles/tenant-dek/v1",
            b"waddles/ban-evasion/v1",
            b"waddles/platform-secret/v1",
        ):
            assert kek.derive_key(other) not in keys
        assert CURRENT not in keys


class TestRootMarkerMatchesTheHook:
    """The DB marker equals the Helm hook's annotation `v1.<kek_id>.<hmac>` byte for byte."""

    def test_marker_equals_an_independent_computation_of_the_hook_construction(self) -> None:
        import hashlib
        import hmac as stdlib_hmac

        kek_raw = bytes.fromhex("00112233445566778899aabbccddeeff" * 2)
        kek_id = hashlib.sha256(kek_raw).digest()[:8].hex()  # hook: first 8 bytes of SHA-256(KEK)
        mac = stdlib_hmac.new(kek_raw, b"waddles-root-check", hashlib.sha256).digest()
        adapter = _kek(kek_raw)
        assert adapter.kek_ids[0] == kek_id
        assert platform_root_mac(adapter) == mac
        annotation = f"v1.{kek_id}.{mac.hex()}"
        assert vault.format_root_marker(kek_id, platform_root_mac(adapter)) == annotation
        assert vault.parse_root_marker(annotation) == (kek_id, mac)
        assert verify_platform_root_mac(mac, kek=adapter, kek_id=kek_id) is True

    @pytest.mark.parametrize(
        "bad",
        ["", "v2.0123456789abcdef." + "00" * 32, "v1.zz.00", "v1.0123456789abcdef.00", "v1.a.b.c"],
    )
    def test_malformed_markers_are_rejected(self, bad: str) -> None:
        with pytest.raises(PlatformSecretInputError):
            vault.parse_root_marker(bad)
