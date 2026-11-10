"""hub-api internal proxies (core-data analytics, svc-streaming RTC) honour transit-TLS policy.

Asserts the F6 client contract: ONE pooled client per proxy instance is reused across calls (no
per-request client => no per-request TLS handshake), and with ``INTERNAL_TLS_REQUIRED`` a plaintext
URL raises ``InternalTlsError`` (fail loud) instead of being sent or downgraded to a masked 502.
"""

from __future__ import annotations

import httpx
import pytest
from flask_core.internal_tls import InternalTlsError

from services.analytics_proxy import AnalyticsCoreProxyClient, AnalyticsProxyConfig
from services.calls_proxy import CallsProxyClient, CallsProxyConfig


def _count_client_builds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Route every AsyncClient onto a mock 200 transport and count constructions."""
    built: list[int] = []
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        built.append(1)
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    return built


async def test_analytics_proxy_reuses_one_pooled_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three calls build exactly one client."""
    monkeypatch.delenv("INTERNAL_TLS_REQUIRED", raising=False)
    built = _count_client_builds(monkeypatch)
    proxy = AnalyticsCoreProxyClient(
        AnalyticsProxyConfig(base_url="https://core-data:8040", api_key="k", timeout_seconds=2.0)
    )
    for _ in range(3):
        result = await proxy.get("/api/v1/analytics/platform/summary")
        assert result.ok
    assert len(built) == 1, f"expected 1 pooled client, built {len(built)}"


async def test_calls_proxy_reuses_one_pooled_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three forwarded calls build exactly one client."""
    monkeypatch.delenv("INTERNAL_TLS_REQUIRED", raising=False)
    built = _count_client_builds(monkeypatch)
    proxy = CallsProxyClient(
        CallsProxyConfig(base_url="https://svc-streaming:8448", timeout_seconds=2.0)
    )
    for _ in range(3):
        result = await proxy.request("GET", "/api/v1/calls", authorization=None)
        assert result.ok
    assert len(built) == 1, f"expected 1 pooled client, built {len(built)}"


async def test_plaintext_internal_url_fails_loud_under_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INTERNAL_TLS_REQUIRED + an http:// upstream raises, never sends and never becomes a 502."""
    monkeypatch.setenv("INTERNAL_TLS_REQUIRED", "true")
    monkeypatch.setenv("INTERNAL_CA_PEM", "unused-because-the-url-check-fires-first")
    built = _count_client_builds(monkeypatch)
    analytics = AnalyticsCoreProxyClient(
        AnalyticsProxyConfig(base_url="http://core-data:8040", api_key="k", timeout_seconds=2.0)
    )
    with pytest.raises(InternalTlsError):
        await analytics.get("/api/v1/analytics/platform/summary")
    calls = CallsProxyClient(
        CallsProxyConfig(base_url="http://svc-streaming:8208", timeout_seconds=2.0)
    )
    with pytest.raises(InternalTlsError):
        await calls.request("GET", "/api/v1/calls", authorization=None)
    assert built == [], "no client may be built (or request sent) for a refused plaintext URL"
