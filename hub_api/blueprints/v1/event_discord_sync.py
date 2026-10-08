"""`POST /api/v1/internal/calendar/events/sync-discord` -- the Discord event-sync trigger endpoint.

**This blueprint is hub-api's half only.** The `calendar_interaction_
module`'s own trigger-wiring (calling this endpoint on event create/
approve/update/cancel) is a SEPARATE follow-up PR -- this file defines
the clean internal contract that follow-up calls against, same split
`event_discord_sync_service.py`'s own module docstring describes.

**This follow-up PR also adds ONE more internal route here**: `POST
.../calendar/guild-pairings/event-sync`, the write side `services.
guild_pairing.list_event_sync_enabled_pairings()` (the push engine's own
fan-out read) was missing -- the public, tenant-JWT-gated `PATCH
/api/v1/communities/<id>/guild-pairings/<pairing_id>` route
(`blueprints/v1/guild_pairing.py`) doesn't thread `event_sync_enabled`
through yet, and `calendar_interaction_module` never holds a user JWT to
call it anyway (hub-api's own `EventCalendarProxyClient` forwards only
`X-API-Key` + `X-User-Context` downstream, see `services/event_calendar_
proxy.py`). Community-wide (every paired guild at once) rather than
per-`pairing_id`, matching the calendar module's own `/<community_id>/
sync/enable` route shape (no `pairing_id` in that path).

Service-to-service only (`X-Service-Key` against `SERVICE_API_KEY`,
`services.community_common.is_valid_service_key()`) -- same mechanism
every other internal blueprint in this port uses (`community_loyalty.py`'s
`loyalty_internal_bp`, `community_connections.py`'s `connections_internal_
bp`). Mounted under `/api/v1/internal/...`, matching that existing,
load-bearing convention (see `community_connections.py`'s own docstring
on why this shape won over a brief's literal path).

**Security note for the PR/security pass (flagged, deliberately deferred):**
`/api/v1/internal/...` is reachable through the public Gateway today --
the shared service key is the only gate on this path, same as every
other internal blueprint in this port (`community_loyalty.py`'s
`loyalty_internal_bp`, `community_connections.py`'s
`connections_internal_bp`). A cluster-wide `CiliumNetworkPolicy` L7 HTTP
rule scoping `/api/v1/internal/*` to in-cluster callers is the real fix
(never relying on the secret alone) -- evaluated for this PR and judged
NOT safely addable in isolation: hub-api's public `/api/v1/*` and
internal `/api/v1/internal/*` surfaces share one port, and Cilium's L7
HTTP enforcement goes default-deny-at-L7 for a port the moment ANY
CiliumNetworkPolicy defines an `http` rule for it (unlike the pure L4
`hub-api-grpc-networkpolicy.yaml` precedent, whose gap is a simple
union-of-allows) -- a narrow internal-path-only rule added without also
re-enumerating every legitimate public route risks breaking the public
API, not just this one. Left for the dedicated security pass with full
route-inventory context, not attempted here -- see `devops-kubernetes.md`
NetworkPolicy.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, cast

from quart import Blueprint, current_app, request

from services import event_discord_sync_service as sync_svc
from services import guild_pairing as pairing_svc
from services.community_common import is_valid_service_key
from services.errors import ApiError, bad_request, not_found
from services.schema import bind_calendar_sync_tables

event_discord_sync_internal_bp = Blueprint(
    "v1_event_discord_sync_internal", __name__, url_prefix="/api/v1/internal"
)

#: `action` values this endpoint accepts -- mirrors `event_discord_sync_service.sync_event`'s own.
_VALID_ACTIONS = ("create", "update", "cancel")


def _dal() -> Any:
    return current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    return {"success": False, "error": {"code": exc.code, "message": exc.message}}, exc.status_code


def _envelope(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    return {"status": "success", "data": data, "meta": {"version": 1}}, 200


@event_discord_sync_internal_bp.route("/calendar/events/sync-discord", methods=["POST"])
async def sync_discord_event() -> tuple[dict[str, Any], int]:
    """`POST /api/v1/internal/calendar/events/sync-discord`.

    Body: `{"event_id": int, "action": "create"|"update"|"cancel"}`.
    Loads the `calendar_events` row fresh (never trusts a caller-supplied
    event payload -- Waddles' own DB row is the only source of truth,
    per `sync_event()`'s own "push the FULL field set" contract) and
    delegates to `event_discord_sync_service.sync_event()`, which is
    itself fail-closed (never raises) -- this route can only return
    404 (event_id unknown) or 400 (bad body) as its OWN errors; every
    Discord-side failure surfaces as a 200 with `sync_status=sync_error`
    in the body, not an HTTP error, so the calendar module's
    fire-and-forget caller never needs special-case error handling for
    "Discord was unreachable" vs "the sync itself failed".
    """
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    body = await request.get_json(force=True, silent=True) or {}
    event_id = body.get("event_id")
    action = body.get("action")
    if not isinstance(event_id, int) or action not in _VALID_ACTIONS:
        return _err(
            bad_request(f"event_id (int) and action (one of {_VALID_ACTIONS}) are required")
        )

    dal = _dal()
    bind_calendar_sync_tables(dal)
    event_row = dal(dal.calendar_events.id == event_id).select().first()
    if event_row is None:
        return _err(not_found(f"calendar event {event_id} not found"))

    result = await sync_svc.sync_event(dal, event_row, action=cast(Any, action))
    return _envelope(asdict(result))


@event_discord_sync_internal_bp.route("/calendar/guild-pairings/event-sync", methods=["POST"])
async def set_event_sync_enabled() -> tuple[dict[str, Any], int]:
    """`POST /api/v1/internal/calendar/guild-pairings/event-sync`.

    Body: `{"community_id": int, "enabled": bool}`. Toggles `event_sync_
    enabled` on every `guild_tenant_pairings` row under `community_id` --
    the push engine (`event_discord_sync_service.py`'s `DiscordScheduled
    EventTarget`) fans out to exactly the set `list_event_sync_enabled_
    pairings()` returns, so this is the one write path that actually
    changes what that fan-out does. Same service-key gate as the route
    above; 404 if the community has no guild pairings at all (nothing to
    toggle), never silently a no-op success.
    """
    if not is_valid_service_key(request):
        return {"success": False, "error": "Invalid service key"}, 401

    body = await request.get_json(force=True, silent=True) or {}
    community_id = body.get("community_id")
    enabled = body.get("enabled")
    if not isinstance(community_id, int) or not isinstance(enabled, bool):
        return _err(bad_request("community_id (int) and enabled (bool) are required"))

    dal = _dal()
    pairings = pairing_svc.list_pairings(dal, community_id)
    if not pairings:
        return _err(not_found(f"no guild pairings for community {community_id}"))

    for pairing in pairings:
        pairing_svc.update_pairing(dal, community_id, pairing.id, event_sync_enabled=enabled)

    return _envelope(
        {
            "community_id": community_id,
            "event_sync_enabled": enabled,
            "pairings_updated": len(pairings),
        }
    )


BLUEPRINTS = [event_discord_sync_internal_bp]
