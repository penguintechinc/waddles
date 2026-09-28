"""CalendarProviderBase — abstract contract for all calendar provider implementations.

Every provider (Google, Microsoft, Apple) implements this interface so the sync
engine and higher-level services can operate against any provider without
branching on provider type.

Implementing a new provider
---------------------------
class MyCalendarProvider(CalendarProviderBase):
    PROVIDER = "mycalendar"

    async def authenticate(self, credentials): ...
    async def list_calendars(self): ...
    async def create_calendar(self, name, description=None): ...
    async def get_events(self, calendar_id, time_min=None, time_max=None,
                         sync_token=None, page_token=None): ...
    async def create_event(self, calendar_id, event): ...
    async def update_event(self, calendar_id, event_id, event): ...
    async def delete_event(self, calendar_id, event_id): ...
    async def sync_changes(self, calendar_id, sync_token=None): ...
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


class CalendarProviderBase(ABC):
    """Abstract base class for all calendar provider integrations.

    Sub-classes must set PROVIDER (e.g. "google") and implement every
    abstract method.  All methods are async; providers must use an async
    HTTP client (httpx.AsyncClient is the project standard).
    """

    # Short identifier used in logs, database records, and error messages.
    PROVIDER: str = "unknown"

    def __init__(self, credentials: Dict[str, Any]):
        """Initialise the provider with OAuth/token credentials.

        Args:
            credentials: Provider-specific credential dict.  The shape varies
                by provider but always contains an access_token at minimum.
                Refresh tokens and expiry should also be included so the
                provider can self-renew without requiring the caller to
                re-authenticate.
        """
        self._credentials = credentials
        self.logger = logging.getLogger(f"calendar_sync.{self.PROVIDER}")

    # ──────────────────────────────────────────────────────────────────────
    # Authentication
    # ──────────────────────────────────────────────────────────────────────

    @abstractmethod
    async def authenticate(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and optionally refresh credentials.

        Called at the start of any sync operation to ensure the access token
        is current.  Should perform a token refresh if the token is expiring
        within the next 5 minutes.

        Args:
            credentials: Current credential dict (may contain a refresh_token).

        Returns:
            Updated credential dict with a fresh access_token and new expiry.

        Raises:
            ValueError: If credentials are invalid or refresh fails.
        """

    # ──────────────────────────────────────────────────────────────────────
    # Calendar-level operations
    # ──────────────────────────────────────────────────────────────────────

    @abstractmethod
    async def list_calendars(self) -> List[Dict[str, Any]]:
        """Return all calendars accessible to the authenticated user.

        Returns:
            List of calendar dicts, each containing at minimum:
            - id (str): Provider-specific calendar identifier.
            - name (str): Display name.
            - description (str | None)
            - primary (bool): True for the user's primary calendar.
            - read_only (bool): True if the calendar cannot be written to.
            - time_zone (str): IANA time zone string.
        """

    @abstractmethod
    async def create_calendar(
        self,
        name: str,
        description: Optional[str] = None,
        time_zone: str = "UTC",
    ) -> Dict[str, Any]:
        """Create a new calendar owned by the authenticated user.

        Args:
            name: Display name for the new calendar.
            description: Optional description.
            time_zone: IANA time zone string (default "UTC").

        Returns:
            Calendar dict (same shape as list_calendars entries).
        """

    # ──────────────────────────────────────────────────────────────────────
    # Event-level operations
    # ──────────────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_events(
        self,
        calendar_id: str,
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
        sync_token: Optional[str] = None,
        page_token: Optional[str] = None,
        max_results: int = 250,
    ) -> Dict[str, Any]:
        """Fetch events from a calendar, supporting pagination and incremental sync.

        When sync_token is provided the provider should use incremental sync
        semantics (only events changed since the token was issued).  When
        sync_token is absent a full time-range query is performed.

        Args:
            calendar_id: Provider calendar identifier.
            time_min: RFC 3339 lower bound (inclusive) for event start time.
            time_max: RFC 3339 upper bound (exclusive) for event start time.
            sync_token: Incremental sync token from a previous call.
            page_token: Pagination token from a previous call.
            max_results: Maximum number of events per page (default 250).

        Returns:
            Dict containing:
            - events (List[Dict]): Normalised event dicts.
            - next_page_token (str | None): Token for the next page.
            - next_sync_token (str | None): Token to use for next incremental sync.
        """

    @abstractmethod
    async def create_event(
        self,
        calendar_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create a new event in the specified calendar.

        Args:
            calendar_id: Provider calendar identifier.
            event: Canonical event dict (see schema.py).

        Returns:
            The created event dict including the provider-assigned event_id.
        """

    @abstractmethod
    async def update_event(
        self,
        calendar_id: str,
        event_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Update an existing event.

        Args:
            calendar_id: Provider calendar identifier.
            event_id: Provider-specific event identifier.
            event: Canonical event dict with updated fields.

        Returns:
            The updated event dict.
        """

    @abstractmethod
    async def delete_event(
        self,
        calendar_id: str,
        event_id: str,
    ) -> bool:
        """Delete (or cancel) an event.

        Args:
            calendar_id: Provider calendar identifier.
            event_id: Provider-specific event identifier.

        Returns:
            True if the deletion succeeded or the event was already absent.
        """

    # ──────────────────────────────────────────────────────────────────────
    # Incremental sync
    # ──────────────────────────────────────────────────────────────────────

    @abstractmethod
    async def sync_changes(
        self,
        calendar_id: str,
        sync_token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Fetch all changes since the given sync token and return the new token.

        This is the primary incremental-sync method.  It handles pagination
        internally and collects every changed event before returning.

        Args:
            calendar_id: Provider calendar identifier.
            sync_token: Token from a previous sync_changes call.  Pass None
                to perform an initial full sync and receive a fresh token.

        Returns:
            A 2-tuple of:
            - changed_events (List[Dict]): All events that changed since the
              last sync, including deletions (marked with status="cancelled").
            - new_sync_token (str | None): Token representing current state;
              pass this back in the next incremental sync call.
        """

    # ──────────────────────────────────────────────────────────────────────
    # Helpers available to all sub-classes
    # ──────────────────────────────────────────────────────────────────────

    def _log_api_call(self, method: str, url: str, status_code: int) -> None:
        """Emit a structured debug log for outbound API calls."""
        self.logger.debug(
            f"[{self.PROVIDER.upper()}] {method} {url} → {status_code}"
        )

    def _log_error(self, operation: str, error: Exception) -> None:
        """Emit a structured error log."""
        self.logger.error(
            f"[{self.PROVIDER.upper()}] {operation} failed: {error}"
        )
