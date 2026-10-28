/**
 * Tests for the admin calendar events page (`/admin/:communityId/calendar/events`):
 * list, empty, load-error and create flows against the mocked admin API --
 * including the regression where an object-shaped `error` envelope was rendered
 * directly (React "Objects are not valid as a child" crash).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import AdminCalendarEvents, { apiErrorMessage } from '../AdminCalendarEvents';
import { adminApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  adminApi: {
    getCalendarEvents: vi.fn(),
    createCalendarEvent: vi.fn(),
    updateCalendarEvent: vi.fn(),
    deleteCalendarEvent: vi.fn(),
    approveCalendarEvent: vi.fn(),
    rejectCalendarEvent: vi.fn(),
  },
}));
vi.mock('../../../components/calendar/EventDiscordSyncToggle', () => ({
  default: () => <div data-testid="sync-toggle-stub" />,
}));
vi.mock('../../../theme/waddlebotTheme', () => ({ WADDLES_COLORS: {} }));
// react-libs dist is a directory-import ESM package vitest can't resolve -- stub the modal.
vi.mock('@penguintechinc/react-libs', () => ({
  FormModalBuilder: ({ isOpen, onSubmit, title }) =>
    isOpen ? (
      <div role="dialog" aria-label={title}>
        <button
          type="button"
          onClick={() => onSubmit({ title: ' Bar Night ', start_date: '2026-10-20T19:00' }).catch(() => {})}
        >
          submit-stub
        </button>
      </div>
    ) : null,
}));

const api = vi.mocked(adminApi);

const EVENT = {
  id: 11,
  title: 'Bar Citizen Meetup',
  location: 'Discord',
  start_date: '2026-10-20T19:00:00Z',
  status: 'approved',
  category: 'meetup',
  rsvp_count: 3,
  max_attendees: 10,
};

const mount = () =>
  render(
    <MemoryRouter initialEntries={['/admin/1/calendar/events']}>
      <Routes>
        <Route path="/admin/:communityId/calendar/events" element={<AdminCalendarEvents />} />
      </Routes>
    </MemoryRouter>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  api.getCalendarEvents.mockResolvedValue({ data: { events: [EVENT] } });
});

describe('AdminCalendarEvents', () => {
  it('lists events from the community-scoped endpoint with the sync toggle', async () => {
    mount();
    expect(await screen.findByText('Bar Citizen Meetup')).toBeInTheDocument();
    expect(api.getCalendarEvents).toHaveBeenCalledWith('1');
    expect(screen.getByText('3 / 10')).toBeInTheDocument();
    expect(screen.getByTestId('sync-toggle-stub')).toBeInTheDocument();
  });

  it('shows the empty state when there are no events', async () => {
    api.getCalendarEvents.mockResolvedValue({ data: { events: [] } });
    mount();
    expect(await screen.findByText(/No events yet/)).toBeInTheDocument();
  });

  it('shows a load error instead of the empty state', async () => {
    api.getCalendarEvents.mockRejectedValue(new Error('boom'));
    mount();
    expect(await screen.findByText('Failed to load events')).toBeInTheDocument();
    expect(screen.queryByText(/No events yet/)).not.toBeInTheDocument();
  });

  it('creates an event with trimmed fields and refreshes the list', async () => {
    api.createCalendarEvent.mockResolvedValue({ data: {} });
    mount();
    await screen.findByText('Bar Citizen Meetup');
    fireEvent.click(screen.getByRole('button', { name: /Create Event/ }));
    fireEvent.click(await screen.findByText('submit-stub'));
    await waitFor(() =>
      expect(api.createCalendarEvent).toHaveBeenCalledWith(
        '1',
        expect.objectContaining({ title: 'Bar Night', start_date: '2026-10-20T19:00', category: 'other' }),
      ),
    );
    expect(await screen.findByText('Event created')).toBeInTheDocument();
    expect(api.getCalendarEvents).toHaveBeenCalledTimes(2);
  });

  it('renders an object-shaped error envelope as text on create failure (no crash)', async () => {
    api.createCalendarEvent.mockRejectedValue({
      response: { data: { success: false, error: { code: 'INTERNAL_ERROR', message: 'calendar down' } } },
    });
    mount();
    await screen.findByText('Bar Citizen Meetup');
    fireEvent.click(screen.getByRole('button', { name: /Create Event/ }));
    fireEvent.click(await screen.findByText('submit-stub'));
    expect(await screen.findByText(/calendar down/)).toBeInTheDocument();
  });
});

describe('apiErrorMessage', () => {
  it('handles string, object, and missing envelopes', () => {
    expect(apiErrorMessage({ response: { data: { error: 'plain' } } }, 'fb')).toBe('plain');
    expect(apiErrorMessage({ response: { data: { error: { message: 'm' } } } }, 'fb')).toBe('m');
    expect(apiErrorMessage(new Error('x'), 'fb')).toBe('fb');
  });
});
