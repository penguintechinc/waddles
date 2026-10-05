"""Per-community enable/disable state for a bundle's declared sub-modules -- default OFF.

Mirrors the platform-wide "features default OFF" rule
(`critical-rules.md` Feature Flags & License Tiers) one level down: a
bundle's own named sub-modules (e.g. `!shoutout`'s `auto`/`ai`) start
disabled for every community until an admin opts in with `!<cmd> enable
<sub-module>` (`waddle_sdk.command.parse_command`'s toggle shape). State is
community-scoped kv (`waddle_sdk.community_kv`), never global/tenant, so
one community enabling a sub-module never affects another.
"""

from __future__ import annotations

from dataclasses import dataclass

from waddle_sdk import community_kv
from waddle_sdk.command import CommandUsageError, ParsedCommand

_ENABLED = b"1"


@dataclass(slots=True, frozen=True)
class SubModuleGate:
    """Community-scoped enable/disable checks for one command's sub-modules.

    `command` namespaces the stored kv keys so two different commands can
    each declare a sub-module of the same name without colliding.
    """

    command: str

    def _key(self, sub_module: str) -> str:
        return f"submodule:{self.command}:{sub_module}"

    async def is_enabled(self, community_id: str, sub_module: str) -> bool:
        """Default OFF -- `True` only once an admin has explicitly enabled `sub_module`."""
        return await community_kv.get(community_id, self._key(sub_module)) == _ENABLED

    async def enable(self, community_id: str, sub_module: str) -> None:
        """Persist `sub_module` as enabled for `community_id` (no expiry)."""
        await community_kv.set(community_id, self._key(sub_module), _ENABLED)

    async def disable(self, community_id: str, sub_module: str) -> None:
        """Clear `sub_module`'s enabled state for `community_id` -- back to default OFF."""
        await community_kv.delete(community_id, self._key(sub_module))

    async def apply_toggle(self, community_id: str, parsed: ParsedCommand) -> bool | None:
        """Apply `parsed.option` if it's `enable`/`disable`; return the new state, else `None`.

        Lets a bundle call this unconditionally on every parse and fall
        through to its own option handling when the result is `None` (a
        non-toggle command). Raises `CommandUsageError` if `parsed.option`
        is a toggle but `parsed.sub_module` is unset -- defensive;
        `parse_command()` already guarantees this never happens for its
        own output.
        """
        if parsed.option not in ("enable", "disable"):
            return None
        if parsed.sub_module is None:
            raise CommandUsageError(f"!{parsed.command} {parsed.option}: no sub-module to toggle")
        if parsed.option == "enable":
            await self.enable(community_id, parsed.sub_module)
            return True
        await self.disable(community_id, parsed.sub_module)
        return False
