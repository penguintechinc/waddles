# ruff: noqa: F811, F401, E501
"""Behavioural proof for the read routes guarded INLINE (stamped with `mark_guarded`).

The walker only proves a stamp exists; these prove the handler really refuses a non-admin:
ai-knowledge sources list/search (community from query/body) and the ingest-sources list.
Never uses `bypass_route_guards`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.ingest_sources import BLUEPRINTS as INGEST_BLUEPRINTS
from services.community_common import ensure_community_tables
from tests import test_community_inventory as _inventory_tests
from tests.community_ids import public_id
from tests.conftest import TENANT_SLUG, make_user_token, seed_membership

# Fixtures reused from the bot blueprint tests (real schema, seeded tenant + community).
from tests.test_bot_blueprint import (
    app as bot_app,
)
from tests.test_bot_blueprint import (
    community_id,
    dal,
)

ADMIN_UID, MEMBER_UID, OUTSIDER_UID = 21, 22, 23
READ = "bot.ai_knowledge:read"


@pytest.fixture
def seeded_bot(dal: Any, community_id: int) -> Any:  # noqa: F811
    ensure_community_tables(dal, migrate=True)
    db = SimpleNamespace(dal=dal)
    seed_membership(db, community_id=community_id, user_id=ADMIN_UID, role="community-admin")
    seed_membership(db, community_id=community_id, user_id=MEMBER_UID, role="member")
    return dal, community_id


def _hdr(uid: int, scope: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_user_token(user_id=uid, scope=scope)}"}


class TestAiKnowledgeInlineGuard:
    async def test_member_cannot_list_sources(self, bot_app: Quart, seeded_bot: Any) -> None:  # noqa: F811
        dal, cid = seeded_bot
        r = await bot_app.test_client().get(
            f"/api/v1/admin/ai-knowledge/sources?communityId={public_id(dal, cid)}",
            headers=_hdr(MEMBER_UID, f"*:read {READ}"),
        )
        assert r.status_code == 403

    async def test_outsider_cannot_list_sources(self, bot_app: Quart, seeded_bot: Any) -> None:  # noqa: F811
        dal, cid = seeded_bot
        r = await bot_app.test_client().get(
            f"/api/v1/admin/ai-knowledge/sources?communityId={public_id(dal, cid)}",
            headers=_hdr(OUTSIDER_UID, f"*:read {READ}"),
        )
        assert r.status_code == 403

    async def test_admin_can_list_sources(self, bot_app: Quart, seeded_bot: Any) -> None:  # noqa: F811
        dal, cid = seeded_bot
        r = await bot_app.test_client().get(
            f"/api/v1/admin/ai-knowledge/sources?communityId={public_id(dal, cid)}",
            headers=_hdr(ADMIN_UID, f"*:read {READ}"),
        )
        assert r.status_code == 200

    async def test_member_cannot_search(self, bot_app: Quart, seeded_bot: Any) -> None:  # noqa: F811
        dal, cid = seeded_bot
        r = await bot_app.test_client().post(
            "/api/v1/admin/ai-knowledge/search",
            headers=_hdr(MEMBER_UID, f"*:read {READ}"),
            json={"query": "hello", "communityId": public_id(dal, cid)},
        )
        assert r.status_code == 403


@pytest.fixture
async def ingest_app(bundle_install_db: Any, install_dal: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["async_dal"] = bundle_install_db
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = install_dal
    for bp in INGEST_BLUEPRINTS:
        quart_app.register_blueprint(bp)
    return quart_app


class TestIngestSourcesInlineGuard:
    async def test_non_admin_member_cannot_list(self, ingest_app: Quart) -> None:
        from tests.test_ingest_sources_blueprint import _make_community_ids

        install_dal = ingest_app.config["install_dal"]
        internal, pid = await _make_community_ids(install_dal, tenant_id=1)
        dal = ingest_app.config["dal"]
        ensure_community_tables(dal, migrate=True)
        seed_membership(
            SimpleNamespace(dal=dal), community_id=internal, user_id=MEMBER_UID, role="member"
        )
        token = make_user_token(user_id=MEMBER_UID, scope="tenant:read", tenant=TENANT_SLUG)
        r = await ingest_app.test_client().get(
            f"/api/v1/communities/{pid}/ingest-sources",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    async def test_community_admin_can_list(self, ingest_app: Quart) -> None:
        from tests.test_ingest_sources_blueprint import _make_community_ids

        install_dal = ingest_app.config["install_dal"]
        internal, pid = await _make_community_ids(install_dal, tenant_id=1)
        dal = ingest_app.config["dal"]
        ensure_community_tables(dal, migrate=True)
        seed_membership(
            SimpleNamespace(dal=dal),
            community_id=internal,
            user_id=ADMIN_UID,
            role="community-admin",
        )
        token = make_user_token(user_id=ADMIN_UID, scope="tenant:read", tenant=TENANT_SLUG)
        r = await ingest_app.test_client().get(
            f"/api/v1/communities/{pid}/ingest-sources",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200


MAINTAINER_A, MEMBER_B_UID = 71, 72
_WRITE_SCOPES = "tenant:admin tenant:read"


async def _two_ingest_communities(ingest_app: Quart) -> tuple[Any, str, str, int]:
    """`(dal, A public id, B public id, B internal id)`: 71 administers A only, 72 is a member of B."""
    from tests.test_ingest_sources_blueprint import _make_community_ids

    install_dal = ingest_app.config["install_dal"]
    a_internal, a_pid = await _make_community_ids(install_dal, tenant_id=1, name="a")
    b_internal, b_pid = await _make_community_ids(install_dal, tenant_id=1, name="b")
    dal = ingest_app.config["dal"]
    ensure_community_tables(dal, migrate=True)
    db = SimpleNamespace(dal=dal)
    seed_membership(db, community_id=a_internal, user_id=MAINTAINER_A, role="community-admin")
    seed_membership(db, community_id=b_internal, user_id=MEMBER_B_UID, role="member")
    return dal, a_pid, b_pid, b_internal


async def _b_source(ingest_app: Quart, b_pid: str) -> str:
    """Create one ingest source in B straight through the service; return its public id."""
    from services import ingest_source_registry_service as svc

    install_dal = ingest_app.config["install_dal"]
    b_internal, _ = await svc.resolve_community(install_dal, tenant_id=1, community_public_id=b_pid)
    row = await svc.create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=b_internal,
        platform="discord",
        source_id="guild-b",
        label="B source",
    )
    return str(row.public_id)


class TestIngestSourceWritesRequireCommunityAdmin:
    """POST/PATCH/DELETE carry only tenant-wide `tenant:admin`; they also need community standing."""

    async def test_maintainer_of_a_cannot_create_in_b(self, ingest_app: Quart) -> None:
        _dal, _a, b_pid, _b = await _two_ingest_communities(ingest_app)
        r = await ingest_app.test_client().post(
            f"/api/v1/communities/{b_pid}/ingest-sources",
            headers=_hdr(MAINTAINER_A, _WRITE_SCOPES),
            json={"platform": "discord", "sourceId": "g", "label": "x"},
        )
        assert r.status_code == 403

    async def test_maintainer_of_a_cannot_update_in_b(self, ingest_app: Quart) -> None:
        _dal, _a, b_pid, _b = await _two_ingest_communities(ingest_app)
        source = await _b_source(ingest_app, b_pid)
        r = await ingest_app.test_client().patch(
            f"/api/v1/communities/{b_pid}/ingest-sources/{source}",
            headers=_hdr(MAINTAINER_A, _WRITE_SCOPES),
            json={"enabled": False},
        )
        assert r.status_code == 403

    async def test_maintainer_of_a_cannot_delete_in_b(self, ingest_app: Quart) -> None:
        _dal, _a, b_pid, _b = await _two_ingest_communities(ingest_app)
        source = await _b_source(ingest_app, b_pid)
        r = await ingest_app.test_client().delete(
            f"/api/v1/communities/{b_pid}/ingest-sources/{source}",
            headers=_hdr(MAINTAINER_A, _WRITE_SCOPES),
        )
        assert r.status_code == 403

    async def test_plain_member_is_refused_on_every_write(self, ingest_app: Quart) -> None:
        _dal, _a, b_pid, _b = await _two_ingest_communities(ingest_app)
        source = await _b_source(ingest_app, b_pid)
        client, hdr = ingest_app.test_client(), _hdr(MEMBER_B_UID, _WRITE_SCOPES)
        base = f"/api/v1/communities/{b_pid}/ingest-sources"
        post = await client.post(
            base, headers=hdr, json={"platform": "discord", "sourceId": "g2", "label": "x"}
        )
        patch = await client.patch(f"{base}/{source}", headers=hdr, json={"enabled": False})
        delete = await client.delete(f"{base}/{source}", headers=hdr)
        assert (post.status_code, patch.status_code, delete.status_code) == (403, 403, 403)

    async def test_community_admin_can_write_in_own_community(self, ingest_app: Quart) -> None:
        """Positive control: the guard is not a blanket deny."""
        _dal, a_pid, _b, _bi = await _two_ingest_communities(ingest_app)
        client, hdr = ingest_app.test_client(), _hdr(MAINTAINER_A, _WRITE_SCOPES)
        base = f"/api/v1/communities/{a_pid}/ingest-sources"
        created = await client.post(
            base, headers=hdr, json={"platform": "discord", "sourceId": "ga", "label": "A"}
        )
        assert created.status_code == 201
        source = (await created.get_json())["ingestSource"]["id"]
        patched = await client.patch(f"{base}/{source}", headers=hdr, json={"enabled": False})
        deleted = await client.delete(f"{base}/{source}", headers=hdr)
        assert (patched.status_code, deleted.status_code) == (200, 204)

    async def test_source_id_from_another_community_is_404(self, ingest_app: Quart) -> None:
        """IDOR: A's admin names B's source id under A's URL -> 404, B's row untouched."""
        _dal, a_pid, b_pid, _b = await _two_ingest_communities(ingest_app)
        source = await _b_source(ingest_app, b_pid)
        client, hdr = ingest_app.test_client(), _hdr(MAINTAINER_A, _WRITE_SCOPES)
        patched = await client.patch(
            f"/api/v1/communities/{a_pid}/ingest-sources/{source}",
            headers=hdr,
            json={"enabled": False},
        )
        deleted = await client.delete(
            f"/api/v1/communities/{a_pid}/ingest-sources/{source}", headers=hdr
        )
        assert (patched.status_code, deleted.status_code) == (404, 404)
        from services import ingest_source_registry_service as svc

        b_internal = (
            await svc.resolve_community(
                ingest_app.config["install_dal"], tenant_id=1, community_public_id=b_pid
            )
        )[0]
        assert (
            await svc.resolve_ingest_source_id(
                ingest_app.config["install_dal"],
                tenant_id=1,
                community_id=b_internal,
                public_id=source,
            )
            is not None
        )

    async def test_unknown_community_is_404_with_the_guard_body(self, ingest_app: Quart) -> None:
        await _two_ingest_communities(ingest_app)
        r = await ingest_app.test_client().post(
            "/api/v1/communities/00000000-0000-4000-8000-00000000dead/ingest-sources",
            headers=_hdr(MAINTAINER_A, _WRITE_SCOPES),
            json={"platform": "discord", "sourceId": "g", "label": "x"},
        )
        assert r.status_code == 404
        assert (await r.get_json())["error"]["message"] == "Community not found"


#: Every `@mark_guarded` route -> the test that sends a plain-member request with the real guards
#: (no `bypass_route_guards`) and asserts 403/404. `test_read_route_guards.py` fails any stamped
#: route missing here, so a stamp can never be a bare label.
INLINE_GUARD_DENY_TESTS: dict[tuple[str, str], Any] = {
    (
        "GET",
        "/api/v1/admin/ai-knowledge/sources",
    ): TestAiKnowledgeInlineGuard.test_member_cannot_list_sources,
    (
        "POST",
        "/api/v1/admin/ai-knowledge/search",
    ): TestAiKnowledgeInlineGuard.test_member_cannot_search,
    (
        "GET",
        "/api/v1/communities/<string:community_public_id>/ingest-sources",
    ): TestIngestSourcesInlineGuard.test_non_admin_member_cannot_list,
    (
        "POST",
        "/api/v1/communities/<string:community_public_id>/ingest-sources",
    ): TestIngestSourceWritesRequireCommunityAdmin.test_maintainer_of_a_cannot_create_in_b,
    (
        "PATCH",
        "/api/v1/communities/<string:community_public_id>/ingest-sources/<string:ingest_source_id>",
    ): TestIngestSourceWritesRequireCommunityAdmin.test_maintainer_of_a_cannot_update_in_b,
    (
        "DELETE",
        "/api/v1/communities/<string:community_public_id>/ingest-sources/<string:ingest_source_id>",
    ): TestIngestSourceWritesRequireCommunityAdmin.test_maintainer_of_a_cannot_delete_in_b,
    **{
        ("GET", f"/api/v1/admin/<cuuid:community_id>/inventory/{suffix}"): (
            _inventory_tests.TestAdminReadsRequireCommunityAdmin.test_plain_member_is_403_despite_universal_read_scope
        )
        for suffix in ("items", "checkouts", "summary", "log")
    },
}


def test_deny_test_registry_points_at_real_tests() -> None:
    assert len(INLINE_GUARD_DENY_TESTS) >= 10
    for key, fn in INLINE_GUARD_DENY_TESTS.items():
        assert callable(fn) and fn.__name__.startswith("test_"), key
