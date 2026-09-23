"""Same name as the real ``flask_core.feature_flags.feature_enabled``.

Spec Sec6.5's ``flags`` capability: "fail-open to the supplied default" on a
flag-server outage. Routes to the WIT ``flags.enabled`` import when the
generated ``wit_world`` binding module is importable (i.e. running inside a
compiled component under wasmtime); falls back to returning ``default``
otherwise, so this module's own tests run host-side without a component.

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
    whenever the WIT binding is unavailable (host-side tests) or the stage
    itself reports a flag-server outage (the stage's own fail-open behavior,
    not duplicated here).
    """
    try:
        import wit_world  # generated binding -- only importable inside a component
    except ImportError:
        return default
    return bool(wit_world.imports.flags.enabled(flag_key, default))
