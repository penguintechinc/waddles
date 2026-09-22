"""Tests for `bundles.inventory_process.transform` -- `!inventory` chat commands."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from flask_core import PlatformEvent, bundle_context, reset_bundle_dal_for_tests, set_bundle_dal
from penguin_dal import AsyncDB
from sqlalchemy import event
from sqlalchemy import text as sa_text

from bundles.inventory_process import transform


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with a minimal `inventory_items` table.

    Registers a SQLite `NOW()` function and wraps raw_sql_rows/raw_sql_write to
    translate `CAST(:metadata AS jsonb)` to plain `:metadata` so the bundle's
    Postgres-flavored SQL can execute against SQLite. Happy-path tests use these
    real, wrapped helpers. DB-failure-injection tests can monkeypatch further.
    """
    from bundles import inventory_process

    db = AsyncDB("sqlite://", pool_size=1, echo=False)

    @event.listens_for(db.engine.sync_engine, "connect")
    def _register_now(dbapi_conn, connection_record):
        """Register SQLite NOW() function to return current UTC timestamp."""
        dbapi_conn.create_function("NOW", 0, lambda: datetime.now(UTC).isoformat())

    # Wrap raw_sql_rows and raw_sql_write to translate SQL for SQLite compatibility
    original_raw_sql_rows = inventory_process.raw_sql_rows
    original_raw_sql_write = inventory_process.raw_sql_write

    def _translate_sql(sql: str) -> str:
        """Translate PostgreSQL-specific SQL to SQLite-compatible SQL."""
        return sql.replace("CAST(:metadata AS jsonb)", ":metadata")

    async def wrapped_raw_sql_rows(dal_param: Any, sql: str, params: Any = None) -> Any:
        """Wrap raw_sql_rows to translate SQL before execution."""
        translated_sql = _translate_sql(sql)
        return await original_raw_sql_rows(dal_param, translated_sql, params)

    async def wrapped_raw_sql_write(dal_param: Any, sql: str, params: Any = None) -> Any:
        """Wrap raw_sql_write to translate SQL before execution."""
        translated_sql = _translate_sql(sql)
        return await original_raw_sql_write(dal_param, translated_sql, params)

    inventory_process.raw_sql_rows = wrapped_raw_sql_rows
    inventory_process.raw_sql_write = wrapped_raw_sql_write

    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE inventory_items ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, community_id INTEGER, name TEXT, "
                "item_type TEXT, quantity INTEGER, available_quantity INTEGER, "
                "metadata TEXT, deleted_at TEXT, created_at TEXT, updated_at TEXT)"
            )
        )
    await db.reflect()

    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()

    # Restore original functions
    inventory_process.raw_sql_rows = original_raw_sql_rows
    inventory_process.raw_sql_write = original_raw_sql_write

    await db.close()


def _event(text: str, *, actor: str | None = "penguinzplays") -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="message",
        actor=actor,
        payload={"text": text},
        occurred_at="2026-01-01T00:00:00+00:00",
    )


async def _run(dal: AsyncDB, text: str, *, community: str | None = "4") -> PlatformEvent | None:
    with bundle_context(tenant="acme", community=community, app_id="waddles.bot.twitch.default"):
        return await transform(_event(text))


class TestBareAndUnknown:
    async def test_bare_command_shows_usage(self, dal: AsyncDB) -> None:
        result = await _run(dal, "!inventory")
        assert result is not None
        assert result.payload["text"].startswith("Inventory commands:")

    async def test_unknown_subcommand(self, dal: AsyncDB) -> None:
        result = await _run(dal, "!inventory launch")
        assert result is not None
        assert "Unknown inventory command" in result.payload["text"]

    async def test_non_inventory_text_returns_none(self) -> None:
        assert await transform(_event("just chatting")) is None

    async def test_malformed_event_raises_value_error(self) -> None:
        event = PlatformEvent(
            platform="twitch", event_type="message", actor="p", payload={}, occurred_at="x"
        )
        with pytest.raises(ValueError, match="text"):
            await transform(event)


class TestAdd:
    async def test_add_new_item(self, dal: AsyncDB) -> None:
        result = await _run(dal, "!inventory add fishing-rod -t tool -o penguinzplays")
        assert result is not None
        assert result.payload["text"] == "\U0001f4e6 added 'fishing-rod' to inventory."

    async def test_add_without_name_shows_usage(self, dal: AsyncDB) -> None:
        result = await _run(dal, "!inventory add")
        assert result is not None
        assert result.payload["text"].startswith("Usage:")

    async def test_add_duplicate_item(self, dal: AsyncDB) -> None:
        await _run(dal, "!inventory add fishing-rod")
        result = await _run(dal, "!inventory add fishing-rod")
        assert result is not None
        assert result.payload["text"] == "'fishing-rod' already exists in inventory."

    async def test_add_missing_community_context_is_graceful(self, dal: AsyncDB) -> None:
        result = await _run(dal, "!inventory add fishing-rod", community=None)
        assert result is not None
        assert "community context" in result.payload["text"]

    async def test_add_db_failure_is_swallowed_gracefully(
        self, dal: AsyncDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GUARDED: a DB error never crashes the bot -- graceful reply instead."""
        from bundles import inventory_process

        async def mock_write(*args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated DB outage")

        monkeypatch.setattr(inventory_process, "raw_sql_write", mock_write)
        result = await _run(dal, "!inventory add fishing-rod")
        assert result is not None
        assert result.payload["text"] == "Error adding 'fishing-rod' to inventory."


class TestRemove:
    async def test_remove_existing_item(self, dal: AsyncDB) -> None:
        await _run(dal, "!inventory add fishing-rod")
        result = await _run(dal, "!inventory remove fishing-rod")
        assert result is not None
        assert result.payload["text"] == "\U0001f5d1 removed 'fishing-rod' from inventory."

    async def test_remove_missing_item(self, dal: AsyncDB) -> None:
        result = await _run(dal, "!inventory remove ghost-item")
        assert result is not None
        assert result.payload["text"] == "'ghost-item' not found in inventory."

    async def test_remove_without_name_shows_usage(self, dal: AsyncDB) -> None:
        result = await _run(dal, "!inventory remove")
        assert result is not None
        assert result.payload["text"].startswith("Usage:")

    async def test_remove_db_failure_is_swallowed_gracefully(
        self, dal: AsyncDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bundles import inventory_process

        async def mock_write(*args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated DB outage")

        monkeypatch.setattr(inventory_process, "raw_sql_write", mock_write)
        result = await _run(dal, "!inventory remove fishing-rod")
        assert result is not None
        assert result.payload["text"] == "Error removing 'fishing-rod' from inventory."


class TestList:
    async def test_list_empty(self, dal: AsyncDB) -> None:
        result = await _run(dal, "!inventory list")
        assert result is not None
        assert result.payload["text"].startswith("(no items yet")

    async def test_list_with_items_shows_owner_and_tags(self, dal: AsyncDB) -> None:
        await _run(dal, "!inventory add fishing-rod -t tool -o penguinzplays")
        result = await _run(dal, "!inventory list")
        assert result is not None
        text = result.payload["text"]
        assert "fishing-rod" in text
        assert "1/1 available" in text
        assert "owner: penguinzplays" in text
        assert "tags: tool" in text

    async def test_list_db_failure_is_swallowed_gracefully(
        self, dal: AsyncDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bundles import inventory_process

        async def mock_rows(*args: object, **kwargs: object) -> list[object]:
            raise RuntimeError("simulated DB outage")

        monkeypatch.setattr(inventory_process, "raw_sql_rows", mock_rows)
        result = await _run(dal, "!inventory list")
        assert result is not None
        assert result.payload["text"] == "Error listing inventory."


class TestCheckoutStub:
    @pytest.mark.parametrize("cmd", ["checkout", "checkin", "return"])
    async def test_checkout_family_returns_graceful_stub(self, dal: AsyncDB, cmd: str) -> None:
        """`checkout`/`checkin`/`return` are deferred -- an honest stub, never a DB write."""
        result = await _run(dal, f"!inventory {cmd} fishing-rod -T someone")
        assert result is not None
        assert "coming soon" in result.payload["text"].lower()
