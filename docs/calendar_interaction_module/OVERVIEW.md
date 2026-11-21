# Calendar Interaction Module

> Full-stack event and appointment management module providing OAuth-connected calendar sync, individual and group booking pages, event lifecycle management, ticketing, and RSVP workflows for WaddleBot communities.

---

## Purpose

The Calendar Interaction Module (`calendar_interaction_module`) is the central hub for all scheduling and event-related functionality within WaddleBot. It manages the full lifecycle of community events from creation and approval through RSVP collection, ticketing, and post-event check-in, while also providing appointment scheduling capabilities for individual hosts and multi-member groups.

OAuth integration with Google Calendar and Microsoft Outlook allows users to connect their external calendars, enabling automatic free/busy synchronization. This bidirectional awareness prevents double-booking: when a user already has a meeting on their Google Calendar, those busy blocks are pulled into WaddleBot and the booking system will not offer overlapping slots. Token refresh is handled automatically — the service checks token expiry within a 5-minute threshold and proactively refreshes using the stored refresh token. A Redis pub/sub channel (`credentials:calendar_interaction:bot:refreshed`) enables live credential rotation without module restart.

Booking pages provide a Calendly-style scheduling surface built natively into WaddleBot. Individual hosts define their weekly availability windows, configure minimum notice periods and buffer times between appointments, and publish a unique slug-based booking URL. The slot computation algorithm respects `min_notice_hours` (how far in advance a booking must be placed), `max_future_days` (how far ahead slots are shown), and `buffer_minutes` (gap inserted between consecutive bookings to allow transitions). Group booking pages aggregate availability across multiple members with a privacy-preserving algorithm that returns slot counts without exposing individual calendar details. The best-slots feature ranks time windows across date ranges to surface the moments when the largest portion of required attendees are free simultaneously.

Event management is community-scoped and platform-aware, supporting events originating from Discord, Twitch, or Slack. Community administrators configure per-community permissions governing who can create events, whether approval is required before publication, and which RSVP and ticket features are enabled. The approval workflow allows moderators to review and accept or reject event submissions with optional written notes. Recurring events are stored with RRULE-compatible patterns and can be configured to repeat daily, weekly, or monthly with optional end dates. The ticketing subsystem supports multiple ticket types per event with capacity limits, QR-code-based check-in, ticket transfer, and detailed check-in audit logs. Per-event admin roles allow event creators to delegate fine-grained permissions to other users across eight distinct permission types. RSVP responses include capacity enforcement with automatic waitlist positioning when a capacity limit is set and `waitlist_enabled=True`.

---

## Key Capabilities

- **Google Calendar OAuth 2.0**: Authorization URL generation, authorization code exchange, `offline` access with `prompt=consent` to guarantee refresh tokens, automatic token refresh via Google token endpoint
- **Microsoft Calendar OAuth 2.0**: Authorization URL generation, code exchange via Azure AD `common/oauth2/v2.0` endpoint, schedule sync via Microsoft Graph `getSchedule` API
- **Free/Busy Synchronization**: Pulls busy blocks from connected calendars into `calendar_free_busy`; merged with existing WaddleBot bookings for slot calculation
- **Individual Booking Pages**: Host-owned slug-based pages (`^[a-z0-9-]+$`) with configurable slot duration (5–480 min), access scope, and up to 8 custom form fields
- **Group Booking Pages**: Community-scoped pages with required/optional member designation; aggregate availability with `meets_requirements` enforcement without exposing individual calendars
- **Best-Slots Ranking**: Scans a date range and returns the top N slots sorted by `available_count` descending
- **Slot Computation Engine**: Algorithm enforces weekly availability windows, `min_notice_hours`, `max_future_days`, `buffer_minutes`, and busy-block overlap elimination
- **Race Condition Protection**: Booking creation uses `SELECT FOR UPDATE` row locking to prevent simultaneous double-booking of the same slot
- **Event CRUD**: Full create/read/update/delete for community events with Pydantic v2 validated inputs; all models use `ConfigDict(extra='forbid')`
- **Approval Workflow**: Events in `pending` status are gated behind moderator approve/reject with optional audit notes
- **Recurring Events**: RRULE-based recurrence with `daily`, `weekly`, `monthly` patterns, `recurring_days` (0=Sunday to 6=Saturday), and optional `recurring_end_date`
- **RSVP Management**: Yes/no/maybe responses with `guest_count`, capacity enforcement, and automatic waitlist positioning with `waitlist_position` tracking
- **Ticketing System**: Multiple ticket types per event, QR-based check-in, transfer, undo check-in, and check-in audit log with method tracking (`qr_scan`, `manual`, `api`, `self_checkin`, `auto_checkin`)
- **Per-Event Admin Roles**: Eight granular permissions — `can_edit_event`, `can_check_in`, `can_view_tickets`, `can_manage_ticket_types`, `can_cancel_tickets`, `can_transfer_tickets`, `can_export_attendance`, `can_assign_event_admins`
- **Platform Sync**: Webhook receivers for Discord and Twitch; manual sync endpoint with sync-status tracking
- **Category Management**: Community-scoped event categories for filtering and organization
- **Context Switching**: Multi-community context management for users active across multiple communities
- **Full-Text Search**: Event search across title and description with date-range, tag, platform, status, and category filters

---

## Documentation Index

| Document | Description |
|---|---|
| [USAGE.md](USAGE.md) | Getting started, running locally, OAuth setup walkthrough, booking flow examples |
| [API.md](API.md) | Every endpoint with method, path, auth requirements, request schema, response schema, error codes |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Internal design, OAuth flow diagrams, service breakdown, data flows, database schema |
| [CONFIGURATION.md](CONFIGURATION.md) | All environment variables, required vs optional, example .env file, Kubernetes config |
| [TESTING.md](TESTING.md) | OAuth mock testing, booking test data, service unit test examples, integration patterns |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | OAuth errors, token expiry, booking conflicts, slot computation failures, FAQ |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | Version history, breaking changes, migration notes |

---

## Quick Reference

| Item | Value |
|---|---|
| Source | action/interactive/calendar_interaction_module |
| Language | Python 3, async (Quart framework) |
| Port | `8030` (default; overridden by `MODULE_PORT` env var) |
| Module Version | `2.0.0` |
| API Blueprints | `/api/v1/calendar`, `/api/v1/context`, `/api/v1/tickets` |
| Database | PostgreSQL via AsyncDAL |
| OAuth Providers | Google Calendar, Microsoft Outlook (Graph API) |
| Token Storage | `platform_integrations` table |
| Connected Calendars | `connected_calendars` table |
| Free/Busy Data | `calendar_free_busy` table |
| Availability Settings | `user_calendar_settings` table |
| Booking Pages | `booking_pages` + `booking_page_members` tables |
| Appointments | `bookings` table |
| Redis Channel | `credentials:calendar_interaction:bot:refreshed` |
| Router Service | `http://router-service:8000` (via `CORE_API_URL`) |
| Maintained by | Penguin Tech Inc |

---

## Service Map

The module instantiates 11 service classes at startup, all sharing a single database abstraction layer (AsyncDAL):

| Service | File | Responsibility |
|---|---|---|
| `CalendarOAuthService` | `services/calendar_oauth_service.py` | Google and Microsoft OAuth flows, token management, free/busy sync |
| `AvailabilityService` | `services/availability_service.py` | User availability settings, free/busy merging, slot computation |
| `BookingService` | `services/booking_service.py` | Individual booking page CRUD, slot availability, booking creation with locking |
| `GroupAvailabilityService` | `services/group_availability_service.py` | Group booking pages, member management, aggregate availability |
| `RSVPService` | `services/rsvp_service.py` | RSVP creation/update, capacity checking, waitlist management |
| `TicketService` | `services/ticket_service.py` | Ticket lifecycle, check-in, undo, transfer, QR verification |
| `EventAdminService` | `services/event_admin_service.py` | Per-event admin roles and granular permission management |
| `CalendarService` | `services/calendar_service.py` | Core event CRUD, approval workflow, recurring event handling |
| `ContextService` | `services/context_service.py` | Multi-community context resolution and switching |
| `PermissionService` | `services/permission_service.py` | Community-level permission checking for event operations |
| `CacheManager` | `services/cache_manager.py` | In-process cache for frequently read data |

---

## API Route Summary

The module exposes routes across three Blueprints. See [API.md](API.md) for complete request/response documentation.

### `/api/v1/calendar` — Calendar Blueprint (50+ routes)

**Events:**
- `GET/POST /api/v1/calendar/{community_id}/events` — list and create events
- `GET/PUT/DELETE /api/v1/calendar/{community_id}/events/{event_id}` — read, update, delete
- `POST /api/v1/calendar/{community_id}/events/{event_id}/approve` — approve pending event
- `POST /api/v1/calendar/{community_id}/events/{event_id}/reject` — reject pending event
- `POST /api/v1/calendar/{community_id}/events/{event_id}/cancel` — cancel event
- `GET/POST/PUT/DELETE /api/v1/calendar/{community_id}/events/{event_id}/rsvp` — RSVP management
- `GET /api/v1/calendar/{community_id}/events/{event_id}/attendees` — attendee list
- `GET /api/v1/calendar/{community_id}/search` — full-text event search
- `GET /api/v1/calendar/{community_id}/upcoming` — upcoming events
- `GET /api/v1/calendar/{community_id}/trending` — trending events
- `POST /api/v1/calendar/{community_id}/sync/enable` — enable platform sync
- `POST /api/v1/calendar/{community_id}/events/{event_id}/sync` — trigger sync
- `GET /api/v1/calendar/{community_id}/events/{event_id}/sync/status` — sync status
- `POST /api/v1/calendar/webhooks/discord` — Discord webhook receiver
- `POST /api/v1/calendar/webhooks/twitch` — Twitch webhook receiver
- `GET/PUT /api/v1/calendar/{community_id}/config/permissions` — permission config
- `GET/PUT /api/v1/calendar/{community_id}/config/reminders` — reminder config
- `GET/POST /api/v1/calendar/{community_id}/categories` — category management

**OAuth:**
- `GET /api/v1/calendar/oauth/google/auth-url` — get Google auth URL
- `GET /api/v1/calendar/oauth/google/callback` — handle Google OAuth callback
- `GET /api/v1/calendar/oauth/microsoft/auth-url` — get Microsoft auth URL
- `GET /api/v1/calendar/oauth/microsoft/callback` — handle Microsoft OAuth callback
- `GET /api/v1/calendar/oauth/calendars` — list connected calendars
- `POST /api/v1/calendar/oauth/calendars/{calendar_id}/sync` — sync free/busy
- `DELETE /api/v1/calendar/oauth/calendars/{calendar_id}` — disconnect calendar

**Availability:**
- `GET/PUT /api/v1/calendar/availability/settings` — calendar settings
- `GET/PUT /api/v1/calendar/availability/weekly` — weekly availability schedule
- `GET /api/v1/calendar/availability/{target_user_id}/slots` — compute available slots

**Booking Pages:**
- `POST/GET /api/v1/calendar/booking-pages` — create and list booking pages
- `GET/PUT/DELETE /api/v1/calendar/booking-pages/{page_id}` — manage booking pages
- `GET /api/v1/calendar/book/{slug}/slots` — get available slots (guest-facing)
- `POST /api/v1/calendar/book/{slug}` — create a booking
- `GET/DELETE /api/v1/calendar/bookings/{uuid}` — view or cancel a booking
- `GET /api/v1/calendar/my-bookings` — list bookings as host

**Group Booking:**
- `POST /api/v1/calendar/booking-pages/{page_id}/members` — add member
- `DELETE /api/v1/calendar/booking-pages/{page_id}/members/{user_id}` — remove member
- `GET /api/v1/calendar/booking-pages/{page_id}/members` — list members
- `GET /api/v1/calendar/booking-pages/{page_id}/group-availability` — aggregate availability
- `GET /api/v1/calendar/booking-pages/{page_id}/best-slots` — top slots across date range

### `/api/v1/context` — Context Blueprint

- `GET /api/v1/context/{entity_id}` — get current context
- `POST /api/v1/context/{entity_id}/switch` — switch community context
- `GET /api/v1/context/{entity_id}/available` — list available communities

### `/api/v1/tickets` — Ticket Blueprint

- `POST /api/v1/tickets/verify-ticket` — verify ticket without check-in
- `GET/POST /api/v1/tickets/{community_id}/events/{event_id}/ticket-types` — ticket types
- `PUT/DELETE /api/v1/tickets/{community_id}/events/{event_id}/ticket-types/{type_id}` — update/delete type
- `POST /api/v1/tickets/{community_id}/events/{event_id}/ticketing/enable` — enable ticketing
- `POST /api/v1/tickets/{community_id}/events/{event_id}/check-in` — check in ticket
- `POST /api/v1/tickets/{community_id}/events/{event_id}/undo-check-in` — undo check-in
- `GET /api/v1/tickets/{community_id}/events/{event_id}/check-in-log` — audit log
- `POST /api/v1/tickets/{community_id}/tickets/{ticket_id}/transfer` — transfer ticket
- `DELETE /api/v1/tickets/{community_id}/tickets/{ticket_id}` — cancel ticket
- `GET/POST /api/v1/tickets/{community_id}/events/{event_id}/admins` — event admin roles
- `PUT/DELETE /api/v1/tickets/{community_id}/events/{event_id}/admins/{admin_id}` — update/revoke
- `GET /api/v1/tickets/{community_id}/events/{event_id}/my-permissions` — current permissions

---

## Deployment Notes

The module is deployed as a standalone container in the WaddleBot Kubernetes cluster. It does not require any sidecar containers.

**Health endpoint:** `GET /health` — returns `{"status": "healthy", "module": "calendar_interaction_module", "version": "2.0.0"}`

**Startup sequence:**
1. Load environment variables (from `.env` or Kubernetes secrets)
2. Initialize database connection via `init_database()`
3. Attempt to load credentials from `platform_integrations` table
4. Start Redis credential listener thread (if `REDIS_URL` is set)
5. Instantiate 11 service classes
6. Register Blueprints and start Quart server on `MODULE_PORT`

**External dependencies:**
- PostgreSQL (`DATABASE_URL`)
- WaddleBot router service (`CORE_API_URL`, `ROUTER_API_URL`)
- Google Calendar API (if `GOOGLE_CALENDAR_CLIENT_ID` is set)
- Microsoft Graph API (if `MICROSOFT_CALENDAR_CLIENT_ID` is set)
- Redis (optional, `REDIS_URL`)

See [CONFIGURATION.md](CONFIGURATION.md) for all environment variable details and [ARCHITECTURE.md](ARCHITECTURE.md) for the complete internal design.

---

## Related Modules

This module integrates with several other WaddleBot modules:

- **Router Module** (processing/router_module) — Provides command routing; the calendar module's services are accessible via the router API at `ROUTER_API_URL`.
- **Labels Core** (`labels-core-service:8025`) — Used for tag and label resolution on events; referenced via `LABELS_API_URL`.
- **Admin Hub Frontend** (`admin/hub_module`) — Admin dashboard pages (`AdminCalendarTicketing.jsx`, `AdminCommunityCalls.jsx`) consume the calendar module's API for event management and community calendar configuration.

For cross-module communication patterns, see the router module documentation at `docs/router_module/`.
