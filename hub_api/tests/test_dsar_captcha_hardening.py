"""Regression tests for the adversarial-review fixes on the public DSAR captcha/limiter path.

Covers: altcha salt/number digit-shift forgery + replay-after-eviction, reCAPTCHA/Turnstile
action/hostname/score binding, rate limiter + replay store fail-closed on backend error (and a
local fallback that never `clear()`s), and per-IP limits on the challenge/config endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from quart import Quart

from services import captcha_service as captcha
from services import dsar_rate_limit as limiter
from services.errors import ApiError

ALTCHA_KEY = "k" * 40


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DSAR_CAPTCHA_PROVIDER", "altcha")
    monkeypatch.setenv("DSAR_ALTCHA_HMAC_KEY", ALTCHA_KEY)
    monkeypatch.setenv("DSAR_VERIFY_URL_BASE", "https://app.example/privacy/verify")
    for name in (
        "DSAR_CAPTCHA_SECRET",
        "DSAR_CAPTCHA_SITE_KEY",
        "DSAR_CAPTCHA_HOSTNAMES",
        "DSAR_CAPTCHA_ACTION",
        "DSAR_RECAPTCHA_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)
    limiter.reset_local()


def _solve(ch: dict[str, Any]) -> int:
    for n in range(ch["maxnumber"] + 1):
        if hashlib.sha256(f"{ch['salt']}{n}".encode()).hexdigest() == ch["challenge"]:
            return n
    raise AssertionError("unsolvable")


def _token(**payload: Any) -> str:
    return base64.b64encode(json.dumps({"algorithm": "SHA-256", **payload}).encode()).decode()


class MemGuard:
    """In-memory replay store that never forgets (the shared-store contract)."""

    def __init__(self) -> None:  # noqa: D107
        self.seen: dict[str, int] = {}

    async def __call__(self, challenge: str, ttl: int) -> bool:  # noqa: D102
        if challenge in self.seen:
            return False
        self.seen[challenge] = ttl
        return True


class TestAltchaForgery:
    async def test_valid_proof_passes_once_and_replay_is_rejected_even_after_eviction(self) -> None:
        ch = captcha.create_altcha_challenge()
        tok = _token(**{k: ch[k] for k in ("challenge", "salt", "signature")}, number=_solve(ch))
        guard = MemGuard()
        assert await captcha.verify(tok, remote_ip=None, replay_guard=guard) == "altcha"
        with pytest.raises(captcha.CaptchaRejected):
            await captcha.verify(tok, remote_ip=None, replay_guard=guard)
        # entry lives at least as long as the maximum signed validity
        assert guard.seen[ch["challenge"]] >= captcha._ALTCHA_TTL_SECONDS - 5
        # after the signed expiry passes, the proof is rejected as expired (not as a "fresh" use)
        later = time.time() + captcha._ALTCHA_TTL_SECONDS + 10
        monkey = pytest.MonkeyPatch()
        try:
            monkey.setattr(captcha.time, "time", lambda: later)
            with pytest.raises(captcha.CaptchaRejected, match="expired"):
                await captcha.verify(tok, remote_ip=None, replay_guard=MemGuard())
        finally:
            monkey.undo()

    async def test_digit_shift_attack_is_rejected(self) -> None:
        """Move the first digit of `number` into the salt: same hash, same old signature."""
        ch = captcha.create_altcha_challenge()
        number = _solve(ch)
        digits = str(number)
        # attacker needs a digit to shift; brute-force a challenge whose number has >= 2 digits
        while len(digits) < 2:
            ch = captcha.create_altcha_challenge()
            digits = str(_solve(ch))
        shifted_salt = ch["salt"] + digits[0]
        assert hashlib.sha256(f"{shifted_salt}{digits[1:]}".encode()).hexdigest() == ch["challenge"]
        tok = _token(
            challenge=ch["challenge"],
            salt=shifted_salt,
            signature=ch["signature"],
            number=int(digits[1:]),
        )
        with pytest.raises(captcha.CaptchaRejected):
            await captcha.verify(tok, remote_ip=None, replay_guard=MemGuard())

    async def test_digit_shift_against_legacy_unterminated_salt_is_rejected(self) -> None:
        """Even a correctly MACed salt without the terminator (old format) is refused."""
        salt = f"{'a' * 24}?expires={int(time.time()) + 100}"
        challenge = hashlib.sha256(f"{salt}7".encode()).hexdigest()
        tok = _token(
            challenge=challenge,
            salt=salt,
            signature=captcha._altcha_sign(salt, challenge),
            number=7,
        )
        with pytest.raises(captcha.CaptchaRejected, match="salt"):
            await captcha.verify(tok, remote_ip=None, replay_guard=MemGuard())

    async def test_far_future_signed_expiry_is_bounded(self) -> None:
        salt = f"{'a' * 24}?expires={int(time.time()) + 10**9}&"
        challenge = hashlib.sha256(f"{salt}7".encode()).hexdigest()
        tok = _token(
            challenge=challenge,
            salt=salt,
            signature=captcha._altcha_sign(salt, challenge),
            number=7,
        )
        with pytest.raises(captcha.CaptchaRejected, match="range"):
            await captcha.verify(tok, remote_ip=None, replay_guard=MemGuard())

    async def test_signature_covers_salt(self) -> None:
        ch = captcha.create_altcha_challenge()
        other = ch["salt"].replace("expires=", "expires=1")  # still shaped right, different salt
        tok = _token(
            challenge=ch["challenge"], salt=other, signature=ch["signature"], number=_solve(ch)
        )
        with pytest.raises(captcha.CaptchaRejected):
            await captcha.verify(tok, remote_ip=None, replay_guard=MemGuard())


def _remote(monkeypatch: pytest.MonkeyPatch, provider: str, body: dict[str, Any]) -> None:
    monkeypatch.setenv("DSAR_CAPTCHA_PROVIDER", provider)
    monkeypatch.setenv("DSAR_CAPTCHA_SECRET", "s")
    monkeypatch.setenv("DSAR_CAPTCHA_SITE_KEY", "k")
    monkeypatch.setattr(
        captcha, "_TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    )


GOOD_V3 = {"success": True, "score": 0.9, "action": "dsar_request", "hostname": "app.example"}


class TestRemoteBinding:
    async def test_recaptcha_v3_good(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _remote(monkeypatch, "recaptcha", GOOD_V3)
        assert await captcha.verify("t", remote_ip=None, replay_guard=MemGuard()) == "recaptcha"

    @pytest.mark.parametrize(
        "mutation",
        [
            {"action": "login"},
            {"action": None},
            {"hostname": "evil.example"},
            {"hostname": None},
            {"score": None},
            {"score": "0.9"},
            {"score": True},
            {"score": 0.2},
        ],
    )
    async def test_recaptcha_v3_rejects_unbound_or_weak(
        self, monkeypatch: pytest.MonkeyPatch, mutation: dict[str, Any]
    ) -> None:
        body = {**GOOD_V3, **mutation}
        body = {k: v for k, v in body.items() if v is not None}
        _remote(monkeypatch, "recaptcha", body)
        with pytest.raises(captcha.CaptchaRejected):
            await captcha.verify("t", remote_ip=None, replay_guard=MemGuard())

    async def test_recaptcha_v2_needs_hostname_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DSAR_RECAPTCHA_VERSION", "v2")
        _remote(monkeypatch, "recaptcha", {"success": True, "hostname": "app.example"})
        assert await captcha.verify("t", remote_ip=None, replay_guard=MemGuard()) == "recaptcha"

    async def test_turnstile_checks_action_and_hostname_when_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _remote(monkeypatch, "turnstile", {"success": True, "hostname": "app.example"})
        assert await captcha.verify("t", remote_ip=None, replay_guard=MemGuard()) == "turnstile"
        for bad in (
            {"success": True, "hostname": "evil.example"},
            {"success": True, "hostname": "app.example", "action": "other"},
        ):
            _remote(monkeypatch, "turnstile", bad)
            with pytest.raises(captcha.CaptchaRejected):
                await captcha.verify("t", remote_ip=None, replay_guard=MemGuard())

    async def test_hostname_allowlist_from_env_and_unconfigured_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DSAR_CAPTCHA_HOSTNAMES", "a.example, B.example")
        _remote(monkeypatch, "recaptcha", {**GOOD_V3, "hostname": "b.example"})
        assert await captcha.verify("t", remote_ip=None, replay_guard=MemGuard()) == "recaptcha"
        monkeypatch.delenv("DSAR_CAPTCHA_HOSTNAMES")
        monkeypatch.delenv("DSAR_VERIFY_URL_BASE")
        with pytest.raises(captcha.CaptchaUnavailable):
            await captcha.verify("t", remote_ip=None, replay_guard=MemGuard())


class FakePipe:
    """Minimal `redis.asyncio` pipeline double (INCR + EXPIRE NX)."""

    def __init__(self, store: FakeRedis) -> None:  # noqa: D107
        self.store, self.ops = store, []

    def incr(self, key: str) -> None:  # noqa: D102
        self.ops.append(("incr", key))

    def expire(self, key: str, ttl: int, nx: bool = False) -> None:  # noqa: D102
        self.ops.append(("expire", key))

    async def execute(self) -> list[int]:  # noqa: D102
        if self.store.fail:
            raise ConnectionError("down")
        out = []
        for op, key in self.ops:
            if op == "incr":
                self.store.counts[key] = self.store.counts.get(key, 0) + 1
                out.append(self.store.counts[key])
            else:
                out.append(1)
        return out


class FakeRedis:
    """In-memory Valkey double with a kill switch."""

    def __init__(self) -> None:  # noqa: D107
        self.counts: dict[str, int] = {}
        self.keys: dict[str, str] = {}
        self.fail = False

    def pipeline(self, transaction: bool = True) -> FakePipe:  # noqa: D102
        return FakePipe(self)

    async def set(
        self, key: str, value: str, nx: bool = False, ex: int | None = None
    ) -> bool | None:  # noqa: D102
        if self.fail:
            raise ConnectionError("down")
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


@pytest.fixture
async def shared_app() -> AsyncIterator[tuple[Quart, FakeRedis]]:
    app = Quart(__name__)
    redis = FakeRedis()
    app.config[limiter._REDIS_CONFIG_KEY] = redis
    async with app.app_context():
        yield app, redis


class TestLimiterFailClosed:
    async def test_shared_allow_and_once_work(self, shared_app: Any) -> None:
        assert [await limiter.allow("k", 2, 60) for _ in range(3)] == [True, True, False]
        assert await limiter.once("c", 60) is True
        assert await limiter.once("c", 60) is False  # atomic SET NX

    async def test_backend_error_fails_closed_for_allow_and_once(self, shared_app: Any) -> None:
        _, redis = shared_app
        redis.fail = True
        for call in (limiter.allow("k", 2, 60), limiter.once("c", 60)):
            with pytest.raises(ApiError) as exc:
                await call
            assert exc.value.status_code == 503 and exc.value.code == "DSAR_UNAVAILABLE"
        assert limiter._LOCAL == {} and limiter._LOCAL_ONCE == {}  # no per-process fallback

    async def test_configured_but_disconnected_limiter_fails_closed(self) -> None:
        app = Quart(__name__)

        class Disconnected:
            _redis = None
            _connected = False

        app.config["rate_limiter"] = Disconnected()
        async with app.app_context():
            with pytest.raises(ApiError) as exc:
                await limiter.allow("k", 1, 60)
            assert exc.value.status_code == 503

    async def test_local_lru_never_clears_and_full_replay_table_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(limiter, "_MAX_LOCAL_KEYS", 3)
        app = Quart(__name__)
        async with app.app_context():
            for i in range(3):
                assert await limiter.allow(f"k{i}", 1, 60)
            assert await limiter.allow("k3", 1, 60)  # LRU evicts k0 only
            assert len(limiter._LOCAL) == 3 and "k1" in limiter._LOCAL
            assert not await limiter.allow("k3", 1, 60)  # k3 still limited: no wipe
            for i in range(3):
                assert await limiter.once(f"r{i}", 600)
            with pytest.raises(ApiError):  # all live: forgetting one would reopen a replay
                await limiter.once("r-new", 600)
            assert await limiter.once("r0", 600) is False


class TestPublicEndpoints:
    @pytest.fixture
    def pub_app(self) -> Quart:
        from quart_schema import QuartSchema

        from blueprints.v1.dsar_requests import public_dsar_bp

        app = Quart(__name__)
        QuartSchema(app)
        app.register_blueprint(public_dsar_bp)
        app.config["dal"] = object()
        return app

    async def test_challenge_and_config_are_rate_limited_per_ip(
        self, pub_app: Quart, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from blueprints.v1 import dsar_requests as mod

        monkeypatch.setattr(mod, "bind_dsar_tables", lambda dal: None)
        monkeypatch.setattr(mod, "CHALLENGE_IP_LIMIT", (2, 3600))
        monkeypatch.setitem(
            mod._PUBLIC_RATE_LIMITED_ENDPOINTS,
            "v1_public_dsar_requests.get_dsar_altcha_challenge",
            (2, 3600),
        )
        url = "/api/v1/public/privacy-requests/altcha-challenge"
        codes = [(await pub_app.test_client().get(url)).status_code for _ in range(3)]
        assert codes == [200, 200, 429]

    async def test_backend_outage_is_uniform_503(
        self, pub_app: Quart, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from blueprints.v1 import dsar_requests as mod

        monkeypatch.setattr(mod, "bind_dsar_tables", lambda dal: None)
        redis = FakeRedis()
        redis.fail = True
        pub_app.config[limiter._REDIS_CONFIG_KEY] = redis
        resp = await pub_app.test_client().get("/api/v1/public/privacy-requests/altcha-challenge")
        assert resp.status_code == 503
        assert (await resp.get_json())["error"]["code"] == "DSAR_UNAVAILABLE"
