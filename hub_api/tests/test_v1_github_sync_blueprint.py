"""`blueprints/v1/github_sync.py` -- ported from `githubSyncController.js`/`routes/githubSync.js`.

Standalone Quart app (mirrors `test_v1_workflow_blueprint.py`'s own
pattern) registering both `github_sync_webhook_bp` and
`github_sync_admin_bp`. `services.github_sync_service._github_request`
(the ONLY function that ever talks to `api.github.com`) is monkeypatched
per-test -- these tests exercise hub-api's OWN logic (community authz,
repo-coordinate validation, the `vendor_id` IDOR fix, HMAC webhook
verification), never a real GitHub API call.

Fail-first proof (executed, not narrated) for SECURITY FIX (1) (repo
coordinate validation): temporarily commented out the
`_validate_repo_coordinates()` call in `services/github_sync_service.py
::create_repo_connection()`. `test_create_connection_rejects_malformed_owner`
went red (201 instead of 400 -- a `repo_owner` containing a `/` was
accepted and stored verbatim). Reverted, green again, full file green.
"""

from __future__ import annotations

import hmac
import json
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

import services.github_sync_service as svc
from blueprints.v1.github_sync import github_sync_admin_bp, github_sync_webhook_bp

COMMUNITY_ID = 1
OTHER_COMMUNITY_ID = 2
ADMIN_USER_ID = 42
NON_ADMIN_USER_ID = 99
ENCRYPTION_KEY = "00" * 32  # 64 hex chars -- 32-byte AES-256 key, test-only


@pytest.fixture(autouse=True)
def _encryption_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SYNC_ENCRYPTION_KEY", ENCRYPTION_KEY)


@pytest.fixture
def app(automation_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(github_sync_webhook_bp)
    quart_app.register_blueprint(github_sync_admin_bp)
    quart_app.config["dal"] = automation_db.dal
    quart_app.config["async_dal"] = automation_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _seed_admin(
    automation_db: Any, *, user_id: int, community_id: int, role: str = "admin"
) -> None:
    dal = automation_db.dal
    if not dal(dal.communities.id == community_id).count():
        dal.communities.insert(id=community_id, name=f"community-{community_id}")
    role_id = dal.community_roles.insert(
        community_id=community_id, name=role, base_claims={"scopes": ["community:manage_members"]}
    )
    dal.community_members.insert(
        community_id=community_id,
        user_id=str(user_id),
        role=role,
        is_active=True,
        community_role_id=role_id,
    )
    dal.commit()


class TestAuthBypass:
    async def test_missing_token_is_401(self, client: Any) -> None:
        response = await client.get(f"/api/v1/{COMMUNITY_ID}/github-sync/connections")
        assert response.status_code == 401


class TestCommunityAdminAuthz:
    async def test_non_admin_is_403(self, client: Any, user_auth_headers: Any) -> None:
        headers = user_auth_headers(user_id=NON_ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/{COMMUNITY_ID}/github-sync/connections", headers=headers
        )
        assert response.status_code == 403

    async def test_admin_can_list_empty_connections(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/{COMMUNITY_ID}/github-sync/connections", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"status": "success", "data": []}


class TestCreateConnection:
    async def test_non_admin_is_403(self, client: Any, user_auth_headers: Any) -> None:
        headers = user_auth_headers(user_id=NON_ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/{COMMUNITY_ID}/github-sync/connections", headers=headers, json={}
        )
        assert response.status_code == 403

    async def test_missing_fields_is_400(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/{COMMUNITY_ID}/github-sync/connections", headers=headers, json={}
        )
        assert response.status_code == 400

    async def test_rejects_malformed_owner(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        """SECURITY FIX (1) -- see module docstring's fail-first proof."""
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/{COMMUNITY_ID}/github-sync/connections",
            headers=headers,
            json={
                "repo_owner": "not/a/valid/owner",
                "repo_name": "repo",
                "auth_type": "pat",
                "token": "ghp_secret",
            },
        )
        assert response.status_code == 400

    async def test_creates_connection_ignoring_body_vendor_id(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        """SECURITY FIX (2) -- a caller-supplied `vendor_id` is never honored."""
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/{COMMUNITY_ID}/github-sync/connections",
            headers=headers,
            json={
                "repo_owner": "penguintechinc",
                "repo_name": "waddlebot",
                "auth_type": "pat",
                "token": "ghp_supersecrettoken1234",
                "vendor_id": 9999,  # attacker-supplied -- must be ignored
            },
        )
        assert response.status_code == 201
        body = await response.get_json()
        conn = body["data"]
        assert conn["community_id"] == COMMUNITY_ID
        assert conn["vendor_id"] is None
        assert conn["token_masked"] == "****1234"
        assert "encrypted_token" not in conn
        # regression: one-time reveal on create only; never in list
        assert conn["webhook_secret"]
        assert conn["webhook_secret_set"] is True
        listed = await client.get(
            f"/api/v1/{COMMUNITY_ID}/github-sync/connections", headers=headers
        )
        assert listed.status_code == 200
        rows = (await listed.get_json())["data"]
        assert len(rows) == 1
        for row in rows:
            assert "webhook_secret" not in row
            assert row["webhook_secret_set"] is True
        assert conn["webhook_secret"] not in json.dumps(rows)


class TestDeleteConnection:
    async def test_non_admin_is_403(self, client: Any, user_auth_headers: Any) -> None:
        headers = user_auth_headers(user_id=NON_ADMIN_USER_ID)
        response = await client.delete(
            f"/api/v1/{COMMUNITY_ID}/github-sync/connections/1", headers=headers
        )
        assert response.status_code == 403

    async def test_non_numeric_connection_id_is_400(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.delete(
            f"/api/v1/{COMMUNITY_ID}/github-sync/connections/not-a-number", headers=headers
        )
        assert response.status_code == 400

    async def test_delete_not_found_is_404(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.delete(
            f"/api/v1/{COMMUNITY_ID}/github-sync/connections/9999", headers=headers
        )
        assert response.status_code == 404

    async def test_delete_cross_community_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        """`delete_repo_connection()` re-verifies against the row's REAL community_id."""
        dal = automation_db.dal
        dal.communities.insert(id=OTHER_COMMUNITY_ID, name="other")
        conn_id = dal.github_repo_connections.insert(
            community_id=OTHER_COMMUNITY_ID,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token="x",
            webhook_secret="y",
            is_active=True,
        )
        dal.commit()
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.delete(
            f"/api/v1/{COMMUNITY_ID}/github-sync/connections/{conn_id}", headers=headers
        )
        assert response.status_code == 403

    async def test_delete_succeeds_for_admin_role(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        dal = automation_db.dal
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID, role="admin")
        conn_id = dal.github_repo_connections.insert(
            community_id=COMMUNITY_ID,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token="x",
            webhook_secret="y",
            is_active=True,
        )
        dal.commit()
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.delete(
            f"/api/v1/{COMMUNITY_ID}/github-sync/connections/{conn_id}", headers=headers
        )
        assert response.status_code == 200
        # `update_async()` never calls `.commit()` (hub_api/PORTING.md
        # Gotcha #3) -- assert via `select_async` (same executor connection
        # as the write), not a bare sync `dal(...)` call on the main
        # thread's own connection, which would see the pre-update row.
        rows = await automation_db.select_async(dal(dal.github_repo_connections.id == conn_id))
        assert rows.first().is_active is False


class TestSyncStatus:
    async def test_non_admin_is_403(self, client: Any, user_auth_headers: Any) -> None:
        headers = user_auth_headers(user_id=NON_ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/{COMMUNITY_ID}/github-sync/ticket/1/sync-status", headers=headers
        )
        assert response.status_code == 403

    async def test_invalid_ticket_id_is_400(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/{COMMUNITY_ID}/github-sync/ticket/not-a-number/sync-status", headers=headers
        )
        assert response.status_code == 400

    async def test_returns_sync_records(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        dal = automation_db.dal
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        conn_id = dal.github_repo_connections.insert(
            community_id=COMMUNITY_ID,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token="x",
            webhook_secret="y",
            is_active=True,
        )
        dal.ticket_github_sync.insert(
            ticket_id=7,
            github_repo_connection_id=conn_id,
            github_issue_number=12,
            sync_status="synced",
        )
        dal.commit()
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/{COMMUNITY_ID}/github-sync/ticket/7/sync-status", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert len(body["data"]) == 1
        assert body["data"][0]["repo_owner"] == "owner"


class TestTriggerSync:
    async def test_non_admin_is_403(self, client: Any, user_auth_headers: Any) -> None:
        headers = user_auth_headers(user_id=NON_ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/{COMMUNITY_ID}/github-sync/ticket/1/sync", headers=headers, json={}
        )
        assert response.status_code == 403

    async def test_invalid_ticket_id_is_400(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/{COMMUNITY_ID}/github-sync/ticket/not-a-number/sync",
            headers=headers,
            json={"repo_connection_id": 1},
        )
        assert response.status_code == 400

    async def test_non_numeric_repo_connection_id_is_400(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/{COMMUNITY_ID}/github-sync/ticket/1/sync",
            headers=headers,
            json={"repo_connection_id": "not-a-number"},
        )
        assert response.status_code == 400

    async def test_missing_repo_connection_id_is_400(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/{COMMUNITY_ID}/github-sync/ticket/1/sync", headers=headers, json={}
        )
        assert response.status_code == 400

    async def test_service_error_is_propagated(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        """`sync_ticket_to_github()` raising `ApiError` (e.g. ticket not found) -> `_err()`."""
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/{COMMUNITY_ID}/github-sync/ticket/9999/sync",
            headers=headers,
            json={"repo_connection_id": 1},
        )
        assert response.status_code == 404

    async def test_syncs_ticket_to_github(
        self,
        client: Any,
        user_auth_headers: Any,
        automation_db: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        dal = automation_db.dal
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        ticket_id = dal.support_tickets.insert(
            subject="Help!", description="It broke", status="open", priority="high"
        )
        conn_id = dal.github_repo_connections.insert(
            community_id=COMMUNITY_ID,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token=svc.encrypt_token("ghp_real_token"),
            webhook_secret="y",
            is_active=True,
        )
        dal.commit()

        async def fake_github_request(
            method: str, path: str, token: str, *, json_body: Any = None
        ) -> Any:
            assert token == "ghp_real_token"
            return True, 201, {"number": 55, "node_id": "MDU6SXNzdWU1NQ=="}

        monkeypatch.setattr(svc, "_github_request", fake_github_request)

        # `json=` routes through quart-schema's test-client `model_dump()` ->
        # `TypeAdapter(dict).dump_python()` -- hits the SAME pydantic-core
        # crash class `services/dto_response.py`'s module docstring documents
        # for route RESPONSES (`TypeError: 'None' is not an instance of
        # 'SchemaSerializer'`), here on the REQUEST-encoding side of the test
        # client instead. Raw `data=` bytes + an explicit content-type header
        # bypass quart-schema's `json=` handling entirely -- the same
        # workaround shape as `jsonify_dto()`, applied at the test-client
        # call site since there's no server-side code path to fix here.
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        headers["Content-Type"] = "application/json"
        response = await client.post(
            f"/api/v1/{COMMUNITY_ID}/github-sync/ticket/{ticket_id}/sync",
            headers=headers,
            data=json.dumps({"repo_connection_id": conn_id}).encode(),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["github_issue_number"] == 55
        assert body["data"]["sync_status"] == "synced"


class TestWebhookReceiver:
    async def test_missing_event_header_is_400(self, client: Any) -> None:
        response = await client.post("/api/v1/github-sync/webhook", data=b"{}")
        assert response.status_code == 400

    async def test_missing_signature_header_is_400(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/github-sync/webhook",
            data=b"{}",
            headers={"X-GitHub-Event": "issues"},
        )
        assert response.status_code == 400

    async def test_invalid_json_is_400(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/github-sync/webhook",
            data=b"not json",
            headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": "sha256=deadbeef"},
        )
        assert response.status_code == 400

    async def test_missing_repository_info_is_400(self, client: Any) -> None:
        response = await client.post(
            "/api/v1/github-sync/webhook",
            data=json.dumps({"repository": {"owner": {}}}).encode(),
            headers={
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": "sha256=deadbeef",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 400

    async def test_invalid_signature_is_rejected(self, client: Any, automation_db: Any) -> None:
        dal = automation_db.dal
        dal.communities.insert(id=COMMUNITY_ID, name="community-1")
        dal.github_repo_connections.insert(
            community_id=COMMUNITY_ID,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token="x",
            webhook_secret="real-secret",
            is_active=True,
        )
        dal.commit()

        payload = json.dumps({"repository": {"owner": {"login": "owner"}, "name": "repo"}}).encode()
        response = await client.post(
            "/api/v1/github-sync/webhook",
            data=payload,
            headers={
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": "sha256=" + "0" * 64,  # wrong secret
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 401

    async def test_valid_signature_closes_ticket_on_issue_closed(
        self, client: Any, automation_db: Any
    ) -> None:
        dal = automation_db.dal
        dal.communities.insert(id=COMMUNITY_ID, name="community-1")
        conn_id = dal.github_repo_connections.insert(
            community_id=COMMUNITY_ID,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token="x",
            webhook_secret="real-secret",
            auto_close_on_github_close=True,
            is_active=True,
        )
        ticket_id = dal.support_tickets.insert(
            subject="s", description="d", status="open", priority="normal"
        )
        dal.ticket_github_sync.insert(
            ticket_id=ticket_id,
            github_repo_connection_id=conn_id,
            github_issue_number=42,
            sync_status="synced",
        )
        dal.commit()

        payload = json.dumps(
            {
                "repository": {"owner": {"login": "owner"}, "name": "repo"},
                "action": "closed",
                "issue": {"number": 42},
            }
        ).encode()
        signature = "sha256=" + hmac.new(b"real-secret", payload, "sha256").hexdigest()

        response = await client.post(
            "/api/v1/github-sync/webhook",
            data=payload,
            headers={
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": signature,
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200
        # See `test_delete_succeeds_for_admin_role`'s comment -- Gotcha #3,
        # assert via `select_async` (same connection as the `update_async` write).
        rows = await automation_db.select_async(dal(dal.support_tickets.id == ticket_id))
        assert rows.first().status == "closed"


class TestRepoCoordinateValidation:
    """`github_sync_service._validate_repo_coordinates` -- SECURITY FIX (1) unit coverage."""

    def test_valid_owner_and_repo_accepted(self) -> None:
        svc._validate_repo_coordinates("penguintechinc", "waddlebot")  # no raise

    def test_owner_with_path_separator_rejected(self) -> None:
        with pytest.raises(Exception, match="repo_owner"):
            svc._validate_repo_coordinates("a/b", "repo")

    def test_repo_name_too_long_rejected(self) -> None:
        with pytest.raises(Exception, match="repo_name"):
            svc._validate_repo_coordinates("owner", "x" * 101)


class TestTokenMasking:
    """`services.github_sync_service.mask_token`/`encrypt_token`/`decrypt_token` round trip."""

    def test_mask_short_token(self) -> None:
        assert svc.mask_token("abc") == "****"

    def test_mask_long_token(self) -> None:
        assert svc.mask_token("ghp_1234567890") == "****7890"

    def test_encrypt_decrypt_round_trip(self) -> None:
        encrypted = svc.encrypt_token("my-secret-pat")
        assert svc.decrypt_token(encrypted) == "my-secret-pat"

    def test_decrypt_tampered_ciphertext_raises(self) -> None:
        encrypted = svc.encrypt_token("my-secret-pat")
        tampered = encrypted[:-4] + ("A" if encrypted[-4] != "A" else "B") + encrypted[-3:]
        with pytest.raises(Exception, match="decrypt"):
            svc.decrypt_token(tampered)
