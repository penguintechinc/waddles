/**
 * Community leaderboard against the SERVED `/api/v1/community/:id/...` routes (singular
 * `/community/`, snake_case wire). The legacy page called `/communities/:id/leaderboard/*`
 * (not served) and read camelCase fields the server never sent.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import CommunityLeaderboard from '../CommunityLeaderboard';
import { httpError, mockApiGet, unexpectedUrl } from '../../../test/api';
import { renderWithProviders } from '../../../test/renderWithProviders';

const entry = (rank: number, over: Record<string, unknown> = {}) => ({
  rank,
  user_id: rank + 100,
  username: `viewer${rank}`,
  avatar_url: null,
  value: rank * 3600,
  value_formatted: `${rank}h 0m`,
  ...over,
});

const board = (entries: unknown[], over: Record<string, unknown> = {}) => ({
  success: true,
  leaderboard: entries,
  pagination: { offset: 0, limit: 25, total: entries.length, has_more: false },
  period: 'weekly',
  ...over,
});

const period = (watch: number, formatted: string, messages: number) => ({
  watch_time_seconds: watch,
  watch_time_formatted: formatted,
  message_count: messages,
});

const STATS = {
  success: true,
  stats: {
    all_time: period(9000, '2h 30m', 90),
    weekly: period(3660, '1h 1m', 42),
    monthly: period(7200, '2h 0m', 70),
    ranks: { watch_time: 3, messages: null },
  },
};

const ME = { success: true, user: { id: 1, communities: [{ id: 7, name: 'bc', displayName: 'Bar Citizen', role: 'member' }] } };

type Handler = (url: string, params: unknown) => unknown;

function serve(overrides: Handler = () => undefined) {
  return mockApiGet((url, params) => {
    const custom = overrides(url, params);
    if (custom !== undefined) return custom;
    if (url === '/api/v1/auth/me') return ME;
    if (url === '/api/v1/community/7/activity/my-stats') return STATS;
    if (url === '/api/v1/community/7/leaderboard/watch-time') return board([entry(1), entry(2)]);
    if (url === '/api/v1/community/7/leaderboard/messages') return board([entry(1, { value: 12345, value_formatted: null })]);
    return unexpectedUrl(url);
  });
}

function renderPage(route = '/dashboard/community/7/leaderboard') {
  return renderWithProviders(<CommunityLeaderboard />, { route, path: '/dashboard/community/:id/leaderboard' });
}

describe('CommunityLeaderboard', () => {
  beforeEach(() => vi.spyOn(console, 'error').mockImplementation(() => {}));
  afterEach(() => vi.restoreAllMocks());

  it('loads watch-time ranks from the served route and maps the snake_case wire shape', async () => {
    const get = serve();
    renderPage();
    await screen.findByText('viewer1');
    const table = screen.getByRole('table');
    expect(within(table).getByText('1h 0m')).toBeInTheDocument();
    expect(within(table).getByText('2h 0m')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith('/api/v1/community/7/leaderboard/watch-time', {
      params: { period: 'weekly', limit: 25, offset: 0 },
    });
    expect(get.mock.calls.map(([url]) => url).filter((url) => url.includes('/communities/'))).toEqual([]);
    expect(screen.getByRole('link', { name: 'Bar Citizen' })).toHaveAttribute('href', '/dashboard/community/7');
  });

  it('shows my weekly stats and ranks, with a dash for an unranked board', async () => {
    serve();
    renderPage();
    const stats = await screen.findByTestId('my-stats');
    expect(within(stats).getByText('1h 1m')).toBeInTheDocument();
    expect(within(stats).getByText('42')).toBeInTheDocument();
    expect(within(stats).getByText('#3')).toBeInTheDocument();
    expect(within(stats).getByText('-')).toBeInTheDocument();
  });

  it('switches to the messages tab and formats counts', async () => {
    const get = serve();
    renderPage();
    await screen.findByText('viewer1');
    fireEvent.click(screen.getByRole('tab', { name: 'Messages' }));
    expect(await screen.findByText('12,345')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith('/api/v1/community/7/leaderboard/messages', {
      params: { period: 'weekly', limit: 25, offset: 0 },
    });
  });

  it('honours ?type=messages on first load', async () => {
    const get = serve();
    renderPage('/dashboard/community/7/leaderboard?type=messages');
    expect(await screen.findByText('12,345')).toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith('/api/v1/community/7/leaderboard/watch-time', expect.anything());
  });

  it('re-queries with the chosen period and resets to the first page', async () => {
    const get = serve();
    renderPage();
    await screen.findByText('viewer1');
    fireEvent.click(screen.getByRole('button', { name: 'Monthly' }));
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith('/api/v1/community/7/leaderboard/watch-time', {
        params: { period: 'monthly', limit: 25, offset: 0 },
      }),
    );
    expect(screen.getByRole('button', { name: 'Monthly' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('paginates with offset when the server reports more rows', async () => {
    const get = serve((url, params) => {
      if (url !== '/api/v1/community/7/leaderboard/watch-time') return undefined;
      const offset = (params as { offset: number }).offset;
      return offset === 0
        ? board([entry(1)], { pagination: { offset: 0, limit: 25, total: 30, has_more: true } })
        : board([entry(26)], { pagination: { offset: 25, limit: 25, total: 30, has_more: false } });
    });
    renderPage();
    await screen.findByText('Showing 1 to 25 of 30');
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(await screen.findByText('viewer26')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith('/api/v1/community/7/leaderboard/watch-time', {
      params: { period: 'weekly', limit: 25, offset: 25 },
    });
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  });

  it('shows the empty state for a period with no activity', async () => {
    serve((url) => (url === '/api/v1/community/7/leaderboard/watch-time' ? board([]) : undefined));
    renderPage();
    expect(await screen.findByText('No activity data yet for this period')).toBeInTheDocument();
  });

  it('shows a retryable error for a failed board, while stats failure stays inline', async () => {
    let calls = 0;
    serve((url) => {
      if (url === '/api/v1/community/7/activity/my-stats') throw httpError(402, { success: false, error: { message: 'Community activity tracking is not enabled for this plan' } });
      if (url !== '/api/v1/community/7/leaderboard/watch-time') return undefined;
      calls += 1;
      if (calls === 1) throw httpError(500, { error: 'Leaderboard exploded' });
      return board([entry(1)]);
    });
    renderPage();
    const alerts = await screen.findAllByRole('alert');
    const boardError = alerts.find((a) => within(a).queryByText('Leaderboard exploded'));
    expect(boardError).toBeDefined();
    expect(await screen.findByText('Community activity tracking is not enabled for this plan')).toBeInTheDocument();
    fireEvent.click(within(boardError!).getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('viewer1')).toBeInTheDocument();
  });

  it('turns the legacy camelCase shape into a visible contract error', async () => {
    serve((url) =>
      url === '/api/v1/community/7/leaderboard/watch-time'
        ? { success: true, leaderboard: [{ rank: 1, userId: 1, username: 'x', watchTimeFormatted: '1h' }], pagination: { offset: 0, limit: 25, total: 1, hasMore: false } }
        : undefined,
    );
    renderPage();
    expect(await screen.findByText(/Unexpected response from the server \(leaderboard\[\]: "value" is not a number\)/)).toBeInTheDocument();
  });

  it('rejects an invalid id without querying', () => {
    const get = serve();
    renderPage('/dashboard/community/abc/leaderboard');
    expect(screen.getByText('This leaderboard link is not valid.')).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });
});
