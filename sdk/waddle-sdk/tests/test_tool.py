"""Tests for `waddle_sdk.tool` (P3b bundle-to-bundle tool invoke)."""

from __future__ import annotations

import json
from typing import Any

import pytest

import waddle_sdk.tool as tool
from waddle_sdk.testing import FakeToolHost, install_fake_tool_host


def _run(coro: Any) -> Any:
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("tool coroutine unexpectedly suspended")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeToolHost:
    return install_fake_tool_host(monkeypatch)


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    saved = dict(tool.registry())
    yield
    tool.registry().clear()
    tool.registry().update(saved)


# --- client ---------------------------------------------------------------


def test_invoke_encodes_canonical_json_args_and_decodes_the_ok_value(host: FakeToolHost) -> None:
    host.script("waddles.core.economy", "adjust_balance", {"balance": 5})
    out = _run(tool.invoke("waddles.core.economy", "adjust_balance", {"b": 1, "a": [1, 2]}))
    assert out == {"balance": 5}
    (call,) = host.calls
    assert call.args == b'{"a":[1,2],"b":1}'
    assert call.idempotency_key is None


def test_invoke_passes_the_idempotency_key(host: FakeToolHost) -> None:
    host.script("t", "w", None)
    assert _run(tool.invoke("t", "w", {}, idempotency_key="k-1")) is None
    assert host.calls[0].idempotency_key == "k-1"


@pytest.mark.parametrize(
    "code",
    [
        "not_granted",
        "forbidden",
        "timeout",
        "depth_exceeded",
        "pool_exhausted",
        "target_unavailable",
        "invalid_args",
    ],
)
def test_tool_error_raises_typed_error_with_snake_case_code(host: FakeToolHost, code: str) -> None:
    host.script_error("t", "x", code)
    with pytest.raises(tool.ToolError) as ei:
        _run(tool.invoke("t", "x", {}))
    assert ei.value.code == code


def test_unserializable_args_raise_before_any_host_call(host: FakeToolHost) -> None:
    with pytest.raises(tool.ToolError) as ei:
        _run(tool.invoke("t", "x", {"a": object()}))
    assert ei.value.code == "invalid_args"
    assert host.calls == []


def test_non_json_host_result_fails_loud(host: FakeToolHost) -> None:
    host.script_raw("t", "x", b"\xff not json")
    with pytest.raises(tool.ToolError):
        _run(tool.invoke("t", "x", {}))


# --- service registry -------------------------------------------------------


def test_dispatch_routes_to_the_decorated_handler() -> None:
    @tool.tool("echo")
    async def echo(args: dict[str, Any], *, idempotency_key: str | None) -> Any:
        return {"echo": args, "key": idempotency_key}

    out = _run(tool.dispatch("echo", json.dumps({"x": 1}).encode()))
    assert json.loads(out) == {"echo": {"x": 1}, "key": None}


def test_idempotency_key_is_extracted_and_stripped_from_args() -> None:
    @tool.tool("echo2")
    def echo2(args: dict[str, Any], *, idempotency_key: str | None) -> Any:
        return {"args": args, "key": idempotency_key}

    raw = json.dumps({"x": 1, "__idempotency_key": "abc"}).encode()
    assert json.loads(_run(tool.dispatch("echo2", raw))) == {"args": {"x": 1}, "key": "abc"}


def test_unknown_tool_raises_typed_error() -> None:
    with pytest.raises(tool.ToolError) as ei:
        _run(tool.dispatch("nope", b"{}"))
    assert ei.value.code == "invalid_args"


def test_malformed_args_raise_invalid_args() -> None:
    @tool.tool("m")
    def m(args: dict[str, Any], *, idempotency_key: str | None) -> Any:
        return 1

    for raw in (b"\xff", b"[1]", b"not json"):
        with pytest.raises(tool.ToolError) as ei:
            _run(tool.dispatch("m", raw))
        assert ei.value.code == "invalid_args"


def test_handler_tool_error_passes_through_and_envelope_form() -> None:
    @tool.tool("deny")
    def deny(args: dict[str, Any], *, idempotency_key: str | None) -> Any:
        raise tool.ToolError("forbidden", "no")

    env = json.loads(_run(tool.dispatch_envelope("deny", b"{}")))
    assert env == {"tool_error": {"code": "forbidden", "message": "no"}}


def test_envelope_ok_and_unknown() -> None:
    @tool.tool("one")
    def one(args: dict[str, Any], *, idempotency_key: str | None) -> Any:
        return 1

    assert json.loads(_run(tool.dispatch_envelope("one", b"{}"))) == {"ok": 1}
    env = json.loads(_run(tool.dispatch_envelope("zzz", b"{}")))
    assert env["tool_error"]["code"] == "invalid_args"


def test_handler_crash_maps_to_target_unavailable_without_leaking_detail() -> None:
    @tool.tool("boom")
    def boom(args: dict[str, Any], *, idempotency_key: str | None) -> Any:
        raise RuntimeError("secret detail")

    with pytest.raises(tool.ToolError) as ei:
        _run(tool.dispatch("boom", b"{}"))
    assert ei.value.code == "target_unavailable"
    assert "secret" not in str(ei.value)


def test_duplicate_registration_rejected() -> None:
    @tool.tool("dup")
    def a(args: dict[str, Any], *, idempotency_key: str | None) -> Any:
        return 1

    with pytest.raises(ValueError):
        tool.tool("dup")(a)


def test_round_trip_client_to_service_idempotency_key() -> None:
    @tool.tool("rt")
    def rt(args: dict[str, Any], *, idempotency_key: str | None) -> Any:
        return {"key": idempotency_key}

    # Stage merges the key into the forwarded args under the reserved field.
    forwarded = json.dumps({"__idempotency_key": "k9"}).encode()
    assert json.loads(_run(tool.dispatch("rt", forwarded))) == {"key": "k9"}
