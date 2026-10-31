"""Superadmin platform-config business logic -- ported from `platformConfigController.js`.

Only the subset mounted under `requireSuperAdmin` in Node's
`routes/superadmin.js` (`GET /platform-config`, `PUT /platform-config/
:platform`, `POST /platform-config/:platform/test`, `GET`/`PUT
/settings`) -- `platformConfigController.js`'s other static methods
(`getBotCredentials`, `get/create/update/deleteMyCredential`,
`get/create/update/deleteCommunityCredential`, `testCredential` by row
id) are mounted in `routes/user.js` (self-service) and `routes/admin.js`
(community-scoped), owned by the Identity (M1, self-service oauth) and
Platform-admin/`adminController` (community admin) groups respectively
-- not ported here (migration plan's own controller table lists
`adminController` as a SEPARATE Platform-admin-area controller from
`platformConfigController`; this M3 PR is scoped to the three named
controllers only). `hub_settings` is schema-owned by this group (see
`services/schema.py`'s own note); read access is shared with
`auth_service.get_hub_settings_map()` (register()/resend_verification()
need signup_enabled/email_configured), reused here rather than
duplicated.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from services import credential_manager_client
from services import platform_integrations_store as store
from services.auth_service import get_hub_settings_map
from services.errors import bad_request, not_found

#: `platform_integrations.integration_type` values (030_platform_integrations.sql scope CHECK).
_INTEGRATION_TYPES = frozenset({"bot", "user_oauth", "community_oauth"})


@dataclass(slots=True, frozen=True)
class CredentialRow:
    """Shaped view of a `platform_integrations` row -- metadata only, no token/secret value."""

    id: int
    platform: str
    integration_type: str
    community_id: int | None
    user_id: int | None
    access_token_set: bool
    refresh_token_set: bool
    client_id: str | None
    client_secret_set: bool
    token_type: str | None
    expires_at: Any
    scopes: list[str] = field(default_factory=list)
    config_data: dict[str, Any] | None = None
    is_active: bool = True
    is_encrypted: bool = True
    created_at: Any = None
    updated_at: Any = None
    created_by_user_id: int | None = None
    updated_by_user_id: int | None = None


def _from_meta(row: store.MetaRow) -> CredentialRow:
    """Map a `MetaRow` (presence flags only -- never a token/secret value) to the DTO."""
    return CredentialRow(
        id=row.id,
        platform=row.platform,
        integration_type=row.integration_type,
        community_id=row.community_id,
        user_id=row.user_id,
        access_token_set=row.access_token_set,
        refresh_token_set=row.refresh_token_set,
        client_id=row.client_id,
        client_secret_set=row.client_secret_set,
        token_type=row.token_type,
        expires_at=row.expires_at,
        scopes=list(row.scopes),
        config_data=row.config_data,
        is_active=row.is_active,
        is_encrypted=row.is_encrypted,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by_user_id=row.created_by_user_id,
        updated_by_user_id=row.updated_by_user_id,
    )


async def get_platform_configs(
    async_dal: Any, dal: Any, *, integration_type: str | None, platform: str | None
) -> list[CredentialRow]:
    """List active platform integrations (metadata only), optionally filtered by type/platform.

    `waddles_hub_api` is denied the table (H-3); this reads via `pi_list_metadata()`, whose return
    type has no token/secret column (gh-969).
    """
    if integration_type is not None and integration_type not in _INTEGRATION_TYPES:
        raise bad_request(f"unsupported integrationType '{integration_type}'")
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(
        async_dal.executor,
        lambda: store.list_metadata(dal, integration_type=integration_type, platform=platform),
    )
    return [_from_meta(r) for r in rows]


async def update_platform_config(async_dal: Any, dal: Any, *, platform: str) -> None:
    """Update a bot credential by platform name.

    Node's own route (`superadmin.js`: `PUT /platform-config/:platform`)
    calls `PlatformConfigController.updatePlatformConfig`, which
    destructures `const { id } = req.params` -- but the route param is
    named `:platform`, not `:id`, so `id` is always `undefined`. The
    resulting `UPDATE platform_integrations ... WHERE id = $1` with
    `$1 = undefined` never matches a row (`id` is a `SERIAL PRIMARY KEY`,
    never NULL) -- this endpoint 404s "Credential not found" for every
    call, in Node, today, regardless of the request body. A faithful port
    reproduces exactly that outcome rather than guessing at a fix for a
    confusingly-underspecified feature (the route's own body validators
    check `client_id`/`client_secret`/`redirect_uri`/`enabled`, but the
    controller reads `clientId`/`clientSecret`/`accessToken`/... --
    disjoint field names -- and the one webui page that calls this
    endpoint, `BotCredentialTab.jsx`, reads `response.data.configs`,
    which doesn't exist on `getPlatformConfigs()`'s real `{success, data,
    count}` response shape either). Three independent, pre-existing
    contract breaks in one feature -- flagged for product/design
    follow-up, not silently patched over with an invented, unverifiable
    semantics here.
    """
    raise not_found("Credential not found")


async def test_platform_connection(
    platform: str, *, bearer_token: str | None
) -> tuple[bool, str | None]:
    """Validate a platform's operator bot credential -- delegated to the credential manager.

    hub-api cannot read operator bot tokens (H-3, gh-969), so the decrypted token never reaches
    this process. An unreachable credential manager is an explicit 503, never a silent pass.
    """
    return await credential_manager_client.test_bot_connection(platform, bearer_token=bearer_token)


async def get_hub_settings(async_dal: Any, dal: Any) -> dict[str, str]:
    """Get every `hub_settings` key/value pair."""
    return await get_hub_settings_map(async_dal, dal)


async def update_hub_settings(
    async_dal: Any, dal: Any, *, updates: dict[str, Any]
) -> dict[str, str]:
    """Upsert `hub_settings` key/value pairs, return the full resulting map."""
    for key, value in updates.items():
        existing = await async_dal.select_async(dal(dal.hub_settings.setting_key == key))
        if existing:
            await async_dal.update_async(
                dal.hub_settings.setting_key == key,
                setting_value=str(value),
                updated_at=datetime.now(UTC),
            )
        else:
            await async_dal.insert_async(
                dal.hub_settings,
                setting_key=key,
                setting_value=str(value),
                updated_at=datetime.now(UTC),
            )
    return await get_hub_settings_map(async_dal, dal)
