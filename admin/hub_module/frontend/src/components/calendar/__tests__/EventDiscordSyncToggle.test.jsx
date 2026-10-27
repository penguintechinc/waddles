import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import EventDiscordSyncToggle from '../EventDiscordSyncToggle';
import { adminApi } from '../../../services/api';
import { useFeatureFlag } from '../../../lib/useFeatureFlag';

vi.mock('../../../services/api', () => ({
  adminApi: { getEventDiscordSync: vi.fn(), setEventDiscordSync: vi.fn() },
}));
vi.mock('../../../lib/useFeatureFlag', () => ({ useFeatureFlag: vi.fn() }));

const state = (over = {}) => ({
  data: { event_sync_enabled: false, pairings_total: 1, pairings_enabled: 0, ...over },
});

beforeEach(() => {
  vi.clearAllMocks();
  useFeatureFlag.mockReturnValue(true);
});

describe('EventDiscordSyncToggle', () => {
  it('renders nothing and never calls the API when the flag is off', () => {
    useFeatureFlag.mockReturnValue(false);
    const { container } = render(<EventDiscordSyncToggle communityId="7" />);
    expect(container).toBeEmptyDOMElement();
    expect(adminApi.getEventDiscordSync).not.toHaveBeenCalled();
    expect(useFeatureFlag).toHaveBeenCalledWith('waddles.calendar.event_discord_sync');
  });

  it('loads state and enables sync via the admin endpoint', async () => {
    adminApi.getEventDiscordSync.mockResolvedValue(state());
    adminApi.setEventDiscordSync.mockResolvedValue(state({ event_sync_enabled: true, pairings_enabled: 1 }));
    render(<EventDiscordSyncToggle communityId="7" />);
    const box = await screen.findByLabelText('Event sync to Discord');
    await waitFor(() => expect(box).not.toBeDisabled());
    expect(box).not.toBeChecked();
    fireEvent.click(box);
    await waitFor(() => expect(adminApi.setEventDiscordSync).toHaveBeenCalledWith('7', true));
    await waitFor(() => expect(box).toBeChecked());
  });

  it('disables the control when the community has no Discord pairing', async () => {
    adminApi.getEventDiscordSync.mockResolvedValue(state({ pairings_total: 0 }));
    render(<EventDiscordSyncToggle communityId="7" />);
    expect(await screen.findByText(/Pair a Discord server first/)).toBeInTheDocument();
    expect(screen.getByLabelText('Event sync to Discord')).toBeDisabled();
  });

  it('surfaces an error when the update fails', async () => {
    adminApi.getEventDiscordSync.mockResolvedValue(state());
    adminApi.setEventDiscordSync.mockRejectedValue({ response: { data: { error: { message: 'nope' } } } });
    render(<EventDiscordSyncToggle communityId="7" />);
    const box = await screen.findByLabelText('Event sync to Discord');
    await waitFor(() => expect(box).not.toBeDisabled());
    fireEvent.click(box);
    expect(await screen.findByRole('alert')).toHaveTextContent('nope');
  });
});
