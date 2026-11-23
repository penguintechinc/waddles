"""`blueprints/v1/passkey.py` -- the M1 self-service passkey (WebAuthn) group.

Only the credential-management routes (register options, list, delete) --
full registration-finish/login-finish flows need a real WebAuthn
authenticator ceremony (browser-side crypto), out of scope for a server-
only test; `services/passkey_service.py`'s option-generation and
challenge-store logic is exercised directly instead.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.passkey import passkey_bp
from tests.conftest import TENANT_SLUG, make_user_token


@pytest.fixture
def app(auth_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(passkey_bp)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _seed_user(auth_db: Any) -> int:
    user_id: int = auth_db.dal.hub_users.insert(
        email="alice@example.com", username="alice", is_active=True, created_at=datetime.now(UTC)
    )
    auth_db.dal.commit()
    return user_id


_UNKNOWN_UUID = "00000000-0000-4000-8000-0000000000a1"


def _seed_passkey(auth_db: Any, user_id: int, credential_id: str) -> str:
    """Insert a passkey row for `user_id`; returns its public UUID."""
    public_id = str(uuid.uuid4())
    auth_db.dal.user_passkeys.insert(
        user_id=user_id,
        credential_id=credential_id,
        public_key="pk",
        device_name="laptop",
        created_at=datetime.now(UTC),
        public_id=public_id,
    )
    auth_db.dal.commit()
    return public_id


def _headers(user_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_user_token(user_id=user_id, tenant=TENANT_SLUG)}"}


class TestRegistrationStart:
    async def test_start_registration_without_token_is_401(self, client: Any) -> None:
        response = await client.post("/api/v1/user/passkey/register/start")
        assert response.status_code == 401

    async def test_start_registration_returns_webauthn_options(
        self, client: Any, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db)
        response = await client.post(
            "/api/v1/user/passkey/register/start", headers=_headers(user_id)
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        assert "challenge" in body["options"]
        assert body["options"]["rp"]["name"]


class TestListCredentials:
    async def test_list_credentials_empty(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user(auth_db)
        response = await client.get("/api/v1/user/passkey/credentials", headers=_headers(user_id))
        assert response.status_code == 200
        body = await response.get_json()
        assert body["credentials"] == []

    async def test_list_credentials_without_token_is_401(self, client: Any) -> None:
        response = await client.get("/api/v1/user/passkey/credentials")
        assert response.status_code == 401


class TestRemoveCredential:
    @pytest.mark.parametrize("bad", ["9999", "not-a-uuid", _UNKNOWN_UUID])
    async def test_numeric_malformed_and_unknown_credential_ids_are_404(
        self, client: Any, auth_db: Any, bad: str
    ) -> None:
        """Only a UUID of the caller's OWN passkey resolves; everything else is the same 404."""
        user_id = _seed_user(auth_db)
        response = await client.delete(
            f"/api/v1/user/passkey/credentials/{bad}", headers=_headers(user_id)
        )
        assert response.status_code == 404

    async def test_remove_credential_without_token_is_401(self, client: Any) -> None:
        response = await client.delete(f"/api/v1/user/passkey/credentials/{_UNKNOWN_UUID}")
        assert response.status_code == 401

    async def test_list_emits_uuid_and_delete_by_uuid_removes_it(
        self, client: Any, auth_db: Any
    ) -> None:
        user_id = _seed_user(auth_db)
        public_id = _seed_passkey(auth_db, user_id, "cred-a")
        response = await client.get("/api/v1/user/passkey/credentials", headers=_headers(user_id))
        listed = (await response.get_json())["credentials"]
        assert [c["id"] for c in listed] == [public_id]
        response = await client.delete(
            f"/api/v1/user/passkey/credentials/{public_id}", headers=_headers(user_id)
        )
        assert response.status_code == 200
        assert auth_db.dal(auth_db.dal.user_passkeys.public_id == public_id).count() == 0

    async def test_another_users_credential_uuid_is_404_and_survives(
        self, client: Any, auth_db: Any
    ) -> None:
        owner = _seed_user(auth_db)
        public_id = _seed_passkey(auth_db, owner, "cred-b")
        intruder: int = auth_db.dal.hub_users.insert(
            email="mallory@example.com",
            username="mallory",
            is_active=True,
            created_at=datetime.now(UTC),
        )
        auth_db.dal.commit()
        response = await client.delete(
            f"/api/v1/user/passkey/credentials/{public_id}", headers=_headers(intruder)
        )
        assert response.status_code == 404
        assert auth_db.dal(auth_db.dal.user_passkeys.public_id == public_id).count() == 1
