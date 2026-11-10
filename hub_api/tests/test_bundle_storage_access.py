"""Cross-prefix storage access: manifest parse, risk class, summary, widening, never-auto-grant."""

from __future__ import annotations

from typing import Any

import pytest

from services import bundle_permission_service as svc
from services.bundle_approval_service import classify_diff
from services.bundle_manifest_v2 import ManifestV2Error, parse_bundle_manifest_v2
from services.bundle_permission_catalog import is_dangerous, permission_family, resolve_risk
from services.bundle_storage_access import (
    StorageAccessError,
    is_storage_access_id,
    parse_storage_access,
    parse_storage_access_id,
)
from services.permission_summary_service import build_permission_summary, permission_hash

_ENTRY = {
    "prefix_owner": "waddles.harness.media",
    "ops": ["read", "list"],
    "reason": "Fetch generated clips",
}
_RAW = {
    "schema_version": 2,
    "app_id": "waddles.core.role_sync.default",
    "name": "Role sync",
    "version": "1.0.0",
    "feature": "waddles.core.role_sync",
    "module": "core",
    "provider": "builtin",
    "author": "github:penguintechinc",
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


def test_is_dangerous_family_and_non_collision_with_existing_storage_ids() -> None:
    pid = "storage.read:waddles.harness.media"
    assert resolve_risk(pid) == "dangerous" and is_dangerous(pid)
    assert permission_family(pid) == "storage.read"
    assert parse_storage_access_id(pid) == ("read", "waddles.harness.media")
    assert not is_storage_access_id("storage.kv") and not is_storage_access_id("storage.tables")
    for bad in (
        "storage.read:",
        "storage.read:../x",
        "storage.read:A",
        "storage.rm:a",
        "storage.read:a/b",
    ):
        assert resolve_risk(bad) is None, bad


def test_manifest_folds_into_dangerous_permissions() -> None:
    manifest = _parse(storage_access=[_ENTRY])
    ids = {d.id: d for d in manifest.permission_declarations}
    assert set(ids) == {"storage.read:waddles.harness.media", "storage.list:waddles.harness.media"}
    assert all(d.risk == "dangerous" for d in ids.values())
    assert manifest.storage_access[0].reason == _ENTRY["reason"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"prefix_owner": "../x"},
        {"prefix_owner": "Bad Owner"},
        {"prefix_owner": "waddles.core.role_sync.default"},
        {"ops": []},
        {"ops": ["admin"]},
        {"reason": ""},
        {"reason": "x" * 281},
    ],
)
def test_invalid_entries_rejected(mutation: dict[str, Any]) -> None:
    with pytest.raises(ManifestV2Error):
        _parse(storage_access=[{**_ENTRY, **mutation}])


def test_direct_permission_id_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(permissions=[{"id": "storage.read:waddles.harness.media", "justification": "x"}])
    assert exc.value.reason == "use_storage_access_block"


def test_parse_shape_errors() -> None:
    assert parse_storage_access(None) == ()
    for bad in ("x", [1], [_ENTRY, _ENTRY]):
        with pytest.raises(StorageAccessError):
            parse_storage_access(bad)


def _summary(**extra: Any) -> dict[str, Any]:
    return build_permission_summary(
        _parse(**extra),
        grant_labels=[],
        component_capabilities=frozenset(),
        min_tier="free",
        flag_key="f",
        allow_private_hosts=False,
    )


def test_summary_carries_storage_access_and_widening_changes_hash() -> None:
    narrow = _summary(storage_access=[{**_ENTRY, "ops": ["read"]}])
    wide = _summary(storage_access=[_ENTRY])
    assert wide["storageAccess"] == [
        {
            "prefixOwner": "waddles.harness.media",
            "ops": ["list", "read"],
            "reason": _ENTRY["reason"],
        }
    ]
    assert permission_hash(narrow) != permission_hash(wide)
    assert classify_diff(wide, narrow) == "widened"
    assert classify_diff(narrow, wide) == "narrowed"
    new_owner = _summary(
        storage_access=[_ENTRY, {**_ENTRY, "prefix_owner": "waddles.other.svc", "ops": ["read"]}]
    )
    assert classify_diff(new_owner, wide) == "widened"
    assert "storageAccess" not in _summary()


async def test_core_seeder_never_auto_grants_storage_access(install_dal: Any) -> None:
    manifest = _parse(
        storage_access=[_ENTRY], permissions=[{"id": "ai.generate", "justification": "j"}]
    )
    await svc.seed_core_permission_requests(
        install_dal, app_id=manifest.app_id, version=manifest.version, manifest=manifest
    )
    ids = await svc.get_approved_permission_ids(
        install_dal, app_id=manifest.app_id, version=manifest.version
    )
    assert ids == frozenset({"ai.generate"})


async def test_system_reseed_preserves_human_approved_storage_access(install_dal: Any) -> None:
    manifest = _parse(storage_access=[_ENTRY])
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
        {"storage.read:waddles.harness.media", "storage.list:waddles.harness.media"}
    )


def test_access_grants_response_carries_storage_blocks_explicitly() -> None:
    from services.access_grant_dto import access_grants_response

    resp = access_grants_response(
        {
            "storageAccess": [
                {"prefixOwner": "waddles.harness.media", "ops": ["read"], "reason": "r"}
            ]
        },
        frozenset({"storage.read:waddles.harness.media"}),
        frozenset(),
        frozenset({"storage.read:waddles.harness.media"}),
    )
    assert [(s.prefixOwner, s.ops, s.reason) for s in resp.storageAccess] == [
        ("waddles.harness.media", ["read"], "r")
    ]
    assert resp.tableAccess == []
