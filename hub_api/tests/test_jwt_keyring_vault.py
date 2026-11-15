"""T10: platform JWTs are ES256 with keys held in platform_vault.

Real keys, the real `PlatformSecretCache` and the real `flask_core` mint/verify path -- only the
clock is injected. Covers: signing with the active vault key, `kid` header, verification by `kid`,
the rotated-key verify-only window (honoured until `verify_until`, refused after), unknown / retired
kids, algorithm confusion, `alg: none`, a forged HS256 token made with the retired shared secret,
wrong `aud` / `iss`, expiry, and a missing vault (no active key => fail closed, never a fallback).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask_core import jwt_keyring as jk
from flask_core.auth import (
    DEFAULT_JWT_AUDIENCE,
    DEFAULT_JWT_ISSUER,
    create_jwt_token,
    verify_jwt_token,
)

from services.platform_secrets.cache import CachedSecret, PlatformSecretCache, verify_until_for
from services.platform_secrets.jwt_keyring import PlatformVaultJwtKeyring
from services.platform_secrets.registry import REGISTRY, SecretType, check_registry
from services.platform_secrets.repository import STATE_ACTIVE, STATE_VERIFY_ONLY
from services.provider_connections.vault import (
    PLATFORM_ALG_ES256,
    generate_platform_key_material,
)

JWT_TYPE = int(SecretType.JWT_SIGNING)
WINDOW_S = REGISTRY[SecretType.JWT_SIGNING].verify_window_s
T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


class _Clock:
    """A settable clock injected into the cache."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _entry(
    version: int, state: int, *, rotated_at: datetime | None = None
) -> tuple[CachedSecret, ec.EllipticCurvePrivateKey]:
    """One opened JWT_SIGNING version backed by a freshly generated real P-256 key."""
    material = generate_platform_key_material(PLATFORM_ALG_ES256)
    assert material.public_material is not None and material.kid is not None
    private = ec.derive_private_key(int.from_bytes(material.plaintext, "big"), ec.SECP256R1())
    return (
        CachedSecret(
            secret_type=JWT_TYPE,
            scope_id=None,
            version=version,
            state=state,
            kid=material.kid,
            public_id=uuid.uuid4(),
            public_material=material.public_material,
            activated_at=T0 - timedelta(hours=version),
            verify_until=verify_until_for(JWT_TYPE, state, rotated_at),
            plaintext=material.plaintext,
        ),
        private,
    )


def _claims(**over: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "7",
        "tenant": "global",
        "scope": "community:read",
        "iss": DEFAULT_JWT_ISSUER,
        "aud": DEFAULT_JWT_AUDIENCE,
        "iat": now,
        "exp": now + 600,
        "jti": "j-1",
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


@pytest.fixture
def clock() -> _Clock:
    """The injected cache clock."""
    return _Clock(T0)


@pytest.fixture
def vault(clock: _Clock) -> dict[str, Any]:
    """A cache with one ACTIVE v2 key and one VERIFY-ONLY v1 key (rotated 10 minutes ago)."""
    old, old_priv = _entry(1, STATE_VERIFY_ONLY, rotated_at=T0 - timedelta(minutes=10))
    new, new_priv = _entry(2, STATE_ACTIVE)
    cache = PlatformSecretCache(clock=clock)
    cache.seed([old, new])
    jk.set_jwt_keyring(PlatformVaultJwtKeyring(cache))
    return {"cache": cache, "old": old, "old_priv": old_priv, "new": new, "new_priv": new_priv}


def _mint() -> str:
    return create_jwt_token(
        user_id="7", username="a", email="a@b.invalid", roles=[], tenant="global"
    )


def test_registry_declares_es256_jwt_signing_key() -> None:
    check_registry()
    spec = REGISTRY[SecretType.JWT_SIGNING]
    assert spec.algorithm.name == "ES256" and spec.import_source is None


def test_mint_signs_with_the_active_vault_key_and_stamps_its_kid(vault: dict[str, Any]) -> None:
    token = _mint()
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256" and header["kid"] == vault["new"].kid
    assert verify_jwt_token(token) is not None


def test_signature_really_is_by_the_vault_private_key(vault: dict[str, Any]) -> None:
    token = _mint()
    pub = vault["new_priv"].public_key()
    claims = jwt.decode(token, pub, algorithms=["ES256"], audience=DEFAULT_JWT_AUDIENCE)
    assert claims["sub"] == "7"


def test_rotated_key_is_verify_only_inside_the_window(vault: dict[str, Any], clock: _Clock) -> None:
    """A token minted by the previous key keeps verifying until `verify_until`, then is refused."""
    old_token = jwt.encode(
        _claims(), vault["old_priv"], algorithm="ES256", headers={"kid": vault["old"].kid}
    )
    assert verify_jwt_token(old_token) is not None  # honoured: rotated 10 min ago, window 2 h
    clock.now = vault["old"].verify_until - timedelta(seconds=1)
    assert verify_jwt_token(old_token) is not None
    clock.now = vault["old"].verify_until + timedelta(seconds=1)
    assert verify_jwt_token(old_token) is None  # past verify_until: refused
    assert verify_jwt_token(_mint()) is not None  # the active key is unaffected


def test_verify_only_key_never_signs(vault: dict[str, Any]) -> None:
    for _ in range(3):
        assert jwt.get_unverified_header(_mint())["kid"] == vault["new"].kid


def test_verify_window_matches_the_registry(vault: dict[str, Any]) -> None:
    assert vault["old"].verify_until == T0 - timedelta(minutes=10) + timedelta(seconds=WINDOW_S)


def test_unknown_kid_is_refused(vault: dict[str, Any]) -> None:
    foreign = ec.generate_private_key(ec.SECP256R1())
    token = jwt.encode(_claims(), foreign, algorithm="ES256", headers={"kid": "kdoesnotexist0001"})
    assert verify_jwt_token(token) is None


def test_retired_key_is_refused(vault: dict[str, Any]) -> None:
    retired, retired_priv = _entry(0, 4)  # state 4 = retired: never servable
    cache: PlatformSecretCache = vault["cache"]
    cache.seed([retired, vault["new"]])
    token = jwt.encode(_claims(), retired_priv, algorithm="ES256", headers={"kid": retired.kid})
    assert verify_jwt_token(token) is None


def test_right_kid_wrong_key_is_refused(vault: dict[str, Any]) -> None:
    attacker = ec.generate_private_key(ec.SECP256R1())
    token = jwt.encode(_claims(), attacker, algorithm="ES256", headers={"kid": vault["new"].kid})
    assert verify_jwt_token(token) is None


def test_alg_none_is_refused(vault: dict[str, Any]) -> None:
    head = base64.urlsafe_b64encode(
        json.dumps({"alg": "none", "typ": "JWT", "kid": vault["new"].kid}).encode()
    ).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(_claims()).encode()).rstrip(b"=")
    assert verify_jwt_token(f"{head.decode()}.{body.decode()}.") is None


@pytest.mark.parametrize("alg", ["HS256", "HS384", "HS512"])
def test_hmac_alg_confusion_with_the_public_key_is_refused(alg: str, vault: dict[str, Any]) -> None:
    pub = vault["new_priv"].public_key()
    digest = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}[alg]
    for raw in (
        pub.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
        vault["new"].public_material,
    ):
        head = base64.urlsafe_b64encode(
            json.dumps({"alg": alg, "typ": "JWT", "kid": vault["new"].kid}).encode()
        ).rstrip(b"=")
        body = base64.urlsafe_b64encode(json.dumps(_claims()).encode()).rstrip(b"=")
        mac = hmac.new(raw, head + b"." + body, digest).digest()
        sig = base64.urlsafe_b64encode(mac).rstrip(b"=")
        assert verify_jwt_token(f"{head.decode()}.{body.decode()}.{sig.decode()}") is None


def test_forged_hs256_with_the_old_shared_secret_is_refused(vault: dict[str, Any]) -> None:
    """Cutover guarantee: whoever still holds the retired JWT_SECRET value can mint nothing."""
    old_secret = b"the-retired-shared-hs256-secret-value"
    for kid in (vault["new"].kid, "hs256-v1", None):
        header: dict[str, Any] = {"alg": "HS256", "typ": "JWT"}
        if kid:
            header["kid"] = kid
        head = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=")
        body = base64.urlsafe_b64encode(json.dumps(_claims()).encode()).rstrip(b"=")
        mac = hmac.new(old_secret, head + b"." + body, hashlib.sha256).digest()
        sig = base64.urlsafe_b64encode(mac).rstrip(b"=")
        assert verify_jwt_token(f"{head.decode()}.{body.decode()}.{sig.decode()}") is None


def test_wrong_audience_and_issuer_are_refused(vault: dict[str, Any]) -> None:
    key, kid = vault["new_priv"], vault["new"].kid
    bad_aud = jwt.encode(_claims(aud="someone-else"), key, algorithm="ES256", headers={"kid": kid})
    bad_iss = jwt.encode(_claims(iss="attacker"), key, algorithm="ES256", headers={"kid": kid})
    good = jwt.encode(_claims(), key, algorithm="ES256", headers={"kid": kid})
    assert verify_jwt_token(bad_aud) is None
    assert verify_jwt_token(bad_iss) is None
    assert verify_jwt_token(good) is not None  # control: only aud/iss differ


def test_expired_token_is_refused(vault: dict[str, Any]) -> None:
    now = int(time.time())
    expired = jwt.encode(
        _claims(iat=now - 7200, exp=now - 3600),
        vault["new_priv"],
        algorithm="ES256",
        headers={"kid": vault["new"].kid},
    )
    assert verify_jwt_token(expired) is None


def test_empty_vault_fails_closed_for_mint_and_verify(vault: dict[str, Any]) -> None:
    token = _mint()
    vault["cache"].seed([])  # vault not bootstrapped / kill-switched: nothing servable
    with pytest.raises(jk.NoSigningKeyError):
        _mint()
    assert verify_jwt_token(token) is None


def test_corrupt_published_public_key_is_refused(vault: dict[str, Any]) -> None:
    broken = CachedSecret(
        **{
            **{f: getattr(vault["new"], f) for f in vault["new"].__slots__},
            "public_material": b"\x04" + b"\x00" * 64,
        }
    )
    vault["cache"].seed([broken])
    assert verify_jwt_token(_mint_with(vault["new_priv"], broken.kid)) is None


def _mint_with(private: ec.EllipticCurvePrivateKey, kid: str) -> str:
    return jwt.encode(_claims(), private, algorithm="ES256", headers={"kid": kid})


class _CountingLoader:
    """A loader that counts reloads and serves a fixed set of entries."""

    def __init__(self, entries: list[CachedSecret]) -> None:
        self.entries = entries
        self.calls = 0

    def __call__(self) -> list[CachedSecret]:
        self.calls += 1
        return list(self.entries)


def _forged_kid_token(kid: str) -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return jwt.encode(_claims(), key, algorithm="ES256", headers={"kid": kid})


def test_bogus_kids_cause_at_most_one_reload_per_interval(clock: _Clock) -> None:
    """N distinct well-formed unknown kids => <= 1 vault reload per negative-TTL interval."""
    active, _ = _entry(2, STATE_ACTIVE)
    loader = _CountingLoader([active])
    cache = PlatformSecretCache(loader=loader, clock=clock)
    cache.seed([active])
    jk.set_jwt_keyring(PlatformVaultJwtKeyring(cache))
    for i in range(200):
        assert verify_jwt_token(_forged_kid_token(f"k{i:016x}")) is None
    assert loader.calls <= 1
    clock.now = clock.now + timedelta(seconds=31)
    for i in range(200, 400):
        assert verify_jwt_token(_forged_kid_token(f"k{i:016x}")) is None
    assert loader.calls <= 2  # one more interval, one more reload


def test_reload_does_not_reset_the_negative_cache(clock: _Clock) -> None:
    active, _ = _entry(2, STATE_ACTIVE)
    loader = _CountingLoader([active])
    cache = PlatformSecretCache(loader=loader, clock=clock)
    cache.seed([active])
    jk.set_jwt_keyring(PlatformVaultJwtKeyring(cache))
    bogus = f"k{1:016x}"
    assert verify_jwt_token(_forged_kid_token(bogus)) is None
    cache.refresh()  # a reload (e.g. the 60 s poll) must not forget the remembered miss
    before = loader.calls
    assert verify_jwt_token(_forged_kid_token(bogus)) is None
    assert loader.calls == before


def test_negative_cache_is_bounded(clock: _Clock) -> None:
    from services.platform_secrets.cache import MAX_NEGATIVE_ENTRIES, UnknownKeyIdError

    active, _ = _entry(2, STATE_ACTIVE)
    cache = PlatformSecretCache(loader=_CountingLoader([active]), clock=clock)
    cache.seed([active])
    for i in range(MAX_NEGATIVE_ENTRIES * 2):
        with pytest.raises(UnknownKeyIdError):
            cache.by_kid(JWT_TYPE, f"k{i:016x}")
        clock.now = clock.now + timedelta(milliseconds=1)
    assert len(cache._negative) <= MAX_NEGATIVE_ENTRIES  # noqa: SLF001


@pytest.mark.parametrize(
    "bad_kid",
    ["../../etc/passwd", "k", "K0123456789abcdef", "k0123456789abcdeg", "k" + "0" * 17, "hs256-v1"],
)
def test_malformed_kid_never_reaches_the_cache(bad_kid: str, clock: _Clock) -> None:
    class _Spy(PlatformSecretCache):
        lookups = 0

        def by_kid(self, secret_type: int, kid: str, scope_id: int | None = None) -> CachedSecret:
            type(self).lookups += 1
            return super().by_kid(secret_type, kid, scope_id)

    active, _ = _entry(2, STATE_ACTIVE)
    loader = _CountingLoader([active])
    cache = _Spy(loader=loader, clock=clock)
    cache.seed([active])
    ring = PlatformVaultJwtKeyring(cache)
    with pytest.raises(jk.UnknownKidError):
        ring.verify_key(bad_kid)
    assert _Spy.lookups == 0 and loader.calls == 0
