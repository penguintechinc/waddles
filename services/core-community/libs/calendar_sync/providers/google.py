"""GoogleCalendarProvider — Google Calendar API v3 integration.

Authentication
--------------
Credentials dict must contain:
    access_token  (str)  — OAuth 2.0 bearer token
    refresh_token (str)  — OAuth 2.0 refresh token
    client_id     (str)  — Google OAuth client ID
    client_secret (str)  — Google OAuth client secret
    token_expiry  (str)  — ISO 8601 expiry timestamp of the access token

Scopes required:
    https://www.googleapis.com/auth/calendar

Sync strategy: syncToken-based incremental sync (Google Calendar push/pull).
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

from libs.calendar_sync.base import CalendarProviderBase
from libs.calendar_sync.schema import normalize_event

logger = logging.getLogger(__name__)

_GOOGLE_API_BASE = "https://www.googleapis.com/calendar/v3"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Minimum seconds before expiry at which the access token is proactively refreshed.
_TOKEN_REFRESH_BUFFER_SECONDS = 300


class GoogleCalendarProvider(CalendarProviderBase):
    """Google Calendar API v3 provider."""

    PROVIDER = "google"

    def __init__(self, credentials: Dict[str, Any]):
        super().__init__(credentials)
        self._access_token: str = credentials.get("access_token", "")
        self._refresh_token: str = credentials.get("refresh_token", "")
        self._client_id: str = credentials.get("client_id", "")
        self._client_secret: str = credentials.get("client_secret", "")
        self._token_expiry: Optional[str] = credentials.get("token_expiry")

    # ──────────────────────────────────────────────────────────────────────
    # Authentication
    # ──────────────────────────────────────────────────────────────────────

    async def authenticate(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        """Refresh the access token if it is expiring within the buffer window.

        Args:
            credentials: Current credential dict.

        Returns:
            Updated credentials dict with a fresh access_token if refreshed.

        Raises:
            ValueError: If the token refresh request fails.
        """
        self._access_token = credentials.get("access_token", self._access_token)
        self._refresh_token = credentials.get("refresh_token", self._refresh_token)
        self._client_id = credentials.get("client_id", self._client_id)
        self._client_secret = credentials.get("client_secret", self._client_secret)
        self._token_expiry = credentials.get("token_expiry", self._token_expiry)

        if self._should_refresh():
            credentials = await self._refresh_access_token(credentials)
            self._access_token = credentials["access_token"]
            self._token_expiry = credentials.get("token_expiry")

        return credentials

    def _should_refresh(self) -> bool:
        """Return True if the access token needs refreshing."""
        if not self._token_expiry:
            return False
        try:
            expiry = datetime.fromisoformat(self._token_expiry)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            buffer = timedelta(seconds=_TOKEN_REFRESH_BUFFER_SECONDS)
            return datetime.now(timezone.utc) >= (expiry - buffer)
        except ValueError:
            return False

    async def _refresh_access_token(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        """Perform an OAuth 2.0 token refresh against Google's token endpoint."""
        if not self._refresh_token:
            raise ValueError("[GOOGLE] Cannot refresh: no refresh_token in credentials.")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            self._log_api_call("POST", _GOOGLE_TOKEN_URL, resp.status_code)

            if resp.status_code != 200:
                raise ValueError(
                    f"[GOOGLE] Token refresh failed: HTTP {resp.status_code} — {resp.text}"
                )

            data = resp.json()
            expires_in = data.get("expires_in", 3600)
            expiry = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            ).isoformat()

            return {
                **credentials,
                "access_token": data["access_token"],
                "token_expiry": expiry,
            }

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    # ──────────────────────────────────────────────────────────────────────
    # Calendar-level operations
    # ──────────────────────────────────────────────────────────────────────

    async def list_calendars(self) -> List[Dict[str, Any]]:
        """List all calendars in the authenticated user's Google account."""
        url = f"{_GOOGLE_API_BASE}/users/me/calendarList"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers=self._auth_headers())
                self._log_api_call("GET", url, resp.status_code)
                resp.raise_for_status()
                data = resp.json()

            calendars = []
            for item in data.get("items", []):
                calendars.append({
                    "id": item.get("id", ""),
                    "name": item.get("summary", ""),
                    "description": item.get("description"),
                    "primary": item.get("primary", False),
                    "read_only": item.get("accessRole", "") in ("reader", "freeBusyReader"),
                    "time_zone": item.get("timeZone", "UTC"),
                })
            return calendars

        except Exception as exc:
            self._log_error("list_calendars", exc)
            return []

    async def create_calendar(
        self,
        name: str,
        description: Optional[str] = None,
        time_zone: str = "UTC",
    ) -> Dict[str, Any]:
        """Create a new secondary calendar in the authenticated user's account."""
        url = f"{_GOOGLE_API_BASE}/calendars"
        payload: Dict[str, Any] = {"summary": name, "timeZone": time_zone}
        if description:
            payload["description"] = description

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    url, json=payload, headers=self._auth_headers()
                )
                self._log_api_call("POST", url, resp.status_code)
                resp.raise_for_status()
                item = resp.json()

            return {
                "id": item.get("id", ""),
                "name": item.get("summary", ""),
                "description": item.get("description"),
                "primary": False,
                "read_only": False,
                "time_zone": item.get("timeZone", time_zone),
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
        """Fetch events from a Google Calendar, supporting pagination and incremental sync."""
        url = f"{_GOOGLE_API_BASE}/calendars/{calendar_id}/events"
        params: Dict[str, Any] = {
            "maxResults": max_results,
            "singleEvents": "true",
            "orderBy": "startTime",
        }

        if sync_token:
            # Incremental sync — ignore time bounds when token is present.
            params["syncToken"] = sync_token
        else:
            if time_min:
                params["timeMin"] = time_min
            if time_max:
                params["timeMax"] = time_max

        if page_token:
            params["pageToken"] = page_token

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    url, params=params, headers=self._auth_headers()
                )
                self._log_api_call("GET", url, resp.status_code)
                resp.raise_for_status()
                data = resp.json()

            # Attach calendarId to each item so schema normalisation has it.
            raw_events = data.get("items", [])
            for item in raw_events:
                item["calendarId"] = calendar_id

            return {
                "events": raw_events,
                "next_page_token": data.get("nextPageToken"),
                "next_sync_token": data.get("nextSyncToken"),
            }

        except Exception as exc:
            self._log_error("get_events", exc)
            raise

    async def create_event(
        self,
        calendar_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create an event in the specified Google Calendar."""
        url = f"{_GOOGLE_API_BASE}/calendars/{calendar_id}/events"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    url, json=event, headers=self._auth_headers()
                )
                self._log_api_call("POST", url, resp.status_code)
                resp.raise_for_status()
                item = resp.json()
                item["calendarId"] = calendar_id
                return normalize_event(item, self.PROVIDER)

        except Exception as exc:
            self._log_error("create_event", exc)
            raise

    async def update_event(
        self,
        calendar_id: str,
        event_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Update an existing Google Calendar event using PUT (full replacement)."""
        url = f"{_GOOGLE_API_BASE}/calendars/{calendar_id}/events/{event_id}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.put(
                    url, json=event, headers=self._auth_headers()
                )
                self._log_api_call("PUT", url, resp.status_code)
                resp.raise_for_status()
                item = resp.json()
                item["calendarId"] = calendar_id
                return normalize_event(item, self.PROVIDER)

        except Exception as exc:
            self._log_error("update_event", exc)
            raise

    async def delete_event(
        self,
        calendar_id: str,
        event_id: str,
    ) -> bool:
        """Delete (cancel) an event from the specified Google Calendar."""
        url = f"{_GOOGLE_API_BASE}/calendars/{calendar_id}/events/{event_id}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.delete(url, headers=self._auth_headers())
                self._log_api_call("DELETE", url, resp.status_code)
                # 204 No Content = success; 404 = already gone (treat as success).
                return resp.status_code in (204, 404)

        except Exception as exc:
            self._log_error("delete_event", exc)
            return False

    # ──────────────────────────────────────────────────────────────────────
    # Incremental sync
    # ──────────────────────────────────────────────────────────────────────

    async def sync_changes(
        self,
        calendar_id: str,
        sync_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Collect all changed events since the given sync token.

        Handles pagination internally.  On 410 Gone (stale token), raises
        ValueError so the engine can fall back to full_sync.

        Args:
            calendar_id: Google Calendar identifier.
            sync_token: Token from a previous sync_changes or get_events call.

        Returns:
            Tuple of (changed_events, new_sync_token).
        """
        changed_events: List[Dict[str, Any]] = []
        new_sync_token: Optional[str] = None
        page_token: Optional[str] = None

        while True:
            try:
                result = await self.get_events(
                    calendar_id=calendar_id,
                    sync_token=sync_token,
                    page_token=page_token,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 410:
                    raise ValueError(
                        f"[GOOGLE] Stale syncToken for calendar {calendar_id}; "
                        "full sync required."
                    ) from exc
                raise

            changed_events.extend(result.get("events", []))
            page_token = result.get("next_page_token")
            new_sync_token = result.get("next_sync_token") or new_sync_token

            if not page_token:
                break

        return changed_events, new_sync_token
