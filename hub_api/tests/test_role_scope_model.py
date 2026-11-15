"""Roles as data-defined scope groups: registry, role-kind guards and the role CRUD endpoints.

Rules under test (owner model, 2026-11-15): admin/moderator can't be deleted; admin scopes are
computed (never editable, new scopes included); moderator scopes are editable by an authorized admin
holding `roles:manage`, never beyond the editor's own scopes; custom roles get full CRUD under
the same guard; nothing authorizes on a role NAME.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.community_interaction import interaction_admin_bp
from services import scope_registry as reg
from services.community_authz import member_scopes_sync
from services.errors import ApiError

MODERATOR_SCOPES = ["channels:moderate", "channels:read", "community:read"]


@pytest.fixture
def app(community_db: Any) -> Quart:
    """Quart app with only the interaction admin blueprint."""
    dal, _ = community_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(interaction_admin_bp)
    quart_app.config["dal"] = dal
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    """Test client."""
    return app.test_client()


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the interactions feature flag ON (role routes do not gate on it, listing does)."""
    from unittest.mock import AsyncMock

    import blueprints.v1.community_interaction as module

    monkeypatch.setattr(module, "feature_enabled", AsyncMock(return_value=True))


@pytest.fixture
def roles(community_db: Any) -> dict[str, int]:
    """Seed member/moderator/admin(+owner) predefined roles and one custom role; return ids."""
    dal, community_id = community_db
    ids = {}
    for name, kind, scopes in (
        ("member", 3, ["community:read"]),
        ("moderator", 2, MODERATOR_SCOPES),
        ("community-admin", 1, ["community:read"]),  # stale stored list: must be ignored
        ("community-owner", 1, []),
    ):
        ids[name] = dal.community_roles.insert(
            community_id=community_id,
            name=name,
            is_system=True,
            role_kind=kind,
            priority=10,
            base_claims={"scopes": scopes},
        )
    ids["vip"] = dal.community_roles.insert(
        community_id=community_id,
        name="vip",
        is_system=False,
        role_kind=0,
        priority=5,
        base_claims={"scopes": ["channels:read"]},
    )
    dal.commit()
    return ids


def _admin(auth_headers: Any) -> dict[str, str]:
    return auth_headers(scope="users:admin", user_id="1")


def _limited(auth_headers: Any, scope: str) -> dict[str, str]:
    return auth_headers(scope=scope, user_id="2")


async def _put(client: Any, path: str, headers: dict[str, str], body: dict[str, Any]) -> Any:
    return await client.put(
        path, headers={**headers, "Content-Type": "application/json"}, data=json.dumps(body)
    )


def _url(community_id: int, role_id: int | None = None) -> str:
    base = f"/api/v1/admin/{community_id}/interaction/roles"
    return base if role_id is None else f"{base}/{role_id}"


class TestRegistry:
    def test_admin_scopes_are_the_full_registry(self) -> None:
        assert reg.effective_scopes(reg.KIND_ADMIN, ["x:y"]) == reg.all_scopes()

    def test_new_scope_reaches_admin_automatically(self) -> None:
        reg.register_scopes({"rooms:create": "Create rooms"})
        assert "rooms:create" in reg.effective_scopes(reg.KIND_ADMIN, [])
        assert "rooms:create" not in reg.effective_scopes(reg.KIND_MODERATOR, MODERATOR_SCOPES)

    def test_register_is_idempotent_and_keeps_description(self) -> None:
        reg.register_scopes({"rooms:kick": "Kick participants"})
        reg.register_scopes(["rooms:kick"])
        assert reg.describe_scopes()["rooms:kick"] == "Kick participants"

    @pytest.mark.parametrize("bad", ["", "nocolon", "UPPER:case", "a b:c", "rooms:"])
    def test_invalid_scope_name_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            reg.register_scopes([bad])

    def test_normalize_rejects_unknown_and_non_list(self) -> None:
        with pytest.raises(ApiError) as exc:
            reg.normalize_scopes(["not:registered"])
        assert exc.value.code == "UNKNOWN_SCOPE"
        with pytest.raises(ApiError):
            reg.normalize_scopes("channels:read")

    @pytest.mark.parametrize("kind", [1, 2, 3])
    def test_guard_delete_refuses_predefined(self, kind: int) -> None:
        with pytest.raises(ApiError) as exc:
            reg.guard_delete(kind)
        assert exc.value.status_code == 409

    def test_guard_delete_allows_custom(self) -> None:
        reg.guard_delete(reg.KIND_CUSTOM)


class TestDeleteProtection:
    @pytest.mark.parametrize("name", ["community-admin", "community-owner", "moderator", "member"])
    async def test_predefined_roles_cannot_be_deleted(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int], name: str
    ) -> None:
        _, community_id = community_db
        response = await client.delete(
            _url(community_id, roles[name]), headers=_admin(auth_headers)
        )
        assert response.status_code == 409
        assert (await response.get_json())["code"] == "ROLE_PROTECTED"
        dal, _ = community_db
        assert dal.community_roles[roles[name]] is not None

    async def test_custom_role_can_be_deleted(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int]
    ) -> None:
        _, community_id = community_db
        response = await client.delete(
            _url(community_id, roles["vip"]), headers=_admin(auth_headers)
        )
        assert response.status_code == 200


class TestAdminLocked:
    async def test_admin_scopes_cannot_be_edited(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int]
    ) -> None:
        _, community_id = community_db
        response = await _put(
            client,
            _url(community_id, roles["community-admin"]),
            _admin(auth_headers),
            {"scopes": ["community:read"]},
        )
        assert response.status_code == 403
        assert (await response.get_json())["code"] == "ROLE_LOCKED"

    async def test_listing_shows_computed_full_set_and_flags(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int]
    ) -> None:
        _, community_id = community_db
        reg.register_scopes({"rooms:close": "Close rooms"})
        response = await client.get(
            _url(community_id),
            headers=auth_headers(scope="community.interaction:read users:admin", user_id="1"),
        )
        assert response.status_code == 200
        by_name = {r["name"]: r for r in (await response.get_json())["roles"]}
        admin = by_name["community-admin"]
        assert set(admin["scopes"]) == reg.all_scopes()
        assert "rooms:close" in admin["scopes"]
        assert (admin["scopes_editable"], admin["deletable"]) == (False, False)
        mod = by_name["moderator"]
        assert (mod["scopes_editable"], mod["deletable"]) == (True, False)
        assert (by_name["vip"]["scopes_editable"], by_name["vip"]["deletable"]) == (True, True)
        assert by_name["member"]["scopes_editable"] is False

    async def test_fixed_predefined_scopes_locked(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int]
    ) -> None:
        _, community_id = community_db
        response = await _put(
            client,
            _url(community_id, roles["member"]),
            _admin(auth_headers),
            {"scopes": ["channels:read"]},
        )
        assert response.status_code == 403


class TestModeratorEdit:
    async def test_admin_can_edit_moderator_scopes_and_cache_is_cleared(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int]
    ) -> None:
        dal, community_id = community_db
        response = await _put(
            client,
            _url(community_id, roles["moderator"]),
            _admin(auth_headers),
            {"scopes": ["channels:read", "resource:pin"]},
        )
        assert response.status_code == 200
        stored = dal.community_roles[roles["moderator"]].base_claims["scopes"]
        assert stored == ["channels:read", "resource:pin"]

    async def test_editor_without_roles_manage_is_403(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int]
    ) -> None:
        _, community_id = community_db
        response = await _put(
            client,
            _url(community_id, roles["moderator"]),
            _limited(auth_headers, "channels:read"),
            {"scopes": ["channels:read"]},
        )
        assert response.status_code == 403

    async def test_cannot_grant_scope_editor_does_not_hold(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int]
    ) -> None:
        dal, community_id = community_db
        response = await _put(
            client,
            _url(community_id, roles["moderator"]),
            _limited(auth_headers, "roles:manage channels:read"),
            {"scopes": [*MODERATOR_SCOPES, "resource:pin"]},
        )
        assert response.status_code == 403
        assert (await response.get_json())["code"] == "SCOPE_ESCALATION"
        assert dal.community_roles[roles["moderator"]].base_claims["scopes"] == MODERATOR_SCOPES

    async def test_editor_must_hold_every_current_scope_to_edit(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int]
    ) -> None:
        """No downgrading a role more powerful than yourself, even just removing scopes."""
        dal, community_id = community_db
        response = await _put(
            client,
            _url(community_id, roles["moderator"]),
            _limited(auth_headers, "roles:manage channels:read community:read"),
            {"scopes": ["channels:read", "community:read"]},
        )
        assert response.status_code == 403
        assert (await response.get_json())["code"] == "SCOPE_ESCALATION"
        assert dal.community_roles[roles["moderator"]].base_claims["scopes"] == MODERATOR_SCOPES

    async def test_editor_holding_all_current_scopes_may_remove_some(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int]
    ) -> None:
        dal, community_id = community_db
        response = await _put(
            client,
            _url(community_id, roles["moderator"]),
            _limited(auth_headers, "roles:manage " + " ".join(MODERATOR_SCOPES)),
            {"scopes": ["channels:read", "community:read"]},
        )
        assert response.status_code == 200
        assert dal.community_roles[roles["moderator"]].base_claims["scopes"] == [
            "channels:read",
            "community:read",
        ]

    async def test_unknown_scope_is_400(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int]
    ) -> None:
        _, community_id = community_db
        response = await _put(
            client,
            _url(community_id, roles["moderator"]),
            _admin(auth_headers),
            {"scopes": ["bogus:scope"]},
        )
        assert response.status_code == 400


class TestCustomRoleCrud:
    async def test_create_requires_roles_manage(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int]
    ) -> None:
        _, community_id = community_db
        response = await client.post(
            _url(community_id),
            headers={**_limited(auth_headers, "channels:read"), "Content-Type": "application/json"},
            data=json.dumps({"name": "x"}),
        )
        assert response.status_code == 403

    async def test_create_cannot_exceed_editor_scopes(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int]
    ) -> None:
        _, community_id = community_db
        response = await client.post(
            _url(community_id),
            headers={
                **_limited(auth_headers, "roles:manage channels:read"),
                "Content-Type": "application/json",
            },
            data=json.dumps({"name": "sneaky", "scopes": ["channels:moderate"]}),
        )
        assert response.status_code == 403
        assert (await response.get_json())["code"] == "SCOPE_ESCALATION"

    async def test_create_update_delete_round_trip(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int]
    ) -> None:
        _, community_id = community_db
        headers = _limited(auth_headers, "roles:manage channels:read channels:send_chat")
        created = await client.post(
            _url(community_id),
            headers={**headers, "Content-Type": "application/json"},
            data=json.dumps({"name": "Helper", "scopes": ["channels:read"]}),
        )
        assert created.status_code == 201
        role_id = (await created.get_json())["role"]["id"]
        updated = await _put(
            client, _url(community_id, role_id), headers, {"scopes": ["channels:send_chat"]}
        )
        assert updated.status_code == 200
        deleted = await client.delete(_url(community_id, role_id), headers=headers)
        assert deleted.status_code == 200


class TestNoDeletingStrongerRole:
    async def test_cannot_delete_custom_role_holding_scopes_editor_lacks(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int]
    ) -> None:
        dal, community_id = community_db
        strong = dal.community_roles.insert(
            community_id=community_id,
            name="strong",
            is_system=False,
            role_kind=0,
            base_claims={"scopes": ["channels:moderate", "channels:read"]},
        )
        dal.commit()
        response = await client.delete(
            _url(community_id, strong), headers=_limited(auth_headers, "roles:manage channels:read")
        )
        assert response.status_code == 403
        assert (await response.get_json())["code"] == "SCOPE_ESCALATION"
        assert dal.community_roles[strong] is not None

    async def test_can_delete_custom_role_whose_scopes_editor_holds(
        self, client: Any, auth_headers: Any, community_db: Any, roles: dict[str, int]
    ) -> None:
        _, community_id = community_db
        response = await client.delete(
            _url(community_id, roles["vip"]),
            headers=_limited(auth_headers, "roles:manage channels:read"),
        )
        assert response.status_code == 200


class TestDedicatedScopes:
    def test_per_action_scopes_registered_and_admin_only(self) -> None:
        for scope in (
            reg.SCOPE_GITHUB_MANAGE,
            reg.SCOPE_COMMUNITY_ADMIN,
            reg.SCOPE_PROFILE_MANAGE,
            reg.SCOPE_BRANDING_MANAGE,
            reg.SCOPE_BILLING_MANAGE,
            reg.SCOPE_MEMBERS_MANAGE,
            reg.SCOPE_WORKFLOW_MANAGE,
            reg.SCOPE_BYOK_MANAGE,
            reg.SCOPE_AI_ROUTING_MANAGE,
            reg.SCOPE_OVERLAY_MANAGE,
            reg.SCOPE_CALLS_MANAGE,
        ):
            assert scope in reg.all_scopes()
            assert scope in reg.effective_scopes(reg.KIND_ADMIN, [])
            assert scope not in reg.effective_scopes(reg.KIND_MODERATOR, MODERATOR_SCOPES)

    def test_moderator_scopes_do_not_satisfy_community_admin_gates(
        self, community_db: Any, roles: dict[str, int]
    ) -> None:
        dal, community_id = community_db
        dal.community_members.insert(
            community_id=community_id,
            user_id="51",
            role="moderator",
            community_role_id=roles["moderator"],
            is_active=True,
        )
        dal.commit()
        held = member_scopes_sync(dal, community_id, 51)
        assert held and reg.SCOPE_GITHUB_MANAGE not in held
        assert reg.SCOPE_COMMUNITY_ADMIN not in held
        assert reg.SCOPE_PROFILE_MANAGE not in held


class TestScopeEndpointAndSync:
    async def test_scope_registry_endpoint(
        self, client: Any, auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/admin/{community_id}/interaction/scopes",
            headers=auth_headers(scope="community.interaction:read", user_id="1"),
        )
        assert response.status_code == 200
        assert "roles:manage" in (await response.get_json())["scopes"]

    def test_member_scopes_sync_admin_kind_is_computed(
        self, community_db: Any, roles: dict[str, int]
    ) -> None:
        dal, community_id = community_db
        dal.community_members.insert(
            community_id=community_id,
            user_id="42",
            role="community-admin",
            community_role_id=roles["community-admin"],
            is_active=True,
            claims_cache=["community:read"],
        )
        dal.commit()
        assert member_scopes_sync(dal, community_id, 42) == reg.all_scopes()

    def test_member_scopes_sync_role_name_without_role_row_is_empty(
        self, community_db: Any
    ) -> None:
        dal, community_id = community_db
        dal.community_members.insert(
            community_id=community_id, user_id="43", role="community-owner", is_active=True
        )
        dal.commit()
        assert member_scopes_sync(dal, community_id, 43) == frozenset()


class TestNoRoleNameAuthorization:
    """Static guard: the role-name authorization tables this change removed must stay gone."""

    @pytest.mark.parametrize(
        "needle",
        [
            "_COMMUNITY_ADMIN_ROLES",
            "ADMIN_PROMOTION_ROLES",
            "_BYPASS_ROLES",
            '"platform-admin" in',
            "role.belongs([",
            "_ADMIN_ROLE_NAMES",
            '"community-owner", "community-admin")',
        ],
    )
    def test_removed_constructs_stay_removed(self, needle: str) -> None:
        root = Path(__file__).resolve().parent.parent
        scanned = 0
        for path in list((root / "services").glob("*.py")) + list(
            (root / "blueprints").rglob("*.py")
        ):
            scanned += 1
            assert needle not in path.read_text(encoding="utf-8"), f"{needle} in {path}"
        assert scanned > 50
