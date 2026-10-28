import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { LeaderboardCard } from '../LeaderboardCard';
import { avatarInitial, formatDuration, formatEntryValue, periodLabel } from '../leaderboardFormat';
import { httpError, mockApiGet, unexpectedUrl } from '../../../test/api';
import { renderWithProviders } from '../../../test/renderWithProviders';

const row = (rank: number, over: Record<string, unknown> = {}) => ({
  rank,
  user_id: rank,
  username: `u${rank}`,
  avatar_url: null,
  value: rank * 60,
  value_formatted: `${rank}m`,
  ...over,
});
const page = (rows: unknown[]) => ({
  success: true,
  leaderboard: rows,
  pagination: { offset: 0, limit: 5, total: rows.length, has_more: false },
  period: 'weekly',
});

describe('LeaderboardCard', () => {
  afterEach(() => vi.restoreAllMocks());

  it('renders the top rows from the served route and links to the full board', async () => {
    const get = mockApiGet((url) => (url === '/api/v1/community/7/leaderboard/watch-time' ? page([row(1), row(2, { avatar_url: 'https://x/a.png' })]) : unexpectedUrl(url)));
    renderWithProviders(<LeaderboardCard communityId={7} title="Top Viewers" limit={5} />);
    const rows = await screen.findAllByTestId('leaderboard-card-row');
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText('u1')).toBeInTheDocument();
    expect(within(rows[0]!).getByText('1m')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith('/api/v1/community/7/leaderboard/watch-time', { params: { period: 'weekly', limit: 5, offset: 0 } });
    expect(screen.getByRole('heading', { name: 'Top Viewers' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View Full Leaderboard' })).toHaveAttribute('href', '/dashboard/community/7/leaderboard?type=watch-time');
  });

  it('uses the messages route and a count format for type="messages"', async () => {
    mockApiGet((url) => (url === '/api/v1/community/7/leaderboard/messages' ? page([row(1, { value: 4321, value_formatted: null })]) : unexpectedUrl(url)));
    renderWithProviders(<LeaderboardCard communityId={7} type="messages" />);
    expect(await screen.findByText('4,321')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Messages' })).toBeInTheDocument();
  });

  it('re-queries when the period changes', async () => {
    const get = mockApiGet(() => page([row(1)]));
    renderWithProviders(<LeaderboardCard communityId={7} />);
    await screen.findByTestId('leaderboard-card-row');
    fireEvent.click(screen.getByRole('button', { name: 'All' }));
    await waitFor(() => expect(get).toHaveBeenCalledWith('/api/v1/community/7/leaderboard/watch-time', { params: { period: 'alltime', limit: 5, offset: 0 } }));
  });

  it('shows empty and error states instead of a blank card', async () => {
    mockApiGet(() => page([]));
    const first = renderWithProviders(<LeaderboardCard communityId={7} />);
    expect(await screen.findByText('No data yet for this period')).toBeInTheDocument();
    first.unmount();

    mockApiGet(() => {
      throw httpError(500, { error: 'nope' });
    });
    renderWithProviders(<LeaderboardCard communityId={7} />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('nope')).toBeInTheDocument();
    expect(within(alert).getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });
});

describe('leaderboardFormat', () => {
  it('formats durations, values, initials and period labels', () => {
    expect(formatDuration(45)).toBe('45s');
    expect(formatDuration(600)).toBe('10m');
    expect(formatDuration(3900)).toBe('1h 5m');
    const entry = { rank: 1, userId: null, username: null, avatarUrl: null, value: 125, valueFormatted: null };
    expect(formatEntryValue('watch-time', entry)).toBe('2m');
    expect(formatEntryValue('watch-time', { ...entry, valueFormatted: '9h' })).toBe('9h');
    expect(formatEntryValue('messages', { ...entry, value: 1500 })).toBe('1,500');
    expect(avatarInitial('zed')).toBe('Z');
    expect(avatarInitial(null)).toBe('?');
    expect(periodLabel('alltime')).toBe('All Time');
    expect(periodLabel('alltime', true)).toBe('All');
    expect(periodLabel('weekly', true)).toBe('Week');
    expect(periodLabel('monthly')).toBe('Monthly');
  });
});
