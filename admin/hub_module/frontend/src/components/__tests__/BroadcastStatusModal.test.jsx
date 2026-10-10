/**
 * Tests for the announcement broadcast modal: history fetch, status badges,
 * platform selection, and the broadcast-then-refresh flow incl. failures.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import BroadcastStatusModal from '../BroadcastStatusModal';
import { adminApi } from '../../services/api';

vi.mock('../../services/api', () => ({
  adminApi: { getBroadcastStatus: vi.fn() },
}));

const TS = '2026-03-04T10:30:00Z';

function envelope(broadcasts, success = true) {
  return { data: { success, broadcasts } };
}

function mount(props = {}) {
  const onClose = vi.fn();
  const view = render(
    <BroadcastStatusModal
      isOpen
      onClose={onClose}
      announcementId={11}
      communityId={5}
      onBroadcast={undefined}
      {...props}
    />,
  );
  return { onClose, ...view };
}

beforeEach(() => {
  vi.clearAllMocks();
  adminApi.getBroadcastStatus.mockResolvedValue(envelope([]));
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('BroadcastStatusModal visibility and loading', () => {
  it('renders nothing and fetches nothing while closed', () => {
    const { container } = mount({ isOpen: false });
    expect(container).toBeEmptyDOMElement();
    expect(adminApi.getBroadcastStatus).not.toHaveBeenCalled();
  });

  it.each([
    ['announcementId', { announcementId: undefined }],
    ['communityId', { communityId: undefined }],
  ])('does not fetch history without a %s', (_label, override) => {
    mount(override);
    expect(adminApi.getBroadcastStatus).not.toHaveBeenCalled();
  });

  it('fetches history for the community + announcement when opened', async () => {
    mount();
    await screen.findByText('No broadcasts yet');
    expect(adminApi.getBroadcastStatus).toHaveBeenCalledWith(5, 11);
  });

  it('shows the empty state when the API reports success:false', async () => {
    adminApi.getBroadcastStatus.mockResolvedValue(envelope([{ platform: 'discord', status: 'success' }], false));
    mount();
    expect(await screen.findByText('No broadcasts yet')).toBeInTheDocument();
  });

  it('falls back to the empty state and logs when the history request fails', async () => {
    adminApi.getBroadcastStatus.mockRejectedValue(new Error('boom'));
    mount();
    expect(await screen.findByText('No broadcasts yet')).toBeInTheDocument();
    expect(console.error).toHaveBeenCalledWith('Failed to fetch broadcast status:', expect.any(Error));
  });

  it('treats a missing broadcasts array as empty', async () => {
    adminApi.getBroadcastStatus.mockResolvedValue({ data: { success: true } });
    mount();
    expect(await screen.findByText('No broadcasts yet')).toBeInTheDocument();
  });
});

describe('BroadcastStatusModal history table', () => {
  it('renders one row per broadcast with platform, target, status, time and error', async () => {
    adminApi.getBroadcastStatus.mockResolvedValue(
      envelope([
        { platform: 'discord', serverName: 'Penguin HQ', status: 'success', timestamp: TS },
        { platform: 'twitch', channelName: 'penguinz', status: 'failed', errorMessage: 'rate limited' },
        { platform: 'matrix', status: 'pending' },
        { platform: 'carrier-pigeon', serverName: 'Coop', status: 'weird' },
      ]),
    );
    mount();

    const table = await screen.findByRole('table');
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(4);

    const [discord, twitch, matrix, pigeon] = rows;
    expect(discord).toHaveTextContent('Discord');
    expect(discord).toHaveTextContent('Penguin HQ');
    expect(discord).toHaveTextContent('Success');
    expect(discord).toHaveTextContent(new Date(TS).toLocaleString());

    expect(twitch).toHaveTextContent('penguinz');
    expect(twitch).toHaveTextContent('Failed');
    expect(twitch).toHaveTextContent('rate limited');

    expect(matrix).toHaveTextContent('Pending');
    expect(matrix).toHaveTextContent('-');

    expect(pigeon).toHaveTextContent('carrier-pigeon');
    expect(pigeon).toHaveTextContent('Unknown');
  });
});

describe('BroadcastStatusModal new broadcast', () => {
  // These cases start from one prior broadcast.
  beforeEach(() => {
    adminApi.getBroadcastStatus.mockResolvedValue(
      envelope([{ platform: 'twitch', serverName: 'Prior Channel', status: 'success' }]),
    );
  });

  it('offers every platform except the hub itself', async () => {
    mount();
    await screen.findByText('Prior Channel');
    expect(screen.getAllByRole('checkbox')).toHaveLength(9);
    expect(screen.queryByText('Hub Chat')).not.toBeInTheDocument();
    expect(screen.getAllByText('Discord')).toHaveLength(2);
  });

  // Regression: the platform picker used to render only once there was
  // history (or a selection, which itself needs the picker), so a first
  // broadcast could never be sent from this modal.
  it('offers the platform picker even before any broadcast has been sent', async () => {
    adminApi.getBroadcastStatus.mockResolvedValue(envelope([]));
    mount();
    await screen.findByText('No broadcasts yet');
    expect(screen.getAllByRole('checkbox').length).toBeGreaterThan(0);
  });

  it('keeps Broadcast Now disabled until a platform is picked, and toggles selection off again', async () => {
    mount();
    await screen.findByText('Prior Channel');
    const button = screen.getByRole('button', { name: 'Broadcast Now' });
    const [first] = screen.getAllByRole('checkbox');
    expect(button).toBeDisabled();

    fireEvent.click(first);
    expect(first).toBeChecked();
    expect(button).toBeEnabled();

    fireEvent.click(first);
    expect(first).not.toBeChecked();
    expect(button).toBeDisabled();
  });

  it('broadcasts to the selected platforms, refreshes history and clears the selection', async () => {
    const onBroadcast = vi.fn().mockResolvedValue(undefined);
    mount({ onBroadcast });
    await screen.findByText('Prior Channel');

    const boxes = screen.getAllByRole('checkbox');
    fireEvent.click(boxes[0]);
    fireEvent.click(boxes[2]);
    adminApi.getBroadcastStatus.mockResolvedValue(
      envelope([{ platform: 'discord', serverName: 'Penguin HQ', status: 'success' }]),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Broadcast Now' }));

    await waitFor(() => expect(onBroadcast).toHaveBeenCalledTimes(1));
    expect(onBroadcast.mock.calls[0][0]).toHaveLength(2);
    expect(await screen.findByText('Penguin HQ')).toBeInTheDocument();
    expect(adminApi.getBroadcastStatus).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Broadcast Now' })).toBeDisabled());
    screen.getAllByRole('checkbox').forEach((box) => expect(box).not.toBeChecked());
  });

  it('shows a Broadcasting... state and blocks double submits while in flight', async () => {
    let release;
    const onBroadcast = vi.fn(() => new Promise((resolve) => { release = resolve; }));
    mount({ onBroadcast });
    await screen.findByText('Prior Channel');

    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Broadcast Now' }));

    const busy = await screen.findByRole('button', { name: 'Broadcasting...' });
    expect(busy).toBeDisabled();
    release();
    await screen.findByRole('button', { name: 'Broadcast Now' });
    expect(onBroadcast).toHaveBeenCalledTimes(1);
  });

  it('still refreshes history when no onBroadcast callback is supplied', async () => {
    mount();
    await screen.findByText('Prior Channel');
    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Broadcast Now' }));
    await waitFor(() => expect(adminApi.getBroadcastStatus).toHaveBeenCalledTimes(2));
  });

  it('logs a failed broadcast, re-enables the button and keeps the selection', async () => {
    const onBroadcast = vi.fn().mockRejectedValue(new Error('send failed'));
    mount({ onBroadcast });
    await screen.findByText('Prior Channel');

    const [first] = screen.getAllByRole('checkbox');
    fireEvent.click(first);
    fireEvent.click(screen.getByRole('button', { name: 'Broadcast Now' }));

    await waitFor(() => expect(console.error).toHaveBeenCalledWith('Broadcast failed:', expect.any(Error)));
    expect(await screen.findByRole('button', { name: 'Broadcast Now' })).toBeEnabled();
    expect(first).toBeChecked();
    expect(adminApi.getBroadcastStatus).toHaveBeenCalledTimes(1);
  });
});

describe('BroadcastStatusModal closing', () => {
  it('closes from both the header X and the footer button', async () => {
    const { onClose } = mount();
    await screen.findByText('No broadcasts yet');
    const buttons = screen.getAllByRole('button');

    fireEvent.click(buttons[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));

    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
