"""`/api/v1/compliance/audit/*` -- Enterprise-gated read / verify / export of the audit chain."""

from __future__ import annotations

import json
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema
from sqlalchemy import select, update

from blueprints.v1 import compliance_audit as module
from blueprints.v1.compliance_audit import BLUEPRINTS
from services.audit_chain import ChainRecord, verify_records
from services.audit_events import AuditAction, AuditCategory, AuditOutcome
from services.audit_service import (
    FEATURE_AUDIT_EXPORT,
    FEATURE_AUDIT_LOGS,
    AuditService,
    AuditWriteError,
    ListFilters,
)
from tests.audit_support import (
    OTHER_USER_UUID,
    USER_UUID,
    make_event,
)
from tests.conftest import make_token

BASE = "/api/v1/compliance/audit"
ADMIN = {"scope": "compliance.audit:admin", "user_id": "7"}

#: Exactly the fields an audit record exposes -- a regression guard against raw-row echo.
EVENT_FIELDS = {
    "seq",
    "event_id",
    "occurred_at",
    "actor_uuid",
    "actor_kind",
    "category",
    "action",
    "outcome",
    "target_type",
    "target_id",
    "details",
    "prev_hash",
    "record_hash",
    "hash_version",
}


class Flags:
    """Controllable `feature_enabled` stand-in: which flags are ON, and every query made."""

    def __init__(self) -> None:
        """Start with both Enterprise audit flags ON."""
        self.on: set[str] = {FEATURE_AUDIT_LOGS, FEATURE_AUDIT_EXPORT}
        self.asked: list[tuple[str, str]] = []

    async def __call__(self, flag: str, *, tenant: str, **_kw: Any) -> bool:
        self.asked.append((flag, tenant))
        return flag in self.on


@pytest.fixture
def flags(monkeypatch: pytest.MonkeyPatch) -> Flags:
    fake = Flags()
    monkeypatch.setattr(module, "feature_enabled", fake)
    return fake


@pytest.fixture
async def app(
    bundle_install_db: Any,
    audit_dal: Any,
    audit_service: AuditService,
    flags: Flags,
) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["async_dal"] = bundle_install_db
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = audit_dal
    quart_app.config["audit_service"] = audit_service
    for blueprint in BLUEPRINTS:
        quart_app.register_blueprint(blueprint)
    return quart_app


def _auth(**kwargs: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(**kwargs)}"}


async def _seed(service: AuditService, count: int = 6, *, tenant_id: int | None = 1) -> None:
    for i in range(count):
        await service.record(make_event(action=f"admin.step_{i}", tenant_id=tenant_id))


async def _get(app: Quart, path: str, **auth: str) -> Any:
    return await app.test_client().get(f"{BASE}{path}", headers=_auth(**(auth or ADMIN)))


class TestAuthorization:
    @pytest.mark.parametrize("path", ["/events", "/head", "/verify", "/export"])
    async def test_every_session_has_star_read_and_that_must_not_open_the_audit_log(
        self, app: Quart, path: str
    ) -> None:
        """Regression: `*:read` (held by EVERY session) must not satisfy the audit scope."""
        response = await _get(app, path, scope="*:read", user_id="7")
        assert response.status_code == 403

    @pytest.mark.parametrize("path", ["/events", "/head", "/verify", "/export"])
    async def test_the_old_read_scope_name_does_not_grant_access(
        self, app: Quart, path: str
    ) -> None:
        assert (
            await _get(app, path, scope="compliance.audit:read", user_id="7")
        ).status_code == 403

    @pytest.mark.parametrize("path", ["/events", "/head", "/verify", "/export"])
    async def test_unauthenticated_is_rejected(self, app: Quart, path: str) -> None:
        response = await app.test_client().get(f"{BASE}{path}")
        assert response.status_code in (401, 403)

    @pytest.mark.parametrize("scope", ["compliance.audit:admin", "*:admin", "*:admin *:read"])
    async def test_audit_admin_and_global_admin_scopes_are_accepted(
        self, app: Quart, scope: str
    ) -> None:
        response = await _get(app, "/head", scope=scope, user_id="7")
        assert response.status_code == 200

    async def test_the_tenant_owner_bundle_grants_the_audit_scope(self, app: Quart) -> None:
        from flask_core.auth import SCOPE_BUNDLES

        scope = " ".join(SCOPE_BUNDLES["global"]["viewer"] + SCOPE_BUNDLES["tenant"]["admin"])
        assert (await _get(app, "/head", scope=scope, user_id="7")).status_code == 200
        viewer_only = " ".join(
            SCOPE_BUNDLES["global"]["viewer"] + SCOPE_BUNDLES["tenant"]["viewer"]
        )
        assert (await _get(app, "/head", scope=viewer_only, user_id="7")).status_code == 403


class TestEntitlement:
    @pytest.mark.parametrize("path", ["/events", "/head", "/verify", "/export"])
    async def test_not_entitled_is_402_with_no_data(
        self,
        app: Quart,
        flags: Flags,
        audit_service: AuditService,
        path: str,
    ) -> None:
        await _seed(audit_service, 2)
        flags.on.clear()
        response = await _get(app, path)
        assert response.status_code == 402
        body = await response.get_json()
        assert body["success"] is False and "records" not in body and "events" not in body

    async def test_export_needs_its_own_enterprise_flag_on_top_of_the_audit_flag(
        self,
        app: Quart,
        flags: Flags,
        audit_service: AuditService,
    ) -> None:
        await _seed(audit_service, 2)
        flags.on = {FEATURE_AUDIT_LOGS}
        assert (await _get(app, "/events")).status_code == 200  # reading still allowed
        assert (await _get(app, "/export")).status_code == 402  # exporting is not
        flags.on = {FEATURE_AUDIT_EXPORT}
        assert (await _get(app, "/export")).status_code == 402  # export alone is not enough

    async def test_the_gate_is_evaluated_for_the_callers_own_tenant(
        self, app: Quart, flags: Flags
    ) -> None:
        await _get(app, "/head")
        assert flags.asked == [(FEATURE_AUDIT_LOGS, "acme-corp")]


class TestListEvents:
    async def test_lists_newest_first_with_an_explicit_field_set(
        self,
        app: Quart,
        audit_service: AuditService,
    ) -> None:
        await _seed(audit_service, 3)
        response = await _get(app, "/events")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["chain_id"] == "tenant:1"
        assert [e["seq"] for e in body["events"]] == [3, 2, 1]
        assert all(set(e) == EVENT_FIELDS for e in body["events"])
        assert body["pagination"] == {"page": 1, "limit": 50, "total": 3, "total_pages": 1}
        first = body["events"][-1]
        assert first["actor_uuid"] == str(USER_UUID)
        assert first["prev_hash"] == "0" * 64

    async def test_a_tenant_only_ever_sees_its_own_chain(
        self,
        app: Quart,
        audit_service: AuditService,
    ) -> None:
        await _seed(audit_service, 3, tenant_id=1)
        await audit_service.record(make_event(tenant_id=2, action="admin.secret_of_other_tenant"))
        mine = await (await _get(app, "/events")).get_json()
        theirs = await (await _get(app, "/events", **ADMIN, tenant="other-co")).get_json()
        assert mine["chain_id"] == "tenant:1" and theirs["chain_id"] == "tenant:2"
        assert "admin.secret_of_other_tenant" not in json.dumps(mine)
        assert [e["action"] for e in theirs["events"]] == ["admin.secret_of_other_tenant"]

    async def test_client_cannot_choose_another_tenants_chain(
        self,
        app: Quart,
        audit_service: AuditService,
    ) -> None:
        await audit_service.record(make_event(tenant_id=2, action="admin.other"))
        for path in ("/events?chain=tenant:2", "/events?tenant_id=2", "/events?tenant=other-co"):
            body = await (await _get(app, path)).get_json()
            assert body["chain_id"] == "tenant:1" and body["events"] == []

    async def test_filters_and_pagination(
        self,
        app: Quart,
        audit_service: AuditService,
    ) -> None:
        await _seed(audit_service, 4)
        await audit_service.record(
            make_event(
                action=AuditAction.AUTHZ_DENIED,
                category=AuditCategory.AUTHZ,
                outcome=AuditOutcome.DENIED,
                user_id=8,
            )
        )
        denied = await (await _get(app, "/events?category=authz&outcome=denied")).get_json()
        assert [e["action"] for e in denied["events"]] == ["authz.denied"]
        by_actor = await (await _get(app, f"/events?actor={OTHER_USER_UUID}")).get_json()
        assert by_actor["pagination"]["total"] == 1
        by_action = await (await _get(app, "/events?action=admin.step_2")).get_json()
        assert [e["seq"] for e in by_action["events"]] == [3]
        paged = await (await _get(app, "/events?limit=2&page=2")).get_json()
        assert [e["seq"] for e in paged["events"]] == [3, 2]
        assert paged["pagination"] == {"page": 2, "limit": 2, "total": 5, "total_pages": 3}
        window = await (
            await _get(app, "/events?since=2000-01-01T00:00:00Z&until=2999-01-01T00:00:00Z")
        ).get_json()
        assert window["pagination"]["total"] == 5

    @pytest.mark.parametrize(
        "query",
        [
            "page=abc",
            "page=0",
            "limit=0",
            "limit=100000",
            "category=nope",
            "outcome=maybe",
            "action=Bad Action",
            "actor=not-a-uuid",
            "since=yesterday",
            "until=%00",
        ],
    )
    async def test_invalid_query_values_are_400_not_500(self, app: Quart, query: str) -> None:
        response = await _get(app, f"/events?{query}")
        assert response.status_code == 400
        assert (await response.get_json())["success"] is False

    async def test_platform_chain_requires_platform_admin(
        self,
        app: Quart,
        audit_service: AuditService,
    ) -> None:
        await audit_service.record(make_event(tenant_id=None, action="admin.platform_thing"))
        tenant_owner = await _get(app, "/events?chain=platform")
        assert tenant_owner.status_code == 403
        platform_admin = await _get(
            app, "/events?chain=platform", scope="compliance.audit:admin users:admin", user_id="7"
        )
        assert platform_admin.status_code == 200
        body = await platform_admin.get_json()
        assert body["chain_id"] == "platform"
        assert [e["action"] for e in body["events"]] == ["admin.platform_thing"]


class TestHeadAndVerify:
    async def test_head_of_empty_and_populated_chain(
        self,
        app: Quart,
        audit_service: AuditService,
    ) -> None:
        empty = await (await _get(app, "/head")).get_json()
        assert empty["head"] is None and empty["hash_version"] == "sha256-v1"
        await _seed(audit_service, 3)
        head = (await (await _get(app, "/head")).get_json())["head"]
        assert head["seq"] == 3 and len(head["record_hash"]) == 64

    async def test_verify_intact(
        self,
        app: Quart,
        audit_service: AuditService,
    ) -> None:
        await _seed(audit_service, 5)
        response = await _get(app, "/verify")
        body = await response.get_json()
        assert response.status_code == 200
        assert (body["status"], body["ok"], body["examined"], body["complete"]) == (
            "intact",
            True,
            5,
            True,
        )
        assert body["chain_break"] is None and body["head_seq"] == 5

    async def test_verify_empty_chain_is_200_but_not_ok(self, app: Quart) -> None:
        response = await _get(app, "/verify")
        body = await response.get_json()
        assert response.status_code == 200
        assert (body["status"], body["ok"], body["examined"]) == ("empty", False, 0)

    async def test_verify_detects_tampering_with_409_and_names_the_first_bad_record(
        self,
        app: Quart,
        audit_dal: Any,
        audit_service: AuditService,
    ) -> None:
        await _seed(audit_service, 6)
        table = audit_dal.metadata.tables["audit_events"]
        async with audit_dal.engine.begin() as conn:
            await conn.execute(update(table).where(table.c.seq == 3).values(outcome="failure"))
        response = await _get(app, "/verify")
        body = await response.get_json()
        assert response.status_code == 409  # a `curl -f` cron cannot mistake this for success
        assert (body["success"], body["status"], body["ok"]) == (False, "broken", False)
        assert body["chain_break"]["seq"] == 3
        assert body["chain_break"]["reason"] == "hash_mismatch"

    async def test_verify_with_a_pinned_head_detects_truncation(
        self,
        app: Quart,
        audit_dal: Any,
        audit_service: AuditService,
    ) -> None:
        await _seed(audit_service, 6)
        head = (await (await _get(app, "/head")).get_json())["head"]
        table = audit_dal.metadata.tables["audit_events"]
        async with audit_dal.engine.begin() as conn:
            await conn.execute(table.delete().where(table.c.seq > 4))
        ok = await _get(app, "/verify")
        assert ok.status_code == 200  # the chain alone cannot see a truncated tail
        pinned = await _get(
            app, f"/verify?expected_head_seq={head['seq']}&expected_head_hash={head['record_hash']}"
        )
        assert pinned.status_code == 409
        assert (await pinned.get_json())["chain_break"]["reason"] == "head_mismatch"

    async def test_verify_resumes_long_chains_in_slices(
        self,
        app: Quart,
        audit_service: AuditService,
    ) -> None:
        await _seed(audit_service, 7)
        first = await (await _get(app, "/verify?max_records=3")).get_json()
        assert (first["complete"], first["next_seq"], first["examined"]) == (False, 4, 3)
        second = await (
            await _get(app, f"/verify?max_records=10&from_seq=4&anchor_hash={first['anchor_hash']}")
        ).get_json()
        assert (second["complete"], second["ok"], second["examined"]) == (True, True, 4)

    @pytest.mark.parametrize(
        "query",
        [
            "from_seq=0",
            "from_seq=abc",
            "from_seq=5",  # resuming without an anchor
            f"anchor_hash={'z' * 64}",
            "anchor_hash=short",
            "expected_head_hash=NOTHEX",
            "expected_head_seq=0",
            "max_records=0",
            "max_records=999999999",
        ],
    )
    async def test_verify_rejects_bad_parameters(self, app: Quart, query: str) -> None:
        assert (await _get(app, f"/verify?{query}")).status_code == 400


class TestExport:
    async def test_export_returns_the_slice_a_manifest_and_an_attachment_header(
        self,
        app: Quart,
        audit_service: AuditService,
    ) -> None:
        await _seed(audit_service, 4)
        response = await _get(app, "/export")
        assert response.status_code == 200
        assert response.headers["Content-Disposition"].startswith(
            'attachment; filename="waddles-audit-'
        )
        body = await response.get_json()
        records = body["records"]
        assert all(set(r) == EVENT_FIELDS for r in records)
        # The export is audited FIRST, so the slice includes its own `audit.exported` record.
        assert [r["action"] for r in records][-1] == "audit.exported"
        assert records[-1]["actor_uuid"] == str(USER_UUID)
        manifest = body["manifest"]
        assert manifest["count"] == 5 and manifest["first_seq"] == 1 and manifest["last_seq"] == 5
        assert manifest["first_prev_hash"] == "0" * 64
        assert manifest["last_hash"] == records[-1]["record_hash"]
        assert manifest["has_more"] is False and manifest["next_after_seq"] is None

    async def test_paged_export_reassembles_into_a_verifiable_chain(
        self,
        app: Quart,
        audit_service: AuditService,
    ) -> None:
        await _seed(audit_service, 6)
        collected: list[dict[str, Any]] = []
        after = 0
        pages = 0
        while True:
            body = await (await _get(app, f"/export?limit=3&after_seq={after}")).get_json()
            collected.extend(body["records"])
            pages += 1
            if not body["manifest"]["has_more"]:
                break
            after = body["manifest"]["next_after_seq"]
            assert pages < 20
        assert pages >= 2
        assert [r["seq"] for r in collected][:6] == [1, 2, 3, 4, 5, 6]
        # An auditor verifies the downloaded records with no database and no server.
        chain = [
            ChainRecord(
                chain_id="tenant:1",
                seq=r["seq"],
                event_id=r["event_id"],
                occurred_at=r["occurred_at"],
                actor_uuid=r["actor_uuid"],
                actor_kind=r["actor_kind"],
                category=r["category"],
                action=r["action"],
                outcome=r["outcome"],
                target_type=r["target_type"],
                target_id=r["target_id"],
                details=r["details"],
                prev_hash=r["prev_hash"],
                record_hash=r["record_hash"],
                hash_version=r["hash_version"],
            )
            for r in collected
        ]
        assert verify_records(chain, chain_id="tenant:1").ok

    async def test_every_export_is_itself_audited_with_its_parameters(
        self,
        app: Quart,
        audit_service: AuditService,
    ) -> None:
        await _seed(audit_service, 2)
        await _get(app, "/export?limit=2&after_seq=1")
        records, _ = await audit_service.list_events(
            "tenant:1",
            filters=ListFilters(action="audit.exported"),
            page=1,
            limit=10,
        )
        (exported,) = records
        assert exported.category == "audit"
        assert exported.details == {"after_seq": 1, "limit": 2}
        assert exported.target_id == "tenant:1"

    async def test_nothing_is_disclosed_if_the_export_cannot_be_recorded(
        self,
        app: Quart,
        audit_service: AuditService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed(audit_service, 3)

        async def boom(self: AuditService, event: Any) -> None:
            raise AuditWriteError("audit store down")

        monkeypatch.setattr(AuditService, "record", boom)
        response = await _get(app, "/export")
        body = await response.get_json()
        assert response.status_code == 500
        assert "records" not in body and "nothing was exported" in body["error"]["message"]

    @pytest.mark.parametrize("query", ["after_seq=-1", "after_seq=x", "limit=0", "limit=100000"])
    async def test_export_rejects_bad_parameters(self, app: Quart, query: str) -> None:
        assert (await _get(app, f"/export?{query}")).status_code == 400

    async def test_export_requires_a_numeric_subject(self, app: Quart) -> None:
        response = await _get(app, "/export", scope="compliance.audit:admin", user_id="u1")
        assert response.status_code in (400, 401)


class TestChainAfterReads:
    async def test_reading_does_not_write_to_the_chain(
        self,
        app: Quart,
        audit_dal: Any,
        audit_service: AuditService,
    ) -> None:
        await _seed(audit_service, 3)
        for path in ("/events", "/head", "/verify"):
            await _get(app, path)
        table = audit_dal.metadata.tables["audit_events"]
        async with audit_dal.engine.connect() as conn:
            rows = (await conn.execute(select(table.c.seq))).all()
        assert len(rows) == 3
