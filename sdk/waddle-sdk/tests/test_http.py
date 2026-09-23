"""Tests for `waddle_sdk.http`."""

from __future__ import annotations

import sys
import types

import pytest
import wit_shapes

from waddle_sdk.http import (
    HttpClient,
    NonRetryableTransportError,
    RetryableTransportError,
    resolve_secret,
)


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("HttpClient coroutine unexpectedly suspended")


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch):
    def send(request: wit_shapes.Request) -> wit_shapes.Response:
        if request.url == "https://timeout.example.com":
            raise wit_shapes.Err(wit_shapes.HttpError_Timeout())
        if request.url == "https://denied.example.com":
            raise wit_shapes.Err(wit_shapes.HttpError_Denied(value="host_not_declared"))
        if request.url == "https://ratelimited.example.com":
            raise wit_shapes.Err(wit_shapes.HttpError_RateLimited(value=2000))
        return wit_shapes.Response(
            status=200,
            headers=[wit_shapes.Header(name="content-type", value="application/json")],
            body=b'{"ok":true}',
            truncated=False,
        )

    http_mod = types.SimpleNamespace(
        send=send,
        Request=wit_shapes.Request,
        Header=wit_shapes.Header,
        Response=wit_shapes.Response,
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(http=http_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return http_mod


def test_get_returns_parsed_response(fake_http) -> None:
    """A successful GET returns status/headers/body/truncated as a plain dict."""
    client = HttpClient()
    response = _run(client.get("https://ok.example.com"))
    assert response["status"] == 200
    assert response["headers"] == {"content-type": "application/json"}
    assert response["body"] == b'{"ok":true}'
    assert response["truncated"] is False


def test_post_sends_method_post(fake_http) -> None:
    """post() delegates to request() with method="POST"."""
    client = HttpClient()
    response = _run(client.post("https://ok.example.com", body=b"{}"))
    assert response["status"] == 200


def test_headers_and_secret_refs_are_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Header dict and SecretRef mapping are converted into the WIT Request shape."""
    captured: dict = {}

    def send(request: wit_shapes.Request) -> wit_shapes.Response:
        captured["request"] = request
        return wit_shapes.Response(status=200, headers=[], body=b"", truncated=False)

    http_mod = types.SimpleNamespace(
        send=send, Request=wit_shapes.Request, Header=wit_shapes.Header
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(http=http_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    ref = resolve_secret("SPOTIFY_API_KEY")
    client = HttpClient()
    _run(
        client.request(
            "GET",
            "https://api.spotify.com/x",
            headers={"X-Foo": "bar"},
            secret_refs={"Authorization": ref},
        )
    )
    request = captured["request"]
    assert request.headers[0].name == "X-Foo"
    assert request.headers[0].value == "bar"
    assert request.secret_refs == [("Authorization", "SPOTIFY_API_KEY")]


def test_timeout_raises_retryable(fake_http) -> None:
    """A WIT `http.Error_Timeout` maps to RetryableTransportError."""
    client = HttpClient()
    with pytest.raises(RetryableTransportError, match="timeout"):
        _run(client.get("https://timeout.example.com"))


def test_rate_limited_raises_retryable(fake_http) -> None:
    """A WIT `http.Error_RateLimited` maps to RetryableTransportError."""
    client = HttpClient()
    with pytest.raises(RetryableTransportError, match="rate limited"):
        _run(client.get("https://ratelimited.example.com"))


def test_denied_raises_non_retryable(fake_http) -> None:
    """A WIT `http.Error_Denied` maps to NonRetryableTransportError."""
    client = HttpClient()
    with pytest.raises(NonRetryableTransportError, match="denied"):
        _run(client.get("https://denied.example.com"))


def test_secret_ref_repr() -> None:
    """SecretRef has a debug-friendly repr and never carries a real value."""
    ref = resolve_secret("MY_SECRET")
    assert ref.name == "MY_SECRET"
    assert "SecretRef" in repr(ref)


def test_transport_error_raises_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A WIT `http.Error_Transport` maps to RetryableTransportError."""

    def send(request):
        raise wit_shapes.Err(wit_shapes.HttpError_Transport(value="connection reset"))

    http_mod = types.SimpleNamespace(
        send=send, Request=wit_shapes.Request, Header=wit_shapes.Header
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(http=http_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    client = HttpClient()
    with pytest.raises(RetryableTransportError, match="transport error"):
        _run(client.get("https://flaky.example.com"))


def test_too_large_raises_non_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A WIT `http.Error_TooLarge` maps to NonRetryableTransportError."""

    def send(request):
        raise wit_shapes.Err(wit_shapes.HttpError_TooLarge(value=1048576))

    http_mod = types.SimpleNamespace(
        send=send, Request=wit_shapes.Request, Header=wit_shapes.Header
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(http=http_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    client = HttpClient()
    with pytest.raises(NonRetryableTransportError, match="too large"):
        _run(client.get("https://huge.example.com"))


def test_unclassified_error_raises_non_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """An error shape this SDK doesn't recognize still raises, never silently succeeds."""

    def send(request):
        raise RuntimeError("something wasmtime-internal")

    http_mod = types.SimpleNamespace(
        send=send, Request=wit_shapes.Request, Header=wit_shapes.Header
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(http=http_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    client = HttpClient()
    with pytest.raises(NonRetryableTransportError, match="unclassified"):
        _run(client.get("https://weird.example.com"))
