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
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

import services.github_sync_service as svc
from blueprints.v1.github_sync import github_sync_admin_bp, github_sync_webhook_bp
from tests.community_ids import UNKNOWN_PUBLIC_ID, install_community_ids, public_id
from tests.conftest import OTHER_TENANT_SLUG, TENANT_SLUG

COMMUNITY_ID = 1
OTHER_COMMUNITY_ID = 2
ADMIN_USER_ID = 42
NON_ADMIN_USER_ID = 99
ENCRYPTION_KEY = "00" * 32  # 64 hex chars -- 32-byte AES-256 key, test-only


@pytest.fixture(autouse=True)
def _encryption_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SYNC_ENCRYPTION_KEY", ENCRYPTION_KEY)


@pytest.fixture(autouse=True)
def _ticket_community_column(automation_db: Any) -> None:
    """Give the fixture's narrow `support_tickets` the real table's `community_id` column.

    Production `support_tickets` has `community_id`; `bind_github_sync_tables()` binds a
    narrower shape. Rows default to community 1 so legacy inserts stay owned by community 1.
    """
    dal = automation_db.dal
    if "community_id" in dal.support_tickets.fields:
        return
    dal.executesql("ALTER TABLE support_tickets ADD COLUMN community_id INTEGER DEFAULT 1")
    dal.commit()
    dal.define_table(
        "support_tickets",
        *[f.clone() for f in dal.support_tickets if f.name != "id"],
        Field("community_id", "integer"),
        redefine=True,
        migrate=False,
    )


def _ensure_community(dal: Any, community_id: int, slug: str = TENANT_SLUG) -> None:
    """Insert (or re-home) `community_id` under the tenant `slug`, creating the tenant if absent."""
    tenant = dal(dal.tenants.slug == slug).select().first()
    if tenant is None:
        tenant_pk = dal.tenants.insert(slug=slug, display_name=slug, is_active=True)
    else:
        tenant_pk = tenant.id
    if dal(dal.communities.id == community_id).count():
        dal(dal.communities.id == community_id).update(tenant_id=tenant_pk)
    else:
        dal.communities.insert(
            id=community_id, name=f"community-{community_id}", tenant_id=tenant_pk
        )
    dal.commit()


@pytest.fixture
def app(automation_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = automation_db.dal
    quart_app.config["async_dal"] = automation_db
    install_community_ids(quart_app, automation_db.dal)
    quart_app.register_blueprint(github_sync_webhook_bp)
    quart_app.register_blueprint(github_sync_admin_bp)
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _cid(automation_db: Any, community_id: int = COMMUNITY_ID, slug: str = TENANT_SLUG) -> str:
    """Public UUID of `community_id`, seeding the bare community row first when absent."""
    _ensure_community(automation_db.dal, community_id, slug)
    return public_id(automation_db, community_id)


def _seed_admin(
    automation_db: Any,
    *,
    user_id: int,
    community_id: int,
    role: str = "admin",
    admin_kind: bool = True,
) -> None:
    """Seed a member holding the admin role (`admin_kind`) or a moderator-like broad-scope role."""
    dal = automation_db.dal
    _ensure_community(dal, community_id)
    role_id = dal.community_roles.insert(
        community_id=community_id,
        name=role,
        is_system=admin_kind,
        role_kind=1 if admin_kind else 0,
        base_claims={"scopes": ["community:manage_members"]},
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
        response = await client.get(f"/api/v1/{UNKNOWN_PUBLIC_ID}/github-sync/connections")
        assert response.status_code == 401


class TestCommunityAdminAuthz:
    async def test_non_admin_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        headers = user_auth_headers(user_id=NON_ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/{_cid(automation_db)}/github-sync/connections", headers=headers
        )
        assert response.status_code == 403

    async def test_admin_can_list_empty_connections(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/{_cid(automation_db)}/github-sync/connections", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"status": "success", "data": []}


class TestCreateConnection:
    async def test_non_admin_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        headers = user_auth_headers(user_id=NON_ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/{_cid(automation_db)}/github-sync/connections", headers=headers, json={}
        )
        assert response.status_code == 403

    async def test_missing_fields_is_400(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/{_cid(automation_db)}/github-sync/connections", headers=headers, json={}
        )
        assert response.status_code == 400

    async def test_rejects_malformed_owner(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        """SECURITY FIX (1) -- see module docstring's fail-first proof."""
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/{_cid(automation_db)}/github-sync/connections",
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
            f"/api/v1/{_cid(automation_db)}/github-sync/connections",
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
        assert conn["community_id"] == public_id(automation_db, COMMUNITY_ID)
        assert conn["vendor_id"] is None
        assert conn["token_masked"] == "****1234"
        assert "encrypted_token" not in conn
        # regression: one-time reveal on create only; never in list
        assert conn["webhook_secret"]
        assert conn["webhook_secret_set"] is True
        listed = await client.get(
            f"/api/v1/{_cid(automation_db)}/github-sync/connections", headers=headers
        )
        assert listed.status_code == 200
        rows = (await listed.get_json())["data"]
        assert len(rows) == 1
        for row in rows:
            assert "webhook_secret" not in row
            assert row["webhook_secret_set"] is True
        assert conn["webhook_secret"] not in json.dumps(rows)


class TestDeleteConnection:
    async def test_non_admin_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        headers = user_auth_headers(user_id=NON_ADMIN_USER_ID)
        response = await client.delete(
            f"/api/v1/{_cid(automation_db)}/github-sync/connections/1", headers=headers
        )
        assert response.status_code == 403

    async def test_non_numeric_connection_id_is_400(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.delete(
            f"/api/v1/{_cid(automation_db)}/github-sync/connections/not-a-number", headers=headers
        )
        assert response.status_code == 400

    async def test_delete_not_found_is_404(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.delete(
            f"/api/v1/{_cid(automation_db)}/github-sync/connections/9999", headers=headers
        )
        assert response.status_code == 404

    async def test_delete_cross_community_is_404(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        """`delete_repo_connection()` re-verifies against the row's REAL community_id."""
        dal = automation_db.dal
        _ensure_community(dal, OTHER_COMMUNITY_ID)
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
            f"/api/v1/{_cid(automation_db)}/github-sync/connections/{conn_id}", headers=headers
        )
        assert response.status_code == 404

    async def test_delete_denied_for_broad_scope_role_without_github_manage(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        """`community:manage_members` (moderator-satisfiable) is not `github:manage`."""
        dal = automation_db.dal
        _seed_admin(
            automation_db,
            user_id=ADMIN_USER_ID,
            community_id=COMMUNITY_ID,
            role="moderator",
            admin_kind=False,
        )
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
        response = await client.delete(
            f"/api/v1/{_cid(automation_db)}/github-sync/connections/{conn_id}",
            headers=user_auth_headers(user_id=ADMIN_USER_ID),
        )
        assert response.status_code == 403
        rows = await automation_db.select_async(dal(dal.github_repo_connections.id == conn_id))
        assert rows.first().is_active

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
            f"/api/v1/{_cid(automation_db)}/github-sync/connections/{conn_id}", headers=headers
        )
        assert response.status_code == 200
        # `update_async()` never calls `.commit()` (hub_api/PORTING.md
        # Gotcha #3) -- assert via `select_async` (same executor connection
        # as the write), not a bare sync `dal(...)` call on the main
        # thread's own connection, which would see the pre-update row.
        rows = await automation_db.select_async(dal(dal.github_repo_connections.id == conn_id))
        assert rows.first().is_active is False


class TestSyncStatus:
    async def test_non_admin_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        headers = user_auth_headers(user_id=NON_ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/{_cid(automation_db)}/github-sync/ticket/1/sync-status", headers=headers
        )
        assert response.status_code == 403

    async def test_invalid_ticket_id_is_400(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/{_cid(automation_db)}/github-sync/ticket/not-a-number/sync-status",
            headers=headers,
        )
        assert response.status_code == 400

    async def test_returns_sync_records(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        dal = automation_db.dal
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        dal.support_tickets.insert(
            id=7, subject="s", description="d", status="open", priority="normal", community_id=1
        )
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
            f"/api/v1/{_cid(automation_db)}/github-sync/ticket/7/sync-status", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert len(body["data"]) == 1
        assert body["data"][0]["repo_owner"] == "owner"


class TestTriggerSync:
    async def test_non_admin_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        headers = user_auth_headers(user_id=NON_ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/{_cid(automation_db)}/github-sync/ticket/1/sync", headers=headers, json={}
        )
        assert response.status_code == 403

    async def test_invalid_ticket_id_is_400(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/{_cid(automation_db)}/github-sync/ticket/not-a-number/sync",
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
            f"/api/v1/{_cid(automation_db)}/github-sync/ticket/1/sync",
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
            f"/api/v1/{_cid(automation_db)}/github-sync/ticket/1/sync", headers=headers, json={}
        )
        assert response.status_code == 400

    async def test_service_error_is_propagated(
        self, client: Any, user_auth_headers: Any, automation_db: Any
    ) -> None:
        """`sync_ticket_to_github()` raising `ApiError` (e.g. ticket not found) -> `_err()`."""
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/{_cid(automation_db)}/github-sync/ticket/9999/sync",
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
            f"/api/v1/{_cid(automation_db)}/github-sync/ticket/{ticket_id}/sync",
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


COMMUNITY_C_ID = 3  # lives in the OTHER tenant
ADMIN_C_USER_ID = 77
REAL_TOKEN_A = "ghp_tokenAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
REAL_TOKEN_B = "ghp_tokenBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


class _IdorWorld:
    """Seeded rows: A (1) + B (2) in tenant acme, C (3) in tenant other-corp."""

    def __init__(self) -> None:
        self.ticket: dict[str, int] = {}
        self.conn: dict[str, int] = {}
        self.github_calls: list[tuple[str, str]] = []
        self.decrypts: list[str] = []


@pytest.fixture
def idor_world(automation_db: Any, monkeypatch: pytest.MonkeyPatch) -> _IdorWorld:
    """Two same-tenant communities + one other-tenant community, each with ticket + connection.

    `_github_request` and `decrypt_token` are spied so a test can assert that a rejected
    request caused neither an outbound GitHub call nor a PAT decrypt.
    """
    world = _IdorWorld()
    dal = automation_db.dal
    _ensure_community(dal, COMMUNITY_ID)
    _ensure_community(dal, OTHER_COMMUNITY_ID)
    _ensure_community(dal, COMMUNITY_C_ID, OTHER_TENANT_SLUG)
    _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
    _seed_admin(automation_db, user_id=ADMIN_C_USER_ID, community_id=COMMUNITY_C_ID)
    tokens = {"a": REAL_TOKEN_A, "b": REAL_TOKEN_B, "c": "ghp_tokenCCCCCCCCCCCCCCCCCCCCCCCCCCCC"}
    ids = {"a": COMMUNITY_ID, "b": OTHER_COMMUNITY_ID, "c": COMMUNITY_C_ID}
    for key, cid in ids.items():
        world.ticket[key] = dal.support_tickets.insert(
            subject=f"secret subject {key}",
            description=f"secret description {key}",
            status="open",
            priority="high",
            community_id=cid,
        )
        world.conn[key] = dal.github_repo_connections.insert(
            community_id=cid,
            repo_owner=f"owner{key}",
            repo_name=f"repo{key}",
            auth_type="pat",
            encrypted_token=svc.encrypt_token(tokens[key]),
            webhook_secret="s",
            is_active=True,
        )
        dal.ticket_github_sync.insert(
            ticket_id=world.ticket[key],
            github_repo_connection_id=world.conn[key],
            github_issue_number=5,
            sync_status="failed",
            last_error=f"boom in {key}",
        )
    dal.commit()

    real_decrypt = svc.decrypt_token

    def spy_decrypt(encrypted_b64: str) -> str:
        world.decrypts.append(encrypted_b64)
        return real_decrypt(encrypted_b64)

    async def fake_github_request(
        method: str, path: str, token: str, *, json_body: Any = None
    ) -> Any:
        world.github_calls.append((path, token))
        return True, 201, {"number": 99, "node_id": "N"}

    monkeypatch.setattr(svc, "decrypt_token", spy_decrypt)
    monkeypatch.setattr(svc, "_github_request", fake_github_request)
    return world


async def _post_sync(
    client: Any, headers: dict[str, str], community_public_id: str, ticket_id: int, conn_id: int
) -> Any:
    headers = {**headers, "Content-Type": "application/json"}
    return await client.post(
        f"/api/v1/{community_public_id}/github-sync/ticket/{ticket_id}/sync",
        headers=headers,
        data=json.dumps({"repo_connection_id": conn_id}).encode(),
    )


# regression: github-sync cross-community IDOR (2026-11-20)
class TestCrossCommunityIdor:
    async def test_admin_a_syncing_b_ticket_is_404_no_github_no_decrypt(
        self,
        client: Any,
        user_auth_headers: Any,
        automation_db: Any,
        idor_world: _IdorWorld,
    ) -> None:
        # regression: github-sync cross-community IDOR (2026-11-20)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await _post_sync(
            client,
            headers,
            _cid(automation_db, COMMUNITY_ID),
            idor_world.ticket["b"],
            idor_world.conn["a"],
        )
        assert response.status_code == 404
        assert idor_world.github_calls == []
        assert idor_world.decrypts == []

    async def test_admin_a_using_b_connection_is_404_no_github_no_decrypt(
        self,
        client: Any,
        user_auth_headers: Any,
        automation_db: Any,
        idor_world: _IdorWorld,
    ) -> None:
        # regression: github-sync cross-community IDOR (2026-11-20)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await _post_sync(
            client,
            headers,
            _cid(automation_db, COMMUNITY_ID),
            idor_world.ticket["a"],
            idor_world.conn["b"],
        )
        assert response.status_code == 404
        assert idor_world.github_calls == []
        assert idor_world.decrypts == []
        dal = automation_db.dal
        assert dal(dal.ticket_github_sync.ticket_id == idor_world.ticket["a"]).count() == 1

    async def test_admin_a_reading_b_sync_status_is_404(
        self,
        client: Any,
        user_auth_headers: Any,
        automation_db: Any,
        idor_world: _IdorWorld,
    ) -> None:
        # regression: github-sync cross-community IDOR (2026-11-20)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/{_cid(automation_db, COMMUNITY_ID)}/github-sync/ticket/"
            f"{idor_world.ticket['b']}/sync-status",
            headers=headers,
        )
        assert response.status_code == 404
        assert "boom in b" not in await response.get_data(as_text=True)

    async def test_admin_a_deleting_b_connection_is_404_and_row_stays_active(
        self,
        client: Any,
        user_auth_headers: Any,
        automation_db: Any,
        idor_world: _IdorWorld,
    ) -> None:
        # regression: github-sync cross-community IDOR (2026-11-20)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.delete(
            f"/api/v1/{_cid(automation_db, COMMUNITY_ID)}/github-sync/connections/"
            f"{idor_world.conn['b']}",
            headers=headers,
        )
        assert response.status_code == 404
        dal = automation_db.dal
        rows = await automation_db.select_async(
            dal(dal.github_repo_connections.id == idor_world.conn["b"])
        )
        assert rows.first().is_active

    async def test_connection_list_only_returns_own_community(
        self,
        client: Any,
        user_auth_headers: Any,
        automation_db: Any,
        idor_world: _IdorWorld,
    ) -> None:
        # regression: github-sync cross-community IDOR (2026-11-20)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/{_cid(automation_db, COMMUNITY_ID)}/github-sync/connections",
            headers=headers,
        )
        assert response.status_code == 200
        ids = [row["id"] for row in (await response.get_json())["data"]]
        assert ids == [idor_world.conn["a"]]

    async def test_cross_tenant_is_404_for_every_route(
        self,
        client: Any,
        user_auth_headers: Any,
        automation_db: Any,
        idor_world: _IdorWorld,
    ) -> None:
        # regression: github-sync cross-community IDOR (2026-11-20)
        # Admin of C (tenant other-corp) aimed at community A (tenant acme), and vice versa.
        c_headers = user_auth_headers(user_id=ADMIN_C_USER_ID, tenant=OTHER_TENANT_SLUG)
        a_url = _cid(automation_db, COMMUNITY_ID)
        response = await _post_sync(
            client, c_headers, a_url, idor_world.ticket["a"], idor_world.conn["a"]
        )
        assert response.status_code == 404
        response = await client.get(
            f"/api/v1/{a_url}/github-sync/ticket/{idor_world.ticket['a']}/sync-status",
            headers=c_headers,
        )
        assert response.status_code == 404
        response = await client.get(f"/api/v1/{a_url}/github-sync/connections", headers=c_headers)
        assert response.status_code == 404

        a_headers = user_auth_headers(user_id=ADMIN_USER_ID)
        c_url = _cid(automation_db, COMMUNITY_C_ID, OTHER_TENANT_SLUG)
        response = await _post_sync(
            client, a_headers, c_url, idor_world.ticket["c"], idor_world.conn["c"]
        )
        assert response.status_code == 404
        assert idor_world.github_calls == []
        assert idor_world.decrypts == []

    async def test_admin_a_on_own_ticket_and_connection_works_and_pat_never_leaks(
        self,
        client: Any,
        user_auth_headers: Any,
        automation_db: Any,
        idor_world: _IdorWorld,
    ) -> None:
        # regression: github-sync cross-community IDOR (2026-11-20)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        url = _cid(automation_db, COMMUNITY_ID)
        response = await _post_sync(
            client, headers, url, idor_world.ticket["a"], idor_world.conn["a"]
        )
        assert response.status_code == 200
        body_text = await response.get_data(as_text=True)
        assert REAL_TOKEN_A not in body_text
        assert idor_world.github_calls == [("/repos/ownera/repoa/issues", REAL_TOKEN_A)]
        assert len(idor_world.decrypts) == 1

        status = await client.get(
            f"/api/v1/{url}/github-sync/ticket/{idor_world.ticket['a']}/sync-status",
            headers=headers,
        )
        assert status.status_code == 200
        status_text = await status.get_data(as_text=True)
        assert REAL_TOKEN_A not in status_text
        assert {r["repo_owner"] for r in (await status.get_json())["data"]} == {"ownera"}

    async def test_github_error_text_never_stores_the_pat(
        self,
        client: Any,
        user_auth_headers: Any,
        automation_db: Any,
        idor_world: _IdorWorld,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # regression: github-sync cross-community IDOR (2026-11-20)
        async def echoing_error(
            method: str, path: str, token: str, *, json_body: Any = None
        ) -> Any:
            return False, 401, {"message": f"Bad credentials {token}"}

        monkeypatch.setattr(svc, "_github_request", echoing_error)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        url = _cid(automation_db, COMMUNITY_ID)
        response = await _post_sync(
            client, headers, url, idor_world.ticket["a"], idor_world.conn["a"]
        )
        assert response.status_code == 502
        assert REAL_TOKEN_A not in await response.get_data(as_text=True)
        status = await client.get(
            f"/api/v1/{url}/github-sync/ticket/{idor_world.ticket['a']}/sync-status",
            headers=headers,
        )
        assert REAL_TOKEN_A not in await status.get_data(as_text=True)

    async def test_webhook_picks_connection_by_signature_across_communities(
        self, client: Any, automation_db: Any
    ) -> None:
        # regression: github-sync cross-community IDOR (2026-11-20)
        # Two communities connect the same repo; the event must close only the ticket of
        # the community whose webhook secret signed it.
        dal = automation_db.dal
        _ensure_community(dal, COMMUNITY_ID)
        _ensure_community(dal, OTHER_COMMUNITY_ID)
        tickets: dict[int, int] = {}
        for cid, secret in ((COMMUNITY_ID, "secret-a"), (OTHER_COMMUNITY_ID, "secret-b")):
            conn_id = dal.github_repo_connections.insert(
                community_id=cid,
                repo_owner="shared",
                repo_name="repo",
                auth_type="pat",
                encrypted_token="x",
                webhook_secret=secret,
                auto_close_on_github_close=True,
                is_active=True,
            )
            tickets[cid] = dal.support_tickets.insert(
                subject="s", description="d", status="open", priority="normal", community_id=cid
            )
            dal.ticket_github_sync.insert(
                ticket_id=tickets[cid],
                github_repo_connection_id=conn_id,
                github_issue_number=42,
                sync_status="synced",
            )
        dal.commit()
        payload = json.dumps(
            {
                "repository": {"owner": {"login": "shared"}, "name": "repo"},
                "action": "closed",
                "issue": {"number": 42},
            }
        ).encode()
        signature = "sha256=" + hmac.new(b"secret-b", payload, "sha256").hexdigest()
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
        statuses = {}
        for cid, tid in tickets.items():
            rows = await automation_db.select_async(dal(dal.support_tickets.id == tid))
            statuses[cid] = rows.first().status
        assert statuses == {COMMUNITY_ID: "open", OTHER_COMMUNITY_ID: "closed"}


class TestServiceLevelOwnership:
    def test_decrypt_refused_for_foreign_connection(self) -> None:
        # regression: github-sync cross-community IDOR (2026-11-20)
        class Conn:
            id = 1
            community_id = 2
            encrypted_token = "never-decrypted"

        with pytest.raises(svc.ApiError) as exc_info:
            svc._decrypt_for_community(Conn(), 1)
        assert exc_info.value.status_code == 404


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
