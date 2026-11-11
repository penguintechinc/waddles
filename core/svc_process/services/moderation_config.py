"""Per-(tenant, community) content-moderation config reads for the P1 moderation gate.

Per docs/plans/2026-09-08-content-moderation-design.md SS3/SS4: the gate
(`services/moderation_gate.py`) needs two pieces of caller-side context
before it can call `ClassificationProvider.classify()` -- the community's
enabled category set (`community_moderation_config`, migration
`0008_moderation_config`, empty/OFF by default -- P3 will grow a real admin
UI on top of this same table) and a numeric tenant id for the classifier's
own `tenant_id` logging/metering parameter. `communities` carries no
`tenant_id` column (this codebase's tenancy model is a single catch-all
tenant (slug `default`; `0007_forum_catalog.py` used `"global"` before alembic 0095),
not a per-community FK) -- `tenants.slug` is resolved separately instead of
joined, so a community with no `tenants` row for its envelope's tenant slug
still degrades to `tenant_id=None` rather than raising.
"""

from __future__ import annotations

import json
from typing import Any, Protocol


class _ExecutableDal(Protocol):
    """Structural type for the `flask_core.AsyncDAL` surface this module calls."""

    async def execute(self, sql: str, params: list[Any] | None = None) -> list[Any]: ...


_ENABLED_CATEGORIES_SQL = (
    "SELECT enabled_categories FROM community_moderation_config WHERE community_id = $1"
)
_TENANT_ID_SQL = "SELECT id FROM tenants WHERE slug = $1"


async def get_enabled_categories(dal: _ExecutableDal, community_id: int) -> set[str]:
    """Return this community's enabled moderation categories, or an empty set.

    Empty set covers both "no row yet" (the default -- moderation OFF) and
    a row whose `enabled_categories` is itself `[]`; the gate treats both
    identically (skip the classifier entirely, per design SS4 point 4).
    """
    rows = await dal.execute(_ENABLED_CATEGORIES_SQL, [community_id])
    if not rows:
        return set()
    raw = rows[0]["enabled_categories"]
    if raw is None:
        return set()
    if isinstance(raw, str):
        raw = json.loads(raw)
    return {str(category) for category in raw}


async def get_tenant_id(dal: _ExecutableDal, tenant_slug: str) -> int | None:
    """Resolve `tenants.id` for `tenant_slug`, or `None` if no such tenant row exists.

    Used only for `ClassificationProvider.classify()`'s `tenant_id` log-
    correlation parameter (`moderation_module.base.ClassificationProvider`'s
    own docstring: never used to widen/narrow `enabled_categories`) -- a
    miss here must never block the gate, only degrade its log context.
    """
    rows = await dal.execute(_TENANT_ID_SQL, [tenant_slug])
    if not rows:
        return None
    return int(rows[0]["id"])
