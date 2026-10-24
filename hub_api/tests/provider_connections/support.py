"""Shared helpers for the connection-framework tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import text

from services.platform_integrations_crypto import platform_connection_aad
from services.provider_connections.service import ConnectionView, ProviderConnectionService
from services.provider_connections.vault import PlatformKeyVault
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


def row_aad(row: Mapping[str, Any]) -> bytes:
    """The AAD a raw ``platform_connections`` row's tokens are bound to (public_id + platform)."""
    return platform_connection_aad(public_id=UUID(str(row["public_id"])), platform=row["platform"])


def open_row(row: Mapping[str, Any], column: str = "access_token") -> str:
    """Open one raw row's sealed column through the versioned vault, exactly as the service does."""
    return PlatformKeyVault().open(
        row[column], aad=row_aad(row), seal_version=int(row["seal_version"])
    )


def seal_for_row(row: Mapping[str, Any], plaintext: str) -> str:
    """Seal ``plaintext`` as if the service had written it to this raw row."""
    return PlatformKeyVault().seal(plaintext, aad=row_aad(row))


async def create_static(
    service: ProviderConnectionService,
    db: Any,
    *,
    provider: str = "external_http",
    scheme: int = 1,
    document: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    community_id: int | None = None,
    tenant_id: int | None = None,
    enabled: bool = True,
) -> ConnectionView:
    """Create a static-secret connection (default: external_http + bearer) for ``acme-corp``."""
    return await service.create_static(
        tenant_id=tenant_id if tenant_id is not None else db.seed.tenant_id,
        community_id=community_id if community_id is not None else db.seed.community_id,
        actor_user_id=db.seed.admin_user_id,
        provider=provider,
        secret_scheme=scheme,
        secret_document=document if document is not None else {"token": STATIC_BEARER},
        metadata=metadata if metadata is not None else {"url": STATIC_URL},
        enabled=enabled,
    )


#: The plaintext secret the static-secret tests seal -- every test asserts it never leaks.
STATIC_BEARER = "STATIC-SECRET-bearer-0123456789"
STATIC_URL = "https://hooks.example.com/waddles"
