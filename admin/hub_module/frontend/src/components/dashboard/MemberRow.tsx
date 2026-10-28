import { Link } from 'react-router-dom';
import type { CommunityMember } from '../../services/communityMembersApi';
import { isFeatureServed } from '../../lib/unservedFeatures';
import { Badge } from '../ui';
import { formatJoined } from './memberFormat';
import { roleLabel, roleTone } from './communityRoles';

/**
 * One roster entry. It links to the member's profile only while hub-api serves member
 * profiles; until then it is a plain list item rather than a link into a dead page.
 */
export function MemberRow({ member }: { member: CommunityMember }) {
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
          <Badge tone={roleTone(member.role)}>{roleLabel(member.role)}</Badge>
        </span>
        <span className="mt-1 block text-sm text-fg-secondary">Joined {formatJoined(member.joinedAt)}</span>
      </span>
    </>
  );
  const rowClass = 'flex items-center gap-4 rounded-lg bg-surface-raised p-4';
  return (
    <li data-testid="member-row">
      {member.userId && isFeatureServed('member-profile') ? (
        <Link to={`/users/${member.userId}`} className={`${rowClass} transition-colors hover:bg-line`}>
          {body}
        </Link>
      ) : (
        <div className={rowClass}>{body}</div>
      )}
    </li>
  );
}
