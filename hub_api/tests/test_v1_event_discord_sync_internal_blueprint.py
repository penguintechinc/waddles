"""`blueprints/v1/event_discord_sync.py` -- the internal guild-pairings event-sync toggle route.

Covers only the route THIS PR adds (`POST .../calendar/guild-pairings/
event-sync`) -- the `sync-discord` route is already covered end-to-end by
`test_event_discord_sync_service.py` (service-layer) and doesn't need a
second blueprint-level test here. Standalone Quart app registering
`event_discord_sync_internal_bp` against the `event_sync_db` fixture
(`tests/conftest.py`), same service-key-only auth pattern `test_v1_
community_music_queue_internal.py` uses for this shape of route.

POSTs via `data=json_module.dumps(...)` + an explicit `Content-Type`
header, not the test client's `json=` kwarg -- same workaround `test_v1_
guild_pairing_blueprint.py` uses (the installed `quart_schema`/`pydantic`
pair's client-side `json=` encoding path raises on a plain `dict` body).
"""

from __future__ import annotations

import json as json_module
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.event_discord_sync import event_discord_sync_internal_bp
from services.guild_pairing import create_pairing, list_pairings

SERVICE_API_KEY = "test-service-key"
_ROUTE = "/api/v1/internal/calendar/guild-pairings/event-sync"


@pytest.fixture
def app(event_sync_db: Any, monkeypatch: pytest.MonkeyPatch) -> Quart:
    monkeypatch.setenv("SERVICE_API_KEY", SERVICE_API_KEY)
    dal, _community_id, _tenant_id, _global_id, _event_id = event_sync_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(event_discord_sync_internal_bp)
    quart_app.config["dal"] = dal
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture
def service_key_headers() -> dict[str, str]:
    return {"X-Service-Key": SERVICE_API_KEY, "Content-Type": "application/json"}


async def _post(client: Any, headers: dict[str, str], body: dict[str, Any]) -> Any:
    return await client.post(_ROUTE, headers=headers, data=json_module.dumps(body))


class TestAuth:
    async def test_missing_service_key_is_401(self, client: Any, event_sync_db: Any) -> None:
        _, community_id, *_rest = event_sync_db
        response = await _post(
            client,
            {"Content-Type": "application/json"},
            {"community_id": community_id, "enabled": True},
        )
        assert response.status_code == 401

    async def test_wrong_service_key_is_401(self, client: Any, event_sync_db: Any) -> None:
        _, community_id, *_rest = event_sync_db
        response = await _post(
            client,
            {"X-Service-Key": "wrong", "Content-Type": "application/json"},
            {"community_id": community_id, "enabled": True},
        )
        assert response.status_code == 401


class TestValidation:
    async def test_missing_community_id_is_400(
        self, client: Any, service_key_headers: dict[str, str]
    ) -> None:
        response = await _post(client, service_key_headers, {"enabled": True})
        assert response.status_code == 400

    async def test_non_bool_enabled_is_400(
        self, client: Any, service_key_headers: dict[str, str], event_sync_db: Any
    ) -> None:
        _, community_id, *_rest = event_sync_db
        response = await _post(
            client, service_key_headers, {"community_id": community_id, "enabled": "yes"}
        )
        assert response.status_code == 400

    async def test_no_pairings_for_community_is_404_not_a_silent_noop(
        self, client: Any, service_key_headers: dict[str, str], event_sync_db: Any
    ) -> None:
        _, community_id, *_rest = event_sync_db
        response = await _post(
            client, service_key_headers, {"community_id": community_id, "enabled": True}
        )
        assert response.status_code == 404


class TestHappyPath:
    async def test_enables_every_pairing_under_the_community(
        self, client: Any, service_key_headers: dict[str, str], event_sync_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id, _event_id = event_sync_db
        create_pairing(
            dal,
            community_id,
            discord_guild_id="111111111111111111",
            direction="bidirectional",
            role_name_prefix="[A]",
            actor_user_id=1,
        )
        create_pairing(
            dal,
            community_id,
            discord_guild_id="222222222222222222",
            direction="bidirectional",
            role_name_prefix="[B]",
            actor_user_id=1,
        )

        response = await _post(
            client, service_key_headers, {"community_id": community_id, "enabled": True}
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["event_sync_enabled"] is True
        assert body["data"]["pairings_updated"] == 2

        pairings = list_pairings(dal, community_id)
        assert len(pairings) == 2
        assert all(p.event_sync_enabled for p in pairings)

    async def test_disables_every_pairing_under_the_community(
        self, client: Any, service_key_headers: dict[str, str], event_sync_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id, _event_id = event_sync_db
        create_pairing(
            dal,
            community_id,
            discord_guild_id="333333333333333333",
            direction="bidirectional",
            role_name_prefix="[C]",
            event_sync_enabled=True,
            actor_user_id=1,
        )

        response = await _post(
            client, service_key_headers, {"community_id": community_id, "enabled": False}
        )
        assert response.status_code == 200

        pairings = list_pairings(dal, community_id)
        assert all(not p.event_sync_enabled for p in pairings)

    async def test_other_communitys_pairings_are_untouched(
        self, client: Any, service_key_headers: dict[str, str], event_sync_db: Any
    ) -> None:
        dal, community_id, tenant_id, _global_id, _event_id = event_sync_db
        other_community_id = dal.communities.insert(name="other-community", tenant_id=tenant_id)
        dal.commit()
        create_pairing(
            dal,
            community_id,
            discord_guild_id="444444444444444444",
            direction="bidirectional",
            role_name_prefix="[D]",
            actor_user_id=1,
        )
        create_pairing(
            dal,
            other_community_id,
            discord_guild_id="555555555555555555",
            direction="bidirectional",
            role_name_prefix="[E]",
            actor_user_id=1,
        )

        response = await _post(
            client, service_key_headers, {"community_id": community_id, "enabled": True}
        )
        assert response.status_code == 200

        other_pairings = list_pairings(dal, other_community_id)
        assert all(not p.event_sync_enabled for p in other_pairings)
