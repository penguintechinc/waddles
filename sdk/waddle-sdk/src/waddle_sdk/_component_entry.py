"""The componentize-py app module for the ``waddle:bundle/stage@1.0.0`` world.

The ONLY module ``bundle-compiler``'s Python build recipe ever points
``componentize-py componentize`` at. Plays the role a stage runner plays for
a real bundle: binds the DAL facade once at process start (``set_bundle_dal``,
normally ``core/svc_process/app.py``'s ``before_serving`` hook) and wraps each
export call in ``bundle_context()`` (normally ``runner.py``'s
``_transform_and_enqueue``). Adapted from the wiring pattern in
``spikes/penguin-dal-wasm/waddle_sdk/app_entry.py`` (spike commit
``964f2729``), generalized to any bundle exposing module-level
``transform``/``dispatch`` functions.

**Two app classes, one per exported interface -- not one combined class.**
Running ``componentize-py bindings -d wit/waddle-bundle -w stage <out>``
against the committed ``wit/waddle-bundle/stage.wit`` and reading
``wit_world/exports/__init__.py`` shows two separate ``Protocol`` classes,
``ProcessStage`` (``transform``) and ``ActionStage`` (``dispatch``) -- because
``world stage`` (``stage.wit``) exports two *separate* interfaces
(``process-stage`` + ``action-stage``), not one. ``componentize-py
componentize`` 0.25.1 resolves each exported interface by looking up a
module-level attribute on the given app module named after the interface
(``getattr(app_module, "ProcessStage")`` / ``"ActionStage"``); a single
combined class (this module's previous ``WitWorld``) fails componentization
outright with ``AttributeError: module 'waddle_sdk._component_entry' has no
attribute 'ProcessStage'`` -- confirmed the hard way building the ``pyping``
bundle. Both classes below are thin, sharing the real transform/dispatch
plumbing via the module-level ``_transform_impl``/``_dispatch_impl``
functions, so a bundle author still only ever writes plain
``transform``/``dispatch`` and never touches this file. See
``bundles/python/pyping/src/app.py`` for a from-scratch bundle wired through
this module directly (no bundle-compiler involved).

Signatures below are not guessed: the exported methods take/return the
generated **typed dataclasses directly** (``types.PlatformEvent`` in,
``Optional[types.PlatformEvent]`` out; ``types.StageEnvelope`` + plain
``str`` config in, ``types.TransportResult`` out) -- never a JSON string
round-trip at this boundary. The unsupported-stage stub raises the generated
``Err(types.UnsupportedStage(...))``/``Err(types.TransportError(...))`` on
the ``result``'s failure arm, matching ``wit_world/exports/__init__.py``'s
own docstrings (``Raises: componentize_py_types.Err(...)``).

**Never imports the bundle's own module directly.** ``bundle.yaml``'s
``stages.<s>.entry`` (e.g. ``app:transform``) is resolved at BUILD time by the
compiler's ``generate_entry_wiring()``, which writes ``_entry_wiring.py`` into
the bundle's own source directory with static ``from {module} import
{function} as bundle_transform``/``bundle_dispatch`` lines -- a static import,
not ``importlib.import_module`` driven by a runtime environment variable,
because the WIT world excludes ``wasi:cli/environment`` (spec Sec6.5): there is
no environment variable to read once this code is actually running inside
the sandbox. ``generate_entry_wiring()`` itself is still stubbed in
``bundle_compiler`` (out of scope here); a hand-built bundle not routed
through the compiler (e.g. ``pyping``) hand-authors its own
``_entry_wiring.py`` in the same shape the compiler would emit.

**Action-stage (``dispatch``) mapping is best-effort and flagged, not fully
verified.** This SDK's oracle proof (the process-stage alias bundle) does not
exercise ``dispatch()`` end to end through a real component build; ``pyping``
is the first bundle whose ``dispatch()`` is proven through an actual
``componentize-py componentize`` build (see its own module docstring). Real
first-party action bundles return ``waddle_transports.TransportResult``
(fields: ``transport``, ``detail``, ``sub_type``, ``http_status`` -- **not**
the WIT ``types.TransportResult``'s ``ok``/``status``/``detail``/
``provider_message_id`` shape), so the mapping below is deliberately
duck-typed (``getattr`` with fallbacks) rather than assuming either shape
exactly. Tighten this once a real first-party action bundle is run through a
real build, same follow-up class as ``waddle_sdk.http``'s flagged items.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any

from waddle_sdk import (
    _asyncio_patch,  # noqa: F401 - must patch asyncio.to_thread before any bundle import
)
from waddle_sdk._poll_loop import PollLoop
from waddle_sdk.db import AsyncDB
from waddle_sdk.flask_core.bundle_runtime import bundle_context, set_bundle_dal
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.http import HttpClient, RetryableTransportError

try:
    import _bundle_preimports  # noqa: F401 - auto-generated into the bundle's own source dir
except ImportError:
    pass  # a bundle with no bundles/ package (single-module bundle) has nothing to pre-import

try:
    import _entry_wiring  # auto-generated (or hand-authored) into the bundle's own source dir
except ImportError:
    # Only during this SDK's own host-side unit tests, which never compile a real component.
    _entry_wiring = None

# Force componentize-py to wizen EVERY ``wit_world.imports.<capability>``
# binding at module load, not just whichever ones happen to already be
# reached by eager code elsewhere. componentize-py's wizening snapshot only
# attaches a host-import submodule when something imports it at Python
# MODULE-LOAD time; every one of this SDK's capability wrappers
# (``clock.py``, ``db.py``, ``http.py``, ``kv.py``, ``log.py``, ``relay.py``,
# ``flask_core/feature_flags.py``, and this module's own ``context`` use)
# instead does ``import wit_world`` lazily, INSIDE a function body, so the
# compiled component's WIT type can correctly declare e.g.
# ``import waddle:bundle/%flags@1.0.0;`` (confirmed via ``wasm-tools
# component wit``) while ``wit_world.imports`` still has no ``flags``
# attribute at runtime -- proven for ``flags`` and ``clock`` the hard way by
# ``core/bundle_executor/tests/flag_on_command_e2e.rs`` (PR #608), which
# freshly compiles a real bundle and exercises the real WIT boundary: every
# command gated by ``feature_flags.feature_enabled()`` silently degraded to
# its fail-open ``default`` because that call's own
# ``getattr(wit_world.imports, "flags", None)`` guard (the #596 guard
# against a stale/pre-``%flags`` world) could never see the binding, fresh
# build or not.
#
# Each import is independently try/except-guarded: a narrower WIT world than
# the full ``stage`` world in ``wit/waddle-bundle/stage.wit`` may legitimately
# omit some of these capabilities, and this SDK's own host-side unit tests
# import this module with no ``wit_world`` package available at all --
# neither case may break module load. Absence after this point is exactly
# what each wrapper's existing ``getattr(..., None)``/``hasattr`` call-site
# guard already handles.
for _wit_capability in (
    "context",
    "http",
    "kv",
    "db",
    "relay",
    "flags",
    "log",
    "clock",
    "reputation",  # stage-next only (issue #726); absent from a stage-1.0.0 world, hence the guard
    "economy",  # stage-next only (issue #714); same guard
):
    try:
        importlib.import_module(f"wit_world.imports.{_wit_capability}")
    except (ImportError, AttributeError):
        pass
del _wit_capability

set_bundle_dal(AsyncDB())


def _dispatch_result_is_ok(result: Any) -> bool:
    """Derive a successful ``dispatch()``'s ``ok`` flag from its own result, never unconditionally.

    ``waddle_transports.TransportResult`` (see this module's docstring) has
    no explicit success flag, only an optional ``http_status`` -- so a bundle
    that returns rather than raises (e.g. ``http_status=500``) must still map
    to ``ok=False``; only a *raised* exception previously signaled failure
    here, silently dropping this case. A result with no ``http_status`` at
    all (non-HTTP transports, e.g. queue/webhook pushes) keeps the prior
    behavior: reaching this function at all already means ``bundle_dispatch``
    did not raise, so no status present is treated as success.
    """
    http_status: Any = getattr(result, "http_status", None)
    if http_status is None:
        return True
    return bool(200 <= http_status < 400)


def _run_coro(coro: Any) -> Any:
    """Drive one coroutine to completion with ``PollLoop``.

    ``asyncio.run()`` cannot be used inside this sandbox (see
    ``_poll_loop.py``'s docstring).
    """
    # PollLoop deliberately implements only the subset of AbstractEventLoop
    # this SDK's facades ever call (see _poll_loop.py's class docstring),
    # not every abstract method the real interface declares.
    loop = PollLoop()  # type: ignore[abstract]
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)


def _transform_impl(event: Any) -> Any:
    """Implement the exported ``process-stage.transform`` WIT function.

    Shared by :class:`ProcessStage` (this module's real componentize-py app
    class); split out so the class itself stays a one-line delegation. ``event``
    is the generated ``wit_world.imports.types.PlatformEvent`` dataclass; the
    return value must be ``None`` or another instance of that same type.
    """
    import wit_world

    sdk_event = PlatformEvent.from_wit_record(event)
    ctx = wit_world.imports.context.get_context()

    if _entry_wiring is None or not hasattr(_entry_wiring, "bundle_transform"):
        from componentize_py_types import Err

        raise Err(wit_world.imports.types.UnsupportedStage(stage="process"))

    async def _run() -> PlatformEvent | None:
        with bundle_context(tenant=ctx.tenant, community=ctx.community, app_id=ctx.app_id):
            # _entry_wiring is generated per-bundle at build time (module
            # docstring) -- mypy has no stub for it and treats every
            # attribute as Any; the real static contract is the WIT
            # world's process-stage.transform signature this method
            # implements, not this module's own local return type.
            return await _entry_wiring.bundle_transform(sdk_event)  # type: ignore[no-any-return]

    result = _run_coro(_run())
    return result.to_wit_record(wit_world.imports.types) if result is not None else None


def _dispatch_impl(envelope: Any, config: str) -> Any:
    """Implement the exported ``action-stage.dispatch`` WIT function.

    Shared by :class:`ActionStage` (this module's real componentize-py app
    class); split out so the class itself stays a one-line delegation. See
    this module's docstring for the flagged, best-effort mapping of an
    action bundle's real return/exception shapes onto the WIT
    ``types.TransportResult``/``types.TransportError`` records.
    """
    import wit_world

    sdk_envelope = StageEnvelope.from_wit_record(envelope)
    config_dict = json.loads(config) if config else {}

    if _entry_wiring is None or not hasattr(_entry_wiring, "bundle_dispatch"):
        from componentize_py_types import Err

        raise Err(
            wit_world.imports.types.TransportError(
                retryable=False,
                code="UNSUPPORTED_STAGE",
                message="no action stage",
                retry_after_ms=None,
            )
        )

    async def _run() -> Any:
        with bundle_context(
            tenant=sdk_envelope.tenant,
            community=sdk_envelope.community,
            app_id=sdk_envelope.app_id,
        ):
            return await _entry_wiring.bundle_dispatch(
                sdk_envelope, config_dict, http_client=HttpClient()
            )

    try:
        result = _run_coro(_run())
    except Exception as exc:  # noqa: BLE001 - maps *TransportError onto types.TransportError
        from componentize_py_types import Err

        # `isinstance`, not a class-name string match: `RetryableTransportError`
        # (imported above) is this SDK's own real, importable class -- unlike
        # the WIT-generated `Err`/`Value_*` types this module and `db.py`
        # classify structurally by name, there is no per-component binding
        # identity problem here, so a name-only match would wrongly miss any
        # subclass a bundle raises.
        retryable = isinstance(exc, RetryableTransportError)
        raise Err(
            wit_world.imports.types.TransportError(
                retryable=retryable,
                code=type(exc).__name__,
                message=str(exc),
                retry_after_ms=None,
            )
        ) from exc

    return wit_world.imports.types.TransportResult(
        ok=_dispatch_result_is_ok(result),
        status=getattr(result, "http_status", None),
        detail=getattr(result, "detail", None),
        provider_message_id=getattr(result, "sub_type", None),
    )


class ProcessStage:
    """componentize-py's real app-class name for the ``process-stage`` export.

    Required exactly under this name (see this module's docstring) --
    ``componentize-py componentize`` looks it up by ``getattr(app_module,
    "ProcessStage")``.
    """

    def transform(self, event: Any) -> Any:
        """Implement the exported ``process-stage.transform`` WIT function.

        See :func:`_transform_impl`.
        """
        return _transform_impl(event)


class ActionStage:
    """componentize-py's real app-class name for the ``action-stage`` export.

    Required exactly under this name (see this module's docstring) --
    ``componentize-py componentize`` looks it up by ``getattr(app_module,
    "ActionStage")``.
    """

    def dispatch(self, envelope: Any, config: str) -> Any:
        """Implement the exported ``action-stage.dispatch`` WIT function.

        See :func:`_dispatch_impl`.
        """
        return _dispatch_impl(envelope, config)


def _unsupported_lifecycle(stage: str) -> Any:
    """Raise the generated ``Err(types.UnsupportedStage(stage=...))`` for a lifecycle hook."""
    import wit_world
    from componentize_py_types import Err

    raise Err(wit_world.imports.types.UnsupportedStage(stage=stage))


class StreamingLifecycle:
    """componentize-py's app-class name for the ``streaming-lifecycle`` export (``stage-next``).

    ``world stage-next`` (``wit/waddle-bundle/stage.wit``) additionally exports
    ``streaming-lifecycle``; componentize-py ``-w stage-next`` therefore
    requires this class on the entry module just as it requires
    ``ProcessStage``/``ActionStage`` (``getattr(app_module,
    "StreamingLifecycle")``). A bundle that reacts to stream events is a
    follow-up (issue #716); until then every hook returns the WIT
    ``unsupported-stage`` failure arm -- "not interested", exactly what the
    interface documents -- so a ``stage-next`` Python bundle built only to
    CALL a new host import (e.g. ``reputation``) componentizes and runs
    unchanged. Unused (and harmless) when the component is built against
    ``world stage``.
    """

    def on_start(self, info: Any) -> Any:
        """Implement ``streaming-lifecycle.on-start`` (unsupported by default)."""
        return _unsupported_lifecycle("on-start")

    def on_stop(self, info: Any) -> Any:
        """Implement ``streaming-lifecycle.on-stop`` (unsupported by default)."""
        return _unsupported_lifecycle("on-stop")

    def on_segment(self, segment: Any) -> Any:
        """Implement ``streaming-lifecycle.on-segment`` (unsupported by default)."""
        return _unsupported_lifecycle("on-segment")

    def on_recording_ready(self, recording: Any) -> Any:
        """Implement ``streaming-lifecycle.on-recording-ready`` (unsupported by default)."""
        return _unsupported_lifecycle("on-recording-ready")
