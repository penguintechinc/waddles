"""Tests for `waddle_sdk._component_entry.WitWorld` -- the componentize-py app class.

`_component_entry` reads its optional `_entry_wiring`/`_bundle_preimports`
sibling modules once, at import time (the same shape the real build recipe
generates them into a bundle's own source directory), so exercising both
"a bundle implements this stage" and "a bundle does not" requires
`importlib.reload()` with `sys.modules["_entry_wiring"]` set beforehand --
mirroring how the real compiler wires (or omits) that module per bundle.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from dataclasses import dataclass

import pytest
import wit_shapes


@dataclass(frozen=True)
class _FakeErr(Exception):
    """Local stand-in for `componentize_py_types.Err` (not pip-installable)."""

    value: object


def _install_fake_component_modules(
    monkeypatch: pytest.MonkeyPatch, *, entry_wiring: types.ModuleType | None
) -> None:
    monkeypatch.setitem(sys.modules, "componentize_py_types", types.SimpleNamespace(Err=_FakeErr))
    if entry_wiring is not None:
        monkeypatch.setitem(sys.modules, "_entry_wiring", entry_wiring)
    else:
        monkeypatch.delitem(sys.modules, "_entry_wiring", raising=False)
    monkeypatch.delitem(sys.modules, "_bundle_preimports", raising=False)

    context_mod = types.SimpleNamespace(
        get_context=lambda: wit_shapes.BundleContext(
            tenant="acme",
            community="main",
            app_id="waddles.social.alias.default",
            feature="alias",
            version="1.0.0",
            message_id="msg-1",
            config_json="{}",
        )
    )
    types_mod = types.SimpleNamespace(
        PlatformEvent=wit_shapes.WitPlatformEvent,
        StageEnvelope=wit_shapes.WitStageEnvelope,
        TransportResult=wit_shapes.TransportResult,
        TransportError=wit_shapes.TransportError,
        UnsupportedStage=wit_shapes.UnsupportedStage,
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(context=context_mod, types=types_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)


def _reload_component_entry():
    import waddle_sdk._component_entry as component_entry

    return importlib.reload(component_entry)


def test_transform_raises_unsupported_stage_when_bundle_has_no_transform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle with no `bundle_transform` gets the manifest-agnostic unsupported-stage stub."""
    _install_fake_component_modules(monkeypatch, entry_wiring=types.ModuleType("_entry_wiring"))
    component_entry = _reload_component_entry()

    event = wit_shapes.WitPlatformEvent(
        platform="discord",
        event_type="chat.message",
        actor="u1",
        payload_json="{}",
        occurred_at="ts",
    )
    with pytest.raises(_FakeErr) as exc_info:
        component_entry.WitWorld().transform(event)
    assert isinstance(exc_info.value.value, wit_shapes.UnsupportedStage)
    assert exc_info.value.value.stage == "process"


def test_transform_calls_bundle_transform_and_converts_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle implementing `bundle_transform` runs inside bundle_context() and converts back."""
    from waddle_sdk.flask_core.stream_pipeline import PlatformEvent

    calls: list[PlatformEvent] = []

    async def bundle_transform(event: PlatformEvent) -> PlatformEvent | None:
        calls.append(event)
        return PlatformEvent(
            platform=event.platform,
            event_type="reply",
            actor=None,
            payload={"text": "pong"},
            occurred_at=event.occurred_at,
        )

    entry_wiring = types.ModuleType("_entry_wiring")
    entry_wiring.bundle_transform = bundle_transform  # type: ignore[attr-defined]
    _install_fake_component_modules(monkeypatch, entry_wiring=entry_wiring)
    component_entry = _reload_component_entry()

    event = wit_shapes.WitPlatformEvent(
        platform="discord",
        event_type="chat.message",
        actor="u1",
        payload_json=json.dumps({"text": "!ping"}),
        occurred_at="ts",
    )
    result = component_entry.WitWorld().transform(event)

    assert len(calls) == 1
    assert calls[0].payload == {"text": "!ping"}
    assert result.event_type == "reply"
    assert json.loads(result.payload_json) == {"text": "pong"}


def test_transform_returns_none_when_bundle_drops_the_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle returning None (no reply) is passed through as None, not an empty record."""

    async def bundle_transform(event):
        return None

    entry_wiring = types.ModuleType("_entry_wiring")
    entry_wiring.bundle_transform = bundle_transform  # type: ignore[attr-defined]
    _install_fake_component_modules(monkeypatch, entry_wiring=entry_wiring)
    component_entry = _reload_component_entry()

    event = wit_shapes.WitPlatformEvent(
        platform="discord",
        event_type="chat.message",
        actor="u1",
        payload_json="{}",
        occurred_at="ts",
    )
    assert component_entry.WitWorld().transform(event) is None


def test_dispatch_raises_unsupported_stage_when_bundle_has_no_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle with no `bundle_dispatch` gets a TransportError stub, never a silent pass."""
    _install_fake_component_modules(monkeypatch, entry_wiring=types.ModuleType("_entry_wiring"))
    component_entry = _reload_component_entry()

    envelope = wit_shapes.WitStageEnvelope(
        tenant="acme",
        community="main",
        app_id="waddles.social.alias.default",
        stage="action",
        event=wit_shapes.WitPlatformEvent(
            platform="discord",
            event_type="chat.message",
            actor="u1",
            payload_json="{}",
            occurred_at="ts",
        ),
        ts="ts",
        target_app_id=None,
        trace_context=None,
    )
    with pytest.raises(_FakeErr) as exc_info:
        component_entry.WitWorld().dispatch(envelope, "{}")
    assert isinstance(exc_info.value.value, wit_shapes.TransportError)
    assert exc_info.value.value.retryable is False
    assert exc_info.value.value.code == "UNSUPPORTED_STAGE"


def test_dispatch_maps_retryable_exception_to_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RetryableTransportError from a bundle's dispatch maps to `TransportError(retryable=True)`."""
    from waddle_sdk.http import RetryableTransportError

    async def bundle_dispatch(envelope, config, *, http_client):
        raise RetryableTransportError("upstream 503")

    entry_wiring = types.ModuleType("_entry_wiring")
    entry_wiring.bundle_dispatch = bundle_dispatch  # type: ignore[attr-defined]
    _install_fake_component_modules(monkeypatch, entry_wiring=entry_wiring)
    component_entry = _reload_component_entry()

    envelope = wit_shapes.WitStageEnvelope(
        tenant="acme",
        community="main",
        app_id="waddles.social.alias.default",
        stage="action",
        event=wit_shapes.WitPlatformEvent(
            platform="discord",
            event_type="chat.message",
            actor="u1",
            payload_json="{}",
            occurred_at="ts",
        ),
        ts="ts",
        target_app_id=None,
        trace_context=None,
    )
    with pytest.raises(_FakeErr) as exc_info:
        component_entry.WitWorld().dispatch(envelope, "{}")
    assert exc_info.value.value.retryable is True
    assert exc_info.value.value.code == "RetryableTransportError"


def test_dispatch_maps_retryable_exception_subclass_to_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `RetryableTransportError` *subclass* also classifies as retryable via `isinstance`.

    Regression for the exact-class-name match this replaced: `type(exc).__name__
    == "RetryableTransportError"` would have missed any subclass entirely.
    """
    from waddle_sdk.http import RetryableTransportError

    class _RateLimitedError(RetryableTransportError):
        """A bundle-defined subclass, e.g. for a more specific retry reason."""

    async def bundle_dispatch(envelope, config, *, http_client):
        raise _RateLimitedError("rate limited, retry later")

    entry_wiring = types.ModuleType("_entry_wiring")
    entry_wiring.bundle_dispatch = bundle_dispatch  # type: ignore[attr-defined]
    _install_fake_component_modules(monkeypatch, entry_wiring=entry_wiring)
    component_entry = _reload_component_entry()

    envelope = wit_shapes.WitStageEnvelope(
        tenant="acme",
        community="main",
        app_id="waddles.social.alias.default",
        stage="action",
        event=wit_shapes.WitPlatformEvent(
            platform="discord",
            event_type="chat.message",
            actor="u1",
            payload_json="{}",
            occurred_at="ts",
        ),
        ts="ts",
        target_app_id=None,
        trace_context=None,
    )
    with pytest.raises(_FakeErr) as exc_info:
        component_entry.WitWorld().dispatch(envelope, "{}")
    assert exc_info.value.value.retryable is True
    assert exc_info.value.value.code == "_RateLimitedError"


def test_dispatch_success_builds_transport_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful dispatch() converts the result into `types.TransportResult(ok=True)`."""

    class _Result:
        http_status = 200
        detail = "sent"
        sub_type = "msg-123"

    async def bundle_dispatch(envelope, config, *, http_client):
        assert config == {"channel_id": "c1"}
        return _Result()

    entry_wiring = types.ModuleType("_entry_wiring")
    entry_wiring.bundle_dispatch = bundle_dispatch  # type: ignore[attr-defined]
    _install_fake_component_modules(monkeypatch, entry_wiring=entry_wiring)
    component_entry = _reload_component_entry()

    envelope = wit_shapes.WitStageEnvelope(
        tenant="acme",
        community="main",
        app_id="waddles.social.alias.default",
        stage="action",
        event=wit_shapes.WitPlatformEvent(
            platform="discord",
            event_type="chat.message",
            actor="u1",
            payload_json="{}",
            occurred_at="ts",
        ),
        ts="ts",
        target_app_id=None,
        trace_context=None,
    )
    result = component_entry.WitWorld().dispatch(envelope, json.dumps({"channel_id": "c1"}))
    assert result.ok is True
    assert result.status == 200
    assert result.detail == "sent"
    assert result.provider_message_id == "msg-123"


def test_dispatch_returning_error_http_status_maps_to_ok_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle that *returns* (never raises) `http_status=500` must not map to `ok=True`.

    Regression: the old code set `ok=True` unconditionally on any non-raising
    return, silently treating a returned server error as success.
    """

    class _Result:
        http_status = 500
        detail = "upstream returned 500"
        sub_type = None

    async def bundle_dispatch(envelope, config, *, http_client):
        return _Result()

    entry_wiring = types.ModuleType("_entry_wiring")
    entry_wiring.bundle_dispatch = bundle_dispatch  # type: ignore[attr-defined]
    _install_fake_component_modules(monkeypatch, entry_wiring=entry_wiring)
    component_entry = _reload_component_entry()

    envelope = wit_shapes.WitStageEnvelope(
        tenant="acme",
        community="main",
        app_id="waddles.social.alias.default",
        stage="action",
        event=wit_shapes.WitPlatformEvent(
            platform="discord",
            event_type="chat.message",
            actor="u1",
            payload_json="{}",
            occurred_at="ts",
        ),
        ts="ts",
        target_app_id=None,
        trace_context=None,
    )
    result = component_entry.WitWorld().dispatch(envelope, "{}")
    assert result.ok is False
    assert result.status == 500


def test_dispatch_result_with_no_http_status_still_maps_to_ok_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-HTTP transport result (no `http_status` at all) keeps the prior default: `ok=True`."""

    class _Result:
        detail = "pushed"
        sub_type = None

    async def bundle_dispatch(envelope, config, *, http_client):
        return _Result()

    entry_wiring = types.ModuleType("_entry_wiring")
    entry_wiring.bundle_dispatch = bundle_dispatch  # type: ignore[attr-defined]
    _install_fake_component_modules(monkeypatch, entry_wiring=entry_wiring)
    component_entry = _reload_component_entry()

    envelope = wit_shapes.WitStageEnvelope(
        tenant="acme",
        community="main",
        app_id="waddles.social.alias.default",
        stage="action",
        event=wit_shapes.WitPlatformEvent(
            platform="discord",
            event_type="chat.message",
            actor="u1",
            payload_json="{}",
            occurred_at="ts",
        ),
        ts="ts",
        target_app_id=None,
        trace_context=None,
    )
    result = component_entry.WitWorld().dispatch(envelope, "{}")
    assert result.ok is True
    assert result.status is None
