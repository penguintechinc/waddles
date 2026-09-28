"""MicrosoftCalendarProvider — Microsoft Graph API calendar integration.

Authentication
--------------
Credentials dict must contain:
    access_token  (str)  — OAuth 2.0 bearer token (Microsoft identity platform)
    refresh_token (str)  — OAuth 2.0 refresh token
    client_id     (str)  — Azure AD application (client) ID
    client_secret (str)  — Azure AD client secret
    tenant_id     (str)  — Azure AD tenant ID (or "common" for multi-tenant)
    token_expiry  (str)  — ISO 8601 expiry timestamp of the access token

Scopes required:
    Calendars.ReadWrite  (or Calendars.Read for read-only access)
    offline_access       (for refresh token)

Sync strategy: Microsoft Graph delta sync using @odata.deltaLink tokens.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

from libs.calendar_sync.base import CalendarProviderBase
from libs.calendar_sync.schema import normalize_event

logger = logging.getLogger(__name__)

_GRAPH_API_BASE = "https://graph.microsoft.com/v1.0/me"
_MS_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

_TOKEN_REFRESH_BUFFER_SECONDS = 300


class MicrosoftCalendarProvider(CalendarProviderBase):
    """Microsoft Graph API (Outlook / Office 365) calendar provider."""

    PROVIDER = "microsoft"

    def __init__(self, credentials: Dict[str, Any]):
        super().__init__(credentials)
        self._access_token: str = credentials.get("access_token", "")
        self._refresh_token: str = credentials.get("refresh_token", "")
        self._client_id: str = credentials.get("client_id", "")
        self._client_secret: str = credentials.get("client_secret", "")
        self._tenant_id: str = credentials.get("tenant_id", "common")
        self._token_expiry: Optional[str] = credentials.get("token_expiry")

    # ──────────────────────────────────────────────────────────────────────
    # Authentication
    # ──────────────────────────────────────────────────────────────────────

    async def authenticate(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        """Refresh the Microsoft access token if it is close to expiry."""
        self._access_token = credentials.get("access_token", self._access_token)
        self._refresh_token = credentials.get("refresh_token", self._refresh_token)
        self._client_id = credentials.get("client_id", self._client_id)
        self._client_secret = credentials.get("client_secret", self._client_secret)
        self._tenant_id = credentials.get("tenant_id", self._tenant_id)
        self._token_expiry = credentials.get("token_expiry", self._token_expiry)

        if self._should_refresh():
            credentials = await self._refresh_access_token(credentials)
            self._access_token = credentials["access_token"]
            self._token_expiry = credentials.get("token_expiry")

        return credentials

    def _should_refresh(self) -> bool:
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
        """Perform an OAuth 2.0 token refresh against Microsoft identity platform."""
        if not self._refresh_token:
            raise ValueError("[MICROSOFT] Cannot refresh: no refresh_token in credentials.")

        token_url = _MS_TOKEN_URL_TEMPLATE.format(tenant_id=self._tenant_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": "Calendars.ReadWrite offline_access",
                },
            )
            self._log_api_call("POST", token_url, resp.status_code)

            if resp.status_code != 200:
                raise ValueError(
                    f"[MICROSOFT] Token refresh failed: HTTP {resp.status_code} — {resp.text}"
                )

            data = resp.json()
            expires_in = data.get("expires_in", 3600)
            expiry = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            ).isoformat()

            updated = {
                **credentials,
                "access_token": data["access_token"],
                "token_expiry": expiry,
            }
            if "refresh_token" in data:
                updated["refresh_token"] = data["refresh_token"]
                self._refresh_token = data["refresh_token"]

            return updated

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    # ──────────────────────────────────────────────────────────────────────
    # Calendar-level operations
    # ──────────────────────────────────────────────────────────────────────

    async def list_calendars(self) -> List[Dict[str, Any]]:
        """List all calendars in the authenticated user's Outlook account."""
        url = f"{_GRAPH_API_BASE}/calendars"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers=self._auth_headers())
                self._log_api_call("GET", url, resp.status_code)
                resp.raise_for_status()
                data = resp.json()

            calendars = []
            for item in data.get("value", []):
                calendars.append({
                    "id": item.get("id", ""),
                    "name": item.get("name", ""),
                    "description": None,  # Graph API does not expose description.
                    "primary": item.get("isDefaultCalendar", False),
                    "read_only": not item.get("canEdit", True),
                    "time_zone": item.get("hexColor", "UTC"),  # time zone not in list API
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
        """Create a new calendar in the authenticated user's Outlook account."""
        url = f"{_GRAPH_API_BASE}/calendars"
        payload: Dict[str, Any] = {"name": name}
        # Microsoft Graph does not support a description field on calendars.

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
                "name": item.get("name", ""),
                "description": None,
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
        """Fetch events from a Microsoft calendar.

        When sync_token is provided it is treated as a delta link URL (the
        full URL returned by a previous delta query).  When absent, a
        calendarView or events endpoint is used.
        """
        # Microsoft delta links are full URLs, not just tokens.
        if sync_token and sync_token.startswith("https://"):
            url = sync_token
            params: Dict[str, Any] = {"$top": max_results}
        elif page_token and page_token.startswith("https://"):
            url = page_token
            params = {"$top": max_results}
        else:
            if time_min or time_max:
                url = f"{_GRAPH_API_BASE}/calendars/{calendar_id}/calendarView"
                params = {"$top": max_results}
                if time_min:
                    params["startDateTime"] = time_min
                if time_max:
                    params["endDateTime"] = time_max
            else:
                url = f"{_GRAPH_API_BASE}/calendars/{calendar_id}/events"
                params = {"$top": max_results}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    url, params=params, headers=self._auth_headers()
                )
                self._log_api_call("GET", url, resp.status_code)
                resp.raise_for_status()
                data = resp.json()

            raw_events = data.get("value", [])
            for item in raw_events:
                item["calendarId"] = calendar_id

            return {
                "events": raw_events,
                "next_page_token": data.get("@odata.nextLink"),
                "next_sync_token": data.get("@odata.deltaLink"),
            }

        except Exception as exc:
            self._log_error("get_events", exc)
            raise

    async def create_event(
        self,
        calendar_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create an event in the specified Outlook calendar."""
        url = f"{_GRAPH_API_BASE}/calendars/{calendar_id}/events"
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
        """Update an existing Outlook calendar event using PATCH."""
        url = f"{_GRAPH_API_BASE}/calendars/{calendar_id}/events/{event_id}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.patch(
                    url, json=event, headers=self._auth_headers()
                )
                self._log_api_call("PATCH", url, resp.status_code)
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
        """Delete an event from the specified Outlook calendar."""
        url = f"{_GRAPH_API_BASE}/calendars/{calendar_id}/events/{event_id}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.delete(url, headers=self._auth_headers())
                self._log_api_call("DELETE", url, resp.status_code)
                return resp.status_code in (204, 404)

        except Exception as exc:
            self._log_error("delete_event", exc)
            return False

    # ──────────────────────────────────────────────────────────────────────
    # Incremental sync (delta)
    # ──────────────────────────────────────────────────────────────────────

    async def sync_changes(
        self,
        calendar_id: str,
        sync_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Collect all changed events since the given delta link token.

        Microsoft Graph delta links are full URLs; when sync_token is None
        the delta endpoint is queried without a token to bootstrap delta tracking.

        Args:
            calendar_id: Microsoft calendar identifier.
            sync_token: Delta link URL from a previous sync_changes call.

        Returns:
            Tuple of (changed_events, new_delta_link).
        """
        if sync_token:
            url = sync_token
        else:
            url = f"{_GRAPH_API_BASE}/calendars/{calendar_id}/events/delta"

        changed_events: List[Dict[str, Any]] = []
        new_delta_link: Optional[str] = None

        while url:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(
                        url,
                        params={"$top": 250},
                        headers=self._auth_headers(),
                    )
                    self._log_api_call("GET", url, resp.status_code)

                    if resp.status_code == 410:
                        raise ValueError(
                            f"[MICROSOFT] Stale deltaLink for calendar {calendar_id}; "
                            "full sync required."
                        )

                    resp.raise_for_status()
                    data = resp.json()

            except httpx.HTTPStatusError as exc:
                raise ValueError(
                    f"[MICROSOFT] Delta sync HTTP error: {exc.response.status_code}"
                ) from exc

            raw_events = data.get("value", [])
            for item in raw_events:
                item["calendarId"] = calendar_id
            changed_events.extend(raw_events)

            next_link = data.get("@odata.nextLink")
            delta_link = data.get("@odata.deltaLink")
            new_delta_link = delta_link or new_delta_link

            url = next_link or ""

        return changed_events, new_delta_link
