"""Per-community `app_activations` dispatch gate (P4, live-dispatch activation unification).

Problem this closes: `runner.py::ProcessRunner._transform_and_enqueue` invoked
every popped bundle's `transform_fn` unconditionally -- `app_activations`
(written by the webui's activation toggle, gh #586) was already the real
on/off switch for the LOADING side (`hub_api/services/distribution_service.py
::list_bundles_for_stage` already filters a community-scoped poll by
`app_activations.enabled`), but nothing on the per-EVENT dispatch path ever
consulted it. A community that disabled a bundle through the webui saw no
effect on live traffic until the next full re-poll happened to drop it from
the distribution set -- and today's single-tenant-wide (`community_id=None`)
`BundlePoller` construction (`app.py`'s `Config.RUNNER_COMMUNITY_ID`, usually
unset) means that LOADING-side filter isn't even exercised for most
deployments, since a `None` `community_id` only ever consults the tenant-wide
`app_tenant_availability` tier, never `app_activations` itself. This module
is the per-EVENT equivalent, checked against the envelope's own RESOLVED
community (`community_for_context`, gh #311) rather than the pod's static
poll scope -- so toggling a community's activation now takes effect on the
very next message for that community, independent of the poll interval.

Fail-open by design, not a no-op (`is_app_activated` returns `True` unless an
EXISTING row explicitly disables the app):

  - `community is None` (tenant-wide envelope, e.g. `COMMUNITY_RESOLUTION_
    ENABLED=false`'s demo-shim escape hatch) -- `app_activations.community_id`
    is `NOT NULL` by schema (3-tier design doc §5.1), so there is nothing to
    gate on; allow.
  - No `app_activations` row for `(community_id, app_id)` at all -- most
    bundles shipped before the 3-tier catalog (`bot_process`'s own
    `waddles.bot.discord.default`/`waddles.bot.twitch.default`, `echo_
    process`) have NEVER been seeded into `app_catalog`/`app_activations`
    (confirmed: `alembic/versions/0014_wave1a_bundle_seeds.py` seeds the
    *feature* bundles' app_ids but only ever mentions the bot's own app_ids
    in a docstring, never an INSERT). Gating hard on "a row must exist"
    would silently stop dispatch for EVERY currently-working bundle the
    moment this lands. An unonboarded app_id is treated as always-on, same
    "no binding -> shipped default" posture `flask_core.app_binding.
    resolve_apps` already established for binding resolution, applied here
    to the boolean gate instead.
  - A DB error -- logged and treated as "no row" (fail-open); this sits in
    the hot per-message dispatch path, so a transient DB hiccup must never
    stop a bundle that was working a moment ago (same posture every sibling
    `services/*.py` best-effort hook in this package already takes, e.g.
    `live_status.record_live_event`, `raid_shoutout`).

This IS still a real gate: an `app_activations` row that exists with
`enabled=False` -- exactly what the webui's activation toggle (#586) writes
on "disable" -- returns `False` and the caller skips dispatch entirely. Only
the two "nothing to check against" cases above fail open, never an explicit
disable.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, cast

logger = logging.getLogger(__name__)


class _ExecutableDal(Protocol):
    """Structural type for the `flask_core.AsyncDAL` surface this module calls.

    Same narrow protocol every sibling `services/*.py` store in this package
    declares independently (`live_status.py`, `raid_shoutout.py`,
    `command_alias_store.py`) rather than sharing one base -- see those
    modules' own identical docstrings for why (keeps each store trivially
    fakeable in tests with no shared-base coupling).
    """

    async def execute(self, sql: str, params: list[Any] | None = None) -> list[Any]: ...


#: `community_id` alone is sufficient -- `app_activations.community_id` is a
#: FK onto `communities`, which already belongs to exactly one tenant, so a
#: community-scoped lookup can never cross a tenant boundary even without an
#: explicit `tenant_id` filter in the WHERE clause (same scoping precedent
#: `services/command_alias_store.py::_SELECT_ALIAS_SQL` already relies on
#: for `command_aliases`, a sibling community-scoped table).
_SELECT_ACTIVATION_SQL = (
    "SELECT enabled FROM app_activations WHERE community_id = $1 AND app_id = $2 LIMIT 1"
)


def _resolve_dal(dal: _ExecutableDal | None) -> _ExecutableDal:
    """Return `dal` if given, else the process-wide DAL bound via `flask_core.set_bundle_dal()`."""
    if dal is not None:
        return dal
    from flask_core import get_bundle_dal

    # `flask_core` ships no py.typed marker (`follow_imports = "skip"` in
    # pyproject.toml) -- same boundary every sibling store's `_resolve_dal`
    # already casts across identically.
    return cast("_ExecutableDal", get_bundle_dal())


async def is_app_activated(
    *,
    community: str | None,
    app_id: str,
    dal: _ExecutableDal | None = None,
) -> bool:
    """True unless an EXISTING `app_activations` row explicitly disables `app_id` for `community`.

    Args:
        community: The pipeline's resolved community id (`StageEnvelope`/
            `community_for_context`'s string form), or `None` for a
            tenant-wide envelope.
        app_id: The bundle's `app_catalog.app_id` being considered for
            dispatch.
        dal: Test-only override; defaults to `flask_core.get_bundle_dal()`.

    Returns:
        `False` only when a real `app_activations` row exists for
        `(community, app_id)` with `enabled=False`. Every other case --
        no community scope, no such row, or a DB failure -- fails open
        (`True`); see module docstring for why each is safe.
    """
    if community is None:
        logger.debug(
            "activation_gate.decision app_id=%s community=None activated=True reason=tenant_wide",
            app_id,
        )
        return True

    try:
        community_id = int(community)
    except ValueError:
        logger.debug(
            "activation_gate.decision app_id=%s community=%s activated=True reason=unparseable",
            app_id,
            community,
        )
        return True

    try:
        active_dal = _resolve_dal(dal)
        rows = await active_dal.execute(_SELECT_ACTIVATION_SQL, [community_id, app_id])
    except Exception as exc:  # noqa: BLE001 - hot dispatch path, must never block on a DB hiccup
        logger.warning(
            "activation_gate.lookup_failed app_id=%s community_id=%s error=%s -- failing open",
            app_id,
            community_id,
            exc,
        )
        return True

    if not rows:
        logger.debug(
            "activation_gate.decision app_id=%s community_id=%s activated=True reason=no_row",
            app_id,
            community_id,
        )
        return True

    enabled = bool(rows[0]["enabled"])
    logger.debug(
        "activation_gate.decision app_id=%s community_id=%s activated=%s reason=row_found",
        app_id,
        community_id,
        enabled,
    )
    return enabled
