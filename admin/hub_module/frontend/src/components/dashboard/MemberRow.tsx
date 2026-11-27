import { Link } from 'react-router-dom';
import type { CommunityMember } from '../../services/communityMembersApi';
import type { CommunityRoleOption } from '../../services/communityRolesApi';
import { isFeatureServed } from '../../lib/unservedFeatures';
import { Badge, Button } from '../ui';
import { formatJoined } from './memberFormat';
import { roleTone } from './communityRoles';

export interface MemberRowProps {
  member: CommunityMember;
  /** All community roles: drives the badge tone (by kind) and the change-role options. */
  roles: readonly CommunityRoleOption[];
  /** Passed only when the caller holds `members:roles.manage`. */
  onChangeRole?: (roleId: string) => void;
  /** Passed only when the caller holds `members:remove`. */
  onRemove?: () => void;
  busy?: boolean;
}

/**
 * One roster entry with optional scope-gated actions. It links to the member's profile only
 * while hub-api serves member profiles; until then it is a plain item, not a dead link.
 */
export function MemberRow({ member, roles, onChangeRole, onRemove, busy = false }: MemberRowProps) {
  const kind = roles.find((r) => r.id === member.roleId)?.roleKind;
  const body = (
    <>
      {member.avatarUrl ? (
        <img src={member.avatarUrl} alt="" className="h-12 w-12 rounded-full object-cover" />
      ) : (
        <span
          aria-hidden="true"
          className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-line text-lg font-semibold text-fg"
        >
          {member.username.charAt(0).toUpperCase()}
        </span>
      )}
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className="truncate font-medium text-fg">{member.username}</span>
          <Badge tone={roleTone(kind)}>{member.roleName ?? 'No role'}</Badge>
        </span>
        <span className="mt-1 block text-sm text-fg-secondary">Joined {formatJoined(member.joinedAt)}</span>
      </span>
    </>
  );
  const rowClass = 'flex items-center gap-4';
  return (
    <li data-testid="member-row" className="rounded-lg bg-surface-raised p-4">
      {isFeatureServed('member-profile') ? (
        <Link to={`/users/${member.userId}`} className={`${rowClass} transition-colors hover:bg-line`}>
          {body}
        </Link>
      ) : (
        <div className={rowClass}>{body}</div>
      )}
      {onChangeRole || onRemove ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {onChangeRole ? (
            <select
              aria-label={`Role for ${member.username}`}
              className="input"
              disabled={busy}
              value={member.roleId ?? ''}
              onChange={(event) => {
                if (event.target.value !== '') onChangeRole(event.target.value);
              }}
            >
              {member.roleId === null ? <option value="">No role</option> : null}
              {roles.map((role) => (
                <option key={role.id} value={role.id}>
                  {role.displayName}
                </option>
              ))}
            </select>
          ) : null}
          {onRemove ? (
            <Button variant="danger" disabled={busy} onClick={onRemove} aria-label={`Remove ${member.username}`}>
              Remove
            </Button>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
