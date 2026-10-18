import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';

import EventDiscordSyncToggle from '../EventDiscordSyncToggle';
import { eventSyncApi, type EventSyncState } from '../../../services/eventSyncApi';
import { useFeatureFlag } from '../../../lib/useFeatureFlag';

vi.mock('../../../services/eventSyncApi', () => ({
  eventSyncApi: { get: vi.fn(), set: vi.fn() },
}));
vi.mock('../../../lib/useFeatureFlag', () => ({ useFeatureFlag: vi.fn() }));

const api = vi.mocked(eventSyncApi);
const flag = vi.mocked(useFeatureFlag);

const state = (over: Partial<EventSyncState> = {}): EventSyncState => ({
  event_sync_enabled: false,
  pairings_total: 1,
  pairings_enabled: 0,
  ...over,
});

function wrap({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const mount = () => render(<EventDiscordSyncToggle communityId="7" />, { wrapper: wrap });

beforeEach(() => {
  vi.clearAllMocks();
  flag.mockReturnValue(true);
});

describe('EventDiscordSyncToggle', () => {
  it('renders nothing and never calls the API when the flag is off', () => {
    flag.mockReturnValue(false);
    const { container } = mount();
    expect(container).toBeEmptyDOMElement();
    expect(api.get).not.toHaveBeenCalled();
    expect(flag).toHaveBeenCalledWith('waddles.calendar.event_discord_sync');
  });

  it('loads state and enables sync via the admin endpoint', async () => {
    const enabled = state({ event_sync_enabled: true, pairings_enabled: 1 });
    // First read is the initial load; the post-mutation invalidation refetch sees the saved state.
    api.get.mockResolvedValueOnce(state()).mockResolvedValue(enabled);
    api.set.mockResolvedValue(enabled);
    mount();
    const box = await screen.findByLabelText('Event sync to Discord');
    await waitFor(() => expect(box).not.toBeDisabled());
    expect(box).not.toBeChecked();
    fireEvent.click(box);
    await waitFor(() => expect(api.set).toHaveBeenCalledWith('7', true));
    await waitFor(() => expect(box).toBeChecked());
  });

  it('disables the control when the community has no Discord pairing', async () => {
    api.get.mockResolvedValue(state({ pairings_total: 0 }));
    mount();
    expect(await screen.findByText(/Pair a Discord server first/)).toBeInTheDocument();
    expect(screen.getByLabelText('Event sync to Discord')).toBeDisabled();
  });

  it('rolls back the optimistic update and shows the server error when PUT fails', async () => {
    api.get.mockResolvedValue(state());
    api.set.mockRejectedValue({ response: { data: { error: { message: 'nope' } } } });
    mount();
    const box = await screen.findByLabelText('Event sync to Discord');
    await waitFor(() => expect(box).not.toBeDisabled());
    fireEvent.click(box);
    expect(await screen.findByRole('alert')).toHaveTextContent('nope');
    await waitFor(() => expect(box).not.toBeChecked());
  });

  it('shows a real error (not a silent no-op) when the initial load fails', async () => {
    api.get.mockRejectedValue({ response: { data: { error: { message: 'Discord event sync is not enabled' } } } });
    mount();
    expect(await screen.findByRole('alert')).toHaveTextContent('Discord event sync is not enabled');
    expect(screen.getByLabelText('Event sync to Discord')).toBeDisabled();
  });

  it('falls back to a generic message when the error has no envelope', async () => {
    api.get.mockRejectedValue(new Error('network'));
    mount();
    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load Discord event sync state');
  });
});
