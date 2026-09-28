"""`controllers/execution_api.py` -- additional coverage beyond `tests/test_execution_api_authz.py`.

That suite covers the community/tenant authz gate; this one covers what
happens once a caller passes it: permission-denied (`can_execute`/
`can_view`), missing workflow, engine exceptions, `list_workflow_executions`
parameter validation/pagination/filters, the error handlers, and
`register_execution_api`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydal import DAL
from quart import Quart

from config import Config
from controllers.execution_api import (
    bad_request,
    forbidden,
    internal_error,
    not_found,
    register_execution_api,
    unauthorized,
)
from flask_core.auth import create_jwt_token
from flask_core.community_access import bind_shared_read_tables
from services.workflow_engine import WorkflowEngineException, WorkflowTimeoutException

SECRET = Config.SECRET_KEY


def _token(*, sub: str = "1", tenant: str = "acme-corp") -> str:
    return create_jwt_token(
        user_id=sub, username=f"user{sub}", email=f"user{sub}@example.com",
        roles=[], secret_key=SECRET, tenant=tenant,
    )


class _FakeAsyncDAL:
    def __init__(self, dal: Any) -> None:
        self.dal = dal

    async def select_async(self, query_set: Any, *fields: Any) -> Any:
        return query_set.select(*fields) if fields else query_set.select()


class _FakeWorkflowDal:
    def __init__(self, workflow_community: dict[str, int]) -> None:
        self._workflow_community = workflow_community

    def executesql(self, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
        if "SELECT community_id FROM workflows" in sql:
            community_id = self._workflow_community.get(params[0])
            return [(community_id,)] if community_id is not None else []
        raise AssertionError(f"unexpected query: {sql}")


class _FakeWorkflowService:
    def __init__(self, dal: _FakeWorkflowDal, workflow: dict[str, Any] | None = "default") -> None:
        self.dal = dal
        self._workflow = workflow

    async def get_workflow(self, **kwargs: Any) -> dict[str, Any] | None:
        if self._workflow == "default":
            return {"workflow_id": kwargs["workflow_id"], "entity_id": 1}
        return self._workflow


class _PermissionService:
    def __init__(self, allow: bool = True) -> None:
        self._allow = allow

    async def check_permission(self, **kwargs: Any) -> bool:
        return self._allow


class _Execution:
    execution_id = "exec-result-1"
    workflow_id = "wf-a"
    execution_path: list[str] = []
    execution_time_seconds = 0.1
    final_variables: dict[str, Any] = {}
    is_successful = True

    class status:
        value = "running"

    class start_time:
        @staticmethod
        def isoformat() -> str:
            return "2026-01-01T00:00:00Z"

    @staticmethod
    def get_node_state(node_id: str) -> None:
        return None

    @staticmethod
    def get_failed_nodes() -> list[str]:
        return []


class _FakeWorkflowEngine:
    def __init__(self, execute_side_effect: Exception | None = None) -> None:
        self._execute_side_effect = execute_side_effect

    async def execute_workflow(self, **kwargs: Any) -> _Execution:
        if self._execute_side_effect:
            raise self._execute_side_effect
        return _Execution()


@pytest.fixture
def db() -> Any:
    dal = DAL("sqlite:memory")
    bind_shared_read_tables(dal, migrate=True)
    yield dal
    dal.close()


@pytest.fixture
def seeded(db: Any) -> dict[str, int]:
    acme_id = db.tenants.insert(slug="acme-corp", is_active=True)
    community_a = db.communities.insert(tenant_id=acme_id)
    db.community_members.insert(
        community_id=community_a, user_id="1", role="community-admin", is_active=True
    )
    db.commit()
    return {"community_a": community_a}


def _build_app(
    db: Any, seeded: dict[str, int], *,
    permission_allow: bool = True, workflow: Any = "default",
    engine_error: Exception | None = None,
) -> Quart:
    app = Quart(__name__)
    app.config["dal"] = db
    app.config["async_dal"] = _FakeAsyncDAL(db)
    workflow_dal = _FakeWorkflowDal(workflow_community={"wf-a": seeded["community_a"]})
    app.config["workflow_service"] = _FakeWorkflowService(workflow_dal, workflow=workflow)
    app.config["permission_service"] = _PermissionService(allow=permission_allow)
    register_execution_api(app, _FakeWorkflowEngine(execute_side_effect=engine_error))
    return app


class TestExecuteWorkflowOutcomes:
    @pytest.mark.asyncio
    async def test_permission_denied_is_403(self, db: Any, seeded: dict[str, int]) -> None:
        app = _build_app(db, seeded, permission_allow=False)
        async with app.test_client() as client:
            resp = await client.post(
                "/api/v1/workflows/wf-a/execute",
                headers={"Authorization": f"Bearer {_token()}"}, json={},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_workflow_not_found_is_400(self, db: Any, seeded: dict[str, int]) -> None:
        app = _build_app(db, seeded, workflow=None)
        async with app.test_client() as client:
            resp = await client.post(
                "/api/v1/workflows/wf-a/execute",
                headers={"Authorization": f"Bearer {_token()}"}, json={},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_engine_timeout_is_504(self, db: Any, seeded: dict[str, int]) -> None:
        app = _build_app(db, seeded, engine_error=WorkflowTimeoutException("too slow"))
        async with app.test_client() as client:
            resp = await client.post(
                "/api/v1/workflows/wf-a/execute",
                headers={"Authorization": f"Bearer {_token()}"}, json={},
            )
        assert resp.status_code == 504

    @pytest.mark.asyncio
    async def test_engine_exception_is_400(self, db: Any, seeded: dict[str, int]) -> None:
        app = _build_app(db, seeded, engine_error=WorkflowEngineException("bad graph"))
        async with app.test_client() as client:
            resp = await client.post(
                "/api/v1/workflows/wf-a/execute",
                headers={"Authorization": f"Bearer {_token()}"}, json={},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_unexpected_exception_is_500(self, db: Any, seeded: dict[str, int]) -> None:
        app = _build_app(db, seeded, engine_error=RuntimeError("boom"))
        async with app.test_client() as client:
            resp = await client.post(
                "/api/v1/workflows/wf-a/execute",
                headers={"Authorization": f"Bearer {_token()}"}, json={},
            )
        assert resp.status_code == 500


class TestWorkflowTestOutcomes:
    @pytest.mark.asyncio
    async def test_permission_denied_is_403(self, db: Any, seeded: dict[str, int]) -> None:
        app = _build_app(db, seeded, permission_allow=False)
        async with app.test_client() as client:
            resp = await client.post(
                "/api/v1/workflows/wf-a/test",
                headers={"Authorization": f"Bearer {_token()}"}, json={},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_workflow_not_found_is_400(self, db: Any, seeded: dict[str, int]) -> None:
        app = _build_app(db, seeded, workflow=None)
        async with app.test_client() as client:
            resp = await client.post(
                "/api/v1/workflows/wf-a/test",
                headers={"Authorization": f"Bearer {_token()}"}, json={},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_success_builds_trace_and_summary(self, db: Any, seeded: dict[str, int]) -> None:
        app = _build_app(db, seeded)
        async with app.test_client() as client:
            resp = await client.post(
                "/api/v1/workflows/wf-a/test",
                headers={"Authorization": f"Bearer {_token()}"},
                json={"variables": {"x": 1}, "metadata": {"session_id": "s1"}},
            )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["data"]["summary"]["passed"] is True

    @pytest.mark.asyncio
    async def test_engine_exception_is_500(self, db: Any, seeded: dict[str, int]) -> None:
        app = _build_app(db, seeded, engine_error=RuntimeError("boom"))
        async with app.test_client() as client:
            resp = await client.post(
                "/api/v1/workflows/wf-a/test",
                headers={"Authorization": f"Bearer {_token()}"}, json={},
            )
        assert resp.status_code == 500


class TestListWorkflowExecutions:
    @pytest.mark.asyncio
    async def test_invalid_sort_by_is_400(self, db: Any, seeded: dict[str, int]) -> None:
        app = _build_app(db, seeded)
        async with app.test_client() as client:
            resp = await client.get(
                "/api/v1/workflows/wf-a/executions?sort_by=bogus",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_sort_order_is_400(self, db: Any, seeded: dict[str, int]) -> None:
        app = _build_app(db, seeded)
        async with app.test_client() as client:
            resp = await client.get(
                "/api/v1/workflows/wf-a/executions?sort_order=sideways",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_pagination_is_400(self, db: Any, seeded: dict[str, int]) -> None:
        app = _build_app(db, seeded)
        async with app.test_client() as client:
            resp = await client.get(
                "/api/v1/workflows/wf-a/executions?page=notanumber",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_page_below_one_is_400(self, db: Any, seeded: dict[str, int]) -> None:
        app = _build_app(db, seeded)
        async with app.test_client() as client:
            resp = await client.get(
                "/api/v1/workflows/wf-a/executions?page=0",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_per_page_out_of_range_is_400(self, db: Any, seeded: dict[str, int]) -> None:
        app = _build_app(db, seeded)
        async with app.test_client() as client:
            resp = await client.get(
                "/api/v1/workflows/wf-a/executions?per_page=1000",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_permission_denied_is_403(self, db: Any, seeded: dict[str, int]) -> None:
        app = _build_app(db, seeded, permission_allow=False)
        async with app.test_client() as client:
            resp = await client.get(
                "/api/v1/workflows/wf-a/executions",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_success_with_results_and_status_filter(
        self, db: Any, seeded: dict[str, int]
    ) -> None:
        # list_workflow_executions() queries via `get_dal()` (app.config["dal"],
        # the shared tenant sqlite DAL), NOT `workflow_service.dal`.
        import datetime

        app = _build_app(db, seeded)
        now = datetime.datetime.utcnow()
        row = ("exec-1", "wf-a", "completed", now, now, 1.5, 3, None, {})

        def _executesql(sql: str, params: list) -> list:
            if "COUNT(*)" in sql:
                return [(1,)]
            if "FROM workflow_executions" in sql:
                return [row]
            raise AssertionError(f"unexpected query: {sql}")

        db.executesql = _executesql
        async with app.test_client() as client:
            resp = await client.get(
                "/api/v1/workflows/wf-a/executions?status=completed&page=1&per_page=10",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["data"][0]["execution_id"] == "exec-1"
        assert body["meta"]["pagination"]["total"] == 1

    @pytest.mark.asyncio
    async def test_count_query_error_defaults_total_zero(
        self, db: Any, seeded: dict[str, int]
    ) -> None:
        app = _build_app(db, seeded)

        def _executesql(sql: str, params: list) -> list:
            if "COUNT(*)" in sql:
                raise Exception("db exploded")
            if "FROM workflow_executions" in sql:
                return []
            raise AssertionError(f"unexpected query: {sql}")

        db.executesql = _executesql
        async with app.test_client() as client:
            resp = await client.get(
                "/api/v1/workflows/wf-a/executions",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["meta"]["pagination"]["total"] == 0

    @pytest.mark.asyncio
    async def test_list_query_error_returns_empty_list(
        self, db: Any, seeded: dict[str, int]
    ) -> None:
        app = _build_app(db, seeded)

        def _executesql(sql: str, params: list) -> list:
            if "COUNT(*)" in sql:
                return [(0,)]
            if "FROM workflow_executions" in sql:
                raise Exception("query exploded")
            raise AssertionError(f"unexpected query: {sql}")

        db.executesql = _executesql
        async with app.test_client() as client:
            resp = await client.get(
                "/api/v1/workflows/wf-a/executions",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["data"] == []


class TestErrorHandlers:
    @pytest.mark.asyncio
    async def test_bad_request(self) -> None:
        app = Quart(__name__)
        async with app.app_context():
            response = await bad_request(ValueError("bad"))
        assert response[1] == 400

    @pytest.mark.asyncio
    async def test_unauthorized(self) -> None:
        app = Quart(__name__)
        async with app.app_context():
            response = await unauthorized(Exception())
        assert response[1] == 401

    @pytest.mark.asyncio
    async def test_forbidden(self) -> None:
        app = Quart(__name__)
        async with app.app_context():
            response = await forbidden(Exception())
        assert response[1] == 403

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        app = Quart(__name__)
        async with app.app_context():
            response = await not_found(Exception())
        assert response[1] == 404

    @pytest.mark.asyncio
    async def test_internal_error(self) -> None:
        app = Quart(__name__)
        async with app.app_context():
            response = await internal_error(Exception("boom"))
        assert response[1] == 500


class TestRegisterExecutionApi:
    def test_registers_blueprint_and_engine(self) -> None:
        app = Quart(__name__)
        engine = _FakeWorkflowEngine()
        register_execution_api(app, engine)
        assert app.config["workflow_engine"] is engine
        assert "execution_api" in app.blueprints
