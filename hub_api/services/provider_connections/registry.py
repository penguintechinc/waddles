"""Default adapter registry: the one place a provider is plugged into hub-api.

Adding a provider (Discord, Slack, Kick, YouTube ...) is exactly one line below plus its adapter
module -- the connection service, store, API and telemetry need no change. If adding a provider
means editing anything else, the framework boundary leaked and the framework should be fixed.
"""

from __future__ import annotations

import httpx

from services.provider_connections.adapter import AdapterRegistry
from services.provider_connections.static_secret import (
    AwsLambdaAdapter,
    ExternalHttpAdapter,
    GcpFunctionAdapter,
)
from services.provider_connections.twitch import TwitchAdapter


def build_default_registry(*, transport: httpx.AsyncBaseTransport | None = None) -> AdapterRegistry:
    """Registry holding every provider this build supports (``transport`` is for tests)."""
    registry = AdapterRegistry()
    registry.register(TwitchAdapter(transport=transport))
    registry.register(ExternalHttpAdapter())
    registry.register(AwsLambdaAdapter())
    registry.register(GcpFunctionAdapter())
    return registry
