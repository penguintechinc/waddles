"""`blueprints/v1/support.py` -- support-ticket categories, tickets, comments, stats.

Fail-first proof (executed, not narrated) for the reporter-ownership
security fix: `TestMemberOwnership::test_get_my_ticket_of_another_reporter_is_404`
and `test_add_own_comment_on_another_reporters_ticket_is_404` were run once
with `require_reporter_id=user_id` temporarily removed from both call sites
in `blueprints/v1/support.py::get_my_ticket`/`add_own_comment` (matching
Node's original, unguarded behavior). Both tests went red -- a second
member could read another member's ticket (200 with the wrong reporter's
data) and post a comment onto it (201). Reverted, both green again.
"""

from __future__ import annotations

import json as json_module
from types import SimpleNamespace
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.support import support_admin_bp, support_member_bp
from tests.community_ids import cpath, install_community_ids
from tests.conftest import seed_membership

ADMIN_SCOPE = "community.support:admin"


@pytest.fixture
def app(support_token_db: Any) -> Quart:
    dal, _ = support_token_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    install_community_ids(quart_app, dal)
    quart_app.register_blueprint(support_admin_bp)
    quart_app.register_blueprint(support_member_bp)
    quart_app.config["dal"] = dal
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _seed_membership(dal: Any, community_id: int, user_id: int, *, active: bool = True) -> None:
    """Insert one `community_members` row (`user_id` is the legacy VARCHAR column)."""
    dal.community_members.insert(
        community_id=community_id, user_id=str(user_id), role="member", is_active=active
    )
    dal.commit()


#: Administers the seeded community (real `community-admin` role row): the admin routes check
#: per-community standing, never just the tenant-wide `community.support:admin` token scope.
SUPPORT_ADMIN_UID = 77


@pytest.fixture(autouse=True)
def _seeded_members(support_token_db: Any) -> None:
    """Users 1 and 2 are active members, 77 is the community admin; 99 is deliberately not."""
    dal, community_id = support_token_db
    _seed_membership(dal, community_id, 1)
    _seed_membership(dal, community_id, 2)
    seed_membership(
        SimpleNamespace(dal=dal),
        community_id=community_id,
        user_id=SUPPORT_ADMIN_UID,
        role="community-admin",
    )


async def _post_json(
    client: Any, path: str, headers: dict[str, str], payload: dict[str, Any]
) -> Any:
    return await client.post(
        path,
        headers={**headers, "Content-Type": "application/json"},
        data=json_module.dumps(payload),
    )


async def _put_json(
    client: Any, path: str, headers: dict[str, str], payload: dict[str, Any]
) -> Any:
    return await client.put(
        path,
        headers={**headers, "Content-Type": "application/json"},
        data=json_module.dumps(payload),
    )


class TestAuthBypass:
    async def test_categories_requires_token(self, client: Any, support_token_db: Any) -> None:
        dal, community_id = support_token_db
        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/categories", community_id)
        )
        assert response.status_code == 401

    async def test_tickets_requires_token(self, client: Any, support_token_db: Any) -> None:
        dal, community_id = support_token_db
        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/tickets", community_id)
        )
        assert response.status_code == 401

    async def test_submit_requires_token(self, client: Any, support_token_db: Any) -> None:
        dal, community_id = support_token_db
        response = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            {},
            {"subject": "help"},
        )
        assert response.status_code == 401

    async def test_my_tickets_requires_token(self, client: Any, support_token_db: Any) -> None:
        dal, community_id = support_token_db
        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/my-tickets", community_id)
        )
        assert response.status_code == 401


class TestMemberGate:
    """regression: unscoped-routes -- member routes were tenant_middleware-only."""

    async def test_non_member_cannot_read_categories(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/categories", community_id),
            headers=user_auth_headers(user_id=99),
        )
        assert response.status_code == 403

    async def test_non_member_cannot_submit_ticket(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        response = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            user_auth_headers(user_id=99),
            {"subject": "spam"},
        )
        assert response.status_code == 403
        assert dal(dal.support_tickets).count() == 0

    async def test_non_member_cannot_list_or_comment_own_tickets(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        mine = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/my-tickets", community_id),
            headers=user_auth_headers(user_id=99),
        )
        one = await client.get(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/my-tickets/{ticket_id}",
                community_id,
                ticket_id=1,
            ),
            headers=user_auth_headers(user_id=99),
        )
        comment = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/my-tickets/{ticket_id}/comments",
                community_id,
                ticket_id=1,
            ),
            user_auth_headers(user_id=99),
            {"content": "hi"},
        )
        assert (mine.status_code, one.status_code, comment.status_code) == (403, 403, 403)

    async def test_cross_tenant_community_is_404(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, _ = support_token_db
        other = dal.tenants.insert(slug="other-tenant", is_active=True)
        foreign_community = dal.communities.insert(name="foreign", tenant_id=other)
        dal.commit()
        _seed_membership(dal, foreign_community, 1)
        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/categories", foreign_community),
            headers=user_auth_headers(user_id=1),
        )
        assert response.status_code == 404

    async def test_deactivated_member_is_403(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        _seed_membership(dal, community_id, 50, active=False)
        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/categories", community_id),
            headers=user_auth_headers(user_id=50),
        )
        assert response.status_code == 403

    async def test_member_can_submit_ticket(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        response = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            user_auth_headers(user_id=1),
            {"subject": "help"},
        )
        assert response.status_code == 201
        assert dal(dal.support_tickets).count() == 1

    async def test_cross_tenant_submit_is_404_and_writes_nothing(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, _ = support_token_db
        other = dal.tenants.insert(slug="other-tenant-submit", is_active=True)
        foreign_community = dal.communities.insert(name="foreign-submit", tenant_id=other)
        dal.commit()
        _seed_membership(dal, foreign_community, 1)
        response = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", foreign_community),
            user_auth_headers(user_id=1),
            {"subject": "spam"},
        )
        assert response.status_code == 404
        assert dal(dal.support_tickets).count() == 0

    async def test_deactivated_member_cannot_submit(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        _seed_membership(dal, community_id, 51, active=False)
        response = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            user_auth_headers(user_id=51),
            {"subject": "spam"},
        )
        assert response.status_code == 403
        assert dal(dal.support_tickets).count() == 0

    async def test_tenant_wide_admin_scope_without_standing_is_refused(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        """`community.support:admin` is tenant-wide: it must not open another community's desk."""
        dal, community_id = support_token_db
        headers = user_auth_headers(user_id=99, scope="community.support:admin")
        read = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/categories", community_id),
            headers=headers,
        )
        submit = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            headers,
            {"subject": "x"},
        )
        assert (read.status_code, submit.status_code) == (403, 403)

    async def test_community_admin_can_read_categories(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        read = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/categories", community_id),
            headers=user_auth_headers(user_id=SUPPORT_ADMIN_UID, scope=ADMIN_SCOPE),
        )
        assert read.status_code == 200

    async def test_member_gets_2xx(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/my-tickets", community_id),
            headers=user_auth_headers(user_id=1),
        )
        assert response.status_code == 200

    async def test_unauthenticated_is_401(self, client: Any, support_token_db: Any) -> None:
        dal, community_id = support_token_db
        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/my-tickets", community_id)
        )
        assert response.status_code == 401


class TestScopeEnforcement:
    async def test_categories_read_open_to_any_authenticated_member(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        """Categories need an ACTIVE community membership (the ticket-form read)."""
        dal, community_id = support_token_db
        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/categories", community_id),
            headers=user_auth_headers(user_id=1, scope=""),
        )
        assert response.status_code == 200

    async def test_create_category_requires_admin_scope(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        response = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/categories", community_id),
            user_auth_headers(user_id=1, scope=""),
            {"name": "Billing"},
        )
        assert response.status_code == 403

    async def test_unknown_community_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            "/api/v1/admin/9999/support/categories", headers=auth_headers(scope="")
        )
        assert response.status_code == 404


class TestCategoryCrud:
    async def test_create_list_update_delete_roundtrip(
        self, client: Any, auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        headers = auth_headers(scope=ADMIN_SCOPE, user_id=str(SUPPORT_ADMIN_UID))
        create = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/categories", community_id),
            headers,
            {"name": "Billing", "description": "Payment issues", "sort_order": 1},
        )
        assert create.status_code == 201
        category = (await create.get_json())["category"]
        assert category["name"] == "Billing"
        category_id = category["id"]

        listing = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/categories", community_id),
            headers=headers,
        )
        assert any(c["id"] == category_id for c in (await listing.get_json())["categories"])

        update = await _put_json(
            client,
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/categories/{category_id}",
                community_id,
                category_id=category_id,
            ),
            headers,
            {"is_active": False},
        )
        assert update.status_code == 200
        updated = (await update.get_json())["category"]
        assert updated["is_active"] is False
        assert updated["name"] == "Billing"  # omitted field kept its old value

        delete = await client.delete(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/categories/{category_id}",
                community_id,
                category_id=category_id,
            ),
            headers=headers,
        )
        assert delete.status_code == 200

        missing_update = await _put_json(
            client,
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/categories/{category_id}",
                community_id,
                category_id=category_id,
            ),
            headers,
            {"name": "gone"},
        )
        assert missing_update.status_code == 404

    async def test_create_requires_name(
        self, client: Any, auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        response = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/categories", community_id),
            auth_headers(scope=ADMIN_SCOPE, user_id=str(SUPPORT_ADMIN_UID)),
            {"name": "  "},
        )
        assert response.status_code == 400


class TestTicketAdminTriage:
    async def test_full_triage_flow(
        self, client: Any, auth_headers: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        admin = auth_headers(scope=ADMIN_SCOPE, user_id=str(SUPPORT_ADMIN_UID))
        reporter = user_auth_headers(user_id=1)

        submit = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            reporter,
            {"subject": "Cannot log in", "priority": "high"},
        )
        assert submit.status_code == 201
        ticket = (await submit.get_json())["ticket"]
        assert ticket["ticket_number"] == "SUP-00001"
        assert ticket["status"] == "open"
        assert ticket["reporter_user_id"] == 1
        ticket_id = ticket["id"]

        listing = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/tickets", community_id), headers=admin
        )
        body = await listing.get_json()
        assert body["total"] == 1
        assert body["tickets"][0]["id"] == ticket_id

        got = await client.get(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/tickets/{ticket_id}",
                community_id,
                ticket_id=ticket_id,
            ),
            headers=admin,
        )
        assert got.status_code == 200
        assert (await got.get_json())["comments"] == []

        status = await _put_json(
            client,
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/tickets/{ticket_id}/status",
                community_id,
                ticket_id=ticket_id,
            ),
            admin,
            {"status": "resolved"},
        )
        assert status.status_code == 200
        resolved_ticket = (await status.get_json())["ticket"]
        assert resolved_ticket["status"] == "resolved"
        assert resolved_ticket["resolved_at"] is not None

        assign = await _put_json(
            client,
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/tickets/{ticket_id}/assign",
                community_id,
                ticket_id=ticket_id,
            ),
            admin,
            {"assignee_user_id": 2},
        )
        assert assign.status_code == 200
        assert (await assign.get_json())["ticket"]["assignee_user_id"] == 2

        priority = await _put_json(
            client,
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/tickets/{ticket_id}/priority",
                community_id,
                ticket_id=ticket_id,
            ),
            admin,
            {"priority": "critical"},
        )
        assert priority.status_code == 200
        assert (await priority.get_json())["ticket"]["priority"] == "critical"

        comment = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/tickets/{ticket_id}/comments",
                community_id,
                ticket_id=ticket_id,
            ),
            admin,
            {"content": "internal note", "is_internal": True},
        )
        assert comment.status_code == 201
        assert (await comment.get_json())["comment"]["is_internal"] is True

        got_again = await client.get(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/tickets/{ticket_id}",
                community_id,
                ticket_id=ticket_id,
            ),
            headers=admin,
        )
        comments = (await got_again.get_json())["comments"]
        assert len(comments) == 1
        assert comments[0]["is_internal"] is True

    async def test_invalid_status_is_400(
        self, client: Any, auth_headers: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        submit = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            user_auth_headers(user_id=1),
            {"subject": "x"},
        )
        ticket_id = (await submit.get_json())["ticket"]["id"]
        response = await _put_json(
            client,
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/tickets/{ticket_id}/status",
                community_id,
                ticket_id=ticket_id,
            ),
            auth_headers(scope=ADMIN_SCOPE, user_id=str(SUPPORT_ADMIN_UID)),
            {"status": "not-a-status"},
        )
        assert response.status_code == 400

    async def test_invalid_priority_is_400(
        self, client: Any, auth_headers: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        submit = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            user_auth_headers(user_id=1),
            {"subject": "x"},
        )
        ticket_id = (await submit.get_json())["ticket"]["id"]
        response = await _put_json(
            client,
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/tickets/{ticket_id}/priority",
                community_id,
                ticket_id=ticket_id,
            ),
            auth_headers(scope=ADMIN_SCOPE, user_id=str(SUPPORT_ADMIN_UID)),
            {"priority": "urgent"},  # Node route-layer drift value -- rejected by controller enum
        )
        assert response.status_code == 400

    async def test_unknown_ticket_is_404(
        self, client: Any, auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        response = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/tickets/9999", community_id),
            headers=auth_headers(scope=ADMIN_SCOPE, user_id=str(SUPPORT_ADMIN_UID)),
        )
        assert response.status_code == 404


class TestMemberSelfService:
    async def test_submit_requires_subject(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        response = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            user_auth_headers(user_id=1),
            {"subject": "   "},
        )
        assert response.status_code == 400

    async def test_my_tickets_only_shows_own_tickets(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        reporter_a = user_auth_headers(user_id=1)
        reporter_b = user_auth_headers(user_id=2)
        await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            reporter_a,
            {"subject": "A's issue"},
        )
        await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            reporter_b,
            {"subject": "B's issue"},
        )

        mine = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/my-tickets", community_id),
            headers=reporter_a,
        )
        tickets = (await mine.get_json())["tickets"]
        assert len(tickets) == 1
        assert tickets[0]["subject"] == "A's issue"


class TestMemberOwnership:
    """SECURITY FIX regression -- see this module's own docstring."""

    async def test_get_my_ticket_of_another_reporter_is_404(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        owner = user_auth_headers(user_id=1)
        intruder = user_auth_headers(user_id=2)
        submit = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            owner,
            {"subject": "private"},
        )
        ticket_id = (await submit.get_json())["ticket"]["id"]

        own_view = await client.get(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/my-tickets/{ticket_id}",
                community_id,
                ticket_id=ticket_id,
            ),
            headers=owner,
        )
        assert own_view.status_code == 200

        intruder_view = await client.get(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/my-tickets/{ticket_id}",
                community_id,
                ticket_id=ticket_id,
            ),
            headers=intruder,
        )
        assert intruder_view.status_code == 404

    async def test_add_own_comment_on_another_reporters_ticket_is_404(
        self, client: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        owner = user_auth_headers(user_id=1)
        intruder = user_auth_headers(user_id=2)
        submit = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            owner,
            {"subject": "private"},
        )
        ticket_id = (await submit.get_json())["ticket"]["id"]

        intruder_comment = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/my-tickets/{ticket_id}/comments",
                community_id,
                ticket_id=ticket_id,
            ),
            intruder,
            {"content": "sneaky"},
        )
        assert intruder_comment.status_code == 404

        owner_comment = await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/my-tickets/{ticket_id}/comments",
                community_id,
                ticket_id=ticket_id,
            ),
            owner,
            {"content": "legit"},
        )
        assert owner_comment.status_code == 201

    async def test_my_ticket_never_shows_internal_comments(
        self, client: Any, auth_headers: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        owner = user_auth_headers(user_id=1)
        admin = auth_headers(scope=ADMIN_SCOPE, user_id=str(SUPPORT_ADMIN_UID))
        submit = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            owner,
            {"subject": "x"},
        )
        ticket_id = (await submit.get_json())["ticket"]["id"]
        await _post_json(
            client,
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/tickets/{ticket_id}/comments",
                community_id,
                ticket_id=ticket_id,
            ),
            admin,
            {"content": "internal only", "is_internal": True},
        )

        own_view = await client.get(
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/my-tickets/{ticket_id}",
                community_id,
                ticket_id=ticket_id,
            ),
            headers=owner,
        )
        assert (await own_view.get_json())["comments"] == []


class TestStats:
    async def test_stats_counts_and_average_resolution(
        self, client: Any, auth_headers: Any, user_auth_headers: Any, support_token_db: Any
    ) -> None:
        dal, community_id = support_token_db
        reporter = user_auth_headers(user_id=1)
        admin = auth_headers(scope=ADMIN_SCOPE, user_id=str(SUPPORT_ADMIN_UID))

        open_ticket = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            reporter,
            {"subject": "still open"},
        )
        resolved_submit = await _post_json(
            client,
            cpath(dal, "/api/v1/admin/{community_id}/support/submit", community_id),
            reporter,
            {"subject": "will resolve"},
        )
        resolved_id = (await resolved_submit.get_json())["ticket"]["id"]
        await _put_json(
            client,
            cpath(
                dal,
                "/api/v1/admin/{community_id}/support/tickets/{resolved_id}/status",
                community_id,
                resolved_id=resolved_id,
            ),
            admin,
            {"status": "resolved"},
        )
        assert open_ticket.status_code == 201

        stats = await client.get(
            cpath(dal, "/api/v1/admin/{community_id}/support/stats", community_id), headers=admin
        )
        body = (await stats.get_json())["stats"]
        assert body["total"] == 2
        assert body["open"] == 1
        assert body["resolved"] == 1
        assert body["avg_resolution_seconds"] is not None


#: Every JSON response read here must carry community ids as UUID strings (conftest guard).
UUID_COMMUNITY_RESPONSES = True
