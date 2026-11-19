"""`blueprints/v1/data_privacy.py` -- the Privacy/Compliance GDPR DSAR group.

Standalone Quart app registering only `data_privacy_bp`, matching
`test_v1_profile_blueprint.py`'s pattern (`privacy_db` fixture instead of
`auth_db` -- this group's own tables plus M1's).

Fail-first proof for the IDOR/BOLA regression tests (executed, not
narrated): `test_export_returns_only_callers_own_data` and
`test_deletion_only_affects_callers_own_account` were run once against a
deliberately-broken `export_user_data()`/`request_data_deletion()` that
took `user_id` from a `request.args.get("user_id")` query param instead
of `get_current_user_id(request)` -- both went red (Bob's export/deletion
call, still carrying Bob's own token, returned/deleted Alice's data via
`?user_id=<alice>`); reverted to the real, JWT-only implementation,
confirmed green again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import bcrypt
import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.data_privacy import data_privacy_bp
from tests.conftest import TENANT_SLUG, make_user_token


@pytest.fixture
def app(privacy_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(data_privacy_bp)
    quart_app.config["dal"] = privacy_db.dal
    quart_app.config["async_dal"] = privacy_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _seed_user(
    privacy_db: Any,
    *,
    email: str = "alice@example.com",
    username: str = "alice",
    password_hash: str | None = None,
) -> int:
    user_id: int = privacy_db.dal.hub_users.insert(
        email=email,
        username=username,
        password_hash=password_hash,
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    privacy_db.dal.commit()
    return user_id


def _headers(user_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_user_token(user_id=user_id, tenant=TENANT_SLUG)}"}


class TestExportUserData:
    async def test_export_without_token_is_401(self, client: Any) -> None:
        response = await client.get("/api/v1/user/me/data")
        assert response.status_code == 401

    async def test_export_unknown_user_is_404(self, client: Any) -> None:
        response = await client.get("/api/v1/user/me/data", headers=_headers(999))
        assert response.status_code == 404

    async def test_export_returns_only_callers_own_data(self, client: Any, privacy_db: Any) -> None:
        """The core IDOR/BOLA regression -- see module docstring for the fail-first proof."""
        alice_id = _seed_user(privacy_db, email="alice@example.com", username="alice")
        bob_id = _seed_user(privacy_db, email="bob@example.com", username="bob")

        response = await client.get("/api/v1/user/me/data", headers=_headers(bob_id))
        assert response.status_code == 200
        body = await response.get_json()

        assert body["subject_id"] == bob_id
        account_rows = body["data"]["account"]
        assert len(account_rows) == 1
        assert account_rows[0]["id"] == bob_id
        assert account_rows[0]["email"] == "bob@example.com"
        assert all(row["id"] != alice_id for row in account_rows)

    async def test_export_never_discloses_credential_material(
        self, client: Any, privacy_db: Any
    ) -> None:
        user_id = _seed_user(privacy_db, password_hash="$2b$12$notarealhash")
        response = await client.get("/api/v1/user/me/data", headers=_headers(user_id))
        assert response.status_code == 200
        body = await response.get_json()
        assert "password_hash" not in body["data"]["account"][0]

    async def test_export_emits_community_public_ids_never_numeric(
        self, client: Any, privacy_db: Any
    ) -> None:
        """Regression: the self-service export must not leak the numeric community PK."""
        user_id = _seed_user(privacy_db)
        dal = privacy_db.dal
        tenant = dal(dal.tenants.slug == TENANT_SLUG).select().first()
        public_id = "5b0e2f0a-3c1d-4a55-9d0e-0f6a5c3d7e11"
        community_id = dal.communities.insert(
            name="c-one", display_name="c-one", tenant_id=tenant.id, public_id=public_id
        )
        dal.hub_chat_messages.insert(
            community_id=community_id,
            channel_name="general",
            sender_hub_user_id=user_id,
            sender_platform="discord",
            sender_username="alice",
            message_content="hello",
            message_type="text",
            created_at=datetime.now(UTC),
        )
        dal.commit()

        response = await client.get("/api/v1/user/me/data", headers=_headers(user_id))
        assert response.status_code == 200
        body = await response.get_json()

        keyed = [row for rows in body["data"].values() for row in rows if "community_id" in row]
        assert keyed, "no community-keyed rows examined: the regression proves nothing"
        assert all(not isinstance(row["community_id"], int) for row in keyed)
        assert {row["community_id"] for row in keyed} == {public_id}

    async def test_export_sets_content_disposition_header(
        self, client: Any, privacy_db: Any
    ) -> None:
        user_id = _seed_user(privacy_db)
        response = await client.get("/api/v1/user/me/data", headers=_headers(user_id))
        assert f"waddles-data-{user_id}.json" in response.headers["Content-Disposition"]


class TestRequestDataDeletion:
    async def test_deletion_without_token_is_401(self, client: Any) -> None:
        response = await client.delete("/api/v1/user/me/data")
        assert response.status_code == 401

    async def test_deletion_unknown_user_is_404(self, client: Any) -> None:
        response = await client.delete("/api/v1/user/me/data", headers=_headers(999), json={})
        assert response.status_code == 404

    async def test_deletion_without_password_when_required_is_400(
        self, client: Any, privacy_db: Any
    ) -> None:
        user_id = _seed_user(privacy_db, password_hash="$2b$12$notarealhash")
        response = await client.delete("/api/v1/user/me/data", headers=_headers(user_id), json={})
        assert response.status_code == 400

    async def test_deletion_wrong_password_is_401(self, client: Any, privacy_db: Any) -> None:
        real_hash = bcrypt.hashpw(b"correct-horse", bcrypt.gensalt(rounds=4)).decode()
        user_id = _seed_user(privacy_db, password_hash=real_hash)
        response = await client.delete(
            "/api/v1/user/me/data", headers=_headers(user_id), json={"password": "wrong"}
        )
        assert response.status_code == 401

    async def test_deletion_succeeds_without_password_when_none_set(
        self, client: Any, privacy_db: Any
    ) -> None:
        user_id = _seed_user(privacy_db, password_hash=None)
        response = await client.delete("/api/v1/user/me/data", headers=_headers(user_id), json={})
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True, "deleted": True}

    async def test_deletion_anonymizes_hub_users_row(self, client: Any, privacy_db: Any) -> None:
        user_id = _seed_user(privacy_db, email="alice@example.com", username="alice")
        response = await client.delete("/api/v1/user/me/data", headers=_headers(user_id), json={})
        assert response.status_code == 200

        # `update_async()` never calls `.commit()` -- assert via the SAME
        # (worker-thread) connection the write happened on, not the
        # fixture's own synchronous `privacy_db.dal(...)` (a different
        # connection, main thread) -- Gotcha #2, hub_api/PORTING.md.
        rows = await privacy_db.select_async(privacy_db.dal(privacy_db.dal.hub_users.id == user_id))
        row = rows.first()
        assert row.email == f"deleted_{user_id}@deleted.waddlebot"
        assert row.username == f"deleted_{user_id}"
        assert row.is_active is False

    async def test_deletion_already_deleted_short_circuits(
        self, client: Any, privacy_db: Any
    ) -> None:
        user_id = _seed_user(privacy_db)
        # Match the exact `deleted_{user_id}@...` shape the anonymization
        # step writes -- can't know `user_id` before the insert above.
        privacy_db.dal(privacy_db.dal.hub_users.id == user_id).update(
            email=f"deleted_{user_id}@deleted.waddlebot", username=f"deleted_{user_id}"
        )
        privacy_db.dal.commit()
        response = await client.delete("/api/v1/user/me/data", headers=_headers(user_id), json={})
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True, "already_deleted": True}

    async def test_deletion_only_affects_callers_own_account(
        self, client: Any, privacy_db: Any
    ) -> None:
        """The erasure-side IDOR/BOLA regression -- see module docstring."""
        alice_id = _seed_user(privacy_db, email="alice@example.com", username="alice")
        bob_id = _seed_user(privacy_db, email="bob@example.com", username="bob")

        response = await client.delete("/api/v1/user/me/data", headers=_headers(bob_id), json={})
        assert response.status_code == 200

        # See test_deletion_anonymizes_hub_users_row's comment -- same
        # connection-visibility requirement applies here.
        alice_rows = await privacy_db.select_async(
            privacy_db.dal(privacy_db.dal.hub_users.id == alice_id)
        )
        alice_row = alice_rows.first()
        assert alice_row.email == "alice@example.com"
        assert alice_row.is_active is True

        bob_rows = await privacy_db.select_async(
            privacy_db.dal(privacy_db.dal.hub_users.id == bob_id)
        )
        assert bob_rows.first().email == f"deleted_{bob_id}@deleted.waddlebot"

    async def test_deletion_writes_audit_record(self, client: Any, privacy_db: Any) -> None:
        user_id = _seed_user(privacy_db)
        response = await client.delete("/api/v1/user/me/data", headers=_headers(user_id), json={})
        assert response.status_code == 200

        rows = await privacy_db.select_async(
            privacy_db.dal(privacy_db.dal.data_deletion_requests.hub_user_id == user_id)
        )
        assert len(rows) == 1
        assert rows.first().status == "completed"
