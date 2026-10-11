"""`token_revocation` against a REAL Valkey server -- the production path, no fakes.

Needs a reachable server in `WADDLES_TEST_VALKEY_URL` (CI's `python-unit` job runs one as a
service container, pinned to the same image digest the chart deploys). A developer machine
without one skips these loudly; CI (`CI=true`) *fails* instead, so a missing service can never
turn the only real-store coverage into a silent pass.

What only a real server can prove: the Lua ``put_max`` really keeps the larger cutoff and never
shortens a TTL, ``MGET`` really returns one answer per key in order, records really expire, a
flushed script cache (``NOSCRIPT``) is survived, and a revocation written by one "replica" is seen
by another that shares nothing with it but the server.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import redis

from flask_core import token_revocation as tr
from flask_core.auth import create_jwt_token, verify_jwt_token
from flask_core.token_revocation import (
    CHECK_OK,
    CHECK_REVOKED_JTI,
    CHECK_REVOKED_SID,
    CHECK_REVOKED_USER,
    RevocationKind,
    RevocationPolicy,
    TokenRevocation,
    ValkeyBackend,
    revocation_key,
)

SECRET = "test-secret-key-not-for-production-use-only"
URL = os.environ.get("WADDLES_TEST_VALKEY_URL", "")


@pytest.fixture(scope="module", autouse=True)
def _require_valkey() -> None:
    if URL:
        return
    if os.environ.get("CI"):
        pytest.fail("WADDLES_TEST_VALKEY_URL is not set in CI: the real-Valkey service is missing")
    pytest.skip("WADDLES_TEST_VALKEY_URL not set -- real-Valkey revocation tests not run here")


@pytest.fixture
def raw() -> Iterator[Any]:
    client = redis.Redis.from_url(URL, decode_responses=True)
    client.ping()
    yield client
    client.close()


@pytest.fixture
def ident() -> str:
    """A per-test unique identifier so parallel runs and reruns never collide."""
    return uuid.uuid4().hex


def replica(**policy: Any) -> TokenRevocation:
    """A fresh 'replica': its own client, its own cache -- sharing only the server."""
    return TokenRevocation(
        ValkeyBackend.from_url(URL, timeout_seconds=1.0),
        policy=RevocationPolicy(**policy),
        backend_name="valkey",
    )


class TestValkeyBackend:
    def test_put_max_keeps_the_larger_value_and_sets_a_ttl(self, raw: Any, ident: str) -> None:
        backend = ValkeyBackend.from_url(URL, timeout_seconds=1.0)
        key = revocation_key(RevocationKind.USER, ident)
        try:
            backend.put_max(key, 100, 600)
            backend.put_max(key, 50, 600)  # lower: must not move the cutoff backwards
            assert backend.get_many([key]) == [100]
            backend.put_max(key, 200, 600)
            assert backend.get_many([key]) == [200]
            assert 0 < raw.ttl(key) <= 600
        finally:
            raw.delete(key)
            backend.close()

    def test_a_lower_write_extends_but_never_shortens_the_ttl(self, raw: Any, ident: str) -> None:
        backend = ValkeyBackend.from_url(URL, timeout_seconds=1.0)
        key = revocation_key(RevocationKind.USER, ident)
        try:
            backend.put_max(key, 100, 30)
            backend.put_max(key, 10, 3000)  # lower value, longer ttl: ttl grows, value stays
            assert backend.get_many([key]) == [100]
            assert raw.ttl(key) > 30
            backend.put_max(key, 10, 5)  # shorter ttl must NOT shrink the record
            assert raw.ttl(key) > 30
        finally:
            raw.delete(key)
            backend.close()

    def test_mget_answers_every_key_in_order(self, raw: Any, ident: str) -> None:
        backend = ValkeyBackend.from_url(URL, timeout_seconds=1.0)
        a = revocation_key(RevocationKind.JTI, f"a{ident}")
        b = revocation_key(RevocationKind.SID, f"b{ident}")
        c = revocation_key(RevocationKind.USER, f"c{ident}")
        try:
            backend.put_max(a, 11, 60)
            backend.put_max(c, 33, 60)
            assert backend.get_many([a, b, c]) == [11, None, 33]
            assert backend.get_many([]) == []
        finally:
            raw.delete(a, b, c)
            backend.close()

    def test_records_expire_on_the_server(self, raw: Any, ident: str) -> None:
        backend = ValkeyBackend.from_url(URL, timeout_seconds=1.0)
        key = revocation_key(RevocationKind.JTI, ident)
        try:
            backend.put_max(key, 1, 1)
            assert backend.get_many([key]) == [1]
            time.sleep(1.2)
            assert backend.get_many([key]) == [None]
        finally:
            raw.delete(key)
            backend.close()

    def test_a_flushed_script_cache_is_survived(self, raw: Any, ident: str) -> None:
        backend = ValkeyBackend.from_url(URL, timeout_seconds=1.0)
        key = revocation_key(RevocationKind.SID, ident)
        try:
            backend.put_max(key, 5, 60)
            raw.script_flush()  # server forgets the Lua script -> NOSCRIPT on next EVALSHA
            backend.put_max(key, 9, 60)
            assert backend.get_many([key]) == [9]
        finally:
            raw.delete(key)
            backend.close()

    def test_a_non_integer_record_counts_as_revoked(self, raw: Any, ident: str) -> None:
        backend = ValkeyBackend.from_url(URL, timeout_seconds=1.0)
        key = revocation_key(RevocationKind.JTI, ident)
        try:
            raw.set(key, "not-a-number", ex=60)
            (value,) = backend.get_many([key])
            assert value is not None and value > 2**61
        finally:
            raw.delete(key)
            backend.close()


class TestCrossReplicaRevocation:
    def test_a_revocation_by_one_replica_reaches_another_sharing_only_the_server(
        self, raw: Any, ident: str
    ) -> None:
        a, b = replica(cache_ttl_seconds=0.0), replica(cache_ttl_seconds=0.0)
        claims = {"sub": f"u{ident}", "jti": f"j{ident}", "sid": f"s{ident}", "iat": int(time.time()) - 5}
        keys = [revocation_key(k, v) for k, v in (
            (RevocationKind.JTI, claims["jti"]),
            (RevocationKind.SID, claims["sid"]),
            (RevocationKind.USER, claims["sub"]),
        )]
        try:
            assert b.check(claims).outcome == CHECK_OK
            a.revoke_token(claims["jti"])
            assert b.check(claims).outcome == CHECK_REVOKED_JTI
            other = dict(claims, jti=f"other{ident}")
            assert b.check(other).outcome == CHECK_OK
            a.revoke_session(claims["sid"])
            assert b.check(other).outcome == CHECK_REVOKED_SID
            third = dict(claims, jti=f"third{ident}", sid=f"s3{ident}")
            assert b.check(third).outcome == CHECK_OK
            a.revoke_user(claims["sub"])
            assert b.check(third).outcome == CHECK_REVOKED_USER
            assert b.check(dict(third, iat=int(time.time()) + 5)).outcome == CHECK_OK
        finally:
            raw.delete(*keys)
            a.close()
            b.close()

    def test_markers_round_trip_through_the_server(self, raw: Any, ident: str) -> None:
        a, b = replica(), replica()
        name = f"rot-{ident}"
        try:
            assert b.has_marker(name) is False
            a.put_marker(name, 30)
            assert b.has_marker(name) is True
        finally:
            raw.delete(f"{tr.MARKER_KEY_PREFIX}:{name}")
            a.close()
            b.close()


class TestEndToEndThroughTheVerifier:
    """`verify_jwt_token` -> real revocation store -> real Valkey."""

    def test_revoked_before_exp_rejected_and_revoke_all_cuts_off_prior_tokens(
        self, raw: Any, ident: str
    ) -> None:
        shared = replica(cache_ttl_seconds=0.0)
        previous = tr.configure_token_revocation(shared)
        user = f"user-{ident}"
        keys: list[str] = []
        try:
            def mint(sid: str | None = None) -> str:
                return create_jwt_token(
                    user_id=user, username="u", email="u@example.invalid", roles=[],
                    secret_key=SECRET, tenant="global", scope="community:read", session_id=sid,
                )

            one, two = mint(f"s1{ident}"), mint(f"s2{ident}")
            assert verify_jwt_token(one, SECRET) is not None
            payload = verify_jwt_token(two, SECRET)
            assert payload is not None
            keys.append(revocation_key(RevocationKind.JTI, payload["jti"]))

            shared.revoke_token(payload["jti"], exp=payload["exp"])  # one token, before its exp
            assert verify_jwt_token(two, SECRET) is None
            assert verify_jwt_token(one, SECRET) is not None  # its sibling is untouched

            shared.revoke_session(f"s1{ident}")
            keys.append(revocation_key(RevocationKind.SID, f"s1{ident}"))
            assert verify_jwt_token(one, SECRET) is None

            survivor = mint()
            assert verify_jwt_token(survivor, SECRET) is not None
            shared.revoke_user(user)
            keys.append(revocation_key(RevocationKind.USER, user))
            assert verify_jwt_token(survivor, SECRET) is None  # revoke-all cut off a prior token
            time.sleep(1.1)  # iat has second resolution: a token minted next second is later
            assert verify_jwt_token(mint(), SECRET) is not None
        finally:
            tr.configure_token_revocation(previous)
            raw.delete(*keys)
            shared.close()

    def test_a_real_outage_degrades_then_fails_closed(self, ident: str) -> None:
        """An unreachable store on the real client: grace first, then fail closed."""
        dead = TokenRevocation(
            ValkeyBackend.from_url("redis://127.0.0.1:1/0", timeout_seconds=0.2),
            policy=RevocationPolicy(grace_seconds=0.0, circuit_open_seconds=0.5),
            backend_name="valkey",
        )
        previous = tr.configure_token_revocation(dead)
        try:
            token = create_jwt_token(
                user_id=f"user-{ident}", username="u", email="u@example.invalid", roles=[],
                secret_key=SECRET, tenant="global", scope="",
            )
            time.sleep(0.05)  # grace_seconds=0: any elapsed time since construction is "beyond"
            assert verify_jwt_token(token, SECRET) is None
        finally:
            tr.configure_token_revocation(previous)
            dead.close()
