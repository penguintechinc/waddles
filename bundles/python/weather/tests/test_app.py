"""Host-native tests for the `weather` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/lurk/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors (fake `kv`/`relay`/`flags`/`log`/`clock`/`http`).
The `kv` fake validates keys exactly like the real host (gh-631), so a `:` key fails loudly.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

import app
from app import dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def _run(coro):
    return asyncio.run(coro)


_ALLOWED_KV_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


class _HostKvError(Exception):
    """Shaped like the generated WIT `Err` (`.value` holds the error union)."""

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value


def _validate_guest_key(key: str) -> None:
    """Reject exactly what the real `bundle_host_kv` capability rejects."""
    if not key or len(key) > 256:
        raise _HostKvError(f"too-large: key length {len(key)}")
    if any(ch not in _ALLOWED_KV_KEY_CHARS for ch in key):
        raise _HostKvError(f"backend: invalid key {key!r}")


class _FakeKv:
    """In-memory `kv` host stand-in -- validates keys like the real host, scripted failures."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.calls: list[tuple[str, tuple]] = []
        self.fail_ops: set[str] = set()

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_ops:
            raise RuntimeError(f"scripted {op} failure")

    def get(self, key: str):
        _validate_guest_key(key)
        self.calls.append(("get", (key,)))
        self._maybe_fail("get")
        return self.store.get(key)

    def set(self, key: str, value, ttl_seconds: int):
        _validate_guest_key(key)
        self.calls.append(("set", (key, bytes(value), ttl_seconds)))
        self._maybe_fail("set")
        self.store[key] = bytes(value)

    def delete(self, key: str):
        _validate_guest_key(key)
        self.calls.append(("delete", (key,)))
        self._maybe_fail("delete")
        self.store.pop(key, None)

    def increment(self, key: str, delta: int, ttl_seconds: int):
        _validate_guest_key(key)
        self.calls.append(("increment", (key, delta, ttl_seconds)))
        self._maybe_fail("increment")
        new_value = int(self.store.get(key, b"0")) + delta
        self.store[key] = str(new_value).encode()
        return new_value


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch):
    """Fake WIT host: flag ON by default, real in-memory `kv`, recording `relay`/`log`."""
    fake_kv = _FakeKv()
    relay_calls: list[tuple[str, dict]] = []
    log_calls: list[tuple[int, str, str]] = []
    flag_state = {"enabled": True}

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: flag_state["enabled"])
    relay_mod = types.SimpleNamespace(
        push=lambda provider, msg: relay_calls.append((provider, json.loads(msg)))
    )
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: log_calls.append((lvl, msg, fields_json)),
    )
    clock_mod = types.SimpleNamespace(
        now_rfc3339=lambda: "2026-10-09T12:00:00.000Z",
        now_millis=lambda: 1_790_000_000_000,
        monotonic_nanos=lambda: 1,
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, kv=fake_kv, relay=relay_mod, log=log_mod, clock=clock_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return types.SimpleNamespace(
        kv=fake_kv,
        relay_calls=relay_calls,
        log_calls=log_calls,
        flag_state=flag_state,
        wit=fake_wit_world,
    )


def _event(
    text: str,
    *,
    platform: str = "twitch",
    is_mod=False,
    is_broadcaster=False,
    channel_id="12345",
    extra: dict | None = None,
    occurred_at: str = "2026-10-05T00:00:00.000Z",
):
    payload: dict = {"text": text, "channel_id": channel_id}
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    if extra:
        payload.update(extra)
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor="viewer-1",
        payload=payload,
        occurred_at=occurred_at,
    )


def _no_role_event(text: str, *, platform: str = "discord", channel_id="guild-1"):
    """A Discord-shaped event -- no `is_mod`/`is_broadcaster` keys (today's real gap)."""
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-05T00:00:00.000Z",
    )


def _reply(result: PlatformEvent | None) -> str:
    assert result is not None
    return result.payload["text"]


def _logs_text(fake_host) -> str:
    return " ".join(f"{msg} {fields}" for _lvl, msg, fields in fake_host.log_calls)


def _make_env(event: PlatformEvent) -> StageEnvelope:
    return StageEnvelope(
        tenant="t",
        community="c",
        app_id="waddles.core.example.test",
        stage="action",
        event=event,
        ts="2026-10-09T00:00:00.000Z",
    )


def test_dispatch_relays_reply(fake_host) -> None:
    env = _make_env(_event("x", channel_id="c1"))
    env.event.payload["text"] = "hello"
    result = _run(dispatch(env, {}, http_client=None))
    assert result.detail == "relayed"
    assert fake_host.relay_calls == [("twitch", {"channel": "c1", "text": "hello"})]


def test_dispatch_requires_channel_and_text(fake_host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_make_env(_event("x", channel_id="")), {}, http_client=None))
    env = _make_env(_event("", channel_id="c1"))
    with pytest.raises(ValueError, match="text"):
        _run(dispatch(env, {}, http_client=None))


@pytest.mark.parametrize("text", ["hello", "", "   "])
def test_non_bang_messages_are_ignored(text: str, fake_host) -> None:
    assert _run(transform(_event(text))) is None


def test_non_chat_payload_is_ignored(fake_host) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None
    assert fake_host.kv.calls == []


# ---------------------------------------------------------------------------
# bundle-specific tests
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

GOOD_BODY = json.dumps(
    {
        "location": {"name": "Paris", "country": "France"},
        "current": {
            "condition": {"text": "Partly cloudy"},
            "temp_c": 18.5,
            "temp_f": 65.3,
            "feelslike_c": 17,
            "humidity": 60,
            "wind_kph": 12.2,
        },
    }
).encode()


@dataclass
class _Header:
    name: str
    value: str


@dataclass
class _Request:
    method: str
    url: str
    headers: list
    body: object
    secret_refs: list


@dataclass
class _Response:
    status: int
    headers: list = field(default_factory=list)
    body: bytes = b""
    truncated: bool = False


class Error_Timeout:  # noqa: N801 - mirrors the generated WIT variant case names
    pass


class Error_Denied:  # noqa: N801
    def __init__(self, value: str = "egress denied") -> None:
        self.value = value


class Error_RateLimited:  # noqa: N801
    value = 500


class Error_Transport:  # noqa: N801
    value = "boom"


class Error_TooLarge:  # noqa: N801
    value = 99


class _Err(Exception):
    def __init__(self, value: object) -> None:
        super().__init__("err")
        self.value = value


@pytest.fixture
def api(fake_host):
    """Mock the WeatherAPI.com SERVER side (the WIT `http.send` import), not the SDK client."""
    state = types.SimpleNamespace(response=_Response(200, body=GOOD_BODY), raises=None, requests=[])

    def send(req):
        state.requests.append(req)
        if state.raises is not None:
            raise state.raises
        return state.response

    fake_host.wit.imports.http = types.SimpleNamespace(Header=_Header, Request=_Request, send=send)
    return state


def _say(text: str) -> str:
    return _reply(_run(transform(_event(text))))


def test_disabled_flag_makes_no_call(fake_host, api) -> None:
    fake_host.flag_state["enabled"] = False
    assert _run(transform(_event("!weather paris"))) is None
    assert api.requests == []


@pytest.mark.parametrize("text", ["!weatherx paris", "weather paris", "!forecast paris"])
def test_other_commands_ignored(text: str, fake_host, api) -> None:
    assert _run(transform(_event(text))) is None
    assert api.requests == []


def test_success_formats_conditions(fake_host, api) -> None:
    reply = _say("!WEATHER   New York  ")
    assert reply == (
        "Weather in Paris, France: Partly cloudy, 18.5°C / 65.3°F "
        "(feels like 17°C), humidity 60%, wind 12.2 km/h."
    )
    (req,) = api.requests
    assert req.method == "GET"
    parsed = urlparse(req.url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "https",
        "api.weatherapi.com",
        "/v1/current.json",
    )
    assert parse_qs(parsed.query)["q"] == ["New York"]


def test_api_key_goes_only_via_secret_ref(fake_host, api) -> None:
    _say("!weather paris")
    (req,) = api.requests
    assert req.secret_refs == [("key", "WEATHER_API_KEY")]
    assert req.headers == []  # no literal header value
    assert "key=" not in req.url.lower().replace("&aqi", "")  # never in the query string either


def test_location_is_url_encoded(fake_host, api) -> None:
    _say("!weather a&b=c/d?e")
    (req,) = api.requests
    assert parse_qs(urlparse(req.url).query)["q"] == ["a&b=c/d?e"]
    assert "&b=" not in req.url.split("q=", 1)[1].split("&aqi")[0]


@pytest.mark.parametrize(
    "text,needle",
    [
        ("!weather", "Usage"),
        ("!weather    ", "Usage"),
        ("!weather " + "x" * 101, "100 characters"),
        ("!weather bad\x07loc", "characters I can't use"),
    ],
)
def test_location_validation_makes_no_call(text: str, needle: str, fake_host, api) -> None:
    assert needle in _say(text)
    assert api.requests == []


@pytest.mark.parametrize(
    "error",
    [
        _Err(Error_Timeout()),
        _Err(Error_Denied()),
        _Err(Error_RateLimited()),
        _Err(Error_Transport()),
        _Err(Error_TooLarge()),
        _Err("weird"),
    ],
)
def test_transport_failures_fail_loud(error: Exception, fake_host, api) -> None:
    api.raises = error
    assert "unavailable right now" in _say("!weather paris")
    logs = _logs_text(fake_host)
    assert "weather.api_unavailable" in logs
    assert "RetryableTransportError" in logs or "NonRetryableTransportError" in logs


@pytest.mark.parametrize("status", [401, 403])
def test_auth_rejection_is_explicit(status: int, fake_host, api) -> None:
    api.response = _Response(status, body=b'{"error":{"code":2006}}')
    assert "isn't configured correctly" in _say("!weather paris")
    assert "weather.auth_rejected" in _logs_text(fake_host)


def test_location_not_found(fake_host, api) -> None:
    api.response = _Response(400, body=b'{"error":{"code":1006,"message":"No matching location"}}')
    assert _say("!weather zzzzzz") == "I couldn't find that location."
    assert "weather.api_unavailable" not in _logs_text(fake_host)


@pytest.mark.parametrize(
    "body", [b'{"error":{"code":1003}}', b"not json", b"\xff\xfe", b"[]", b'{"error": 5}']
)
def test_other_400s_are_unavailable_not_not_found(body: bytes, fake_host, api) -> None:
    api.response = _Response(400, body=body)
    assert "unavailable right now" in _say("!weather paris")
    assert "weather.bad_response" in _logs_text(fake_host)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 204])
def test_non_200_is_unavailable(status: int, fake_host, api) -> None:
    api.response = _Response(status, body=GOOD_BODY)
    assert "unavailable right now" in _say("!weather paris")
    assert "weather.bad_response" in _logs_text(fake_host)


def test_truncated_body_is_unavailable(fake_host, api) -> None:
    api.response = _Response(200, body=GOOD_BODY, truncated=True)
    assert "unavailable right now" in _say("!weather paris")
    assert "weather.bad_response" in _logs_text(fake_host)


def _mutate(path: list[str], value) -> bytes:
    data = json.loads(GOOD_BODY)
    node = data
    for part in path[:-1]:
        node = node[part]
    if value is KeyError:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return json.dumps(data).encode()


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe",
        b"{}",
        b"[]",
        _mutate(["current", "temp_c"], KeyError),
        _mutate(["current", "temp_c"], "hot"),
        _mutate(["current", "humidity"], True),
        _mutate(["current", "condition"], {}),
        _mutate(["location", "name"], KeyError),
    ],
)
def test_malformed_200_is_loud_not_defaulted(body: bytes, fake_host, api) -> None:
    api.response = _Response(200, body=body)
    reply = _say("!weather paris")
    assert "unavailable right now" in reply
    assert "weather.bad_response" in _logs_text(fake_host)
    assert "Weather in" not in reply


def test_country_is_optional(fake_host, api) -> None:
    api.response = _Response(200, body=_mutate(["location", "country"], ""))
    assert _say("!weather paris").startswith("Weather in Paris:")


def test_logs_are_pii_free(fake_host, api) -> None:
    _say("!weather Secretville")
    api.raises = _Err(Error_Timeout())
    _say("!weather Secretville")
    api.raises = None
    api.response = _Response(500)
    _say("!weather Secretville")
    api.response = _Response(400, body=b'{"error":{"code":1006}}')
    _say("!weather Secretville")
    logs = _logs_text(fake_host)
    for needle in ("Secretville", "secretville", "viewer-1", "WEATHER_API_KEY"):
        assert needle not in logs


def test_manifest_declares_exactly_one_egress_host_and_no_secret_value() -> None:
    import pathlib

    root = pathlib.Path(app.__file__).resolve().parent.parent
    for name in ("bundle.yaml", "hub-manifest.yaml"):
        text = (root / name).read_text()
        assert "net.http.fqdn:api.weatherapi.com" in text
        assert text.count("net.http.") == 1
        assert "api.weatherapi.com" in text
        assert "apikey" not in text.lower() and "secret:" not in text.lower()
