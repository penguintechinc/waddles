/**
 * Tier gating of the three formerly `user.isPremium`-gated admin pages. The tier comes from the
 * real AuthProvider reading /auth/me `communities[].licenseTier` (a missing field = Free).
 * Communities are Free (1) or Premium (2) only. Bot detection + analytics need Premium; AI
 * providers: Free = Ollama / WaddleAI, Premium adds OpenAI / Gemini / Anthropic (BYOK). The UI gate
 * is a hint only (the server is authoritative).
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

/** Mounts a page at /admin/00000007-0000-4000-8000-000000000000/x with /auth/me reporting community 7 at `tier` (undefined = field absent). */
async function mount(Page, tier) {
  const community = { id: '00000007-0000-4000-8000-000000000000', name: 'acme', role: 'community-admin' };
  if (tier !== undefined) community.licenseTier = tier;
  api.get.mockResolvedValue({ data: { success: true, user: { id: 1, communities: [community] } } });
  render(
    <MemoryRouter initialEntries={['/admin/00000007-0000-4000-8000-000000000000/x']}>
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

describe('AdminBotDetection tier gate (Premium)', () => {
  it.each([[1], [undefined], [3]])('is locked with an informative message for tier %s (missing/unknown = Free)', async (tier) => {
    await mount(AdminBotDetection, tier);
    expect(await screen.findByText('Bot Detection requires Premium')).toBeInTheDocument();
    expect(screen.getByTestId('tier-locked').textContent).toMatch(/on the Free tier/);
    expect(screen.queryByRole('heading', { name: 'Bot Detection' })).not.toBeInTheDocument();
  });

  it('is unlocked for Premium (tier 2)', async () => {
    await mount(AdminBotDetection, 2);
    expect(await screen.findByRole('heading', { name: 'Bot Detection' })).toBeInTheDocument();
    expect(screen.queryByTestId('tier-locked')).not.toBeInTheDocument();
  });
});

describe('AdminAnalytics tier gate (Premium)', () => {
  it.each([[1], [undefined]])('shows the locked hint and skips premium calls for tier %s', async (tier) => {
    await mount(AdminAnalytics, tier);
    expect(await screen.findByText('Advanced analytics requires Premium')).toBeInTheDocument();
    expect(screen.queryByText('Community Health Score')).not.toBeInTheDocument();
    expect(adminApi.getAnalyticsHealthScore).not.toHaveBeenCalled();
  });

  it('loads premium analytics for Premium (tier 2)', async () => {
    await mount(AdminAnalytics, 2);
    expect(await screen.findByText('Community Health Score')).toBeInTheDocument();
    expect(screen.queryByText('Advanced analytics requires Premium')).not.toBeInTheDocument();
  });
});

describe('AdminAIResearcherConfig provider matrix', () => {
  const provider = () => screen.findByLabelText('AI Provider');
  const optionDisabled = async (id) => (await provider()).querySelector(`option[value="${id}"]`).disabled;

  it.each([[1], [undefined]])('Free (tier %s): Ollama and WaddleAI selectable, OpenAI/Gemini/Anthropic locked', async (tier) => {
    await mount(AdminAIResearcherConfig, tier);
    expect(await optionDisabled('ollama')).toBe(false);
    expect(await optionDisabled('waddleai')).toBe(false);
    for (const id of ['openai', 'gemini', 'anthropic']) expect(await optionDisabled(id)).toBe(true);
    const locked = (await provider()).querySelector('option[value="openai"]');
    expect(locked.textContent).toMatch(/Requires Premium/);
    expect(await screen.findByText(/OpenAI, Gemini and Anthropic \(bring your own key\) require Premium/)).toBeInTheDocument();
  });

  it('Premium: all five providers selectable', async () => {
    await mount(AdminAIResearcherConfig, 2);
    for (const id of ['ollama', 'waddleai', 'openai', 'gemini', 'anthropic']) expect(await optionDisabled(id)).toBe(false);
    expect(screen.queryByText(/require Premium/)).not.toBeInTheDocument();
  });

  it.each([[1], [undefined]])('disables bot detection toggle with a requires-Premium hint for tier %s', async (tier) => {
    await mount(AdminAIResearcherConfig, tier);
    expect(await screen.findByText(/Advanced bot detection requires the Premium tier/)).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Bot Detection' })).toBeDisabled();
  });

  it('enables the bot detection toggle for Premium', async () => {
    await mount(AdminAIResearcherConfig, 2);
    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'Bot Detection' })).toBeEnabled());
  });
});
