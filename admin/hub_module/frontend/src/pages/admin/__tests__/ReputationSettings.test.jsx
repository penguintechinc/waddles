import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { adminApi } from '../../../services/api';
import { isFeatureServed } from '../../../lib/unservedFeatures';
import { httpError, mockApiGet, unexpectedUrl } from '../../../test/api';
import { createTestQueryClient } from '../../../test/renderWithProviders';
import { QueryClientProvider } from '@tanstack/react-query';
import ReputationSettings from '../ReputationSettings';

// The config + at-risk endpoints are not served by hub-api today, so the registry marks them
// unserved. The editor tests below exercise the editor as it will behave once they are served.
vi.mock('../../../lib/unservedFeatures', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, isFeatureServed: vi.fn(() => true) };
});

vi.mock('../../../services/api', () => ({
  adminApi: {
    getReputationConfig: vi.fn(),
    updateReputationConfig: vi.fn(),
    getAtRiskUsers: vi.fn(),
  },
}));

function rawConfig(over = {}) {
  return {
    isPremium: true,
    canCustomize: true,
    weights: { chatMessage: 0.5, subscription: 10, warn: -5, timeout: -10 },
    policy: { autoBanEnabled: false, autoBanThreshold: 450, startingScore: 600 },
    ...over,
  };
}

function renderPage() {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <MemoryRouter initialEntries={['/c/5/rep']}>
        <Routes>
          <Route path="/c/:communityId/rep" element={<ReputationSettings />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const SERVED_LEADERBOARD = '/api/v1/community/5/reputation/leaderboard';
const envelope = (entries) => ({ status: 'success', data: { entries }, meta: { version: 1 } });

describe('ReputationSettings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    isFeatureServed.mockReturnValue(true);
    vi.spyOn(console, 'error').mockImplementation(() => {});
    adminApi.getReputationConfig.mockResolvedValue({ data: { success: true, config: rawConfig() } });
  });

  it('loads config for the route community and renders tiers + previews', async () => {
    renderPage();
    expect(await screen.findByText('Reputation Tiers')).toBeInTheDocument();
    expect(adminApi.getReputationConfig).toHaveBeenCalledWith('5');
    expect(screen.getByText('Exceptional')).toBeInTheDocument();
    // chat 0.5 -> 20 msgs; sub 10 -> ceil(200/10)=20; warn -5 -> ceil(150/5)=30
    expect(screen.getAllByText('20')).toHaveLength(2);
    expect(screen.getByText('30')).toBeInTheDocument();
    expect(screen.queryByText('Premium Feature')).not.toBeInTheDocument();
  });

  it('shows infinity previews and premium banner for non-premium zero weights', async () => {
    adminApi.getReputationConfig.mockResolvedValue({
      data: { success: true, config: { isPremium: false, weights: {}, policy: {} } },
    });
    renderPage();
    expect(await screen.findByText('Premium Feature')).toBeInTheDocument();
    expect(screen.getAllByText('∞')).toHaveLength(3);
    expect(screen.getAllByRole('spinbutton')[0]).toBeDisabled();
  });

  it('shows the fallback when load fails', async () => {
    adminApi.getReputationConfig.mockRejectedValue(new Error('x'));
    renderPage();
    expect(await screen.findByText('Failed to load configuration')).toBeInTheDocument();
  });

  it('shows the fallback when API reports success=false', async () => {
    adminApi.getReputationConfig.mockResolvedValue({ data: { success: false } });
    renderPage();
    expect(await screen.findByText('Failed to load configuration')).toBeInTheDocument();
  });

  it('edits weights, toggles auto-ban, and saves the config', async () => {
    adminApi.updateReputationConfig.mockResolvedValue({});
    renderPage();
    await screen.findByText('Reputation Tiers');
    const inputs = screen.getAllByRole('spinbutton');
    fireEvent.change(inputs[0], { target: { value: '2' } });
    fireEvent.change(inputs[1], { target: { value: '' } });
    fireEvent.click(screen.getByRole('checkbox'));
    const ranges = screen.getAllByRole('slider');
    expect(ranges).toHaveLength(2);
    fireEvent.change(ranges[0], { target: { value: '700' } });
    fireEvent.change(ranges[1], { target: { value: '400' } });
    fireEvent.click(screen.getByText('Save Changes'));
    await screen.findByText('Reputation configuration saved');
    const [cid, body] = adminApi.updateReputationConfig.mock.calls[0];
    expect(cid).toBe('5');
    expect(body).toMatchObject({
      chat_message: 2,
      command_usage: 0,
      auto_ban_enabled: true,
      starting_score: 700,
      auto_ban_threshold: 400,
    });
    fireEvent.click(screen.getByText('x'));
    expect(screen.queryByText('Reputation configuration saved')).not.toBeInTheDocument();
  });

  it.each([
    [{ response: { status: 403 } }, 'Premium feature: Upgrade to customize weights'],
    [{ response: { status: 400, data: { error: { message: 'bad weights' } } } }, 'bad weights'],
    [new Error('net'), 'Failed to save configuration'],
  ])('surfaces save error %#', async (err, text) => {
    adminApi.updateReputationConfig.mockRejectedValue(err);
    renderPage();
    await screen.findByText('Reputation Tiers');
    fireEvent.click(screen.getByText('Save Changes'));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it('opens and closes the at-risk modal with users', async () => {
    adminApi.getAtRiskUsers.mockResolvedValue({
      data: {
        success: true,
        users: [
          { id: 1, username: 'alice', platform: 'discord', reputation: 470 },
          { id: 2, username: 'bob', platform: 'twitch', reputation: 820, avatar_url: 'http://x/a.png' },
        ],
      },
    });
    renderPage();
    await screen.findByText('Reputation Tiers');
    fireEvent.click(screen.getByText('View At-Risk Users'));
    expect(await screen.findByText('At-Risk Users')).toBeInTheDocument();
    expect(screen.getByText('alice')).toBeInTheDocument();
    expect(screen.getByText('470')).toBeInTheDocument();
    fireEvent.click(screen.getByText('At-Risk Users').nextSibling);
    expect(screen.queryByText('At-Risk Users')).not.toBeInTheDocument();
  });

  it('shows empty at-risk state and tolerates missing users field', async () => {
    adminApi.getAtRiskUsers.mockResolvedValue({ data: { success: true } });
    renderPage();
    await screen.findByText('Reputation Tiers');
    fireEvent.click(screen.getByText('View At-Risk Users'));
    expect(await screen.findByText('No users near the auto-ban threshold')).toBeInTheDocument();
  });

  it('surfaces at-risk fetch failure', async () => {
    adminApi.getAtRiskUsers.mockRejectedValue(new Error('x'));
    renderPage();
    await screen.findByText('Reputation Tiers');
    fireEvent.click(screen.getByText('View At-Risk Users'));
    expect(await screen.findByText('Failed to load at-risk users')).toBeInTheDocument();
  });

  it('leaderboard tab: reads the served route, renders rows, and hides settings actions', async () => {
    const get = mockApiGet((url) =>
      url === SERVED_LEADERBOARD
        ? envelope([
            { display_name: 'zed', score: 810, tier: 'Legend' },
            { display_name: 'amy', score: 500, tier: 'Newcomer' },
          ])
        : unexpectedUrl(url)
    );
    renderPage();
    await screen.findByText('Reputation Tiers');
    fireEvent.click(screen.getByText('Leaderboard'));
    expect(await screen.findByText('zed')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith(SERVED_LEADERBOARD, { params: { limit: 50 } });
    expect(screen.getByText('Legend')).toBeInTheDocument();
    expect(screen.getByText('#1')).toBeInTheDocument();
    expect(screen.getByText('#2')).toBeInTheDocument();
    expect(screen.getByText('810')).toBeInTheDocument();
    expect(screen.getByText('500')).toBeInTheDocument();
    expect(screen.queryByText('Save Changes')).not.toBeInTheDocument();
    // The legacy admin route is gone from the contract.
    expect(get.mock.calls.map(([u]) => u)).not.toContain('/api/v1/admin/5/reputation/leaderboard');
  });

  it('leaderboard tab: empty state', async () => {
    mockApiGet(() => envelope([]));
    renderPage();
    await screen.findByText('Reputation Tiers');
    fireEvent.click(screen.getByText('Leaderboard'));
    expect(await screen.findByText('No Reputation Data')).toBeInTheDocument();
  });

  it('leaderboard tab: shows the server error with retry instead of an empty table', async () => {
    let calls = 0;
    mockApiGet(() => {
      calls += 1;
      if (calls === 1) throw httpError(402, { status: 'error', error: { code: 'FEATURE_NOT_ENABLED', message: 'Community reputation is not enabled' } });
      return envelope([{ display_name: 'zed', score: 700, tier: 'Trusted' }]);
    });
    renderPage();
    await screen.findByText('Reputation Tiers');
    fireEvent.click(screen.getByText('Leaderboard'));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Community reputation is not enabled');
    expect(screen.queryByText('No Reputation Data')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('zed')).toBeInTheDocument();
  });

  describe('while hub-api does not serve the config or at-risk routes', () => {
    beforeEach(() => {
      isFeatureServed.mockImplementation((key) => !['reputation-config', 'reputation-at-risk'].includes(key));
    });

    it('flags settings as not available yet and makes no dead calls', async () => {
      renderPage();
      const state = await screen.findByTestId('not-available-yet');
      expect(state).toHaveAttribute('data-feature', 'reputation-config');
      expect(screen.getByText("Reputation settings isn't available yet")).toBeInTheDocument();
      expect(screen.queryByText('Save Changes')).not.toBeInTheDocument();
      expect(screen.queryByText('View At-Risk Users')).not.toBeInTheDocument();
      expect(screen.queryByText('Failed to load configuration')).not.toBeInTheDocument();
      expect(adminApi.getReputationConfig).not.toHaveBeenCalled();
      expect(adminApi.getAtRiskUsers).not.toHaveBeenCalled();
    });

    it('still serves the leaderboard tab', async () => {
      mockApiGet((url) => (url === SERVED_LEADERBOARD ? envelope([{ display_name: 'zed', score: 810, tier: 'Legend' }]) : unexpectedUrl(url)));
      renderPage();
      await screen.findByTestId('not-available-yet');
      fireEvent.click(screen.getByText('Leaderboard'));
      expect(await screen.findByText('zed')).toBeInTheDocument();
    });
  });
});
