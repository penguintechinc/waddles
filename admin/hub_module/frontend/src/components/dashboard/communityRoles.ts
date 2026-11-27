import type { BadgeTone } from '../ui';
import { ROLE_KIND_ADMIN } from '../../services/communityRolesApi';

// Display helpers only. Authorization is scope-based (`useCommunityScopes`); nothing here
// decides what a caller may do.

/** `community-admin` -> `Admin`; unknown roles are title-cased as-is. */
export function roleLabel(role: string): string {
  const bare = role.replace('community-', '');
  return bare === '' ? 'Member' : bare.charAt(0).toUpperCase() + bare.slice(1);
}

/** Badge tone by role kind code (admin kind is highlighted); the label always accompanies it. */
export function roleTone(roleKind: number | null | undefined): BadgeTone {
  return roleKind === ROLE_KIND_ADMIN ? 'sky' : 'gray';
}
