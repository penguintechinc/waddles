"""Tests for `services.app_source_binding_service` (AUTO-BIND, spec Sec5.1/Sec9.5).

`sync_bindings()` is exercised directly against `install_dal.engine.begin()`
(the same transaction primitive `bundle_approval_service._write_approval_
and_activate()` uses) rather than only through `approve_version()` --
`test_bundle_approval_service.py` covers the end-to-end wiring; this file
covers the binding-resolution logic in isolation (platform matching,
tenant-wide vs community-scoped `ingest_sources` filtering, replace-on-
resync, zero-source WARN).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.app_source_binding_service import (
    provision_source_stream_groups,
    sync_bindings,
)
from services.bundle_manifest_v2 import BundleManifestV2, ConsumeRule, Limits

_APP_ID = "waddles.socials.music.default"


def _manifest(*platforms: str) -> BundleManifestV2:
    return BundleManifestV2(
        schema_version=2,
        app_id=_APP_ID,
        name="Music Station",
        version="1.0.0",
        feature="waddles.socials.music",
        module="socials",
        provider="builtin",
        language="python",
        artifact="source",
        execution_model="native",
        is_default=False,
        stages={},
        egress=(),
        data_tables=(),
        limits=Limits(timeout_ms=2000, memory_mb=64, egress_rps=10),
        permissions=(),
        routes_to=(),
        consumes=tuple(
            ConsumeRule(platform=p, source_id=None, event_types=("chat.message",))
            for p in platforms
        ),
    )


async def _seed_ingest_source(
    install_dal: Any,
    *,
    tenant_id: int = 1,
    community_id: int | None = None,
    platform: str = "twitch",
    source_id: str = "tw-a",
    enabled: bool = True,
) -> None:
    now = datetime.now(UTC)
    await install_dal.ingest_sources.async_insert(
        tenant_id=tenant_id,
        community_id=community_id,
        platform=platform,
        source_id=source_id,
        label=source_id,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )


async def _sync(
    install_dal: Any, *, tenant_id: int, community_id: int | None, manifest: BundleManifestV2
) -> dict[str, list[str]]:
    bindings_table = install_dal.metadata.tables["app_source_bindings"]
    ingest_sources_table = install_dal.metadata.tables["ingest_sources"]
    async with install_dal.engine.begin() as conn:
        return await sync_bindings(
            conn,
            tenant_id=tenant_id,
            community_id=community_id,
            app_id=_APP_ID,
            manifest=manifest,
            bindings_table=bindings_table,
            ingest_sources_table=ingest_sources_table,
        )


async def _binding_rows(install_dal: Any) -> Any:
    return await install_dal(install_dal.app_source_bindings.app_id == _APP_ID).select()


async def test_sync_bindings_binds_every_matching_enabled_source(install_dal: Any) -> None:
    await _seed_ingest_source(install_dal, source_id="tw-a")
    await _seed_ingest_source(install_dal, source_id="tw-b")
    bound = await _sync(install_dal, tenant_id=1, community_id=None, manifest=_manifest("twitch"))
    assert bound == {"twitch": ["tw-a", "tw-b"]}
    rows = await _binding_rows(install_dal)
    assert {r.source_id for r in rows} == {"tw-a", "tw-b"}
    assert all(r.community_id == 0 for r in rows)  # TENANT_WIDE_COMMUNITY_SENTINEL


async def test_sync_bindings_ignores_disabled_sources(install_dal: Any) -> None:
    await _seed_ingest_source(install_dal, source_id="tw-a", enabled=True)
    await _seed_ingest_source(install_dal, source_id="tw-disabled", enabled=False)
    bound = await _sync(install_dal, tenant_id=1, community_id=None, manifest=_manifest("twitch"))
    assert bound == {"twitch": ["tw-a"]}


async def test_sync_bindings_zero_sources_for_a_platform_binds_nothing_and_warns(
    install_dal: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        bound = await _sync(
            install_dal, tenant_id=1, community_id=None, manifest=_manifest("discord")
        )
    assert bound == {}
    rows = await _binding_rows(install_dal)
    assert not rows
    assert any("no configured ingest sources" in r.message for r in caplog.records)


async def test_sync_bindings_no_consumes_binds_nothing(install_dal: Any) -> None:
    await _seed_ingest_source(install_dal)
    bound = await _sync(install_dal, tenant_id=1, community_id=None, manifest=_manifest())
    assert bound == {}
    assert not await _binding_rows(install_dal)


async def test_sync_bindings_tenant_wide_ignores_community_scoped_sources(
    install_dal: Any,
) -> None:
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    await _seed_ingest_source(install_dal, community_id=community_id, source_id="tw-community")
    bound = await _sync(install_dal, tenant_id=1, community_id=None, manifest=_manifest("twitch"))
    assert bound == {}  # the only configured source is community-scoped, not tenant-wide


async def test_sync_bindings_community_scoped_ignores_tenant_wide_sources(
    install_dal: Any,
) -> None:
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    await _seed_ingest_source(install_dal, community_id=None, source_id="tw-tenant-wide")
    bound = await _sync(
        install_dal, tenant_id=1, community_id=community_id, manifest=_manifest("twitch")
    )
    assert bound == {}  # the only configured source is tenant-wide, not this community's


async def test_sync_bindings_community_scoped_binds_its_own_sources(install_dal: Any) -> None:
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    await _seed_ingest_source(install_dal, community_id=community_id, source_id="tw-community")
    bound = await _sync(
        install_dal, tenant_id=1, community_id=community_id, manifest=_manifest("twitch")
    )
    assert bound == {"twitch": ["tw-community"]}
    rows = await _binding_rows(install_dal)
    assert rows.first().community_id == community_id


async def test_sync_bindings_ignores_a_different_tenants_source(install_dal: Any) -> None:
    await _seed_ingest_source(install_dal, tenant_id=999, source_id="tw-other-tenant")
    bound = await _sync(install_dal, tenant_id=1, community_id=None, manifest=_manifest("twitch"))
    assert bound == {}


async def test_sync_bindings_replaces_on_resync(install_dal: Any) -> None:
    await _seed_ingest_source(install_dal, source_id="tw-a")
    first = await _sync(install_dal, tenant_id=1, community_id=None, manifest=_manifest("twitch"))
    assert first == {"twitch": ["tw-a"]}

    await _seed_ingest_source(install_dal, source_id="tw-b")
    second = await _sync(install_dal, tenant_id=1, community_id=None, manifest=_manifest("twitch"))
    assert second == {"twitch": ["tw-a", "tw-b"]}
    rows = await _binding_rows(install_dal)
    assert len(rows) == 2  # the stale single-row set was replaced, not appended to


async def test_sync_bindings_resync_to_a_narrower_manifest_drops_the_stale_binding(
    install_dal: Any,
) -> None:
    await _seed_ingest_source(install_dal, platform="twitch", source_id="tw-a")
    await _seed_ingest_source(install_dal, platform="discord", source_id="dg-a")
    await _sync(
        install_dal, tenant_id=1, community_id=None, manifest=_manifest("twitch", "discord")
    )
    assert len(await _binding_rows(install_dal)) == 2

    narrowed = await _sync(
        install_dal, tenant_id=1, community_id=None, manifest=_manifest("twitch")
    )
    assert narrowed == {"twitch": ["tw-a"]}
    rows = await _binding_rows(install_dal)
    assert {r.platform for r in rows} == {"twitch"}


async def test_provision_source_stream_groups_uses_the_exact_source_stream_key() -> None:
    fake_client = AsyncMock()
    await provision_source_stream_groups(
        fake_client,
        tenant_slug="acme",
        community_segment=None,
        app_id=_APP_ID,
        bound={"twitch": ["tw-a", "tw-b"], "discord": ["dg-a"]},
    )
    assert fake_client.xgroup_create.await_count == 3
    called = {call.args[0]: call.args[1] for call in fake_client.xgroup_create.await_args_list}
    assert called == {
        "waddles:t:acme:c:_tenant:src:twitch:tw-a:events": _APP_ID,
        "waddles:t:acme:c:_tenant:src:twitch:tw-b:events": _APP_ID,
        "waddles:t:acme:c:_tenant:src:discord:dg-a:events": _APP_ID,
    }


async def test_provision_source_stream_groups_with_a_community_segment() -> None:
    fake_client = AsyncMock()
    await provision_source_stream_groups(
        fake_client,
        tenant_slug="acme",
        community_segment="acme-community",
        app_id=_APP_ID,
        bound={"twitch": ["tw-community"]},
    )
    fake_client.xgroup_create.assert_awaited_once_with(
        "waddles:t:acme:c:acme-community:src:twitch:tw-community:events",
        _APP_ID,
        id="$",
        mkstream=True,
    )


async def test_provision_source_stream_groups_with_no_bindings_calls_nothing() -> None:
    fake_client = AsyncMock()
    await provision_source_stream_groups(
        fake_client, tenant_slug="acme", community_segment=None, app_id=_APP_ID, bound={}
    )
    fake_client.xgroup_create.assert_not_called()
