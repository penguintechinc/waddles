"""`services/workflow_service.py` -- workflow CRUD with license/permission/validation gating.

`self.dal.executesql(...)` is called synchronously (matches the raw pydal
`DAL` app.py actually injects, same as `permission_service.py` -- see that
suite's docstring). Fake `dal` here is a plain `MagicMock`/callable, never
`AsyncMock`.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.license_service import LicenseValidationException
from services.workflow_service import (
    WorkflowNotFoundException,
    WorkflowPermissionException,
    WorkflowService,
    WorkflowServiceException,
)


def _get_workflow_row(workflow_id: str = "wf-1") -> tuple:
    now = datetime.utcnow()
    return (
        workflow_id, 1, 2, "My Workflow", "desc", "1.0.0",
        "draft", False, {}, [], "command", {},
        300, 100, {}, 0, 0, 0, None,
        1, 1, now, now,
    )


def _list_row() -> tuple:
    now = datetime.utcnow()
    return ("wf-1", "My Workflow", "desc", "draft", False, "command", 0, 0, 0, now, now, None)


def _service(dal: MagicMock | None = None) -> tuple[WorkflowService, MagicMock, MagicMock, MagicMock, MagicMock]:
    dal = dal or MagicMock()
    license_service = MagicMock()
    license_service.validate_workflow_creation = AsyncMock()
    permission_service = MagicMock()
    permission_service.check_permission = AsyncMock(return_value=True)
    permission_service.list_workflows_for_user = AsyncMock(return_value=["wf-1"])
    validation_service = MagicMock()
    svc = WorkflowService(
        dal=dal, license_service=license_service, permission_service=permission_service,
        validation_service=validation_service, logger_instance=MagicMock(),
    )
    return svc, dal, license_service, permission_service, validation_service


class TestExceptions:
    def test_not_found_exception(self) -> None:
        exc = WorkflowNotFoundException("wf-1")
        assert exc.status_code == 404

    def test_permission_exception(self) -> None:
        exc = WorkflowPermissionException("wf-1", "can_edit")
        assert exc.status_code == 403
        assert "can_edit" in exc.message


class TestCreateWorkflow:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        svc, dal, license_service, _, _ = _service()
        dal.executesql.return_value = [(1,)]
        result = await svc.create_workflow(
            {"name": "Test WF"}, community_id=1, entity_id=2, user_id=3
        )
        assert result["metadata"]["name"] == "Test WF"
        license_service.validate_workflow_creation.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_license_exception_propagates(self) -> None:
        svc, dal, license_service, _, _ = _service()
        license_service.validate_workflow_creation = AsyncMock(
            side_effect=LicenseValidationException("no license", community_id=1)
        )
        with pytest.raises(LicenseValidationException):
            await svc.create_workflow({"name": "x"}, 1, 2, 3)

    @pytest.mark.asyncio
    async def test_db_error_wraps_exception(self) -> None:
        svc, dal, _, _, _ = _service()
        dal.executesql.side_effect = Exception("db down")
        with pytest.raises(WorkflowServiceException):
            await svc.create_workflow({"name": "x"}, 1, 2, 3)


class TestGetWorkflow:
    @pytest.mark.asyncio
    async def test_permission_denied(self) -> None:
        svc, dal, _, permission_service, _ = _service()
        permission_service.check_permission = AsyncMock(return_value=False)
        with pytest.raises(WorkflowPermissionException):
            await svc.get_workflow("wf-1", user_id=1)

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        svc, dal, _, _, _ = _service()
        dal.executesql.return_value = []
        with pytest.raises(WorkflowNotFoundException):
            await svc.get_workflow("wf-1", user_id=1)

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        svc, dal, _, _, _ = _service()
        dal.executesql.return_value = [_get_workflow_row()]
        result = await svc.get_workflow("wf-1", user_id=1, community_id=5)
        assert result["workflow_id"] == "wf-1"
        assert result["name"] == "My Workflow"

    @pytest.mark.asyncio
    async def test_db_error_wraps_exception(self) -> None:
        svc, dal, _, _, _ = _service()
        dal.executesql.side_effect = Exception("db down")
        with pytest.raises(WorkflowServiceException):
            await svc.get_workflow("wf-1", user_id=1)


class TestUpdateWorkflow:
    @pytest.mark.asyncio
    async def test_permission_denied(self) -> None:
        svc, dal, _, permission_service, _ = _service()
        permission_service.check_permission = AsyncMock(return_value=False)
        with pytest.raises(WorkflowPermissionException):
            await svc.update_workflow("wf-1", {"name": "New"}, user_id=1)

    @pytest.mark.asyncio
    async def test_no_valid_fields_raises(self) -> None:
        svc, dal, _, _, _ = _service()
        with pytest.raises(WorkflowServiceException, match="No valid fields"):
            await svc.update_workflow("wf-1", {"bogus_field": "x"}, user_id=1)

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        svc, dal, _, _, _ = _service()
        dal.executesql.return_value = []
        with pytest.raises(WorkflowNotFoundException):
            await svc.update_workflow("wf-1", {"name": "New"}, user_id=1)

    @pytest.mark.asyncio
    async def test_success_fetches_updated_workflow(self) -> None:
        svc, dal, _, _, _ = _service()

        def _executesql(query: str, params: list) -> list:
            if query.strip().startswith("UPDATE"):
                return [("wf-1",)]
            if query.strip().startswith("SELECT"):
                return [_get_workflow_row()]
            return []

        dal.executesql.side_effect = _executesql
        result = await svc.update_workflow(
            "wf-1", {"name": "Updated", "nodes": {"a": 1}}, user_id=1, community_id=2
        )
        assert result["workflow_id"] == "wf-1"

    @pytest.mark.asyncio
    async def test_db_error_wraps_exception(self) -> None:
        svc, dal, _, _, _ = _service()
        dal.executesql.side_effect = Exception("db down")
        with pytest.raises(WorkflowServiceException):
            await svc.update_workflow("wf-1", {"name": "x"}, user_id=1)


class TestDeleteWorkflow:
    @pytest.mark.asyncio
    async def test_permission_denied(self) -> None:
        svc, dal, _, permission_service, _ = _service()
        permission_service.check_permission = AsyncMock(return_value=False)
        with pytest.raises(WorkflowPermissionException):
            await svc.delete_workflow("wf-1", user_id=1)

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        svc, dal, _, _, _ = _service()
        dal.executesql.return_value = []
        with pytest.raises(WorkflowNotFoundException):
            await svc.delete_workflow("wf-1", user_id=1)

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        svc, dal, _, _, _ = _service()
        dal.executesql.return_value = [("wf-1",)]
        result = await svc.delete_workflow("wf-1", user_id=1, community_id=2)
        assert result["status"] == "archived"

    @pytest.mark.asyncio
    async def test_db_error_wraps_exception(self) -> None:
        svc, dal, _, _, _ = _service()
        dal.executesql.side_effect = Exception("db down")
        with pytest.raises(WorkflowServiceException):
            await svc.delete_workflow("wf-1", user_id=1)


class TestListWorkflows:
    @pytest.mark.asyncio
    async def test_no_accessible_workflows_returns_empty(self) -> None:
        svc, dal, _, permission_service, _ = _service()
        permission_service.list_workflows_for_user = AsyncMock(return_value=[])
        result = await svc.list_workflows(entity_id=1, user_id=1)
        assert result["workflows"] == []
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_success_with_filters(self) -> None:
        svc, dal, _, _, _ = _service()

        def _executesql(query: str, params: list) -> list:
            if query.strip().startswith("SELECT COUNT"):
                return [(1,)]
            return [_list_row()]

        dal.executesql.side_effect = _executesql
        result = await svc.list_workflows(
            entity_id=1, user_id=1,
            filters={"status": "draft", "search": "My"},
            page=1, per_page=10,
        )
        assert result["total"] == 1
        assert len(result["workflows"]) == 1
        assert result["workflows"][0]["workflow_id"] == "wf-1"

    @pytest.mark.asyncio
    async def test_db_error_wraps_exception(self) -> None:
        svc, dal, _, _, _ = _service()
        dal.executesql.side_effect = Exception("db down")
        with pytest.raises(WorkflowServiceException):
            await svc.list_workflows(entity_id=1, user_id=1)


class TestPublishWorkflow:
    @pytest.mark.asyncio
    async def test_permission_denied(self) -> None:
        svc, dal, _, permission_service, _ = _service()
        permission_service.check_permission = AsyncMock(return_value=False)
        with pytest.raises(WorkflowPermissionException):
            await svc.publish_workflow("wf-1", user_id=1)

    @pytest.mark.asyncio
    async def test_validation_failure_raises(self) -> None:
        svc, dal, _, _, _ = _service()
        svc.validate_workflow = AsyncMock(
            return_value={"is_valid": False, "errors": ["no trigger"]}
        )
        with pytest.raises(WorkflowServiceException, match="validation failed"):
            await svc.publish_workflow("wf-1", user_id=1)

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        svc, dal, _, _, _ = _service()
        svc.validate_workflow = AsyncMock(return_value={"is_valid": True, "errors": []})
        dal.executesql.return_value = []
        with pytest.raises(WorkflowNotFoundException):
            await svc.publish_workflow("wf-1", user_id=1)

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        svc, dal, _, _, _ = _service()
        svc.validate_workflow = AsyncMock(return_value={"is_valid": True, "errors": []})

        def _executesql(query: str, params: list) -> list:
            if query.strip().startswith("UPDATE"):
                return [("wf-1",)]
            return [_get_workflow_row()]

        dal.executesql.side_effect = _executesql
        result = await svc.publish_workflow("wf-1", user_id=1, community_id=2)
        assert result["workflow_id"] == "wf-1"

    @pytest.mark.asyncio
    async def test_db_error_wraps_exception(self) -> None:
        svc, dal, _, _, _ = _service()
        svc.validate_workflow = AsyncMock(return_value={"is_valid": True, "errors": []})
        dal.executesql.side_effect = Exception("db down")
        with pytest.raises(WorkflowServiceException):
            await svc.publish_workflow("wf-1", user_id=1)


class TestValidateWorkflow:
    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        svc, dal, _, _, _ = _service()
        dal.executesql.return_value = []
        with pytest.raises(WorkflowNotFoundException):
            await svc.validate_workflow("wf-1")

    @pytest.mark.asyncio
    async def test_success_runs_validation_service(self) -> None:
        svc, dal, _, _, validation_service = _service()
        dal.executesql.return_value = [({}, [], "command", {}, "My WF", "desc")]
        fake_result = MagicMock()
        fake_result.is_valid = False
        fake_result.errors = ["Workflow must contain at least one node"]
        fake_result.to_dict.return_value = {"is_valid": False, "errors": fake_result.errors}
        validation_service.validate_workflow.return_value = fake_result
        result = await svc.validate_workflow("wf-1")
        assert result["is_valid"] is False
        validation_service.validate_workflow.assert_called_once()

    @pytest.mark.asyncio
    async def test_db_error_wraps_exception(self) -> None:
        svc, dal, _, _, _ = _service()
        dal.executesql.side_effect = Exception("db down")
        with pytest.raises(WorkflowServiceException):
            await svc.validate_workflow("wf-1")


class TestLogAudit:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        svc, dal, _, _, _ = _service()
        await svc._log_audit("wf-1", "created", 1, changes={"a": 1}, metadata={"b": 2})
        dal.executesql.assert_called_once()

    @pytest.mark.asyncio
    async def test_db_error_is_swallowed(self) -> None:
        svc, dal, _, _, _ = _service()
        dal.executesql.side_effect = Exception("db down")
        await svc._log_audit("wf-1", "created", 1)  # must not raise
