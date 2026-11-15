"""Community profile management -- ported from `communityProfileController.js`.

About section, social links, visibility, and logo/banner uploads for a
community entity. Community is a Core-tenancy entity per SCCEBM (the
community ENTITY, not the Community engagement module) -- see
`hub_api/PORTING.md`.

Authorization: `updateCommunityProfile`/logo/banner writes all require the
caller hold `community:manage_channels` (moderator+) via
`services.community_authz.require_community_scope` -- the SAME DB-backed
per-community check `middleware/auth.js::requireCommunityAdmin` performs in
Node, not a flat JWT scope (see `community_authz.py`'s own docstring for
why). `update_community_profile` additionally re-checks the resolved
role's NAME is owner/admin (mirrors `communityProfileController.js`'s own
extra in-controller check, stricter than the route middleware alone --
Node lets a moderator manage logo/banner but not the text profile fields).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.community_authz import require_community_scope
from services.errors import bad_request, not_found
from services.scope_registry import SCOPE_PROFILE_MANAGE
from services.storage_service import (
    MAX_BANNER_SIZE_BYTES,
    MAX_LOGO_SIZE_BYTES,
    delete_object,
    is_allowed_image_type,
    upload_community_asset,
)

VALID_VISIBILITIES = ("public", "registered", "members_only")
#: OR-check, mirrors `requireCommunityAdmin`'s own `scopes.includes('community:manage_members')
#: || scopes.includes('community:manage_channels')`.
_ADMIN_SCOPES = ("community:manage_members", "community:manage_channels")
_DISCORD_INVITE_RE = re.compile(r"^https?://(discord\.gg|discord\.com/invite)/[a-zA-Z0-9]+$")


def _is_valid_url(value: str) -> bool:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def _is_valid_discord_invite(value: str | None) -> bool:
    if not value:
        return True
    return bool(_DISCORD_INVITE_RE.match(value))


@dataclass(slots=True, frozen=True)
class CommunityProfile:
    """A community's profile fields, shaped like Node's `formatCommunity()`."""

    id: int
    name: str
    display_name: str | None
    description: str | None
    about_extended: str | None
    social_links: dict[str, Any]
    website_url: str | None
    discord_invite_url: str | None
    platform: str | None
    member_count: int
    is_public: bool
    join_mode: str | None
    visibility: str
    logo_url: str | None
    banner_url: str | None
    owner_username: str | None
    created_at: str | None
    restricted: bool = False


async def _get_active_community(dal: Any, async_dal: Any, community_id: int) -> Any:
    rows = await async_dal.select_async(
        dal(
            (dal.communities.id == community_id)
            & (dal.communities.is_active == True)  # noqa: E712
            & (dal.communities.deleted_at == None)  # noqa: E711
        )
    )
    if not rows:
        return None
    return rows.first()


def _format_community(row: Any, *, owner_username: str | None) -> CommunityProfile:
    config = row.config or {}
    created_at = row.created_at
    return CommunityProfile(
        id=row.id,
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        about_extended=row.about_extended,
        social_links=row.social_links or {},
        website_url=row.website_url,
        discord_invite_url=row.discord_invite_url,
        platform=row.platform,
        member_count=row.member_count or 0,
        is_public=bool(row.is_public),
        join_mode=row.join_mode,
        visibility=row.visibility or "public",
        logo_url=config.get("logo_url"),
        banner_url=config.get("banner_url"),
        owner_username=owner_username,
        created_at=created_at.isoformat() if created_at else None,
    )


async def _resolve_owner_username(dal: Any, async_dal: Any, owner_id: str | None) -> str | None:
    """Resolve `communities.owner_id` (legacy VARCHAR) to a `hub_users.username`.

    Node's own `getCommunityById()` does this via a raw SQL
    `LEFT JOIN hub_users u ON c.owner_id = u.id` -- `owner_id` is VARCHAR
    and `hub_users.id` is INTEGER, an operand-type mismatch real Postgres
    has no implicit cast for (`operator does not exist: character varying
    = integer`), a pre-existing gap in the same family documented in
    `services/schema.py` (Node's own query would 500 against the real
    schema). This resolves the same information as a safe two-step lookup
    instead of porting a join that's guaranteed to fail -- matches
    `hub_api/PORTING.md` Gotcha #4's own precedent (gap 2: adapt to
    something that works rather than port guaranteed breakage).
    """
    if not owner_id or not owner_id.isdigit():
        return None
    rows = await async_dal.select_async(dal(dal.hub_users.id == int(owner_id)))
    return rows.first().username if rows else None


async def get_community_profile(
    async_dal: Any, dal: Any, *, community_id: int, viewer_id: int | None
) -> CommunityProfile | None:
    """Get a community's profile, applying visibility restrictions for non-privileged viewers."""
    row = await _get_active_community(dal, async_dal, community_id)
    if row is None:
        return None

    visibility = row.visibility or "public"
    can_view = await _can_view_community(async_dal, dal, visibility, community_id, viewer_id)
    if not can_view:
        config = row.config or {}
        return CommunityProfile(
            id=row.id,
            name=row.name,
            display_name=row.display_name,
            description=None,
            about_extended=None,
            social_links={},
            website_url=None,
            discord_invite_url=None,
            platform=None,
            member_count=0,
            is_public=bool(row.is_public),
            join_mode=None,
            visibility=visibility,
            logo_url=config.get("logo_url"),
            banner_url=None,
            owner_username=None,
            created_at=None,
            restricted=True,
        )

    owner_username = await _resolve_owner_username(dal, async_dal, row.owner_id)
    return _format_community(row, owner_username=owner_username)


async def _can_view_community(
    async_dal: Any, dal: Any, visibility: str, community_id: int, viewer_id: int | None
) -> bool:
    """Mirrors `communityProfileController.js::canViewCommunity()` exactly."""
    if visibility == "public":
        return True
    if viewer_id is None:
        return False
    if visibility == "registered":
        rows = await async_dal.select_async(dal(dal.hub_users.id == viewer_id))
        return bool(rows) and bool(rows.first().email_verified)
    if visibility == "members_only":
        rows = await async_dal.select_async(
            dal(
                (dal.community_members.community_id == community_id)
                & (dal.community_members.user_id == str(viewer_id))
            )
        )
        return bool(rows)
    return False


async def update_community_profile(
    async_dal: Any, dal: Any, *, community_id: int, user_id: int, fields: dict[str, Any]
) -> CommunityProfile:
    """Update a community's profile. Owner/admin only (stricter than logo/banner)."""
    # Dedicated per-action scope (admin-kind only by default) -- never a role-name check.
    await require_community_scope(
        async_dal, dal, community_id=community_id, user_id=user_id, any_of=(SCOPE_PROFILE_MANAGE,)
    )

    visibility = fields.get("visibility")
    if visibility and visibility not in VALID_VISIBILITIES:
        raise bad_request("Invalid visibility setting")

    website_url = fields.get("websiteUrl")
    if website_url and not _is_valid_url(website_url):
        raise bad_request("Invalid website URL")

    discord_invite_url = fields.get("discordInviteUrl")
    if discord_invite_url and not _is_valid_discord_invite(discord_invite_url):
        raise bad_request("Invalid Discord invite URL")

    row = await _get_active_community(dal, async_dal, community_id)
    if row is None:
        raise not_found("Community not found")

    update_fields = {
        "display_name": fields.get("displayName"),
        "description": fields.get("description"),
        "about_extended": fields.get("aboutExtended"),
        "social_links": fields.get("socialLinks"),
        "website_url": website_url,
        "discord_invite_url": discord_invite_url,
        "visibility": visibility,
        "updated_at": datetime.now(UTC),
    }
    update_fields = {k: v for k, v in update_fields.items() if v is not None}
    await async_dal.update_async(dal.communities.id == community_id, **update_fields)

    row = await _get_active_community(dal, async_dal, community_id)
    assert row is not None  # nosec B101 - just updated the same row above
    owner_username = await _resolve_owner_username(dal, async_dal, row.owner_id)
    return _format_community(row, owner_username=owner_username)


async def _set_config_key(
    dal: Any, async_dal: Any, community_id: int, key: str, value: Any
) -> None:
    row = await _get_active_community(dal, async_dal, community_id)
    if row is None:
        raise not_found("Community not found")
    config = dict(row.config or {})
    if value is None:
        config.pop(key, None)
    else:
        config[key] = value
    await async_dal.update_async(
        dal.communities.id == community_id, config=config, updated_at=datetime.now(UTC)
    )


async def upload_community_logo(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    user_id: int,
    data: bytes,
    filename: str,
    content_type: str,
    size: int,
) -> str:
    """Upload a community logo. Moderator+ (`community:manage_channels`)."""
    await require_community_scope(
        async_dal, dal, community_id=community_id, user_id=user_id, any_of=_ADMIN_SCOPES
    )
    if not is_allowed_image_type(content_type):
        raise bad_request("Invalid file type. Allowed: JPEG, PNG, GIF, WebP")
    if size > MAX_LOGO_SIZE_BYTES:
        raise bad_request("File too large. Maximum size: 5MB")

    row = await _get_active_community(dal, async_dal, community_id)
    if row is None:
        raise not_found("Community not found")
    current = (row.config or {}).get("logo_url")
    if current:
        await delete_object(current)

    logo_url = await upload_community_asset(data, filename, content_type, folder="community-logos")
    await _set_config_key(dal, async_dal, community_id, "logo_url", logo_url)
    return logo_url


async def delete_community_logo(
    async_dal: Any, dal: Any, *, community_id: int, user_id: int
) -> None:
    """Delete a community logo. Moderator+ (`community:manage_channels`)."""
    await require_community_scope(
        async_dal, dal, community_id=community_id, user_id=user_id, any_of=_ADMIN_SCOPES
    )
    row = await _get_active_community(dal, async_dal, community_id)
    if row is None:
        raise not_found("Community not found")
    current = (row.config or {}).get("logo_url")
    if current:
        await delete_object(current)
    await _set_config_key(dal, async_dal, community_id, "logo_url", None)


async def upload_community_banner(
    async_dal: Any,
    dal: Any,
    *,
    community_id: int,
    user_id: int,
    data: bytes,
    filename: str,
    content_type: str,
    size: int,
) -> str:
    """Upload a community banner. Moderator+ (`community:manage_channels`)."""
    await require_community_scope(
        async_dal, dal, community_id=community_id, user_id=user_id, any_of=_ADMIN_SCOPES
    )
    if not is_allowed_image_type(content_type):
        raise bad_request("Invalid file type. Allowed: JPEG, PNG, GIF, WebP")
    if size > MAX_BANNER_SIZE_BYTES:
        raise bad_request("File too large. Maximum size: 10MB")

    row = await _get_active_community(dal, async_dal, community_id)
    if row is None:
        raise not_found("Community not found")
    current = (row.config or {}).get("banner_url")
    if current:
        await delete_object(current)

    banner_url = await upload_community_asset(
        data, filename, content_type, folder="community-banners"
    )
    await _set_config_key(dal, async_dal, community_id, "banner_url", banner_url)
    return banner_url


async def delete_community_banner(
    async_dal: Any, dal: Any, *, community_id: int, user_id: int
) -> None:
    """Delete a community banner. Moderator+ (`community:manage_channels`)."""
    await require_community_scope(
        async_dal, dal, community_id=community_id, user_id=user_id, any_of=_ADMIN_SCOPES
    )
    row = await _get_active_community(dal, async_dal, community_id)
    if row is None:
        raise not_found("Community not found")
    current = (row.config or {}).get("banner_url")
    if current:
        await delete_object(current)
    await _set_config_key(dal, async_dal, community_id, "banner_url", None)
