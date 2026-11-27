# Roles and scopes

A **role** is a named group of **scopes**, stored as data. Authorization checks scopes only -- never
role names. The role name is kept for display and audit.

## Where roles live

| Layer | Definition | Expanded into scopes |
|---|---|---|
| Global / tenant | `flask_core.auth.SCOPE_BUNDLES` + `hub_users.is_super_admin` / `tenant_admins.role` | At token issuance (`auth_service._scopes_for`): the JWT `scope` claim |
| Community | `community_roles` rows (`base_claims.scopes`, `role_kind`) | At request time from the caller's membership (`community_authz`); never carried on the JWT |

Global admin is the `users:admin` scope on the token (the only token claim that bypasses community
checks). A `roles` claim such as `super_admin` or `platform-admin` authorizes nothing.

## Predefined roles (`community_roles.role_kind`)

| Kind | Roles | Delete | Scope edit | Scopes |
|---|---|---|---|---|
| 1 admin | `community-admin`, `community-owner` | refused (409 `ROLE_PROTECTED`) | refused (403 `ROLE_LOCKED`) | **computed**: every registered scope |
| 2 moderator | `moderator` | refused (409) | allowed, needs `roles:manage` | stored list (seeded with the original moderator scopes) |
| 3 fixed | `member`, `communicator`, `speaker` | refused (409) | refused (403) | stored list |
| 0 custom | anything an admin creates | allowed | allowed | stored list |

The kind is set by migration `0117_community_role_kind` (backfill plus a `BEFORE INSERT` trigger for
`seed_community_system_roles()`), so the guards read data, not names.

Every community gets its predefined roles from the database function `seed_community_system_roles()`
(the only definition of their names and scopes), called inside community create's transaction; the
creator is linked to the system `community-owner` role. A member holds one role through
`community_members.community_role_id`, and authz resolves scopes and the role name only through
that link: a member with no linked role row holds no scopes and no role name, whatever its legacy
`role` string says. All membership-role writes go through
`hub_api/services/member_roles.set_member_role` (it also keeps the legacy `role` string in sync).
Migration `0146_community_role_backfill` repaired communities created before this.

- **Reserved names.** The seeded names (`member`, `communicator`, `speaker`, `moderator`,
  `community-admin`, `community-owner`) are reserved: creating a custom role with one is a `400
  ROLE_NAME_RESERVED`, and every lookup by a reserved name matches only the `is_system` row, so a
  same-named custom role never stands in for a system role.
- **Owner protection.** The owner (the member linked to the system owner role) can be demoted or
  removed only by a global or tenant admin. Any other caller also needs every scope the target
  holds. The check and the write run in one transaction under a row lock
  (`community_authz.lock_member_for_change`). The platform view finds the owner the same way.
- **Last-owner guard.** A community never ends up with zero owners. Demoting, removing, banning or
  deactivating an owner is refused with `409 LAST_OWNER` (even for a global or tenant admin) unless
  another active owner remains; transfer ownership first. The same guard locks the community's
  owner rows `FOR UPDATE` before the target, so parallel demotions of the last two owners leave
  exactly one, and a transfer that promotes the new owner before demoting the old one passes.

## Role management rules

* Create, edit and delete need the `roles:manage` scope (held by the admin kind; global and tenant
  admins hold every registered scope).
* The editor can only **grant** scopes they hold themselves (403 `SCOPE_ESCALATION`). Removing a scope
  the editor lacks is allowed.
* The editor must hold **every scope currently on the target role** to edit its scopes or delete it
  (no downgrading or deleting a role more powerful than yourself).
* Scopes must be registered (400 `UNKNOWN_SCOPE`).
* Each mutation is recorded as `role.definition_changed` in the audit chain (declarative route
  classification in `services/audit_events.py`) and logged with ids and a scope count only.

## The scope registry (`hub_api/services/scope_registry.py`)

`register_scopes({"rooms:create": "Create rooms", ...})` adds scopes; the call is idempotent and
additive. The admin kind is computed from the registry, so a newly registered scope reaches admin with
no data change and never reaches the moderator or custom roles until an authorized admin grants it.
`GET /api/v1/admin/<id>/interaction/scopes` lists the registry for the role editor.

### Plugging in the room scopes (`rooms:create/update/close/guests/kick/mute/lock/hands.*`)

1. In the module that owns the room feature, call `register_scopes` at import time with every scope
   and a one-line description. Import that module from `hub_api` startup (or add the dict to
   `scope_registry.py`'s seed list).
2. Nothing else for admin: it picks them up automatically.
3. Grant to moderators or custom roles through `PUT .../interaction/roles/<id>` (needs `roles:manage`,
   and the editor must hold the scopes).
4. Enforce at the receiver with the scope, not a role name. For hub-api routes use the community
   membership scopes (`authorize_community` -> `membership.scopes`, or `member_scopes_sync`).

## Role-name branches removed

| Was | Now |
|---|---|
| `_BYPASS_ROLES` / `"platform-admin" in roles` JWT-claim bypass (`community_authz`) | `users:admin` token scope |
| `_COMMUNITY_ADMIN_ROLES` (`marketplace_billing_service`) | `member_scopes_sync` and admin scopes |
| `role.belongs(["admin","owner"])` (`github_sync_service`) | dedicated `github:manage` scope |
| `_ADMIN_ROLES` (`community_access.require_community_admin`, overlay/calls admin) | dedicated `community:admin` scope |
| `_ADMIN_ROLE_NAMES` (`community_profile_service.update_community_profile`) | dedicated `community.profile:manage` scope |
| `ADMIN_PROMOTION_ROLES` (`admin_service.update_member_role`) | assigner must hold every scope of the assigned role; an undefined role is refused (400), never assigned |

Per-action scopes (`github:manage`, `community:admin`, `community.profile:manage`, `community.branding:manage`, `billing:manage`, `community.members:manage`, `workflow:manage`, `media.byok:manage`, `ai.routing:manage`, `overlay:manage`, `calls:manage`, `roles:manage`) are
registered, so the admin kind holds them automatically and the moderator kind does not unless an
authorized admin grants them.

Retained on purpose: `tenant_admins.role` -> scope bundle expansion at token issuance (that is the
role-to-scopes step itself), and `target.role == "community-owner"` in `admin_service` (member change/removal)
protects the community's owner row, a data invariant on the *target*, not authorization of the caller.

## Per-action scope map

Every privileged community action has its own registered scope (an AST test fails if any blueprint
admin gate omits `scope=`). The admin kind holds all of them (computed). `M` = seeded on the
predefined moderator role (moderation-level actions moderators could already perform).

| Action | Scope | Moderator |
|---|---|---|
| Role create/edit/delete | `roles:manage` | no |
| Assign member roles | `members:roles.manage` | no |
| Remove members | `members:remove` | M |
| Reputation adjust/config/soft-bans | `members:reputation.manage` | M |
| Join requests | `community.members:manage` | M |
| Logo/banner | `community.branding:manage` | M |
| Overlay admin | `overlay:manage` | M |
| Calls admin | `calls:manage` | M |
| Music station admin | `music:station.manage` | M |
| Music queue admin | `music:queue.manage` | M |
| Bundle admin tools, moderation level | `bundles.admin:moderate` | M |
| Bundle admin tools, admin level | `bundles.admin:admin` | no |
| Community settings | `community.settings:manage` | no |
| Member list (admin view) | `community.members:view` | M |
| Module configuration | `community.modules:manage` | no |
| Temporary passwords | `members:temp_password.reset` | no |
| Connected platforms | `community.platforms:manage` | no |
| Community commands | `community.commands:manage` | no |
| Hub channels and overrides | `community.channels:manage` | M |
| Forum moderation | `community.forums:manage` | M |
| Role/scope listing | `community.roles:view` | M |
| Profile edit | `community.profile:manage` | no |
| Marketplace billing | `billing:manage` | no |
| Token billing admin | `token.billing:manage` | no |
| Marketplace lifecycle | `marketplace.lifecycle:manage` | no |
| Bundle permissions / activation | `bundles.permissions:manage`, `bundles.activation:manage` | no |
| Provider connections | `provider.connections:manage` | no |
| Stream config and keys | `stream:config.manage` | no |
| GitHub sync | `github:manage` | no |
| Workflow | `workflow:manage` | no |
| Media BYOK / AI routing | `media.byok:manage`, `ai.routing:manage` | no |
| Community admin (default for `community_access.require_community_admin`) | `community:admin` | no |

Migration `0117` merges the moderator list into every moderator-kind row and a trigger does the same
for future communities; `scope_registry.MODERATOR_SEED_SCOPES` mirrors it (a test asserts equality).

No JWT-scope admin gates remain: `admin.py` and the interaction admin routes resolve every scope
from the caller's community role (plus global/tenant-admin bypass); an AST test enforces it.
