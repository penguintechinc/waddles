"""Shared HTTP plumbing for the REST KMS adapters."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from services.envelope._http import SharedHttp, TokenCache, error_code, observed_call
from services.envelope.errors import KmsAccessDeniedError, KmsUnavailableError


class TestTokenCache:
    """Single-flight, in-memory bearer-token cache."""

    async def test_concurrent_callers_share_one_refresh(self) -> None:
        calls = {"n": 0}

        async def fetch() -> tuple[str, float]:
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return "tok", 3600

        cache = TokenCache(fetch)
        tokens = await asyncio.gather(*[cache.get() for _ in range(20)])
        assert set(tokens) == {"tok"} and calls["n"] == 1

    async def test_refreshes_inside_the_margin_and_on_invalidate(self) -> None:
        now = {"t": 0.0}
        issued: list[str] = []

        async def fetch() -> tuple[str, float]:
            issued.append(f"t{len(issued)}")
            return issued[-1], 300

        cache = TokenCache(fetch, clock=lambda: now["t"], refresh_margin_s=100)
        assert await cache.get() == "t0"
        now["t"] = 150
        assert await cache.get() == "t0"  # 300-100 > 150: still fresh
        now["t"] = 250
        assert await cache.get() == "t1"  # inside the margin: refreshed early
        cache.invalidate()
        assert await cache.get() == "t2"

    async def test_a_failed_fetch_is_not_cached(self) -> None:
        attempts = {"n": 0}

        async def fetch() -> tuple[str, float]:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise KmsUnavailableError("idp down")
            return "ok", 60

        cache = TokenCache(fetch)
        with pytest.raises(KmsUnavailableError):
            await cache.get()
        assert await cache.get() == "ok"


class TestSharedHttp:
    """One pooled client; redirects and environment proxies are off."""

    async def test_client_is_reused_then_replaced_after_close(self) -> None:
        holder = SharedHttp()
        first = holder.client()
        assert holder.client() is first
        assert first.follow_redirects is False
        assert first.trust_env is False
        await holder.aclose()
        await holder.aclose()  # idempotent
        assert holder.client() is not first
        await holder.aclose()


class TestObservedCall:
    """Timeout + error classification around every provider request."""

    async def test_success_returns_the_value(self) -> None:
        async def call() -> int:
            return 7

        assert await observed_call("p", "wrap", 1.0, call) == 7

    async def test_timeout_becomes_a_transient_error_with_a_code(self) -> None:
        async def call() -> int:
            await asyncio.sleep(1)
            return 1

        with pytest.raises(KmsUnavailableError) as raised:
            await observed_call("p", "wrap", 0.05, call)
        assert raised.value.code == "Timeout"

    async def test_transport_failures_become_transient_errors(self) -> None:
        async def call() -> int:
            raise httpx.ConnectError("refused")

        with pytest.raises(KmsUnavailableError) as raised:
            await observed_call("p", "wrap", 1.0, call)
        assert raised.value.code == "ConnectError"

    async def test_classified_errors_pass_through_unchanged(self) -> None:
        async def call() -> int:
            raise KmsAccessDeniedError("denied", code="X")

        with pytest.raises(KmsAccessDeniedError) as raised:
            await observed_call("p", "wrap", 1.0, call)
        assert raised.value.code == "X"

    async def test_programming_errors_are_not_disguised_as_kms_outages(self) -> None:
        async def call() -> int:
            raise KeyError("bug")

        with pytest.raises(KeyError):
            await observed_call("p", "wrap", 1.0, call)


class TestErrorCode:
    """Provider error codes are reduced to short log-safe identifiers."""

    def test_extracts_nested_codes(self) -> None:
        response = httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})
        assert error_code(response, "error", "status") == "PERMISSION_DENIED"

    def test_hostile_or_missing_bodies_yield_safe_strings(self) -> None:
        hostile = httpx.Response(400, json={"error": {"status": "evil\nINJECT " + "x" * 500}})
        code = error_code(hostile, "error", "status")
        assert "\n" not in code and " " not in code and len(code) <= 64
        assert error_code(httpx.Response(500, text="<html>"), "error", "status") == ""
        assert error_code(httpx.Response(400, json={"error": "flat"}), "error", "status") == ""
        assert (
            error_code(httpx.Response(400, json={"error": {"status": 5}}), "error", "status") == ""
        )
        assert error_code(httpx.Response(400, json=[1, 2]), "error") == ""
