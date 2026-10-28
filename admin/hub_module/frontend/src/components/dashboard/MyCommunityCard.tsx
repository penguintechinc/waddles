import { Link } from 'react-router-dom';
import type { MyCommunity, PublicCommunity } from '../../services/myCommunitiesApi';
import { getPlatformIcon, getPlatformLabel } from '../../utils/platformConfig';
import { Badge } from '../ui';
import { roleLabel, roleTone } from './communityRoles';

export interface MyCommunityCardProps {
  community: MyCommunity;
  /** Public details (logo, description, member count) when the server could supply them. */
  details?: PublicCommunity | undefined;
}

/**
 * One tile on the post-login landing. `/auth/me` is enough to render the name and
 * role; the logo, description and member count appear only when public details loaded.
 */
export function MyCommunityCard({ community, details }: MyCommunityCardProps) {
  const platform = details?.platform ?? null;
  return (
    <Link
      to={`/dashboard/community/${community.id}`}
      className="card card-interactive group block overflow-hidden"
      data-testid="community-card"
    >
      <div className="flex aspect-video items-center justify-center bg-surface-raised">
        {details?.logoUrl ? (
          <img src={details.logoUrl} alt="" className="h-full w-full object-cover" />
        ) : (
          <img src="/waddlebot-logo.png" alt="" className="h-16 w-16" />
        )}
      </div>
      <div className="p-4">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            {platform ? <span title={getPlatformLabel(platform)}>{getPlatformIcon(platform)}</span> : null}
            <h3 className="truncate font-semibold text-fg">{community.displayName}</h3>
          </div>
          <Badge tone={roleTone(community.role)} className="whitespace-nowrap">
            {roleLabel(community.role)}
          </Badge>
        </div>
        {details ? (
          <>
            <p className="line-clamp-2 text-sm text-fg-secondary">{details.description ?? 'No description'}</p>
            <p className="mt-3 text-xs text-fg-muted">{details.memberCount.toLocaleString()} members</p>
          </>
        ) : null}
      </div>
    </Link>
  );
}
