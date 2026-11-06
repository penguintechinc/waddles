"""Connector host selector for the Python svc-ingest (spec 2026-11-05 section 5).

Mirror of the Rust `connector_runtime::selector`: both services read the SAME
env baselines (`CONNECTOR_{TWITCH,DISCORD,YOUTUBE,WIX}_ENABLED`, set from one helm
value) so they cannot disagree about which implementation owns a platform.

Never reads the PostHog / licensing flag client: the `*.waddles.app` domain
bypass makes that client answer every flag ON, which would flip every
environment at once. Env baseline only, default OFF, strict boolean parsing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# Mirrors the Rust const `connector_runtime::selector::IDENTIFY_BUDGET_READY`:
# True now the Discord IDENTIFY budget is wired into the runtime. While False,
# a Discord connector ON is fatal.
IDENTIFY_BUDGET_READY = True

PLATFORMS = ("twitch", "discord", "youtube", "wix")
WEBHOOK_CAPABLE = ("twitch", "discord", "wix")


class ConnectorConfigError(Exception):
    """A selector setting is malformed or blocked; names the item, never a value."""


def _parse_bool(environ: Mapping[str, str], name: str) -> bool:
    """Strict boolean env parse: unset is OFF; only true/false/1/0 are accepted."""
    if name not in environ:
        return False
    raw = environ[name].strip().lower()
    if raw in ("true", "1"):
        return True
    if raw in ("false", "0"):
        return False
    raise ConnectorConfigError(f"{name} is malformed")


def parse_webhook_platforms(raw: str) -> tuple[str, ...]:
    """Parse `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS` (csv); unknown entries are fatal."""
    if not raw.strip():
        return ()
    out: list[str] = []
    for part in raw.split(","):
        platform = part.strip().lower()
        if platform not in WEBHOOK_CAPABLE:
            raise ConnectorConfigError("CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS is malformed")
        if platform not in out:
            out.append(platform)
    return tuple(out)


@dataclass(frozen=True)
class ConnectorSelector:
    """Which platforms run on the connector runtime instead of the Python receivers."""

    twitch: bool = False
    discord: bool = False
    youtube: bool = False
    wix: bool = False
    tools_enabled: bool = False
    webhook_platforms: tuple[str, ...] = ()

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
        *,
        identify_budget_ready: bool | None = None,
    ) -> ConnectorSelector:
        """Read the baselines; malformed is fatal; Discord ON is fatal until the budget lands."""
        ready = IDENTIFY_BUDGET_READY if identify_budget_ready is None else identify_budget_ready
        sel = cls(
            twitch=_parse_bool(environ, "CONNECTOR_TWITCH_ENABLED"),
            discord=_parse_bool(environ, "CONNECTOR_DISCORD_ENABLED"),
            youtube=_parse_bool(environ, "CONNECTOR_YOUTUBE_ENABLED"),
            wix=_parse_bool(environ, "CONNECTOR_WIX_ENABLED"),
            tools_enabled=_parse_bool(environ, "CONNECTOR_TOOLS_ENABLED"),
            webhook_platforms=parse_webhook_platforms(
                environ.get("CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS", "")
            ),
        )
        if sel.discord and not ready:
            raise ConnectorConfigError(
                "discord connector requires the identify budget, not yet ported"
            )
        return sel

    def connector(self, platform: str) -> bool:
        """True when the connector path owns `platform` (unknown platforms: False)."""
        return bool(getattr(self, platform, False)) if platform in PLATFORMS else False

    def legacy(self, platform: str) -> bool:
        """True when the Python receivers own `platform`."""
        return not self.connector(platform)

    @property
    def twitch_outbound_legacy(self) -> bool:
        """Legacy Twitch outbound drain runs unless the executor path replaces it."""
        return not (self.twitch and self.tools_enabled)

    @property
    def twitch_eventsub_legacy(self) -> bool:
        """Legacy EventSub handler stays unless the runtime owns Twitch webhooks."""
        return not (self.twitch and "twitch" in self.webhook_platforms)
