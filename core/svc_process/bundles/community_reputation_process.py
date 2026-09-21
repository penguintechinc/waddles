"""Community reputation process bundle -- read-only `!reputation`/`!rep` lookup (gh #299).

Board-demo command: replies with the requesting user's reputation on BOTH
scoring tiers -- the tenant-wide `reputation_global.score` (the FICO-style
score `reputation_module` maintains cross-community, keyed by
`hub_user_id`) and the community-scoped `community_members.reputation` --
each with a human tier label. `community_members.user_id` stores that same
`str(hub_user_id)` (see `flask_core.community_access`'s identical
`dal.community_members.user_id == str(user_id)` convention), so the
community-member row found here is also how this bundle resolves the hub
user for the global-score lookup; a member never linked to a hub account
(no `user_id`) simply shows the 600 baseline on the global side.

Every score defaults to 600 (`reputation_global.score`'s own DB column
default, and `community_members.reputation`'s) whenever no row exists --
this is never presented as an error; a new member or a member with no hub
link both look identical to "everyone starts at 600". Only a genuinely
missing `BundleContext.community` or a DB failure falls back to a distinct
guard reply, matching `bot_process._dispatch_feature`'s guard one layer up.

Community is read from `get_bundle_context().community` (never
`event.payload`, which is untrusted platform-supplied data --
security.md Tenant Isolation). Read-only: SELECTs only, never a write.
Matches the requester by `(platform, platform_user_id)` when the event
carries a native platform user id (`event.payload["author_id"]`, same
field `social_welcome_process` uses), else falls back to matching
`display_name == event.actor`.

`REPUTATION_TIERS` (gh-310) is the ONE tier-label table shared with
`hub_api/services/community_reputation_service.py`'s webui-facing reads
-- mirrored, not imported: `hub_api` and this service are independently
deployed processes with separate dependency trees, and both happen to
own a top-level `services` package, so a cross-import would silently
resolve to the wrong one. See that module's own docstring for the full
0-1000 -> 300-850 rescale derivation this table encodes.
`tests/test_bundles_community_reputation_process.py::
test_tier_table_matches_hub_api` parses hub-api's source file directly
(no import) to assert the two stay byte-identical.
"""

from __future__ import annotations

import dataclasses
import logging

from flask_core import PlatformEvent, get_bundle_context, get_bundle_dal
from flask_core.bundle_runtime import raw_sql_rows

logger = logging.getLogger(__name__)

_COMMAND_WORDS = ("reputation", "rep")
_GUARD_REPLY = "reputation lookup is unavailable right now -- try again in a bit! \U0001f427"

#: FICO-style baseline every score defaults to -- matches `reputation_global
#: .score`'s own DB column default (migration 080) and `community_members
#: .reputation`'s.
_DEFAULT_SCORE = 600

_MEMBER_BY_PLATFORM_SQL = (
    "SELECT cm.display_name, cm.reputation, cm.user_id AS hub_user_id "
    "FROM community_members cm "
    "WHERE cm.community_id = :community_id AND cm.platform = :platform "
    "AND cm.platform_user_id = :platform_user_id LIMIT 1"
)
_MEMBER_BY_DISPLAY_NAME_SQL = (
    "SELECT cm.display_name, cm.reputation, cm.user_id AS hub_user_id "
    "FROM community_members cm "
    "WHERE cm.community_id = :community_id AND cm.display_name = :display_name LIMIT 1"
)

_COMMUNITY_LABEL_SQL = (
    "SELECT COALESCE(display_name, name) AS label FROM communities "
    "WHERE id = :community_id LIMIT 1"
)

_GLOBAL_SCORE_SQL = "SELECT score FROM reputation_global WHERE hub_user_id = :hub_user_id"


#: Ascending `(exclusive_upper_bound, label)` cut points -- MUST stay
#: byte-identical to `hub_api/services/community_reputation_service.py`'s
#: own `REPUTATION_TIERS` (see module docstring). A score strictly below
#: the first threshold is "Newcomer"; a score at or above the last
#: threshold falls through to `_TOP_TIER_LABEL` ("Legend").
REPUTATION_TIERS: tuple[tuple[int, str], ...] = (
    (465, "Newcomer"),
    (575, "Regular"),
    (658, "Trusted"),
    (740, "Respected"),
    (795, "Champion"),
)
_TOP_TIER_LABEL = "Legend"


def _reputation_label(score: int) -> str:
    """Map a 300-850 FICO-style reputation score to a human tier label -- see `REPUTATION_TIERS`."""
    for threshold, label in REPUTATION_TIERS:
        if score < threshold:
            return label
    return _TOP_TIER_LABEL


async def _fetch_community_label(community_id: int) -> str:
    """Look up this community's display label, falling back to its id.

    Fetched independently of membership so a brand-new member (no
    `community_members` row yet) still gets a real community name in the
    reply, not just a bare score.
    """
    dal = get_bundle_dal()
    rows = await raw_sql_rows(dal, _COMMUNITY_LABEL_SQL, {"community_id": community_id})
    if rows and rows[0]["label"]:
        return str(rows[0]["label"])
    return f"community {community_id}"


async def _fetch_member(
    *,
    community_id: int,
    platform: str,
    platform_user_id: str | None,
    actor: str | None,
) -> tuple[str, int, str | None] | None:
    """Look up `(display_name, community_reputation, hub_user_id)` for this user.

    Tries an exact `(community_id, platform, platform_user_id)` match first
    (the event's native platform user id); falls back to `display_name ==
    actor` when the event carries no platform user id. Returns `None` if
    neither lookup finds a row -- the caller treats that as "new member",
    defaulting the community score to 600, never as an error.
    """
    dal = get_bundle_dal()

    if platform_user_id:
        rows = await raw_sql_rows(
            dal,
            _MEMBER_BY_PLATFORM_SQL,
            {
                "community_id": community_id,
                "platform": platform,
                "platform_user_id": platform_user_id,
            },
        )
        if rows:
            row = rows[0]
            reputation = row["reputation"]
            return (
                row["display_name"] or platform_user_id,
                int(reputation) if reputation is not None else _DEFAULT_SCORE,
                row["hub_user_id"],
            )

    if actor:
        rows = await raw_sql_rows(
            dal, _MEMBER_BY_DISPLAY_NAME_SQL, {"community_id": community_id, "display_name": actor}
        )
        if rows:
            row = rows[0]
            reputation = row["reputation"]
            return (
                row["display_name"] or actor,
                int(reputation) if reputation is not None else _DEFAULT_SCORE,
                row["hub_user_id"],
            )

    return None


async def _fetch_global_score(hub_user_id: str | None) -> int:
    """Look up the tenant-wide `reputation_global.score` for this hub user.

    Defaults to 600 when `hub_user_id` is unset (this community member was
    never linked to a hub account) or has no `reputation_global` row yet --
    the same FICO baseline `reputation_global.score`'s own column default
    uses.
    """
    if not hub_user_id:
        return _DEFAULT_SCORE
    try:
        parsed_id = int(hub_user_id)
    except (TypeError, ValueError):
        return _DEFAULT_SCORE

    dal = get_bundle_dal()
    rows = await raw_sql_rows(dal, _GLOBAL_SCORE_SQL, {"hub_user_id": parsed_id})
    if rows and rows[0]["score"] is not None:
        return int(rows[0]["score"])
    return _DEFAULT_SCORE


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Reply to `!reputation`/`!rep` with the requester's global + community scores.

    Read-only -- SELECTs against `community_members`, `communities`, and
    `reputation_global`, scoped to `get_bundle_context().community`. Every
    score defaults to 600 (FICO baseline) when no row exists, each shown
    with a human tier label -- a new user is never told "no reputation",
    both tiers just show the baseline. Never raises out of this function on
    a lookup failure (DB error, missing context): logged and turned into a
    graceful guard reply, matching `bot_process._dispatch_feature`'s guard
    one layer up (defense in depth -- this bundle must be safe to call
    directly, not just via the router).

    Raises `ValueError` only on a malformed event (`text` missing/non-str),
    same convention as every other feature bundle here -- the process
    runner / `bot_process._dispatch_feature` catches this per-event.
    """
    raw_text = event.payload.get("text")
    if not isinstance(raw_text, str):
        raise ValueError("event payload missing required 'text' string field")
    text = raw_text.strip()
    if not text.startswith("!"):
        return None
    parts = text[1:].split(maxsplit=1)
    if not parts or parts[0].lower() not in _COMMAND_WORDS:
        return None  # not a reputation command

    try:
        ctx = get_bundle_context()
        community_id = int(ctx.community) if ctx.community else None
        if community_id is None:
            reply_text = _GUARD_REPLY
        else:
            raw_author_id = event.payload.get("author_id")
            platform_user_id = raw_author_id if isinstance(raw_author_id, str) else None
            member = await _fetch_member(
                community_id=community_id,
                platform=event.platform,
                platform_user_id=platform_user_id,
                actor=event.actor,
            )
            display_name = platform_user_id or event.actor or "you"
            community_score = _DEFAULT_SCORE
            hub_user_id: str | None = None
            if member is not None:
                display_name, community_score, hub_user_id = member

            community_label = await _fetch_community_label(community_id)
            global_score = await _fetch_global_score(hub_user_id)

            reply_text = (
                f"\U0001f427 {display_name} — "
                f"Global: {global_score} ({_reputation_label(global_score)}) · "
                f"{community_label}: {community_score} ({_reputation_label(community_score)})"
            )
    except Exception as exc:  # noqa: BLE001 -- read-only lookup must never crash the bot
        logger.error("community_reputation.lookup_failed error=%s", exc)
        reply_text = _GUARD_REPLY

    return dataclasses.replace(event, payload={**event.payload, "text": reply_text})
