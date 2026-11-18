/** CommunityDashboard passes the community's tier (not a hardcoded false) to the bot score card. */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import CommunityDashboard from '../CommunityDashboard';
import { mockApiGet, unexpectedUrl } from '../../../test/api';
import { renderWithProviders } from '../../../test/renderWithProviders';

const tierRef = vi.hoisted(() => ({ value: 1 as number }));
vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'tux' },
    isAuthenticated: true,
    isSuperAdmin: false,
    refreshUser: vi.fn(),
    communityTier: () => tierRef.value,
  }),
}));
vi.mock('../../../components/dashboard/CommunityReputationPanel', () => ({ default: () => null }));
vi.mock('../../../components/BotScoreCard', () => ({
  default: ({ isPremium }: { isPremium: boolean }) => <div data-testid="bot-score-card" data-premium={String(isPremium)} />,
}));
vi.mock('../../../components/ui', async (orig) => {
  const actual = await orig<typeof import('../../../components/ui')>();
  return { ...actual, ServedFeature: ({ children }: { children: React.ReactNode }) => <>{children}</> };
});

function serve() {
  mockApiGet((url) => {
    if (url === '/api/v1/auth/me') {
      return { success: true, user: { id: 1, communities: [{ id: 7, name: 'acme', displayName: 'Acme', role: 'community-admin' }] } };
    }
    if (url === '/api/v1/public/communities/7') {
      return { success: true, community: { id: 7, name: 'acme', displayName: 'Acme HQ', description: 'd', platform: 'discord', memberCount: 1, joinMode: 'open', createdAt: '2025-01-01T00:00:00Z' } };
    }
    if (url === '/api/v1/communities/7/streams') return { success: true, streams: [] };
    if (url === '/api/v1/admin/7/announcements') return { success: true, data: [], pagination: {} };
    if (url.startsWith('/api/v1/community/7/leaderboard/')) {
      return { success: true, leaderboard: [], pagination: { offset: 0, limit: 5, total: 0, has_more: false }, period: 'weekly' };
    }
    return unexpectedUrl(url);
  });
}

const render = () => renderWithProviders(<CommunityDashboard />, { route: '/dashboard/community/7', path: '/dashboard/community/:id' });

describe('CommunityDashboard bot score tier', () => {
  beforeEach(() => vi.spyOn(console, 'error').mockImplementation(() => {}));

  it.each([[1, 'false'], [2, 'true'], [3, 'true']])('tier %s -> isPremium=%s', async (tier, expected) => {
    tierRef.value = tier;
    serve();
    render();
    expect((await screen.findByTestId('bot-score-card')).getAttribute('data-premium')).toBe(expected);
  });
});
