"""v1 public `discord.bot_install` route -- the SaaS/global bot-install link.

Discord requires a valid/installable app provider to expose a working
"add this bot to your server" link; this blueprint is the ONE route the
webui needs to render that link without holding the Discord client id (or
any permissions-bitfield knowledge) itself -- `services.discord_bot_
install.build_install_url()` owns both. PRE-AUTH, mounted under
`/api/v1/public`, same posture as `blueprints.v1.public` (no JWT exists at
this point in a visitor's session -- `identify_callback_base_url`/
`public.py`'s own module docstring). SEPARATE from the per-tenant custom
Discord app install flow (`blueprints.v1.tenant_discord_install` --
authenticated, tenant-scoped, a different Discord application per tenant).

Matches the discovery contract every v1 port group follows: a module-level
`BLUEPRINTS: list[Blueprint]`, found and mounted by auto-discovery -- no
edit to `routers/v1.py`/`blueprints/__init__.py` needed.
"""

from __future__ import annotations

from dataclasses import dataclass

from quart import Blueprint
from quart_schema import validate_response

from services.discord_bot_install import build_install_url
from services.oauth_providers import ProviderNotConfigured

discord_bot_install_bp = Blueprint("v1_discord_bot_install", __name__, url_prefix="/api/v1/public")


@dataclass(slots=True, frozen=True)
class BotInstallUrlResponse:
    """`installUrl` envelope -- the Discord client id is public, but never a secret appears here."""

    success: bool
    installUrl: str


@discord_bot_install_bp.route("/discord/bot-install", methods=["GET"])
@validate_response(BotInstallUrlResponse)
async def get_bot_install_url() -> BotInstallUrlResponse | tuple[dict[str, object], int]:
    """`GET /api/v1/public/discord/bot-install` -- PUBLIC, no auth decorator.

    503 `provider_not_configured` (same shape `blueprints.v1.community_
    connections` already returns for any other misconfigured OAuth
    provider) when `DISCORD_CLIENT_ID` is unset in this deployment --
    never a silently-broken or placeholder install link.
    """
    try:
        install_url = build_install_url()
    except ProviderNotConfigured:
        return {"error": "provider_not_configured", "provider": "discord"}, 503

    return BotInstallUrlResponse(success=True, installUrl=install_url)


BLUEPRINTS: list[Blueprint] = [discord_bot_install_bp]
