"""Scope registry + role-kind guards for data-defined community roles.

A ROLE is a named group of SCOPES stored in `community_roles` (`base_claims.scopes`); authorization
checks scopes only, never role names. This module owns the two things that make that model safe:

* the **scope registry** -- every scope the platform knows about, so the admin role can be
  *computed* as "all registered scopes" (a newly introduced scope reaches admin with no data edit);
* the **role-kind guards** -- delete/edit rules keyed on the integer `community_roles.role_kind`
  column (data), never on a role's name.

Other features add scopes with :func:`register_scopes` at import time (see
`docs/roles-and-scopes.md`); the registry is idempotent and additive-only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Final

from services.errors import ApiError

#: `community_roles.role_kind` int codes (0 = custom, so a column default is safe).
KIND_CUSTOM: Final[int] = 0
#: Predefined admin class (admin/owner): undeletable, scopes = FULL registry, never editable.
KIND_ADMIN: Final[int] = 1
#: Predefined moderator class: undeletable, scopes editable by an authorized admin.
KIND_MODERATOR: Final[int] = 2
#: Other predefined roles (member, speaker, ...): undeletable, scopes locked.
KIND_FIXED: Final[int] = 3

#: Scope that authorizes role create/edit/delete. Held by the computed admin role only by default.
SCOPE_ROLES_MANAGE: Final[str] = "roles:manage"
#: Per-action scopes (dedicated, never satisfied by broad moderator scopes); admin-kind holds all.
SCOPE_GITHUB_MANAGE: Final[str] = "github:manage"
SCOPE_COMMUNITY_ADMIN: Final[str] = "community:admin"
SCOPE_PROFILE_MANAGE: Final[str] = "community.profile:manage"
SCOPE_BRANDING_MANAGE: Final[str] = "community.branding:manage"
SCOPE_BILLING_MANAGE: Final[str] = "billing:manage"
SCOPE_MEMBERS_MANAGE: Final[str] = "community.members:manage"
SCOPE_WORKFLOW_MANAGE: Final[str] = "workflow:manage"
SCOPE_BYOK_MANAGE: Final[str] = "media.byok:manage"
SCOPE_AI_ROUTING_MANAGE: Final[str] = "ai.routing:manage"
SCOPE_OVERLAY_MANAGE: Final[str] = "overlay:manage"
SCOPE_CALLS_MANAGE: Final[str] = "calls:manage"
SCOPE_MUSIC_STATION: Final[str] = "music:station.manage"
SCOPE_MUSIC_QUEUE: Final[str] = "music:queue.manage"
SCOPE_STREAM_CONFIG: Final[str] = "stream:config.manage"
SCOPE_TOKEN_BILLING: Final[str] = "token.billing:manage"  # noqa: S105  # nosec B105
SCOPE_MARKETPLACE_LIFECYCLE: Final[str] = "marketplace.lifecycle:manage"
SCOPE_BUNDLE_PERMISSIONS: Final[str] = "bundles.permissions:manage"
SCOPE_BUNDLE_ACTIVATION: Final[str] = "bundles.activation:manage"
SCOPE_PROVIDER_CONNECTIONS: Final[str] = "provider.connections:manage"
SCOPE_MEMBERS_ROLES: Final[str] = "members:roles.manage"
SCOPE_MEMBERS_REMOVE: Final[str] = "members:remove"
SCOPE_REPUTATION_MANAGE: Final[str] = "members:reputation.manage"
SCOPE_BUNDLE_ADMIN_MODERATE: Final[str] = "bundles.admin:moderate"
SCOPE_BUNDLE_ADMIN_ADMIN: Final[str] = "bundles.admin:admin"
SCOPE_SETTINGS_MANAGE: Final[str] = "community.settings:manage"
SCOPE_MODULES_MANAGE: Final[str] = "community.modules:manage"
SCOPE_TEMP_PASSWORD: Final[str] = "members:temp_password.reset"  # noqa: S105  # nosec B105
SCOPE_PLATFORMS_MANAGE: Final[str] = "community.platforms:manage"
SCOPE_COMMANDS_MANAGE: Final[str] = "community.commands:manage"
SCOPE_CHANNELS_MANAGE: Final[str] = "community.channels:manage"
SCOPE_FORUMS_MANAGE: Final[str] = "community.forums:manage"
SCOPE_MEMBERS_VIEW: Final[str] = "community.members:view"
SCOPE_ROLES_VIEW: Final[str] = "community.roles:view"
#: Global-admin literal on a JWT; the only token claim that bypasses community checks.
SCOPE_GLOBAL_ADMIN: Final[str] = "users:admin"

_SCOPE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_.-]*:[a-z0-9_.*-]+$")

_REGISTRY: dict[str, str] = {}


def register_scopes(scopes: Mapping[str, str] | Iterable[str]) -> None:
    """Register scopes (`{scope: description}` or bare names). Idempotent; invalid names raise."""
    items = scopes.items() if isinstance(scopes, Mapping) else ((s, "") for s in scopes)
    for scope, description in items:
        if not _SCOPE_RE.match(scope):
            raise ValueError(f"invalid scope name: {scope!r}")
        if description or scope not in _REGISTRY:
            _REGISTRY[scope] = description or _REGISTRY.get(scope, "")


def all_scopes() -> frozenset[str]:
    """Every registered scope -- the computed scope set of the admin-kind role."""
    return frozenset(_REGISTRY)


def describe_scopes() -> dict[str, str]:
    """`{scope: description}` for the scope picker."""
    return dict(sorted(_REGISTRY.items()))


register_scopes(
    {
        "community:read": "Read community data",
        "community:manage_channels": "Create and manage channels",
        "community:manage_members": "Manage members",
        "community:manage_roles": "Legacy role management scope",
        "channels:read": "Read channels",
        "channels:send_chat": "Send chat messages",
        "channels:speak": "Speak in voice channels",
        "channels:share_video": "Share video",
        "channels:screenshare": "Share screen",
        "channels:create": "Create channels",
        "channels:moderate": "Moderate channels",
        "channels:override_screenshare": "Override screen-share restrictions",
        "resource:delete_any": "Delete any resource",
        "resource:pin": "Pin resources",
        "resource:moderate": "Moderate resources",
        SCOPE_ROLES_MANAGE: "Create, edit and delete roles",
        # Rooms (call rooms): every action has its own scope. The admin kind picks all of them up
        # automatically; the moderator kind is seeded with the four moderation ones (migration
        # 0118_room_scopes_moderator), never with the management ones.
        "rooms:create": "Create call rooms",
        "rooms:update": "Change a room's kind, schedule or closed state",
        "rooms:close": "Close (delete) call rooms",
        "rooms:guests": "Allow guests into a room",
        "rooms:kick": "Remove participants from a room",
        "rooms:mute": "Mute and unmute participants in a room",
        "rooms:lock": "Lock and unlock a room",
        "rooms:hands": "Moderate the raised-hand queue",
        SCOPE_GITHUB_MANAGE: "Manage the community GitHub sync connections",
        SCOPE_COMMUNITY_ADMIN: "Administer the community (overlays, calls admin)",
        SCOPE_PROFILE_MANAGE: "Edit the community profile",
        SCOPE_BRANDING_MANAGE: "Upload and delete the community logo and banner",
        SCOPE_BILLING_MANAGE: "Manage marketplace billing and subscriptions",
        SCOPE_MEMBERS_MANAGE: "Review and decide member join requests",
        SCOPE_WORKFLOW_MANAGE: "Manage community workflows",
        SCOPE_BYOK_MANAGE: "Manage media bring-your-own-key credentials",
        SCOPE_AI_ROUTING_MANAGE: "Manage AI routing and BYOK keys",
        SCOPE_OVERLAY_MANAGE: "Manage stream overlays",
        SCOPE_CALLS_MANAGE: "Administer voice call rooms",
        SCOPE_MUSIC_STATION: "Administer the community music station",
        SCOPE_MUSIC_QUEUE: "Administer the community music queue",
        SCOPE_STREAM_CONFIG: "Manage streaming configuration and keys",
        SCOPE_TOKEN_BILLING: "Administer community token billing",
        SCOPE_MARKETPLACE_LIFECYCLE: "Administer marketplace installs",
        SCOPE_BUNDLE_PERMISSIONS: "Manage bundle permission grants",
        SCOPE_BUNDLE_ACTIVATION: "Activate and deactivate bundles",
        SCOPE_PROVIDER_CONNECTIONS: "Manage provider connections",
        SCOPE_MEMBERS_ROLES: "Assign member roles",
        SCOPE_MEMBERS_REMOVE: "Remove community members",
        SCOPE_REPUTATION_MANAGE: "Adjust and administer reputation",
        SCOPE_BUNDLE_ADMIN_MODERATE: "Use moderation-level bundle admin tools",
        SCOPE_BUNDLE_ADMIN_ADMIN: "Use admin-level bundle admin tools",
        SCOPE_SETTINGS_MANAGE: "Edit community settings",
        SCOPE_MODULES_MANAGE: "Configure community modules",
        SCOPE_TEMP_PASSWORD: "Generate member temporary passwords",
        SCOPE_PLATFORMS_MANAGE: "View and manage connected platforms",
        SCOPE_COMMANDS_MANAGE: "View and manage community commands",
        SCOPE_CHANNELS_MANAGE: "Administer hub channels and overrides",
        SCOPE_FORUMS_MANAGE: "Moderate forum posts and replies",
        SCOPE_MEMBERS_VIEW: "List community members (admin view)",
        SCOPE_ROLES_VIEW: "List roles and the scope registry",
    }
)


#: Scopes the predefined moderator role is seeded with beyond its original channel/chat scopes:
#: exactly the moderation-level privileged actions moderators could already perform. Admin-level
#: money/infra/secret actions (billing, BYOK, AI routing, GitHub, workflow, roles, token billing,
#: marketplace lifecycle, bundle permissions/activation, provider connections, stream config,
#: role assignment, `bundles.admin:admin`) are deliberately NOT here. Mirrors migration 0117.
MODERATOR_SEED_SCOPES: Final[tuple[str, ...]] = (
    SCOPE_MEMBERS_MANAGE,
    SCOPE_OVERLAY_MANAGE,
    SCOPE_CALLS_MANAGE,
    SCOPE_BRANDING_MANAGE,
    SCOPE_MUSIC_STATION,
    SCOPE_MUSIC_QUEUE,
    SCOPE_REPUTATION_MANAGE,
    SCOPE_MEMBERS_REMOVE,
    SCOPE_BUNDLE_ADMIN_MODERATE,
    SCOPE_CHANNELS_MANAGE,
    SCOPE_FORUMS_MANAGE,
    SCOPE_MEMBERS_VIEW,
    SCOPE_ROLES_VIEW,
)


def effective_scopes(role_kind: int | None, stored: Iterable[str] | None) -> frozenset[str]:
    """Scopes a role grants: the full registry for admin-kind, else its stored list."""
    if role_kind == KIND_ADMIN:
        return all_scopes()
    return frozenset(stored or ())


def normalize_scopes(raw: object) -> list[str]:
    """Validate a payload `scopes` value: registered scope strings, de-duplicated, sorted."""
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
        raise ApiError("scopes must be a list of strings", 400, "BAD_REQUEST")
    unknown = sorted(set(raw) - all_scopes())
    if unknown:
        raise ApiError(f"Unknown scope(s): {', '.join(unknown)}", 400, "UNKNOWN_SCOPE")
    return sorted(set(raw))


def guard_grant(editor_scopes: frozenset[str], granted: Iterable[str]) -> None:
    """Refuse (403) granting any scope the editor does not itself hold."""
    missing = sorted(set(granted) - editor_scopes)
    if missing:
        raise ApiError(
            f"Cannot grant scopes you do not hold: {', '.join(missing)}", 403, "SCOPE_ESCALATION"
        )


def guard_delete(role_kind: int | None) -> None:
    """Refuse (409) deleting any predefined role (admin, moderator, ...)."""
    if role_kind != KIND_CUSTOM:
        raise ApiError("Predefined roles cannot be deleted", 409, "ROLE_PROTECTED")


def guard_scope_edit(role_kind: int | None) -> None:
    """Refuse (403) a scope edit on an admin-kind or fixed predefined role."""
    if role_kind == KIND_ADMIN:
        raise ApiError(
            "The admin role always holds every scope and cannot be edited", 403, "ROLE_LOCKED"
        )
    if role_kind == KIND_FIXED:
        raise ApiError("This predefined role's scopes cannot be edited", 403, "ROLE_LOCKED")
