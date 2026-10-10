"""Log-redaction test for `LocalOllamaClassifier` -- SECURITY (PII in logs).

# regression: the unreachable-Ollama degradation path logged
# ``extra={"error": str(exc)}``. ``str(httpx.HTTPStatusError)`` embeds the full
# request URL and transport errors can embed arbitrary peer text, so the
# exception message must never reach the log stream -- only its type.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from moderation_module.providers.local_ollama import OllamaConfig
from tests.test_local_ollama import _client

SENTINEL = "SENTINEL-moderated-text-6e2b"


def _degradation_text(caplog: pytest.LogCaptureFixture) -> tuple[logging.LogRecord, str]:
    """Return the single degradation record and everything the module logged, fully rendered.

    Scoped to the ``moderation_module`` loggers: httpx's own INFO request line carries the
    operator-configured Ollama URL, which is deployment config rather than user data.
    """
    ours = [r for r in caplog.records if r.name.startswith("moderation_module")]
    records = [r for r in ours if r.getMessage() == "moderation.ollama_unreachable"]
    assert len(records) == 1, "expected exactly one degradation warning"
    rendered = "\n".join(f"{r.getMessage()}|{r.__dict__!r}" for r in ours)
    return records[0], rendered


async def test_transport_error_logs_type_not_message(caplog: pytest.LogCaptureFixture) -> None:
    """A transport failure whose message carries peer text logs only its class name."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection refused {SENTINEL}", request=request)

    classifier, _ = _client(handler)
    with caplog.at_level(logging.DEBUG):
        result = await classifier.classify("hi", {"hate_speech"}, tenant_id=1, community_id=1)

    assert result is None
    record, rendered = _degradation_text(caplog)
    assert SENTINEL not in rendered
    assert getattr(record, "error_type", None) == "ConnectError"
    assert not hasattr(record, "error")


async def test_http_status_error_logs_type_not_url(caplog: pytest.LogCaptureFixture) -> None:
    """``str(HTTPStatusError)`` is the request URL; it must not reach the log."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    config = OllamaConfig(ollama_url=f"http://ollama.test:11434/{SENTINEL}")
    classifier, _ = _client(handler, config)
    with caplog.at_level(logging.DEBUG):
        result = await classifier.classify("hi", {"hate_speech"}, tenant_id=1, community_id=1)

    assert result is None
    record, rendered = _degradation_text(caplog)
    assert SENTINEL not in rendered
    assert getattr(record, "error_type", None) == "HTTPStatusError"
