"""Room scopes live in the predefined role (scope-bundle) DEFINITIONS, never in role-name checks."""

from __future__ import annotations

from flask_core.auth import SCOPE_BUNDLES

MANAGEMENT = ("rooms:create", "rooms:update", "rooms:close", "rooms:guests")
MODERATION = ("rooms:kick", "rooms:mute", "rooms:lock", "rooms:hands")


def test_community_admin_bundle_holds_every_room_scope() -> None:
    """The admin group carries all room-management and moderation scopes."""
    admin = SCOPE_BUNDLES["community"]["admin"]
    for scope in (*MANAGEMENT, *MODERATION):
        assert scope in admin, scope


def test_community_maintainer_bundle_holds_exactly_the_moderation_scopes() -> None:
    """The moderator group keeps what moderators do today and gains no management scope."""
    maintainer = SCOPE_BUNDLES["community"]["maintainer"]
    for scope in MODERATION:
        assert scope in maintainer, scope
    for scope in MANAGEMENT:
        assert scope not in maintainer, f"maintainer must not hold {scope}"


def test_viewer_and_other_levels_hold_no_room_scope() -> None:
    """No other bundle at any level carries a dedicated room scope."""
    examined = 0
    for level, bundles in SCOPE_BUNDLES.items():
        for name, scopes in bundles.items():
            examined += 1
            if (level, name) in {("community", "admin"), ("community", "maintainer")}:
                continue
            assert not [s for s in scopes if s.startswith("rooms:")], (level, name)
    assert examined >= 9, f"only {examined} bundles examined"
