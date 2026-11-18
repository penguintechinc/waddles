"""`blueprints/v1/community_activity.py` -- leaderboards, admin config, internal ingestion.

Covers the three auth surfaces this group mixes: member `tenant_middleware`
+ `require_scope`, admin `:admin` scope, and internal `X-Service-Key`
(no tenant/JWT at all -- `test_community_auth_bypass.py` already proves
every one of these rejects an unauthenticated caller; this file covers
scope enforcement + response shape for the authenticated path).
"""

from __future__ import annotations

import json as json_module
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.community_activity import (
    activity_admin_bp,
    activity_internal_bp,
    activity_member_bp,
)
from tests.community_ids import UNKNOWN_PUBLIC_ID, install_community_ids, public_id


@pytest.fixture
def app(community_db: Any) -> Quart:
    dal, _ = community_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    install_community_ids(quart_app, dal)
    quart_app.register_blueprint(activity_member_bp)
    quart_app.register_blueprint(activity_admin_bp)
    quart_app.register_blueprint(activity_internal_bp)
    quart_app.config["dal"] = dal
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Default the `community.activity` two-gate Feature flag ON for every test in this file.

    Only `watch_time_leaderboard` carries the guard (this PR's one worked
    gate example for this blueprint) -- harmless no-op for the other
    routes in this file that never call `feature_enabled`.
    """
    import blueprints.v1.community_activity as activity_module

    monkeypatch.setattr(activity_module, "feature_enabled", AsyncMock(return_value=True))


class TestMemberLeaderboards:
    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/community/{public_id(community_db[0], community_id)}/leaderboard/watch-time",
            headers=auth_headers(scope="community.activity:write"),
        )
        assert response.status_code == 403

    async def test_empty_leaderboard_shape(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/community/{public_id(community_db[0], community_id)}/leaderboard/watch-time",
            headers=auth_headers(scope="community.activity:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {
            "success": True,
            "leaderboard": [],
            "pagination": {"offset": 0, "limit": 25, "total": 0, "has_more": False},
            "period": "alltime",
        }

    @pytest.mark.skip(
        reason="_period_stats' weekly/monthly branches use Postgres-only "
        "CURRENT_DATE - INTERVAL syntax sqlite:memory can't execute; "
        "exercised against real Postgres in integration testing, not this "
        "sqlite-backed unit fixture."
    )
    async def test_my_stats_requires_auth_required_user(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/community/{public_id(community_db[0], community_id)}/activity/my-stats",
            headers=auth_headers(scope="community.activity:read", user_id="1"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["stats"]["all_time"]["watch_time_seconds"] == 0
        assert body["stats"]["ranks"] == {"watch_time": None, "messages": None}

    async def test_my_stats_non_numeric_user_id_is_400(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        """Returns before ever calling `get_my_activity_stats` (Postgres-only.

        weekly/monthly branch, see the skip above) -- the `int(...)` parse
        failure short-circuits first.
        """
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/community/{public_id(community_db[0], community_id)}/activity/my-stats",
            headers=auth_headers(scope="community.activity:read", user_id="not-a-number"),
        )
        assert response.status_code == 400

    async def test_watch_time_leaderboard_unknown_community_is_404(
        self, client: Any, auth_headers: Any
    ) -> None:
        response = await client.get(
            f"/api/v1/community/{UNKNOWN_PUBLIC_ID}/leaderboard/watch-time",
            headers=auth_headers(scope="community.activity:read"),
        )
        assert response.status_code == 404

    async def test_message_leaderboard_empty_shape(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/community/{public_id(community_db[0], community_id)}/leaderboard/messages",
            headers=auth_headers(scope="community.activity:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {
            "success": True,
            "leaderboard": [],
            "pagination": {"offset": 0, "limit": 25, "total": 0, "has_more": False},
            "period": "alltime",
        }

    async def test_message_leaderboard_unknown_community_is_404(
        self, client: Any, auth_headers: Any
    ) -> None:
        response = await client.get(
            f"/api/v1/community/{UNKNOWN_PUBLIC_ID}/leaderboard/messages",
            headers=auth_headers(scope="community.activity:read"),
        )
        assert response.status_code == 404

    async def test_non_numeric_query_params_fall_back_to_defaults(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        """`_int_arg`'s `except ValueError` branch."""
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/community/{public_id(community_db[0], community_id)}"
            f"/leaderboard/watch-time?limit=not-a-number",
            headers=auth_headers(scope="community.activity:read"),
        )
        assert response.status_code == 200
        assert (await response.get_json())["pagination"]["limit"] == 25


class TestAdminLeaderboardConfig:
    async def test_get_returns_defaults(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/leaderboard-config",
            headers=auth_headers(scope="community.activity:admin"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["config"]["display_limit"] == 25

    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/leaderboard-config",
            headers=auth_headers(scope="community.activity:read"),
        )
        assert response.status_code == 403

    async def test_get_unknown_community_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            f"/api/v1/admin/{UNKNOWN_PUBLIC_ID}/leaderboard-config",
            headers=auth_headers(scope="community.activity:admin"),
        )
        assert response.status_code == 404

    async def test_update_unknown_community_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.put(
            f"/api/v1/admin/{UNKNOWN_PUBLIC_ID}/leaderboard-config",
            headers={
                **auth_headers(scope="community.activity:admin"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"display_limit": 10}),
        )
        assert response.status_code == 404


class TestInternalIngestion:
    async def test_missing_service_key_is_401(self, client: Any) -> None:
        response = await client.post("/api/v1/internal/activity/watch-session", json={})
        assert response.status_code == 401

    async def test_wrong_service_key_is_401(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/internal/activity/watch-session",
            headers={"X-Service-Key": "not-the-real-key"},
            json={},
        )
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "path_suffix",
        ["message", "batch", "close-stale-sessions"],
    )
    async def test_other_internal_routes_reject_missing_service_key(
        self, client: Any, path_suffix: str
    ) -> None:
        response = await client.post(f"/api/v1/internal/activity/{path_suffix}", json={})
        assert response.status_code == 401

    async def test_valid_key_records_message_event(
        self, client: Any, service_key_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        """`_update_daily_stats`'s `ON CONFLICT` target is expression-based.

        (COALESCE(...) columns, matching migration 044's expression unique
        index) -- sqlite:memory's plain `define_table` has no equivalent
        expression index. Stubbed here (module-level monkeypatch, same
        pattern as `community_inventory.py::_run_stock_fn`) so the
        surrounding validation/insert/error-handling code -- the actual
        thing this test is about -- still runs for real.
        """
        import services.community_activity as activity_svc

        monkeypatch.setattr(activity_svc, "_update_daily_stats", lambda *a, **k: None)
        dal, community_id = community_db
        response = await client.post(
            "/api/v1/internal/activity/message",
            headers={**service_key_headers, "Content-Type": "application/json"},
            data=json_module.dumps(
                {
                    "communityId": community_id,
                    "platform": "twitch",
                    "platformUserId": "p1",
                    "platformUsername": "streamer1",
                }
            ),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True}
        assert dal(dal.activity_message_events.community_id == community_id).count() == 1

    async def test_message_missing_fields_is_400(
        self, client: Any, service_key_headers: Any
    ) -> None:
        response = await client.post(
            "/api/v1/internal/activity/message",
            headers={**service_key_headers, "Content-Type": "application/json"},
            data=json_module.dumps({"communityId": 1}),
        )
        assert response.status_code == 400

    async def test_batch_caps_at_100_events(self, client: Any, service_key_headers: Any) -> None:
        response = await client.post(
            "/api/v1/internal/activity/batch",
            headers=service_key_headers,
            json={"events": [{"type": "message"}] * 101},
        )
        assert response.status_code == 400

    async def test_batch_requires_nonempty_events_list(
        self, client: Any, service_key_headers: Any
    ) -> None:
        response = await client.post(
            "/api/v1/internal/activity/batch",
            headers=service_key_headers,
            json={"events": []},
        )
        assert response.status_code == 400

    async def test_batch_processes_events_with_daily_stats_stubbed(
        self, client: Any, service_key_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        import services.community_activity as activity_svc

        monkeypatch.setattr(activity_svc, "_update_daily_stats", lambda *a, **k: None)
        _, community_id = community_db

        response = await client.post(
            "/api/v1/internal/activity/batch",
            headers={**service_key_headers, "Content-Type": "application/json"},
            data=json_module.dumps(
                {
                    "events": [
                        {
                            "type": "message",
                            "communityId": community_id,
                            "platform": "twitch",
                            "platformUserId": "p1",
                        },
                        {"type": "not-a-message"},
                    ]
                }
            ),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True, "processed": 1, "failed": 0}

    async def test_batch_counts_failures_without_stubbing(
        self, client: Any, service_key_headers: Any, community_db: Any
    ) -> None:
        """No `_update_daily_stats` stub here -- its real Postgres-only `ON CONFLICT`.

        raises on sqlite, which `record_activity_batch`'s own `except Exception`
        catches and counts as `failed`. Exercises that error path for real,
        without needing a Postgres fixture.
        """
        _, community_id = community_db
        response = await client.post(
            "/api/v1/internal/activity/batch",
            headers={**service_key_headers, "Content-Type": "application/json"},
            data=json_module.dumps(
                {
                    "events": [
                        {
                            "type": "message",
                            "communityId": community_id,
                            "platform": "twitch",
                            "platformUserId": "p1",
                        }
                    ]
                }
            ),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True, "processed": 0, "failed": 1}

    async def test_close_stale_sessions_success_with_query_stubbed(
        self, client: Any, service_key_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        """`close_stale_watch_sessions`'s own `UPDATE ... RETURNING` +.

        `EXTRACT(EPOCH ...)` + `(... )::INTERVAL` cast are all Postgres-only
        syntax sqlite:memory can't parse -- `dal.executesql` is stubbed for
        just that call (same technique as `test_community_relay.py`), so the
        route wiring + `_update_daily_stats` fan-out loop (real code) still
        runs against a real sqlite `dal`.
        """
        import services.community_activity as activity_svc

        dal, community_id = community_db
        real_executesql = dal.executesql

        def fake_executesql(sql: str, placeholders: Any = None, **kwargs: Any) -> list[Any]:
            if "activity_watch_sessions" in sql and "RETURNING" in sql:
                return [(community_id, None, "p1", "streamer1", 42)]
            return real_executesql(sql, placeholders=placeholders, **kwargs)

        monkeypatch.setattr(dal, "executesql", fake_executesql)
        monkeypatch.setattr(activity_svc, "_update_daily_stats", lambda *a, **k: None)

        response = await client.post(
            "/api/v1/internal/activity/close-stale-sessions", headers=service_key_headers
        )
        assert response.status_code == 200
        assert (await response.get_json()) == {"success": True, "closedSessions": 1}

    async def test_watch_session_join_then_heartbeat(
        self, client: Any, service_key_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        join_payload = {
            "eventType": "join",
            "communityId": community_id,
            "platform": "twitch",
            "platformUserId": "p1",
            "channelId": "chan-1",
        }
        join_resp = await client.post(
            "/api/v1/internal/activity/watch-session",
            headers={**service_key_headers, "Content-Type": "application/json"},
            data=json_module.dumps(join_payload),
        )
        assert join_resp.status_code == 200
        assert dal(dal.activity_watch_sessions.community_id == community_id).count() == 1

        # A second "join" for the same session updates the existing row, not a new insert.
        rejoin_resp = await client.post(
            "/api/v1/internal/activity/watch-session",
            headers={**service_key_headers, "Content-Type": "application/json"},
            data=json_module.dumps(join_payload),
        )
        assert rejoin_resp.status_code == 200
        assert dal(dal.activity_watch_sessions.community_id == community_id).count() == 1

        heartbeat_resp = await client.post(
            "/api/v1/internal/activity/watch-session",
            headers={**service_key_headers, "Content-Type": "application/json"},
            data=json_module.dumps({**join_payload, "eventType": "heartbeat"}),
        )
        assert heartbeat_resp.status_code == 200

    async def test_watch_session_invalid_event_type_is_400(
        self, client: Any, service_key_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.post(
            "/api/v1/internal/activity/watch-session",
            headers={**service_key_headers, "Content-Type": "application/json"},
            data=json_module.dumps(
                {
                    "eventType": "not-a-real-event",
                    "communityId": community_id,
                    "platform": "twitch",
                    "platformUserId": "p1",
                    "channelId": "c1",
                }
            ),
        )
        assert response.status_code == 400

    async def test_watch_session_leave_with_query_stubbed(
        self, client: Any, service_key_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        """The "leave" branch's own `UPDATE ... RETURNING id, duration_seconds`.

        is Postgres-only syntax (see `test_close_stale_sessions_success_with_
        query_stubbed`'s matching comment) -- `dal.executesql` stubbed for
        just that call; `_update_daily_stats` (also Postgres-only `ON
        CONFLICT`) stubbed too, so the branch dispatch + duration handling
        (real code) still runs.
        """
        import services.community_activity as activity_svc

        dal, community_id = community_db
        real_executesql = dal.executesql

        def fake_executesql(sql: str, placeholders: Any = None, **kwargs: Any) -> list[Any]:
            if "activity_watch_sessions" in sql and "RETURNING" in sql:
                return [(1, 42)]
            return real_executesql(sql, placeholders=placeholders, **kwargs)

        monkeypatch.setattr(dal, "executesql", fake_executesql)
        monkeypatch.setattr(activity_svc, "_update_daily_stats", lambda *a, **k: None)

        payload = {
            "eventType": "leave",
            "communityId": community_id,
            "platform": "twitch",
            "platformUserId": "p1",
            "channelId": "chan-1",
        }
        leave_resp = await client.post(
            "/api/v1/internal/activity/watch-session",
            headers={**service_key_headers, "Content-Type": "application/json"},
            data=json_module.dumps(payload),
        )
        assert leave_resp.status_code == 200
        assert (await leave_resp.get_json()) == {"success": True}


class TestAdminLeaderboardConfigUpdate:
    async def test_update_success(self, client: Any, auth_headers: Any, community_db: Any) -> None:
        _, community_id = community_db
        response = await client.put(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/leaderboard-config",
            headers={
                **auth_headers(scope="community.activity:admin"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps(
                {"enabled_platforms": ["twitch", "discord"], "display_limit": 50}
            ),
        )
        assert response.status_code == 200
        assert (await response.get_json()) == {
            "success": True,
            "message": "Leaderboard configuration updated",
        }

        get_resp = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/leaderboard-config",
            headers=auth_headers(scope="community.activity:admin"),
        )
        assert (await get_resp.get_json())["config"]["display_limit"] == 50

    async def test_update_twice_hits_update_not_insert_branch(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        headers = {
            **auth_headers(scope="community.activity:admin"),
            "Content-Type": "application/json",
        }
        await client.put(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/leaderboard-config",
            headers=headers,
            data=json_module.dumps({"display_limit": 10}),
        )
        response = await client.put(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/leaderboard-config",
            headers=headers,
            data=json_module.dumps({"display_limit": 20}),
        )
        assert response.status_code == 200

    async def test_invalid_platform_is_400(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.put(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/leaderboard-config",
            headers={
                **auth_headers(scope="community.activity:admin"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"enabled_platforms": ["not-a-real-platform"]}),
        )
        assert response.status_code == 400
