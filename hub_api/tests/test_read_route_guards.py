"""CI guard: every community-scoped route whose scopes are all `:read` carries a guard.

Every session holds the viewer bundle's universal `*:read`, so a route protected only by
`require_scope("X:read")` is open to any signed-in user in the tenant. This walks the real app's
url_map and fails for any such route that has no admin/member/owner guard stamped by
`services.route_guards` (`__authz_guard__`) and is not in `JUSTIFIED_UNGUARDED` with a reason.
"""

from __future__ import annotations

# ruff: noqa: E501
from pathlib import Path
from typing import Any

from app import create_app
from tests.route_guard_walker import (
    chain as _chain,
)
from tests.route_guard_walker import (
    guard_kind as _guard_kind,
)
from tests.route_guard_walker import (
    has_enforcement_call,
    inline_guard_gaps,
    is_inline_stamped,
    iter_routes,
)
from tests.test_app_factory import _test_config
from tests.test_read_route_inline_guards import INLINE_GUARD_DENY_TESTS

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
}
JUSTIFIED_UNGUARDED: dict[tuple[str, str], str] = {
    ("GET", path): reason for path, reason in _JUSTIFIED_GET_PATHS.items()
}


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
    # A `mark_guarded` stamp is only a label: it must also reach a real enforcement call and
    # have a keyed deny test (`INLINE_GUARD_DENY_TESTS`).
    inline_problems = inline_guard_gaps(iter_routes(application), INLINE_GUARD_DENY_TESTS)
    assert not inline_problems, f"mark_guarded routes without proof: {inline_problems}"
    live = {(m, r) for m, r, v in iter_routes(application) if is_inline_stamped(v)}
    stale_inline = [k for k in INLINE_GUARD_DENY_TESTS if k not in live]
    assert not stale_inline, f"INLINE_GUARD_DENY_TESTS names non-stamped routes: {stale_inline}"
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


#: Path prefix -> reason, for scope-less community routes whose enforcement is not an
#: authorization-primitive CALL the walker can follow (public/internal/own-data/DB-gated flows).
#: Entries go stale (and must be deleted) when a route gains a guard or scope.
NOSCOPE_ALLOWLIST: dict[str, str] = {
    "/api/v1/public/communities/": "public community profile/live status, by design",
    "/api/v1/internal/": "service-key authenticated internal route (not user reachable)",
    "/api/v1/communities/<cuuid:community_id>/join": "join flow: caller acts on their own join",
    "/api/v1/<cuuid:community_id>/join-requests": (
        "own-request flow: tenant fence services/join_request_service.py:115, own data only"
    ),
    "/api/v1/admin/<cuuid:community_id>/inventory/": (
        "member self-service: DB membership gate services/community_inventory_authz.py:115 "
        "(`require_inventory_member`); deny tests tests/test_community_inventory.py::"
        "TestMemberRoutesAuthorizeThroughTheCommunity"
    ),
}

#: Where the admin routes that used to be "UNVERIFIED" are enforced. The walker already proves a
#: real primitive call exists; `test_enforcement_evidence_is_current` pins each citation to its
#: line so a moved/removed check cannot leave a stale claim behind.
ENFORCEMENT_EVIDENCE: dict[str, tuple[str, int, str]] = {
    "admin join-requests list": (
        "services/join_request_service.py",
        204,
        "require_community_scope",
    ),
    "admin join-requests approve": (
        "services/join_request_service.py",
        256,
        "require_community_scope",
    ),
    "admin join-requests reject": (
        "services/join_request_service.py",
        320,
        "require_community_scope",
    ),
    "admin profile update": (
        "services/community_profile_service.py",
        202,
        "require_community_scope",
    ),
    "admin logo upload": ("services/community_profile_service.py", 269, "require_community_scope"),
    "admin logo delete": ("services/community_profile_service.py", 293, "require_community_scope"),
    "admin banner upload": (
        "services/community_profile_service.py",
        317,
        "require_community_scope",
    ),
    "admin banner delete": (
        "services/community_profile_service.py",
        343,
        "require_community_scope",
    ),
    "admin interaction/roles": (
        "blueprints/v1/community_interaction.py",
        235,
        "resolve_editor_scopes",
    ),
    "admin reputation/": ("services/admin_service.py", 356, "require_community_admin"),
    "admin streams": ("blueprints/v1/streaming.py", 129, "scope=SCOPE_STREAM_CONFIG"),
    "bundles gateway membership": (
        "blueprints/v1/bundle_admin_gateway.py",
        134,
        "authorize_community",
    ),
    "bundles descriptor scope": ("blueprints/v1/bundle_admin_gateway.py", 249, "caller_holds"),
    "bundles config read": ("services/bundle_admin_gateway_service.py", 219, "_require_scope"),
    "bundles config history": ("services/bundle_admin_gateway_service.py", 247, "_require_scope"),
    "bundles config save": ("services/bundle_admin_gateway_service.py", 278, "_require_scope"),
    "bundles actions/data views": (
        "services/bundle_admin_gateway_service.py",
        398,
        "_require_scope",
    ),
}


def test_enforcement_evidence_is_current() -> None:
    root = Path(__file__).resolve().parent.parent
    for name, (rel, line, token) in ENFORCEMENT_EVIDENCE.items():
        text = (root / rel).read_text().splitlines()[line - 1]
        assert token in text, f"{name}: {rel}:{line} no longer contains `{token}`: {text!r}"


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
        if has_enforcement_call(view):
            continue
        flagged.add(rule.rule)
    assert examined >= 100, f"walker examined only {examined} scope-less routes"
    unjustified = sorted(r for r in flagged if not r.startswith(tuple(NOSCOPE_ALLOWLIST)))
    assert not unjustified, (
        "community routes with NO require_scope and no enforcement call in its call chain; "
        f"add a guard or a justified NOSCOPE_ALLOWLIST entry: {unjustified}"
    )
    stale = [p for p in NOSCOPE_ALLOWLIST if not any(r.startswith(p) for r in flagged)]
    assert not stale, f"NOSCOPE_ALLOWLIST entries no longer match any flagged route: {stale}"
