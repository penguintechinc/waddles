"""Tests for `waddle_sdk._poll_loop.PollLoop`."""

from __future__ import annotations

import asyncio

import pytest

from waddle_sdk._poll_loop import PollLoop


def test_poll_loop_runs_a_simple_coroutine() -> None:
    """A coroutine that never suspends runs to completion."""

    async def coro() -> int:
        return 7

    loop = PollLoop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(coro())
    finally:
        asyncio.set_event_loop(None)
    assert result == 7


def test_run_in_executor_returns_an_already_done_future() -> None:
    """`run_in_executor` runs the callable synchronously and returns a completed Future."""
    loop = PollLoop()
    future = loop.run_in_executor(None, lambda: 99)
    assert future.done()
    assert future.result() == 99


def test_run_in_executor_propagates_exceptions_via_the_future() -> None:
    """An exception raised by the callable is captured on the returned Future."""
    loop = PollLoop()

    def raises() -> None:
        raise ValueError("boom")

    future = loop.run_in_executor(None, raises)
    assert future.done()
    with pytest.raises(ValueError, match="boom"):
        future.result()


def test_run_until_complete_raises_when_coroutine_suspends_on_real_io() -> None:
    """A coroutine awaiting real (unschedulable) I/O raises rather than hanging forever."""

    async def suspends_forever() -> None:
        await asyncio.Future()

    loop = PollLoop()
    asyncio.set_event_loop(loop)
    try:
        with pytest.raises(RuntimeError, match="suspended waiting on real I/O"):
            loop.run_until_complete(suspends_forever())
    finally:
        asyncio.set_event_loop(None)


def test_get_debug_and_is_closed() -> None:
    """Trivial accessor coverage."""
    loop = PollLoop()
    assert loop.get_debug() is False
    assert loop.is_closed() is True
    assert loop.is_running() is False
