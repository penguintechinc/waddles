/**
 * Admin community calls page against the served hub-api routes: rooms by label (never ids), moderator hands queue
 * (ordered, acknowledge/reset), participants by name, flag-off (402) and retryable 503 states, errors as errors.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import AdminCommunityCalls from '../AdminCommunityCalls';
import { renderWithProviders } from '../../../test/renderWithProviders';
import {
  ADA,
  GRACE,
  OTHER_ROOM_ID,
  ROOM_ID,
  envelope,
  handWire,
  httpError,
  participantWire,
  roomWire,
  serve,
} from '../../../test/callsFixtures';

const BASE = '/api/v1/admin/7/calls/rooms';

function renderPage(route = '/admin/7/calls') {
  return renderWithProviders(<AdminCommunityCalls />, { route, path: '/admin/:communityId/calls' });
}

const roomsOk = { success: true, rooms: [roomWire(), roomWire({ room_id: OTHER_ROOM_ID, label: null, locked: true, publisher_ids: [], subscriber_count: 0 })] };
const participantsOk = {
  success: true,
  locked: false,
  participants: [participantWire(ADA, 'Ada Lovelace'), participantWire(GRACE, 'Grace Hopper', { muted: true, publisher: false, subscriber: true })],
};
const handsOk = {
  success: true,
  count: 2,
  raised_hands: [handWire(GRACE, 'Grace Hopper', 1760000200), handWire(ADA, 'Ada Lovelace', 1760000300)],
};

const detailRoutes = {
  [`GET ${BASE}`]: roomsOk,
  [`GET ${BASE}/${ROOM_ID}/participants`]: participantsOk,
  [`GET ${BASE}/${ROOM_ID}/raised-hands`]: handsOk,
};

describe('AdminCommunityCalls', () => {
  beforeEach(() => vi.spyOn(console, 'error').mockImplementation(() => {}));
  afterEach(() => vi.restoreAllMocks());

  it('lists rooms by label and never shows a room id', async () => {
    serve({ [`GET ${BASE}`]: roomsOk });
    const { container } = renderPage();
    expect(await screen.findByText('Town hall')).toBeInTheDocument();
    expect(screen.getByText('Untitled room')).toBeInTheDocument();
    expect(screen.getByText('3 participants')).toBeInTheDocument();
    expect(screen.getByText('Locked')).toBeInTheDocument();
    expect(container.textContent).not.toContain(ROOM_ID);
    expect(container.textContent).not.toContain(OTHER_ROOM_ID);
    expect(screen.getByText(/Select a room to see who is in it/)).toBeInTheDocument();
  });

  it('shows a calm empty state for a community with no rooms', async () => {
    serve({ [`GET ${BASE}`]: { success: true, rooms: [] } });
    renderPage();
    expect(await screen.findByTestId('calls-no-rooms')).toHaveTextContent('No active rooms');
  });

  it('shows the moderator view: hands in queue order with names, participants by name, no ids', async () => {
    serve(detailRoutes);
    const { container } = renderPage();
    fireEvent.click(await screen.findByRole('button', { name: /^Town hall/ }));
    const queue = await screen.findByTestId('hands-queue');
    const items = within(queue).getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('1.Grace Hopper');
    expect(items[1]).toHaveTextContent('2.Ada Lovelace');
    expect(within(queue).getByText('Raised hands (2)')).toBeInTheDocument();
    const participants = await screen.findByRole('list', { name: 'Participants' });
    expect(within(participants).getByText('Ada Lovelace')).toBeInTheDocument();
    expect(within(participants).getByText('Muted')).toBeInTheDocument();
    for (const id of [ADA, GRACE, ROOM_ID]) expect(container.textContent).not.toContain(id);
  });

  it('acknowledges a hand with the participant UUID, then refreshes the queue', async () => {
    let acknowledged = false;
    const { post } = serve({
      ...detailRoutes,
      [`GET ${BASE}/${ROOM_ID}/raised-hands`]: () => (acknowledged ? { success: true, count: 1, raised_hands: [handWire(ADA, 'Ada Lovelace', 1760000300)] } : handsOk),
      [`POST ${BASE}/${ROOM_ID}/acknowledge-hand`]: () => {
        acknowledged = true;
        return { success: true, hand: handWire(GRACE, 'Grace Hopper', 1760000200, { acknowledged_at: 1760000400, acknowledged_by: ADA }) };
      },
    });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: /^Town hall/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Acknowledge Grace Hopper' }));
    await waitFor(() => expect(screen.getByText('Raised hands (1)')).toBeInTheDocument());
    expect(post).toHaveBeenCalledWith(`${BASE}/${ROOM_ID}/acknowledge-hand`, { user_id: GRACE });
    expect(screen.getByText('Acknowledged Grace Hopper.')).toBeInTheDocument();
  });

  it('clears the whole queue and reports how many hands were cleared', async () => {
    const { post } = serve({
      ...detailRoutes,
      [`POST ${BASE}/${ROOM_ID}/reset-hands`]: { success: true, cleared: 2 },
    });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: /^Town hall/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Clear all hands' }));
    expect(await screen.findByText('Cleared 2 hands.')).toBeInTheDocument();
    expect(post).toHaveBeenCalledWith(`${BASE}/${ROOM_ID}/reset-hands`);
  });

  it('mutes, unmutes and removes participants and mutes all, by UUID', async () => {
    const { post } = serve({
      ...detailRoutes,
      [`POST ${BASE}/${ROOM_ID}/mute`]: { success: true, participant_id: ADA, muted: true },
      [`POST ${BASE}/${ROOM_ID}/unmute`]: { success: true, participant_id: GRACE, muted: false },
      [`POST ${BASE}/${ROOM_ID}/kick`]: { success: true, message: 'Participant removed' },
      [`POST ${BASE}/${ROOM_ID}/mute-all`]: { success: true, muted: 1, message: 'All participants muted' },
    });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: /^Town hall/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Mute Ada Lovelace' }));
    await waitFor(() => expect(post).toHaveBeenCalledWith(`${BASE}/${ROOM_ID}/mute`, { identity: ADA }));
    fireEvent.click(await screen.findByRole('button', { name: 'Unmute Grace Hopper' }));
    await waitFor(() => expect(post).toHaveBeenCalledWith(`${BASE}/${ROOM_ID}/unmute`, { identity: GRACE }));
    fireEvent.click(screen.getByRole('button', { name: 'Remove Ada Lovelace from the room' }));
    await waitFor(() => expect(post).toHaveBeenCalledWith(`${BASE}/${ROOM_ID}/kick`, { identity: ADA }));
    expect(await screen.findByText('Removed Ada Lovelace from the room.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Mute all' }));
    expect(await screen.findByText('Muted 1 speaker.')).toBeInTheDocument();
  });

  it('creates a room with room_name, then selects it', async () => {
    let created = false;
    const { post } = serve({
      ...detailRoutes,
      [`GET ${BASE}`]: () => (created ? roomsOk : { success: true, rooms: [] }),
      [`POST ${BASE}`]: () => {
        created = true;
        return roomWire();
      },
    });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Create room' }));
    fireEvent.change(screen.getByLabelText(/Room name/), { target: { value: '  Town hall  ' } });
    fireEvent.change(screen.getByLabelText(/Max participants/), { target: { value: '40' } });
    fireEvent.click(within(screen.getByRole('region', { name: 'Create a room' })).getByRole('button', { name: 'Create room' }));
    expect(await screen.findByText('Room created.')).toBeInTheDocument();
    expect(post).toHaveBeenCalledWith(BASE, { room_name: 'Town hall', max_participants: 40 });
    expect(await screen.findByTestId('room-detail')).toBeInTheDocument();
  });

  it('shows the server message when creating a room fails, then cancels the form and dismisses notices', async () => {
    serve({ [`GET ${BASE}`]: roomsOk, [`POST ${BASE}`]: httpError(400, envelope('INVALID', 'Room name is too long')) });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Create room' }));
    fireEvent.change(screen.getByLabelText(/Room name/), { target: { value: 'x' } });
    fireEvent.click(within(screen.getByRole('region', { name: 'Create a room' })).getByRole('button', { name: 'Create room' }));
    expect(await screen.findByText('Room name is too long')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss message' }));
    expect(screen.queryByText('Room name is too long')).not.toBeInTheDocument();
    fireEvent.click(within(screen.getByRole('region', { name: 'Create a room' })).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('region', { name: 'Create a room' })).not.toBeInTheDocument();
  });

  it('deleting the selected room clears the detail pane and dismisses the success notice', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    let deleted = false;
    serve({
      ...detailRoutes,
      [`GET ${BASE}`]: () => (deleted ? { success: true, rooms: [] } : roomsOk),
      [`DELETE ${BASE}/${ROOM_ID}`]: () => {
        deleted = true;
        return { success: true, message: 'Room deleted' };
      },
    });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: /^Town hall/ }));
    await screen.findByTestId('room-detail');
    fireEvent.click(screen.getByRole('button', { name: 'Delete room Town hall' }));
    expect(await screen.findByText('Room deleted.')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId('room-detail')).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss message' }));
    expect(screen.queryByText('Room deleted.')).not.toBeInTheDocument();
  });

  it('locks and deletes a room (after confirmation)', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { post, del } = serve({
      ...detailRoutes,
      [`POST ${BASE}/${ROOM_ID}/lock`]: { success: true, message: 'Room locked' },
      [`DELETE ${BASE}/${ROOM_ID}`]: { success: true, message: 'Room deleted' },
    });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Lock room Town hall' }));
    expect(await screen.findByText('Room locked.')).toBeInTheDocument();
    expect(post).toHaveBeenCalledWith(`${BASE}/${ROOM_ID}/lock`);
    fireEvent.click(screen.getByRole('button', { name: 'Delete room Town hall' }));
    expect(await screen.findByText('Room deleted.')).toBeInTheDocument();
    expect(del).toHaveBeenCalledWith(`${BASE}/${ROOM_ID}`);
  });

  it('does not delete when the confirmation is declined', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    const { del } = serve(detailRoutes);
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete room Town hall' }));
    expect(del).not.toHaveBeenCalled();
  });

  it('shows the server message when an action is refused, reading error.message', async () => {
    serve({
      ...detailRoutes,
      [`POST ${BASE}/${ROOM_ID}/mute-all`]: httpError(403, envelope('FORBIDDEN', 'Moderator scope required')),
    });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: /^Town hall/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Mute all' }));
    const banner = await screen.findByText('Moderator scope required');
    expect(banner.closest('[role="alert"]')).toHaveTextContent('You do not have permission to do that');
  });

  describe('failure states', () => {
    it('flag off (402) shows a clear "not enabled for this community" state and no create button', async () => {
      serve({ [`GET ${BASE}`]: httpError(402, envelope('FEATURE_NOT_ENABLED', 'Calls / RTC is not enabled for this plan')) });
      renderPage();
      const state = await screen.findByTestId('calls-feature-disabled');
      expect(state).toHaveTextContent('Calls are not enabled for this community');
      expect(screen.queryByRole('button', { name: 'Create room' })).not.toBeInTheDocument();
      expect(screen.queryByTestId('calls-no-rooms')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument();
    });

    it('503 shows the server message with a retry that recovers', async () => {
      let healthy = false;
      const { get } = serve({
        [`GET ${BASE}`]: () => (healthy ? roomsOk : httpError(503, envelope('STREAMING_UNAVAILABLE', 'Streaming service unavailable', { retryable: true }))),
      });
      renderPage();
      const state = await screen.findByTestId('calls-unavailable');
      expect(state).toHaveTextContent('The calls service is unavailable');
      expect(state).toHaveTextContent('Streaming service unavailable');
      healthy = true;
      fireEvent.click(within(state).getByRole('button', { name: 'Try again' }));
      expect(await screen.findByText('Town hall')).toBeInTheDocument();
      expect(get).toHaveBeenCalledTimes(2);
    });

    it('a 404 or 500 on the room list is an error state, never "No active rooms"', async () => {
      serve({ [`GET ${BASE}`]: httpError(404, envelope('NOT_FOUND', 'Community not found')) });
      renderPage();
      expect(await screen.findByTestId('calls-error')).toHaveTextContent('Community not found');
      expect(screen.queryByTestId('calls-no-rooms')).not.toBeInTheDocument();
    });

    it('a drifted response shape is an error state', async () => {
      serve({ [`GET ${BASE}`]: { rooms: [{ name: 'legacy' }] } });
      renderPage();
      expect(await screen.findByTestId('calls-error')).toBeInTheDocument();
      expect(screen.queryByTestId('calls-no-rooms')).not.toBeInTheDocument();
    });

    it('shows errors for the hands and participants panes separately', async () => {
      serve({
        [`GET ${BASE}`]: roomsOk,
        [`GET ${BASE}/${ROOM_ID}/participants`]: httpError(503, envelope('STREAMING_UNAVAILABLE', 'Rooms backend down', { retryable: true })),
        [`GET ${BASE}/${ROOM_ID}/raised-hands`]: httpError(404, envelope('ROOM_NOT_FOUND', 'Room not found')),
      });
      renderPage();
      fireEvent.click(await screen.findByRole('button', { name: /^Town hall/ }));
      expect(await screen.findByText('Rooms backend down')).toBeInTheDocument();
      expect(await screen.findByText('Room not found')).toBeInTheDocument();
      expect(screen.queryByTestId('hands-queue')).not.toBeInTheDocument();
    });
  });

  it('rejects an invalid community id without calling the API', () => {
    const { get } = serve({});
    renderPage('/admin/abc/calls');
    expect(screen.getByText('This calls link is not valid.')).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });
});
