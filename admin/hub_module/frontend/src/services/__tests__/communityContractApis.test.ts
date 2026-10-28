/**
 * Parser-level tests for the typed served-route services: defaults for optional wire fields
 * and loud failures for required ones, plus the exact URL/params each function sends.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '../../lib/apiClient';
import { mockApiGet, ok } from '../../test/api';
import { fetchCommunityMembers, parseMemberPage } from '../communityMembersApi';
import { fetchLiveStreams, fetchPublishedAnnouncements } from '../communityOverviewApi';
import { fetchJoinRequests, fetchMyJoinRequest, reviewJoinRequest, submitJoinRequest } from '../joinRequestsApi';
import { fetchMyCommunities, fetchPublicCommunity, parsePublicCommunity, searchPublicCommunities } from '../myCommunitiesApi';
import { fetchReputationLeaderboard, parseReputationLeaderboard } from '../reputationApi';
import { fetchLeaderboard, fetchLiveActivity, fetchMyActivityStats, parseLeaderboard, parseLiveActivityEvent, parseMyStats } from '../communityActivityApi';

afterEach(() => vi.restoreAllMocks());

describe('myCommunitiesApi', () => {
  it('falls back to the community name, then a numbered label, for display names', () => {
    expect(parsePublicCommunity({ id: 1, name: 'bc' })).toMatchObject({ displayName: 'bc', memberCount: 0, description: null, joinMode: null });
    expect(parsePublicCommunity({ id: 2 })).toMatchObject({ displayName: 'Community 2', name: null });
    expect(parsePublicCommunity({ id: 3, displayName: 'Shown', name: 'x', memberCount: 9 })).toMatchObject({ displayName: 'Shown', memberCount: 9 });
  });

  it('defaults a membership displayName to its name and rejects a missing role', async () => {
    mockApiGet(() => ({ user: { communities: [{ id: 1, name: 'n', role: 'member' }] } }));
    await expect(fetchMyCommunities()).resolves.toEqual([{ id: 1, name: 'n', displayName: 'n', role: 'member' }]);
    mockApiGet(() => ({ user: { communities: [{ id: 1, name: 'n' }] } }));
    await expect(fetchMyCommunities()).rejects.toThrow('"role" is not a string');
  });

  it('requires the community wrapper and a list of results', async () => {
    mockApiGet(() => ({ success: true }));
    await expect(fetchPublicCommunity(5)).rejects.toThrow('missing "community"');
    mockApiGet(() => ({ communities: [{ id: 1, name: 'a' }] }));
    await expect(searchPublicCommunities('a', 3)).resolves.toHaveLength(1);
  });
});

describe('communityActivityApi', () => {
  it('defaults the period and treats has_more as false when absent', () => {
    const page = parseLeaderboard({ leaderboard: [], pagination: { offset: 0, limit: 5, total: 0 } });
    expect(page).toMatchObject({ period: 'alltime', hasMore: false, entries: [] });
  });

  it('maps null user ids and parses unranked stats', () => {
    const page = parseLeaderboard({
      leaderboard: [{ rank: 1, user_id: null, username: null, avatar_url: null, value: 5 }],
      pagination: { offset: 0, limit: 5, total: 1, has_more: true },
      period: 'weekly',
    });
    expect(page.entries[0]).toMatchObject({ userId: null, username: null, valueFormatted: null });
    expect(page.hasMore).toBe(true);
    const zero = { watch_time_seconds: 0, watch_time_formatted: '0s', message_count: 0 };
    expect(parseMyStats({ stats: { all_time: zero, weekly: zero, monthly: zero, ranks: { watch_time: 2, messages: null } } }).ranks).toEqual({ watchTime: 2, messages: null });
  });

  it('parses an event with only the required fields', () => {
    expect(parseLiveActivityEvent({ id: 1, platform: 'twitch', occurred_at: 't' })).toEqual({
      id: 1,
      platform: 'twitch',
      actor: null,
      messageIn: null,
      replyOut: null,
      occurredAt: 't',
    });
  });

  it('sends the documented URLs', async () => {
    const get = vi.spyOn(apiClient, 'get').mockImplementation(async (url) => {
      if (url.endsWith('/my-stats')) return ok({ stats: { all_time: {}, weekly: {}, monthly: {}, ranks: {} } });
      return ok({ events: [], leaderboard: [], pagination: { offset: 0, limit: 1, total: 0, has_more: false } });
    });
    await expect(fetchLiveActivity(3, 20)).resolves.toEqual([]);
    await fetchLeaderboard(3, 'messages', { period: 'alltime', limit: 10, offset: 20 });
    expect(get).toHaveBeenCalledWith('/api/v1/community/3/live-activity', { params: { limit: 20 } });
    expect(get).toHaveBeenCalledWith('/api/v1/community/3/leaderboard/messages', { params: { period: 'alltime', limit: 10, offset: 20 } });
    await expect(fetchMyActivityStats(3)).rejects.toThrow('"watch_time_seconds" is not a number');
  });
});

describe('communityMembersApi', () => {
  it('defaults username and totalPages, and omits blank filters from the request', async () => {
    const page = parseMemberPage({ members: [{ id: 1, role: 'member' }], pagination: { page: 1, limit: 25, total: 1 } });
    expect(page.members[0]).toMatchObject({ username: 'Unknown', userId: null, avatarUrl: null, joinedAt: null });
    expect(page.totalPages).toBe(1);
    const get = mockApiGet(() => ({ members: [], pagination: { page: 1, limit: 25, total: 0 } }));
    await fetchCommunityMembers(4, { page: 1, limit: 25, search: '  ', role: '' });
    expect(get).toHaveBeenCalledWith('/api/v1/admin/4/members', { params: { page: 1, limit: 25 } });
  });
});

describe('communityOverviewApi', () => {
  it('fills defaults for sparse streams and announcements', async () => {
    mockApiGet(() => ({ streams: [{ entityId: 'e1' }] }));
    await expect(fetchLiveStreams(2)).resolves.toEqual([
      { entityId: 'e1', platform: '', channelName: 'Unknown channel', title: '', game: '', thumbnailUrl: '', viewerCount: 0 },
    ]);
    const get = mockApiGet(() => ({ data: [{ id: 1, title: 'T' }] }));
    await expect(fetchPublishedAnnouncements(2, 5)).resolves.toEqual([
      { id: 1, title: 'T', content: '', type: 'general', isPinned: false, createdByName: null, createdAt: null },
    ]);
    expect(get).toHaveBeenCalledWith('/api/v1/admin/2/announcements', { params: { status: 'published', limit: 5 } });
  });
});

describe('joinRequestsApi', () => {
  it('sends a null message for blank input and echoes a trimmed-empty message as null', async () => {
    const post = vi
      .spyOn(apiClient, 'post')
      .mockResolvedValue(ok({ request: { id: 1, status: 'pending', created_at: 't' } }, 201));
    await expect(submitJoinRequest(7, '   ')).resolves.toMatchObject({ id: 1, message: null, reviewedAt: null });
    expect(post).toHaveBeenCalledWith('/api/v1/7/join-requests', { message: null });
    await submitJoinRequest(7, 'hi');
    expect(post).toHaveBeenLastCalledWith('/api/v1/7/join-requests', { message: 'hi' });
  });

  it('reads the caller request and tolerates its absence', async () => {
    mockApiGet(() => ({ request: { id: 2, status: 'rejected', message: 'm', created_at: 'c', reviewed_at: 'r' } }));
    await expect(fetchMyJoinRequest(7)).resolves.toMatchObject({ status: 'rejected', reviewedAt: 'r' });
    mockApiGet(() => ({ request: null }));
    await expect(fetchMyJoinRequest(7)).resolves.toBeNull();
  });

  it('defaults sparse admin rows and totalPages; reviews with PUT', async () => {
    mockApiGet(() => ({ requests: [{ id: 1, status: 'pending', created_at: 'c' }], pagination: { total: 1, page: 1 } }));
    await expect(fetchJoinRequests(7)).resolves.toMatchObject({ totalPages: 1, requests: [{ username: 'Unknown', email: null, message: null }] });
    const put = vi.spyOn(apiClient, 'put').mockResolvedValue(ok({ success: true }));
    await reviewJoinRequest(7, 3, 'reject');
    expect(put).toHaveBeenCalledWith('/api/v1/admin/7/join-requests/3/reject');
  });
});

describe('reputationApi', () => {
  it('parses the envelope and rejects a missing data wrapper', async () => {
    expect(parseReputationLeaderboard({ data: { entries: [{ display_name: 'a', score: 700, tier: 'Trusted' }] } })).toEqual([
      { displayName: 'a', score: 700, tier: 'Trusted' },
    ]);
    expect(() => parseReputationLeaderboard({ entries: [] })).toThrow('reputation leaderboard data: expected an object');
    const get = mockApiGet(() => ({ data: { entries: [] } }));
    await fetchReputationLeaderboard(5, 50);
    expect(get).toHaveBeenCalledWith('/api/v1/community/5/reputation/leaderboard', { params: { limit: 50 } });
  });
});
