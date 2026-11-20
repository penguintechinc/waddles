"""CI guard: every community-scoped route whose scopes are all `:read` carries a guard.

Every session holds the viewer bundle's universal `*:read`, so a route protected only by
`require_scope("X:read")` is open to any signed-in user in the tenant. This walks the real app's
url_map and fails for any such route that has no admin/member/owner guard stamped by
`services.route_guards` (`__authz_guard__`) and is not in `JUSTIFIED_UNGUARDED` with a reason.
"""

from __future__ import annotations

# ruff: noqa: E501
import inspect
import re
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


#: Inline enforcement markers recognised in a handler's (or its wrappers') source. A route
#: with NO `require_scope` is only accepted when its chain visibly calls one of these.
_INLINE_ENFORCEMENT = re.compile(
    r"require_member_scope|require_community_scope|authorize_community|_require_member|_gated_|"
    r"require_community_admin|require_community_member|_require_community_admin|"
    r"resolve_editor_scopes|_admin_actor|_member_actor|is_community_admin|community_access|"
    r"_require_admin|_require_manage|member_scopes_sync|resolve_community_membership"
)

#: Path prefix -> reason, for scope-less community routes the walker cannot see enforced.
#: PR #1472 is fixing the truly unscoped ones; entries go stale (and must be deleted) when a
#: route gains a guard or scope. UNVERIFIED means "no enforcement the walker recognises".
NOSCOPE_ALLOWLIST: dict[str, str] = {
    "/api/v1/public/communities/": "public community profile/live status, by design",
    "/api/v1/internal/": "service-key authenticated internal route (not user reachable)",
    "/api/v1/communities/<cuuid:community_id>/join": "join flow: caller acts on their own join",
    "/api/v1/<cuuid:community_id>/join-requests": "UNVERIFIED: own-request flow; PR #1472 triage",
    "/api/v1/<cuuid:community_id>/github-sync/": "UNVERIFIED: PR #1472 / follow-up",
    "/api/v1/admin/<cuuid:community_id>/join-requests": "UNVERIFIED: PR #1472 / follow-up",
    "/api/v1/admin/<cuuid:community_id>/banner": "UNVERIFIED: PR #1472 / follow-up",
    "/api/v1/admin/<cuuid:community_id>/logo": "UNVERIFIED: PR #1472 / follow-up",
    "/api/v1/admin/<cuuid:community_id>/profile": "UNVERIFIED: PR #1472 / follow-up",
    "/api/v1/admin/<cuuid:community_id>/interaction/roles": "UNVERIFIED: PR #1472 / follow-up",
    "/api/v1/admin/<cuuid:community_id>/reputation/": "UNVERIFIED: PR #1472 / follow-up",
    "/api/v1/admin/<cuuid:community_id>/streams": "UNVERIFIED: PR #1472 / follow-up",
    "/api/v1/admin/<cuuid:community_id>/support/": "UNVERIFIED: PR #1472 / follow-up",
    "/api/v1/admin/<cuuid:community_id>/workflows": "UNVERIFIED: PR #1472 / follow-up",
    "/api/v1/apps/community/<cuuid:community_id>/bundles/": "UNVERIFIED: PR #1472 / follow-up",
}


def _handler_source(view: Any) -> str:
    out = ""
    for fn in _chain(view):
        try:
            out += inspect.getsource(fn)
        except (OSError, TypeError):
            continue
    return out


def test_scopeless_community_routes_are_enforced_or_justified() -> None:
    application = create_app(_test_config())
    examined = 0
    flagged: set[str] = set()
    for rule in application.url_map.iter_rules():
        if not _COMMUNITY_ARGS & set(rule.arguments):
            continue
        view = application.view_functions[rule.endpoint]
        if _required_scopes(view):
            continue
        examined += 1
        if _guard_kind(view) or _INLINE_ENFORCEMENT.search(_handler_source(view)):
            continue
        flagged.add(rule.rule)
    assert examined >= 100, f"walker examined only {examined} scope-less routes"
    unjustified = sorted(r for r in flagged if not r.startswith(tuple(NOSCOPE_ALLOWLIST)))
    assert not unjustified, (
        "community routes with NO require_scope and no recognised inline admin/member check; "
        f"add a guard or a justified NOSCOPE_ALLOWLIST entry: {unjustified}"
    )
    stale = [p for p in NOSCOPE_ALLOWLIST if not any(r.startswith(p) for r in flagged)]
    assert not stale, f"NOSCOPE_ALLOWLIST entries no longer match any flagged route: {stale}"
