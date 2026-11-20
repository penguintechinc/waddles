"""CI guard: every community-scoped route whose scopes are all `:read` carries a guard.

Every session holds the viewer bundle's universal `*:read`, so a route protected only by
`require_scope("X:read")` is open to any signed-in user in the tenant. This walks the real app's
url_map and fails for any such route that has no admin/member/owner guard stamped by
`services.route_guards` (`__authz_guard__`) and is not in `JUSTIFIED_UNGUARDED` with a reason.
"""

from __future__ import annotations

# ruff: noqa: E501
from typing import Any

from app import create_app
from services.route_guards import GUARD_ATTR
from tests.test_app_factory import _test_config

_COMMUNITY_ARGS = {"community_id", "community_public_id"}

#: (METHOD, rule) -> why no decorator guard is needed. Each must stay a live route.
_JUSTIFIED_GET_PATHS: dict[str, str] = {
    # Guarded inline by the handler itself (membership / ownership), pre-dating the decorators.
    "/api/v1/community/<cuuid:community_id>/activity/my-stats": "own data (auth_required)",
    "/api/v1/community/<cuuid:community_id>/reputation/me": "own data (caller's own user id)",
    "/api/v1/community/<cuuid:community_id>/interact/voice/rooms": "calls._gated_member inline",
    "/api/v1/communities/<cuuid:community_id>/town-hall": "calls._gated_member inline",
    "/api/v1/communities/<cuuid:community_id>/calls/rooms/<room_id>/hands/mine": "calls._member_actor",
    "/api/v1/communities/<cuuid:community_id>/calls/rooms/<room_id>/participants": "calls._member_actor",
    "/api/v1/marketplace/communities/<cuuid:community_id>/subscriptions": "_require_community_admin",
    # Out of scope for this change: fixed separately by PR #1469.
    "/api/v1/admin/<cuuid:community_id>/inventory/items": "community_inventory: PR #1469",
    "/api/v1/admin/<cuuid:community_id>/inventory/summary": "community_inventory: PR #1469",
    "/api/v1/admin/<cuuid:community_id>/inventory/log": "community_inventory: PR #1469",
    "/api/v1/admin/<cuuid:community_id>/inventory/checkouts": "community_inventory: PR #1469",
    # FOLLOW-UP (not among the 53 audited routes): event.py ProxyRoutes, downstream-owned check.
    "/api/v1/admin/<cuuid:community_id>/calendar/events/<event_id>/rsvp-counts": "FOLLOW-UP event.py ProxyRoute",
    "/api/v1/admin/<cuuid:community_id>/calendar/events/<event_id>/my-permissions": "FOLLOW-UP event.py ProxyRoute",
}
JUSTIFIED_UNGUARDED: dict[tuple[str, str], str] = {
    ("GET", path): reason for path, reason in _JUSTIFIED_GET_PATHS.items()
}


def _chain(view: Any) -> list[Any]:
    """The view plus every function it wraps (`functools.wraps` `__wrapped__` chain)."""
    out = []
    while view is not None:
        out.append(view)
        view = getattr(view, "__wrapped__", None)
    return out


def _required_scopes(view: Any) -> list[str]:
    """Scopes passed to every `require_scope(...)` in the chain (read from its closure)."""
    scopes: list[str] = []
    for fn in _chain(view):
        code = getattr(fn, "__code__", None)
        cells = getattr(fn, "__closure__", None)
        if code is None or not cells or "required_scopes" not in code.co_freevars:
            continue
        value = cells[code.co_freevars.index("required_scopes")].cell_contents
        scopes.extend(value)
    return scopes


def _guard_kind(view: Any) -> str | None:
    for fn in _chain(view):
        kind = getattr(fn, GUARD_ATTR, None)
        if kind:
            return str(kind)
    return None


def _read_only(scopes: list[str]) -> bool:
    return bool(scopes) and all(s.endswith(":read") for s in scopes)


def test_every_community_read_route_has_a_guard() -> None:
    application = create_app(_test_config())
    examined = 0
    candidates: set[tuple[str, str]] = set()
    unguarded: list[tuple[str, str, list[str]]] = []
    for rule in application.url_map.iter_rules():
        if not _COMMUNITY_ARGS & set(rule.arguments):
            continue
        view = application.view_functions[rule.endpoint]
        scopes = _required_scopes(view)
        if not _read_only(scopes):
            continue
        for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
            examined += 1
            key = (method, rule.rule)
            candidates.add(key)
            if _guard_kind(view) is None and key not in JUSTIFIED_UNGUARDED:
                unguarded.append((method, rule.rule, scopes))
    assert examined >= 40, f"walker examined only {examined} read routes; it is mis-pointed"
    assert not unguarded, (
        "community routes with only `:read` scopes are open to any signed-in tenant user; add "
        "community_admin_guard / community_member_guard (services.route_guards) or justify in "
        f"JUSTIFIED_UNGUARDED: {unguarded}"
    )
    stale = [k for k in JUSTIFIED_UNGUARDED if k not in candidates]
    assert not stale, f"JUSTIFIED_UNGUARDED names routes that are not read-only/live: {stale}"


def test_platform_analytics_routes_are_guarded() -> None:
    application = create_app(_test_config())
    seen = 0
    for rule in application.url_map.iter_rules():
        if rule.rule.startswith("/api/v1/analytics/platform/"):
            seen += 1
            assert _guard_kind(application.view_functions[rule.endpoint]) == "platform", rule.rule
    assert seen >= 5
