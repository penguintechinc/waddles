"""`blueprints/v1/community_forms.py` -- pure reverse-proxy to `core-engagement`.

Same pattern as `test_community_polls.py` -- see its module docstring.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.community_forms import forms_bp
from services import community_engagement_proxy as proxy
from tests.community_ids import UNKNOWN_PUBLIC_ID, install_community_ids, public_id


@pytest.fixture
def app(community_db: Any) -> Quart:
    dal, _ = community_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    install_community_ids(quart_app, dal)
    quart_app.register_blueprint(forms_bp)
    quart_app.config["dal"] = dal
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Default the `community.forms` two-gate Feature flag ON for every test in this file."""
    import blueprints.v1.community_forms as forms_module

    monkeypatch.setattr(forms_module, "feature_enabled", AsyncMock(return_value=True))


class TestScopeAndTenant:
    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/forms",
            headers=auth_headers(scope="community.forms:write"),
        )
        assert response.status_code == 403

    async def test_unknown_community_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            f"/api/v1/admin/{UNKNOWN_PUBLIC_ID}/forms",
            headers=auth_headers(scope="community.forms:read"),
        )
        assert response.status_code == 404

    async def test_numeric_community_id_is_not_routed(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        """Community routes take the public UUID; the numeric id no longer matches `cuuid`."""
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/admin/{community_id}/forms",
            headers=auth_headers(scope="community.forms:read"),
        )
        assert response.status_code == 404


class TestProxyPassthrough:
    async def test_form_submissions_empty_on_404_from_downstream(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db

        async def fake_get_submissions(
            cid: int, fid: int, authorization: str | None
        ) -> tuple[dict[str, Any], int]:
            return {"success": True, "submissions": []}, 200

        monkeypatch.setattr(proxy, "get_form_submissions", fake_get_submissions)

        response = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/forms/7/submissions",
            headers=auth_headers(scope="community.forms:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True, "submissions": []}

    async def test_list_forms_forwards(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db

        async def fake_get_forms(cid: int, authorization: str | None) -> tuple[dict[str, Any], int]:
            return {"success": True, "forms": [{"id": 1, "title": "Feedback"}]}, 200

        monkeypatch.setattr(proxy, "get_forms", fake_get_forms)
        response = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/forms",
            headers=auth_headers(scope="community.forms:read"),
        )
        assert response.status_code == 200
        assert (await response.get_json())["forms"][0]["title"] == "Feedback"

    async def test_get_single_form_forwards(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db

        async def fake_get_form(
            cid: int, fid: int, authorization: str | None
        ) -> tuple[dict[str, Any], int]:
            return {"success": True, "form": {"id": fid}}, 200

        monkeypatch.setattr(proxy, "get_form", fake_get_form)
        response = await client.get(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/forms/9",
            headers=auth_headers(scope="community.forms:read"),
        )
        assert response.status_code == 200
        assert (await response.get_json())["form"]["id"] == 9

    async def test_create_form_forwards(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        import json as json_module

        _, community_id = community_db

        async def fake_create_form(
            cid: int, payload: dict[str, Any], authorization: str | None
        ) -> tuple[dict[str, Any], int]:
            return {"success": True, "form": {"id": 1, "title": payload["title"]}}, 200

        monkeypatch.setattr(proxy, "create_form", fake_create_form)
        response = await client.post(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/forms",
            headers={
                **auth_headers(scope="community.forms:write"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"title": "Signup", "fields": [{"name": "email"}]}),
        )
        assert response.status_code == 200
        assert (await response.get_json())["form"]["title"] == "Signup"

    async def test_delete_form_forwards(
        self, client: Any, auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db

        async def fake_delete_form(
            cid: int, fid: int, authorization: str | None
        ) -> tuple[dict[str, Any], int]:
            return {"success": True, "message": "Form deleted"}, 200

        monkeypatch.setattr(proxy, "delete_form", fake_delete_form)
        response = await client.delete(
            f"/api/v1/admin/{public_id(community_db[0], community_id)}/forms/9",
            headers=auth_headers(scope="community.forms:write"),
        )
        assert response.status_code == 200
        assert (await response.get_json())["message"] == "Form deleted"


pytestmark = pytest.mark.usefixtures("bypass_route_guards")
