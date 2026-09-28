"""Calendar Sync Providers — concrete implementations for supported calendar platforms.

Available providers:
- GoogleCalendarProvider  — Google Calendar API v3
- MicrosoftCalendarProvider — Microsoft Graph API (Outlook / Office 365)
- AppleCalendarProvider  — Apple CalDAV / iCloud Calendar
"""
from libs.calendar_sync.providers.google import GoogleCalendarProvider
from libs.calendar_sync.providers.microsoft import MicrosoftCalendarProvider
from libs.calendar_sync.providers.apple import AppleCalendarProvider

__all__ = [
    "GoogleCalendarProvider",
    "MicrosoftCalendarProvider",
    "AppleCalendarProvider",
]
