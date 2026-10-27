"""`PlatformKekAdapter.derive_key` / `kek_ids` -- the seam ban-evasion suppression keys off.

Suppression needs a per-tenant HMAC key rooted in the platform KEK without holding the KEK or a
second copy of the Secret. The adapter derives it (`derive_key`, HKDF-SHA256 via
`crypto.derive_kek_subkey`) so the KEK never leaves it; `kek_ids` lists the loaded KEKs, current
first, so rotation lookups can try the previous one.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from services.envelope.crypto import KEY_LENGTH, derive_kek_subkey, kek_id
from services.envelope.errors import EnvelopeInputError, PlatformKekError
from services.envelope.platform_kek import PlatformKekAdapter

CURRENT = bytes.fromhex("33" * 32)
PREVIOUS = bytes.fromhex("44" * 32)


def _hkdf_sha256_nosalt(ikm: bytes, info: bytes) -> bytes:
    """RFC 5869 extract+expand (one block), from hashlib/hmac only."""
    prk = hmac.new(b"\x00" * 32, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()


class TestDeriveKekSubkey:
    def test_matches_an_independent_rfc5869_implementation(self) -> None:
        assert derive_kek_subkey(CURRENT, b"label-a") == _hkdf_sha256_nosalt(CURRENT, b"label-a")

    def test_is_32_bytes_deterministic_and_not_the_kek(self) -> None:
        key = derive_kek_subkey(CURRENT, b"label")
        assert len(key) == KEY_LENGTH
        assert key == derive_kek_subkey(CURRENT, b"label")
        assert key != CURRENT

    def test_distinct_info_or_kek_gives_distinct_keys(self) -> None:
        assert derive_kek_subkey(CURRENT, b"a") != derive_kek_subkey(CURRENT, b"b")
        assert derive_kek_subkey(CURRENT, b"a") != derive_kek_subkey(PREVIOUS, b"a")

    @pytest.mark.parametrize("kek", [b"", b"short", bytes(31), bytes(33)])
    def test_rejects_a_kek_that_is_not_32_bytes(self, kek: bytes) -> None:
        with pytest.raises(EnvelopeInputError):
            derive_kek_subkey(kek, b"label")

    def test_rejects_an_empty_label(self) -> None:
        with pytest.raises(EnvelopeInputError):
            derive_kek_subkey(CURRENT, b"")


class TestAdapterDerivation:
    def test_derives_from_the_current_kek_by_default(self) -> None:
        adapter = PlatformKekAdapter(CURRENT, PREVIOUS)
        assert adapter.derive_key(b"label") == derive_kek_subkey(CURRENT, b"label")

    def test_can_derive_from_a_previous_kek_by_id(self) -> None:
        adapter = PlatformKekAdapter(CURRENT, PREVIOUS)
        assert adapter.derive_key(b"label", key_id=kek_id(PREVIOUS)) == derive_kek_subkey(
            PREVIOUS, b"label"
        )

    def test_an_unknown_kek_id_fails_closed_without_naming_a_key(self) -> None:
        adapter = PlatformKekAdapter(CURRENT)
        with pytest.raises(PlatformKekError) as excinfo:
            adapter.derive_key(b"label", key_id=kek_id(PREVIOUS))
        assert CURRENT.hex() not in str(excinfo.value) and PREVIOUS.hex() not in str(excinfo.value)

    def test_kek_ids_list_current_first_then_previous(self) -> None:
        assert PlatformKekAdapter(CURRENT, PREVIOUS).kek_ids == (kek_id(CURRENT), kek_id(PREVIOUS))
        assert PlatformKekAdapter(CURRENT).kek_ids == (kek_id(CURRENT),)

    def test_a_rotation_to_the_same_key_lists_it_once(self) -> None:
        assert PlatformKekAdapter(CURRENT, CURRENT).kek_ids == (kek_id(CURRENT),)

    def test_an_empty_label_is_rejected(self) -> None:
        with pytest.raises(EnvelopeInputError):
            PlatformKekAdapter(CURRENT).derive_key(b"")

    def test_derivation_does_not_disturb_wrapping(self) -> None:
        import asyncio

        adapter = PlatformKekAdapter(CURRENT, PREVIOUS)
        adapter.derive_key(b"label")
        dek, context = bytes(32), {"waddles_purpose": "verify"}

        async def round_trip() -> bytes:
            return await adapter.unwrap(await adapter.wrap(dek, context=context), context=context)

        assert asyncio.run(round_trip()) == dek
