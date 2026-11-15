"""Room scopes are registered and the moderator seed holds only the moderation ones."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from services.scope_registry import KIND_ADMIN, KIND_MODERATOR, all_scopes, effective_scopes

ROOT = Path(__file__).resolve().parents[2]
MANAGEMENT = ("rooms:create", "rooms:update", "rooms:close", "rooms:guests")
MODERATION = ("rooms:kick", "rooms:mute", "rooms:lock", "rooms:hands")


def test_every_room_scope_is_registered_and_admin_holds_them_all() -> None:
    """The computed admin set includes every room scope with no data edit."""
    admin = effective_scopes(KIND_ADMIN, [])
    for scope in (*MANAGEMENT, *MODERATION):
        assert scope in all_scopes(), scope
        assert scope in admin, scope


def test_moderator_stored_list_is_not_computed() -> None:
    """The moderator kind holds only what is stored, so it never gains management scopes."""
    stored = ["community:read", *MODERATION]
    held = effective_scopes(KIND_MODERATOR, stored)
    assert set(MODERATION) <= held
    assert not set(MANAGEMENT) & held


def _load_migration():
    path = ROOT / "alembic" / "versions" / "0118_room_scopes_moderator.py"
    spec = importlib.util.spec_from_file_location("mig0118", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mig0118"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_migration_seeds_exactly_the_moderation_scopes_keyed_on_role_kind() -> None:
    """Short chained revision; SQL keys on role_kind and names only the moderation scopes."""
    mod = _load_migration()
    assert len(mod.revision) <= 32
    assert mod.down_revision == "0117_community_role_kind"
    assert tuple(mod.MODERATOR_ROOM_SCOPES) == MODERATION
    src = (ROOT / "alembic" / "versions" / "0118_room_scopes_moderator.py").read_text()
    assert "role_kind = 2" in src
    assert not re.search(r"name\s*=\s*'moderator'", src), "must key on role_kind, never a role name"
    for scope in MANAGEMENT:
        assert scope not in src.split('"""', 2)[2], f"{scope} must not be granted to moderators"
