/**
 * Licensed-feature 402 handling for the pages behind the remaining alpha 402s (#1466 sweep):
 * every gated page renders the shared "Upgrade to Premium for this feature" state instead of an
 * error banner or a blank page, while a non-402 failure keeps the page's own error handling.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const mockState = vi.hoisted(() => ({ failure: null }));

/** Every API method rejects with the current failure (set per test). */
vi.mock('../../services/api', () => {
  const group = () =>
    new Proxy(
      {},
      {
        get: (_t, name) => (name === 'then' ? undefined : vi.fn(() => Promise.reject(mockState.failure))),
      },
    );
  const api = { get: vi.fn(() => Promise.reject(mockState.failure)), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return {
    default: api,
    adminApi: group(),
    calendarApi: group(),
    inventoryApi: group(),
    interactionApi: group(),
    channelPermissionsApi: group(),
    rolesApi: group(),
  };
});
vi.mock('@penguintechinc/react-libs', async () => ({
  FormModalBuilder: (await import('../../test/formModalStub')).FormModalStub,
}));

import LoyaltySettings from '../admin/LoyaltySettings';
import LoyaltyLeaderboard from '../admin/LoyaltyLeaderboard';
import AdminRaffleCustomization from '../admin/AdminRaffleCustomization';
import AdminInventory from '../admin/AdminInventory';
import AdminCalendarEvents from '../admin/AdminCalendarEvents';
import AdminInteractionChannels from '../admin/AdminInteractionChannels';
import AdminStreamOverlays from '../admin/AdminStreamOverlays';
import BookingPages from '../calendar/BookingPages';
import MyBookings from '../calendar/MyBookings';
import CalendarSettings from '../calendar/CalendarSettings';

const licensed402 = () => ({
  response: {
    status: 402,
    data: { success: false, error: { code: 'FEATURE_NOT_ENABLED', message: 'not enabled for this plan' } },
  },
});

const PAGES = [
  ['LoyaltySettings', LoyaltySettings],
  ['LoyaltyLeaderboard', LoyaltyLeaderboard],
  ['AdminRaffleCustomization', AdminRaffleCustomization],
  ['AdminInventory', AdminInventory],
  ['AdminCalendarEvents', AdminCalendarEvents],
  ['AdminInteractionChannels', AdminInteractionChannels],
  ['AdminStreamOverlays', AdminStreamOverlays],
  ['BookingPages', BookingPages],
  ['MyBookings', MyBookings],
  ['CalendarSettings', CalendarSettings],
];

function renderPage(Page) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/admin/7/page']}>
        <Routes>
          <Route path="/admin/:communityId/page" element={<Page />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe.each(PAGES)('%s licensed-feature handling', (_name, Page) => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  it('renders the Upgrade to Premium state on a 402', async () => {
    mockState.failure = licensed402();
    renderPage(Page);
    expect(await screen.findByText('Upgrade to Premium for this feature')).toBeInTheDocument();
  });

  it('does not show the upgrade state for a non-402 failure', async () => {
    mockState.failure = { response: { status: 500, data: { error: { code: 'X', message: 'boom' } } } };
    renderPage(Page);
    await waitFor(() => expect(screen.queryByText(/loading/i)).not.toBeInTheDocument());
    expect(screen.queryByText('Upgrade to Premium for this feature')).not.toBeInTheDocument();
  });
});
