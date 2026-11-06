"""Feature module: Wix events -> community/Discord events (`!wix enable events`), default OFF.

Architecture: platform SERVICE bundles own the Discord tool definitions and this APP bundle only
calls them through `waddle_sdk.tool.invoke` under manifest `tool.call:` grants -- never direct
Discord HTTP. Tools this module will call: `discord.scheduled_event.create`.
The trigger is the Wix webhook connector (separate work). Until the runtime provides the `tool`
import (a `stage-*-tools` build with the grants approved), enabling is REFUSED loudly, never a
silent no-op flag.
"""

from __future__ import annotations

TARGET_TOOLS = ("discord.scheduled_event.create",)
#: The WIT import the tool-invocation path needs; absent in the current `stage-next` build.
REQUIRED_IMPORT = "tool"
MISSING_CAPABILITY_MSG = (
    "Wix events sync can't be enabled: this runtime doesn't provide the tool-invocation "
    "capability for it yet."
)


def capability_available() -> bool:
    """True iff the runtime's `wit_world.imports` exposes the tool-invocation import."""
    # The capability probe's contract is a boolean: callers refuse enabling loudly with
    # MISSING_CAPABILITY_MSG when it is False. `log` itself needs `wit_world`, so it cannot be
    # used here -- absence of the import IS the signal being reported, not a swallowed error.
    try:
        import wit_world
    except ImportError:
        wit_world = None
    if wit_world is None:
        return False
    return getattr(wit_world.imports, REQUIRED_IMPORT, None) is not None
