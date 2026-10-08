"""Community reputation process bundle -- read-only `!reputation`/`!rep` lookup (gh #299).

Board-demo command: replies with the requesting user's reputation on BOTH
scoring tiers -- the tenant-wide `reputation_tenant.score` (the FICO-style
score `reputation_module` maintains cross-community but NEVER cross-tenant,
keyed by `(tenant_id, hub_user_id)` -- security.md Tenant Isolation) and the
community-scoped `community_members.reputation` -- each with a human tier
label. `community_members.user_id` stores that same `str(hub_user_id)` (see
`flask_core.community_access`'s identical `dal.community_members.user_id ==
str(user_id)` convention), so the community-member row found here is also
how this bundle resolves the hub user for the tenant-score lookup; a member
never linked to a hub account (no `user_id`) simply shows the 600 baseline
on the tenant side. Tenant is resolved from `communities.tenant_id` via the
already-trusted `community_id` (`get_bundle_context().community`) -- never
client-supplied, same mapping `reputation_module` itself uses.

Every score defaults to 600 (`reputation_tenant.score`'s own DB column
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

`REPUTATION_TIERS`/`reputation_tier()` (gh-310) come from
`flask_core.reputation_tiers` -- the single shared source for this
tier-label table, also used by
`hub_api/services/community_reputation_service.py`'s webui-facing reads.
Both processes already depend on `flask_core`, so importing from there
(rather than each process keeping its own hand-mirrored copy, guarded only
by a source-parsing drift test) is the correct single source of truth.
"""

from __future__ import annotations

import dataclasses
import logging

from flask_core import PlatformEvent, get_bundle_context, get_bundle_dal
from flask_core.bundle_runtime import raw_sql_rows
from flask_core.reputation_tiers import reputation_tier as _reputation_label

logger = logging.getLogger(__name__)

_COMMAND_WORDS = ("reputation", "rep")
_GUARD_REPLY = "reputation lookup is unavailable right now -- try again in a bit! \U0001f427"

#: FICO-style baseline every score defaults to -- matches `reputation_tenant
#: .score`'s own DB column default (migration 097) and `community_members
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
    "SELECT COALESCE(display_name, name) AS label FROM communities WHERE id = :community_id LIMIT 1"
)

#: Resolves tenant from the already-trusted `community_id` (never from
#: client-supplied event payload data) in the same query as the tenant
#: score lookup -- `communities.tenant_id` is NOT NULL (migration 058).
_TENANT_SCORE_SQL = (
    "SELECT rt.score FROM reputation_tenant rt "
    "JOIN communities c ON c.tenant_id = rt.tenant_id "
    "WHERE c.id = :community_id AND rt.hub_user_id = :hub_user_id"
)


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


async def _fetch_tenant_score(community_id: int, hub_user_id: str | None) -> int:
    """Look up the tenant-wide `reputation_tenant.score` for this hub user.

    Tenant is resolved from `community_id` (the already-trusted
    `BundleContext.community`, never client-supplied) via
    `communities.tenant_id` -- `_TENANT_SCORE_SQL`'s join -- so this NEVER
    returns another tenant's score (security.md Tenant Isolation). Defaults
    to 600 when `hub_user_id` is unset (this community member was never
    linked to a hub account) or has no `reputation_tenant` row yet -- the
    same FICO baseline `reputation_tenant.score`'s own column default uses.
    """
    if not hub_user_id:
        return _DEFAULT_SCORE
    try:
        parsed_id = int(hub_user_id)
    except (TypeError, ValueError):
        return _DEFAULT_SCORE

    dal = get_bundle_dal()
    rows = await raw_sql_rows(
        dal, _TENANT_SCORE_SQL, {"community_id": community_id, "hub_user_id": parsed_id}
    )
    if rows and rows[0]["score"] is not None:
        return int(rows[0]["score"])
    return _DEFAULT_SCORE


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Reply to `!reputation`/`!rep` with the requester's tenant + community scores.

    Read-only -- SELECTs against `community_members`, `communities`, and
    `reputation_tenant`, scoped to `get_bundle_context().community` (and,
    transitively, that community's single owning tenant -- never another
    tenant's data). Every score defaults to 600 (FICO baseline) when no row
    exists, each shown with a human tier label -- a new user is never told
    "no reputation", both tiers just show the baseline. Never raises out of
    this function on a lookup failure (DB error, missing context): logged
    and turned into a graceful guard reply, matching
    `bot_process._dispatch_feature`'s guard one layer up (defense in depth
    -- this bundle must be safe to call directly, not just via the router).

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
            tenant_score = await _fetch_tenant_score(community_id, hub_user_id)

            reply_text = (
                f"\U0001f427 {display_name} — "
                f"Tenant: {tenant_score} ({_reputation_label(tenant_score)}) · "
                f"{community_label}: {community_score} ({_reputation_label(community_score)})"
            )
    except Exception as exc:  # noqa: BLE001 -- read-only lookup must never crash the bot
        logger.error("community_reputation.lookup_failed error=%s", exc)
        reply_text = _GUARD_REPLY

    return dataclasses.replace(event, payload={**event.payload, "text": reply_text})
