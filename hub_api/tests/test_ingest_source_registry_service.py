"""Tests for `services/ingest_source_registry_service.py` -- per-community ingest_sources registry.

No secret material is ever generated or stored by this module (contrast
`test_ingest_source_service.py`, the sibling webhook-secret feature over
the same table). `community_id` is required on every operation (product
requirement: a physical source may eventually link to multiple communities
of one tenant) -- see the module's own docstring for the known gap against
migration 0020's still-tenant-wide `UNIQUE (tenant_id, platform, source_id)`
constraint, exercised here by `test_same_source_in_two_communities_of_one_tenant_conflicts`.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.errors import ApiError
from services.ingest_source_registry_service import (
    create_ingest_source,
    delete_ingest_source,
    get_ingest_source,
    list_ingest_sources,
    update_ingest_source,
)


async def _make_community(install_dal: Any, tenant_id: int, name: str = "acme-community") -> int:
    return int(await install_dal.communities.async_insert(tenant_id=tenant_id, name=name))


async def test_create_ingest_source_stores_no_secret(install_dal: Any) -> None:
    community_id = await _make_community(install_dal, tenant_id=1)
    row = await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        platform="discord",
        source_id="guild-123",
        label="Main Discord",
    )
    assert row.platform == "discord"
    assert row.source_id == "guild-123"
    assert row.community_id == community_id
    assert row.secret_ciphertext is None
    assert row.secret_iv is None
    assert row.enabled is True


async def test_create_ingest_source_creates_a_workstream(install_dal: Any) -> None:
    community_id = await _make_community(install_dal, tenant_id=1)
    row = await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        platform="twitch",
        source_id="channel-1",
        label="Twitch",
    )
    workstream = (
        await install_dal(install_dal.workstreams.ingest_source_id == row.id).select()
    ).first()
    assert workstream is not None
    assert workstream.disabled_at is None


async def test_create_ingest_source_rejects_unsupported_platform(install_dal: Any) -> None:
    community_id = await _make_community(install_dal, tenant_id=1)
    with pytest.raises(ApiError) as exc:
        await create_ingest_source(
            install_dal,
            tenant_id=1,
            community_id=community_id,
            platform="myspace",
            source_id="x",
            label="x",
        )
    assert exc.value.status_code == 422


async def test_create_ingest_source_rejects_an_unbounded_source_id(install_dal: Any) -> None:
    community_id = await _make_community(install_dal, tenant_id=1)
    with pytest.raises(ApiError) as exc:
        await create_ingest_source(
            install_dal,
            tenant_id=1,
            community_id=community_id,
            platform="discord",
            source_id="x" * 300,
            label="x",
        )
    assert exc.value.status_code == 422


async def test_create_ingest_source_rejects_a_community_from_another_tenant(
    install_dal: Any,
) -> None:
    other_community_id = await _make_community(
        install_dal, tenant_id=999, name="other-tenant-community"
    )
    with pytest.raises(ApiError) as exc:
        await create_ingest_source(
            install_dal,
            tenant_id=1,
            community_id=other_community_id,
            platform="discord",
            source_id="guild-1",
            label="x",
        )
    assert exc.value.status_code == 404


async def test_duplicate_platform_source_in_same_community_raises_409(install_dal: Any) -> None:
    community_id = await _make_community(install_dal, tenant_id=1)
    await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        platform="discord",
        source_id="guild-dup",
        label="a",
    )
    with pytest.raises(ApiError) as exc:
        await create_ingest_source(
            install_dal,
            tenant_id=1,
            community_id=community_id,
            platform="discord",
            source_id="guild-dup",
            label="b",
        )
    assert exc.value.status_code == 409


async def test_same_source_in_two_communities_of_one_tenant_conflicts(install_dal: Any) -> None:
    """Known gap (module docstring): migration 0020's real constraint is tenant-wide.

    The product-approved model allows one physical source across multiple
    communities of a tenant; today the DB's `UNIQUE (tenant_id, platform,
    source_id)` still blocks it. This asserts the module surfaces that as a
    clear, documented 409 rather than silently succeeding or corrupting data.
    """
    community_a = await _make_community(install_dal, tenant_id=1, name="community-a")
    community_b = await _make_community(install_dal, tenant_id=1, name="community-b")
    await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_a,
        platform="discord",
        source_id="shared-guild",
        label="a",
    )
    with pytest.raises(ApiError) as exc:
        await create_ingest_source(
            install_dal,
            tenant_id=1,
            community_id=community_b,
            platform="discord",
            source_id="shared-guild",
            label="b",
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "SOURCE_LINKED_TO_ANOTHER_COMMUNITY"


async def test_same_platform_source_allowed_for_a_different_tenant(install_dal: Any) -> None:
    await install_dal.tenants.async_insert(slug="other-corp", display_name="Other", is_active=True)
    community_1 = await _make_community(install_dal, tenant_id=1)
    community_2 = await _make_community(install_dal, tenant_id=2)
    await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_1,
        platform="discord",
        source_id="guild-shared",
        label="a",
    )
    row = await create_ingest_source(
        install_dal,
        tenant_id=2,
        community_id=community_2,
        platform="discord",
        source_id="guild-shared",
        label="b",
    )
    assert row.tenant_id == 2


async def test_list_ingest_sources_is_community_scoped(install_dal: Any) -> None:
    community_a = await _make_community(install_dal, tenant_id=1, name="community-a")
    community_b = await _make_community(install_dal, tenant_id=1, name="community-b")
    await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_a,
        platform="discord",
        source_id="a",
        label="a",
    )
    await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_b,
        platform="discord",
        source_id="b",
        label="b",
    )
    rows, next_cursor = await list_ingest_sources(
        install_dal, tenant_id=1, community_id=community_a
    )
    assert len(rows) == 1
    assert rows[0].community_id == community_a
    assert next_cursor is None


async def test_list_ingest_sources_filters_by_platform_and_enabled(install_dal: Any) -> None:
    community_id = await _make_community(install_dal, tenant_id=1)
    row_a = await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        platform="discord",
        source_id="a",
        label="a",
    )
    await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        platform="twitch",
        source_id="b",
        label="b",
    )
    await update_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        ingest_source_id=row_a.id,
        enabled=False,
    )

    discord_only, _ = await list_ingest_sources(
        install_dal, tenant_id=1, community_id=community_id, platform="discord"
    )
    assert [r.id for r in discord_only] == [row_a.id]

    enabled_only, _ = await list_ingest_sources(
        install_dal, tenant_id=1, community_id=community_id, enabled=True
    )
    assert row_a.id not in [r.id for r in enabled_only]


async def test_list_ingest_sources_empty_result(install_dal: Any) -> None:
    community_id = await _make_community(install_dal, tenant_id=1)
    rows, next_cursor = await list_ingest_sources(
        install_dal, tenant_id=1, community_id=community_id
    )
    assert rows == []
    assert next_cursor is None


async def test_list_ingest_sources_invalid_limit_raises_400(install_dal: Any) -> None:
    community_id = await _make_community(install_dal, tenant_id=1)
    with pytest.raises(ApiError) as exc:
        await list_ingest_sources(install_dal, tenant_id=1, community_id=community_id, limit=0)
    assert exc.value.status_code == 400
    with pytest.raises(ApiError) as exc:
        await list_ingest_sources(install_dal, tenant_id=1, community_id=community_id, limit=9999)
    assert exc.value.status_code == 400


async def test_list_ingest_sources_paginates_via_cursor(install_dal: Any) -> None:
    community_id = await _make_community(install_dal, tenant_id=1)
    for i in range(3):
        await create_ingest_source(
            install_dal,
            tenant_id=1,
            community_id=community_id,
            platform="discord",
            source_id=f"guild-{i}",
            label=f"g{i}",
        )
    page1, cursor1 = await list_ingest_sources(
        install_dal, tenant_id=1, community_id=community_id, limit=2
    )
    assert len(page1) == 2
    assert cursor1 is not None
    page2, cursor2 = await list_ingest_sources(
        install_dal, tenant_id=1, community_id=community_id, limit=2, cursor=cursor1
    )
    assert len(page2) == 1
    assert cursor2 is None
    assert {r.id for r in page1} & {r.id for r in page2} == set()


async def test_update_ingest_source_disables_the_workstream(install_dal: Any) -> None:
    community_id = await _make_community(install_dal, tenant_id=1)
    row = await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        platform="discord",
        source_id="a",
        label="a",
    )
    updated = await update_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        ingest_source_id=row.id,
        enabled=False,
    )
    assert updated.enabled is False
    workstream = (
        await install_dal(install_dal.workstreams.ingest_source_id == row.id).select()
    ).first()
    assert workstream.disabled_at is not None


async def test_update_ingest_source_updates_label_only(install_dal: Any) -> None:
    community_id = await _make_community(install_dal, tenant_id=1)
    row = await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        platform="discord",
        source_id="a",
        label="a",
    )
    updated = await update_ingest_source(
        install_dal, tenant_id=1, community_id=community_id, ingest_source_id=row.id, label="new"
    )
    assert updated.label == "new"
    assert updated.enabled is True


async def test_update_ingest_source_wrong_tenant_raises_404(install_dal: Any) -> None:
    community_id = await _make_community(install_dal, tenant_id=1)
    row = await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        platform="discord",
        source_id="a",
        label="a",
    )
    with pytest.raises(ApiError) as exc:
        await update_ingest_source(
            install_dal,
            tenant_id=2,
            community_id=community_id,
            ingest_source_id=row.id,
            label="hijacked",
        )
    assert exc.value.status_code == 404


async def test_update_ingest_source_wrong_community_raises_404(install_dal: Any) -> None:
    community_a = await _make_community(install_dal, tenant_id=1, name="community-a")
    community_b = await _make_community(install_dal, tenant_id=1, name="community-b")
    row = await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_a,
        platform="discord",
        source_id="a",
        label="a",
    )
    with pytest.raises(ApiError) as exc:
        await update_ingest_source(
            install_dal,
            tenant_id=1,
            community_id=community_b,
            ingest_source_id=row.id,
            label="hijacked",
        )
    assert exc.value.status_code == 404


async def test_delete_ingest_source_removes_the_row(install_dal: Any) -> None:
    community_id = await _make_community(install_dal, tenant_id=1)
    row = await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        platform="discord",
        source_id="a",
        label="a",
    )
    deleted = await delete_ingest_source(
        install_dal, tenant_id=1, community_id=community_id, ingest_source_id=row.id
    )
    assert deleted is True
    assert (
        await get_ingest_source(
            install_dal, tenant_id=1, community_id=community_id, ingest_source_id=row.id
        )
        is None
    )


async def test_delete_ingest_source_wrong_tenant_raises_404(install_dal: Any) -> None:
    """`community_id` belongs to tenant 1, not tenant 2 -- community validation 404s first."""
    community_id = await _make_community(install_dal, tenant_id=1)
    row = await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        platform="discord",
        source_id="a",
        label="a",
    )
    with pytest.raises(ApiError) as exc:
        await delete_ingest_source(
            install_dal, tenant_id=2, community_id=community_id, ingest_source_id=row.id
        )
    assert exc.value.status_code == 404


async def test_delete_ingest_source_wrong_community_returns_false(install_dal: Any) -> None:
    community_a = await _make_community(install_dal, tenant_id=1, name="community-a")
    community_b = await _make_community(install_dal, tenant_id=1, name="community-b")
    row = await create_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=community_a,
        platform="discord",
        source_id="a",
        label="a",
    )
    deleted = await delete_ingest_source(
        install_dal, tenant_id=1, community_id=community_b, ingest_source_id=row.id
    )
    assert deleted is False


async def test_delete_ingest_source_missing_returns_false(install_dal: Any) -> None:
    community_id = await _make_community(install_dal, tenant_id=1)
    deleted = await delete_ingest_source(
        install_dal, tenant_id=1, community_id=community_id, ingest_source_id=999999
    )
    assert deleted is False
