"""Host-side (no WASM) proof the asyncio.to_thread patch runs synchronously in place."""

from __future__ import annotations

import asyncio

import waddle_sdk._asyncio_patch  # noqa: F401 - import-time patch application


def test_to_thread_runs_synchronously() -> None:
    """`asyncio.to_thread` runs the callable inline, never on a real thread."""
    calls: list[int] = []

    def blocking() -> int:
        calls.append(1)
        return 42

    async def run() -> int:
        return await asyncio.to_thread(blocking)

    result = asyncio.run(run())
    assert result == 42
    assert calls == [1]


def test_to_thread_propagates_exceptions() -> None:
    """An exception raised inside the wrapped callable propagates to the awaiter."""

    def raises() -> None:
        raise ValueError("boom")

    async def run() -> None:
        await asyncio.to_thread(raises)

    try:
        asyncio.run(run())
        raise AssertionError("expected ValueError to propagate")
    except ValueError as exc:
        assert str(exc) == "boom"


def test_to_thread_passes_args_and_kwargs() -> None:
    """Positional and keyword arguments reach the wrapped callable unchanged."""

    def add(a: int, b: int, *, c: int = 0) -> int:
        return a + b + c

    async def run() -> int:
        return await asyncio.to_thread(add, 1, 2, c=3)

    assert asyncio.run(run()) == 6
