"""`create_jwt_token` carries the optional OIDC step-up claims (`amr`, `auth_time`) -- and only then."""

from __future__ import annotations

from flask_core.auth import create_jwt_token, verify_jwt_token

SECRET = "x" * 48  # throwaway test key


def _mint(**extra: object) -> dict[str, object]:
    token = create_jwt_token(
        user_id="1",
        username="u",
        email="u@example.invalid",
        roles=["viewer"],
        secret_key=SECRET,
        tenant="system",
        **extra,  # type: ignore[arg-type]
    )
    payload = verify_jwt_token(token, SECRET)
    assert payload is not None
    return payload


def test_claims_are_absent_by_default() -> None:
    payload = _mint()
    assert "amr" not in payload and "auth_time" not in payload


def test_claims_round_trip_when_given() -> None:
    payload = _mint(amr=["pwd", "otp"], auth_time=1_700_000_000)
    assert payload["amr"] == ["pwd", "otp"] and payload["auth_time"] == 1_700_000_000
