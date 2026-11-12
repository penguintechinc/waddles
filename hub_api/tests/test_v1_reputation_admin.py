"""Reputation admin routes: community config / at-risk / history / soft bans, and the tenant policy.

Auth model under test: COMMUNITY routes require a DB-backed admin role in THAT community (the
flat `community:admin` JWT scope is never granted to community admins), tenant routes require the
`tenant:admin` scope plus a URL slug equal to the JWT tenant. The Postgres-only soft-ban functions
are faked with a table-writing stand-in here; their real behaviour is covered against Postgres in
`alembic/tests/test_0096_reputation_unify.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from flask_core.database import AsyncDAL
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.admin import admin_bp
from blueprints.v1.tenant_reputation import tenant_reputation_bp
from services import community_reputation_service as reputation_svc
from services.schema import (
    bind_admin_tables,
    bind_auth_tables,
    bind_community_authz_tables,
    bind_erasure_reputation_tables,
    bind_reputation_policy_tables,
)
from tests.conftest import TENANT_SLUG, make_user_token

OTHER_TENANT = "other-tenant"


def _uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


@pytest.fixture
def rep_db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Sqlite AsyncDAL with auth/admin/authz + reputation tables; soft-ban SQL faked onto tables."""
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'rep_admin.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_admin_tables(dal, migrate=True)
    bind_community_authz_tables(dal, migrate=True)
    bind_erasure_reputation_tables(dal, migrate=True)
    bind_reputation_policy_tables(dal, migrate=True)
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme", is_active=True)
    dal.tenants.insert(slug=OTHER_TENANT, display_name="Other", is_active=True)
    dal.commit()
    for name in dal.tables:
        dal(dal[name]).count()

    async def fake_sql(sql: str, placeholders: list[Any] | None = None) -> list[list[Any]]:
        args = placeholders or []
        bans = dal.bundle_reputation_soft_bans
        if "reputation_set_soft_ban" in sql:
            tenant_id, community_id, user_uuid, reason, set_by, expires = args
            dal((bans.community_id == community_id) & (bans.user_uuid == user_uuid)).delete()
            bans.insert(
                tenant_id=tenant_id,
                community_id=community_id,
                user_uuid=user_uuid,
                reason_code=reason,
                set_by=set_by,
                set_at=datetime.now(UTC),
                expires_at=expires.replace(tzinfo=None) if expires else None,
            )
            dal.commit()
            return [[None]]
        if "reputation_clear_soft_ban" in sql:
            _tenant_id, community_id, user_uuid = args
            n = dal((bans.community_id == community_id) & (bans.user_uuid == user_uuid)).delete()
            dal.commit()
            return [[bool(n)]]
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(async_dal, "executesql_async", fake_sql)
    yield async_dal
    dal.close()


@pytest.fixture
def client(rep_db: Any) -> Any:
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(admin_bp)
    app.register_blueprint(tenant_reputation_bp)
    app.config["dal"] = rep_db.dal
    app.config["async_dal"] = rep_db
    return app.test_client()


def _tenant_id(dal: Any, slug: str) -> int:
    return int(dal(dal.tenants.slug == slug).select().first().id)


def _community(dal: Any, slug: str = TENANT_SLUG, name: str = "c1") -> int:
    cid = dal.communities.insert(
        name=name, display_name=name, tenant_id=_tenant_id(dal, slug), is_active=True
    )
    dal.commit()
    return int(cid)


def _user(dal: Any, n: int) -> int:
    uid = dal.hub_users.insert(
        id=n, username=f"user{n}", email=f"u{n}@example.com", is_active=True, is_super_admin=False
    )
    dal.commit()
    return int(uid)


def _member(
    dal: Any, community_id: int, n: int, *, admin: bool = False, score: int | None = None
) -> None:
    """Member `n` of the community; `admin` gives a role carrying an admin scope there."""
    _user(dal, n) if dal(dal.hub_users.id == n).select().first() is None else None
    scopes = ["community:manage_members"] if admin else ["community:read"]
    role_id = dal.community_roles.insert(
        community_id=community_id, name=f"role{n}", base_claims={"scopes": scopes}
    )
    dal.community_members.insert(
        community_id=community_id,
        user_id=str(n),
        user_uuid=_uuid(n),
        display_name=f"user{n}",
        role="admin" if admin else "member",
        is_active=True,
        community_role_id=role_id,
        joined_at=datetime.now(UTC),
    )
    if score is not None:
        dal.bundle_reputation_scores.insert(
            tenant_id=dal.communities[community_id].tenant_id,
            community_id=community_id,
            user_uuid=_uuid(n),
            balance=score,
        )
    dal.commit()


def _h(user_id: int, *, scope: str = "", tenant: str = TENANT_SLUG) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {make_user_token(user_id=user_id, scope=scope, tenant=tenant)}"
    }


@pytest.fixture
def seeded(rep_db: Any) -> dict[str, int]:
    """Community with admin user 1 and plain member user 2; a second same-tenant community."""
    dal = rep_db.dal
    c1 = _community(dal, name="c1")
    c2 = _community(dal, name="c2")
    _member(dal, c1, 1, admin=True)
    _member(dal, c1, 2)
    _member(dal, c2, 3, admin=True)  # admin of the OTHER community only
    return {"c1": c1, "c2": c2}


CONFIG_FIELDS = {
    "success",
    "softBanEnabled",
    "softBanFloor",
    "hardBanEnabled",
    "hardBanFloor",
    "atRiskThreshold",
    "scoreMin",
    "scoreMax",
    "scoreDefault",
    "updatedAt",
}


class TestCommunityConfig:
    async def test_defaults_are_off_with_ordered_floors_and_exact_fields(
        self, client: Any, seeded: dict[str, int]
    ) -> None:
        r = await client.get(f"/api/v1/admin/{seeded['c1']}/reputation/config", headers=_h(1))
        assert r.status_code == 200
        body = await r.get_json()
        assert set(body) == CONFIG_FIELDS
        assert body["softBanEnabled"] is False and body["hardBanEnabled"] is False
        assert (body["hardBanFloor"], body["softBanFloor"], body["atRiskThreshold"]) == (
            400,
            550,
            600,
        )
        assert (body["scoreMin"], body["scoreMax"], body["scoreDefault"]) == (300, 850, 600)

    async def test_community_admin_can_set_and_it_persists(
        self, client: Any, seeded: dict[str, int]
    ) -> None:
        url = f"/api/v1/admin/{seeded['c1']}/reputation/config"
        r = await client.put(
            url,
            headers=_h(1),
            json={
                "softBanEnabled": True,
                "softBanFloor": 520,
                "hardBanFloor": 380,
                "atRiskThreshold": 640,
            },
        )
        assert r.status_code == 200
        body = await r.get_json()
        assert body["softBanEnabled"] is True and body["hardBanEnabled"] is False
        assert (body["hardBanFloor"], body["softBanFloor"], body["atRiskThreshold"]) == (
            380,
            520,
            640,
        )
        again = await (await client.get(url, headers=_h(1))).get_json()
        assert again["softBanFloor"] == 520 and again["softBanEnabled"] is True

    async def test_partial_update_keeps_other_fields(
        self, client: Any, seeded: dict[str, int]
    ) -> None:
        url = f"/api/v1/admin/{seeded['c1']}/reputation/config"
        await client.put(url, headers=_h(1), json={"hardBanEnabled": True})
        body = await (await client.get(url, headers=_h(1))).get_json()
        assert body["hardBanEnabled"] is True and body["hardBanFloor"] == 400

    @pytest.mark.parametrize(
        "payload",
        [
            {"softBanFloor": 200},  # below the 300 scale
            {"atRiskThreshold": 900},  # above the 850 scale
            {"hardBanFloor": 560},  # hard >= soft (550)
            {"softBanFloor": 610},  # soft >= at-risk (600)
            {"softBanFloor": 400},  # soft == hard (400)
        ],
    )
    async def test_out_of_range_or_misordered_is_400(
        self, client: Any, seeded: dict[str, int], payload: dict[str, int]
    ) -> None:
        r = await client.put(
            f"/api/v1/admin/{seeded['c1']}/reputation/config", headers=_h(1), json=payload
        )
        assert r.status_code == 400

    async def test_plain_member_is_403(self, client: Any, seeded: dict[str, int]) -> None:
        for method in ("get", "put"):
            kw: dict[str, Any] = {"json": {"softBanEnabled": True}} if method == "put" else {}
            r = await getattr(client, method)(
                f"/api/v1/admin/{seeded['c1']}/reputation/config", headers=_h(2), **kw
            )
            assert r.status_code == 403

    async def test_admin_of_another_community_is_403(
        self, client: Any, seeded: dict[str, int]
    ) -> None:
        r = await client.put(
            f"/api/v1/admin/{seeded['c1']}/reputation/config",
            headers=_h(3),
            json={"softBanEnabled": True},
        )
        assert r.status_code == 403

    async def test_other_tenant_community_is_404(
        self, client: Any, rep_db: Any, seeded: dict[str, int]
    ) -> None:
        foreign = _community(rep_db.dal, OTHER_TENANT, "foreign")
        r = await client.get(f"/api/v1/admin/{foreign}/reputation/config", headers=_h(1))
        assert r.status_code == 404

    async def test_unauthenticated_is_401(self, client: Any, seeded: dict[str, int]) -> None:
        r = await client.get(f"/api/v1/admin/{seeded['c1']}/reputation/config")
        assert r.status_code == 401


class TestSoftBans:
    async def test_manual_set_list_clear(
        self, client: Any, rep_db: Any, seeded: dict[str, int]
    ) -> None:
        base = f"/api/v1/admin/{seeded['c1']}/reputation/soft-bans"
        assert (await client.put(f"{base}/2", headers=_h(1), json={})).status_code == 200
        listed = await (await client.get(base, headers=_h(1))).get_json()
        assert [(b["userId"], b["reason"], b["setByType"]) for b in listed["softBans"]] == [
            ("2", "manual", "admin")
        ]
        assert set(listed["softBans"][0]) == {
            "userId",
            "displayName",
            "reason",
            "setByType",
            "setAt",
            "expiresAt",
        }
        assert (await client.delete(f"{base}/2", headers=_h(1))).status_code == 200
        assert (await client.delete(f"{base}/2", headers=_h(1))).status_code == 404
        assert (await (await client.get(base, headers=_h(1))).get_json())["softBans"] == []

    async def test_floor_set_soft_ban_is_listed_as_floor_and_admin_can_clear_it(
        self, client: Any, rep_db: Any, seeded: dict[str, int]
    ) -> None:
        dal = rep_db.dal
        dal.bundle_reputation_soft_bans.insert(
            tenant_id=dal.communities[seeded["c1"]].tenant_id,
            community_id=seeded["c1"],
            user_uuid=_uuid(2),
            reason_code=1,
            set_by="bundle:accrual",
            set_at=datetime.now(UTC),
        )
        dal.commit()
        base = f"/api/v1/admin/{seeded['c1']}/reputation/soft-bans"
        listed = await (await client.get(base, headers=_h(1))).get_json()
        assert listed["softBans"][0]["reason"] == "floor"
        assert listed["softBans"][0]["setByType"] == "bundle"
        assert (await client.delete(f"{base}/2", headers=_h(1))).status_code == 200

    async def test_expired_bans_are_not_listed_and_past_expiry_is_400(
        self, client: Any, rep_db: Any, seeded: dict[str, int]
    ) -> None:
        base = f"/api/v1/admin/{seeded['c1']}/reputation/soft-bans"
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        assert (
            await client.put(f"{base}/2", headers=_h(1), json={"expiresAt": past})
        ).status_code == 400
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        assert (
            await client.put(f"{base}/2", headers=_h(1), json={"expiresAt": future})
        ).status_code == 200
        assert (
            await client.put(f"{base}/2", headers=_h(1), json={"expiresAt": "nope"})
        ).status_code == 400

    async def test_unknown_member_is_404(self, client: Any, seeded: dict[str, int]) -> None:
        base = f"/api/v1/admin/{seeded['c1']}/reputation/soft-bans"
        assert (await client.put(f"{base}/999", headers=_h(1), json={})).status_code == 404

    async def test_member_and_foreign_admin_are_403(
        self, client: Any, seeded: dict[str, int]
    ) -> None:
        base = f"/api/v1/admin/{seeded['c1']}/reputation/soft-bans"
        for uid in (2, 3):
            assert (await client.put(f"{base}/2", headers=_h(uid), json={})).status_code == 403
            assert (await client.delete(f"{base}/2", headers=_h(uid))).status_code == 403
            assert (await client.get(base, headers=_h(uid))).status_code == 403


class TestAtRiskAndHistory:
    async def test_at_risk_lists_members_below_threshold_with_floor_flags(
        self, client: Any, rep_db: Any, seeded: dict[str, int]
    ) -> None:
        dal = rep_db.dal
        _member(dal, seeded["c1"], 10, score=380)  # below hard (400) and soft (550)
        _member(dal, seeded["c1"], 11, score=570)  # below at-risk (600) only
        _member(dal, seeded["c1"], 12, score=700)  # fine
        r = await client.get(f"/api/v1/admin/{seeded['c1']}/reputation/at-risk", headers=_h(1))
        members = (await r.get_json())["members"]
        assert [
            (m["userId"], m["score"], m["belowSoftBanFloor"], m["belowHardBanFloor"])
            for m in members
        ] == [
            ("10", 380, True, True),
            ("11", 570, False, False),
        ]

    async def test_history_lists_newest_first_filters_and_hides_internal_actor_ids(
        self, client: Any, rep_db: Any, seeded: dict[str, int]
    ) -> None:
        dal = rep_db.dal
        for i, (actor, delta) in enumerate((("bundle:accrual", 5), ("admin:1", -20))):
            dal.bundle_reputation_adjustments.insert(
                app_id=None,
                tenant_id=dal.communities[seeded["c1"]].tenant_id,
                community_id=seeded["c1"],
                target_user_uuid=_uuid(2),
                scope="community",
                delta=delta,
                reason_code="100",
                occurred_at=datetime.now(UTC),
                kind="adjust",
                balance_after=600 + delta,
                actor=actor,
                source_event=f"e{i}",
                reason_code_id=100,
            )
        dal.commit()
        url = f"/api/v1/admin/{seeded['c1']}/reputation/history"
        entries = (await (await client.get(url, headers=_h(1))).get_json())["entries"]
        assert [e["delta"] for e in entries] == [-20, 5]
        assert entries[0]["actorType"] == "admin" and "admin:1" not in str(entries)
        assert set(entries[0]) == {
            "id",
            "userUuid",
            "delta",
            "balanceAfter",
            "kind",
            "reasonCode",
            "actorType",
            "sourceEvent",
            "occurredAt",
        }
        only = (await (await client.get(f"{url}?userId=2", headers=_h(1))).get_json())["entries"]
        assert len(only) == 2
        assert (await client.get(f"{url}?userId=abc", headers=_h(1))).status_code == 400
        assert (await client.get(url, headers=_h(2))).status_code == 403


TENANT_FIELDS = {
    "success",
    "tenantAutoBanEnabled",
    "tenantAutoBanFloor",
    "tenantAtRiskThreshold",
    "updatedAt",
}


class TestTenantConfig:
    def _url(self, slug: str = TENANT_SLUG) -> str:
        return f"/api/v1/tenant/{slug}/reputation/config"

    async def test_defaults_off_floor_350_exact_fields(
        self, client: Any, seeded: dict[str, int]
    ) -> None:
        r = await client.get(self._url(), headers=_h(1, scope="tenant:admin"))
        body = await r.get_json()
        assert r.status_code == 200 and set(body) == TENANT_FIELDS
        assert body["tenantAutoBanEnabled"] is False
        assert (body["tenantAutoBanFloor"], body["tenantAtRiskThreshold"]) == (350, 450)

    async def test_tenant_admin_sets_and_it_persists(
        self, client: Any, seeded: dict[str, int]
    ) -> None:
        r = await client.put(
            self._url(),
            headers=_h(1, scope="tenant:admin"),
            json={
                "tenantAutoBanEnabled": True,
                "tenantAutoBanFloor": 400,
                "tenantAtRiskThreshold": 500,
            },
        )
        assert r.status_code == 200
        body = await (await client.get(self._url(), headers=_h(1, scope="tenant:admin"))).get_json()
        assert body["tenantAutoBanEnabled"] is True and body["tenantAutoBanFloor"] == 400

    async def test_community_admin_cannot_set_the_tenant_floor(
        self, client: Any, seeded: dict[str, int]
    ) -> None:
        # user 1 administers community c1 but holds no tenant:admin scope
        r = await client.put(self._url(), headers=_h(1), json={"tenantAutoBanFloor": 320})
        assert r.status_code == 403
        assert (await client.get(self._url(), headers=_h(1))).status_code == 403

    async def test_slug_must_match_the_jwt_tenant(
        self, client: Any, seeded: dict[str, int]
    ) -> None:
        r = await client.put(
            self._url(OTHER_TENANT),
            headers=_h(1, scope="tenant:admin"),
            json={"tenantAutoBanFloor": 320},
        )
        assert r.status_code == 403

    @pytest.mark.parametrize(
        "payload",
        [
            {"tenantAutoBanFloor": 299},
            {"tenantAtRiskThreshold": 851},
            {"tenantAutoBanFloor": 500, "tenantAtRiskThreshold": 500},
        ],
    )
    async def test_out_of_range_is_400(
        self, client: Any, seeded: dict[str, int], payload: dict[str, int]
    ) -> None:
        r = await client.put(self._url(), headers=_h(1, scope="tenant:admin"), json=payload)
        assert r.status_code == 400

    async def test_unauthenticated_is_401(self, client: Any) -> None:
        assert (await client.get(self._url())).status_code == 401


class TestApplyBoundary:
    async def test_apply_reputation_calls_the_db_function_with_exact_args(
        self, rep_db: Any
    ) -> None:
        seen: list[tuple[str, list[Any]]] = []

        async def fake(sql: str, placeholders: list[Any] | None = None) -> list[list[Any]]:
            seen.append((sql, placeholders or []))
            return [[612, True]]

        rep_db.executesql_async = fake  # type: ignore[method-assign]
        result = await reputation_svc.apply_reputation(
            rep_db,
            tenant_id=1,
            community_id=2,
            user_uuid=_uuid(5),
            delta=12,
            reason_code=reputation_svc.ReasonCode.ADMIN_ADJUST,
            actor="admin:9",
            source_event="admin.reputation.adjust",
            idempotency_key="k",
        )
        assert (result.new_score, result.applied) == (612, True)
        sql, args = seen[0]
        assert "reputation_apply(" in sql
        assert args == [1, 2, _uuid(5), 12, 100, "admin:9", "admin.reputation.adjust", "k"]
