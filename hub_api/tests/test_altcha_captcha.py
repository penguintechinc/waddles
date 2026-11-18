"""Tests for self-hosted ALTCHA challenge issuance and verification."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

from services import auth_service


def _solve_challenge(challenge: dict[str, Any]) -> str:
    salt = challenge["salt"]
    target = challenge["challenge"]
    for number in range(challenge["maxnumber"] + 1):
        if hashlib.sha256(f"{salt}{number}".encode()).hexdigest() == target:
            payload = {
                "algorithm": challenge["algorithm"],
                "challenge": target,
                "number": number,
                "salt": salt,
                "signature": challenge["signature"],
            }
            return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    raise AssertionError("failed to solve test ALTCHA challenge")


def test_create_and_verify_altcha_challenge() -> None:
    hmac_key = "test-altcha-hmac-key"
    challenge = auth_service.create_altcha_challenge(hmac_key, max_number=200)
    assert set(challenge.keys()) == {"algorithm", "challenge", "maxnumber", "salt", "signature"}
    assert challenge["algorithm"] == "SHA-256"
    assert challenge["maxnumber"] == 200

    token = _solve_challenge(challenge)
    assert auth_service.verify_altcha_payload(token, hmac_key) is True
    assert auth_service.verify_altcha_payload(token, "wrong-key") is False


def test_verify_altcha_rejects_tampered_and_expired_payloads() -> None:
    hmac_key = "test-altcha-hmac-key"
    challenge = auth_service.create_altcha_challenge(hmac_key, max_number=200)
    token = _solve_challenge(challenge)
    decoded = json.loads(base64.b64decode(token).decode("utf-8"))

    # Wrong number
    bad_number = {**decoded, "number": decoded["number"] + 1}
    assert (
        auth_service.verify_altcha_payload(
            base64.b64encode(json.dumps(bad_number).encode()).decode("ascii"), hmac_key
        )
        is False
    )

    # Expired challenge
    expired_challenge = auth_service.create_altcha_challenge(
        hmac_key, max_number=100, ttl_seconds=-10
    )
    expired_token = _solve_challenge(expired_challenge)
    assert auth_service.verify_altcha_payload(expired_token, hmac_key) is False

    # Malformed inputs
    assert auth_service.verify_altcha_payload("not-base64!!!", hmac_key) is False
    assert auth_service.verify_altcha_payload("", hmac_key) is False
    assert auth_service.verify_altcha_payload(token, "") is False


async def test_validate_captcha_altcha_dispatch() -> None:
    hmac_key = "test-altcha-hmac-key"
    challenge = auth_service.create_altcha_challenge(hmac_key, max_number=200)
    token = _solve_challenge(challenge)
    assert await auth_service.validate_captcha("altcha", hmac_key, token, None) is True
    assert await auth_service.validate_captcha("altcha", hmac_key, "invalid", None) is False


async def test_captcha_challenge_endpoint(auth_db: Any) -> None:
    from quart import Quart
    from quart_schema import QuartSchema

    from blueprints.v1.auth import BLUEPRINTS
    from tests.test_v1_auth_blueprint import _test_config

    app = Quart(__name__)
    QuartSchema(app)
    app.config["async_dal"] = auth_db
    app.config["dal"] = auth_db.dal
    app.config["HUB_API_CONFIG"] = _test_config()
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)

    client = app.test_client()
    response = await client.get("/api/v1/captcha/challenge")
    assert response.status_code == 200
    body = await response.get_json()
    assert set(body.keys()) == {"algorithm", "challenge", "maxnumber", "salt", "signature"}
    assert body["algorithm"] == "SHA-256"
    assert isinstance(body["maxnumber"], int) and body["maxnumber"] > 0
    assert "?expires=" in body["salt"]
    assert int(body["salt"].rsplit("?expires=", 1)[1]) > int(time.time())
