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

## Still on the broad community-admin gate (no dedicated scope yet)

Routes using `authorize_community(admin=True)` / default `require_community_admin` (any of
`community:manage_members` or `community:manage_channels`): music station and music queue admin,
streaming/stream config, token billing admin, marketplace lifecycle admin, bundle permissions and
activation (community tier), provider connections, member admin (`admin_service`: roles, reputation,
removal). Bundle admin gateway maps manifest-declared `community:moderate|admin` onto those two
scopes by design. Convert each by passing `scope=` (the helper already accepts it) and registering
the scope.
