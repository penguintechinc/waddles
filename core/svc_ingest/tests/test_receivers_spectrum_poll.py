"""Tests for `receivers.spectrum_poll` + `builtin_handlers.spectrum_ingest` (gh #101).

HTTP is exercised through `RsiRestProvider` over `httpx.MockTransport` (the SSRF-guard
DNS pin is patched to a passthrough; the guard itself is exercised unpatched against an
IP literal). The loop is driven by a scripted provider. The final class runs the REAL
ingest path: receiver -> `fanout.fan_out_event` -> fakeredis -> `normalize`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from flask_core.app_registry import AppRegistry
from flask_core.stream_pipeline import bundle_stream_key
from waddle_transports import (
    Direction,
    NonRetryableTransportError,
    RetryableTransportError,
    Transport,
)

import receivers.spectrum_poll as sp
from builtin_handlers.spectrum_ingest import SPECTRUM_MANIFEST, normalize, register_default_bundles
from fanout import fan_out_event
from receivers.spectrum_poll import (
    CONSUMES_TAG,
    ORG_CONSUMES_TAG,
    RsiRestProvider,
    SpectrumAuthError,
    SpectrumEndpointError,
    SpectrumItem,
    SpectrumPollReceiver,
    SpectrumTransientError,
)

TOKEN = "rsi-secret-token-value"  # noqa: S105


def _item(
    item_id: str,
    *,
    kind: str = "lobby",
    text: str = "hello",
    epoch: float | None = 1_700_000_000.0,
    is_reply: bool = False,
) -> SpectrumItem:
    return SpectrumItem(
        item_id=item_id,
        kind=kind,
        source_id="src1",
        text=text,
        author_id="42",
        display_name="CitizenOne",
        created_at=sp._iso(epoch),  # noqa: SLF001
        created_epoch=epoch,
        thread_id="t1" if kind == "forum" else None,
        is_reply=is_reply,
    )


class ScriptedProvider:
    """Returns/raises each scripted step in order; raises `StopAsyncIteration`-like stop at end."""

    def __init__(self, steps: list[Any]) -> None:
        """Store the scripted steps."""
        self.steps = list(steps)
        self.calls = 0

    async def fetch(self, kind: str, source_id: str) -> list[SpectrumItem]:
        self.calls += 1
        if not self.steps:
            raise _StopError
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return list(step)


class _StopError(Exception):
    """Ends a receive() loop in tests once the script is exhausted."""


def _receiver(provider: Any, **kw: Any) -> tuple[SpectrumPollReceiver, list[float]]:
    sleeps: list[float] = []
    rcv = SpectrumPollReceiver(provider=provider, **kw)

    async def _sleep(s: float) -> None:
        sleeps.append(s)

    rcv._sleep = _sleep  # noqa: SLF001
    return rcv, sleeps


async def _drain(rcv: SpectrumPollReceiver, config: dict[str, Any]) -> list[Any]:
    out: list[Any] = []
    try:
        async for item in rcv.receive(config):
            out.append(item)
    except _StopError:
        pass
    return out


_CFG: dict[str, Any] = {"kind": "lobby", "source_id": "src1", "emit_backlog": True}


class TestContract:
    def test_inbound_only_transport_no_outbound(self) -> None:
        assert issubclass(SpectrumPollReceiver, Transport)
        assert SpectrumPollReceiver.directions == frozenset({Direction.INBOUND})
        assert Direction.OUTBOUND not in SpectrumPollReceiver.directions

    @pytest.mark.parametrize(
        "config",
        [
            {},
            {"kind": "dm", "source_id": "x"},
            {"kind": "lobby"},
            {"kind": "lobby", "source_id": ""},
        ],
    )
    async def test_bad_config_non_retryable(self, config: dict[str, Any]) -> None:
        rcv, _ = _receiver(ScriptedProvider([]))
        with pytest.raises(NonRetryableTransportError):
            await _drain(rcv, config)

    async def test_missing_token_ref_non_retryable(self) -> None:
        rcv = SpectrumPollReceiver()
        with pytest.raises(NonRetryableTransportError, match="token_ref"):
            await _drain(rcv, {"kind": "lobby", "source_id": "s"})

    async def test_unset_token_env_non_retryable_never_leaks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SPECTRUM_RSI_TOKEN", raising=False)
        rcv = SpectrumPollReceiver()
        with pytest.raises(NonRetryableTransportError, match="SPECTRUM_RSI_TOKEN"):
            await _drain(
                rcv, {"kind": "lobby", "source_id": "s", "token_ref": "SPECTRUM_RSI_TOKEN"}
            )


class TestPollLoop:
    async def test_dedupes_and_yields_new_only(self) -> None:
        prov = ScriptedProvider([[_item("1"), _item("2")], [_item("1"), _item("2"), _item("3")]])
        rcv, sleeps = _receiver(prov)
        out = await _drain(rcv, _CFG)
        assert [e["message_id"] for e in out] == ["1", "2", "3"]
        assert out[0]["platform"] == "spectrum"
        assert sleeps == [sp.DEFAULT_POLL_INTERVAL_S, sp.DEFAULT_POLL_INTERVAL_S]

    async def test_first_poll_primes_without_backlog_by_default(self) -> None:
        prov = ScriptedProvider([[_item("1"), _item("2")], [_item("2"), _item("3")]])
        rcv, _ = _receiver(prov)
        out = await _drain(rcv, {"kind": "lobby", "source_id": "src1"})
        assert [e["message_id"] for e in out] == ["3"]

    async def test_poll_interval_has_a_floor(self) -> None:
        prov = ScriptedProvider([[]])
        rcv, sleeps = _receiver(prov)
        await _drain(rcv, {**_CFG, "poll_interval_s": 0.01})
        assert sleeps == [sp.MIN_POLL_INTERVAL_S]

    async def test_seen_set_is_bounded(self) -> None:
        state = sp._PollState()  # noqa: SLF001
        for i in range(sp._SEEN_CAP + 50):  # noqa: SLF001
            assert state.remember(str(i)) is True
        assert len(state.seen) == sp._SEEN_CAP  # noqa: SLF001
        assert state.remember("0") is True  # evicted -> new again
        assert state.remember(str(sp._SEEN_CAP + 49)) is False  # noqa: SLF001

    async def test_forum_event_shape(self) -> None:
        prov = ScriptedProvider([[_item("9", kind="forum", is_reply=True)]])
        rcv, _ = _receiver(prov)
        out = await _drain(rcv, {**_CFG, "kind": "forum"})
        assert out[0]["kind"] == "forum"
        assert out[0]["thread_id"] == "t1"
        assert out[0]["is_reply"] is True


class TestFailureHandling:
    async def test_auth_error_never_retried(self) -> None:
        prov = ScriptedProvider([SpectrumAuthError("HTTP 401"), [_item("1")]])
        rcv, sleeps = _receiver(prov)
        with pytest.raises(NonRetryableTransportError, match="401"):
            await _drain(rcv, _CFG)
        assert prov.calls == 1
        assert sleeps == []

    async def test_endpoint_changed_fails_loud(self) -> None:
        prov = ScriptedProvider([SpectrumEndpointError("endpoint /x returned HTTP 404")])
        rcv, _ = _receiver(prov)
        with pytest.raises(NonRetryableTransportError, match="404"):
            await _drain(rcv, _CFG)

    async def test_transient_backs_off_exponentially_then_recovers(self) -> None:
        prov = ScriptedProvider(
            [
                SpectrumTransientError("http_503"),
                SpectrumTransientError("http_503"),
                SpectrumTransientError("http_503"),
                [_item("1")],
            ]
        )
        rcv, sleeps = _receiver(prov)
        out = await _drain(rcv, {**_CFG, "base_backoff_s": 2.0, "max_backoff_s": 5.0})
        assert [e["message_id"] for e in out] == ["1"]
        assert sleeps[:3] == [2.0, 4.0, 5.0]

    async def test_retry_after_is_honored(self) -> None:
        prov = ScriptedProvider([SpectrumTransientError("rate_limited", 42.0), []])
        rcv, sleeps = _receiver(prov)
        await _drain(rcv, _CFG)
        assert sleeps[0] == 42.0

    async def test_gives_up_retryable_after_max_consecutive(self) -> None:
        prov = ScriptedProvider([SpectrumTransientError("network:X")] * 3)
        rcv, _ = _receiver(prov)
        with pytest.raises(RetryableTransportError, match="3x"):
            await _drain(rcv, {**_CFG, "max_consecutive_errors": 3})

    async def test_success_resets_error_counter(self) -> None:
        err = SpectrumTransientError("http_500")
        prov = ScriptedProvider([err, err, [], err, err, []])
        rcv, _ = _receiver(prov)
        await _drain(rcv, {**_CFG, "max_consecutive_errors": 3})
        assert prov.calls == 7  # never gave up; ended on scripted _StopError


class TestFlag:
    async def test_flag_off_idles_without_polling(self) -> None:
        sleeps: list[float] = []

        async def off() -> bool:
            return False

        async def _sleep(s: float) -> None:
            sleeps.append(s)
            if len(sleeps) >= 3:
                raise _StopError

        prov = ScriptedProvider([[_item("1")]])
        rcv, _ = _receiver(prov, flag_check=off)
        rcv._sleep = _sleep  # noqa: SLF001
        assert await _drain(rcv, _CFG) == []
        assert prov.calls == 0
        assert len(sleeps) == 3

    async def test_flag_flip_on_starts_polling(self) -> None:
        state = {"on": False, "n": 0}

        async def check() -> bool:
            state["n"] += 1
            state["on"] = state["n"] >= 2
            return state["on"]

        prov = ScriptedProvider([[_item("1")]])
        rcv, _ = _receiver(prov, flag_check=check)
        rcv._monotonic = lambda: state["n"] * 1000.0  # noqa: SLF001
        out = await _drain(rcv, _CFG)
        assert [e["message_id"] for e in out] == ["1"]

    async def test_flag_backend_failure_keeps_last_value(self) -> None:
        seq = iter([True, RuntimeError("posthog down")])

        async def check() -> bool:
            v = next(seq)
            if isinstance(v, Exception):
                raise v
            return v

        prov = ScriptedProvider([[_item("1")], [_item("2")]])
        rcv, _ = _receiver(prov, flag_check=check)
        t = {"n": 0}

        def mono() -> float:
            t["n"] += 1
            return t["n"] * 1000.0

        rcv._monotonic = mono  # noqa: SLF001
        out = await _drain(rcv, _CFG)
        assert [e["message_id"] for e in out] == ["1", "2"]

    async def test_no_flag_check_means_enabled(self) -> None:
        prov = ScriptedProvider([[_item("1")]])
        rcv, _ = _receiver(prov, flag_check=None)
        assert len(await _drain(rcv, _CFG)) == 1


@pytest.fixture
def passthrough_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _req(client: httpx.AsyncClient, method: str, url: str, **kw: Any) -> httpx.Response:
        return await client.request(method, url, **kw)

    monkeypatch.setattr(sp, "guarded_request", _req)


def _provider(handler: Callable[[httpx.Request], httpx.Response], **kw: Any) -> RsiRestProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RsiRestProvider(client, TOKEN, api_base="https://rsi.test/api/spectrum/", **kw)


def _ok(key: str, items: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"success": 1, "code": "OK", "data": {key: items}})


@pytest.mark.usefixtures("passthrough_guard")
class TestRsiRestProvider:
    async def test_forum_request_and_mapping(self) -> None:
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["url"] = str(req.url)
            seen["body"] = json.loads(req.content)
            seen["cookie"] = req.headers["Cookie"]
            seen["tok"] = req.headers["X-Rsi-Token"]
            return _ok(
                "threads",
                [
                    {
                        "id": 2,
                        "subject": "Later",
                        "time_created": 1700000100,
                        "member": {"id": 7, "displayname": "B"},
                    },
                    {
                        "id": "1",
                        "plaintext": " First ",
                        "time_created": "1700000000",
                        "member": {"nickname": "A"},
                        "parent_id": 5,
                        "thread_id": 5,
                    },
                    {"id": 3},  # no text -> dropped
                    "junk",  # non-mapping -> dropped
                ],
            )

        items = await _provider(handler).fetch("forum", "chan9")
        assert seen["url"] == "https://rsi.test/api/spectrum/forum/channel/threads"
        assert seen["body"]["channel_id"] == "chan9"
        assert seen["cookie"] == f"Rsi-Token={TOKEN}" and seen["tok"] == TOKEN
        assert [i.item_id for i in items] == ["1", "2"]  # oldest first
        assert items[0].text == "First" and items[0].is_reply and items[0].display_name == "A"
        assert items[1].author_id == "7"

    async def test_lobby_uses_messages_key_and_custom_path(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/api/spectrum/custom/msgs"
            assert json.loads(req.content)["lobby_id"] == "L1"
            return _ok("messages", [{"id": 1, "plaintext": "yo", "time_created": 1.5}])

        items = await _provider(handler, paths={"lobby": "/custom/msgs"}).fetch("lobby", "L1")
        assert items[0].text == "yo"

    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_status(self, status: int) -> None:
        with pytest.raises(SpectrumAuthError):
            await _provider(lambda r: httpx.Response(status)).fetch("lobby", "x")

    async def test_429_carries_retry_after(self) -> None:
        with pytest.raises(SpectrumTransientError) as ei:
            await _provider(lambda r: httpx.Response(429, headers={"Retry-After": "7"})).fetch(
                "lobby", "x"
            )
        assert ei.value.retry_after_s == 7.0 and ei.value.reason == "rate_limited"

    @pytest.mark.parametrize(("hdr", "expected"), [("9999", 300.0), ("abc", None), (None, None)])
    async def test_retry_after_parsing(self, hdr: str | None, expected: float | None) -> None:
        headers = {"Retry-After": hdr} if hdr else {}
        with pytest.raises(SpectrumTransientError) as ei:
            await _provider(lambda r: httpx.Response(503, headers=headers)).fetch("lobby", "x")
        assert ei.value.retry_after_s == expected

    async def test_other_4xx_is_endpoint_error(self) -> None:
        with pytest.raises(SpectrumEndpointError, match="404"):
            await _provider(lambda r: httpx.Response(404)).fetch("lobby", "x")

    async def test_network_error_transient(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        with pytest.raises(SpectrumTransientError, match="network:ConnectError"):
            await _provider(handler).fetch("lobby", "x")

    @pytest.mark.parametrize(
        "resp",
        [
            httpx.Response(200, text="<html>"),
            httpx.Response(200, json=[1]),
            httpx.Response(200, json={"success": 0, "code": "ErrSomething"}),
            httpx.Response(200, json={"success": 1, "data": []}),
            httpx.Response(200, json={"success": 1, "data": {"other": []}}),
        ],
    )
    async def test_envelope_drift_is_endpoint_error(self, resp: httpx.Response) -> None:
        with pytest.raises(SpectrumEndpointError):
            await _provider(lambda r: resp).fetch("lobby", "x")

    async def test_login_required_envelope_is_auth(self) -> None:
        resp = httpx.Response(200, json={"success": 0, "code": "ErrApiLoginRequired"})
        with pytest.raises(SpectrumAuthError):
            await _provider(lambda r: resp).fetch("lobby", "x")


class TestSsrfGuardUnpatched:
    async def test_loopback_api_base_rejected(self) -> None:
        client = httpx.AsyncClient(follow_redirects=False)
        prov = RsiRestProvider(client, TOKEN, api_base="https://127.0.0.1/api/spectrum")
        with pytest.raises(SpectrumEndpointError, match="SSRF"):
            await prov.fetch("lobby", "x")
        await client.aclose()


@pytest.mark.usefixtures("passthrough_guard")
class TestReceiveWiring:
    async def test_receive_resolves_token_and_polls_over_injected_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPECTRUM_RSI_TOKEN", TOKEN)
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            assert req.headers["X-Rsi-Token"] == TOKEN
            if calls["n"] == 1:
                return _ok("messages", [{"id": 1, "plaintext": "a", "time_created": 1}])
            return httpx.Response(401)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        rcv = SpectrumPollReceiver(http_client=client)

        async def _nosleep(s: float) -> None:
            return None

        rcv._sleep = _nosleep  # noqa: SLF001
        cfg = {
            "kind": "lobby",
            "source_id": "L",
            "token_ref": "SPECTRUM_RSI_TOKEN",
            "api_base": "https://rsi.test/api/spectrum",
            "emit_backlog": True,
        }
        got: list[Any] = []
        with pytest.raises(NonRetryableTransportError):
            async for item in rcv.receive(cfg):
                got.append(item)
        assert [g["message_id"] for g in got] == ["1"]

    async def test_receive_builds_and_closes_own_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPECTRUM_RSI_TOKEN", TOKEN)

        async def _req(
            client: httpx.AsyncClient, method: str, url: str, **kw: Any
        ) -> httpx.Response:
            return httpx.Response(401)

        monkeypatch.setattr(sp, "guarded_request", _req)
        rcv = SpectrumPollReceiver()
        with pytest.raises(NonRetryableTransportError):
            async for _ in rcv.receive(
                {"kind": "forum", "source_id": "c", "token_ref": "SPECTRUM_RSI_TOKEN"}
            ):
                pass


class TestTelemetryAndLogs:
    async def test_metrics_instruments_record_without_provider(self) -> None:
        prov = ScriptedProvider([SpectrumTransientError("http_500"), [_item("1")]])
        rcv, _ = _receiver(prov)
        out = await _drain(rcv, _CFG)
        assert len(out) == 1  # instruments are no-op API objects here; must not raise

    async def test_logs_are_pii_free(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="receivers.spectrum_poll")
        secret_text = "super-private-message-body"
        prov = ScriptedProvider(
            [
                [_item("1", text=secret_text)],
                SpectrumTransientError("http_500"),
                [_item("2", text=secret_text)],
            ]
        )
        rcv, _ = _receiver(prov)
        await _drain(rcv, _CFG)
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "spectrum" in blob
        assert secret_text not in blob and "CitizenOne" not in blob and TOKEN not in blob


class TestIngestPathEndToEnd:
    async def test_receiver_fanout_normalize_real_path(self, redis_client: Any) -> None:
        registry = AppRegistry()
        manifest = register_default_bundles(registry)
        assert manifest.stage_specs["ingest"].consumes == (CONSUMES_TAG, ORG_CONSUMES_TAG)
        prov = ScriptedProvider(
            [[_item("1", text="  o7 fleet  ")], [_item("1"), _item("2", text="second")]]
        )
        rcv, _ = _receiver(prov)

        for raw in await _drain(rcv, _CFG):
            n = await fan_out_event(
                raw,
                consumes_tag=CONSUMES_TAG,
                tenant="global",
                community=None,
                redis_client=redis_client,
                registry=registry,
            )
            assert n == 1

        key = bundle_stream_key("global", None, SPECTRUM_MANIFEST["app_id"], "ingest")
        assert await redis_client.llen(key) == 2
        raw_json = await redis_client.rpop(key)
        event = await normalize(json.loads(raw_json))
        assert event.platform == "spectrum" and event.event_type == "message"
        assert event.payload["text"] == "o7 fleet" and event.actor == "42"
        assert event.occurred_at.startswith("2023-11-14")


class TestNormalize:
    async def test_forum_thread_vs_reply(self) -> None:
        base = {"text": "t", "source_id": "c", "kind": "forum"}
        assert (await normalize(dict(base))).event_type == "thread"
        assert (await normalize({**base, "is_reply": True})).event_type == "reply"

    async def test_defaults_actor_and_occurred_at(self) -> None:
        ev = await normalize({"text": "t", "source_id": "c", "kind": "lobby"})
        assert ev.actor == "unknown" and ev.occurred_at

    @pytest.mark.parametrize(
        "raw",
        [
            {"source_id": "c", "kind": "lobby"},
            {"text": "  ", "source_id": "c", "kind": "lobby"},
            {"text": "t", "kind": "lobby"},
            {"text": "t", "source_id": "c", "kind": "dm"},
        ],
    )
    async def test_malformed_raises_value_error(self, raw: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            await normalize(raw)


class TestHelpers:
    def test_coercions(self) -> None:
        assert sp._s(True) is None and sp._s(5) == "5" and sp._s("") is None  # noqa: SLF001
        assert sp._epoch(True) is None and sp._epoch("x") is None and sp._epoch([]) is None  # noqa: SLF001
        assert sp._epoch("2.5") == 2.5 and sp._epoch(3) == 3.0  # noqa: SLF001
        assert sp._iso(None) is None  # noqa: SLF001
        assert (
            sp._item_from_mapping({"id": 1, "plaintext": "x"}, "lobby", "s").created_epoch is None
        )  # noqa: SLF001
