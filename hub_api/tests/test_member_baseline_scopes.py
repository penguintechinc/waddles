"""Regression: member-facing Quartermaster routes were 403 for every non-admin session.

`GET /api/v1/admin/<id>/inventory/{available,my-items}` require `community.inventory:use`, an
action the `*:read` wildcard cannot cover and no bundle minted -- so the member webui pages got
403. The scope is now a member baseline on every session; admin-only inventory scopes
(`:read`/`:write` beyond `*:read`) stay un-minted for members.
"""

from __future__ import annotations

from typing import Any

from flask_core.auth import MEMBER_BASELINE_SCOPES, SCOPE_BUNDLES

from services import auth_service
from tests.test_v1_auth_blueprint import _seed_user


class TestMemberBaselineScopes:
    def test_baseline_is_exactly_the_member_use_scope(self) -> None:
        assert MEMBER_BASELINE_SCOPES == ["community.inventory:use"]

    def test_baseline_never_carries_write_or_admin_actions(self) -> None:
        for scope in MEMBER_BASELINE_SCOPES:
            assert scope.rsplit(":", 1)[1] not in {"write", "admin", "delete"}

    def test_community_viewer_bundle_includes_baseline(self) -> None:
        assert set(MEMBER_BASELINE_SCOPES) <= set(SCOPE_BUNDLES["community"]["viewer"])

    async def test_plain_member_session_can_use_but_not_administer_inventory(
        self, auth_db: Any
    ) -> None:
        uid = _seed_user(auth_db, email="member@example.com")
        user = auth_service.SessionUser(id=uid, email="member@example.com", username="member")
        _, scopes = await auth_service._scopes_for(auth_db, auth_db.dal, user=user, tenant_id=None)
        assert "community.inventory:use" in scopes
        assert "community.inventory:write" not in scopes
        assert "users:admin" not in scopes
