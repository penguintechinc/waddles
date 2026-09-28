"""Calendar Sync Library — provider-pattern calendar synchronization for WaddleBot.

This library provides a unified interface for synchronizing events across Google
Calendar, Microsoft Outlook/Graph, and Apple CalDAV. A canonical event schema
normalizes differences between providers so the rest of WaddleBot never needs to
know which external calendar it is talking to.

Usage
-----
from libs.calendar_sync import (
    CalendarProviderBase,
    GoogleCalendarProvider,
    MicrosoftCalendarProvider,
    AppleCalendarProvider,
    CalendarSyncEngine,
)

engine = CalendarSyncEngine(provider=GoogleCalendarProvider(credentials={...}), dal=dal)
await engine.full_sync(user_id="u1", calendar_id="primary")
"""
from libs.calendar_sync.base import CalendarProviderBase
from libs.calendar_sync.providers.google import GoogleCalendarProvider
from libs.calendar_sync.providers.microsoft import MicrosoftCalendarProvider
from libs.calendar_sync.providers.apple import AppleCalendarProvider
from libs.calendar_sync.sync_engine import CalendarSyncEngine

__all__ = [
    "CalendarProviderBase",
    "GoogleCalendarProvider",
    "MicrosoftCalendarProvider",
    "AppleCalendarProvider",
    "CalendarSyncEngine",
]
