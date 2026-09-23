"""A minimal ``asyncio`` event loop that runs inside componentize-py's WASI sandbox.

``asyncio.run()``'s default ``SelectorEventLoop`` cannot even construct itself
there (``socket.socketpair()`` raises ``PermissionError``). Adapted from
``spikes/penguin-dal-wasm/waddle_sdk/_poll_loop.py`` (branch
``spike/penguin-dal-wasm``, commit ``964f2729``), itself trimmed from
componentize-py's own ``poll_loop.PollLoop`` example (its ``wasi:http``-specific
send/Stream/Sink helpers removed -- this SDK's WIT world never imports
``wasi:http``). ``run_in_executor`` is patched to run synchronously in place
(Round 2's fix) rather than the upstream's ``raise NotImplementedError`` --
see ``_asyncio_patch.py``'s docstring for exactly what this class of fix is,
and is not, safe for.
"""

from __future__ import annotations

import asyncio
from typing import Any


class PollLoop(asyncio.AbstractEventLoop):
    """Drives a coroutine that only ever awaits already-resolved work.

    Exactly this SDK's facades, which perform a synchronous WIT host call
    inside an ``async def`` and never actually suspend.
    """

    def __init__(self) -> None:
        """Initialize an empty, not-yet-running loop."""
        self.running = False
        self.handles: list[asyncio.Handle] = []
        self.exception: BaseException | None = None

    def get_debug(self) -> bool:
        """Return ``False`` -- debug mode is never enabled for this loop."""
        return False

    def run_until_complete(self, future: Any) -> Any:
        """Drive ``future`` to completion, running only already-queued callbacks.

        Always clears asyncio's process-wide "current running loop" marker on
        the way out (success or exception) -- a component invocation is
        expected to be a one-shot, isolated call in the real sandbox, but
        this SDK's own host-side pytest suite calls into this loop from
        many tests in one process; leaving the marker set after the first
        call broke every later ``asyncio.run()`` in the same test session
        with ``"asyncio.run() cannot be called from a running event loop"``.
        """
        future = asyncio.ensure_future(future, loop=self)
        self.running = True
        asyncio.events._set_running_loop(self)
        try:
            while self.running and not future.done():
                handles, self.handles = self.handles, []
                for handle in handles:
                    if not handle._cancelled:
                        handle._run()
                if not handles and not future.done():
                    raise RuntimeError(
                        "PollLoop: coroutine suspended waiting on real I/O this loop cannot "
                        "service (no wasi:io/poll wakers path)"
                    )
                if self.exception is not None:
                    raise self.exception
            return future.result()
        finally:
            asyncio.events._set_running_loop(None)

    def is_running(self) -> bool:
        """Return whether the loop is currently driving a future."""
        return self.running

    def is_closed(self) -> bool:
        """Return whether the loop has been stopped."""
        return not self.running

    def stop(self) -> None:
        """Stop the loop."""
        self.running = False

    def close(self) -> None:
        """Stop the loop (alias for ``stop``)."""
        self.running = False

    def shutdown_asyncgens(self) -> Any:
        """No-op -- no async generators need shutdown coordination here."""

    def call_exception_handler(self, context: dict[str, Any]) -> None:
        """Record the exception so :meth:`run_until_complete` re-raises it."""
        self.exception = context.get("exception")

    # Deliberately `Any`-typed rather than the stdlib's `*_Ts` overload -- this
    # loop only ever schedules already-known, argument-free continuations.
    def call_soon(  # type: ignore[override]
        self, callback: Any, *args: Any, context: Any = None
    ) -> asyncio.Handle:
        """Queue ``callback`` to run on the next loop iteration."""
        handle = asyncio.Handle(callback, args, self, context)
        self.handles.append(handle)
        return handle

    def create_task(self, coroutine: Any) -> asyncio.Task[Any]:  # type: ignore[override]
        """Wrap ``coroutine`` in a ``Task`` bound to this loop."""
        return asyncio.Task(coroutine, loop=self)

    def create_future(self) -> asyncio.Future[Any]:
        """Create a ``Future`` bound to this loop."""
        return asyncio.Future(loop=self)

    # Deliberately `Any`-typed rather than the stdlib's `*_Ts` overload -- see
    # this method's own docstring for why "executor" here always runs inline.
    def run_in_executor(  # type: ignore[override]
        self, executor: Any, func: Any, *args: Any
    ) -> asyncio.Future[Any]:
        """Run ``func(*args)`` synchronously in place.

        See ``_asyncio_patch.py``'s docstring for the safety argument and its
        boundary. componentize-py's own upstream ``poll_loop.py`` leaves this
        ``raise NotImplementedError``.
        """
        future = self.create_future()
        try:
            future.set_result(func(*args))
        except BaseException as exc:  # noqa: BLE001 - mirror a real executor: propagate via the Future
            future.set_exception(exc)
        return future
