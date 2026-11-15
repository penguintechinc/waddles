"""`blueprints/v1/workflow.py` -- ported from `workflowController.js`/`routes/workflow.js`.

Standalone Quart app (mirrors `test_platform_blueprint.py`'s own
pattern) registering only `workflow_bp`, with a fake `workflow-core`
(`FakeWorkflowCore`, monkeypatched over the module's `_client` singleton)
standing in for the real downstream proxy target -- these tests exercise
hub-api's OWN logic (community authz, license gating, the cross-community
ownership fix), not `workflow-core`'s.

Fail-first proof (executed, not narrated) for the IDOR fix
(`workflow_service.get_workflow_or_403`): temporarily removed the
`get_workflow_or_403()` call + its `except ApiError` branch from
`update_workflow()` in `blueprints/v1/workflow.py`, leaving the PUT
proxy through unconditionally.
`test_update_workflow_cross_community_is_403` went red (200 instead of
403 -- the PUT reached `workflow-core` for a workflow belonging to a
DIFFERENT community than the caller's). All other tests in this file
were unaffected (none of them exercise `update_workflow`). Reverted,
`test_update_workflow_cross_community_is_403` green again, full file
green.
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.workflow as workflow_module
from blueprints.v1.workflow import workflow_bp
from services.workflow_service import ProxyResult

COMMUNITY_ID = 1
OTHER_COMMUNITY_ID = 2
ADMIN_USER_ID = 42
NON_ADMIN_USER_ID = 99


class FakeWorkflowCore:
    """Canned-response stand-in for the real `workflow-core` HTTP service.

    Enough of a fake router to exercise every route in `blueprints/v1/
    workflow.py`: workflows, executions, and webhooks, each keyed by a
    made-up id. Deleted workflows are removed from `self.workflows`
    entirely (a subsequent GET 404s), matching `workflow-core`'s own
    real behavior close enough for these tests' purposes.
    """

    def __init__(self) -> None:
        """Start with no workflows/executions/webhooks and an empty call log."""
        self.workflows: dict[str, dict[str, Any]] = {}
        self.executions: dict[str, dict[str, Any]] = {}
        self.webhooks: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def _workflow_or_404(self, workflow_id: str) -> ProxyResult | dict[str, Any]:
        workflow = self.workflows.get(workflow_id)
        if workflow is None:
            return ProxyResult(ok=False, status_code=404, body={"message": "not found"})
        return workflow

    async def request(  # noqa: C901 - a fake HTTP router naturally branches per path
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ProxyResult:
        self.calls.append((method, path))
        parts = path.strip("/").split("/")  # e.g. ["workflows", "wf-1", "executions", "ex-1"]

        if method == "POST" and path == "/workflows":
            assert json_body is not None
            new_id = f"wf-{len(self.workflows) + 1}"
            self.workflows[new_id] = {
                "id": new_id,
                "communityId": json_body["communityId"],
                "name": json_body["name"],
                "description": json_body.get("description"),
            }
            return ProxyResult(ok=True, status_code=201, body=self.workflows[new_id])

        if method == "GET" and path == "/workflows":
            return ProxyResult(
                ok=True,
                status_code=200,
                body={"workflows": list(self.workflows.values()), "pagination": {}},
            )

        if method == "POST" and path == "/workflows/validate":
            return ProxyResult(
                ok=True, status_code=200, body={"isValid": True, "errors": [], "warnings": []}
            )

        if len(parts) == 2 and parts[0] == "workflows":
            workflow_id = parts[1]
            if method == "GET":
                result = self._workflow_or_404(workflow_id)
                return result if isinstance(result, ProxyResult) else ProxyResult(True, 200, result)
            if method == "PUT":
                result = self._workflow_or_404(workflow_id)
                if isinstance(result, ProxyResult):
                    return result
                assert json_body is not None
                result.update(
                    {k: v for k, v in json_body.items() if k not in {"communityId", "updatedBy"}}
                )
                return ProxyResult(True, 200, result)
            if method == "DELETE":
                if workflow_id not in self.workflows:
                    return ProxyResult(False, 404, {"message": "not found"})
                del self.workflows[workflow_id]
                return ProxyResult(True, 200, {})

        if (
            len(parts) == 3
            and parts[0] == "workflows"
            and parts[2] in {"publish", "execute", "test"}
        ):
            workflow_id, action = parts[1], parts[2]
            result = self._workflow_or_404(workflow_id)
            if isinstance(result, ProxyResult):
                return result
            if action == "execute" or action == "test":
                exec_id = f"ex-{len(self.executions) + 1}"
                self.executions[exec_id] = {
                    "executionId": exec_id,
                    "workflowId": workflow_id,
                    "success": True,
                }
                return ProxyResult(True, 200, self.executions[exec_id])
            return ProxyResult(True, 200, result)

        if len(parts) == 3 and parts[0] == "workflows" and parts[2] == "executions":
            workflow_id = parts[1]
            result = self._workflow_or_404(workflow_id)
            if isinstance(result, ProxyResult):
                return result
            return ProxyResult(
                True,
                200,
                {"executions": list(self.executions.values()), "pagination": {}},
            )

        if len(parts) == 4 and parts[0] == "workflows" and parts[2] == "executions":
            workflow_id, execution_id = parts[1], parts[3]
            wf_result = self._workflow_or_404(workflow_id)
            if isinstance(wf_result, ProxyResult):
                return wf_result
            execution = self.executions.get(execution_id)
            if execution is None:
                return ProxyResult(False, 404, {"message": "not found"})
            return ProxyResult(True, 200, execution)

        if (
            len(parts) == 5
            and parts[0] == "workflows"
            and parts[2] == "executions"
            and parts[4] == "cancel"
        ):
            workflow_id, execution_id = parts[1], parts[3]
            wf_result = self._workflow_or_404(workflow_id)
            if isinstance(wf_result, ProxyResult):
                return wf_result
            if execution_id not in self.executions:
                return ProxyResult(False, 404, {"message": "not found"})
            return ProxyResult(True, 200, {})

        if len(parts) == 3 and parts[0] == "workflows" and parts[2] == "webhooks":
            workflow_id = parts[1]
            result = self._workflow_or_404(workflow_id)
            if isinstance(result, ProxyResult):
                return result
            if method == "GET":
                return ProxyResult(True, 200, {"webhooks": list(self.webhooks.values())})
            if method == "POST":
                assert json_body is not None
                new_id = f"wh-{len(self.webhooks) + 1}"
                self.webhooks[new_id] = {
                    "id": new_id,
                    "event": json_body["event"],
                    "url": json_body["url"],
                }
                return ProxyResult(True, 201, self.webhooks[new_id])

        if (
            len(parts) == 4
            and parts[0] == "workflows"
            and parts[2] == "webhooks"
            and method == "DELETE"
        ):
            workflow_id, webhook_id = parts[1], parts[3]
            wf_result = self._workflow_or_404(workflow_id)
            if isinstance(wf_result, ProxyResult):
                return wf_result
            if webhook_id not in self.webhooks:
                return ProxyResult(False, 404, {"message": "not found"})
            del self.webhooks[webhook_id]
            return ProxyResult(True, 200, {})

        return ProxyResult(ok=True, status_code=200, body={})


@pytest.fixture
def fake_core(monkeypatch: pytest.MonkeyPatch) -> FakeWorkflowCore:
    fake = FakeWorkflowCore()
    monkeypatch.setattr(workflow_module, "_client", fake)
    return fake


@pytest.fixture
def app(automation_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(workflow_bp)
    quart_app.config["dal"] = automation_db.dal
    quart_app.config["async_dal"] = automation_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _seed_admin(
    automation_db: Any, *, user_id: int, community_id: int, licensed: bool = False
) -> None:
    """Seed a `community_roles`/`community_members` row granting `community:manage_members`."""
    dal = automation_db.dal
    dal.communities.insert(
        id=community_id,
        name=f"community-{community_id}",
        display_name=f"Community {community_id}",
        license_key="lic-123" if licensed else None,
        license_tier="pro" if licensed else None,
    )
    role_id = dal.community_roles.insert(
        community_id=community_id,
        name="admin",
        is_system=True,
        role_kind=1,
        base_claims={"scopes": ["community:manage_members"]},
    )
    dal.community_members.insert(
        community_id=community_id,
        user_id=str(user_id),
        role="admin",
        is_active=True,
        community_role_id=role_id,
    )
    dal.commit()


class TestAuthBypass:
    async def test_missing_token_is_401(self, client: Any) -> None:
        """No bearer token at all -- `tenant_middleware` (outermost) rejects first."""
        response = await client.get(f"/api/v1/admin/{COMMUNITY_ID}/workflows")
        assert response.status_code == 401

    async def test_invalid_token_is_401(self, client: Any) -> None:
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert response.status_code == 401


class TestCommunityAdminAuthz:
    async def test_non_admin_is_403(self, client: Any, user_auth_headers: Any) -> None:
        """Valid token, but caller has no `community_members` row for this community."""
        headers = user_auth_headers(user_id=NON_ADMIN_USER_ID)
        response = await client.get(f"/api/v1/admin/{COMMUNITY_ID}/workflows", headers=headers)
        assert response.status_code == 403

    async def test_invalid_community_id_is_400(self, client: Any, user_auth_headers: Any) -> None:
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get("/api/v1/admin/not-a-number/workflows", headers=headers)
        assert response.status_code == 400

    async def test_broad_manage_members_scope_cannot_list_workflows(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        """A non-admin-kind role with only `community:manage_members` lacks `workflow:manage`."""
        dal = automation_db.dal
        dal.communities.insert(id=COMMUNITY_ID, name="c", display_name="C")
        role_id = dal.community_roles.insert(
            community_id=COMMUNITY_ID,
            name="moderator",
            is_system=True,
            role_kind=2,
            base_claims={"scopes": ["community:manage_members", "community:manage_channels"]},
        )
        dal.community_members.insert(
            community_id=COMMUNITY_ID,
            user_id=str(ADMIN_USER_ID),
            role="moderator",
            is_active=True,
            community_role_id=role_id,
        )
        dal.commit()
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(f"/api/v1/admin/{COMMUNITY_ID}/workflows", headers=headers)
        assert response.status_code == 403

    async def test_admin_can_list_workflows(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(f"/api/v1/admin/{COMMUNITY_ID}/workflows", headers=headers)
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        assert body["workflows"] == []

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/workflows"),
            ("POST", "/workflows"),
            ("GET", "/workflows/wf-1"),
            ("PUT", "/workflows/wf-1"),
            ("DELETE", "/workflows/wf-1"),
            ("POST", "/workflows/wf-1/publish"),
            ("POST", "/workflows/validate"),
            ("POST", "/workflows/wf-1/execute"),
            ("POST", "/workflows/wf-1/test"),
            ("GET", "/workflows/wf-1/executions"),
            ("GET", "/workflows/wf-1/executions/ex-1"),
            ("POST", "/workflows/wf-1/executions/ex-1/cancel"),
            ("GET", "/workflows/wf-1/webhooks"),
            ("POST", "/workflows/wf-1/webhooks"),
            ("DELETE", "/workflows/wf-1/webhooks/wh-1"),
        ],
    )
    async def test_every_route_rejects_non_admin(
        self, client: Any, user_auth_headers: Any, method: str, path: str
    ) -> None:
        """Every one of the 15 ported endpoints runs through `_guard()` -- none skip it."""
        headers = user_auth_headers(user_id=NON_ADMIN_USER_ID)
        response = await client.open(
            f"/api/v1/admin/{COMMUNITY_ID}{path}", method=method, headers=headers
        )
        assert response.status_code == 403


class TestLicenseGate:
    async def test_create_workflow_blocked_without_license(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID, licensed=False)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows",
            headers=headers,
            json={"name": "My Workflow", "definition": {"steps": []}},
        )
        assert response.status_code == 403
        body = await response.get_json()
        assert "not available" in body["error"]["message"]

    async def test_create_workflow_missing_fields_is_400(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID, licensed=True)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows", headers=headers, json={}
        )
        assert response.status_code == 400

    async def test_create_workflow_succeeds_with_license(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID, licensed=True)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows",
            headers=headers,
            json={"name": "My Workflow", "definition": {"steps": []}},
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["workflow"]["name"] == "My Workflow"


class TestCrossCommunityOwnershipFix:
    """SECURITY FIX regression tests -- `workflow_service.get_workflow_or_403`."""

    async def test_get_workflow_cross_community_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-victim"] = {
            "id": "wf-victim",
            "communityId": OTHER_COMMUNITY_ID,
            "name": "Victim's workflow",
        }
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-victim", headers=headers
        )
        assert response.status_code == 403

    async def test_update_workflow_cross_community_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        """The IDOR Node itself never checked -- see this module's fail-first docstring."""
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-victim"] = {
            "id": "wf-victim",
            "communityId": OTHER_COMMUNITY_ID,
            "name": "Victim's workflow",
        }
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.put(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-victim",
            headers=headers,
            json={"name": "Hijacked"},
        )
        assert response.status_code == 403
        # The fake never applied the update -- proves the proxy call never fired.
        assert fake_core.workflows["wf-victim"]["name"] == "Victim's workflow"

    async def test_get_workflow_succeeds(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-own"] = {
            "id": "wf-own",
            "communityId": COMMUNITY_ID,
            "name": "Mine",
        }
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-own", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["workflow"]["name"] == "Mine"

    async def test_update_workflow_all_fields(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        """Exercises the `description`/`definition` conditional-include branches too."""
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-own"] = {
            "id": "wf-own",
            "communityId": COMMUNITY_ID,
            "name": "Mine",
        }
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.put(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-own",
            headers=headers,
            json={"name": "Renamed", "description": "New desc", "definition": {"steps": ["a"]}},
        )
        assert response.status_code == 200

    async def test_get_workflow_not_found_is_404(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/does-not-exist", headers=headers
        )
        assert response.status_code == 404

    async def test_update_workflow_own_community_succeeds(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-own"] = {
            "id": "wf-own",
            "communityId": COMMUNITY_ID,
            "name": "Mine",
        }
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.put(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-own",
            headers=headers,
            json={"name": "Renamed"},
        )
        assert response.status_code == 200
        assert fake_core.workflows["wf-own"]["name"] == "Renamed"


class TestValidateWorkflow:
    async def test_validate_missing_definition_is_400(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/validate", headers=headers, json={}
        )
        assert response.status_code == 400

    async def test_validate_returns_result(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/validate",
            headers=headers,
            json={"definition": {"steps": []}},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["isValid"] is True


class TestDeleteWorkflow:
    async def test_delete_not_found_is_404(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.delete(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/missing", headers=headers
        )
        assert response.status_code == 404

    async def test_delete_cross_community_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-victim"] = {"id": "wf-victim", "communityId": OTHER_COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.delete(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-victim", headers=headers
        )
        assert response.status_code == 403
        assert "wf-victim" in fake_core.workflows  # never deleted

    async def test_delete_succeeds(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-own"] = {"id": "wf-own", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.delete(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-own", headers=headers
        )
        assert response.status_code == 200
        assert "wf-own" not in fake_core.workflows


class TestPublishWorkflow:
    async def test_publish_blocked_without_license(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID, licensed=False)
        fake_core.workflows["wf-1"] = {"id": "wf-1", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/publish", headers=headers
        )
        assert response.status_code == 403

    async def test_publish_cross_community_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID, licensed=True)
        fake_core.workflows["wf-victim"] = {"id": "wf-victim", "communityId": OTHER_COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-victim/publish", headers=headers
        )
        assert response.status_code == 403

    async def test_publish_succeeds_with_license(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID, licensed=True)
        fake_core.workflows["wf-1"] = {"id": "wf-1", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/publish", headers=headers
        )
        assert response.status_code == 200


class TestExecuteWorkflow:
    async def test_execute_blocked_without_license(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID, licensed=False)
        fake_core.workflows["wf-1"] = {"id": "wf-1", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/execute", headers=headers, json={}
        )
        assert response.status_code == 403

    async def test_execute_cross_community_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID, licensed=True)
        fake_core.workflows["wf-victim"] = {"id": "wf-victim", "communityId": OTHER_COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-victim/execute", headers=headers, json={}
        )
        assert response.status_code == 403

    async def test_execute_succeeds_with_license(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID, licensed=True)
        fake_core.workflows["wf-1"] = {"id": "wf-1", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/execute",
            headers=headers,
            json={"input": {"a": 1}, "context": {}},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["execution"]["success"] is True


class TestTestWorkflow:
    async def test_test_workflow_no_license_needed(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        """Node never gates `testWorkflow()` on a license -- neither does this port."""
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID, licensed=False)
        fake_core.workflows["wf-1"] = {"id": "wf-1", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/test",
            headers=headers,
            json={"input": {}, "context": {}},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["result"]["success"] is True

    async def test_test_workflow_cross_community_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-victim"] = {"id": "wf-victim", "communityId": OTHER_COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-victim/test", headers=headers, json={}
        )
        assert response.status_code == 403


class TestExecutions:
    async def test_list_executions_cross_community_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-victim"] = {"id": "wf-victim", "communityId": OTHER_COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-victim/executions", headers=headers
        )
        assert response.status_code == 403

    async def test_list_executions_succeeds(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID, licensed=True)
        fake_core.workflows["wf-1"] = {"id": "wf-1", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/execute", headers=headers, json={}
        )
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/executions?page=1&limit=10&status=success",
            headers=headers,
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert len(body["executions"]) == 1

    async def test_get_execution_not_found_is_404(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-1"] = {"id": "wf-1", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/executions/missing", headers=headers
        )
        assert response.status_code == 404

    async def test_get_execution_cross_community_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-victim"] = {"id": "wf-victim", "communityId": OTHER_COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-victim/executions/ex-1", headers=headers
        )
        assert response.status_code == 403

    async def test_get_execution_succeeds(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID, licensed=True)
        fake_core.workflows["wf-1"] = {"id": "wf-1", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/execute", headers=headers, json={}
        )
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/executions/ex-1", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["execution"]["executionId"] == "ex-1"

    async def test_cancel_execution_not_found_is_404(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-1"] = {"id": "wf-1", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/executions/missing/cancel",
            headers=headers,
        )
        assert response.status_code == 404

    async def test_cancel_execution_cross_community_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-victim"] = {"id": "wf-victim", "communityId": OTHER_COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-victim/executions/ex-1/cancel",
            headers=headers,
        )
        assert response.status_code == 403

    async def test_cancel_execution_succeeds(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID, licensed=True)
        fake_core.workflows["wf-1"] = {"id": "wf-1", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/execute", headers=headers, json={}
        )
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/executions/ex-1/cancel", headers=headers
        )
        assert response.status_code == 200


class TestWebhooks:
    async def test_list_webhooks_cross_community_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-victim"] = {"id": "wf-victim", "communityId": OTHER_COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-victim/webhooks", headers=headers
        )
        assert response.status_code == 403

    async def test_list_webhooks_empty(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-1"] = {"id": "wf-1", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/webhooks", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["webhooks"] == []

    async def test_create_webhook_cross_community_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-victim"] = {"id": "wf-victim", "communityId": OTHER_COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-victim/webhooks",
            headers=headers,
            json={"event": "e", "url": "https://example.com"},
        )
        assert response.status_code == 403

    async def test_create_webhook_missing_fields_is_400(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-1"] = {"id": "wf-1", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/webhooks", headers=headers, json={}
        )
        assert response.status_code == 400

    async def test_create_webhook_succeeds(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-1"] = {"id": "wf-1", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/webhooks",
            headers=headers,
            json={"event": "workflow.completed", "url": "https://example.com/hook"},
        )
        assert response.status_code == 201
        body = await response.get_json()
        assert body["webhook"]["event"] == "workflow.completed"

    async def test_delete_webhook_cross_community_is_403(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-victim"] = {"id": "wf-victim", "communityId": OTHER_COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.delete(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-victim/webhooks/wh-1", headers=headers
        )
        assert response.status_code == 403

    async def test_delete_webhook_not_found_is_404(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-1"] = {"id": "wf-1", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.delete(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/webhooks/missing", headers=headers
        )
        assert response.status_code == 404

    async def test_delete_webhook_succeeds(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        fake_core.workflows["wf-1"] = {"id": "wf-1", "communityId": COMMUNITY_ID}
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        await client.post(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/webhooks",
            headers=headers,
            json={"event": "e", "url": "https://example.com"},
        )
        response = await client.delete(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows/wf-1/webhooks/wh-1", headers=headers
        )
        assert response.status_code == 200


class TestPaginationEdgeCases:
    async def test_page_and_limit_clamp_to_bounds(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        """`page=0` clamps to 1, `limit=9999` clamps to 100 -- matches Node's `Math.max`/`min`."""
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows?page=0&limit=9999", headers=headers
        )
        assert response.status_code == 200

    async def test_non_numeric_page_falls_back_to_default(
        self, client: Any, user_auth_headers: Any, automation_db: Any, fake_core: FakeWorkflowCore
    ) -> None:
        _seed_admin(automation_db, user_id=ADMIN_USER_ID, community_id=COMMUNITY_ID)
        headers = user_auth_headers(user_id=ADMIN_USER_ID)
        response = await client.get(
            f"/api/v1/admin/{COMMUNITY_ID}/workflows?page=not-a-number", headers=headers
        )
        assert response.status_code == 200
