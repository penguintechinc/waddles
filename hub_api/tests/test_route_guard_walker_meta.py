"""Meta-tests: prove the route-guard walker can fail.

A walker that cannot fail is not a gate. These plant routes that an over-eager walker would accept
-- a name in a comment/docstring/string, a dead branch, a guard under a one-armed `if`, a guard
whose result is ignored, a guard wrapped in `try/except: pass`, a local no-op that merely shares a
guard's NAME, and a `@mark_guarded` stamp with no deny test -- and assert each is rejected, while
the real thing (direct call, helper, both-armed `if`) is accepted.
"""

from __future__ import annotations

# ruff: noqa: E501
from typing import Any

from quart import Quart

from services import community_authz as real_authz
from services.route_guards import GUARD_ADMIN, mark_guarded
from tests.route_guard_walker import has_enforcement_call, inline_guard_gaps, iter_routes


async def authorize_community(*_a: object, **_k: object) -> None:
    """Local no-op that shares the real primitive's NAME (identity, not name, must decide)."""
    return None


def community_admin_guard(fn: Any) -> Any:
    """Local no-op decorator that shares the real guard's NAME."""
    return fn


async def _real(community_id: int) -> None:
    await real_authz.authorize_community(None, None, None, community_id=community_id, admin=True)


# ---- violations the walker must REJECT -------------------------------------------------------
async def _comment_only(community_id: int) -> dict[str, int]:
    # _require_admin(community_id) and authorize_community(...) -- only a comment
    return {"id": community_id}


async def _string_only(community_id: int) -> str:
    """Docstring claims authorize_community(community_id) is called."""
    return "require_community_scope(community_id)"


async def _dead_branch(community_id: int) -> dict[str, int]:
    if False:
        await _real(community_id)
    return {"id": community_id}


async def _after_return(community_id: int) -> dict[str, int]:
    return {"id": community_id}
    await _real(community_id)


async def _conditional(community_id: int, flag: bool) -> dict[str, int]:
    if flag:
        await _real(community_id)
    return {"id": community_id}


async def _one_armed_direct(community_id: int, flag: bool) -> dict[str, int]:
    if flag:
        await real_authz.authorize_community(
            None, None, None, community_id=community_id, admin=True
        )
    return {"id": community_id}


async def _ignored_result(community_id: int) -> dict[str, int]:
    standing = real_authz.resolve_caller_standing_sync(None, None, community_id=community_id)  # noqa: F841
    return {"id": community_id}


async def _bare_value_primitive(community_id: int) -> dict[str, int]:
    real_authz.resolve_caller_standing_sync(None, None, community_id=community_id)
    return {"id": community_id}


async def _real_verdict(community_id: int) -> bool:
    await _real(community_id)
    return True


async def _ignored_helper_verdict(community_id: int) -> dict[str, int]:
    verdict = await _real_verdict(community_id)  # noqa: F841
    return {"id": community_id}


async def _swallowed(community_id: int) -> dict[str, int]:
    try:
        await real_authz.authorize_community(
            None, None, None, community_id=community_id, admin=True
        )
    except Exception:  # noqa: BLE001, S110
        pass
    return {"id": community_id}


async def _bare_except(community_id: int) -> dict[str, int]:
    try:
        await _real(community_id)
    except:  # noqa: E722, S110
        pass
    return {"id": community_id}


async def _local_noop_same_name(community_id: int) -> dict[str, int]:
    await authorize_community(None, None, None, community_id=community_id, admin=True)
    return {"id": community_id}


@community_admin_guard
async def _local_noop_decorator(community_id: int) -> dict[str, int]:
    return {"id": community_id}


@mark_guarded(GUARD_ADMIN)
async def _stamped_comment_only(community_id: int) -> dict[str, int]:
    # _require_admin(community_id)
    return {"id": community_id}


# ---- real enforcement the walker must ACCEPT -------------------------------------------------
async def _direct(community_id: int) -> dict[str, int]:
    await real_authz.authorize_community(None, None, None, community_id=community_id, admin=True)
    return {"id": community_id}


async def _via_helper(community_id: int) -> dict[str, int]:
    await _real(community_id)
    return {"id": community_id}


async def _both_arms(community_id: int, flag: bool) -> dict[str, int]:
    if flag:
        await _real(community_id)
    else:
        await real_authz.authorize_community(
            None, None, None, community_id=community_id, admin=False
        )
    return {"id": community_id}


async def _value_used(community_id: int) -> dict[str, int] | tuple[str, int]:
    standing = real_authz.resolve_caller_standing_sync(None, None, community_id=community_id)
    if not standing.is_admin:
        return "denied", 403
    return {"id": community_id}


@mark_guarded(GUARD_ADMIN)
async def _stamped_real(community_id: int) -> dict[str, int]:
    await _real(community_id)
    return {"id": community_id}


def test_violations_are_rejected() -> None:
    """Every planted violation FAILS the check (denominator asserted so it cannot be vacuous)."""
    planted = [
        _comment_only,
        _string_only,
        _dead_branch,
        _after_return,
        _conditional,
        _one_armed_direct,
        _ignored_result,
        _bare_value_primitive,
        _ignored_helper_verdict,
        _swallowed,
        _bare_except,
        _local_noop_same_name,
        _local_noop_decorator,
        _stamped_comment_only,
    ]
    assert len(planted) == 14
    accepted = [view.__name__ for view in planted if has_enforcement_call(view)]
    assert accepted == [], f"walker accepted non-enforcing routes: {accepted}"


def test_real_enforcement_is_accepted() -> None:
    real = [_direct, _via_helper, _both_arms, _value_used, _stamped_real]
    rejected = [view.__name__ for view in real if not has_enforcement_call(view)]
    assert rejected == [], f"walker rejected real enforcement: {rejected}"


def test_stamp_without_deny_test_fails() -> None:
    """A `@mark_guarded` route needs an enforcement call AND a keyed deny test."""
    routes = [
        ("GET", "/planted/real", _stamped_real),
        ("GET", "/planted/comment", _stamped_comment_only),
        ("GET", "/planted/unstamped", _comment_only),  # unstamped: other check's job
    ]
    gaps = inline_guard_gaps(routes, {})
    assert any("/planted/real" in g and "no keyed deny test" in g for g in gaps), gaps
    assert any("/planted/comment" in g and "no enforcement call" in g for g in gaps), gaps
    assert not any("/planted/unstamped" in g for g in gaps), gaps
    # With deny tests registered, only the missing enforcement call remains.
    keyed = {
        ("GET", "/planted/real"): test_stamp_without_deny_test_fails,
        ("GET", "/planted/comment"): test_stamp_without_deny_test_fails,
    }
    gaps = inline_guard_gaps(routes, keyed)
    assert gaps == ["GET /planted/comment: stamped mark_guarded but no enforcement call"], gaps


def test_planted_routes_registered_in_an_app_are_caught() -> None:
    """End to end through `iter_routes`: planted violations in a real Quart url_map FAIL."""
    app = Quart(__name__)
    app.add_url_rule("/planted/stamped-ok/<int:community_id>", view_func=_stamped_real)
    app.add_url_rule("/planted/stamped-comment/<int:community_id>", view_func=_stamped_comment_only)
    app.add_url_rule("/planted/plain/<int:community_id>", view_func=_comment_only)
    routes = list(iter_routes(app))
    assert len(routes) >= 3, routes  # a denominator: zero routes examined proves nothing
    gaps = inline_guard_gaps(routes, {})
    assert len(gaps) == 3, gaps  # stamped-ok: no deny test; stamped-comment: both problems
    assert not any("/planted/plain/" in g for g in gaps), gaps
