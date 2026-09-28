"""License-tier lookup over the WIT ``%flags`` import (spec Sec6.5).

Sibling to ``flask_core.feature_flags.feature_enabled`` (the PostHog flag
half of the same host interface) -- this module exposes the other half,
``tier()``, for bundles that need to gate a feature on the caller's
license tier directly (e.g. an Enterprise-only capability) rather than a
boolean flag alone.

Binding shape confirmed via ``componentize-py bindings`` against the
committed ``wit/waddle-bundle/stage.wit``: ``tier() -> string``, one of
``"free"``/``"professional"``/``"enterprise"``.
"""

from __future__ import annotations

#: Safest default when the WIT binding is unavailable (host-side tests without a
#: compiled component) -- denies any tier-gated feature rather than fail-open.
_DEFAULT_TIER = "free"


async def tier() -> str:
    """Return the caller's resolved license tier.

    One of ``"free"``, ``"professional"``, or ``"enterprise"``. Falls back to
    :data:`_DEFAULT_TIER` when the generated ``wit_world`` binding is
    unavailable (host-side tests run with no compiled component) -- never raises.
    """
    try:
        import wit_world
    except ImportError:
        return _DEFAULT_TIER
    return str(wit_world.imports.flags.tier())
