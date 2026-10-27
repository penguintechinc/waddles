"""Regression tests for the live calendar authz-bypass fix (2026-10).

`/api/v1/calendar/*` Gateway routes used to point directly at this
service (`waddlebot-interactive-productivity`), bypassing hub-api's
scope-gated `event_calendar_bp` + `EventCalendarProxyClient`
(hub_api/services/event_calendar_proxy.py) -- the only legitimate
JWT-validator and setter of the `X-User-Context` header this module
trusts. `get_user_context()` (app.py) `json.loads()`s `X-User-Context`
with no verification of its own, so any direct caller could set an
arbitrary role (e.g. 'admin') and be trusted outright, while legitimate
calls arriving with no identity at all silently fell back to
role='member'.

The fix adds `check_service_api_key()`, a `before_request` hook
registered on every Blueprint in this module (`calendar_bp`, `context_bp`,
`ticket_bp`, `tournament_bp`), that validates `X-API-Key` against
`Config.SERVICE_API_KEY` via `flask_core.auth.verify_service_key`
(constant-time compare, fails closed when unconfigured) BEFORE any route
body -- including `get_user_context()` -- runs. These tests prove:

1. A request with no/invalid `X-API-Key` is rejected with 401.
2. `X-User-Context` is NOT trusted without a valid `X-API-Key` --
   impersonation (e.g. claiming `role: admin`) is blocked at the gate,
   never reaching the handler.

Fail-first proof: with `check_service_api_key` temporarily unregistered
from `calendar_bp`, `test_impersonation_via_x_user_context_is_blocked`
fails (200, with the spoofed admin role accepted) -- this is exactly the
live bypass being fixed.
"""

from __future__ import annotations

from typing import Any

import pytest
from app import app, calendar_bp

from config import Config

_REAL_SERVICE_API_KEY = "test-calendar-service-key"

# A GET route registered on calendar_bp (`/api/v1/calendar/<int>/events`)
# that reaches `check_service_api_key()` via the blueprint's
# `before_request` before touching any uninitialized service global --
# safe to hit under `test_client()`, which never runs `before_serving`
# (so `calendar_service` etc. stay `None`).
_EVENTS_ROUTE = "/api/v1/calendar/1/events"

_IMPERSONATION_CONTEXT = (
    '{"user_id": "attacker", "username": "attacker", "platform": "api", '
    '"platform_user_id": "attacker", "role": "admin"}'
)


@pytest.fixture
def client():
    return app.test_client()


@pytest.fixture
def configured_service_key(monkeypatch: pytest.MonkeyPatch):
    """Set a known SERVICE_API_KEY so the "valid key" path is deterministic."""
    monkeypatch.setattr(Config, "SERVICE_API_KEY", _REAL_SERVICE_API_KEY)


class TestServiceApiKeyGate:
    """`check_service_api_key()` registered as a Blueprint `before_request`."""

    def test_registered_on_every_blueprint(self) -> None:
        """Structural guard against the gate being wired to only one Blueprint
        (e.g. `calendar_bp`) while `context_bp`/`ticket_bp`/`tournament_bp`
        stay open -- all four share this module's trust boundary."""
        from app import check_service_api_key, context_bp, ticket_bp, tournament_bp

        for blueprint in (calendar_bp, context_bp, ticket_bp, tournament_bp):
            registered = [
                fn
                for funcs in blueprint.before_request_funcs.values()
                for fn in funcs
            ]
            assert check_service_api_key in registered, (
                f"check_service_api_key not registered on {blueprint.name!r}"
            )

    @pytest.mark.asyncio
    async def test_missing_api_key_is_401(
        self, client: Any, configured_service_key: None
    ) -> None:
        response = await client.get(_EVENTS_ROUTE)
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_api_key_is_401(
        self, client: Any, configured_service_key: None
    ) -> None:
        response = await client.get(
            _EVENTS_ROUTE, headers={"X-API-Key": "wrong-key"}
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_unconfigured_service_key_fails_closed(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No SERVICE_API_KEY configured anywhere -- every request rejected,
        never silently allowed (critical-rules.md Verification Integrity /
        `verify_service_key`'s documented fail-closed contract)."""
        monkeypatch.setattr(Config, "SERVICE_API_KEY", "")
        response = await client.get(
            _EVENTS_ROUTE, headers={"X-API-Key": "anything"}
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_impersonation_via_x_user_context_is_blocked(
        self, client: Any, configured_service_key: None
    ) -> None:
        """The core bypass: a caller with no valid `X-API-Key` cannot use
        `X-User-Context` to claim an elevated role (here, 'admin'). Must be
        rejected at the gate -- `get_user_context()` must never be trusted
        with attacker-supplied identity."""
        response = await client.get(
            _EVENTS_ROUTE,
            headers={"X-User-Context": _IMPERSONATION_CONTEXT},
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_impersonation_via_x_user_context_blocked_with_wrong_key(
        self, client: Any, configured_service_key: None
    ) -> None:
        """Same impersonation attempt, this time pairing the spoofed
        `X-User-Context` with an invalid `X-API-Key` -- still rejected;
        a plausible-looking (but wrong) key must not be enough either."""
        response = await client.get(
            _EVENTS_ROUTE,
            headers={
                "X-API-Key": "wrong-key",
                "X-User-Context": _IMPERSONATION_CONTEXT,
            },
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_api_key_passes_the_gate(
        self, client: Any, configured_service_key: None
    ) -> None:
        """A valid X-API-Key clears `check_service_api_key()` -- the request
        proceeds past the gate (any later failure is from uninitialized
        services under `test_client()`, never a 401 from this check)."""
        response = await client.get(
            _EVENTS_ROUTE, headers={"X-API-Key": _REAL_SERVICE_API_KEY}
        )

        assert response.status_code != 401
