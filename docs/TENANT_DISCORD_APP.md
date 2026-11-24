# Tenant-owned Discord app

Our WaddleBot Discord application serves **only the default tenant**. A private tenant brings its
own Discord app (application id, client secret, bot token). A non-default tenant never uses our
app or any `DISCORD_*` env credential; with no app installed, hub-api fails loud.

## Flow

1. `POST /api/v1/tenants/discord/install/authorize` (scope `tenant.discord_install:write`, flag
   `waddles.tenant.discord_install`; default tenant rejected). Body:
   `{application_id, client_secret, bot_token}` -- all three required. Returns the Discord
   authorize URL and sets the `__Host-` state-binding cookie.
2. Discord redirects the admin's browser to `GET <identity_callback_base_url>/api/v1/discord/install/callback`
   (public; the single-use `state` is the gate). `identity_callback_base_url` is
   `HubAPIConfig.identity_callback_base_url`.
3. `complete_install` verifies state, exchanges the code with the tenant's own app, validates the
   bot token against Discord, seals the app credentials into `tenant_platform_apps`, then upserts
   the connection (below). Idempotent on re-install; a new token replaces the old one.

## Connection row (what the data plane reads)

`platform_connections`, tenant-scoped, one per `(tenant, 'discord', guild)`:

| column | value |
|---|---|
| `platform` / `resource_type` | `discord` / `discord_guild` |
| `resource_id` | guild snowflake (Discord's token response `guild.id`, else callback `guild_id`) |
| `access_token` | **bot token**, AAD-sealed (never plaintext at rest); `seal_version=1` |
| `connection_kind` | `1` (platform OAuth; DB default) |
| `status` | `active` |
| `resource_metadata` | `guild_id`, `application_id`, `bot_user_id`, `scopes: []`, `capabilities: [chat_ingest, action_relay]`, `tenant_id` |

hub-api serves it to svc-ingest via the provider-connections binding list/resolve (metadata
`guild_id`, `application_id`, `managed_roles`; bot token as the kind-1 `accessToken`). A
`community_connection_access` grant (approved + enabled) is required for it to be listed; it is
created automatically when the tenant has exactly one community, otherwise an admin grants it
(status shows `awaiting_community_grant`).

## Fail-loud

`DefaultCredentialResolver.resolve()` for a non-default tenant is DB-only. Missing/incomplete app
raises `TenantDiscordAppMissing` (`TENANT_DISCORD_APP_MISSING`) and increments
`waddles_tenant_discord_app_missing_total{tenant=<public id>, reason}`. Install writes emit a span
`tenant_discord_app.write_connection` and an INFO log (tenant public id only).

## Status endpoint

`GET /api/v1/tenants/discord/install/status` (scope `tenant.discord_install:read`):
`{success, installed, application_id, connection_status, guild_id, updated_at, redirect_uri}`;
`connection_status` is `not_installed | no_connection | awaiting_community_grant | active | ...`.
Never contains a secret or token.

## Flags

`waddles.tenant.discord_install` (gates authorize; exposed to the webui via `/flags`).
