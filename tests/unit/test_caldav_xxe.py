"""Regression tests: the Apple CalDAV provider must be XXE / entity-expansion safe.

The CalDAV ``server_url`` is user-configurable, so every PROPFIND / REPORT
response body is attacker-influenceable.  ``libs/calendar_sync/providers/apple.py``
used to parse those bodies with the stdlib ``xml.etree.ElementTree.fromstring``
(no XXE protection); it now goes through ``defusedxml``.

Each hostile payload below (external-entity file read, external-entity SSRF,
internal entity expansion, billion laughs, parameter entity, bare DTD) must be
rejected -- the provider returns an empty result, never the expanded content,
never issues a request to the entity's target, and never logs the
attacker-chosen entity name.

A byte-identical copy of the library ships inside the core-community image
(``services/core-community/libs/calendar_sync``); a drift guard keeps the fix
in both places.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Self

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (REPO_ROOT, Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from _xxe_payloads import (  # noqa: E402
    ENTITY_NAME,
    PAYLOAD_IDS,
    Canary,
    Hostile,
    build_hostile,
)
from defusedxml.common import DefusedXmlException  # noqa: E402

from libs.calendar_sync.providers.apple import (  # noqa: E402
    AppleCalendarProvider,
    _parse_xml,
)

_CAL_URL = "https://caldav.example.test/cal/work/"
_HOME_URL = "https://caldav.example.test/cal/"

_ICAL = (
    "BEGIN:VCALENDAR\n"
    "BEGIN:VEVENT\n"
    "UID:evt-1\n"
    "SUMMARY:Standup{ref}\n"
    "DTSTART:20260101T100000Z\n"
    "DTEND:20260101T103000Z\n"
    "END:VEVENT\n"
    "END:VCALENDAR\n"
)


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


def _wrap(hostile: Hostile, body: str) -> str:
    """Wrap a multistatus body in an XML declaration + the hostile DOCTYPE."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"{hostile.doctype}"
        '<d:multistatus xmlns:d="DAV:" '
        'xmlns:c="urn:ietf:params:xml:ns:caldav" '
        'xmlns:cs="http://calendarserver.org/ns/">'
        f"{body}</d:multistatus>"
    )


def _propfind_body(ref: str) -> str:
    """Return a PROPFIND calendar <response> with ``ref`` in its name and ctag."""
    return (
        "<d:response><d:href>/cal/work/</d:href><d:propstat><d:prop>"
        "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
        f"<d:displayname>Work{ref}</d:displayname>"
        f"<cs:getctag>ctag-1{ref}</cs:getctag>"
        "</d:prop></d:propstat></d:response>"
    )


def _report_body(ref: str) -> str:
    """Return a REPORT <response> whose VEVENT SUMMARY carries ``ref``."""
    return (
        "<d:response><d:href>/cal/work/evt-1.ics</d:href><d:propstat><d:prop>"
        '<d:getetag>"etag-1"</d:getetag>'
        f"<c:calendar-data>{_ICAL.format(ref=ref)}</c:calendar-data>"
        "</d:prop></d:propstat></d:response>"
    )


def _home_body(ref: str) -> str:
    """Return a calendar-home-set <response> whose href carries ``ref``."""
    return (
        "<d:response><d:propstat><d:prop><c:calendar-home-set>"
        f"<d:href>/cal/{ref}</d:href>"
        "</c:calendar-home-set></d:prop></d:propstat></d:response>"
    )


def _provider() -> AppleCalendarProvider:
    """Build an Apple provider aimed at a non-routable example host."""
    return AppleCalendarProvider(
        {"username": "u", "password": "p", "server_url": "https://caldav.example.test"}
    )


class _FakeResponse:
    """Minimal httpx.Response stand-in for a CalDAV 207 Multi-Status."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 207


class _FakeClient:
    """Async-context-manager httpx.AsyncClient stand-in returning a fixed body."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(self._text)


def _serve(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """Make every httpx.AsyncClient in the provider return ``text``."""
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(text))


class TestHostilePayloadsRejected:
    """Every XXE / entity-expansion payload is rejected on every parse path."""

    def test_parse_xml_raises_defused_exception(self, hostile: Hostile) -> None:
        """_parse_xml rejects every hostile payload with a DefusedXmlException."""
        with pytest.raises(DefusedXmlException):
            _parse_xml(_wrap(hostile, _propfind_body(hostile.ref)))

    def test_calendar_listing_is_empty(self, hostile: Hostile, canary: Canary) -> None:
        """A hostile PROPFIND listing yields no calendars and no outbound request."""
        body = _wrap(hostile, _propfind_body(hostile.ref))
        assert _provider()._parse_calendar_propfind(body, _HOME_URL) == []
        assert canary.hits == []

    def test_report_yields_no_events(self, hostile: Hostile, canary: Canary) -> None:
        """A hostile REPORT yields no events and no outbound request."""
        body = _wrap(hostile, _report_body(hostile.ref))
        assert _provider()._parse_report_response(body, _CAL_URL) == []
        assert canary.hits == []

    def test_calendar_home_not_extracted(self, hostile: Hostile, canary: Canary) -> None:
        """A hostile home-set response yields no calendar home."""
        body = _wrap(hostile, _home_body(hostile.ref))
        assert _provider()._extract_calendar_home(body) is None
        assert canary.hits == []

    def test_ctag_fetch_returns_none(
        self,
        hostile: Hostile,
        canary: Canary,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end _get_ctag returns None for a hostile response."""
        _serve(monkeypatch, _wrap(hostile, _propfind_body(hostile.ref)))
        assert asyncio.run(_provider()._get_ctag(_CAL_URL)) is None
        assert canary.hits == []

    def test_calendar_home_discovery_returns_none(
        self,
        hostile: Hostile,
        canary: Canary,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end home discovery returns None for a hostile response."""
        _serve(monkeypatch, _wrap(hostile, _home_body(hostile.ref)))
        assert asyncio.run(_provider()._discover_calendar_home()) is None
        assert canary.hits == []

    def test_attacker_entity_name_not_logged(
        self, hostile: Hostile, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The rejection is logged, without the attacker-chosen entity name."""
        body = _wrap(hostile, _propfind_body(hostile.ref))
        with caplog.at_level(logging.DEBUG):
            _provider()._parse_calendar_propfind(body, _HOME_URL)
        assert "Failed to parse calendar PROPFIND" in caplog.text
        assert ENTITY_NAME not in caplog.text
        assert "etc/passwd" not in caplog.text


class TestBenignResponsesStillParse:
    """Positive control: legitimate CalDAV responses are unaffected by the fix."""

    _DECL = '<?xml version="1.0" encoding="UTF-8"?>'
    _NS = (
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
        'xmlns:cs="http://calendarserver.org/ns/">'
    )

    def _doc(self, body: str) -> str:
        """Wrap ``body`` in a plain (DTD-free) multistatus document."""
        return f"{self._DECL}{self._NS}{body}</d:multistatus>"

    def test_calendar_listing(self) -> None:
        """A benign PROPFIND listing parses into one calendar."""
        cals = _provider()._parse_calendar_propfind(self._doc(_propfind_body("")), _HOME_URL)
        assert [(c["name"], c["_ctag"]) for c in cals] == [("Work", "ctag-1")]

    def test_report_events(self) -> None:
        """A benign REPORT parses into one event."""
        events = _provider()._parse_report_response(self._doc(_report_body("")), _CAL_URL)
        assert len(events) == 1
        assert events[0]["uid"] == "evt-1"
        assert events[0]["summary"] == "Standup"
        assert events[0]["etag"] == '"etag-1"'
        assert events[0]["calendar_id"] == _CAL_URL

    def test_calendar_home(self) -> None:
        """A benign home-set response yields the calendar home URL."""
        home = _provider()._extract_calendar_home(self._doc(_home_body("")))
        assert home == "https://caldav.example.test/cal/"

    def test_ctag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end _get_ctag returns the ctag of a benign response."""
        _serve(monkeypatch, self._doc(_propfind_body("")))
        assert asyncio.run(_provider()._get_ctag(_CAL_URL)) == "ctag-1"

    def test_malformed_xml_is_not_fatal(self) -> None:
        """Malformed XML is logged and yields an empty result, never an exception."""
        assert _provider()._parse_calendar_propfind("<d:multistatus", _HOME_URL) == []
        assert _provider()._extract_calendar_home("not xml at all") is None


class TestCoreCommunityCopyInSync:
    """The core-community image bundles a copy of libs/calendar_sync."""

    def test_apple_provider_copy_is_byte_identical(self) -> None:
        """The bundled copy must carry the same XXE fix as the canonical module."""
        canonical = REPO_ROOT / "libs/calendar_sync/providers/apple.py"
        bundled = REPO_ROOT / "services/core-community/libs/calendar_sync/providers/apple.py"
        assert canonical.read_bytes() == bundled.read_bytes()
