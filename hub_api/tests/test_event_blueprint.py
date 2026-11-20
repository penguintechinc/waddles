"""`blueprints/v1/event.py` -- characterization tests for the Event module port (M8).

Covers every one of the 58 `ProxyRoute` entries data-driven (path/method/
scope), plus targeted tests for the proxy-specific behaviors a bare
route-table walk can't prove: downstream path templating, query
whitelisting, body/`ip_address`/`user_agent` forwarding, the CSV export
branch, and the masked-500 failure path (see event.py's "Known inherited
behavior" docstring section).

Fail-first proof (executed, not narrated), two rounds:

Round 1 exposed a real gap in this file's first draft: `adm_delete_event`'s
`scope` was changed `SCOPE_ADMIN` -> `SCOPE_READ` in `blueprints/v1/
event.py`, expecting `test_admin_routes_reject_read_only_scope`
(then parametrized over `[r for r in ALL_ROUTES if r.scope ==
SCOPE_ADMIN]`) to go red. It didn't -- total passed dropped 168 -> 167
with **zero** failures, because that filter is evaluated once at
collection time: the mutated route simply fell out of the parametrize
set instead of failing inside it. A live route table can silently drop
test coverage on a regression instead of catching it. Fixed by adding
`test_scope_assignment_matches_expected_table`, parametrized over the
full, count-stable `ALL_ROUTES` (58, always) and asserting each route's
scope against `_EXPECTED_SCOPES`, a hand-transcribed oracle independent
of the table under test -- `test_admin_routes_reject_read_only_scope`
kept as a complementary behavioral check, not a replacement.

Round 2, same mutation, re-run: exactly one failure --
`test_scope_assignment_matches_expected_table[adm_delete_event]`,
`AssertionError: assert 'event.calendar:read' == 'event.calendar:admin'`
-- 225 passed, 1 failed, 4 skipped (was 227 passed, 4 skipped). Reverted;
back to 227 passed, 4 skipped, 0 failed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.event import (
    _CALENDAR_ADMIN_ROUTES,
    _CALENDAR_ROUTES,
    ALL_ROUTES,
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    event_calendar_admin_bp,
    event_calendar_bp,
)
from services.event_calendar_proxy import ProxyResult
from tests.community_ids import install_community_ids, public_id

_PATH_FILL = {
    "id": "42",
    "user_id": "7",
    "page_id": "9",
    "slug": "acme-standup",
    "uuid": "b3f1c2d4-0000-4000-8000-000000000001",
    "community_id": "3",
    "event_id": "11",
    "type_id": "5",
    "ticket_id": "77",
    "admin_id": "8",
}

#: `route.rule` is relative to its owning blueprint's `url_prefix` (by
#: design -- see event.py's `ProxyRoute` docstring); tests need the full
#: hub-api-facing path, so map each route name back to the prefix its
#: table (`_CALENDAR_ROUTES` vs `_CALENDAR_ADMIN_ROUTES`) was registered
#: under.
_ROUTE_PREFIX: dict[str, str] = {
    **{route.name: event_calendar_bp.url_prefix or "" for route in _CALENDAR_ROUTES},
    **{route.name: event_calendar_admin_bp.url_prefix or "" for route in _CALENDAR_ADMIN_ROUTES},
}


def _full_path(route: Any, pid: str) -> str:
    """Prefix + `/booking-pages/<page_id>` -> `/api/v1/calendar/booking-pages/9`."""
    filled = route.rule.replace("<community_id>", pid)
    for name, value in _PATH_FILL.items():
        filled = filled.replace(f"<{name}>", value)
    return _ROUTE_PREFIX[route.name] + filled


#: Independent oracle of every route's intended scope -- hand-transcribed
#: from `blueprints/v1/event.py` at port time, deliberately NOT derived
#: from `ALL_ROUTES` itself (a table that asserts against its own source
#: catches nothing). `test_scope_assignment_matches_expected_table` diffs
#: the live table against this one.
_EXPECTED_SCOPES: dict[str, str | None] = {
    "cal_google_auth_url": SCOPE_READ,
    "cal_microsoft_auth_url": SCOPE_READ,
    "cal_connected_calendars": SCOPE_READ,
    "cal_sync_calendar": SCOPE_WRITE,
    "cal_disconnect_calendar": SCOPE_WRITE,
    "cal_get_availability_settings": SCOPE_READ,
    "cal_update_availability_settings": SCOPE_WRITE,
    "cal_get_weekly_availability": SCOPE_READ,
    "cal_update_weekly_availability": SCOPE_WRITE,
    "cal_available_slots": SCOPE_READ,
    "cal_create_booking_page": SCOPE_WRITE,
    "cal_list_booking_pages": SCOPE_READ,
    "cal_get_booking_page": SCOPE_READ,
    "cal_update_booking_page": SCOPE_WRITE,
    "cal_delete_booking_page": SCOPE_WRITE,
    "cal_booking_slots": None,
    "cal_create_booking": None,
    "cal_my_bookings": SCOPE_READ,
    "cal_get_booking": SCOPE_READ,
    "cal_cancel_booking": SCOPE_WRITE,
    "cal_add_group_member": SCOPE_WRITE,
    "cal_remove_group_member": SCOPE_WRITE,
    "cal_group_members": SCOPE_READ,
    "cal_group_availability": SCOPE_READ,
    "cal_best_slots": SCOPE_READ,
    "adm_list_events": SCOPE_ADMIN,
    "adm_create_event": SCOPE_ADMIN,
    "adm_get_event": SCOPE_ADMIN,
    "adm_update_event": SCOPE_ADMIN,
    "adm_delete_event": SCOPE_ADMIN,
    "adm_approve_event": SCOPE_ADMIN,
    "adm_reject_event": SCOPE_ADMIN,
    "adm_rsvp_create": SCOPE_WRITE,
    "adm_rsvp_cancel": SCOPE_WRITE,
    "adm_attendees": SCOPE_ADMIN,
    "adm_rsvp_counts": SCOPE_READ,
    "adm_list_ticket_types": SCOPE_ADMIN,
    "adm_create_ticket_type": SCOPE_ADMIN,
    "adm_update_ticket_type": SCOPE_ADMIN,
    "adm_delete_ticket_type": SCOPE_ADMIN,
    "adm_list_tickets": SCOPE_ADMIN,
    "adm_create_ticket": SCOPE_ADMIN,
    "adm_get_ticket": SCOPE_ADMIN,
    "adm_cancel_ticket": SCOPE_ADMIN,
    "adm_transfer_ticket": SCOPE_ADMIN,
    "adm_verify_ticket": SCOPE_WRITE,
    "adm_check_in": SCOPE_ADMIN,
    "adm_undo_check_in": SCOPE_ADMIN,
    "adm_attendance_stats": SCOPE_ADMIN,
    "adm_check_in_log": SCOPE_ADMIN,
    "adm_export_attendance": SCOPE_ADMIN,
    "adm_list_event_admins": SCOPE_ADMIN,
    "adm_assign_event_admin": SCOPE_ADMIN,
    "adm_update_event_admin": SCOPE_ADMIN,
    "adm_revoke_event_admin": SCOPE_ADMIN,
    "adm_my_permissions": SCOPE_READ,
    "adm_enable_ticketing": SCOPE_ADMIN,
    "adm_disable_ticketing": SCOPE_ADMIN,
}


@pytest.fixture
def app(tenant_db: Any) -> Quart:
    # Minimal communities table; ids 1..3 seeded so community 3 matches the numeric id the
    # downstream-path assertions expect (the cuuid hook swaps the public UUID back to it).
    tenant_db.define_table("communities", Field("name"))
    for n in range(1, 4):
        tenant_db.communities.insert(name=f"c{n}")
    tenant_db.commit()
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = tenant_db
    install_community_ids(quart_app, tenant_db)
    quart_app.register_blueprint(event_calendar_bp)
    quart_app.register_blueprint(event_calendar_admin_bp)
    return quart_app


@pytest.fixture
def cpid(app: Quart, tenant_db: Any) -> str:
    """Public UUID of seeded community 3."""
    return public_id(tenant_db, 3)


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture
def proxy_stub(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the module-level `_proxy_client.request` -- no real network I/O in tests.

    Default return is a successful, empty-body relay; individual tests
    override `.return_value`/`.side_effect` to assert failure-masking,
    CSV branching, etc. Patches the shared singleton instance's bound
    method (not the module attribute) so every already-built handler
    closure -- which looks up `_proxy_client` by module-global name at
    call time -- picks it up transparently.
    """
    import blueprints.v1.event as event_module

    stub = AsyncMock(return_value=ProxyResult(ok=True, status_code=200, body={"ok": True}))
    monkeypatch.setattr(event_module._proxy_client, "request", stub)
    return stub


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Default the two-gate Event Feature flags ON for every test in this file.

    `blueprints/v1/event.py`'s `_make_handler` gates every non-public route
    on `feature_enabled(route.feature_flag, ...)` (this PR -- see
    `TestFeatureGate` below for the dedicated OFF/tier-denial proof). Every
    OTHER test class here exercises routing/scope/body-forwarding behavior
    unrelated to the flag gate, so this autouse fixture keeps them green by
    defaulting the gate to always-pass -- same pattern
    `test_v1_analytics_blueprint.py` uses per-test, applied file-wide here
    since nearly every test in this file would otherwise need it.
    """
    import blueprints.v1.event as event_module

    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(event_module, "feature_enabled", stub)
    return stub


class TestRouteTableCharacterization:
    """Data-driven proof over all 58 ported endpoints -- path/method/auth, not shape."""

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: r.name)
    async def test_gated_route_without_token_is_401(
        self, cpid: str, client: Any, route: Any
    ) -> None:
        """Every scoped route rejects an anonymous caller -- `tenant_middleware` first."""
        if route.scope is None:
            pytest.skip("public route -- no auth decorator by design (Node's optionalAuth)")
        response = await client.open(_full_path(route, cpid), method=route.method)
        assert response.status_code == 401

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: r.name)
    async def test_gated_route_with_correct_scope_reaches_proxy(
        self,
        cpid: str,
        client: Any,
        route: Any,
        auth_headers: Any,
        proxy_stub: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A token carrying exactly the declared scope reaches the handler (not 401/403)."""
        if route.scope is None:
            pytest.skip("public route -- covered by TestPublicBookingRoutes")
        if route.member_guard:  # membership is proven in test_read_route_authz.py
            from tests.conftest import apply_route_guard_bypass

            apply_route_guard_bypass(monkeypatch)
            # this fixture's `communities` table has no tenant_id column
            monkeypatch.setattr("services.route_guards.community_in_tenant", lambda *_a, **_k: True)
        response = await client.open(
            _full_path(route, cpid),
            method=route.method,
            headers=auth_headers(scope=route.scope),
            json={} if route.has_body else None,
        )
        assert response.status_code == route.status_code
        proxy_stub.assert_awaited_once()

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: r.name)
    async def test_scope_assignment_matches_expected_table(self, route: Any) -> None:
        """Every route's declared `scope` matches `_EXPECTED_SCOPES` -- an independent oracle.

        Parametrized over the full, always-58-long `ALL_ROUTES` (not a
        live filter of it) specifically so a scope regression on any one
        route turns THAT parametrization red instead of silently
        vanishing from a `[r for r in ALL_ROUTES if r.scope == ...]`
        filter evaluated at collection time -- the exact failure mode
        this module's own fail-first proof (module docstring) surfaced
        and fixed.
        """
        assert route.scope == _EXPECTED_SCOPES[route.name]

    @pytest.mark.parametrize(
        "route", [r for r in ALL_ROUTES if r.scope == SCOPE_ADMIN], ids=lambda r: r.name
    )
    async def test_admin_routes_reject_read_only_scope(
        self, cpid: str, client: Any, route: Any, auth_headers: Any
    ) -> None:
        """`SCOPE_ADMIN` routes 403 a caller with only `event.calendar:read`.

        Complements (does not replace) `test_scope_assignment_matches_
        expected_table` above -- that test catches drift even if it
        shrinks this filter's parametrize set; this test proves the
        *behavioral* consequence for whatever currently carries
        `SCOPE_ADMIN`.
        """
        response = await client.open(
            _full_path(route, cpid), method=route.method, headers=auth_headers(scope=SCOPE_READ)
        )
        assert response.status_code == 403

    @pytest.mark.parametrize(
        "route", [r for r in ALL_ROUTES if r.scope == SCOPE_WRITE], ids=lambda r: r.name
    )
    async def test_write_routes_accept_wildcard_write_scope(
        self, cpid: str, client: Any, route: Any, auth_headers: Any, proxy_stub: AsyncMock
    ) -> None:
        """`*:write` (a real SCOPE_BUNDLES entry) covers every `event.calendar:write` route."""
        response = await client.open(
            _full_path(route, cpid),
            method=route.method,
            headers=auth_headers(scope="*:write"),
            json={} if route.has_body else None,
        )
        assert response.status_code == route.status_code


class TestPublicBookingRoutes:
    """`cal_booking_slots`/`cal_create_booking` -- Node's `optionalAuth`, no token required."""

    async def test_booking_slots_reachable_with_no_auth_header(
        self, client: Any, proxy_stub: AsyncMock
    ) -> None:
        response = await client.get("/api/v1/calendar/book/acme-standup/slots")
        assert response.status_code == 200
        proxy_stub.assert_awaited_once()

    async def test_create_booking_reachable_with_no_auth_header(
        self, client: Any, proxy_stub: AsyncMock
    ) -> None:
        response = await client.post(
            "/api/v1/calendar/book/acme-standup", json={"name": "Guest", "email": "g@example.com"}
        )
        assert response.status_code == 201

    async def test_booking_slots_with_garbage_bearer_token_still_reaches_proxy_as_anonymous(
        self, client: Any, proxy_stub: AsyncMock
    ) -> None:
        """An unverifiable token on a public route degrades to anonymous, never a 401/500.

        `_build_user_context`'s `payload is None` branch -- a caller who
        happens to send a stale/tampered token to a genuinely public
        booking page must not be worse off than sending no token at all.
        """
        response = await client.get(
            "/api/v1/calendar/book/acme-standup/slots",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert response.status_code == 200
        forwarded_context = proxy_stub.await_args.kwargs["user_context"]
        assert forwarded_context.role == "anonymous"


class TestDownstreamPathTemplating:
    async def test_nested_admin_path_is_built_from_all_three_path_params(
        self, cpid: str, client: Any, auth_headers: Any, proxy_stub: AsyncMock
    ) -> None:
        """`cancelTicket`'s 3 path params all land in the forwarded downstream path."""
        await client.post(
            f"/api/v1/admin/{cpid}/calendar/events/11/tickets/77/cancel",
            headers=auth_headers(scope=SCOPE_ADMIN),
            json={"reason": "duplicate"},
        )
        called_path = proxy_stub.await_args.args[1]
        assert called_path == "/api/v1/calendar/3/events/11/tickets/77/cancel"

    async def test_verify_ticket_has_no_community_id_segment(
        self, client: Any, auth_headers: Any, proxy_stub: AsyncMock
    ) -> None:
        """`verify-ticket` is NOT nested under `:communityId` in Node -- must stay that way."""
        await client.post(
            "/api/v1/admin/calendar/verify-ticket",
            headers=auth_headers(scope=SCOPE_WRITE),
            json={"ticket_code": "abc123"},
        )
        called_path = proxy_stub.await_args.args[1]
        assert called_path == "/api/v1/calendar/verify-ticket"


class TestQueryParamWhitelisting:
    async def test_only_whitelisted_query_params_are_forwarded(
        self, client: Any, auth_headers: Any, proxy_stub: AsyncMock
    ) -> None:
        """`getAvailableSlots` forwards start/end/duration only -- not an arbitrary extra param."""
        await client.get(
            "/api/v1/calendar/availability/7/slots"
            "?start=2026-09-01&end=2026-09-02&duration=30&unexpected=drop-me",
            headers=auth_headers(scope=SCOPE_READ),
        )
        forwarded_query = proxy_stub.await_args.kwargs["query"]
        assert forwarded_query == {"start": "2026-09-01", "end": "2026-09-02", "duration": "30"}


class TestBodyAndClientMetaForwarding:
    async def test_write_route_forwards_posted_json_body(
        self, client: Any, auth_headers: Any, proxy_stub: AsyncMock
    ) -> None:
        await client.put(
            "/api/v1/calendar/availability/settings",
            headers=auth_headers(scope=SCOPE_WRITE),
            json={"timezone": "America/Chicago"},
        )
        forwarded_body = proxy_stub.await_args.kwargs["json_body"]
        assert forwarded_body == {"timezone": "America/Chicago"}

    async def test_check_in_attaches_ip_and_user_agent(
        self, cpid: str, client: Any, auth_headers: Any, proxy_stub: AsyncMock
    ) -> None:
        await client.post(
            f"/api/v1/admin/{cpid}/calendar/events/11/check-in",
            headers={**auth_headers(scope=SCOPE_ADMIN), "User-Agent": "scanner-app/1.0"},
            json={"ticket_id": 77},
        )
        forwarded_body = proxy_stub.await_args.kwargs["json_body"]
        assert forwarded_body["ticket_id"] == 77
        assert forwarded_body["user_agent"] == "scanner-app/1.0"
        assert "ip_address" in forwarded_body

    async def test_get_route_never_reads_a_body(
        self, client: Any, auth_headers: Any, proxy_stub: AsyncMock
    ) -> None:
        await client.get("/api/v1/calendar/booking-pages", headers=auth_headers(scope=SCOPE_READ))
        assert proxy_stub.await_args.kwargs["json_body"] is None


class TestCsvExportBranch:
    async def test_format_csv_sets_content_type_and_disposition(
        self, cpid: str, client: Any, auth_headers: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import blueprints.v1.event as event_module

        stub = AsyncMock(
            return_value=ProxyResult(ok=True, status_code=200, body={"rows": [{"ticket_id": 1}]})
        )
        monkeypatch.setattr(event_module._proxy_client, "request", stub)

        response = await client.get(
            f"/api/v1/admin/{cpid}/calendar/events/11/attendance/export?format=csv",
            headers=auth_headers(scope=SCOPE_ADMIN),
        )
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("text/csv")
        assert "attendance-11.csv" in response.headers["Content-Disposition"]

    async def test_format_json_returns_plain_json_relay(
        self, cpid: str, client: Any, auth_headers: Any, proxy_stub: AsyncMock
    ) -> None:
        response = await client.get(
            f"/api/v1/admin/{cpid}/calendar/events/11/attendance/export",
            headers=auth_headers(scope=SCOPE_ADMIN),
        )
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("application/json")


class TestDownstreamFailureIsMasked:
    """Preserves Node's `errorHandler.js` bug (event.py's own docstring) -- always a plain 500."""

    async def test_downstream_4xx_surfaces_as_generic_500(
        self, client: Any, auth_headers: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import blueprints.v1.event as event_module

        stub = AsyncMock(
            return_value=ProxyResult(ok=False, status_code=404, body={"error": "not found"})
        )
        monkeypatch.setattr(event_module._proxy_client, "request", stub)

        response = await client.get(
            "/api/v1/calendar/booking-pages/999", headers=auth_headers(scope=SCOPE_READ)
        )
        assert response.status_code == 500
        body = await response.get_json()
        assert body["error"]["code"] == "INTERNAL_ERROR"
        assert body["error"]["message"] == "An unexpected error occurred"

    async def test_downstream_timeout_also_surfaces_as_generic_500(
        self, client: Any, auth_headers: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import blueprints.v1.event as event_module

        stub = AsyncMock(return_value=ProxyResult(ok=False, status_code=502, body=None))
        monkeypatch.setattr(event_module._proxy_client, "request", stub)

        response = await client.get(
            "/api/v1/calendar/my-bookings", headers=auth_headers(scope=SCOPE_READ)
        )
        assert response.status_code == 500


class TestRouteTableIntegrity:
    """Guards the table itself, not just what it produces -- catches a copy/paste collision."""

    def test_route_count_matches_node_source(self) -> None:
        """25 (`calendarController.js`) + 33 (`calendarAdmin.js` + `ticketController.js`) = 58."""
        assert len(ALL_ROUTES) == 58

    def test_every_route_name_is_unique(self) -> None:
        names = [r.name for r in ALL_ROUTES]
        assert len(names) == len(set(names))

    def test_every_rule_plus_method_pair_is_unique(self) -> None:
        pairs = [(r.rule, r.method) for r in ALL_ROUTES]
        assert len(pairs) == len(set(pairs))

    def test_expected_scopes_oracle_covers_exactly_the_live_route_names(self) -> None:
        """A new/renamed route with no `_EXPECTED_SCOPES` entry fails loudly, not via KeyError."""
        assert set(_EXPECTED_SCOPES) == {r.name for r in ALL_ROUTES}


class TestFeatureGate:
    """The two-gate Feature guard added to `_make_handler` this PR.

    `libs/event_module/features.py`'s `event.calendar`/`event.ticketing`
    contracts. `_feature_enabled_default_on` (module-level autouse fixture
    above) is overridden per-test here to prove the OFF/denied path, same
    pattern `test_v1_analytics_blueprint.py` uses for its one existing
    guard.
    """

    async def test_calendar_route_blocked_when_flag_off(
        self, client: Any, auth_headers: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A calendar-capability route (below-min-tier OR flag OFF) never reaches the proxy."""
        import blueprints.v1.event as event_module

        monkeypatch.setattr(event_module, "feature_enabled", AsyncMock(return_value=False))
        stub = AsyncMock(return_value=ProxyResult(ok=True, status_code=200, body={}))
        monkeypatch.setattr(event_module._proxy_client, "request", stub)

        response = await client.get(
            "/api/v1/calendar/my-bookings", headers=auth_headers(scope=SCOPE_READ)
        )

        assert response.status_code == 402
        stub.assert_not_awaited()

    async def test_ticketing_route_blocked_when_flag_off(
        self, cpid: str, client: Any, auth_headers: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ticketing-capability route is independently gated -- also 402 when denied."""
        import blueprints.v1.event as event_module

        monkeypatch.setattr(event_module, "feature_enabled", AsyncMock(return_value=False))
        stub = AsyncMock(return_value=ProxyResult(ok=True, status_code=200, body={}))
        monkeypatch.setattr(event_module._proxy_client, "request", stub)

        response = await client.get(
            f"/api/v1/admin/{cpid}/calendar/events/11/ticket-types",
            headers=auth_headers(scope=SCOPE_ADMIN),
        )

        assert response.status_code == 402
        stub.assert_not_awaited()

    async def test_calendar_and_ticketing_routes_gate_on_distinct_flags(
        self, cpid: str, client: Any, auth_headers: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calendar routes check `.event.calendar`; ticket routes check `.event.ticketing`."""
        import blueprints.v1.event as event_module

        seen_flags: list[str] = []

        async def _record(flag_key: str, **_kwargs: Any) -> bool:
            seen_flags.append(flag_key)
            return True

        monkeypatch.setattr(event_module, "feature_enabled", _record)
        monkeypatch.setattr(
            event_module._proxy_client,
            "request",
            AsyncMock(return_value=ProxyResult(ok=True, status_code=200, body={})),
        )

        await client.get("/api/v1/calendar/my-bookings", headers=auth_headers(scope=SCOPE_READ))
        await client.get(
            f"/api/v1/admin/{cpid}/calendar/events/11/ticket-types",
            headers=auth_headers(scope=SCOPE_ADMIN),
        )

        assert seen_flags == ["waddles.event.calendar", "waddles.event.ticketing"]

    async def test_public_booking_route_never_calls_feature_enabled(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`feature_flag=None` on the public booking routes -- no tenant context to gate against."""
        import blueprints.v1.event as event_module

        gate = AsyncMock(return_value=True)
        monkeypatch.setattr(event_module, "feature_enabled", gate)
        monkeypatch.setattr(
            event_module._proxy_client,
            "request",
            AsyncMock(return_value=ProxyResult(ok=True, status_code=200, body={})),
        )

        response = await client.get("/api/v1/calendar/book/acme-standup/slots")

        assert response.status_code == 200
        gate.assert_not_awaited()
