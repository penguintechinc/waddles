import { Link } from 'react-router-dom';
import { MapPinIcon, MegaphoneIcon } from '@heroicons/react/24/outline';
import { useAnnouncements } from '../../hooks/useCommunityPanels';
import type { Announcement } from '../../services/communityOverviewApi';
import { Alert, Badge, Card, CardHeader, type BadgeTone } from '../ui';
import { getErrorMessage } from '../ui/errors';

/** Statuses meaning "this community/plan/role simply has no announcements feed for you": not an error. */
const EXPECTED_DENIALS = new Set([401, 402, 403, 404]);

const TYPE_TONE: Record<string, BadgeTone> = { important: 'red', event: 'gold', update: 'sky' };

function statusOf(error: unknown): number | undefined {
  if (typeof error !== 'object' || error === null || !('response' in error)) return undefined;
  const response = error.response;
  if (typeof response !== 'object' || response === null || !('status' in response)) return undefined;
  return typeof response.status === 'number' ? response.status : undefined;
}

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
 * Latest published announcements. Renders nothing when there are none or when the server
 * says this plan/role has no feed (expected 401-404); any other failure is shown, not hidden.
 */
export function AnnouncementsPanel({ communityId, canManage }: { communityId: number; canManage: boolean }) {
  const query = useAnnouncements(communityId, 5, true);
  if (query.isPending) return null;
  if (query.isError) {
    const status = statusOf(query.error);
    if (status !== undefined && EXPECTED_DENIALS.has(status)) return null;
    return (
      <Alert tone="warning" title="We could not load announcements" className="mb-8">
        {getErrorMessage(query.error)}
      </Alert>
    );
  }
  if (query.data.length === 0) return null;
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
          canManage ? (
            <Link to={`/admin/${communityId}/announcements`} className="text-sm text-interactive hover:text-interactive-hover">
              Manage
            </Link>
          ) : null
        }
      />
      <ul className="divide-y divide-line">
        {query.data.slice(0, 3).map((announcement) => (
          <AnnouncementItem key={announcement.id} announcement={announcement} />
        ))}
      </ul>
    </Card>
  );
}
