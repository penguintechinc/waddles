"""Default adapter registry: the one place a provider is plugged into hub-api.

Adding a provider (Discord, Slack, Kick, YouTube ...) is exactly one line below plus its adapter
module -- the connection service, store, API and telemetry need no change. If adding a provider
means editing anything else, the framework boundary leaked and the framework should be fixed.
"""

from __future__ import annotations

import httpx

from services.provider_connections.adapter import AdapterRegistry
from services.provider_connections.discord import DiscordAdapter
from services.provider_connections.kick import KickAdapter
from services.provider_connections.spectrum import SpectrumAdapter
from services.provider_connections.static_secret import (
    AwsLambdaAdapter,
    ExternalHttpAdapter,
    GcpFunctionAdapter,
)
from services.provider_connections.twitch import TwitchAdapter
from services.provider_connections.wix import WixAdapter
from services.provider_connections.youtube import AppCredentials, YoutubeAdapter


def build_default_registry(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    youtube_app_credentials: AppCredentials | None = None,
) -> AdapterRegistry:
    """Registry holding every provider this build supports (``transport`` is for tests).

    ``youtube_app_credentials`` resolves a tenant's own Google OAuth application for token
    refresh (per-tenant bot credentials); without it a YouTube refresh fails loudly as
    ``refresh_not_configured`` instead of guessing a global app.
    """
    registry = AdapterRegistry()
    registry.register(TwitchAdapter(transport=transport))
    registry.register(DiscordAdapter(transport=transport))
    registry.register(YoutubeAdapter(transport=transport, app_credentials=youtube_app_credentials))
    registry.register(KickAdapter(transport=transport))
    registry.register(SpectrumAdapter(transport=transport))
    registry.register(WixAdapter())
    registry.register(ExternalHttpAdapter())
    registry.register(AwsLambdaAdapter())
    registry.register(GcpFunctionAdapter())
    return registry
