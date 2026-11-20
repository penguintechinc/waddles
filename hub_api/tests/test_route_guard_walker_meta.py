"""Meta-tests: prove the route-guard walker can fail.

A walker that cannot fail is not a gate. These plant routes the old regex walker accepted (a name
in a comment, a docstring, a string, a dead branch) and a `@mark_guarded` stamp with no deny test,
and assert the AST walker rejects each of them.
"""

from __future__ import annotations

# ruff: noqa: E501
from quart import Quart

from services.community_authz import authorize_community, require_community_scope
from services.route_guards import GUARD_ADMIN, mark_guarded
from tests.route_guard_walker import has_enforcement_call, inline_guard_gaps, iter_routes


async def _planted_comment_only(community_id: int) -> dict[str, int]:
    # _require_admin(community_id) and authorize_community(...) -- only a comment
    return {"id": community_id}


async def _planted_string_only(community_id: int) -> str:
    """Docstring claims authorize_community(community_id) is called."""
    return "require_community_scope(community_id)"


async def _planted_dead_branch(community_id: int) -> dict[str, int]:
    if False:
        await authorize_community(None, None, None, community_id=community_id, admin=True)
    return {"id": community_id}


async def _planted_after_return(community_id: int) -> dict[str, int]:
    return {"id": community_id}
    await authorize_community(None, None, None, community_id=community_id, admin=True)  # noqa: B018


async def _helper_that_enforces(community_id: int) -> None:
    await require_community_scope(None, None, community_id=community_id, user_id=1, any_of=())


async def _planted_real_via_helper(community_id: int) -> dict[str, int]:
    await _helper_that_enforces(community_id)
    return {"id": community_id}


async def _planted_real_direct(community_id: int) -> dict[str, int]:
    await authorize_community(None, None, None, community_id=community_id, admin=True)
    return {"id": community_id}


@mark_guarded(GUARD_ADMIN)
async def _planted_stamped_real(community_id: int) -> dict[str, int]:
    await authorize_community(None, None, None, community_id=community_id, admin=True)
    return {"id": community_id}


@mark_guarded(GUARD_ADMIN)
async def _planted_stamped_comment_only(community_id: int) -> dict[str, int]:
    # _require_admin(community_id)
    return {"id": community_id}


def test_enforcement_must_be_a_real_call() -> None:
    """Comments, docstrings, strings, dead branches and unreachable code all FAIL the check."""
    for view in (
        _planted_comment_only,
        _planted_string_only,
        _planted_dead_branch,
        _planted_after_return,
    ):
        assert not has_enforcement_call(view), view.__name__
    for view in (_planted_real_direct, _planted_real_via_helper, _planted_stamped_real):
        assert has_enforcement_call(view), view.__name__


def test_stamp_without_deny_test_fails() -> None:
    """A `@mark_guarded` route needs an enforcement call AND a keyed deny test."""
    routes = [
        ("GET", "/planted/real", _planted_stamped_real),
        ("GET", "/planted/comment", _planted_stamped_comment_only),
        ("GET", "/planted/unstamped", _planted_comment_only),  # unstamped: other check's job
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
    app.add_url_rule("/planted/stamped-ok/<int:community_id>", view_func=_planted_stamped_real)
    app.add_url_rule(
        "/planted/stamped-comment/<int:community_id>", view_func=_planted_stamped_comment_only
    )
    app.add_url_rule("/planted/plain/<int:community_id>", view_func=_planted_comment_only)
    routes = list(iter_routes(app))
    assert len(routes) >= 3, routes  # a denominator: zero routes examined proves nothing
    gaps = inline_guard_gaps(routes, {})
    assert len(gaps) == 3, gaps  # stamped-ok: no deny test; stamped-comment: both problems
    assert not any("/planted/plain/" in g for g in gaps), gaps
