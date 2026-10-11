"""`flask_core.auth` -- short access-token TTL and server-side revocation through the REAL verifier.

Every assertion runs a real minted (or hand-forged) token through the production
`verify_jwt_token`, which consults the production `TokenRevocation` (process-local backend here,
a real Valkey in `test_token_revocation_valkey.py`). Covers the four behaviours the enterprise /
SOC 2 session-management finding asks for: a revoked token is rejected before `exp`, revoke-all
cuts off every prior-`iat` token, the short TTL is enforced (mint and verify), and a
revocation-store outage behaves exactly as documented (bounded grace, then fail closed).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import jwt
import pytest

from flask_core import auth
from flask_core import token_revocation as tr
from flask_core.auth import (
    ACCESS_TOKEN_TTL_SECONDS,
    DEFAULT_JWT_AUDIENCE,
    DEFAULT_JWT_ISSUER,
    DEFAULT_JWT_KID,
    JWT_CLOCK_SKEW_SECONDS,
    MAX_ACCESS_TOKEN_TTL_SECONDS,
    MIN_ACCESS_TOKEN_TTL_SECONDS,
    create_jwt_token,
    verify_jwt_token,
)
from flask_core.jwt_hardening import MAX_JWT_LIFETIME_SECONDS

SECRET = "test-secret-key-not-for-production-use-only"


def mint(sub: str = "42", **kwargs: Any) -> str:
    return create_jwt_token(
        user_id=sub,
        username="alice",
        email="alice@example.invalid",
        roles=[],
        secret_key=SECRET,
        tenant="global",
        scope="community:read",
        **kwargs,
    )


def forge(**overrides: Any) -> str:
    """A correctly signed token with hand-picked claims (a None override drops the claim)."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "42",
        "tenant": "global",
        "scope": "",
        "iss": DEFAULT_JWT_ISSUER,
        "aud": DEFAULT_JWT_AUDIENCE,
        "iat": now,
        "exp": now + 600,
        "jti": "forged-jti-1",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, SECRET, algorithm="HS256", headers={"kid": DEFAULT_JWT_KID})


def claims_of(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


class ManualClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class SwitchBackend(tr.MemoryBackend):
    """A real in-memory store with an outage switch."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    def get_many(self, keys: Any) -> list[int | None]:
        if self.down:
            raise tr.RevocationStoreError("ConnectionError")
        return super().get_many(keys)


class TestShortAccessTtl:
    def test_default_access_token_lives_the_configured_window(self) -> None:
        claims = claims_of(mint())
        assert claims["exp"] - claims["iat"] == ACCESS_TOKEN_TTL_SECONDS
        assert MIN_ACCESS_TOKEN_TTL_SECONDS <= ACCESS_TOKEN_TTL_SECONDS <= MAX_ACCESS_TOKEN_TTL_SECONDS
        assert ACCESS_TOKEN_TTL_SECONDS <= 15 * 60

    def test_default_is_fifteen_minutes(self) -> None:
        assert auth._load_access_token_ttl(None) == 900
        assert auth._load_access_token_ttl("  ") == 900

    @pytest.mark.parametrize("raw", ["300", "600", "900"])
    def test_in_window_override_is_honoured(self, raw: str) -> None:
        assert auth._load_access_token_ttl(raw) == int(raw)

    @pytest.mark.parametrize("raw", ["299", "901", "86400", "0", "-5", "soon", "9.5"])
    def test_out_of_window_override_is_refused(self, raw: str) -> None:
        with pytest.raises(ValueError, match="JWT_ACCESS_TTL_SECONDS"):
            auth._load_access_token_ttl(raw)

    @pytest.mark.parametrize("raw", ["86400", "soon"])
    def test_a_bad_ttl_env_stops_the_service_at_import(self, raw: str) -> None:
        env = {k: v for k, v in os.environ.items() if k != "JWT_ACCESS_TTL_SECONDS"}
        env["JWT_ACCESS_TTL_SECONDS"] = raw
        proc = subprocess.run(  # noqa: S603 - fixed argv, test-controlled env
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import flask_core.auth",
                str(Path(auth.__file__).resolve().parent.parent),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert proc.returncode != 0
        assert "JWT_ACCESS_TTL_SECONDS" in proc.stderr

    def test_machine_tokens_may_ask_for_their_one_hour_ceiling(self) -> None:
        claims = claims_of(mint(expiration_hours=1))
        assert claims["exp"] - claims["iat"] == 3600

    def test_explicit_seconds(self) -> None:
        claims = claims_of(mint(expiration_seconds=300))
        assert claims["exp"] - claims["iat"] == 300

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"expiration_hours": 24},  # the retired default
            {"expiration_hours": 2},
            {"expiration_seconds": MAX_JWT_LIFETIME_SECONDS + 1},
            {"expiration_seconds": 0},
            {"expiration_seconds": -60},
            {"expiration_hours": 0},
            {"expiration_hours": 1, "expiration_seconds": 600},
        ],
    )
    def test_minting_a_long_lived_token_is_refused(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            mint(**kwargs)

    def test_verifier_refuses_a_token_lived_past_the_ceiling(self, metrics_capture: Any) -> None:
        now = int(time.time())
        legacy = forge(iat=now, exp=now + 24 * 3600)  # what the old default minted
        assert verify_jwt_token(legacy, SECRET) is None
        assert ("platform_hs256", "hs256", "lifetime_exceeded") in metrics_capture.verifications()

    @pytest.mark.parametrize(
        ("lifetime", "accepted"),
        [
            (600, True),
            (MAX_JWT_LIFETIME_SECONDS, True),
            (MAX_JWT_LIFETIME_SECONDS + JWT_CLOCK_SKEW_SECONDS, True),
            (MAX_JWT_LIFETIME_SECONDS + JWT_CLOCK_SKEW_SECONDS + 1, False),
            (7 * 24 * 3600, False),
        ],
    )
    def test_lifetime_ceiling_boundary(self, lifetime: int, accepted: bool) -> None:
        now = int(time.time())
        token = forge(iat=now, exp=now + lifetime)
        assert (verify_jwt_token(token, SECRET) is not None) is accepted

    def test_lifetime_rejection_is_an_error_level_alarm_without_token_data(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        now = int(time.time())
        with caplog.at_level(logging.DEBUG, logger="flask_core"):
            assert verify_jwt_token(forge(iat=now, exp=now + 86400, sub="sentinel-sub-9"), SECRET) is None
        rejected = [r for r in caplog.records if "JWT rejected" in r.getMessage()]
        assert [r.levelname for r in rejected] == ["ERROR"]
        assert "lifetime_exceeded" in rejected[0].getMessage()
        assert "sentinel" not in caplog.text


class TestMintedClaims:
    def test_every_token_carries_a_unique_valid_jti(self) -> None:
        jtis = {claims_of(mint())["jti"] for _ in range(50)}
        assert len(jtis) == 50
        assert all(tr.is_valid_identifier(j) for j in jtis)

    def test_sid_is_present_only_when_a_session_is_named(self) -> None:
        assert "sid" not in claims_of(mint())
        assert claims_of(mint(session_id="sess-17"))["sid"] == "sess-17"

    @pytest.mark.parametrize("bad", ["", "has space", "-lead", "x" * 129, "a/b"])
    def test_invalid_session_id_is_refused_at_mint(self, bad: str) -> None:
        with pytest.raises(ValueError, match="session_id"):
            mint(session_id=bad)

    def test_token_without_jti_still_verifies(self) -> None:
        """Service tokens minted before the rollout carry no jti; they are not rejected for it."""
        assert verify_jwt_token(forge(jti=None), SECRET) is not None

    @pytest.mark.parametrize(
        "overrides",
        [
            {"jti": "has space"},
            {"jti": "x" * 129},
            {"jti": ""},
            {"sid": "../etc"},
            {"sid": 7},
            {"sub": "s" * 129},
        ],
    )
    def test_hostile_identifier_claims_are_invalid_claims(
        self, overrides: dict[str, Any], metrics_capture: Any
    ) -> None:
        assert verify_jwt_token(forge(**overrides), SECRET) is None
        outcomes = {o for (_v, _a, o) in metrics_capture.verifications()}
        assert outcomes & {"invalid_claim", "invalid"}


class TestRevokedTokenIsRejectedBeforeExp:
    def test_revoked_jti(self, metrics_capture: Any) -> None:
        token = mint()
        payload = verify_jwt_token(token, SECRET)
        assert payload is not None and payload["exp"] > time.time() + 60
        tr.get_token_revocation().revoke_token(payload["jti"], exp=payload["exp"])
        assert verify_jwt_token(token, SECRET) is None  # still ~15 minutes from its exp
        assert ("platform_hs256", "hs256", "revoked") in metrics_capture.verifications()

    def test_only_the_revoked_token_dies(self) -> None:
        keep, kill = mint(), mint()
        tr.get_token_revocation().revoke_token(claims_of(kill)["jti"])
        assert verify_jwt_token(keep, SECRET) is not None
        assert verify_jwt_token(kill, SECRET) is None

    def test_revoked_session_kills_every_token_minted_under_it(self) -> None:
        first, second = mint(session_id="sess-1"), mint(session_id="sess-1")
        other = mint(session_id="sess-2")
        tr.get_token_revocation().revoke_session("sess-1")
        assert verify_jwt_token(first, SECRET) is None
        assert verify_jwt_token(second, SECRET) is None
        assert verify_jwt_token(other, SECRET) is not None

    def test_revoked_token_logs_a_warning_without_any_token_data(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        token = mint(sub="sentinel-user-31", session_id="sentinel-sess-31")
        payload = claims_of(token)
        tr.get_token_revocation().revoke_token(payload["jti"])
        with caplog.at_level(logging.DEBUG, logger="flask_core"):
            assert verify_jwt_token(token, SECRET) is None
        rejected = [r for r in caplog.records if "JWT rejected" in r.getMessage()]
        assert [r.levelname for r in rejected] == ["WARNING"]  # ordinary, not an alarm
        assert "reason=revoked" in rejected[0].getMessage()
        assert payload["jti"] not in caplog.text
        assert "sentinel" not in caplog.text
        assert token not in caplog.text


class TestRevokeAllCutsOffPriorTokens:
    def test_every_token_issued_at_or_before_the_cutoff_dies_and_a_later_one_lives(self) -> None:
        now = int(time.time())
        before = [forge(sub="7", iat=now - age, exp=now - age + 600, jti=f"j{age}") for age in (1, 60, 300)]
        same_second = forge(sub="7", iat=now, exp=now + 600, jti="jnow")
        bystander = forge(sub="8", iat=now - 30, exp=now + 600, jti="jother")
        for token in [*before, same_second, bystander]:
            assert verify_jwt_token(token, SECRET) is not None

        cutoff = tr.get_token_revocation().revoke_user("7", cutoff=now)
        assert cutoff == now

        for token in [*before, same_second]:
            assert verify_jwt_token(token, SECRET) is None
        assert verify_jwt_token(bystander, SECRET) is not None  # another user is untouched
        after = forge(sub="7", iat=now + 5, exp=now + 605, jti="jafter")  # inside iat leeway
        assert verify_jwt_token(after, SECRET) is not None

    def test_revoke_all_is_a_cutoff_not_a_list_of_tokens(self) -> None:
        """Tokens minted before the revoke but never seen by the store are covered too."""
        tokens = [mint(sub="99") for _ in range(25)]
        tr.get_token_revocation().revoke_user("99")
        assert all(verify_jwt_token(t, SECRET) is None for t in tokens)

    def test_a_service_token_without_jti_or_sid_is_covered_by_the_cutoff(self) -> None:
        now = int(time.time())
        token = forge(sub="svc-ingest", jti=None, iat=now - 5, exp=now + 600)
        assert verify_jwt_token(token, SECRET) is not None
        tr.get_token_revocation().revoke_user("svc-ingest", cutoff=now)
        assert verify_jwt_token(token, SECRET) is None


class TestStoreOutageBehavesAsDocumented:
    def _install(self, **policy: Any) -> tuple[SwitchBackend, ManualClock]:
        backend, clock = SwitchBackend(), ManualClock()
        rev = tr.TokenRevocation(
            backend,
            policy=tr.RevocationPolicy(**policy),
            monotonic=clock,
        )
        tr.configure_token_revocation(rev)  # the conftest fixture restores the previous one
        return backend, clock

    def test_brief_outage_keeps_serving(self, metrics_capture: Any) -> None:
        backend, clock = self._install(grace_seconds=60.0)
        token = mint()
        assert verify_jwt_token(token, SECRET) is not None
        backend.down = True
        clock.now += 6  # local cache expired -> a store read is attempted -> it fails
        assert verify_jwt_token(token, SECRET) is not None  # degraded, not broken
        assert verify_jwt_token(mint(), SECRET) is not None  # a brand-new token is served too

    def test_known_revocations_are_still_enforced_during_the_outage(self) -> None:
        backend, clock = self._install(grace_seconds=60.0)
        token = mint()
        tr.get_token_revocation().revoke_token(claims_of(token)["jti"])
        backend.down = True
        clock.now += 30
        assert verify_jwt_token(token, SECRET) is None

    def test_beyond_the_grace_window_it_fails_closed(
        self, metrics_capture: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend, clock = self._install(grace_seconds=60.0)
        token = mint()
        assert verify_jwt_token(token, SECRET) is not None
        backend.down = True
        clock.now += 61
        with caplog.at_level(logging.DEBUG, logger="flask_core"):
            assert verify_jwt_token(token, SECRET) is None
        assert ("platform_hs256", "hs256", "revocation_unavailable") in metrics_capture.verifications()
        rejected = [r for r in caplog.records if "JWT rejected" in r.getMessage()]
        assert [r.levelname for r in rejected] == ["ERROR"]

    def test_fail_open_is_an_explicit_operator_opt_in(self) -> None:
        backend, clock = self._install(grace_seconds=60.0, fail_closed=False)
        token = mint()
        backend.down = True
        clock.now += 61
        assert verify_jwt_token(token, SECRET) is not None

    def test_service_recovers_without_a_restart(self) -> None:
        backend, clock = self._install(grace_seconds=60.0, circuit_open_seconds=5.0)
        token = mint()
        backend.down = True
        clock.now += 61
        assert verify_jwt_token(token, SECRET) is None
        backend.down = False
        clock.now += 5.1
        assert verify_jwt_token(token, SECRET) is not None


class TestMisconfigurationIsLoud:
    def test_a_bad_revocation_env_is_not_swallowed_by_the_verifier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running without revocation because of a typo is worse than refusing to start."""
        monkeypatch.setenv("JWT_REVOCATION_FAIL_MODE", "ajar")
        tr.configure_token_revocation(None)
        with pytest.raises(ValueError, match="JWT_REVOCATION_FAIL_MODE"):
            verify_jwt_token(mint(), SECRET)


def test_exp_is_still_strict_and_expired_tokens_are_not_revocation_checked() -> None:
    """Revocation never loosens the existing checks: an expired token is rejected, full stop."""
    now = int(time.time())
    expired = forge(iat=now - 700, exp=now - 100)
    assert verify_jwt_token(expired, SECRET) is None
