"""`blueprints/v1/community_raffle.py` -- per-event-type sound + message-template config."""

from __future__ import annotations

import json as json_module
from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema
from werkzeug.datastructures import FileStorage

from blueprints.v1.community_raffle import raffle_bp
from tests.community_ids import UNKNOWN_PUBLIC_ID, install_community_ids, public_id


@pytest.fixture
def app(community_db: Any) -> Quart:
    dal, _ = community_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = dal
    install_community_ids(quart_app, dal)
    quart_app.register_blueprint(raffle_bp)
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Default the `community.raffles` two-gate Feature flag ON for every test in this file."""
    import blueprints.v1.community_raffle as raffle_module

    monkeypatch.setattr(raffle_module, "feature_enabled", AsyncMock(return_value=True))


def _admin(community_db: Any, community_id: int) -> str:
    """Admin URL prefix carrying the community's public UUID."""
    return f"/api/v1/admin/{public_id(community_db[0], community_id)}"


async def _put_json(
    client: Any, path: str, headers: dict[str, str], payload: dict[str, Any]
) -> Any:
    return await client.put(
        path,
        headers={**headers, "Content-Type": "application/json"},
        data=json_module.dumps(payload),
    )


class TestScopeAndTenant:
    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/raffle-customization",
            headers=auth_headers(scope="community.raffle:write"),
        )
        assert response.status_code == 403

    async def test_unknown_community_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            f"/api/v1/admin/{UNKNOWN_PUBLIC_ID}/raffle-customization",
            headers=auth_headers(scope="community.raffle:read"),
        )
        assert response.status_code == 404

    @pytest.mark.parametrize(
        "method,path_suffix",
        [
            ("PUT", "raffle-customization/raffle_start"),
            ("DELETE", "raffle-customization/raffle_start"),
            ("POST", "raffle-customization/raffle_start/upload"),
        ],
    )
    async def test_remaining_routes_404_on_unknown_community(
        self, client: Any, auth_headers: Any, method: str, path_suffix: str
    ) -> None:
        response = await client.open(
            f"/api/v1/admin/{UNKNOWN_PUBLIC_ID}/{path_suffix}",
            method=method,
            headers=auth_headers(scope="community.raffle:write"),
        )
        assert response.status_code == 404


class TestUpsertAndDelete:
    async def test_invalid_event_type_is_400(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await _put_json(
            client,
            f"{_admin(community_db, community_id)}/raffle-customization/not_a_real_event",
            auth_headers(scope="community.raffle:write"),
            {"message_template": "hi"},
        )
        assert response.status_code == 400

    async def test_upsert_then_list_then_delete(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        write_headers = auth_headers(scope="community.raffle:write")

        upsert_resp = await _put_json(
            client,
            f"{_admin(community_db, community_id)}/raffle-customization/raffle_winner",
            write_headers,
            {"message_template": "{{winner_name}} won {{prize_name}}!", "is_active": True},
        )
        assert upsert_resp.status_code == 200
        body = await upsert_resp.get_json()
        assert body["success"] is True
        assert body["customization"]["message_template"] == "{{winner_name}} won {{prize_name}}!"

        list_resp = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/raffle-customization",
            headers=auth_headers(scope="community.raffle:read"),
        )
        list_body = await list_resp.get_json()
        assert "raffle_winner" in list_body["customizations"]

        delete_resp = await client.delete(
            f"{_admin(community_db, community_id)}/raffle-customization/raffle_winner",
            headers=write_headers,
        )
        assert delete_resp.status_code == 200

        list_after = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/raffle-customization",
            headers=auth_headers(scope="community.raffle:read"),
        )
        assert "raffle_winner" not in (await list_after.get_json())["customizations"]

    async def test_delete_invalid_event_type_is_400(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.delete(
            f"{_admin(community_db, community_id)}/raffle-customization/not_a_real_event",
            headers=auth_headers(scope="community.raffle:write"),
        )
        assert response.status_code == 400


class TestUpload:
    async def test_upload_requires_a_file(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.post(
            f"{_admin(community_db, community_id)}/raffle-customization/raffle_start/upload",
            headers=auth_headers(scope="community.raffle:write"),
        )
        assert response.status_code == 400

    async def test_upload_rejects_bad_format(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.post(
            f"{_admin(community_db, community_id)}/raffle-customization/raffle_start/upload",
            headers=auth_headers(scope="community.raffle:write"),
            files={"sound": FileStorage(BytesIO(b"not-really-audio"), filename="clip.exe")},
        )
        assert response.status_code == 400

    async def test_upload_rejects_oversized_file(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        oversized = b"x" * (3 * 1024 * 1024)
        response = await client.post(
            f"{_admin(community_db, community_id)}/raffle-customization/raffle_start/upload",
            headers=auth_headers(scope="community.raffle:write"),
            files={"sound": FileStorage(BytesIO(oversized), filename="clip.mp3")},
        )
        assert response.status_code == 400
        assert "2MB" in (await response.get_json())["error"]["message"]

    async def test_upload_success(
        self, client: Any, auth_headers: Any, community_db: Any, tmp_path: Any, monkeypatch: Any
    ) -> None:
        import services.community_raffle as raffle_svc

        monkeypatch.setattr(raffle_svc, "_UPLOAD_BASE_DIR", str(tmp_path))
        _, community_id = community_db

        response = await client.post(
            f"{_admin(community_db, community_id)}/raffle-customization/raffle_start/upload",
            headers=auth_headers(scope="community.raffle:write"),
            files={"sound": FileStorage(BytesIO(b"real-ish-audio-bytes"), filename="clip.mp3")},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        assert body["customization"]["sound_format"] == "mp3"


#: Every JSON response read here must carry community ids as UUID strings (conftest guard).
UUID_COMMUNITY_RESPONSES = True
