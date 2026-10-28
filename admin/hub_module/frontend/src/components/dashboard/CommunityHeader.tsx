import { Link } from 'react-router-dom';
import type { MyCommunity, PublicCommunity } from '../../services/myCommunitiesApi';
import { roleLabel } from './communityRoles';

export interface CommunityHeaderProps {
  membership: MyCommunity;
  /** Public details (logo, name) when available; the header renders from `membership` alone. */
  details: PublicCommunity | undefined;
  canAdminister: boolean;
}

/** Community logo, name, the caller's role and, for admins, the admin-panel entry point. */
export function CommunityHeader({ membership, details, canAdminister }: CommunityHeaderProps) {
  const name = details?.displayName ?? membership.displayName;
  return (
    <header className="mb-8 flex flex-wrap items-center justify-between gap-4">
      <div className="flex items-center gap-4">
        <div className="flex h-16 w-16 items-center justify-center overflow-hidden rounded-xl border border-line bg-surface-raised">
          {details?.logoUrl ? (
            <img src={details.logoUrl} alt="" className="h-full w-full object-cover" />
          ) : (
            <img src="/waddlebot-logo.png" alt="" className="h-12 w-12" />
          )}
        </div>
        <div>
          <h1 className="text-2xl font-bold text-fg">{name}</h1>
          <p className="text-fg-secondary">{roleLabel(membership.role)}</p>
        </div>
      </div>
      {canAdminister ? (
        <Link to={`/admin/${membership.id}`} className="btn btn-primary">
          Admin Panel
        </Link>
      ) : null}
    </header>
  );
}
