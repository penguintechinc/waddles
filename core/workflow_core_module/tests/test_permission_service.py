"""`services/permission_service.py` -- granular owner/user/role/entity workflow permissions.

`self.dal.executesql(...)` is called synchronously throughout (no
`await`) -- correct for the raw pydal `DAL` instance `app.py`'s startup()
actually injects (`dal = async_dal.dal`, whose `.executesql()` is
synchronous; the async variant lives on the wrapper as
`executesql_async`). This suite's fake `dal` mirrors that real shape
(`MagicMock` with a plain, non-async `executesql`), not an `AsyncMock`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.permission_service import GrantResult, PermissionInfo, PermissionService


def _service(dal: MagicMock | None = None) -> tuple[PermissionService, MagicMock, MagicMock]:
    dal = dal or MagicMock()
    logger = MagicMock()
    return PermissionService(dal=dal, logger=logger), dal, logger


class TestPermissionInfo:
    def test_to_dict(self) -> None:
        info = PermissionInfo(can_view=True, can_edit=False)
        d = info.to_dict()
        assert d["can_view"] is True and d["can_edit"] is False

    def test_has_any_permission_true(self) -> None:
        assert PermissionInfo(can_view=True).has_any_permission() is True

    def test_has_any_permission_false(self) -> None:
        assert PermissionInfo().has_any_permission() is False

    def test_bool_dunder(self) -> None:
        assert bool(PermissionInfo(can_execute=True)) is True
        assert bool(PermissionInfo()) is False


class TestGrantResult:
    def test_construction_defaults(self) -> None:
        result = GrantResult(success=True, message="ok")
        assert result.workflow_id is None
        assert result.error is None


class TestCheckPermission:
    @pytest.mark.asyncio
    async def test_invalid_permission_type_returns_false(self) -> None:
        svc, dal, logger = _service()
        result = await svc.check_permission("wf-1", 1, "not_a_permission")
        assert result is False
        logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_owner_has_permission(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = [(1,)]  # created_by == user_id
        result = await svc.check_permission("wf-1", 1, "can_edit")
        assert result is True
        logger.audit.assert_called_once()

    @pytest.mark.asyncio
    async def test_user_level_permission_grants_access(self) -> None:
        svc, dal, logger = _service()

        def _executesql(query: str, params: list) -> list:
            if "created_by" in query:
                return [(999,)]  # not the owner
            if "workflow_permissions" in query and "permission_type" in query:
                return [(True, False, False, False, False)]
            return []

        dal.executesql.side_effect = _executesql
        result = await svc.check_permission("wf-1", 1, "can_view")
        assert result is True

    @pytest.mark.asyncio
    async def test_role_level_permission_grants_access(self) -> None:
        svc, dal, logger = _service()

        def _executesql(query: str, params: list) -> list:
            if "created_by" in query:
                return [(999,)]
            if "user_roles" in query:
                return [(5,)]
            if "workflow_permissions" in query and params and params[-1] == 5:
                return [(False, True, False, False, False)]
            return []

        dal.executesql.side_effect = _executesql
        result = await svc.check_permission("wf-1", 1, "can_edit", community_id=10)
        assert result is True

    @pytest.mark.asyncio
    async def test_entity_level_permission_grants_access(self) -> None:
        svc, dal, logger = _service()

        def _executesql(query: str, params: list) -> list:
            if "created_by" in query:
                return [(999,)]
            if "user_roles" in query:
                return []
            if "entity_id FROM workflows" in query:
                return [(42,)]
            if "permission_type = 'entity'" in query:
                return [(False, False, True, False, False)]
            return []

        dal.executesql.side_effect = _executesql
        result = await svc.check_permission("wf-1", 1, "can_execute")
        assert result is True

    @pytest.mark.asyncio
    async def test_no_matching_permission_denies(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = []
        result = await svc.check_permission("wf-1", 1, "can_view")
        assert result is False
        logger.authz.assert_called_with(
            action="check_permission", user="1", community="", result="DENIED",
            extra={"workflow_id": "wf-1", "permission": "can_view"},
        )

    @pytest.mark.asyncio
    async def test_unexpected_exception_returns_false(self) -> None:
        svc, dal, logger = _service()
        svc._is_workflow_owner = MagicMock(side_effect=Exception("boom"))
        # _is_workflow_owner is awaited, so make it a coroutine function
        async def _raise(*a, **k):
            raise Exception("boom")
        svc._is_workflow_owner = _raise
        result = await svc.check_permission("wf-1", 1, "can_view")
        assert result is False
        logger.error.assert_called()


class TestGrantPermission:
    @pytest.mark.asyncio
    async def test_invalid_target_type(self) -> None:
        svc, dal, logger = _service()
        result = await svc.grant_permission("wf-1", "bogus", 1, {"can_view": True})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_invalid_permission_key(self) -> None:
        svc, dal, logger = _service()
        result = await svc.grant_permission("wf-1", "user", 1, {"can_fly": True})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_updates_existing_permission(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = [(True, False, False, False, False)]
        result = await svc.grant_permission(
            "wf-1", "user", 1, {"can_edit": True}, granted_by=99
        )
        assert result.success is True
        assert result.message == "Permission updated"

    @pytest.mark.asyncio
    async def test_inserts_new_permission(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = []
        result = await svc.grant_permission("wf-1", "role", 5, {"can_view": True})
        assert result.success is True
        assert result.message == "Permission granted"

    @pytest.mark.asyncio
    async def test_db_error_returns_failure(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.side_effect = Exception("db down")
        result = await svc.grant_permission("wf-1", "user", 1, {"can_view": True})
        assert result.success is False
        # SECURITY (PII in logs): the failure text is value-free (type only) -- never the
        # driver message, which can embed bound values.
        assert result.error == "type=Exception"
        assert "db down" not in result.message


class TestRevokePermission:
    @pytest.mark.asyncio
    async def test_invalid_target_type(self) -> None:
        svc, dal, logger = _service()
        result = await svc.revoke_permission("wf-1", "bogus", 1)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        svc, dal, logger = _service()
        result = await svc.revoke_permission("wf-1", "user", 1, revoked_by=5)
        assert result.success is True
        assert result.message == "Permission revoked"

    @pytest.mark.asyncio
    async def test_db_error_returns_failure(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.side_effect = Exception("db down")
        result = await svc.revoke_permission("wf-1", "user", 1)
        assert result.success is False


class TestGetUserPermissions:
    @pytest.mark.asyncio
    async def test_owner_gets_full_permissions(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = [(1,)]
        info = await svc.get_user_permissions("wf-1", 1)
        assert info.can_manage_permissions is True

    @pytest.mark.asyncio
    async def test_combines_user_role_entity_permissions(self) -> None:
        svc, dal, logger = _service()

        def _executesql(query: str, params: list) -> list:
            if "created_by" in query:
                return [(999,)]
            if "user_roles" in query:
                return [(5,)]
            if "target_type" not in query and "permission_type" in query and len(params) == 3:
                # user-level lookup
                if params[1] == "user":
                    return [(True, False, False, False, False)]
                if params[1] == "role":
                    return [(False, True, False, False, False)]
            if "LIMIT 1" in query:
                return [(False, False, True, False, False)]
            return []

        dal.executesql.side_effect = _executesql
        info = await svc.get_user_permissions("wf-1", 1, community_id=1)
        assert info.can_view is True
        assert info.can_edit is True
        assert info.can_execute is True

    @pytest.mark.asyncio
    async def test_exception_returns_empty_permission_info(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.side_effect = Exception("db down")
        info = await svc.get_user_permissions("wf-1", 1)
        assert info == PermissionInfo()
        logger.error.assert_called()


class TestListWorkflowsForUser:
    @pytest.mark.asyncio
    async def test_invalid_permission_returns_empty(self) -> None:
        svc, dal, logger = _service()
        result = await svc.list_workflows_for_user(1, 10, permission="can_fly")
        assert result == []

    @pytest.mark.asyncio
    async def test_success_returns_workflow_ids(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = [("wf-1",), ("wf-2",)]
        result = await svc.list_workflows_for_user(1, 10, permission="can_view", community_id=5)
        assert result == ["wf-1", "wf-2"]

    @pytest.mark.asyncio
    async def test_db_error_returns_empty(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.side_effect = Exception("db down")
        result = await svc.list_workflows_for_user(1, 10)
        assert result == []


class TestPrivateHelpers:
    @pytest.mark.asyncio
    async def test_is_workflow_owner_true(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = [(1,)]
        assert await svc._is_workflow_owner("wf-1", 1) is True

    @pytest.mark.asyncio
    async def test_is_workflow_owner_false(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = [(2,)]
        assert await svc._is_workflow_owner("wf-1", 1) is False

    @pytest.mark.asyncio
    async def test_is_workflow_owner_not_found(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = []
        assert await svc._is_workflow_owner("wf-1", 1) is False

    @pytest.mark.asyncio
    async def test_is_workflow_owner_swallows_exception(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.side_effect = Exception("db down")
        assert await svc._is_workflow_owner("wf-1", 1) is False

    @pytest.mark.asyncio
    async def test_get_permission_found(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = [(True, True, False, False, False)]
        perm = await svc._get_permission("wf-1", "user", 1)
        assert perm.can_view is True and perm.can_edit is True

    @pytest.mark.asyncio
    async def test_get_permission_not_found(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = []
        assert await svc._get_permission("wf-1", "user", 1) is None

    @pytest.mark.asyncio
    async def test_get_permission_swallows_exception(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.side_effect = Exception("db down")
        assert await svc._get_permission("wf-1", "user", 1) is None

    @pytest.mark.asyncio
    async def test_get_user_roles_with_community(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = [(1,), (2,)]
        roles = await svc._get_user_roles(1, community_id=5)
        assert roles == [1, 2]

    @pytest.mark.asyncio
    async def test_get_user_roles_without_community(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = [(3,)]
        roles = await svc._get_user_roles(1)
        assert roles == [3]

    @pytest.mark.asyncio
    async def test_get_user_roles_empty(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = []
        assert await svc._get_user_roles(1) == []

    @pytest.mark.asyncio
    async def test_get_user_roles_swallows_exception(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.side_effect = Exception("db down")
        assert await svc._get_user_roles(1) == []

    @pytest.mark.asyncio
    async def test_get_workflow_entity_permission_workflow_not_found(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = []
        assert await svc._get_workflow_entity_permission("wf-1", "can_view") is None

    @pytest.mark.asyncio
    async def test_get_workflow_entity_permission_found(self) -> None:
        svc, dal, logger = _service()

        def _executesql(query: str, params: list) -> list:
            if "entity_id FROM workflows" in query:
                return [(42,)]
            return [(True, False, False, False, False)]

        dal.executesql.side_effect = _executesql
        perm = await svc._get_workflow_entity_permission("wf-1", "can_view")
        assert perm.can_view is True

    @pytest.mark.asyncio
    async def test_get_workflow_entity_permission_no_perm_row(self) -> None:
        svc, dal, logger = _service()

        def _executesql(query: str, params: list) -> list:
            if "entity_id FROM workflows" in query:
                return [(42,)]
            return []

        dal.executesql.side_effect = _executesql
        assert await svc._get_workflow_entity_permission("wf-1", "can_view") is None

    @pytest.mark.asyncio
    async def test_get_workflow_entity_permission_swallows_exception(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.side_effect = Exception("db down")
        assert await svc._get_workflow_entity_permission("wf-1", "can_view") is None

    @pytest.mark.asyncio
    async def test_get_all_workflow_entity_permissions_found(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = [(False, False, False, True, False)]
        perm = await svc._get_all_workflow_entity_permissions("wf-1")
        assert perm.can_delete is True

    @pytest.mark.asyncio
    async def test_get_all_workflow_entity_permissions_not_found(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.return_value = []
        assert await svc._get_all_workflow_entity_permissions("wf-1") is None

    @pytest.mark.asyncio
    async def test_get_all_workflow_entity_permissions_swallows_exception(self) -> None:
        svc, dal, logger = _service()
        dal.executesql.side_effect = Exception("db down")
        assert await svc._get_all_workflow_entity_permissions("wf-1") is None
