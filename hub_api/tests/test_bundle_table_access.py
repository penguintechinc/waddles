"""Platform-table access: allowlist, manifest parse, risk class, summary, never-auto-grant."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from services import bundle_permission_service as svc
from services.bundle_approval_service import classify_diff
from services.bundle_manifest_v2 import ManifestV2Error, parse_bundle_manifest_v2
from services.bundle_permission_catalog import is_dangerous, permission_family, resolve_risk
from services.bundle_table_access import (
    PLATFORM_TABLE_ALLOWLIST,
    TableAccessError,
    is_allowed,
    parse_table_access,
    parse_table_access_id,
)
from services.permission_summary_service import build_permission_summary, permission_hash

_FIXTURE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "core/bundle_capability_gate/tests/fixtures/platform_table_allowlist.json"
)
_ENTRY = {
    "table": "community_members",
    "columns": ["role"],
    "ops": ["read", "write"],
    "reason": "Mirror Discord role to community role",
}
_RAW = {
    "schema_version": 2,
    "app_id": "waddles.core.role_sync.default",
    "name": "Role sync",
    "version": "1.0.0",
    "feature": "waddles.core.role_sync",
    "module": "core",
    "provider": "builtin",
    "language": "python",
    "artifact": "source",
    "stages": {
        "process": {
            "entry": "x:y",
            "consumes": [{"platform": "discord", "event_types": ["chat.message"]}],
        }
    },
}


def _parse(**extra: Any) -> Any:
    return parse_bundle_manifest_v2(
        {**_RAW, **extra},
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )


def test_allowlist_matches_rust_parity_fixture() -> None:
    fixture = json.loads(_FIXTURE.read_text())
    py = {
        t: {c: sorted(o) for c, o in cols.items()} for t, cols in PLATFORM_TABLE_ALLOWLIST.items()
    }
    assert py == {t: {c: sorted(o) for c, o in cols.items()} for t, cols in fixture.items()}


@pytest.mark.parametrize(
    "table", ["hub_users", "hub_user_identities", "platform_connections", "tenants", "users"]
)
def test_identity_and_credential_tables_never_allowlisted(table: str) -> None:
    assert table not in PLATFORM_TABLE_ALLOWLIST
    assert not is_allowed("read", table, "email")


@pytest.mark.parametrize(
    "column", ["display_name", "avatar_url", "bio", "social_links", "platform_user_id", "user_id"]
)
def test_pii_columns_denied(column: str) -> None:
    assert not is_allowed("read", "community_members", column)
    assert not is_allowed("write", "community_members", column)


def test_is_dangerous_and_family() -> None:
    pid = "platform.table.write:community_members.role"
    assert resolve_risk(pid) == "dangerous" and is_dangerous(pid)
    assert permission_family(pid) == "platform.table.write"
    assert resolve_risk("platform.table.write:community_members.is_active") is None
    assert resolve_risk("platform.table.read:hub_users.email") is None
    assert parse_table_access_id("platform.table.read:community_members.joined_at") == (
        "read",
        "community_members",
        "joined_at",
    )


def test_manifest_folds_into_dangerous_permissions() -> None:
    manifest = _parse(table_access=[_ENTRY])
    ids = {d.id: d for d in manifest.permission_declarations}
    assert set(ids) == {
        "platform.table.read:community_members.role",
        "platform.table.write:community_members.role",
    }
    assert all(d.risk == "dangerous" for d in ids.values())
    assert manifest.table_access[0].reason == _ENTRY["reason"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"table": "hub_users"},
        {"columns": ["display_name"]},
        {"columns": []},
        {"ops": ["write"], "columns": ["is_active"]},
        {"ops": ["delete"]},
        {"reason": ""},
        {"reason": "x" * 281},
    ],
)
def test_invalid_entries_rejected(mutation: dict[str, Any]) -> None:
    with pytest.raises(ManifestV2Error):
        _parse(table_access=[{**_ENTRY, **mutation}])


def test_direct_permission_id_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            permissions=[
                {"id": "platform.table.write:community_members.role", "justification": "x"}
            ]
        )
    assert exc.value.reason == "use_table_access_block"


def test_service_bundle_cannot_request_table_access() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            {**_RAW, "kind": "service", "table_access": [_ENTRY]},
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
            trusted_source=True,
        )
    assert exc.value.reason == "table_access_app_only"


def test_parse_table_access_shape_errors() -> None:
    assert parse_table_access(None) == ()
    for bad in ("x", [1], [_ENTRY, _ENTRY]):
        with pytest.raises(TableAccessError):
            parse_table_access(bad)


def _summary(**extra: Any) -> dict[str, Any]:
    return build_permission_summary(
        _parse(**extra),
        grant_labels=[],
        component_capabilities=frozenset(),
        min_tier="free",
        flag_key="f",
        allow_private_hosts=False,
    )


def test_summary_carries_table_access_and_widening_changes_hash() -> None:
    narrow = _summary(table_access=[{**_ENTRY, "ops": ["read"]}])
    wide = _summary(table_access=[_ENTRY])
    assert wide["tableAccess"] == [
        {
            "table": "community_members",
            "columns": ["role"],
            "ops": ["read", "write"],
            "reason": _ENTRY["reason"],
        }
    ]
    assert permission_hash(narrow) != permission_hash(wide)
    assert classify_diff(wide, narrow) == "widened"
    assert classify_diff(narrow, wide) == "narrowed"
    assert "tableAccess" not in _summary()


async def test_core_seeder_never_auto_grants_table_access(install_dal: Any) -> None:
    manifest = _parse(
        table_access=[_ENTRY], permissions=[{"id": "ai.generate", "justification": "j"}]
    )
    await svc.seed_core_permission_requests(
        install_dal, app_id=manifest.app_id, version=manifest.version, manifest=manifest
    )
    ids = await svc.get_approved_permission_ids(
        install_dal, app_id=manifest.app_id, version=manifest.version
    )
    assert ids == frozenset({"ai.generate"})


async def test_system_reseed_preserves_human_approved_table_access(install_dal: Any) -> None:
    manifest = _parse(table_access=[_ENTRY])
    await svc.record_permission_requests(
        install_dal,
        app_id=manifest.app_id,
        version=manifest.version,
        declarations=manifest.permission_declarations,
        approved_by=7,
        approved_permissions=frozenset(d.id for d in manifest.permission_declarations),
    )
    await svc.seed_core_permission_requests(
        install_dal, app_id=manifest.app_id, version=manifest.version, manifest=manifest
    )
    ids = await svc.get_approved_permission_ids(
        install_dal, app_id=manifest.app_id, version=manifest.version
    )
    assert ids == frozenset(
        {
            "platform.table.read:community_members.role",
            "platform.table.write:community_members.role",
        }
    )
