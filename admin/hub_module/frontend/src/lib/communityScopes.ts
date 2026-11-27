/**
 * Community scope strings the webui gates on. They mirror `hub_api/services/scope_registry.py`
 * and the scope each guarded hub-api route enforces; the server stays authoritative and the UI
 * only hides what the caller could not use. Never compare role names for authorization.
 */
export const COMMUNITY_SCOPES = {
  admin: 'community:admin',
  membersView: 'community.members:view',
  rolesView: 'community.roles:view',
  membersRoles: 'members:roles.manage',
  membersRemove: 'members:remove',
  streamConfig: 'stream:config.manage',
  providerConnections: 'provider.connections:manage',
  bundlesModerate: 'bundles.admin:moderate',
  bundlesAdmin: 'bundles.admin:admin',
  announcementsRead: 'community.announcements:read',
} as const;

/** Holding any of these means the caller has at least one community admin surface to open. */
export const ADMIN_SURFACE_SCOPES: readonly string[] = [
  COMMUNITY_SCOPES.admin,
  COMMUNITY_SCOPES.membersView,
  COMMUNITY_SCOPES.bundlesModerate,
  COMMUNITY_SCOPES.bundlesAdmin,
  COMMUNITY_SCOPES.providerConnections,
];

/** True when `scopes` holds at least one admin-surface scope. */
export function hasAdminSurface(scopes: readonly string[] | undefined): boolean {
  return Array.isArray(scopes) && scopes.some((scope) => ADMIN_SURFACE_SCOPES.includes(scope));
}
