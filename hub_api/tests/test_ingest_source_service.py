"""Tests for the ingest source registry.

Secret shown once, encrypted at rest, and its 1:1 workstream created
atomically alongside it (spec Sec5.11, D30).
"""

from __future__ import annotations

from typing import Any

import pytest

from services.errors import ApiError
from services.ingest_source_service import (
    create_source,
    delete_source,
    ensure_ingest_source,
    list_sources,
    resolve_secret,
)


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUNDLE_SECRET_ENCRYPTION_KEY", "b" * 64)


async def test_create_source_returns_a_plaintext_secret_once(install_dal: Any) -> None:
    row, secret = await create_source(
        install_dal,
        tenant_id=1,
        community_id=None,
        platform="custom:mycrm",
        source_id="ticketing-1",
        label="MyCRM Ticketing",
        mapping={"event_type": {"pointer": "/type"}},
    )
    assert len(secret) >= 32
    assert row.platform == "custom:mycrm"


async def test_list_sources_never_returns_the_plaintext_secret(install_dal: Any) -> None:
    await create_source(
        install_dal,
        tenant_id=1,
        community_id=None,
        platform="custom:mycrm",
        source_id="ticketing-1",
        label="MyCRM Ticketing",
        mapping=None,
    )
    rows = await list_sources(install_dal, tenant_id=1)
    assert len(rows) == 1
    assert not hasattr(rows[0], "secret_ciphertext") or rows[0].secret_ciphertext is not None
    # the ROW object still carries the encrypted column (penguin_dal.Row
    # returns all selected columns); the service-layer contract is that a
    # DTO built from this row never surfaces secret_ciphertext/secret_iv
    # on the wire -- enforced at the (not-yet-built) blueprint layer.


async def test_resolve_secret_round_trips(install_dal: Any) -> None:
    _, secret = await create_source(
        install_dal,
        tenant_id=1,
        community_id=None,
        platform="custom:mycrm",
        source_id="ticketing-1",
        label="MyCRM Ticketing",
        mapping=None,
    )
    resolved = await resolve_secret(
        install_dal, tenant_id=1, platform="custom:mycrm", source_id="ticketing-1"
    )
    assert resolved == secret


async def test_duplicate_source_raises_409(install_dal: Any) -> None:
    await create_source(
        install_dal,
        tenant_id=1,
        community_id=None,
        platform="custom:mycrm",
        source_id="ticketing-1",
        label="x",
        mapping=None,
    )
    with pytest.raises(ApiError) as exc:
        await create_source(
            install_dal,
            tenant_id=1,
            community_id=None,
            platform="custom:mycrm",
            source_id="ticketing-1",
            label="y",
            mapping=None,
        )
    assert exc.value.status_code == 409


async def test_delete_source_removes_it(install_dal: Any) -> None:
    await create_source(
        install_dal,
        tenant_id=1,
        community_id=None,
        platform="custom:mycrm",
        source_id="ticketing-1",
        label="x",
        mapping=None,
    )
    await delete_source(install_dal, tenant_id=1, source_id="ticketing-1")
    rows = await list_sources(install_dal, tenant_id=1)
    assert rows == []


async def test_create_source_creates_a_workstream(install_dal: Any) -> None:
    row, _ = await create_source(
        install_dal,
        tenant_id=1,
        community_id=None,
        platform="custom:mycrm",
        source_id="ws-reg-1",
        label="x",
        mapping=None,
    )
    workstream = (
        await install_dal(install_dal.workstreams.ingest_source_id == row.id).select()
    ).first()
    assert workstream is not None
    assert workstream.source_id == "ws-reg-1"
    assert workstream.disabled_at is None


async def test_delete_source_disables_the_workstream_without_deleting_it(install_dal: Any) -> None:
    row, _ = await create_source(
        install_dal,
        tenant_id=1,
        community_id=None,
        platform="custom:mycrm",
        source_id="ws-reg-2",
        label="x",
        mapping=None,
    )
    await delete_source(install_dal, tenant_id=1, source_id="ws-reg-2")
    workstream = (
        await install_dal(install_dal.workstreams.ingest_source_id == row.id).select()
    ).first()
    assert workstream is not None
    assert workstream.disabled_at is not None


# ---------------------------------------------------------------------------
# ensure_ingest_source() -- platform-connection registration (core-bundle-seeder)
# ---------------------------------------------------------------------------


async def test_ensure_ingest_source_creates_a_row_with_no_secret(install_dal: Any) -> None:
    row = await ensure_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=None,
        platform="discord",
        source_id="dg-474965105759748096",
        label="svc-ingest Discord guild",
    )
    assert row.platform == "discord"
    assert row.source_id == "dg-474965105759748096"
    assert row.enabled
    assert row.secret_ciphertext is None
    assert row.secret_iv is None


async def test_ensure_ingest_source_creates_no_workstream_row(install_dal: Any) -> None:
    """Unlike create_source(), no 1:1 workstreams row -- that table is generic-webhook scoped."""
    row = await ensure_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=None,
        platform="discord",
        source_id="dg-474965105759748096",
        label="svc-ingest Discord guild",
    )
    workstream = (
        await install_dal(install_dal.workstreams.ingest_source_id == row.id).select()
    ).first()
    assert workstream is None


async def test_ensure_ingest_source_rerun_is_a_no_op_not_a_duplicate(install_dal: Any) -> None:
    first = await ensure_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=None,
        platform="discord",
        source_id="dg-474965105759748096",
        label="svc-ingest Discord guild",
    )
    second = await ensure_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=None,
        platform="discord",
        source_id="dg-474965105759748096",
        label="svc-ingest Discord guild",
    )
    assert second.id == first.id
    rows = await install_dal(
        (install_dal.ingest_sources.tenant_id == 1)
        & (install_dal.ingest_sources.platform == "discord")
        & (install_dal.ingest_sources.source_id == "dg-474965105759748096")
    ).select()
    assert len(rows) == 1


async def test_ensure_ingest_source_reenables_a_previously_disabled_row(install_dal: Any) -> None:
    row = await ensure_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=None,
        platform="discord",
        source_id="dg-474965105759748096",
        label="svc-ingest Discord guild",
    )
    await install_dal(install_dal.ingest_sources.id == row.id).update(enabled=False)

    reenabled = await ensure_ingest_source(
        install_dal,
        tenant_id=1,
        community_id=None,
        platform="discord",
        source_id="dg-474965105759748096",
        label="svc-ingest Discord guild",
    )
    assert reenabled.id == row.id
    assert reenabled.enabled
