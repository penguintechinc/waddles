# ruff: noqa: F811
"""`PUT /superadmin/communities/<id>`: tier override + absent-vs-null field semantics.

The Premium checkbox was replaced by a Free/Professional/Enterprise `licenseTier`:

* `isPremium` in a request is a 400 (it is derived from the tier and returned read-only);
* `licenseTier` needs a `reasonCode`, the global-admin scope, and goes through
  `community_tier_service.allocate(source=operator_override)` -- never blocked, counted, audited;
* an ABSENT key leaves a field unchanged, an explicit null clears the nullable `seatLimit` /
  `description` and is a 400 for the non-nullable `displayName`/`isActive`/`isPublic`.
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.superadmin import superadmin_bp
from services import community_tier_service as svc
from services.global_identity import SYSTEM_TENANT_SLUG
from services.schema import bind_community_tier_tables
from tests.conftest import TENANT_SLUG
from tests.test_v1_superadmin_platform_blueprint import (  # noqa: F401
    _headers,
    _seed_community,
    provisioned,
)

PRO, ENT, FREE = svc.TIER_CODE_PROFESSIONAL, svc.TIER_CODE_ENTERPRISE, svc.TIER_CODE_FREE
REASON = "support_request"


class RecordingAudit:
    """In-memory audit sink with the `AuditService.record` surface."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[Any] = []

    async def record(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> RecordingAudit:
    sink = RecordingAudit()
    monkeypatch.setattr("blueprints.v1.superadmin.get_audit_service", lambda: sink)
    return sink


@pytest.fixture
def app(admin_db: Any, provisioned: Any) -> Quart:
    dal = admin_db.dal
    bind_community_tier_tables(dal, migrate=True)
    for name in ("community_tier_allocations", "tenant_license_pools"):
        dal(dal[name]).count()
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(superadmin_bp)
    quart_app.config["dal"] = dal
    quart_app.config["async_dal"] = admin_db
    quart_app.config["envelope_runtime"] = type("R", (), {"service": object()})()
    quart_app.config["install_dal"] = object()
    quart_app.config["POOL_PROVIDER"] = svc.StorePoolProvider(svc.PoolStore(admin_db, dal))
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _tenant_id(admin_db: Any) -> int:
    dal = admin_db.dal
    return int(dal(dal.tenants.slug == TENANT_SLUG).select().first().id)


def _community(admin_db: Any, **kw: Any) -> int:
    return _seed_community(admin_db, tenant_id=_tenant_id(admin_db), **kw)


@pytest.fixture(autouse=True)
def _system_tenant(admin_db: Any) -> None:
    """The reserved `system` tenant row (operator actions require a system-tenant caller)."""
    dal = admin_db.dal
    dal.tenants.insert(slug=SYSTEM_TENANT_SLUG, display_name="System", is_active=True)
    dal.commit()


async def _put(client: Any, community_id: int, body: dict[str, Any], **hdr: Any) -> tuple[int, Any]:
    if "licenseTier" in body:  # tier overrides are operator actions: the system tenant by default
        hdr.setdefault("tenant", SYSTEM_TENANT_SLUG)
    response = await client.put(
        f"/api/v1/superadmin/communities/{community_id}", headers=_headers(**hdr), json=body
    )
    return response.status_code, await response.get_json()


def _row(admin_db: Any, community_id: int) -> Any:
    return admin_db.dal(admin_db.dal.communities.id == community_id).select().first()


def _active(admin_db: Any, community_id: int) -> Any:
    t = admin_db.dal.community_tier_allocations
    dal = admin_db.dal
    return dal((t.community_id == community_id) & (t.released_at == None)).select().first()  # noqa: E711


class TestIsPremiumRejected:
    @pytest.mark.parametrize("value", [True, False, None])
    async def test_is_premium_is_a_400_pointing_at_license_tier(
        self, client: Any, admin_db: Any, value: Any
    ) -> None:
        cid = _community(admin_db)
        status, body = await _put(client, cid, {"isPremium": value, "displayName": "X"})
        assert status == 400
        assert "licenseTier" in body["error"]["message"]
        row = _row(admin_db, cid)
        assert not row.is_premium and row.display_name == "Acme Community"


class TestSeatLimitAndDescriptionSemantics:
    async def test_absent_seat_limit_is_unchanged(self, client: Any, admin_db: Any) -> None:
        cid = _community(admin_db, seat_limit=7)
        status, _ = await _put(client, cid, {"displayName": "Renamed"})
        assert status == 200
        row = _row(admin_db, cid)
        assert row.seat_limit == 7 and row.display_name == "Renamed"

    async def test_null_seat_limit_resets_to_unlimited(self, client: Any, admin_db: Any) -> None:
        cid = _community(admin_db, seat_limit=7)
        status, _ = await _put(client, cid, {"seatLimit": None})
        assert status == 200
        assert _row(admin_db, cid).seat_limit is None

    async def test_int_seat_limit_persists(self, client: Any, admin_db: Any) -> None:
        cid = _community(admin_db)
        status, _ = await _put(client, cid, {"seatLimit": 25})
        assert status == 200
        assert _row(admin_db, cid).seat_limit == 25

    async def test_zero_seat_limit_persists(self, client: Any, admin_db: Any) -> None:
        cid = _community(admin_db, seat_limit=3)
        status, _ = await _put(client, cid, {"seatLimit": 0})
        assert status == 200
        assert _row(admin_db, cid).seat_limit == 0

    async def test_null_description_clears_it(self, client: Any, admin_db: Any) -> None:
        cid = _community(admin_db, description="old")
        status, _ = await _put(client, cid, {"description": None})
        assert status == 200
        assert _row(admin_db, cid).description is None

    async def test_absent_description_is_unchanged(self, client: Any, admin_db: Any) -> None:
        cid = _community(admin_db, description="keep")
        status, _ = await _put(client, cid, {"displayName": "Renamed"})
        assert status == 200
        assert _row(admin_db, cid).description == "keep"

    async def test_empty_body_is_no_updates(self, client: Any, admin_db: Any) -> None:
        cid = _community(admin_db)
        status, _ = await _put(client, cid, {})
        assert status == 400

    @pytest.mark.parametrize("key", ["displayName", "isActive", "isPublic"])
    async def test_non_nullable_fields_reject_null(
        self, client: Any, admin_db: Any, key: str
    ) -> None:
        cid = _community(admin_db)
        status, body = await _put(client, cid, {key: None})
        assert status == 400
        assert key in body["error"]["message"]

    @pytest.mark.parametrize("bad", ["", "abc", "12", 1.5, -1, True])
    async def test_garbage_seat_limit_is_not_coerced(
        self, client: Any, admin_db: Any, bad: Any
    ) -> None:
        cid = _community(admin_db)
        status, body = await _put(client, cid, {"seatLimit": bad})
        assert status == 400
        assert "seatLimit" in body["error"]["message"]


class TestLicenseTierOverride:
    async def test_override_allocates_and_denormalizes_and_audits(
        self, client: Any, admin_db: Any, audit: RecordingAudit
    ) -> None:
        cid = _community(admin_db)
        status, _ = await _put(client, cid, {"licenseTier": PRO, "reasonCode": REASON}, user_id=1)
        assert status == 200
        row, alloc = _row(admin_db, cid), _active(admin_db, cid)
        assert alloc.tier_code == PRO and alloc.source_code == svc.SOURCE_OPERATOR_OVERRIDE
        assert row.license_tier == "professional" and row.is_premium
        event = audit.events[-1]
        assert event.action == "license.community_tier_overridden"
        assert REASON in str(event)

    async def test_never_blocked_when_pool_full_or_unknown(
        self, client: Any, admin_db: Any, audit: RecordingAudit
    ) -> None:
        store = svc.PoolStore(admin_db, admin_db.dal)
        await store.put(_tenant_id(admin_db), PRO, 0, svc.POOL_SOURCE_OPERATOR_SET, svc.now_utc())
        cid = _community(admin_db)
        status, _ = await _put(client, cid, {"licenseTier": PRO, "reasonCode": REASON})
        assert status == 200
        assert _active(admin_db, cid).tier_code == PRO
        # Enterprise pool was never recorded at all (PoolUnavailable): operator still proceeds.
        cid2 = _community(admin_db, name="second")
        status, _ = await _put(client, cid2, {"licenseTier": ENT, "reasonCode": REASON})
        assert status == 200
        assert _row(admin_db, cid2).license_tier == "enterprise"

    async def test_override_counts_toward_pool_usage(self, client: Any, admin_db: Any) -> None:
        store = svc.PoolStore(admin_db, admin_db.dal)
        tid = _tenant_id(admin_db)
        await store.put(tid, PRO, 1, svc.POOL_SOURCE_OPERATOR_SET, svc.now_utc())
        await store.put(tid, ENT, 1, svc.POOL_SOURCE_OPERATOR_SET, svc.now_utc())
        for i in range(2):
            cid = _community(admin_db, name=f"c{i}")
            status, _ = await _put(client, cid, {"licenseTier": PRO, "reasonCode": REASON})
            assert status == 200
        usage = await svc.pool_usage(admin_db, admin_db.dal, tid, pool=svc.StorePoolProvider(store))
        assert usage[PRO].used == 2 and usage[PRO].overage == 1

    async def test_downgrade_to_free_releases_the_slot(self, client: Any, admin_db: Any) -> None:
        cid = _community(admin_db)
        await _put(client, cid, {"licenseTier": ENT, "reasonCode": REASON})
        status, _ = await _put(client, cid, {"licenseTier": FREE, "reasonCode": REASON})
        assert status == 200
        row = _row(admin_db, cid)
        assert _active(admin_db, cid) is None
        assert row.license_tier == "free" and not row.is_premium

    async def test_tier_with_other_fields_in_one_call(self, client: Any, admin_db: Any) -> None:
        cid = _community(admin_db, seat_limit=4)
        status, _ = await _put(
            client,
            cid,
            {"licenseTier": PRO, "reasonCode": REASON, "seatLimit": None, "displayName": "Both"},
        )
        assert status == 200
        row = _row(admin_db, cid)
        assert row.seat_limit is None and row.display_name == "Both" and row.is_premium

    @pytest.mark.parametrize("body", [{"licenseTier": PRO}, {"licenseTier": PRO, "reasonCode": ""}])
    async def test_tier_without_reason_is_400(
        self, client: Any, admin_db: Any, body: dict[str, Any]
    ) -> None:
        cid = _community(admin_db)
        status, resp = await _put(client, cid, body)
        assert status == 400
        assert "reasonCode" in resp["error"]["message"]
        assert _active(admin_db, cid) is None

    async def test_free_free_text_reason_is_400(self, client: Any, admin_db: Any) -> None:
        cid = _community(admin_db)
        status, _ = await _put(
            client, cid, {"licenseTier": PRO, "reasonCode": "Customer asked nicely"}
        )
        assert status == 400

    @pytest.mark.parametrize("tier", [0, 4, -1, 99, "2", 2.5, True, None])
    async def test_invalid_tier_code_is_400(self, client: Any, admin_db: Any, tier: Any) -> None:
        cid = _community(admin_db)
        status, _ = await _put(client, cid, {"licenseTier": tier, "reasonCode": REASON})
        assert status == 400
        assert _active(admin_db, cid) is None

    async def test_tier_needs_global_admin_scope(self, client: Any, admin_db: Any) -> None:
        cid = _community(admin_db)
        status, _ = await _put(
            client,
            cid,
            {"licenseTier": PRO, "reasonCode": REASON},
            scope="communities:admin",
        )
        assert status == 403
        assert _active(admin_db, cid) is None
        # Without a tier the same narrower scope can still edit plain fields.
        status, _ = await _put(client, cid, {"displayName": "Ok"}, scope="communities:admin")
        assert status == 200

    async def test_tier_on_community_without_tenant_is_400(
        self, client: Any, admin_db: Any
    ) -> None:
        cid = _seed_community(admin_db)  # no tenant_id
        status, _ = await _put(client, cid, {"licenseTier": PRO, "reasonCode": REASON})
        assert status == 400

    async def test_unknown_community_is_404(self, client: Any) -> None:
        status, _ = await _put(client, 9999, {"licenseTier": PRO, "reasonCode": REASON})
        assert status == 404


class TestOperatorActionAuthorization:
    """Adversarial review C: a tenant owner must never reach the operator tier override."""

    def _tenant_owner_scope(self) -> str:
        from flask_core.auth import SCOPE_BUNDLES

        bundle = [*SCOPE_BUNDLES["global"]["viewer"], *SCOPE_BUNDLES["tenant"]["admin"]]
        return " ".join(sorted(set(bundle)))

    async def test_tenant_owner_bundle_scopes_are_403_on_tier_override(
        self, client: Any, admin_db: Any
    ) -> None:
        cid = _community(admin_db)
        for tenant in (TENANT_SLUG, SYSTEM_TENANT_SLUG):
            status, _ = await _put(
                client,
                cid,
                {"licenseTier": ENT, "reasonCode": REASON},
                scope=self._tenant_owner_scope(),
                tenant=tenant,
            )
            assert status == 403
        assert _active(admin_db, cid) is None

    async def test_tenant_owner_bundle_cannot_edit_any_community_even_plain_fields(
        self, client: Any, admin_db: Any
    ) -> None:
        cid = _community(admin_db)
        status, _ = await _put(
            client, cid, {"displayName": "Pwn"}, scope=self._tenant_owner_scope()
        )
        assert status == 403
        assert _row(admin_db, cid).display_name == "Acme Community"

    async def test_global_scopes_in_a_non_system_tenant_cannot_override_tier(
        self, client: Any, admin_db: Any
    ) -> None:
        """`*:admin` + `users:admin` but a customer-tenant token (federated session): 403."""
        cid = _community(admin_db)
        status, body = await _put(
            client,
            cid,
            {"licenseTier": ENT, "reasonCode": REASON},
            scope="*:admin users:admin",
            tenant=TENANT_SLUG,
        )
        assert status == 403 and "system tenant" in body["error"]["message"]
        assert _active(admin_db, cid) is None

    async def test_audit_failure_is_loud_not_a_silent_success(
        self, client: Any, admin_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Boom:
            async def record(self, event: Any) -> None:
                raise RuntimeError("audit sink down")

        monkeypatch.setattr("blueprints.v1.superadmin.get_audit_service", lambda: Boom())
        cid = _community(admin_db)
        status, body = await _put(client, cid, {"licenseTier": PRO, "reasonCode": REASON})
        assert status == 500 and body["error"]["code"] == "TIER_OVERRIDE_FAILED"


class TestLegacyTierMapping:
    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ("pro", PRO), ("professional", PRO), ("Premium", PRO), ("enterprise", ENT),
            ("free", FREE), ("garbage", FREE), (None, FREE),
        ],
    )  # fmt: skip
    async def test_one_mapping_for_legacy_and_new_names(
        self, client: Any, admin_db: Any, stored: Any, expected: int
    ) -> None:
        _community(admin_db, license_tier=stored)
        response = await client.get("/api/v1/superadmin/communities", headers=_headers())
        row = (await response.get_json())["communities"][0]
        assert row["licenseTier"] == expected
        assert row["isPremium"] is (expected > FREE)


class TestListExposesTier:
    async def test_list_carries_tier_and_derived_premium(self, client: Any, admin_db: Any) -> None:
        cid = _community(admin_db)
        await _put(client, cid, {"licenseTier": ENT, "reasonCode": REASON})
        _community(admin_db, name="plain")
        response = await client.get("/api/v1/superadmin/communities", headers=_headers())
        body = await response.get_json()
        by_name = {c["name"]: c for c in body["communities"]}
        assert by_name["acme-community"]["licenseTier"] == ENT
        assert by_name["acme-community"]["isPremium"] is True
        assert by_name["plain"]["licenseTier"] == FREE
        assert by_name["plain"]["isPremium"] is False
        assert set(by_name["plain"]) == {
            "id", "name", "displayName", "description", "platform", "platformServerId",
            "ownerId", "ownerName", "memberCount", "isActive", "isPublic", "communityType",
            "isPremium", "licenseTier", "seatLimit", "createdAt", "updatedAt",
        }  # fmt: skip

    async def test_legacy_premium_row_without_tier_name_reads_as_professional(
        self, client: Any, admin_db: Any
    ) -> None:
        _community(admin_db, is_premium=True)
        response = await client.get("/api/v1/superadmin/communities", headers=_headers())
        body = await response.get_json()
        assert body["communities"][0]["licenseTier"] == PRO
