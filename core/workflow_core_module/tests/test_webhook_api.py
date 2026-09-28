"""`controllers/webhook_api.py` -- webhook trigger/management endpoints.

This blueprint has 0% coverage and is never registered by `app.py`
(`register_webhook_api` has no caller) -- while writing this suite,
`WorkflowPermissionException("Permission denied")` was found to raise
`TypeError` (the real signature is `(workflow_id, permission)`) on every
permission-denied branch in `list_webhooks`/`create_webhook`/
`delete_webhook`; fixed at the call sites. The underlying `dal.table()`/
`dal.select()`/`dal.insert()`/`dal.update()`/`dal.delete()` calls also don't
match either DAL shape used elsewhere in this codebase (raw pydal `DAL` or
flask_core's `AsyncDAL.select_async`/etc) -- this is a pre-existing,
unregistered-blueprint integration gap out of scope for a coverage pass;
this suite mocks `dal` at the same interface `webhook_api.py` itself calls
so the module's own branching logic (HMAC verification, IP allowlist,
rate limiting, permission checks, error handling) is exercised.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from config import Config
from controllers.webhook_api import (
    WebhookConfig,
    WebhookExecutionResult,
    WebhookRateLimiter,
    bad_request,
    check_ip_allowlist,
    forbidden,
    generate_webhook_secret,
    generate_webhook_token,
    get_webhook_by_token,
    get_webhook_from_db,
    internal_error,
    not_found,
    register_webhook_api,
    unauthorized,
    update_webhook_trigger_stats,
    verify_webhook_signature,
)
from flask_core.auth import create_jwt_token
from services.workflow_service import (
    WorkflowNotFoundException,
    WorkflowPermissionException,
)

SECRET = Config.SECRET_KEY


def _token(*, sub: str = "1") -> str:
    return create_jwt_token(
        user_id=sub, username=f"user{sub}", email=f"user{sub}@example.com",
        roles=[], secret_key=SECRET, tenant="t1",
    )


def _row(**overrides: Any) -> SimpleNamespace:
    defaults = dict(
        webhook_id="wh-1", workflow_id="wf-1", token="tok123", secret="sec123",
        name="My Webhook", description=None, url="https://x/y", enabled=True,
        require_signature=True, ip_allowlist=[], rate_limit_max=60,
        rate_limit_window=60, created_at=None, updated_at=None,
        last_triggered_at=None, trigger_count=0,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _fake_dal(select_return: list[Any] | None = None) -> MagicMock:
    dal = MagicMock()
    dal.table = MagicMock(return_value=MagicMock())
    dal.select = AsyncMock(return_value=select_return if select_return is not None else [])
    dal.insert = AsyncMock(return_value=None)
    dal.update = AsyncMock(return_value=None)
    dal.delete = AsyncMock(return_value=None)
    return dal


def _build_app(*, dal: MagicMock, workflow_service: Any, permission_service: Any, workflow_engine: Any) -> Quart:
    app = Quart(__name__)
    app.config["dal"] = dal
    register_webhook_api(app, workflow_service, permission_service, workflow_engine)
    return app


class TestPureHelpers:
    def test_generate_webhook_token_and_secret_are_hex32(self) -> None:
        token = generate_webhook_token()
        secret = generate_webhook_secret()
        assert len(token) == 32 and len(secret) == 32
        int(token, 16)
        int(secret, 16)

    def test_verify_webhook_signature_valid(self) -> None:
        token, secret, body = "tok", "sec", b'{"a":1}'
        message = token.encode() + body
        sig = "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
        assert verify_webhook_signature(token, secret, body, sig) is True

    def test_verify_webhook_signature_invalid(self) -> None:
        assert verify_webhook_signature("tok", "sec", b"{}", "sha256=deadbeef") is False

    @pytest.mark.parametrize(
        "ip,allowlist,expected",
        [
            ("1.2.3.4", [], True),
            ("192.168.1.5", ["192.168.1.0/24"], True),
            ("192.168.2.5", ["192.168.1.0/24"], False),
            ("10.0.0.1", ["10.0.0.1"], True),
            ("10.0.0.2", ["10.0.0.1"], False),
            ("not-an-ip", ["10.0.0.1"], False),
        ],
    )
    def test_check_ip_allowlist(self, ip: str, allowlist: list[str], expected: bool) -> None:
        assert check_ip_allowlist(ip, allowlist) is expected

    def test_webhook_config_to_dict_excludes_secret(self) -> None:
        webhook = WebhookConfig(
            webhook_id="wh-1", workflow_id="wf-1", token="tok", secret="shh", name="n"
        )
        data = webhook.to_dict()
        assert "secret" not in data
        assert data["ip_allowlist"] == []

    def test_webhook_execution_result_fields(self) -> None:
        result = WebhookExecutionResult(
            execution_id="e1", workflow_id="wf-1", webhook_id="wh-1",
            status="queued", timestamp="2024-01-01T00:00:00Z", trigger_data={},
        )
        assert result.error_message is None


class TestWebhookRateLimiter:
    def test_allows_under_limit_and_blocks_over(self) -> None:
        limiter = WebhookRateLimiter()
        for _ in range(3):
            allowed, remaining = limiter.check_rate_limit("wh-1", max_requests=3, window_seconds=60)
            assert allowed is True
        allowed, remaining = limiter.check_rate_limit("wh-1", max_requests=3, window_seconds=60)
        assert allowed is False
        assert remaining == 0

    def test_window_expiry_resets_count(self) -> None:
        limiter = WebhookRateLimiter()
        allowed, _ = limiter.check_rate_limit("wh-2", max_requests=1, window_seconds=-1)
        assert allowed is True
        # Negative window means every prior entry is already "outside" the window.
        allowed_again, _ = limiter.check_rate_limit("wh-2", max_requests=1, window_seconds=-1)
        assert allowed_again is True


class TestDbHelpers:
    @pytest.mark.asyncio
    async def test_get_webhook_from_db_found(self) -> None:
        dal = _fake_dal(select_return=[_row()])
        webhook = await get_webhook_from_db(dal, "wf-1", "wh-1")
        assert webhook is not None
        assert webhook.webhook_id == "wh-1"

    @pytest.mark.asyncio
    async def test_get_webhook_from_db_not_found(self) -> None:
        dal = _fake_dal(select_return=[])
        assert await get_webhook_from_db(dal, "wf-1", "wh-1") is None

    @pytest.mark.asyncio
    async def test_get_webhook_from_db_swallows_exception(self) -> None:
        dal = MagicMock()
        dal.table = MagicMock(side_effect=Exception("db down"))
        assert await get_webhook_from_db(dal, "wf-1", "wh-1") is None

    @pytest.mark.asyncio
    async def test_get_webhook_by_token_found(self) -> None:
        dal = _fake_dal(select_return=[_row()])
        webhook = await get_webhook_by_token(dal, "tok123")
        assert webhook.token == "tok123"

    @pytest.mark.asyncio
    async def test_get_webhook_by_token_not_found(self) -> None:
        dal = _fake_dal(select_return=[])
        assert await get_webhook_by_token(dal, "missing") is None

    @pytest.mark.asyncio
    async def test_get_webhook_by_token_swallows_exception(self) -> None:
        dal = MagicMock()
        dal.table = MagicMock(side_effect=Exception("db down"))
        assert await get_webhook_by_token(dal, "tok") is None

    @pytest.mark.asyncio
    async def test_update_webhook_trigger_stats_success(self) -> None:
        dal = _fake_dal()
        await update_webhook_trigger_stats(dal, "wh-1", "exec-1")
        dal.update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_webhook_trigger_stats_swallows_exception(self) -> None:
        dal = MagicMock()
        dal.table = MagicMock(side_effect=Exception("db down"))
        await update_webhook_trigger_stats(dal, "wh-1", "exec-1")  # must not raise


class TestErrorHandlers:
    """`jsonify` inside `error_response` needs a bound Quart app context."""

    @pytest.mark.asyncio
    async def test_bad_request_handler(self) -> None:
        app = Quart(__name__)
        async with app.app_context():
            response = await bad_request(ValueError("bad"))
        assert response[1] == 400

    @pytest.mark.asyncio
    async def test_unauthorized_handler(self) -> None:
        app = Quart(__name__)
        async with app.app_context():
            response = await unauthorized(Exception())
        assert response[1] == 401

    @pytest.mark.asyncio
    async def test_forbidden_handler(self) -> None:
        app = Quart(__name__)
        async with app.app_context():
            response = await forbidden(Exception())
        assert response[1] == 403

    @pytest.mark.asyncio
    async def test_not_found_handler(self) -> None:
        app = Quart(__name__)
        async with app.app_context():
            response = await not_found(Exception())
        assert response[1] == 404

    @pytest.mark.asyncio
    async def test_internal_error_handler(self) -> None:
        app = Quart(__name__)
        async with app.app_context():
            response = await internal_error(Exception("boom"))
        assert response[1] == 500


class TestRegisterWebhookApi:
    def test_register_stores_services_and_blueprint(self) -> None:
        dal = _fake_dal()
        workflow_service, permission_service, workflow_engine = MagicMock(), MagicMock(), MagicMock()
        app = _build_app(
            dal=dal, workflow_service=workflow_service,
            permission_service=permission_service, workflow_engine=workflow_engine,
        )
        assert app.config["workflow_service"] is workflow_service
        assert app.config["permission_service"] is permission_service
        assert app.config["workflow_engine"] is workflow_engine
        assert "webhook_api" in app.blueprints


class TestTriggerWebhookPublic:
    @pytest.mark.asyncio
    async def test_unknown_token_is_404(self) -> None:
        dal = _fake_dal(select_return=[])
        app = _build_app(
            dal=dal, workflow_service=MagicMock(), permission_service=MagicMock(),
            workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.post("/api/v1/workflows/webhooks/unknown", json={})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_disabled_webhook_is_403(self) -> None:
        dal = _fake_dal(select_return=[_row(enabled=False)])
        app = _build_app(
            dal=dal, workflow_service=MagicMock(), permission_service=MagicMock(),
            workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.post("/api/v1/workflows/webhooks/tok123", json={})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_ip_not_allowed_is_403(self) -> None:
        dal = _fake_dal(select_return=[_row(ip_allowlist=["10.0.0.1"])])
        app = _build_app(
            dal=dal, workflow_service=MagicMock(), permission_service=MagicMock(),
            workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.post("/api/v1/workflows/webhooks/tok123", json={})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_signature_is_403(self) -> None:
        dal = _fake_dal(select_return=[_row(require_signature=True)])
        app = _build_app(
            dal=dal, workflow_service=MagicMock(), permission_service=MagicMock(),
            workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.post("/api/v1/workflows/webhooks/tok123", json={})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_invalid_signature_is_403(self) -> None:
        dal = _fake_dal(select_return=[_row(require_signature=True)])
        app = _build_app(
            dal=dal, workflow_service=MagicMock(), permission_service=MagicMock(),
            workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.post(
                "/api/v1/workflows/webhooks/tok123",
                json={"a": 1},
                headers={"X-Webhook-Signature": "sha256=deadbeef"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded_is_429(self) -> None:
        import controllers.webhook_api as webhook_api_module

        dal = _fake_dal(select_return=[_row(require_signature=False, rate_limit_max=0)])
        app = _build_app(
            dal=dal, workflow_service=MagicMock(), permission_service=MagicMock(),
            workflow_engine=MagicMock(),
        )
        webhook_api_module.webhook_rate_limiter = WebhookRateLimiter()
        async with app.test_client() as client:
            resp = await client.post("/api/v1/workflows/webhooks/tok123", json={})
        assert resp.status_code == 429

    @pytest.mark.asyncio
    async def test_successful_trigger_returns_200(self) -> None:
        dal = _fake_dal(select_return=[_row(require_signature=False)])
        workflow_engine = MagicMock()
        workflow_engine.execute_workflow = AsyncMock(
            return_value=SimpleNamespace(execution_id="exec-99")
        )
        app = _build_app(
            dal=dal, workflow_service=MagicMock(), permission_service=MagicMock(),
            workflow_engine=workflow_engine,
        )
        async with app.test_client() as client:
            resp = await client.post("/api/v1/workflows/webhooks/tok123", json={"hello": "world"})
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["data"]["execution_id"] == "exec-99"

    @pytest.mark.asyncio
    async def test_execution_failure_is_500(self) -> None:
        dal = _fake_dal(select_return=[_row(require_signature=False)])
        workflow_engine = MagicMock()
        workflow_engine.execute_workflow = AsyncMock(side_effect=Exception("engine exploded"))
        app = _build_app(
            dal=dal, workflow_service=MagicMock(), permission_service=MagicMock(),
            workflow_engine=workflow_engine,
        )
        async with app.test_client() as client:
            resp = await client.post("/api/v1/workflows/webhooks/tok123", json={})
        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_empty_body_skips_json_parse(self) -> None:
        dal = _fake_dal(select_return=[_row(require_signature=False)])
        workflow_engine = MagicMock()
        workflow_engine.execute_workflow = AsyncMock(
            return_value=SimpleNamespace(execution_id="exec-1")
        )
        app = _build_app(
            dal=dal, workflow_service=MagicMock(), permission_service=MagicMock(),
            workflow_engine=workflow_engine,
        )
        async with app.test_client() as client:
            resp = await client.post("/api/v1/workflows/webhooks/tok123", data=b"")
        assert resp.status_code == 200


class TestListWebhooks:
    @pytest.mark.asyncio
    async def test_success_returns_webhook_list(self) -> None:
        dal = _fake_dal(select_return=[_row()])
        workflow_service = MagicMock()
        workflow_service.get_workflow = AsyncMock(return_value={"workflow_id": "wf-1"})
        permission_service = MagicMock()
        permission_service.check_permission = AsyncMock(
            return_value=SimpleNamespace(can_view=True, can_edit=True)
        )
        app = _build_app(
            dal=dal, workflow_service=workflow_service,
            permission_service=permission_service, workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.get(
                "/api/v1/workflows/wf-1/webhooks",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert len(body["data"]) == 1

    @pytest.mark.asyncio
    async def test_permission_denied_is_403(self) -> None:
        dal = _fake_dal()
        workflow_service = MagicMock()
        workflow_service.get_workflow = AsyncMock(return_value={"workflow_id": "wf-1"})
        permission_service = MagicMock()
        permission_service.check_permission = AsyncMock(
            return_value=SimpleNamespace(can_view=False, can_edit=False)
        )
        app = _build_app(
            dal=dal, workflow_service=workflow_service,
            permission_service=permission_service, workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.get(
                "/api/v1/workflows/wf-1/webhooks",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_workflow_not_found_is_404(self) -> None:
        dal = _fake_dal()
        workflow_service = MagicMock()
        workflow_service.get_workflow = AsyncMock(
            side_effect=WorkflowNotFoundException("wf-1")
        )
        app = _build_app(
            dal=dal, workflow_service=workflow_service,
            permission_service=MagicMock(), workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.get(
                "/api/v1/workflows/wf-1/webhooks",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_no_auth_is_401(self) -> None:
        app = _build_app(
            dal=_fake_dal(), workflow_service=MagicMock(),
            permission_service=MagicMock(), workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.get("/api/v1/workflows/wf-1/webhooks")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_db_error_is_500(self) -> None:
        dal = MagicMock()
        dal.table = MagicMock(side_effect=Exception("db exploded"))
        workflow_service = MagicMock()
        workflow_service.get_workflow = AsyncMock(return_value={"workflow_id": "wf-1"})
        permission_service = MagicMock()
        permission_service.check_permission = AsyncMock(
            return_value=SimpleNamespace(can_view=True, can_edit=True)
        )
        app = _build_app(
            dal=dal, workflow_service=workflow_service,
            permission_service=permission_service, workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.get(
                "/api/v1/workflows/wf-1/webhooks",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 500


class TestCreateWebhook:
    @pytest.mark.asyncio
    async def test_success_returns_201(self) -> None:
        dal = _fake_dal()
        workflow_service = MagicMock()
        workflow_service.get_workflow = AsyncMock(return_value={"workflow_id": "wf-1"})
        permission_service = MagicMock()
        permission_service.check_permission = AsyncMock(
            return_value=SimpleNamespace(can_view=True, can_edit=True)
        )
        app = _build_app(
            dal=dal, workflow_service=workflow_service,
            permission_service=permission_service, workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.post(
                "/api/v1/workflows/wf-1/webhooks",
                json={"name": "My Hook"},
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 201
        body = await resp.get_json()
        assert body["data"]["name"] == "My Hook"
        assert "secret" not in body["data"]

    @pytest.mark.asyncio
    async def test_missing_body_is_400(self) -> None:
        app = _build_app(
            dal=_fake_dal(), workflow_service=MagicMock(),
            permission_service=MagicMock(), workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.post(
                "/api/v1/workflows/wf-1/webhooks",
                json={},
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_missing_name_field_is_400(self) -> None:
        app = _build_app(
            dal=_fake_dal(), workflow_service=MagicMock(),
            permission_service=MagicMock(), workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.post(
                "/api/v1/workflows/wf-1/webhooks",
                json={"description": "no name"},
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_permission_denied_is_403(self) -> None:
        workflow_service = MagicMock()
        workflow_service.get_workflow = AsyncMock(return_value={"workflow_id": "wf-1"})
        permission_service = MagicMock()
        permission_service.check_permission = AsyncMock(
            return_value=SimpleNamespace(can_view=False, can_edit=False)
        )
        app = _build_app(
            dal=_fake_dal(), workflow_service=workflow_service,
            permission_service=permission_service, workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.post(
                "/api/v1/workflows/wf-1/webhooks",
                json={"name": "Hook"},
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_db_error_is_500(self) -> None:
        dal = MagicMock()
        dal.table = MagicMock(return_value=MagicMock())
        dal.insert = AsyncMock(side_effect=Exception("insert failed"))
        workflow_service = MagicMock()
        workflow_service.get_workflow = AsyncMock(return_value={"workflow_id": "wf-1"})
        permission_service = MagicMock()
        permission_service.check_permission = AsyncMock(
            return_value=SimpleNamespace(can_view=True, can_edit=True)
        )
        app = _build_app(
            dal=dal, workflow_service=workflow_service,
            permission_service=permission_service, workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.post(
                "/api/v1/workflows/wf-1/webhooks",
                json={"name": "Hook"},
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 500


class TestDeleteWebhook:
    @pytest.mark.asyncio
    async def test_success_returns_200(self) -> None:
        dal = _fake_dal(select_return=[_row()])
        workflow_service = MagicMock()
        workflow_service.get_workflow = AsyncMock(return_value={"workflow_id": "wf-1"})
        permission_service = MagicMock()
        permission_service.check_permission = AsyncMock(
            return_value=SimpleNamespace(can_view=True, can_edit=True)
        )
        app = _build_app(
            dal=dal, workflow_service=workflow_service,
            permission_service=permission_service, workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.delete(
                "/api/v1/workflows/wf-1/webhooks/wh-1",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_permission_denied_is_403(self) -> None:
        workflow_service = MagicMock()
        workflow_service.get_workflow = AsyncMock(return_value={"workflow_id": "wf-1"})
        permission_service = MagicMock()
        permission_service.check_permission = AsyncMock(
            return_value=SimpleNamespace(can_view=False, can_edit=False)
        )
        app = _build_app(
            dal=_fake_dal(), workflow_service=workflow_service,
            permission_service=permission_service, workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.delete(
                "/api/v1/workflows/wf-1/webhooks/wh-1",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_webhook_not_found_is_404(self) -> None:
        dal = _fake_dal(select_return=[])
        workflow_service = MagicMock()
        workflow_service.get_workflow = AsyncMock(return_value={"workflow_id": "wf-1"})
        permission_service = MagicMock()
        permission_service.check_permission = AsyncMock(
            return_value=SimpleNamespace(can_view=True, can_edit=True)
        )
        app = _build_app(
            dal=dal, workflow_service=workflow_service,
            permission_service=permission_service, workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.delete(
                "/api/v1/workflows/wf-1/webhooks/wh-missing",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_db_error_is_500(self) -> None:
        dal = _fake_dal(select_return=[_row()])
        dal.delete = AsyncMock(side_effect=Exception("delete failed"))
        workflow_service = MagicMock()
        workflow_service.get_workflow = AsyncMock(return_value={"workflow_id": "wf-1"})
        permission_service = MagicMock()
        permission_service.check_permission = AsyncMock(
            return_value=SimpleNamespace(can_view=True, can_edit=True)
        )
        app = _build_app(
            dal=dal, workflow_service=workflow_service,
            permission_service=permission_service, workflow_engine=MagicMock(),
        )
        async with app.test_client() as client:
            resp = await client.delete(
                "/api/v1/workflows/wf-1/webhooks/wh-1",
                headers={"Authorization": f"Bearer {_token()}"},
            )
        assert resp.status_code == 500
