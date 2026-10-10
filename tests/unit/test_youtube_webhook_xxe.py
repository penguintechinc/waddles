"""Regression tests: the YouTube PubSubHubbub webhook must be XXE safe.

``trigger/receiver/youtube_live_module/services/webhook_handler.py`` parses the
Atom body POSTed to a public, unauthenticated callback URL.  It used to call the
stdlib ``xml.etree.ElementTree.fromstring`` (no XXE protection); it now goes
through ``defusedxml``.  Every hostile payload must be rejected as ``Invalid
XML``: no event is forwarded, no request reaches the entity's target, and the
attacker-chosen entity name is not logged.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _xxe_payloads import (  # noqa: E402
    ENTITY_NAME,
    PAYLOAD_IDS,
    Canary,
    Hostile,
    build_hostile,
)

_HANDLER_PATH = REPO_ROOT / "trigger/receiver/youtube_live_module/services/webhook_handler.py"

_ATOM_NS = 'xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom"'


@pytest.fixture
def handler_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Load webhook_handler.py in isolation with a stub ``config`` module."""
    stub = types.ModuleType("config")
    stub.Config = type("Config", (), {"ROUTER_API_URL": "http://router.invalid/api"})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "config", stub)
    spec = importlib.util.spec_from_file_location(
        "youtube_webhook_handler_under_test", _HANDLER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def canary() -> Iterator[Canary]:
    """Yield a running SSRF canary server."""
    server = Canary()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture(params=PAYLOAD_IDS)
def hostile(request: pytest.FixtureRequest, canary: Canary) -> Hostile:
    """Build each hostile payload, aimed at the SSRF canary where relevant."""
    return build_hostile(request.param, canary.url)


def _feed(prologue: str, title: str) -> bytes:
    """Return a one-entry YouTube Atom feed with ``prologue`` before the root."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"{prologue}"
        f"<feed {_ATOM_NS}>"
        "<author><name>Chan</name>"
        "<uri>https://www.youtube.com/channel/UC123</uri></author>"
        "<entry><yt:videoId>vid123</yt:videoId>"
        f"<title>{title}</title>"
        "<published>2026-01-01T00:00:00+00:00</published>"
        "<updated>2026-01-01T00:00:00+00:00</updated>"
        '<link rel="alternate" href="https://www.youtube.com/watch?v=vid123"/>'
        "</entry></feed>"
    ).encode()


def _run(module: types.ModuleType, body: bytes) -> tuple[dict[str, Any], list[Any]]:
    """Run process_notification with event forwarding captured."""
    handler = module.WebhookHandler()
    forwarded: list[Any] = []

    async def _capture(event: Any) -> None:
        forwarded.append(event)

    handler._forward_event = _capture
    return asyncio.run(handler.process_notification(body)), forwarded


def test_hostile_payload_rejected(
    handler_module: types.ModuleType,
    hostile: Hostile,
    canary: Canary,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hostile Atom bodies yield 'Invalid XML', no event, no SSRF, no entity name logged."""
    body = _feed(hostile.doctype, f"Title{hostile.ref}")
    with caplog.at_level(logging.DEBUG):
        result, forwarded = _run(handler_module, body)
    assert result == {"success": False, "error": "Invalid XML"}
    assert forwarded == []
    assert canary.hits == []
    assert "Rejected unsafe XML" in caplog.text
    assert ENTITY_NAME not in caplog.text
    assert "etc/passwd" not in caplog.text


def test_benign_feed_still_processed(handler_module: types.ModuleType) -> None:
    """Positive control: a legitimate Atom notification is unaffected by the fix."""
    result, forwarded = _run(handler_module, _feed("", "Going live"))
    assert result == {"success": True, "channel_id": "UC123", "events_processed": 1}
    assert [e["video_id"] for e in forwarded] == ["vid123"]
    assert forwarded[0]["title"] == "Going live"


def test_malformed_xml_is_invalid(handler_module: types.ModuleType) -> None:
    """Malformed XML keeps returning 'Invalid XML' (ParseError path unchanged)."""
    result, forwarded = _run(handler_module, b"<feed><entry>")
    assert result == {"success": False, "error": "Invalid XML"}
    assert forwarded == []
