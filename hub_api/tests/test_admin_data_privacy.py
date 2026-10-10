"""Enterprise tenant-admin DSAR console -- `blueprints/v1/admin_data_privacy.py`.

SECURITY REVIEW surface (admin DSAR / tiering). Real JWTs via
`flask_core.auth.create_jwt_token`, real pydal queries against a sqlite
file (no mocking of the auth chain, the tenant fence, or the audit
table). The Enterprise gate is exercised two ways: a mocked
`feature_enabled` for the routing tests, and the REAL
`EntitlementClient` over fake PostHog/license gates (`entitlement_fakes`)
for the tier-resolution and fail-closed proofs.

One `AsyncDAL` per module with every non-`tenants` table emptied per test --
defining ~100 tables per test (`privacy_db`'s approach) costs seconds each.

Fail-first proofs (executed, not narrated -- each mutation applied
in-process, the named test re-run, then dropped):
- `test_gate_off_blocks_every_route_before_any_data_access` -- `_authorize()`
  with the `feature_enabled` check removed: 4 of 4 route params red.
- `test_cross_tenant_target_is_404_and_untouched` -- `resolve_scope()`
  reporting every existing user `in_tenant=True`: 3 of 3 red (the other
  tenant's user was acted on).
- `test_audit_write_failure_blocks_the_erasure` -- `_audit_begin()` swallowing
  its failure instead of failing closed: red (user erased with no audit row).
- `test_shared_identity_across_tenants_is_refused` +
  `test_admin_cannot_erase_themself` -- `_erase_refusal()` always `None`:
  2 of 2 red.
- `test_export_excludes_other_tenants_community_rows` -- `_community_scope()`
  returning no filter: red (the other tenant's rows disclosed).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import bcrypt
import pytest
from flask_core.database import AsyncDAL
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.admin_data_privacy as admin_module
from blueprints.v1.admin_data_privacy import admin_data_privacy_bp
from services import admin_data_privacy_service as svc
from services import data_privacy_service as privacy
from services.schema import bind_admin_privacy_tables
from tests.conftest import OTHER_TENANT_SLUG, TENANT_SLUG, make_user_token
from tests.entitlement_fakes import FakeFlagGate, FakeLicenseGate, install_entitlement_client

ACME_ID = 1
OTHER_ID = 2
FLAG = "waddles.compliance.bulk_dsar"


def _define_tenants(dal: Any, *, migrate: bool) -> None:
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
        migrate=migrate,
    )


@pytest.fixture(scope="module")
def module_db(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Build the schema + two tenants ONCE per module (defining ~100 tables costs seconds)."""
    path = tmp_path_factory.mktemp("admin-dsar") / "admin_dsar.db"
    async_dal = AsyncDAL(f"sqlite://{path}", pool_size=1)
    dal = async_dal.dal
    _define_tenants(dal, migrate=True)
    bind_admin_privacy_tables(dal, migrate=True)
    assert dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme", is_active=True) == ACME_ID
    assert dal.tenants.insert(slug=OTHER_TENANT_SLUG, display_name="Other", is_active=True) == (
        OTHER_ID
    )
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


@pytest.fixture
def db(module_db: Any) -> Any:
    """The module's database with every non-`tenants` table emptied -- per-test isolation."""
    dal = module_db.dal
    for table_name in dal.tables:
        if table_name != "tenants":
            dal(dal[table_name]).delete()
    dal.commit()
    return module_db


@pytest.fixture
def app(db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(admin_data_privacy_bp)
    quart_app.config["dal"] = db.dal
    quart_app.config["async_dal"] = db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture
def gate_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Enterprise gate ON (mocked `feature_enabled`) -- for routing/behavior tests."""
    gate = AsyncMock(return_value=True)
    monkeypatch.setattr(admin_module, "feature_enabled", gate)
    return gate


def _user(db: Any, *, name: str, is_super_admin: bool = False, password: str | None = None) -> int:
    password_hash = (
        bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode() if password else None
    )
    user_id = db.dal.hub_users.insert(
        email=f"{name}@example.com",
        username=name,
        display_name=name.title(),
        password_hash=password_hash,
        is_super_admin=is_super_admin,
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.dal.commit()
    return int(user_id)  # plain int: a pydal `Reference` crashes quart-schema's test client


def _community(db: Any, *, tenant_id: int, name: str, is_global: bool = False) -> int:
    community_id = db.dal.communities.insert(
        name=name, display_name=name, tenant_id=tenant_id, is_global=is_global, is_active=True
    )
    db.dal.commit()
    return int(community_id)


def _member(db: Any, *, community_id: int, user_id: int | str) -> None:
    db.dal.community_members.insert(community_id=community_id, user_id=str(user_id), role="member")
    db.dal.commit()


def _tenant_admin(db: Any, *, tenant_id: int, user_id: int) -> None:
    db.dal.tenant_admins.insert(tenant_id=tenant_id, user_id=user_id)
    db.dal.commit()


def _message_event(db: Any, *, community_id: int, user_id: int) -> None:
    db.dal.activity_message_events.insert(
        community_id=community_id,
        hub_user_id=user_id,
        platform="discord",
        platform_user_id=f"p{user_id}",
        platform_username="someone",
        channel_id="c1",
        created_at=datetime.now(UTC),
    )
    db.dal.commit()


def _audit_rows(db: Any) -> list[Any]:
    rows = db.dal(db.dal.audit_log.id > 0).select(orderby=db.dal.audit_log.id)
    db.dal.commit()
    return list(rows)


def _user_row(db: Any, user_id: int) -> Any:
    row = db.dal(db.dal.hub_users.id == user_id).select().first()
    db.dal.commit()
    return row


def _headers(
    admin_id: int, *, scope: str = "tenant:admin", tenant: str = TENANT_SLUG
) -> dict[str, str]:
    token = make_user_token(user_id=admin_id, scope=scope, tenant=tenant)
    return {"Authorization": f"Bearer {token}"}


def _base(slug: str = TENANT_SLUG) -> str:
    return f"/api/v1/tenant/{slug}/privacy"


@pytest.fixture
def world(db: Any) -> dict[str, int]:
    """Acme admin + Acme member, an Other-only member, and the two tenants' communities."""
    acme_community = _community(db, tenant_id=ACME_ID, name="acme-main")
    other_community = _community(db, tenant_id=OTHER_ID, name="other-main")
    admin = _user(db, name="admin")
    _tenant_admin(db, tenant_id=ACME_ID, user_id=admin)
    alice = _user(db, name="alice")
    _member(db, community_id=acme_community, user_id=alice)
    mallory = _user(db, name="mallory")
    _member(db, community_id=other_community, user_id=mallory)
    return {
        "acme_community": acme_community,
        "other_community": other_community,
        "admin": admin,
        "alice": alice,
        "mallory": mallory,
    }


class TestEnterpriseGate:
    ROUTES = [
        ("get", "/users/{uid}/export", None),
        ("post", "/users/{uid}/erase", {"confirm": True}),
        ("put", "/users/{uid}/do-not-sell", None),
        ("post", "/bulk", {"action": "do_not_sell", "userIds": ["{uid}"]}),
    ]

    @staticmethod
    async def _call(client: Any, world: dict[str, int], method: str, path: str, body: Any) -> Any:
        uid = world["alice"]
        url = _base() + path.format(uid=uid)
        kwargs: dict[str, Any] = {"headers": _headers(world["admin"])}
        if body is not None:
            kwargs["json"] = {k: ([uid] if v == ["{uid}"] else v) for k, v in body.items()}
        return await getattr(client, method)(url, **kwargs)

    @pytest.mark.parametrize(("method", "path", "body"), ROUTES)
    async def test_gate_off_blocks_every_route_before_any_data_access(
        self,
        client: Any,
        db: Any,
        world: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
        method: str,
        path: str,
        body: Any,
    ) -> None:
        gate = AsyncMock(return_value=False)
        monkeypatch.setattr(admin_module, "feature_enabled", gate)

        response = await self._call(client, world, method, path, body)

        assert response.status_code == 402
        assert gate.await_args.args[0] == FLAG
        assert gate.await_args.kwargs["tenant"] == TENANT_SLUG
        assert _audit_rows(db) == []  # nothing was attempted, so nothing audited
        alice = _user_row(db, world["alice"])
        assert alice.email == "alice@example.com"  # untouched
        assert db.dal(db.dal.cookie_consent.user_id == world["alice"]).count() == 0

    @pytest.mark.parametrize(("method", "path", "body"), ROUTES)
    async def test_unreachable_gates_fail_closed(
        self,
        client: Any,
        db: Any,
        world: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
        method: str,
        path: str,
        body: Any,
    ) -> None:
        """Real `EntitlementClient`, PostHog AND license server both down, nothing cached."""
        install_entitlement_client(
            monkeypatch,
            flag_gate=FakeFlagGate(raises=ConnectionError("posthog down")),
            license_gate=FakeLicenseGate(raises=ConnectionError("license down")),
            tier_requirements={FLAG: "enterprise"},
        )

        response = await self._call(client, world, method, path, body)

        assert response.status_code == 402
        assert _audit_rows(db) == []

    @pytest.mark.parametrize(
        ("tier", "flag_result", "expected"),
        [
            ("free", True, 402),
            ("community", True, 402),  # penguin_licensing's unlicensed floor name
            ("professional", True, 402),
            ("enterprise", False, 402),  # entitled by license but flag OFF
            ("enterprise", None, 402),  # flag unresolvable -> fail closed
            ("enterprise", True, 200),
        ],
    )
    async def test_tier_resolution_through_the_real_two_gate(
        self,
        client: Any,
        world: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
        tier: str,
        flag_result: bool | None,
        expected: int,
    ) -> None:
        install_entitlement_client(
            monkeypatch,
            flag_gate=FakeFlagGate(result=flag_result),
            license_gate=FakeLicenseGate(tier=tier),
            tier_requirements={FLAG: "enterprise"},
        )

        response = await client.put(
            _base() + f"/users/{world['alice']}/do-not-sell", headers=_headers(world["admin"])
        )

        assert response.status_code == expected


class TestSelfServiceStaysUngated:
    """Statutory self-service DSAR must work in EVERY tier -- critical-rules.md."""

    async def test_self_service_works_with_every_gate_denied(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from blueprints.v1.data_privacy import data_privacy_bp

        flag_gate = FakeFlagGate(result=False)
        license_gate = FakeLicenseGate(tier="free")
        install_entitlement_client(
            monkeypatch,
            flag_gate=flag_gate,
            license_gate=license_gate,
            tier_requirements={FLAG: "enterprise"},
        )
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        quart_app.register_blueprint(data_privacy_bp)
        quart_app.config["dal"] = db.dal
        quart_app.config["async_dal"] = db
        test_client = quart_app.test_client()
        user_id = _user(db, name="selfserve")
        headers = {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}

        export = await test_client.get("/api/v1/user/me/data", headers=headers)
        deletion = await test_client.delete("/api/v1/user/me/data", headers=headers, json={})

        assert export.status_code == 200
        assert deletion.status_code == 200
        assert (flag_gate.calls, license_gate.calls) == ([], 0)  # never even consulted

    async def test_self_service_deletion_with_the_correct_password_still_works(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from blueprints.v1.data_privacy import data_privacy_bp

        install_entitlement_client(
            monkeypatch,
            flag_gate=FakeFlagGate(result=False),
            license_gate=FakeLicenseGate(tier="free"),
            tier_requirements={FLAG: "enterprise"},
        )
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        quart_app.register_blueprint(data_privacy_bp)
        quart_app.config["dal"] = db.dal
        quart_app.config["async_dal"] = db
        user_id = _user(db, name="withpw", password="correct-horse")
        headers = {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}

        response = await quart_app.test_client().delete(
            "/api/v1/user/me/data", headers=headers, json={"password": "correct-horse"}
        )

        assert response.status_code == 200
        assert _user_row(db, user_id).email == f"deleted_{user_id}@deleted.waddlebot"

    def test_self_service_modules_never_reference_the_entitlement_gate(self) -> None:
        import blueprints.v1.data_privacy as data_privacy_blueprint

        for module in (data_privacy_blueprint, privacy):
            source = Path(module.__file__).read_text()
            assert "feature_enabled" not in source
            assert "payment_required" not in source

    async def test_self_service_export_still_spans_every_community(
        self, db: Any, world: dict[str, int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The tenant-scoping added for admins must not narrow the subject's OWN export."""
        from blueprints.v1.data_privacy import data_privacy_bp

        _member(db, community_id=world["other_community"], user_id=world["alice"])
        _message_event(db, community_id=world["acme_community"], user_id=world["alice"])
        _message_event(db, community_id=world["other_community"], user_id=world["alice"])
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        quart_app.register_blueprint(data_privacy_bp)
        quart_app.config["dal"] = db.dal
        quart_app.config["async_dal"] = db

        response = await quart_app.test_client().get(
            "/api/v1/user/me/data", headers=_headers(world["alice"])
        )

        body = await response.get_json()
        communities = {row["community_id"] for row in body["data"]["message_activity"]}
        assert communities == {world["acme_community"], world["other_community"]}


class TestAuthz:
    async def test_no_token_is_401(self, client: Any, world: dict[str, int], gate_on: Any) -> None:
        response = await client.get(_base() + f"/users/{world['alice']}/export")
        assert response.status_code == 401

    async def test_missing_tenant_admin_scope_is_403(
        self, client: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.get(
            _base() + f"/users/{world['alice']}/export",
            headers=_headers(world["admin"], scope="tenant:read community:admin"),
        )
        assert response.status_code == 403
        gate_on.assert_not_awaited()  # scope is checked BEFORE the feature gate

    async def test_url_tenant_must_match_jwt_tenant(
        self, client: Any, world: dict[str, int], gate_on: Any, db: Any
    ) -> None:
        """An Acme admin pointing the URL at Other's slug is a 403, not a cross-tenant action."""
        response = await client.put(
            _base(OTHER_TENANT_SLUG) + f"/users/{world['mallory']}/do-not-sell",
            headers=_headers(world["admin"], tenant=TENANT_SLUG),
        )
        # tenant_middleware resolves Acme from the JWT; require_matching_tenant rejects the URL.
        assert response.status_code == 403
        assert db.dal(db.dal.cookie_consent.user_id == world["mallory"]).count() == 0

    async def test_global_admin_wildcard_scope_is_accepted(
        self, client: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.put(
            _base() + f"/users/{world['alice']}/do-not-sell",
            headers=_headers(world["admin"], scope="*:admin"),
        )
        assert response.status_code == 200


class TestTenantScope:
    @pytest.mark.parametrize(
        ("method", "suffix", "body"),
        [
            ("get", "export", None),
            ("post", "erase", {"confirm": True}),
            ("put", "do-not-sell", None),
        ],
    )
    async def test_cross_tenant_target_is_404_and_untouched(
        self,
        client: Any,
        db: Any,
        world: dict[str, int],
        gate_on: Any,
        method: str,
        suffix: str,
        body: Any,
    ) -> None:
        """Acme's admin cannot DSAR Other's user -- 404 (no existence oracle), nothing changes."""
        kwargs: dict[str, Any] = {"headers": _headers(world["admin"])}
        if body is not None:
            kwargs["json"] = body

        response = await getattr(client, method)(
            _base() + f"/users/{world['mallory']}/{suffix}", **kwargs
        )

        assert response.status_code == 404
        mallory = _user_row(db, world["mallory"])
        assert mallory.email == "mallory@example.com"
        assert mallory.is_active is True
        assert db.dal(db.dal.cookie_consent.user_id == world["mallory"]).count() == 0
        assert db.dal(db.dal.data_deletion_requests.id > 0).count() == 0
        rows = _audit_rows(db)
        assert len(rows) == 1  # the denied attempt itself is audited
        assert rows[0].details["outcome"] == "denied_not_in_tenant"
        assert rows[0].user_id == world["admin"]
        assert rows[0].target_id == str(world["mallory"])

    async def test_unknown_user_id_is_404(
        self, client: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.get(
            _base() + "/users/99999/export", headers=_headers(world["admin"])
        )
        assert response.status_code == 404

    async def test_user_in_no_tenant_is_404(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        orphan = _user(db, name="orphan")
        response = await client.get(
            _base() + f"/users/{orphan}/export", headers=_headers(world["admin"])
        )
        assert response.status_code == 404

    async def test_tenant_admin_row_counts_as_membership(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        second_admin = _user(db, name="admin2")
        _tenant_admin(db, tenant_id=ACME_ID, user_id=second_admin)
        response = await client.get(
            _base() + f"/users/{second_admin}/export", headers=_headers(world["admin"])
        )
        assert response.status_code == 200

    async def test_other_identity_strings_in_community_members_never_match(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        _member(db, community_id=world["acme_community"], user_id="discord-12345")
        response = await client.get(
            _base() + f"/users/{world['alice']}/export", headers=_headers(world["admin"])
        )
        assert response.status_code == 200

    async def test_export_excludes_other_tenants_community_rows(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        """A user active in BOTH tenants: Acme's admin never sees Other's rows."""
        _member(db, community_id=world["other_community"], user_id=world["alice"])
        _message_event(db, community_id=world["acme_community"], user_id=world["alice"])
        _message_event(db, community_id=world["other_community"], user_id=world["alice"])

        response = await client.get(
            _base() + f"/users/{world['alice']}/export", headers=_headers(world["admin"])
        )

        body = await response.get_json()
        events = body["result"]["data"]["message_activity"]
        assert [e["community_id"] for e in events] == [world["acme_community"]]

    async def test_export_for_tenant_with_no_communities_is_empty_not_unscoped(
        self, client: Any, db: Any, gate_on: Any
    ) -> None:
        """Empty community scope must match NOTHING (not degrade to 'no filter')."""
        lonely_admin = _user(db, name="lonelyadmin")
        _tenant_admin(db, tenant_id=ACME_ID, user_id=lonely_admin)
        other_community = _community(db, tenant_id=OTHER_ID, name="other-x")
        _message_event(db, community_id=other_community, user_id=lonely_admin)

        response = await client.get(
            _base() + f"/users/{lonely_admin}/export", headers=_headers(lonely_admin)
        )

        body = await response.get_json()
        assert body["result"]["data"]["message_activity"] == []

    async def test_global_community_membership_is_tenant_neutral_for_erase(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        """Everyone is auto-joined to the global community; that must not block an erase."""
        global_community = _community(db, tenant_id=OTHER_ID, name="global", is_global=True)
        _member(db, community_id=global_community, user_id=world["alice"])

        response = await client.post(
            _base() + f"/users/{world['alice']}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 200

    async def test_config_flagged_global_community_is_also_tenant_neutral(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        """Node's competing signal: `config.is_global == "true"` with the boolean column False."""
        community_id = _community(db, tenant_id=OTHER_ID, name="global-by-config")
        db.dal(db.dal.communities.id == community_id).update(config={"is_global": "true"})
        db.dal.commit()
        _member(db, community_id=community_id, user_id=world["alice"])

        response = await client.post(
            _base() + f"/users/{world['alice']}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 200

    async def test_global_community_owned_by_acme_makes_a_user_acmes(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        """A user only in the global community belongs to the tenant that owns it."""
        global_community = _community(db, tenant_id=ACME_ID, name="acme-global", is_global=True)
        newcomer = _user(db, name="newcomer")
        _member(db, community_id=global_community, user_id=newcomer)

        response = await client.get(
            _base() + f"/users/{newcomer}/export", headers=_headers(world["admin"])
        )

        assert response.status_code == 200


class TestExport:
    async def test_export_returns_data_header_and_audit_row(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        _message_event(db, community_id=world["acme_community"], user_id=world["alice"])

        response = await client.get(
            _base() + f"/users/{world['alice']}/export", headers=_headers(world["admin"])
        )

        assert response.status_code == 200
        assert f"waddles-dsar-{world['alice']}.json" in response.headers["Content-Disposition"]
        body = await response.get_json()
        assert body["success"] is True
        assert body["action"] == "export"
        assert body["result"]["userId"] == world["alice"]
        assert body["result"]["status"] == "completed"
        account = body["result"]["data"]["account"][0]
        assert account["email"] == "alice@example.com"
        assert "password_hash" not in account
        rows = _audit_rows(db)
        assert len(rows) == 1
        row = rows[0]
        assert (row.action, row.target_type, row.target_id) == (
            "dsar.export",
            "user",
            str(world["alice"]),
        )
        assert row.user_id == world["admin"]
        assert row.details["tenant_id"] == ACME_ID
        assert row.details["tenant_slug"] == TENANT_SLUG
        assert row.details["outcome"] == "completed"
        assert row.details["row_counts"]["message_activity"] == 1
        # PII-free audit: ids/counts/enums only -- never the subject's email/username.
        assert "alice" not in str(row.details)

    async def test_partial_export_reports_failed_source_and_still_audits(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any, monkeypatch: Any
    ) -> None:
        async def boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError("source down")

        monkeypatch.setattr(privacy, "_export_passkeys", boom)

        response = await client.get(
            _base() + f"/users/{world['alice']}/export", headers=_headers(world["admin"])
        )

        body = await response.get_json()
        assert response.status_code == 200
        assert body["result"]["incomplete"] == [{"source": "passkeys", "error": "source down"}]
        assert _audit_rows(db)[0].details["incomplete_sources"] == ["passkeys"]


class TestErase:
    async def test_erase_requires_confirm(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.post(
            _base() + f"/users/{world['alice']}/erase", headers=_headers(world["admin"]), json={}
        )
        assert response.status_code == 400
        assert _user_row(db, world["alice"]).email == "alice@example.com"
        assert _audit_rows(db) == []

    async def test_erase_anonymizes_records_and_audits(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        _message_event(db, community_id=world["acme_community"], user_id=world["alice"])

        response = await client.post(
            _base() + f"/users/{world['alice']}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["result"]["status"] == "completed"
        alice = _user_row(db, world["alice"])
        assert alice.email == f"deleted_{world['alice']}@deleted.waddlebot"
        assert alice.is_active is False
        assert db.dal(db.dal.activity_message_events.hub_user_id == world["alice"]).count() == 0
        deletion = db.dal(db.dal.data_deletion_requests.hub_user_id == world["alice"]).select()
        assert [d.status for d in deletion] == ["completed"]
        rows = _audit_rows(db)
        assert [(r.action, r.details["outcome"]) for r in rows] == [("dsar.erase", "completed")]
        assert rows[0].user_id == world["admin"]

    async def test_erase_needs_no_password_for_an_account_that_has_one(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        """The admin cannot know the subject's password; the audit trail is the control instead."""
        carol = _user(db, name="carol", password="s3cret-pass")
        _member(db, community_id=world["acme_community"], user_id=carol)

        response = await client.post(
            _base() + f"/users/{carol}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 200
        assert _user_row(db, carol).password_hash is None

    async def test_erase_is_idempotent(
        self, client: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        url = _base() + f"/users/{world['alice']}/erase"
        first = await client.post(url, headers=_headers(world["admin"]), json={"confirm": True})
        second = await client.post(url, headers=_headers(world["admin"]), json={"confirm": True})

        assert first.status_code == 200
        assert second.status_code == 200
        assert (await second.get_json())["result"]["status"] == "already_done"

    async def test_admin_cannot_erase_themself(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.post(
            _base() + f"/users/{world['admin']}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 409
        assert _user_row(db, world["admin"]).email == "admin@example.com"
        assert _audit_rows(db)[0].details["outcome"] == "denied_self"

    async def test_platform_super_admin_cannot_be_erased_from_a_tenant(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        root = _user(db, name="root", is_super_admin=True)
        _member(db, community_id=world["acme_community"], user_id=root)

        response = await client.post(
            _base() + f"/users/{root}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 409
        assert _user_row(db, root).email == "root@example.com"
        assert _audit_rows(db)[0].details["outcome"] == "denied_super_admin"

    async def test_shared_identity_across_tenants_is_refused(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        """`hub_users` is one global row -- a user also in Other's community is not tenant-local."""
        _member(db, community_id=world["other_community"], user_id=world["alice"])

        response = await client.post(
            _base() + f"/users/{world['alice']}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 409
        assert _user_row(db, world["alice"]).email == "alice@example.com"
        assert _audit_rows(db)[0].details["outcome"] == "denied_shared_identity"

    async def test_foreign_tenant_admin_row_also_blocks_erase(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        _tenant_admin(db, tenant_id=OTHER_ID, user_id=world["alice"])

        response = await client.post(
            _base() + f"/users/{world['alice']}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 409

    async def test_audit_write_failure_blocks_the_erasure(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any, monkeypatch: Any
    ) -> None:
        """Unaudited admin erasure is a compliance defect -- the audit write fails CLOSED."""
        original = db.insert_async

        async def failing(table: Any, **fields: Any) -> Any:
            if table._tablename == "audit_log":
                raise RuntimeError("audit store down")
            return await original(table, **fields)

        monkeypatch.setattr(db, "insert_async", failing)

        response = await client.post(
            _base() + f"/users/{world['alice']}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 503
        body = await response.get_json()
        assert body["result"]["status"] == "audit_unavailable"
        assert _user_row(db, world["alice"]).email == "alice@example.com"  # NOT erased
        assert db.dal(db.dal.data_deletion_requests.id > 0).count() == 0

    async def test_action_failure_is_reported_and_audited_as_failed(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any, monkeypatch: Any
    ) -> None:
        async def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("disk full")

        monkeypatch.setattr(privacy, "anonymize_user_data", boom)

        response = await client.post(
            _base() + f"/users/{world['alice']}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 500
        row = _audit_rows(db)[0]
        assert row.details["outcome"] == "failed"
        assert row.details["error_type"] == "RuntimeError"
        assert "disk full" not in str(row.details)

    async def test_audit_finish_failure_never_changes_the_result(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any, monkeypatch: Any
    ) -> None:
        original = db.update_async

        async def failing(query: Any, **fields: Any) -> Any:
            if "details" in fields:
                raise RuntimeError("audit update failed")
            return await original(query, **fields)

        monkeypatch.setattr(db, "update_async", failing)

        response = await client.put(
            _base() + f"/users/{world['alice']}/do-not-sell", headers=_headers(world["admin"])
        )

        assert response.status_code == 200
        assert _audit_rows(db)[0].details["outcome"] == "attempted"  # durable row still exists

    async def test_denied_audit_write_failure_does_not_break_the_404(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any, monkeypatch: Any
    ) -> None:
        original = db.insert_async

        async def failing(table: Any, **fields: Any) -> Any:
            if table._tablename == "audit_log":
                raise RuntimeError("audit store down")
            return await original(table, **fields)

        monkeypatch.setattr(db, "insert_async", failing)

        response = await client.get(
            _base() + f"/users/{world['mallory']}/export", headers=_headers(world["admin"])
        )

        assert response.status_code == 404


class TestDoNotSell:
    async def test_creates_a_privacy_maximal_record_when_none_exists(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.put(
            _base() + f"/users/{world['alice']}/do-not-sell", headers=_headers(world["admin"])
        )

        assert response.status_code == 200
        row = db.dal(db.dal.cookie_consent.user_id == world["alice"]).select().first()
        assert row.preferences["doNotSell"] is True
        assert row.preferences["marketing"] is False
        assert row.preferences["analytics"] is False
        assert row.consent_method == "admin_dsar"
        audit = db.dal(db.dal.cookie_audit_log.user_id == world["alice"]).select().first()
        assert audit.action == "ADMIN_DO_NOT_SELL"
        rows = _audit_rows(db)
        assert [(r.action, r.details["outcome"]) for r in rows] == [
            ("dsar.do_not_sell", "completed")
        ]

    async def test_updates_existing_records_and_forces_marketing_off(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        db.dal.cookie_consent.insert(
            user_id=world["alice"],
            consent_id="cid-1",
            preferences={
                "necessary": True,
                "analytics": True,
                "marketing": True,
                "doNotSell": False,
            },
            consent_version="1.0",
        )
        db.dal.commit()

        response = await client.put(
            _base() + f"/users/{world['alice']}/do-not-sell", headers=_headers(world["admin"])
        )

        assert response.status_code == 200
        row = db.dal(db.dal.cookie_consent.consent_id == "cid-1").select().first()
        assert row.preferences["doNotSell"] is True
        assert row.preferences["marketing"] is False
        assert row.preferences["analytics"] is True  # only the sale/sharing categories change

    async def test_is_idempotent_and_one_way(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        url = _base() + f"/users/{world['alice']}/do-not-sell"
        first = await client.put(url, headers=_headers(world["admin"]))
        second = await client.put(url, headers=_headers(world["admin"]))

        assert (await first.get_json())["result"]["status"] == "completed"
        assert (await second.get_json())["result"]["status"] == "already_done"
        assert db.dal(db.dal.cookie_consent.user_id == world["alice"]).count() == 1

    async def test_only_the_target_user_is_changed(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        bob = _user(db, name="bob")
        _member(db, community_id=world["acme_community"], user_id=bob)

        await client.put(
            _base() + f"/users/{world['alice']}/do-not-sell", headers=_headers(world["admin"])
        )

        assert db.dal(db.dal.cookie_consent.user_id == bob).count() == 0

    async def test_new_record_adopts_the_active_policy_version(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        db.dal.cookie_policy_versions.insert(version="2.3", content="policy", is_active=True)
        db.dal.commit()

        await client.put(
            _base() + f"/users/{world['alice']}/do-not-sell", headers=_headers(world["admin"])
        )

        row = db.dal(db.dal.cookie_consent.user_id == world["alice"]).select().first()
        assert row.consent_version == "2.3"


class TestBulk:
    async def test_bulk_do_not_sell_mixed_targets_never_aborts_the_batch(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        bob = _user(db, name="bob")
        _member(db, community_id=world["acme_community"], user_id=bob)

        response = await client.post(
            _base() + "/bulk",
            headers=_headers(world["admin"]),
            json={
                "action": "do_not_sell",
                "userIds": [world["alice"], world["mallory"], bob, 99999],
            },
        )

        assert response.status_code == 200
        body = await response.get_json()
        statuses = {r["userId"]: r["status"] for r in body["results"]}
        assert statuses == {
            world["alice"]: "completed",
            world["mallory"]: "not_found",  # other tenant
            bob: "completed",
            99999: "not_found",
        }
        assert (body["requested"], body["succeeded"], body["failed"]) == (4, 2, 2)
        assert body["success"] is False
        assert db.dal(db.dal.cookie_consent.user_id == world["mallory"]).count() == 0
        rows = _audit_rows(db)
        assert len(rows) == 4  # every target audited, including the denied ones
        bulk_ids = {r.details["bulk_id"] for r in rows}
        assert len(bulk_ids) == 1 and None not in bulk_ids

    async def test_bulk_success_flag_and_dedup(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.post(
            _base() + "/bulk",
            headers=_headers(world["admin"]),
            json={"action": "do_not_sell", "userIds": [world["alice"], world["alice"]]},
        )

        body = await response.get_json()
        assert body["requested"] == 1  # de-duplicated: never runs/audits twice
        assert body["success"] is True
        assert len(_audit_rows(db)) == 1
        assert _audit_rows(db)[0].details["bulk_id"] is None  # single target -> no bulk id

    async def test_bulk_erase_one_failure_does_not_abort_the_rest(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any, monkeypatch: Any
    ) -> None:
        bob = _user(db, name="bob")
        _member(db, community_id=world["acme_community"], user_id=bob)
        original = privacy.anonymize_user_data

        async def flaky(async_dal: Any, dal: Any, *, user_id: int, email: Any) -> None:
            if user_id == world["alice"]:
                raise RuntimeError("boom")
            await original(async_dal, dal, user_id=user_id, email=email)

        monkeypatch.setattr(privacy, "anonymize_user_data", flaky)

        response = await client.post(
            _base() + "/bulk",
            headers=_headers(world["admin"]),
            json={"action": "erase", "userIds": [world["alice"], bob], "confirm": True},
        )

        body = await response.get_json()
        statuses = {r["userId"]: r["status"] for r in body["results"]}
        assert statuses == {world["alice"]: "failed", bob: "completed"}
        assert _user_row(db, bob).email == f"deleted_{bob}@deleted.waddlebot"
        assert _user_row(db, world["alice"]).email == "alice@example.com"

    async def test_bulk_export_returns_each_users_data(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        bob = _user(db, name="bob")
        _member(db, community_id=world["acme_community"], user_id=bob)

        response = await client.post(
            _base() + "/bulk",
            headers=_headers(world["admin"]),
            json={"action": "export", "userIds": [world["alice"], bob]},
        )

        body = await response.get_json()
        emails = {r["data"]["account"][0]["email"] for r in body["results"]}
        assert emails == {"alice@example.com", "bob@example.com"}

    @pytest.mark.parametrize(
        ("payload", "needle"),
        [
            ({"action": "erase", "userIds": [1]}, "confirm"),
            ({"action": "do_not_sell", "userIds": []}, "non-empty"),
            ({"action": "do_not_sell", "userIds": [0]}, "positive"),
            ({"action": "do_not_sell", "userIds": [-3]}, "positive"),
            ({"action": "do_not_sell", "userIds": list(range(1, 102))}, "At most 100"),
            ({"action": "export", "userIds": list(range(1, 27))}, "At most 25"),
        ],
    )
    async def test_bulk_validation_is_400_before_any_audit(
        self,
        client: Any,
        db: Any,
        world: dict[str, int],
        gate_on: Any,
        payload: dict[str, Any],
        needle: str,
    ) -> None:
        response = await client.post(
            _base() + "/bulk", headers=_headers(world["admin"]), json=payload
        )

        assert response.status_code == 400
        assert needle in (await response.get_json())["error"]["message"]
        assert _audit_rows(db) == []

    @pytest.mark.parametrize(
        "payload",
        [
            {"action": "drop_tables", "userIds": [1]},
            {"userIds": [1]},
            {"action": "export"},
            {"action": "export", "userIds": ["abc"]},
        ],
    )
    async def test_bulk_rejects_malformed_bodies(
        self, client: Any, world: dict[str, int], gate_on: Any, payload: dict[str, Any]
    ) -> None:
        response = await client.post(
            _base() + "/bulk", headers=_headers(world["admin"]), json=payload
        )
        assert response.status_code in (400, 422)


class TestScopeResolution:
    async def test_resolve_scope_batches_and_classifies(
        self, db: Any, world: dict[str, int]
    ) -> None:
        both = _user(db, name="both")
        _member(db, community_id=world["acme_community"], user_id=both)
        _member(db, community_id=world["other_community"], user_id=both)

        tenant_communities, scopes = await svc.resolve_scope(
            db, db.dal, tenant_id=ACME_ID, user_ids=[world["alice"], world["mallory"], both, 4242]
        )

        assert tenant_communities == frozenset({world["acme_community"]})
        assert scopes[world["alice"]].in_tenant and not scopes[world["alice"]].foreign_tenant_ids
        assert not scopes[world["mallory"]].in_tenant
        assert scopes[both].in_tenant and scopes[both].foreign_tenant_ids == {OTHER_ID}
        assert not scopes[4242].exists

    def test_validate_rejects_bool_and_non_int_ids(self) -> None:
        actor = svc.DsarActor(user_id=1, tenant_id=ACME_ID, tenant_slug=TENANT_SLUG)
        for bad in ([True], ["3"], [1.5], [None]):
            with pytest.raises(svc.ApiError):
                svc.validate_dsar_request(
                    actor=actor,
                    action=svc.DsarAction.EXPORT,
                    user_ids=bad,
                    confirm=False,  # type: ignore[arg-type]
                )

    def test_max_users_for_each_action(self) -> None:
        assert svc.max_users_for(svc.DsarAction.EXPORT) == svc.MAX_BULK_EXPORT_USERS
        assert svc.max_users_for(svc.DsarAction.ERASE) == svc.MAX_BULK_USERS
        assert svc.max_users_for(svc.DsarAction.DO_NOT_SELL) == svc.MAX_BULK_USERS


class TestPathUserId:
    @pytest.mark.parametrize("raw", ["abc", "0", "-1", "1.5", "\u0661\u0662", "9" * 19, "%20"])
    async def test_malformed_user_id_is_400_after_the_gate(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any, raw: str
    ) -> None:
        response = await client.get(
            _base() + f"/users/{raw}/export", headers=_headers(world["admin"])
        )

        assert response.status_code == 400
        assert _audit_rows(db) == []

    async def test_malformed_user_id_still_hits_the_gate_first(
        self, client: Any, world: dict[str, int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(admin_module, "feature_enabled", AsyncMock(return_value=False))

        response = await client.get(_base() + "/users/abc/export", headers=_headers(world["admin"]))

        assert response.status_code == 402


class TestOpenApiPaths:
    def test_single_user_routes_publish_their_full_paths(self, app: Quart) -> None:
        """quart-schema collapses a typed param after another; untyped `<user_id>` must not."""
        schema = app.extensions["QUART_SCHEMA"].openapi_provider.schema()

        paths = set(schema["paths"])
        base = "/api/v1/tenant/{tenant_slug}/privacy"
        assert f"{base}/users/{{user_id}}/export" in paths
        assert f"{base}/users/{{user_id}}/erase" in paths
        assert f"{base}/users/{{user_id}}/do-not-sell" in paths
        assert f"{base}/bulk" in paths
        assert not any(path.startswith("/api/v1/tenant/{user_id}") for path in paths)


class TestScopeEdgeCases:
    async def test_member_row_with_null_community_is_ignored(
        self, db: Any, world: dict[str, int]
    ) -> None:
        db.dal.community_members.insert(community_id=None, user_id=str(world["alice"]))
        db.dal.commit()

        _, scopes = await svc.resolve_scope(
            db, db.dal, tenant_id=ACME_ID, user_ids=[world["alice"]]
        )

        assert scopes[world["alice"]].in_tenant  # still via the real Acme membership
        assert scopes[world["alice"]].foreign_tenant_ids == frozenset()

    async def test_erase_of_a_vanished_user_is_not_found(self, db: Any) -> None:
        result = await svc._erase_one(db, db.dal, user_id=99999)

        assert result.status is svc.DsarStatus.NOT_FOUND


class TestAnonymizeCore:
    """`anonymize_user_data()` -- the erasure core shared with self-service."""

    async def test_failure_is_recorded_then_the_original_error_propagates(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id = _user(db, name="victim")
        original = db.delete_async

        async def failing(query: Any) -> Any:
            if "hub_user_profiles" in str(query):
                raise RuntimeError("profiles locked")
            return await original(query)

        monkeypatch.setattr(db, "delete_async", failing)

        with pytest.raises(RuntimeError, match="profiles locked"):
            await privacy.anonymize_user_data(
                db, db.dal, user_id=user_id, email="victim@example.com"
            )

        rows = list(db.dal(db.dal.data_deletion_requests.hub_user_id == user_id).select())
        db.dal.commit()
        assert [r.status for r in rows] == ["failed"]
        assert "profiles locked" in rows[0].error_detail
        assert _user_row(db, user_id).email == "victim@example.com"  # not anonymized

    async def test_failure_to_record_the_failure_never_masks_the_original_error(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id = _user(db, name="victim2")

        async def failing_delete(query: Any) -> Any:
            raise RuntimeError("original failure")

        async def failing_insert(table: Any, **fields: Any) -> Any:
            raise OSError("cannot record either")

        monkeypatch.setattr(db, "delete_async", failing_delete)
        monkeypatch.setattr(db, "insert_async", failing_insert)

        with pytest.raises(RuntimeError, match="original failure"):
            await privacy.anonymize_user_data(db, db.dal, user_id=user_id, email=None)

    async def test_account_without_an_email_is_still_anonymized(self, db: Any) -> None:
        user_id = _user(db, name="noemail")
        db.dal(db.dal.hub_users.id == user_id).update(email=None)
        db.dal.commit()

        await privacy.anonymize_user_data(db, db.dal, user_id=user_id, email=None)

        row = db.dal(db.dal.data_deletion_requests.hub_user_id == user_id).select().first()
        db.dal.commit()
        assert row.status == "completed"
        assert row.deletion_scope["temp_passwords"] == 0
