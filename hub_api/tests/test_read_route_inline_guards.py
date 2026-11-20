# ruff: noqa: F811, F401
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
