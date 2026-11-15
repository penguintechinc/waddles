/**
 * Landing stats: the page renders the cached snapshot with a freshness label on 200 and a
 * "warming" message (no crash, no retry loop) when hub-api answers 503 {"status":"warming"}.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import HomePage from '../HomePage';

const api = vi.hoisted(() => ({ getStats: vi.fn(), getSpotlightedCommunities: vi.fn() }));

vi.mock('../../../services/api', () => ({ publicApi: api }));

function renderPage() {
  return render(
    <MemoryRouter>
      <HomePage />
    </MemoryRouter>,
  );
}

describe('HomePage landing stats', () => {
  beforeEach(() => {
    api.getStats.mockReset();
    api.getSpotlightedCommunities.mockReset();
    api.getSpotlightedCommunities.mockResolvedValue({ data: { communities: [] } });
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders stats and "Updated N min ago" on 200', async () => {
    const computedAt = new Date(Date.now() - 3 * 60000).toISOString();
    api.getStats.mockResolvedValue({
      data: {
        success: true,
        computedAt,
        stats: {
          communities: 42,
          discord: { servers: 7, channels: 9 },
          twitch: { channels: 5, live: 1, viewers: 3 },
          slack: { workspaces: 2, channels: 2 },
        },
      },
    });
    renderPage();
    expect(await screen.findByText('42')).toBeTruthy();
    expect(screen.getByText(/Updated 3 min ago/)).toBeTruthy();
    expect(screen.queryByText(/warming up/)).toBeNull();
  });

  it('shows the warming message on 503 without crashing or retrying', async () => {
    api.getStats.mockRejectedValue({ response: { status: 503, data: { status: 'warming' } } });
    renderPage();
    expect(await screen.findByText(/Stats are warming up/)).toBeTruthy();
    await waitFor(() => expect(screen.queryByText('...')).toBeNull());
    expect(screen.queryByText(/Updated/)).toBeNull();
    expect(api.getStats).toHaveBeenCalledTimes(1);
  });
});
