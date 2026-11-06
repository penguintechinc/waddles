"""Feature module: Wix pricing plan -> Discord role (`!wix enable roles`), default OFF.

Architecture: platform SERVICE bundles own the Discord tool definitions and this APP bundle only
calls them through `waddle_sdk.tool.invoke` under manifest `tool.call:` grants -- never direct
Discord HTTP. Tools this module will call: `discord.member.roles.add` / `discord.member.roles.remove`.
The trigger is the Wix webhook connector (separate work). Until the runtime provides the `tool`
import (a `stage-*-tools` build with the grants approved), enabling is REFUSED loudly, never a
silent no-op flag.
"""

from __future__ import annotations

TARGET_TOOLS = (
    "discord.member.roles.add",
    "discord.member.roles.remove",
)
#: The WIT import the tool-invocation path needs; absent in the current `stage-next` build.
REQUIRED_IMPORT = "tool"
MISSING_CAPABILITY_MSG = (
    "Wix roles sync can't be enabled: this runtime doesn't provide the tool-invocation "
    "capability for it yet."
)


def capability_available() -> bool:
    """True iff the runtime's `wit_world.imports` exposes the tool-invocation import."""
    try:
        import wit_world
    except ImportError:
        return False
    return getattr(wit_world.imports, REQUIRED_IMPORT, None) is not None
