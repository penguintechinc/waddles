/**
 * Tier gating of the three formerly `user.isPremium`-gated admin pages. The tier comes from the
 * real AuthProvider reading /auth/me `communities[].licenseTier` (a missing field = Free).
 * Thresholds mirror the server gates: bot detection + analytics Professional+, premium AI
 * providers Enterprise, BYOK custom endpoint Professional+. The UI gate is a hint only.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import api, { adminApi } from '../../../services/api';
import { AuthProvider } from '../../../contexts/AuthContext';
import AdminAIResearcherConfig from '../AdminAIResearcherConfig';
import AdminAnalytics from '../AdminAnalytics';
import AdminBotDetection from '../AdminBotDetection';

vi.mock('../../../services/api', () => ({
  default: { get: vi.fn(), post: vi.fn() },
  adminApi: {
    getBotDetections: vi.fn(),
    getBotScore: vi.fn(),
    getAnalyticsBasic: vi.fn(),
    getAnalyticsHealthScore: vi.fn(),
    getAnalyticsBadActors: vi.fn(),
    getAnalyticsRetention: vi.fn(),
    getAIResearcherConfig: vi.fn(),
    getAvailableAIModels: vi.fn(),
  },
}));
vi.mock('../../../components/BotScoreBadge', () => ({ default: () => null }));
vi.mock('../../../components/FormModal', () => ({ FormModalBuilder: () => null }));

/** Mounts a page at /admin/7/x with /auth/me reporting community 7 at `tier` (undefined = field absent). */
async function mount(Page, tier) {
  const community = { id: 7, name: 'acme', role: 'community-admin' };
  if (tier !== undefined) community.licenseTier = tier;
  api.get.mockResolvedValue({ data: { success: true, user: { id: 1, communities: [community] } } });
  render(
    <MemoryRouter initialEntries={['/admin/7/x']}>
      <AuthProvider>
        <Routes><Route path="/admin/:communityId/x" element={<Page />} /></Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, 'error').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  adminApi.getBotDetections.mockResolvedValue({ data: { success: true, detections: [], pagination: null } });
  adminApi.getBotScore.mockResolvedValue({ data: { success: true, botScore: null } });
  adminApi.getAnalyticsBasic.mockResolvedValue({ data: { data: { totalMembers: 1 } } });
  adminApi.getAnalyticsHealthScore.mockResolvedValue({ data: { data: { score: 77, status: 'ok' } } });
  adminApi.getAnalyticsBadActors.mockResolvedValue({ data: { data: [] } });
  adminApi.getAnalyticsRetention.mockResolvedValue({ data: { data: {} } });
  adminApi.getAIResearcherConfig.mockResolvedValue({ data: { success: true, config: null } });
  adminApi.getAvailableAIModels.mockResolvedValue({ data: { success: true, data: {} } });
});

describe('AdminBotDetection tier gate (Professional+)', () => {
  it.each([[1], [undefined]])('is locked with an informative message for tier %s (missing = Free)', async (tier) => {
    await mount(AdminBotDetection, tier);
    expect(await screen.findByText('Bot Detection requires Professional')).toBeInTheDocument();
    expect(screen.getByTestId('tier-locked').textContent).toMatch(/on the Free tier/);
    expect(screen.queryByRole('heading', { name: 'Bot Detection' })).not.toBeInTheDocument();
  });

  it.each([[2], [3]])('is unlocked for tier %s', async (tier) => {
    await mount(AdminBotDetection, tier);
    expect(await screen.findByRole('heading', { name: 'Bot Detection' })).toBeInTheDocument();
    expect(screen.queryByTestId('tier-locked')).not.toBeInTheDocument();
  });
});

describe('AdminAnalytics tier gate (Professional+)', () => {
  it.each([[1], [undefined]])('shows the locked hint and skips premium calls for tier %s', async (tier) => {
    await mount(AdminAnalytics, tier);
    expect(await screen.findByText('Advanced analytics requires Professional')).toBeInTheDocument();
    expect(screen.queryByText('Community Health Score')).not.toBeInTheDocument();
    expect(adminApi.getAnalyticsHealthScore).not.toHaveBeenCalled();
  });

  it.each([[2], [3]])('loads premium analytics for tier %s', async (tier) => {
    await mount(AdminAnalytics, tier);
    expect(await screen.findByText('Community Health Score')).toBeInTheDocument();
    expect(screen.queryByText('Advanced analytics requires Professional')).not.toBeInTheDocument();
  });
});

describe('AdminAIResearcherConfig tier gate', () => {
  const provider = () => screen.findByLabelText('AI Provider');
  const openaiOption = async () => (await provider()).querySelector('option[value="openai"]');

  it.each([[1], [2], [undefined]])('locks premium providers below Enterprise (tier %s)', async (tier) => {
    await mount(AdminAIResearcherConfig, tier);
    expect((await openaiOption()).disabled).toBe(true);
    expect(await screen.findByText(/Premium AI providers require the Enterprise tier/)).toBeInTheDocument();
  });

  it('unlocks premium providers for Enterprise', async () => {
    await mount(AdminAIResearcherConfig, 3);
    expect((await openaiOption()).disabled).toBe(false);
  });

  it.each([[1], [undefined]])('disables bot detection toggle with a requires-Professional hint for tier %s', async (tier) => {
    await mount(AdminAIResearcherConfig, tier);
    expect(await screen.findByText(/Advanced bot detection requires the Professional tier/)).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Bot Detection' })).toBeDisabled();
  });

  it.each([[2], [3]])('enables the bot detection toggle for tier %s', async (tier) => {
    await mount(AdminAIResearcherConfig, tier);
    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'Bot Detection' })).toBeEnabled());
  });
});
