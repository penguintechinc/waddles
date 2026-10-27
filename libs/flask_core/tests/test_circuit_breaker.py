"""Tests for flask_core.circuit_breaker -- state machine, retry/backoff, manager."""

import asyncio
import time

import pytest

from flask_core import circuit_breaker as cb
from flask_core.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerError,
    CircuitBreakerManager,
    CircuitState,
    retry_with_backoff,
    with_retry,
)


async def _ok(value="ok"):
    return value


async def _fail():
    raise RuntimeError("downstream failed")


async def _trip(breaker: CircuitBreaker, n: int) -> None:
    for _ in range(n):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)


class TestStateMachine:
    async def test_starts_closed_and_passes_calls(self):
        b = CircuitBreaker("svc")
        assert b.state is CircuitState.CLOSED
        assert b.is_closed and not b.is_open and not b.is_half_open
        assert await b.call(_ok, "x") == "x"
        m = b.get_metrics()
        assert m["total_calls"] == 1 and m["total_successes"] == 1
        assert m["failure_rate"] == 0

    async def test_opens_after_threshold_and_rejects(self):
        b = CircuitBreaker("svc", failure_threshold=3)
        await _trip(b, 3)
        assert b.is_open
        with pytest.raises(CircuitBreakerError, match="OPEN"):
            await b.call(_ok)
        m = b.get_metrics()
        assert m["total_rejections"] == 1
        assert m["total_failures"] == 3
        assert m["failure_rate"] == 1.0

    async def test_success_resets_failure_streak(self):
        b = CircuitBreaker("svc", failure_threshold=3)
        await _trip(b, 2)
        await b.call(_ok)
        await _trip(b, 2)
        assert b.is_closed  # streak was reset, never reached 3 in a row

    async def test_half_open_after_timeout_then_closes_on_successes(self):
        b = CircuitBreaker("svc", failure_threshold=1, success_threshold=2, timeout_seconds=30)
        await _trip(b, 1)
        assert b.is_open
        b._last_failure_time = time.time() - 31  # timeout elapsed
        assert await b.call(_ok) == "ok"
        assert b.is_half_open
        assert await b.call(_ok) == "ok"
        assert b.is_closed

    async def test_half_open_failure_reopens(self):
        b = CircuitBreaker("svc", failure_threshold=1, timeout_seconds=30)
        await _trip(b, 1)
        b._last_failure_time = time.time() - 31
        with pytest.raises(RuntimeError):
            await b.call(_fail)
        assert b.is_open

    async def test_open_before_timeout_does_not_attempt_reset(self):
        b = CircuitBreaker("svc", failure_threshold=1, timeout_seconds=3600)
        await _trip(b, 1)
        assert b._should_attempt_reset() is False

    def test_should_attempt_reset_guards(self):
        b = CircuitBreaker("svc")
        assert b._should_attempt_reset() is False  # not open
        b._state = CircuitState.OPEN
        b._last_failure_time = None
        assert b._should_attempt_reset() is False  # open, no failure recorded

    async def test_unexpected_exception_type_not_counted(self):
        b = CircuitBreaker("svc", failure_threshold=1, expected_exception=KeyError)

        async def boom():
            raise ValueError("not tracked")

        with pytest.raises(ValueError):
            await b.call(boom)
        assert b.is_closed
        assert b.get_metrics()["total_failures"] == 0

    async def test_manual_reset_closes(self):
        b = CircuitBreaker("svc", failure_threshold=1)
        await _trip(b, 1)
        b.reset()
        assert b.is_closed
        assert await b.call(_ok) == "ok"

    async def test_decorator_form_wraps_and_preserves_name(self):
        b = CircuitBreaker("svc", failure_threshold=1)

        @b
        async def fetch(x):
            """doc"""
            return x * 2

        assert fetch.__name__ == "fetch"
        assert await fetch(4) == 8

    def test_metrics_config_block(self):
        b = CircuitBreaker("svc", failure_threshold=7, success_threshold=3, timeout_seconds=9)
        assert b.get_metrics()["config"] == {
            "failure_threshold": 7,
            "success_threshold": 3,
            "timeout_seconds": 9,
        }


class _Flaky:
    def __init__(self, fail_times: int):
        self.calls = 0
        self.fail_times = fail_times

    async def __call__(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError(f"attempt {self.calls}")
        return "done"


@pytest.fixture
def sleeps(monkeypatch):
    """Record backoff sleeps without actually waiting."""
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *a, **k):
        recorded.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(cb.asyncio, "sleep", fake_sleep)
    return recorded


class TestRetryWithBackoff:
    async def test_succeeds_first_try_no_sleep(self, sleeps):
        assert await retry_with_backoff(_Flaky(0)) == "done"
        assert sleeps == []

    async def test_retries_with_exponential_delays(self, sleeps):
        f = _Flaky(3)
        assert await retry_with_backoff(f, max_retries=3, initial_delay=1.0) == "done"
        assert f.calls == 4
        assert sleeps == [1.0, 2.0, 4.0]

    async def test_delay_capped_at_max(self, sleeps):
        f = _Flaky(3)
        await retry_with_backoff(f, max_retries=3, initial_delay=10.0, max_delay=15.0)
        assert sleeps == [10.0, 15.0, 15.0]

    async def test_exhaustion_raises_last_error(self, sleeps):
        f = _Flaky(99)
        with pytest.raises(ConnectionError, match="attempt 3"):
            await retry_with_backoff(f, max_retries=2, initial_delay=0.1)
        assert f.calls == 3
        assert len(sleeps) == 2

    async def test_non_matching_exception_propagates_immediately(self, sleeps):
        async def boom():
            raise KeyError("x")

        with pytest.raises(KeyError):
            await retry_with_backoff(boom, exceptions=(ConnectionError,))
        assert sleeps == []

    async def test_accepts_awaitable_instead_of_callable(self, sleeps):
        assert await retry_with_backoff(_ok("coro")) == "coro"

    async def test_negative_retries_runs_nothing(self, sleeps):
        f = _Flaky(0)
        assert await retry_with_backoff(f, max_retries=-1) is None
        assert f.calls == 0


class TestWithRetryDecorator:
    async def test_decorator_retries_then_returns(self, sleeps):
        state = {"n": 0}

        @with_retry(max_retries=2, initial_delay=0.5)
        async def sometimes(a, b=1):
            state["n"] += 1
            if state["n"] < 3:
                raise OSError("flap")
            return a + b

        assert await sometimes(1, b=2) == 3
        assert sleeps == [0.5, 1.0]
        assert sometimes.__name__ == "sometimes"


class TestManager:
    def test_get_breaker_is_idempotent_per_name(self):
        mgr = CircuitBreakerManager()
        a = mgr.get_breaker("a", failure_threshold=2)
        assert mgr.get_breaker("a", failure_threshold=99) is a
        assert a.config.failure_threshold == 2
        assert mgr.get_breaker_by_name("a") is a
        assert mgr.get_breaker_by_name("missing") is None

    async def test_all_metrics_and_reset_all(self):
        mgr = CircuitBreakerManager()
        a = mgr.get_breaker("a", failure_threshold=1)
        mgr.get_breaker("b")
        await _trip(a, 1)
        metrics = {m["name"]: m for m in mgr.get_all_metrics()}
        assert metrics["a"]["state"] == "open"
        assert metrics["b"]["state"] == "closed"
        mgr.reset_all()
        assert a.is_closed
