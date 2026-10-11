"""Shared helpers for the connection-framework tests."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from services.provider_connections.service import ConnectionView, ProviderConnectionService
from tests.provider_connections.conftest import CHANNEL_LOGIN


async def create_twitch(
    service: ProviderConnectionService,
    db: Any,
    tokens: tuple[str, str | None],
    *,
    community_id: int | None = None,
    tenant_id: int | None = None,
    channel: str = CHANNEL_LOGIN,
    enabled: bool = True,
    **overrides: Any,
) -> ConnectionView:
    """Create a Twitch connection for ``acme-corp``'s main community unless told otherwise."""
    params: dict[str, Any] = {
        "tenant_id": tenant_id if tenant_id is not None else db.seed.tenant_id,
        "community_id": community_id if community_id is not None else db.seed.community_id,
        "actor_user_id": db.seed.admin_user_id,
        "provider": "twitch",
        "access_token": tokens[0],
        "refresh_token": tokens[1],
        "metadata": {"channel_login": channel},
        "enabled": enabled,
    }
    params.update(overrides)
    return await service.create(**params)


async def raw_rows(db: Any, table: str) -> list[dict[str, Any]]:
    """Every row of ``table`` as plain dicts, read straight from the database."""
    async with db.install_dal.engine.connect() as conn:
        result = await conn.execute(text(f"SELECT * FROM {table} ORDER BY id"))  # noqa: S608 - fixed test table names
        return [dict(row._mapping) for row in result]


async def grant_to(
    db: Any, *, community_id: int, connection_id: int, status: str = "approved"
) -> None:
    """Insert a layer-3 grant for another community (the reuse/approval path's end state)."""
    async with db.install_dal.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO community_connection_access (community_id, connection_id, status, enabled)"
                " VALUES (:c, :k, :s, 1)"
            ),
            {"c": community_id, "k": connection_id, "s": status},
        )


async def set_column(db: Any, table: str, row_id: int, **values: Any) -> None:
    """Force columns on one row (simulating state another worker/replica produced)."""
    assignments = ", ".join(f"{name} = :{name}" for name in values)
    async with db.install_dal.engine.begin() as conn:
        await conn.execute(
            text(f"UPDATE {table} SET {assignments} WHERE id = :_id"),  # noqa: S608 - fixed test table names
            {**values, "_id": row_id},
        )
