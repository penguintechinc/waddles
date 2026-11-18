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
- `test_bulk_gate_off_blocks_before_any_data_access` -- `_authorize(bulk=True)`
  with the `feature_enabled` check removed: red. Single-subject routes are NOT gated
  (`test_single_subject_routes_never_consult_the_enterprise_gate`).
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
- `test_production_catalog_alone_denies_bulk_below_enterprise` --
  `waddles.compliance.bulk_dsar` removed from `flask_core.tier_catalog`: 3 of 3
  tiers red -- the gate was fail-open for a flag-on Free tenant.
- `test_self_service_dsar_erasure_and_do_not_sell_work_in_every_tier` -- the self-service
  export handler consulting the entitlement gate: 14 red (12 tier/flag params + 2 siblings).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock
from uuid import UUID

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
from services.cookie_consent_service import (
    DNS_SOURCE_TENANT_ADMIN_DSAR,
    effective_do_not_sell,
)
from services.global_identity import grant_global_admin_sync
from services.schema import bind_admin_privacy_tables, bind_tenant_tables
from tests.conftest import OTHER_TENANT_SLUG, TENANT_SLUG, make_user_token
from tests.entitlement_fakes import FakeFlagGate, FakeLicenseGate, install_entitlement_client

ACME_ID = 1
OTHER_ID = 2
FLAG = "waddles.compliance.bulk_dsar"
UNKNOWN_UUID = "9d1c2f4e-3b5a-4c6d-8e7f-0a1b2c3d4e5f"


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
    bind_tenant_tables(dal, migrate=True)  # extends `communities` with `public_id`
    assert dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme", is_active=True) == ACME_ID
    assert dal.tenants.insert(slug=OTHER_TENANT_SLUG, display_name="Other", is_active=True) == (
        OTHER_ID
    )
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal
    dal.close()


_CURRENT_DB: list[Any] = []


def _uuids(count: int) -> list[str]:
    """`count` distinct well-formed (unknown) UUID strings."""
    return [str(UUID(int=i + 1)) for i in range(count)]


def _uuid(user_id: int) -> str:
    """The boundary identifier (`hub_users.uuid`) of internal user `user_id` (current test DB)."""
    db = _CURRENT_DB[-1]
    row = db.dal(db.dal.hub_users.id == user_id).select(db.dal.hub_users.uuid).first()
    db.dal.commit()
    return str(row.uuid)


@pytest.fixture
def db(module_db: Any) -> Any:
    """The module's database with every non-`tenants` table emptied -- per-test isolation."""
    _CURRENT_DB[:] = [module_db]
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
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    if is_super_admin:
        grant_global_admin_sync(db.dal, int(user_id))
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


def _chat_message(db: Any, *, community_id: int, user_id: int, text: str = "hello") -> None:
    db.dal.hub_chat_messages.insert(
        community_id=community_id,
        channel_name="general",
        sender_hub_user_id=user_id,
        sender_platform="discord",
        sender_username="someone",
        message_content=text,
        message_type="text",
        created_at=datetime.now(UTC),
    )
    db.dal.commit()


def _community_public_id(db: Any, community_id: int) -> str:
    row = db.dal(db.dal.communities.id == community_id).select(db.dal.communities.public_id).first()
    db.dal.commit()
    return str(row.public_id)


def _chat_count(db: Any, user_id: int) -> int:
    count = db.dal(db.dal.hub_chat_messages.sender_hub_user_id == user_id).count()
    db.dal.commit()
    return int(count)


def _fill_member_pii(db: Any, *, user_id: int) -> None:
    """Give every `community_members` row of `user_id` the full set of profile PII."""
    db.dal(db.dal.community_members.user_id == str(user_id)).update(
        display_name="Sentinel Name",
        avatar_url="https://cdn.example/sentinel.png",
        bio="sentinel bio",
        social_links={"twitter": "@sentinel"},
        platform_user_id=f"discord-{user_id}",
    )
    db.dal.commit()


def _member_pii_rows(db: Any, *, user_id: int) -> list[tuple[Any, ...]]:
    rows = db.dal(db.dal.community_members.user_id == str(user_id)).select(
        orderby=db.dal.community_members.id
    )
    db.dal.commit()
    return [(r.display_name, r.avatar_url, r.bio, r.social_links, r.platform_user_id) for r in rows]


def db_deletion_requests(db: Any, user_id: int) -> int:
    """Number of `data_deletion_requests` ledger rows for `user_id`."""
    count = db.dal(db.dal.data_deletion_requests.hub_user_id == user_id).count()
    db.dal.commit()
    return int(count)


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
    """Only BULK is Enterprise-gated.

    Single-subject export/erase/do-not-sell are statutory rights and work in every tier
    (regression: the whole console was gated before the DSAR inbox).
    """

    SINGLE_ROUTES = [
        ("get", "/users/{uid}/export", None),
        ("post", "/users/{uid}/erase", {"confirm": True}),
        ("put", "/users/{uid}/do-not-sell", None),
    ]
    BULK_ROUTE = ("post", "/bulk", {"action": "do_not_sell", "userIds": ["{uid}"]})
    ROUTES = [*SINGLE_ROUTES, BULK_ROUTE]

    @staticmethod
    async def _call(client: Any, world: dict[str, int], method: str, path: str, body: Any) -> Any:
        uid = _uuid(world["alice"])
        url = _base() + path.format(uid=uid)
        kwargs: dict[str, Any] = {"headers": _headers(world["admin"])}
        if body is not None:
            kwargs["json"] = {k: ([uid] if v == ["{uid}"] else v) for k, v in body.items()}
        return await getattr(client, method)(url, **kwargs)

    async def test_bulk_gate_off_blocks_before_any_data_access(
        self,
        client: Any,
        db: Any,
        world: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gate = AsyncMock(return_value=False)
        monkeypatch.setattr(admin_module, "feature_enabled", gate)

        response = await self._call(client, world, *self.BULK_ROUTE)

        assert response.status_code == 402
        assert gate.await_args.args[0] == FLAG
        assert gate.await_args.kwargs["tenant"] == TENANT_SLUG
        assert _audit_rows(db) == []  # nothing was attempted, so nothing audited
        alice = _user_row(db, world["alice"])
        assert alice.email == "alice@example.com"  # untouched
        assert db.dal(db.dal.cookie_consent.user_id == world["alice"]).count() == 0

    @pytest.mark.parametrize(("method", "path", "body"), SINGLE_ROUTES)
    async def test_single_subject_routes_never_consult_the_enterprise_gate(
        self,
        client: Any,
        world: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
        method: str,
        path: str,
        body: Any,
    ) -> None:
        gate = AsyncMock(return_value=False)  # gate OFF: single-subject must still work
        monkeypatch.setattr(admin_module, "feature_enabled", gate)

        response = await self._call(client, world, method, path, body)

        assert response.status_code == 200
        gate.assert_not_awaited()

    async def test_unreachable_gates_fail_closed_for_bulk(
        self,
        client: Any,
        db: Any,
        world: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Real `EntitlementClient`, PostHog AND license server both down, nothing cached."""
        install_entitlement_client(
            monkeypatch,
            flag_gate=FakeFlagGate(raises=ConnectionError("posthog down")),
            license_gate=FakeLicenseGate(raises=ConnectionError("license down")),
            tier_requirements={FLAG: "enterprise"},
        )

        response = await self._call(client, world, *self.BULK_ROUTE)

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
    async def test_bulk_tier_resolution_through_the_real_two_gate(
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

        response = await self._call(client, world, *self.BULK_ROUTE)

        assert response.status_code == expected

    @pytest.mark.parametrize("tier", ["free", "community", "professional", "enterprise"])
    async def test_single_subject_actions_work_in_every_tier(
        self, client: Any, world: dict[str, int], monkeypatch: pytest.MonkeyPatch, tier: str
    ) -> None:
        """Regression (statutory rights never tier-gated): Free/Pro tenants can run all three."""
        install_entitlement_client(
            monkeypatch,
            flag_gate=FakeFlagGate(result=False),  # even with the bulk flag OFF
            license_gate=FakeLicenseGate(tier=tier),
        )

        # Erase last: it deletes the membership rows the console proves tenant scope with.
        export, erase, do_not_sell = self.SINGLE_ROUTES
        for method, path, body in (export, do_not_sell, erase):
            response = await self._call(client, world, method, path, body)
            assert response.status_code == 200, (tier, method, path)

    @pytest.mark.parametrize("tier", ["free", "community", "professional"])
    async def test_production_catalog_alone_denies_bulk_below_enterprise(
        self,
        client: Any,
        db: Any,
        world: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
        tier: str,
    ) -> None:
        """Regression: `bulk_dsar` was missing from `tier_catalog.FEATURE_MIN_TIERS`.

        Every other test here hands `tier_requirements={FLAG: "enterprise"}` to the client,
        which masks that hub-api's REAL client (no registry populated, no explicit
        requirements) required only "free" for the flag -- a Free tenant with the PostHog flag
        on got the Enterprise console. Here NOTHING is passed: only the static catalog speaks.
        """
        flag_gate = FakeFlagGate(result=True)
        install_entitlement_client(
            monkeypatch, flag_gate=flag_gate, license_gate=FakeLicenseGate(tier=tier)
        )

        response = await self._call(client, world, *self.BULK_ROUTE)

        assert response.status_code == 402
        assert _audit_rows(db) == []
        assert _user_row(db, world["alice"]).email == "alice@example.com"  # untouched

    async def test_production_catalog_alone_admits_an_enterprise_tenant_to_bulk(
        self, client: Any, world: dict[str, int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_entitlement_client(
            monkeypatch,
            flag_gate=FakeFlagGate(result=True),
            license_gate=FakeLicenseGate("enterprise"),
        )

        response = await self._call(client, world, *self.BULK_ROUTE)

        assert response.status_code == 200


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

    @pytest.mark.parametrize("tier", ["free", "community", "professional", "enterprise"])
    @pytest.mark.parametrize("flag_result", [False, None, True])
    async def test_self_service_dsar_erasure_and_do_not_sell_work_in_every_tier(
        self, db: Any, monkeypatch: pytest.MonkeyPatch, tier: str, flag_result: bool | None
    ) -> None:
        """Access, erasure AND Do-Not-Sell answer 200 whatever the tier or flag state.

        The bulk console for the same operations is Enterprise-only (see
        `TestEnterpriseGate`); these statutory rights never consult either gate.
        """
        from blueprints.v1.cookie_consent import cookie_consent_bp
        from blueprints.v1.data_privacy import data_privacy_bp

        flag_gate = FakeFlagGate(result=flag_result)
        license_gate = FakeLicenseGate(tier=tier)
        install_entitlement_client(monkeypatch, flag_gate=flag_gate, license_gate=license_gate)
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        quart_app.register_blueprint(data_privacy_bp)
        quart_app.register_blueprint(cookie_consent_bp)
        quart_app.config["dal"] = db.dal
        quart_app.config["async_dal"] = db
        quart_app.config["HUB_API_CONFIG"] = None
        test_client = quart_app.test_client()
        user_id = _user(db, name=f"tier-{tier}-{flag_result}")
        headers = {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}

        do_not_sell = await test_client.post(
            "/api/v1/cookie",
            headers=headers,
            json={"preferences": {"functional": True, "doNotSell": True}},
        )
        export = await test_client.get("/api/v1/user/me/data", headers=headers)
        deletion = await test_client.delete("/api/v1/user/me/data", headers=headers, json={})

        assert (do_not_sell.status_code, export.status_code, deletion.status_code) == (
            200,
            200,
            200,
        )
        assert (await do_not_sell.get_json())["data"]["preferences"]["doNotSell"] is True
        assert _user_row(db, user_id).email == f"deleted_{user_id}@deleted.waddlebot"
        assert (flag_gate.calls, license_gate.calls) == ([], 0)  # never even consulted

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
        response = await client.get(_base() + f"/users/{_uuid(world['alice'])}/export")
        assert response.status_code == 401

    async def test_missing_tenant_admin_scope_is_403(
        self, client: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.get(
            _base() + f"/users/{_uuid(world['alice'])}/export",
            headers=_headers(world["admin"], scope="tenant:read community:admin"),
        )
        assert response.status_code == 403
        gate_on.assert_not_awaited()  # scope is checked BEFORE the feature gate

    async def test_url_tenant_must_match_jwt_tenant(
        self, client: Any, world: dict[str, int], gate_on: Any, db: Any
    ) -> None:
        """An Acme admin pointing the URL at Other's slug is a 403, not a cross-tenant action."""
        response = await client.put(
            _base(OTHER_TENANT_SLUG) + f"/users/{_uuid(world['mallory'])}/do-not-sell",
            headers=_headers(world["admin"], tenant=TENANT_SLUG),
        )
        # tenant_middleware resolves Acme from the JWT; require_matching_tenant rejects the URL.
        assert response.status_code == 403
        assert db.dal(db.dal.cookie_consent.user_id == world["mallory"]).count() == 0

    async def test_global_admin_wildcard_scope_is_accepted(
        self, client: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.put(
            _base() + f"/users/{_uuid(world['alice'])}/do-not-sell",
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
            _base() + f"/users/{_uuid(world['mallory'])}/{suffix}", **kwargs
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
        assert rows[0].target_id == _uuid(world["mallory"])

    async def test_unknown_user_id_is_404(
        self, client: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.get(
            _base() + f"/users/{UNKNOWN_UUID}/export", headers=_headers(world["admin"])
        )
        assert response.status_code == 404

    async def test_user_in_no_tenant_is_404(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        orphan = _user(db, name="orphan")
        response = await client.get(
            _base() + f"/users/{_uuid(orphan)}/export", headers=_headers(world["admin"])
        )
        assert response.status_code == 404

    async def test_tenant_admin_row_counts_as_membership(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        second_admin = _user(db, name="admin2")
        _tenant_admin(db, tenant_id=ACME_ID, user_id=second_admin)
        response = await client.get(
            _base() + f"/users/{_uuid(second_admin)}/export", headers=_headers(world["admin"])
        )
        assert response.status_code == 200

    async def test_other_identity_strings_in_community_members_never_match(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        _member(db, community_id=world["acme_community"], user_id="discord-12345")
        response = await client.get(
            _base() + f"/users/{_uuid(world['alice'])}/export", headers=_headers(world["admin"])
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
            _base() + f"/users/{_uuid(world['alice'])}/export", headers=_headers(world["admin"])
        )

        body = await response.get_json()
        events = body["result"]["data"]["message_activity"]
        assert [e["community_id"] for e in events] == [
            _community_public_id(db, world["acme_community"])
        ]

    async def test_export_for_tenant_with_no_communities_is_empty_not_unscoped(
        self, client: Any, db: Any, gate_on: Any
    ) -> None:
        """Empty community scope must match NOTHING (not degrade to 'no filter')."""
        lonely_admin = _user(db, name="lonelyadmin")
        _tenant_admin(db, tenant_id=ACME_ID, user_id=lonely_admin)
        other_community = _community(db, tenant_id=OTHER_ID, name="other-x")
        _message_event(db, community_id=other_community, user_id=lonely_admin)

        response = await client.get(
            _base() + f"/users/{_uuid(lonely_admin)}/export", headers=_headers(lonely_admin)
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
            _base() + f"/users/{_uuid(world['alice'])}/erase",
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
            _base() + f"/users/{_uuid(world['alice'])}/erase",
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
            _base() + f"/users/{_uuid(newcomer)}/export", headers=_headers(world["admin"])
        )

        assert response.status_code == 200


class TestExport:
    async def test_export_returns_data_header_and_audit_row(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        _message_event(db, community_id=world["acme_community"], user_id=world["alice"])

        response = await client.get(
            _base() + f"/users/{_uuid(world['alice'])}/export", headers=_headers(world["admin"])
        )

        assert response.status_code == 200
        assert (
            f"waddles-dsar-{_uuid(world['alice'])}.json" in response.headers["Content-Disposition"]
        )
        body = await response.get_json()
        assert body["success"] is True
        assert body["action"] == "export"
        assert body["result"]["userId"] == _uuid(world["alice"])
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
            _uuid(world["alice"]),
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
            _base() + f"/users/{_uuid(world['alice'])}/export", headers=_headers(world["admin"])
        )

        body = await response.get_json()
        assert response.status_code == 200
        # Bounded reason code only -- never the raw exception text.
        assert body["result"]["incomplete"] == [
            {"source": "passkeys", "error": "source_unavailable"}
        ]
        assert body["result"]["status"] == "partial"
        assert body["success"] is False
        assert "source down" not in str(body)
        assert _audit_rows(db)[0].details["outcome"] == "partial"
        assert _audit_rows(db)[0].details["incomplete_sources"] == ["passkeys"]


class TestErase:
    async def test_erase_requires_confirm(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.post(
            _base() + f"/users/{_uuid(world['alice'])}/erase",
            headers=_headers(world["admin"]),
            json={},
        )
        assert response.status_code == 400
        assert _user_row(db, world["alice"]).email == "alice@example.com"
        assert _audit_rows(db) == []

    async def test_erase_anonymizes_records_and_audits(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        _message_event(db, community_id=world["acme_community"], user_id=world["alice"])

        response = await client.post(
            _base() + f"/users/{_uuid(world['alice'])}/erase",
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

    async def test_erase_removes_chat_messages_but_keeps_consent_and_audit_logs(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        """GRC#1 through the admin console: chat deleted, Art. 5(2) evidence retained."""
        alice = world["alice"]
        _chat_message(db, community_id=world["acme_community"], user_id=alice, text="one")
        _chat_message(db, community_id=world["other_community"], user_id=alice, text="two")
        db.dal.cookie_consent.insert(
            user_id=alice,
            consent_id="c-alice",
            consent_version="1.0",
            consented_at=datetime.now(UTC),
        )
        db.dal.commit()
        assert _chat_count(db, alice) == 2

        response = await client.post(
            _base() + f"/users/{_uuid(alice)}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 200
        assert _chat_count(db, alice) == 0  # both communities -- erasure is not tenant-scoped
        assert db.dal(db.dal.cookie_consent.user_id == alice).count() == 1
        assert [r.action for r in _audit_rows(db)] == ["dsar.erase"]

    async def test_erase_deletes_memberships_in_every_community_and_spares_others(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        """Art. 17: memberships (PII AND reputation) are deleted, not nulled-and-kept."""
        alice = world["alice"]
        second = _community(db, tenant_id=ACME_ID, name="acme-second")
        _member(db, community_id=second, user_id=alice)  # erasure spans every community
        _fill_member_pii(db, user_id=alice)
        _fill_member_pii(db, user_id=world["mallory"])
        assert len(_member_pii_rows(db, user_id=alice)) == 2

        response = await client.post(
            _base() + f"/users/{_uuid(alice)}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 200
        assert _member_pii_rows(db, user_id=alice) == []
        [mallory_row] = _member_pii_rows(db, user_id=world["mallory"])
        assert mallory_row[0] == "Sentinel Name"  # a bystander's membership is untouched
        [deletion] = db.dal(db.dal.data_deletion_requests.hub_user_id == alice).select()
        assert deletion.deletion_scope["community_members"] == 2
        assert deletion.deletion_scope["suppression_records_written"] == 0  # benign: nothing kept

    async def test_erase_needs_no_password_for_an_account_that_has_one(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        """The admin cannot know the subject's password; the audit trail is the control instead."""
        carol = _user(db, name="carol", password="s3cret-pass")
        _member(db, community_id=world["acme_community"], user_id=carol)

        response = await client.post(
            _base() + f"/users/{_uuid(carol)}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 200
        assert _user_row(db, carol).password_hash is None

    async def test_erase_is_idempotent(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        url = _base() + f"/users/{_uuid(world['alice'])}/erase"
        first = await client.post(url, headers=_headers(world["admin"]), json={"confirm": True})
        second = await client.post(url, headers=_headers(world["admin"]), json={"confirm": True})

        assert first.status_code == 200
        # The first erase deleted the memberships the console proves tenant scope with, so a
        # repeat is `not_found` (never an existence oracle, never a second erasure) -- before
        # suppression-only retention the retained member rows kept it `already_done`.
        assert second.status_code == 404
        assert db_deletion_requests(db, world["alice"]) == 1

    async def test_admin_cannot_erase_themself(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.post(
            _base() + f"/users/{_uuid(world['admin'])}/erase",
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
            _base() + f"/users/{_uuid(root)}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 409
        assert _user_row(db, root).email == "root@example.com"
        assert _audit_rows(db)[0].details["outcome"] == "denied_super_admin"

    async def test_bootstrap_platform_account_cannot_be_erased_from_a_tenant(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        """The non-person bootstrap account: typed 403 + audit, never erased (route level)."""
        root = _user(db, name="root", is_super_admin=True)
        _member(db, community_id=world["acme_community"], user_id=root)
        db.dal(db.dal.hub_users.id == root).update(is_bootstrap=True)
        db.dal.commit()

        response = await client.post(
            _base() + f"/users/{_uuid(root)}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 403
        body = await response.get_json()
        assert "PLATFORM_ACCOUNT_PROTECTED" in body["result"]["detail"]
        assert body["result"]["status"] == "forbidden"
        assert _user_row(db, root).email == "root@example.com"
        assert _audit_rows(db)[0].details["outcome"] == "denied_platform_account"

    async def test_shared_identity_across_tenants_is_refused(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        """`hub_users` is one global row -- a user also in Other's community is not tenant-local."""
        _member(db, community_id=world["other_community"], user_id=world["alice"])

        response = await client.post(
            _base() + f"/users/{_uuid(world['alice'])}/erase",
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
            _base() + f"/users/{_uuid(world['alice'])}/erase",
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
            _base() + f"/users/{_uuid(world['alice'])}/erase",
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
            _base() + f"/users/{_uuid(world['alice'])}/erase",
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
            _base() + f"/users/{_uuid(world['alice'])}/do-not-sell",
            headers=_headers(world["admin"]),
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
            _base() + f"/users/{_uuid(world['mallory'])}/export", headers=_headers(world["admin"])
        )

        assert response.status_code == 404


def _tenant_dns(db: Any, *, tenant_id: int, user_id: int) -> list[Any]:
    """The `tenant_do_not_sell` rows of `(tenant_id, user_id)`."""
    t = db.dal.tenant_do_not_sell
    rows = db.dal((t.tenant_id == tenant_id) & (t.user_id == user_id)).select().as_list()
    db.dal.commit()
    return rows


def _global_dns(db: Any, user_id: int, *, value: bool = True) -> None:
    """Give `user_id` their OWN global cookie-consent preference (`doNotSell=value`)."""
    db.dal.cookie_consent.insert(
        user_id=user_id,
        consent_id=f"cid-global-{user_id}",
        preferences={"necessary": True, "marketing": False, "doNotSell": value},
        consent_version="1.0",
    )
    db.dal.commit()


class TestDoNotSell:
    async def test_writes_only_the_tenant_record(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.put(
            _base() + f"/users/{_uuid(world['alice'])}/do-not-sell",
            headers=_headers(world["admin"]),
        )

        assert response.status_code == 200
        rows = _tenant_dns(db, tenant_id=ACME_ID, user_id=world["alice"])
        assert len(rows) == 1
        assert rows[0]["source_code"] == DNS_SOURCE_TENANT_ADMIN_DSAR
        assert rows[0]["recorded_by"] == world["admin"]
        # The user's own global consent record is never created or changed by an admin.
        assert db.dal(db.dal.cookie_consent.user_id == world["alice"]).count() == 0
        assert db.dal(db.dal.cookie_audit_log.user_id == world["alice"]).count() == 0
        assert [(r.action, r.details["outcome"]) for r in _audit_rows(db)] == [
            ("dsar.do_not_sell", "completed")
        ]

    async def test_existing_global_consent_row_is_left_untouched(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        prefs = {"necessary": True, "analytics": True, "marketing": True, "doNotSell": False}
        db.dal.cookie_consent.insert(
            user_id=world["alice"], consent_id="cid-1", preferences=prefs, consent_version="1.0"
        )
        db.dal.commit()

        response = await client.put(
            _base() + f"/users/{_uuid(world['alice'])}/do-not-sell",
            headers=_headers(world["admin"]),
        )

        assert response.status_code == 200
        row = db.dal(db.dal.cookie_consent.consent_id == "cid-1").select().first()
        assert row.preferences == prefs
        assert len(_tenant_dns(db, tenant_id=ACME_ID, user_id=world["alice"])) == 1

    async def test_is_idempotent_and_one_way(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        url = _base() + f"/users/{_uuid(world['alice'])}/do-not-sell"
        first = await client.put(url, headers=_headers(world["admin"]))
        second = await client.put(url, headers=_headers(world["admin"]))

        assert (await first.get_json())["result"]["status"] == "completed"
        assert (await second.get_json())["result"]["status"] == "already_done"
        assert len(_tenant_dns(db, tenant_id=ACME_ID, user_id=world["alice"])) == 1

    async def test_admin_opt_out_survives_a_later_self_service_preferences_update(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        """A later self-service consent edit never touches the tenant record."""
        from blueprints.v1.cookie_consent import cookie_consent_bp

        _global_dns(db, world["alice"], value=False)  # the user's own record, opted back in
        await client.put(
            _base() + f"/users/{_uuid(world['alice'])}/do-not-sell",
            headers=_headers(world["admin"]),
        )
        quart_app = Quart(__name__)
        QuartSchema(quart_app)
        quart_app.register_blueprint(cookie_consent_bp)
        quart_app.config["dal"] = db.dal
        quart_app.config["async_dal"] = db
        quart_app.config["HUB_API_CONFIG"] = None

        response = await quart_app.test_client().patch(
            "/api/v1/cookie/preferences",
            headers=_headers(world["alice"]),
            json={"preferences": {"functional": True, "analytics": True, "doNotSell": False}},
        )

        assert response.status_code == 200
        assert len(_tenant_dns(db, tenant_id=ACME_ID, user_id=world["alice"])) == 1
        assert await effective_do_not_sell(db, db.dal, tenant_id=ACME_ID, user_id=world["alice"])

    async def test_only_the_target_user_is_changed(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        bob = _user(db, name="bob")
        _member(db, community_id=world["acme_community"], user_id=bob)

        await client.put(
            _base() + f"/users/{_uuid(world['alice'])}/do-not-sell",
            headers=_headers(world["admin"]),
        )

        assert _tenant_dns(db, tenant_id=ACME_ID, user_id=bob) == []
        assert db.dal(db.dal.cookie_consent.user_id == bob).count() == 0

    async def test_tenant_a_admin_does_not_change_tenant_b_state(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        """regression: a tenant admin's DSAR Do-Not-Sell used to apply in EVERY tenant."""
        _member(db, community_id=world["other_community"], user_id=world["alice"])
        await client.put(
            _base() + f"/users/{_uuid(world['alice'])}/do-not-sell",
            headers=_headers(world["admin"]),
        )

        assert await effective_do_not_sell(db, db.dal, tenant_id=ACME_ID, user_id=world["alice"])
        assert not await effective_do_not_sell(
            db, db.dal, tenant_id=OTHER_ID, user_id=world["alice"]
        )
        assert _tenant_dns(db, tenant_id=OTHER_ID, user_id=world["alice"]) == []

    async def test_users_own_global_opt_out_still_applies_in_every_tenant(
        self, db: Any, world: dict[str, int]
    ) -> None:
        _global_dns(db, world["alice"])

        for tenant_id in (ACME_ID, OTHER_ID):
            assert await effective_do_not_sell(
                db, db.dal, tenant_id=tenant_id, user_id=world["alice"]
            )

    @pytest.mark.parametrize(
        ("global_pref", "tenant_a", "tenant_b", "expect_a", "expect_b"),
        [
            (False, False, False, False, False),
            (False, True, False, True, False),
            (False, False, True, False, True),
            (True, False, False, True, True),
            (True, True, False, True, True),
            (False, True, True, True, True),
        ],
    )
    async def test_effective_do_not_sell_truth_table(
        self,
        db: Any,
        world: dict[str, int],
        global_pref: bool,
        tenant_a: bool,
        tenant_b: bool,
        expect_a: bool,
        expect_b: bool,
    ) -> None:
        user = world["alice"]
        if global_pref:
            _global_dns(db, user)
        else:
            _global_dns(db, user, value=False)  # an explicit opt-back-in is not an opt-out
        if tenant_a:
            await svc.apply_do_not_sell(db, db.dal, tenant_id=ACME_ID, user_id=user)
        if tenant_b:
            await svc.apply_do_not_sell(db, db.dal, tenant_id=OTHER_ID, user_id=user)

        assert await effective_do_not_sell(db, db.dal, tenant_id=ACME_ID, user_id=user) is expect_a
        assert await effective_do_not_sell(db, db.dal, tenant_id=OTHER_ID, user_id=user) is expect_b

    async def test_already_done_ignores_global_preference_and_other_tenants(
        self, db: Any, world: dict[str, int]
    ) -> None:
        """Apply returns True for THIS tenant's first write despite opt-outs elsewhere."""
        user = world["alice"]
        _global_dns(db, user)
        assert await svc.apply_do_not_sell(db, db.dal, tenant_id=OTHER_ID, user_id=user) is True
        assert await svc.apply_do_not_sell(db, db.dal, tenant_id=ACME_ID, user_id=user) is True
        assert await svc.apply_do_not_sell(db, db.dal, tenant_id=ACME_ID, user_id=user) is False

    async def test_erasure_deletes_the_users_tenant_records(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        alice = world["alice"]
        await svc.apply_do_not_sell(db, db.dal, tenant_id=ACME_ID, user_id=alice)
        await svc.apply_do_not_sell(db, db.dal, tenant_id=OTHER_ID, user_id=alice)
        bob = _user(db, name="bob")
        _member(db, community_id=world["acme_community"], user_id=bob)
        await svc.apply_do_not_sell(db, db.dal, tenant_id=ACME_ID, user_id=bob, recorded_by=alice)

        response = await client.post(
            _base() + f"/users/{_uuid(alice)}/erase",
            headers=_headers(world["admin"]),
            json={"confirm": True},
        )

        assert response.status_code == 200
        assert db.dal(db.dal.tenant_do_not_sell.user_id == alice).count() == 0
        # a row recorded BY the erased user about someone else stays (pseudonymous actor ref)
        assert db.dal(db.dal.tenant_do_not_sell.user_id == bob).count() == 1

    async def test_concurrent_double_apply_records_once_and_reports_already_done(
        self, db: Any, world: dict[str, int]
    ) -> None:
        user = world["alice"]
        outcomes = await asyncio.gather(
            *[svc.apply_do_not_sell(db, db.dal, tenant_id=ACME_ID, user_id=user) for _ in range(8)]
        )

        assert sorted(outcomes) == [False] * 7 + [True]
        assert len(_tenant_dns(db, tenant_id=ACME_ID, user_id=user)) == 1

    async def test_insert_failure_propagates_and_rolls_back(
        self, db: Any, world: dict[str, int]
    ) -> None:
        def boom(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("db down")

        with (
            mock.patch.object(db.dal.tenant_do_not_sell, "insert", boom),
            pytest.raises(RuntimeError),
        ):
            await svc.apply_do_not_sell(db, db.dal, tenant_id=ACME_ID, user_id=world["alice"])
        assert _tenant_dns(db, tenant_id=ACME_ID, user_id=world["alice"]) == []


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
                "userIds": [
                    _uuid(world["alice"]),
                    _uuid(world["mallory"]),
                    _uuid(bob),
                    UNKNOWN_UUID,
                ],
            },
        )

        assert response.status_code == 200
        body = await response.get_json()
        statuses = {r["userId"]: r["status"] for r in body["results"]}
        assert statuses == {
            _uuid(world["alice"]): "completed",
            _uuid(world["mallory"]): "not_found",  # other tenant
            _uuid(bob): "completed",
            UNKNOWN_UUID: "not_found",
        }
        assert (body["requested"], body["succeeded"], body["failed"]) == (4, 2, 2)
        assert body["success"] is False
        assert db.dal(db.dal.cookie_consent.user_id == world["mallory"]).count() == 0
        rows = _audit_rows(db)
        assert len(rows) == 4  # every target audited, including the denied ones
        bulk_ids = {r.details["bulk_id"] for r in rows}
        assert len(bulk_ids) == 1 and None not in bulk_ids

    async def test_bulk_erase_removes_every_targets_chat_messages(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        bob = _user(db, name="bob")
        _member(db, community_id=world["acme_community"], user_id=bob)
        for uid in (world["alice"], bob):
            _chat_message(db, community_id=world["acme_community"], user_id=uid)
        bystander = _user(db, name="bystander")
        _member(db, community_id=world["acme_community"], user_id=bystander)
        _chat_message(db, community_id=world["acme_community"], user_id=bystander)

        response = await client.post(
            _base() + "/bulk",
            headers=_headers(world["admin"]),
            json={
                "action": "erase",
                "userIds": [_uuid(world["alice"]), _uuid(bob)],
                "confirm": True,
            },
        )

        assert response.status_code == 200
        assert (await response.get_json())["succeeded"] == 2
        assert (_chat_count(db, world["alice"]), _chat_count(db, bob)) == (0, 0)
        assert _chat_count(db, bystander) == 1

    async def test_bulk_erase_deletes_every_targets_memberships(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        bob = _user(db, name="bob-members")
        _member(db, community_id=world["acme_community"], user_id=bob)
        bystander = _user(db, name="bystander-members")
        _member(db, community_id=world["acme_community"], user_id=bystander)
        for uid in (world["alice"], bob, bystander):
            _fill_member_pii(db, user_id=uid)

        response = await client.post(
            _base() + "/bulk",
            headers=_headers(world["admin"]),
            json={
                "action": "erase",
                "userIds": [_uuid(world["alice"]), _uuid(bob)],
                "confirm": True,
            },
        )

        assert response.status_code == 200
        assert (await response.get_json())["succeeded"] == 2
        assert _member_pii_rows(db, user_id=world["alice"]) == []
        assert _member_pii_rows(db, user_id=bob) == []
        assert _member_pii_rows(db, user_id=bystander)[0][0] == "Sentinel Name"

    async def test_bulk_success_flag_and_dedup(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.post(
            _base() + "/bulk",
            headers=_headers(world["admin"]),
            json={
                "action": "do_not_sell",
                "userIds": [
                    _uuid(world["alice"]),
                    _uuid(world["alice"]).upper(),
                    f" {_uuid(world['alice'])} ",
                ],
            },
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
            json={
                "action": "erase",
                "userIds": [_uuid(world["alice"]), _uuid(bob)],
                "confirm": True,
            },
        )

        body = await response.get_json()
        statuses = {r["userId"]: r["status"] for r in body["results"]}
        assert statuses == {_uuid(world["alice"]): "failed", _uuid(bob): "completed"}
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
            json={"action": "export", "userIds": [_uuid(world["alice"]), _uuid(bob)]},
        )

        body = await response.get_json()
        emails = {r["data"]["account"][0]["email"] for r in body["results"]}
        assert emails == {"alice@example.com", "bob@example.com"}

    @pytest.mark.parametrize(
        ("payload", "needle"),
        [
            ({"action": "erase", "userIds": [UNKNOWN_UUID]}, "confirm"),
            ({"action": "do_not_sell", "userIds": []}, "non-empty"),
            ({"action": "do_not_sell", "userIds": _uuids(101)}, "At most 100"),
            ({"action": "export", "userIds": _uuids(26)}, "At most 25"),
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
            {"action": "drop_tables", "userIds": [UNKNOWN_UUID]},
            {"userIds": [UNKNOWN_UUID]},
            {"action": "export"},
            {"action": "export", "userIds": [1, 2]},  # numeric ids are no longer accepted
            {"action": "export", "userIds": [None]},
        ],
    )
    async def test_bulk_rejects_malformed_bodies(
        self, client: Any, world: dict[str, int], gate_on: Any, payload: dict[str, Any]
    ) -> None:
        response = await client.post(
            _base() + "/bulk", headers=_headers(world["admin"]), json=payload
        )
        assert response.status_code in (400, 422)

    @pytest.mark.parametrize(
        ("action", "extra"),
        [("erase", {"confirm": True}), ("do_not_sell", {})],
    )
    async def test_bulk_non_export_results_never_carry_an_export_payload(
        self,
        client: Any,
        db: Any,
        world: dict[str, int],
        gate_on: Any,
        action: str,
        extra: dict[str, Any],
    ) -> None:
        """Response-shape guard: only `export` may populate `data`/`incomplete`.

        `DsarResultDTO.data` holds the subject's raw personal rows (account
        records, emails) -- it is the point of an access-request export and
        the one PII-bearing field on this polymorphic endpoint's response.
        `bulk_dsar` branches its result shape on `action`; a regression that
        let `erase`/`do_not_sell` echo an export payload would leak that PII
        through the bulk console's response. This pins the non-export variants
        to an empty `data`/`incomplete` (asserted present-as-null OR absent, so
        it holds regardless of how None fields serialize), contrasting the
        `export`-carries-`data` path proven by
        `test_bulk_export_returns_each_users_data`.
        """
        bob = _user(db, name="bob")
        _member(db, community_id=world["acme_community"], user_id=bob)

        response = await client.post(
            _base() + "/bulk",
            headers=_headers(world["admin"]),
            json={"action": action, "userIds": [_uuid(world["alice"]), _uuid(bob)], **extra},
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["action"] == action
        results = body["results"]
        assert len(results) == 2, body
        for row in results:
            assert row.get("data") is None, (action, row)
            assert row.get("incomplete") is None, (action, row)


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
    @pytest.mark.parametrize("raw", ["abc", "0", "-1", "1.5", "\u0661\u0662", "9" * 19, "%20", "7"])
    async def test_malformed_or_numeric_user_id_is_the_uniform_404(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any, raw: str
    ) -> None:
        response = await client.get(
            _base() + f"/users/{raw}/export", headers=_headers(world["admin"])
        )
        unknown = await client.get(
            _base() + f"/users/{UNKNOWN_UUID}/export", headers=_headers(world["admin"])
        )

        assert response.status_code == unknown.status_code == 404
        body = await response.get_json()
        assert body["result"]["status"] == "not_found"
        assert body["result"]["detail"] == "User not found"

    async def test_numeric_internal_id_never_resolves_even_for_a_real_tenant_user(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        for suffix, method, kwargs in (
            ("export", "get", {}),
            ("erase", "post", {"json": {"confirm": True}}),
            ("do-not-sell", "put", {}),
        ):
            response = await getattr(client, method)(
                _base() + f"/users/{world['alice']}/{suffix}",
                headers=_headers(world["admin"]),
                **kwargs,
            )
            assert response.status_code == 404, suffix
        assert _user_row(db, world["alice"]).email == "alice@example.com"
        assert db.dal(db.dal.cookie_consent.user_id == world["alice"]).count() == 0

    async def test_malformed_id_is_404_even_with_the_bulk_gate_off(
        self, client: Any, world: dict[str, int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(admin_module, "feature_enabled", AsyncMock(return_value=False))

        response = await client.get(_base() + "/users/abc/export", headers=_headers(world["admin"]))

        assert response.status_code == 404


class TestUuidBoundary:
    """Numeric PKs never cross the boundary: indistinguishable misses, UUID-only responses."""

    async def test_unknown_and_foreign_single_routes_are_byte_identical(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        foreign = _uuid(world["mallory"])
        for method, suffix, kwargs in (
            ("get", "export", {}),
            ("post", "erase", {"json": {"confirm": True}}),
            ("put", "do-not-sell", {}),
        ):
            a = await getattr(client, method)(
                _base() + f"/users/{foreign}/{suffix}",
                headers=_headers(world["admin"]),
                **kwargs,
            )
            b = await getattr(client, method)(
                _base() + f"/users/{UNKNOWN_UUID}/{suffix}",
                headers=_headers(world["admin"]),
                **kwargs,
            )
            assert a.status_code == b.status_code == 404, suffix
            text_a = (await a.get_data(as_text=True)).replace(foreign, "<U>")
            text_b = (await b.get_data(as_text=True)).replace(UNKNOWN_UUID, "<U>")
            assert text_a == text_b, suffix
        # The foreign user was never touched.
        assert _user_row(db, world["mallory"]).email == "mallory@example.com"
        assert db.dal(db.dal.cookie_consent.user_id == world["mallory"]).count() == 0
        # Internal audit rows keep the true reason; the client cannot see them.
        reasons = {r.details["outcome"] for r in _audit_rows(db)}
        assert reasons == {"denied_not_in_tenant", "denied_unknown_user"}

    async def test_bulk_unknown_foreign_and_malformed_items_are_identical(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        foreign = _uuid(world["mallory"])
        response = await client.post(
            _base() + "/bulk",
            headers=_headers(world["admin"]),
            json={"action": "do_not_sell", "userIds": [foreign, UNKNOWN_UUID, "not-a-uuid", "12"]},
        )

        assert response.status_code == 200
        results = (await response.get_json())["results"]
        assert len(results) == 4
        assert {
            tuple(sorted((k, str(v)) for k, v in r.items() if k != "userId")) for r in results
        } == {
            (
                ("data", "None"),
                ("detail", "User not found"),
                ("incomplete", "None"),
                ("status", "not_found"),
            )
        }
        assert db.dal(db.dal.cookie_consent.user_id == world["mallory"]).count() == 0

    async def test_dto_field_sets_and_no_numeric_ids(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        alice = _uuid(world["alice"])
        single = await client.put(
            _base() + f"/users/{alice}/do-not-sell", headers=_headers(world["admin"])
        )
        body = await single.get_json()
        assert set(body) == {"success", "action", "result"}
        assert set(body["result"]) == {"userId", "status", "detail", "data", "incomplete"}
        assert body["result"]["userId"] == alice

        export = await client.get(
            _base() + f"/users/{alice}/export", headers=_headers(world["admin"])
        )
        ebody = await export.get_json()
        account = ebody["result"]["data"]["account"][0]
        assert "id" not in account and account["public_id"] == alice
        assert ebody["result"]["userId"] == alice
        assert str(world["alice"]) not in export.headers["Content-Disposition"]

        bulk = await client.post(
            _base() + "/bulk",
            headers=_headers(world["admin"]),
            json={"action": "do_not_sell", "userIds": [alice]},
        )
        bbody = await bulk.get_json()
        assert set(bbody) == {"success", "action", "requested", "succeeded", "failed", "results"}
        assert set(bbody["results"][0]) == {"userId", "status", "detail", "data", "incomplete"}
        assert bbody["results"][0]["userId"] == alice

    async def test_bulk_dedupes_on_canonical_form_and_audits_once(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        alice = _uuid(world["alice"])
        response = await client.post(
            _base() + "/bulk",
            headers=_headers(world["admin"]),
            json={"action": "do_not_sell", "userIds": [alice, alice.upper(), f"  {alice} "]},
        )
        body = await response.get_json()
        assert body["requested"] == 1 and [r["userId"] for r in body["results"]] == [alice]
        assert len(_audit_rows(db)) == 1

    async def test_cross_tenant_cannot_export_erase_or_opt_out_in_bulk(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        mallory = _uuid(world["mallory"])
        for action, extra in (("export", {}), ("erase", {"confirm": True}), ("do_not_sell", {})):
            response = await client.post(
                _base() + "/bulk",
                headers=_headers(world["admin"]),
                json={"action": action, "userIds": [mallory], **extra},
            )
            result = (await response.get_json())["results"][0]
            assert result["status"] == "not_found" and result["data"] is None, action
        assert _user_row(db, world["mallory"]).email == "mallory@example.com"


class TestOpenApiPaths:
    def test_single_user_routes_publish_their_full_paths(self, app: Quart) -> None:
        """quart-schema collapses a typed param after another; untyped `<user_uuid>` must not."""
        schema = app.extensions["QUART_SCHEMA"].openapi_provider.schema()

        paths = set(schema["paths"])
        base = "/api/v1/tenant/{tenant_slug}/privacy"
        assert f"{base}/users/{{user_uuid}}/export" in paths
        assert f"{base}/users/{{user_uuid}}/erase" in paths
        assert f"{base}/users/{{user_uuid}}/do-not-sell" in paths
        assert f"{base}/bulk" in paths
        assert not any(path.startswith("/api/v1/tenant/{user_uuid}") for path in paths)


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
        result = await svc._erase_one(db, db.dal, user_id=99999, user_uuid=UNKNOWN_UUID)

        assert result.status is svc.DsarStatus.NOT_FOUND


class TestAnonymizeCore:
    """`anonymize_user_data()` -- the erasure core shared with self-service.

    Failure/atomicity/retention proofs live in `test_data_privacy_erasure.py`.
    """

    async def test_account_without_an_email_is_still_anonymized(self, db: Any) -> None:
        user_id = _user(db, name="noemail")
        db.dal(db.dal.hub_users.id == user_id).update(email=None)
        db.dal.commit()

        await privacy.anonymize_user_data(db, db.dal, user_id=user_id, email=None)

        row = db.dal(db.dal.data_deletion_requests.hub_user_id == user_id).select().first()
        db.dal.commit()
        assert row.status == "completed"
        assert row.deletion_scope["temp_passwords"] == 0


class TestAdversarialReviewFixes:
    """Adversarial-review items 2, 3, 4, 5, 6 (area C)."""

    async def test_export_rows_carry_community_public_uuid_never_numeric_pk(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        _message_event(db, community_id=world["acme_community"], user_id=world["alice"])
        _chat_message(db, community_id=world["acme_community"], user_id=world["alice"])

        response = await client.get(
            _base() + f"/users/{_uuid(world['alice'])}/export", headers=_headers(world["admin"])
        )

        data = (await response.get_json())["result"]["data"]
        expected = _community_public_id(db, world["acme_community"])
        assert [r["community_id"] for r in data["message_activity"]] == [expected]
        assert [r["community_id"] for r in data["chat_messages"]] == [expected]
        assert "id" not in data["account"][0]

    async def test_do_not_sell_already_done_is_not_a_cross_tenant_oracle(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        """Another tenant's record (or the user's global choice) never shows up as already_done."""
        _member(db, community_id=world["other_community"], user_id=world["alice"])
        await svc.apply_do_not_sell(db, db.dal, tenant_id=OTHER_ID, user_id=world["alice"])
        _global_dns(db, world["alice"])
        url = _base() + f"/users/{_uuid(world['alice'])}/do-not-sell"
        first = await client.put(url, headers=_headers(world["admin"]))
        second = await client.put(url, headers=_headers(world["admin"]))

        assert (await first.get_json())["result"]["status"] == "completed"
        assert (await first.get_json())["result"]["detail"] is None
        assert (await second.get_json())["result"]["status"] == "already_done"

    async def test_do_not_sell_single_tenant_user_keeps_already_done(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        url = _base() + f"/users/{_uuid(world['alice'])}/do-not-sell"
        await client.put(url, headers=_headers(world["admin"]))
        second = await client.put(url, headers=_headers(world["admin"]))

        assert (await second.get_json())["result"]["status"] == "already_done"

    async def test_all_four_routes_identical_for_foreign_preopted_out_and_unknown(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        """Foreign user (already opted out) vs unknown UUID: same status+body on every route."""
        db.dal.cookie_consent.insert(
            user_id=world["mallory"],
            consent_id="cid-m",
            preferences={"necessary": True, "marketing": False, "doNotSell": True},
            consent_version="v1",
            consent_method="banner",
            consented_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.dal.commit()
        foreign = _uuid(world["mallory"])

        async def call(uid: str, route: str) -> tuple[int, str]:
            if route == "bulk":
                r = await client.post(
                    _base() + "/bulk",
                    headers=_headers(world["admin"]),
                    json={"action": "do_not_sell", "userIds": [uid]},
                )
                text = await r.get_data(as_text=True)
            else:
                method, suffix, kw = {
                    "export": ("get", "export", {}),
                    "erase": ("post", "erase", {"json": {"confirm": True}}),
                    "dns": ("put", "do-not-sell", {}),
                }[route]
                r = await getattr(client, method)(
                    _base() + f"/users/{uid}/{suffix}", headers=_headers(world["admin"]), **kw
                )
                text = await r.get_data(as_text=True)
            return r.status_code, text.replace(uid, "<U>")

        for route in ("export", "erase", "dns", "bulk"):
            assert await call(foreign, route) == await call(UNKNOWN_UUID, route), route

    async def test_bulk_cap_applies_to_raw_list_including_duplicates(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.post(
            _base() + "/bulk",
            headers=_headers(world["admin"]),
            json={"action": "do_not_sell", "userIds": [UNKNOWN_UUID] * (svc.MAX_BULK_USERS + 1)},
        )

        assert response.status_code == 400
        assert _audit_rows(db) == []

    async def test_oversize_post_body_is_413(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        response = await client.post(
            _base() + "/bulk",
            headers={**_headers(world["admin"]), "Content-Type": "application/json"},
            data=b"{" + b" " * 20000 + b"}",
        )

        assert response.status_code == 413

    def test_partial_status_is_a_distinct_non_success_value(self) -> None:
        assert svc.DsarStatus.PARTIAL.value == "partial"
        assert svc.DsarStatus.PARTIAL not in (svc.DsarStatus.COMPLETED, svc.DsarStatus.ALREADY_DONE)


class TestAdversarialReviewFixesAreaD:
    """Adversarial-review area D: consent fail-closed, export scope, failure audit, NUL, PK."""

    async def test_effective_binds_unbound_table_and_does_not_raise(self, tmp_path: Any) -> None:
        from services.cookie_consent_service import has_tenant_do_not_sell
        from services.schema import bind_privacy_tables

        fresh = AsyncDAL(f"sqlite://{tmp_path / 'fresh3.db'}", pool_size=1)
        try:
            bind_privacy_tables(fresh.dal, migrate=True)
            assert "tenant_do_not_sell" not in fresh.dal.tables
            # Production schema comes from alembic 0125; stand in for it on sqlite.
            fresh.dal.executesql(
                "CREATE TABLE tenant_do_not_sell (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " public_id VARCHAR(36), tenant_id INTEGER, user_id INTEGER,"
                " source_code INTEGER, recorded_by INTEGER, created_at TIMESTAMP)"
            )
            fresh.dal.commit()
            assert await has_tenant_do_not_sell(fresh, fresh.dal, tenant_id=1, user_id=5) is False
            assert "tenant_do_not_sell" in fresh.dal.tables
        finally:
            fresh.dal.close()

    @pytest.mark.parametrize("failing", ["has_global_do_not_sell", "has_tenant_do_not_sell"])
    async def test_effective_do_not_sell_fails_closed_on_lookup_error(
        self,
        db: Any,
        world: dict[str, int],
        caplog: pytest.LogCaptureFixture,
        failing: str,
    ) -> None:
        import services.cookie_consent_service as ccs

        async def boom(*a: Any, **kw: Any) -> bool:
            raise ConnectionError("secret-dsn-detail")

        with (
            mock.patch.object(ccs, failing, boom),
            caplog.at_level(logging.ERROR, logger=ccs.__name__),
        ):
            assert await effective_do_not_sell(
                db, db.dal, tenant_id=ACME_ID, user_id=world["alice"]
            )
        record = next(r for r in caplog.records if r.levelno == logging.ERROR)
        assert record.error_type == "ConnectionError"  # type: ignore[attr-defined]
        assert "secret-dsn-detail" not in caplog.text and record.exc_info is None

    async def test_export_shows_only_this_tenants_do_not_sell_record(
        self, client: Any, db: Any, world: dict[str, int], gate_on: Any
    ) -> None:
        alice = world["alice"]
        await svc.apply_do_not_sell(db, db.dal, tenant_id=OTHER_ID, user_id=alice)

        def export() -> Any:
            return client.get(
                _base() + f"/users/{_uuid(alice)}/export", headers=_headers(world["admin"])
            )

        body = await (await export()).get_json()
        assert body["result"]["data"]["tenant_do_not_sell"] == []  # tenant B's record never shows

        await svc.apply_do_not_sell(db, db.dal, tenant_id=ACME_ID, user_id=alice)
        rows = (await (await export()).get_json())["result"]["data"]["tenant_do_not_sell"]
        assert len(rows) == 1
        assert set(rows[0]) == {"public_id", "source_code", "created_at"}  # no numeric ids

    async def test_failed_item_audited_and_bulk_loop_continues(
        self, db: Any, world: dict[str, int]
    ) -> None:
        bob = _user(db, name="bob")
        _member(db, community_id=world["acme_community"], user_id=bob)
        actor = svc.DsarActor(user_id=world["admin"], tenant_id=ACME_ID, tenant_slug=TENANT_SLUG)
        real = svc.apply_do_not_sell
        calls = {"n": 0}

        async def flaky(*a: Any, **kw: Any) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("txn aborted")
            return await real(*a, **kw)

        with mock.patch.object(svc, "apply_do_not_sell", flaky):
            results = await svc.run_dsar_by_uuid(
                db,
                db.dal,
                actor=actor,
                action=svc.DsarAction.DO_NOT_SELL,
                user_uuids=[_uuid(world["alice"]), _uuid(bob)],
            )

        assert [r.status.value for r in results] == ["failed", "completed"]
        outcomes = [dict(r.details or {}).get("outcome") for r in _audit_rows(db)]
        assert outcomes == ["failed", "completed"]

    async def test_nul_bytes_never_reach_audit_target_or_result(
        self, db: Any, world: dict[str, int]
    ) -> None:
        actor = svc.DsarActor(user_id=world["admin"], tenant_id=ACME_ID, tenant_slug=TENANT_SLUG)
        results = await svc.run_dsar_by_uuid(
            db,
            db.dal,
            actor=actor,
            action=svc.DsarAction.EXPORT,
            user_uuids=["abc\x00def"],
        )

        assert results[0].status is svc.DsarStatus.NOT_FOUND
        assert "\x00" not in results[0].user_uuid
        audit = _audit_rows(db)
        assert len(audit) == 1 and "\x00" not in audit[0].target_id
        assert audit[0].target_id == "abcdef"

    def test_dsar_result_cannot_carry_a_numeric_user_pk(self) -> None:
        import dataclasses

        assert "user_id" not in {f.name for f in dataclasses.fields(svc.DsarResult)}
