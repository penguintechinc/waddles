"""Ban-suppression key derivation -- `services/suppression_keys.py`.

The code stored in `suppression_records.hmac` is the ONLY thing retained about a
banned user who erases their account, so its three properties are each pinned:

- **keyed**: not recoverable (or even testable) without ``K_master``;
- **per tenant**: the same platform id has a different code in every tenant;
- **stable**: a format change would silently un-match every stored row, so a
  known-answer vector computed by an independent RFC 5869 implementation pins it.

No mocks: real HKDF / HMAC / hashlib throughout.
"""

from __future__ import annotations

import hashlib
import hmac as stdlib_hmac

import pytest

from services import suppression_keys as keys
from services.suppression_keys import (
    MasterKey,
    PlatformKekKeySource,
    SuppressionInputError,
    SuppressionKeyError,
)

MASTER_HEX = "11" * 32
#: A Discord-shaped snowflake, built by concatenation so scanners do not read a client id.
SNOWFLAKE = "1234567890" "12345678"
OTHER_HEX = "22" * 32


def _master(hex_key: str = MASTER_HEX) -> MasterKey:
    return PlatformKekKeySource({keys.ENV_CURRENT: hex_key}).master_keys().current


def _independent_code(master_hex: str, tenant_id: int, platform: str, puid: str) -> str:
    """RFC 5869 HKDF-SHA256 (no salt) + HMAC re-implemented from hashlib/hmac only."""
    ikm = bytes.fromhex(master_hex)
    prk = stdlib_hmac.new(b"\x00" * 32, ikm, hashlib.sha256).digest()
    info = f"waddles-suppression/v1/tenant:{tenant_id}".encode()
    tenant_key = stdlib_hmac.new(prk, info + b"\x01", hashlib.sha256).digest()
    message = b"waddles-suppression/v1\x00" + platform.encode() + b"\x00" + puid.encode()
    return stdlib_hmac.new(tenant_key, message, hashlib.sha256).hexdigest()


class TestCodeProperties:
    def test_known_answer_vector_pins_the_format(self) -> None:
        code = keys.compute_code(_master(), 7, "discord", SNOWFLAKE)
        assert code.hmac == _independent_code(MASTER_HEX, 7, "discord", SNOWFLAKE)
        # Frozen literals: a change here silently un-matches every stored suppression row.
        assert code.key_ver == "02d449a31fbb267c"  # gitleaks:allow -- public test vector
        # gitleaks:allow -- public test vector, not a credential
        assert code.hmac == (
            "a45b2ae61da43ba311102fe90b6694bb897b069eea00192bb1a1a2d797084e27"  # gitleaks:allow
        )

    def test_deterministic_same_inputs_same_code(self) -> None:
        first = keys.compute_code(_master(), 3, "twitch", "42")
        second = keys.compute_code(_master(), 3, "twitch", "42")
        assert first == second
        assert len(first.hmac) == 64

    def test_same_platform_id_has_a_different_code_in_every_tenant(self) -> None:
        codes = {keys.compute_code(_master(), t, "discord", "999").hmac for t in range(1, 21)}
        assert len(codes) == 20  # no cross-tenant collision, hence no cross-tenant match

    def test_equal_ids_on_two_platforms_are_not_linkable(self) -> None:
        discord = keys.compute_code(_master(), 1, "discord", "555")
        twitch = keys.compute_code(_master(), 1, "twitch", "555")
        assert discord.hmac != twitch.hmac

    def test_distinct_ids_never_collide_in_a_sample(self) -> None:
        sample = {keys.compute_code(_master(), 1, "twitch", str(i)).hmac for i in range(500)}
        assert len(sample) == 500

    def test_a_different_master_key_gives_a_different_code_and_version(self) -> None:
        a = keys.compute_code(_master(MASTER_HEX), 1, "discord", "1")
        b = keys.compute_code(_master(OTHER_HEX), 1, "discord", "1")
        assert a.hmac != b.hmac
        assert a.key_ver != b.key_ver

    def test_id_case_is_preserved_but_whitespace_and_platform_case_are_normalised(self) -> None:
        base = keys.compute_code(_master(), 1, "discord", "AbC")
        assert keys.compute_code(_master(), 1, "  Discord ", " AbC\n") == base
        assert keys.compute_code(_master(), 1, "discord", "abc") != base

    def test_derive_tenant_key_is_per_tenant_32_bytes_and_not_the_master(self) -> None:
        master = _master()
        k1, k2 = keys.derive_tenant_key(master, 1), keys.derive_tenant_key(master, 2)
        assert len(k1) == len(k2) == 32
        assert k1 != k2
        assert k1 != master.material


class TestNotReversibleWithoutTheKey:
    """The stored value reveals nothing a database thief can test a guess against."""

    PUID = SNOWFLAKE

    def _stored(self) -> str:
        return keys.compute_code(_master(), 5, "discord", self.PUID).hmac

    def test_no_unkeyed_construction_reproduces_the_stored_code(self) -> None:
        stored = self._stored()
        candidates = {
            "sha256(id)": hashlib.sha256(self.PUID.encode()).hexdigest(),
            "sha256(tenant|id)": hashlib.sha256(f"5|{self.PUID}".encode()).hexdigest(),
            "sha256(platform|id)": hashlib.sha256(f"discord|{self.PUID}".encode()).hexdigest(),
            "sha1(id)": hashlib.sha1(self.PUID.encode(), usedforsecurity=False).hexdigest(),
            "hmac(empty key)": stdlib_hmac.new(b"", self.PUID.encode(), hashlib.sha256).hexdigest(),
            "hmac(tenant as key)": stdlib_hmac.new(
                b"5", self.PUID.encode(), hashlib.sha256
            ).hexdigest(),
        }
        for name, value in candidates.items():
            assert value != stored, name

    def test_wrong_master_cannot_confirm_a_guess_but_the_right_one_can(self) -> None:
        stored = self._stored()
        space = [str(10**17 + i) for i in range(2000)] + [self.PUID]
        wrong = [
            i
            for i in space
            if keys.compute_code(_master(OTHER_HEX), 5, "discord", i).hmac == stored
        ]
        right = [i for i in space if keys.compute_code(_master(), 5, "discord", i).hmac == stored]
        print(
            f"enumeration over {len(space)} ids: "
            f"wrong-key hits={len(wrong)} right-key hits={len(right)}"
        )
        assert wrong == []
        assert right == [self.PUID]

    def test_wrong_tenant_cannot_confirm_a_guess(self) -> None:
        stored = self._stored()
        assert keys.compute_code(_master(), 6, "discord", self.PUID).hmac != stored


class TestMasterKeySource:
    def test_loads_current_and_exposes_a_non_reversible_version(self) -> None:
        loaded = PlatformKekKeySource({keys.ENV_CURRENT: MASTER_HEX}).master_keys()
        assert (
            loaded.current.key_ver == hashlib.sha256(bytes.fromhex(MASTER_HEX)).digest()[:8].hex()
        )
        assert loaded.previous == ()
        assert loaded.all() == (loaded.current,)

    def test_previous_key_is_loaded_for_rotation_lookups(self) -> None:
        env = {keys.ENV_CURRENT: MASTER_HEX, keys.ENV_PREVIOUS: OTHER_HEX}
        loaded = PlatformKekKeySource(env).master_keys()
        assert [k.key_ver for k in loaded.all()] == [
            keys.key_version(bytes.fromhex(MASTER_HEX)),
            keys.key_version(bytes.fromhex(OTHER_HEX)),
        ]

    def test_whitespace_around_the_hex_is_tolerated(self) -> None:
        loaded = PlatformKekKeySource({keys.ENV_CURRENT: f"  {MASTER_HEX}\n"}).master_keys()
        assert loaded.current.material == bytes.fromhex(MASTER_HEX)

    @pytest.mark.parametrize(
        "env",
        [
            {},
            {keys.ENV_CURRENT: ""},
            {keys.ENV_CURRENT: "   "},
            {keys.ENV_CURRENT: "11" * 31},
            {keys.ENV_CURRENT: "zz" * 32},
            {keys.ENV_CURRENT: MASTER_HEX, keys.ENV_PREVIOUS: "11" * 3},
            {keys.ENV_CURRENT: MASTER_HEX, keys.ENV_PREVIOUS: "qq" * 32},
        ],
    )
    def test_missing_or_malformed_key_fails_loudly_without_echoing_it(
        self, env: dict[str, str]
    ) -> None:
        with pytest.raises(SuppressionKeyError) as excinfo:
            PlatformKekKeySource(env).master_keys()
        message = str(excinfo.value)
        assert all(value.strip() not in message for value in env.values() if value.strip())

    def test_reads_process_environment_lazily_and_never_caches_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(keys.ENV_CURRENT, raising=False)
        source = PlatformKekKeySource()
        with pytest.raises(SuppressionKeyError):
            source.master_keys()
        monkeypatch.setenv(keys.ENV_CURRENT, MASTER_HEX)
        assert source.master_keys().current.material == bytes.fromhex(MASTER_HEX)

    def test_repr_never_shows_key_material(self) -> None:
        loaded = PlatformKekKeySource(
            {keys.ENV_CURRENT: MASTER_HEX, keys.ENV_PREVIOUS: OTHER_HEX}
        ).master_keys()
        text = repr(loaded) + repr(loaded.current)
        assert MASTER_HEX not in text and OTHER_HEX not in text
        assert "\\x11" not in text and "b'" not in text


class TestRotation:
    def test_row_written_under_the_old_key_is_still_found_after_rotation(self) -> None:
        before = PlatformKekKeySource({keys.ENV_CURRENT: MASTER_HEX})
        written = keys.current_code(before, 4, "twitch", "77")
        after = PlatformKekKeySource({keys.ENV_CURRENT: OTHER_HEX, keys.ENV_PREVIOUS: MASTER_HEX})
        looked_up = keys.lookup_codes(after, 4, "twitch", "77")
        assert looked_up[0].key_ver == keys.key_version(bytes.fromhex(OTHER_HEX))  # new writes
        assert written in looked_up  # old rows still match via the previous key
        assert keys.current_code(after, 4, "twitch", "77") != written

    def test_without_the_previous_key_the_old_row_is_unmatchable(self) -> None:
        written = keys.current_code(
            PlatformKekKeySource({keys.ENV_CURRENT: MASTER_HEX}), 4, "x", "1"
        )
        after = PlatformKekKeySource({keys.ENV_CURRENT: OTHER_HEX})
        assert written not in keys.lookup_codes(after, 4, "x", "1")


class TestInputValidation:
    @pytest.mark.parametrize("tenant", [0, -1, True, "1", None, 1.5])
    def test_bad_tenant_ids_are_rejected(self, tenant: object) -> None:
        with pytest.raises(SuppressionInputError):
            keys.compute_code(_master(), tenant, "discord", "1")  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("platform", "puid"),
        [
            ("discord", ""),
            ("discord", "   "),
            ("discord", "a\x00b"),
            ("discord", "a\nb"),
            ("discord", "x" * 256),
            ("", "1"),
            ("Dis cord", "1"),
            ("1discord", "1"),
            ("a" * 51, "1"),
            (None, "1"),
            ("discord", None),
            ("discord", 123),
        ],
    )
    def test_malformed_identities_are_rejected_without_echo(
        self, platform: object, puid: object
    ) -> None:
        with pytest.raises(SuppressionInputError) as excinfo:
            keys.compute_code(_master(), 1, platform, puid)  # type: ignore[arg-type]
        assert "x" * 20 not in str(excinfo.value)

    def test_normalize_identity_returns_canonical_pair(self) -> None:
        assert keys.normalize_identity(" Twitch ", " 42 ") == ("twitch", "42")

    def test_limit_length_id_is_accepted(self) -> None:
        assert keys.compute_code(_master(), 1, "kick", "x" * 255).hmac
