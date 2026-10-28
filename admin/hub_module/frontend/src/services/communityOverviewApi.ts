import { apiClient } from '../lib/apiClient';
import {
  expectRecord,
  readArray,
  readBool,
  readNumber,
  readOptNumber,
  readOptString,
  readString,
} from '../lib/wire';

// Typed reads for the community dashboard's secondary panels, from served routes only:
//   GET /api/v1/communities/:id/streams            (live streams, camelCase DTO)
//   GET /api/v1/admin/:id/announcements?status=    (announcements, snake_case DTO under `data`)
// The dashboard's old single `GET /communities/:id/dashboard` call has no hub-api equivalent;
// the page is composed from these panels plus `/auth/me` and `/public/communities/:id`.

export interface LiveStream {
  entityId: string;
  platform: string;
  channelName: string;
  title: string;
  game: string;
  thumbnailUrl: string;
  viewerCount: number;
}

export interface Announcement {
  id: number;
  title: string;
  content: string;
  type: string;
  isPinned: boolean;
  createdByName: string | null;
  createdAt: string | null;
}

function parseStream(raw: unknown): LiveStream {
  const row = expectRecord(raw, 'streams[]');
  return {
    entityId: readString(row, 'entityId', 'streams[]'),
    platform: readOptString(row, 'platform') ?? '',
    channelName: readOptString(row, 'channelName') ?? 'Unknown channel',
    title: readOptString(row, 'title') ?? '',
    game: readOptString(row, 'game') ?? '',
    thumbnailUrl: readOptString(row, 'thumbnailUrl') ?? '',
    viewerCount: readOptNumber(row, 'viewerCount') ?? 0,
  };
}

function parseAnnouncement(raw: unknown): Announcement {
  const row = expectRecord(raw, 'announcements[]');
  return {
    id: readNumber(row, 'id', 'announcements[]'),
    title: readString(row, 'title', 'announcements[]'),
    content: readOptString(row, 'content') ?? '',
    type: readOptString(row, 'announcement_type') ?? 'general',
    isPinned: readBool(row, 'is_pinned'),
    createdByName: readOptString(row, 'created_by_name'),
    createdAt: readOptString(row, 'created_at'),
  };
}

/** `GET /api/v1/communities/:id/streams`. */
export async function fetchLiveStreams(communityId: number): Promise<LiveStream[]> {
  const { data } = await apiClient.get<unknown>(`/api/v1/communities/${communityId}/streams`);
  return readArray(expectRecord(data, 'streams'), 'streams', 'streams').map(parseStream);
}

/** `GET /api/v1/admin/:id/announcements?status=published`; rejected with 403/402 for non-admins. */
export async function fetchPublishedAnnouncements(communityId: number, limit: number): Promise<Announcement[]> {
  const { data } = await apiClient.get<unknown>(`/api/v1/admin/${communityId}/announcements`, {
    params: { status: 'published', limit },
  });
  return readArray(expectRecord(data, 'announcements'), 'data', 'announcements').map(parseAnnouncement);
}
