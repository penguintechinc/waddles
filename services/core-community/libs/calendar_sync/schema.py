"""Calendar Sync Schema — canonical event format and provider normalisation helpers.

All calendar providers (Google, Microsoft, Apple) use different field names and
data representations.  This module defines a single canonical event dict and
provides normalize_event() / denormalize_event() to convert to/from each
provider's native shape.

Canonical Event Dict
--------------------
{
    "id":           str,        # WaddleBot-internal UUID (set after first save)
    "provider_id":  str,        # Provider-assigned event identifier
    "calendar_id":  str,        # Provider calendar identifier
    "title":        str,        # Summary / subject line
    "description":  str | None, # Body / notes
    "location":     str | None, # Physical or virtual location string
    "start":        str,        # RFC 3339 datetime or date (all-day)
    "end":          str,        # RFC 3339 datetime or date (all-day)
    "all_day":      bool,       # True for date-only events
    "time_zone":    str,        # IANA time zone string
    "status":       str,        # "confirmed" | "tentative" | "cancelled"
    "recurrence":   list | None,# RRULE strings (RFC 5545) or None
    "organizer":    {
        "email":    str | None,
        "name":     str | None,
    },
    "attendees":    [           # Empty list if none
        {
            "email":  str,
            "name":   str | None,
            "status": str,      # "accepted" | "declined" | "tentative" | "needs_action"
        }
    ],
    "html_link":    str | None, # Deep link to event in provider's UI
    "created_at":   str | None, # RFC 3339 creation timestamp
    "updated_at":   str | None, # RFC 3339 last-modification timestamp
    "etag":         str | None, # Provider ETag / change-detection token
    "raw":          dict,       # Original provider payload (for debugging)
}
"""
from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

# Human-readable display names for each provider identifier.
PROVIDER_NAMES: Dict[str, str] = {
    "google": "Google Calendar",
    "microsoft": "Microsoft Outlook",
    "apple": "Apple Calendar",
}

# Mapping of provider-specific attendee status strings → canonical values.
_GOOGLE_ATTENDEE_STATUS: Dict[str, str] = {
    "accepted": "accepted",
    "declined": "declined",
    "tentative": "tentative",
    "needsAction": "needs_action",
}

_MICROSOFT_ATTENDEE_STATUS: Dict[str, str] = {
    "accepted": "accepted",
    "declined": "declined",
    "tentativelyAccepted": "tentative",
    "none": "needs_action",
}

_MICROSOFT_EVENT_STATUS: Dict[str, str] = {
    "normal": "confirmed",
    "cancelled": "cancelled",
    "tentative": "tentative",
}


# ──────────────────────────────────────────────────────────────────────────────
# Normalisation: provider → canonical
# ──────────────────────────────────────────────────────────────────────────────

def normalize_event(raw: Dict[str, Any], provider: str) -> Dict[str, Any]:
    """Convert a provider-native event dict to the canonical WaddleBot format.

    Args:
        raw: The raw event dict returned by the provider API.
        provider: One of "google", "microsoft", "apple".

    Returns:
        Canonical event dict.  The "raw" key always contains the original
        payload so callers can access provider-specific fields if needed.

    Raises:
        ValueError: If the provider identifier is not recognised.
    """
    if provider == "google":
        return _normalize_google(raw)
    if provider == "microsoft":
        return _normalize_microsoft(raw)
    if provider == "apple":
        return _normalize_apple(raw)
    raise ValueError(f"Unknown provider: {provider!r}. Valid providers: {list(PROVIDER_NAMES)}")


def _normalize_google(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a Google Calendar API v3 event object."""
    start = raw.get("start", {})
    end = raw.get("end", {})
    all_day = "date" in start and "dateTime" not in start

    organizer_raw = raw.get("organizer", {})
    organizer = {
        "email": organizer_raw.get("email"),
        "name": organizer_raw.get("displayName"),
    }

    attendees: List[Dict[str, Any]] = []
    for a in raw.get("attendees", []):
        attendees.append({
            "email": a.get("email", ""),
            "name": a.get("displayName"),
            "status": _GOOGLE_ATTENDEE_STATUS.get(
                a.get("responseStatus", "needsAction"), "needs_action"
            ),
        })

    return {
        "id": None,
        "provider_id": raw.get("id", ""),
        "calendar_id": raw.get("calendarId", ""),
        "title": raw.get("summary", ""),
        "description": raw.get("description"),
        "location": raw.get("location"),
        "start": start.get("dateTime") or start.get("date", ""),
        "end": end.get("dateTime") or end.get("date", ""),
        "all_day": all_day,
        "time_zone": (
            start.get("timeZone")
            or end.get("timeZone")
            or "UTC"
        ),
        "status": raw.get("status", "confirmed"),
        "recurrence": raw.get("recurrence"),
        "organizer": organizer,
        "attendees": attendees,
        "html_link": raw.get("htmlLink"),
        "created_at": raw.get("created"),
        "updated_at": raw.get("updated"),
        "etag": raw.get("etag"),
        "raw": raw,
    }


def _normalize_microsoft(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a Microsoft Graph API event object."""
    start_raw = raw.get("start", {})
    end_raw = raw.get("end", {})
    all_day = raw.get("isAllDay", False)

    # Microsoft stores times in the timeZone field alongside dateTime.
    start_dt = start_raw.get("dateTime", "")
    end_dt = end_raw.get("dateTime", "")
    time_zone = start_raw.get("timeZone", "UTC")

    organizer_raw = raw.get("organizer", {}).get("emailAddress", {})
    organizer = {
        "email": organizer_raw.get("address"),
        "name": organizer_raw.get("name"),
    }

    attendees: List[Dict[str, Any]] = []
    for a in raw.get("attendees", []):
        ea = a.get("emailAddress", {})
        status_raw = a.get("status", {}).get("response", "none")
        attendees.append({
            "email": ea.get("address", ""),
            "name": ea.get("name"),
            "status": _MICROSOFT_ATTENDEE_STATUS.get(status_raw, "needs_action"),
        })

    ms_status = raw.get("showAs", "normal")
    if raw.get("isCancelled", False):
        ms_status = "cancelled"
    canonical_status = _MICROSOFT_EVENT_STATUS.get(ms_status, "confirmed")

    # Microsoft uses recurrencePattern rather than RRULE strings; carry raw.
    recurrence = None
    if raw.get("recurrence"):
        recurrence = [str(raw["recurrence"])]

    return {
        "id": None,
        "provider_id": raw.get("id", ""),
        "calendar_id": raw.get("calendarId", ""),
        "title": raw.get("subject", ""),
        "description": raw.get("bodyPreview") or (
            raw.get("body", {}).get("content")
        ),
        "location": (raw.get("location") or {}).get("displayName"),
        "start": start_dt,
        "end": end_dt,
        "all_day": all_day,
        "time_zone": time_zone,
        "status": canonical_status,
        "recurrence": recurrence,
        "organizer": organizer,
        "attendees": attendees,
        "html_link": raw.get("webLink"),
        "created_at": raw.get("createdDateTime"),
        "updated_at": raw.get("lastModifiedDateTime"),
        "etag": raw.get("@odata.etag"),
        "raw": raw,
    }


def _normalize_apple(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a parsed iCalendar (CalDAV REPORT) event component.

    The raw dict here is produced by the AppleCalendarProvider which parses
    VCALENDAR/VEVENT iCal text into a plain Python dict before passing it here.
    Expected keys mirror the iCalendar property names in lowercase.
    """
    all_day = raw.get("all_day", False)

    organizer_raw = raw.get("organizer", "")
    # ORGANIZER value is typically "mailto:user@example.com"
    organizer_email = organizer_raw.replace("mailto:", "") if organizer_raw else None
    organizer = {
        "email": organizer_email or None,
        "name": raw.get("organizer_cn"),
    }

    attendees: List[Dict[str, Any]] = []
    for a in raw.get("attendees", []):
        email = (a.get("value", "") or "").replace("mailto:", "")
        part_stat = (a.get("partstat", "NEEDS-ACTION") or "NEEDS-ACTION").upper()
        status_map = {
            "ACCEPTED": "accepted",
            "DECLINED": "declined",
            "TENTATIVE": "tentative",
            "NEEDS-ACTION": "needs_action",
        }
        attendees.append({
            "email": email,
            "name": a.get("cn"),
            "status": status_map.get(part_stat, "needs_action"),
        })

    rrule = raw.get("rrule")
    recurrence = [f"RRULE:{rrule}"] if rrule else None

    status_raw = (raw.get("status") or "CONFIRMED").upper()
    status_map2 = {
        "CONFIRMED": "confirmed",
        "TENTATIVE": "tentative",
        "CANCELLED": "cancelled",
    }

    return {
        "id": None,
        "provider_id": raw.get("uid", ""),
        "calendar_id": raw.get("calendar_id", ""),
        "title": raw.get("summary", ""),
        "description": raw.get("description"),
        "location": raw.get("location"),
        "start": raw.get("dtstart", ""),
        "end": raw.get("dtend", ""),
        "all_day": all_day,
        "time_zone": raw.get("time_zone", "UTC"),
        "status": status_map2.get(status_raw, "confirmed"),
        "recurrence": recurrence,
        "organizer": organizer,
        "attendees": attendees,
        "html_link": raw.get("url"),
        "created_at": raw.get("created"),
        "updated_at": raw.get("last_modified"),
        "etag": raw.get("etag"),
        "raw": raw,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Denormalisation: canonical → provider
# ──────────────────────────────────────────────────────────────────────────────

def denormalize_event(event: Dict[str, Any], provider: str) -> Dict[str, Any]:
    """Convert a canonical WaddleBot event dict to a provider-native payload.

    Used when pushing events to an external calendar.

    Args:
        event: Canonical event dict.
        provider: One of "google", "microsoft", "apple".

    Returns:
        Provider-native event dict ready to be submitted to the API.

    Raises:
        ValueError: If the provider identifier is not recognised.
    """
    if provider == "google":
        return _denormalize_google(event)
    if provider == "microsoft":
        return _denormalize_microsoft(event)
    if provider == "apple":
        return _denormalize_apple(event)
    raise ValueError(f"Unknown provider: {provider!r}. Valid providers: {list(PROVIDER_NAMES)}")


def _denormalize_google(event: Dict[str, Any]) -> Dict[str, Any]:
    """Build a Google Calendar API v3 event request body."""
    all_day = event.get("all_day", False)
    time_zone = event.get("time_zone", "UTC")

    if all_day:
        # Google expects date-only strings for all-day events.
        start_val = event["start"][:10]
        end_val = event["end"][:10]
        start = {"date": start_val}
        end = {"date": end_val}
    else:
        start = {"dateTime": event["start"], "timeZone": time_zone}
        end = {"dateTime": event["end"], "timeZone": time_zone}

    payload: Dict[str, Any] = {
        "summary": event.get("title", ""),
        "description": event.get("description"),
        "location": event.get("location"),
        "start": start,
        "end": end,
        "status": event.get("status", "confirmed"),
    }

    if event.get("recurrence"):
        payload["recurrence"] = event["recurrence"]

    attendees = []
    for a in event.get("attendees", []):
        g_status_map = {
            "accepted": "accepted",
            "declined": "declined",
            "tentative": "tentative",
            "needs_action": "needsAction",
        }
        attendees.append({
            "email": a["email"],
            "displayName": a.get("name"),
            "responseStatus": g_status_map.get(a.get("status", "needs_action"), "needsAction"),
        })
    if attendees:
        payload["attendees"] = attendees

    return payload


def _denormalize_microsoft(event: Dict[str, Any]) -> Dict[str, Any]:
    """Build a Microsoft Graph API event request body."""
    all_day = event.get("all_day", False)
    time_zone = event.get("time_zone", "UTC")

    payload: Dict[str, Any] = {
        "subject": event.get("title", ""),
        "body": {
            "contentType": "text",
            "content": event.get("description") or "",
        },
        "start": {
            "dateTime": event["start"],
            "timeZone": time_zone,
        },
        "end": {
            "dateTime": event["end"],
            "timeZone": time_zone,
        },
        "isAllDay": all_day,
    }

    if event.get("location"):
        payload["location"] = {"displayName": event["location"]}

    attendees = []
    for a in event.get("attendees", []):
        ms_status_map = {
            "accepted": "accepted",
            "declined": "declined",
            "tentative": "tentativelyAccepted",
            "needs_action": "none",
        }
        attendees.append({
            "emailAddress": {
                "address": a["email"],
                "name": a.get("name", a["email"]),
            },
            "type": "required",
            "status": {
                "response": ms_status_map.get(a.get("status", "needs_action"), "none"),
            },
        })
    if attendees:
        payload["attendees"] = attendees

    return payload


def _denormalize_apple(event: Dict[str, Any]) -> Dict[str, Any]:
    """Build a dict of iCalendar VEVENT properties for CalDAV PUT.

    The AppleCalendarProvider is responsible for serialising this dict to
    valid iCal text (VCALENDAR/VEVENT format).
    """
    all_day = event.get("all_day", False)
    rrule = None
    for r in event.get("recurrence") or []:
        if r.startswith("RRULE:"):
            rrule = r[len("RRULE:"):]
            break

    attendees = []
    for a in event.get("attendees", []):
        ical_status_map = {
            "accepted": "ACCEPTED",
            "declined": "DECLINED",
            "tentative": "TENTATIVE",
            "needs_action": "NEEDS-ACTION",
        }
        attendees.append({
            "value": f"mailto:{a['email']}",
            "cn": a.get("name"),
            "partstat": ical_status_map.get(a.get("status", "needs_action"), "NEEDS-ACTION"),
        })

    return {
        "summary": event.get("title", ""),
        "description": event.get("description"),
        "location": event.get("location"),
        "dtstart": event["start"],
        "dtend": event["end"],
        "all_day": all_day,
        "time_zone": event.get("time_zone", "UTC"),
        "status": (event.get("status", "confirmed") or "confirmed").upper(),
        "rrule": rrule,
        "attendees": attendees,
        "organizer": (
            f"mailto:{event['organizer']['email']}"
            if event.get("organizer", {}).get("email")
            else None
        ),
        "organizer_cn": (event.get("organizer") or {}).get("name"),
    }
