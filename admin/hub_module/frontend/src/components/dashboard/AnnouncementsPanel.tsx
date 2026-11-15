import { Link } from 'react-router-dom';
import { MapPinIcon, MegaphoneIcon } from '@heroicons/react/24/outline';
import { useAnnouncements } from '../../hooks/useCommunityPanels';
import type { Announcement } from '../../services/communityOverviewApi';
import { Badge, Card, CardHeader, PanelState, type BadgeTone } from '../ui';

const TYPE_TONE: Record<string, BadgeTone> = { important: 'red', event: 'gold', update: 'sky' };

function AnnouncementItem({ announcement }: { announcement: Announcement }) {
  return (
    <li className="p-4" data-testid="announcement">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            {announcement.isPinned ? <MapPinIcon className="h-4 w-4 text-fg-accent" aria-label="Pinned" /> : null}
            <h3 className="font-medium text-fg">{announcement.title}</h3>
          </div>
          <p className="mt-1 line-clamp-2 text-sm text-fg-secondary">{announcement.content}</p>
          <p className="mt-2 text-xs text-fg-muted">
            {announcement.createdByName ?? 'Admin'}
            {announcement.createdAt ? ` · ${new Date(announcement.createdAt).toLocaleDateString()}` : ''}
          </p>
        </div>
        <Badge tone={TYPE_TONE[announcement.type] ?? 'gray'}>
          {announcement.type.charAt(0).toUpperCase() + announcement.type.slice(1)}
        </Badge>
      </div>
    </li>
  );
}

/**
 * Latest published announcements (community admins only; the server rejects everyone else, so
 * non-managers are never queried). Every response shows a state: empty, plan (402), unavailable (503), error.
 */
export function AnnouncementsPanel({ communityId, canManage }: { communityId: number; canManage: boolean }) {
  const query = useAnnouncements(communityId, 5, canManage);
  if (!canManage) return null;
  return (
    <Card as="section" className="mb-8" aria-label="Announcements">
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            <MegaphoneIcon className="h-5 w-5 text-fg-accent" aria-hidden="true" />
            Announcements
          </span>
        }
        actions={
          <Link to={`/admin/${communityId}/announcements`} className="text-sm text-interactive hover:text-interactive-hover">
            Manage
          </Link>
        }
      />
      <PanelState
        query={query}
        feature="Announcements"
        isEmpty={(items) => items.length === 0}
        emptyTitle="No announcements yet"
        emptyDescription="Published announcements appear here. Announcement channels are set up in your connections."
        emptyAction={{ to: `/admin/${communityId}/connections`, label: 'Connect Discord or Twitch' }}
        className="py-8"
      >
        {(items) => (
          <ul className="divide-y divide-line">
            {items.slice(0, 3).map((announcement) => (
              <AnnouncementItem key={announcement.id} announcement={announcement} />
            ))}
          </ul>
        )}
      </PanelState>
    </Card>
  );
}
