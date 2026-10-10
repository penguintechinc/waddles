"""
H-2 Phase 0 JWT hardening: attack-shaped tests against the real verifiers.

Every test here forges the token the way an attacker would (hand-built
headers, hand-computed HMACs, real RSA/Ed25519 keys) and runs it through the
production `verify_jwt_token` / `inspect_header` / metric code -- nothing is
mocked except the OTel exporter, which is replaced by an in-memory reader so
emission can be counted (a zero count is a failure, never a skip).

Covers RFC 8725: one-alg-per-verifier (3.1/3.2), `alg: none`, key-material
headers (`jku`/`jwk`/`x5u`/`x5c`/`crit`), `kid` hygiene, plus the
per-algorithm verification metric and PII-free logging.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from flask_core import jwt_hardening as hardening
from flask_core.auth import (
    DEFAULT_JWT_AUDIENCE,
    DEFAULT_JWT_ISSUER,
    DEFAULT_JWT_KID,
    PLATFORM_JWT_ALGORITHM,
    verify_jwt_token,
)
from flask_core.jwt_hardening import (
    ALG_LABEL_ABSENT,
    ALG_LABEL_NONE,
    ALG_LABEL_OTHER,
    FORBIDDEN_HEADER_PARAMS,
    JwtRejection,
    alg_label,
    classify_decode_error,
    inspect_header,
    is_valid_kid,
)

SECRET = "test-secret-key-not-for-production-use-only"
SENTINEL_USER = "sentinel-user-9f3a1c"
SENTINEL_EMAIL = "sentinel-9f3a1c@example.invalid"
HS256 = ("HS256",)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _claims(**overrides: Any) -> dict[str, Any]:
    """Valid platform claims as JSON-ready ints; a None override drops that claim."""
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": "u1",
        "username": SENTINEL_USER,
        "email": SENTINEL_EMAIL,
        "roles": [],
        "tenant": "global",
        "scope": "community:read",
        "teams": [],
        "iss": DEFAULT_JWT_ISSUER,
        "aud": DEFAULT_JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _forge(
    header: dict[str, Any],
    claims: dict[str, Any],
    *,
    signature: bytes | None = None,
    key: bytes | str = SECRET,
) -> str:
    """Hand-assemble a compact JWT: arbitrary header, HMAC-SHA256 (or caller) signature."""
    head = _b64(json.dumps(header).encode())
    body = _b64(json.dumps(claims).encode())
    if signature is None:
        raw_key = key.encode() if isinstance(key, str) else key
        signature = hmac.new(raw_key, f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(signature)}"


def _valid_header(**extra: Any) -> dict[str, Any]:
    return {"alg": "HS256", "typ": "JWT", "kid": DEFAULT_JWT_KID, **extra}


# --------------------------------------------------------------------------
# alg_label / kid
# --------------------------------------------------------------------------


class TestAlgLabel:
    @pytest.mark.parametrize(
        ("raw", "label"),
        [
            ("HS256", "hs256"),
            ("hs256", "hs256"),
            ("EdDSA", "eddsa"),
            ("ES256", "es256"),
            ("none", ALG_LABEL_NONE),
            (None, ALG_LABEL_ABSENT),
            (123, ALG_LABEL_OTHER),
            (["HS256"], ALG_LABEL_OTHER),
            ("totally-made-up", ALG_LABEL_OTHER),
            ("A" * 5000, ALG_LABEL_OTHER),
        ],
    )
    def test_label_is_bounded(self, raw: object, label: str) -> None:
        assert alg_label(raw) == label

    def test_label_is_idempotent(self) -> None:
        for raw in ("HS256", "none", None, "junk"):
            assert alg_label(alg_label(raw)) == alg_label(raw)


class TestKid:
    @pytest.mark.parametrize("kid", ["hs256-v1", "k1", "waddlebot1", "a.b:c_d-e", "A" * 64, "_x"])
    def test_valid(self, kid: str) -> None:
        assert is_valid_kid(kid)

    @pytest.mark.parametrize(
        "kid",
        [
            "",
            "-lead",
            ".lead",
            "a b",
            "a/b",
            "../etc/passwd",
            "a;b",
            "x" * 65,
            "k\n1",
            "k\x00",
            "é",
            None,
            7,
        ],
    )
    def test_invalid(self, kid: object) -> None:
        assert not is_valid_kid(kid)


# --------------------------------------------------------------------------
# inspect_header
# --------------------------------------------------------------------------


class TestInspectHeader:
    def test_valid_header_returns_vetted_subset(self) -> None:
        header = inspect_header(
            _forge(_valid_header(), _claims()), allowed_algs=HS256, validate_kid=True
        )
        assert (header.alg, header.kid) == ("HS256", DEFAULT_JWT_KID)

    def test_missing_kid_is_allowed_unless_required(self) -> None:
        token = _forge({"alg": "HS256"}, _claims())
        assert inspect_header(token, allowed_algs=HS256, validate_kid=True).kid is None
        with pytest.raises(JwtRejection) as exc:
            inspect_header(token, allowed_algs=HS256, require_kid=True)
        assert exc.value.reason == hardening.REASON_BAD_KID

    @pytest.mark.parametrize("alg", ["none", "None", "NONE", "nOnE"])
    def test_alg_none_any_case_is_rejected_first(self, alg: str) -> None:
        token = _forge({"alg": alg, "typ": "JWT"}, _claims(), signature=b"")
        with pytest.raises(JwtRejection) as exc:
            inspect_header(token, allowed_algs=HS256)
        assert exc.value.reason == hardening.REASON_ALG_NONE
        assert exc.value.alg == ALG_LABEL_NONE

    def test_alg_none_wins_over_a_forbidden_param(self) -> None:
        token = _forge(
            {"alg": "none", "jku": "https://evil.example/jwks"}, _claims(), signature=b""
        )
        with pytest.raises(JwtRejection) as exc:
            inspect_header(token, allowed_algs=HS256)
        assert exc.value.reason == hardening.REASON_ALG_NONE

    @pytest.mark.parametrize("param", sorted(FORBIDDEN_HEADER_PARAMS))
    def test_key_material_params_rejected_even_with_the_right_alg(self, param: str) -> None:
        token = _forge(_valid_header(**{param: "attacker-controlled"}), _claims())
        with pytest.raises(JwtRejection) as exc:
            inspect_header(token, allowed_algs=HS256, validate_kid=True)
        assert exc.value.reason == hardening.REASON_FORBIDDEN_HEADER
        assert exc.value.alg == "hs256"

    def test_forbidden_param_set_is_the_audited_one(self) -> None:
        assert (
            {"jku", "jwk", "x5u"} <= FORBIDDEN_HEADER_PARAMS == {"jku", "jwk", "x5u", "x5c", "crit"}
        )

    @pytest.mark.parametrize(
        "alg", ["HS384", "HS512", "RS256", "ES256", "EdDSA", "PS256", "hs256", "HS256 ", ""]
    )
    def test_any_other_alg_rejected_one_alg_per_verifier(self, alg: str) -> None:
        with pytest.raises(JwtRejection) as exc:
            inspect_header(_forge({"alg": alg}, _claims()), allowed_algs=HS256)
        assert exc.value.reason == hardening.REASON_ALG_MISMATCH

    @pytest.mark.parametrize("alg", [None, 256, ["HS256"], {"a": 1}])
    def test_absent_or_non_string_alg_rejected(self, alg: object) -> None:
        header: dict[str, Any] = {} if alg is None else {"alg": alg}
        with pytest.raises(JwtRejection) as exc:
            inspect_header(_forge(header, _claims()), allowed_algs=HS256)
        assert exc.value.reason == hardening.REASON_ALG_MISMATCH

    def test_allow_list_may_hold_several_algs_for_an_asymmetric_verifier(self) -> None:
        token = _forge({"alg": "ES256", "kid": "k"}, _claims())
        assert inspect_header(token, allowed_algs=("RS256", "ES256")).alg == "ES256"

    @pytest.mark.parametrize("bad_kid", ["../../dev/null", "a b", "x" * 65, "k;DROP", ""])
    def test_hostile_kid_rejected_when_validated(self, bad_kid: str) -> None:
        token = _forge(_valid_header(kid=bad_kid), _claims())
        with pytest.raises(JwtRejection) as exc:
            inspect_header(token, allowed_algs=HS256, validate_kid=True)
        assert exc.value.reason == hardening.REASON_BAD_KID

    def test_non_string_kid_rejected_when_validated(self) -> None:
        token = _forge(_valid_header(kid=["a"]), _claims())
        with pytest.raises(JwtRejection) as exc:
            inspect_header(token, allowed_algs=HS256, validate_kid=True)
        assert exc.value.reason == hardening.REASON_BAD_KID

    def test_kid_charset_is_not_enforced_for_external_idps(self) -> None:
        """OIDC IdPs use arbitrary kid strings; `validate_kid` is opt-in per verifier."""
        token = _forge(_valid_header(kid="https://idp.example/keys/1?v=2"), _claims())
        header = inspect_header(token, allowed_algs=HS256)
        assert header.kid == "https://idp.example/keys/1?v=2"

    def test_non_string_kid_is_dropped_when_not_validated(self) -> None:
        token = _forge(_valid_header(kid=42), _claims())
        assert inspect_header(token, allowed_algs=HS256).kid is None

    @pytest.mark.parametrize(
        "junk", ["", "garbage", "a.b", "a.b.c", "....", None, 7, b"x.y.z", ["a.b.c"]]
    )
    def test_malformed_tokens(self, junk: object) -> None:
        with pytest.raises(JwtRejection) as exc:
            inspect_header(junk, allowed_algs=HS256)
        assert exc.value.reason == hardening.REASON_MALFORMED
        assert exc.value.alg == ALG_LABEL_ABSENT

    def test_deeply_nested_header_json_is_malformed_not_a_crash(self) -> None:
        nested = b"[" * 9000 + b"]" * 9000
        token = f"{_b64(nested)}.{_b64(b'{}')}.{_b64(b'sig')}"
        with pytest.raises(JwtRejection) as exc:
            inspect_header(token, allowed_algs=HS256)
        assert exc.value.reason == hardening.REASON_MALFORMED

    def test_oversized_header_segment_refused_before_parsing(self) -> None:
        huge = {"alg": "HS256", "x5c": ["A" * 20000]}
        with pytest.raises(JwtRejection) as exc:
            inspect_header(_forge(huge, _claims()), allowed_algs=HS256)
        assert exc.value.reason == hardening.REASON_MALFORMED

    @pytest.mark.parametrize(
        "token", ["a.b", "a.b.c.d", ".b.c", "!!!.b.c", "e30.b.c.", "\u00e9.b.c"]
    )
    def test_wrong_segment_shape_or_undecodable_header_is_malformed(self, token: str) -> None:
        with pytest.raises(JwtRejection) as exc:
            inspect_header(token, allowed_algs=HS256)
        assert exc.value.reason == hardening.REASON_MALFORMED

    def test_non_utf8_header_is_malformed(self) -> None:
        token = f"{_b64(b'\xff\xfe{{')}.{_b64(b'{{}}')}.{_b64(b'sig')}"
        with pytest.raises(JwtRejection) as exc:
            inspect_header(token, allowed_algs=HS256)
        assert exc.value.reason == hardening.REASON_MALFORMED

    def test_header_that_is_json_but_not_an_object_is_malformed(self) -> None:
        token = f"{_b64(b'[1,2,3]')}.{_b64(b'{}')}.{_b64(b'sig')}"
        with pytest.raises(JwtRejection) as exc:
            inspect_header(token, allowed_algs=HS256)
        assert exc.value.reason == hardening.REASON_MALFORMED

    def test_rejection_text_is_the_reason_only(self) -> None:
        """str(exc) can never carry header values, so a handler that stringifies it stays clean."""
        token = _forge(_valid_header(jku="https://evil.example/SECRET-PATH"), _claims())
        with pytest.raises(JwtRejection) as exc:
            inspect_header(token, allowed_algs=HS256)
        assert str(exc.value) == hardening.REASON_FORBIDDEN_HEADER
        assert "evil.example" not in repr(exc.value)


# --------------------------------------------------------------------------
# attacks against the real platform verifier
# --------------------------------------------------------------------------


class TestPlatformVerifierAttacks:
    def test_control_a_hand_forged_valid_token_verifies(self) -> None:
        """Without this, every rejection below could be passing for the wrong reason."""
        assert verify_jwt_token(_forge(_valid_header(), _claims()), SECRET) is not None

    @pytest.mark.parametrize("alg", ["none", "None", "NONE", "nOnE"])
    def test_alg_none_unsigned_token_rejected(self, alg: str, metrics_capture: Any) -> None:
        token = _forge({"alg": alg, "typ": "JWT"}, _claims(), signature=b"")
        assert token.endswith(".")
        assert verify_jwt_token(token, SECRET) is None
        assert ("platform_hs256", "none", "alg_none") in metrics_capture.verifications()

    def test_alg_none_with_garbage_signature_rejected(self) -> None:
        assert (
            verify_jwt_token(_forge({"alg": "none"}, _claims(), signature=b"anything"), SECRET)
            is None
        )

    def test_alg_none_via_pyjwt_encode_rejected(self) -> None:
        unsigned = jwt.encode(_claims(), key=None, algorithm="none")  # type: ignore[arg-type]
        assert verify_jwt_token(unsigned, SECRET) is None

    def test_other_hmac_variant_with_the_correct_secret_rejected(
        self, metrics_capture: Any
    ) -> None:
        """HS512 signed with the REAL secret is a valid signature -- only the alg pin refuses it."""
        token = jwt.encode(_claims(), SECRET, algorithm="HS512", headers={"kid": DEFAULT_JWT_KID})
        assert verify_jwt_token(token, SECRET) is None
        assert ("platform_hs256", "hs512", "alg_mismatch") in metrics_capture.verifications()

    def test_rs256_token_rejected(self, metrics_capture: Any) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode(_claims(), key, algorithm="RS256", headers={"kid": DEFAULT_JWT_KID})
        assert verify_jwt_token(token, SECRET) is None
        assert ("platform_hs256", "rs256", "alg_mismatch") in metrics_capture.verifications()

    def test_es256_token_rejected(self) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        assert verify_jwt_token(jwt.encode(_claims(), key, algorithm="ES256"), SECRET) is None

    def test_classic_alg_confusion_hmac_keyed_with_a_public_key_pem(self) -> None:
        """RSA public key PEM used as the HMAC secret (the textbook confusion attack).

        Two layers refuse it: the alg pin (the RS256 verifier case) and PyJWT's refusal to HMAC with
        a PEM. Here the *verifier* is (mis)configured with the PEM as its secret and the attacker
        signs HS256 with those same bytes -- the one shape the alg pin alone would accept.
        """
        public_pem = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
        forged = _forge(_valid_header(), _claims(), key=public_pem)
        assert verify_jwt_token(forged, public_pem.decode()) is None

    @pytest.mark.parametrize("param", sorted(FORBIDDEN_HEADER_PARAMS))
    def test_key_material_header_rejected_despite_a_valid_signature(
        self, param: str, metrics_capture: Any
    ) -> None:
        token = _forge(_valid_header(**{param: "https://evil.example/jwks.json"}), _claims())
        assert verify_jwt_token(token, SECRET) is None
        assert ("platform_hs256", "hs256", "forbidden_header") in metrics_capture.verifications()

    def test_embedded_jwk_signed_by_attacker_key_rejected(self) -> None:
        """Classic `jwk` injection: self-signed RS256 token carrying its own public key."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        token = jwt.encode(
            _claims(), key, algorithm="RS256", headers={"jwk": jwk, "kid": DEFAULT_JWT_KID}
        )
        assert verify_jwt_token(token, SECRET) is None

    @pytest.mark.parametrize("bad_kid", ["../../../dev/null", "k' OR '1'='1", "x" * 200])
    def test_hostile_kid_rejected_before_any_use(self, bad_kid: str, metrics_capture: Any) -> None:
        token = _forge(_valid_header(kid=bad_kid), _claims())
        assert verify_jwt_token(token, SECRET) is None
        assert ("platform_hs256", "hs256", "bad_kid") in metrics_capture.verifications()

    def test_forged_signature_rejected(self, metrics_capture: Any) -> None:
        token = _forge(_valid_header(), _claims(), key="not-the-real-secret-at-all-xxxxxxxxxxxx")
        assert verify_jwt_token(token, SECRET) is None
        assert ("platform_hs256", "hs256", "bad_signature") in metrics_capture.verifications()

    def test_privilege_escalation_via_claim_tampering_rejected(self) -> None:
        good = _forge(_valid_header(), _claims())
        head, _body, sig = good.split(".")
        evil_body = _b64(json.dumps(_claims(scope="*:admin")).encode())
        assert verify_jwt_token(f"{head}.{evil_body}.{sig}", SECRET) is None


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------


class TestVerificationMetrics:
    def test_success_is_counted_per_algorithm(self, metrics_capture: Any) -> None:
        assert verify_jwt_token(_forge(_valid_header(), _claims()), SECRET) is not None
        points = metrics_capture.verifications()
        assert points  # a zero denominator is a failure, not a pass
        assert points[("platform_hs256", "hs256", "ok")] == 1
        assert metrics_capture.duration_counts()[("platform_hs256", "hs256")] == 1

    def test_counts_accumulate_across_outcomes(self, metrics_capture: Any) -> None:
        good = _forge(_valid_header(), _claims())
        for _ in range(3):
            assert verify_jwt_token(good, SECRET) is not None
        assert verify_jwt_token(_forge({"alg": "none"}, _claims(), signature=b""), SECRET) is None
        assert verify_jwt_token("garbage", SECRET) is None
        points = metrics_capture.verifications()
        assert points[("platform_hs256", "hs256", "ok")] == 3
        assert points[("platform_hs256", "none", "alg_none")] == 1
        assert points[("platform_hs256", "absent", "malformed")] == 1
        assert sum(points.values()) == 5

    @pytest.mark.parametrize(
        ("claims", "outcome"),
        [
            (_claims(iss="someone-else"), "bad_issuer"),
            (_claims(aud="someone-else"), "bad_audience"),
            (_claims(iss=None), "missing_claim"),
            (_claims(aud=None), "missing_claim"),
            (_claims(tenant=None), "missing_claim"),
            (_claims(tenant=""), "invalid_claim"),
            (_claims(exp=1), "expired"),
            (_claims(iat=int(datetime.now(UTC).timestamp()) + 3600), "immature"),
            (_claims(sub=7), "invalid_claim"),
        ],
        ids=[
            "iss",
            "aud",
            "no-iss",
            "no-aud",
            "no-tenant",
            "blank-tenant",
            "expired",
            "iat-future",
            "int-sub",
        ],
    )
    def test_each_rejection_has_its_own_outcome(
        self, claims: dict[str, Any], outcome: str, metrics_capture: Any
    ) -> None:
        assert verify_jwt_token(_forge(_valid_header(), claims), SECRET) is None
        assert ("platform_hs256", "hs256", outcome) in metrics_capture.verifications()

    def test_attacker_chosen_alg_cannot_mint_label_values(self, metrics_capture: Any) -> None:
        for i in range(25):
            verify_jwt_token(_forge({"alg": f"X-{i}-{'A' * 40}"}, _claims()), SECRET)
        labels = {alg for (_v, alg, _o) in metrics_capture.verifications()}
        assert labels == {ALG_LABEL_OTHER}

    def test_empty_secret_is_counted_as_no_key(self, metrics_capture: Any) -> None:
        assert verify_jwt_token(_forge(_valid_header(), _claims()), "") is None
        assert ("platform_hs256", "absent", "no_key") in metrics_capture.verifications()

    def test_dead_exporter_never_changes_the_verdict(
        self,
        metrics_capture: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class _Boom:
            def add(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("exporter down: token=SHOULD-NOT-LEAK")

            def record(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("exporter down")

        monkeypatch.setattr(hardening._instruments, "verifications", _Boom())
        monkeypatch.setattr(hardening._instruments, "duration", _Boom())
        good = _forge(_valid_header(), _claims())
        with caplog.at_level(logging.WARNING, logger="flask_core.jwt_hardening"):
            assert verify_jwt_token(good, SECRET) is not None  # success survives a dead exporter
            assert (
                verify_jwt_token(_forge({"alg": "none"}, _claims(), signature=b""), SECRET) is None
            )  # so does denial
        warnings = [r for r in caplog.records if "metric emit failed" in r.getMessage()]
        assert warnings, "a metric failure must be logged, not silently swallowed"
        assert "SHOULD-NOT-LEAK" not in caplog.text  # only the exception TYPE is logged


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------


class TestRejectionLogging:
    def _verify_logged(self, token: str, caplog: pytest.LogCaptureFixture) -> str:
        with caplog.at_level(logging.DEBUG, logger="flask_core"):
            assert verify_jwt_token(token, SECRET) is None
        return " | ".join(
            f"{r.levelname} {r.getMessage()} {sorted(r.__dict__.items())}" for r in caplog.records
        )

    def test_logs_never_contain_token_claims_or_secret(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        token = _forge(
            _valid_header(),
            _claims(iss="attacker-issuer-7731"),
            key="wrong-secret-yyyyyyyyyyyyyyyyyyyy",
        )
        rendered = self._verify_logged(token, caplog)
        for forbidden in (
            token,
            SENTINEL_USER,
            SENTINEL_EMAIL,
            "attacker-issuer-7731",
            SECRET,
            token.split(".")[1],
        ):
            assert forbidden not in rendered
        assert "reason=bad_signature" in rendered

    def test_header_attack_logs_only_the_closed_vocabulary(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        token = _forge(_valid_header(jku="https://evil.example/SECRET-PATH-31337"), _claims())
        rendered = self._verify_logged(token, caplog)
        assert "SECRET-PATH-31337" not in rendered and "evil.example" not in rendered
        assert "reason=forbidden_header" in rendered and "alg=hs256" in rendered

    @pytest.mark.parametrize(
        ("token_factory", "level", "reason"),
        [
            (lambda: _forge({"alg": "none"}, _claims(), signature=b""), "ERROR", "alg_none"),
            (lambda: _forge(_valid_header(), _claims(aud="x")), "ERROR", "bad_audience"),
            (lambda: _forge(_valid_header(), _claims(exp=1)), "WARNING", "expired"),
            (lambda: "garbage", "WARNING", "malformed"),
        ],
        ids=["alg-none-error", "bad-aud-error", "expired-warning", "malformed-warning"],
    )
    def test_severity_matches_the_threat(
        self, token_factory: Any, level: str, reason: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._verify_logged(token_factory(), caplog)
        rejected = [r for r in caplog.records if "JWT rejected" in r.getMessage()]
        assert len(rejected) == 1
        assert (rejected[0].levelname, rejected[0].reason) == (level, reason)  # type: ignore[attr-defined]

    def test_unset_secret_is_critical(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger="flask_core"):
            assert verify_jwt_token(_forge(_valid_header(), _claims()), "") is None
        assert [r.levelname for r in caplog.records if "JWT rejected" in r.getMessage()] == [
            "CRITICAL"
        ]

    def test_success_logs_at_debug_only(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger="flask_core"):
            assert verify_jwt_token(_forge(_valid_header(), _claims()), SECRET) is not None
        verified = [r for r in caplog.records if "JWT verified" in r.getMessage()]
        assert [r.levelname for r in verified] == ["DEBUG"]
        assert SENTINEL_USER not in caplog.text

    def test_every_rejection_is_stamped_for_the_audit_pipeline(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._verify_logged("garbage", caplog)
        record = next(r for r in caplog.records if "JWT rejected" in r.getMessage())
        assert (record.event_type, record.result) == ("AUTH", "FAILURE")  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# classifier / rejection plumbing
# --------------------------------------------------------------------------


class TestClassifyDecodeError:
    @pytest.mark.parametrize(
        ("exc", "reason"),
        [
            (jwt.ExpiredSignatureError("x"), "expired"),
            (jwt.ImmatureSignatureError("x"), "immature"),
            (jwt.InvalidSignatureError("x"), "bad_signature"),
            (jwt.InvalidIssuerError("x"), "bad_issuer"),
            (jwt.InvalidAudienceError("x"), "bad_audience"),
            (jwt.MissingRequiredClaimError("sub"), "missing_claim"),
            (jwt.exceptions.InvalidIssuedAtError("x"), "invalid_claim"),
            (jwt.exceptions.InvalidSubjectError("x"), "invalid_claim"),
            (jwt.exceptions.InvalidJTIError("x"), "invalid_claim"),
            (jwt.InvalidAlgorithmError("x"), "alg_mismatch"),
            (jwt.DecodeError("x"), "malformed"),
            (jwt.InvalidKeyError("x"), "invalid"),
            (jwt.InvalidTokenError("x"), "invalid"),
            (jwt.PyJWTError("x"), "invalid"),
        ],
    )
    def test_mapping(self, exc: jwt.PyJWTError, reason: str) -> None:
        assert classify_decode_error(exc) == reason

    def test_every_outcome_is_in_the_closed_vocabulary(self) -> None:
        reasons = {v for k, v in vars(hardening).items() if k.startswith(("REASON_", "OUTCOME_"))}
        for exc in (
            jwt.ExpiredSignatureError(),
            jwt.DecodeError(),
            jwt.PyJWTError(),
            jwt.InvalidAlgorithmError(),
        ):
            assert classify_decode_error(exc) in reasons


class TestJwtRejection:
    def test_alg_is_normalised_to_a_label(self) -> None:
        assert JwtRejection("x", alg="HS256").alg == "hs256"
        assert JwtRejection("x", alg="whatever").alg == ALG_LABEL_OTHER
        assert JwtRejection("x").alg == ALG_LABEL_ABSENT


class TestPlatformAlgorithmIsPinned:
    def test_single_algorithm_constant(self) -> None:
        assert PLATFORM_JWT_ALGORITHM == "HS256"


class TestJwtKidEnvValidation:
    """A malformed JWT_KID must stop the service at import, not mint unverifiable tokens."""

    def _import_with(self, kid: str | None) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k != "JWT_KID"}
        if kid is not None:
            env["JWT_KID"] = kid
        pkg_parent = str(Path(hardening.__file__).resolve().parent.parent)
        return subprocess.run(  # noqa: S603 - fixed argv, test-controlled env
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "import flask_core.auth as a; print(a.DEFAULT_JWT_KID)",
                pkg_parent,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def test_default_kid_when_unset(self) -> None:
        proc = self._import_with(None)
        assert proc.returncode == 0, proc.stderr[-400:]
        assert proc.stdout.strip().splitlines()[-1] == "hs256-v1"

    def test_custom_valid_kid_honoured(self) -> None:
        proc = self._import_with("hs256-v7")
        assert proc.returncode == 0, proc.stderr[-400:]
        assert proc.stdout.strip().splitlines()[-1] == "hs256-v7"

    def test_blank_kid_is_treated_as_unset(self) -> None:
        """A blank Helm value must not crash every service at import."""
        proc = self._import_with("")
        assert proc.returncode == 0, proc.stderr[-400:]
        assert proc.stdout.strip().splitlines()[-1] == "hs256-v1"

    @pytest.mark.parametrize("bad", ["has space", "../x", "x" * 65])
    def test_malformed_kid_fails_the_import_loudly(self, bad: str) -> None:
        proc = self._import_with(bad)
        assert proc.returncode != 0
        assert "JWT_KID must match" in proc.stderr
