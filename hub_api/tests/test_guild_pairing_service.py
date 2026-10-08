"""`services/guild_pairing.py` -- direct service-layer tests (Bar Citizen foundation).

Uses `bar_citizen_db` (real `bind_bar_citizen_tables()`, migration-0034
field list, `migrate=True` sqlite) rather than a hand-duplicated `Field`
list -- same rationale as `test_community_connections_service.py`'s
`connections_db` fixture.
"""

from __future__ import annotations

from typing import Any

import pytest

from services import guild_pairing as svc
from services.errors import ApiError


class TestCreateAndListPairings:
    def test_create_pairing_defaults_opt_out(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = svc.create_pairing(
            dal,
            community_id,
            discord_guild_id="123456789012345678",  # gitleaks:allow - fake snowflake, not a secret
            direction="bidirectional",
            role_name_prefix="[BCSEA]",
            actor_user_id=7,
        )
        assert pairing.sync_enabled is False
        assert pairing.direction == "bidirectional"
        assert pairing.role_name_prefix == "[BCSEA]"
        assert pairing.created_by_user_id == 7

    def test_list_returns_only_this_communitys_pairings(self, bar_citizen_db: Any) -> None:
        dal, community_id, tenant_id, _global_id = bar_citizen_db
        other_community_id = dal.communities.insert(name="other", tenant_id=tenant_id)
        dal.commit()

        svc.create_pairing(
            dal,
            community_id,
            discord_guild_id="111",
            direction="discord_to_twitch",
            role_name_prefix="[A]",
            actor_user_id=None,
        )
        svc.create_pairing(
            dal,
            other_community_id,
            discord_guild_id="222",
            direction="discord_to_twitch",
            role_name_prefix="[B]",
            actor_user_id=None,
        )

        result = svc.list_pairings(dal, community_id)
        assert [p.discord_guild_id for p in result] == ["111"]

    def test_duplicate_guild_pairing_is_conflict(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        svc.create_pairing(
            dal,
            community_id,
            discord_guild_id="999",
            direction="bidirectional",
            role_name_prefix="[X]",
            actor_user_id=None,
        )
        with pytest.raises(ApiError) as excinfo:
            svc.create_pairing(
                dal,
                community_id,
                discord_guild_id="999",
                direction="bidirectional",
                role_name_prefix="[Y]",
                actor_user_id=None,
            )
        assert excinfo.value.status_code == 409

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("discord_guild_id", "not-numeric"),
            ("discord_guild_id", ""),
            ("direction", "sideways"),
            ("role_name_prefix", ""),
            ("role_name_prefix", "x" * 51),
        ],
    )
    def test_invalid_input_is_bad_request(
        self, bar_citizen_db: Any, field: str, value: str
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        kwargs: dict[str, Any] = {
            "discord_guild_id": "123",
            "direction": "bidirectional",
            "role_name_prefix": "[X]",
            "actor_user_id": None,
        }
        kwargs[field] = value
        with pytest.raises(ApiError) as excinfo:
            svc.create_pairing(dal, community_id, **kwargs)
        assert excinfo.value.status_code == 400


class TestUpdateAndDeletePairing:
    def test_update_toggles_sync_enabled(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = svc.create_pairing(
            dal,
            community_id,
            discord_guild_id="555",
            direction="bidirectional",
            role_name_prefix="[Z]",
            actor_user_id=None,
        )
        updated = svc.update_pairing(dal, community_id, pairing.id, sync_enabled=True)
        assert updated.sync_enabled is True
        assert updated.direction == "bidirectional"

    def test_update_unknown_pairing_is_not_found(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        with pytest.raises(ApiError) as excinfo:
            svc.update_pairing(dal, community_id, 9999, sync_enabled=True)
        assert excinfo.value.status_code == 404

    def test_delete_pairing_cascades_bindings(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = svc.create_pairing(
            dal,
            community_id,
            discord_guild_id="777",
            direction="twitch_to_discord",
            role_name_prefix="[C]",
            actor_user_id=None,
        )
        svc.create_binding(
            dal, community_id, pairing.id, sync_scope="moderator", discord_role_id="42"
        )
        assert svc.delete_pairing(dal, community_id, pairing.id) is True
        assert svc.get_pairing(dal, community_id, pairing.id) is None
        with pytest.raises(ApiError):
            svc.list_bindings(dal, community_id, pairing.id)

    def test_delete_unknown_pairing_returns_false(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        assert svc.delete_pairing(dal, community_id, 9999) is False


class TestRoleSyncBindings:
    @pytest.fixture
    def pairing_id(self, bar_citizen_db: Any) -> int:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = svc.create_pairing(
            dal,
            community_id,
            discord_guild_id="333",
            direction="bidirectional",
            role_name_prefix="[P]",
            actor_user_id=None,
        )
        return pairing.id

    def test_create_subscriber_tier_binding(self, bar_citizen_db: Any, pairing_id: int) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        binding = svc.create_binding(
            dal,
            community_id,
            pairing_id,
            sync_scope="subscriber_tier",
            subscriber_tier=2,
            discord_role_id="888",
        )
        assert binding.subscriber_tier == 2
        assert binding.sync_scope == "subscriber_tier"

    def test_subscriber_tier_requires_tier_value(
        self, bar_citizen_db: Any, pairing_id: int
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        with pytest.raises(ApiError) as excinfo:
            svc.create_binding(
                dal, community_id, pairing_id, sync_scope="subscriber_tier", discord_role_id="1"
            )
        assert excinfo.value.status_code == 400

    def test_moderator_rejects_tier_value(self, bar_citizen_db: Any, pairing_id: int) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        with pytest.raises(ApiError) as excinfo:
            svc.create_binding(
                dal,
                community_id,
                pairing_id,
                sync_scope="moderator",
                subscriber_tier=1,
                discord_role_id="1",
            )
        assert excinfo.value.status_code == 400

    def test_duplicate_tier_binding_is_conflict(self, bar_citizen_db: Any, pairing_id: int) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        svc.create_binding(
            dal,
            community_id,
            pairing_id,
            sync_scope="subscriber_tier",
            subscriber_tier=1,
            discord_role_id="1",
        )
        with pytest.raises(ApiError) as excinfo:
            svc.create_binding(
                dal,
                community_id,
                pairing_id,
                sync_scope="subscriber_tier",
                subscriber_tier=1,
                discord_role_id="2",
            )
        assert excinfo.value.status_code == 409

    def test_duplicate_moderator_binding_is_conflict(
        self, bar_citizen_db: Any, pairing_id: int
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        svc.create_binding(
            dal, community_id, pairing_id, sync_scope="moderator", discord_role_id="1"
        )
        with pytest.raises(ApiError) as excinfo:
            svc.create_binding(
                dal, community_id, pairing_id, sync_scope="moderator", discord_role_id="2"
            )
        assert excinfo.value.status_code == 409

    def test_different_tiers_coexist(self, bar_citizen_db: Any, pairing_id: int) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        svc.create_binding(
            dal,
            community_id,
            pairing_id,
            sync_scope="subscriber_tier",
            subscriber_tier=1,
            discord_role_id="1",
        )
        svc.create_binding(
            dal,
            community_id,
            pairing_id,
            sync_scope="subscriber_tier",
            subscriber_tier=2,
            discord_role_id="2",
        )
        result = svc.list_bindings(dal, community_id, pairing_id)
        assert {b.subscriber_tier for b in result} == {1, 2}

    def test_list_bindings_unknown_pairing_is_not_found(self, bar_citizen_db: Any) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        with pytest.raises(ApiError) as excinfo:
            svc.list_bindings(dal, community_id, 9999)
        assert excinfo.value.status_code == 404

    def test_delete_binding(self, bar_citizen_db: Any, pairing_id: int) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        binding = svc.create_binding(
            dal, community_id, pairing_id, sync_scope="moderator", discord_role_id="1"
        )
        assert svc.delete_binding(dal, community_id, pairing_id, binding.id) is True
        assert svc.list_bindings(dal, community_id, pairing_id) == []

    def test_delete_unknown_binding_returns_false(
        self, bar_citizen_db: Any, pairing_id: int
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        assert svc.delete_binding(dal, community_id, pairing_id, 9999) is False


class TestCommunityRoleBindings:
    """`sync_scope="community_role"` (migration 0036) -- the Discord -> platform direction."""

    @pytest.fixture
    def pairing_id(self, bar_citizen_db: Any) -> int:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        pairing = svc.create_pairing(
            dal,
            community_id,
            discord_guild_id="334",
            direction="discord_to_twitch",
            role_name_prefix="[CR]",
            actor_user_id=None,
        )
        return pairing.id

    def test_create_community_role_binding(self, bar_citizen_db: Any, pairing_id: int) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        binding = svc.create_binding(
            dal,
            community_id,
            pairing_id,
            sync_scope="community_role",
            discord_role_id="901",
            community_role="moderator",
        )
        assert binding.sync_scope == "community_role"
        assert binding.community_role == "moderator"
        assert binding.subscriber_tier is None

    def test_community_role_requires_role_value(self, bar_citizen_db: Any, pairing_id: int) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        with pytest.raises(ApiError) as excinfo:
            svc.create_binding(
                dal, community_id, pairing_id, sync_scope="community_role", discord_role_id="1"
            )
        assert excinfo.value.status_code == 400

    def test_community_role_rejects_unknown_role_name(
        self, bar_citizen_db: Any, pairing_id: int
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        with pytest.raises(ApiError) as excinfo:
            svc.create_binding(
                dal,
                community_id,
                pairing_id,
                sync_scope="community_role",
                discord_role_id="1",
                community_role="community-owner",
            )
        assert excinfo.value.status_code == 400

    def test_community_role_rejects_tier_value(self, bar_citizen_db: Any, pairing_id: int) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        with pytest.raises(ApiError) as excinfo:
            svc.create_binding(
                dal,
                community_id,
                pairing_id,
                sync_scope="community_role",
                discord_role_id="1",
                community_role="member",
                subscriber_tier=1,
            )
        assert excinfo.value.status_code == 400

    def test_distinct_discord_roles_may_map_to_same_or_different_community_roles(
        self, bar_citizen_db: Any, pairing_id: int
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        svc.create_binding(
            dal,
            community_id,
            pairing_id,
            sync_scope="community_role",
            discord_role_id="1",
            community_role="member",
        )
        svc.create_binding(
            dal,
            community_id,
            pairing_id,
            sync_scope="community_role",
            discord_role_id="2",
            community_role="community-admin",
        )
        result = svc.list_bindings(dal, community_id, pairing_id)
        assert {(b.discord_role_id, b.community_role) for b in result} == {
            ("1", "member"),
            ("2", "community-admin"),
        }

    def test_duplicate_discord_role_as_community_role_is_conflict(
        self, bar_citizen_db: Any, pairing_id: int
    ) -> None:
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        svc.create_binding(
            dal,
            community_id,
            pairing_id,
            sync_scope="community_role",
            discord_role_id="1",
            community_role="member",
        )
        with pytest.raises(ApiError) as excinfo:
            svc.create_binding(
                dal,
                community_id,
                pairing_id,
                sync_scope="community_role",
                discord_role_id="1",
                community_role="vip",
            )
        assert excinfo.value.status_code == 409

    def test_same_discord_role_cannot_be_bound_both_directions(
        self, bar_citizen_db: Any, pairing_id: int
    ) -> None:
        """Structural loop-prevention (migration 0036): one role, one direction, per pairing."""
        dal, community_id, _tenant_id, _global_id = bar_citizen_db
        svc.create_binding(
            dal, community_id, pairing_id, sync_scope="moderator", discord_role_id="999"
        )
        with pytest.raises(ApiError) as excinfo:
            svc.create_binding(
                dal,
                community_id,
                pairing_id,
                sync_scope="community_role",
                discord_role_id="999",
                community_role="member",
            )
        assert excinfo.value.status_code == 409

        with pytest.raises(ApiError) as excinfo2:
            svc.create_binding(
                dal,
                community_id,
                pairing_id,
                sync_scope="moderator",
                discord_role_id="999",
            )
        assert excinfo2.value.status_code == 409
