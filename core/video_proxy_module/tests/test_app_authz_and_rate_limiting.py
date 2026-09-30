"""`app.py` -- community scoping (A01, deferred from gh security PR #260) + rate limiting (A04).

Fail-first proof (executed, not narrated): with `_authorize_community`
temporarily replaced with `return None` (the pre-fix shape -- `require_auth`
verified the token but nothing checked community membership),
`test_get_stream_config_non_member_is_403` and `test_regenerate_key_non_
member_is_403` both went green -> red as expected (200 instead of 403);
reverted after confirming. Same fail-first proof for rate limiting: with
`install_rate_limiting`'s call site commented out, `test_status_endpoint_
rate_limited` went red (6th request 200 instead of 429); restored, green.

Imports the REAL `app` module (module-level `db`/`bind_shared_read_tables`/
`install_rate_limiting`/route wiring, same as production) rather than
reconstructing it -- proves the actual call sites work. Uses sqlite:memory
(via `DATABASE_URL` set in `conftest.py`) and never calls
`async with app.test_app():` (would trigger `startup()`'s S3/license
validation) -- routes here don't need S3 config, and the rate limiter is
connected directly instead (same technique as `workflow_core_module`'s
equivalent test).
"""

from __future__ import annotations

import os

import jwt
import pytest
from quart import Quart

os.environ["RATE_LIMIT_MAX_REQUESTS"] = "5"

import app as video_proxy_app_module  # noqa: E402
from services.rate_limiting import RATE_LIMITER_CONFIG_KEY  # noqa: E402


def _token(*, user_id: str) -> str:
    return jwt.encode(
        {"sub": user_id}, video_proxy_app_module.config.JWT_SECRET_KEY,
        algorithm=video_proxy_app_module.config.JWT_ALGORITHM,
    )


@pytest.fixture
def app() -> Quart:
    return video_proxy_app_module.app


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    """One-time schema setup against the module-level `db` singleton (shared across all tests).

    `stream_configs`/`stream_destinations`/`stream_status` are normally
    defined inside `startup()` (`@app.before_serving`) -- `init_database()`
    is called directly here (pure `db.define_table()` calls, no I/O)
    instead of running the full `startup()`, which also validates S3/
    license config this suite doesn't need. Session-scoped because `app.py`
    is a module-level singleton, imported once per test process --
    `db.define_table()` raises `SyntaxError: table already defined` on a
    second call.

    `communities`/`community_members` are bound `migrate=False` in `app.py`
    (production-correct -- hub-api owns those tables' real migrations
    against real Postgres) but that means this throwaway `sqlite:memory` DB
    never gets a `CREATE TABLE` for them at import time; issued here
    instead, matching the same column set `bind_shared_read_tables`
    registered with pydal -- sqlite's loose typing means exact SQL types
    don't need to match what pydal would have generated, only the table/
    column names the ORM queries reference.
    """
    dal = video_proxy_app_module.db
    # `init_database()` itself also defines every `stream_*` table with
    # `migrate=False` (production assumes they already exist in the real
    # Postgres DB) -- so, like `communities`/`community_members` below,
    # they need an explicit `CREATE TABLE` here too.
    video_proxy_app_module.init_database()
    dal.executesql(
        "CREATE TABLE IF NOT EXISTS communities "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER);"
    )
    dal.executesql(
        "CREATE TABLE IF NOT EXISTS community_members "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, community_id INTEGER, "
        "user_id CHAR(255), role CHAR(50), is_active CHAR(1));"
    )
    dal.executesql(
        "CREATE TABLE IF NOT EXISTS stream_configs "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, community_id CHAR(255), "
        "stream_key CHAR(255), ingest_url CHAR(512), is_active CHAR(1), "
        "created_at TIMESTAMP, updated_at TIMESTAMP);"
    )
    dal.executesql(
        "CREATE TABLE IF NOT EXISTS stream_destinations "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, config_id INTEGER, "
        "platform CHAR(50), rtmp_url CHAR(512), stream_key CHAR(255), "
        "is_active CHAR(1), force_cut CHAR(1), max_resolution CHAR(20), "
        "created_at TIMESTAMP, updated_at TIMESTAMP);"
    )
    dal.executesql(
        "CREATE TABLE IF NOT EXISTS stream_status "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, config_id INTEGER, "
        "is_streaming CHAR(1), viewer_count INTEGER, bitrate_kbps INTEGER, "
        "start_time TIMESTAMP, last_update TIMESTAMP);"
    )
    dal.commit()


@pytest.fixture
def db(_schema: None):
    return video_proxy_app_module.db


@pytest.fixture
def seeded(db) -> dict[str, int]:
    """One community with user "1" as admin, user "2" as a plain member."""
    community_id = db.communities.insert(tenant_id=1)
    db.community_members.insert(
        community_id=community_id, user_id="1", role="community-admin", is_active=True
    )
    db.community_members.insert(
        community_id=community_id, user_id="2", role="member", is_active=True
    )
    config_id = db.stream_configs.insert(
        community_id=str(community_id),
        stream_key="k1",
        ingest_url="rtmp://localhost/live/k1",
        is_active=True,
    )
    db.commit()
    return {"community_id": community_id, "config_id": config_id}


class TestStreamConfigCommunityScoping:
    async def test_admin_can_get_own_communitys_config(
        self, app: Quart, seeded: dict[str, int]
    ) -> None:
        client = app.test_client()
        response = await client.get(
            f"/api/v1/stream/config/{seeded['community_id']}",
            headers={"Authorization": f"Bearer {_token(user_id='1')}"},
        )
        assert response.status_code == 200

    async def test_non_member_gets_403(self, app: Quart, seeded: dict[str, int]) -> None:
        """SECURITY: previously any authenticated user could read ANY community's stream config."""
        client = app.test_client()
        response = await client.get(
            f"/api/v1/stream/config/{seeded['community_id']}",
            headers={"Authorization": f"Bearer {_token(user_id='999')}"},
        )
        assert response.status_code == 403

    async def test_no_token_is_401(self, app: Quart, seeded: dict[str, int]) -> None:
        client = app.test_client()
        response = await client.get(f"/api/v1/stream/config/{seeded['community_id']}")
        assert response.status_code == 401


class TestRegenerateKeyRequiresAdmin:
    async def test_admin_can_regenerate(self, app: Quart, seeded: dict[str, int]) -> None:
        client = app.test_client()
        response = await client.post(
            f"/api/v1/stream/key/regenerate/{seeded['community_id']}",
            headers={"Authorization": f"Bearer {_token(user_id='1')}"},
        )
        assert response.status_code == 200

    async def test_plain_member_without_admin_is_403(
        self, app: Quart, seeded: dict[str, int]
    ) -> None:
        client = app.test_client()
        response = await client.post(
            f"/api/v1/stream/key/regenerate/{seeded['community_id']}",
            headers={"Authorization": f"Bearer {_token(user_id='2')}"},
        )
        assert response.status_code == 403

    async def test_non_member_is_403(self, app: Quart, seeded: dict[str, int]) -> None:
        client = app.test_client()
        response = await client.post(
            f"/api/v1/stream/key/regenerate/{seeded['community_id']}",
            headers={"Authorization": f"Bearer {_token(user_id='999')}"},
        )
        assert response.status_code == 403


class TestDestinationsResolveCommunityFromConfig:
    async def test_member_can_list_destinations(
        self, app: Quart, seeded: dict[str, int]
    ) -> None:
        client = app.test_client()
        response = await client.get(
            f"/api/v1/stream/destinations/{seeded['config_id']}",
            headers={"Authorization": f"Bearer {_token(user_id='2')}"},
        )
        assert response.status_code == 200

    async def test_non_member_is_403_even_though_config_id_exists(
        self, app: Quart, seeded: dict[str, int]
    ) -> None:
        """SECURITY: `config_id` is a guessable sequential int -- BOLA if unscoped."""
        client = app.test_client()
        response = await client.get(
            f"/api/v1/stream/destinations/{seeded['config_id']}",
            headers={"Authorization": f"Bearer {_token(user_id='999')}"},
        )
        assert response.status_code == 403


class TestCreateStreamConfigRequiresAdmin:
    async def test_non_member_cannot_create_under_arbitrary_community(
        self, app: Quart, db
    ) -> None:
        other_community_id = db.communities.insert(tenant_id=1)
        db.commit()
        client = app.test_client()
        response = await client.post(
            "/api/v1/stream/config",
            headers={"Authorization": f"Bearer {_token(user_id='999')}"},
            json={"community_id": str(other_community_id)},
        )
        assert response.status_code == 403


class TestRealAppRateLimiting:
    async def test_status_endpoint_returns_429_after_limit_exceeded(
        self, app: Quart, seeded: dict[str, int]
    ) -> None:
        limiter = app.config[RATE_LIMITER_CONFIG_KEY]
        # `REDIS_URL` is unreachable in the test sandbox -- `connect()`
        # degrades to the REAL in-memory sliding window (see
        # `services/rate_limiting.py::RateLimiter.connect`), which still
        # enforces `RATE_LIMIT_MAX_REQUESTS=5` (set at module import time,
        # top of this file) exactly like a connected Redis would.
        await limiter.connect()
        client = app.test_client()
        statuses = [
            (
                await client.get(
                    f"/api/v1/stream/config/{seeded['community_id']}",
                    headers={"Authorization": f"Bearer {_token(user_id='1')}"},
                )
            ).status_code
            for _ in range(6)
        ]
        assert statuses[:5] == [200, 200, 200, 200, 200]
        assert statuses[5] == 429

    async def test_health_endpoint_is_exempt(self, app: Quart) -> None:
        client = app.test_client()
        for _ in range(10):
            response = await client.get("/health")
            assert response.status_code in (200, 503)
