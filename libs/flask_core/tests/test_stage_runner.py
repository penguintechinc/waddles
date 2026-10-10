"""Tests for `flask_core.stage_runner` -- the svc-ingest/svc-process poll+load engine.

`load_entrypoint` and the pure-parsing bits of `BundlePoller`/
`fetch_active_bundles` depend only on stdlib + httpx, so this file uses the
same lightweight import-shim `conftest.py` establishes for other leaf
modules (stream_pipeline, workload_identity) -- flask_core/__init__.py
eagerly imports the full pydal/quart/authlib stack this module has no
business depending on.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest


def _load_stage_runner_module() -> Any:
    """Load stage_runner.py directly, bypassing flask_core/__init__.py -- see module docstring."""
    module_name = "flask_core.stage_runner"
    if module_name in sys.modules:
        return sys.modules[module_name]
    src = Path(__file__).resolve().parent.parent / "flask_core" / "stage_runner.py"
    spec = importlib.util.spec_from_file_location(module_name, src)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


sr = _load_stage_runner_module()


# ---------------------------------------------------------------------------
# load_entrypoint
# ---------------------------------------------------------------------------


class TestLoadEntrypoint:
    def test_loads_real_callable(self) -> None:
        func = sr.load_entrypoint("json:dumps")
        import json

        assert func is json.dumps

    def test_rejects_missing_colon(self) -> None:
        with pytest.raises(sr.EntrypointLoadError, match="must be"):
            sr.load_entrypoint("json.dumps")

    def test_rejects_unimportable_module(self) -> None:
        with pytest.raises(sr.EntrypointLoadError, match="cannot import"):
            sr.load_entrypoint("no_such_module_xyz:func")

    def test_rejects_missing_attribute(self) -> None:
        with pytest.raises(sr.EntrypointLoadError, match="no callable attribute"):
            sr.load_entrypoint("json:not_a_real_function")

    def test_rejects_non_callable_module_constant(self) -> None:
        with pytest.raises(sr.EntrypointLoadError, match="no callable attribute"):
            sr.load_entrypoint("sys:path")  # sys.path is a list, not callable


# ---------------------------------------------------------------------------
# fetch_active_bundles / BundlePoller -- mocked httpx transport
# ---------------------------------------------------------------------------


def _make_client(handler: Any) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


def _success_handler(bundles: list[dict[str, Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "stage": request.url.params.get("stage"),
                "bundles": bundles,
                "meta": {"version": 1, "timestamp": "2026-08-31T00:00:00+00:00"},
            },
        )

    return handler


class TestFetchActiveBundles:
    async def test_parses_bundle_list(self) -> None:
        client = _make_client(
            _success_handler(
                [
                    {
                        "appId": "waddles.core.demo.echo",
                        "communityId": 42,
                        "entrypoint": "builtin_handlers.echo_ingest:normalize",
                        "spec": {},
                        "config": {"greeting": "hi"},
                    }
                ]
            )
        )
        bundles = await sr.fetch_active_bundles(
            client,
            "http://hub-api/api/v1/distribution/bundles",
            stage="ingest",
            jwt="t.o.k",
        )
        assert len(bundles) == 1
        assert bundles[0] == sr.BundleDistribution(
            app_id="waddles.core.demo.echo",
            community_id=42,
            entrypoint="builtin_handlers.echo_ingest:normalize",
            spec={},
            config={"greeting": "hi"},
        )
        await client.aclose()

    async def test_sends_authorization_header_and_stage_param(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            seen["stage"] = request.url.params.get("stage")
            seen["community_id"] = request.url.params.get("community_id")
            return httpx.Response(
                200,
                json={"success": True, "stage": "process", "bundles": [], "meta": {}},
            )

        client = _make_client(handler)
        await sr.fetch_active_bundles(
            client,
            "http://hub-api/api/v1/distribution/bundles",
            stage="process",
            jwt="my-jwt",
            community_id=7,
        )
        assert seen["auth"] == "Bearer my-jwt"
        assert seen["stage"] == "process"
        assert seen["community_id"] == "7"
        await client.aclose()

    async def test_raises_on_http_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        client = _make_client(handler)
        with pytest.raises(httpx.HTTPStatusError):
            await sr.fetch_active_bundles(
                client,
                "http://hub-api/api/v1/distribution/bundles",
                stage="ingest",
                jwt="t",
            )
        await client.aclose()


class TestBundlePoller:
    async def test_poll_once_success_resets_backoff(self) -> None:
        client = _make_client(
            _success_handler(
                [
                    {
                        "appId": "waddles.core.demo.echo",
                        "communityId": None,
                        "entrypoint": "builtin_handlers.echo_process:transform",
                        "spec": {},
                        "config": {},
                    }
                ]
            )
        )
        poller = sr.BundlePoller(
            client,
            "http://hub-api/api/v1/distribution/bundles",
            stage="process",
            jwt_provider=lambda: "t",
            poll_interval_s=5.0,
            base_backoff_s=1.0,
        )
        bundles = await poller.poll_once()
        assert len(bundles) == 1
        assert poller.last_known == bundles
        assert poller.next_delay_s == 5.0
        await client.aclose()

    async def test_poll_once_failure_degrades_to_last_known_and_backs_off(self) -> None:
        """Fail-first proof: a poller that only ever failed would return () forever.

        First call succeeds (seeds `last_known`), second call fails --
        `poll_once()` must return the FIRST call's bundles unchanged, not an
        empty set, and `next_delay_s` must have grown past `poll_interval_s`
        (backoff, not the steady-state interval).
        """
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "bundles": [
                            {
                                "appId": "waddles.core.demo.echo",
                                "communityId": None,
                                "entrypoint": "builtin_handlers.echo_ingest:normalize",
                                "spec": {},
                                "config": {},
                            }
                        ]
                    },
                )
            return httpx.Response(503, json={"error": "unavailable"})

        client = _make_client(handler)
        poller = sr.BundlePoller(
            client,
            "http://hub-api/api/v1/distribution/bundles",
            stage="ingest",
            jwt_provider=lambda: "t",
            poll_interval_s=5.0,
            base_backoff_s=1.0,
            max_backoff_s=60.0,
        )

        first = await poller.poll_once()
        assert len(first) == 1

        second = await poller.poll_once()
        assert second == first  # graceful degrade -- last-known set, not empty
        assert poller.next_delay_s == 1.0  # first failure's backoff (base)

        third = await poller.poll_once()
        assert third == first
        assert poller.next_delay_s == 2.0  # doubled

        await client.aclose()

    async def test_backoff_caps_at_max(self) -> None:
        def always_fails(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "down"})

        client = _make_client(always_fails)
        poller = sr.BundlePoller(
            client,
            "http://hub-api/api/v1/distribution/bundles",
            stage="action",
            jwt_provider=lambda: "t",
            base_backoff_s=10.0,
            max_backoff_s=15.0,
        )
        await poller.poll_once()
        assert poller.next_delay_s == 10.0
        await poller.poll_once()
        assert poller.next_delay_s == 15.0  # would be 20, capped at 15
        await poller.poll_once()
        assert poller.next_delay_s == 15.0  # stays capped
        await client.aclose()

    async def test_poll_failure_logs_error_type_and_repr_on_empty_str(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify poll_failed logs error_type and falls back to repr() when str(exc) is empty.

        Some exceptions (e.g. redis.exceptions.ConnectionError()) have an
        empty str() representation, which would result in `error=` with no
        value. The fix ensures error_type is always logged and error falls
        back to repr(exc) when str(exc) is empty.
        """

        class EmptyStrException(Exception):
            """An exception whose str() is empty."""

            def __str__(self) -> str:
                return ""

        def handler(request: httpx.Request) -> httpx.Response:
            raise EmptyStrException("some context")

        client = _make_client(handler)
        poller = sr.BundlePoller(
            client,
            "http://hub-api/api/v1/distribution/bundles",
            stage="ingest",
            jwt_provider=lambda: "t",
            base_backoff_s=1.0,
        )

        with caplog.at_level("WARNING"):
            await poller.poll_once()

        # Verify error_type is logged with the exception class name
        assert "error_type=EmptyStrException" in caplog.text
        # Verify error falls back to repr when str is empty
        assert "error=" in caplog.text
        assert "EmptyStrException" in caplog.text  # from repr fallback
        assert "stage_runner.poll_failed" in caplog.text
        await client.aclose()
