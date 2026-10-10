"""The persistence layer on its own: transactional guards that the service also checks upstream."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from penguin_dal import AsyncDB
from sqlalchemy import MetaData, text

from services.provider_connections import store
from services.provider_connections.errors import (
    ConnectionConflict,
    ConnectionNotFound,
    SchemaMissing,
)
from services.provider_connections.service import ProviderConnectionService
from tests.provider_connections.conftest import OTHER_CHANNEL_LOGIN, build_pc_metadata
from tests.provider_connections.fake_twitch import FakeTwitch
from tests.provider_connections.support import create_twitch, grant_to, raw_rows

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
async def view(pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any) -> Any:
    return await create_twitch(pc_service, pc_db, bot_tokens)


class TestUpdateLink:
    async def test_the_store_itself_refuses_to_rewrite_a_shared_link(
        self, pc_db: Any, view: Any
    ) -> None:
        """Defence in depth: a grant added after the service's pre-check must still be honoured."""
        cid = view.binding.connection.id
        await grant_to(pc_db, community_id=pc_db.seed.second_community_id, connection_id=cid)
        before = await raw_rows(pc_db, "platform_connections")
        with pytest.raises(ConnectionConflict, match="shared with other communities"):
            await store.update_link(
                pc_db.install_dal,
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                connection_id=cid,
                values={"resource_id": "hijacked"},
                now=NOW,
            )
        assert await raw_rows(pc_db, "platform_connections") == before

    async def test_missing_connection(self, pc_db: Any) -> None:
        with pytest.raises(ConnectionNotFound):
            await store.update_link(
                pc_db.install_dal,
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                connection_id=42,
                values={"status": "active"},
                now=NOW,
            )

    async def test_other_tenants_connection_is_not_found(self, pc_db: Any, view: Any) -> None:
        with pytest.raises(ConnectionNotFound):
            await store.update_link(
                pc_db.install_dal,
                tenant_id=pc_db.seed.other_tenant_id,
                community_id=pc_db.seed.community_id,
                connection_id=view.binding.connection.id,
                values={"status": "revoked"},
                now=NOW,
            )
        assert (await raw_rows(pc_db, "platform_connections"))[0]["status"] == "active"

    async def test_unique_violation_on_repoint_is_a_conflict(
        self, pc_service: ProviderConnectionService, pc_db: Any, view: Any, fake_twitch: FakeTwitch
    ) -> None:
        from tests.provider_connections.conftest import BOT

        other = await create_twitch(
            pc_service, pc_db, fake_twitch.mint(account=BOT), channel=OTHER_CHANNEL_LOGIN
        )
        with pytest.raises(ConnectionConflict, match="already connected"):
            await store.update_link(
                pc_db.install_dal,
                tenant_id=pc_db.seed.tenant_id,
                community_id=pc_db.seed.community_id,
                connection_id=other.binding.connection.id,
                values={"resource_id": view.binding.connection.resource_id},
                now=NOW,
            )


class TestMissingRows:
    async def test_toggle_delete_and_get_on_nothing(self, pc_db: Any) -> None:
        scope = {"tenant_id": pc_db.seed.tenant_id, "community_id": pc_db.seed.community_id}
        assert (
            await store.set_enabled(
                pc_db.install_dal, connection_id=1, enabled=True, now=NOW, **scope
            )
            is None
        )
        assert await store.delete_for_community(pc_db.install_dal, connection_id=1, **scope) is None
        assert await store.get_binding(pc_db.install_dal, connection_id=1, **scope) is None
        assert (
            await store.read_resolve_state(
                pc_db.install_dal, community_id=pc_db.seed.community_id, connection_id=1
            )
            is None
        )

    async def test_lock_for_refresh_on_nothing_yields_none(self, pc_db: Any) -> None:
        async with store.lock_for_refresh(
            pc_db.install_dal, community_id=pc_db.seed.community_id, connection_id=1
        ) as locked:
            assert locked is None

    async def test_the_lock_context_rolls_back_when_its_body_raises(
        self, pc_db: Any, view: Any
    ) -> None:
        cid = view.binding.connection.id
        with pytest.raises(RuntimeError, match="boom"):
            async with store.lock_for_refresh(
                pc_db.install_dal, community_id=pc_db.seed.community_id, connection_id=cid
            ) as locked:
                assert locked is not None
                await locked.mark_unusable(status="revoked", reason="oops", now=NOW)
                raise RuntimeError("boom")
        assert (await raw_rows(pc_db, "platform_connections"))[0]["status"] == "active"

    async def test_the_lock_context_commits_on_clean_exit(self, pc_db: Any, view: Any) -> None:
        cid = view.binding.connection.id
        async with store.lock_for_refresh(
            pc_db.install_dal, community_id=pc_db.seed.community_id, connection_id=cid
        ) as locked:
            assert locked is not None
            await locked.mark_unusable(status="expired", reason="token_expired", now=NOW)
        row = (await raw_rows(pc_db, "platform_connections"))[0]
        assert (row["status"], row["status_reason"]) == ("expired", "token_expired")


class TestSchemaGuard:
    async def test_missing_tables_fail_loudly_with_the_migration_to_run(
        self, tmp_path: Any
    ) -> None:
        db = AsyncDB(f"sqlite://{tmp_path / 'empty.db'}", pool_size=1)
        await db.reflect()
        try:
            with pytest.raises(SchemaMissing, match="table.*not present"):
                await store.list_bindings(db, tenant_id=1, community_id=1)
        finally:
            await db.close()

    async def test_missing_0056_columns_fail_loudly_naming_the_migration(
        self, tmp_path: Any
    ) -> None:
        """A database that has 0035 but not 0056 must refuse, not run on a half-schema."""
        db = AsyncDB(f"sqlite://{tmp_path / 'half.db'}", pool_size=1)
        async with db.engine.begin() as conn:
            await conn.run_sync(build_pc_metadata().create_all)
            await conn.execute(
                text("CREATE TABLE communities (id INTEGER PRIMARY KEY, tenant_id INTEGER)")
            )
            for column in ("resource_metadata", "expires_at", "status_reason"):
                await conn.execute(text(f"ALTER TABLE platform_connections DROP COLUMN {column}"))
            await conn.execute(text("ALTER TABLE community_connection_access DROP COLUMN enabled"))
        await db.reflect()
        try:
            with pytest.raises(SchemaMissing, match="0056_provider_connections") as info:
                await store.list_bindings(db, tenant_id=1, community_id=1)
            assert "resource_metadata" in info.value.message and "enabled" in info.value.message
        finally:
            await db.close()


class TestDecoding:
    def test_json_text_is_decoded_and_junk_is_empty(self) -> None:
        assert store._as_dict('{"a": 1}') == {"a": 1}  # noqa: SLF001
        assert store._as_dict({"a": 1}) == {"a": 1}  # noqa: SLF001
        assert store._as_dict(None) == {}  # noqa: SLF001
        assert store._as_dict("[1, 2]") == {}  # noqa: SLF001

    def test_naive_datetimes_are_treated_as_utc(self) -> None:
        assert store._aware(None) is None  # noqa: SLF001
        naive = datetime(2026, 1, 1, 0, 0)
        assert store._aware(naive) == naive.replace(tzinfo=UTC)  # noqa: SLF001
        aware = datetime(2026, 1, 1, tzinfo=UTC)
        assert store._aware(aware) is aware  # noqa: SLF001

    def test_unused_metadata_import_is_the_mirror_builder(self) -> None:
        assert isinstance(build_pc_metadata(), MetaData)
