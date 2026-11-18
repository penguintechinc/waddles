"""Tests for self-hosted ALTCHA challenge issuance, verification, single-use and rate limiting."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import time
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.auth import ALTCHA_STORE_CONFIG_KEY, BLUEPRINTS
from services import auth_service
from services.errors import ApiError
from tests.test_v1_auth_blueprint import _test_config

KEY = "test-altcha-hmac-key"


class _FakePipeline:
    """MULTI/EXEC stand-in recording INCR + EXPIRE NX against the fake store."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.ops: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def incr(self, key: str) -> None:
        self.ops.append(("incr", (key,)))

    def expire(self, key: str, ttl: int, nx: bool = False) -> None:
        self.ops.append(("expire", (key, ttl, nx)))

    async def execute(self) -> list[int]:
        if self.store.down:
            raise ConnectionError("store down")
        out: list[int] = []
        for op, args in self.ops:
            if op == "incr":
                self.store.counters[args[0]] = self.store.counters.get(args[0], 0) + 1
                out.append(self.store.counters[args[0]])
            else:
                self.store.ttls.setdefault(args[0], args[1])
                out.append(1)
        return out


class FakeStore:
    """Shared-store double with the `SET NX EX` + pipeline semantics the code relies on."""

    def __init__(self) -> None:
        """Start empty and up."""
        self.keys: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.counters: dict[str, int] = {}
        self.down = False

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if self.down:
            raise ConnectionError("store down")
        if nx and key in self.keys:
            return False
        self.keys[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)


def _b64(data: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(data).encode("utf-8")).decode("ascii")


def _solve(challenge: dict[str, Any]) -> dict[str, Any]:
    salt, target = challenge["salt"], challenge["challenge"]
    for number in range(challenge["maxnumber"] + 1):
        if hashlib.sha256(f"{salt}{number}".encode()).hexdigest() == target:
            return {
                "algorithm": challenge["algorithm"],
                "challenge": target,
                "number": number,
                "salt": salt,
                "signature": challenge["signature"],
            }
    raise AssertionError("failed to solve test ALTCHA challenge")


def _forge(salt: str, number: int, key: str = KEY) -> dict[str, Any]:
    """A payload whose hash is valid and whose MAC is a genuine server MAC for (salt, hash)."""
    challenge = hashlib.sha256(f"{salt}{number}".encode()).hexdigest()
    return {
        "algorithm": "SHA-256",
        "challenge": challenge,
        "number": number,
        "salt": salt,
        "signature": auth_service.altcha_signature(key, salt, challenge),
    }


async def test_create_and_verify_once() -> None:
    store = FakeStore()
    challenge = auth_service.create_altcha_challenge(KEY, max_number=200)
    assert set(challenge) == {"algorithm", "challenge", "maxnumber", "salt", "signature"}
    assert challenge["maxnumber"] == 200
    token = _b64(_solve(challenge))
    assert await auth_service.verify_altcha_payload(token, KEY, store) is True
    assert await auth_service.verify_altcha_payload(token, KEY, store) is False


async def test_wrong_key_rejected_and_not_consumed() -> None:
    store = FakeStore()
    token = _b64(_solve(auth_service.create_altcha_challenge(KEY, max_number=100)))
    assert await auth_service.verify_altcha_payload(token, "other-key", store) is False
    assert store.keys == {}
    assert await auth_service.verify_altcha_payload(token, KEY, store) is True


async def test_digit_shift_expiry_extension_rejected() -> None:
    """Regression: moving number digits into the (unterminated) salt must not extend expiry."""
    store = FakeStore()
    expires = int(time.time()) + 100
    base = f"{'ab' * 12}?expires={expires}&"
    payload = _forge(base, 12345)
    shifted = dict(payload, salt=f"{'ab' * 12}?expires={expires}&1", number=2345)
    assert (
        hashlib.sha256(f"{shifted['salt']}{shifted['number']}".encode()).hexdigest()
        == (payload["challenge"])
    )
    assert await auth_service.verify_altcha_payload(_b64(shifted), KEY, store) is False
    # The old unterminated format shifted cleanly into a larger expiry; no such salt matches now.
    legacy = f"{'ab' * 12}?expires={expires}1"
    assert await auth_service.verify_altcha_payload(_b64(_forge(legacy, 2345)), KEY, store) is False


async def test_salt_without_expiry_rejected() -> None:
    store = FakeStore()
    for salt in ("ab" * 12, f"{'ab' * 12}?expires=", f"{'ab' * 12}&expires=9999999999&"):
        assert await auth_service.verify_altcha_payload(_b64(_forge(salt, 1)), KEY, store) is False


async def test_far_future_and_expired_expiry_rejected() -> None:
    store = FakeStore()
    far = int(time.time()) + 10_000_000
    assert (
        await auth_service.verify_altcha_payload(
            _b64(_forge(f"{'ab' * 12}?expires={far}&", 3)), KEY, store
        )
        is False
    )
    expired = auth_service.create_altcha_challenge(KEY, max_number=50, ttl_seconds=-10)
    assert await auth_service.verify_altcha_payload(_b64(_solve(expired)), KEY, store) is False


async def test_mac_covers_salt() -> None:
    store = FakeStore()
    challenge = auth_service.create_altcha_challenge(KEY, max_number=50)
    solved = _solve(challenge)
    other_salt = f"{'cd' * 12}?expires={int(time.time()) + 100}&"
    forged = dict(solved, salt=other_salt)
    assert await auth_service.verify_altcha_payload(_b64(forged), KEY, store) is False


async def test_replay_survives_in_process_state_clear() -> None:
    """Single-use lives in the shared store, not process memory."""
    store = FakeStore()
    token = _b64(_solve(auth_service.create_altcha_challenge(KEY, max_number=100)))
    assert await auth_service.verify_altcha_payload(token, KEY, store) is True
    # A fresh "replica" sharing only the store still rejects it.
    assert await auth_service.verify_altcha_payload(token, KEY, store) is False
    (key,) = store.keys
    assert store.ttls[key] >= auth_service._ALTCHA_TTL_SECONDS - 5  # covers remaining validity


async def test_store_down_fails_closed() -> None:
    store = FakeStore()
    token = _b64(_solve(auth_service.create_altcha_challenge(KEY, max_number=100)))
    store.down = True
    with pytest.raises(ApiError) as exc:
        await auth_service.verify_altcha_payload(token, KEY, store)
    assert exc.value.status_code == 503
    with pytest.raises(ApiError) as exc2:
        await auth_service.validate_captcha("altcha", KEY, token, None, None)
    assert exc2.value.status_code == 503


async def test_unset_secret_fails_closed() -> None:
    with pytest.raises(ApiError) as exc:
        auth_service.create_altcha_challenge("")
    assert exc.value.status_code == 503
    with pytest.raises(ApiError) as exc2:
        await auth_service.verify_altcha_payload("x", "", FakeStore())
    assert exc2.value.status_code == 503


async def test_garbage_and_out_of_range_payloads_rejected() -> None:
    store = FakeStore()
    challenge = auth_service.create_altcha_challenge(KEY, max_number=50)
    solved = _solve(challenge)
    for bad in (
        "not-base64!!!",
        "",
        base64.b64encode(b"\xff\xfe").decode(),
        base64.b64encode(b"[1,2]").decode(),
        _b64(dict(solved, signature="é" * 8)),
        _b64(dict(solved, challenge="٣" * 64)),
        _b64(dict(solved, salt="٣٣")),
        _b64(dict(solved, number=-1)),
        _b64(dict(solved, number=True)),
        _b64(dict(solved, number=auth_service._ALTCHA_MAX_NUMBER + 1)),
        _b64(dict(solved, number=10**40)),
        _b64(dict(solved, algorithm="MD5")),
    ):
        assert await auth_service.verify_altcha_payload(bad, KEY, store) is False
    assert store.keys == {}


async def test_validate_captcha_altcha_dispatch() -> None:
    store = FakeStore()
    token = _b64(_solve(auth_service.create_altcha_challenge(KEY, max_number=100)))
    assert await auth_service.validate_captcha("altcha", KEY, token, None, store) is True
    assert await auth_service.validate_captcha("altcha", KEY, "invalid", None, store) is False


def _app(auth_db: Any, store: FakeStore | None, key: str = KEY) -> Quart:
    app = Quart(__name__)
    QuartSchema(app)
    app.config["async_dal"] = auth_db
    app.config["dal"] = auth_db.dal
    app.config["HUB_API_CONFIG"] = dataclasses.replace(_test_config(), altcha_hmac_key=key)
    if store is not None:
        app.config[ALTCHA_STORE_CONFIG_KEY] = store
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    return app


async def test_endpoint_end_to_end_flow(auth_db: Any) -> None:
    store = FakeStore()
    client = _app(auth_db, store).test_client()
    response = await client.get("/api/v1/captcha/challenge")
    assert response.status_code == 200
    body = await response.get_json()
    assert set(body) == {"algorithm", "challenge", "maxnumber", "salt", "signature"}
    assert body["algorithm"] == "SHA-256"
    assert body["salt"].endswith("&") and "?expires=" in body["salt"]
    token = _b64(_solve(body))
    assert await auth_service.verify_altcha_payload(token, KEY, store) is True
    assert await auth_service.verify_altcha_payload(token, KEY, store) is False


async def test_endpoint_rate_limited_per_ip(auth_db: Any) -> None:
    store = FakeStore()
    client = _app(auth_db, store).test_client()
    limit = auth_service.ALTCHA_CHALLENGE_RATE_LIMIT
    for _ in range(limit):
        assert (await client.get("/api/v1/captcha/challenge")).status_code == 200
    blocked = await client.get("/api/v1/captcha/challenge")
    assert blocked.status_code == 429
    assert (await blocked.get_json())["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert len(store.counters) == 1


async def test_endpoint_fails_closed(auth_db: Any) -> None:
    down = FakeStore()
    down.down = True
    for app in (_app(auth_db, down), _app(auth_db, None), _app(auth_db, FakeStore(), key="")):
        response = await app.test_client().get("/api/v1/captcha/challenge")
        assert response.status_code == 503
