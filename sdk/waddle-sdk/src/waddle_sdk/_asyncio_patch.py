"""Makes ``asyncio.to_thread()`` work inside the WASI sandbox (spec A20).

Adapted from ``spikes/penguin-dal-wasm/waddle_sdk/_asyncio_patch.py`` (branch
``spike/penguin-dal-wasm``, commit ``964f2729``) -- Round 2's confirmed fix.
No real OS threads exist inside ``componentize-py``'s WASI sandbox, and its
own official ``PollLoop`` (see ``_poll_loop.py``) leaves ``run_in_executor``
unimplemented for exactly that reason. This module runs the wrapped callable
synchronously in place instead of raising ``NotImplementedError``.

Safe here specifically because: (a) every host capability this SDK's facades
call through (``db``, ``http``, ``kv``, ...) is itself a synchronous WIT host
call on both sides of the component boundary -- there is no real blocking I/O
being protected from an event loop in the first place, and (b) a bundle's own
``transform``/``dispatch`` entrypoint never runs concurrently with another
invocation of itself (one wasmtime store, one call at a time -- spec Sec7.2:
"every instance is fresh per call"). This does **not** generalize to a bundle
using ``to_thread`` for genuine CPU-bound parallelism expecting real
concurrency -- documented as a boundary in ``docs/APP_BUNDLE_AUTHORING.md`` v2
(M6), not here.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import Any


async def _sync_to_thread[T](func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Drop-in replacement for ``asyncio.to_thread`` that runs ``func`` in place."""
    call = functools.partial(func, *args, **kwargs)
    return call()


asyncio.to_thread = _sync_to_thread
