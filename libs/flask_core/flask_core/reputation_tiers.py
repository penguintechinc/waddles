"""Single shared FICO-style reputation tier table -- `hub_api` and `core/svc_process`'s
`community_reputation_process.py` bundle both display the identical
`community_members.reputation` / `reputation_tenant.score` value with a
human tier label, and previously kept two hand-mirrored copies of this
table (guarded only by a source-parsing drift test on each side) because
each process has its own top-level `services`/`bundles` package and a
cross-import between THOSE would silently resolve to the wrong module.

`flask_core` is already a dependency every core service (`reputation_module`,
`hub_api`, `svc_process`) installs/vendors (see each service's own
`sys.path`/`from flask_core import ...` usage) -- so it, not either
service's own package, is the correct single source of truth for a table
shared across process boundaries.

See module docstring history: `hub_api/services/community_reputation_service.py`
and `core/svc_process/builtin_handlers/community_reputation_process.py` for the full
0-1000 -> 300-850 rescale derivation this table encodes (gh-310).
"""

from __future__ import annotations

#: Ascending `(exclusive_upper_bound, label)` cut points. A score strictly
#: below the first threshold is "Newcomer"; a score at or above the last
#: threshold's bound falls through to `_TOP_TIER_LABEL` ("Legend").
REPUTATION_TIERS: tuple[tuple[int, str], ...] = (
    (465, "Newcomer"),
    (575, "Regular"),
    (658, "Trusted"),
    (740, "Respected"),
    (795, "Champion"),
)
_TOP_TIER_LABEL = "Legend"


def reputation_tier(score: int) -> str:
    """Map a 300-850 FICO-style reputation score to its human tier label.

    Never raises -- a score outside `[300, 850]` (shouldn't happen; every
    write path clamps before storing) still resolves to a sane tier at
    either end (`Newcomer` below the first cut, `Legend` at/above the last).
    """
    for threshold, label in REPUTATION_TIERS:
        if score < threshold:
            return label
    return _TOP_TIER_LABEL
