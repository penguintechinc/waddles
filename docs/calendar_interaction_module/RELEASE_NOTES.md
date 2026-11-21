# Calendar Interaction Module — Release Notes

Release notes are prepended in reverse chronological order. Most recent version appears first.

---

## v2.0.0 — Full Booking and OAuth Feature Release

*Released: 2026-02-16*

### Summary

Version 2.0.0 is a major feature release delivering the complete calendar integration stack: Google and Microsoft Calendar OAuth flows, free/busy synchronization, individual and group booking pages with slot computation, and full event management with ticketing, RSVP, and per-event admin roles.

### New Features

**Calendar OAuth Integration**
- Google Calendar OAuth 2.0 flow with `access_type=offline` and `prompt=consent` to guarantee refresh tokens
- Microsoft Calendar OAuth 2.0 flow via Azure AD common endpoint supporting organizational and personal accounts
- Token exchange and storage in `platform_integrations` table
- Automatic token refresh when within 5 minutes of expiry
- Free/busy sync from Google Calendar API (`/freeBusy`) and Microsoft Graph (`/getSchedule`)
- Connected calendar management: list, sync, disconnect
- Redis pub/sub listener for live credential rotation (`credentials:calendar_interaction:bot:refreshed`)

**Individual Booking Pages**
- Slug-based booking pages with URL pattern `^[a-z0-9-]+$`
- Configurable slot duration (5–480 minutes), access scope (public/registered/community), and up to 8 custom form fields
- `FOR UPDATE` row locking in booking creation to prevent double-booking race conditions
- Booking lifecycle: create, confirm, cancel (by host or guest), complete, no-show status tracking
- Host booking list with status and date range filters
- Guest booking retrieval by UUID

**Slot Computation Engine**
- Weekly availability windows per user (stored as JSON in `user_calendar_settings`)
- Constraints: `min_notice_hours`, `max_future_days`, `buffer_minutes`
- Busy block merging from `calendar_free_busy` (external calendar sync) and `bookings` tables
- Slot-overlap elimination with O(n*m) check across busy blocks and candidates

**Group Booking Pages**
- Community-scoped group pages with required/optional member designation
- Privacy-preserving availability aggregation — returns `available_count`, `unavailable_count`, and `meets_requirements` without exposing individual member calendars
- Best-slots ranking across date ranges (sorted by `available_count` descending)
- Member add/remove with upsert semantics (`ON CONFLICT DO UPDATE`)

**Event Management Enhancements**
- Pydantic v2 validation models for all endpoints, eliminating 500 errors from unsafe type conversions
- `EventCreateRequest.model_config = ConfigDict(extra='forbid')` on all models
- Multi-field model validators for date ordering (end_date after event_date, rsvp_deadline before event_date)
- Full-text search across event title and description
- Approval workflow with optional moderator notes
- Recurring events with RRULE-compatible patterns (daily, weekly, monthly) and `recurring_days` list (0=Sunday to 6=Saturday)
- Category management endpoints
- Platform sync via Discord and Twitch webhooks
- Trending events endpoint

**Ticketing System**
- Multiple ticket types per event with configurable capacity and price
- Ticket states: `valid`, `checked_in`, `cancelled`, `expired`, `refunded`, `transferred`
- Check-in methods: `qr_scan`, `manual`, `api`, `self_checkin`, `auto_checkin`
- Check-in result codes: `success`, `already_checked_in`, `invalid_ticket`, `wrong_event`, `expired`, `cancelled`, `event_not_started`, `event_ended`, `unauthorized`
- Undo check-in support
- Ticket transfer between users
- Check-in audit log with pagination
- Standalone ticket verification without check-in

**Per-Event Admin Roles**
- Eight granular permission flags per event admin: `can_edit_event`, `can_check_in`, `can_view_tickets`, `can_manage_ticket_types`, `can_cancel_tickets`, `can_transfer_tickets`, `can_export_attendance`, `can_assign_event_admins`
- Community admin and event creator always retain full access regardless of role configuration
- Delegation: admins with `can_assign_event_admins` can assign other event admins
- Full CRUD for event admin records with audit logging

**RSVP Enhancements**
- Waitlist with position tracking when `max_attendees` is reached and `waitlist_enabled=True`
- Guest count support in RSVP requests
- Automatic ticket generation on RSVP `yes` for ticketed events (RSVPService + TicketService integration)

### Breaking Changes

- All request bodies now use strict Pydantic validation with `extra='forbid'`. Requests containing unknown fields that previously passed through silently will now return `400`.
- `event_date` and `end_date` validation is now enforced at the model level — previously invalid date orderings that reached the service layer will now be rejected at the endpoint.
- `BookingCreateRequest.slot_end` must be strictly after `slot_start` (previously unchecked).
- Booking page slugs must match `^[a-z0-9-]+$` — uppercase, spaces, and underscores are rejected.

### Database Migrations Required

Apply migrations in order before deploying v2.0.0:

```bash
psql -U waddlebot -d waddlebot -f config/postgres/migrations/036_calendar_appointments.sql
psql -U waddlebot -d waddlebot -f config/postgres/migrations/037_fix_community_schema.sql
```

Migration `036_calendar_appointments.sql` creates:
- `user_calendar_settings` — per-user availability and booking configuration
- `connected_calendars` — OAuth calendar connections
- `calendar_free_busy` — busy blocks from external calendar sync
- `booking_pages` — individual and group booking page definitions
- `booking_page_members` — group page membership
- `bookings` — appointment records

Migration `037_fix_community_schema.sql` applies corrections to community-related schema required for availability queries.

### Configuration Changes

New environment variables required for Google and Microsoft OAuth:
- `GOOGLE_CALENDAR_CLIENT_ID`
- `GOOGLE_CALENDAR_CLIENT_SECRET`
- `MICROSOFT_CALENDAR_CLIENT_ID`
- `MICROSOFT_CALENDAR_CLIENT_SECRET`

Optional new variable:
- `REDIS_URL` — enables live credential refresh via pub/sub

### Service Architecture

The v2.0.0 service layer includes 11 services total:

| Service | Module File |
|---|---|
| CalendarOAuthService | services/calendar_oauth_service.py |
| AvailabilityService | services/availability_service.py |
| BookingService | services/booking_service.py |
| GroupAvailabilityService | services/group_availability_service.py |
| RSVPService | services/rsvp_service.py |
| TicketService | services/ticket_service.py |
| EventAdminService | services/event_admin_service.py |
| CalendarService | services/calendar_service.py |
| ContextService | services/context_service.py |
| PermissionService | services/permission_service.py |
| CacheManager | services/cache_manager.py |

---

## v0.1.0 — Initial Documentation Release

*Released: 2026-02-16*

- Initial module documentation package created
- OVERVIEW.md, USAGE.md, API.md, ARCHITECTURE.md, CONFIGURATION.md, TESTING.md, TROUBLESHOOTING.md, RELEASE_NOTES.md all established from source code review
- Documentation reflects v2.0.0 codebase state as found in action/interactive/calendar_interaction_module

---

## Pre-Release History

### Development Milestones Prior to v2.0.0

The following milestones were completed during internal development prior to the v2.0.0 release. These are documented here for historical context.

**Phase 4A — Calendar OAuth and Free/Busy Sync**
- Implemented `CalendarOAuthService` with Google and Microsoft OAuth 2.0 flows
- Token storage in `platform_integrations` table with `platform='google_calendar'` and `platform='microsoft_calendar'`
- `connected_calendars` table for per-user calendar records
- `calendar_free_busy` table for synchronized busy block storage
- Automatic token refresh with 5-minute expiry threshold
- Google FreeBusy API integration (`/v3/freeBusy`)
- Microsoft Graph Schedule API integration (`/v1.0/me/calendar/getSchedule`)
- Calendar disconnect with `is_active=FALSE` soft deactivation

**Phase 4B — Availability Settings and Slot Computation**
- `user_calendar_settings` table for per-user availability configuration
- Weekly availability windows as JSON (day name keys, `{start, end}` time objects)
- `AvailabilityService` with full `compute_available_slots` algorithm
- Settings upsert with dynamic SQL generation for partial updates
- Free/busy merging from both `calendar_free_busy` and `bookings` tables
- Slot filtering with overlap detection

**Phase 4C — Individual Booking Pages**
- `booking_pages` table with `page_type='individual'`
- `BookingService` with full CRUD for booking pages
- Slug-based page lookup with Pydantic pattern validation (`^[a-z0-9-]+$`)
- Booking creation with `SELECT FOR UPDATE` locking for race condition safety
- `bookings` table with UUID-based public reference
- Booking cancellation with role-aware status (`cancelled_by_host` vs `cancelled_by_guest`)
- Host booking list with status and date range filters

**Phase 4D — Group Booking Pages**
- `booking_page_members` table with `is_required` flag
- `GroupAvailabilityService` with privacy-preserving aggregate availability
- Member add/remove with upsert semantics
- `get_most_available_slots` for date-range best-slot ranking
- Group availability returns only `available_count`, `unavailable_count`, `meets_requirements` — no individual calendar data exposed

**Validation Hardening (all phases)**
- All request bodies converted from inline `request.get_json()` to Pydantic v2 models
- `ConfigDict(extra='forbid')` on all models to reject unknown fields
- Multi-field model validators for date ordering rules
- `field_validator` for slug patterns, email format, URL format, recurring_days ranges
- Eliminated all `int()` unsafe conversions on query parameters that previously caused 500 errors

---

## Planned Future Work

The following items are identified for future releases and are not yet implemented:

- **Calendar write-back** — Create WaddleBot events on the user's external calendar (requires `https://www.googleapis.com/auth/calendar.events` scope)
- **iCal/ICS export** — Export events in `.ics` format for calendar client import
- **Email notifications** — Send booking confirmations and reminders via SMTP
- **Payment integration** — Stripe/PayPal integration for paid ticket types
- **Booking page analytics** — View counts, conversion rates, slot popularity heatmaps
- **Recurring bookings** — Allow guests to book recurring slots (weekly, biweekly)
- **Waitlist notifications** — Automatic notification when a waitlisted user is promoted off the waitlist
- **Best-slots caching** — Cache daily availability results to accelerate `get_most_available_slots` for large groups and wide date ranges
- **Multi-timezone display** — Show event and slot times in the viewer's local timezone rather than UTC
