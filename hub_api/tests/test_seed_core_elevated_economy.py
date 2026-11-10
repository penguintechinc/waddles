"""The core seeder never auto-grants the ELEVATED economy permissions to an app bundle.

`economy.transfer_from` / `economy.app_mint` let an APP bundle move/burn a non-actor member's
funds or mint through the economy service: a human community admin must approve each app
(recorded approver + admin-set caps), exactly like `platform.table.*`. The only auto-granted
elevated id is the economy SERVICE bundle's own `economy.transfer_from` (the host's gate for
exposing the primitive; the grant that matters is the calling app's).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cli.seed_core_bundles import seed_one
from services.bundle_permission_service import (
    ECONOMY_ELEVATED_IDS,
    ECONOMY_SERVICE_APP_ID,
    get_approved_permission_ids,
    never_auto_granted,
)
from tests.test_seed_core_bundles import (
    _MANIFEST,
    _patch_validator_and_storage,
    _seed_community,
    _write_bundle,
)

_ELEVATED = [
    {"id": "economy.transfer_from", "justification": "Game rules move a victim's funds."},
    {"id": "economy.app_mint", "justification": "Game rules award currency."},
]


def test_never_auto_granted_covers_elevated_ids_except_the_services_own_transfer_from() -> None:
    assert ECONOMY_ELEVATED_IDS == {"economy.transfer_from", "economy.app_mint"}
    for pid in ECONOMY_ELEVATED_IDS:
        assert never_auto_granted("waddles.core.example.ping", pid)
        assert never_auto_granted("waddles.superpenguintv.duel", pid)
    assert not never_auto_granted(ECONOMY_SERVICE_APP_ID, "economy.transfer_from")
    assert never_auto_granted(ECONOMY_SERVICE_APP_ID, "economy.app_mint")
    # Ordinary permissions stay auto-grantable; resource-access grants stay human-only.
    assert not never_auto_granted("waddles.core.example.ping", "storage.kv")
    assert not never_auto_granted("waddles.core.example.ping", "economy.credit")
    assert never_auto_granted("waddles.core.example.ping", "platform.table.read:users.score")


async def _grants_of(install_dal: Any, app_id: str, community_id: int) -> set[str]:
    rows = await install_dal(
        (install_dal.community_permission_grants.app_id == app_id)
        & (install_dal.community_permission_grants.community_id == community_id)
        & (install_dal.community_permission_grants.revoked_at == None)  # noqa: E711
    ).select()
    return {r.permission_id for r in rows}


async def test_a_core_app_bundle_is_never_auto_granted_an_elevated_permission(
    install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_validator_and_storage(monkeypatch)
    community_id = await _seed_community(install_dal)
    manifest = {
        **_MANIFEST,
        "permissions": [
            {"id": "storage.kv", "justification": "Persists per-community cooldowns."},
            *_ELEVATED,
        ],
    }
    entry = _write_bundle(tmp_path, manifest=manifest, community_id=community_id)
    await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())

    # Neither the global ceiling nor the community grant carries the elevated ids.
    assert await get_approved_permission_ids(
        install_dal, app_id=entry.app_id, version=entry.version
    ) == {"storage.kv"}
    assert await _grants_of(install_dal, entry.app_id, community_id) == {"storage.kv"}


async def test_the_economy_service_bundle_is_granted_only_its_own_transfer_from(
    install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_validator_and_storage(monkeypatch)
    community_id = await _seed_community(install_dal)
    manifest = {
        **_MANIFEST,
        "app_id": ECONOMY_SERVICE_APP_ID,
        "feature": "waddles.core.service",
        "permissions": [
            {"id": "storage.kv", "justification": "Reads the currency policy."},
            *_ELEVATED,
        ],
    }
    entry = _write_bundle(tmp_path, manifest=manifest, community_id=community_id)
    await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())

    assert await _grants_of(install_dal, ECONOMY_SERVICE_APP_ID, community_id) == {
        "storage.kv",
        "economy.transfer_from",
    }
