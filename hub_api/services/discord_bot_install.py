"""The SaaS/global "Add Waddles to Discord" bot-install link.

Discord requires every listed app provider to expose a working install
link (`discord.com/oauth2/authorize?...&scope=bot...`) that adds the bot
to a server -- this is that link's builder, SEPARATE from the `identify
guilds` OAuth *login* flow (`services.oauth_providers.PROVIDERS["discord"]`)
and SEPARATE from the per-tenant custom-app install flow
(`services.discord_install_service` -- Bar Citizen tenants bringing their
OWN Discord application). This module always points at the ONE shared SaaS
Discord application tenant 0 (and every tenant without its own app) uses,
templated on the same `DISCORD_CLIENT_ID` env var `oauth_providers.py`
already resolves for login -- Discord does not distinguish an
"application id" from an OAuth2 "client id"; they are the same public
number (never a secret -- `client.md` Authentication & Tokens only
restricts the client SECRET/bot token, never the id that appears in every
browser address bar during install).

The requested `permissions` bitfield is derived from what this product's
Discord bot code actually does, not guessed -- see
`DISCORD_BOT_INSTALL_PERMISSIONS`'s own comment for the full
permission -> feature -> source-module mapping.
"""

from __future__ import annotations

import os
from urllib.parse import urlencode

from services.oauth_providers import ProviderNotConfigured

#: Same env var `oauth_providers.PROVIDERS["discord"].client_id_env`
#: resolves for the login flow -- one shared SaaS Discord application,
#: one client id, two different uses (login vs. bot-install).
_DISCORD_CLIENT_ID_ENV = "DISCORD_CLIENT_ID"

_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"

#: `bot` adds the application as a guild member with the permissions
#: below; `applications.commands` is required separately so Discord
#: registers/renders this app's slash commands to guild members (a bot
#: added without it can still call the REST API, but no slash command
#: surfaces in Discord's own UI). Distinct from the `identify guilds`
#: scope `oauth_providers.PROVIDERS["discord"]` uses for account login.
DISCORD_BOT_INSTALL_SCOPES = "bot applications.commands"

#: `integration_type=0` -- GUILD_INSTALL. Every Waddles Discord feature
#: (commands, role-sync, event-sync, message relay) operates in a guild
#: context; there is no per-user (`USER_INSTALL`, `integration_type=1`)
#: surface, so only the guild install context is requested.
DISCORD_BOT_INSTALL_INTEGRATION_TYPE = 0

#: Discord permissions bitfield requested at bot-install time -- the full
#: set this product's Discord code paths actually exercise, each bit named
#: so a future addition/removal is a one-line diff with its own rationale,
#: never a bare magic number:
#:
#:   bit value              permission            feature / source
#:   ----------------------  --------------------  ---------------------------
#:   2       (1<<1)          KICK_MEMBERS           discord_service.kick_user
#:                                                   (action/pushing/
#:                                                   discord_action_module --
#:                                                   moderation action)
#:   4       (1<<2)          BAN_MEMBERS            discord_service.ban_user
#:   64      (1<<6)          ADD_REACTIONS          discord_service.add_reaction
#:   1024    (1<<10)         VIEW_CHANNEL           see the channels the bot
#:                                                   posts/relays in
#:   2048    (1<<11)         SEND_MESSAGES          discord_send_action bundle
#:                                                   (core/svc_action, the
#:                                                   standard-bundle command
#:                                                   reply path) + legacy
#:                                                   discord_service.send_message
#:   8192    (1<<13)         MANAGE_MESSAGES        discord_service.delete_message
#:                                                   / edit_message (moderation)
#:   16384   (1<<14)         EMBED_LINKS            discord_service.send_embed
#:   65536   (1<<16)         READ_MESSAGE_HISTORY   command context; editing/
#:                                                   reacting to prior messages
#:   268435456 (1<<28)       MANAGE_ROLES           role-sync
#:                                                   (hub_api.services.
#:                                                   role_sync_service.
#:                                                   HttpDiscordRoleTargetClient)
#:                                                   + discord_service.manage_role
#:   536870912 (1<<29)       MANAGE_WEBHOOKS        discord_service.create_webhook
#:                                                   / send_webhook
#:   8589934592 (1<<33)      MANAGE_EVENTS          calendar event-sync guild
#:                                                   scheduled-events push
#:                                                   engine (HttpDiscordEvent
#:                                                   TargetClient --
#:                                                   feature/event-discord-sync,
#:                                                   not yet merged to this
#:                                                   release branch as of this
#:                                                   PR; included because it
#:                                                   ships imminently and a
#:                                                   stale install link would
#:                                                   otherwise need a user to
#:                                                   re-consent)
#:   1099511627776 (1<<40)   MODERATE_MEMBERS       discord_service.timeout_user
#:                                                   (moderation action)
#:
#: Deliberately NOT Administrator. Deliberately excludes CONNECT/SPEAK
#: (voice channel permissions) -- `core/svc_streaming/src/egress/
#: discord_voice.rs`'s `DiscordVoiceSink` is an explicit, fail-loud
#: `Unimplemented` scaffold today, not a shipped feature (no-stubs rule);
#: add voice permissions in the same PR that implements that sink.
DISCORD_BOT_INSTALL_PERMISSIONS = 1_108_906_961_990


def build_install_url() -> str:
    """Build the public "Add Waddles to Discord" bot-install URL.

    Raises `ProviderNotConfigured` (-> 503 `provider_not_configured` at the
    blueprint layer, same shape `blueprints.v1.community_connections`
    already returns for a misconfigured OAuth provider) when
    `DISCORD_CLIENT_ID` is unset -- fail loud, never render a
    placeholder/broken link.
    """
    client_id = os.getenv(_DISCORD_CLIENT_ID_ENV)
    if not client_id:
        raise ProviderNotConfigured(f"discord: missing {_DISCORD_CLIENT_ID_ENV}")

    params = {
        "client_id": client_id,
        "scope": DISCORD_BOT_INSTALL_SCOPES,
        "permissions": str(DISCORD_BOT_INSTALL_PERMISSIONS),
        "integration_type": str(DISCORD_BOT_INSTALL_INTEGRATION_TYPE),
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"
