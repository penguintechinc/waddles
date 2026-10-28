import { apiClient } from '../lib/apiClient';
import {
  expectRecord,
  readArray,
  readNumber,
  readOptNumber,
  readOptString,
  readString,
  readBool,
  type WireRecord,
} from '../lib/wire';

// Typed reads for community activity, mirroring hub_api/blueprints/v1/community_activity.py
// and live_activity.py. Those blueprints serialise snake_case; the legacy UI read camelCase
// (`userId`, `watchTimeFormatted`, `messageCount`, `hasMore`) from the old Node contract, so
// every field was silently undefined even where the path matched. These parsers are the one
// place that translates the wire shape into the view-model the pages render.
//
// Served routes (note the SINGULAR `/community/` prefix; `/communities/:id/leaderboard/*` is
// not a hub-api route):
//   GET /api/v1/community/:id/leaderboard/watch-time | messages
//   GET /api/v1/community/:id/activity/my-stats
//   GET /api/v1/community/:id/live-activity

export type LeaderboardType = 'watch-time' | 'messages';
export type LeaderboardPeriod = 'weekly' | 'monthly' | 'alltime';

export const LEADERBOARD_PERIODS: readonly LeaderboardPeriod[] = ['weekly', 'monthly', 'alltime'];

/** One ranked row; `value` is seconds for watch-time, a count for messages. */
export interface LeaderboardEntry {
  rank: number;
  userId: number | null;
  username: string | null;
  avatarUrl: string | null;
  value: number;
  valueFormatted: string | null;
}

export interface LeaderboardPage {
  entries: LeaderboardEntry[];
  offset: number;
  limit: number;
  total: number;
  hasMore: boolean;
  period: string;
}

export interface LeaderboardQuery {
  period: LeaderboardPeriod;
  limit: number;
  offset: number;
}

export interface PeriodStats {
  watchTimeSeconds: number;
  watchTimeFormatted: string;
  messageCount: number;
}

export interface MyActivityStats {
  allTime: PeriodStats;
  weekly: PeriodStats;
  monthly: PeriodStats;
  ranks: { watchTime: number | null; messages: number | null };
}

/** One bot-interaction event from the live-activity feed (backfill and SSE share this). */
export interface LiveActivityEvent {
  id: number;
  platform: string;
  actor: string | null;
  messageIn: string | null;
  replyOut: string | null;
  occurredAt: string;
}

function parseEntry(raw: unknown): LeaderboardEntry {
  const row = expectRecord(raw, 'leaderboard[]');
  return {
    rank: readNumber(row, 'rank', 'leaderboard[]'),
    userId: readOptNumber(row, 'user_id'),
    username: readOptString(row, 'username'),
    avatarUrl: readOptString(row, 'avatar_url'),
    value: readNumber(row, 'value', 'leaderboard[]'),
    valueFormatted: readOptString(row, 'value_formatted'),
  };
}

/** Parses a `LeaderboardResponse` body. */
export function parseLeaderboard(data: unknown): LeaderboardPage {
  const body = expectRecord(data, 'leaderboard');
  const pagination = expectRecord(body['pagination'], 'leaderboard pagination');
  return {
    entries: readArray(body, 'leaderboard', 'leaderboard').map(parseEntry),
    offset: readNumber(pagination, 'offset', 'leaderboard pagination'),
    limit: readNumber(pagination, 'limit', 'leaderboard pagination'),
    total: readNumber(pagination, 'total', 'leaderboard pagination'),
    hasMore: readBool(pagination, 'has_more'),
    period: readOptString(body, 'period') ?? 'alltime',
  };
}

function parsePeriod(raw: unknown, what: string): PeriodStats {
  const row: WireRecord = expectRecord(raw, what);
  return {
    watchTimeSeconds: readNumber(row, 'watch_time_seconds', what),
    watchTimeFormatted: readString(row, 'watch_time_formatted', what),
    messageCount: readNumber(row, 'message_count', what),
  };
}

/** Parses a `MyActivityStatsResponse` body. */
export function parseMyStats(data: unknown): MyActivityStats {
  const stats = expectRecord(expectRecord(data, 'my-stats')['stats'], 'my-stats stats');
  const ranks = expectRecord(stats['ranks'], 'my-stats ranks');
  return {
    allTime: parsePeriod(stats['all_time'], 'my-stats all_time'),
    weekly: parsePeriod(stats['weekly'], 'my-stats weekly'),
    monthly: parsePeriod(stats['monthly'], 'my-stats monthly'),
    ranks: { watchTime: readOptNumber(ranks, 'watch_time'), messages: readOptNumber(ranks, 'messages') },
  };
}

/** Parses one live-activity event (an element of the list body, or an SSE `data:` frame). */
export function parseLiveActivityEvent(raw: unknown): LiveActivityEvent {
  const row = expectRecord(raw, 'live-activity event');
  return {
    id: readNumber(row, 'id', 'live-activity event'),
    platform: readString(row, 'platform', 'live-activity event'),
    actor: readOptString(row, 'actor'),
    messageIn: readOptString(row, 'message_in'),
    replyOut: readOptString(row, 'reply_out'),
    occurredAt: readString(row, 'occurred_at', 'live-activity event'),
  };
}

/** `GET /api/v1/community/:id/leaderboard/{watch-time,messages}`. */
export async function fetchLeaderboard(
  communityId: number,
  type: LeaderboardType,
  query: LeaderboardQuery,
): Promise<LeaderboardPage> {
  const { data } = await apiClient.get<unknown>(`/api/v1/community/${communityId}/leaderboard/${type}`, {
    params: query,
  });
  return parseLeaderboard(data);
}

/** `GET /api/v1/community/:id/activity/my-stats`. */
export async function fetchMyActivityStats(communityId: number): Promise<MyActivityStats> {
  const { data } = await apiClient.get<unknown>(`/api/v1/community/${communityId}/activity/my-stats`);
  return parseMyStats(data);
}

/** `GET /api/v1/community/:id/live-activity?limit=`; the body is `{ success, events }`, newest first. */
export async function fetchLiveActivity(communityId: number, limit: number): Promise<LiveActivityEvent[]> {
  const { data } = await apiClient.get<unknown>(`/api/v1/community/${communityId}/live-activity`, {
    params: { limit },
  });
  return readArray(expectRecord(data, 'live-activity'), 'events', 'live-activity').map(parseLiveActivityEvent);
}
