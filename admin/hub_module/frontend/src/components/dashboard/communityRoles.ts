import type { BadgeTone } from '../ui';

/** Community roles that can see admin surfaces (mirrors hub-api's admin/moderator gate). */
export const COMMUNITY_ADMIN_ROLES: readonly string[] = ['community-owner', 'community-admin', 'moderator'];

/** True when `role` is a community owner, admin or moderator. */
export function isCommunityAdminRole(role: string | undefined): boolean {
  return role !== undefined && COMMUNITY_ADMIN_ROLES.includes(role);
}

/** `community-admin` -> `Admin`; unknown roles are title-cased as-is. */
export function roleLabel(role: string): string {
  const bare = role.replace('community-', '');
  return bare === '' ? 'Member' : bare.charAt(0).toUpperCase() + bare.slice(1);
}

/** Badge tone per role; the label text always accompanies it (never colour-only). */
export function roleTone(role: string): BadgeTone {
  switch (role) {
    case 'community-owner':
      return 'gold';
    case 'community-admin':
    case 'moderator':
      return 'sky';
    default:
      return 'gray';
  }
}

/**
 * Roster role filters. hub-api matches `role` exactly, so each filter is ONE role; the legacy
 * "Owners & Admins" comma list would silently match nobody.
 */
export const COMMUNITY_ROLE_FILTERS: readonly { value: string; label: string }[] = [
  { value: '', label: 'All Members' },
  { value: 'community-owner', label: 'Owners' },
  { value: 'community-admin', label: 'Admins' },
  { value: 'moderator', label: 'Moderators' },
  { value: 'vip', label: 'VIPs' },
  { value: 'member', label: 'Members' },
];
