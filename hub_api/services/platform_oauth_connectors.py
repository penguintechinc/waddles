"""Pluggable per-platform OAuth connect-flow registry (owner clarification, 2026-09-29).

Credential storage/rotation/resolution (`services/tenant_platform_credentials_service.py`,
`services/guild_credential_resolution.py`) is fully platform-generic --
every tenant other than the global tenant needs its own app integration
for every platform, not only Discord. The OAuth *connect flow* itself
(building an authorize URL, exchanging a code, and activating whatever
platform-specific "this tenant now controls this external resource"
record results) is necessarily platform-shaped: Discord's flow activates
a `guild_tenant_pairings` row (one Discord app install per guild, contract
Sec2); a future Twitch/YouTube/Kick/Slack/Teams connector would activate
a different table shaped around that platform's own resource model (a
Twitch channel, a Slack workspace, ...), none of which exist yet.

This module is therefore a thin **registry + status table**, not a
uniform implementation: `CONNECTORS` records which platforms have a
working connect flow today vs. storage-only, so a caller (blueprint,
future data-plane onboarding UI) can ask "can a tenant self-serve connect
this platform yet?" without hardcoding a platform list in multiple
places. Adding a new platform's connect flow means adding its own
`*_oauth_install_service.py` module (following
`guild_oauth_install_service.py`'s shape) and registering it here --
this module never needs a shared `Protocol` beyond
`ConnectFlowStatus` below, since each connector's actual activation
target is a different table/return shape by design.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.tenant_platform_credentials_service import SUPPORTED_PLATFORMS


@dataclass(slots=True, frozen=True)
class ConnectFlowStatus:
    """Whether `platform` has a working self-serve OAuth connect flow today."""

    platform: str
    storage_supported: bool
    connect_flow_implemented: bool
    connect_flow_module: str | None


#: One entry per `SUPPORTED_PLATFORMS` member -- kept in sync by
#: `tests/test_guild_pairing_oauth.py::test_supported_platforms_match_connector_registry`.
CONNECTORS: dict[str, ConnectFlowStatus] = {
    "discord": ConnectFlowStatus(
        platform="discord",
        storage_supported=True,
        connect_flow_implemented=True,
        connect_flow_module="services.guild_oauth_install_service",
    ),
    "twitch": ConnectFlowStatus("twitch", True, False, None),
    "youtube": ConnectFlowStatus("youtube", True, False, None),
    "kick": ConnectFlowStatus("kick", True, False, None),
    "slack": ConnectFlowStatus("slack", True, False, None),
    "teams": ConnectFlowStatus("teams", True, False, None),
    "mattermost": ConnectFlowStatus("mattermost", True, False, None),
}


def get_connect_flow_status(platform: str) -> ConnectFlowStatus:
    """Look up `platform`'s connect-flow status. Raises `ValueError` for an unknown platform."""
    status = CONNECTORS.get(platform)
    if status is None:
        raise ValueError(f"unknown platform {platform!r}")
    return status


if set(CONNECTORS) != SUPPORTED_PLATFORMS:
    raise RuntimeError(
        "platform_oauth_connectors.CONNECTORS must stay in sync with "
        "tenant_platform_credentials_service.SUPPORTED_PLATFORMS"
    )
