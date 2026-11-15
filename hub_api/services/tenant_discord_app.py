"""Tenant-owned Discord app -> data-plane connection glue.

Our WaddleBot Discord application serves ONLY the default tenant. A private tenant brings its
own application (id / client secret / bot token); after its install completes, this module makes
the tenant's bot visible to the data plane in exactly the shape hub-api's connection resolver
serves to svc-ingest (`core/svc_ingest/src/outbound_creds_discord.rs`):

* one tenant-scoped `platform_connections` row: `platform='discord'`,
  `resource_type='discord_guild'`, `resource_id=<guild snowflake>`, the BOT TOKEN sealed (AAD-bound)
  in `access_token`, `connection_kind=1` (platform OAuth, DB default), `status='active'`;
* non-secret `resource_metadata`: `guild_id`, `application_id`, `bot_user_id`, `scopes=[]`,
  `capabilities=[chat_ingest, action_relay]`, `tenant_id`;
* a `community_connection_access` grant (approved + enabled) so the data-plane binding list
  serves it -- written automatically only when the tenant has exactly ONE community (otherwise
  the grant is a deliberate admin step and status reports `awaiting_community_grant`).

There is no env or shared-app fallback anywhere in here: no installed app => fail loud
(`TENANT_DISCORD_APP_MISSING`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from services.credential_resolver import (
    PLATFORM_DISCORD,
    grant_community_connection_access,
    tenant_public_id,
    upsert_platform_connection,
)
from services.provider_connections.discord import (
    CAPABILITY_ACTION_RELAY,
    CAPABILITY_CHAT_INGEST,
    RESOURCE_TYPE_GUILD,
)
from services.schema import bind_guild_pairing_tables

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("waddles.hub_api.tenant_discord_app")


@dataclass(slots=True, frozen=True)
class ConnectionWriteResult:
    """Outcome of writing the tenant's Discord connection -- ids only, never a secret."""

    connection_id: int
    granted_community_id: int | None


@dataclass(slots=True, frozen=True)
class InstallStatus:
    """Install status for the webui. Deliberately carries no secret/token field at all."""

    installed: bool
    application_id: str | None
    connection_status: str
    guild_id: str | None
    updated_at: str | None


def ensure_tenant_discord_connection(
    dal: Any,
    *,
    tenant_id: int,
    application_id: str,
    bot_token: str,
    guild_id: str,
    installed_by_user_id: int | None,
) -> ConnectionWriteResult:
    """Idempotently upsert the tenant's discord `platform_connections` row (+ sole-community grant).

    Never called for the default tenant (the install flow rejects it first). Raises on any DB
    failure -- a half-installed tenant must be loud, not silent.
    """
    public_id = tenant_public_id(dal, tenant_id)
    with _tracer.start_as_current_span("tenant_discord_app.write_connection") as span:
        span.set_attribute("tenant.public_id", public_id)
        try:
            connection_id = upsert_platform_connection(
                dal,
                tenant_id=tenant_id,
                platform=PLATFORM_DISCORD,
                resource_type=RESOURCE_TYPE_GUILD,
                resource_id=guild_id,
                access_token=bot_token,
                refresh_token=None,
                installed_by_user_id=installed_by_user_id,
                resource_metadata={
                    "guild_id": guild_id,
                    "application_id": application_id,
                    "bot_user_id": application_id,
                    "scopes": [],
                    "capabilities": [CAPABILITY_CHAT_INGEST, CAPABILITY_ACTION_RELAY],
                    "tenant_id": tenant_id,
                },
            )
            granted = _grant_sole_community(dal, tenant_id, connection_id, installed_by_user_id)
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR))
            span.record_exception(type(exc)("redacted"))
            logger.error(
                "tenant_discord_app.write_connection_failed tenant=%s error=%s",
                public_id,
                type(exc).__name__,
            )
            raise
        span.set_attribute("connection.granted", granted is not None)
        logger.info(
            "tenant_discord_app.connection_written tenant=%s connection_id=%d granted=%s",
            public_id,
            connection_id,
            granted is not None,
        )
        return ConnectionWriteResult(connection_id=connection_id, granted_community_id=granted)


def _grant_sole_community(
    dal: Any, tenant_id: int, connection_id: int, user_id: int | None
) -> int | None:
    """Approve+enable the grant when the tenant has exactly one community; else an admin does."""
    bind_guild_pairing_tables(dal)
    communities = dal(dal.communities.tenant_id == tenant_id).select(dal.communities.id)
    ids = [int(c.id) for c in communities]
    if len(ids) != 1:
        return None
    grant_community_connection_access(
        dal,
        community_id=ids[0],
        connection_id=connection_id,
        status="approved",
        requested_by_user_id=user_id,
        approved_by_user_id=user_id,
    )
    return ids[0]


def get_install_status(dal: Any, *, tenant_id: int) -> InstallStatus:
    """Read-only install status for `tenant_id`'s Discord app. Never selects a secret column."""
    bind_guild_pairing_tables(dal)
    apps = dal.tenant_platform_apps
    app_row = (
        dal((apps.tenant_id == tenant_id) & (apps.platform == PLATFORM_DISCORD))
        .select(apps.id, apps.updated_at)
        .first()
    )
    if app_row is None:
        return InstallStatus(False, None, "not_installed", None, None)

    conn = dal.platform_connections
    row = (
        dal((conn.tenant_id == tenant_id) & (conn.platform == PLATFORM_DISCORD))
        .select(
            conn.id,
            conn.status,
            conn.resource_id,
            conn.resource_metadata,
            conn.updated_at,
            orderby=~conn.updated_at,
        )
        .first()
    )
    updated = app_row.updated_at
    if row is None:
        return InstallStatus(
            True, None, "no_connection", None, updated.isoformat() if updated else None
        )

    meta = row.resource_metadata if isinstance(row.resource_metadata, dict) else {}
    status = str(row.status)
    if status == "active":
        access = dal.community_connection_access
        granted = dal(
            (access.connection_id == row.id)
            & (access.status == "approved")
            & (access.enabled == True)  # noqa: E712 - pydal expression, not a Python comparison
        ).count()
        if granted == 0:
            status = "awaiting_community_grant"
    newest = max([t for t in (updated, row.updated_at) if t is not None], default=None)
    return InstallStatus(
        installed=True,
        application_id=str(meta.get("application_id") or "") or None,
        connection_status=status,
        guild_id=str(row.resource_id),
        updated_at=newest.isoformat() if newest else None,
    )
