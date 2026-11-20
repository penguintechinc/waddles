"""`services/workflow_service.py` -- HTTP client + effective-tier license gate.

`tests/test_v1_workflow_blueprint.py` monkeypatches `WorkflowCoreProxyClient`
entirely (`FakeWorkflowCore`, no real network I/O) -- which means the
actual `httpx` call and success/failure/timeout branching inside
`WorkflowCoreProxyClient.request()` is exercised nowhere else. This file
closes that gap with `httpx.MockTransport` (real `httpx` request/response
objects, no real socket), mirroring `tests/test_event_calendar_proxy.py`'s
own pattern for the sibling Event group's proxy client. Also covers
`validate_license()`'s DB-driven branches directly (no HTTP involved).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from services import community_tier_service as tier_svc
from services.errors import ApiError
from services.workflow_service import (
    ProxyResult,
    WorkflowCoreProxyClient,
    error_from_proxy,
    validate_license,
)
from tests.ai_routing_helpers import ensure_tier_tables, patch_tenant_tier, set_tier


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Force every `httpx.AsyncClient` in `workflow_service.py` onto a mock transport."""
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


class TestWorkflowCoreProxyClientRequest:
    async def test_success_relays_status_and_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "wf-1", "name": "My Workflow"})

        _patch_transport(monkeypatch, handler)
        client = WorkflowCoreProxyClient(base_url="http://workflow-core:8070")
        result = await client.request("GET", "/workflows/wf-1")
        assert result == ProxyResult(
            ok=True, status_code=200, body={"id": "wf-1", "name": "My Workflow"}
        )

    async def test_sends_expected_method_and_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            return httpx.Response(201, json={})

        _patch_transport(monkeypatch, handler)
        client = WorkflowCoreProxyClient(base_url="http://workflow-core:8070")
        await client.request("POST", "/workflows", json_body={"name": "x"})
        assert captured["method"] == "POST"
        assert captured["url"] == "http://workflow-core:8070/api/v1/workflows"

    async def test_query_params_are_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["query"] = dict(request.url.params)
            return httpx.Response(200, json={})

        _patch_transport(monkeypatch, handler)
        client = WorkflowCoreProxyClient(base_url="http://workflow-core:8070")
        await client.request("GET", "/workflows", params={"communityId": 1, "page": 2})
        assert captured["query"] == {"communityId": "1", "page": "2"}

    async def test_downstream_4xx_is_ok_false_with_relayed_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "not found"})

        _patch_transport(monkeypatch, handler)
        client = WorkflowCoreProxyClient(base_url="http://workflow-core:8070")
        result = await client.request("GET", "/workflows/missing")
        assert result.ok is False
        assert result.status_code == 404
        assert result.body == {"message": "not found"}

    async def test_non_json_response_body_is_none_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        _patch_transport(monkeypatch, handler)
        client = WorkflowCoreProxyClient(base_url="http://workflow-core:8070")
        result = await client.request("GET", "/workflows")
        assert result.ok is True
        assert result.body is None

    async def test_connect_timeout_raises_workflow_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("connection timed out", request=request)

        _patch_transport(monkeypatch, handler)
        client = WorkflowCoreProxyClient(base_url="http://workflow-core:8070")
        with pytest.raises(ApiError) as exc_info:
            await client.request("GET", "/workflows")
        assert exc_info.value.status_code == 503
        assert exc_info.value.code == "WORKFLOW_UNAVAILABLE"

    async def test_dns_failure_raises_workflow_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """regression: alpha has no workflow-core -> `[Errno -2] Name or service not known` 500."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("[Errno -2] Name or service not known", request=request)

        _patch_transport(monkeypatch, handler)
        client = WorkflowCoreProxyClient(base_url="http://workflow-core:8070")
        with pytest.raises(ApiError) as exc_info:
            await client.request("GET", "/workflows")
        assert exc_info.value.status_code == 503
        assert exc_info.value.code == "WORKFLOW_UNAVAILABLE"
        assert "Errno" not in exc_info.value.message

    async def test_read_timeout_is_a_real_upstream_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        _patch_transport(monkeypatch, handler)
        client = WorkflowCoreProxyClient(base_url="http://workflow-core:8070")
        with pytest.raises(ApiError) as exc_info:
            await client.request("GET", "/workflows")
        assert exc_info.value.status_code == 502
        assert exc_info.value.code == "WORKFLOW_UPSTREAM_ERROR"

    def test_base_url_defaults_to_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WORKFLOW_CORE_URL", "http://workflow-core.svc:9000")
        client = WorkflowCoreProxyClient()
        assert client._base_url == "http://workflow-core.svc:9000"  # noqa: SLF001


class TestErrorFromProxy:
    def test_404_maps_to_not_found(self) -> None:
        result = ProxyResult(ok=False, status_code=404, body={"message": "gone"})
        err = error_from_proxy(result, not_found_message="Widget not found")
        assert err.status_code == 404
        assert err.message == "Widget not found"

    def test_non_404_maps_to_generic_internal_error_with_relayed_message(self) -> None:
        result = ProxyResult(ok=False, status_code=500, body={"message": "boom"})
        err = error_from_proxy(result, not_found_message="Widget not found")
        assert err.status_code == 500
        assert err.code == "INTERNAL_ERROR"
        assert err.message == "boom"

    def test_non_dict_body_falls_back_to_generic_message(self) -> None:
        result = ProxyResult(ok=False, status_code=500, body=None)
        err = error_from_proxy(result, not_found_message="Widget not found")
        assert err.message == "Workflow service error"


TENANT = "acme-corp"
PREMIUM = tier_svc.TIER_CODE_PREMIUM


@pytest.fixture(autouse=True)
def _free_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_tenant_tier(monkeypatch, "free")


@pytest.fixture
def wf_db(automation_db: Any) -> Any:
    """Real file-backed `AsyncDAL` (tenants + communities) with the tier-allocation tables."""
    ensure_tier_tables(automation_db.dal)
    return automation_db


def _community(db: Any, *, tenant_slug: str = TENANT, **extra: Any) -> int:
    dal = db.dal
    tenant_id = dal(dal.tenants.slug == tenant_slug).select().first().id
    cid = int(dal.communities.insert(name="acme", tenant_id=tenant_id, **extra))
    dal.commit()
    return cid


class TestValidateLicense:
    """Workflows gate on the community's Premium tier (allocation only); no license key."""

    async def test_community_not_found(self, wf_db: Any) -> None:
        result = await validate_license(wf_db, wf_db.dal, community_id=999)
        assert result.valid is False
        # Same denial as Free: no existence oracle for unknown communities.
        assert result.reason == "Workflows not included in current license tier"

    async def test_inactive_community_denied_like_free(self, wf_db: Any) -> None:
        cid = _community(wf_db)
        await set_tier(wf_db, cid, PREMIUM)
        assert (await validate_license(wf_db, wf_db.dal, community_id=cid)).valid is True
        await wf_db.update_async(wf_db.dal.communities.id == cid, is_active=False)
        result = await validate_license(wf_db, wf_db.dal, community_id=cid)
        assert result.valid is False
        assert result.reason == "Workflows not included in current license tier"

    async def test_other_tenant_caller_gets_the_free_denial(self, wf_db: Any) -> None:
        cid = _community(wf_db)
        await set_tier(wf_db, cid, PREMIUM)
        own = await validate_license(wf_db, wf_db.dal, community_id=cid, tenant=TENANT)
        other = await validate_license(wf_db, wf_db.dal, community_id=cid, tenant="other-corp")
        assert own.valid is True
        assert other == await validate_license(wf_db, wf_db.dal, community_id=999, tenant=TENANT)
        assert other.valid is False

    async def test_require_workflow_tier_raises_403_for_free(self, wf_db: Any) -> None:
        from services.workflow_service import require_workflow_tier

        cid = _community(wf_db)
        with pytest.raises(ApiError) as exc:
            await require_workflow_tier(wf_db, wf_db.dal, community_id=cid)
        assert exc.value.status_code == 403
        await set_tier(wf_db, cid, PREMIUM)
        await require_workflow_tier(wf_db, wf_db.dal, community_id=cid)

    async def test_free_denied(self, wf_db: Any) -> None:
        cid = _community(wf_db)
        result = await validate_license(wf_db, wf_db.dal, community_id=cid)
        assert result.valid is False
        assert result.reason == "Workflows not included in current license tier"

    async def test_allocated_premium_allowed_without_license_key(self, wf_db: Any) -> None:
        """Allocation honored (tenant Free); no license_key / expiry anywhere on the row."""
        cid = _community(wf_db, license_key=None, license_expires_at=None)
        await set_tier(wf_db, cid, PREMIUM)
        result = await validate_license(wf_db, wf_db.dal, community_id=cid)
        assert result.valid is True
        assert result.reason is None

    async def test_expired_legacy_license_key_is_ignored(self, wf_db: Any) -> None:
        """The dead per-community key checks are gone: an expired key neither grants nor denies."""
        cid = _community(
            wf_db,
            license_key="lic-1",
            license_expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        await set_tier(wf_db, cid, PREMIUM)
        assert (await validate_license(wf_db, wf_db.dal, community_id=cid)).valid is True

    async def test_legacy_key_without_allocation_does_not_grant(self, wf_db: Any) -> None:
        cid = _community(
            wf_db,
            license_key="lic-1",
            license_expires_at=datetime.now(UTC) + timedelta(days=30),
            license_tier="enterprise",
            is_premium=True,
        )
        assert (await validate_license(wf_db, wf_db.dal, community_id=cid)).valid is False

    @pytest.mark.parametrize("tenant_tier", ["free", "professional", "enterprise"])
    async def test_tenant_tier_never_uplifts_a_community(
        self, wf_db: Any, monkeypatch: pytest.MonkeyPatch, tenant_tier: str
    ) -> None:
        """Regression: a Professional/Enterprise TENANT with no allocation is still Free."""
        cid = _community(wf_db)
        patch_tenant_tier(monkeypatch, {TENANT: tenant_tier})
        result = await validate_license(wf_db, wf_db.dal, community_id=cid)
        assert result.valid is False

    async def test_resolver_failure_denies_and_logs_error(
        self, wf_db: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Tier lookup blows up -> Free for the gate (denied, no raise) and an ERROR record."""
        cid = _community(wf_db)
        await set_tier(wf_db, cid, PREMIUM)

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("allocation store down")

        monkeypatch.setattr(tier_svc, "_community_active_row", _boom)
        with caplog.at_level(logging.ERROR):
            result = await validate_license(wf_db, wf_db.dal, community_id=cid)
        assert result.valid is False
        assert any(
            r.levelno >= logging.ERROR and "RuntimeError" in r.getMessage() for r in caplog.records
        )

    async def test_two_tenants_sharing_a_display_name_cannot_cross_grant(self, wf_db: Any) -> None:
        dal = wf_db.dal
        dal.tenants.insert(slug="acme-twin", display_name="Acme Corp", is_active=True)
        dal.commit()
        granted = _community(wf_db)
        twin = _community(wf_db, tenant_slug="acme-twin")
        await set_tier(wf_db, granted, PREMIUM)
        assert (await validate_license(wf_db, dal, community_id=granted)).valid is True
        assert (await validate_license(wf_db, dal, community_id=twin)).valid is False

    async def test_slug_change_keeps_allocation_and_old_slug_uplift_does_not_follow(
        self, wf_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal = wf_db.dal
        kept, free = _community(wf_db), _community(wf_db)
        await set_tier(wf_db, kept, PREMIUM)
        dal(dal.tenants.slug == TENANT).update(slug="acme-renamed")
        dal.commit()
        patch_tenant_tier(monkeypatch, {TENANT: "enterprise"})  # license keyed to the OLD slug
        assert (await validate_license(wf_db, dal, community_id=kept)).valid is True
        assert (await validate_license(wf_db, dal, community_id=free)).valid is False
