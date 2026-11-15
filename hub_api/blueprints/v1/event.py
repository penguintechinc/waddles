"""v1 `event` group -- Event module (`waddles.event.calendar`), M8 in the migration plan.

Ports BOTH Node controllers the migration plan's §2 SCCEBM mapping
assigns to the Event module: `calendarController.js` (user-facing OAuth,
availability, booking pages, public booking, user bookings, group
scheduling -- mounted at `/api/v1/calendar/*`) and `ticketController.js`
(admin ticketing/check-in/attendance, mounted alongside
`calendarAdmin.js`'s own event-CRUD/RSVP handlers at `/api/v1/admin/*`,
`ticketController.js`'s own module docstring: "proxy requests to calendar
module for ticketing operations" -- migration plan §2 footnote "proxy to
calendar for event ticketing"). Both Node files hold no business logic:
every handler is a pure reverse-proxy to the standalone
`calendar-interaction` service (`action/interactive/
calendar_interaction_module`, port 8038) via `calendarProxy.js`'s
`proxyToCalendar`/`buildUserContext` -- ported as
`services/event_calendar_proxy.py`.

Pattern deviation from `blueprints/v2/platform.py`'s per-function exemplar
(documented, not accidental): platform.py's copy-me pattern is one
function per endpoint with an explicit request/response DTO pair, right
for a group that OWNS its data. This group's 58 endpoints are
structurally identical opaque-proxy calls (route -> tenant+scope ->
forward path/query/body to `calendar-interaction` -> relay its JSON
verbatim) with zero owned schema to model -- a `ProxyRoute` table +
one generic handler keeps the route/method/downstream-path/scope mapping
auditable in one place instead of 58 near-duplicate function bodies, at
zero cost to the migration checklist's mandates (path IDENTICAL to
`api.js`, tenant-before-scope, tenant from JWT only). `@validate_request`/
`@validate_response` are intentionally NOT used here: the response body
is whatever `calendar-interaction` returns, not a hub-api-owned row, so
security.md's Output Validation concern (accidental over-serialization of
an owned model) does not apply -- see `services/event_calendar_proxy.py`'s
module docstring.

Known inherited behavior (preserved, not fixed -- migration plan's
Non-goals: "no behavior changes"): Node's `errorHandler.js` reads
`err.statusCode`, but `calendarProxy.js`'s thrown `Error` only ever sets
`err.status` (property-name mismatch) -- every downstream proxy failure,
whatever `calendar-interaction`'s real status code was, surfaces to the
React app as a generic HTTP 500 `{success:false, error:{code:
"INTERNAL_ERROR", message:"An unexpected error occurred"}}` in
production. `EventCalendarProxyClient`/this blueprint reproduce that
externally-observable contract exactly (`_make_handler`'s `not result.ok`
branch) rather
than silently improving it mid-port; flagged here and in the PR
description as a pre-existing bug for a follow-up ticket, not an
in-scope fix for a byte-for-byte controller port.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from flask_core.api_utils import error_response
from flask_core.auth import verify_jwt_token
from flask_core.authz import require_scope
from flask_core.feature_flags import feature_enabled
from flask_core.secrets import require_secret_key
from flask_core.tenancy import get_tenant_context, tenant_middleware
from quart import Blueprint, Response, request

from services.event_calendar_proxy import EventCalendarProxyClient, UserContext

#: Two-gate feature flags (license tier AND PostHog) for the Event module's
#: two capabilities -- `libs/event_module/features.py`'s
#: `event.calendar`/`event.ticketing` Feature contracts. `calendarController.
#: js`/`calendarAdmin.js` handlers (event CRUD, availability, booking,
#: RSVPs) gate on `FEATURE_EVENT_CALENDAR`; `ticketController.js` handlers
#: (ticket types, tickets, check-in, attendance, ticketing config) gate on
#: `FEATURE_EVENT_TICKETING` -- see migration plan SS2's controller table.
FEATURE_EVENT_CALENDAR = "waddles.event.calendar"
FEATURE_EVENT_TICKETING = "waddles.event.ticketing"

#: Any authenticated caller may read/write their own calendar data --
#: Node's `requireAuth` checks only "is logged in", no per-action role.
#: Formalized as OIDC scopes (security.md: never role names) rather than
#: left as a bare authenticated/not-authenticated check, matching how
#: every other ported group already declares its scopes; a `viewer`
#: bundle's `*:read` / a `maintainer` bundle's `*:read *:write` (both
#: pre-existing SCOPE_BUNDLES, per `_scope_covers`'s wildcard rule)
#: already covers these for any normal logged-in user.
SCOPE_READ = "event.calendar:read"
SCOPE_WRITE = "event.calendar:write"
#: Node's `requireCommunityAdmin` -- community-admin-only surfaces
#: (event CRUD, ticketing config, check-in, attendance, event admins).
SCOPE_ADMIN = "event.calendar:admin"

event_calendar_bp = Blueprint("v1_event_calendar", __name__, url_prefix="/api/v1/calendar")
event_calendar_admin_bp = Blueprint("v1_event_calendar_admin", __name__, url_prefix="/api/v1/admin")

_proxy_client = EventCalendarProxyClient()


@dataclass(slots=True, frozen=True)
class ProxyRoute:
    """One `calendarController.js`/`ticketController.js` handler, table-driven.

    `rule`/`methods` are the hub-api-facing path (relative to the owning
    blueprint's `url_prefix`) -- IDENTICAL to the Node route (migration
    checklist §4 step 1). `downstream_template` is the path forwarded to
    `calendar-interaction`, `.format(**path_args)`-filled from the same
    Quart path converters as `rule` (Node: `req.params` interpolated
    into the proxied URL, byte-for-byte matched here).
    """

    name: str
    rule: str
    method: str
    downstream_template: str
    #: `None` = no auth decorator at all -- Node's `optionalAuth`/public
    #: booking surface (calendar.js's `book/:slug/*`), never a scope
    #: gate applied then silently satisfied by an empty scope claim.
    scope: str | None
    status_code: int = 200
    query_params: tuple[str, ...] = ()
    has_body: bool = False
    #: `verifyTicket`/`checkIn` append `ip_address`/`user_agent` to the
    #: forwarded body (audit trail for check-in scanning) -- Node does
    #: this only for these two, not uniformly.
    attach_client_meta: bool = False
    csv_capable: bool = False
    #: Two-gate Feature flag this route is gated by (`FEATURE_EVENT_CALENDAR`
    #: or `FEATURE_EVENT_TICKETING`). `None` = ungated (the public,
    #: `scope=None` booking routes -- no tenant context to evaluate a gate
    #: against; see `_make_handler`).
    feature_flag: str | None = FEATURE_EVENT_CALENDAR


# calendarController.js -- /api/v1/calendar/* (user-facing)

_CALENDAR_ROUTES: tuple[ProxyRoute, ...] = (
    # OAuth
    ProxyRoute("cal_google_auth_url", "/oauth/google/auth-url", "GET",
               "/api/v1/calendar/oauth/google/auth-url", SCOPE_READ),
    ProxyRoute("cal_microsoft_auth_url", "/oauth/microsoft/auth-url", "GET",
               "/api/v1/calendar/oauth/microsoft/auth-url", SCOPE_READ),
    ProxyRoute("cal_connected_calendars", "/oauth/calendars", "GET",
               "/api/v1/calendar/oauth/calendars", SCOPE_READ),
    ProxyRoute("cal_sync_calendar", "/oauth/calendars/<id>/sync", "POST",
               "/api/v1/calendar/oauth/calendars/{id}/sync", SCOPE_WRITE),
    ProxyRoute("cal_disconnect_calendar", "/oauth/calendars/<id>", "DELETE",
               "/api/v1/calendar/oauth/calendars/{id}", SCOPE_WRITE),
    # Availability
    ProxyRoute("cal_get_availability_settings", "/availability/settings", "GET",
               "/api/v1/calendar/availability/settings", SCOPE_READ),
    ProxyRoute("cal_update_availability_settings", "/availability/settings", "PUT",
               "/api/v1/calendar/availability/settings", SCOPE_WRITE, has_body=True),
    ProxyRoute("cal_get_weekly_availability", "/availability/weekly", "GET",
               "/api/v1/calendar/availability/weekly", SCOPE_READ),
    ProxyRoute("cal_update_weekly_availability", "/availability/weekly", "PUT",
               "/api/v1/calendar/availability/weekly", SCOPE_WRITE, has_body=True),
    ProxyRoute("cal_available_slots", "/availability/<user_id>/slots", "GET",
               "/api/v1/calendar/availability/{user_id}/slots", SCOPE_READ,
               query_params=("start", "end", "duration")),
    # Booking pages
    ProxyRoute("cal_create_booking_page", "/booking-pages", "POST",
               "/api/v1/calendar/booking-pages", SCOPE_WRITE, status_code=201, has_body=True),
    ProxyRoute("cal_list_booking_pages", "/booking-pages", "GET",
               "/api/v1/calendar/booking-pages", SCOPE_READ),
    ProxyRoute("cal_get_booking_page", "/booking-pages/<id>", "GET",
               "/api/v1/calendar/booking-pages/{id}", SCOPE_READ),
    ProxyRoute("cal_update_booking_page", "/booking-pages/<id>", "PUT",
               "/api/v1/calendar/booking-pages/{id}", SCOPE_WRITE, has_body=True),
    ProxyRoute("cal_delete_booking_page", "/booking-pages/<id>", "DELETE",
               "/api/v1/calendar/booking-pages/{id}", SCOPE_WRITE),
    # Public booking -- no auth decorator, matches Node's `optionalAuth`.
    # feature_flag=None: no tenant context (tenant_middleware never runs
    # for a scope=None route) to evaluate a gate against.
    ProxyRoute("cal_booking_slots", "/book/<slug>/slots", "GET",
               "/api/v1/calendar/book/{slug}/slots", None, query_params=("start", "end"),
               feature_flag=None),
    ProxyRoute("cal_create_booking", "/book/<slug>", "POST",
               "/api/v1/calendar/book/{slug}", None, status_code=201, has_body=True,
               feature_flag=None),
    # User bookings
    ProxyRoute("cal_my_bookings", "/my-bookings", "GET",
               "/api/v1/calendar/my-bookings", SCOPE_READ,
               query_params=("status", "limit", "offset")),
    ProxyRoute("cal_get_booking", "/bookings/<uuid>", "GET",
               "/api/v1/calendar/bookings/{uuid}", SCOPE_READ),
    ProxyRoute("cal_cancel_booking", "/bookings/<uuid>", "DELETE",
               "/api/v1/calendar/bookings/{uuid}", SCOPE_WRITE),
    # Group scheduling
    ProxyRoute("cal_add_group_member", "/booking-pages/<page_id>/members", "POST",
               "/api/v1/calendar/booking-pages/{page_id}/members", SCOPE_WRITE,
               status_code=201, has_body=True),
    ProxyRoute("cal_remove_group_member", "/booking-pages/<page_id>/members/<user_id>", "DELETE",
               "/api/v1/calendar/booking-pages/{page_id}/members/{user_id}", SCOPE_WRITE),
    ProxyRoute("cal_group_members", "/booking-pages/<page_id>/members", "GET",
               "/api/v1/calendar/booking-pages/{page_id}/members", SCOPE_READ),
    ProxyRoute("cal_group_availability", "/booking-pages/<page_id>/group-availability", "GET",
               "/api/v1/calendar/booking-pages/{page_id}/group-availability", SCOPE_READ,
               query_params=("start", "end")),
    ProxyRoute("cal_best_slots", "/booking-pages/<page_id>/best-slots", "GET",
               "/api/v1/calendar/booking-pages/{page_id}/best-slots", SCOPE_READ,
               query_params=("start", "end", "duration")),
)

# calendarAdmin.js + ticketController.js -- /api/v1/admin/* (community admin)

_CALENDAR_ADMIN_ROUTES: tuple[ProxyRoute, ...] = (
    # Calendar events CRUD
    ProxyRoute("adm_list_events", "/<community_id>/calendar/events", "GET",
               "/api/v1/calendar/{community_id}/events", SCOPE_ADMIN,
               query_params=("status", "category", "limit", "offset")),
    ProxyRoute("adm_create_event", "/<community_id>/calendar/events", "POST",
               "/api/v1/calendar/{community_id}/events", SCOPE_ADMIN,
               status_code=201, has_body=True),
    ProxyRoute("adm_get_event", "/<community_id>/calendar/events/<event_id>", "GET",
               "/api/v1/calendar/{community_id}/events/{event_id}", SCOPE_ADMIN),
    ProxyRoute("adm_update_event", "/<community_id>/calendar/events/<event_id>", "PUT",
               "/api/v1/calendar/{community_id}/events/{event_id}", SCOPE_ADMIN, has_body=True),
    ProxyRoute("adm_delete_event", "/<community_id>/calendar/events/<event_id>", "DELETE",
               "/api/v1/calendar/{community_id}/events/{event_id}", SCOPE_ADMIN),
    # Event approval
    ProxyRoute("adm_approve_event", "/<community_id>/calendar/events/<event_id>/approve", "POST",
               "/api/v1/calendar/{community_id}/events/{event_id}/approve", SCOPE_ADMIN),
    ProxyRoute("adm_reject_event", "/<community_id>/calendar/events/<event_id>/reject", "POST",
               "/api/v1/calendar/{community_id}/events/{event_id}/reject", SCOPE_ADMIN,
               has_body=True),
    # RSVPs -- base auth only in Node (no requireCommunityAdmin)
    ProxyRoute("adm_rsvp_create", "/<community_id>/calendar/events/<event_id>/rsvp", "POST",
               "/api/v1/calendar/{community_id}/events/{event_id}/rsvp", SCOPE_WRITE,
               has_body=True),
    ProxyRoute("adm_rsvp_cancel", "/<community_id>/calendar/events/<event_id>/rsvp", "DELETE",
               "/api/v1/calendar/{community_id}/events/{event_id}/rsvp", SCOPE_WRITE),
    ProxyRoute("adm_attendees", "/<community_id>/calendar/events/<event_id>/attendees", "GET",
               "/api/v1/calendar/{community_id}/events/{event_id}/attendees", SCOPE_ADMIN),
    ProxyRoute("adm_rsvp_counts", "/<community_id>/calendar/events/<event_id>/rsvp-counts", "GET",
               "/api/v1/calendar/{community_id}/events/{event_id}/rsvp-counts", SCOPE_READ),
    # Ticket types -- ticketController.js -> FEATURE_EVENT_TICKETING
    ProxyRoute("adm_list_ticket_types",
               "/<community_id>/calendar/events/<event_id>/ticket-types", "GET",
               "/api/v1/calendar/{community_id}/events/{event_id}/ticket-types", SCOPE_ADMIN,
               feature_flag=FEATURE_EVENT_TICKETING),
    ProxyRoute("adm_create_ticket_type",
               "/<community_id>/calendar/events/<event_id>/ticket-types", "POST",
               "/api/v1/calendar/{community_id}/events/{event_id}/ticket-types", SCOPE_ADMIN,
               status_code=201, has_body=True, feature_flag=FEATURE_EVENT_TICKETING),
    ProxyRoute("adm_update_ticket_type",
               "/<community_id>/calendar/events/<event_id>/ticket-types/<type_id>", "PUT",
               "/api/v1/calendar/{community_id}/events/{event_id}/ticket-types/{type_id}",
               SCOPE_ADMIN, has_body=True, feature_flag=FEATURE_EVENT_TICKETING),
    ProxyRoute("adm_delete_ticket_type",
               "/<community_id>/calendar/events/<event_id>/ticket-types/<type_id>", "DELETE",
               "/api/v1/calendar/{community_id}/events/{event_id}/ticket-types/{type_id}",
               SCOPE_ADMIN, feature_flag=FEATURE_EVENT_TICKETING),
    # Tickets -- ticketController.js -> FEATURE_EVENT_TICKETING
    ProxyRoute("adm_list_tickets", "/<community_id>/calendar/events/<event_id>/tickets", "GET",
               "/api/v1/calendar/{community_id}/events/{event_id}/tickets", SCOPE_ADMIN,
               query_params=("status", "is_checked_in", "ticket_type_id", "search",
                             "limit", "offset"), feature_flag=FEATURE_EVENT_TICKETING),
    ProxyRoute("adm_create_ticket", "/<community_id>/calendar/events/<event_id>/tickets", "POST",
               "/api/v1/calendar/{community_id}/events/{event_id}/tickets", SCOPE_ADMIN,
               status_code=201, has_body=True, feature_flag=FEATURE_EVENT_TICKETING),
    ProxyRoute("adm_get_ticket",
               "/<community_id>/calendar/events/<event_id>/tickets/<ticket_id>", "GET",
               "/api/v1/calendar/{community_id}/events/{event_id}/tickets/{ticket_id}",
               SCOPE_ADMIN, feature_flag=FEATURE_EVENT_TICKETING),
    ProxyRoute("adm_cancel_ticket",
               "/<community_id>/calendar/events/<event_id>/tickets/<ticket_id>/cancel", "POST",
               "/api/v1/calendar/{community_id}/events/{event_id}/tickets/{ticket_id}/cancel",
               SCOPE_ADMIN, has_body=True, feature_flag=FEATURE_EVENT_TICKETING),
    ProxyRoute("adm_transfer_ticket",
               "/<community_id>/calendar/events/<event_id>/tickets/<ticket_id>/transfer", "POST",
               "/api/v1/calendar/{community_id}/events/{event_id}/tickets/{ticket_id}/transfer",
               SCOPE_ADMIN, has_body=True, feature_flag=FEATURE_EVENT_TICKETING),
    # Check-in -- ticketController.js -> FEATURE_EVENT_TICKETING
    ProxyRoute("adm_verify_ticket", "/calendar/verify-ticket", "POST",
               "/api/v1/calendar/verify-ticket", SCOPE_WRITE,
               has_body=True, attach_client_meta=True, feature_flag=FEATURE_EVENT_TICKETING),
    ProxyRoute("adm_check_in", "/<community_id>/calendar/events/<event_id>/check-in", "POST",
               "/api/v1/calendar/{community_id}/events/{event_id}/check-in", SCOPE_ADMIN,
               has_body=True, attach_client_meta=True, feature_flag=FEATURE_EVENT_TICKETING),
    ProxyRoute("adm_undo_check_in",
               "/<community_id>/calendar/events/<event_id>/tickets/<ticket_id>/undo-check-in",
               "POST",
               "/api/v1/calendar/{community_id}/events/{event_id}/tickets/{ticket_id}/undo-check-in",
               SCOPE_ADMIN, has_body=True, feature_flag=FEATURE_EVENT_TICKETING),
    # Attendance & reporting -- ticketController.js -> FEATURE_EVENT_TICKETING
    ProxyRoute("adm_attendance_stats", "/<community_id>/calendar/events/<event_id>/attendance",
               "GET", "/api/v1/calendar/{community_id}/events/{event_id}/attendance", SCOPE_ADMIN,
               feature_flag=FEATURE_EVENT_TICKETING),
    ProxyRoute("adm_check_in_log", "/<community_id>/calendar/events/<event_id>/check-in-log",
               "GET", "/api/v1/calendar/{community_id}/events/{event_id}/check-in-log", SCOPE_ADMIN,
               query_params=("limit", "offset", "success_only"),
               feature_flag=FEATURE_EVENT_TICKETING),
    ProxyRoute("adm_export_attendance",
               "/<community_id>/calendar/events/<event_id>/attendance/export", "GET",
               "/api/v1/calendar/{community_id}/events/{event_id}/attendance/export", SCOPE_ADMIN,
               query_params=("format",), csv_capable=True, feature_flag=FEATURE_EVENT_TICKETING),
    # Event admins
    ProxyRoute("adm_list_event_admins", "/<community_id>/calendar/events/<event_id>/admins",
               "GET", "/api/v1/calendar/{community_id}/events/{event_id}/admins", SCOPE_ADMIN),
    ProxyRoute("adm_assign_event_admin", "/<community_id>/calendar/events/<event_id>/admins",
               "POST", "/api/v1/calendar/{community_id}/events/{event_id}/admins", SCOPE_ADMIN,
               status_code=201, has_body=True),
    ProxyRoute("adm_update_event_admin",
               "/<community_id>/calendar/events/<event_id>/admins/<admin_id>", "PUT",
               "/api/v1/calendar/{community_id}/events/{event_id}/admins/{admin_id}", SCOPE_ADMIN,
               has_body=True),
    ProxyRoute("adm_revoke_event_admin",
               "/<community_id>/calendar/events/<event_id>/admins/<admin_id>", "DELETE",
               "/api/v1/calendar/{community_id}/events/{event_id}/admins/{admin_id}", SCOPE_ADMIN,
               has_body=True),
    ProxyRoute("adm_my_permissions", "/<community_id>/calendar/events/<event_id>/my-permissions",
               "GET", "/api/v1/calendar/{community_id}/events/{event_id}/my-permissions",
               SCOPE_READ),
    # Ticketing configuration -- ticketController.js -> FEATURE_EVENT_TICKETING
    ProxyRoute("adm_enable_ticketing",
               "/<community_id>/calendar/events/<event_id>/ticketing/enable", "POST",
               "/api/v1/calendar/{community_id}/events/{event_id}/ticketing/enable", SCOPE_ADMIN,
               has_body=True, feature_flag=FEATURE_EVENT_TICKETING),
    ProxyRoute("adm_disable_ticketing",
               "/<community_id>/calendar/events/<event_id>/ticketing/disable", "POST",
               "/api/v1/calendar/{community_id}/events/{event_id}/ticketing/disable", SCOPE_ADMIN,
               feature_flag=FEATURE_EVENT_TICKETING),
)


def _build_user_context(req: Any) -> UserContext:
    """Port of `calendarProxy.js::buildUserContext` -- re-decodes the bearer token.

    Self-contained re-decode (same pattern `authz.require_scope` already
    uses, its own docstring's rationale: independent of
    `tenant_middleware`'s request-local state, testable in isolation) --
    load-bearing here because public routes (`scope=None`) never run
    `tenant_middleware` at all, yet `optionalAuth` callers may still carry
    a bearer token that should populate `user_id`/`username`. No
    `isSuperAdmin` DB flag exists in this JWT (that lives with
    `userManagementController`, M1 -- out of scope for the Event group);
    `roles` (audit/display only per security.md) is the closest available
    signal, so a `"super_admin"` entry maps to `role="super_admin"`,
    matching Node's boolean check as closely as this token's claims allow.
    """
    auth_header = req.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return UserContext(user_id=None, username=None, role="anonymous")
    secret_key = require_secret_key()
    payload = verify_jwt_token(auth_header[7:], secret_key)
    if payload is None:
        return UserContext(user_id=None, username=None, role="anonymous")
    roles = payload.get("roles") or []
    role = "super_admin" if "super_admin" in roles else "admin"
    return UserContext(user_id=payload.get("sub"), username=payload.get("username"), role=role)


def _select_query(req: Any, names: tuple[str, ...]) -> dict[str, str]:
    """Whitelist-forward only the query params Node's own controller reads."""
    selected: dict[str, str] = {}
    for name in names:
        value = req.args.get(name)
        if value is not None:
            selected[name] = value
    return selected


async def _read_body(req: Any) -> dict[str, Any]:
    """Port of `JSON.stringify(req.body)` -- Express defaults a bodyless request to `{}`."""
    data = await req.get_json(force=True, silent=True)
    return data if isinstance(data, dict) else {}


def _make_handler(route: ProxyRoute) -> Callable[..., Any]:
    """Build one Quart view function from a `ProxyRoute` row."""

    async def handler(**path_args: str) -> Any:
        if route.feature_flag is not None:
            ctx = get_tenant_context(request)
            if ctx is None or not await feature_enabled(route.feature_flag, tenant=ctx.tenant_slug):
                return error_response(
                    "This Event feature requires a higher plan or is not yet enabled",
                    status_code=402,
                    error_code="FEATURE_NOT_ENABLED",
                )
        user_context = _build_user_context(request)
        query = _select_query(request, route.query_params)
        body: dict[str, Any] | None = await _read_body(request) if route.has_body else None
        if route.attach_client_meta:
            body = {
                **(body or {}),
                "ip_address": request.remote_addr or "",
                "user_agent": request.headers.get("User-Agent", ""),
            }
        downstream_path = route.downstream_template.format(**path_args)
        result = await _proxy_client.request(
            route.method,
            downstream_path,
            user_context=user_context,
            query=query or None,
            json_body=body,
        )
        if (
            not result.ok
            and result.status_code == 503
            and isinstance(result.body, dict)
            and result.body.get("error") == "service_unavailable"
        ):
            return result.body, 503
        if not result.ok:
            # See module docstring "Known inherited behavior" -- masked
            # 500 on every downstream failure, matching Node's
            # `errorHandler.js` property-name-mismatch bug exactly.
            return error_response(
                "An unexpected error occurred", status_code=500, error_code="INTERNAL_ERROR"
            )
        if route.csv_capable and query.get("format") == "csv":
            event_id = path_args.get("event_id", "export")
            return Response(
                json.dumps(result.body),
                mimetype="text/csv",
                headers={"Content-Disposition": f"attachment; filename=attendance-{event_id}.csv"},
            )
        return result.body, route.status_code

    handler.__name__ = route.name
    return handler


def _register(bp: Blueprint, routes: tuple[ProxyRoute, ...]) -> None:
    """Wire every `ProxyRoute` onto `bp`: tenant_middleware(outermost) -> require_scope -> handler.

    `scope=None` routes (public booking) get neither decorator -- matches
    Node's `optionalAuth` (auth genuinely optional, not silently
    downgraded to an always-satisfied scope check).
    """
    for route in routes:
        view = _make_handler(route)
        if route.scope is not None:
            # Unlike `blueprints/v2/platform.py`'s static `@decorator` use
            # (which mypy --strict flags "untyped decorator" per that
            # file's own docstring), calling `require_scope(...)`/
            # `tenant_middleware` here is a plain function call against
            # `Callable[..., Any]` -- already `Any`-typed at both ends
            # under `follow_imports = "skip"` (pyproject.toml), so no
            # ignore is needed or accepted (`warn_unused_ignores = true`).
            view = require_scope(route.scope)(view)
            view = tenant_middleware(view)
        bp.route(route.rule, methods=[route.method], endpoint=route.name)(view)


_register(event_calendar_bp, _CALENDAR_ROUTES)
_register(event_calendar_admin_bp, _CALENDAR_ADMIN_ROUTES)

BLUEPRINTS: list[Blueprint] = [event_calendar_bp, event_calendar_admin_bp]

#: Exposed for tests -- data-driven characterization coverage over every
#: ported route without hand-duplicating the table (see
#: `tests/test_event_blueprint.py`).
ALL_ROUTES: tuple[ProxyRoute, ...] = _CALENDAR_ROUTES + _CALENDAR_ADMIN_ROUTES
