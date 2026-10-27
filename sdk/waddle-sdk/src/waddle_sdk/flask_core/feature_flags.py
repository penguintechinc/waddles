"""Same name as the real ``flask_core.feature_flags.feature_enabled``.

Spec Sec6.5's ``flags`` capability: "fail-open to the supplied default" on a
flag-server outage. Routes to the WIT ``flags.enabled`` import when the
generated ``wit_world`` binding module is importable (i.e. running inside a
compiled component under wasmtime) *and* that binding actually exposes a
``flags`` submodule; falls back to returning ``default`` in either case, so
this module's own tests run host-side without a component, and a component
built against an older ``wit_world`` (one wizened/cached before the
``%flags`` import existed on its ``wit_world.imports``, e.g. a stale
``core-bundle-seeder`` artifact) degrades instead of crashing ``transform()``
with ``AttributeError: module 'wit_world.imports' has no attribute
'flags'``. Confirmed via a from-scratch ``componentize-py componentize``
build against the committed ``wit/waddle-bundle/stage.wit`` (which already
declares ``import %flags;``) that a current build's component type does
carry ``import waddle:bundle/%flags@1.0.0;`` -- this guard is defense
against *stale* artifacts, not a sign the current world is missing the
import.

**Documented, necessary deviation from the real signature.** The current
``libs/flask_core/flask_core/feature_flags.py`` on this branch is ``async def
feature_enabled(flag_key: str, *, tenant: str, community: int | None = None,
default: bool = False) -> bool`` -- ``tenant``/``community`` select which
license/PostHog scope to evaluate against. The WIT ``flags`` interface
(``wit/waddle-bundle/stage.wit``) takes no such parameters: tenant/community
scoping happens host-side, resolved from the same per-call ``context``
capability every stage invocation already carries (spec Sec6.5's capability
table), not re-supplied by the guest on every flag check. This shim therefore
accepts (and discards) ``tenant``/``community`` for call-site compatibility --
an unmodified bundle's ``await feature_enabled(key, tenant=ctx.tenant,
community=..., default=True)`` line keeps working unchanged -- while only
``key``/``default`` actually cross the WIT boundary.
"""

from __future__ import annotations

#: The three license tiers, lowest to highest (`critical-rules.md` Feature
#: Flags & License Tiers). Used by `tier_at_least()` for a simple ordinal
#: comparison rather than re-deriving tier order at every call site.
_TIER_RANK = {"free": 0, "professional": 1, "enterprise": 2}


async def feature_enabled(
    flag_key: str,
    *,
    tenant: str | None = None,
    community: int | None = None,
    default: bool = False,
) -> bool:
    """Two-gate PostHog + license entitlement check, answered over the WIT ``flags`` import.

    ``tenant``/``community`` are accepted for call-site compatibility and
    discarded -- see this module's docstring. Fails open to ``default``
    whenever the WIT binding is unavailable (host-side tests), the binding
    has no ``flags`` import (a component wizened against an older world),
    or the stage itself reports a flag-server outage (the stage's own
    fail-open behavior, not duplicated here).
    """
    try:
        import wit_world  # generated binding -- only importable inside a component
    except ImportError:
        return default
    flags_mod = getattr(wit_world.imports, "flags", None)
    if flags_mod is None:
        return default
    return bool(flags_mod.enabled(flag_key, default))


async def tier() -> str:
    """The tenant's current license tier: `"free"`/`"professional"`/`"enterprise"`.

    License-gate helper for Enterprise sub-features, mirroring
    `feature_enabled`'s own WIT-import-or-degrade shape: routes to the WIT
    ``flags.tier`` import (`wit/waddle-bundle/stage.wit`) when available,
    degrading to ``"free"`` whenever the binding is unavailable, has no
    ``flags`` import (stale component, same guard as `feature_enabled`), or
    reports a string this SDK version doesn't recognize (host/SDK skew) --
    the same fail-open-to-``Free`` behavior as the Rust SDK's
    ``Tier::from_str`` (`sdk/waddle-sdk-rs/src/flags.rs`).
    """
    try:
        import wit_world  # generated binding -- only importable inside a component
    except ImportError:
        return "free"
    flags_mod = getattr(wit_world.imports, "flags", None)
    if flags_mod is None:
        return "free"
    reported = str(flags_mod.tier())
    return reported if reported in _TIER_RANK else "free"


async def tier_at_least(required: str) -> bool:
    """`True` if the tenant's current tier (see `tier()`) is at or above `required`.

    `required` must be one of `"free"`/`"professional"`/`"enterprise"` --
    raises `ValueError` for anything else, since a typo in a bundle's own
    gate call is a bug to surface immediately, not fail open on.
    """
    if required not in _TIER_RANK:
        raise ValueError(f"unknown license tier {required!r}")
    return _TIER_RANK[await tier()] >= _TIER_RANK[required]
