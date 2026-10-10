"""
Tests for flask_core.auth's platform (HS256) JWT minting and verification.

H-2 Phase 0 + MED-5 (2026-10-09 security audit): `verify_jwt_token` enforces
`iss`/`aud` (a token without them is rejected, not "grandfathered"), requires
every claim in `REQUIRED_JWT_CLAIMS`, and the minter stamps a `kid` header.
Algorithm-confusion / `alg: none` / key-material-header attacks live in
`test_jwt_hardening.py`; the retired default-tenant fallback in
`test_tenancy.py`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import pytest

from flask_core.auth import (
    DEFAULT_JWT_AUDIENCE,
    DEFAULT_JWT_ISSUER,
    DEFAULT_JWT_KID,
    JWT_CLOCK_SKEW_SECONDS,
    PLATFORM_JWT_ALGORITHM,
    REQUIRED_JWT_CLAIMS,
    create_jwt_token,
    verify_jwt_token,
)

SECRET = "test-secret-key-not-for-production-use-only"


def _payload(**overrides: Any) -> dict[str, Any]:
    """A fully valid platform payload; a value of None in `overrides` drops that claim."""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": "u1",
        "username": "alice",
        "email": "alice@example.com",
        "roles": [],
        "tenant": "global",
        "scope": "",
        "teams": [],
        "iss": DEFAULT_JWT_ISSUER,
        "aud": DEFAULT_JWT_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(hours=1),
        "type": "access",
    }
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not None}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _sign(payload: dict[str, Any], *, kid: str | None = DEFAULT_JWT_KID) -> str:
    headers = {"kid": kid} if kid is not None else None
    return jwt.encode(payload, SECRET, algorithm="HS256", headers=headers)


def _sign_raw(payload: dict[str, Any], *, secret: str = SECRET) -> str:
    """Sign `payload` verbatim -- bypassing PyJWT's encode-side type checks (iss must be str, ...)."""
    claims = {
        k: int(v.timestamp()) if isinstance(v, datetime) else v for k, v in payload.items()
    }
    return jwt.api_jws.PyJWS().encode(
        json.dumps(claims).encode(), secret, algorithm="HS256", headers={"kid": DEFAULT_JWT_KID}
    )


def _mint(**kw: Any) -> str:
    args: dict[str, Any] = {
        "user_id": "u1",
        "username": "alice",
        "email": "alice@example.com",
        "roles": ["viewer"],
        "secret_key": SECRET,
        "tenant": "global",
    }
    args.update(kw)
    return create_jwt_token(**args)


class TestCreateJwtToken:
    def test_default_issuer_audience_teams_emitted(self) -> None:
        decoded = jwt.decode(_mint(), SECRET, algorithms=["HS256"], options={"verify_aud": False})
        assert decoded["iss"] == DEFAULT_JWT_ISSUER
        assert decoded["aud"] == DEFAULT_JWT_AUDIENCE
        assert decoded["teams"] == []

    def test_teams_claim_passed_through(self) -> None:
        decoded = jwt.decode(
            _mint(teams=["team-a", "team-b"]), SECRET, algorithms=["HS256"], options={"verify_aud": False}
        )
        assert decoded["teams"] == ["team-a", "team-b"]

    def test_custom_issuer_and_audience(self) -> None:
        decoded = jwt.decode(
            _mint(issuer="custom-issuer", audience="custom-audience"),
            SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        assert (decoded["iss"], decoded["aud"]) == ("custom-issuer", "custom-audience")

    def test_minted_token_carries_every_required_claim(self) -> None:
        decoded = jwt.decode(_mint(), SECRET, algorithms=["HS256"], options={"verify_aud": False})
        assert set(REQUIRED_JWT_CLAIMS) <= decoded.keys()

    def test_header_is_pinned_algorithm_with_default_kid(self) -> None:
        header = jwt.get_unverified_header(_mint())
        assert header["alg"] == PLATFORM_JWT_ALGORITHM == "HS256"
        assert header["kid"] == DEFAULT_JWT_KID == "hs256-v1"

    def test_custom_kid_stamped(self) -> None:
        assert jwt.get_unverified_header(_mint(kid="hs256-v2"))["kid"] == "hs256-v2"

    @pytest.mark.parametrize("bad_kid", ["", "../../etc/passwd", "a b", "x" * 65, "kid\nX: y", "'; DROP--"])
    def test_malformed_kid_refused_at_mint(self, bad_kid: str) -> None:
        with pytest.raises(ValueError, match="kid"):
            _mint(kid=bad_kid)

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_secret_refused_at_mint(self, empty: Any) -> None:
        """A token signed with an empty HMAC key is forgeable by anyone -- fail loud at mint."""
        with pytest.raises(ValueError, match="secret_key"):
            _mint(secret_key=empty)

    def test_iat_is_utc_correct_regardless_of_process_timezone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time

        monkeypatch.setenv("TZ", "Pacific/Kiritimati")  # UTC+14
        time.tzset()
        try:
            decoded = jwt.decode(_mint(), SECRET, algorithms=["HS256"], options={"verify_aud": False})
        finally:
            monkeypatch.delenv("TZ")
            time.tzset()
        assert abs(decoded["iat"] - datetime.now(timezone.utc).timestamp()) < 5

    def test_creation_log_carries_no_username_email_or_token(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("DEBUG", logger="flask_core.auth"):
            token = _mint(username="alice-the-streamer", email="alice@example.com")
        rendered = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        assert "alice-the-streamer" not in rendered
        assert "alice@example.com" not in rendered
        assert token not in rendered


class TestRoundTrip:
    def test_create_and_verify_round_trip(self) -> None:
        payload = verify_jwt_token(_mint(scope="community:read tenant:read", tenant="acme"), SECRET)
        assert payload is not None
        assert payload["iss"] == DEFAULT_JWT_ISSUER
        assert payload["aud"] == DEFAULT_JWT_AUDIENCE
        assert payload["tenant"] == "acme"
        assert payload["scope"] == "community:read tenant:read"

    def test_empty_scope_is_a_valid_present_claim(self) -> None:
        """`scope: ""` = "no scopes granted" -- present, so accepted."""
        assert verify_jwt_token(_mint(scope=""), SECRET) is not None

    def test_audience_list_containing_ours_is_accepted(self) -> None:
        token = _sign(_payload(aud=["other-service", DEFAULT_JWT_AUDIENCE]))
        assert verify_jwt_token(token, SECRET) is not None

    def test_token_without_kid_header_still_verifies(self) -> None:
        """Tokens minted before the `kid` header existed stay valid for their 24h lifetime."""
        assert verify_jwt_token(_sign(_payload(), kid=None), SECRET) is not None


class TestIssuerAudienceEnforced:
    """MED-5: `iss` and `aud` are ENFORCED -- present-but-wrong AND absent are both rejected."""

    def test_wrong_issuer_rejected(self) -> None:
        assert verify_jwt_token(_sign(_payload(iss="some-other-service")), SECRET) is None

    def test_wrong_audience_rejected(self) -> None:
        assert verify_jwt_token(_sign(_payload(aud="some-other-audience")), SECRET) is None

    def test_wrong_issuer_and_audience_rejected(self) -> None:
        assert verify_jwt_token(_sign(_payload(iss="attacker", aud="attacker")), SECRET) is None

    def test_audience_list_without_ours_rejected(self) -> None:
        assert verify_jwt_token(_sign(_payload(aud=["a", "b"])), SECRET) is None

    def test_non_string_issuer_rejected(self) -> None:
        assert verify_jwt_token(_sign_raw(_payload(iss=["waddlebot"])), SECRET) is None

    def test_custom_expected_issuer_and_audience_enforced(self) -> None:
        """A verifier expecting non-default iss/aud rejects the platform default."""
        assert (
            verify_jwt_token(_sign(_payload()), SECRET, issuer="other-issuer", audience="other-audience")
            is None
        )

    def test_custom_expected_issuer_and_audience_accepted_when_matching(self) -> None:
        token = _sign(_payload(iss="other-issuer", aud="other-audience"))
        assert verify_jwt_token(token, SECRET, issuer="other-issuer", audience="other-audience") is not None

    @pytest.mark.parametrize(
        "missing",
        [("iss",), ("aud",), ("iss", "aud")],
        ids=["no-iss", "no-aud", "no-iss-no-aud"],
    )
    def test_token_missing_iss_or_aud_rejected(self, missing: tuple[str, ...]) -> None:
        """regression (MED-5): these used to verify ('missing-but-still-verifying')."""
        token = _sign(_payload(**{name: None for name in missing}))
        assert verify_jwt_token(token, SECRET) is None

    def test_null_iss_or_aud_claim_rejected(self) -> None:
        raw = _payload()
        raw["iss"] = None
        assert verify_jwt_token(_sign_raw(raw), SECRET) is None
        raw = _payload()
        raw["aud"] = None
        assert verify_jwt_token(_sign_raw(raw), SECRET) is None


class TestRequiredClaims:
    """Every claim in REQUIRED_JWT_CLAIMS is mandatory; a clean None (401), never a KeyError 500."""

    def test_required_claims_are_the_audited_set(self) -> None:
        assert set(REQUIRED_JWT_CLAIMS) == {"sub", "iss", "aud", "iat", "exp", "scope", "tenant"}

    @pytest.mark.parametrize("claim", REQUIRED_JWT_CLAIMS)
    def test_missing_required_claim_rejected(self, claim: str) -> None:
        assert verify_jwt_token(_sign(_payload(**{claim: None})), SECRET) is None

    @pytest.mark.parametrize("claim", ["sub", "tenant"])
    @pytest.mark.parametrize("empty", ["", "   "])
    def test_blank_identity_claim_rejected(self, claim: str, empty: str) -> None:
        assert verify_jwt_token(_sign(_payload(**{claim: empty})), SECRET) is None

    @pytest.mark.parametrize("claim", ["sub", "tenant"])
    def test_non_string_identity_claim_rejected(self, claim: str) -> None:
        assert verify_jwt_token(_sign_raw(_payload(**{claim: 12345})), SECRET) is None

    def test_non_string_scope_rejected(self) -> None:
        """`authz` space-splits `scope`; a list/dict scope must never reach it."""
        assert verify_jwt_token(_sign(_payload(scope=["*:admin"])), SECRET) is None

    def test_optional_claims_may_be_absent(self) -> None:
        """`teams`/`roles`/`username`/`email` are not in the required set."""
        token = _sign(_payload(teams=None, roles=None, username=None, email=None, type=None))
        assert verify_jwt_token(token, SECRET) is not None


class TestTimeValidation:
    def test_expired_token_rejected(self) -> None:
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        token = _sign(_payload(iat=past - timedelta(hours=1), exp=past))
        assert verify_jwt_token(token, SECRET) is None

    def test_exp_comparison_is_utc_correct(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """regression: the old naive-local-time compare mis-judged expiry by the UTC offset."""
        import time

        monkeypatch.setenv("TZ", "America/Los_Angeles")
        time.tzset()
        try:
            soon = datetime.now(timezone.utc) + timedelta(minutes=5)
            assert verify_jwt_token(_sign(_payload(exp=soon)), SECRET) is not None
        finally:
            monkeypatch.delenv("TZ")
            time.tzset()

    def test_iat_within_skew_accepted(self) -> None:
        near_future = datetime.now(timezone.utc) + timedelta(seconds=JWT_CLOCK_SKEW_SECONDS // 3)
        assert verify_jwt_token(_sign(_payload(iat=near_future)), SECRET) is not None

    def test_iat_far_in_future_rejected(self) -> None:
        far = datetime.now(timezone.utc) + timedelta(minutes=10)
        assert verify_jwt_token(_sign(_payload(iat=far, exp=far + timedelta(hours=1))), SECRET) is None

    def test_nbf_in_future_rejected(self) -> None:
        nbf = datetime.now(timezone.utc) + timedelta(hours=1)
        assert verify_jwt_token(_sign(_payload(nbf=nbf)), SECRET) is None

    def test_non_numeric_iat_rejected(self) -> None:
        assert verify_jwt_token(_sign_raw(_payload(iat="yesterday")), SECRET) is None


class TestSignatureAndKey:
    def test_wrong_secret_rejected(self) -> None:
        assert verify_jwt_token(_sign(_payload()), "a-different-secret-entirely-xxxxxxxxxx") is None

    def test_tampered_payload_rejected(self) -> None:
        head, body, sig = _sign(_payload()).split(".")
        forged = _sign(_payload(scope="*:admin")).split(".")[1]
        assert verify_jwt_token(f"{head}.{forged}.{sig}", SECRET) is None
        assert body != forged

    @pytest.mark.parametrize("empty", ["", None, b""])
    def test_empty_verifier_secret_refused_even_for_a_token_signed_with_it(self, empty: Any) -> None:
        """An unset secret must not turn into 'anyone can forge a token with the empty key'."""
        # PyJWT >= 2.15 refuses to sign with an empty key, so forge the HMAC by hand.
        claims = {k: int(v.timestamp()) if isinstance(v, datetime) else v for k, v in _payload().items()}
        head = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": DEFAULT_JWT_KID}).encode())
        body = _b64(json.dumps(claims).encode())
        sig = hmac.new(b"", f"{head}.{body}".encode(), hashlib.sha256).digest()
        forged = f"{head}.{body}.{_b64(sig)}"
        assert verify_jwt_token(forged, empty) is None

    @pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b", "a.b.c", "....", None, 123])
    def test_garbage_token_rejected_without_raising(self, garbage: Any) -> None:
        assert verify_jwt_token(garbage, SECRET) is None
