"""AppleCalendarProvider — Apple Calendar / iCloud CalDAV integration.

Authentication
--------------
Credentials dict must contain:
    username      (str)  — Apple ID or CalDAV username
    password      (str)  — App-specific password (NOT the Apple ID password)
    server_url    (str)  — CalDAV server URL
                           e.g. "https://caldav.icloud.com" for iCloud
                               "https://caldav.example.com" for self-hosted

Sync strategy: WebDAV ctag/etag based change detection.  A PROPFIND request
fetches the collection ctag; if unchanged since the last sync, no events have
been modified.  Changed events are fetched via a CALENDAR-REPORT (REPORT with
calendar-query) using the iCalendar VEVENT component filter.

Protocol
--------
CalDAV (RFC 4791) uses standard HTTP verbs plus WebDAV extensions:
    PROPFIND  — Discover calendars and collection properties (ctag, displayname)
    REPORT    — Query events (calendar-multiget / calendar-query)
    PUT       — Create or update an event (iCalendar payload)
    DELETE    — Delete an event

This implementation parses minimal iCalendar (RFC 5545) manually to avoid a
hard dependency on icalendar/vobject.  For production use consider adding
the `icalendar` package and replacing _parse_vcal / _build_vcal with it.
"""
import logging
import re
import uuid as uuid_mod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import httpx

from libs.calendar_sync.base import CalendarProviderBase
from libs.calendar_sync.schema import normalize_event

logger = logging.getLogger(__name__)

# XML namespaces used in CalDAV / WebDAV requests and responses.
_NS = {
    "d": "DAV:",
    "c": "urn:ietf:params:xml:ns:caldav",
    "cs": "http://calendarserver.org/ns/",
    "ical": "http://apple.com/ns/ical/",
}
# Register so ET.tostring() uses human-readable prefixes.
for _prefix, _uri in _NS.items():
    ET.register_namespace(_prefix, _uri)


class AppleCalendarProvider(CalendarProviderBase):
    """Apple Calendar / iCloud CalDAV provider.

    Uses Basic Auth with an app-specific password as required by Apple.
    """

    PROVIDER = "apple"

    def __init__(self, credentials: Dict[str, Any]):
        super().__init__(credentials)
        self._username: str = credentials.get("username", "")
        self._password: str = credentials.get("password", "")
        self._server_url: str = credentials.get("server_url", "").rstrip("/")
        # Maps calendar_id → last-known ctag (for change detection).
        self._ctag_cache: Dict[str, str] = {}

    # ──────────────────────────────────────────────────────────────────────
    # Authentication
    # ──────────────────────────────────────────────────────────────────────

    async def authenticate(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        """Validate CalDAV Basic Auth credentials with a lightweight PROPFIND.

        CalDAV uses HTTP Basic Auth (no OAuth); this method verifies that the
        server accepts the credentials by issuing a minimal PROPFIND on the
        principal resource.

        Args:
            credentials: Credential dict with username, password, server_url.

        Returns:
            Unchanged credentials dict (CalDAV does not issue tokens).

        Raises:
            ValueError: If the server rejects the credentials.
        """
        self._username = credentials.get("username", self._username)
        self._password = credentials.get("password", self._password)
        self._server_url = credentials.get("server_url", self._server_url).rstrip("/")

        url = f"{self._server_url}/"
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<d:propfind xmlns:d="DAV:">'
            "<d:prop><d:current-user-principal/></d:prop>"
            "</d:propfind>"
        )
        try:
            async with httpx.AsyncClient(
                auth=(self._username, self._password), timeout=15.0
            ) as client:
                resp = await client.request(
                    "PROPFIND", url,
                    content=body.encode(),
                    headers={"Depth": "0", "Content-Type": "application/xml"},
                )
                self._log_api_call("PROPFIND", url, resp.status_code)
                if resp.status_code in (401, 403):
                    raise ValueError(
                        f"[APPLE] Authentication failed for {self._username}: "
                        f"HTTP {resp.status_code}"
                    )
        except httpx.RequestError as exc:
            raise ValueError(f"[APPLE] Cannot reach CalDAV server: {exc}") from exc

        return credentials

    def _auth(self) -> Tuple[str, str]:
        return (self._username, self._password)

    # ──────────────────────────────────────────────────────────────────────
    # Calendar-level operations
    # ──────────────────────────────────────────────────────────────────────

    async def list_calendars(self) -> List[Dict[str, Any]]:
        """Discover all CalDAV calendars for the authenticated user.

        Issues a PROPFIND Depth:1 against the user's calendar home to
        enumerate calendar collections.
        """
        # Discover the calendar-home-set path first.
        home_url = await self._discover_calendar_home()
        if not home_url:
            self.logger.error("[APPLE] Could not discover calendar-home-set.")
            return []

        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"'
            ' xmlns:cs="http://calendarserver.org/ns/">'
            "<d:prop>"
            "<d:displayname/>"
            "<d:resourcetype/>"
            "<cs:getctag/>"
            "<c:calendar-description/>"
            "</d:prop>"
            "</d:propfind>"
        )
        try:
            async with httpx.AsyncClient(
                auth=self._auth(), timeout=30.0
            ) as client:
                resp = await client.request(
                    "PROPFIND", home_url,
                    content=body.encode(),
                    headers={"Depth": "1", "Content-Type": "application/xml"},
                )
                self._log_api_call("PROPFIND", home_url, resp.status_code)
                if resp.status_code not in (207, 200):
                    self.logger.error(
                        f"[APPLE] list_calendars PROPFIND failed: {resp.status_code}"
                    )
                    return []

                return self._parse_calendar_propfind(resp.text, home_url)

        except Exception as exc:
            self._log_error("list_calendars", exc)
            return []

    async def create_calendar(
        self,
        name: str,
        description: Optional[str] = None,
        time_zone: str = "UTC",
    ) -> Dict[str, Any]:
        """Create a new calendar collection on the CalDAV server."""
        home_url = await self._discover_calendar_home()
        if not home_url:
            raise ValueError("[APPLE] Cannot create calendar: calendar home not found.")

        cal_uid = str(uuid_mod.uuid4())
        cal_url = f"{home_url.rstrip('/')}/{cal_uid}/"

        desc_xml = (
            f'<c:calendar-description xmlns:c="urn:ietf:params:xml:ns:caldav">'
            f"{description or ''}</c:calendar-description>"
        ) if description else ""

        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<d:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:set><d:prop>"
            f"<d:displayname>{name}</d:displayname>"
            f"{desc_xml}"
            "</d:prop></d:set>"
            "</d:mkcalendar>"
        )
        try:
            async with httpx.AsyncClient(
                auth=self._auth(), timeout=30.0
            ) as client:
                resp = await client.request(
                    "MKCALENDAR", cal_url,
                    content=body.encode(),
                    headers={"Content-Type": "application/xml"},
                )
                self._log_api_call("MKCALENDAR", cal_url, resp.status_code)
                if resp.status_code not in (201, 207):
                    raise ValueError(
                        f"[APPLE] create_calendar failed: HTTP {resp.status_code}"
                    )

            return {
                "id": cal_url,
                "name": name,
                "description": description,
                "primary": False,
                "read_only": False,
                "time_zone": time_zone,
            }

        except Exception as exc:
            self._log_error("create_calendar", exc)
            raise

    # ──────────────────────────────────────────────────────────────────────
    # Event-level operations
    # ──────────────────────────────────────────────────────────────────────

    async def get_events(
        self,
        calendar_id: str,
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
        sync_token: Optional[str] = None,
        page_token: Optional[str] = None,
        max_results: int = 250,
    ) -> Dict[str, Any]:
        """Fetch events from a CalDAV calendar using a CALENDAR-QUERY REPORT.

        CalDAV does not support server-side pagination in the same way as
        REST APIs; all matching events are returned in a single response.

        Args:
            calendar_id: CalDAV calendar collection URL.
            time_min: RFC 3339 lower bound (converted to iCal DTSTART filter).
            time_max: RFC 3339 upper bound (converted to iCal DTEND filter).
            sync_token: Not used for CalDAV full-fetch; use sync_changes instead.
            page_token: Not applicable; CalDAV returns all results at once.
            max_results: Ignored (CalDAV returns all matching events).

        Returns:
            Standard get_events dict with events, next_page_token, next_sync_token.
        """
        time_filter_xml = self._build_time_filter_xml(time_min, time_max)
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
            "<c:filter>"
            '<c:comp-filter name="VCALENDAR">'
            f'<c:comp-filter name="VEVENT">{time_filter_xml}</c:comp-filter>'
            "</c:comp-filter>"
            "</c:filter>"
            "</c:calendar-query>"
        )
        try:
            async with httpx.AsyncClient(
                auth=self._auth(), timeout=60.0
            ) as client:
                resp = await client.request(
                    "REPORT", calendar_id,
                    content=body.encode(),
                    headers={"Depth": "1", "Content-Type": "application/xml"},
                )
                self._log_api_call("REPORT", calendar_id, resp.status_code)
                if resp.status_code not in (207, 200):
                    raise ValueError(
                        f"[APPLE] get_events REPORT failed: HTTP {resp.status_code}"
                    )

                events = self._parse_report_response(resp.text, calendar_id)
                # Fetch current ctag for the collection for use as sync token.
                ctag = await self._get_ctag(calendar_id)
                return {
                    "events": events,
                    "next_page_token": None,
                    "next_sync_token": ctag,
                }

        except Exception as exc:
            self._log_error("get_events", exc)
            raise

    async def create_event(
        self,
        calendar_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create a new event in the CalDAV calendar using HTTP PUT.

        Args:
            calendar_id: CalDAV calendar collection URL.
            event: Canonical (or denormalized apple) event dict.

        Returns:
            Normalised canonical event dict.
        """
        # event may already be denormalized by sync_engine; if it has "dtstart"
        # it is already apple-format.  Otherwise convert from canonical.
        if "dtstart" not in event:
            from libs.calendar_sync.schema import denormalize_event
            apple_event = denormalize_event(event, self.PROVIDER)
        else:
            apple_event = event

        uid = apple_event.get("uid") or str(uuid_mod.uuid4())
        apple_event["uid"] = uid

        ical_text = self._build_vcal(apple_event)
        event_url = f"{calendar_id.rstrip('/')}/{uid}.ics"

        try:
            async with httpx.AsyncClient(
                auth=self._auth(), timeout=30.0
            ) as client:
                resp = await client.put(
                    event_url,
                    content=ical_text.encode("utf-8"),
                    headers={
                        "Content-Type": "text/calendar; charset=utf-8",
                        "If-None-Match": "*",
                    },
                )
                self._log_api_call("PUT", event_url, resp.status_code)
                if resp.status_code not in (201, 204):
                    raise ValueError(
                        f"[APPLE] create_event PUT failed: HTTP {resp.status_code}"
                    )

            etag = resp.headers.get("ETag", "")
            apple_event["uid"] = uid
            apple_event["calendar_id"] = calendar_id
            apple_event["etag"] = etag
            return normalize_event(apple_event, self.PROVIDER)

        except Exception as exc:
            self._log_error("create_event", exc)
            raise

    async def update_event(
        self,
        calendar_id: str,
        event_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Update an existing CalDAV event using HTTP PUT with If-Match ETag.

        Args:
            calendar_id: CalDAV calendar collection URL.
            event_id: The UID of the event (used to construct the resource URL).
            event: Canonical (or denormalized apple) event dict.

        Returns:
            Normalised canonical event dict.
        """
        if "dtstart" not in event:
            from libs.calendar_sync.schema import denormalize_event
            apple_event = denormalize_event(event, self.PROVIDER)
        else:
            apple_event = event

        apple_event["uid"] = event_id
        ical_text = self._build_vcal(apple_event)
        event_url = f"{calendar_id.rstrip('/')}/{event_id}.ics"
        etag = apple_event.get("etag", "")

        try:
            headers: Dict[str, str] = {"Content-Type": "text/calendar; charset=utf-8"}
            if etag:
                headers["If-Match"] = etag

            async with httpx.AsyncClient(
                auth=self._auth(), timeout=30.0
            ) as client:
                resp = await client.put(
                    event_url,
                    content=ical_text.encode("utf-8"),
                    headers=headers,
                )
                self._log_api_call("PUT", event_url, resp.status_code)
                if resp.status_code not in (201, 204):
                    raise ValueError(
                        f"[APPLE] update_event PUT failed: HTTP {resp.status_code}"
                    )

            new_etag = resp.headers.get("ETag", etag)
            apple_event["calendar_id"] = calendar_id
            apple_event["etag"] = new_etag
            return normalize_event(apple_event, self.PROVIDER)

        except Exception as exc:
            self._log_error("update_event", exc)
            raise

    async def delete_event(
        self,
        calendar_id: str,
        event_id: str,
    ) -> bool:
        """Delete a CalDAV event resource using HTTP DELETE.

        Args:
            calendar_id: CalDAV calendar collection URL.
            event_id: The UID of the event.

        Returns:
            True on success or if the event is already absent.
        """
        event_url = f"{calendar_id.rstrip('/')}/{event_id}.ics"
        try:
            async with httpx.AsyncClient(
                auth=self._auth(), timeout=30.0
            ) as client:
                resp = await client.delete(event_url, auth=self._auth())
                self._log_api_call("DELETE", event_url, resp.status_code)
                return resp.status_code in (204, 404)

        except Exception as exc:
            self._log_error("delete_event", exc)
            return False

    # ──────────────────────────────────────────────────────────────────────
    # Incremental sync (ctag-based)
    # ──────────────────────────────────────────────────────────────────────

    async def sync_changes(
        self,
        calendar_id: str,
        sync_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Detect and return changed events since the last sync token (ctag).

        CalDAV does not natively expose changed-only events via a simple token;
        the standard approach is:
        1. Fetch the collection ctag.
        2. If ctag matches the last-known value, nothing changed.
        3. Otherwise, fetch all ETags via a minimal PROPFIND.
        4. Compare against a locally stored ETag map to identify changes/deletions.
        5. Fetch changed event bodies and return.

        For simplicity (and because WaddleBot maintains the sync map), step 3-4
        are approximated here by returning ALL events when the ctag changes and
        delegating deduplication to the sync engine's conflict resolution.

        Args:
            calendar_id: CalDAV calendar collection URL.
            sync_token: ctag from a previous sync_changes call.

        Returns:
            Tuple of (changed_events, new_ctag).
        """
        current_ctag = await self._get_ctag(calendar_id)

        if sync_token and current_ctag == sync_token:
            # Nothing changed.
            logger.debug(
                f"[APPLE] No changes for calendar {calendar_id} (ctag unchanged)."
            )
            return [], current_ctag

        logger.info(
            f"[APPLE] Calendar {calendar_id} changed (old_ctag={sync_token!r}, "
            f"new_ctag={current_ctag!r}); fetching all events."
        )

        try:
            result = await self.get_events(calendar_id=calendar_id)
            return result.get("events", []), current_ctag
        except Exception as exc:
            self._log_error("sync_changes", exc)
            return [], current_ctag

    # ──────────────────────────────────────────────────────────────────────
    # CalDAV helpers
    # ──────────────────────────────────────────────────────────────────────

    async def _discover_calendar_home(self) -> Optional[str]:
        """Discover the calendar-home-set URL for the authenticated user."""
        url = f"{self._server_url}/"
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop>"
            "<d:current-user-principal/>"
            "<c:calendar-home-set/>"
            "</d:prop>"
            "</d:propfind>"
        )
        try:
            async with httpx.AsyncClient(
                auth=self._auth(), timeout=15.0
            ) as client:
                resp = await client.request(
                    "PROPFIND", url,
                    content=body.encode(),
                    headers={"Depth": "0", "Content-Type": "application/xml"},
                )
                self._log_api_call("PROPFIND", url, resp.status_code)
                if resp.status_code not in (207, 200):
                    return None

            return self._extract_calendar_home(resp.text)
        except Exception as exc:
            self._log_error("_discover_calendar_home", exc)
            return None

    def _extract_calendar_home(self, xml_text: str) -> Optional[str]:
        """Parse PROPFIND response to extract calendar-home-set href."""
        try:
            root = ET.fromstring(xml_text)
            # Try calendar-home-set first.
            for tag in (
                "{urn:ietf:params:xml:ns:caldav}calendar-home-set",
                "{DAV:}current-user-principal",
            ):
                elem = root.find(f".//{tag}/{{DAV:}}href")
                if elem is not None and elem.text:
                    href = elem.text.strip()
                    if href.startswith("/"):
                        return f"{self._server_url}{href}"
                    return href
        except ET.ParseError as exc:
            self.logger.error(f"[APPLE] Failed to parse PROPFIND XML: {exc}")
        return None

    async def _get_ctag(self, calendar_id: str) -> Optional[str]:
        """Fetch the current ctag for a calendar collection."""
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
            "<d:prop><cs:getctag/></d:prop>"
            "</d:propfind>"
        )
        try:
            async with httpx.AsyncClient(
                auth=self._auth(), timeout=15.0
            ) as client:
                resp = await client.request(
                    "PROPFIND", calendar_id,
                    content=body.encode(),
                    headers={"Depth": "0", "Content-Type": "application/xml"},
                )
                self._log_api_call("PROPFIND", calendar_id, resp.status_code)
                if resp.status_code not in (207, 200):
                    return None

            root = ET.fromstring(resp.text)
            ctag_elem = root.find(
                ".//{http://calendarserver.org/ns/}getctag"
            )
            return ctag_elem.text.strip() if ctag_elem is not None else None
        except Exception as exc:
            self._log_error("_get_ctag", exc)
            return None

    def _parse_calendar_propfind(
        self, xml_text: str, home_url: str
    ) -> List[Dict[str, Any]]:
        """Parse a Depth:1 PROPFIND response to extract calendar collections."""
        calendars: List[Dict[str, Any]] = []
        try:
            root = ET.fromstring(xml_text)
            for response in root.findall("{DAV:}response"):
                # Skip the home collection itself.
                href_elem = response.find("{DAV:}href")
                href = (href_elem.text or "").strip() if href_elem is not None else ""

                # Check resourcetype contains calendar.
                res_type = response.find(
                    ".//{DAV:}resourcetype/{urn:ietf:params:xml:ns:caldav}calendar"
                )
                if res_type is None:
                    continue  # Not a calendar collection.

                name_elem = response.find(".//{DAV:}displayname")
                name = (name_elem.text or "").strip() if name_elem is not None else ""
                ctag_elem = response.find(
                    ".//{http://calendarserver.org/ns/}getctag"
                )
                ctag = (ctag_elem.text or "").strip() if ctag_elem is not None else ""

                cal_url = href if href.startswith("http") else f"{self._server_url}{href}"
                calendars.append({
                    "id": cal_url,
                    "name": name or cal_url,
                    "description": None,
                    "primary": False,
                    "read_only": False,
                    "time_zone": "UTC",
                    "_ctag": ctag,
                })
        except ET.ParseError as exc:
            self.logger.error(f"[APPLE] Failed to parse calendar PROPFIND: {exc}")
        return calendars

    def _parse_report_response(
        self, xml_text: str, calendar_id: str
    ) -> List[Dict[str, Any]]:
        """Parse a CALENDAR-QUERY REPORT response and extract event dicts."""
        events: List[Dict[str, Any]] = []
        try:
            root = ET.fromstring(xml_text)
            for response in root.findall("{DAV:}response"):
                etag_elem = response.find(".//{DAV:}getetag")
                etag = (
                    (etag_elem.text or "").strip()
                    if etag_elem is not None
                    else ""
                )
                cal_data_elem = response.find(
                    ".//{urn:ietf:params:xml:ns:caldav}calendar-data"
                )
                if cal_data_elem is None or not cal_data_elem.text:
                    continue

                parsed = self._parse_vcal(cal_data_elem.text)
                if parsed:
                    parsed["calendar_id"] = calendar_id
                    parsed["etag"] = etag
                    events.append(parsed)

        except ET.ParseError as exc:
            self.logger.error(f"[APPLE] Failed to parse REPORT response: {exc}")
        return events

    def _parse_vcal(self, ical_text: str) -> Optional[Dict[str, Any]]:
        """Parse a VCALENDAR/VEVENT iCalendar string into a plain dict.

        This is a lightweight parser that extracts the most common VEVENT
        properties.  For complex rrule / timezone handling in production,
        use the `icalendar` package instead.
        """
        in_vevent = False
        event: Dict[str, Any] = {"attendees": []}

        for raw_line in ical_text.splitlines():
            line = raw_line.strip()
            if line == "BEGIN:VEVENT":
                in_vevent = True
                continue
            if line == "END:VEVENT":
                break
            if not in_vevent:
                continue

            if ":" not in line:
                continue

            prop, _, value = line.partition(":")
            prop_upper = prop.upper().split(";")[0]

            params_str = prop[len(prop_upper):]
            params: Dict[str, str] = {}
            for param_token in params_str.split(";"):
                if "=" in param_token:
                    k, _, v = param_token.partition("=")
                    params[k.strip().upper()] = v.strip()

            if prop_upper == "UID":
                event["uid"] = value
            elif prop_upper == "SUMMARY":
                event["summary"] = value
            elif prop_upper == "DESCRIPTION":
                event["description"] = value.replace("\\n", "\n")
            elif prop_upper == "LOCATION":
                event["location"] = value
            elif prop_upper == "DTSTART":
                event["dtstart"] = self._ical_dt_to_iso(value, params)
                event["all_day"] = "DATE" in params.get("VALUE", "") or (
                    len(value) == 8 and "T" not in value
                )
                event["time_zone"] = params.get("TZID", "UTC")
            elif prop_upper == "DTEND":
                event["dtend"] = self._ical_dt_to_iso(value, params)
            elif prop_upper == "STATUS":
                event["status"] = value
            elif prop_upper == "RRULE":
                event["rrule"] = value
            elif prop_upper == "URL":
                event["url"] = value
            elif prop_upper == "CREATED":
                event["created"] = self._ical_dt_to_iso(value, {})
            elif prop_upper == "LAST-MODIFIED":
                event["last_modified"] = self._ical_dt_to_iso(value, {})
            elif prop_upper == "ORGANIZER":
                event["organizer"] = value
                event["organizer_cn"] = params.get("CN")
            elif prop_upper == "ATTENDEE":
                event["attendees"].append({
                    "value": value,
                    "cn": params.get("CN"),
                    "partstat": params.get("PARTSTAT", "NEEDS-ACTION"),
                })

        return event if event.get("uid") else None

    @staticmethod
    def _ical_dt_to_iso(value: str, params: Dict[str, str]) -> str:
        """Convert an iCalendar DTSTART/DTEND value to an ISO 8601 string.

        Handles:
        - DATE format: YYYYMMDD → YYYY-MM-DD
        - DATE-TIME (floating): YYYYMMDDTHHmmss → YYYY-MM-DDTHH:mm:ss
        - DATE-TIME (UTC): YYYYMMDDTHHmmssZ → YYYY-MM-DDTHH:mm:ssZ
        """
        value = value.strip()
        if len(value) == 8 and "T" not in value:
            return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
        if "T" in value:
            date_part = value[:8]
            time_part = value[9:15] if len(value) >= 15 else value[9:]
            utc = value.endswith("Z")
            iso = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}T{time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}"
            return f"{iso}Z" if utc else iso
        return value

    def _build_time_filter_xml(
        self, time_min: Optional[str], time_max: Optional[str]
    ) -> str:
        """Build a CalDAV time-range XML filter fragment."""
        if not time_min and not time_max:
            return ""
        attrs = []
        if time_min:
            # Convert ISO 8601 to iCal UTC format YYYYMMDDTHHmmssZ.
            attrs.append(f'start="{self._iso_to_ical_utc(time_min)}"')
        if time_max:
            attrs.append(f'end="{self._iso_to_ical_utc(time_max)}"')
        return (
            f'<c:time-range xmlns:c="urn:ietf:params:xml:ns:caldav" '
            f'{" ".join(attrs)}/>'
        )

    @staticmethod
    def _iso_to_ical_utc(iso: str) -> str:
        """Convert an RFC 3339 datetime string to iCal UTC format."""
        # Strip timezone suffix, normalise separators, append Z.
        clean = re.sub(r"[:\-]", "", iso.replace("T", "T").split("+")[0].rstrip("Z"))
        if "T" not in clean:
            clean = f"{clean}T000000"
        return f"{clean}Z"

    def _build_vcal(self, event: Dict[str, Any]) -> str:
        """Serialise an apple-format event dict to a VCALENDAR iCal string."""
        uid = event.get("uid") or str(uuid_mod.uuid4())
        summary = event.get("summary", "")
        description = (event.get("description") or "").replace("\n", "\\n")
        location = event.get("location", "")
        dtstart = event.get("dtstart", "")
        dtend = event.get("dtend", "")
        status = (event.get("status") or "CONFIRMED").upper()
        rrule = event.get("rrule")
        tzid = event.get("time_zone") or event.get("tzid", "UTC")
        all_day = event.get("all_day", False)

        def fmt_dt(dt: str, all_day_flag: bool, tz: str) -> str:
            # Strip separators for iCal format.
            clean = re.sub(r"[:\-]", "", dt.split("T")[0]) if all_day_flag else ""
            if all_day_flag:
                return f";VALUE=DATE:{clean}"
            # For date-time, include TZID param.
            raw = re.sub(r"[:\-]", "", dt)
            raw = raw.replace("T", "T")
            if raw.endswith("Z"):
                return f":{raw}"
            return f";TZID={tz}:{raw}"

        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//WaddleBot//Calendar Sync//EN",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"SUMMARY:{summary}",
        ]

        dtstart_str = fmt_dt(dtstart, all_day, tzid)
        dtend_str = fmt_dt(dtend, all_day, tzid)
        lines.append(f"DTSTART{dtstart_str}")
        lines.append(f"DTEND{dtend_str}")

        if description:
            lines.append(f"DESCRIPTION:{description}")
        if location:
            lines.append(f"LOCATION:{location}")
        lines.append(f"STATUS:{status}")
        if rrule:
            lines.append(f"RRULE:{rrule}")

        organizer = event.get("organizer")
        organizer_cn = event.get("organizer_cn")
        if organizer:
            cn_part = f"CN={organizer_cn};" if organizer_cn else ""
            lines.append(f"ORGANIZER;{cn_part}{organizer}")

        for att in event.get("attendees", []):
            partstat = att.get("partstat", "NEEDS-ACTION")
            cn = att.get("cn")
            cn_part = f"CN={cn};" if cn else ""
            lines.append(
                f"ATTENDEE;{cn_part}PARTSTAT={partstat}:{att.get('value', '')}"
            )

        now_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines.extend([
            f"DTSTAMP:{now_str}",
            f"LAST-MODIFIED:{now_str}",
            "END:VEVENT",
            "END:VCALENDAR",
        ])

        return "\r\n".join(lines) + "\r\n"
