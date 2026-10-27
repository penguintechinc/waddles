"""Standard provider/connection framework (hub-api control plane).

One adapter interface, one connection API and one encrypted per-community store for every external
platform Waddles talks to (Twitch first; Discord, Slack, Kick, YouTube next). See
``docs/guides/provider-connections.md`` for the operator/developer guide and
``docs/CONNECTION_MODEL.local.md`` for the three-layer storage model it builds on.

Module map (each file has one job, and ``adapter``/``errors``/``vault`` carry no Waddles-specific
coupling so the package can move into ``penguin-aaa`` later):

* ``adapter``   -- :class:`AdapterSpec` / :class:`ProviderAdapter` contract, metadata validation
* ``twitch``    -- the first adapter (bot account + channel, refresh via the platform Twitch app)
* ``registry``  -- provider id -> adapter; the single wiring point
* ``vault``     -- the only code that encrypts or decrypts a connection secret
* ``store``     -- penguin-dal persistence over the layer-2/3 connection tables
* ``service``   -- orchestration: create / update / delete / data-plane list + resolve
* ``telemetry`` -- OTel metrics + spans and PII-safe failure logging
"""

from __future__ import annotations

from services.provider_connections.adapter import AdapterRegistry, AdapterSpec, ProviderAdapter
from services.provider_connections.service import ProviderConnectionService
from services.provider_connections.vault import PlatformKeyVault, TokenVault

__all__ = [
    "AdapterRegistry",
    "AdapterSpec",
    "PlatformKeyVault",
    "ProviderAdapter",
    "ProviderConnectionService",
    "TokenVault",
]
