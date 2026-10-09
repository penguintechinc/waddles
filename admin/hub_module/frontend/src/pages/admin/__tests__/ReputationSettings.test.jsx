import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { adminApi } from '../../../services/api';
import ReputationSettings from '../ReputationSettings';

vi.mock('../../../services/api', () => ({
  adminApi: {
    getReputationConfig: vi.fn(),
    updateReputationConfig: vi.fn(),
    getAtRiskUsers: vi.fn(),
    getReputationLeaderboard: vi.fn(),
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
    <MemoryRouter initialEntries={['/c/5/rep']}>
      <Routes>
        <Route path="/c/:communityId/rep" element={<ReputationSettings />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('ReputationSettings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
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

  it('leaderboard tab: fetches, renders rows, and hides settings actions', async () => {
    adminApi.getReputationLeaderboard.mockResolvedValue({
      data: {
        success: true,
        users: [
          { userId: 1, username: 'zed', rank: 1, reputation: { score: 810, label: 'Top' } },
          { userId: 2, username: 'amy', avatarUrl: 'http://x/y.png', reputation: { score: 500 } },
          { userId: 3, username: 'noscore' },
        ],
      },
    });
    const { container } = renderPage();
    await screen.findByText('Reputation Tiers');
    fireEvent.click(screen.getByText('Leaderboard'));
    expect(await screen.findByText('zed')).toBeInTheDocument();
    expect(adminApi.getReputationLeaderboard).toHaveBeenCalledWith('5', { limit: 50 });
    expect(screen.getByText('Top')).toBeInTheDocument();
    expect(screen.getByText('#1')).toBeInTheDocument();
    expect(screen.getByText('#2')).toBeInTheDocument();
    expect(screen.getByText('Z')).toBeInTheDocument();
    expect(container.querySelector('img')).toHaveAttribute('src', 'http://x/y.png');
    expect(screen.queryByText('Save Changes')).not.toBeInTheDocument();
  });

  it('leaderboard tab: empty and error states', async () => {
    adminApi.getReputationLeaderboard.mockResolvedValue({ data: { success: true } });
    renderPage();
    await screen.findByText('Reputation Tiers');
    fireEvent.click(screen.getByText('Leaderboard'));
    expect(await screen.findByText('No Reputation Data')).toBeInTheDocument();
    adminApi.getReputationLeaderboard.mockRejectedValue(new Error('x'));
    fireEvent.click(screen.getByText('Settings'));
    fireEvent.click(screen.getByText('Leaderboard'));
    await waitFor(() => expect(adminApi.getReputationLeaderboard).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('No Reputation Data')).toBeInTheDocument();
  });
});
