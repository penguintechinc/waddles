"""`flask_core.token_revocation` -- the server-side revocation store, cache and outage policy.

Real code paths throughout: a real `MemoryBackend`, a real `redis` client pointed at a closed
port for the genuine connection-refused failure, and a controllable backend subclass only to
*toggle* an outage mid-test (the failure it raises is the production `RevocationStoreError`).
Time is injected so grace / cache / circuit windows are exact rather than slept through. The
real-Valkey round trips live in `test_token_revocation_valkey.py`.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from flask_core import token_revocation as tr
from flask_core.jwt_hardening import MAX_JWT_LIFETIME_SECONDS, REVOCATION_RECORD_TTL_SECONDS
from flask_core.token_revocation import (
    CHECK_DEGRADED_ALLOWED,
    CHECK_OK,
    CHECK_REVOKED_JTI,
    CHECK_REVOKED_SID,
    CHECK_REVOKED_USER,
    CHECK_UNAVAILABLE_ALLOWED,
    CHECK_UNAVAILABLE_REJECTED,
    MemoryBackend,
    RevocationKind,
    RevocationPolicy,
    RevocationStoreError,
    TokenRevocation,
    ValkeyBackend,
    revocation_key,
)

NOW = 1_800_000_000


class Clock:
    """A settable time source standing in for both `time.time` and `time.monotonic`."""

    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ToggleBackend(MemoryBackend):
    """A real in-memory store whose reads/writes fail on demand -- an outage switch."""

    def __init__(self, clock: Clock) -> None:
        super().__init__(clock=clock)
        self.down = False
        self.reads = 0

    def get_many(self, keys: Any) -> list[int | None]:
        self.reads += 1
        if self.down:
            raise RevocationStoreError("ConnectionError")
        return super().get_many(keys)

    def put_max(self, key: str, value: int, ttl_seconds: int) -> None:
        if self.down:
            raise RevocationStoreError("ConnectionError")
        super().put_max(key, value, ttl_seconds)


class Metrics:
    """In-memory OTel reader bound to a `TokenRevocation`'s own instruments."""

    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        self.provider = MeterProvider(metric_readers=[self.reader])

    def _points(self, name: str) -> list[Any]:
        data = self.reader.get_metrics_data()
        points: list[Any] = []
        for resource in data.resource_metrics if data else []:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == name:
                        points.extend(metric.data.data_points)
        return points

    def counts(self, name: str, label: str) -> dict[str, int]:
        return {p.attributes[label]: int(p.value) for p in self._points(name)}

    def observations(self, name: str) -> int:
        return sum(int(p.count) for p in self._points(name))


def claims(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"sub": "42", "jti": "jti-aaaa", "sid": "sid-1", "iat": NOW - 10}
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def make(
    backend: Any = None, *, clock: Clock | None = None, mono: Clock | None = None, **policy: Any
) -> tuple[TokenRevocation, Clock, Clock, Metrics]:
    wall = clock or Clock(NOW)
    monotonic = mono or Clock(1000.0)
    metrics = Metrics()
    rev = TokenRevocation(
        backend if backend is not None else MemoryBackend(clock=wall),
        policy=RevocationPolicy(**policy),
        clock=wall,
        monotonic=monotonic,
        meter_provider=metrics.provider,
    )
    return rev, wall, monotonic, metrics


class TestPolicy:
    def test_defaults_are_the_documented_ones(self) -> None:
        p = RevocationPolicy()
        assert (p.cache_ttl_seconds, p.grace_seconds, p.fail_closed) == (5.0, 60.0, True)
        assert p.record_ttl_seconds == REVOCATION_RECORD_TTL_SECONDS
        assert p.record_ttl_seconds > MAX_JWT_LIFETIME_SECONDS

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"cache_ttl_seconds": -1},
            {"cache_ttl_seconds": 31},
            {"grace_seconds": -1},
            {"grace_seconds": 601},
            {"circuit_open_seconds": 0.1},
            {"circuit_open_seconds": 61},
            {"store_timeout_seconds": 0.01},
            {"store_timeout_seconds": 3},
            {"record_ttl_seconds": MAX_JWT_LIFETIME_SECONDS - 1},
            {"cache_max_entries": 4},
        ],
    )
    def test_out_of_range_values_stop_the_service(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            RevocationPolicy(**kwargs)

    def test_policy_from_env_reads_every_knob(self) -> None:
        p = tr.policy_from_env(
            {
                "JWT_REVOCATION_CACHE_TTL_SECONDS": "2",
                "JWT_REVOCATION_GRACE_SECONDS": "10",
                "JWT_REVOCATION_FAIL_MODE": "OPEN",
                "JWT_REVOCATION_CIRCUIT_OPEN_SECONDS": "3",
                "JWT_REVOCATION_TIMEOUT_SECONDS": "0.5",
            }
        )
        assert (p.cache_ttl_seconds, p.grace_seconds, p.fail_closed) == (2.0, 10.0, False)
        assert (p.circuit_open_seconds, p.store_timeout_seconds) == (3.0, 0.5)

    def test_blank_env_is_the_default(self) -> None:
        assert tr.policy_from_env({"JWT_REVOCATION_GRACE_SECONDS": "  "}) == RevocationPolicy()

    @pytest.mark.parametrize(
        "env",
        [
            {"JWT_REVOCATION_GRACE_SECONDS": "soon"},
            {"JWT_REVOCATION_FAIL_MODE": "ajar"},
            {"JWT_REVOCATION_CACHE_TTL_SECONDS": "999"},
        ],
    )
    def test_bad_env_is_a_startup_error_not_a_silent_default(self, env: dict[str, str]) -> None:
        with pytest.raises(ValueError):
            tr.policy_from_env(env)


class TestMemoryBackend:
    def test_larger_value_wins_and_a_smaller_one_cannot_lower_it(self) -> None:
        clock = Clock(100.0)
        b = MemoryBackend(clock=clock)
        b.put_max("k", 50, 60)
        b.put_max("k", 40, 60)
        assert b.get_many(["k", "absent"]) == [50, None]
        b.put_max("k", 70, 60)
        assert b.get_many(["k"]) == [70]

    def test_records_expire(self) -> None:
        clock = Clock(100.0)
        b = MemoryBackend(clock=clock)
        b.put_max("k", 1, 10)
        clock.advance(9)
        assert b.get_many(["k"]) == [1]
        clock.advance(2)
        assert b.get_many(["k"]) == [None]

    def test_rewrite_after_expiry_starts_a_fresh_record(self) -> None:
        clock = Clock(100.0)
        b = MemoryBackend(clock=clock)
        b.put_max("k", 90, 5)
        clock.advance(10)
        b.put_max("k", 10, 5)  # lower than the (expired) old value: must still be stored
        assert b.get_many(["k"]) == [10]

    def test_a_smaller_write_still_extends_the_ttl(self) -> None:
        clock = Clock(100.0)
        b = MemoryBackend(clock=clock)
        b.put_max("k", 90, 10)
        clock.advance(8)
        b.put_max("k", 10, 10)
        clock.advance(8)  # past the first TTL, inside the extended one
        assert b.get_many(["k"]) == [90]

    def test_expired_records_are_swept_when_the_store_grows(self) -> None:
        clock = Clock(100.0)
        b = MemoryBackend(clock=clock)
        for i in range(4100):
            b.put_max(f"old{i}", 1, 1)
        clock.advance(5)
        b.put_max("fresh", 1, 60)
        assert len(b._data) < 4100
        assert b.get_many(["fresh"]) == [1]


class TestChecks:
    def test_unrevoked_token_is_ok(self) -> None:
        rev, *_ = make()
        verdict = rev.check(claims())
        assert verdict.allowed and verdict.outcome == CHECK_OK

    def test_revoked_jti_is_rejected_and_neighbours_are_not(self) -> None:
        rev, *_ = make()
        rev.revoke_token("jti-aaaa")
        assert rev.check(claims()).outcome == CHECK_REVOKED_JTI
        assert not rev.check(claims()).allowed
        assert rev.check(claims(jti="jti-bbbb")).allowed

    def test_revoked_session_rejects_every_token_of_that_session(self) -> None:
        rev, *_ = make()
        rev.revoke_session("sid-1")
        assert rev.check(claims(jti="x1")).outcome == CHECK_REVOKED_SID
        assert rev.check(claims(jti="x2")).outcome == CHECK_REVOKED_SID
        assert rev.check(claims(sid="sid-2")).allowed

    def test_user_cutoff_rejects_at_or_before_and_allows_after(self) -> None:
        rev, *_ = make()
        cutoff = rev.revoke_user("42")
        assert cutoff == NOW
        for iat in (NOW - 3600, NOW - 1, NOW):
            assert rev.check(claims(iat=iat)).outcome == CHECK_REVOKED_USER
        assert rev.check(claims(iat=NOW + 1)).outcome == CHECK_OK
        assert rev.check(claims(sub="43", iat=NOW - 5)).outcome == CHECK_OK

    def test_cutoff_never_moves_backwards(self) -> None:
        rev, wall, *_ = make()
        rev.revoke_user("42", cutoff=NOW + 100)
        rev.revoke_user("42", cutoff=NOW - 500)
        assert rev.check(claims(iat=NOW + 100)).outcome == CHECK_REVOKED_USER
        assert rev.check(claims(iat=NOW + 101)).outcome == CHECK_OK

    def test_token_without_jti_or_sid_is_still_covered_by_the_user_cutoff(self) -> None:
        rev, *_ = make()
        rev.revoke_user("42")
        assert rev.check(claims(jti=None, sid=None)).outcome == CHECK_REVOKED_USER

    def test_unreadable_iat_cannot_prove_it_postdates_a_cutoff(self) -> None:
        rev, *_ = make()
        rev.revoke_user("42")
        assert rev.check(claims(iat="yesterday")).outcome == CHECK_REVOKED_USER
        assert rev.check(claims(iat=None)).outcome == CHECK_REVOKED_USER

    def test_malformed_jti_or_sid_never_reach_a_key(self) -> None:
        rev, *_ = make()
        # An invalid identifier is simply not looked up (the verifier already refuses such
        # tokens); it must not raise or poison the lookup of the valid claims beside it.
        assert rev.check(claims(jti="bad jti!", sid="sid/../x")).allowed

    def test_revoke_token_ttl_follows_the_tokens_own_expiry(self) -> None:
        recorded: list[tuple[str, int, int]] = []

        class Recording(MemoryBackend):
            def put_max(self, key: str, value: int, ttl_seconds: int) -> None:
                recorded.append((key, value, ttl_seconds))
                super().put_max(key, value, ttl_seconds)

        rev, *_ = make(Recording())
        rev.revoke_token("jti-1", exp=NOW + 120)
        rev.revoke_token("jti-2", exp=NOW - 500)  # already expired -> floor of 1s + skew
        rev.revoke_token("jti-3", exp=NOW + 10**6)  # capped at the record TTL
        rev.revoke_token("jti-4")
        ttls = [r[2] for r in recorded]
        assert ttls[0] == 120 + 30 + 1
        assert ttls[1] == 1  # already expired: floored, never zero or negative
        assert ttls[2] == REVOCATION_RECORD_TTL_SECONDS
        assert ttls[3] == REVOCATION_RECORD_TTL_SECONDS
        assert recorded[0][0] == revocation_key(RevocationKind.JTI, "jti-1")

    @pytest.mark.parametrize(
        ("method", "bad"),
        [("revoke_token", "no good"), ("revoke_session", "x" * 129), ("revoke_session", "")],
    )
    def test_invalid_identifiers_are_refused_loudly(self, method: str, bad: str) -> None:
        rev, *_ = make()
        with pytest.raises(ValueError):
            getattr(rev, method)(bad)

    @pytest.mark.parametrize("bad", ["", "x" * 129])
    def test_invalid_subject_is_refused_loudly(self, bad: str) -> None:
        rev, *_ = make()
        with pytest.raises(ValueError):
            rev.revoke_user(bad)

    def test_cache_is_bounded(self) -> None:
        rev, *_ = make(cache_max_entries=16)
        for i in range(200):
            rev.check(claims(sub=str(i), jti=f"j{i}", sid=f"s{i}"))
        assert len(rev._cache) <= 16


class TestReplicaPropagation:
    """Two replicas sharing one store: the documented propagation bound."""

    def _pair(self) -> tuple[TokenRevocation, TokenRevocation, Clock]:
        wall = Clock(NOW)
        mono = Clock(1000.0)
        store = MemoryBackend(clock=wall)
        a, *_ = make(store, clock=wall, mono=mono, cache_ttl_seconds=5.0)
        b, *_ = make(store, clock=wall, mono=mono, cache_ttl_seconds=5.0)
        return a, b, mono

    def test_other_replica_rejects_within_the_cache_ttl(self) -> None:
        a, b, mono = self._pair()
        assert b.check(claims()).allowed  # b now caches "not revoked"
        a.revoke_token("jti-aaaa")
        assert b.check(claims()).allowed  # still inside b's cache window
        mono.advance(5.1)
        assert b.check(claims()).outcome == CHECK_REVOKED_JTI

    def test_the_revoking_replica_rejects_immediately(self) -> None:
        a, b, mono = self._pair()
        assert a.check(claims()).allowed  # a caches "not revoked"
        a.revoke_token("jti-aaaa")
        assert a.check(claims()).outcome == CHECK_REVOKED_JTI  # write-through, no wait

    def test_revoked_jti_stays_cached_for_the_tokens_whole_life(self) -> None:
        wall, mono = Clock(NOW), Clock(1000.0)
        store = ToggleBackend(wall)
        a, *_ = make(store, clock=wall, mono=mono)
        b, *_ = make(store, clock=wall, mono=mono)
        a.revoke_token("jti-aaaa")
        assert b.check(claims()).outcome == CHECK_REVOKED_JTI
        reads = store.reads
        mono.advance(600)  # far past cache_ttl; a revoked verdict never needs a re-read
        assert b.check(claims()).outcome == CHECK_REVOKED_JTI
        assert store.reads == reads

    def test_zero_cache_ttl_always_asks_the_store(self) -> None:
        wall, mono = Clock(NOW), Clock(1000.0)
        store = MemoryBackend(clock=wall)
        a, *_ = make(store, clock=wall, mono=mono, cache_ttl_seconds=0.0)
        b, *_ = make(store, clock=wall, mono=mono, cache_ttl_seconds=0.0)
        assert b.check(claims()).allowed
        a.revoke_session("sid-1")
        assert b.check(claims()).outcome == CHECK_REVOKED_SID


class TestStoreOutage:
    def _outage(self, **policy: Any) -> tuple[TokenRevocation, ToggleBackend, Clock, Metrics]:
        wall, mono = Clock(NOW), Clock(1000.0)
        backend = ToggleBackend(wall)
        rev, _, _, metrics = make(backend, clock=wall, mono=mono, **policy)
        return rev, backend, mono, metrics

    def test_within_grace_serving_continues_and_is_flagged_degraded(self) -> None:
        rev, backend, mono, metrics = self._outage()
        assert rev.check(claims()).outcome == CHECK_OK
        backend.down = True
        mono.advance(6)  # cache expired -> a store read is needed -> it fails
        verdict = rev.check(claims())
        assert verdict.allowed and verdict.outcome == CHECK_DEGRADED_ALLOWED
        assert metrics.counts("waddles_jwt_revocation_store_errors_total", "backend") == {"memory": 1}

    def test_a_dead_store_is_probed_once_per_circuit_window_not_per_request(self) -> None:
        rev, backend, mono, _ = self._outage(circuit_open_seconds=5.0)
        backend.down = True
        for i in range(50):
            assert rev.check(claims(jti=f"j{i}")).allowed
        assert backend.reads == 1  # one failure opened the circuit; 49 checks never touched it
        mono.advance(5.1)
        rev.check(claims(jti="again"))
        assert backend.reads == 2

    def test_known_revocations_stay_enforced_through_an_outage(self) -> None:
        rev, backend, mono, _ = self._outage()
        rev.revoke_token("jti-aaaa")
        rev.revoke_session("sid-9")
        backend.down = True
        mono.advance(30)
        assert rev.check(claims()).outcome == CHECK_REVOKED_JTI
        assert rev.check(claims(jti="other", sid="sid-9")).outcome == CHECK_REVOKED_SID

    def test_a_stale_user_cutoff_is_still_applied_during_an_outage(self) -> None:
        rev, backend, mono, _ = self._outage()
        rev.revoke_user("42")
        backend.down = True
        mono.advance(30)  # the cutoff entry is stale but it is the last thing we know
        assert rev.check(claims(iat=NOW - 5)).outcome == CHECK_REVOKED_USER

    def test_beyond_grace_the_default_is_to_fail_closed(self) -> None:
        rev, backend, mono, metrics = self._outage(grace_seconds=60.0)
        assert rev.check(claims()).outcome == CHECK_OK
        backend.down = True
        mono.advance(61)
        verdict = rev.check(claims(jti="never-seen"))
        assert not verdict.allowed and verdict.outcome == CHECK_UNAVAILABLE_REJECTED
        assert metrics.counts("waddles_jwt_revocation_checks_total", "outcome")[
            CHECK_UNAVAILABLE_REJECTED
        ] == 1

    def test_fail_open_mode_is_an_explicit_opt_in_and_is_loud(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rev, backend, mono, _ = self._outage(grace_seconds=60.0, fail_closed=False)
        backend.down = True
        mono.advance(61)
        with caplog.at_level(logging.CRITICAL, logger="flask_core.token_revocation"):
            verdict = rev.check(claims())
        assert verdict.allowed and verdict.outcome == CHECK_UNAVAILABLE_ALLOWED
        assert any(
            r.levelno == logging.CRITICAL and "WITHOUT a revocation check" in r.getMessage()
            for r in caplog.records
        )

    def test_recovery_restores_exact_checks_and_resets_the_grace_clock(self) -> None:
        rev, backend, mono, _ = self._outage(grace_seconds=60.0, circuit_open_seconds=5.0)
        backend.down = True
        mono.advance(61)
        assert rev.check(claims(jti="a")).outcome == CHECK_UNAVAILABLE_REJECTED
        backend.down = False
        mono.advance(5.1)  # circuit window over: next lookup probes the store again
        assert rev.check(claims(jti="b")).outcome == CHECK_OK
        backend.down = True
        mono.advance(6)  # grace restarts from the last good contact
        assert rev.check(claims(jti="c")).outcome == CHECK_DEGRADED_ALLOWED

    def test_process_started_into_an_outage_gets_grace_from_its_own_start(self) -> None:
        rev, backend, mono, _ = self._outage(grace_seconds=60.0)
        backend.down = True
        assert rev.check(claims()).outcome == CHECK_DEGRADED_ALLOWED
        mono.advance(61)
        assert rev.check(claims(jti="later")).outcome == CHECK_UNAVAILABLE_REJECTED

    def test_outage_logs_are_rate_limited_and_free_of_token_data(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rev, backend, mono, _ = self._outage()
        backend.down = True
        with caplog.at_level(logging.DEBUG, logger="flask_core.token_revocation"):
            for i in range(40):
                rev.check(claims(sub="sentinel-sub-77", jti=f"sentinel-jti-{i}", sid="sentinel-sid"))
        assert len(caplog.records) <= 3  # one store error + one degraded notice, not 40 lines
        assert "sentinel" not in caplog.text

    def test_write_failure_is_loud_but_this_replica_still_rejects(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rev, backend, mono, metrics = self._outage()
        backend.down = True
        with caplog.at_level(logging.ERROR, logger="flask_core.token_revocation"):
            with pytest.raises(RevocationStoreError):
                rev.revoke_token("sentinel-jti-1")
            with pytest.raises(RevocationStoreError):
                rev.revoke_user("42")
        assert rev.check(claims(jti="sentinel-jti-1")).outcome == CHECK_REVOKED_JTI
        assert rev.check(claims(iat=NOW - 1)).outcome == CHECK_REVOKED_USER
        assert metrics.counts("waddles_jwt_revocation_write_errors_total", "kind") == {
            "jti": 1,
            "sub": 1,
        }
        assert "sentinel" not in caplog.text
        assert any("could not be recorded" in r.getMessage() for r in caplog.records)

    def test_real_connection_refused_is_mapped_to_the_store_error(self) -> None:
        backend = ValkeyBackend.from_url("redis://127.0.0.1:1/0", timeout_seconds=0.2)
        try:
            with pytest.raises(RevocationStoreError) as read_failure:
                backend.get_many(["k"])
            with pytest.raises(RevocationStoreError):
                backend.put_max("k", 1, 10)
        finally:
            backend.close()
        # the text is the exception TYPE only -- never the server address or any payload
        assert str(read_failure.value) == "ConnectionError"

    def test_real_failing_store_degrades_through_the_full_check_path(self) -> None:
        wall, mono = Clock(NOW), Clock(1000.0)
        backend = ValkeyBackend.from_url("redis://127.0.0.1:1/0", timeout_seconds=0.2)
        metrics = Metrics()
        rev = TokenRevocation(
            backend,
            policy=RevocationPolicy(grace_seconds=60.0),
            backend_name="valkey",
            clock=wall,
            monotonic=mono,
            meter_provider=metrics.provider,
        )
        try:
            assert rev.check(claims()).outcome == CHECK_DEGRADED_ALLOWED
            mono.advance(61)
            mono.advance(6)  # past the circuit window too, so the store is probed again
            assert rev.check(claims()).outcome == CHECK_UNAVAILABLE_REJECTED
        finally:
            rev.close()
        assert metrics.counts("waddles_jwt_revocation_store_errors_total", "backend") == {"valkey": 2}


class TestMarkers:
    def test_marker_round_trip_and_expiry(self) -> None:
        rev, wall, *_ = make()
        assert rev.has_marker("rot-abc") is False
        rev.put_marker("rot-abc", 10)
        assert rev.has_marker("rot-abc") is True
        wall.advance(11)
        assert rev.has_marker("rot-abc") is False

    def test_marker_names_are_validated(self) -> None:
        rev, *_ = make()
        with pytest.raises(ValueError):
            rev.put_marker("not ok", 5)
        with pytest.raises(ValueError):
            rev.has_marker("")

    def test_marker_on_a_dead_store_raises_rather_than_guessing(self) -> None:
        wall = Clock(NOW)
        backend = ToggleBackend(wall)
        rev, *_ = make(backend, clock=wall)
        backend.down = True
        with pytest.raises(RevocationStoreError):
            rev.put_marker("rot-1", 5)
        with pytest.raises(RevocationStoreError):
            rev.has_marker("rot-1")

    def test_marker_keys_do_not_collide_with_revocation_keys(self) -> None:
        rev, *_ = make()
        rev.put_marker("jti-aaaa", 30)
        assert rev.check(claims()).outcome == CHECK_OK


class TestAsyncWrappers:
    async def test_async_revokes_and_markers_reach_the_same_store(self) -> None:
        rev, *_ = make()
        await rev.arevoke_token("jti-aaaa")
        assert rev.check(claims()).outcome == CHECK_REVOKED_JTI
        await rev.arevoke_session("sid-7")
        assert rev.check(claims(jti="q", sid="sid-7")).outcome == CHECK_REVOKED_SID
        assert await rev.arevoke_user("42") == NOW
        assert rev.check(claims(jti="q2", sid="s", iat=NOW - 1)).outcome == CHECK_REVOKED_USER
        await rev.aput_marker("rot-1", 30)
        assert await rev.ahas_marker("rot-1") is True


class TestTelemetry:
    def test_every_outcome_and_write_is_counted_and_lookups_are_timed(self) -> None:
        rev, wall, mono, metrics = make()
        rev.check(claims())
        rev.revoke_token("jti-aaaa")
        rev.revoke_session("sid-1")
        rev.revoke_user("42")
        rev.check(claims(jti="fresh", sid="fresh", iat=NOW + 5))
        rev.check(claims())
        checks = metrics.counts("waddles_jwt_revocation_checks_total", "outcome")
        assert checks[CHECK_OK] == 2 and checks[CHECK_REVOKED_JTI] == 1
        assert metrics.counts("waddles_jwt_revocations_total", "kind") == {
            "jti": 1,
            "sid": 1,
            "sub": 1,
        }
        # a zero denominator would be a failure, not a pass
        assert metrics.observations("waddles_jwt_revocation_lookup_seconds") >= 1

    def test_a_broken_meter_never_changes_the_verdict(self, caplog: pytest.LogCaptureFixture) -> None:
        rev, *_ = make()

        class Boom:
            def add(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("SHOULD-NOT-LEAK")

            def record(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("SHOULD-NOT-LEAK")

        rev._instruments.checks = Boom()
        rev._instruments.lookup_seconds = Boom()
        with caplog.at_level(logging.WARNING, logger="flask_core.token_revocation"):
            assert rev.check(claims()).allowed
        assert "SHOULD-NOT-LEAK" not in caplog.text
        assert any("metric emit failed" in r.getMessage() for r in caplog.records)

    def test_revocation_log_lines_are_pii_free(self, caplog: pytest.LogCaptureFixture) -> None:
        rev, *_ = make()
        with caplog.at_level(logging.DEBUG, logger="flask_core.token_revocation"):
            rev.revoke_token("sentinel-jti-123")
            rev.revoke_session("sentinel-sid-123")
            rev.revoke_user("sentinel-sub-123")
        rendered = " ".join(f"{r.getMessage()} {sorted(r.__dict__.items())}" for r in caplog.records)
        assert "sentinel" not in rendered
        assert "kind=jti" in rendered and "kind=sub" in rendered


class TestBuildFromEnv:
    def test_no_url_means_process_local_and_says_so(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="flask_core.token_revocation"):
            rev = tr.build_from_env({})
        assert rev.backend_name == "memory" and isinstance(rev._backend, MemoryBackend)
        assert any("PROCESS-LOCAL" in r.getMessage() for r in caplog.records)

    def test_explicit_memory_backend(self) -> None:
        rev = tr.build_from_env({"JWT_REVOCATION_BACKEND": "memory", "VALKEY_URL": "redis://h:1/0"})
        assert rev.backend_name == "memory"

    def test_url_selects_valkey_with_the_password_injected(self) -> None:
        rev = tr.build_from_env(
            {"VALKEY_URL": "rediss://valkey.svc:6380/0", "VALKEY_PASSWORD": "s3cret-pw"}
        )
        try:
            assert rev.backend_name == "valkey"
            kwargs = rev._backend._client.connection_pool.connection_kwargs
            assert kwargs["password"] == "s3cret-pw"
            assert kwargs["host"] == "valkey.svc"
        finally:
            rev.close()

    def test_redis_url_is_the_fallback_and_embedded_credentials_win(self) -> None:
        rev = tr.build_from_env(
            {"REDIS_URL": "redis://:from-url@redis.svc:6379/0", "VALKEY_PASSWORD": "ignored"}
        )
        try:
            assert rev._backend._client.connection_pool.connection_kwargs["password"] == "from-url"
        finally:
            rev.close()

    def test_valkey_backend_without_a_url_is_refused(self) -> None:
        with pytest.raises(ValueError, match="VALKEY_URL"):
            tr.build_from_env({"JWT_REVOCATION_BACKEND": "valkey"})

    def test_unknown_backend_is_refused(self) -> None:
        with pytest.raises(ValueError, match="JWT_REVOCATION_BACKEND"):
            tr.build_from_env({"JWT_REVOCATION_BACKEND": "disabled"})

    def test_env_policy_reaches_the_instance(self) -> None:
        rev = tr.build_from_env({"JWT_REVOCATION_GRACE_SECONDS": "7"})
        assert rev.policy.grace_seconds == 7.0


class TestProcessSingleton:
    def test_lazily_built_once_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VALKEY_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        previous = tr.configure_token_revocation(None)
        try:
            first = tr.get_token_revocation()
            assert tr.get_token_revocation() is first
        finally:
            tr.configure_token_revocation(previous)

    def test_configure_returns_the_previous_and_none_rearms_the_lazy_build(self) -> None:
        mine = TokenRevocation(MemoryBackend())
        previous = tr.configure_token_revocation(mine)
        try:
            assert tr.get_token_revocation() is mine
            assert tr.configure_token_revocation(None) is mine
        finally:
            tr.configure_token_revocation(previous)

    def test_a_bad_environment_fails_loudly_on_first_use(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JWT_REVOCATION_FAIL_MODE", "ajar")
        previous = tr.configure_token_revocation(None)
        try:
            with pytest.raises(ValueError):
                tr.get_token_revocation()
        finally:
            tr.configure_token_revocation(previous)


class TestKeyLayout:
    """The cross-language contract the Rust verifiers implement -- pinned verbatim."""

    def test_keys(self) -> None:
        assert revocation_key(RevocationKind.JTI, "abc") == "waddles:jwt:rev:jti:abc"
        assert revocation_key(RevocationKind.SID, "7") == "waddles:jwt:rev:sid:7"
        assert revocation_key(RevocationKind.USER, "42") == "waddles:jwt:rev:sub:42"

    def test_identifier_charset(self) -> None:
        assert tr.is_valid_identifier("a1_b-c.d:e")
        for bad in ("", "-lead", ".lead", "sp ace", "x" * 129, 5, None, "a/b"):
            assert not tr.is_valid_identifier(bad)
